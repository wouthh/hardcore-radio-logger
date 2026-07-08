from pathlib import Path

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_spotify_asset, upsert_youtube_asset
from hcr_sync.reconcile import manual_exclude, reconcile
from hcr_sync.spotify_sync import PlaylistSnapshot, SpotifyTrack, backfill_spotify, sync_spotify
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

    def auth_check(self):
        return "fake-user"

    def playlist_snapshot(self, playlist_id):
        return PlaylistSnapshot(playlist_id=playlist_id, snapshot_id="snap", tracks=self.snapshot_tracks, complete=True)

    def search_track(self, artist, title):
        return self.search_tracks

    def add_tracks(self, playlist_id, uris):
        self.added.extend(uris)

    def remove_tracks(self, playlist_id, uris):
        self.removed.extend(uris)


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
    config = make_config(tmp_path)
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
