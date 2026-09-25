"""Hardcore Radio polling producer."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import html.parser
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .db import add_event, connect, now_utc, transaction
from .identity import compact_text, display_from_parts, fingerprint, normalize_for_match

MAX_STATUS_BYTES = 1_000_000
PLAYER_PAGE_URL = "https://www.hardcoreradio.nl/player/"
PLAYER_PAGE_TIMEOUT = 15.0
PLAYER_PAGE_RETRIES = 0
UNCACHED_QUERY_PARAMETER = "_hcr_poll"
LOGGER = logging.getLogger(__name__)


class PollError(Exception):
    pass


class PollSourceUnavailable(PollError):
    """A radio metadata source returned unusable data or could not be reached."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class PollSourcesUnavailable(PollError):
    """No configured source returned usable radio metadata."""

    def __init__(self, icecast_reason: str, player_page_reason: str):
        self.icecast_reason = icecast_reason
        self.player_page_reason = player_page_reason
        super().__init__(f"icecast={icecast_reason}; player_page={player_page_reason}")


@dataclass(frozen=True)
class _TrackMetadata:
    track: str
    source: str
    source_url: str


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


def _uncached_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != UNCACHED_QUERY_PARAMETER
    ]
    query.append((UNCACHED_QUERY_PARAMETER, uuid.uuid4().hex))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query), fragment=""))


def _transport_reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError) and exc.errno == errno.ECONNREFUSED:
        return "connection_refused"
    if isinstance(exc, urllib.error.HTTPError):
        return "not_modified" if exc.code == 304 else f"http_status_{exc.code}"
    if isinstance(exc, http.client.HTTPException):
        return "http_error"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return "timeout"
        if isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
            return "connection_refused"
        return "connection_error"
    if isinstance(exc, (OSError, ConnectionError)):
        return "connection_error"
    return "request_error"


def _fetch_response(
    url: str,
    *,
    accept: str,
    timeout: float,
    retries: int,
) -> tuple[bytes, str]:
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            _uncached_url(url),
            headers={
                "Accept": accept,
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
                "User-Agent": "hcr-sync/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status != 200:
                    raise PollSourceUnavailable(f"http_status_{status}")
                raw = response.read(MAX_STATUS_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
            if len(raw) > MAX_STATUS_BYTES:
                raise PollSourceUnavailable("response_too_large")
            return raw, content_type
        except PollSourceUnavailable:
            # Invalid status and oversized replies are unusable metadata, not
            # transport failures subject to the Icecast retry policy.
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = PollSourceUnavailable(_transport_reason(exc))
        if last_error is not None:
            if attempt < retries:
                time.sleep(min(2.0, 0.25 * (attempt + 1)))
                continue
            raise last_error
    raise PollSourceUnavailable(_transport_reason(last_error or OSError()))


def fetch_status(status_url: str, timeout: float = 15.0, retries: int = 2) -> dict[str, Any]:
    raw, _content_type = _fetch_response(
        status_url,
        accept="application/json",
        timeout=timeout,
        retries=retries,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PollSourceUnavailable("invalid_json") from None
    if not isinstance(payload, dict):
        raise PollSourceUnavailable("invalid_payload")
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
        raise PollSourceUnavailable("missing_track_metadata")
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
    raise PollSourceUnavailable("missing_track_metadata")


class _PlayerPageParser(html.parser.HTMLParser):
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict[str, Any] = {"tag": "", "classes": set(), "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        node = {
            "tag": tag.casefold(),
            "classes": set((values.get("class") or "").split()),
            "children": [],
            "closed": False,
        }
        self.stack[-1]["children"].append(node)
        if node["tag"] not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == normalized:
                self.stack[index]["closed"] = True
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1]["children"].append(data)


def _descendants(node: dict[str, Any]):
    pending = [child for child in reversed(node["children"]) if isinstance(child, dict)]
    while pending:
        child = pending.pop()
        yield child
        pending.extend(grandchild for grandchild in reversed(child["children"]) if isinstance(grandchild, dict))


def _node_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    pending = list(reversed(node["children"]))
    while pending:
        child = pending.pop()
        if isinstance(child, dict):
            if child["tag"] not in {"script", "style"}:
                pending.extend(reversed(child["children"]))
        else:
            parts.append(child)
    return compact_text(" ".join(parts))


def track_from_player_page(page: str) -> str:
    parser = _PlayerPageParser()
    try:
        parser.feed(page)
        parser.close()
    except (AssertionError, ValueError):
        raise PollSourceUnavailable("invalid_html") from None

    containers = [
        node
        for node in _descendants(parser.root)
        if node["tag"] == "div" and "track" in node["classes"]
    ]
    if len(containers) != 1:
        raise PollSourceUnavailable("ambiguous_track_markup" if containers else "missing_track_markup")
    if not containers[0]["closed"]:
        raise PollSourceUnavailable("invalid_html")

    fields = {"artist": [], "title": []}
    field_nodes = {"artist": [], "title": []}
    for node in _descendants(containers[0]):
        for field in fields:
            if node["tag"] == "span" and field in node["classes"]:
                fields[field].append(_node_text(node))
                field_nodes[field].append(node)
    if len(fields["artist"]) > 1 or len(fields["title"]) > 1:
        raise PollSourceUnavailable("ambiguous_track_fields")
    if not fields["artist"] or not fields["title"]:
        raise PollSourceUnavailable("missing_track_fields")
    if any(not nodes[0]["closed"] for nodes in field_nodes.values()):
        raise PollSourceUnavailable("invalid_html")
    artist, title = fields["artist"][0], fields["title"][0]
    if not artist or not title:
        raise PollSourceUnavailable("missing_track_fields")
    return display_from_parts(artist, title)


def _canonical_source_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urllib.parse.urlunsplit((parts.scheme.casefold(), netloc, parts.path, "", ""))


def fetch_player_track(player_url: str = PLAYER_PAGE_URL) -> str:
    raw, content_type = _fetch_response(
        player_url,
        accept="text/html",
        timeout=PLAYER_PAGE_TIMEOUT,
        retries=PLAYER_PAGE_RETRIES,
    )
    if content_type.split(";", 1)[0].strip().casefold() != "text/html":
        raise PollSourceUnavailable("unexpected_content_type")
    try:
        page = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PollSourceUnavailable("invalid_encoding") from None
    return track_from_player_page(page)


def _fetch_track_metadata(config: Config) -> _TrackMetadata:
    stream_url = config.get("HCR_STREAM_URL")
    status_url = config.get("HCR_STATUS_URL") or status_url_for_stream(stream_url)
    try:
        track = current_track(fetch_status(status_url), stream_url)
    except PollSourceUnavailable as icecast_error:
        try:
            track = fetch_player_track()
        except PollSourceUnavailable as player_page_error:
            raise PollSourcesUnavailable(icecast_error.reason, player_page_error.reason) from None
        LOGGER.warning(
            "Icecast metadata unavailable (%s); using the player webpage fallback",
            icecast_error.reason,
        )
        return _TrackMetadata(track, "player_webpage", _canonical_source_url(PLAYER_PAGE_URL))
    return _TrackMetadata(track, "icecast", _canonical_source_url(status_url))


def record_poll_unavailable(config: Config, failure: PollSourcesUnavailable, *, apply: bool) -> None:
    if not apply or not config.bool("HCR_AUDIT_VERBOSE"):
        return
    with connect(config) as con:
        with transaction(con):
            add_event(
                con,
                None,
                "radio_poll_unavailable",
                "poll_radio",
                {
                    "observed_at": now_utc(),
                    "icecast_reason": failure.icecast_reason,
                    "player_page_reason": failure.player_page_reason,
                },
            )


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
    metadata = _fetch_track_metadata(config)
    track = metadata.track
    track_fingerprint = fingerprint(track)
    observed_at = now_utc()
    if not apply:
        return True, track
    with file_lock(config.logger_lock_path):
        seen = _seen_fingerprints(config.seen_tracks_path)
        if track_fingerprint in seen:
            if config.bool("HCR_AUDIT_VERBOSE"):
                with connect(config) as con:
                    with transaction(con):
                        add_event(
                            con,
                            None,
                            "radio_poll_seen",
                            "poll_radio",
                            {
                                "track": track,
                                "fingerprint": track_fingerprint,
                                "duplicate": True,
                                "changed": False,
                                "observed_at": observed_at,
                                "status_url": status_url,
                                "stream_url": stream_url,
                                "metadata_source": metadata.source,
                                "metadata_source_url": metadata.source_url,
                            },
                        )
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
            "metadata_source": metadata.source,
            "metadata_source_url": metadata.source_url,
        }
        with config.seen_tracks_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    if config.bool("HCR_AUDIT_VERBOSE"):
        with connect(config) as con:
            with transaction(con):
                add_event(
                    con,
                    None,
                    "radio_poll_seen",
                    "poll_radio",
                    {
                        "track": track,
                        "fingerprint": track_fingerprint,
                        "duplicate": False,
                        "changed": True,
                        "observed_at": observed_at,
                        "status_url": status_url,
                        "stream_url": stream_url,
                        "metadata_source": metadata.source,
                        "metadata_source_url": metadata.source_url,
                    },
                )
    return True, track
