"""FastAPI surface.

Run under `opentelemetry-instrument` so these endpoints are traced automatically -- that is
what makes Port's automation calling /heal visible as a span rather than a README claim.

Endpoints:
  POST /heal        <- Port automation (health == drifting) AND the SigNoz alert webhook
  POST /build       <- Port self-service action: takes a brief, creates a verified scraper
  POST /feature     <- Port self-service action: the Builder turns a requirement into a PR
  POST /run         <- Port self-service action: scrape one or all providers in the background
  POST /catalog     <- Port self-service action: refresh the Philo record-link catalog
  POST /publish     <- Port self-service action: rank + push the top-20 to Port
  GET  /top20       <- the product: clickable top-20 upcoming titles, record links included
  GET  /api/top20   <- same, as JSON
  GET  /healthz
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hmac
import html as html_lib
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brightdata, catalog, pipeline, port, portwatch, publish, rank, telemetry
from .agents import builder, healer, verifier

telemetry.init()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Port/Bright Data pull bridge as a background task, if Port creds exist.

    Port and Bright Data don't push their control-plane activity or budget; portwatch polls
    them and re-emits into telemetry. Guarded so a credential-less local run still boots.
    """
    task: asyncio.Task | None = None
    if portwatch.creds_present():
        task = asyncio.create_task(portwatch.watch())
        telemetry.log().info("portwatch background task scheduled")
    else:
        telemetry.log().info("portwatch disabled: Port credentials not set")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Scraper Factory", lifespan=lifespan)


def _require_token(request: Request) -> None:
    """Shared-secret gate on every webhook route (Port workflows/actions + SigNoz).

    Callers carry the token as ?token=... (baked into the provisioned webhook URLs by the
    bootstrap scripts) or an x-factory-token header. When FACTORY_WEBHOOK_TOKEN is unset
    the gate is open — local dev without provisioned webhooks still works.
    """
    expected = os.getenv("FACTORY_WEBHOOK_TOKEN", "")
    if not expected:
        return
    supplied = (
        request.query_params.get("token")
        or request.headers.get("x-factory-token")
        or ""
    )
    if not hmac.compare_digest(supplied, expected):
        telemetry.log().warning("rejected unauthenticated webhook call to %s", request.url.path)
        raise HTTPException(status_code=401, detail="missing or invalid factory token")


webhook_auth = [Depends(_require_token)]


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


@app.post("/heal", dependencies=webhook_auth)
def heal(req: HealRequest, background: BackgroundTasks) -> dict[str, Any]:
    """Idempotent entry point shared by both trigger paths.

    Fast path: Port automation, seconds after the inline detector sets health=drifting.
    Safety net: SigNoz alert webhook, up to ~5 min later due to notification grouping.
    Whichever arrives first does the work; the second finds health already healthy.
    """
    provider, collector_id, drift = _extract(req)
    if not provider or not collector_id:
        # Port Workflow webhook nodes may deliver unresolved/empty template fields; the
        # catalog itself is the source of truth, so fall back to the newest bad run there.
        provider, collector_id, drift = _resolve_bad_run_from_port()
    if not provider or not collector_id:
        return {"accepted": False, "reason": "could not resolve provider/collector_id"}

    telemetry.log().info("heal request accepted for %s (%s)", provider, collector_id)
    background.add_task(
        healer.heal, provider, collector_id, drift or "drift detected", "webhook"
    )
    return {"accepted": True, "provider": provider, "collector_id": collector_id}


@app.post("/build", dependencies=webhook_auth)
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


@app.post("/feature", dependencies=webhook_auth)
async def feature(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    """Port self-service action backend: a requirement in, a pull request out.

    The Builder runs locally (operator decision: Anthropic credentials never leave this
    machine) -- headless Claude Code edits a scratch clone, deterministic code opens the
    PR, and the CI gate + human merge stay the promotion gates.
    """
    body = await request.json()
    title, details, kind, run_id = _feature_fields(body)
    if not title or not details:
        return {"accepted": False, "reason": "could not resolve title/details from payload"}

    req_id = "req_" + re.sub(r"[^A-Za-z0-9_-]", "", run_id or dt.datetime.now().strftime("%Y%m%d%H%M%S"))
    port.upsert_entity(
        "requirement",
        identifier=req_id,
        title=title,
        properties={"description": details, "kind": kind, "status": "building"},
    )
    telemetry.log().info("feature request accepted: %s (%s)", title, req_id)
    background.add_task(builder.build_feature, req_id, title, details, kind, run_id)
    return {"accepted": True, "requirement": req_id}


def _resolve_bad_run_from_port() -> tuple[str | None, str | None, str | None]:
    """Newest error/drift scrape_run from the last hour -> (provider, collector_id, drift).

    Run identifiers encode the provider (run_<provider>_<hash>) and the scraper relation
    carries the collector id, so any trigger — however empty its payload — can be resolved
    against the Context Lake. Returns (None, None, None) when nothing recent is bad.
    """
    try:
        import httpx

        resp = httpx.get(
            f"{port.API}/blueprints/scrape_run/entities",
            headers=port._headers(),
            timeout=20,
        )
        resp.raise_for_status()
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        bad = [
            e
            for e in resp.json().get("entities", [])
            if (e.get("createdAt") or "") >= cutoff
            and (
                e["properties"].get("status") == "error"
                or e["properties"].get("drift_detected")
            )
        ]
        if not bad:
            return None, None, None
        newest = max(bad, key=lambda e: e.get("createdAt") or "")
        match = re.match(r"run_([a-z]+)_", newest["identifier"])
        provider = match.group(1) if match else None
        collector = (newest.get("relations") or {}).get("scraper")
        detail = newest["properties"].get("error_detail") or "bad run detected via Port"
        return provider, collector, detail
    except Exception as exc:
        telemetry.log().warning("could not resolve bad run from Port: %s", exc)
        return None, None, None


def _feature_fields(body: dict[str, Any]) -> tuple[str | None, str | None, str, str | None]:
    """Tolerate both a flat test payload and Port's nested action-run webhook body."""
    def _find(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            if key in obj and not isinstance(obj[key], (dict, list)):
                return obj[key]
            for v in obj.values():
                found = _find(v, key)
                if found is not None:
                    return found
        return None

    # User inputs live in some "properties" dict that has our field names.
    def _props(obj: Any) -> dict[str, Any] | None:
        if isinstance(obj, dict):
            props = obj.get("properties")
            if isinstance(props, dict) and "title" in props and "details" in props:
                return props
            for v in obj.values():
                found = _props(v)
                if found:
                    return found
        return None

    props = _props(body) or body
    title = props.get("title")
    details = props.get("details")
    kind = props.get("kind") or "other"
    run_id = _find(body, "runId") or _find(body, "run_id") or _find(body, "port_run_id")
    return title, details, kind, str(run_id) if run_id else None


@app.post("/run", dependencies=webhook_auth)
async def run(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    """Port self-service action backend: scrape one provider or all, in the background.

    Tolerant body like /feature: {provider?, days?, max_channels?} either flat or nested
    under a Port action-run "properties" dict. An omitted/empty/"all" provider fans out to
    every provider in pipeline.PROVIDERS. Returns immediately; the run happens in ONE
    background task that iterates providers sequentially -- Bright Data budget and the relay
    cache both assume providers don't scrape concurrently.
    """
    body = await request.json()
    provider, days, max_channels = _run_fields(body)
    providers = (
        list(pipeline.PROVIDERS)
        if not provider or provider.lower() == "all"
        else [provider]
    )
    telemetry.log().info(
        "run request accepted: providers=%s days=%d", providers, days
    )

    def _scrape_all() -> None:
        for name in providers:
            try:
                pipeline.run_once(name, days=days, max_channels=max_channels)
            except Exception as exc:  # the error scrape_run write is the durable record
                telemetry.log().error("background run for %s failed: %s", name, exc)

    background.add_task(_scrape_all)
    return {"accepted": True, "providers": providers, "days": days}


@app.post("/catalog", dependencies=webhook_auth)
def refresh_catalog(background: BackgroundTasks) -> dict[str, Any]:
    """Port self-service action backend: refresh the Philo record-link catalog in the background."""
    telemetry.log().info("catalog refresh request accepted")

    def _refresh() -> None:
        try:
            catalog.refresh()
        except Exception as exc:  # the catalog error scrape_run write is the durable record
            telemetry.log().error("background catalog refresh failed: %s", exc)

    background.add_task(_refresh)
    return {"accepted": True}


@app.post("/publish", dependencies=webhook_auth)
def publish_top20(background: BackgroundTasks) -> dict[str, Any]:
    """Port self-service action backend: rank + push the top-20 to Port, in the background."""
    telemetry.log().info("publish request accepted")

    def _publish() -> None:
        try:
            publish.push()
        except Exception as exc:
            telemetry.log().error("background publish failed: %s", exc)

    background.add_task(_publish)
    return {"accepted": True}


def _run_fields(body: dict[str, Any]) -> tuple[str | None, int, int | None]:
    """Pull provider/days/max_channels from a flat body or a Port nested "properties" dict."""
    def _props(obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            props = obj.get("properties")
            if isinstance(props, dict):
                return props
            for v in obj.values():
                found = _props(v)
                if found:
                    return found
        return {}

    src = body if isinstance(body, dict) else {}
    props = _props(src) or src
    provider = props.get("provider") or src.get("provider")

    def _int(val: Any, default: int | None) -> int | None:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    days = _int(props.get("days", src.get("days")), 1) or 1
    max_channels = _int(props.get("max_channels", src.get("max_channels")), None)
    return (str(provider) if provider else None), days, max_channels


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


# The multipage demo site (demo/) doubles as the service's front door: GET / serves the
# project story, /architecture.html the diagrams, etc. Mounted last so every API route
# above wins; StaticFiles only sees paths nothing else claimed.
_DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
if _DEMO_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_DEMO_DIR), html=True), name="demo")
