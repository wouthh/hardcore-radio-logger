"""Spotify backfill and sync using Spotipy."""

from __future__ import annotations

from dataclasses import dataclass
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
from .identity import match_confidence


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
            )
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
                fields="items(track(id,uri,name,duration_ms,artists(name))),next,total",
            )
            items = page.get("items") or []
            for item in items:
                track = item.get("track") or {}
                artists = track.get("artists") or []
                tracks.append(
                    SpotifyTrack(
                        uri=str(track.get("uri") or ""),
                        track_id=str(track.get("id") or ""),
                        artist=", ".join(str(artist.get("name") or "") for artist in artists),
                        title=str(track.get("name") or ""),
                        duration_ms=track.get("duration_ms"),
                    )
                )
            if not page.get("next"):
                break
            offset += len(items)
            if not items:
                complete = False
                break
        return PlaylistSnapshot(playlist_id=playlist_id, snapshot_id=snapshot_id, tracks=tracks, complete=complete)

    def search_track(self, artist: str, title: str) -> list[SpotifyTrack]:
        query = f"artist:{artist} track:{title}" if artist else title
        result = self.sp.search(q=query, type="track", limit=10)
        tracks = []
        for item in (result.get("tracks") or {}).get("items") or []:
            artists = item.get("artists") or []
            tracks.append(
                SpotifyTrack(
                    uri=str(item.get("uri") or ""),
                    track_id=str(item.get("id") or ""),
                    artist=", ".join(str(artist.get("name") or "") for artist in artists),
                    title=str(item.get("name") or ""),
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


@dataclass
class SpotifySummary:
    seen: int = 0
    linked: int = 0
    added: int = 0
    review: int = 0
    skipped: int = 0


def spotify_auth(config: Config, client: SpotifyClientProtocol | None = None) -> str:
    client = client or SpotipyClient(config)
    return client.auth_check()


def backfill_spotify(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None) -> SpotifySummary:
    summary = SpotifySummary()
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        raise RuntimeError("HCR_SPOTIFY_PLAYLIST_ID is required")
    client = client or SpotipyClient(config)
    snapshot = client.playlist_snapshot(playlist_id)
    if not snapshot.complete or not snapshot.snapshot_id:
        raise RuntimeError("Spotify playlist snapshot was incomplete")
    summary.seen = len(snapshot.tracks)
    if not apply:
        summary.linked = len(snapshot.tracks)
        return summary
    with connect(config) as con:
        with transaction(con):
            for item in snapshot.tracks:
                track = ensure_track(con, artist=item.artist, title=item.title, status="wanted")
                upsert_spotify_asset(
                    con,
                    track_id=track["id"],
                    playlist_id=playlist_id,
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
                    "spotify_backfill",
                    {"spotify_track_id": item.track_id, "playlist_id": playlist_id},
                    dedupe_key=f"spotify_playlist_seen:{playlist_id}:{item.track_id}",
                )
                summary.linked += 1
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_snapshot_id", snapshot.snapshot_id)
            set_state(con, "last_spotify_playlist_count", str(len(snapshot.tracks)))
            set_state(con, "last_spotify_scan_at", now_utc())
    return summary


def sync_spotify(config: Config, *, apply: bool, client: SpotifyClientProtocol | None = None) -> SpotifySummary:
    summary = SpotifySummary()
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    if not playlist_id:
        summary.skipped += 1
        return summary
    client = client or SpotipyClient(config)
    with connect(config) as con:
        tracks = wanted_tracks(con)
        existing = {
            row["track_id"]
            for row in con.execute(
                "SELECT track_id FROM spotify_assets WHERE playlist_id = ? AND in_playlist = 1",
                (playlist_id,),
            )
        }
        for track in tracks:
            if track["id"] in existing:
                summary.skipped += 1
                continue
            candidates = client.search_track(track["display_artist"], track["display_title"])
            best = None
            best_score = 0.0
            for candidate in candidates:
                score = match_confidence(
                    artist=track["display_artist"],
                    title=track["display_title"],
                    candidate_artist=candidate.artist,
                    candidate_title=candidate.title,
                )
                if score > best_score:
                    best = candidate
                    best_score = score
            threshold = config.float("HCR_SPOTIFY_MATCH_THRESHOLD")
            if best is None or best_score < threshold:
                summary.review += 1
                if apply:
                    with transaction(con):
                        upsert_spotify_asset(
                            con,
                            track_id=track["id"],
                            playlist_id=playlist_id,
                            in_playlist=False,
                            match_confidence=best_score or None,
                            status="review",
                        )
                        add_event(con, track["id"], "ambiguous_spotify_match", "spotify_sync", {"score": best_score})
                continue
            if not apply:
                summary.added += 1
                continue
            with transaction(con):
                current = con.execute("SELECT status FROM tracks WHERE id = ?", (track["id"],)).fetchone()
                if not current or current["status"] == "excluded":
                    summary.skipped += 1
                    continue
            client.add_tracks(playlist_id, [best.uri])
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
                    status="added",
                    added_at=now_utc(),
                )
                add_event(con, track["id"], "spotify_added", "spotify_sync", {"spotify_track_id": best.track_id})
                summary.added += 1
    return summary
