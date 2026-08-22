#!/usr/bin/env python3
"""Idempotent Port bootstrap: blueprints, the drift automation, and the scorecard.

This being a script rather than a list of UI clicks in the README is the difference
between a demo and a product. Safe to run repeatedly.

    python scripts/bootstrap_port.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from factory import port, telemetry  # noqa: E402

HEAL_WEBHOOK = os.getenv("HEAL_WEBHOOK_URL", "http://localhost:8000/heal")
BUILD_WEBHOOK = os.getenv("BUILD_WEBHOOK_URL", "http://localhost:8000/build")

DATA_SOURCE = {
    "identifier": "data_source",
    "title": "Data Source",
    "schema": {
        "properties": {
            "provider_name": {"type": "string", "title": "Provider"},
            "target_url": {"type": "string", "title": "Target URL", "format": "url"},
            "brief": {"type": "string", "title": "Operator brief", "format": "markdown"},
            "status": {
                "type": "string",
                "title": "Status",
                "enum": ["requested", "approved", "live"],
            },
        },
        "required": ["provider_name", "target_url"],
    },
    "relations": {},
}

SCRAPER = {
    "identifier": "scraper",
    "title": "Scraper",
    "schema": {
        "properties": {
            "version": {"type": "string", "title": "Version"},
            "collector_id": {"type": "string", "title": "Collector ID"},
            "prompt_text": {"type": "string", "title": "Prompt", "format": "markdown"},
            "verify_status": {"type": "string", "title": "Verify status", "enum": ["pass", "fail"]},
            "last_verified_at": {
                "type": "string",
                "title": "Last verified",
                "format": "date-time",
            },
            "health": {
                "type": "string",
                "title": "Health",
                "enum": ["healthy", "drifting", "broken"],
                "enumColors": {"healthy": "green", "drifting": "yellow", "broken": "red"},
            },
        },
        "required": ["health"],
    },
    "relations": {
        "data_source": {"target": "data_source", "required": False, "many": False}
    },
}

SCRAPE_RUN = {
    "identifier": "scrape_run",
    "title": "Scrape Run",
    "schema": {
        "properties": {
            "started_at": {"type": "string", "title": "Started", "format": "date-time"},
            "status": {
                "type": "string",
                "title": "Status",
                "enum": ["success", "drift", "error"],
                "enumColors": {"success": "green", "drift": "yellow", "error": "red"},
            },
            "rows_returned": {"type": "number", "title": "Rows"},
            "windows_scraped": {"type": "number", "title": "Guide windows"},
            "schema_hash": {"type": "string", "title": "Schema hash"},
            "drift_detected": {"type": "boolean", "title": "Drift detected"},
            # format url makes this render as a clickable deep link into SigNoz
            "trace_url": {"type": "string", "title": "Trace", "format": "url"},
        }
    },
    "relations": {"scraper": {"target": "scraper", "required": False, "many": False}},
}

RANKED_TITLE = {
    "identifier": "ranked_title",
    "title": "Ranked Title",
    "schema": {
        "properties": {
            "rank": {"type": "number", "title": "Rank"},
            "show_title": {"type": "string", "title": "Title"},
            "kind": {"type": "string", "title": "Kind", "enum": ["movie", "show"]},
            "score": {"type": "number", "title": "Score"},
            "next_airing_at": {"type": "string", "title": "Next airing", "format": "date-time"},
            "channel": {"type": "string", "title": "Channel"},
            "provider": {"type": "string", "title": "Provider"},
            "is_new": {"type": "boolean", "title": "New episode"},
            "airings": {"type": "number", "title": "Airings this week"},
            # format url renders as a clickable record link (Philo player page preferred)
            "record_url": {"type": "string", "title": "Record", "format": "url"},
        },
        "required": ["rank", "show_title"],
    },
    "relations": {},
}

HEAL_EVENT = {
    "identifier": "heal_event",
    "title": "Heal Event",
    "schema": {
        "properties": {
            "trigger_rule": {"type": "string", "title": "Trigger rule"},
            "drift_description": {"type": "string", "title": "Drift"},
            "heal_prompt": {"type": "string", "title": "Heal prompt", "format": "markdown"},
            "outcome": {
                "type": "string",
                "title": "Outcome",
                "enum": ["healed", "rolled_back", "escalated"],
                "enumColors": {"healed": "green", "rolled_back": "orange", "escalated": "red"},
            },
            "rows_before": {"type": "number", "title": "Rows before"},
            "rows_after": {"type": "number", "title": "Rows after"},
            "trace_url": {"type": "string", "title": "Trace", "format": "url"},
        }
    },
    "relations": {"scraper": {"target": "scraper", "required": False, "many": False}},
}

# ENTITY_UPDATED on scraper, gated by a JQ condition on the post-update state.
# publish: true is REQUIRED or the automation sits inert.
HEAL_AUTOMATION = {
    "identifier": "invoke_healer_on_drift",
    "title": "Invoke healer on drift",
    "description": "Re-prompts a drifting scraper and re-verifies it against its fixture",
    "trigger": {
        "type": "automation",
        "event": {"type": "ENTITY_UPDATED", "blueprintIdentifier": "scraper"},
        "condition": {
            "type": "JQ",
            "expressions": ['.diff.after.properties.health == "drifting"'],
            "combinator": "and",
        },
    },
    "invocationMethod": {"type": "WEBHOOK", "url": HEAL_WEBHOOK},
    "publish": True,
}

BUILD_ACTION = {
    "identifier": "request_data_source",
    "title": "Request a data source",
    "description": "Submit a brief; the factory builds and verifies a scraper for it",
    "trigger": {
        "type": "self-service",
        "operation": "CREATE",
        "blueprintIdentifier": "data_source",
        "userInputs": {
            "properties": {
                "provider": {"type": "string", "title": "Provider name"},
                "target_url": {"type": "string", "title": "Target URL", "format": "url"},
                "brief": {"type": "string", "title": "Brief", "format": "markdown"},
            },
            "required": ["provider", "target_url", "brief"],
        },
    },
    "invocationMethod": {"type": "WEBHOOK", "url": BUILD_WEBHOOK},
    "publish": True,
}

# Governance gate on the scraper fleet. Levels ascend Basic -> Bronze -> Silver -> Gold.
# The lowest level ("Basic") is the fallback for entities matching no rule and carries no
# rule of its own. The date presets accepted by the `between` operator are today/tomorrow/
# yesterday only (not lastDay/last7Days), so "verified recently" is expressed as `today`.
SCORECARD = {
    "identifier": "scraper_reliability",
    "title": "Scraper reliability",
    "levels": [
        {"title": "Basic", "color": "paleBlue"},
        {"title": "Bronze", "color": "bronze"},
        {"title": "Silver", "color": "silver"},
        {"title": "Gold", "color": "gold"},
    ],
    "rules": [
        {
            "identifier": "verified_recently",
            "title": "Verified recently (today)",
            "level": "Bronze",
            "query": {
                "combinator": "and",
                "conditions": [
                    {
                        "operator": "between",
                        "property": "last_verified_at",
                        "value": {"preset": "today"},
                    }
                ],
            },
        },
        {
            "identifier": "verify_pass",
            "title": "Fixture verification passing",
            "level": "Silver",
            "query": {
                "combinator": "and",
                "conditions": [{"operator": "=", "property": "verify_status", "value": "pass"}],
            },
        },
        {
            "identifier": "healthy_and_verified",
            "title": "Healthy and verified",
            "level": "Gold",
            "query": {
                "combinator": "and",
                "conditions": [
                    {"operator": "=", "property": "health", "value": "healthy"},
                    {"operator": "=", "property": "verify_status", "value": "pass"},
                ],
            },
        },
    ],
}

_DASH_MD = (
    "## Scraper Factory — Operator\n\n"
    "Self-healing web-scraper factory. **Bright Data** collectors scrape guide + catalog "
    "data, **SigNoz** traces every run, and **Port** is the control plane.\n\n"
    "- **Fast path:** inline detector sets `health = drifting` -> the "
    "`invoke_healer_on_drift` automation fires in seconds.\n"
    "- **Safety net:** SigNoz alert webhook (~5 min) catches a dead pipeline.\n"
    "- **Gate:** the Verifier is deterministic code; nothing is promoted without a passing "
    "fixture diff.\n\n"
    "_Judgment goes to the agent. Proof stays deterministic._"
)


def _table_widget(wid: str, title: str, icon: str, blueprint: str) -> dict:
    # A dashboard table targets a blueprint via a $blueprint rule in its dataset; the
    # widget's `dataset` object itself only accepts `combinator` and `rules`.
    return {
        "id": wid,
        "type": "table-entities-explorer",
        "title": title,
        "icon": icon,
        "dataset": {
            "combinator": "and",
            "rules": [{"operator": "=", "property": "$blueprint", "value": blueprint}],
        },
    }


# Dashboard page. Widget ids must match the column ids referenced in `layout`.
OPERATOR_DASHBOARD = {
    "identifier": "factory_operator",
    "title": "Factory Operator",
    "icon": "Scraper",
    "type": "dashboard",
    "widgets": [
        {
            "type": "dashboard-widget",
            "id": "factory-dash",
            "layout": [
                {"height": 420, "columns": [{"id": "col-health", "size": 4}, {"id": "col-md", "size": 8}]},
                {"height": 460, "columns": [{"id": "col-heals", "size": 12}]},
                {"height": 460, "columns": [{"id": "col-ranked", "size": 12}]},
            ],
            "widgets": [
                {
                    "id": "col-health",
                    "type": "entities-pie-chart",
                    "title": "Scrapers by health",
                    "icon": "Scraper",
                    "property": "property#health",
                    "blueprint": "scraper",
                    "dataset": {"combinator": "and", "rules": []},
                },
                {
                    "id": "col-md",
                    "type": "markdown",
                    "title": "Architecture",
                    "icon": "BlankPage",
                    "markdown": _DASH_MD,
                },
                _table_widget("col-heals", "Recent heal events", "Bolt", "heal_event"),
                _table_widget("col-ranked", "Ranked titles", "Star", "ranked_title"),
            ],
        }
    ],
}


def _seed_entities() -> None:
    """Seed the two live collectors so the catalog isn't empty on first load."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    channel = os.getenv("SCRAPER_STUDIO_COLLECTOR_ID_CHANNEL")
    catalog = os.getenv("SCRAPER_STUDIO_COLLECTOR_ID_PHILO_CATALOG")
    if channel:
        port.upsert_entity(
            "scraper", channel, "channel-schedule",
            {"collector_id": channel, "health": "healthy", "verify_status": "pass",
             "last_verified_at": now},
        )
    if catalog:
        port.upsert_entity(
            "scraper", catalog, "philo-catalog",
            {"collector_id": catalog, "health": "healthy"},
        )


# Port AI agents are entities of the system blueprint `_ai_agent`. Approval Required:
# the agent recommends; humans (and the deterministic Verifier) keep the final word.
TRIAGE_AGENT = {
    "identifier": "triage",
    "title": "Triage",
    "properties": {
        "description": "The judgment layer of the Scraper Factory. Reads scrape_run and "
                       "heal_event history for a drifting scraper and decides whether to "
                       "attempt an automated heal or escalate to a human, authoring the "
                       "plain-language drift description the healer sends to Bright Data.",
        "status": "active",
        "execution_mode": "Approval Required",
        "prompt": "You are the Triage agent for a self-healing scraper factory. When a "
                  "scraper entity health becomes drifting: 1) Read its recent scrape_run "
                  "entities (rows_returned, drift_detected, schema_hash) and heal_event "
                  "history. 2) Decide: if the same drift signature failed a heal recently, "
                  "or heal_events show repeated rolled_back outcomes, escalate to a human "
                  "instead of healing again. Otherwise recommend a heal. 3) Author a "
                  "plain-language drift description of the observed SYMPTOM (what changed "
                  "in the data), never a guess at the fix. Example: rows per page dropped "
                  "from 230 to 12 and start_raw no longer parses; card markup likely "
                  "restructured. Keep it under 400 characters so it fits the Bright Data "
                  "heal prompt. You may read scraper, scrape_run, and heal_event entities. "
                  "Never approve or reject heals yourself: the deterministic Verifier owns "
                  "promotion.",
        "conversation_starters": [
            "Why is channel-schedule drifting?",
            "Should we heal or escalate?",
            "Summarize the last 5 heal events",
        ],
    },
}


def main() -> int:
    telemetry.init()
    for blueprint in (DATA_SOURCE, SCRAPER, SCRAPE_RUN, HEAL_EVENT, RANKED_TITLE):
        port.create_blueprint(blueprint)

    for definition in (HEAL_AUTOMATION, BUILD_ACTION):
        try:
            port.create_automation(definition)
        except Exception as exc:
            # Action/automation payload shapes evolve; blueprints are the hard dependency.
            print(f"!! could not create {definition['identifier']}: {exc}")
            print("   create it in the Port UI or via the MCP server, then re-run.")

    # Governance + interface. Non-fatal: the blueprints and automation are the hard
    # dependency; a rejected scorecard/dashboard should not fail the whole bootstrap.
    try:
        port.create_scorecard("scraper", SCORECARD)
    except Exception as exc:
        print(f"!! could not create scorecard scraper_reliability: {exc}")
    try:
        port.create_page(OPERATOR_DASHBOARD)
    except Exception as exc:
        print(f"!! could not create Factory Operator dashboard: {exc}")

    try:
        _seed_entities()
    except Exception as exc:
        print(f"!! could not seed scraper entities: {exc}")

    try:
        port.upsert_entity(
            "_ai_agent", TRIAGE_AGENT["identifier"], TRIAGE_AGENT["title"],
            TRIAGE_AGENT["properties"],
        )
        print("AI agent ready: triage")
    except Exception as exc:
        print(f"!! could not create the Triage agent: {exc}")
        print("   register it via the Port UI (AI Builder), then re-run.")

    print("\nbootstrap complete.")
    print("Remaining, best done in the Port UI:")
    print("  - Embed the SigNoz dashboard as an iframe widget on Factory Operator")
    print("  - Point HEAL_WEBHOOK_URL / BUILD_WEBHOOK_URL at a public tunnel, then re-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
