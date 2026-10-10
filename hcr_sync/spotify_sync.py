"""Spotify backfill and sync using Spotipy."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, nullcontext, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .config import Config
from .db import (
    add_event,
    connect,
    ensure_track,
    get_state,
    now_utc,
    set_state,
    transaction,
    upsert_spotify_asset,
    upsert_spotify_playlist_recording,
)
from .identity import (
    canonical_key,
    compact_text,
    token_set,
    likely_same_recording,
    match_confidence,
    normalize_for_match,
    radio_metadata_placeholder,
)
from .spotify_matching import comparison_artists, comparison_title, compatible_titles

NON_TRACK_RE = re.compile(
    r"\b("
    r"full\s+mix|full\s+set|dj\s+set|live\s+set|liveset|mixtape|megamix|yearmix|podcast|radio\s+show|"
    r"compilation|full\s+album|continuous\s+mix|mix\s+session|festival\s+set|various\s+artists|"
    r"aftermovie|trailer|teaser|preview|interview|documentary|recap|artist\s+series|episode"
    r")\b",
    re.I,
)
BRACKETED_TITLE_EXTRA_RE = re.compile(r"\s*[\(\[].*?[\)\]]")
GENERIC_BRACKETED_VERSION_RE = re.compile(
    r"\s*[\(\[]\s*"
    r"(?:original|extended|radio|edit|album|single|full|club|official|hq|hd)"
    r"(?:\s+(?:mix|version|edit|cut))?"
    r"\s*[\)\]]",
    re.I,
)
GENERIC_VERSION_SUFFIX_RE = re.compile(
    r"\s+-\s+(?:original|extended|radio|radio edit|edit|album|single|full)(?:\s+(?:mix|version|edit))?$",
    re.I,
)
MAIN_ARTIST_SPLIT_RE = re.compile(r"\s+(?:&|\+|x|and|vs\.?|feat\.?|ft\.?|featuring)\s+|[,/|]", re.I)
SPOTIFY_FIRST_RETRY_DAYS = 7
SPOTIFY_STEADY_RETRY_DAYS = 14


from .spotify_adapter import (
    SpotifyTrack, PlaylistSnapshot, SpotipyClient, BudgetDeferred,
    _spotify_track_from_playlist_item,
)


class SpotifyClientProtocol(Protocol):
    def auth_check(self) -> str: ...
    def playlist_snapshot(self, playlist_id: str) -> PlaylistSnapshot: ...
    def search_track(self, artist: str, title: str) -> list[SpotifyTrack]: ...
    def add_tracks(self, playlist_id: str, uris: list[str]) -> dict | None: ...
    def remove_tracks(self, playlist_id: str, uris: list[str]) -> dict | None: ...


@dataclass
class SpotifySummary:
    seen: int = 0
    linked: int = 0
    added: int = 0
    tentative_added: int = 0
    review: int = 0
    skipped: int = 0
    ambiguous: int = 0
    rate_limited: bool = False
    first_time: int = 0
    retries: int = 0
    requests: int = 0
    acknowledged: int = 0
    pending: int = 0
    budget_deferred: int = 0
    required_budget: int = 0
    _snapshot: PlaylistSnapshot | None = None
    _client: SpotifyClientProtocol | None = None


def spotify_auth(config: Config, client: SpotifyClientProtocol | None = None) -> str:
    client = client or SpotipyClient(config)
    return client.auth_check()


def spotify_enabled(config: Config) -> bool:
    return config.bool("HCR_SPOTIFY_ENABLED")


def looks_like_non_track(artist: str, title: str) -> bool:
    return radio_metadata_placeholder(artist, title) or bool(NON_TRACK_RE.search(f"{artist} {title}"))


def _core_spotify_title(title: str) -> str:
    title = BRACKETED_TITLE_EXTRA_RE.sub("", title or "")
    return GENERIC_VERSION_SUFFIX_RE.sub("", title).strip()


def _searchable_spotify_title(title: str) -> str:
    title = GENERIC_BRACKETED_VERSION_RE.sub("", title or "")
    return GENERIC_VERSION_SUFFIX_RE.sub("", title).strip()


def _main_spotify_artist(artist: str) -> str:
    parts = [compact_text(part) for part in MAIN_ARTIST_SPLIT_RE.split(artist or "") if compact_text(part)]
    return parts[0] if parts else compact_text(artist)


def _spotify_search_queries(artist: str, title: str) -> list[str]:
    artist = compact_text(artist)
    title = compact_text(title)
    main_artist = _main_spotify_artist(artist)
    clean_title = _searchable_spotify_title(title)

    parts: list[tuple[str, str]] = [(artist, title)]
    if clean_title and clean_title != title:
        parts.append((artist, clean_title))
    if main_artist and main_artist != artist:
        parts.append((main_artist, clean_title or title))
    if clean_title and (main_artist != artist or clean_title != title):
        parts.append((main_artist or artist, clean_title))

    queries: list[str] = []
    seen: set[str] = set()
    for query_artist, query_title in parts:
        query_artist = compact_text(query_artist)
        query_title = compact_text(query_title)
        if not query_artist and not query_title:
            continue
        query = f"artist:{query_artist} track:{query_title}" if query_artist else query_title
        if query not in seen:
            seen.add(query)
            queries.append(query)

    free_text = compact_text(f"{main_artist or artist} {clean_title or title}")
    if free_text and free_text not in seen:
        queries.append(free_text)
    return queries


def _spotify_match_score(track, candidate: SpotifyTrack) -> float:
    if looks_like_non_track(candidate.artist, candidate.title):
        return 0.0
    source_title = comparison_title(track["display_title"])
    candidate_title = comparison_title(candidate.title)
    if not compatible_titles(source_title, candidate_title):
        return 0.0
    artists = comparison_artists(track["display_artist"], candidate.artist, candidate.artist_names)
    if artists is None:
        return 0.0
    source_tokens = token_set(source_title.base)
    candidate_tokens = token_set(candidate_title.base)
    if not source_tokens or not candidate_tokens:
        return 0.0
    overlap = len(source_tokens & candidate_tokens) / max(1, len(source_tokens))
    reverse_overlap = len(source_tokens & candidate_tokens) / max(1, len(candidate_tokens))
    if overlap < 0.75 or reverse_overlap < 0.75:
        return 0.0
    return match_confidence(
        artist=artists[0],
        title=source_title.text,
        candidate_artist=artists[1],
        candidate_title=candidate_title.text,
    )


def _is_rate_limited(exc: Exception) -> bool:
    return getattr(exc, "http_status", None) == 429


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rate_limit_details(config: Config, exc: Exception) -> dict[str, object]:
    headers = getattr(exc, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after") or ""
    retry_after_source = "header" if retry_after else ""
    if not retry_after:
        match = re.search(r"Retry(?:\s+will\s+occur)?\s+after:\s*(\d+)", str(exc), re.I)
        if match:
            retry_after = match.group(1)
            retry_after_source = "error_message"
    try:
        seconds = max(1, int(float(retry_after)))
        fallback_used = False
    except (TypeError, ValueError):
        seconds = config.int("HCR_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS")
        fallback_used = True
        retry_after_source = retry_after_source or "fallback"
    cooldown_until = _format_utc(datetime.now(timezone.utc) + timedelta(seconds=seconds))
    return {
        "http_status": getattr(exc, "http_status", None),
        "retry_after": str(retry_after or ""),
        "retry_after_source": retry_after_source,
        "retry_after_seconds": seconds,
        "fallback_used": fallback_used,
        "cooldown_until": cooldown_until,
        "error_type": type(exc).__name__,
        "reason": getattr(exc, "reason", None),
    }


def _spotify_cooldown_active(con) -> bool:
    until = _parse_utc(get_state(con, "spotify_rate_limited_until", ""))
    return bool(until and until > datetime.now(timezone.utc))


def _configured_spotify_cooldown_active(config: Config) -> bool:
    if not config.db_path.exists():
        return False
    with closing(sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'").fetchone():
            return False
        return _spotify_cooldown_active(con)


SPOTIFY_RATE_LIMIT_STATE_KEYS = (
    "spotify_rate_limited_until",
    "spotify_rate_limit_last_response",
    "spotify_rate_limit_source",
)


def _remember_spotify_rate_limit(con, config: Config, exc: Exception, *, event_source: str = "spotify_sync") -> None:
    payload = _rate_limit_details(config, exc)
    payload["event_source"] = event_source
    with nullcontext() if con.in_transaction else transaction(con):
        set_state(con, "spotify_rate_limited_until", str(payload["cooldown_until"]))
        set_state(con, "spotify_rate_limit_last_response", json.dumps(payload, sort_keys=True))
        set_state(con, "spotify_rate_limit_source", event_source)
        add_event(con, None, "spotify_rate_limited", event_source, payload)


def _spotify_rate_limit_source(con) -> str:
    source = get_state(con, "spotify_rate_limit_source", "")
    if source:
        return source
    last_response = get_state(con, "spotify_rate_limit_last_response", "")
    if last_response:
        try:
            return str(json.loads(last_response).get("event_source") or "")
        except json.JSONDecodeError:
            return ""
    return ""


def _clear_spotify_rate_limit(con, *, event_source: str) -> None:
    if _spotify_cooldown_active(con):
        return
    previous_until = get_state(con, "spotify_rate_limited_until", "")
    previous_response = get_state(con, "spotify_rate_limit_last_response", "")
    if not previous_until and not previous_response:
        return
    previous_source = _spotify_rate_limit_source(con)
    if previous_source == "spotify_sync" and event_source != "spotify_sync":
        return
    con.execute(
        f"DELETE FROM sync_state WHERE key IN ({','.join('?' for _ in SPOTIFY_RATE_LIMIT_STATE_KEYS)})",
        SPOTIFY_RATE_LIMIT_STATE_KEYS,
    )
    add_event(
        con,
        None,
        "spotify_rate_limit_cooldown_cleared",
        event_source,
        {"previous_cooldown_until": previous_until, "previous_event_source": previous_source},
        dedupe_key=f"spotify_rate_limit_cooldown_cleared:{event_source}:{previous_until}:{previous_source}",
    )


def _log_spotify_cooldown_skip(con) -> None:
    until = get_state(con, "spotify_rate_limited_until", "")
    add_event(
        con,
        None,
        "spotify_rate_limit_cooldown_active",
        "spotify_sync",
        {"cooldown_until": until},
        dedupe_key=f"spotify_rate_limit_cooldown_active:{until}",
    )


def _spotify_sync_candidates(con, *, playlist_id: str):
    return list(
        con.execute(
            """
            SELECT
                t.*,
                s.search_last_at AS spotify_search_last_at,
                s.search_next_at AS spotify_search_next_at
              FROM tracks t
              LEFT JOIN spotify_assets s
                ON s.track_id = t.id
               AND s.playlist_id = ?
             WHERE t.status = 'wanted'
             ORDER BY
                CASE WHEN s.search_last_at IS NULL THEN 0 ELSE 1 END,
                COALESCE(s.search_last_at, t.created_at),
                t.id
            """,
            (playlist_id,),
        )
    )


def _spotify_search_deferred(asset, now: datetime) -> bool:
    next_at = _parse_utc(asset["search_next_at"] or "")
    return bool(next_at and next_at > now)


def _next_spotify_retry_at(searched_at: datetime, attempts: int) -> str:
    days = SPOTIFY_FIRST_RETRY_DAYS if attempts <= 1 else SPOTIFY_STEADY_RETRY_DAYS
    return _format_utc(searched_at + timedelta(days=days))


def _spotify_search_attempts(asset) -> int:
    if not asset:
        return 0
    try:
        return max(0, int(asset["search_attempts"] or 0))
    except (TypeError, ValueError):
        return 0


def _remember_failed_spotify_attempt(con, config: Config, track, asset, exc: Exception, *, operation: str, rotation: tuple[str, int] | None = None) -> None:
    rate_limited = _is_rate_limited(exc)
    if rate_limited:
        _remember_spotify_rate_limit(con, config, exc)
    attempted_at = datetime.now(timezone.utc)
    attempts = _spotify_search_attempts(asset) + (0 if rate_limited else 1)
    next_at = get_state(con, "spotify_rate_limited_until") if rate_limited else _next_spotify_retry_at(attempted_at, attempts)
    with transaction(con):
        upsert_spotify_asset(
            con,
            track_id=track["id"],
            playlist_id=config.get("HCR_SPOTIFY_PLAYLIST_ID"),
            in_playlist=bool(asset["in_playlist"]) if asset else False,
            match_confidence=asset["match_confidence"] if asset else None,
            status=asset["status"] if asset else "error",
            search_last_at=_format_utc(attempted_at),
            search_attempts=attempts,
            search_next_at=next_at,
            update_search=True,
        )
        if rotation is not None:
            set_state(con, rotation[0], str(rotation[1]))
            con.execute("DELETE FROM spotify_pending_work WHERE playlist_id=? AND track_id=? AND kind='search'", (config.get("HCR_SPOTIFY_PLAYLIST_ID"), track['id']))
        add_event(con, track["id"], "spotify_request_failed", "spotify_sync", {
            "playlist_id": config.get("HCR_SPOTIFY_PLAYLIST_ID"),
            "operation": operation,
            "http_status": getattr(exc, "http_status", None),
            "error_type": type(exc).__name__,
            "search_next_at": next_at,
        })


def _is_removed_tentative_asset(asset, match_threshold: float) -> bool:
    score = asset["match_confidence"]
    return score is not None and float(score) < match_threshold


def _spotify_asset_has_primary_history(con, asset) -> bool:
    """Return whether an inactive asset is an established playlist primary."""
    if asset is None:
        return False
    if asset["in_playlist"]:
        return True
    # A prior add or removal is evidence that this was once a playlist
    # recording. An inactive review candidate with no add timestamp is only a
    # search result and must yield primary ownership to a later confident add.
    if asset["added_at"] or asset["status"] in {"added", "missing", "removed"}:
        return True
    spotify_track_id = asset["spotify_track_id"] or ""
    if not spotify_track_id:
        return False
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spotify_playlist_recordings'"
    ).fetchone():
        return False
    recording = con.execute(
        """
        SELECT in_playlist, status
          FROM spotify_playlist_recordings
         WHERE playlist_id = ? AND spotify_track_id = ?
        """,
        (asset["playlist_id"], spotify_track_id),
    ).fetchone()
    return bool(recording and (recording["in_playlist"] or recording["status"] in {"added", "missing", "removed"}))


def _suspected_local_delete_track_ids(con) -> set[int]:
    return {
        row["track_id"]
        for row in con.execute(
            "SELECT DISTINCT track_id FROM youtube_assets WHERE suspected_missing_at IS NOT NULL"
        )
    }


def _spotify_candidate_conflict_track_ids(con) -> set[int]:
    return {
        row["track_id"]
        for row in con.execute(
            """
            SELECT DISTINCT track_id
              FROM events
             WHERE event_type = 'spotify_candidate_already_linked'
               AND track_id IS NOT NULL
            """
        )
    }


def _spotify_candidate_used_by_other_track(con, *, playlist_id: str, track_id: int, spotify_track_id: str):
    if not spotify_track_id:
        return None
    asset = con.execute(
        """
        SELECT *
          FROM spotify_assets
         WHERE playlist_id = ?
           AND spotify_track_id = ?
           AND track_id != ?
         LIMIT 1
        """,
        (playlist_id, spotify_track_id, track_id),
    ).fetchone()
    if asset:
        return asset
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spotify_playlist_recordings'"
    ).fetchone():
        return con.execute(
            """
            SELECT *
             FROM spotify_playlist_recordings
             WHERE playlist_id = ?
               AND spotify_track_id = ?
               AND (track_id IS NULL OR track_id != ?)
             LIMIT 1
            """,
            (playlist_id, spotify_track_id, track_id),
        ).fetchone()
    return None


def _track_has_ambiguous_spotify_recording(con, *, playlist_id: str, track) -> bool:
    """Do not search/add a source row while its playlist recording is unresolved."""
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spotify_playlist_recordings'"
    ).fetchone():
        return False
    source_key = canonical_key(track["display_artist"], track["display_title"])
    for row in con.execute(
        """
        SELECT spotify_artist, spotify_title
          FROM spotify_playlist_recordings
         WHERE playlist_id = ? AND in_playlist = 1
           AND track_id IS NULL AND association_status = 'ambiguous'
        """,
        (playlist_id,),
    ):
        if canonical_key(row["spotify_artist"], row["spotify_title"]) == source_key:
            return True
        if likely_same_recording(
            artist=track["display_artist"],
            title=track["display_title"],
            other_artist=row["spotify_artist"],
            other_title=row["spotify_title"],
        ):
            return True
    return False


class SpotifyAssociationConflict(RuntimeError):
    """A snapshot cannot safely replace an existing candidate association."""


def _recording_artist_ids(value) -> frozenset[str]:
    if isinstance(value, SpotifyTrack):
        values = value.artist_ids
    elif isinstance(value, (tuple, list, set, frozenset)):
        values = value
    else:
        try:
            values = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
    if not isinstance(values, (tuple, list, set, frozenset)):
        return frozenset()
    return frozenset(str(item) for item in values if str(item))


def _recording_title_key(value) -> str:
    title = value.title if isinstance(value, SpotifyTrack) else value["spotify_title"]
    return normalize_for_match(title)


def _owner_identity_key(track) -> str:
    """Use the source title's generic version-free identity for owner checks."""
    return canonical_key(track["display_artist"], _core_spotify_title(track["display_title"]))


def _recording_identity_key(item: SpotifyTrack) -> str:
    return canonical_key(item.artist, _core_spotify_title(item.title))


def _recording_metadata_key(item: SpotifyTrack) -> tuple[object, ...]:
    return (
        str(item.uri or ""),
        canonical_key(str(item.artist or ""), str(item.title or "")),
        tuple(sorted(_recording_artist_ids(item))),
        str(item.album or ""),
        str(item.isrc or ""),
        "" if item.duration_ms is None else str(item.duration_ms),
    )


def _snapshot_recordings(snapshot: PlaylistSnapshot) -> list[SpotifyTrack]:
    """Return one deterministic item per recording, retaining ambiguity markers."""
    by_id: dict[str, SpotifyTrack] = {}
    for item in snapshot.tracks:
        if not item.track_id or not item.uri:
            raise SpotifyAssociationConflict("Spotify playlist snapshot association conflict")
        if not item.title or not item.artist:
            item = replace(item, metadata_ambiguous=True)
        previous = by_id.get(item.track_id)
        if previous is None:
            by_id[item.track_id] = item
            continue
        if _recording_metadata_key(previous) == _recording_metadata_key(item):
            continue
        # Keep one row so the snapshot remains processable, but make the
        # recording ineligible for automatic association. Choose the stable
        # lexical representation so response order cannot change diagnostics.
        chosen = min((previous, item), key=_recording_metadata_key)
        by_id[item.track_id] = replace(chosen, metadata_ambiguous=True)
    return sorted(
        by_id.values(),
        key=lambda item: (canonical_key(item.artist, item.title), item.track_id, item.uri),
    )


@dataclass(frozen=True)
class _SnapshotResolution:
    item: SpotifyTrack
    canonical_key: str
    track_id: int | None = None
    create_track: bool = False
    ambiguous_reason: str = ""


def _recording_anchor_matches(anchor, item: SpotifyTrack) -> bool:
    anchor_ids = _recording_artist_ids(anchor)
    candidate_ids = _recording_artist_ids(item)
    return bool(anchor_ids and candidate_ids and anchor_ids == candidate_ids and _recording_title_key(anchor) == _recording_title_key(item))


def _stored_spotify_metadata_matches(row, item: SpotifyTrack) -> bool:
    """Check a known provider row before comparing its local owner label."""
    stored_artist = str(row["spotify_artist"] or "")
    stored_title = str(row["spotify_title"] or "")
    if not stored_artist or not stored_title:
        return False

    row_keys = row.keys() if hasattr(row, "keys") else ()
    stored_artist_ids = (
        _recording_artist_ids(row["spotify_artist_ids"])
        if "spotify_artist_ids" in row_keys
        else frozenset()
    )
    item_artist_ids = _recording_artist_ids(item)
    if stored_artist_ids and item_artist_ids and stored_artist_ids != item_artist_ids:
        return False

    stored_isrc = str(row["spotify_isrc"] or "") if "spotify_isrc" in row_keys else ""
    if stored_isrc and item.isrc and stored_isrc.casefold() != item.isrc.casefold():
        return False

    stored_duration = row["duration_ms"] if "duration_ms" in row_keys else None
    if stored_duration is not None and item.duration_ms is not None:
        try:
            if abs(int(stored_duration) - int(item.duration_ms)) > 20_000:
                return False
        except (TypeError, ValueError):
            return False

    return canonical_key(stored_artist, stored_title) == canonical_key(item.artist, item.title) or likely_same_recording(
        artist=stored_artist,
        title=stored_title,
        other_artist=item.artist,
        other_title=item.title,
    )


def _known_spotify_metadata_matches(
    provider_rows: dict[str, list[object]], item: SpotifyTrack
) -> bool:
    """Use stored provider metadata when a local owner label is a variant."""
    return any(
        _stored_spotify_metadata_matches(row, item)
        for row in provider_rows.get(item.track_id, [])
    )


def _spotify_track_has_established_history(con, *, playlist_id: str, track_id: int) -> bool:
    """Check whether a canonical duplicate already owns playlist history."""
    assets = list(
        con.execute(
            "SELECT * FROM spotify_assets WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        )
    )
    if any(_spotify_asset_has_primary_history(con, asset) for asset in assets):
        return True
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spotify_playlist_recordings'"
    ).fetchone():
        return False
    return bool(
        con.execute(
            """
            SELECT 1
              FROM spotify_playlist_recordings
             WHERE playlist_id = ? AND track_id = ?
               AND (in_playlist = 1 OR status IN ('added', 'missing', 'removed'))
             LIMIT 1
            """,
            (playlist_id, track_id),
        ).fetchone()
    )


def _snapshot_association_plan(con, snapshot: PlaylistSnapshot) -> list[_SnapshotResolution]:
    """Plan stable recording ownership before any apply-mode write."""
    recordings = _snapshot_recordings(snapshot)
    existing_tracks: dict[str, int] = {}
    registry_rows: dict[str, object] = {}
    provider_rows: dict[str, list[object]] = {}
    asset_owners: dict[str, int] = {}
    owner_identity: dict[int, str] = {}
    owner_labels: dict[int, tuple[str, str]] = {}
    anchors: dict[int, list[object]] = {}
    primary_ids: dict[int, str] = {}
    table_exists = False
    if con is not None:
        existing_tracks = {
            row["canonical_key"]: int(row["id"])
            for row in con.execute("SELECT id, canonical_key FROM tracks")
        }
        owner_identity = {
            int(row["id"]): _owner_identity_key(row)
            for row in con.execute("SELECT id, display_artist, display_title FROM tracks")
        }
        owner_labels = {
            int(row["id"]): (row["display_artist"], row["display_title"])
            for row in con.execute("SELECT id, display_artist, display_title FROM tracks")
        }
        primary_ids = {
            int(row["track_id"]): str(row["spotify_track_id"] or "")
            for row in con.execute(
                "SELECT track_id, spotify_track_id FROM spotify_assets WHERE playlist_id = ?",
                (snapshot.playlist_id,),
            )
        }
        table_exists = bool(
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spotify_playlist_recordings'"
            ).fetchone()
        )
        if table_exists:
            for row in con.execute(
                """
                SELECT * FROM spotify_playlist_recordings
                 WHERE playlist_id = ? AND track_id IS NOT NULL
                """,
                (snapshot.playlist_id,),
            ):
                registry_rows[row["spotify_track_id"]] = row
                provider_rows.setdefault(row["spotify_track_id"], []).append(row)
                if row["association_status"] == "linked" and _recording_artist_ids(row):
                    anchors.setdefault(int(row["track_id"]), []).append(row)
        for row in con.execute(
            """
            SELECT *
              FROM spotify_assets
             WHERE playlist_id = ? AND spotify_track_id IS NOT NULL AND spotify_track_id <> ''
            """,
            (snapshot.playlist_id,),
        ):
            provider_rows.setdefault(row["spotify_track_id"], []).append(row)
            asset_owners[row["spotify_track_id"]] = int(row["track_id"])

    for track_id, rows in anchors.items():
        primary_id = primary_ids.get(track_id, "")
        rows.sort(key=lambda row: (0 if row["spotify_track_id"] == primary_id else 1, row["spotify_track_id"]))

    grouped: dict[str, list[SpotifyTrack]] = {}
    for item in recordings:
        grouped.setdefault(canonical_key(item.artist, item.title), []).append(item)

    resolutions: dict[str, _SnapshotResolution] = {}
    ambiguous_reason = "ambiguous Spotify recording metadata"
    for key, items in grouped.items():
        if all(item.metadata_ambiguous for item in items):
            for item in items:
                registry = registry_rows.get(item.track_id)
                owner = registry['track_id'] if registry is not None and registry['track_id'] is not None else asset_owners.get(item.track_id)
                resolutions[item.track_id] = _SnapshotResolution(item, key, track_id=owner, ambiguous_reason=ambiguous_reason)
            continue
        known_owners = {
            int(registry_rows[item.track_id]["track_id"])
            for item in items
            if item.track_id in registry_rows and registry_rows[item.track_id]["track_id"] is not None
        }
        known_owners.update(asset_owners[item.track_id] for item in items if item.track_id in asset_owners)
        existing_track_id = existing_tracks.get(key)
        for owner_id in known_owners:
            owner_artist, owner_title = owner_labels.get(owner_id, ("", ""))
            same_recording = owner_id in owner_labels and likely_same_recording(
                artist=owner_artist,
                title=owner_title,
                other_artist=items[0].artist,
                other_title=items[0].title,
            )
            if (
                owner_id in owner_identity
                and owner_identity[owner_id] != _recording_identity_key(items[0])
                and not same_recording
                and not _known_spotify_metadata_matches(provider_rows, items[0])
            ):
                raise SpotifyAssociationConflict("Spotify playlist snapshot association conflict")
        if existing_track_id is not None:
            if known_owners and known_owners != {existing_track_id}:
                known_owner_established = len(known_owners) == 1 and _spotify_track_has_established_history(
                    con,
                    playlist_id=snapshot.playlist_id,
                    track_id=next(iter(known_owners)),
                )
                if (
                    len(known_owners) != 1
                    or _spotify_track_has_established_history(
                        con,
                        playlist_id=snapshot.playlist_id,
                        track_id=existing_track_id,
                    )
                    or not known_owner_established
                ):
                    raise SpotifyAssociationConflict("Spotify playlist snapshot association conflict")
                # A duplicate local canonical label is only a source row when
                # it has no established Spotify membership/history. Keep the
                # known provider-ID owner and leave the duplicate untouched.
                target_track_id = next(iter(known_owners))
            else:
                target_track_id = next(iter(known_owners), existing_track_id)
        elif len(known_owners) > 1:
            raise SpotifyAssociationConflict("Spotify playlist snapshot association conflict")
        else:
            target_track_id = next(iter(known_owners), None)

        anchor_rows = list(anchors.get(target_track_id, [])) if target_track_id is not None else []
        owner_items = {
            item.track_id
            for item in items
            if item.track_id in registry_rows and registry_rows[item.track_id]["track_id"] == target_track_id
        }
        owner_items.update(item.track_id for item in items if asset_owners.get(item.track_id) == target_track_id)
        anchor_rows.extend(
            item
            for item in items
            if item.track_id in owner_items and _recording_artist_ids(item) and not item.metadata_ambiguous
        )
        def _anchor_sort_key(row):
            if isinstance(row, SpotifyTrack):
                return (0, row.track_id)
            return (0 if row["spotify_track_id"] == primary_ids.get(target_track_id, "") else 1, row["spotify_track_id"])

        anchor_rows.sort(key=_anchor_sort_key)
        anchor_item = None
        if target_track_id is not None and not anchor_rows and not primary_ids.get(target_track_id, ""):
            # A track that predates the registry (or has an id-less legacy
            # primary) still needs a deterministic first anchor. Once that
            # anchor carries Spotify artist IDs, later recordings must satisfy
            # the normal recording identity check before they are linked.
            eligible = [item for item in items if _recording_artist_ids(item) and not item.metadata_ambiguous]
            if eligible:
                anchor_item = eligible[0]
            elif len(items) == 1 and not items[0].metadata_ambiguous:
                # Preserve the historical single-recording import for old
                # fixtures/databases whose provider payload had no artist IDs.
                anchor_item = items[0]
            if anchor_item is not None:
                anchor_rows = [anchor_item]
        if target_track_id is None:
            # A new logical track needs one deterministic anchor. Prefer a
            # recording with a nonempty artist-ID set; otherwise retain the
            # historical single-recording behaviour and leave extras under
            # review because they cannot be distinguished safely.
            eligible = [item for item in items if _recording_artist_ids(item) and not item.metadata_ambiguous]
            anchor_item = (eligible or items[:1])[0] if items else None
            if anchor_item is None:
                continue
            target_track_id = -1
            anchor_rows = [anchor_item]
            owner_items = set()
        for item in items:
            if item.metadata_ambiguous:
                resolutions[item.track_id] = _SnapshotResolution(item, key, ambiguous_reason=ambiguous_reason)
                continue
            owned = item.track_id in owner_items
            matches_anchor = (
                owned
                or (anchor_item is not None and item.track_id == anchor_item.track_id)
                or any(_recording_anchor_matches(anchor, item) for anchor in anchor_rows)
            )
            if target_track_id == -1:
                if item.track_id == anchor_item.track_id:
                    resolutions[item.track_id] = _SnapshotResolution(item, key, create_track=True)
                elif matches_anchor:
                    resolutions[item.track_id] = _SnapshotResolution(item, key)
                else:
                    resolutions[item.track_id] = _SnapshotResolution(item, key, ambiguous_reason=ambiguous_reason)
            elif matches_anchor:
                resolutions[item.track_id] = _SnapshotResolution(item, key, track_id=target_track_id)
            else:
                resolutions[item.track_id] = _SnapshotResolution(item, key, ambiguous_reason=ambiguous_reason)
    return [resolutions[item.track_id] for item in recordings]


def _validate_snapshot_associations(con, snapshot: PlaylistSnapshot) -> None:
    """Validate the same ownership plan used by apply-mode imports."""
    _snapshot_association_plan(con, snapshot)


def _validate_playlist_snapshot(snapshot: PlaylistSnapshot) -> None:
    if not snapshot.complete or not snapshot.snapshot_id:
        raise RuntimeError("Spotify playlist snapshot was incomplete")
    if snapshot.tracks:
        if not any(track.track_id for track in snapshot.tracks):
            raise RuntimeError("Spotify playlist snapshot had no usable track identities")
        _snapshot_recordings(snapshot)


def _spotify_presence_status(config: Config, asset, con=None) -> str:
    if asset is None:
        return "added"
    if con is not None and asset["status"] == "removed":
        if con.execute(
            "SELECT 1 FROM events WHERE event_type = 'spotify_tentative_removed_by_user' AND dedupe_key = ?",
            (f"spotify_tentative_removed_by_user:{asset['track_id']}:{asset['id']}",),
        ).fetchone():
            return "review"
    confidence = asset["match_confidence"]
    if (
        asset["status"] == "review"
        or confidence is None
        or (
            asset["status"] != "added"
            and confidence < config.float("HCR_SPOTIFY_MATCH_THRESHOLD")
        )
    ):
        return "review"
    return "added"


def _spotify_asset_for_recording_owner(con, *, playlist_id: str, track_id: int):
    return con.execute(
        "SELECT * FROM spotify_assets WHERE playlist_id = ? AND track_id = ?",
        (playlist_id, track_id),
    ).fetchone()


def _import_playlist_snapshot(
    config: Config,
    snapshot: PlaylistSnapshot,
    *,
    apply: bool,
    event_source: str,
    establish_baseline: bool,
) -> SpotifySummary:
    summary = SpotifySummary()
    summary.seen = len(snapshot.tracks)
    recordings = _snapshot_recordings(snapshot)
    if not apply:
        if config.db_path.exists():
            # The normal DB connector performs migrations. Preview validation
            # must neither migrate nor create the configured database.
            with closing(sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
                con.row_factory = sqlite3.Row
                resolutions = _snapshot_association_plan(con, snapshot)
        else:
            resolutions = _snapshot_association_plan(None, snapshot)
        summary.linked = sum(1 for resolution in resolutions if not resolution.ambiguous_reason)
        summary.ambiguous = len(resolutions) - summary.linked
        return summary
    with connect(config) as con:
        with transaction(con):
            resolutions = _snapshot_association_plan(con, snapshot)
            created_tracks: dict[str, int] = {}
            for resolution in resolutions:
                item = resolution.item
                existing_recording = con.execute(
                    """
                    SELECT * FROM spotify_playlist_recordings
                     WHERE playlist_id = ? AND spotify_track_id = ?
                    """,
                    (snapshot.playlist_id, item.track_id),
                ).fetchone()
                if resolution.ambiguous_reason:
                    if existing_recording is not None and existing_recording["track_id"] is not None:
                        # A duplicate provider row may disagree with itself,
                        # but it must never overwrite an already-owned
                        # recording's metadata or association. Record only
                        # presence and a redacted diagnostic.
                        con.execute(
                            """
                            UPDATE spotify_playlist_recordings
                               SET in_playlist = 1,
                                   status = CASE WHEN status = 'review' THEN 'review' ELSE 'added' END,
                                   last_seen_at = ?, suspected_missing_at = NULL, updated_at = ?
                             WHERE id = ?
                            """,
                            (now_utc(), now_utc(), existing_recording["id"]),
                        )
                        asset = _spotify_asset_for_recording_owner(
                            con,
                            playlist_id=snapshot.playlist_id,
                            track_id=existing_recording["track_id"],
                        )
                        if asset is not None:
                            upsert_spotify_asset(
                                con,
                                track_id=asset["track_id"],
                                playlist_id=snapshot.playlist_id,
                                in_playlist=True,
                                match_confidence=asset["match_confidence"],
                                status=_spotify_presence_status(config, asset, con),
                            )
                        add_event(
                            con,
                            existing_recording["track_id"],
                            "ambiguous_spotify_recording",
                            event_source,
                            {"spotify_track_id": item.track_id, "reason": resolution.ambiguous_reason},
                            dedupe_key=f"ambiguous_spotify_recording:{snapshot.playlist_id}:{item.track_id}:{resolution.ambiguous_reason}",
                        )
                        summary.ambiguous += 1
                        continue
                    upsert_spotify_playlist_recording(
                        con,
                        playlist_id=snapshot.playlist_id,
                        spotify_track_id=item.track_id,
                        spotify_track_uri=item.uri,
                        spotify_artist=item.artist,
                        spotify_title=item.title,
                        artist_ids=item.artist_ids,
                        album=item.album,
                        isrc=item.isrc,
                        duration_ms=item.duration_ms,
                        track_id=None,
                        in_playlist=True,
                        status="review",
                        association_status="ambiguous",
                        association_reason=resolution.ambiguous_reason,
                    )
                    add_event(
                        con,
                        None,
                        "ambiguous_spotify_recording",
                        event_source,
                        {
                            "spotify_track_id": item.track_id,
                            "reason": resolution.ambiguous_reason,
                        },
                        dedupe_key=f"ambiguous_spotify_recording:{snapshot.playlist_id}:{item.track_id}:{resolution.ambiguous_reason}",
                    )
                    summary.ambiguous += 1
                    continue
                track_id = resolution.track_id
                if track_id is None:
                    track_id = created_tracks.get(resolution.canonical_key)
                if track_id is None and resolution.create_track:
                    track = ensure_track(con, artist=item.artist, title=item.title, status="wanted")
                    track_id = int(track["id"])
                    created_tracks[resolution.canonical_key] = track_id
                if track_id is None:
                    # A matching secondary appeared before its new anchor only
                    # if the snapshot was malformed; keep the refusal generic.
                    raise SpotifyAssociationConflict("Spotify playlist snapshot association conflict")
                track = con.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
                if track is None:
                    raise SpotifyAssociationConflict("Spotify playlist snapshot association conflict")
                existing_asset = con.execute(
                    "SELECT * FROM spotify_assets WHERE track_id = ? AND playlist_id = ?",
                    (track["id"], snapshot.playlist_id),
                ).fetchone()
                existing_recording_status = existing_recording["status"] if existing_recording is not None else None
                upsert_spotify_playlist_recording(
                    con,
                    playlist_id=snapshot.playlist_id,
                    spotify_track_id=item.track_id,
                    spotify_track_uri=item.uri,
                    spotify_artist=item.artist,
                    spotify_title=item.title,
                    artist_ids=item.artist_ids,
                    album=item.album,
                    isrc=item.isrc,
                    duration_ms=item.duration_ms,
                    track_id=track["id"],
                    in_playlist=True,
                    status="review" if existing_recording_status == "review" else "added",
                    association_status="linked",
                    association_reason=resolution.ambiguous_reason,
                )
                if existing_asset is None:
                    # A new logical track gets a deterministic primary: the
                    # recordings list is sorted by canonical identity and ID.
                    upsert_spotify_asset(
                        con,
                        track_id=track["id"],
                        playlist_id=snapshot.playlist_id,
                        spotify_track_uri=item.uri,
                        spotify_track_id=item.track_id,
                        spotify_artist=item.artist,
                        spotify_title=item.title,
                        in_playlist=True,
                        match_confidence=1.0,
                        status="added",
                        added_at=None,
                    )
                elif not existing_asset["spotify_track_id"] or existing_asset["spotify_track_id"] == item.track_id:
                    # This is the primary recording (or a legacy id-less
                    # association). Refresh provider metadata while retaining
                    # its stored confidence, classification, and timestamps.
                    upsert_spotify_asset(
                        con,
                        track_id=track["id"],
                        playlist_id=snapshot.playlist_id,
                        spotify_track_uri=item.uri,
                        spotify_track_id=item.track_id,
                        spotify_artist=item.artist,
                        spotify_title=item.title,
                        in_playlist=True,
                        match_confidence=existing_asset["match_confidence"],
                        status=_spotify_presence_status(config, existing_asset, con),
                        added_at=None,
                    )
                else:
                    # A secondary recording establishes logical membership but
                    # must never replace the primary's provenance.
                    con.execute(
                        """
                        UPDATE spotify_assets
                           SET in_playlist = 1,
                               status = ?,
                               suspected_missing_at = NULL,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (_spotify_presence_status(config, existing_asset, con), now_utc(), existing_asset["id"]),
                    )
                if track["status"] == "excluded":
                    add_event(
                        con,
                        track["id"],
                        "skipped_excluded_track",
                        event_source,
                        {"spotify_track_id": item.track_id, "playlist_id": snapshot.playlist_id},
                        dedupe_key=f"spotify_scan_skipped_excluded:{snapshot.playlist_id}:{item.track_id}",
                    )
                    summary.skipped += 1
                    continue
                add_event(
                    con,
                    track["id"],
                    "spotify_playlist_seen",
                    event_source,
                    {"spotify_track_id": item.track_id, "playlist_id": snapshot.playlist_id},
                    dedupe_key=f"spotify_playlist_seen:{snapshot.playlist_id}:{item.track_id}",
                )
                summary.linked += 1
            if establish_baseline and snapshot.identified:
                set_state(con, "spotify_baseline_complete", "true")
                set_state(con, "last_spotify_snapshot_id", snapshot.snapshot_id)
                set_state(con, "last_spotify_playlist_count", str(len(snapshot.tracks)))
                set_state(con, "last_spotify_scan_at", now_utc())
            _clear_spotify_rate_limit(con, event_source=event_source)
    return summary


def backfill_spotify(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None) -> SpotifySummary:
    summary = SpotifySummary()
    if not spotify_enabled(config):
        summary.skipped += 1
        return summary
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        raise RuntimeError("HCR_SPOTIFY_PLAYLIST_ID is required")
    if _configured_spotify_cooldown_active(config):
        return SpotifySummary(skipped=1, rate_limited=True)
    client = client or SpotipyClient(config)
    try:
        from .spotify_work import obtain_snapshot
        if not apply and not config.db_path.exists():
            snapshot = client.playlist_snapshot(playlist_id)
        else:
            with (connect(config) if apply else closing(sqlite3.connect(config.db_path.resolve().as_uri() + '?mode=ro', uri=True))) as con:
                con.row_factory = sqlite3.Row
                snapshot = obtain_snapshot(con, config, client, protected=False, apply=apply)
    except BudgetDeferred as exc:
        return SpotifySummary(budget_deferred=1, required_budget=exc.required or 0)
    except Exception as exc:
        if not _is_rate_limited(exc):
            raise
        if apply:
            with connect(config) as con:
                _remember_spotify_rate_limit(con, config, exc, event_source="spotify_backfill")
        return SpotifySummary(skipped=1, rate_limited=True)
    _validate_playlist_snapshot(snapshot)
    if apply:
        from .spotify_work import recover_pending
        with connect(config) as con:
            recovered = recover_pending(con, config, client, snapshot, dispatch=False)
            if recovered.failure:
                raise recovered.failure
    return _import_playlist_snapshot(config, snapshot, apply=apply, event_source="spotify_backfill", establish_baseline=True)


def scan_spotify_playlist(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None, protected: bool = True) -> SpotifySummary:
    summary = SpotifySummary()
    if not spotify_enabled(config):
        summary.skipped += 1
        return summary
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        summary.skipped += 1
        return summary
    if _configured_spotify_cooldown_active(config):
        return SpotifySummary(skipped=1, rate_limited=True)
    client = client or SpotipyClient(config)
    summary._client = client
    try:
        from .spotify_work import obtain_snapshot
        if not apply and not config.db_path.exists():
            snapshot = client.playlist_snapshot(playlist_id)
        else:
            with (connect(config) if apply else closing(sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True))) as con:
                con.row_factory = sqlite3.Row
                snapshot = obtain_snapshot(con, config, client, protected=protected, apply=apply)
        _validate_playlist_snapshot(snapshot)
    except BudgetDeferred as exc:
        summary.budget_deferred = 1
        summary.required_budget = exc.required or 0
        summary.requests = getattr(getattr(client, "budget", None), "used", 0)
        return summary
    except Exception as exc:
        if _is_rate_limited(exc):
            if apply:
                with connect(config) as con:
                    _remember_spotify_rate_limit(con, config, exc, event_source="spotify_scan")
            summary.rate_limited = True
            summary.skipped += 1
            return summary
        raise
    if apply:
        from .spotify_work import recover_pending
        with connect(config) as con:
            recovered = recover_pending(con, config, client, snapshot, dispatch=False)
            if recovered.failure:
                raise recovered.failure
    summary = _import_playlist_snapshot(config, snapshot, apply=apply, event_source="spotify_scan", establish_baseline=False)
    if apply:
        summary.added = recovered.added
        summary.tentative_added = recovered.tentative_added
        summary.pending = recovered.pending
    summary._snapshot = snapshot
    summary._client = client
    if hasattr(client, "budget"):
        client.budget.reserve = 6
    summary.requests = getattr(getattr(client, "budget", None), "used", 0)
    return summary


def _save_search_schedule(con, playlist_id, track_id, *, searched_at, attempts, next_at, status='review', confidence=None):
    asset = con.execute('SELECT * FROM spotify_assets WHERE playlist_id=? AND track_id=?', (playlist_id, track_id)).fetchone()
    if asset is None:
        upsert_spotify_asset(con, playlist_id=playlist_id, track_id=track_id, in_playlist=False,
            match_confidence=confidence, status=status, search_last_at=searched_at, search_attempts=attempts,
            search_next_at=next_at, update_search=True)
    else:
        con.execute('UPDATE spotify_assets SET search_last_at=?,search_attempts=?,search_next_at=?,updated_at=? WHERE id=?',
                    (searched_at, attempts, next_at, now_utc(), asset['id']))


@contextmanager
def _spotify_connection(config, *, apply):
    if apply:
        with connect(config) as con:
            yield con
        return
    if not config.db_path.exists():
        from .db import SCHEMA
        with closing(sqlite3.connect(':memory:')) as con:
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            con.execute("CREATE TEMP VIEW spotify_pending_work AS SELECT NULL AS playlist_id,NULL AS track_id,NULL AS kind,NULL AS payload_json WHERE 0")
            yield con
        return
    with closing(sqlite3.connect(config.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='spotify_pending_work'").fetchone():
            # Legacy previews need only an empty work view, never a database copy.
            con.execute("CREATE TEMP VIEW spotify_pending_work AS SELECT NULL AS playlist_id,NULL AS track_id,NULL AS kind,NULL AS payload_json WHERE 0")
        yield con


def sync_spotify(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None,
                 snapshot: PlaylistSnapshot | None = None) -> SpotifySummary:
    from dataclasses import asdict
    from .spotify_adapter import BudgetDeferred
    from .spotify_work import save_work, recover_pending, obtain_snapshot
    summary = SpotifySummary()
    playlist_id = config.get('HCR_SPOTIFY_PLAYLIST_ID')
    if not spotify_enabled(config) or not playlist_id:
        summary.skipped = 1
        return summary
    with _spotify_connection(config, apply=apply) as con:
        if _spotify_cooldown_active(con):
            if apply:
                with transaction(con):
                    _log_spotify_cooldown_skip(con)
            summary.rate_limited = True
            summary.skipped = 1
            return summary
        client = client or SpotipyClient(config)
        budget = getattr(client, 'budget', None)
        if budget:
            budget.reserve = 6
        pending_result = None
        if apply:
            pending_result = recover_pending(con, config, client, snapshot)
            summary.added += pending_result.added
            summary.tentative_added += pending_result.tentative_added
            summary.acknowledged += pending_result.acknowledged
            summary.pending = pending_result.pending
            summary.budget_deferred += pending_result.deferred
            if pending_result.rate_limited:
                summary.rate_limited = True
                return summary
            if pending_result.failure:
                raise pending_result.failure
        if budget:
            budget.reserve = 0
        weights = (config.int('HCR_SPOTIFY_FIRST_TIME_WEIGHT'), config.int('HCR_SPOTIFY_RETRY_WEIGHT'))
        if min(weights) < 1:
            raise ValueError('Spotify rotation weights must be positive')
        cycle = ['first'] * weights[0] + ['retry'] * weights[1]
        cursor_key = f'spotify_rotation:{playlist_id}'
        cursor = int(get_state(con, cursor_key, '0')) % len(cycle)
        now = datetime.now(timezone.utc)
        suspected = _suspected_local_delete_track_ids(con)
        conflicts = _spotify_candidate_conflict_track_ids(con)
        removed_events = {row[0] for row in con.execute("SELECT DISTINCT track_id FROM events WHERE event_type='spotify_tentative_removed_by_user'")}
        threshold = config.float('HCR_SPOTIFY_MATCH_THRESHOLD')
        tentative_threshold = config.float('HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD')
        lanes = {'first': [], 'retry': []}
        for track in _spotify_sync_candidates(con, playlist_id=playlist_id):
            asset = con.execute('SELECT * FROM spotify_assets WHERE playlist_id=? AND track_id=?', (playlist_id, track['id'])).fetchone()
            work = con.execute("SELECT * FROM spotify_pending_work WHERE playlist_id=? AND track_id=? AND kind='search'", (playlist_id, track['id'])).fetchone()
            queued_write = con.execute("SELECT 1 FROM spotify_pending_work WHERE playlist_id=? AND track_id=? AND kind!='search'", (playlist_id, track['id'])).fetchone()
            if track['id'] in suspected or (asset and asset['in_playlist']) or queued_write:
                summary.skipped += 1
                if apply and track['id'] in suspected:
                    with transaction(con):
                        add_event(con, track['id'], 'spotify_skipped_suspected_local_delete', 'spotify_sync', {'reason':'local deletion awaiting confirmation'}, dedupe_key=f"spotify_skipped_suspected_local_delete:{track['id']}")
                continue
            if track['id'] in conflicts or track['id'] in removed_events or (asset and asset['status']=='removed' and _is_removed_tentative_asset(asset, threshold)) or _track_has_ambiguous_spotify_recording(con, playlist_id=playlist_id, track=track):
                summary.review += 1
                continue
            if asset and _spotify_search_deferred(asset, now):
                if asset['status'] == 'review':
                    summary.review += 1
                else:
                    summary.skipped += 1
                continue
            if asset and asset['status'] == 'review' and not config.bool('HCR_SPOTIFY_ADD_REVIEW_MATCHES'):
                summary.review += 1
                continue
            if looks_like_non_track(track['display_artist'], track['display_title']):
                summary.review += 1
                if apply and asset is None:
                    with transaction(con):
                        upsert_spotify_asset(con, track_id=track['id'], playlist_id=playlist_id, in_playlist=False, match_confidence=0.0, status='review')
                continue
            lane = json.loads(work['payload_json'])['lane'] if work else ('first' if not asset or not asset['search_last_at'] else 'retry')
            lanes[lane].append((track, asset, work))
        searched = 0
        sync_limit = config.int('HCR_SPOTIFY_SYNC_LIMIT')
        while (lanes['first'] or lanes['retry']) and (sync_limit <= 0 or searched < sync_limit):
            wanted_lane = cycle[cursor]
            lane = wanted_lane if lanes[wanted_lane] else ('retry' if wanted_lane == 'first' else 'first')
            both = bool(lanes['first'] and lanes['retry'])
            track, asset, work = lanes[lane].pop(0)
            fingerprint = [track['display_artist'], track['display_title']]
            payload = json.loads(work['payload_json']) if work else {}
            if payload.get('source') != fingerprint:
                payload = {'source': fingerprint, 'lane': lane, 'next_query': 0, 'candidates': []}
            searched += 1
            try:
                if hasattr(client, 'search_query'):
                    queries = client.search_queries(*fingerprint)
                    for index in range(payload['next_query'], len(queries)):
                        # Leave room for a write and compensation; reads resume next run.
                        if budget and budget.remaining < 3:
                            raise BudgetDeferred()
                        candidates = client.search_query(queries[index])
                        by_id = {item['track_id']: item for item in payload['candidates']}
                        by_id.update({item.track_id: asdict(item) for item in candidates})
                        payload['candidates'] = list(by_id.values())
                        payload['next_query'] = index + 1
                        if apply:
                            with transaction(con):
                                save_work(con, playlist_id, track['id'], 'search', payload)
                    candidates = [SpotifyTrack(**item) for item in payload['candidates']]
                else:
                    candidates = client.search_track(*fingerprint)
            except BudgetDeferred:
                if apply:
                    with transaction(con):
                        save_work(con, playlist_id, track['id'], 'search', payload)
                summary.budget_deferred += 1
                break
            except Exception as exc:
                if apply:
                    with transaction(con):
                        save_work(con, playlist_id, track['id'], 'search', payload)
                    terminal = getattr(exc, 'http_status', None) in {400, 404}
                    next_cursor = (cursor + 1) % len(cycle) if both else 0
                    _remember_failed_spotify_attempt(con, config, track, asset, exc, operation='search', rotation=(cursor_key, next_cursor) if terminal else None)
                    if not _is_rate_limited(exc) and not terminal:
                        with transaction(con):
                            # Retry a fresh candidate pool after provider backoff.
                            payload['next_query'], payload['candidates'] = 0, []
                            save_work(con, playlist_id, track['id'], 'search', payload)
                if _is_rate_limited(exc):
                    summary.rate_limited = True
                    summary.skipped += 1
                    break
                if getattr(exc, 'http_status', None) in {400,404}:
                    cursor = (cursor + 1) % len(cycle) if both else 0
                    continue
                raise
            best, best_score = None, 0.0
            for candidate in candidates:
                score = _spotify_match_score(track, candidate)
                if score > best_score:
                    best, best_score = candidate, score
            confident = best is not None and best_score >= threshold
            matched = confident or (best is not None and config.bool('HCR_SPOTIFY_ADD_REVIEW_MATCHES') and best_score >= tentative_threshold)
            searched_dt = datetime.now(timezone.utc)
            searched_at = _format_utc(searched_dt)
            attempts = 0 if matched else _spotify_search_attempts(asset) + 1
            next_at = None if matched else _next_spotify_retry_at(searched_dt, attempts)
            conflict = _spotify_candidate_used_by_other_track(con, playlist_id=playlist_id, track_id=track['id'], spotify_track_id=best.track_id) if matched else None
            if not matched or conflict:
                summary.review += 1
            if not apply:
                if matched and not conflict:
                    summary.added += 1
                    summary.tentative_added += int(not confident)
                cursor = (cursor + 1) % len(cycle) if both else 0
                continue
            with transaction(con):
                _save_search_schedule(con, playlist_id, track['id'], searched_at=searched_at, attempts=attempts, next_at=next_at, confidence=0.0 if conflict else (best_score if not matched else None))
                cursor = (cursor + 1) % len(cycle) if both else 0
                set_state(con, cursor_key, str(cursor))
                set_state(con, f'spotify_scan_only_spent:{playlist_id}', 'false')
                con.execute("DELETE FROM spotify_pending_work WHERE playlist_id=? AND track_id=? AND kind='search'", (playlist_id, track['id']))
                add_event(con, track['id'], 'spotify_search_completed', 'spotify_sync', {
                    'playlist_id': playlist_id, 'lane': lane, 'spotify_search_last_at': searched_at,
                    'spotify_search_attempts': attempts, 'spotify_search_next_at': next_at,
                    'spotify_track_id': best.track_id if best else '', 'score': best_score,
                    'outcome': 'conflict' if conflict else 'matched' if matched else 'review',
                })
                if conflict:
                    add_event(con, track['id'], 'spotify_candidate_already_linked', 'spotify_sync', {
                        'playlist_id': playlist_id, 'spotify_track_id': best.track_id,
                        'existing_track_id': conflict['track_id'], 'existing_asset_id': conflict['id'],
                        'reason': 'candidate Spotify recording already owned',
                    }, dedupe_key=f"spotify_candidate_already_linked:{track['id']}:{best.track_id}:{conflict['id']}")
                elif not matched:
                    add_event(con, track['id'], 'ambiguous_spotify_match', 'spotify_sync', {
                        'playlist_id': playlist_id, 'spotify_track_id': best.track_id if best else '',
                        'score': best_score, 'reason': 'below tentative threshold or not found', 'match_status': 'review', 'spotify_search_last_at': searched_at,
                        'spotify_search_attempts': attempts, 'spotify_search_next_at': next_at,
                    })
                else:
                    save_work(con, playlist_id, track['id'], 'add', {'candidate': asdict(best),
                        'confident': confident, 'score': best_score, 'searched_at': searched_at, 'source': fingerprint}, work_key=best.uri)
            summary.first_time += int(lane == 'first')
            summary.retries += int(lane == 'retry')
            if matched and not conflict:
                pending = recover_pending(con, config, client, snapshot)
                snapshot = None
                summary.added += pending.added
                summary.tentative_added += pending.tentative_added
                summary.acknowledged += pending.acknowledged
                summary.pending = pending.pending
                summary.budget_deferred += pending.deferred
                if pending.rate_limited:
                    summary.rate_limited = True
                    break
                if pending.failure:
                    raise pending.failure
        summary.skipped += len(lanes['first']) + len(lanes['retry'])
        if budget:
            summary.requests = budget.used
    return summary
