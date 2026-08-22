"""SQLite store. One container fewer than Postgres, one failure mode fewer.

Holds: the previous run per scraper (drift comparison), the two-consecutive-runs
confirmation counter, the scraped guide rows the ranker reads, the Philo title -> record
URL catalog, and a TMDB response cache so re-ranking is free and offline.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

DB_PATH = os.getenv("STATE_DB", "out/factory.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    scraper_id   TEXT PRIMARY KEY,
    rows_json    TEXT NOT NULL,
    schema_hash  TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_change (
    scraper_id   TEXT PRIMARY KEY,
    signature    TEXT NOT NULL,
    seen_count   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS guide_row (
    provider      TEXT NOT NULL,
    channel       TEXT NOT NULL,
    title         TEXT NOT NULL,
    episode_title TEXT,
    kind          TEXT,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT,
    start_raw     TEXT,
    is_new        INTEGER NOT NULL DEFAULT 0,
    scraped_at    TEXT NOT NULL,
    PRIMARY KEY (provider, channel, title, start_utc)
);
CREATE TABLE IF NOT EXISTS catalog (
    title_norm  TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tmdb_cache (
    title_norm  TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineup (
    provider    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    url         TEXT NOT NULL,
    position    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (provider, url)
);
CREATE TABLE IF NOT EXISTS seen_run (
    kind        TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (kind, run_id)
);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def previous(scraper_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT rows_json, schema_hash, row_count FROM snapshot WHERE scraper_id = ?",
            (scraper_id,),
        ).fetchone()
    if not row:
        return None
    return {"rows": json.loads(row[0]), "schema_hash": row[1], "row_count": row[2]}


def save(scraper_id: str, rows: list[dict[str, Any]], schema_hash: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO snapshot (scraper_id, rows_json, schema_hash, row_count, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(scraper_id) DO UPDATE SET rows_json=excluded.rows_json, "
            "schema_hash=excluded.schema_hash, row_count=excluded.row_count, "
            "updated_at=excluded.updated_at",
            (scraper_id, json.dumps(rows), schema_hash, len(rows)),
        )


def confirm_change(scraper_id: str, signature: str) -> int:
    """Two-consecutive-runs rule.

    A drifted scraper and a genuine site change look identical in one sample. Returns how
    many consecutive runs have now reported the same change; callers require >= 2 before
    treating a lineup/price change as real rather than as scraper breakage.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT signature, seen_count FROM pending_change WHERE scraper_id = ?",
            (scraper_id,),
        ).fetchone()
        count = row[1] + 1 if row and row[0] == signature else 1
        conn.execute(
            "INSERT INTO pending_change (scraper_id, signature, seen_count) VALUES (?, ?, ?) "
            "ON CONFLICT(scraper_id) DO UPDATE SET signature=excluded.signature, "
            "seen_count=excluded.seen_count",
            (scraper_id, signature, count),
        )
    return count


def clear_pending(scraper_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM pending_change WHERE scraper_id = ?", (scraper_id,))


# ---- guide rows (what the ranker reads) --------------------------------------------


def normalize_title(title: str) -> str:
    """Matching key across the guide, the Philo catalog, and the TMDB cache."""
    t = title.lower().strip()
    # Guide movie titles carry release-year suffixes ("Major League (1989)") that the
    # catalog and TMDB never use; drop them or movies can never match.
    t = re.sub(r"\s*\((?:19|20)\d{2}\)$", "", t)
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def save_guide_rows(provider: str, rows: list[dict[str, Any]]) -> None:
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO guide_row (provider, channel, title, episode_title, kind, "
            "start_utc, end_utc, start_raw, is_new, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(provider, channel, title, start_utc) DO UPDATE SET "
            "episode_title=excluded.episode_title, kind=excluded.kind, "
            "end_utc=excluded.end_utc, start_raw=excluded.start_raw, "
            "is_new=excluded.is_new, scraped_at=excluded.scraped_at",
            [
                (
                    provider,
                    row.get("channel") or "",
                    row.get("title") or "",
                    row.get("episode_title"),
                    row.get("kind"),
                    row.get("start_utc") or "",
                    row.get("end_utc"),
                    row.get("start_raw"),
                    1 if row.get("is_new") else 0,
                )
                for row in rows
                if row.get("title") and row.get("start_utc")
            ],
        )


def upcoming_guide_rows(days: int = 7) -> list[dict[str, Any]]:
    """Guide rows airing between now and `days` ahead, across all providers."""
    with _conn() as conn:
        cur = conn.execute(
            "SELECT provider, channel, title, episode_title, kind, start_utc, end_utc, "
            "is_new FROM guide_row WHERE start_utc >= datetime('now') "
            "AND start_utc <= datetime('now', ?) ORDER BY start_utc",
            (f"+{days} days",),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---- Philo record-link catalog ------------------------------------------------------


def save_catalog(entries: list[dict[str, Any]]) -> int:
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO catalog (title_norm, title, url, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(title_norm) DO UPDATE SET title=excluded.title, "
            "url=excluded.url, updated_at=excluded.updated_at",
            [
                (normalize_title(e["title"]), e["title"], e["url"])
                for e in entries
                if e.get("title") and e.get("url")
            ],
        )
    return len(entries)


def catalog_url(title: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT url FROM catalog WHERE title_norm = ?", (normalize_title(title),)
        ).fetchone()
    return row[0] if row else None


def catalog_size() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]


# ---- provider channel lineups -------------------------------------------------------


def save_lineup(provider: str, channels: list[dict[str, Any]]) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM lineup WHERE provider = ?", (provider,))
        conn.executemany(
            "INSERT INTO lineup (provider, channel, url, position, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            [(provider, c["channel"], c["url"], i) for i, c in enumerate(channels)],
        )


def lineup(provider: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT channel, url FROM lineup WHERE provider = ? ORDER BY position",
            (provider,),
        )
        return [{"channel": row[0], "url": row[1]} for row in cur.fetchall()]


# ---- control-plane run dedup (portwatch) --------------------------------------------


def mark_run_seen(kind: str, run_id: str) -> bool:
    """Record a control-plane run id. Returns True if it is NEW (first time seen).

    portwatch calls this per run so a restart or an overlapping poll never re-emits a run
    already bridged into telemetry. `kind` namespaces the id space ("action" vs "workflow").
    """
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_run (kind, run_id, seen_at) "
            "VALUES (?, ?, datetime('now'))",
            (kind, run_id),
        )
        return cur.rowcount > 0


# ---- TMDB cache ---------------------------------------------------------------------


def tmdb_cached(title: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT body_json FROM tmdb_cache WHERE title_norm = ?",
            (normalize_title(title),),
        ).fetchone()
    return json.loads(row[0]) if row else None


def tmdb_save(title: str, body: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tmdb_cache (title_norm, body_json, fetched_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(title_norm) DO UPDATE SET body_json=excluded.body_json, "
            "fetched_at=excluded.fetched_at",
            (normalize_title(title), json.dumps(body)),
        )
