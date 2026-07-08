"""Hardcore Radio polling producer."""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config
from .db import now_utc
from .identity import compact_text, display_from_parts, fingerprint, normalize_for_match

MAX_STATUS_BYTES = 1_000_000


class PollError(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def status_url_for_stream(stream_url: str) -> str:
    parsed = urllib.parse.urlparse(stream_url)
    if not parsed.scheme or not parsed.netloc:
        raise PollError(f"invalid stream URL: {stream_url}")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/status-json.xsl", "", "", ""))


def fetch_status(status_url: str, timeout: float = 15.0, retries: int = 2) -> dict[str, Any]:
    request = urllib.request.Request(
        status_url,
        headers={"Accept": "application/json", "User-Agent": "hcr-sync/0.1"},
    )
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_STATUS_BYTES + 1)
            break
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2.0, 0.25 * (attempt + 1)))
                continue
            raise PollError(f"could not fetch Icecast status: {exc}") from exc
    else:
        raise PollError(f"could not fetch Icecast status: {last_error}")
    if len(raw) > MAX_STATUS_BYTES:
        raise PollError("Icecast status response was too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise PollError("Icecast status was not a JSON object")
    return payload


def _sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source = payload.get("icestats", {}).get("source") if isinstance(payload.get("icestats"), dict) else None
    if isinstance(source, dict):
        return [source]
    if isinstance(source, list):
        return [item for item in source if isinstance(item, dict)]
    return []


def _url_path(value: str) -> str:
    return urllib.parse.urlparse(value).path.rstrip("/")


def _source_score(source: dict[str, Any], stream_url: str) -> int:
    mount_path = _url_path(stream_url)
    listen_url = compact_text(source.get("listenurl"))
    listen_path = _url_path(listen_url)
    server_type = compact_text(source.get("server_type")).casefold()
    subtype = compact_text(source.get("subtype")).casefold()
    score = 0
    if mount_path and listen_path == mount_path:
        score += 100
    if mount_path and mount_path in listen_url:
        score += 40
    if "ogg" in server_type or "vorbis" in subtype:
        score += 20
    if compact_text(source.get("title")):
        score += 5
    return score


def current_track(payload: dict[str, Any], stream_url: str) -> str:
    entries = _sources(payload)
    if not entries:
        raise PollError("Icecast status listed no active sources")
    source = sorted(entries, key=lambda item: _source_score(item, stream_url), reverse=True)[0]
    artist = compact_text(source.get("artist"))
    title = compact_text(source.get("title"))
    if artist and title:
        normalized_artist = normalize_for_match(artist)
        normalized_title = normalize_for_match(title)
        if normalized_title == normalized_artist or normalized_title.startswith(f"{normalized_artist} "):
            return title
        return display_from_parts(artist, title)
    if title:
        return title
    if artist:
        return artist
    raise PollError("selected Icecast source has no artist/title")


def _seen_fingerprints(seen_path: Path) -> set[str]:
    if not seen_path.exists():
        return set()
    seen: set[str] = set()
    for line in seen_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            seen.add(fingerprint(line))
            continue
        if isinstance(record, dict):
            seen.add(str(record.get("fingerprint") or fingerprint(str(record.get("track") or ""))))
    return seen


def poll_radio(config: Config, *, apply: bool) -> tuple[bool, str]:
    stream_url = config.get("HCR_STREAM_URL")
    status_url = config.get("HCR_STATUS_URL") or status_url_for_stream(stream_url)
    payload = fetch_status(status_url)
    track = current_track(payload, stream_url)
    track_fingerprint = fingerprint(track)
    observed_at = now_utc()
    if not apply:
        return True, track
    with file_lock(config.logger_lock_path):
        seen = _seen_fingerprints(config.seen_tracks_path)
        if track_fingerprint in seen:
            return False, track
        config.played_tracks_path.parent.mkdir(parents=True, exist_ok=True)
        config.seen_tracks_path.parent.mkdir(parents=True, exist_ok=True)
        with config.played_tracks_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{observed_at}\t{track}\n")
        record = {
            "first_seen_at": observed_at,
            "fingerprint": track_fingerprint,
            "status_url": status_url,
            "stream_url": stream_url,
            "track": track,
        }
        with config.seen_tracks_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return True, track
