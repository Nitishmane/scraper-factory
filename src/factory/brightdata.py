"""Thin wrapper around the Bright Data CLI.

The CLI is the interface the event asks for -- terminal commands, no browser dashboard.
Every call is traced so the demo can show the factory's use of Scraper Studio in SigNoz.

Command name: docs use `bdata`, the GitHub README uses `brightdata`. Set BRIGHTDATA_BIN
to whichever your install exposes (check `brightdata --help` / `bdata --help`).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from . import telemetry

BIN = os.getenv("BRIGHTDATA_BIN", "brightdata")
TIMEOUT = int(os.getenv("BRIGHTDATA_TIMEOUT", "180"))


class BrightDataError(RuntimeError):
    pass


class ProxyConfigError(BrightDataError):
    """The collector runtime's proxy zone refused the target (statusCode=403).

    Live domains hit this when the account's scraping zone is missing/unbound; tunneled
    pages pass. The pipeline falls back to relay mode: fetch via `brightdata scrape`
    (separate zone, works), serve through the fixture tunnel, extract with the collector.
    """


@dataclass
class HealProposal:
    """Response envelope from `scraper heal`: status=awaiting_approval + preview_result."""

    collector_id: str
    status: str
    preview_result: list[dict[str, Any]]

    @property
    def awaiting_approval(self) -> bool:
        return self.status == "awaiting_approval"


def _run(args: list[str], parse_json: bool = True) -> Any:
    cmd = [BIN, *args]
    with telemetry.tracer().start_as_current_span("brightdata.cli") as span:
        span.set_attribute("cli.command", " ".join(args[:2]))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        span.set_attribute("cli.exit_code", proc.returncode)
        if proc.returncode != 0:
            telemetry.log().error("bright data cli failed: %s", proc.stderr.strip())
            raise BrightDataError(f"{' '.join(cmd)} -> {proc.returncode}: {proc.stderr.strip()}")
        if not parse_json:
            return proc.stdout
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise BrightDataError(f"non-JSON output from {' '.join(cmd)}: {exc}") from exc


def create(url: str, description: str) -> str:
    """Create a scraper from a SEMANTIC description. Returns the collector_id."""
    out = _run(["scraper", "create", url, description])
    collector_id = _dig(out, "collector_id") or _dig(out, "id")
    if not collector_id:
        raise BrightDataError(f"no collector_id in create response: {out!r}")
    telemetry.log().info("created scraper %s for %s", collector_id, url)
    return collector_id


def run(collector_id: str, urls: list[str]) -> list[dict[str, Any]]:
    """Execute a scraper. Returns data rows only; error rows are logged and dropped.

    Raises ProxyConfigError when the runtime's proxy zone rejects the target outright
    (no data at all) so callers can fall back to relay mode.
    """
    out = _run(["scraper", "run", collector_id, "--urls", ",".join(urls), "--pretty"])
    rows = _rows(out)
    errors = [r for r in rows if r.get("error")]
    good = [r for r in rows if not r.get("error")]
    if errors and not good and any(r.get("error_code") == "proxy_config" for r in errors):
        raise ProxyConfigError(errors[0].get("error", "proxy_config"))
    if errors:
        telemetry.log().warning(
            "collector %s returned %d error rows (kept %d data rows): %s",
            collector_id, len(errors), len(good), errors[0].get("error"),
        )
    return good


def heal(collector_id: str, drift_description: str) -> HealProposal:
    """Ask Scraper Studio to repair a drifted scraper.

    Stops at the approval gate by default and returns a preview -- which the Verifier
    diffs against the frozen fixture before anything is promoted.
    """
    telemetry.metric("heal_attempts").add(1, {"scraper.id": collector_id})
    telemetry.log().info("healing %s: %s", collector_id, drift_description)
    out = _run(["scraper", "heal", collector_id, drift_description])
    return HealProposal(
        collector_id=collector_id,
        status=_dig(out, "status") or "unknown",
        preview_result=_rows(_dig(out, "preview_result") or []),
    )


def approve(collector_id: str, reject: bool = False) -> None:
    """Promote or roll back a proposed fix."""
    args = ["scraper", "approve", collector_id] + (["--reject"] if reject else [])
    _run(args, parse_json=False)
    telemetry.log().info("%s %s", "rejected" if reject else "approved", collector_id)


def capture_fixture(url: str, out_path: str) -> None:
    """Freeze a page's HTML as a verification fixture."""
    _run(["scrape", url, "-f", "html", "-o", out_path], parse_json=False)


def fetch_html(url: str) -> str:
    """Fetch a page's HTML through Bright Data (plain scrape, no collector)."""
    return _run(["scrape", url, "-f", "html"], parse_json=False)


def budget() -> float | None:
    """Remaining Bright Data account balance in USD, or None if it can't be read.

    Parses `brightdata budget balance --json`. The balance endpoint needs an account-scoped
    token; a scraping-only key gets a 403 and the CLI prints a human-readable error to stdout
    with exit 0 (not JSON) -- that, and any parse failure, yields None rather than raising.
    Never raises: a budget read must never break a scrape or the startup path that emits it.
    """
    try:
        proc = subprocess.run(
            [BIN, "budget", "balance", "--json"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # 403 / permission errors print prose to stdout, not JSON.
            telemetry.log().warning("brightdata budget unreadable: %s", out[:200])
            return None
        return _dig_balance(data)
    except Exception as exc:  # missing binary, timeout, etc. -- never fatal
        telemetry.log().warning("brightdata budget read failed: %s", exc)
        return None


def _dig_balance(data: Any) -> float | None:
    """Pull a numeric balance out of the budget JSON, whatever the exact key path.

    Bright Data's balance shape isn't stably documented; try the balance-ish keys and fall
    back to the first top-level number so a shape change degrades to a value, not a None.
    """
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, dict):
        for key in ("balance", "remaining", "amount", "available", "credit", "usd", "value"):
            val = data.get(key)
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, dict):  # e.g. {"balance": {"amount": 12.3}}
                nested = _dig_balance(val)
                if nested is not None:
                    return nested
        for val in data.values():  # last resort: first numeric anywhere in the object
            if isinstance(val, (int, float)):
                return float(val)
    return None


# Bright Data policy (confirmed by their support engineer): collector runs cannot reach
# streaming-media domains; the endorsed pattern is Web Unlocker fetch + collector extract.
# After the first rejection in a process, skip the doomed direct attempts.
_relay_sticky = False


def run_with_relay(collector_id: str, url: str) -> tuple[list[dict[str, Any]], str, bool]:
    """Run the collector on a URL, relaying when Bright Data policy blocks the domain.

    Relay is the Bright Data-endorsed pipeline for streaming-media targets: fetch the page
    via `brightdata scrape` (Web Unlocker), serve the HTML from fixtures/relay/ through
    the fixture tunnel, extract with the collector. Same vendor for fetch and extraction;
    different transport. Side effect: every relayed scrape's input HTML is archived --
    drift investigations get the exact bytes the collector saw.
    Returns (rows, "direct" | "relay", cache_hit). cache_hit is meaningful only in relay
    mode (direct never touches the relay cache, so it is always False there).
    """
    global _relay_sticky
    if not _relay_sticky:
        try:
            rows = run(collector_id, [url])
            telemetry.log().info("direct scrape of %s -> %d rows", url, len(rows))
            return rows, "direct", False
        except ProxyConfigError:
            _relay_sticky = True
            telemetry.log().info(
                "collector proxy rejected %s (Bright Data streaming-domain policy) -- "
                "relay mode for the rest of this process", url
            )
    rows, cache_hit = _run_relayed(collector_id, url)
    return rows, "relay", cache_hit


def _run_relayed(collector_id: str, url: str) -> tuple[list[dict[str, Any]], bool]:
    import re
    import time as _time
    from pathlib import Path

    base = os.getenv("FIXTURE_BASE_URL", "").rstrip("/")
    if not base:
        raise BrightDataError(
            "Bright Data policy blocks this domain for collector runs and "
            "FIXTURE_BASE_URL is not set -- start `make serve-fixtures` + a tunnel so "
            "relay mode can serve fetched pages"
        )
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower().split("//", 1)[-1]).strip("-")[:80]
    relay_dir = Path("fixtures/relay")
    relay_dir.mkdir(parents=True, exist_ok=True)
    relay_path = relay_dir / f"{slug}.html"
    cache_minutes = int(os.getenv("RELAY_CACHE_MINUTES", "30"))
    cache_hit = (
        relay_path.exists()
        and _time.time() - relay_path.stat().st_mtime < cache_minutes * 60
    )
    telemetry.metric("relay_cache").add(1, {"hit": cache_hit})
    if cache_hit:
        telemetry.log().info("relay cache HIT for %s (%s)", url, relay_path.name)
    else:
        telemetry.log().info("relay cache MISS for %s -- fetching fresh via Web Unlocker", url)
        relay_path.write_text(fetch_html(url))
    return run(collector_id, [f"{base}/relay/{slug}.html"]), cache_hit


# --- envelope helpers -------------------------------------------------------
# The CLI wraps payloads in an envelope whose exact shape varies by command and
# version, so dig rather than assume a path.

def _dig(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _dig(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _dig(item, key)
            if found is not None:
                return found
    return None


def _rows(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        out = []
        for r in obj:
            if isinstance(r, dict):
                # channel-schedule nests its cards: {programs: [...], product_page_url}.
                # Flatten to one row per program; previews may pad the list with
                # "N more items" strings, so keep dicts only.
                if isinstance(r.get("programs"), list):
                    out.extend(p for p in r["programs"] if isinstance(p, dict))
                else:
                    out.append(r)
        return out
    if isinstance(obj, dict):
        for key in ("data", "results", "rows", "result"):
            if isinstance(obj.get(key), list):
                return _rows(obj[key])
        return [obj]
    return []
