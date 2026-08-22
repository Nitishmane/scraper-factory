"""Publish the top-20 to Port's Context Lake, where the webapp reads it.

The factory runs on a laptop; the webapp runs on Vercel. The seam between them is the
lake itself: `python -m factory publish` computes the ranking and upserts the 20
ranked_title entities, and the webapp reads them straight from the Port API. One backend,
one source of truth -- the product surface is a Context Lake consumer like everything else.
"""
from __future__ import annotations

from typing import Any

from . import rank, telemetry


def push(top: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Rank and upsert the top-20 into the Context Lake. Returns a summary dict."""
    with telemetry.tracer().start_as_current_span("publish.push") as span:
        if top is None:
            top = rank.compute()
        if not top:
            raise RuntimeError("nothing to publish -- run the scrapers, then rank")
        span.set_attribute("publish.rows", len(top))
        rank.write_to_port(top)
        telemetry.log().info("published %d ranked titles to the Context Lake", len(top))
        return {"published": len(top)}
