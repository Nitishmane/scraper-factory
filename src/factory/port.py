"""Port client: auth, blueprints, and Context Lake entity upserts.

Everything writes with upsert=true&merge=true so create and update share one code path.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from . import telemetry

API = os.getenv("PORT_API_BASE", "https://api.port.io/v1")
CLIENT_ID = os.getenv("PORT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("PORT_CLIENT_SECRET", "")

_token: str | None = None
_token_expiry: float = 0.0


class PortError(RuntimeError):
    pass


def token() -> str:
    """Cached bearer token. Short-lived, so fetch once per process and refresh lazily."""
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 60:
        return _token
    if not CLIENT_ID or not CLIENT_SECRET:
        raise PortError("PORT_CLIENT_ID / PORT_CLIENT_SECRET not set (see .env.example)")
    resp = httpx.post(
        f"{API}/auth/access_token",
        json={"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    _token = body["accessToken"]
    _token_expiry = time.time() + int(body.get("expiresIn", 3600))
    return _token


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}


def create_blueprint(definition: dict[str, Any]) -> None:
    """Idempotent: an existing blueprint is PUT rather than treated as an error."""
    ident = definition["identifier"]
    resp = httpx.post(f"{API}/blueprints", json=definition, headers=_headers(), timeout=30)
    if resp.status_code in (409, 422):
        resp = httpx.put(
            f"{API}/blueprints/{ident}", json=definition, headers=_headers(), timeout=30
        )
    if resp.status_code >= 300:
        raise PortError(f"blueprint {ident}: {resp.status_code} {resp.text}")
    telemetry.log().info("blueprint ready: %s", ident)


def upsert_entity(
    blueprint: str,
    identifier: str,
    title: str,
    properties: dict[str, Any],
    relations: dict[str, Any] | None = None,
) -> None:
    payload = {
        "identifier": identifier,
        "title": title,
        "properties": {k: v for k, v in properties.items() if v is not None},
        "relations": relations or {},
    }
    with telemetry.tracer().start_as_current_span("port.upsert") as span:
        span.set_attribute("port.blueprint", blueprint)
        span.set_attribute("port.identifier", identifier)
        started = time.perf_counter()
        success = False
        try:
            resp = httpx.post(
                f"{API}/blueprints/{blueprint}/entities",
                params={"upsert": "true", "merge": "true"},
                json=payload,
                headers=_headers(),
                timeout=30,
            )
            success = resp.status_code < 300
            span.set_attribute("http.status_code", resp.status_code)
            if not success:
                raise PortError(
                    f"entity {blueprint}/{identifier}: {resp.status_code} {resp.text}"
                )
        finally:
            telemetry.metric("port_write_duration").record(
                time.perf_counter() - started,
                {"blueprint": blueprint, "success": success},
            )


def set_scraper_health(scraper_id: str, health: str, **extra: Any) -> None:
    """Setting health='drifting' is what fires the Port automation that invokes the healer.

    This is the fast path: seconds, versus the SigNoz alert's ~5 minute webhook grouping.
    Never raises: a Port outage must not kill a scrape or a repair mid-flight -- the SigNoz
    alert path is the independent safety net, and the warning below is the audit trail.
    Extra kwargs become entity properties (e.g. collector_id=..., verify_status=...).
    """
    try:
        upsert_entity(
            "scraper",
            identifier=scraper_id,
            title=extra.pop("title", scraper_id),
            properties={"health": health, **extra},
        )
        telemetry.log().info("scraper %s health -> %s", scraper_id, health)
    except Exception as exc:
        telemetry.log().warning(
            "could not set scraper %s health -> %s in Port: %s", scraper_id, health, exc
        )


def patch_run(run_id: str, success: bool, message: str, link: str | None = None) -> None:
    """Close a self-service action run so it doesn't dangle 'in progress' in Port.

    Never raises -- run bookkeeping must not kill the builder that just did the work.
    """
    body: dict[str, Any] = {
        "status": "SUCCESS" if success else "FAILURE",
        "logMessage": message,
    }
    if link:
        body["link"] = [link]
    try:
        httpx.patch(
            f"{API}/actions/runs/{run_id}", json=body, headers=_headers(), timeout=30
        ).raise_for_status()
    except Exception as exc:
        telemetry.log().warning("could not patch action run %s: %s", run_id, exc)


def create_automation(definition: dict[str, Any]) -> None:
    resp = httpx.post(f"{API}/actions", json=definition, headers=_headers(), timeout=30)
    if resp.status_code in (409, 422):
        resp = httpx.put(
            f"{API}/actions/{definition['identifier']}",
            json=definition,
            headers=_headers(),
            timeout=30,
        )
    if resp.status_code >= 300:
        raise PortError(f"automation {definition['identifier']}: {resp.status_code} {resp.text}")
    telemetry.log().info("automation ready: %s", definition["identifier"])


def create_scorecard(blueprint: str, definition: dict[str, Any]) -> None:
    """Idempotent scorecard upsert on a blueprint. An existing one is PUT."""
    ident = definition["identifier"]
    resp = httpx.post(
        f"{API}/blueprints/{blueprint}/scorecards",
        json=definition,
        headers=_headers(),
        timeout=30,
    )
    if resp.status_code in (409, 422):
        resp = httpx.put(
            f"{API}/blueprints/{blueprint}/scorecards/{ident}",
            json=definition,
            headers=_headers(),
            timeout=30,
        )
    if resp.status_code >= 300:
        raise PortError(f"scorecard {blueprint}/{ident}: {resp.status_code} {resp.text}")
    telemetry.log().info("scorecard ready: %s/%s", blueprint, ident)


def create_page(definition: dict[str, Any]) -> None:
    """Idempotent page (dashboard) upsert. An existing page is PUT."""
    ident = definition["identifier"]
    resp = httpx.post(f"{API}/pages", json=definition, headers=_headers(), timeout=30)
    if resp.status_code in (409, 422):
        resp = httpx.put(
            f"{API}/pages/{ident}", json=definition, headers=_headers(), timeout=30
        )
    if resp.status_code >= 300:
        raise PortError(f"page {ident}: {resp.status_code} {resp.text}")
    telemetry.log().info("page ready: %s", ident)
