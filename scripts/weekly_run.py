"""Laptop-free pipeline driver for the weekly GitHub Actions run.

Runs the same stages an operator would run by hand -- scrape per provider, heal when the
drift detector fires, refresh the catalog, publish the top-20 -- inside one process so the
Actions job needs no service, no inbound webhooks, and no JSON-parsing of CLI output.

The heal path is the CLAUDE.md contract unchanged: brightdata heal -> deterministic fixture
verify -> approve/reject. A drifted provider that heals cleanly gets exactly one re-scrape;
one that escalates is left broken in Port for a human, and this script exits non-zero so the
Actions run surfaces it.

Usage: PYTHONPATH=src python scripts/weekly_run.py [--provider all|philo|...] [--days 7]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from factory import catalog, pipeline, publish, telemetry
from factory.agents import healer

# Web Unlocker fetches fail transiently ("Proxy Error") and the pipeline has no built-in
# retry (a documented limitation). Retrying the whole provider run is cheap: pages fetched
# on a previous attempt are still inside the RELAY_CACHE_MINUTES window, so only the pages
# that actually failed hit Web Unlocker again.
ATTEMPTS = 3
RETRY_DELAY_S = 30


def _run_with_retry(provider: str, days: int) -> dict:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return pipeline.run_once(provider, days=days)
        except Exception as exc:
            print(f"{provider}: attempt {attempt}/{ATTEMPTS} raised: {exc}")
            if attempt == ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")


def _scrape(provider: str, days: int) -> tuple[dict | None, str | None]:
    """One provider: scrape, heal-if-drifted, single retry. Returns (summary, failure)."""
    try:
        result = _run_with_retry(provider, days)
    except Exception as exc:  # a dead tunnel / Bright Data outage should not kill the loop
        return None, f"{provider}: scrape raised {exc}"

    if not result.get("drift"):
        return result, None

    description = result.get("drift_description") or "drift detected in scheduled weekly run"
    print(f"{provider}: drift detected -- invoking healer: {description}")
    if not healer.heal(provider, pipeline.collector_id(), description, "weekly-run"):
        return result, f"{provider}: drifted and heal escalated (scraper left broken in Port)"

    retry = _run_with_retry(provider, days)
    if retry.get("drift"):
        return retry, f"{provider}: still drifting after an approved heal"
    return retry, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="all")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    telemetry.init()
    providers = list(pipeline.PROVIDERS) if args.provider == "all" else [args.provider]

    failures: list[str] = []
    for provider in providers:
        summary, failure = _scrape(provider, args.days)
        if summary:
            print(json.dumps(summary, indent=2))
        if failure:
            print(f"!! {failure}")
            failures.append(failure)

    # Catalog refresh is best-effort: record links degrade gracefully to guide URLs, and the
    # catalog has its own MIN_ROWS gate, so a hiccup here must not block the publish.
    try:
        print(json.dumps(catalog.refresh(), indent=2))
    except Exception as exc:
        print(f"!! catalog refresh failed (non-fatal): {exc}")

    # run_once never persists drifted rows, so whatever is in the DB is safe to rank/publish.
    print(json.dumps(publish.push(), indent=2))

    if failures:
        print(f"completed with {len(failures)} failure(s); see above")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
