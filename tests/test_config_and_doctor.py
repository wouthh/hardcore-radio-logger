from pathlib import Path

from hcr_sync.config import DEFAULTS, Config, load_config, parse_env_file
from hcr_sync.db import init_db
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
