from pathlib import Path

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_spotify_asset, upsert_youtube_asset
from hcr_sync.reconcile import manual_exclude, reconcile
from hcr_sync.spotify_sync import PlaylistSnapshot, SpotifyTrack, backfill_spotify, sync_spotify
from hcr_sync.system import LegacyDownloaderActive, assert_legacy_downloader_safe


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
