"""Port control-plane -> telemetry pull bridge.

Port does not push its self-service action runs or workflow runs anywhere; it exposes them
over its REST API. This module polls those two listings on an interval and re-emits each NEW
run as a log line + a counter datapoint, so control-plane activity (someone clicked "Publish",
an automation fired the healer) shows up in SigNoz beside the data-plane scrape metrics.

Dedup is durable (state.seen_run in SQLite), so a service restart or an overlapping tick
never double-counts a run. Every tick is best-effort: any error is a warning and the loop
continues -- a Port outage must never take the FastAPI service down with it.
"""
from __future__ import annotations

import asyncio
import os

import httpx

from . import port, state, telemetry

# Bright Data's balance also has no push; the same watcher emits its budget gauge once per
# tick so the gauge has a fresh datapoint even on a day with no scrape run.
from . import brightdata

DEFAULT_INTERVAL_S = float(os.getenv("PORT_WATCH_INTERVAL_S", "120"))


def creds_present() -> bool:
    """True when Port credentials are configured. The watcher no-ops without them."""
    return bool(port.CLIENT_ID and port.CLIENT_SECRET)


def _fetch(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(
        f"{port.API}/{path}", params=params, headers=port._headers(), timeout=20
    )
    resp.raise_for_status()
    return resp.json()


def _poll_actions() -> None:
    """Emit a counter datapoint for each new self-service action run.

    GET /v1/actions/runs?version=v2 -> {"ok": true, "runs": [{"id", "status",
    "action": {"identifier"}}]} (shape confirmed live 2026-08-22). version=v2 is required:
    the default (v1) listing returns only approval-pending runs, so our fire-and-forget
    self-service runs (publish_now, run_scrape_now, ...) never appear without it.
    """
    runs = _fetch("actions/runs", {"version": "v2"}).get("runs") or []
    for run in runs:
        run_id = run.get("id")
        if not run_id or not state.mark_run_seen("action", run_id):
            continue
        action = (run.get("action") or {}).get("identifier") or "unknown"
        status = run.get("status") or "unknown"
        telemetry.log().info("port action run %s -> %s", action, status)
        telemetry.metric("port_action_runs").add(1, {"action": action, "status": status})


def _poll_workflows() -> None:
    """Emit a counter datapoint for each new workflow run.

    GET /v1/workflows/runs -> {"ok": true, "workflowRuns": [{"identifier", "status",
    "workflowVersion": {"workflow": {"identifier"}}}]} (shape confirmed live 2026-08-22).
    """
    runs = _fetch("workflows/runs").get("workflowRuns") or []
    for run in runs:
        run_id = run.get("identifier")
        if not run_id or not state.mark_run_seen("workflow", run_id):
            continue
        workflow = (
            ((run.get("workflowVersion") or {}).get("workflow") or {}).get("identifier")
            or run.get("workflowVersionIdentifier")
            or "unknown"
        )
        status = run.get("status") or "unknown"
        telemetry.log().info("port workflow run %s -> %s", workflow, status)
        telemetry.metric("port_workflow_runs").add(
            1, {"workflow": workflow, "status": status}
        )


def _emit_budget() -> None:
    """Publish the Bright Data budget gauge. None (unreadable) is simply not emitted."""
    remaining = brightdata.budget()
    if remaining is not None:
        telemetry.metric("brightdata_budget").set(remaining)
        telemetry.log().info("brightdata budget remaining: %.2f", remaining)


def tick() -> None:
    """One poll pass. Each source is isolated so one failing does not skip the others."""
    for name, fn in (("actions", _poll_actions), ("workflows", _poll_workflows),
                     ("budget", _emit_budget)):
        try:
            fn()
        except Exception as exc:  # never raise out of a tick
            telemetry.log().warning("portwatch %s poll failed: %s", name, exc)


async def watch(interval: float = DEFAULT_INTERVAL_S) -> None:
    """Poll Port control-plane activity + Bright Data budget every `interval` seconds.

    Runs forever; cancellation (service shutdown) exits cleanly. The blocking httpx/CLI work
    is pushed to a thread so it never stalls the FastAPI event loop.
    """
    telemetry.log().info("portwatch started (interval=%ss)", interval)
    while True:
        await asyncio.to_thread(tick)
        await asyncio.sleep(interval)
