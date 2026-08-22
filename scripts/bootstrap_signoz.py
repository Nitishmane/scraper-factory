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


def _widget(widget_id: str, title: str, description: str, panel: str, queries: list[dict],
            formulas: list[dict] | None = None, y_unit: str = "none") -> dict:
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
        "thresholds": [],
        "query": {
            "queryType": "builder",
            "promql": [],
            "clickhouse_sql": [],
            "builder": {"queryData": queries, "queryFormulas": formulas or []},
        },
    }


def dashboard_payload() -> dict:
    w1 = _widget(
        "drift", "Drift detections", "scraper.drift.detected -- what the alert watches",
        "graph",
        [_query("A", "scraper.drift.detected", "Sum", "increase", "sum", group_by_provider=True)],
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
    w11 = _widget(
        "recent_logs", "Recent logs (scraper-factory)",
        "trace-correlated application logs from the service and CLI runs",
        "list",
        [_logs_query("A", os.getenv("OTEL_SERVICE_NAME", "scraper-factory"))],
    )
    widgets = [w1, w2, w3, w4, w5, w6, w7, w8, w9, w10, w_budget, w_port, w11]
    layout = []
    y = 0
    for n, w in enumerate(widgets):
        if w["id"] == "recent_logs":
            layout.append({"i": w["id"], "x": 0, "y": y + 100, "w": 12, "h": 5,
                           "moved": False, "static": False})
        else:
            layout.append({"i": w["id"], "x": (n % 2) * 6, "y": (n // 2) * 3, "w": 6,
                           "h": 3, "moved": False, "static": False})
    return {
        "title": DASHBOARD_TITLE,
        "description": "Self-healing scraper factory: drift, heals, verification, volume.",
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
