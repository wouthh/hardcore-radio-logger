from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.spotify_adapter import (
    BudgetDeferred, RequestBudget, SnapshotOversized, SpotipyClient,
    _spotify_track_from_playlist_item,
)


def item(index=0, **overrides):
    track = {"id": f"track{index}", "uri": f"spotify:track:track{index}",
             "name": f"Song {index}", "type": "track",
             "artists": [{"id": "artist", "name": "Artist"}]}
    track.update(overrides)
    return {"item": track}


def metadata(total, version="v1"):
    return {"snapshot_id": version, "items": {"total": total}}


def page(total, offset=0):
    return {"total": total, "offset": offset,
            "next": f"http://unused.test/items?offset={offset + 50}" if offset + 50 < total else None,
            "items": [item(index) for index in range(offset, min(offset + 50, total))]}


@contextmanager
def spotify_http(tmp_path, responses, *, budget=None, **overrides):
    """Real installed SDK, synthetic credentials, loopback responses only."""
    pytest.importorskip("spotipy")
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            raw_body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            calls.append({"method": self.command, "path": self.path,
                          "body": json.loads(raw_body) if raw_body else None})
            if not responses:
                self.send_error(500, "unexpected test request")
                return
            response = responses.pop(0)
            if response.get("drop"):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(response.get("status", 200))
            self.send_header("Content-Type", "application/json")
            for key, value in response.get("headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(response.get("body", {})).encode())

        do_GET = do_POST = do_DELETE = respond

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    values = dict(DEFAULTS, HCR_SPOTIFY_CLIENT_ID="synthetic", HCR_SPOTIFY_CLIENT_SECRET="synthetic",
                  HCR_SPOTIFY_TOKEN_CACHE=str(tmp_path / "cache"))
    values.update(overrides)
    client = None
    try:
        client = SpotipyClient(Config(values), budget=budget)
        client.sp.auth_manager = None
        client.sp._auth = "synthetic"
        client.sp.prefix = f"http://127.0.0.1:{server.server_port}/"
        yield client, calls
    finally:
        if client is not None:
            client.sp._session.close()
        server.shutdown()
        server.server_close()
        thread.join()


def response(body, **kwargs):
    return {"body": body, **kwargs}


def test_budget_preserves_reserve_and_rejects_invalid_cap():
    with pytest.raises(ValueError, match="at least 9"):
        RequestBudget(8)
    budget = RequestBudget(20, reserve=6)
    for _ in range(14):
        budget.consume()
    with pytest.raises(BudgetDeferred):
        budget.consume()
    assert (budget.used, budget.remaining) == (14, 6)
    budget.reserve = 0
    budget.consume()
    assert budget.used == 15


@pytest.mark.parametrize("setting", ["HCR_SPOTIFY_REQUEST_RETRIES", "HCR_SPOTIFY_STATUS_RETRIES"])
def test_nonzero_retries_are_rejected_before_oauth(tmp_path, setting):
    values = dict(DEFAULTS, **{setting: "1"})
    with pytest.raises(ValueError, match="retries to be zero"):
        SpotipyClient(Config(values))


def test_send_budget_counts_actual_requests_and_stops_before_dispatch(tmp_path):
    responses = [response({"tracks": {"items": []}}) for _ in range(9)]
    with spotify_http(tmp_path, responses, budget=RequestBudget(9)) as (client, calls):
        for _ in range(9):
            assert client.search_query("Artist Song") == []
        with pytest.raises(BudgetDeferred):
            client.search_query("Artist Song")
        assert client.budget.used == len(calls) == 9


@pytest.mark.parametrize("status", [429, 503])
def test_status_and_retry_headers_survive_without_retries(tmp_path, status):
    responses = [response({"error": {"message": "synthetic", "reason": "QUOTA_EXCEEDED"}},
                          status=status, headers={"Retry-After": "82375"})]
    with spotify_http(tmp_path, responses) as (client, calls):
        with pytest.raises(Exception) as caught:
            client.search_query("Artist Song")
        assert caught.value.http_status == status
        assert caught.value.headers["Retry-After"] == "82375"
        assert caught.value.reason == "QUOTA_EXCEEDED"
        assert client.budget.used == len(calls) == 1
        assert all(adapter.max_retries.total == 0 for adapter in client.sp._session.adapters.values())


def test_redirect_is_counted_and_never_followed(tmp_path):
    responses = [response({}, status=302, headers={"Location": "/another-search"})]
    with spotify_http(tmp_path, responses) as (client, calls):
        with pytest.raises(RuntimeError, match="redirects are disabled") as caught:
            client.search_query("Artist Song")
        assert caught.value.http_status == 302 and caught.value.reason == "redirect_refused"
        assert client.budget.used == len(calls) == 1


def test_add_uses_uris_object_and_remove_returns_acknowledgement(tmp_path):
    responses = [response({"snapshot_id": "added"}), response({"snapshot_id": "removed"})]
    with spotify_http(tmp_path, responses) as (client, calls):
        assert client.add_tracks("playlist", ["spotify:track:one"]) == {"snapshot_id": "added"}
        assert client.remove_tracks("playlist", ["spotify:track:one"]) == {"snapshot_id": "removed"}
        assert [(call["method"], urlsplit(call["path"]).path) for call in calls] == [
            ("POST", "/playlists/playlist/items"), ("DELETE", "/playlists/playlist/items")]
        assert calls[0]["body"] == {"uris": ["spotify:track:one"]}
        assert calls[1]["body"] == {"items": [{"uri": "spotify:track:one"}]}
        assert client.budget.used == 2
        assert client.add_tracks("playlist", []) == client.remove_tracks("playlist", []) == {}
        assert client.budget.used == 2


@pytest.mark.parametrize("total,required", [(0, 3), (1, 3), (51, 4), (416, 11)])
def test_complete_snapshot_uses_fifty_item_pages_and_final_version_check(tmp_path, total, required):
    responses = [response(metadata(total))]
    responses += [response(page(total, offset)) for offset in range(0, max(1, total), 50)]
    responses += [response(metadata(total))]
    with spotify_http(tmp_path, responses, budget=RequestBudget(20, reserve=6)) as (client, calls):
        snapshot = client.playlist_snapshot("playlist")
        assert snapshot.complete and snapshot.identified and snapshot.total == total
        assert len(snapshot.tracks) == total
        assert snapshot.snapshot_id == "v1"
        assert client.budget.used == len(calls) == required
        pages = [call for call in calls if urlsplit(call["path"]).path.endswith("/items")]
        assert [parse_qs(urlsplit(call["path"]).query)["limit"] for call in pages] == [["50"]] * len(pages)
        assert [parse_qs(urlsplit(call["path"]).query)["offset"] for call in pages] == [
            [str(offset)] for offset in range(0, max(1, total), 50)]


def test_pre_read_metadata_is_not_requested_twice(tmp_path):
    responses = [response(metadata(1)), response(page(1)), response(metadata(1))]
    with spotify_http(tmp_path, responses) as (client, calls):
        meta = client.playlist_metadata("playlist")
        assert client.playlist_snapshot("playlist", metadata=meta).total == 1
        assert client.budget.used == len(calls) == 3


@pytest.mark.parametrize("total,reserve,required", [(1000, 0, 22), (51, 6, 4)])
def test_oversized_snapshot_stops_before_first_page(tmp_path, total, reserve, required):
    responses = [response(metadata(total))]
    with spotify_http(tmp_path, responses, budget=RequestBudget(9, reserve=reserve)) as (client, calls):
        with pytest.raises(SnapshotOversized) as caught:
            client.playlist_snapshot("playlist")
        assert caught.value.required == required
        assert client.budget.used == len(calls) == 1


@pytest.mark.parametrize("broken", [
    {"total": 2, "offset": 0, "items": [item()], "next": None},
    {"total": 1, "offset": 1, "items": [item()], "next": None},
    {"total": 1, "offset": 0, "items": [], "next": None},
    {"total": 1, "offset": 0, "items": [item()], "next": "unexpected"},
    {"total": 1, "offset": 0, "items": [item()], "next": False},
    {"total": 1, "items": [item()], "next": None},
    {"total": 1, "offset": 0, "items": [item()]},
])
def test_malformed_pagination_is_refused_without_final_scan(tmp_path, broken):
    responses = [response(metadata(1)), response(broken)]
    with spotify_http(tmp_path, responses) as (client, calls):
        with pytest.raises(RuntimeError, match="pagination was incomplete"):
            client.playlist_snapshot("playlist")
        assert client.budget.used == len(calls) == 2


def test_snapshot_version_change_is_refused(tmp_path):
    responses = [response(metadata(1)), response(page(1)), response(metadata(1, "v2"))]
    with spotify_http(tmp_path, responses) as (client, calls):
        with pytest.raises(RuntimeError, match="changed during pagination"):
            client.playlist_snapshot("playlist")
        assert client.budget.used == len(calls) == 3


def test_null_entries_degrade_identity_without_discarding_unavailable_ids(tmp_path):
    mixed = page(4)
    mixed["items"] = [item(0), {"item": None}, item(2, name=None, artists=[]),
                      {"item": {"id": "episode", "uri": "spotify:episode:episode", "type": "episode"}}]
    responses = [response(metadata(4)), response(mixed), response(metadata(4))]
    with spotify_http(tmp_path, responses) as (client, calls):
        snapshot = client.playlist_snapshot("playlist")
        assert snapshot.complete and not snapshot.identified
        assert [track.track_id for track in snapshot.tracks] == ["track0", "track2"]
        assert snapshot.tracks[1].uri == "spotify:track:track2"
        assert snapshot.tracks[1].metadata_ambiguous and snapshot.tracks[1].title == ""
        assert snapshot.total == 4 and client.budget.used == len(calls) == 3


def test_identity_only_entries_retain_known_id_or_uri():
    only_id = _spotify_track_from_playlist_item({"item": {"id": "known"}})
    only_uri = _spotify_track_from_playlist_item({"track": {"uri": "spotify:track:known"}})
    assert only_id == only_uri
    assert only_id.track_id == "known" and only_id.uri == "spotify:track:known"
    assert only_id.metadata_ambiguous


def test_search_variants_share_transport_budget_and_deduplicate(tmp_path):
    first = item()["item"]
    responses = [response({"tracks": {"items": [first, first]}}) for _ in range(4)]
    with spotify_http(tmp_path, responses) as (client, calls):
        result = client.search_track("EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")
        assert [track.track_id for track in result] == ["track0"]
        assert len(client.search_queries("EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")) == 4
        assert client.budget.used == len(calls) == 4


def test_lost_write_response_does_not_retry_and_positive_state_can_be_read(tmp_path):
    responses = [{"drop": True}, response(metadata(1)), response(page(1)), response(metadata(1))]
    with spotify_http(tmp_path, responses) as (client, calls):
        with pytest.raises(requests.ConnectionError):
            client.add_tracks("playlist", ["spotify:track:track0"])
        assert len(calls) == client.budget.used == 1
        assert client.playlist_snapshot("playlist").tracks[0].track_id == "track0"
        assert len(calls) == client.budget.used == 4
        assert sum(call["method"] == "POST" for call in calls) == 1
