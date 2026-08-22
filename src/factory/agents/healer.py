"""The Healer + Promoter: repair a drifted scraper, but only ship a verified fix.

Flow: scraper heal -> awaiting_approval + preview_result -> Verifier diffs the preview
against the frozen fixture -> approve, or approve --reject and escalate to a human.
"""
from __future__ import annotations

import datetime as dt
import uuid

from .. import brightdata, port, telemetry
from . import verifier


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _emit_outcome(outcome: str, provider: str, trigger: str) -> None:
    """One heal.events datapoint per terminal outcome (repaired/escalated/rejected)."""
    telemetry.metric("heal_events").add(
        1, {"outcome": outcome, "trigger": trigger, "target.provider": provider}
    )


def heal(provider: str, collector_id: str, drift_description: str, trigger_rule: str = "manual") -> bool:
    """Returns True if a verified fix was promoted.

    Idempotent enough to be called from both trigger paths (the fast inline detector and
    the slower SigNoz alert webhook) without double-promoting.
    """
    with telemetry.tracer().start_as_current_span("scrape.heal.reprompt") as span:
        span.set_attribute("target.provider", provider)
        span.set_attribute("scraper.id", collector_id)
        span.set_attribute("run.mode", "heal")
        span.set_attribute("heal.attempt", 1)

        trace_url = telemetry.trace_url()
        heal_id = f"heal_{provider}_{uuid.uuid4().hex[:8]}"
        telemetry.log().info("heal requested for %s: %s", provider, drift_description)

        proposal = brightdata.heal(collector_id, drift_description)
        rows_before = 0
        rows_after = len(proposal.preview_result)
        span.set_attribute("heal.preview_rows", rows_after)

        if not proposal.awaiting_approval:
            _record(heal_id, collector_id, trigger_rule, drift_description,
                    drift_description, "escalated", rows_before, rows_after, trace_url)
            _emit_outcome("escalated", provider, trigger_rule)
            port.set_scraper_health(collector_id, "broken", verify_status="fail")
            telemetry.log().error(
                "heal for %s returned status=%s, not awaiting_approval -- escalating",
                provider, proposal.status,
            )
            return False

        # The gate. Deterministic, no model in the loop. The guide scraper is ONE
        # collector shared across providers, so its fixture is the shared channel page --
        # keyed by scraper, not by provider.
        try:
            verdict = verifier.verify("channel", proposal.preview_result)
        except FileNotFoundError as exc:
            # Reject the pending proposal before escalating -- a dangling heal blocks
            # every future heal on this collector ("Another refactor job in progress").
            try:
                brightdata.approve(collector_id, reject=True)
            except Exception as reject_exc:
                telemetry.log().error("could not reject dangling heal: %s", reject_exc)
            _record(heal_id, collector_id, trigger_rule, drift_description,
                    drift_description, "escalated", rows_before, rows_after, trace_url)
            _emit_outcome("escalated", provider, trigger_rule)
            port.set_scraper_health(collector_id, "broken", verify_status="fail")
            telemetry.log().error("heal for %s cannot be verified (%s) -- escalating", provider, exc)
            return False
        span.set_attribute("verify.status", "pass" if verdict.passed else "fail")

        if verdict.passed:
            brightdata.approve(collector_id)
            telemetry.metric("heal_success").add(1, {"target.provider": provider})
            _record(heal_id, collector_id, trigger_rule, drift_description,
                    drift_description, "healed", rows_before, rows_after, trace_url)
            _emit_outcome("repaired", provider, trigger_rule)
            port.set_scraper_health(
                collector_id, "healthy", verify_status="pass", last_verified_at=_now()
            )
            telemetry.log().info("heal PROMOTED for %s", provider)
            return True

        brightdata.approve(collector_id, reject=True)
        _record(heal_id, collector_id, trigger_rule, drift_description,
                drift_description, "rolled_back", rows_before, rows_after, trace_url)
        _emit_outcome("rejected", provider, trigger_rule)
        port.set_scraper_health(collector_id, "broken", verify_status="fail")
        telemetry.log().error(
            "heal REJECTED for %s (%s) -- escalated to a human in Port",
            provider, verdict.summary(),
        )
        return False


def _record(
    heal_id: str,
    collector_id: str,
    trigger_rule: str,
    drift_description: str,
    heal_prompt: str,
    outcome: str,
    rows_before: int,
    rows_after: int,
    trace_url: str | None,
) -> None:
    """Write the heal_event into Port's Context Lake. This is what the operator dashboard reads."""
    try:
        port.upsert_entity(
            "heal_event",
            identifier=heal_id,
            title=f"{outcome}: {collector_id}",
            properties={
                "trigger_rule": trigger_rule,
                "drift_description": drift_description,
                "heal_prompt": heal_prompt,
                "outcome": outcome,
                "rows_before": rows_before,
                "rows_after": rows_after,
                "trace_url": trace_url,
            },
            relations={"scraper": collector_id},
        )
    except Exception as exc:  # never let bookkeeping break the repair path
        telemetry.log().warning("could not write heal_event to Port: %s", exc)
