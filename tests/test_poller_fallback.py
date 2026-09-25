import errno
import io
import json
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, ensure_track, init_db, mark_excluded, transaction
from hcr_sync.logger_importer import import_logger
from hcr_sync.poller import (
    MAX_STATUS_BYTES,
    PLAYER_PAGE_URL,
    PollSourceUnavailable,
    PollSourcesUnavailable,
    _fetch_track_metadata,
    fetch_player_track,
    fetch_status,
    poll_radio,
    record_poll_unavailable,
    track_from_player_page,
)


TRACK_HTML = """<!doctype html><html><body>
<div class="track"><span class="artist">Artist &amp; Co</span>
<span class="title">  Synthetic   Song </span></div>
</body></html>"""


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "application/json"):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def make_config(tmp_path: Path, **overrides: str) -> Config:
    values = dict(DEFAULTS)
    values.update(
        {
            "HCR_DB_PATH": str(tmp_path / "hcr_music.db"),
            "HCR_MUSIC_DIR": str(tmp_path / "music"),
            "HCR_TRASH_DIR": str(tmp_path / "music" / ".hcr-trash"),
            "HCR_SEEN_TRACKS_JSONL": str(tmp_path / "seen-tracks.jsonl"),
            "HCR_PLAYED_TRACKS_TSV": str(tmp_path / "played-tracks.tsv"),
            "HCR_SPOTIFY_TOKEN_CACHE": str(tmp_path / "token-cache"),
            "HCR_STREAM_URL": "https://stream.example.test:9000/live.ogg",
            "HCR_STATUS_URL": "https://status.example.test/status-json.xsl",
            "HCR_LOGGER_LOCK_FILE": str(tmp_path / ".logger.lock"),
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


def icecast_response(artist: str = "Artist & Co", title: str = "Synthetic Song") -> FakeResponse:
    body = json.dumps(
        {"icestats": {"source": {"artist": artist, "title": title, "listenurl": "https://stream.example.test:9000/live.ogg"}}}
    ).encode()
    return FakeResponse(body)


def player_response(body: bytes | str = TRACK_HTML.encode(), *, content_type: str = "text/html; charset=utf-8") -> FakeResponse:
    if isinstance(body, str):
        body = body.encode()
    return FakeResponse(body, content_type=content_type)


def refused() -> urllib.error.URLError:
    return urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refusal"))


def test_icecast_success_does_not_request_player_page(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return icecast_response()

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)

    metadata = _fetch_track_metadata(config)

    assert metadata.track == "Artist & Co - Synthetic Song"
    assert metadata.source == "icecast"
    assert metadata.source_url == "https://status.example.test/status-json.xsl"
    assert len(requests) == 1
    assert urllib.parse.urlsplit(requests[0][0].full_url).hostname == "status.example.test"


def test_player_fallback_is_used_after_icecast_refusal_then_next_run_recovers(monkeypatch, tmp_path):
    config = make_config(tmp_path, HCR_STATUS_URL="https://status.example.test/status-json.xsl?token=synthetic")
    requests = []
    outcomes = [refused(), refused(), refused(), player_response(), icecast_response()]

    def open_request(request, timeout):
        requests.append((request, timeout))
        result = outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)

    fallback = _fetch_track_metadata(config)
    recovered = _fetch_track_metadata(config)

    assert fallback.source == "player_webpage"
    assert fallback.source_url == PLAYER_PAGE_URL
    assert recovered.source == "icecast"
    assert recovered.track == fallback.track
    assert [urllib.parse.urlsplit(request.full_url).hostname for request, _ in requests] == [
        "status.example.test",
        "status.example.test",
        "status.example.test",
        "www.hardcoreradio.nl",
        "status.example.test",
    ]
    request_nonces = [
        dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))["_hcr_poll"]
        for request, _ in requests
    ]
    assert len(request_nonces) == len(set(request_nonces))
    assert "token=synthetic" not in fallback.source_url


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        ("<div class='track'><span class='artist'>Artist</span></div>", "missing_track_fields"),
        ("<div class='track'><span class='artist'>Artist</span><span class='artist'>Other</span><span class='title'>Song</span></div>", "ambiguous_track_fields"),
        ("<div class='track'><span class='artist'>Artist</span><span class='title'>Song</span></div><div class='track'></div>", "ambiguous_track_markup"),
        ("<div class='track'><span class='artist'>Artist</span><span class='title'>Song</span>", "invalid_html"),
    ],
)
def test_player_parser_rejects_missing_ambiguous_and_malformed_markup(page, reason):
    with pytest.raises(PollSourceUnavailable) as error:
        track_from_player_page(page)
    assert error.value.reason == reason


def test_player_parser_decodes_entities_and_normalizes_whitespace():
    assert track_from_player_page(TRACK_HTML) == "Artist & Co - Synthetic Song"


def test_player_parser_rejects_empty_fields():
    with pytest.raises(PollSourceUnavailable, match="missing_track_fields"):
        track_from_player_page('<div class="track"><span class="artist"> </span><span class="title">Song</span></div>')


def test_icecast_invalid_json_uses_stable_source_error(monkeypatch):
    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse(b"not json"))
    with pytest.raises(PollSourceUnavailable) as error:
        fetch_status("https://status.example.test/status-json.xsl")
    assert error.value.reason == "invalid_json"


def test_unusable_icecast_metadata_falls_back_to_player(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    requests = []
    outcomes = [FakeResponse(b'{"icestats": {"source": []}}'), player_response()]

    def open_request(request, timeout):
        requests.append((request, timeout))
        return outcomes.pop(0)

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)

    metadata = _fetch_track_metadata(config)

    assert metadata.source == "player_webpage"
    assert metadata.track == "Artist & Co - Synthetic Song"
    assert len(requests) == 2


def test_standalone_poll_radio_fails_when_no_source_has_metadata(monkeypatch, tmp_path, capsys):
    from hcr_sync.cli import main

    config = make_config(tmp_path, HCR_RUN_POLLER="true")
    monkeypatch.setattr("hcr_sync.cli.load_config", lambda _path: config)
    monkeypatch.setattr(
        "hcr_sync.cli.poll_radio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PollSourcesUnavailable("connection_refused", "timeout")),
    )

    assert main(["poll-radio", "--dry-run"]) == 1
    assert capsys.readouterr().err == "error: icecast=connection_refused; player_page=timeout\n"


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (TimeoutError("synthetic timeout"), "timeout"),
        (urllib.error.HTTPError("https://radio.test/", 502, "bad gateway", None, io.BytesIO()), "http_status_502"),
    ],
)
def test_player_network_failures_are_reported_without_leaking_details(monkeypatch, outcome, reason):
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        raise outcome

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)

    with pytest.raises(PollSourceUnavailable) as error:
        fetch_player_track()

    assert error.value.reason == reason
    assert len(calls) == 1
    assert calls[0][1] == 15.0
    assert "synthetic" not in str(error.value)


def test_player_rejects_unexpected_content_type_and_invalid_encoding(monkeypatch):
    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", lambda *_args, **_kwargs: player_response(content_type="application/json"))
    with pytest.raises(PollSourceUnavailable, match="unexpected_content_type"):
        fetch_player_track()

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", lambda *_args, **_kwargs: player_response(b"\xff", content_type="text/html"))
    with pytest.raises(PollSourceUnavailable, match="invalid_encoding"):
        fetch_player_track()


def test_sources_reject_oversized_responses_and_not_modified_status(monkeypatch):
    monkeypatch.setattr(
        "hcr_sync.poller.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"x" * (MAX_STATUS_BYTES + 1)),
    )
    with pytest.raises(PollSourceUnavailable, match="response_too_large"):
        fetch_status("https://status.example.test/status-json.xsl")

    monkeypatch.setattr(
        "hcr_sync.poller.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"", status=304),
    )
    with pytest.raises(PollSourceUnavailable, match="http_status_304"):
        fetch_status("https://status.example.test/status-json.xsl")


def test_each_http_attempt_has_unique_cache_busting_url_and_no_conditional_headers(monkeypatch):
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        if len(requests) < 3:
            raise refused()
        return icecast_response()

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)
    fetch_status("https://status.example.test/status-json.xsl?existing=kept&_hcr_poll=old")

    urls = [urllib.parse.urlsplit(request.full_url) for request, _ in requests]
    nonces = [dict(urllib.parse.parse_qsl(url.query))["_hcr_poll"] for url in urls]
    assert len(set(nonces)) == 3
    assert all(dict(urllib.parse.parse_qsl(url.query))["existing"] == "kept" for url in urls)
    for request, timeout in requests:
        assert request.get_header("Cache-control") == "no-cache, no-store, max-age=0"
        assert request.get_header("Pragma") == "no-cache"
        assert request.get_header("Accept") == "application/json"
        assert request.get_header("If-none-match") is None
        assert request.get_header("If-modified-since") is None
        assert request.data is None
        assert timeout == 15.0


def test_fallback_then_icecast_duplicate_preserves_first_provenance_and_verbose_audit(monkeypatch, tmp_path):
    config = make_config(tmp_path, HCR_AUDIT_VERBOSE="true")
    init_db(config)
    outcomes = [refused(), refused(), refused(), player_response(), icecast_response()]

    def open_request(_request, timeout):
        result = outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)

    assert poll_radio(config, apply=True) == (True, "Artist & Co - Synthetic Song")
    assert poll_radio(config, apply=True) == (False, "Artist & Co - Synthetic Song")
    record = json.loads(config.seen_tracks_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["metadata_source"] == "player_webpage"
    assert record["metadata_source_url"] == PLAYER_PAGE_URL
    assert config.played_tracks_path.read_text(encoding="utf-8").count("Artist & Co - Synthetic Song") == 1
    with connect(config) as con:
        events = [json.loads(row[0]) for row in con.execute("SELECT payload_json FROM events WHERE event_type = 'radio_poll_seen' ORDER BY id")]
    assert [event["metadata_source"] for event in events] == ["player_webpage", "icecast"]
    assert events[0]["metadata_source_url"] == PLAYER_PAGE_URL
    assert events[1]["metadata_source_url"] == "https://status.example.test/status-json.xsl"


def test_apply_false_fetches_fallback_without_writing_logger_files(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    outcomes = [refused(), refused(), refused(), player_response()]

    def open_request(_request, timeout):
        result = outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)

    assert poll_radio(config, apply=False) == (True, "Artist & Co - Synthetic Song")
    assert not config.seen_tracks_path.exists()
    assert not config.played_tracks_path.exists()


def test_no_previous_track_is_replayed_when_both_sources_fail(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    outcomes = [icecast_response(), refused(), refused(), refused(), TimeoutError("page timeout")]

    def open_request(_request, timeout):
        result = outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)

    assert poll_radio(config, apply=True) == (True, "Artist & Co - Synthetic Song")
    with pytest.raises(PollSourcesUnavailable) as error:
        poll_radio(config, apply=True)
    assert error.value.icecast_reason == "connection_refused"
    assert error.value.player_page_reason == "timeout"
    assert len(config.seen_tracks_path.read_text(encoding="utf-8").splitlines()) == 1
    assert config.played_tracks_path.read_text(encoding="utf-8").count("Artist & Co - Synthetic Song") == 1


def test_unavailable_poll_audit_is_optional_and_contains_only_sanitized_reasons(tmp_path):
    config = make_config(tmp_path, HCR_AUDIT_VERBOSE="true")
    init_db(config)
    failure = PollSourcesUnavailable("connection_refused", "timeout")

    record_poll_unavailable(config, failure, apply=True)

    with connect(config) as con:
        row = con.execute("SELECT event_type, event_source, payload_json FROM events").fetchone()
    payload = json.loads(row["payload_json"])
    assert (row["event_type"], row["event_source"]) == ("radio_poll_unavailable", "poll_radio")
    assert payload["icecast_reason"] == "connection_refused"
    assert payload["player_page_reason"] == "timeout"
    assert payload["observed_at"]
    assert set(payload) == {"observed_at", "icecast_reason", "player_page_reason"}


def test_unavailable_poll_audit_does_not_write_during_dry_run(tmp_path):
    config = make_config(tmp_path, HCR_AUDIT_VERBOSE="true")
    failure = PollSourcesUnavailable("connection_refused", "timeout")

    record_poll_unavailable(config, failure, apply=False)

    assert not config.db_path.exists()


def test_fallback_logger_import_does_not_reactivate_excluded_track(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist & Co", title="Synthetic Song", status="wanted")
            mark_excluded(con, track_id=track["id"], source="manual", reason="test")
    outcomes = [refused(), refused(), refused(), player_response()]

    def open_request(_request, timeout):
        result = outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("hcr_sync.poller.urllib.request.urlopen", open_request)
    monkeypatch.setattr("hcr_sync.poller.time.sleep", lambda _duration: None)

    poll_radio(config, apply=True)
    summary = import_logger(config, apply=True)
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()[0] == "excluded"
    assert summary.skipped_excluded == 2
