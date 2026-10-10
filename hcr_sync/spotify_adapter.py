"""Budgeted Spotify Web API transport and complete playlist reads."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config


class BudgetDeferred(RuntimeError):
    def __init__(self, required: int | None = None):
        self.required = required
        super().__init__("Spotify request budget deferred work")


class SnapshotOversized(BudgetDeferred):
    def __init__(self, required: int):
        super().__init__(required)


@dataclass
class RequestBudget:
    limit: int = 20
    used: int = 0
    reserve: int = 0

    def __post_init__(self):
        if self.limit < 9 or self.used < 0 or self.used > self.limit or self.reserve < 0:
            raise ValueError("Spotify request budget must be at least 9 with nonnegative usage and reserve")

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self) -> None:
        if self.remaining <= self.reserve:
            raise BudgetDeferred(self.used + self.reserve + 1)
        self.used += 1


class _BudgetedSession(requests.Session):
    def __init__(self, budget: RequestBudget):
        super().__init__()
        self.budget = budget
        retry = Retry(total=0, connect=0, read=0, redirect=0, status=0,
                      raise_on_status=False, respect_retry_after_header=False)
        self.mount("http://", HTTPAdapter(max_retries=retry))
        self.mount("https://", HTTPAdapter(max_retries=retry))

    def send(self, request, **kwargs):
        self.budget.consume()
        # Redirects would make a reserved operation use an unbounded call count.
        kwargs["allow_redirects"] = False
        response = super().send(request, **kwargs)
        if 300 <= response.status_code < 400:
            response.close()
            error = RuntimeError("Spotify Web API redirects are disabled")
            error.http_status = response.status_code
            error.headers = response.headers
            error.reason = "redirect_refused"
            raise error
        return response


@dataclass(frozen=True)
class SpotifyTrack:
    uri: str
    track_id: str
    artist: str
    title: str
    duration_ms: int | None = None
    artist_ids: tuple[str, ...] = ()
    album: str = ""
    isrc: str = ""
    metadata_ambiguous: bool = False
    artist_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaylistSnapshot:
    playlist_id: str
    snapshot_id: str
    tracks: list[SpotifyTrack]
    complete: bool = True
    identified: bool = True
    total: int | None = None


def _spotify_track_from_playlist_item(item: dict) -> SpotifyTrack | None:
    if not isinstance(item, dict):
        return None
    track = item.get("track") or item.get("item")
    if not isinstance(track, dict) or track.get("type") not in {None, "track"}:
        return None
    track_id = str(track.get("id") or "")
    uri = str(track.get("uri") or "")
    if not track_id and uri.startswith("spotify:track:"):
        track_id = uri.removeprefix("spotify:track:")
    if track_id and not uri:
        uri = f"spotify:track:{track_id}"
    if not track_id or not uri:
        return None
    artists = [artist for artist in track.get("artists") or [] if isinstance(artist, dict)]
    title = str(track.get("name") or "")
    artist = ", ".join(str(item.get("name") or "") for item in artists)
    return SpotifyTrack(
        uri=uri,
        track_id=track_id,
        artist=artist,
        title=title,
        duration_ms=track.get("duration_ms"),
        artist_ids=tuple(str(item["id"]) for item in artists if item.get("id")),
        artist_names=tuple(str(item.get("name") or "") for item in artists),
        album=str((track.get("album") or {}).get("name") or ""),
        isrc=str((track.get("external_ids") or {}).get("isrc") or ""),
        metadata_ambiguous=not title or not artist,
    )


def _snapshot_metadata(meta) -> dict:
    if not isinstance(meta, dict):
        raise RuntimeError("Spotify playlist metadata was incomplete")
    snapshot_id = meta.get("snapshot_id")
    items = meta.get("items") or meta.get("tracks") or {}
    total = meta.get("total", items.get("total") if isinstance(items, dict) else None)
    if not isinstance(snapshot_id, str) or not snapshot_id or type(total) is not int or total < 0:
        raise RuntimeError("Spotify playlist metadata was incomplete")
    return {"snapshot_id": snapshot_id, "total": total}


class SpotipyClient:
    def __init__(self, config: Config, budget: RequestBudget | None = None):
        self.config = config
        self.budget = budget if budget is not None else RequestBudget(int(config.get("HCR_SPOTIFY_REQUEST_BUDGET") or 20))
        if config.int("HCR_SPOTIFY_REQUEST_RETRIES") or config.int("HCR_SPOTIFY_STATUS_RETRIES"):
            raise ValueError("Budgeted Spotify requests require request and status retries to be zero")
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth
        except ImportError as exc:
            raise RuntimeError("Spotipy is not installed; install requirements.txt first") from exc
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=config.get("HCR_SPOTIFY_CLIENT_ID"),
                client_secret=config.get("HCR_SPOTIFY_CLIENT_SECRET"),
                redirect_uri=config.get("HCR_SPOTIFY_REDIRECT_URI"),
                scope=config.spotify_scopes,
                cache_path=str(config.path("HCR_SPOTIFY_TOKEN_CACHE")),
                open_browser=True,
            ),
            requests_session=_BudgetedSession(self.budget),
            requests_timeout=config.int("HCR_SPOTIFY_REQUEST_TIMEOUT"),
            retries=0,
            status_retries=0,
        )

    def auth_check(self) -> str:
        user = self.sp.current_user()
        return str(user.get("id") or user.get("display_name") or "authenticated")

    def playlist_metadata(self, playlist_id: str) -> dict:
        return _snapshot_metadata(self.sp.playlist(playlist_id, fields="snapshot_id,items(total)"))

    def playlist_snapshot(self, playlist_id: str, *, metadata: dict | None = None) -> PlaylistSnapshot:
        meta = self.playlist_metadata(playlist_id) if metadata is None else _snapshot_metadata(metadata)
        total = meta["total"]
        required = 2 + max(1, ceil(total / 50))
        if required - 1 > self.budget.remaining - self.budget.reserve:
            raise SnapshotOversized(required)
        tracks: list[SpotifyTrack] = []
        identified = True
        offset = 0
        while True:
            page = self.sp.playlist_items(
                playlist_id,
                offset=offset,
                limit=50,
                fields="items(track(id,uri,name,duration_ms,artists(id,name),album(name),external_ids(isrc),type),item(id,uri,name,duration_ms,artists(id,name),album(name),external_ids(isrc),type)),next,total,offset",
            )
            if not isinstance(page, dict):
                raise RuntimeError("Spotify playlist pagination was incomplete")
            items = page.get("items")
            if (not isinstance(items, list) or type(page.get("total")) is not int
                    or page["total"] != total or type(page.get("offset")) is not int
                    or page["offset"] != offset or len(items) != min(50, total - offset)
                    or "next" not in page
                    or page["next"] is not None and not isinstance(page["next"], str)):
                raise RuntimeError("Spotify playlist pagination was incomplete")
            for item in items:
                track = _spotify_track_from_playlist_item(item)
                if track is not None:
                    tracks.append(track)
                    identified = identified and bool(track.track_id and track.uri)
                else:
                    raw = item.get("track") or item.get("item") if isinstance(item, dict) else None
                    if not isinstance(raw, dict) or raw.get("type") not in {"episode"}:
                        identified = False
            offset += len(items)
            if bool(page["next"]) != (offset < total):
                raise RuntimeError("Spotify playlist pagination was incomplete")
            if offset >= total:
                break
        final_meta = self.playlist_metadata(playlist_id)
        if final_meta != meta:
            raise RuntimeError("Spotify playlist changed during pagination")
        return PlaylistSnapshot(playlist_id, meta["snapshot_id"], tracks, identified=identified, total=total)

    @staticmethod
    def search_queries(artist: str, title: str) -> list[str]:
        from .spotify_sync import _spotify_search_queries
        return _spotify_search_queries(artist, title)

    def search_query(self, query: str) -> list[SpotifyTrack]:
        result = self.sp.search(q=query, type="track", limit=10)
        if not isinstance(result, dict) or not isinstance(result.get("tracks"), dict):
            raise RuntimeError("Spotify search response was incomplete")
        items = result["tracks"].get("items")
        if not isinstance(items, list):
            raise RuntimeError("Spotify search response was incomplete")
        tracks = []
        seen_ids: set[str] = set()
        for item in items:
            track = _spotify_track_from_playlist_item({"track": item})
            if track is not None and track.track_id and track.uri and track.title and track.track_id not in seen_ids:
                seen_ids.add(track.track_id)
                tracks.append(track)
        return tracks

    def search_track(self, artist: str, title: str) -> list[SpotifyTrack]:
        tracks: dict[str, SpotifyTrack] = {}
        for query in self.search_queries(artist, title):
            for track in self.search_query(query):
                tracks.setdefault(track.track_id, track)
        return list(tracks.values())

    def add_tracks(self, playlist_id: str, uris: list[str]) -> dict:
        if not uris:
            return {}
        playlist_id = self.sp._get_id("playlist", playlist_id)
        acknowledgement = self.sp._post(f"playlists/{playlist_id}/items", payload={
            "uris": [self.sp._get_uri("track", uri) for uri in uris],
        })
        return acknowledgement if isinstance(acknowledgement, dict) else {}

    def remove_tracks(self, playlist_id: str, uris: list[str]) -> dict:
        if not uris:
            return {}
        acknowledgement = self.sp.playlist_remove_all_occurrences_of_items(playlist_id, uris)
        return acknowledgement if isinstance(acknowledgement, dict) else {}
