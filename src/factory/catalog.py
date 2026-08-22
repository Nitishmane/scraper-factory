"""The Philo record-link catalog: title -> philo.com/player/show/<id>.

philo.com/go/allshows is public and links every show tile to its player page, where a
logged-in subscriber can hit Save/Record. The player-page IDs are internal (base64 blobs),
not derivable from titles, so the map has to be scraped. This is the second collector in
the factory; it feeds the ranker's click-through URLs.

Gated by a row-count band rather than a fixture: the catalog is a moving target by design
(shows come and go), so a frozen expected.json would rot within days.
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import brightdata, state, telemetry

CATALOG_URL = "https://www.philo.com/go/allshows"
# philo.com/go/allmovies is the movies sibling of allshows, linked from the same nav.
# Movies are modeled as "Show:" internally -- same base64 ID prefix, same
# philo.com/player/show/<id> record URL -- so the embedded-JSON parse is identical. This is
# an embedded-JSON fetch only (no collector): the catalog collector stays on allshows.
MOVIES_URL = "https://www.philo.com/go/allmovies"
COLLECTOR_ENV = "SCRAPER_STUDIO_COLLECTOR_ID_PHILO_CATALOG"

# The movies page legitimately renders far fewer tiles than allshows; treat a parse below
# this as a broken movies fetch (skip it, keep the shows) rather than a shrunk catalog.
MIN_MOVIE_ROWS = 100

# philo advertises 70k+ titles but the browse page renders a large subset; below this the
# scrape is considered broken rather than the catalog having shrunk.
MIN_ROWS = 200

PROMPT = (
    "This page is a catalog of TV shows and movies, one tile per title. For every tile "
    "capture: the display title of the show or movie, as title; and the absolute URL its "
    "link points to, as url (the links look like philo.com/player/show/<id>). Include "
    "every tile on the page; do not truncate the list."
)


def collector_id() -> str:
    value = os.getenv(COLLECTOR_ENV)
    if not value:
        raise RuntimeError(
            f"{COLLECTOR_ENV} not set -- create the catalog scraper and pin the ID in CLAUDE.md"
        )
    return value


def refresh() -> dict[str, Any]:
    """Scrape the catalog and persist the title -> record-URL map. Returns a summary."""
    with telemetry.tracer().start_as_current_span("catalog.refresh") as span:
        span.set_attribute("target.url", CATALOG_URL)
        rows, mode, cache_hit = brightdata.run_with_relay(collector_id(), CATALOG_URL)
        span.set_attribute("fetch.mode", mode)
        span.set_attribute("relay.cache_hit", cache_hit)
        span.set_attribute("rows.returned", len(rows))

        entries = [r for r in rows if r.get("title") and str(r.get("url", "")).startswith("http")]
        if len(entries) < MIN_ROWS:
            # philo.com is a JS-rendered SPA: a static relay copy renders empty tiles.
            # The fetched HTML embeds the catalog as JSON, though -- parse it directly.
            telemetry.log().warning(
                "catalog collector yielded %d usable rows (< %d) -- parsing the show "
                "IDs embedded in the fetched page instead",
                len(entries),
                MIN_ROWS,
            )
            entries = _parse_embedded(brightdata.fetch_html(CATALOG_URL))
            span.set_attribute("fetch.mode", "embedded_json")

        if len(entries) < MIN_ROWS:
            telemetry.log().error(
                "catalog yielded %d usable rows (< %d) -- keeping the previous map",
                len(entries),
                MIN_ROWS,
            )
            return {"rows": len(rows), "saved": 0, "ok": False}

        state.save_catalog(entries)
        # Movies are a separate page and an additive merge: a movies-page failure must not
        # sink the shows refresh, so it logs a warning and returns 0 rather than raising.
        movies = _refresh_movies()
        span.set_attribute("movies.saved", movies)
        telemetry.log().info(
            "catalog refreshed: %d show titles + %d movie titles mapped",
            len(entries),
            movies,
        )
        return {
            "rows": len(rows),
            "saved": len(entries),
            "movies": movies,
            "ok": True,
        }


def _refresh_movies() -> int:
    """Merge philo.com/go/allmovies into the same catalog map. Returns rows saved.

    Best-effort and additive: any failure (fetch error, empty parse, too few rows) logs a
    warning and returns 0 so the shows refresh still counts as a success.
    """
    with telemetry.tracer().start_as_current_span("catalog.refresh_movies") as span:
        span.set_attribute("target.url", MOVIES_URL)
        try:
            entries = _parse_embedded(brightdata.fetch_html(MOVIES_URL))
        except Exception as exc:  # network/CLI failure -- keep the shows we already saved
            span.set_attribute("movies.error", str(exc)[:120])
            telemetry.log().warning("movies catalog fetch failed, skipping: %s", exc)
            return 0
        span.set_attribute("rows.returned", len(entries))
        if len(entries) < MIN_MOVIE_ROWS:
            telemetry.log().warning(
                "movies page yielded %d usable rows (< %d) -- skipping the movies merge",
                len(entries),
                MIN_MOVIE_ROWS,
            )
            return 0
        state.save_catalog(entries)
        telemetry.log().info("movies catalog merged: %d titles mapped", len(entries))
        return len(entries)


def _parse_embedded(html: str) -> list[dict[str, Any]]:
    """Show IDs + titles from the JSON state embedded in the allshows page.

    IDs are base64 blobs beginning 'U2hvdzo' ('Show:'); the player URL is
    philo.com/player/show/<id>, where a logged-in subscriber can hit Save/Record.
    """
    pairs: dict[str, str] = {}
    for pat in (
        r'"id":"(U2hvdzo[A-Za-z0-9+/=]+)"[^{}]*?"title":"((?:[^"\\]|\\.)+?)"',
        r'"title":"((?:[^"\\]|\\.)+?)"[^{}]*?"id":"(U2hvdzo[A-Za-z0-9+/=]+)"',
    ):
        for m in re.finditer(pat, html):
            a, b = m.group(1), m.group(2)
            sid, title = (a, b) if a.startswith("U2hvdzo") else (b, a)
            pairs.setdefault(sid, title.encode().decode("unicode_escape"))
    return [
        {"title": title, "url": f"https://www.philo.com/player/show/{sid}"}
        for sid, title in pairs.items()
    ]
