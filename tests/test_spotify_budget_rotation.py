"""Whole-run Spotify limits must permit durable progress and fair retries."""

from contextlib import contextmanager
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.db import connect, ensure_track, get_state, init_db, set_state, transaction, upsert_spotify_asset
from hcr_sync.spotify_adapter import BudgetDeferred, PlaylistSnapshot, RequestBudget, SpotifyTrack
from hcr_sync.spotify_sync import scan_spotify_playlist, sync_spotify
from test_spotify_adapter import item, metadata, page, response, spotify_http


def config_for(tmp_path, **overrides):
    values = dict(DEFAULTS, HCR_DB_PATH=str(tmp_path / "db.sqlite"),
                  HCR_MUSIC_DIR=str(tmp_path / "music"), HCR_SPOTIFY_PLAYLIST_ID="playlist",
                  HCR_SPOTIFY_TOKEN_CACHE=str(tmp_path / "cache"))
    values.update(overrides)
    config = Config(values)
    init_db(config)
    config.music_dir.mkdir()
    return config


def seed(config, artist, title="Song", *, retry=False, playlist_id="playlist"):
    with connect(config) as con, transaction(con):
        track = ensure_track(con, artist=artist, title=title)
        if retry:
            upsert_spotify_asset(con, track_id=track["id"], playlist_id=playlist_id,
                                 in_playlist=False, match_confidence=0.0, status="review",
                                 search_last_at="2000-01-01T00:00:00Z", search_attempts=1,
                                 search_next_at="2000-01-02T00:00:00Z", update_search=True)
        return track["id"]


def queries(calls):
    return [parse_qs(urlsplit(call["path"]).query)["q"][0]
            for call in calls if urlsplit(call["path"]).path == "/search"]


def empty_searches(count):
    return [response({"tracks": {"items": []}}) for _ in range(count)]


class SearchOnlySpotify:
    def __init__(self, candidates=()):
        self.candidates = list(candidates)
        self.searches = []

    def search_track(self, artist, title):
        self.searches.append((artist, title))
        return self.candidates

    def playlist_snapshot(self, playlist_id):
        return PlaylistSnapshot(playlist_id, "empty", [])

    def add_tracks(self, playlist_id, uris):
        pytest.fail("search-only fixture must not dispatch additions")


def test_impossible_scans_do_not_repeat_pages_or_starve_new_sources(tmp_path):
    config = config_for(tmp_path)
    for index in range(3):
        source_id = seed(config, f"Source{index}")
        responses = [response(metadata(1000))] + empty_searches(2)
        with spotify_http(tmp_path, responses) as (client, calls):
            scanned = scan_spotify_playlist(config, apply=True, client=client)
            assert scanned.budget_deferred == 1
            synced = sync_spotify(config, apply=True, client=client)
            assert synced.first_time == 1
            assert client.budget.used == len(calls) == 3
            assert not any(urlsplit(call["path"]).path.endswith("/items") for call in calls)
        with connect(config) as con:
            degraded = json.loads(get_state(con, "spotify_degraded:playlist"))
            assert degraded["required_scan_budget"] == 22
            assert degraded["required_work_budget"] == 28
            assert degraded["configured_budget"] == 20
            assert "28" in degraded["recovery"]
            assert con.execute("SELECT search_last_at FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()[0]
            assert get_state(con, "spotify_membership:playlist", "") == ""


def test_raising_budget_recovers_scan_then_reuses_only_verified_cache(tmp_path):
    config = config_for(tmp_path)
    with spotify_http(tmp_path, [response(metadata(1000))]) as (client, calls):
        assert scan_spotify_playlist(config, apply=True, client=client).budget_deferred == 1
        assert len(calls) == 1
    source_id = seed(config, "Source")
    config.values["HCR_SPOTIFY_REQUEST_BUDGET"] = "28"
    responses = [response(metadata(1000))]
    responses += [response(page(1000, offset)) for offset in range(0, 1000, 50)]
    responses += [response(metadata(1000))] + empty_searches(2)
    with spotify_http(tmp_path, responses, budget=RequestBudget(28)) as (client, calls):
        scanned = scan_spotify_playlist(config, apply=True, client=client)
        assert scanned.seen == 1000
        assert sync_spotify(config, apply=True, client=client, snapshot=scanned._snapshot).first_time == 1
        assert len(calls) == client.budget.used == 24
    with connect(config) as con:
        assert get_state(con, "spotify_degraded:playlist") == ""
        assert con.execute("SELECT search_last_at FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()[0]
    with spotify_http(tmp_path, [response(metadata(1000))], budget=RequestBudget(28)) as (client, calls):
        assert scan_spotify_playlist(config, apply=True, client=client).seen == 1000
        assert len(calls) == client.budget.used == 1


def test_scan_only_does_not_restart_on_changed_versions_before_source_progress(tmp_path):
    config = config_for(tmp_path)
    source_id = seed(config, "EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")
    responses = [response(metadata(650))]
    responses += [response(page(650, offset)) for offset in range(0, 650, 50)]
    responses += [response(metadata(650))] + empty_searches(3)
    with spotify_http(tmp_path, responses) as (client, calls):
        scanned = scan_spotify_playlist(config, apply=True, client=client)
        assert scanned.seen == 650 and client.budget.used == 15
        synced = sync_spotify(config, apply=True, client=client, snapshot=scanned._snapshot)
        assert synced.first_time == 0 and synced.budget_deferred == 1
        assert len(calls) == client.budget.used == 18
        first_queries = queries(calls)
    with connect(config) as con:
        assert get_state(con, "spotify_scan_only_spent:playlist") == "true"
        pending = con.execute("SELECT payload_json FROM spotify_pending_work WHERE track_id=? AND kind='search'", (source_id,)).fetchone()
        assert json.loads(pending[0])["next_query"] == 3
    with spotify_http(tmp_path, [response(metadata(650, "v2"))] + empty_searches(1)) as (client, calls):
        assert scan_spotify_playlist(config, apply=True, client=client).budget_deferred == 1
        assert sync_spotify(config, apply=True, client=client).first_time == 1
        assert len(calls) == client.budget.used == 2
        assert len(first_queries + queries(calls)) == len(set(first_queries + queries(calls))) == 4
        assert not any(urlsplit(call["path"]).path.endswith("/items") for call in calls)
    with connect(config) as con:
        assert get_state(con, "spotify_scan_only_spent:playlist") == "false"
        assert con.execute("SELECT search_last_at FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()[0]


def test_partial_queries_resume_after_budget_restart_without_attempt_or_cursor_loss(tmp_path):
    config = config_for(tmp_path)
    source_id = seed(config, "EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")
    with connect(config) as con, transaction(con):
        set_state(con, "spotify_rotation:playlist", "2")
    with spotify_http(tmp_path, empty_searches(7), budget=RequestBudget(9)) as (client, calls):
        for _ in range(5):
            client.search_query("prior-stage work")
        expected = client.search_queries("EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")
        summary = sync_spotify(config, apply=True, client=client)
        assert summary.budget_deferred == 1 and summary.first_time == 0
        assert queries(calls)[5:] == expected[:2]
        assert len(calls) == client.budget.used == 7
    with connect(config) as con:
        assert con.execute("SELECT 1 FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone() is None
        assert get_state(con, "spotify_rotation:playlist") == "2"
        pending = json.loads(con.execute("SELECT payload_json FROM spotify_pending_work WHERE track_id=?", (source_id,)).fetchone()[0])
        assert (pending["next_query"], pending["lane"]) == (2, "first")
    with spotify_http(tmp_path, empty_searches(2), budget=RequestBudget(9)) as (client, calls):
        assert sync_spotify(config, apply=True, client=client).first_time == 1
        assert queries(calls) == expected[2:]
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
        assert asset["search_attempts"] == 1 and asset["search_next_at"]
        assert con.execute("SELECT 1 FROM spotify_pending_work WHERE track_id=?", (source_id,)).fetchone() is None


def test_partial_search_429_and_cooldown_keep_first_time_slot_until_completion(tmp_path):
    config = config_for(tmp_path)
    source_id = seed(config, "EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")
    seed(config, "Retry", retry=True)
    with connect(config) as con, transaction(con):
        set_state(con, "spotify_rotation:playlist", "2")
    responses = empty_searches(2) + [response({"error": {"message": "synthetic"}},
                                             status=429, headers={"Retry-After": "1000"})]
    with spotify_http(tmp_path, responses) as (client, calls):
        expected = client.search_queries("EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")
        summary = sync_spotify(config, apply=True, client=client)
        assert summary.rate_limited and summary.first_time == summary.retries == 0
        assert queries(calls) == expected[:3]
        assert client.budget.used == 3
    with spotify_http(tmp_path, []) as (client, calls):
        assert sync_spotify(config, apply=True, client=client).rate_limited
        assert calls == [] and client.budget.used == 0
    with connect(config) as con, transaction(con):
        assert get_state(con, "spotify_rotation:playlist") == "2"
        pending = json.loads(con.execute("SELECT payload_json FROM spotify_pending_work WHERE track_id=?", (source_id,)).fetchone()[0])
        assert pending["next_query"] == 2 and pending["lane"] == "first"
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
        assert asset["search_attempts"] == 0
        set_state(con, "spotify_rate_limited_until", "2000-01-01T00:00:00Z")
        con.execute("UPDATE spotify_assets SET search_next_at='2000-01-01T00:00:00Z' WHERE track_id=?", (source_id,))
    config.values["HCR_SPOTIFY_SYNC_LIMIT"] = "1"
    with spotify_http(tmp_path, empty_searches(2)) as (client, calls):
        summary = sync_spotify(config, apply=True, client=client)
        assert summary.first_time == 1 and summary.retries == 0
        assert queries(calls) == expected[2:]
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "3"


@pytest.mark.parametrize("first_weight,retry_weight", [(3, 1), (2, 1)])
def test_weighted_rotation_survives_restarts_and_continuous_new_arrivals(tmp_path, first_weight, retry_weight):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="1", HCR_SPOTIFY_FIRST_TIME_WEIGHT=str(first_weight),
                        HCR_SPOTIFY_RETRY_WEIGHT=str(retry_weight))
    for index in range(6):
        seed(config, f"Retry{index}", retry=True)
    observed = []
    for index in range((first_weight + retry_weight) * 3):
        seed(config, f"Fresh{index}")
        fake = SearchOnlySpotify()
        summary = sync_spotify(config, apply=True, client=fake)
        assert len(fake.searches) == summary.first_time + summary.retries == 1
        observed.append("retry" if summary.retries else "first")
    assert observed == (["first"] * first_weight + ["retry"] * retry_weight) * 3


def test_rotation_cursor_is_scoped_to_configured_playlist(tmp_path):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="1")
    seed(config, "Fresh")
    retry_id = seed(config, "Retry", retry=True)
    with connect(config) as con, transaction(con):
        upsert_spotify_asset(con, track_id=retry_id, playlist_id="other", in_playlist=False,
                             match_confidence=0.0, status="review", search_attempts=1,
                             search_last_at="2000-01-01T00:00:00Z", search_next_at="2000-01-02T00:00:00Z",
                             update_search=True)
        set_state(con, "spotify_rotation:playlist", "2")
        set_state(con, "spotify_rotation:other", "3")
    config.values["HCR_SPOTIFY_PLAYLIST_ID"] = "other"
    fake = SearchOnlySpotify()
    assert sync_spotify(config, apply=True, client=fake).retries == 1
    assert fake.searches == [("Retry", "Song")]
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "2"
        assert get_state(con, "spotify_rotation:other") == "0"
    config.values["HCR_SPOTIFY_PLAYLIST_ID"] = "playlist"
    fake = SearchOnlySpotify()
    assert sync_spotify(config, apply=True, client=fake).first_time == 1
    assert fake.searches == [("Fresh", "Song")]
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "3"
        assert get_state(con, "spotify_rotation:other") == "0"


def test_two_slot_runs_keep_cursor_and_empty_queue_fallback_has_no_debt(tmp_path):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="2")
    for index in range(8):
        seed(config, f"Fresh{index}")
    for index in range(4):
        seed(config, f"Retry{index}", retry=True)
    observed = []
    for _ in range(3):
        fake = SearchOnlySpotify()
        sync_spotify(config, apply=True, client=fake)
        observed.extend("retry" if artist.startswith("Retry") else "first" for artist, _ in fake.searches)
    assert observed == ["first", "first", "first", "retry", "first", "first"]
    with connect(config) as con, transaction(con):
        con.execute("UPDATE spotify_assets SET search_next_at='2099-01-01T00:00:00Z' WHERE search_last_at IS NOT NULL")
        set_state(con, "spotify_rotation:playlist", "3")
    for _ in range(2):
        fake = SearchOnlySpotify()
        sync_spotify(config, apply=True, client=fake)
        assert all(artist.startswith("Fresh") for artist, _ in fake.searches)
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "0"
    seed(config, "NewAfterEmpty")
    seed(config, "RetryAfterEmpty", retry=True)
    config.values["HCR_SPOTIFY_SYNC_LIMIT"] = "1"
    fake = SearchOnlySpotify()
    assert sync_spotify(config, apply=True, client=fake).first_time == 1


def test_ineligible_tracks_and_budget_deferral_do_not_consume_cursor(tmp_path):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="1")
    seed(config, "DJ", "Live Set")
    source_id = seed(config, "Eligible")
    seed(config, "Retry", retry=True)
    with connect(config) as con, transaction(con):
        set_state(con, "spotify_rotation:playlist", "2")

    class DeferredSpotify(SearchOnlySpotify):
        def search_track(self, artist, title):
            self.searches.append((artist, title))
            raise BudgetDeferred()

    fake = DeferredSpotify()
    assert sync_spotify(config, apply=True, client=fake).budget_deferred == 1
    assert fake.searches == [("Eligible", "Song")]
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "2"
        pending = json.loads(con.execute("SELECT payload_json FROM spotify_pending_work WHERE track_id=?", (source_id,)).fetchone()[0])
        assert pending["lane"] == "first"
    assert sync_spotify(config, apply=True, client=SearchOnlySpotify()).first_time == 1
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "3"


def test_low_confidence_shared_candidate_never_replaces_owners_or_repeats(tmp_path):
    config = config_for(tmp_path)
    owner_id = seed(config, "Owner", "Catalog")
    sources = [seed(config, "Source", "Song"), seed(config, "Source", "Another Song")]
    with connect(config) as con, transaction(con):
        upsert_spotify_asset(con, track_id=owner_id, playlist_id="playlist", spotify_track_id="owned",
                             spotify_track_uri="spotify:track:owned", spotify_artist="Source",
                             spotify_title="Different Song", in_playlist=True, match_confidence=1.0,
                             status="added", added_at="2000-01-01T00:00:00Z")
        before = dict(con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (owner_id,)).fetchone())
    fake = SearchOnlySpotify([SpotifyTrack("spotify:track:owned", "owned", "Source", "Different Song")])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.first_time == summary.review == 2
    with connect(config) as con:
        assert dict(con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (owner_id,)).fetchone()) == before
        for source_id in sources:
            row = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
            assert row["search_last_at"] and row["search_attempts"] == 1 and row["search_next_at"]
            assert not row["spotify_track_id"]
    again = SearchOnlySpotify(fake.candidates)
    sync_spotify(config, apply=True, client=again)
    assert again.searches == []


def test_completion_commit_failure_preserves_queries_without_cursor_or_write_intent(tmp_path, monkeypatch):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="1")
    source_id = seed(config, "Artist", "Song")
    seed(config, "Retry", retry=True)
    with connect(config) as con, transaction(con):
        set_state(con, "spotify_rotation:playlist", "2")
    import hcr_sync.spotify_sync as sync_module
    real_transaction = sync_module.transaction
    injected = False

    @contextmanager
    def failing_completion(con):
        nonlocal injected
        with real_transaction(con):
            yield con
            if not injected and con.execute("SELECT 1 FROM events WHERE event_type='spotify_search_completed' AND track_id=?", (source_id,)).fetchone():
                injected = True
                raise sqlite3.OperationalError("synthetic completion commit failure")

    monkeypatch.setattr(sync_module, "transaction", failing_completion)
    candidate = item(0, id="candidate", uri="spotify:track:candidate", name="Song")["item"]
    with spotify_http(tmp_path, [response({"tracks": {"items": [candidate]}})] * 2) as (client, calls):
        with pytest.raises(sqlite3.OperationalError, match="completion commit failure"):
            sync_spotify(config, apply=True, client=client)
        assert len(calls) == 2 and all(call["method"] == "GET" for call in calls)
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "2"
        assert con.execute("SELECT 1 FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone() is None
        rows = list(con.execute("SELECT kind,payload_json FROM spotify_pending_work WHERE track_id=?", (source_id,)))
        assert len(rows) == 1 and rows[0]["kind"] == "search"
        assert json.loads(rows[0]["payload_json"])["next_query"] == 2
        assert con.execute("SELECT 1 FROM events WHERE event_type='spotify_search_completed' AND track_id=?", (source_id,)).fetchone() is None
    present = page(1)
    present["items"] = [{"item": candidate}]
    responses = [response(metadata(0)), response(page(0)), response(metadata(0)),
                 response({"snapshot_id": "v2"}), response(metadata(1, "v2")),
                 response(present), response(metadata(1, "v2"))]
    with spotify_http(tmp_path, responses) as (client, calls):
        summary = sync_spotify(config, apply=True, client=client)
        assert summary.first_time == summary.added == 1
        assert queries(calls) == [] and len(calls) == 7
        assert sum(call["method"] == "POST" for call in calls) == 1
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "3"
        assert con.execute("SELECT COUNT(*) FROM spotify_pending_work WHERE track_id=?", (source_id,)).fetchone()[0] == 0
        assert con.execute("SELECT in_playlist FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()[0] == 1
