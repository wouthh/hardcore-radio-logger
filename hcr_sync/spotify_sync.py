"""Spotify backfill and sync using Spotipy."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
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
)
from .identity import compact_text, duplicate_title_tokens, match_confidence

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
REMIX_RE = re.compile(r"\bremix\b", re.I)
MAIN_ARTIST_SPLIT_RE = re.compile(r"\s+(?:&|\+|x|and|vs\.?|feat\.?|ft\.?|featuring)\s+|[,/|]", re.I)
SPOTIFY_FIRST_RETRY_DAYS = 7
SPOTIFY_STEADY_RETRY_DAYS = 14


@dataclass(frozen=True)
class SpotifyTrack:
    uri: str
    track_id: str
    artist: str
    title: str
    duration_ms: int | None = None


@dataclass(frozen=True)
class PlaylistSnapshot:
    playlist_id: str
    snapshot_id: str
    tracks: list[SpotifyTrack]
    complete: bool = True


class SpotifyClientProtocol(Protocol):
    def auth_check(self) -> str: ...
    def playlist_snapshot(self, playlist_id: str) -> PlaylistSnapshot: ...
    def search_track(self, artist: str, title: str) -> list[SpotifyTrack]: ...
    def add_tracks(self, playlist_id: str, uris: list[str]) -> None: ...
    def remove_tracks(self, playlist_id: str, uris: list[str]) -> None: ...


class SpotipyClient:
    def __init__(self, config: Config):
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
        except Exception as exc:
            raise RuntimeError("Spotipy is not installed; install requirements.txt first") from exc

        self.config = config
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=config.get("HCR_SPOTIFY_CLIENT_ID"),
                client_secret=config.get("HCR_SPOTIFY_CLIENT_SECRET"),
                redirect_uri=config.get("HCR_SPOTIFY_REDIRECT_URI"),
                scope=config.spotify_scopes,
                cache_path=str(config.path("HCR_SPOTIFY_TOKEN_CACHE")),
                open_browser=True,
            ),
            requests_timeout=config.int("HCR_SPOTIFY_REQUEST_TIMEOUT"),
            retries=config.int("HCR_SPOTIFY_REQUEST_RETRIES"),
            status_retries=config.int("HCR_SPOTIFY_STATUS_RETRIES"),
        )

    def auth_check(self) -> str:
        user = self.sp.current_user()
        return str(user.get("id") or user.get("display_name") or "authenticated")

    def playlist_snapshot(self, playlist_id: str) -> PlaylistSnapshot:
        meta = self.sp.playlist(playlist_id, fields="snapshot_id")
        snapshot_id = str(meta.get("snapshot_id") or "")
        tracks: list[SpotifyTrack] = []
        offset = 0
        complete = True
        while True:
            page = self.sp.playlist_items(
                playlist_id,
                offset=offset,
                limit=100,
                fields="items(track(id,uri,name,duration_ms,artists(name),type),item(id,uri,name,duration_ms,artists(name),type)),next,total",
            )
            items = page.get("items") or []
            for item in items:
                track = _spotify_track_from_playlist_item(item)
                if track is not None:
                    tracks.append(track)
            if not page.get("next"):
                break
            offset += len(items)
            if not items:
                complete = False
                break
        return PlaylistSnapshot(playlist_id=playlist_id, snapshot_id=snapshot_id, tracks=tracks, complete=complete)

    def search_track(self, artist: str, title: str) -> list[SpotifyTrack]:
        tracks = []
        seen_ids: set[str] = set()
        for query in _spotify_search_queries(artist, title):
            result = self.sp.search(q=query, type="track", limit=10)
            for item in (result.get("tracks") or {}).get("items") or []:
                artists = item.get("artists") or []
                track_id = str(item.get("id") or "")
                uri = str(item.get("uri") or "")
                name = str(item.get("name") or "")
                if not track_id or not uri or not name or track_id in seen_ids:
                    continue
                seen_ids.add(track_id)
                tracks.append(
                    SpotifyTrack(
                        uri=uri,
                        track_id=track_id,
                        artist=", ".join(str(artist.get("name") or "") for artist in artists),
                        title=name,
                        duration_ms=item.get("duration_ms"),
                    )
                )
        return tracks

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        if uris:
            self.sp.playlist_add_items(playlist_id, uris)

    def remove_tracks(self, playlist_id: str, uris: list[str]) -> None:
        if uris:
            self.sp.playlist_remove_all_occurrences_of_items(playlist_id, uris)


def _spotify_track_from_playlist_item(item: dict) -> SpotifyTrack | None:
    track = item.get("track") or item.get("item") or {}
    if track.get("type") and track.get("type") != "track":
        return None
    track_id = str(track.get("id") or "")
    uri = str(track.get("uri") or "")
    title = str(track.get("name") or "")
    if not track_id or not uri or not title:
        return None
    artists = track.get("artists") or []
    return SpotifyTrack(
        uri=uri,
        track_id=track_id,
        artist=", ".join(str(artist.get("name") or "") for artist in artists),
        title=title,
        duration_ms=track.get("duration_ms"),
    )


@dataclass
class SpotifySummary:
    seen: int = 0
    linked: int = 0
    added: int = 0
    tentative_added: int = 0
    review: int = 0
    skipped: int = 0
    rate_limited: bool = False


def spotify_auth(config: Config, client: SpotifyClientProtocol | None = None) -> str:
    client = client or SpotipyClient(config)
    return client.auth_check()


def spotify_enabled(config: Config) -> bool:
    return config.bool("HCR_SPOTIFY_ENABLED")


def looks_like_non_track(artist: str, title: str) -> bool:
    return bool(NON_TRACK_RE.search(f"{artist} {title}"))


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
    source_tokens = duplicate_title_tokens(track["display_title"])
    candidate_tokens = duplicate_title_tokens(_core_spotify_title(candidate.title))
    if not source_tokens or not candidate_tokens:
        return 0.0
    overlap = len(source_tokens & candidate_tokens) / max(1, len(source_tokens))
    reverse_overlap = len(source_tokens & candidate_tokens) / max(1, len(candidate_tokens))
    if overlap < 0.75 or reverse_overlap < 0.75:
        return 0.0
    if REMIX_RE.search(candidate.title) and not REMIX_RE.search(track["display_title"]):
        return 0.0
    return match_confidence(
        artist=track["display_artist"],
        title=track["display_title"],
        candidate_artist=candidate.artist,
        candidate_title=candidate.title,
    )


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "http_status", None)
    headers = getattr(exc, "headers", {}) or {}
    return status == 429 or "Retry-After" in headers or "retry-after" in headers


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
        "error": str(exc)[:500],
    }


def _spotify_cooldown_active(con) -> bool:
    until = _parse_utc(get_state(con, "spotify_rate_limited_until", ""))
    return bool(until and until > datetime.now(timezone.utc))


SPOTIFY_RATE_LIMIT_STATE_KEYS = (
    "spotify_rate_limited_until",
    "spotify_rate_limit_last_response",
    "spotify_rate_limit_source",
)


def _remember_spotify_rate_limit(con, config: Config, exc: Exception, *, event_source: str = "spotify_sync") -> None:
    payload = _rate_limit_details(config, exc)
    payload["event_source"] = event_source
    with transaction(con):
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
                COALESCE(s.search_next_at, s.search_last_at, t.created_at),
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


def _is_removed_tentative_asset(asset, match_threshold: float) -> bool:
    score = asset["match_confidence"]
    return score is not None and float(score) < match_threshold


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
    return con.execute(
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


def _mark_spotify_candidate_conflict(
    con,
    *,
    playlist_id: str,
    track,
    candidate: SpotifyTrack,
    score: float,
    existing_asset,
    searched_at: str,
    search_attempts: int,
) -> None:
    upsert_spotify_asset(
        con,
        track_id=track["id"],
        playlist_id=playlist_id,
        spotify_track_uri=candidate.uri,
        spotify_track_id="",
        spotify_artist=candidate.artist,
        spotify_title=candidate.title,
        in_playlist=False,
        match_confidence=0.0,
        status="review",
        search_last_at=searched_at,
        search_attempts=search_attempts,
        search_next_at=None,
        update_search=True,
    )
    add_event(
        con,
        track["id"],
        "spotify_candidate_already_linked",
        "spotify_sync",
        {
            "spotify_track_id": candidate.track_id,
            "spotify_artist": candidate.artist,
            "spotify_title": candidate.title,
            "score": score,
            "existing_track_id": existing_asset["track_id"],
            "existing_asset_id": existing_asset["id"],
            "reason": "candidate Spotify track is already linked to another DB track",
        },
        dedupe_key=f"spotify_candidate_already_linked:{track['id']}:{candidate.track_id}:{existing_asset['id']}",
    )


def _validate_playlist_snapshot(snapshot: PlaylistSnapshot) -> None:
    if not snapshot.complete or not snapshot.snapshot_id:
        raise RuntimeError("Spotify playlist snapshot was incomplete")
    if snapshot.tracks and not any(track.track_id for track in snapshot.tracks):
        raise RuntimeError("Spotify playlist snapshot had no usable track identities")


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
    if not apply:
        summary.linked = len(snapshot.tracks)
        return summary
    with connect(config) as con:
        with transaction(con):
            for item in snapshot.tracks:
                existing_asset = con.execute(
                    "SELECT * FROM spotify_assets WHERE playlist_id = ? AND spotify_track_id = ?",
                    (snapshot.playlist_id, item.track_id),
                ).fetchone()
                if existing_asset:
                    track = con.execute("SELECT * FROM tracks WHERE id = ?", (existing_asset["track_id"],)).fetchone()
                else:
                    track = ensure_track(con, artist=item.artist, title=item.title, status="wanted")
                if track["status"] == "excluded":
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
                add_event(
                    con,
                    track["id"],
                    "spotify_playlist_seen",
                    event_source,
                    {"spotify_track_id": item.track_id, "playlist_id": snapshot.playlist_id},
                    dedupe_key=f"spotify_playlist_seen:{snapshot.playlist_id}:{item.track_id}",
                )
                summary.linked += 1
            if establish_baseline:
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
    client = client or SpotipyClient(config)
    snapshot = client.playlist_snapshot(playlist_id)
    _validate_playlist_snapshot(snapshot)
    return _import_playlist_snapshot(config, snapshot, apply=apply, event_source="spotify_backfill", establish_baseline=True)


def scan_spotify_playlist(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None) -> SpotifySummary:
    summary = SpotifySummary()
    if not spotify_enabled(config):
        summary.skipped += 1
        return summary
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        summary.skipped += 1
        return summary
    client = client or SpotipyClient(config)
    try:
        snapshot = client.playlist_snapshot(playlist_id)
        _validate_playlist_snapshot(snapshot)
    except Exception as exc:
        if _is_rate_limited(exc):
            if apply:
                with connect(config) as con:
                    _remember_spotify_rate_limit(con, config, exc, event_source="spotify_scan")
            summary.rate_limited = True
            summary.skipped += 1
            return summary
        raise
    return _import_playlist_snapshot(config, snapshot, apply=apply, event_source="spotify_scan", establish_baseline=False)


def sync_spotify(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None) -> SpotifySummary:
    summary = SpotifySummary()
    if not spotify_enabled(config):
        summary.skipped += 1
        return summary
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        summary.skipped += 1
        return summary
    client = client or SpotipyClient(config)
    with connect(config) as con:
        if _spotify_cooldown_active(con):
            if apply:
                with transaction(con):
                    _log_spotify_cooldown_skip(con)
            summary.rate_limited = True
            summary.skipped += 1
            return summary
        tracks = _spotify_sync_candidates(con, playlist_id=playlist_id)
        suspected_local_delete_ids = _suspected_local_delete_track_ids(con)
        candidate_conflict_ids = _spotify_candidate_conflict_track_ids(con)
        playlist_assets = {
            row["track_id"]: row
            for row in con.execute("SELECT * FROM spotify_assets WHERE playlist_id = ?", (playlist_id,))
        }
        existing = {track_id for track_id, row in playlist_assets.items() if row["in_playlist"]}
        review_assets = {
            track_id: row
            for track_id, row in playlist_assets.items()
            if row["status"] == "review" and not row["in_playlist"]
        }
        threshold = config.float("HCR_SPOTIFY_MATCH_THRESHOLD")
        tentative_threshold = config.float("HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD")
        add_review_matches = config.bool("HCR_SPOTIFY_ADD_REVIEW_MATCHES")
        tentative_removed_event_ids = {
            row["track_id"]
            for row in con.execute(
                "SELECT DISTINCT track_id FROM events WHERE event_type = 'spotify_tentative_removed_by_user' AND track_id IS NOT NULL"
            )
        }
        removed_tentative_ids = {
            row["track_id"]
            for row in con.execute(
                "SELECT * FROM spotify_assets WHERE playlist_id = ? AND status = 'removed' AND in_playlist = 0",
                (playlist_id,),
            )
            if _is_removed_tentative_asset(row, threshold) or row["track_id"] in tentative_removed_event_ids
        }
        sync_limit = config.int("HCR_SPOTIFY_SYNC_LIMIT")
        searched = 0
        run_started_at = datetime.now(timezone.utc)
        for track in tracks:
            if track["id"] in suspected_local_delete_ids:
                summary.skipped += 1
                if apply:
                    with transaction(con):
                        add_event(
                            con,
                            track["id"],
                            "spotify_skipped_suspected_local_delete",
                            "spotify_sync",
                            {"reason": "local deletion is awaiting confirmation"},
                            dedupe_key=f"spotify_skipped_suspected_local_delete:{track['id']}",
                        )
                continue
            if track["id"] in existing:
                summary.skipped += 1
                continue
            if track["id"] in removed_tentative_ids:
                summary.review += 1
                continue
            if track["id"] in candidate_conflict_ids:
                summary.review += 1
                continue
            asset = playlist_assets.get(track["id"])
            if asset and _spotify_search_deferred(asset, run_started_at):
                if asset["status"] == "review":
                    summary.review += 1
                else:
                    summary.skipped += 1
                continue
            review_asset = review_assets.get(track["id"])
            if review_asset and not add_review_matches:
                summary.review += 1
                continue
            if looks_like_non_track(track["display_artist"], track["display_title"]):
                summary.review += 1
                if apply:
                    with transaction(con):
                        upsert_spotify_asset(
                            con,
                            track_id=track["id"],
                            playlist_id=playlist_id,
                            in_playlist=False,
                            match_confidence=0.0,
                            status="review",
                        )
                        add_event(
                            con,
                            track["id"],
                            "ambiguous_spotify_match",
                            "spotify_sync",
                            {
                                "reason": "source row looks like a mix, set, compilation, or non-track item",
                                "match_status": "review",
                            },
                            dedupe_key=f"spotify_non_track_source:{track['id']}",
                        )
                continue
            if sync_limit > 0 and searched >= sync_limit:
                summary.skipped += 1
                continue
            searched += 1
            try:
                candidates = client.search_track(track["display_artist"], track["display_title"])
            except Exception as exc:
                if _is_rate_limited(exc):
                    if apply:
                        _remember_spotify_rate_limit(con, config, exc)
                    summary.rate_limited = True
                    summary.skipped += 1
                    break
                raise
            best = None
            best_score = 0.0
            for candidate in candidates:
                score = _spotify_match_score(track, candidate)
                if score > best_score:
                    best = candidate
                    best_score = score
            searched_at_dt = datetime.now(timezone.utc)
            searched_at = _format_utc(searched_at_dt)
            failed_search_attempts = _spotify_search_attempts(asset) + 1
            confident_match = best is not None and best_score >= threshold
            tentative_match = best is not None and add_review_matches and best_score >= tentative_threshold
            if not confident_match and not tentative_match:
                next_search_at = _next_spotify_retry_at(searched_at_dt, failed_search_attempts)
                summary.review += 1
                if apply:
                    with transaction(con):
                        upsert_spotify_asset(
                            con,
                            track_id=track["id"],
                            playlist_id=playlist_id,
                            spotify_track_uri=best.uri if best else "",
                            spotify_track_id=best.track_id if best else "",
                            spotify_artist=best.artist if best else "",
                            spotify_title=best.title if best else "",
                            in_playlist=False,
                            match_confidence=best_score if best else 0.0,
                            status="review",
                            search_last_at=searched_at,
                            search_attempts=failed_search_attempts,
                            search_next_at=next_search_at,
                            update_search=True,
                        )
                        add_event(
                            con,
                            track["id"],
                            "ambiguous_spotify_match",
                            "spotify_sync",
                            {
                                "reason": "below tentative threshold or not found",
                                "score": best_score,
                                "spotify_track_id": best.track_id if best else "",
                                "spotify_artist": best.artist if best else "",
                                "spotify_title": best.title if best else "",
                                "match_threshold": threshold,
                                "tentative_threshold": tentative_threshold,
                                "add_review_matches": add_review_matches,
                                "match_status": "review",
                                "spotify_search_last_at": searched_at,
                                "spotify_search_attempts": failed_search_attempts,
                                "spotify_search_next_at": next_search_at,
                            },
                            dedupe_key=f"ambiguous_spotify_match:{track['id']}:{best.track_id if best else 'none'}:{best_score:.3f}",
                        )
                continue
            conflicting_asset = _spotify_candidate_used_by_other_track(
                con,
                playlist_id=playlist_id,
                track_id=track["id"],
                spotify_track_id=best.track_id,
            )
            if conflicting_asset:
                summary.review += 1
                if apply:
                    with transaction(con):
                        _mark_spotify_candidate_conflict(
                            con,
                            playlist_id=playlist_id,
                            track=track,
                            candidate=best,
                            score=best_score,
                            existing_asset=conflicting_asset,
                            searched_at=searched_at,
                            search_attempts=failed_search_attempts,
                        )
                continue
            if not apply:
                summary.added += 1
                if tentative_match and not confident_match:
                    summary.tentative_added += 1
                continue
            with transaction(con):
                current = con.execute("SELECT status FROM tracks WHERE id = ?", (track["id"],)).fetchone()
                if not current or current["status"] == "excluded":
                    summary.skipped += 1
                    continue
            try:
                client.add_tracks(playlist_id, [best.uri])
            except Exception as exc:
                if _is_rate_limited(exc):
                    if apply:
                        _remember_spotify_rate_limit(con, config, exc)
                    summary.rate_limited = True
                    summary.skipped += 1
                    break
                raise
            with transaction(con):
                current = con.execute("SELECT status FROM tracks WHERE id = ?", (track["id"],)).fetchone()
                if not current or current["status"] == "excluded":
                    client.remove_tracks(playlist_id, [best.uri])
                    summary.skipped += 1
                    continue
                upsert_spotify_asset(
                    con,
                    track_id=track["id"],
                    playlist_id=playlist_id,
                    spotify_track_uri=best.uri,
                    spotify_track_id=best.track_id,
                    spotify_artist=best.artist,
                    spotify_title=best.title,
                    in_playlist=True,
                    match_confidence=best_score,
                    status="added" if confident_match else "review",
                    added_at=now_utc(),
                    search_last_at=searched_at,
                    search_attempts=0,
                    search_next_at=None,
                    update_search=True,
                )
                event_type = "spotify_added" if confident_match else "spotify_tentatively_added"
                add_event(
                    con,
                    track["id"],
                    event_type,
                    "spotify_sync",
                    {
                        "spotify_track_id": best.track_id,
                        "spotify_artist": best.artist,
                        "spotify_title": best.title,
                        "score": best_score,
                        "match_threshold": threshold,
                        "tentative_threshold": tentative_threshold,
                        "match_status": "added" if confident_match else "tentative_review",
                        "spotify_search_last_at": searched_at,
                    },
                    dedupe_key=f"{event_type}:{track['id']}:{best.track_id}",
                )
                summary.added += 1
                if tentative_match and not confident_match:
                    summary.tentative_added += 1
    return summary
