"""The scrape pipeline: pull upcoming TV guide data per channel, emit all three signals,
detect drift, write to Port.

Payload architecture (per-channel mode):
  - streamingtvguides.com renders each channel's page (/Channel/<ID>) as a flat list of
    ~455 program cards spanning ~12 days ahead -- far deeper than the 3.5h guide grid.
  - ONE shared collector scrapes any channel page (the structure is identical); the
    provider -> channel mapping comes from each provider's guide page, parsed
    deterministically from its channel-cell anchors with a plain fetch (no AI scraper).
  - Bright Data's AI generation fails on pages this large, so the collector was created
    against a trimmed 30-card copy of a real channel page served via tunnel
    (fixtures/channel.html). The generated code walks any number of cards at runtime.

Setting scraper health to 'drifting' here is the FAST PATH -- it fires the Port automation
in seconds. The SigNoz alert on scraper.drift.detected is the independent safety net, but
its webhook deliveries are grouped every ~5 minutes, so it is not what a live demo waits on.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from . import brightdata, detect, port, state, telemetry
from .agents import verifier

PROVIDERS: dict[str, dict[str, Any]] = {
    "philo": {
        "guide_url": "https://streamingtvguides.com/Philo-TV/Guide",
        "channel_count": 137,
    },
    "youtubetv": {
        "guide_url": "https://streamingtvguides.com/YouTube-TV/Guide",
        "channel_count": 175,
    },
    "sling": {
        "guide_url": "https://streamingtvguides.com/Sling-TV/Orange-Blue-Guide",
        "channel_count": 119,
    },
}

# One collector for every channel page; the pages share one structure.
COLLECTOR_ENV = "SCRAPER_STUDIO_COLLECTOR_ID_CHANNEL"

# Channel pages to scrape per provider per run. ~455 programs each, so even 25 channels
# is ~11k rows; raise GUIDE_MAX_CHANNELS once budget is known.
MAX_CHANNELS = int(os.getenv("GUIDE_MAX_CHANNELS", "25"))

# The site displays times in America/New_York (per its own schema.org metadata).
GUIDE_TZ = ZoneInfo("America/New_York")

# Bright Data caps create descriptions at 500 chars, and its AI generation fails on
# full-size pages -- this is EXACTLY the prompt channel-schedule was created with,
# against the trimmed fixture. It is the healing artifact; keep code and collector in sync.
PROMPT = (
    "This page lists a TV channel's upcoming schedule as program cards. Extract every "
    "program card. For each: the program title as title, the episode title if shown as "
    "episode_title, the airing date exactly as displayed (like Sat, Aug 22) as date_raw, "
    "the start time exactly as displayed (like 2:00 PM) as start_raw, the time range text "
    "if shown (like Sat, Aug 22 2:00 PM - 2:30 PM) as time_range_raw, the category text "
    "as category. Include every card."
)


def collector_id() -> str:
    value = os.getenv(COLLECTOR_ENV)
    if not value:
        raise RuntimeError(
            f"{COLLECTOR_ENV} not set -- create the scraper and pin the ID in CLAUDE.md"
        )
    return value


def fixture_url(name: str = "channel", mutated: bool = False) -> str | None:
    """Public URL of a frozen fixture page, or None when no fixture server is exposed.

    Bright Data collectors execute in Bright Data's cloud and cannot reach localhost, so
    fixtures must be served through a tunnel (FIXTURE_BASE_URL) to be scrapeable.
    """
    base = os.getenv("FIXTURE_BASE_URL", "").rstrip("/")
    if not base:
        return None
    suffix = "mutated.html" if mutated else "html"
    return f"{base}/{name}.{suffix}"


def lineup(provider: str, refresh: bool = False) -> list[dict[str, Any]]:
    """The provider's channel list [(channel, /Channel/<ID> url), ...], cached in SQLite.

    Parsed deterministically from the guide page's channel-cell anchors -- structured
    markup, not a scraping problem worth an AI collector.
    """
    cached = state.lineup(provider)
    if cached and not refresh:
        return cached
    html = brightdata.fetch_html(PROVIDERS[provider]["guide_url"])
    found = re.findall(
        r'<a class="channel-cell" href="(/Channel/[^"]+)">.*?<span>(.*?)</span>',
        html,
        re.S,
    )
    channels = [
        {"channel": _unescape(name), "url": f"https://streamingtvguides.com{path}"}
        for path, name in found
    ]
    if not channels:
        raise RuntimeError(f"no channel-cell anchors found on {provider} guide page")
    state.save_lineup(provider, channels)
    telemetry.log().info("lineup for %s: %d channels", provider, len(channels))
    return channels


def _unescape(s: str) -> str:
    import html as html_lib

    return html_lib.unescape(s).strip()


def _parse_card_times(row: dict[str, Any]) -> None:
    """date_raw ('Sat, Aug 22') + start_raw ('2:00 PM') in Eastern -> start_utc/end_utc.

    Deterministic code, not the scraper's job. A card whose raw strings this parser
    cannot turn into a start_utc is the time_parse_failure drift signal.
    """
    rng = str(row.get("time_range_raw") or "")
    date_raw = str(row.get("date_raw") or "")
    start_raw = str(row.get("start_raw") or "")
    m = re.search(
        r"(\w{3}),?\s+(\w{3})\.?\s+(\d{1,2})\s+(\d{1,2}:\d{2}\s*[AP]M)(?:\s*-\s*(\d{1,2}:\d{2}\s*[AP]M))?",
        rng,
    ) or re.search(
        r"(\w{3}),?\s+(\w{3})\.?\s+(\d{1,2})", date_raw
    )
    if not m:
        row.setdefault("start_utc", None)
        return
    month_name, day = m.group(2), int(m.group(3))
    start_clock = m.group(4) if m.lastindex and m.lastindex >= 4 else start_raw
    end_clock = m.group(5) if m.lastindex and m.lastindex >= 5 else None

    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    try:
        month = months.index(month_name.lower()[:3]) + 1
    except ValueError:
        row.setdefault("start_utc", None)
        return

    now = dt.datetime.now(GUIDE_TZ)
    year = now.year
    candidate_date = dt.date(year, month, day)
    if (candidate_date - now.date()).days < -60:  # Dec page read in Jan
        candidate_date = dt.date(year + 1, month, day)

    row["start_utc"] = _clock_to_utc(candidate_date, start_clock)
    if end_clock:
        end = _clock_to_utc(candidate_date, end_clock)
        # ranges crossing midnight land a day later
        if end and row["start_utc"] and end < row["start_utc"]:
            end = _clock_to_utc(candidate_date + dt.timedelta(days=1), end_clock)
        row["end_utc"] = end


def _clock_to_utc(date: dt.date, clock: str) -> str | None:
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", clock or "", re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)
    local = dt.datetime(date.year, date.month, date.day, hour, int(m.group(2)), tzinfo=GUIDE_TZ)
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def run_once(
    provider: str,
    days: int = 7,
    target_url: str | None = None,
    max_channels: int | None = None,
) -> dict[str, Any]:
    """One traced scrape run over the provider's channel pages. Returns a summary dict.

    target_url short-circuits to a single page -- the fixture/demo path.
    """
    cid = collector_id()
    if target_url:
        pages = [{"channel": "fixture", "url": target_url}]
    else:
        pages = lineup(provider)[: max_channels or MAX_CHANNELS]
    run_id = f"run_{provider}_{uuid.uuid4().hex[:8]}"

    with telemetry.tracer().start_as_current_span("scrape.run") as span:
        started = time.perf_counter()
        span.set_attribute("scraper.id", cid)
        span.set_attribute("target.provider", provider)
        span.set_attribute("run.mode", "scheduled")
        span.set_attribute("guide.days", days)
        span.set_attribute("guide.channels", len(pages))
        telemetry.log().info(
            "scrape run %s starting for %s: %d channel page(s), %d-day window",
            run_id, provider, len(pages), days,
        )

        rows: list[dict[str, Any]] = []
        empty_pages = 0
        for page in pages:
            with telemetry.tracer().start_as_current_span("scrape.fetch") as fetch:
                fetch_started = time.perf_counter()
                channel_id = _channel_id(page["url"])
                fetch.set_attribute("target.provider", provider)
                fetch.set_attribute("channel.name", page["channel"])
                fetch.set_attribute("channel.id", channel_id)
                fetch.set_attribute("channel.url", page["url"])
                page_rows, mode, cache_hit = _scrape_page(cid, page["url"])
                fetch_secs = time.perf_counter() - fetch_started
                fetch.set_attribute("fetch.mode", mode)
                fetch.set_attribute("channel.rows", len(page_rows))
                fetch.set_attribute("relay.cache_hit", cache_hit)
                telemetry.metric("fetch_duration").record(
                    fetch_secs, {"target.provider": provider, "fetch.mode": mode}
                )
                telemetry.metric("pages_fetched").add(
                    1, {"target.provider": provider, "fetch.mode": mode}
                )
                telemetry.metric("rows_extracted").add(
                    len(page_rows), {"target.provider": provider}
                )
                if not page_rows:
                    empty_pages += 1
                for row in page_rows:
                    row["channel"] = page["channel"]
                rows.extend(page_rows)

        with telemetry.tracer().start_as_current_span("scrape.extract"):
            cutoff = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
            ).strftime("%Y-%m-%dT%H:%M:00Z")
            for row in rows:
                _parse_card_times(row)
            rows = [
                r
                for r in _dedupe(rows)
                if not r.get("start_utc") or r["start_utc"] <= cutoff
            ]
            span.set_attribute("rows.returned", len(rows))

        with telemetry.tracer().start_as_current_span("scrape.validate"):
            drift = detect.detect(
                provider,
                cid,
                rows,
                pages=len(pages),
                empty_pages=empty_pages,
                expected_channels=len(pages),
            )
            span.set_attribute("schema.hash", detect.schema_hash(rows))
            span.set_attribute("drift.detected", drift.detected)

        if not drift.detected:
            # Guide rows feed the ranker; don't poison it with a drifted run's output.
            state.save_guide_rows(provider, rows)
            telemetry.log().info(
                "scrape run %s for %s: kept %d rows from %d page(s), no drift -- persisted",
                run_id, provider, len(rows), len(pages),
            )
        else:
            telemetry.log().warning(
                "scrape run %s for %s DRIFTED (%s): %d rows from %d page(s) NOT persisted",
                run_id, provider, drift.description, len(rows), len(pages),
            )

        duration_ms = (time.perf_counter() - started) * 1000
        telemetry.metric("run_duration").record(duration_ms, {"target.provider": provider})
        trace_url = telemetry.trace_url()

        _write_run(run_id, cid, provider, rows, len(pages), drift, trace_url)

        if drift.detected:
            # Fast path: this Port write fires the automation that invokes the healer.
            port.set_scraper_health(cid, "drifting", title=provider, collector_id=cid)
        return {
            "run_id": run_id,
            "provider": provider,
            "channels": len(pages),
            "rows": len(rows),
            "drift": drift.detected,
            "drift_description": drift.description,
            "trace_url": trace_url,
        }


def _scrape_page(cid: str, url: str) -> tuple[list[dict[str, Any]], str, bool]:
    return brightdata.run_with_relay(cid, url)


def _channel_id(url: str) -> str:
    """The <ID> from a streamingtvguides.com/Channel/<ID> page URL; '' if not that shape."""
    m = re.search(r"/Channel/([^/?#]+)", url)
    return m.group(1) if m else ""


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out = []
    for row in rows:
        key = (row.get("channel"), row.get("title"), row.get("date_raw"), row.get("start_raw"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def verify_all() -> int:
    """Run the guide scraper against its frozen fixture. Returns a process exit code.

    One shared collector, one fixture: the trimmed real channel page. Reproducible when
    FIXTURE_BASE_URL serves it; degrades to a live channel page with a warning otherwise.
    """
    url = fixture_url("channel")
    if url is None:
        url = "https://streamingtvguides.com/Channel/AETV"
        telemetry.log().warning(
            "FIXTURE_BASE_URL not set -- verifying against the LIVE channel page, "
            "not the frozen fixture; the gate is not reproducible this way"
        )
    try:
        rows = brightdata.run(collector_id(), [url])
        return 0 if verifier.verify("channel", rows).passed else 1
    except Exception as exc:
        telemetry.log().error("verify errored: %s", exc)
        return 1


def _write_run(
    run_id: str,
    cid: str,
    provider: str,
    rows: list[dict[str, Any]],
    channels_scraped: int,
    drift: detect.Drift,
    trace_url: str | None,
) -> None:
    try:
        port.upsert_entity(
            "scrape_run",
            identifier=run_id,
            title=f"{provider} {dt.datetime.now().strftime('%H:%M:%S')}",
            properties={
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": "drift" if drift.detected else "success",
                "rows_returned": len(rows),
                "windows_scraped": channels_scraped,
                "schema_hash": detect.schema_hash(rows),
                "drift_detected": drift.detected,
                "trace_url": trace_url,
            },
            relations={"scraper": cid},
        )
    except Exception as exc:
        telemetry.log().warning("could not write scrape_run to Port: %s", exc)
