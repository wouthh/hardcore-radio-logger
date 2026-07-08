"""Configuration loading for hcr_sync."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

SPOTIFY_SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
)

DEFAULT_CONFIG_PATH = Path("~/.config/hcr-sync/hcr-sync.env").expanduser()

DEFAULTS: dict[str, str] = {
    "HCR_DB_PATH": "./hcr_music.db",
    "HCR_MUSIC_DIR": "./music",
    "HCR_DELETE_MODE": "trash",
    "HCR_TRASH_DIR": "./music/.hcr-trash",
    "HCR_SEEN_TRACKS_JSONL": "./seen-tracks.jsonl",
    "HCR_PLAYED_TRACKS_TSV": "./played-tracks.tsv",
    "HCR_LOGGER_LOCK_FILE": "./.hcr-track-logger.lock",
    "HCR_STREAM_URL": "https://stream.hardcoreradio.nl:9000/hcr.ogg",
    "HCR_STATUS_URL": "",
    "HCR_RUN_POLLER": "false",
    "HCR_LEGACY_LOGGER_COMMAND": "",
    "HCR_LEGACY_LOGGER_UNITS": "",
    "HCR_LEGACY_DOWNLOADER_UNITS": "",
    "HCR_ALLOW_LEGACY_DOWNLOADER_RUNNING": "false",
    "HCR_SPOTIFY_PLAYLIST_ID": "",
    "HCR_SPOTIFY_ENABLED": "true",
    "HCR_SPOTIFY_CLIENT_ID": "",
    "HCR_SPOTIFY_CLIENT_SECRET": "",
    "HCR_SPOTIFY_REDIRECT_URI": "http://127.0.0.1:9090",
    "HCR_SPOTIFY_TOKEN_CACHE": "~/.config/hcr-sync/spotipy-token-cache",
    "HCR_SPOTIFY_SYNC_LIMIT": "15",
    "HCR_SPOTIFY_REQUEST_TIMEOUT": "10",
    "HCR_SPOTIFY_REQUEST_RETRIES": "0",
    "HCR_SPOTIFY_STATUS_RETRIES": "0",
    "HCR_SPOTIFY_ADD_REVIEW_MATCHES": "true",
    "HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD": "0.85",
    "HCR_YTDLP_BIN": "yt-dlp",
    "HCR_NODE_RUNTIME": "",
    "HCR_YOUTUBE_DOWNLOAD_ARCHIVE": "./youtube-download-archive.txt",
    "HCR_DOWNLOAD_TMP_DIR": "./download-tmp",
    "HCR_YOUTUBE_COMPLETE_IDLESS_LOCAL": "false",
    "HCR_YOUTUBE_MATCH_THRESHOLD": "0.90",
    "HCR_SPOTIFY_MATCH_THRESHOLD": "0.90",
    "HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO": "0.60",
    "HCR_RECONCILE_REQUIRE_TWO_PASSES": "true",
    "HCR_RECONCILE_MAX_EXCLUSIONS": "25",
}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                values[key] = shlex.split(value, comments=True)[0]
                continue
            except (IndexError, ValueError):
                pass
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def list_value(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", " ").split() if part.strip()]


def path_value(value: str) -> Path:
    return Path(value).expanduser()


@dataclass(frozen=True)
class Config:
    values: dict[str, str]
    loaded_files: list[Path] = field(default_factory=list)

    def get(self, key: str) -> str:
        return self.values.get(key, DEFAULTS.get(key, ""))

    def bool(self, key: str) -> bool:
        return truthy(self.get(key))

    def float(self, key: str) -> float:
        return float(self.get(key))

    def int(self, key: str) -> int:
        return int(self.get(key))

    def path(self, key: str) -> Path:
        return path_value(self.get(key))

    @property
    def db_path(self) -> Path:
        return self.path("HCR_DB_PATH")

    @property
    def music_dir(self) -> Path:
        return self.path("HCR_MUSIC_DIR")

    @property
    def trash_dir(self) -> Path:
        return self.path("HCR_TRASH_DIR")

    @property
    def seen_tracks_path(self) -> Path:
        return self.path("HCR_SEEN_TRACKS_JSONL")

    @property
    def played_tracks_path(self) -> Path:
        return self.path("HCR_PLAYED_TRACKS_TSV")

    @property
    def logger_lock_path(self) -> Path:
        return self.path("HCR_LOGGER_LOCK_FILE")

    @property
    def sync_lock_path(self) -> Path:
        return self.db_path.with_name(".hcr-sync.lock")

    @property
    def loaded_config_label(self) -> str:
        if not self.loaded_files:
            return "(none)"
        return ", ".join(str(path) for path in self.loaded_files)

    @property
    def spotify_scopes(self) -> str:
        return " ".join(SPOTIFY_SCOPES)


def _candidate_files(explicit_config: str | None) -> list[Path]:
    candidates: list[Path] = []
    if DEFAULT_CONFIG_PATH.exists():
        candidates.append(DEFAULT_CONFIG_PATH)
    local_env = Path(".env")
    if local_env.exists():
        candidates.append(local_env)
    if os.environ.get("HCR_CONFIG_FILE"):
        candidates.append(path_value(os.environ["HCR_CONFIG_FILE"]))
    if explicit_config:
        candidates.append(path_value(explicit_config))
    return candidates


def load_config(explicit_config: str | None = None) -> Config:
    values = dict(DEFAULTS)
    loaded_files: list[Path] = []
    for path in _candidate_files(explicit_config):
        if path.exists():
            values.update(parse_env_file(path))
            loaded_files.append(path.resolve())
    for key in set(DEFAULTS) | {key for key in os.environ if key.startswith("HCR_")}:
        if key in os.environ:
            values[key] = os.environ[key]
    return Config(values=values, loaded_files=loaded_files)
