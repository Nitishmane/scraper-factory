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

import httpx  # noqa: E402

from factory import port, telemetry  # noqa: E402

def _tokened(url: str) -> str:
    """Append the shared webhook token so only our Port org can drive the factory.

    The token rides as a query param (not a header) because Port workflow webhook nodes,
    automations, actions, and the SigNoz channel all accept a URL verbatim — one mechanism
    covers every caller. The FastAPI side verifies it constant-time on every webhook route.
    """
    token = os.getenv("FACTORY_WEBHOOK_TOKEN", "")
    if not token:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={token}"


HEAL_WEBHOOK = _tokened(os.getenv("HEAL_WEBHOOK_URL", "http://localhost:8000/heal"))
BUILD_WEBHOOK = _tokened(os.getenv("BUILD_WEBHOOK_URL", "http://localhost:8000/build"))
# The Claude builder runs locally behind the FastAPI service (no Anthropic creds in GitHub
# Actions), so the feature-request action posts to /feature. Point FEATURE_WEBHOOK_URL at a
# public tunnel in .env, exactly like HEAL_WEBHOOK_URL / BUILD_WEBHOOK_URL.
FEATURE_WEBHOOK = _tokened(os.getenv("FEATURE_WEBHOOK_URL", "http://localhost:8000/feature"))

# Endpoints driven by Port Workflows + self-service actions (Phase 3).
RUN_WEBHOOK = _tokened(os.getenv("RUN_WEBHOOK_URL", "http://localhost:8000/run"))
CATALOG_WEBHOOK = _tokened(os.getenv("CATALOG_WEBHOOK_URL", "http://localhost:8000/catalog"))
PUBLISH_WEBHOOK = _tokened(os.getenv("PUBLISH_WEBHOOK_URL", "http://localhost:8000/publish"))

# The production scrape path is laptop-free: Port dispatches the weekly-scrape GitHub Actions
# workflow, which runs scrape -> heal-if-drifted -> catalog -> publish inside the runner (see
# .github/workflows/weekly-scrape.yml). The *_WEBHOOK endpoints above remain for the optional
# always-on dev/demo service. Auth: a fine-grained GitHub PAT (this repo only, Actions:write)
# stored as the Port org secret `github_pat` (Port UI -> Credentials -> Secrets, or
# POST /v1/organization/secrets); Port resolves {{ .secrets.github_pat }} at invocation.
# Underscore name on purpose: the dashed bracket form ({{ .secrets["..."] }}) did not resolve.
GITHUB_REPO = os.getenv("GITHUB_DISPATCH_REPO", "Nitishmane/scraper-factory")
GITHUB_DISPATCH_URL = (
    f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/weekly-scrape.yml/dispatches"
)
GITHUB_DISPATCH_HEADERS = {
    "Authorization": "Bearer {{ .secrets.github_pat }}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

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
            "error_detail": {"type": "string", "title": "Error detail"},
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

# The Claude-builder workforce: an operator's feature request becomes a Requirement, the
# GitHub workflow builds it on a branch and opens a PR, and a Deployment records the result.
REQUIREMENT = {
    "identifier": "requirement",
    "title": "Requirement",
    "schema": {
        "properties": {
            "description": {"type": "string", "title": "Description", "format": "markdown"},
            "kind": {
                "type": "string",
                "title": "Kind",
                "enum": ["provider_add", "webapp_change", "other"],
            },
            "status": {
                "type": "string",
                "title": "Status",
                "enum": ["requested", "building", "in_review", "deployed", "failed"],
                "enumColors": {
                    "requested": "lightGray",
                    "building": "blue",
                    "in_review": "yellow",
                    "deployed": "green",
                    "failed": "red",
                },
            },
            "branch": {"type": "string", "title": "Branch"},
            "pr_url": {"type": "string", "title": "PR", "format": "url"},
        },
        "required": ["status"],
    },
    "relations": {},
}

# A stock "deployment" blueprint already exists in this org (Port quickstart) with a
# different schema, so ours is registered as `factory_deployment` to avoid clobbering it.
DEPLOYMENT = {
    "identifier": "factory_deployment",
    "title": "Deployment",
    "icon": "Rocket",
    "schema": {
        "properties": {
            "environment": {"type": "string", "title": "Environment"},
            "url": {"type": "string", "title": "URL", "format": "url"},
            "commit_sha": {"type": "string", "title": "Commit SHA"},
            "status": {
                "type": "string",
                "title": "Status",
                "enum": ["success", "failure"],
                "enumColors": {"success": "green", "failure": "red"},
            },
            "deployed_at": {"type": "string", "title": "Deployed at", "format": "date-time"},
        }
    },
    "relations": {
        "requirement": {"target": "requirement", "required": False, "many": False}
    },
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
    "title": "Onboard data source \u2192 Bright Data scraper",
    "description": "Submit a brief; the factory creates a Bright Data collector, runs it, and verifies it against the fixture before registering the scraper",
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

# Self-service front door for the Claude builder: creates a Requirement, then posts to the
# FastAPI /feature endpoint. The builder runs locally behind that service (no Anthropic creds
# in GitHub Actions), so this is a WEBHOOK invocation like request_data_source, not a GitHub
# workflow dispatch. Port sends the run + entity payload; the endpoint reads inputs from it.
FEATURE_REQUEST_ACTION = {
    "identifier": "submit_feature_request",
    "title": "Feature request \u2192 Claude Builder PR",
    "description": "File a requirement; the local Claude Code builder implements it and opens a GitHub PR that must pass the deterministic CI gate and a human merge",
    "trigger": {
        "type": "self-service",
        "operation": "CREATE",
        "blueprintIdentifier": "requirement",
        "userInputs": {
            "properties": {
                "title": {"type": "string", "title": "Title"},
                "details": {"type": "string", "title": "Details", "format": "markdown"},
                "kind": {
                    "type": "string",
                    "title": "Kind",
                    "enum": ["provider_add", "webapp_change", "other"],
                    "default": "webapp_change",
                },
            },
            "required": ["title", "details"],
        },
    },
    "invocationMethod": {"type": "WEBHOOK", "url": FEATURE_WEBHOOK},
    "publish": True,
}

# --- Port Workflows (Phase 3) -------------------------------------------------------------
# Port Workflows is GA on this org. Shapes below were confirmed empirically against the live
# POST/PUT /v1/workflows API (the public OpenAPI spec does not yet document it):
#
#   workflow  = {identifier, title, nodes:[...], connections:[...]}
#   trigger node.config (schedule) = {type:"SCHEDULE_TRIGGER", cron:"m h dom mon dow"}
#              (5-field cron, UTC, 5-min minimum interval; `published` defaults to true)
#   trigger node.config (event)    = {type:"EVENT_TRIGGER",
#                                     event:{type:"ENTITY_CREATED"|"ENTITY_UPDATED",
#                                            blueprintIdentifier:"..."},
#                                     condition:{type:"JQ", expressions:[...],
#                                                combinator:"and"|"or"}}   # condition optional
#   webhook  node.config           = {type:"WEBHOOK", method:"POST", url, body:{...}}
#   agent    node.config           = {type:"AI_AGENT", agentIdentifier, userPrompt}
#   connections                    = [{sourceIdentifier, targetIdentifier}, ...]
#
# JQ conditions and webhook/prompt bodies reference the trigger entity the same way the
# existing automations do -- .diff.after.properties.<x> -- addressed as .trigger.event.diff.*
# inside a workflow. The API stores these template strings verbatim; Port resolves them at run.

WF_SCHEDULED_SCRAPE = {
    "identifier": "wf_scheduled_scrape",
    "title": "Weekly scrape (all providers, laptop-free)",
    "nodes": [
        {
            "identifier": "trigger",
            "title": "Mondays at 08:00 UTC",
            "config": {"type": "SCHEDULE_TRIGGER", "cron": "0 8 * * 1", "published": True},
        },
        {
            "identifier": "run_scrape",
            "title": "Dispatch the weekly-scrape GitHub Actions workflow",
            # Dispatches GitHub Actions instead of the laptop /run webhook: the runner hosts
            # the whole pipeline (scrape -> heal-if-drifted -> catalog -> publish) so nothing
            # depends on the operator's machine. workflow_dispatch inputs are strings.
            "config": {"type": "WEBHOOK", "method": "POST", "url": GITHUB_DISPATCH_URL,
                       "headers": GITHUB_DISPATCH_HEADERS,
                       "body": {"ref": "main",
                                "inputs": {"provider": "all", "days": "7"}}},
        },
    ],
    "connections": [{"sourceIdentifier": "trigger", "targetIdentifier": "run_scrape"}],
}

WF_SCHEDULED_CATALOG = {
    "identifier": "wf_scheduled_catalog",
    "title": "Scheduled catalog refresh",
    "nodes": [
        {
            "identifier": "trigger",
            "title": "Daily at 07:30 UTC (paused: catalog refresh moved into the weekly job)",
            # Unpublished: scripts/weekly_run.py refreshes the catalog inside the GitHub
            # Actions run, and this webhook only resolves when the dev/demo laptop service is
            # up. Flip published back to True if an always-on /catalog endpoint returns.
            "config": {"type": "SCHEDULE_TRIGGER", "cron": "30 7 * * *", "published": False},
        },
        {
            "identifier": "refresh_catalog",
            "title": "Refresh the Philo catalog",
            "config": {"type": "WEBHOOK", "method": "POST", "url": CATALOG_WEBHOOK, "body": {}},
        },
    ],
    "connections": [{"sourceIdentifier": "trigger", "targetIdentifier": "refresh_catalog"}],
}

# Publishing now happens inside the weekly GitHub Actions job (scripts/weekly_run.py calls
# publish.push() right after the scrape), so this workflow is unpublished: with no always-on
# /publish endpoint it would log a failed invocation after every weekly scrape_run entity.
# Flip published back to True if the dev/demo laptop service becomes the scrape path again.
WF_PUBLISH_ON_SUCCESS = {
    "identifier": "wf_publish_on_success",
    "title": "Publish top-20 on a successful run (paused: publish moved into the weekly job)",
    "nodes": [
        {
            "identifier": "trigger",
            "title": "scrape_run succeeded",
            "config": {
                "type": "EVENT_TRIGGER",
                "event": {"type": "ENTITY_CREATED", "blueprintIdentifier": "scrape_run"},
                "condition": {
                    "type": "JQ",
                    "expressions": ['.diff.after.properties.status == "success"'],
                    "combinator": "and",
                },
                "published": False,
            },
        },
        {
            "identifier": "publish",
            "title": "Publish the ranked top-20",
            "config": {"type": "WEBHOOK", "method": "POST", "url": PUBLISH_WEBHOOK, "body": {}},
        },
    ],
    "connections": [{"sourceIdentifier": "trigger", "targetIdentifier": "publish"}],
}

# A bad run (error, or drift detected) triages then heals. The triage AI agent authors the
# plain-language SYMPTOM description the healer sends to Bright Data (never a guessed fix, per
# CLAUDE.md); the deterministic Verifier still owns promotion downstream of /heal.
# NOTE: /heal only resolves while the dev/demo laptop service is up. The weekly GitHub Actions
# job heals drift in-job (scripts/weekly_run.py), so this workflow is the *between-runs* path
# and is expected to no-op (failed invocation) when no always-on service exists.
WF_HEAL_ON_BAD_RUN = {
    "identifier": "wf_heal_on_bad_run",
    "title": "Triage + heal on a bad run",
    "nodes": [
        {
            "identifier": "trigger",
            "title": "scrape_run failed or drifted",
            "config": {
                "type": "EVENT_TRIGGER",
                "event": {"type": "ENTITY_CREATED", "blueprintIdentifier": "scrape_run"},
                "condition": {
                    "type": "JQ",
                    "expressions": [
                        '.diff.after.properties.status == "error"',
                        ".diff.after.properties.drift_detected == true",
                    ],
                    "combinator": "or",
                },
                "published": True,
            },
        },
        {
            "identifier": "triage",
            "title": "Triage: author a drift description",
            "config": {
                "type": "AI_AGENT",
                "agentIdentifier": "triage",
                "userPrompt": (
                    "A scrape run just failed or drifted. Run: {{ .trigger.event.diff.after.title }}. "
                    "status={{ .trigger.event.diff.after.properties.status }}, "
                    "rows={{ .trigger.event.diff.after.properties.rows_returned }}, "
                    "drift_detected={{ .trigger.event.diff.after.properties.drift_detected }}, "
                    "error_detail={{ .trigger.event.diff.after.properties.error_detail }}. "
                    "Read the scraper's recent scrape_run and heal_event history and author a "
                    "plain-language description of the observed SYMPTOM (what changed in the "
                    "data), never a guess at the fix. Keep it under 400 characters."
                ),
            },
        },
        {
            "identifier": "heal",
            "title": "Invoke the healer",
            "config": {
                "type": "WEBHOOK",
                "method": "POST",
                "url": HEAL_WEBHOOK,
                "body": {
                    "provider": "{{ .trigger.event.diff.after.title }}",
                    "collector_id": "{{ .trigger.event.diff.after.relations.scraper }}",
                    "drift_description": "{{ .triage.output }}",
                },
            },
        },
    ],
    "connections": [
        {"sourceIdentifier": "trigger", "targetIdentifier": "triage"},
        {"sourceIdentifier": "triage", "targetIdentifier": "heal"},
    ],
}

# Fallback bodies for the two event-driven workflows if the /v1/workflows API rejects the
# AI_AGENT node or a workflow shape after honest iteration: express them as plain automations
# (the proven pattern in this file). The healer then gets a static description instead of an
# agent-authored one -- still a valid symptom string, just less specific.
PUBLISH_AUTOMATION_FALLBACK = {
    "identifier": "publish_on_success",
    "title": "Publish top-20 on a successful run",
    "description": "Publishes the ranked top-20 when a scrape_run turns success",
    "trigger": {
        "type": "automation",
        "event": {"type": "ENTITY_CREATED", "blueprintIdentifier": "scrape_run"},
        "condition": {
            "type": "JQ",
            "expressions": ['.diff.after.properties.status == "success"'],
            "combinator": "and",
        },
    },
    "invocationMethod": {"type": "WEBHOOK", "url": PUBLISH_WEBHOOK},
    "publish": True,
}

HEAL_ON_BAD_RUN_AUTOMATION_FALLBACK = {
    "identifier": "heal_on_bad_run",
    "title": "Heal on a bad run",
    "description": "Invokes the healer when a scrape_run errors or reports drift",
    "trigger": {
        "type": "automation",
        "event": {"type": "ENTITY_CREATED", "blueprintIdentifier": "scrape_run"},
        "condition": {
            "type": "JQ",
            "expressions": [
                '.diff.after.properties.status == "error"',
                ".diff.after.properties.drift_detected == true",
            ],
            "combinator": "or",
        },
    },
    "invocationMethod": {"type": "WEBHOOK", "url": HEAL_WEBHOOK},
    "publish": True,
}

SCHEDULED_WORKFLOWS = (WF_SCHEDULED_SCRAPE, WF_SCHEDULED_CATALOG)
EVENT_WORKFLOWS = (WF_PUBLISH_ON_SUCCESS, WF_HEAL_ON_BAD_RUN)

# --- Self-service actions (Phase 3) -------------------------------------------------------
# BUILD_ACTION / WEBHOOK pattern, but with no CREATE blueprint operation -- these are pure
# "run this now" buttons that post straight to the pipeline endpoints. operation DAY-2 with no
# blueprint is how Port models a global self-service button.
RUN_SCRAPE_NOW_ACTION = {
    "identifier": "run_scrape_now",
    "title": "Scrape TV guides \u2192 Bright Data collectors",
    "description": "Dispatch the weekly-scrape GitHub Actions workflow now (one provider or all); the runner scrapes via the Bright Data fetch/extract relay and results land as scrape_run entities",
    "trigger": {
        "type": "self-service",
        "operation": "DAY-2",
        "userInputs": {
            "properties": {
                "provider": {
                    "type": "string",
                    "title": "Provider",
                    "enum": ["philo", "youtubetv", "sling", "all"],
                    "default": "all",
                },
                # String, not number: GitHub workflow_dispatch inputs must be strings, and Port
                # substitutes a lone {{ .inputs.x }} template with the input's native type.
                "days": {"type": "string", "title": "Days", "default": "1"},
            },
            "required": [],
        },
    },
    "invocationMethod": {
        "type": "WEBHOOK",
        "url": GITHUB_DISPATCH_URL,
        "headers": GITHUB_DISPATCH_HEADERS,
        "body": {
            "ref": "main",
            "inputs": {
                "provider": "{{ .inputs.provider }}",
                "days": "{{ .inputs.days }}",
            },
        },
    },
    "publish": True,
}

REFRESH_CATALOG_NOW_ACTION = {
    "identifier": "refresh_catalog_now",
    "title": "Refresh Philo record-link catalog",
    "description": "Re-scrape philo.com/go/allshows + /go/allmovies so ranked titles resolve to real Record pages",
    "trigger": {
        "type": "self-service",
        "operation": "DAY-2",
        "userInputs": {"properties": {}, "required": []},
    },
    "invocationMethod": {"type": "WEBHOOK", "url": CATALOG_WEBHOOK},
    "publish": True,
}

PUBLISH_NOW_ACTION = {
    "identifier": "publish_now",
    "title": "Publish top-10s \u2192 Context Lake",
    "description": "Re-rank (TMDB scoring) and upsert the top-10 shows + top-10 movies as ranked_title entities; the Vercel webapp reads them live",
    "trigger": {
        "type": "self-service",
        "operation": "DAY-2",
        "userInputs": {"properties": {}, "required": []},
    },
    "invocationMethod": {"type": "WEBHOOK", "url": PUBLISH_WEBHOOK},
    "publish": True,
}

SELF_SERVICE_ACTIONS = (RUN_SCRAPE_NOW_ACTION, REFRESH_CATALOG_NOW_ACTION, PUBLISH_NOW_ACTION)


def _upsert_workflow(defn: dict) -> None:
    """Idempotent workflow create-or-update against POST/PUT /v1/workflows.

    Raises RuntimeError on a non-2xx so main() can fall back to a plain automation. Kept in
    this script (not factory.port) so teammates editing port.py nearby are undisturbed.
    """
    ident = defn["identifier"]
    resp = httpx.post(
        f"{port.API}/workflows", json=defn, headers=port._headers(), timeout=30
    )
    # An existing identifier 409s ("Unique constraint failed on the orgId,identifier field");
    # a shape re-validation can 422. Either way PUT updates it in place. Mirrors
    # port.create_blueprint / create_automation, which branch on the status code alone.
    if resp.status_code in (409, 422):
        resp = httpx.put(
            f"{port.API}/workflows/{ident}", json=defn, headers=port._headers(), timeout=30
        )
    if resp.status_code >= 300:
        raise RuntimeError(f"workflow {ident}: {resp.status_code} {resp.text}")
    print(f"workflow ready: {ident}")


def _provision_workflows() -> None:
    """Create the four workflows; on honest failure of an event workflow, fall back to an
    automation. Scheduled workflows have no automation equivalent -- if they fail, print the
    exact UI config an operator must enter instead."""
    for wf in SCHEDULED_WORKFLOWS:
        try:
            _upsert_workflow(wf)
        except Exception as exc:
            cron = wf["nodes"][0]["config"]["cron"]
            url = wf["nodes"][1]["config"]["url"]
            body = wf["nodes"][1]["config"].get("body", {})
            print(f"!! could not create scheduled workflow {wf['identifier']}: {exc}")
            print("   NO automation equivalent for a schedule -- create in the Port UI:")
            print(f"     Workflows -> New -> Trigger: Schedule, cron '{cron}' (UTC)")
            print(f"     -> Webhook node: POST {url}  body {body}")

    for wf in EVENT_WORKFLOWS:
        try:
            _upsert_workflow(wf)
        except Exception as exc:
            print(f"!! could not create event workflow {wf['identifier']}: {exc}")
            fallback = (
                PUBLISH_AUTOMATION_FALLBACK
                if wf is WF_PUBLISH_ON_SUCCESS
                else HEAL_ON_BAD_RUN_AUTOMATION_FALLBACK
            )
            print(f"   falling back to automation {fallback['identifier']} "
                  "(no AI-agent triage; healer gets no drift description)")
            try:
                port.create_automation(fallback)
                print(f"automation (fallback) ready: {fallback['identifier']}")
            except Exception as exc2:
                print(f"!! fallback automation {fallback['identifier']} also failed: {exc2}")


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

# Read-only narrator of the heal loop. Automatic (no approval): it only reads and explains.
# `tools` is a regex allowlist; the live _ai_agent schema stores it as an array of patterns.
HEAL_EXPLAINER_AGENT = {
    "identifier": "heal_explainer",
    "title": "Heal Explainer",
    "properties": {
        "description": "Narrates heal events: what drifted, what the healer tried, why the "
                       "deterministic Verifier approved or rejected.",
        "status": "active",
        "execution_mode": "Automatic",
        "tools": ["^(list|get|search|describe)_.*"],
        "prompt": "You are the Heal Explainer for a self-healing scraper factory. Given a "
                  "scraper or heal_event, read heal_event, scrape_run and scraper entities "
                  "and narrate: what drifted, what the healer tried, and why the "
                  "deterministic Verifier approved or rejected it. Quote drift_description "
                  "and outcome verbatim. You only read and explain; you never trigger heals "
                  "or approve anything.",
        "conversation_starters": [
            "Explain the latest heal event",
            "Why was the last heal rolled back?",
            "Which scrapers healed this week?",
        ],
    },
}

# Operator Q&A. Approval Required because it can trigger submit_feature_request.
ONCALL_ASSISTANT_AGENT = {
    "identifier": "oncall_assistant",
    "title": "On-Call Assistant",
    "properties": {
        "description": "Operator Q&A over factory health; can file a feature request to "
                       "kick off the Claude builder (with approval).",
        "status": "active",
        "execution_mode": "Approval Required",
        "tools": ["^(list|get|search|describe)_.*|^run_submit_feature_request$"],
        "prompt": "You are the On-Call Assistant for the scraper factory. Answer operator "
                  "questions from scraper, scrape_run, heal_event, requirement and "
                  "deployment entities: current health, last good run, open drift, recent "
                  "deploys. If heal history shows repeated rolled_back outcomes, recommend "
                  "escalation and, if asked, trigger submit_feature_request to file a fix. "
                  "Never mark anything healthy yourself; the Verifier owns promotion.",
        "conversation_starters": [
            "What is broken right now?",
            "Summarize factory health",
            "File a fix request for the broken scraper",
        ],
    },
}

# Read-only coverage analyst over the ranked top titles.
CATALOG_COVERAGE_AGENT = {
    "identifier": "catalog_coverage",
    "title": "Catalog Coverage",
    "properties": {
        "description": "Reports record-link coverage of the ranked top titles and where "
                       "catalog gaps hurt most.",
        "status": "active",
        "execution_mode": "Automatic",
        "tools": ["^(list|get|search|describe)_.*"],
        "prompt": "You are the Catalog Coverage analyst for the scraper factory. Read "
                  "ranked_title entities and report coverage: how many top titles have a "
                  "philo.com record_url versus guide-link fallbacks, which channels and "
                  "providers dominate, and how coverage moved since the last publish. "
                  "Suggest which catalog gaps matter most. You only read and report; you "
                  "never modify entities or trigger actions.",
        "conversation_starters": [
            "What is record-link coverage today?",
            "Which top titles lack record links?",
            "Which provider dominates the top 20?",
        ],
    },
}

NEW_AGENTS = (HEAL_EXPLAINER_AGENT, ONCALL_ASSISTANT_AGENT, CATALOG_COVERAGE_AGENT)

# The two deployable services, on the org's existing (empty) `service` blueprint. That
# blueprint carries only `criticality` and a github_repository relation -- no url property --
# so the webapp URL lives on its factory_deployment entities, not here. The github_repository
# relation is skipped unless a matching repo entity was ingested (the GitHub app is not on
# this repo, so it usually is not).
SERVICE_ENTITIES = (
    {"identifier": "scraper_factory", "title": "scraper-factory",
     "properties": {"criticality": "high"}, "repo": "scraper-factory"},
    {"identifier": "top20_webapp", "title": "top20-webapp",
     "properties": {"criticality": "medium"}, "repo": "scraper-factory-top20"},
)


def _seed_services() -> None:
    """Upsert the two service entities, relating to a github repo only if one exists."""
    for svc in SERVICE_ENTITIES:
        relations = {}
        repo = svc.get("repo")
        if repo:
            resp = httpx.get(
                f"{port.API}/blueprints/githubRepository/entities/{repo}",
                headers=port._headers(),
                timeout=30,
            )
            if resp.status_code < 300:
                relations = {"github_repository": repo}
            else:
                print(f"   github repo entity '{repo}' not ingested; skipping relation")
        port.upsert_entity(
            "service", svc["identifier"], svc["title"], svc["properties"], relations,
        )
        print(f"service entity ready: {svc['identifier']}")


def main() -> int:
    telemetry.init()
    # REQUIREMENT must precede DEPLOYMENT: factory_deployment relates to it.
    for blueprint in (
        DATA_SOURCE, SCRAPER, SCRAPE_RUN, HEAL_EVENT, RANKED_TITLE,
        REQUIREMENT, DEPLOYMENT,
    ):
        port.create_blueprint(blueprint)

    for definition in (
        HEAL_AUTOMATION, BUILD_ACTION, FEATURE_REQUEST_ACTION, *SELF_SERVICE_ACTIONS,
    ):
        try:
            port.create_automation(definition)
        except Exception as exc:
            # Action/automation payload shapes evolve; blueprints are the hard dependency.
            print(f"!! could not create {definition['identifier']}: {exc}")
            print("   create it in the Port UI or via the MCP server, then re-run.")

    # Port Workflows: two scheduled, two event-driven (with automation fallback).
    try:
        _provision_workflows()
    except Exception as exc:
        print(f"!! workflow provisioning failed: {exc}")

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

    for agent in (TRIAGE_AGENT, *NEW_AGENTS):
        try:
            port.upsert_entity(
                "_ai_agent", agent["identifier"], agent["title"], agent["properties"],
            )
            print(f"AI agent ready: {agent['identifier']}")
        except Exception as exc:
            print(f"!! could not create the {agent['identifier']} agent: {exc}")
            print("   register it via the Port UI (AI Builder), then re-run.")

    try:
        _seed_services()
    except Exception as exc:
        print(f"!! could not seed service entities: {exc}")

    print("\nbootstrap complete.")
    print("Remaining, best done in the Port UI:")
    print("  - Embed the SigNoz dashboard as an iframe widget on Factory Operator")
    print("  - Point HEAL_WEBHOOK_URL / BUILD_WEBHOOK_URL at a public tunnel, then re-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
