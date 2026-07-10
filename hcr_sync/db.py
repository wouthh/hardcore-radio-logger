"""SQLite schema and repository helpers."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .identity import canonical_key, compact_text, display_from_parts, normalize_for_match, parse_artist_title

TRACK_STATUSES = {"wanted", "excluded", "review", "missing", "error"}
YOUTUBE_STATUSES = {"downloaded", "missing", "deleted", "error", "review"}
SPOTIFY_STATUSES = {"added", "missing", "removed", "error", "review"}
EXCLUSION_SOURCES = {"local_deleted", "spotify_removed", "manual", "low_quality", "wrong_match"}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_artist TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    display_artist TEXT NOT NULL,
    display_title TEXT NOT NULL,
    canonical_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('wanted', 'excluded', 'review', 'missing', 'error')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radio_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE RESTRICT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_artist TEXT NOT NULL,
    raw_title TEXT NOT NULL,
    raw_line TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    observation_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS youtube_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE RESTRICT,
    youtube_video_id TEXT,
    youtube_url TEXT,
    file_path TEXT,
    file_exists INTEGER NOT NULL DEFAULT 0,
    match_confidence REAL,
    status TEXT NOT NULL CHECK (status IN ('downloaded', 'missing', 'deleted', 'error', 'review')),
    downloaded_at TEXT,
    last_seen_at TEXT,
    suspected_missing_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(track_id, file_path),
    UNIQUE(track_id, youtube_video_id)
);

CREATE TABLE IF NOT EXISTS spotify_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE RESTRICT,
    spotify_track_uri TEXT,
    spotify_track_id TEXT,
    spotify_artist TEXT NOT NULL DEFAULT '',
    spotify_title TEXT NOT NULL DEFAULT '',
    playlist_id TEXT NOT NULL DEFAULT '',
    in_playlist INTEGER NOT NULL DEFAULT 0,
    match_confidence REAL,
    status TEXT NOT NULL CHECK (status IN ('added', 'missing', 'removed', 'error', 'review')),
    added_at TEXT,
    last_seen_at TEXT,
    search_last_at TEXT,
    search_attempts INTEGER NOT NULL DEFAULT 0,
    search_next_at TEXT,
    suspected_missing_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(track_id, playlist_id),
    UNIQUE(playlist_id, spotify_track_id)
);

CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('local_deleted', 'spotify_removed', 'manual', 'low_quality', 'wrong_match')),
    created_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE(track_id, source, reason)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER REFERENCES tracks(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    event_source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dedupe_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_youtube_track ON youtube_assets(track_id);
CREATE INDEX IF NOT EXISTS idx_youtube_file_exists ON youtube_assets(file_exists);
CREATE INDEX IF NOT EXISTS idx_spotify_track ON spotify_assets(track_id);
CREATE INDEX IF NOT EXISTS idx_spotify_playlist ON spotify_assets(playlist_id, in_playlist);
CREATE INDEX IF NOT EXISTS idx_spotify_search_schedule ON spotify_assets(playlist_id, in_playlist, search_next_at, search_last_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
"""


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def migrate_db(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "spotify_assets"):
        return

    columns = _table_columns(con, "spotify_assets")
    added_search_columns = False
    migrations = [
        ("search_last_at", "ALTER TABLE spotify_assets ADD COLUMN search_last_at TEXT"),
        ("search_attempts", "ALTER TABLE spotify_assets ADD COLUMN search_attempts INTEGER NOT NULL DEFAULT 0"),
        ("search_next_at", "ALTER TABLE spotify_assets ADD COLUMN search_next_at TEXT"),
    ]
    for column, sql in migrations:
        if column not in columns:
            con.execute(sql)
            columns.add(column)
            added_search_columns = True

    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_spotify_search_schedule "
        "ON spotify_assets(playlist_id, in_playlist, search_next_at, search_last_at)"
    )

    if added_search_columns:
        con.execute(
            """
            UPDATE spotify_assets
               SET search_last_at = COALESCE(updated_at, created_at, last_seen_at),
                   search_attempts = CASE WHEN search_attempts < 2 THEN 2 ELSE search_attempts END,
                   search_next_at = strftime('%Y-%m-%dT%H:%M:%SZ', COALESCE(updated_at, created_at, last_seen_at), '+14 days')
             WHERE status = 'review'
               AND in_playlist = 0
               AND search_last_at IS NULL
               AND match_confidence IS NOT NULL
            """
        )
    if _table_exists(con, "schema_migrations"):
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (2, now_utc()),
        )


def connect(config_or_path: Config | Path | str) -> sqlite3.Connection:
    path = config_or_path.db_path if isinstance(config_or_path, Config) else Path(config_or_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    migrate_db(con)
    con.commit()
    return con


def init_db(config: Config) -> None:
    with connect(config) as con:
        con.executescript(SCHEMA)
        migrate_db(con)
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, now_utc()),
        )
        con.commit()


@contextlib.contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        con.execute("BEGIN")
        yield con
    except Exception:
        con.rollback()
        raise
    else:
        con.commit()


def track_from_raw(raw: str) -> tuple[str, str]:
    return parse_artist_title(raw)


def ensure_track(
    con: sqlite3.Connection,
    *,
    artist: str,
    title: str,
    status: str = "wanted",
    reactivate: bool = False,
) -> sqlite3.Row:
    if status not in TRACK_STATUSES:
        raise ValueError(f"invalid track status: {status}")
    artist = compact_text(artist)
    title = compact_text(title)
    if not artist and not title:
        raise ValueError("artist or title is required")
    key = canonical_key(artist, title)
    now = now_utc()
    existing = con.execute("SELECT * FROM tracks WHERE canonical_key = ?", (key,)).fetchone()
    if existing:
        next_status = existing["status"]
        if reactivate:
            next_status = status
        elif existing["status"] != "excluded" and status == "wanted":
            next_status = "wanted"
        con.execute(
            """
            UPDATE tracks
               SET display_artist = CASE WHEN display_artist = '' THEN ? ELSE display_artist END,
                   display_title = CASE WHEN display_title = '' THEN ? ELSE display_title END,
                   status = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (artist, title, next_status, now, existing["id"]),
        )
        return con.execute("SELECT * FROM tracks WHERE id = ?", (existing["id"],)).fetchone()
    con.execute(
        """
        INSERT INTO tracks (
            normalized_artist, normalized_title, display_artist, display_title,
            canonical_key, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalize_for_match(artist),
            normalize_for_match(title),
            artist,
            title,
            key,
            status,
            now,
            now,
        ),
    )
    track = con.execute("SELECT * FROM tracks WHERE canonical_key = ?", (key,)).fetchone()
    add_event(con, track["id"], "track_created", "db", {"canonical_key": key}, dedupe_key=f"track_created:{key}")
    return track


def ensure_track_from_raw(con: sqlite3.Connection, raw: str, *, status: str = "wanted") -> sqlite3.Row:
    artist, title = track_from_raw(raw)
    return ensure_track(con, artist=artist, title=title, status=status)


def display_track(track: sqlite3.Row) -> str:
    return display_from_parts(track["display_artist"], track["display_title"])


def add_observation(
    con: sqlite3.Connection,
    *,
    track_id: int,
    observed_at: str,
    source: str,
    raw_artist: str,
    raw_title: str,
    raw_line: str,
) -> bool:
    observation_key = f"{source}:{observed_at}:{raw_line}"
    try:
        con.execute(
            """
            INSERT INTO radio_observations (
                track_id, observed_at, source, raw_artist, raw_title,
                raw_line, imported_at, observation_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (track_id, observed_at, source, raw_artist, raw_title, raw_line, now_utc(), observation_key),
        )
    except sqlite3.IntegrityError:
        return False
    add_event(
        con,
        track_id,
        "observed",
        source,
        {"observed_at": observed_at, "raw_line": raw_line},
        dedupe_key=f"observed:{observation_key}",
    )
    return True


def add_event(
    con: sqlite3.Connection,
    track_id: int | None,
    event_type: str,
    event_source: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
) -> bool:
    try:
        con.execute(
            """
            INSERT INTO events(track_id, event_type, event_source, payload_json, created_at, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (track_id, event_type, event_source, json.dumps(payload, sort_keys=True), now_utc(), dedupe_key),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def get_state(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_utc()),
    )


def mark_excluded(
    con: sqlite3.Connection,
    *,
    track_id: int,
    source: str,
    reason: str,
    notes: str = "",
) -> None:
    if source not in EXCLUSION_SOURCES:
        raise ValueError(f"invalid exclusion source: {source}")
    now = now_utc()
    con.execute("UPDATE tracks SET status = 'excluded', updated_at = ? WHERE id = ?", (now, track_id))
    con.execute(
        """
        INSERT OR IGNORE INTO exclusions(track_id, reason, source, created_at, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (track_id, reason, source, now, notes),
    )
    add_event(
        con,
        track_id,
        "globally_excluded",
        source,
        {"reason": reason, "notes": notes},
        dedupe_key=f"globally_excluded:{track_id}:{source}:{reason}",
    )


def unexclude_track(con: sqlite3.Connection, *, track_id: int, reason: str = "manual") -> None:
    now = now_utc()
    con.execute("UPDATE tracks SET status = 'wanted', updated_at = ? WHERE id = ?", (now, track_id))
    add_event(con, track_id, "manually_unexcluded", "manual", {"reason": reason})


def upsert_youtube_asset(
    con: sqlite3.Connection,
    *,
    track_id: int,
    youtube_video_id: str = "",
    youtube_url: str = "",
    file_path: str = "",
    file_exists: bool,
    match_confidence: float | None,
    status: str,
    downloaded_at: str | None = None,
) -> sqlite3.Row:
    if status not in YOUTUBE_STATUSES:
        raise ValueError(f"invalid youtube status: {status}")
    now = now_utc()
    existing = None
    if file_path:
        existing = con.execute("SELECT * FROM youtube_assets WHERE track_id = ? AND file_path = ?", (track_id, file_path)).fetchone()
    if existing is None and youtube_video_id:
        existing = con.execute(
            "SELECT * FROM youtube_assets WHERE track_id = ? AND youtube_video_id = ?",
            (track_id, youtube_video_id),
        ).fetchone()
    if existing:
        con.execute(
            """
            UPDATE youtube_assets
               SET youtube_video_id = COALESCE(NULLIF(?, ''), youtube_video_id),
                   youtube_url = COALESCE(NULLIF(?, ''), youtube_url),
                   file_path = COALESCE(NULLIF(?, ''), file_path),
                   file_exists = ?,
                   match_confidence = ?,
                   status = ?,
                   downloaded_at = COALESCE(?, downloaded_at),
                   last_seen_at = ?,
                   suspected_missing_at = CASE WHEN ? THEN NULL ELSE suspected_missing_at END,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                youtube_video_id,
                youtube_url,
                file_path,
                1 if file_exists else 0,
                match_confidence,
                status,
                downloaded_at,
                now,
                1 if file_exists else 0,
                now,
                existing["id"],
            ),
        )
        return con.execute("SELECT * FROM youtube_assets WHERE id = ?", (existing["id"],)).fetchone()
    con.execute(
        """
        INSERT INTO youtube_assets (
            track_id, youtube_video_id, youtube_url, file_path, file_exists,
            match_confidence, status, downloaded_at, last_seen_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            track_id,
            youtube_video_id or None,
            youtube_url or None,
            file_path or None,
            1 if file_exists else 0,
            match_confidence,
            status,
            downloaded_at,
            now,
            now,
            now,
        ),
    )
    return con.execute("SELECT * FROM youtube_assets WHERE id = last_insert_rowid()").fetchone()


def upsert_spotify_asset(
    con: sqlite3.Connection,
    *,
    track_id: int,
    playlist_id: str,
    spotify_track_uri: str = "",
    spotify_track_id: str = "",
    spotify_artist: str = "",
    spotify_title: str = "",
    in_playlist: bool,
    match_confidence: float | None,
    status: str,
    added_at: str | None = None,
    search_last_at: str | None = None,
    search_attempts: int | None = None,
    search_next_at: str | None = None,
    update_search: bool = False,
) -> sqlite3.Row:
    if status not in SPOTIFY_STATUSES:
        raise ValueError(f"invalid spotify status: {status}")
    now = now_utc()
    existing = con.execute(
        "SELECT * FROM spotify_assets WHERE track_id = ? AND playlist_id = ?",
        (track_id, playlist_id),
    ).fetchone()
    if existing is None and spotify_track_id:
        existing = con.execute(
            "SELECT * FROM spotify_assets WHERE playlist_id = ? AND spotify_track_id = ?",
            (playlist_id, spotify_track_id),
        ).fetchone()
    if existing:
        con.execute(
            """
            UPDATE spotify_assets
               SET spotify_track_uri = COALESCE(NULLIF(?, ''), spotify_track_uri),
                   spotify_track_id = COALESCE(NULLIF(?, ''), spotify_track_id),
                   spotify_artist = COALESCE(NULLIF(?, ''), spotify_artist),
                   spotify_title = COALESCE(NULLIF(?, ''), spotify_title),
                   in_playlist = ?,
                   match_confidence = ?,
                   status = ?,
                   added_at = COALESCE(?, added_at),
                   last_seen_at = ?,
                   search_last_at = CASE WHEN ? THEN ? ELSE search_last_at END,
                   search_attempts = CASE WHEN ? THEN ? ELSE search_attempts END,
                   search_next_at = CASE WHEN ? THEN ? ELSE search_next_at END,
                   suspected_missing_at = CASE WHEN ? THEN NULL ELSE suspected_missing_at END,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                spotify_track_uri,
                spotify_track_id,
                spotify_artist,
                spotify_title,
                1 if in_playlist else 0,
                match_confidence,
                status,
                added_at,
                now,
                1 if update_search else 0,
                search_last_at,
                1 if update_search else 0,
                search_attempts if search_attempts is not None else 0,
                1 if update_search else 0,
                search_next_at,
                1 if in_playlist else 0,
                now,
                existing["id"],
            ),
        )
        return con.execute("SELECT * FROM spotify_assets WHERE id = ?", (existing["id"],)).fetchone()
    con.execute(
        """
        INSERT INTO spotify_assets (
            track_id, spotify_track_uri, spotify_track_id, spotify_artist, spotify_title,
            playlist_id, in_playlist, match_confidence, status, added_at,
            last_seen_at, search_last_at, search_attempts, search_next_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            track_id,
            spotify_track_uri or None,
            spotify_track_id or None,
            spotify_artist,
            spotify_title,
            playlist_id,
            1 if in_playlist else 0,
            match_confidence,
            status,
            added_at,
            now,
            search_last_at if update_search else None,
            (search_attempts if search_attempts is not None else 0) if update_search else 0,
            search_next_at if update_search else None,
            now,
            now,
        ),
    )
    return con.execute("SELECT * FROM spotify_assets WHERE id = last_insert_rowid()").fetchone()


def wanted_tracks(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute("SELECT * FROM tracks WHERE status = 'wanted' ORDER BY id"))


def track_by_id(con: sqlite3.Connection, track_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
