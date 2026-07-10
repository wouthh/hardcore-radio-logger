from pathlib import Path
import sqlite3

from hcr_sync.config import DEFAULTS, Config, load_config, parse_env_file
from hcr_sync.db import connect, init_db
from hcr_sync.doctor import format_doctor, run_doctor


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
            "HCR_SPOTIFY_PLAYLIST_ID": "playlist",
            "HCR_RUN_POLLER": "true",
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


def test_config_precedence_uses_config_file_then_environment(tmp_path, monkeypatch):
    config_file = tmp_path / "hcr-sync.env"
    config_file.write_text('HCR_DB_PATH="from-file.db"\nHCR_RUN_POLLER=true\n', encoding="utf-8")
    monkeypatch.setenv("HCR_DB_PATH", "from-env.db")

    config = load_config(str(config_file))

    assert str(config.db_path) == "from-env.db"
    assert config.bool("HCR_RUN_POLLER") is True
    assert config.loaded_files[-1] == config_file.resolve()


def test_env_parser_keeps_unquoted_paths_with_spaces(tmp_path):
    config_file = tmp_path / "hcr-sync.env"
    config_file.write_text("HCR_MUSIC_DIR=/tmp/Pixel 6a/Music\nHCR_TRASH_DIR='/tmp/Trash Folder'\n", encoding="utf-8")

    values = parse_env_file(config_file)

    assert values["HCR_MUSIC_DIR"] == "/tmp/Pixel 6a/Music"
    assert values["HCR_TRASH_DIR"] == "/tmp/Trash Folder"


def test_doctor_prints_config_file_and_is_non_destructive(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    config_file = tmp_path / "hcr-sync.env"
    config_file.write_text("", encoding="utf-8")
    config = make_config(tmp_path)
    object.__setattr__(config, "loaded_files", [config_file])
    init_db(config)

    result = run_doctor(config)
    output = format_doctor(result)

    assert f"Config file: {config_file}" in output
    assert not config.trash_dir.exists()


def test_connect_migrates_existing_spotify_search_schedule_columns(tmp_path):
    config = make_config(tmp_path)
    con = sqlite3.connect(config.db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_artist TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            display_artist TEXT NOT NULL,
            display_title TEXT NOT NULL,
            canonical_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('wanted', 'excluded', 'review', 'missing', 'error')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE spotify_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE RESTRICT,
            spotify_track_uri TEXT,
            spotify_track_id TEXT,
            spotify_artist TEXT NOT NULL DEFAULT '',
            spotify_title TEXT NOT NULL DEFAULT '',
            playlist_id TEXT NOT NULL DEFAULT '',
            in_playlist INTEGER NOT NULL DEFAULT 0,
            match_confidence REAL,
            status TEXT NOT NULL CHECK (status IN ('added', 'missing', 'removed', 'error', 'review')),
            added_at TEXT,
            last_seen_at TEXT,
            suspected_missing_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(track_id, playlist_id),
            UNIQUE(playlist_id, spotify_track_id)
        );
        INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z');
        INSERT INTO tracks(
            normalized_artist, normalized_title, display_artist, display_title, canonical_key, status, created_at, updated_at
        ) VALUES ('artist', 'missing', 'Artist', 'Missing', 'artist::missing', 'wanted', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        INSERT INTO spotify_assets(
            track_id, playlist_id, in_playlist, match_confidence, status, created_at, updated_at
        ) VALUES (1, 'playlist', 0, 0.1, 'review', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
        """
    )
    con.commit()
    con.close()

    with connect(config) as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(spotify_assets)")}
        asset = migrated.execute("SELECT * FROM spotify_assets").fetchone()
        version = migrated.execute("SELECT * FROM schema_migrations WHERE version = 2").fetchone()

    assert {"search_last_at", "search_attempts", "search_next_at"} <= columns
    assert asset["search_last_at"] == "2026-01-02T00:00:00Z"
    assert asset["search_attempts"] == 2
    assert asset["search_next_at"] == "2026-01-16T00:00:00Z"
    assert version is not None
