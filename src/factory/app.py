"""FastAPI surface.

Run under `opentelemetry-instrument` so these endpoints are traced automatically -- that is
what makes Port's automation calling /heal visible as a span rather than a README claim.

Endpoints:
  POST /heal        <- Port automation (health == drifting) AND the SigNoz alert webhook
  POST /build       <- Port self-service action: takes a brief, creates a verified scraper
  GET  /top20       <- the product: clickable top-20 upcoming titles, record links included
  GET  /api/top20   <- same, as JSON
  GET  /healthz
"""
from __future__ import annotations

import datetime as dt
import html as html_lib
from typing import Any

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import brightdata, pipeline, port, rank, telemetry
from .agents import healer, verifier

telemetry.init()
app = FastAPI(title="Scraper Factory")


class HealRequest(BaseModel):
    provider: str | None = None
    collector_id: str | None = None
    drift_description: str | None = None
    # Port automations post the whole entity diff; SigNoz posts a grouped alerts array.
    payload: dict[str, Any] | None = None


class BuildRequest(BaseModel):
    brief: str
    target_url: str
    provider: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/top20")
def api_top20() -> list[dict[str, Any]]:
    """Top-20 upcoming titles as JSON. Reads SQLite + TMDB cache; never scrapes."""
    return rank.compute()


@app.get("/top20", response_class=HTMLResponse)
def top20() -> str:
    """The product surface: what to record this week, one click to Philo's record page."""
    entries = rank.compute()
    if not entries:
        body = (
            '<p class="empty">No upcoming guide data yet — run the scrapers first: '
            "<code>make run</code>, then <code>make rank</code>.</p>"
        )
    else:
        cards = []
        for e in entries:
            title = html_lib.escape(e["title"])
            channel = html_lib.escape(str(e["channel"]))
            provider = html_lib.escape(str(e["provider"]))
            url = html_lib.escape(e["record_url"], quote=True)
            airing = _fmt_airing(e["next_airing_utc"])
            kind = "MOVIE" if e["kind"] == "movie" else "SHOW"
            new_badge = '<span class="badge new">NEW</span>' if e.get("is_new") else ""
            on_philo = "philo.com" in e["record_url"]
            btn = "Record on Philo" if on_philo else "View guide"
            cards.append(
                f'<a class="card" href="{url}" target="_blank" rel="noopener">'
                f'<span class="rank">{e["rank"]}</span>'
                f'<span class="meta"><span class="title">{title}</span>'
                f'<span class="sub">{channel} · {provider} · {airing}</span></span>'
                f'<span class="badges"><span class="badge">{kind}</span>{new_badge}'
                f'<span class="btn">{btn} →</span></span></a>'
            )
        body = "\n".join(cards)
    return _PAGE.replace("{{BODY}}", body).replace(
        "{{STAMP}}", dt.datetime.now().strftime("%b %d, %H:%M")
    )


def _fmt_airing(start_utc: str) -> str:
    try:
        t = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    except ValueError:
        return start_utc
    return t.strftime("%a %b %d, %H:%M UTC")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Top 20 upcoming — Scraper Factory</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body { background: #0d1117; color: #e6edf3; font: 16px/1.5 -apple-system, "Segoe UI", sans-serif;
         max-width: 780px; margin: 0 auto; padding: 2rem 1rem 4rem; }
  h1 { font-size: 1.5rem; margin-bottom: .25rem; }
  .stamp { color: #8b949e; font-size: .85rem; margin-bottom: 1.5rem; }
  .empty { color: #8b949e; margin-top: 2rem; }
  .card { display: flex; align-items: center; gap: 1rem; padding: .85rem 1rem; margin: .5rem 0;
          background: #161b22; border: 1px solid #30363d; border-radius: 10px;
          text-decoration: none; color: inherit; transition: border-color .15s; }
  .card:hover { border-color: #58a6ff; }
  .rank { font-size: 1.3rem; font-weight: 700; color: #58a6ff; min-width: 2.2rem; text-align: right; }
  .meta { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .title { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sub { color: #8b949e; font-size: .85rem; }
  .badges { display: flex; align-items: center; gap: .5rem; flex-shrink: 0; }
  .badge { font-size: .7rem; font-weight: 700; letter-spacing: .05em; color: #8b949e;
           border: 1px solid #30363d; border-radius: 999px; padding: .1rem .5rem; }
  .badge.new { color: #3fb950; border-color: #3fb950; }
  .btn { font-size: .85rem; font-weight: 600; color: #0d1117; background: #58a6ff;
         border-radius: 8px; padding: .35rem .7rem; white-space: nowrap; }
</style></head><body>
<h1>Top 20 upcoming this week</h1>
<p class="stamp">Ranked from scraped guide data + TMDB popularity · updated {{STAMP}}</p>
{{BODY}}
</body></html>"""


@app.post("/heal")
def heal(req: HealRequest, background: BackgroundTasks) -> dict[str, Any]:
    """Idempotent entry point shared by both trigger paths.

    Fast path: Port automation, seconds after the inline detector sets health=drifting.
    Safety net: SigNoz alert webhook, up to ~5 min later due to notification grouping.
    Whichever arrives first does the work; the second finds health already healthy.
    """
    provider, collector_id, drift = _extract(req)
    if not provider or not collector_id:
        return {"accepted": False, "reason": "could not resolve provider/collector_id"}

    telemetry.log().info("heal request accepted for %s (%s)", provider, collector_id)
    background.add_task(
        healer.heal, provider, collector_id, drift or "drift detected", "webhook"
    )
    return {"accepted": True, "provider": provider, "collector_id": collector_id}


@app.post("/build")
def build(req: BuildRequest) -> dict[str, Any]:
    """Port self-service action backend: a brief in, a verified scraper out."""
    with telemetry.tracer().start_as_current_span("factory.build") as span:
        span.set_attribute("target.provider", req.provider)
        span.set_attribute("target.url", req.target_url)

        prompt = f"{pipeline.PROMPT}\n\nOperator brief: {req.brief}"
        collector_id = brightdata.create(req.target_url, prompt)

        rows = brightdata.run(collector_id, [req.target_url])
        try:
            verdict = verifier.verify(req.provider, rows)
        except FileNotFoundError:
            # No fixture yet for a brand-new source: register unverified for a human to bless.
            verdict = None

        health = "healthy" if (verdict and verdict.passed) else "drifting"
        port.upsert_entity(
            "scraper",
            identifier=collector_id,
            title=req.provider,
            properties={
                "version": "1",
                "collector_id": collector_id,
                "prompt_text": prompt,
                "health": health,
                "verify_status": "pass" if (verdict and verdict.passed) else "fail",
            },
        )
        return {
            "collector_id": collector_id,
            "rows": len(rows),
            "verified": bool(verdict and verdict.passed),
            "trace_url": telemetry.trace_url(),
        }


def _extract(req: HealRequest) -> tuple[str | None, str | None, str | None]:
    """Pull provider/collector_id/drift out of either payload shape."""
    if req.provider and req.collector_id:
        return req.provider, req.collector_id, req.drift_description

    body = req.payload or {}

    # Port automation: the entity diff carries the scraper.
    entity = (
        body.get("diff", {}).get("after")
        or body.get("entity")
        or {}
    )
    if entity:
        props = entity.get("properties", {})
        return (
            entity.get("title") or props.get("provider_name"),
            props.get("collector_id") or entity.get("identifier"),
            props.get("drift_description") or "drift detected via Port automation",
        )

    # SigNoz webhook: grouped alerts array with labels.
    alerts = body.get("alerts") or []
    if alerts:
        labels = alerts[0].get("labels", {})
        return (
            labels.get("target_provider"),
            labels.get("scraper_id"),
            "drift detected via SigNoz alert",
        )

    return None, None, None
