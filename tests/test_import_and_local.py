import json
from pathlib import Path

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, init_db, mark_excluded, transaction, upsert_youtube_asset
from hcr_sync.identity import parse_artist_title
from hcr_sync.local_files import import_local_files, inspect_audio_file
from hcr_sync.logger_importer import import_logger


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
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


def test_logger_import_is_idempotent_and_creates_wanted_track(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.seen_tracks_path.write_text(
        json.dumps({"first_seen_at": "2026-01-01T00:00:00Z", "track": "Artist - Title"}) + "\n",
        encoding="utf-8",
    )

    first = import_logger(config, apply=True)
    second = import_logger(config, apply=True)

    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM tracks").fetchone()["count"] == 1
        assert con.execute("SELECT COUNT(*) AS count FROM radio_observations").fetchone()["count"] == 1
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "wanted"
        assert con.execute("SELECT value FROM sync_state WHERE key = 'last_seen_tracks_jsonl_size'").fetchone()
    assert first.observations_added == 1
    assert second.observations_added == 0


def test_logger_import_does_not_reactivate_excluded_track(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.played_tracks_path.write_text("2026-01-01T00:00:00Z\tArtist - Title\n", encoding="utf-8")
    with connect(config) as con:
        with transaction(con):
            artist, title = parse_artist_title("Artist - Title")
            from hcr_sync.db import ensure_track

            track = ensure_track(con, artist=artist, title=title, status="wanted")
            mark_excluded(con, track_id=track["id"], source="manual", reason="manual")

    summary = import_logger(config, apply=True)

    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks").fetchone()["status"] == "excluded"
        assert con.execute("SELECT COUNT(*) AS count FROM radio_observations").fetchone()["count"] == 1
    assert summary.skipped_excluded == 1


def test_local_backfill_imports_existing_file_and_sets_baseline(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    (config.music_dir / "Artist - Title [abc123xyz].mp3").write_bytes(b"not really audio")

    summary = import_local_files(config, apply=True, establish_baseline=True)

    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM tracks").fetchone()["count"] == 1
        asset = con.execute("SELECT * FROM youtube_assets").fetchone()
        assert asset["file_exists"] == 1
        assert asset["youtube_video_id"] == "abc123xyz"
        assert con.execute("SELECT value FROM sync_state WHERE key = 'local_baseline_complete'").fetchone()["value"] == "true"
    assert summary.baseline_complete is True


def test_inspect_audio_file_prefers_download_filename_over_bad_tags_for_video_id(tmp_path, monkeypatch):
    path = tmp_path / "Angerfist - Gathering Of Gods [Extended Mix] [Y4tgndTal5w].mp3"
    path.write_bytes(b"not really audio")
    monkeypatch.setattr("hcr_sync.local_files._tag_values", lambda _path: ("Rob-In", "Angerfist - Gathering Of Gods [Extended Mix]"))

    item = inspect_audio_file(path)

    assert item is not None
    assert item.artist == "Angerfist"
    assert item.title == "Gathering Of Gods [Extended Mix]"
    assert item.youtube_video_id == "Y4tgndTal5w"


def test_inspect_audio_file_prefers_en_dash_download_filename_over_bad_tags(tmp_path, monkeypatch):
    path = tmp_path / "Drokz – The Mind (Signs Of Life) [JrNBkozgrsY].mp3"
    path.write_bytes(b"not really audio")
    monkeypatch.setattr("hcr_sync.local_files._tag_values", lambda _path: ("Davide Montana", "Drokz – The Mind (Signs Of Life)"))

    item = inspect_audio_file(path)

    assert item is not None
    assert item.artist == "Drokz"
    assert item.title == "The Mind (Signs Of Life)"
    assert item.youtube_video_id == "JrNBkozgrsY"


def test_local_scan_reuses_existing_asset_by_youtube_id_instead_of_tags(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / "Binary Code [z0cgqlrs7_U].mp3"
    path.write_bytes(b"not really audio")
    monkeypatch.setattr("hcr_sync.local_files._tag_values", lambda _path: ("My Ear Bleeds Wine, Dan Marsh", "Binary Code"))
    with connect(config) as con:
        with transaction(con):
            from hcr_sync.db import ensure_track

            track = ensure_track(con, artist="My Ear Bleeds Wine", title="Binary Code", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                youtube_video_id="z0cgqlrs7_U",
                file_path=str(path),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
                downloaded_at="2026-01-01T00:00:00Z",
            )

    import_local_files(config, apply=True, establish_baseline=False)

    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM tracks").fetchone()["count"] == 1
        assert con.execute("SELECT COUNT(*) AS count FROM youtube_assets").fetchone()["count"] == 1
        asset = con.execute("SELECT * FROM youtube_assets").fetchone()
        assert asset["track_id"] == 1
