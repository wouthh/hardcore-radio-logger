"""Spotify backfill and sync using Spotipy."""

from __future__ import annotations

import re
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
    wanted_tracks,
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
    return status == 429 or "Retry-After" in headers


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _retry_after_until(config: Config, exc: Exception) -> str:
    headers = getattr(exc, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after") or ""
    if not retry_after:
        match = re.search(r"Retry(?:\s+will\s+occur)?\s+after:\s*(\d+)", str(exc), re.I)
        if match:
            retry_after = match.group(1)
    try:
        seconds = max(1, int(float(retry_after)))
    except (TypeError, ValueError):
        seconds = config.int("HCR_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS")
    return _format_utc(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def _spotify_cooldown_active(con) -> bool:
    until = _parse_utc(get_state(con, "spotify_rate_limited_until", ""))
    return bool(until and until > datetime.now(timezone.utc))


def _remember_spotify_rate_limit(con, config: Config, exc: Exception) -> None:
    with transaction(con):
        set_state(con, "spotify_rate_limited_until", _retry_after_until(config, exc))


def _is_terminal_review_asset(asset, tentative_threshold: float) -> bool:
    score = asset["match_confidence"]
    return score is not None and float(score) < tentative_threshold


def _is_removed_tentative_asset(asset, match_threshold: float) -> bool:
    score = asset["match_confidence"]
    return score is not None and float(score) < match_threshold


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
            summary.rate_limited = True
            summary.skipped += 1
            return summary
        tracks = wanted_tracks(con)
        existing = {
            row["track_id"]
            for row in con.execute(
                "SELECT track_id FROM spotify_assets WHERE playlist_id = ? AND in_playlist = 1",
                (playlist_id,),
            )
        }
        review_assets = {
            row["track_id"]: row
            for row in con.execute(
                "SELECT * FROM spotify_assets WHERE playlist_id = ? AND status = 'review' AND in_playlist = 0",
                (playlist_id,),
            )
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
        for track in tracks:
            if track["id"] in existing:
                summary.skipped += 1
                continue
            if track["id"] in removed_tentative_ids:
                summary.review += 1
                continue
            review_asset = review_assets.get(track["id"])
            if review_asset and (not add_review_matches or _is_terminal_review_asset(review_asset, tentative_threshold)):
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
                            {"reason": "source row looks like a mix, set, compilation, or non-track item"},
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
            confident_match = best is not None and best_score >= threshold
            tentative_match = best is not None and add_review_matches and best_score >= tentative_threshold
            if not confident_match and not tentative_match:
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
                        )
                        add_event(
                            con,
                            track["id"],
                            "ambiguous_spotify_match",
                            "spotify_sync",
                            {"score": best_score, "spotify_track_id": best.track_id if best else ""},
                            dedupe_key=f"ambiguous_spotify_match:{track['id']}:{best.track_id if best else 'none'}:{best_score:.3f}",
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
                )
                event_type = "spotify_added" if confident_match else "spotify_tentatively_added"
                add_event(
                    con,
                    track["id"],
                    event_type,
                    "spotify_sync",
                    {"spotify_track_id": best.track_id, "score": best_score},
                    dedupe_key=f"{event_type}:{track['id']}:{best.track_id}",
                )
                summary.added += 1
                if tentative_match and not confident_match:
                    summary.tentative_added += 1
    return summary
