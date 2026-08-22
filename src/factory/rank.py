"""Rank upcoming titles: guide rows + TMDB popularity -> top-10 shows + top-10 movies.

Every distinct title airing in the next week is enriched from TMDB (/search/multi gives
popularity and vote_average for movies AND shows) and scored. TMDB responses are cached in
SQLite so re-ranking is free and works offline. Without a TMDB_API_KEY the ranker degrades
to guide-only heuristics (airing volume, prime time, NEW badges) with a logged warning.

The click-through: a Philo catalog match wins (philo.com/player/show/<id>, where Record
lives); otherwise fall back to the provider's guide page.
"""
from __future__ import annotations

import datetime as dt
import math
import os
from typing import Any

import httpx

from . import port, state, telemetry

TMDB_API = "https://api.themoviedb.org/3"
TOP_PER_KIND = 10  # top-10 shows + top-10 movies, ranked 1..10 within each kind

# Weights: popularity dominates, quality seasons it, the guide's own signals break ties.
W_POPULARITY = 0.60
W_RATING = 0.25
W_AIRINGS = 0.10
BOOST_NEW = 0.10
BOOST_PRIME_TIME = 0.05

FALLBACK_URLS = {
    "philo": "https://streamingtvguides.com/Philo-TV/Guide",
    "youtubetv": "https://streamingtvguides.com/YouTube-TV/Guide",
    "sling": "https://streamingtvguides.com/Sling-TV/Orange-Blue-Guide",
}


def _tmdb_lookup(title: str) -> dict[str, Any] | None:
    """Popularity/vote/kind for a title, from cache first, else one TMDB search call."""
    cached = state.tmdb_cached(title)
    telemetry.metric("tmdb_cache").add(1, {"hit": cached is not None})
    if cached is not None:
        return cached or None  # empty dict = cached miss
    key = os.getenv("TMDB_API_KEY", "")
    if not key:
        return None
    with telemetry.tracer().start_as_current_span("tmdb.lookup") as span:
        span.set_attribute("tmdb.title", title)
        try:
            resp = httpx.get(
                f"{TMDB_API}/search/multi",
                params={"query": title, "api_key": key, "include_adult": "false"},
                timeout=15,
            )
            resp.raise_for_status()
            results = [
                r for r in resp.json().get("results", []) if r.get("media_type") in ("movie", "tv")
            ]
        except Exception as exc:
            span.set_attribute("tmdb.error", str(exc)[:120])
            telemetry.log().warning("TMDB lookup failed for %r: %s", title, exc)
            return None
        span.set_attribute("tmdb.results", len(results))
    if not results:
        state.tmdb_save(title, {})  # cache the miss too
        return None
    best = results[0]
    slim = {
        "popularity": best.get("popularity", 0.0),
        "vote_average": best.get("vote_average", 0.0),
        "kind": "movie" if best["media_type"] == "movie" else "show",
    }
    state.tmdb_save(title, slim)
    return slim


def _is_prime_time(start_utc: str) -> bool:
    """7-11pm US Eastern, expressed in the guide's UTC timestamps."""
    try:
        hour = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00")).hour
    except ValueError:
        return False
    return hour >= 23 or hour < 3


def compute(days: int = 7) -> list[dict[str, Any]]:
    """The top-20 upcoming titles across providers, best first."""
    with telemetry.tracer().start_as_current_span("rank.compute") as span:
        rows = state.upcoming_guide_rows(days)
        span.set_attribute("guide.rows", len(rows))

        # Aggregate airings by normalized title.
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = state.normalize_title(row["title"])
            if not key:
                continue
            g = groups.setdefault(
                key,
                {
                    "title": row["title"],
                    "kind": row.get("kind"),
                    "airings": 0,
                    "is_new": False,
                    "next_airing_utc": row["start_utc"],
                    "channel": row["channel"],
                    "provider": row["provider"],
                },
            )
            g["airings"] += 1
            g["is_new"] = g["is_new"] or bool(row.get("is_new"))
            if row["start_utc"] < g["next_airing_utc"]:
                g.update(
                    next_airing_utc=row["start_utc"],
                    channel=row["channel"],
                    provider=row["provider"],
                )

        span.set_attribute("rank.titles_considered", len(groups))
        telemetry.metric("titles_considered").set(len(groups))
        if not groups:
            telemetry.log().warning("no upcoming guide rows to rank -- run the scrapers first")
            return []

        if not os.getenv("TMDB_API_KEY") and not any(
            state.tmdb_cached(g["title"]) for g in list(groups.values())[:5]
        ):
            telemetry.log().warning(
                "TMDB_API_KEY not set and no cache -- ranking on guide heuristics only"
            )

        enriched = []
        for g in groups.values():
            tmdb = _tmdb_lookup(g["title"])
            g["popularity"] = (tmdb or {}).get("popularity", 0.0)
            g["vote_average"] = (tmdb or {}).get("vote_average", 0.0)
            g["kind"] = (tmdb or {}).get("kind") or g.get("kind") or "show"
            enriched.append(g)

        max_pop = max((g["popularity"] for g in enriched), default=0.0) or 1.0
        max_airings = max((g["airings"] for g in enriched), default=1)
        for g in enriched:
            score = (
                W_POPULARITY * (g["popularity"] / max_pop)
                + W_RATING * (g["vote_average"] / 10.0)
                + W_AIRINGS * (math.log1p(g["airings"]) / math.log1p(max_airings))
            )
            if g["is_new"]:
                score += BOOST_NEW
            if _is_prime_time(g["next_airing_utc"]):
                score += BOOST_PRIME_TIME
            g["score"] = round(score, 4)

        ordered = sorted(enriched, key=lambda g: (-g["score"], g["title"]))
        movies = [g for g in ordered if g["kind"] == "movie"][:TOP_PER_KIND]
        shows = [g for g in ordered if g["kind"] != "movie"][:TOP_PER_KIND]
        top = shows + movies

        matched = 0
        for section in (shows, movies):
            for rank_pos, g in enumerate(section, start=1):
                record_url = state.catalog_url(g["title"])
                if record_url:
                    matched += 1
                g["record_url"] = record_url or FALLBACK_URLS.get(
                    g["provider"], FALLBACK_URLS["philo"]
                )
                g["rank"] = rank_pos
                g.pop("popularity", None)
                g.pop("vote_average", None)

        match_rate = matched / len(top) if top else 0.0
        span.set_attribute("rank.catalog_match_rate", match_rate)
        span.set_attribute("rank.titles_matched", matched)
        telemetry.metric("catalog_match_rate").set(match_rate)
        telemetry.log().info(
            "ranked %d titles, top-%d computed, %.0f%% with Philo record links",
            len(groups),
            len(top),
            match_rate * 100,
        )
        return top


def write_to_port(top: list[dict[str, Any]]) -> None:
    """Upsert the two top-10 lists as ranked_title entities (identifier = kind + rank)."""
    for g in top:
        try:
            port.upsert_entity(
                "ranked_title",
                identifier=f"{g['kind']}_{g['rank']:02d}",
                title=f"#{g['rank']} {g['title']} ({g['kind']})",
                properties={
                    "rank": g["rank"],
                    "show_title": g["title"],
                    "kind": g["kind"],
                    "score": g["score"],
                    "next_airing_at": g["next_airing_utc"],
                    "channel": g["channel"],
                    "provider": g["provider"],
                    "is_new": bool(g.get("is_new")),
                    "airings": int(g.get("airings", 0)),
                    "record_url": g["record_url"],
                },
            )
        except Exception as exc:
            telemetry.log().warning("could not write ranked_title #%d: %s", g["rank"], exc)
            return
