"""Drift detection over guide data.

Emits scraper.drift.detected, which is what the SigNoz alert rule watches. Callers use the
returned Drift to take the fast path (set Port health = drifting immediately).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import state, telemetry

REQUIRED_FIELDS = ("channel", "title", "start_utc")
NULL_RATE_LIMIT = 0.10
EMPTY_PAGE_LIMIT = 0.20     # >20% of channel pages returning nothing is breakage
CHANNEL_COLLAPSE = 0.80     # <80% of the requested channels yielding rows is breakage
LINEUP_DELTA_LIMIT = 0.10   # >10% channel-set churn needs two confirming runs

# Rows one channel page should yield after the days-window filter. A page returning 3
# cards for a channel that airs ~40 programs/day is broken, not news; so is one
# returning thousands.
PAGE_ROW_BANDS = (15, 800)


@dataclass
class Drift:
    detected: bool = False
    rules: list[str] = field(default_factory=list)
    description: str = ""

    def add(self, rule: str, detail: str) -> None:
        self.detected = True
        self.rules.append(rule)
        self.description = f"{self.description}; {detail}".strip("; ")


def schema_hash(rows: list[dict[str, Any]]) -> str:
    keys = sorted({k for row in rows for k in row})
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]


def null_rate(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 1.0
    slots = len(rows) * len(REQUIRED_FIELDS)
    nulls = sum(1 for row in rows for f in REQUIRED_FIELDS if row.get(f) in (None, ""))
    return nulls / slots if slots else 1.0


def detect(
    provider: str,
    scraper_id: str,
    rows: list[dict[str, Any]],
    pages: int = 1,
    empty_pages: int = 0,
    expected_channels: int | None = None,
) -> Drift:
    drift = Drift()
    # One collector serves every provider, but drift comparison must be within a
    # provider's own history -- comparing sling's channels to philo's snapshot reads
    # as mass channel loss.
    state_key = f"{scraper_id}:{provider}"
    prev = state.previous(state_key)
    current_hash = schema_hash(rows)

    lo, hi = PAGE_ROW_BANDS
    per_page = len(rows) / pages if pages else 0
    if not lo <= per_page <= hi:
        drift.add(
            "rows_out_of_band",
            f"{per_page:.0f} rows/page over {pages} channel pages, expected {lo}-{hi}",
        )

    if pages and empty_pages / pages > EMPTY_PAGE_LIMIT:
        drift.add("empty_pages", f"{empty_pages}/{pages} channel pages returned nothing")

    rate = null_rate(rows)
    if rate > NULL_RATE_LIMIT:
        drift.add("null_rate", f"required-field null rate {rate:.0%}")

    # The raw-string payoff: the displayed time is present but the parse produced nothing.
    for row in rows:
        if row.get("start_raw") and row.get("start_utc") in (None, ""):
            drift.add("time_parse_failure", f"start_raw={row['start_raw']!r} parsed to null")
            break

    channels = {row.get("channel") for row in rows} - {None, ""}
    if expected_channels and len(channels) < CHANNEL_COLLAPSE * expected_channels:
        drift.add(
            "channel_collapse",
            f"only {len(channels)} of {expected_channels} requested channels yielded rows",
        )

    if prev:
        if prev["schema_hash"] != current_hash:
            drift.add("schema_hash_changed", f"{prev['schema_hash']} -> {current_hash}")
        # Lineup churn could be a real carriage change. Require two consecutive runs
        # agreeing before accepting it; the fixture diff is what ultimately arbitrates.
        delta = _lineup_delta(prev["rows"], rows)
        if delta > LINEUP_DELTA_LIMIT:
            signature = f"lineup:{_channel_sig(rows)}"
            seen = state.confirm_change(state_key, signature)
            if seen < 2:
                drift.add(
                    "lineup_change",
                    f"channel set moved {delta:.0%} (unconfirmed, run {seen}/2)",
                )
            else:
                telemetry.log().info(
                    "lineup change of %.0f%% confirmed over %d runs -- treating as real",
                    delta * 100,
                    seen,
                )
        else:
            state.clear_pending(state_key)

    state.save(state_key, rows, current_hash)

    telemetry.metric("rows_returned").set(len(rows), {"target.provider": provider})
    telemetry.metric("null_rate").set(rate, {"target.provider": provider})
    if drift.detected:
        telemetry.metric("drift_detected").add(
            1, {"target.provider": provider, "scraper.id": scraper_id}
        )
        telemetry.log().warning("drift on %s: %s", provider, drift.description)
    return drift


def _channel_sig(rows: list[dict[str, Any]]) -> str:
    channels = sorted({str(row.get("channel")) for row in rows if row.get("channel")})
    return hashlib.sha256(json.dumps(channels).encode()).hexdigest()[:12]


def _lineup_delta(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> float:
    """Fraction of previously-seen channels that DISAPPEARED this run.

    Only disappearance signals breakage. New channels appearing is normal -- the operator
    widened GUIDE_MAX_CHANNELS, or the provider added carriage -- and the row-band and
    channel-collapse rules already catch a scrape that shrank.
    """
    old_set = {row.get("channel") for row in old} - {None, ""}
    new_set = {row.get("channel") for row in new} - {None, ""}
    if not old_set:
        return 0.0
    return len(old_set - new_set) / len(old_set)
