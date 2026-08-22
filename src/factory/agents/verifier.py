"""The Verifier -- deliberately NOT an agent.

Judgment goes to the LLM agent; proof stays deterministic. A verification gate you cannot
trust deterministically is not a gate, so this is plain code: normalise both sides, diff,
return a verdict. It is the only thing allowed to authorise a promotion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import telemetry

FIXTURES = Path("fixtures")
# Raw extracted fields only: heal previews and verify runs are compared BEFORE the
# pipeline tags channel names or computes UTC times.
COMPARE_FIELDS = ("title", "date_raw", "start_raw")


@dataclass
class Verdict:
    passed: bool
    diff: list[str]

    def summary(self) -> str:
        return "fixture match" if self.passed else "; ".join(self.diff[:4])


def _normalise(rows: list[dict[str, Any]]) -> list[tuple]:
    out = []
    for row in rows:
        key = tuple(_scalar(row.get(f)) for f in COMPARE_FIELDS)
        out.append(key)
    return sorted(out)


def _scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, float):
        return round(value, 2)
    return value


def expected(provider: str) -> list[dict[str, Any]]:
    path = FIXTURES / f"{provider}.expected.json"
    if not path.exists():
        raise FileNotFoundError(
            f"missing fixture {path} -- freeze fixtures before running the gate"
        )
    return json.loads(path.read_text())


def verify(provider: str, rows: list[dict[str, Any]]) -> Verdict:
    """Diff candidate output against the frozen expected output for this provider."""
    with telemetry.tracer().start_as_current_span("scrape.verify.fixture") as span:
        span.set_attribute("target.provider", provider)
        want, got = _normalise(expected(provider)), _normalise(rows)
        span.set_attribute("verify.rows_expected", len(want))
        span.set_attribute("verify.rows_got", len(got))

        if want == got:
            span.set_attribute("verify.status", "pass")
            telemetry.metric("verify_pass").add(1, {"target.provider": provider})
            telemetry.log().info("verify PASS for %s (%d rows)", provider, len(got))
            return Verdict(True, [])

        diff = []
        missing = [w for w in want if w not in got]
        extra = [g for g in got if g not in want]
        if len(want) != len(got):
            diff.append(f"row count {len(got)}, expected {len(want)}")
        for item in missing[:3]:
            diff.append(f"missing {item}")
        for item in extra[:3]:
            diff.append(f"unexpected {item}")

        span.set_attribute("verify.status", "fail")
        # Logged inside the span, so trace_id is attached automatically -- this line is
        # the audit trail for why a repair was rejected.
        telemetry.log().error("verify FAIL for %s: %s", provider, "; ".join(diff))
        return Verdict(False, diff)
