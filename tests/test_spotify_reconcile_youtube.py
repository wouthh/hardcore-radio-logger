import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, ensure_track, init_db, mark_excluded, set_state, transaction, upsert_spotify_asset, upsert_youtube_asset
from hcr_sync.reconcile import manual_exclude, reconcile
from hcr_sync.spotify_sync import PlaylistSnapshot, SpotifyTrack, _spotify_search_queries, _spotify_track_from_playlist_item, backfill_spotify, scan_spotify_playlist, sync_spotify
from hcr_sync.system import LegacyDownloaderActive, assert_legacy_downloader_safe
from hcr_sync.youtube_sync import YouTubeCandidate, sync_youtube


def make_config(tmp_path: Path, **overrides: str) -> Config:
    values = dict(DEFAULTS)
    values.update(
        {
            "HCR_DB_PATH": str(tmp_path / "hcr_music.db"),
            "HCR_MUSIC_DIR": str(tmp_path / "music"),
            "HCR_TRASH_DIR": str(tmp_path / "music" / ".hcr-trash"),
            "HCR_SPOTIFY_PLAYLIST_ID": "playlist",
            "HCR_SPOTIFY_TOKEN_CACHE": str(tmp_path / "token-cache"),
            "HCR_RECONCILE_REQUIRE_TWO_PASSES": "true",
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


class FakeSpotify:
    def __init__(self, snapshot_tracks=None, search_tracks=None):
        self.snapshot_tracks = snapshot_tracks or []
        self.search_tracks = search_tracks or []
        self.added = []
        self.removed = []
        self.search_calls = []

    def auth_check(self):
        return "fake-user"

    def playlist_snapshot(self, playlist_id):
        return PlaylistSnapshot(playlist_id=playlist_id, snapshot_id="snap", tracks=self.snapshot_tracks, complete=True)

    def search_track(self, artist, title):
        self.search_calls.append((artist, title))
        return self.search_tracks

    def add_tracks(self, playlist_id, uris):
        self.added.extend(uris)

    def remove_tracks(self, playlist_id, uris):
        self.removed.extend(uris)


def test_playlist_item_parser_accepts_spotify_item_shape():
    track = _spotify_track_from_playlist_item(
        {
            "item": {
                "id": "abc",
                "uri": "spotify:track:abc",
                "name": "Song",
                "type": "track",
                "duration_ms": 180000,
                "artists": [{"name": "Artist"}],
            }
        }
    )

    assert track == SpotifyTrack(
        uri="spotify:track:abc",
        track_id="abc",
        artist="Artist",
        title="Song",
        duration_ms=180000,
    )


def test_spotify_backfill_and_sync_with_fake_client(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    backfill = FakeSpotify([SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])

    summary = backfill_spotify(config, apply=True, client=backfill)

    assert summary.linked == 1
    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM spotify_assets WHERE in_playlist = 1").fetchone()["count"] == 1

    sync_client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:2", track_id="2", artist="Other", title="Song")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Other", title="Song", status="wanted")

    sync_summary = sync_spotify(config, apply=True, client=sync_client)

    assert sync_summary.added == 1
    assert sync_client.added == ["spotify:track:2"]
    with connect(config) as con:
        payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'spotify_added'").fetchone()["payload_json"])
        assert payload["spotify_track_id"] == "2"
        assert payload["match_status"] == "added"
        assert payload["match_threshold"] == 0.9


def test_spotify_scan_imports_playlist_addition_for_youtube_sync(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="true")
    init_db(config)
    config.music_dir.mkdir()
    spotify = FakeSpotify(
        snapshot_tracks=[
            SpotifyTrack(
                uri="spotify:track:spotify-new",
                track_id="spotify-new",
                artist="Artist",
                title="Title",
            )
        ]
    )

    scan_summary = scan_spotify_playlist(config, apply=True, client=spotify)

    assert scan_summary.seen == 1
    assert scan_summary.linked == 1
    with connect(config) as con:
        track = con.execute("SELECT * FROM tracks").fetchone()
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert track["status"] == "wanted"
        assert asset["in_playlist"] == 1

    class DownloadingYouTube:
        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=ytnew123",
                    video_id="ytnew123",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            path = config.music_dir / "Artist - Title [ytnew123].mp3"
            path.write_bytes(b"audio")
            return path

    youtube_summary = sync_youtube(config, apply=True, client=DownloadingYouTube())

    assert youtube_summary.downloaded == 1
    with connect(config) as con:
        youtube = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'ytnew123'").fetchone()
        assert youtube is not None


def test_spotify_scan_reuses_existing_asset_by_spotify_id(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="true")
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Drokz", title="Karma", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:karma",
                spotify_track_id="karma",
                spotify_artist="Drokz",
                spotify_title="Karma",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )

    summary = scan_spotify_playlist(
        config,
        apply=True,
        client=FakeSpotify(
            snapshot_tracks=[
                SpotifyTrack(
                    uri="spotify:track:karma",
                    track_id="karma",
                    artist="Drokz",
                    title="Karma - Original Mix",
                )
            ]
        ),
    )

    assert summary.linked == 1
    with connect(config) as con:
        tracks = list(con.execute("SELECT * FROM tracks"))
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert len(tracks) == 1
        assert tracks[0]["display_title"] == "Karma"
        assert asset["track_id"] == tracks[0]["id"]
        assert asset["spotify_title"] == "Karma - Original Mix"


def test_spotify_scan_does_not_reactivate_excluded_track(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="true")
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            mark_excluded(con, track_id=track["id"], source="manual", reason="manual")

    summary = scan_spotify_playlist(
        config,
        apply=True,
        client=FakeSpotify(snapshot_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")]),
    )

    assert summary.skipped == 1
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "excluded"
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["in_playlist"] == 1
        assert asset["spotify_track_id"] == "1"


def test_spotify_scan_records_excluded_playlist_asset_for_reconcile_removal(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="true")
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            mark_excluded(con, track_id=track["id"], source="manual", reason="manual")
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "0")
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "1")

    scan_summary = scan_spotify_playlist(
        config,
        apply=True,
        client=FakeSpotify(snapshot_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")]),
    )

    assert scan_summary.skipped == 1
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "excluded"
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["in_playlist"] == 1
        assert asset["spotify_track_id"] == "1"

    spotify = FakeSpotify(snapshot_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    reconcile_summary = reconcile(config, apply=True, spotify_client=spotify)

    assert reconcile_summary.refused == []
    assert reconcile_summary.spotify_removed == 1
    assert spotify.removed == ["spotify:track:1"]
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["in_playlist"] == 0
        assert asset["status"] == "removed"


def test_spotify_disabled_skips_without_client(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="false")
    init_db(config)

    assert backfill_spotify(config, apply=True).skipped == 1
    assert sync_spotify(config, apply=True).skipped == 1


def test_spotify_sync_reviews_non_track_sources_without_searching(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:mix", track_id="mix", artist="Various Artists", title="Full Mix")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Various Artists", title="Dominator Festival Full Mix", status="wanted")

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.review == 1
    assert client.added == []
    with connect(config) as con:
        assert con.execute("SELECT status FROM spotify_assets").fetchone()["status"] == "review"


def test_spotify_sync_rejects_generic_title_inside_longer_candidate(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:weekend", track_id="weekend", artist="Drokz", title="I am ready for the weekend")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Drokz", title="Weekend", status="wanted")

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.review == 1
    assert client.added == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE status = 'review'").fetchone()
    assert asset is not None


def test_spotify_sync_rejects_named_remix_when_source_is_not_remix(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:understream", track_id="understream", artist="Drokz, Execrate", title="The Understream - Execrate Remix")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Drokz", title="The Understream", status="wanted")

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.review == 1
    assert client.added == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE status = 'review'").fetchone()
    assert asset is not None


def test_spotify_sync_allows_bracketed_subtitle_when_core_title_matches(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:mind", track_id="mind", artist="Drokz", title="The Mind (Signs Of Life)")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Drokz", title="The Mind", status="wanted")

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.added == 1
    assert client.added == ["spotify:track:mind"]


def test_spotify_sync_skips_existing_review_asset_without_search(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ADD_REVIEW_MATCHES="false")
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                in_playlist=False,
                match_confidence=None,
                status="review",
            )

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.review == 1
    assert client.added == []


def test_spotify_search_queries_try_main_artist_and_clean_title():
    queries = _spotify_search_queries("EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE (Extended Mix)")

    assert queries[0] == "artist:EQUAL2 & PSYCHOWEAPON track:HARDCORE LIFESTYLE (Extended Mix)"
    assert "artist:EQUAL2 & PSYCHOWEAPON track:HARDCORE LIFESTYLE" in queries
    assert "artist:EQUAL2 track:HARDCORE LIFESTYLE" in queries
    assert "EQUAL2 HARDCORE LIFESTYLE" in queries


def test_spotify_sync_tentatively_adds_review_match_below_confident_threshold(tmp_path):
    config = make_config(
        tmp_path,
        HCR_SPOTIFY_MATCH_THRESHOLD="1.01",
        HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD="0.90",
    )
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.added == 1
    assert summary.tentative_added == 1
    assert client.added == ["spotify:track:1"]
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["in_playlist"] == 1
        assert asset["status"] == "review"
        assert asset["spotify_track_id"] == "1"
        payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'spotify_tentatively_added'").fetchone()["payload_json"])
        assert payload["match_status"] == "tentative_review"
        assert payload["match_threshold"] == 1.01
        assert payload["tentative_threshold"] == 0.9


def test_spotify_sync_does_not_readd_removed_tentative_match(tmp_path):
    config = make_config(
        tmp_path,
        HCR_SPOTIFY_MATCH_THRESHOLD="1.01",
        HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD="0.90",
    )
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=False,
                match_confidence=1.0,
                status="removed",
            )

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.review == 1
    assert client.search_calls == []
    assert client.added == []


def test_spotify_sync_retries_old_review_once_then_keeps_terminal_review(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD="0.85")
    init_db(config)
    low_match = SpotifyTrack(uri="spotify:track:low", track_id="low", artist="Other", title="Title")
    client = FakeSpotify(search_tracks=[low_match])
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                in_playlist=False,
                match_confidence=None,
                status="review",
            )

    first = sync_spotify(config, apply=True, client=client)
    second = sync_spotify(config, apply=True, client=client)

    assert first.review == 1
    assert second.review == 1
    assert client.added == []
    assert len(client.search_calls) == 1
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["status"] == "review"
        assert asset["in_playlist"] == 0
        assert asset["match_confidence"] == 0.55
        assert asset["spotify_track_id"] == "low"
        payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'ambiguous_spotify_match'").fetchone()["payload_json"])
        assert payload["reason"] == "below tentative threshold or not found"
        assert payload["match_status"] == "review"
        assert payload["score"] == 0.55


def test_spotify_sync_respects_per_run_limit(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="1")
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")
            ensure_track(con, artist="Other", title="Song", status="wanted")

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.added == 1
    assert summary.skipped == 1
    assert len(client.added) == 1


def test_spotify_sync_stops_cleanly_on_rate_limit(tmp_path):
    config = make_config(tmp_path)
    init_db(config)

    class RateLimitedSpotify(FakeSpotify):
        def search_track(self, artist, title):
            self.search_calls.append((artist, title))
            exc = RuntimeError("rate limited")
            exc.http_status = 429
            exc.headers = {"Retry-After": "3600"}
            raise exc

    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    summary = sync_spotify(config, apply=True, client=RateLimitedSpotify())

    assert summary.rate_limited is True
    assert summary.skipped == 1
    with connect(config) as con:
        assert con.execute("SELECT value FROM sync_state WHERE key = 'spotify_rate_limited_until'").fetchone() is not None
        last_response = json.loads(con.execute("SELECT value FROM sync_state WHERE key = 'spotify_rate_limit_last_response'").fetchone()["value"])
        event_payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'spotify_rate_limited'").fetchone()["payload_json"])
        assert last_response["retry_after_seconds"] == 3600
        assert last_response["retry_after_source"] == "header"
        assert last_response["fallback_used"] is False
        assert event_payload["retry_after_seconds"] == 3600

    next_client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    second = sync_spotify(config, apply=True, client=next_client)

    assert second.rate_limited is True
    assert second.skipped == 1
    assert next_client.search_calls == []
    assert next_client.added == []
    third = sync_spotify(config, apply=True, client=next_client)
    assert third.rate_limited is True
    with connect(config) as con:
        cooldown_events = con.execute("SELECT COUNT(*) AS count FROM events WHERE event_type = 'spotify_rate_limit_cooldown_active'").fetchone()["count"]
        assert cooldown_events == 1


def test_spotify_sync_dry_run_rate_limit_does_not_write_cooldown(tmp_path):
    config = make_config(tmp_path)
    init_db(config)

    class RateLimitedSpotify(FakeSpotify):
        def search_track(self, artist, title):
            self.search_calls.append((artist, title))
            exc = RuntimeError("rate limited")
            exc.http_status = 429
            exc.headers = {"Retry-After": "3600"}
            raise exc

    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    summary = sync_spotify(config, apply=False, client=RateLimitedSpotify())

    assert summary.rate_limited is True
    with connect(config) as con:
        assert con.execute("SELECT * FROM sync_state WHERE key LIKE 'spotify_rate_limit%'").fetchall() == []
        assert con.execute("SELECT * FROM events WHERE event_type = 'spotify_rate_limited'").fetchone() is None


def test_spotify_sync_reviews_candidate_track_id_linked_to_other_track_without_adding(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:same", track_id="same", artist="Artist", title="Title")])
    with connect(config) as con:
        with transaction(con):
            other = ensure_track(con, artist="Other Artist", title="Other Title", status="wanted")
            ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=other["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:same",
                spotify_track_id="same",
                spotify_artist="Other Artist",
                spotify_title="Other Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.review == 1
    assert client.added == []
    with connect(config) as con:
        rows = list(con.execute("SELECT * FROM spotify_assets ORDER BY track_id"))
        event = con.execute("SELECT * FROM events WHERE event_type = 'spotify_candidate_already_linked'").fetchone()
        assert len(rows) == 2
        assert rows[0]["spotify_track_id"] == "same"
        assert rows[0]["in_playlist"] == 1
        assert rows[1]["status"] == "review"
        assert rows[1]["spotify_track_id"] is None
        assert event is not None


def test_spotify_rate_limit_cooldown_parses_retry_text(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS="7200")
    init_db(config)

    class TextRateLimitedSpotify(FakeSpotify):
        def search_track(self, artist, title):
            self.search_calls.append((artist, title))
            exc = RuntimeError("Your application has reached a rate/request limit. Retry will occur after: 120 s")
            exc.http_status = 429
            exc.headers = {}
            raise exc

    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    summary = sync_spotify(config, apply=True, client=TextRateLimitedSpotify())

    assert summary.rate_limited is True
    with connect(config) as con:
        value = con.execute("SELECT value FROM sync_state WHERE key = 'spotify_rate_limited_until'").fetchone()["value"]
        payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'spotify_rate_limited'").fetchone()["payload_json"])
    until = datetime.fromisoformat(value.replace("Z", "+00:00"))
    delta_seconds = (until - datetime.now(timezone.utc)).total_seconds()
    assert 60 <= delta_seconds <= 180
    assert payload["retry_after_seconds"] == 120
    assert payload["retry_after_source"] == "error_message"
    assert payload["fallback_used"] is False


def test_spotify_sync_adds_even_when_youtube_is_review(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    client = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_exists=False,
                match_confidence=None,
                status="review",
            )

    summary = sync_spotify(config, apply=True, client=client)

    assert summary.added == 1
    assert client.added == ["spotify:track:1"]
    with connect(config) as con:
        spotify = con.execute("SELECT * FROM spotify_assets WHERE in_playlist = 1").fetchone()
        youtube = con.execute("SELECT * FROM youtube_assets WHERE status = 'review'").fetchone()
    assert spotify is not None
    assert youtube is not None


def test_suspected_local_delete_is_held_out_of_spotify_and_youtube_sync(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="true")
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(config.music_dir / "Artist - Title.mp3"),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )
            con.execute("UPDATE youtube_assets SET suspected_missing_at = ? WHERE track_id = ?", ("2026-01-01T00:00:00Z", track["id"]))

    spotify = FakeSpotify(search_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    spotify_summary = sync_spotify(config, apply=True, client=spotify)

    assert spotify_summary.skipped == 1
    assert spotify.search_calls == []
    assert spotify.added == []

    class SearchingYouTube:
        def __init__(self):
            self.search_calls = []

        def search(self, artist, title):
            self.search_calls.append((artist, title))
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=abc123xyz",
                    video_id="abc123xyz",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            path = config.music_dir / "Artist - Title [abc123xyz].mp3"
            path.write_bytes(b"audio")
            return path

    youtube = SearchingYouTube()
    youtube_summary = sync_youtube(config, apply=True, client=youtube)

    assert youtube_summary.skipped == 1
    assert youtube.search_calls == []
    with connect(config) as con:
        spotify_event = con.execute("SELECT * FROM events WHERE event_type = 'spotify_skipped_suspected_local_delete'").fetchone()
        youtube_event = con.execute("SELECT * FROM events WHERE event_type = 'youtube_skipped_suspected_local_delete'").fetchone()
        assert spotify_event is not None
        assert youtube_event is not None


def test_youtube_sync_downloads_even_when_spotify_is_review(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="false")
    init_db(config)
    config.music_dir.mkdir()

    class DownloadingYouTube:
        def __init__(self):
            self.downloads = []

        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=abc123xyz",
                    video_id="abc123xyz",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate)
            path = config.music_dir / "Artist - Title [abc123xyz].mp3"
            path.write_bytes(b"audio")
            return path

    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                in_playlist=False,
                match_confidence=None,
                status="review",
            )

    client = DownloadingYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.downloaded == 1
    assert len(client.downloads) == 1
    with connect(config) as con:
        youtube = con.execute("SELECT * FROM youtube_assets WHERE file_exists = 1").fetchone()
        spotify = con.execute("SELECT * FROM spotify_assets WHERE status = 'review'").fetchone()
    assert youtube is not None
    assert spotify is not None


def test_reconcile_refuses_missing_music_dir_before_destructive_detection(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(config.music_dir / "missing.mp3"),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "1")

    summary = reconcile(config, apply=True)

    assert any(item.startswith("local:") for item in summary.refused)
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "wanted"


def test_reconcile_does_not_exclude_tracks_missing_never_created_assets(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "0")
            set_state(con, "last_spotify_playlist_count", "0")

    summary = reconcile(config, apply=True, spotify_client=FakeSpotify())

    assert summary.excluded_local == 0
    assert summary.excluded_spotify == 0
    assert summary.refused == []
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "wanted"


def test_reconcile_refuses_many_local_known_missing_even_when_replacement_files_keep_count(tmp_path):
    config = make_config(
        tmp_path,
        HCR_SPOTIFY_ENABLED="false",
        HCR_RECONCILE_MAX_EXCLUSIONS="1",
    )
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            for index in range(3):
                track = ensure_track(con, artist=f"Missing {index}", title="Song", status="wanted")
                upsert_youtube_asset(
                    con,
                    track_id=track["id"],
                    file_path=str(config.music_dir / f"missing-{index}.mp3"),
                    file_exists=True,
                    match_confidence=1.0,
                    status="downloaded",
                )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "3")
    for index in range(3):
        (config.music_dir / f"replacement-{index}.mp3").write_bytes(b"x")

    summary = reconcile(config, apply=True)

    assert "local: too many local exclusions would be detected without --force-mass-delete" in summary.refused
    assert summary.suspected_local == 0
    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM tracks WHERE status = 'excluded'").fetchone()["count"] == 0
        assert con.execute("SELECT COUNT(*) AS count FROM youtube_assets WHERE suspected_missing_at IS NOT NULL").fetchone()["count"] == 0


def test_reconcile_does_not_suspect_recent_self_added_spotify_asset(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:01:00Z",
            )
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "0")
            set_state(con, "last_spotify_scan_at", "2026-01-01T00:00:00Z")
            set_state(con, "local_baseline_complete", "true")

    summary = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=[]))

    assert summary.suspected_spotify == 0
    assert summary.planned == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["suspected_missing_at"] is None
        assert asset["in_playlist"] == 1


def test_reconcile_refuses_empty_spotify_snapshot_with_known_assets(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:00:00Z",
            )
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "0")
            set_state(con, "last_spotify_scan_at", "2026-01-01T01:00:00Z")
            set_state(con, "local_baseline_complete", "true")

    summary = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=[]))

    assert "spotify: spotify playlist snapshot is empty while DB has known playlist assets" in summary.refused
    assert summary.suspected_spotify == 0
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["suspected_missing_at"] is None


def test_reconcile_refuses_spotify_snapshot_without_track_identities(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:00:00Z",
            )
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "1")
            set_state(con, "last_spotify_scan_at", "2026-01-01T01:00:00Z")
            set_state(con, "local_baseline_complete", "true")

    snapshot_track = SpotifyTrack(uri="", track_id="", artist="", title="")
    summary = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=[snapshot_track]))

    assert "spotify: spotify playlist snapshot has no usable track identities" in summary.refused
    assert summary.suspected_spotify == 0
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["suspected_missing_at"] is None


def test_reconcile_refuses_many_spotify_known_missing_even_when_replacements_keep_count(tmp_path):
    config = make_config(tmp_path, HCR_RECONCILE_MAX_EXCLUSIONS="1")
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            for index in range(3):
                track = ensure_track(con, artist=f"Missing {index}", title="Song", status="wanted")
                upsert_spotify_asset(
                    con,
                    track_id=track["id"],
                    playlist_id="playlist",
                    spotify_track_uri=f"spotify:track:old-{index}",
                    spotify_track_id=f"old-{index}",
                    spotify_artist=f"Missing {index}",
                    spotify_title="Song",
                    in_playlist=True,
                    match_confidence=1.0,
                    status="added",
                    added_at="2026-01-01T00:00:00Z",
                )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "0")
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "3")
            set_state(con, "last_spotify_scan_at", "2026-01-01T01:00:00Z")

    replacements = [
        SpotifyTrack(uri=f"spotify:track:new-{index}", track_id=f"new-{index}", artist=f"New {index}", title="Song")
        for index in range(3)
    ]
    summary = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=replacements))

    assert "spotify: too many spotify exclusions would be detected without --force-mass-delete" in summary.refused
    assert summary.suspected_spotify == 0
    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM tracks WHERE status = 'excluded'").fetchone()["count"] == 0
        assert con.execute("SELECT COUNT(*) AS count FROM spotify_assets WHERE suspected_missing_at IS NOT NULL").fetchone()["count"] == 0


def test_reconcile_clears_spotify_removal_suspicion_when_track_is_seen(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:00:00Z",
            )
            con.execute("UPDATE spotify_assets SET suspected_missing_at = ? WHERE spotify_track_id = ?", ("2026-01-01T01:00:00Z", "1"))
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "1")
            set_state(con, "last_spotify_scan_at", "2026-01-01T02:00:00Z")
            set_state(con, "local_baseline_complete", "true")

    summary = reconcile(
        config,
        apply=True,
        spotify_client=FakeSpotify(snapshot_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")]),
    )

    assert summary.refused == []
    assert summary.planned == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        assert asset["suspected_missing_at"] is None
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "wanted"


def test_reconcile_ignores_lingering_non_audio_files_when_audio_scan_empty(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    stfolder = config.music_dir / ".stfolder"
    stfolder.mkdir()
    jpg = config.music_dir / "leftover.jpg"
    jpg.write_bytes(b"image")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(config.music_dir / "missing.mp3"),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "1")

    summary = reconcile(config, apply=True, spotify_client=FakeSpotify())

    assert "local: local audio scan is empty while DB has known local assets" in summary.refused
    assert stfolder.is_dir()
    assert jpg.exists()
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "wanted"


def test_reconcile_two_pass_local_delete_then_cascade(tmp_path):
    config = make_config(tmp_path, HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO="0.40")
    init_db(config)
    config.music_dir.mkdir()
    keep = config.music_dir / "Keep - Song.mp3"
    gone = config.music_dir / "Gone - Song.mp3"
    keep.write_bytes(b"x")
    gone.write_bytes(b"x")
    with connect(config) as con:
        with transaction(con):
            keep_track = ensure_track(con, artist="Keep", title="Song", status="wanted")
            gone_track = ensure_track(con, artist="Gone", title="Song", status="wanted")
            upsert_youtube_asset(con, track_id=keep_track["id"], file_path=str(keep), file_exists=True, match_confidence=1.0, status="downloaded")
            upsert_youtube_asset(con, track_id=gone_track["id"], file_path=str(gone), file_exists=True, match_confidence=1.0, status="downloaded")
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "2")
    gone.unlink()

    first = reconcile(config, apply=True, spotify_client=FakeSpotify())
    second = reconcile(config, apply=True, spotify_client=FakeSpotify())

    assert first.suspected_local == 1
    assert second.excluded_local == 1
    with connect(config) as con:
        statuses = {row["display_artist"]: row["status"] for row in con.execute("SELECT * FROM tracks")}
    assert statuses["Gone"] == "excluded"
    assert statuses["Keep"] == "wanted"


def test_reconcile_trashes_excluded_local_file_reintroduced_by_scan(tmp_path):
    config = make_config(tmp_path, HCR_SPOTIFY_ENABLED="false")
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / "Artist - Title.mp3"
    path.write_bytes(b"x")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            mark_excluded(con, track_id=track["id"], source="manual", reason="manual")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(path),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "1")

    summary = reconcile(config, apply=True)

    assert summary.local_trashed == 1
    assert not path.exists()
    assert any(config.trash_dir.iterdir())
    with connect(config) as con:
        track = con.execute("SELECT * FROM tracks").fetchone()
        asset = con.execute("SELECT * FROM youtube_assets").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type = 'local_file_moved_to_trash'").fetchone()
        last_local_count = con.execute("SELECT value FROM sync_state WHERE key = 'last_local_scan_count'").fetchone()["value"]
        assert track["status"] == "excluded"
        assert asset["status"] == "deleted"
        assert event is not None
        assert last_local_count == "0"


def test_excluded_track_keeps_pending_spotify_removal_until_client_available(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "0")
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "1")
            track_id = track["id"]

    manual_exclude(config, track_id=track_id, reason="manual", apply=True, spotify_client=None)

    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        deferred = con.execute("SELECT * FROM events WHERE event_type = 'spotify_removal_deferred_due_to_missing_client'").fetchone()
        assert asset["in_playlist"] == 1
        assert asset["status"] == "added"
        assert deferred is not None

    spotify = FakeSpotify(snapshot_tracks=[SpotifyTrack(uri="spotify:track:1", track_id="1", artist="Artist", title="Title")])
    summary = reconcile(config, apply=True, spotify_client=spotify)

    assert summary.spotify_removed == 1
    assert spotify.removed == ["spotify:track:1"]
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets").fetchone()
        removed = con.execute("SELECT * FROM events WHERE event_type = 'removed_from_spotify_due_to_exclusion'").fetchone()
        assert asset["in_playlist"] == 0
        assert asset["status"] == "removed"
        assert removed is not None


def test_reconcile_two_pass_spotify_remove_then_cascade_clears_suspicion(tmp_path):
    config = make_config(tmp_path, HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO="0.40")
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / "Artist - Title.mp3"
    path.write_bytes(b"x")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            keep_track = ensure_track(con, artist="Keep", title="Song", status="wanted")
            upsert_youtube_asset(con, track_id=track["id"], file_path=str(path), file_exists=True, match_confidence=1.0, status="downloaded")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:1",
                spotify_track_id="1",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:00:00Z",
            )
            upsert_spotify_asset(
                con,
                track_id=keep_track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:2",
                spotify_track_id="2",
                spotify_artist="Keep",
                spotify_title="Song",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:00:00Z",
            )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "1")
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "2")
            set_state(con, "last_spotify_scan_at", "2026-01-01T01:00:00Z")

    snapshot = [SpotifyTrack(uri="spotify:track:2", track_id="2", artist="Keep", title="Song")]
    first = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=snapshot))
    second = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=snapshot))

    assert first.suspected_spotify == 1
    assert second.excluded_spotify == 1
    assert second.local_trashed == 1
    assert not path.exists()
    with connect(config) as con:
        track = con.execute("SELECT * FROM tracks WHERE display_artist = 'Artist'").fetchone()
        keep_track = con.execute("SELECT * FROM tracks WHERE display_artist = 'Keep'").fetchone()
        spotify = con.execute("SELECT * FROM spotify_assets WHERE spotify_track_id = '1'").fetchone()
        keep_spotify = con.execute("SELECT * FROM spotify_assets WHERE spotify_track_id = '2'").fetchone()
        youtube = con.execute("SELECT * FROM youtube_assets").fetchone()
        spotify_removed_payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'spotify_removed_by_user'").fetchone()["payload_json"])
        trash_payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'local_file_moved_to_trash'").fetchone()["payload_json"])
        last_local_count = con.execute("SELECT value FROM sync_state WHERE key = 'last_local_scan_count'").fetchone()["value"]
        assert track["status"] == "excluded"
        assert keep_track["status"] == "wanted"
        assert spotify["status"] == "removed"
        assert spotify["in_playlist"] == 0
        assert spotify["suspected_missing_at"] is None
        assert keep_spotify["in_playlist"] == 1
        assert youtube["status"] == "deleted"
        assert last_local_count == "0"
        assert spotify_removed_payload["reason"] == "confirmed Spotify playlist removal; global exclusion cascade"
        assert trash_payload["reason"] == "local cascade after exclusion"
        assert trash_payload["delete_mode"] == "trash"


def test_reconcile_tentative_spotify_remove_does_not_exclude_or_trash_local(tmp_path):
    config = make_config(
        tmp_path,
        HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO="0.40",
        HCR_SPOTIFY_MATCH_THRESHOLD="1.01",
    )
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / "Artist - Title.mp3"
    path.write_bytes(b"x")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            keep_track = ensure_track(con, artist="Keep", title="Song", status="wanted")
            upsert_youtube_asset(con, track_id=track["id"], file_path=str(path), file_exists=True, match_confidence=1.0, status="downloaded")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:tentative",
                spotify_track_id="tentative",
                spotify_artist="Artist",
                spotify_title="Title",
                in_playlist=True,
                match_confidence=1.0,
                status="review",
                added_at="2026-01-01T00:00:00Z",
            )
            upsert_spotify_asset(
                con,
                track_id=keep_track["id"],
                playlist_id="playlist",
                spotify_track_uri="spotify:track:keep",
                spotify_track_id="keep",
                spotify_artist="Keep",
                spotify_title="Song",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
                added_at="2026-01-01T00:00:00Z",
            )
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "1")
            set_state(con, "spotify_baseline_complete", "true")
            set_state(con, "last_spotify_playlist_count", "2")
            set_state(con, "last_spotify_scan_at", "2026-01-01T01:00:00Z")

    snapshot = [SpotifyTrack(uri="spotify:track:keep", track_id="keep", artist="Keep", title="Song")]
    first = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=snapshot))
    second = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot_tracks=snapshot))

    assert first.suspected_spotify == 1
    assert second.tentative_spotify_removed == 1
    assert second.excluded_spotify == 0
    assert second.local_trashed == 0
    assert path.exists()
    with connect(config) as con:
        track = con.execute("SELECT * FROM tracks WHERE display_artist = 'Artist'").fetchone()
        spotify = con.execute("SELECT * FROM spotify_assets WHERE spotify_track_id = 'tentative'").fetchone()
        youtube = con.execute("SELECT * FROM youtube_assets").fetchone()
        event_payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type = 'spotify_tentative_removed_by_user'").fetchone()["payload_json"])
        assert track["status"] == "wanted"
        assert spotify["status"] == "removed"
        assert spotify["in_playlist"] == 0
        assert spotify["suspected_missing_at"] is None
        assert youtube["status"] == "downloaded"
        assert youtube["file_exists"] == 1
        assert con.execute("SELECT COUNT(*) AS count FROM exclusions").fetchone()["count"] == 0
        assert event_payload["reason"] == "tentative Spotify match removed; track remains wanted"


def test_manual_exclude_leaves_non_audio_asset_path_untouched(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    jpg = config.music_dir / "leftover.jpg"
    jpg.write_bytes(b"image")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(jpg),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )
            track_id = track["id"]

    summary = manual_exclude(config, track_id=track_id, reason="manual", apply=True, spotify_client=FakeSpotify())

    assert summary.local_trashed == 0
    assert jpg.exists()
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type = 'local_file_left_unmanaged'").fetchone()
        assert asset["file_exists"] == 0
        assert event is not None


def test_manual_exclude_moves_local_file_to_trash(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / "Artist - Title.mp3"
    path.write_bytes(b"x")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(con, track_id=track["id"], file_path=str(path), file_exists=True, match_confidence=1.0, status="downloaded")
            track_id = track["id"]

    summary = manual_exclude(config, track_id=track_id, reason="manual", apply=True, spotify_client=FakeSpotify())

    assert summary.local_trashed == 1
    assert not path.exists()
    assert any(config.trash_dir.iterdir())
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "excluded"


def test_legacy_downloader_preflight_blocks_apply(monkeypatch, tmp_path):
    config = make_config(tmp_path, HCR_LEGACY_DOWNLOADER_UNITS="old-downloader.service")
    monkeypatch.setattr("hcr_sync.system.active_user_units", lambda units: ["old-downloader.service"])

    with pytest.raises(LegacyDownloaderActive):
        assert_legacy_downloader_safe(config)
