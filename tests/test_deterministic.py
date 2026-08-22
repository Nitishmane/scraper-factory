"""Deterministic-core tests: the parts of the factory that must be trustworthy without
an LLM, a network, Bright Data, or Port.

Everything here is pure logic:
  - drift rules in `detect.detect`
  - the ET -> UTC card-time parse in `pipeline._parse_card_times`
  - the fixture gate in `verifier.verify`
  - title normalisation in `state.normalize_title`
  - the `pipeline.PROVIDERS` table shape

`detect.detect` and the `state` helpers read/write SQLite. `state.DB_PATH` is captured at
import time, so pointing STATE at a tmp DB means rebinding that module attribute (not just
the env var). The `isolated_state` fixture does that per-test so runs never share history.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from factory import brightdata, detect, pipeline, port, portwatch, state
from factory.agents import verifier

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "channel.expected.json"


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the SQLite store at a throwaway DB for this test only.

    `state.DB_PATH` is read at import time, so rebinding the module attribute is what
    actually redirects the store; the schema is created lazily on first connection.
    """
    db = tmp_path / "factory.db"
    monkeypatch.setattr(state, "DB_PATH", str(db))
    return db


# --- helpers -----------------------------------------------------------------------


def _good_rows(n=20, channel="AETV"):
    """Rows that trip no drift rule: enough per page, no nulls, parseable times."""
    return [
        {
            "channel": channel,
            "title": f"Program {i}",
            "start_utc": "2026-08-24T00:00:00Z",
            "start_raw": "8:00 PM",
        }
        for i in range(n)
    ]


# --- drift rules -------------------------------------------------------------------


def test_detect_clean_rows_no_drift(isolated_state):
    drift = detect.detect("philo", "c_test", _good_rows(20), pages=1)
    assert not drift.detected, drift.rules


def test_detect_rows_out_of_band_low(isolated_state):
    # 3 rows/page is below the (15, 800) band -> a shrunken/broken scrape.
    drift = detect.detect("philo", "c_test", _good_rows(3), pages=1)
    assert drift.detected
    assert "rows_out_of_band" in drift.rules


def test_detect_rows_out_of_band_high(isolated_state):
    # 900 rows/page is above the band -> the crawl walked something it shouldn't have.
    drift = detect.detect("philo", "c_test", _good_rows(900), pages=1)
    assert drift.detected
    assert "rows_out_of_band" in drift.rules


def test_detect_null_rate(isolated_state):
    # Half the rows are missing the required `title`, over the 10% null-rate limit.
    rows = _good_rows(20)
    for r in rows[:10]:
        r["title"] = None
    drift = detect.detect("philo", "c_test", rows, pages=1)
    assert drift.detected
    assert "null_rate" in drift.rules


def test_detect_time_parse_failure(isolated_state):
    # A displayed start_raw that produced a null start_utc is the raw-string drift signal.
    rows = _good_rows(20)
    rows[5]["start_raw"] = "8:00 PM"
    rows[5]["start_utc"] = None
    drift = detect.detect("philo", "c_test", rows, pages=1)
    assert drift.detected
    assert "time_parse_failure" in drift.rules


def test_detect_empty_pages(isolated_state):
    # >20% of channel pages returning nothing is breakage. 3 of 10 empty = 30%.
    drift = detect.detect("philo", "c_test", _good_rows(200), pages=10, empty_pages=3)
    assert drift.detected
    assert "empty_pages" in drift.rules


def test_detect_channel_collapse(isolated_state):
    # Only 1 distinct channel yielded rows when 10 were requested (< 80% threshold).
    drift = detect.detect(
        "philo", "c_test", _good_rows(200, channel="AETV"), pages=10, expected_channels=10
    )
    assert drift.detected
    assert "channel_collapse" in drift.rules


def test_detect_state_key_is_per_provider(isolated_state):
    # One collector serves every provider; drift history must not bleed across providers.
    # A philo run then a sling run with a disjoint channel set must NOT read as lineup loss.
    detect.detect("philo", "c_shared", _good_rows(20, channel="AETV"), pages=1)
    drift = detect.detect("sling", "c_shared", _good_rows(20, channel="ESPN"), pages=1)
    assert "lineup_change" not in drift.rules
    assert "schema_hash_changed" not in drift.rules


# --- pipeline.run_once failure path ------------------------------------------------


def test_run_once_reraises_and_records_error(isolated_state, monkeypatch):
    """A fetch that raises must re-raise AND leave an error scrape_run in Port.

    Everything that would touch the network is monkeypatched: the collector id, the lineup
    (so no guide page is fetched), the per-page scrape (raises), and both Port writes (which
    are captured, never sent). The assertion is that run_once propagates the exception and
    that a captured upsert carries status="error" with a non-empty error_detail.
    """
    monkeypatch.setattr(pipeline, "collector_id", lambda: "c_test")
    monkeypatch.setattr(
        pipeline, "lineup", lambda provider: [{"channel": "AETV", "url": "https://x/Channel/AETV"}]
    )

    def _boom(cid, url):
        raise brightdata.BrightDataError("collector exploded")

    monkeypatch.setattr(pipeline, "_scrape_page", _boom)

    upserts: list[dict] = []
    monkeypatch.setattr(
        port,
        "upsert_entity",
        lambda blueprint, identifier, title, properties, relations=None: upserts.append(
            {"blueprint": blueprint, "identifier": identifier, "properties": properties}
        ),
    )
    health_calls: list[tuple] = []
    monkeypatch.setattr(
        port,
        "set_scraper_health",
        lambda scraper_id, health, **extra: health_calls.append((scraper_id, health)),
    )

    with pytest.raises(brightdata.BrightDataError):
        pipeline.run_once("philo", days=1)

    run_writes = [u for u in upserts if u["blueprint"] == "scrape_run"]
    assert run_writes, "run_once must record a scrape_run on failure"
    props = run_writes[-1]["properties"]
    assert props["status"] == "error"
    assert props.get("error_detail")
    assert "BrightDataError" in props["error_detail"]
    # A failed run never flips health to healthy (that would clear a real problem).
    assert ("c_test", "healthy") not in health_calls


def test_run_once_clean_success_sets_healthy(isolated_state, monkeypatch):
    """A clean run persists rows AND self-clears health to 'healthy'."""
    monkeypatch.setattr(pipeline, "collector_id", lambda: "c_test")
    monkeypatch.setattr(
        pipeline, "lineup", lambda provider: [{"channel": "AETV", "url": "https://x/Channel/AETV"}]
    )
    monkeypatch.setattr(
        pipeline,
        "_scrape_page",
        lambda cid, url: (
            [
                {"title": f"Program {i}", "date_raw": "Sat, Aug 22", "start_raw": "8:00 PM"}
                for i in range(20)
            ],
            "relay",
            False,
        ),
    )
    monkeypatch.setattr(
        port, "upsert_entity", lambda *a, **k: None
    )
    health_calls: list[tuple] = []
    monkeypatch.setattr(
        port,
        "set_scraper_health",
        lambda scraper_id, health, **extra: health_calls.append((scraper_id, health)),
    )

    result = pipeline.run_once("philo", days=7)
    assert not result["drift"], result["drift_description"]
    assert ("c_test", "healthy") in health_calls


# --- portwatch new-run diffing -----------------------------------------------------


def test_portwatch_emits_each_run_once(isolated_state, monkeypatch):
    """Feeding the same Port payloads twice emits counters only on the first pass.

    portwatch dedups on state.seen_run (durable), so a restart or overlapping tick never
    re-counts a run. We capture the counter .add() calls instead of talking to OTLP/Port.
    """
    actions_payload = {
        "runs": [
            {"id": "r_1", "status": "SUCCESS", "action": {"identifier": "publish_now"}},
            {"id": "r_2", "status": "FAILURE", "action": {"identifier": "run_scrape_now"}},
        ]
    }
    workflows_payload = {
        "workflowRuns": [
            {
                "identifier": "wfr_1",
                "status": "WAITING_FOR_INPUT",
                "workflowVersion": {"workflow": {"identifier": "repair_scraper"}},
            }
        ]
    }
    monkeypatch.setattr(
        portwatch,
        "_fetch",
        lambda path, params=None: (
            actions_payload if path == "actions/runs" else workflows_payload
        ),
    )
    # Budget poll is out of scope for this test -- stub it to a no-op emit.
    monkeypatch.setattr(portwatch.brightdata, "budget", lambda: None)

    action_emits: list[tuple] = []
    workflow_emits: list[tuple] = []

    class _Counter:
        def __init__(self, sink):
            self.sink = sink

        def add(self, amount, attrs):
            self.sink.append((amount, attrs))

    counters = {
        "port_action_runs": _Counter(action_emits),
        "port_workflow_runs": _Counter(workflow_emits),
    }
    monkeypatch.setattr(
        portwatch.telemetry, "metric", lambda name: counters.get(name, _Counter([]))
    )

    portwatch.tick()
    assert len(action_emits) == 2, "first pass should emit both action runs"
    assert len(workflow_emits) == 1, "first pass should emit the workflow run"
    # Attributes carry the action/workflow identifier and status.
    assert {a[1]["action"] for a in action_emits} == {"publish_now", "run_scrape_now"}
    assert workflow_emits[0][1] == {"workflow": "repair_scraper", "status": "WAITING_FOR_INPUT"}

    action_emits.clear()
    workflow_emits.clear()

    portwatch.tick()
    assert action_emits == [], "second pass must emit nothing new (already seen)"
    assert workflow_emits == [], "second pass must emit nothing new (already seen)"


def test_portwatch_tick_never_raises(isolated_state, monkeypatch):
    """A failing Port fetch is a warning, not an exception out of the tick."""
    def _boom(path, params=None):
        raise RuntimeError("port down")

    monkeypatch.setattr(portwatch, "_fetch", _boom)
    monkeypatch.setattr(portwatch.brightdata, "budget", lambda: None)
    portwatch.tick()  # must not raise


# --- pipeline._parse_card_times ----------------------------------------------------


def _expected_utc(month, day, hour24, minute, day_offset=0):
    """The UTC string the parser should produce for a given ET wall-clock time.

    Mirrors the parser's own year inference (this-year unless >60 days past, else next
    year) and America/New_York -> UTC conversion, so the assertion stays correct whatever
    date CI runs on and whether the date falls in EDT or EST.
    """
    now = dt.datetime.now(pipeline.GUIDE_TZ)
    base = dt.date(now.year, month, day)
    if (base - now.date()).days < -60:
        base = dt.date(now.year + 1, month, day)
    base = base + dt.timedelta(days=day_offset)
    local = dt.datetime(
        base.year, base.month, base.day, hour24, minute, tzinfo=pipeline.GUIDE_TZ
    )
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def test_parse_card_times_from_time_range():
    # Full "Aug 22 2:00 PM - 2:30 PM" range in ET, converted to UTC by the parser.
    row = {
        "date_raw": "Sat, Aug 22",
        "start_raw": "2:00 PM",
        "time_range_raw": "Sat, Aug 22 2:00 PM - 2:30 PM",
    }
    pipeline._parse_card_times(row)
    assert row["start_utc"] == _expected_utc(8, 22, 14, 0)
    assert row["end_utc"] == _expected_utc(8, 22, 14, 30)


def test_parse_card_times_from_date_and_start_only():
    # No time_range_raw: fall back to date_raw + start_raw. 8:00 PM ET.
    row = {"date_raw": "Sat, Aug 22", "start_raw": "8:00 PM"}
    pipeline._parse_card_times(row)
    assert row["start_utc"] == _expected_utc(8, 22, 20, 0)


def test_parse_card_times_midnight_crossing_end():
    # A range whose end clock is earlier than its start lands the end on the next day.
    row = {
        "date_raw": "Sat, Aug 22",
        "start_raw": "11:30 PM",
        "time_range_raw": "Sat, Aug 22 11:30 PM - 12:30 AM",
    }
    pipeline._parse_card_times(row)
    assert row["start_utc"] == _expected_utc(8, 22, 23, 30)
    assert row["end_utc"] == _expected_utc(8, 22, 0, 30, day_offset=1)
    assert row["end_utc"] > row["start_utc"]


def test_parse_card_times_unparseable_sets_null():
    # Garbage the parser can't turn into a start_utc -> None (the time_parse_failure feed).
    row = {"date_raw": "not a date", "start_raw": "whenever"}
    pipeline._parse_card_times(row)
    assert row["start_utc"] is None


# --- verifier.verify ---------------------------------------------------------------


def test_verify_passes_fixture_against_itself():
    rows = json.loads(FIXTURE.read_text())
    verdict = verifier.verify("channel", rows)
    assert verdict.passed, verdict.diff


def test_verify_fails_on_mutated_copy():
    # Mutate an in-memory copy only -- fixtures/ is a frozen input and never touched.
    rows = copy.deepcopy(json.loads(FIXTURE.read_text()))
    rows[0]["title"] = rows[0]["title"] + " CHANGED"
    verdict = verifier.verify("channel", rows)
    assert not verdict.passed
    assert verdict.diff


def test_verify_does_not_write_fixtures():
    # Guard against the suite ever regenerating the frozen fixture.
    before = FIXTURE.read_bytes()
    verifier.verify("channel", json.loads(FIXTURE.read_text()))
    assert FIXTURE.read_bytes() == before


# --- state.normalize_title ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Major League (1989)", "major league"),  # year-suffix stripped
        ("The Office", "office"),  # leading article dropped
        ("A Quiet Place", "quiet place"),
        ("An American Tail", "american tail"),
        ("  Storage Wars  ", "storage wars"),  # whitespace trimmed
        ("Law & Order: SVU", "law order svu"),  # punctuation removed
        ("WandaVision", "wandavision"),
        ("Movie (2021)", "movie"),
        ("Nineteen (1919)", "nineteen"),  # 19xx suffix stripped too
    ],
)
def test_normalize_title(raw, expected):
    assert state.normalize_title(raw) == expected


def test_normalize_title_keeps_non_year_parens():
    # Only a trailing 4-digit 19xx/20xx suffix is dropped; other parens survive.
    assert state.normalize_title("Title (Extended)") == "title extended"


# --- pipeline.PROVIDERS shape ------------------------------------------------------


def test_providers_shape():
    assert pipeline.PROVIDERS, "PROVIDERS must not be empty"
    for name, cfg in pipeline.PROVIDERS.items():
        assert "guide_url" in cfg, f"{name} missing guide_url"
        assert cfg["guide_url"].startswith("http"), f"{name} guide_url not a URL"
        assert "channel_count" in cfg, f"{name} missing channel_count"
        assert isinstance(cfg["channel_count"], int) and cfg["channel_count"] > 0
