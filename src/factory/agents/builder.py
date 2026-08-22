"""The Builder worker: turns a filed requirement into a pull request.

Runs LOCALLY by design — the operator decided no Anthropic credentials leave this
machine, so instead of a cloud runner this shells out to the locally-authenticated
Claude Code CLI (`claude -p`, headless) in a scratch clone of the repo. Claude only
edits files; branch/commit/push/PR mechanics stay in deterministic code here, which
also guarantees the PR-body contract (`Requirement: req_<id>` on the first line)
that deploy.yml parses to flip the requirement entity on merge.

The deterministic CI gate on the PR and the human merge remain the promotion gates;
the Builder never merges.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import port, telemetry

REPO = "Nitishmane/scraper-factory"
CLAUDE_TIMEOUT_S = 1200  # a small feature; if Claude needs >20min something is wrong

# Files the Builder must never touch — the same invariants CLAUDE.md pins.
FORBIDDEN = ("fixtures/", "src/factory/agents/verifier.py", "src/factory/detect.py")

PROMPT = """Read CLAUDE.md first — it is the repo contract.

Implement this requirement:
Title: {title}
Kind: {kind}
Details:
{details}

Hard rules:
- NEVER modify anything under fixtures/, src/factory/agents/verifier.py, or the drift
  thresholds in src/factory/detect.py.
- Keep the diff small and focused on the requirement; match surrounding code style.
- Only read and edit files. Do NOT run git, gh, npm install, or any network command.
- When done, write BUILDER_SUMMARY.md in the repo root: a short plain-text summary of
  what you changed and why (it becomes the PR description; it is removed before commit).
"""


def build_feature(req_id: str, title: str, details: str, kind: str, run_id: str | None) -> None:
    """Background task: clone -> Claude edits -> branch/commit/push -> PR -> Port status."""
    with telemetry.tracer().start_as_current_span("builder.feature") as span:
        span.set_attribute("requirement.id", req_id)
        span.set_attribute("requirement.kind", kind)
        workdir = Path(tempfile.mkdtemp(prefix=f"builder-{req_id}-"))
        try:
            pr_url = _run(req_id, title, details, kind, workdir)
        except Exception as exc:
            telemetry.log().warning("builder failed for %s: %s", req_id, exc)
            span.set_attribute("builder.error", str(exc)[:200])
            _status(req_id, title, details, kind, "failed")
            if run_id:
                port.patch_run(run_id, success=False, message=f"builder failed: {exc}")
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        _status(req_id, title, details, kind, "in_review",
                branch=f"req/{req_id}", pr_url=pr_url)
        if run_id:
            port.patch_run(run_id, success=True, message=f"PR opened: {pr_url}", link=pr_url)
        telemetry.log().info("builder opened %s for %s", pr_url, req_id)


def _run(req_id: str, title: str, details: str, kind: str, workdir: Path) -> str:
    branch = f"req/{req_id}"
    _sh(["gh", "repo", "clone", REPO, str(workdir), "--", "--depth", "1"], cwd=None)
    _sh(["git", "checkout", "-b", branch], cwd=workdir)

    prompt = PROMPT.format(title=title, kind=kind, details=details)
    _sh(
        ["claude", "-p", prompt,
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Write,Edit,Glob,Grep"],
        cwd=workdir, timeout=CLAUDE_TIMEOUT_S,
    )

    summary_file = workdir / "BUILDER_SUMMARY.md"
    summary = summary_file.read_text().strip() if summary_file.exists() else "(no summary)"
    summary_file.unlink(missing_ok=True)

    changed = _sh(["git", "status", "--porcelain"], cwd=workdir).strip()
    if not changed:
        raise RuntimeError("Claude produced no changes")
    touched = [line[3:] for line in changed.splitlines()]
    bad = [f for f in touched if f.startswith(FORBIDDEN[0]) or f in FORBIDDEN[1:]]
    if bad:
        raise RuntimeError(f"builder touched forbidden files: {bad}")

    _sh(["git", "add", "-A"], cwd=workdir)
    _sh(["git", "commit", "-m", f"Builder: {title}\n\nRequirement: {req_id}\n\n"
         "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"], cwd=workdir)
    _sh(["git", "push", "-u", "origin", branch], cwd=workdir)
    body = f"Requirement: {req_id}\n\n{summary}\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    out = _sh(["gh", "pr", "create", "--title", f"[builder] {title}",
               "--body", body, "--head", branch, "--base", "main"], cwd=workdir)
    match = re.search(r"https://github\.com/\S+/pull/\d+", out)
    if not match:
        raise RuntimeError(f"gh pr create gave no PR url: {out[-200:]}")
    return match.group(0)


def _status(req_id: str, title: str, details: str, kind: str, status: str, **extra: str) -> None:
    """Requirement-entity writes never raise — mirrors the health-write rule."""
    try:
        port.upsert_entity(
            "requirement",
            identifier=req_id,
            title=title,
            properties={"description": details, "kind": kind, "status": status, **extra},
        )
    except Exception as exc:
        telemetry.log().warning("could not write requirement %s: %s", req_id, exc)


def _sh(cmd: list[str], cwd: Path | None, timeout: int = 120) -> str:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout
