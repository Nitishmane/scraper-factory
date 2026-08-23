#!/usr/bin/env python3
"""Idempotent SigNoz bootstrap: the factory dashboard and the drift alert rule.

Mirrors bootstrap_port.py -- observability as code, not UI clicks. Safe to re-run:
matches the dashboard by title and the rule by alert name, updating in place.

    set -a; source .env; set +a
    python3 scripts/bootstrap_signoz.py
"""
from __future__ import annotations

import os
import sys
import uuid

import httpx

BASE = os.getenv("SIGNOZ_UI_URL", "http://localhost:8080").rstrip("/")
KEY = os.getenv("SIGNOZ_API_KEY", "")
HEADERS = {"SIGNOZ-API-KEY": KEY, "Content-Type": "application/json"}

DASHBOARD_TITLE = "Scraper Factory"
ALERT_NAME = "Scraper drift detected"
CHANNEL_NAME = "factory-heal-webhook"
# SigNoz runs in docker; localhost inside its container is not the host. SigNoz webhook
# channels can't send custom headers, so the shared factory token rides in the URL.
_base = os.getenv("SIGNOZ_HEAL_WEBHOOK", "http://host.docker.internal:8000/heal")
_token = os.getenv("FACTORY_WEBHOOK_TOKEN", "")
WEBHOOK_URL = f"{_base}{'&' if '?' in _base else '?'}token={_token}" if _token else _base


def _metric(key: str, mtype: str) -> dict:
    return {"key": key, "dataType": "float64", "type": mtype, "isColumn": True}


def _query(name: str, key: str, mtype: str, time_agg: str, space_agg: str,
           group_by_provider: bool = False, group_by: str | None = None) -> dict:
    q = {
        "dataSource": "metrics",
        "queryName": name,
        "aggregateOperator": time_agg,
        "aggregateAttribute": _metric(key, mtype),
        "timeAggregation": time_agg,
        "spaceAggregation": space_agg,
        "functions": [],
        "filters": {"items": [], "op": "AND"},
        "expression": name,
        "disabled": False,
        "stepInterval": 60,
        "having": [],
        "limit": None,
        "orderBy": [],
        "groupBy": [],
        "legend": "",
        "reduceTo": "avg",
    }
    # group_by wins if given; otherwise the provider shortcut is honored.
    gkey = group_by or ("target.provider" if group_by_provider else None)
    if gkey:
        q["groupBy"] = [
            {"key": gkey, "dataType": "string", "type": "tag", "isColumn": False}
        ]
        q["legend"] = "{{" + gkey + "}}"
    return q


def _logs_query(name: str, service: str) -> dict:
    """A logs list query filtered to one service, newest first."""
    return {
        "dataSource": "logs",
        "queryName": name,
        "aggregateOperator": "noop",
        "aggregateAttribute": {},
        "timeAggregation": "",
        "spaceAggregation": "",
        "functions": [],
        "filters": {"items": [
            {"key": {"key": "service.name", "dataType": "string", "type": "resource",
                     "isColumn": False},
             "op": "=", "value": service}
        ], "op": "AND"},
        "expression": name,
        "disabled": False,
        "stepInterval": 60,
        "having": [],
        "limit": None,
        "orderBy": [{"columnName": "timestamp", "order": "desc"}],
        "groupBy": [],
        "legend": "",
        "offset": 0,
        "pageSize": 30,
    }


def _threshold(label: str, value: float, color: str = "#e5484d") -> dict:
    """A single SigNoz panel threshold band (default red). Applied above `value`."""
    return {
        "index": label,
        "keyIndex": 0,
        "moveThreshold": 0,
        "thresholdValue": value,
        "thresholdFormat": "Text",
        "thresholdOperator": ">",
        "thresholdUnit": "none",
        "thresholdColor": color,
        "thresholdLabel": label,
    }


def _widget(widget_id: str, title: str, description: str, panel: str, queries: list[dict],
            formulas: list[dict] | None = None, y_unit: str = "none",
            thresholds: list[dict] | None = None) -> dict:
    return {
        "id": widget_id,
        "title": title,
        "description": description,
        "panelTypes": panel,
        "isStacked": False,
        "nullZeroValues": "zero",
        "opacity": "1",
        "fillSpans": False,
        "yAxisUnit": y_unit,
        "softMax": None,
        "softMin": None,
        "thresholds": thresholds or [],
        "query": {
            "queryType": "builder",
            "promql": [],
            "clickhouse_sql": [],
            "builder": {"queryData": queries, "queryFormulas": formulas or []},
        },
    }


def _value_query(name: str, mtype: str, time_agg: str, space_agg: str,
                 reduce_to: str = "sum") -> dict:
    """A single-number query for a `value` panel: reduceTo collapses the series to one point."""
    q = _query("A", name, mtype, time_agg, space_agg)
    q["reduceTo"] = reduce_to
    return q


def dashboard_payload() -> dict:
    w1 = _widget(
        "drift", "Drift detections", "scraper.drift.detected -- what the alert watches",
        "graph",
        [_query("A", "scraper.drift.detected", "Sum", "increase", "sum", group_by_provider=True)],
        thresholds=[_threshold("drift", 0)],
    )
    w2 = _widget(
        "heal", "Heals: success vs attempts", "the headline reliability number",
        "graph",
        [
            _query("A", "scraper.heal.success", "Sum", "increase", "sum"),
            _query("B", "scraper.heal.attempts", "Sum", "increase", "sum"),
        ],
    )
    w3 = _widget(
        "rows", "Rows returned by provider", "drift appears as a visible cliff",
        "graph",
        [_query("A", "scraper.rows_returned", "Gauge", "latest", "max", group_by_provider=True)],
    )
    w4 = _widget(
        "duration", "Run duration p95", "scrape.run wall time",
        "graph",
        [_query("A", "scraper.run.duration", "Histogram", "p95", "p95")],
    )
    w5 = _widget(
        "verify", "Verify gate passes", "fixture-gate throughput",
        "graph",
        [_query("A", "scraper.verify.pass", "Sum", "increase", "sum")],
    )
    # --- panels for the enriched signals -----------------------------------
    # p95 (SigNoz-native histogram quantile) plus an avg-latency fallback derived from the
    # histogram's .sum/.count -- some SigNoz builds don't render OTLP histogram quantiles,
    # and the derived average always plots, so the panel is never blank.
    w6 = _widget(
        "fetch_p95", "Fetch duration: p95 + avg (s)",
        "scrape.fetch.duration per channel page; C = avg = sum/count",
        "graph",
        [
            _query("A", "scrape.fetch.duration", "Histogram", "p95", "p95",
                   group_by="fetch.mode"),
            _query("B", "scrape.fetch.duration.sum", "Sum", "rate", "sum"),
            _query("C", "scrape.fetch.duration.count", "Sum", "rate", "sum"),
        ],
        formulas=[{"queryName": "F1", "expression": "B/C", "legend": "avg (s)",
                   "disabled": False}],
        y_unit="s",
    )
    w7 = _widget(
        "rows_extracted", "Rows extracted by provider",
        "scrape.rows.extracted -- raw extraction volume upstream of drift filtering",
        "graph",
        [_query("A", "scrape.rows.extracted", "Sum", "increase", "sum",
                group_by_provider=True)],
    )
    w8 = _widget(
        "relay_cache", "Relay cache: hits vs misses",
        "relay.cache grouped by hit -- how often Web Unlocker is spared a fetch",
        "graph",
        [_query("A", "relay.cache", "Sum", "increase", "sum", group_by="hit")],
    )
    w9 = _widget(
        "heal_outcomes", "Heal outcomes",
        "heal.events grouped by outcome (repaired/escalated/rejected)",
        "graph",
        [_query("A", "heal.events", "Sum", "increase", "sum", group_by="outcome")],
    )
    w10 = _widget(
        "port_latency", "Port write latency: p95 + avg (s)",
        "port.write.duration; F1 = avg = sum/count (renders even where quantiles do not)",
        "graph",
        [
            _query("A", "port.write.duration", "Histogram", "p95", "p95",
                   group_by="blueprint"),
            _query("B", "port.write.duration.sum", "Sum", "rate", "sum"),
            _query("C", "port.write.duration.count", "Sum", "rate", "sum"),
        ],
        formulas=[{"queryName": "F1", "expression": "B/C", "legend": "avg (s)",
                   "disabled": False}],
        y_unit="s",
    )
    # --- control-plane pull bridges ----------------------------------------
    # brightdata.budget.remaining is a gauge (latest value); port.action.runs and
    # port.workflow.runs are counters (increase per window), grouped by status. These are
    # simple sum/latest -- no histogram-quantile quirk to work around here.
    w_budget = _widget(
        "bd_budget", "Bright Data budget remaining ($)",
        "brightdata.budget.remaining -- polled from the CLI (Bright Data does not push it)",
        "graph",
        [_query("A", "brightdata.budget.remaining", "Gauge", "latest", "max")],
        y_unit="none",
    )
    w_port = _widget(
        "port_activity", "Port control-plane activity",
        "port.action.runs + port.workflow.runs by status -- polled from Port's API "
        "(Port does not push run activity)",
        "graph",
        [
            _query("A", "port.action.runs", "Sum", "increase", "sum", group_by="status"),
            _query("B", "port.workflow.runs", "Sum", "increase", "sum", group_by="status"),
        ],
    )
    # Failure-visibility metric added today; grouped by provider (error.type is also on the
    # series and shows in the legend hover). Red threshold: any failure is worth a look.
    w_failures = _widget(
        "run_failures", "Run failures",
        "scraper.run.failures by provider -- fault-injection + real scrape errors",
        "graph",
        [_query("A", "scraper.run.failures", "Sum", "increase", "sum",
                group_by_provider=True)],
        thresholds=[_threshold("failure", 0)],
    )
    w11 = _widget(
        "recent_logs", "Recent logs (scraper-factory)",
        "trace-correlated application logs from the service and CLI runs",
        "list",
        [_logs_query("A", os.getenv("OTEL_SERVICE_NAME", "scraper-factory"))],
    )

    # --- overview row: compact single-number "value" panels (verified to persist on
    # SigNoz v0.111.0). At-a-glance health above the detailed graphs.
    o1 = _widget(
        "ov_rows", "Rows returned (latest)",
        "scraper.rows_returned, most recent run across providers",
        "value",
        [_value_query("scraper.rows_returned", "Gauge", "latest", "max", reduce_to="last")],
    )
    o2 = _widget(
        "ov_drift", "Drift detections (24h)",
        "scraper.drift.detected summed over the window",
        "value",
        [_value_query("scraper.drift.detected", "Sum", "increase", "sum", reduce_to="sum")],
        thresholds=[_threshold("drift", 0)],
    )
    o3 = _widget(
        "ov_failures", "Run failures (24h)",
        "scraper.run.failures summed over the window",
        "value",
        [_value_query("scraper.run.failures", "Sum", "increase", "sum", reduce_to="sum")],
        thresholds=[_threshold("failure", 0)],
    )
    o4 = _widget(
        "ov_heals", "Heal successes (total)",
        "scraper.heal.success -- promotions that passed the fixture gate",
        "value",
        [_value_query("scraper.heal.success", "Sum", "sum", "sum", reduce_to="sum")],
    )

    # Logical rows via an explicit react-grid-layout array (12-col grid). Each dict:
    # i=widget id, x/y grid coords, w/h span. Ordered top-to-bottom by section.
    overview = [o1, o2, o3, o4]
    scrape_pipeline = [w7, w3, w6, w8, w4]          # rows extracted, rows by provider, fetch dur, relay, run dur
    heal_loop = [w1, w9, w2, w5, w_failures]        # drift, heal outcomes, heals s/a, verify, failures
    control_plane = [w_port, w10, w_budget]         # port activity, port latency, budget
    logs_row = [w11]
    widgets = overview + scrape_pipeline + heal_loop + control_plane + logs_row

    layout: list[dict] = []
    y = 0

    def _row(items: list[dict], w: int, h: int) -> None:
        nonlocal y
        per_row = max(1, 12 // w)
        for i, wid in enumerate(items):
            if i and i % per_row == 0:
                y += h
            layout.append({"i": wid["id"], "x": (i % per_row) * w, "y": y,
                           "w": w, "h": h, "moved": False, "static": False})
        y += h

    _row(overview, w=3, h=2)          # 4 compact value tiles across the top
    _row(scrape_pipeline, w=6, h=3)   # two-up graphs
    _row(heal_loop, w=6, h=3)
    _row(control_plane, w=6, h=3)
    _row(logs_row, w=12, h=6)         # full-width logs at the bottom

    return {
        "title": DASHBOARD_TITLE,
        "description": "Self-healing scraper factory: overview, scrape pipeline, heal loop, "
                       "control plane, and logs.",
        "tags": ["scraper-factory"],
        "layout": layout,
        "widgets": widgets,
        "variables": {},
    }


def alert_payload() -> dict:
    return {
        "alert": ALERT_NAME,
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "threshold_rule",
        "evalWindow": "5m0s",
        "frequency": "1m0s",
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "builderQueries": {
                    "A": _query("A", "scraper.drift.detected", "Sum", "increase", "sum"),
                },
                "unit": "none",
            },
            "op": "1",           # above
            "target": 0,
            "matchType": "2",    # at least once
            "selectedQueryName": "A",
        },
        "labels": {"severity": "warning"},
        "annotations": {
            "summary": "A scraper reported drift in the last 5 minutes.",
            "description": "Safety-net path: webhook -> /heal. The inline detector has "
                           "usually already fired; this catches what it cannot.",
        },
        "preferredChannels": [CHANNEL_NAME],
    }


def ensure_channel(client: httpx.Client) -> None:
    resp = client.get(f"{BASE}/api/v1/channels")
    resp.raise_for_status()
    if any(c.get("name") == CHANNEL_NAME for c in resp.json().get("data") or []):
        print(f"channel exists: {CHANNEL_NAME}")
        return
    resp = client.post(
        f"{BASE}/api/v1/channels",
        json={
            "name": CHANNEL_NAME,
            "webhook_configs": [{"url": WEBHOOK_URL, "send_resolved": True}],
        },
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"channel create failed: {resp.status_code} {resp.text[:300]}")
    print(f"channel created: {CHANNEL_NAME} -> {WEBHOOK_URL}")


def upsert_dashboard(client: httpx.Client) -> str:
    resp = client.get(f"{BASE}/api/v1/dashboards")
    resp.raise_for_status()
    existing = None
    for d in resp.json().get("data") or []:
        data = d.get("data") or d
        if data.get("title") == DASHBOARD_TITLE:
            existing = d.get("uuid") or d.get("id")
            break
    payload = dashboard_payload()
    if existing:
        resp = client.put(f"{BASE}/api/v1/dashboards/{existing}", json=payload)
        action = "updated"
    else:
        resp = client.post(f"{BASE}/api/v1/dashboards", json=payload)
        action = "created"
    if resp.status_code >= 300:
        raise RuntimeError(f"dashboard {action} failed: {resp.status_code} {resp.text[:400]}")
    body = resp.json().get("data") or {}
    uid = body.get("uuid") or body.get("id") or existing or "?"
    print(f"dashboard {action}: {DASHBOARD_TITLE} ({uid})")
    return str(uid)


def upsert_alert(client: httpx.Client) -> None:
    resp = client.get(f"{BASE}/api/v1/rules")
    resp.raise_for_status()
    rules = (resp.json().get("data") or {}).get("rules") or []
    existing = next((r.get("id") for r in rules if r.get("alert") == ALERT_NAME), None)
    payload = alert_payload()
    if existing:
        resp = client.put(f"{BASE}/api/v1/rules/{existing}", json=payload)
        action = "updated"
    else:
        resp = client.post(f"{BASE}/api/v1/rules", json=payload)
        action = "created"
    if resp.status_code >= 300:
        raise RuntimeError(f"alert {action} failed: {resp.status_code} {resp.text[:400]}")
    print(f"alert {action}: {ALERT_NAME}")


def main() -> int:
    if not KEY:
        print("SIGNOZ_API_KEY not set (see .env)")
        return 2
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        uid = upsert_dashboard(client)
        ensure_channel(client)
        try:
            upsert_alert(client)
        except Exception as exc:
            print(f"!! alert rule failed ({exc}); create it in the UI: "
                  f"scraper.drift.detected above 0, at least once, 5m window")
        print(f"\nopen: {BASE}/dashboard/{uid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
