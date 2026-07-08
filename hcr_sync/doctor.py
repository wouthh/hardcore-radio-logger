"""Non-destructive environment checks."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, list_value
from .system import active_user_units


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False


@dataclass
class DoctorResult:
    config_file: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok or check.warning for check in self.checks)


def _git_check_ignore(path: str) -> bool | None:
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if git_dir.returncode != 0:
        return None
    result = subprocess.run(["git", "check-ignore", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _recent(path: Path, max_age_seconds: int = 3600) -> bool:
    if not path.exists():
        return False
    return (Path.cwd().stat().st_mtime if False else __import__("time").time()) - path.stat().st_mtime <= max_age_seconds


def run_doctor(config: Config) -> DoctorResult:
    result = DoctorResult(config_file=config.loaded_config_label)
    db_parent = config.db_path.parent
    result.checks.append(Check("DB path", db_parent.exists() and os.access(db_parent, os.W_OK), str(config.db_path)))
    result.checks.append(Check("Music dir", config.music_dir.exists() and os.access(config.music_dir, os.R_OK), str(config.music_dir)))
    trash_parent = config.trash_dir.parent
    result.checks.append(Check("Trash dir", trash_parent.exists() and os.access(trash_parent, os.W_OK), str(config.trash_dir)))

    seen_exists = config.seen_tracks_path.exists()
    played_exists = config.played_tracks_path.exists()
    logger_detail = "configured logger files exist" if (seen_exists or played_exists) else "configured logger files are missing"
    result.checks.append(Check("Logger input files", seen_exists or played_exists, logger_detail, warning=not (seen_exists or played_exists)))
    if seen_exists or played_exists:
        fresh = any(_recent(path) for path in [config.seen_tracks_path, config.played_tracks_path] if path.exists())
        result.checks.append(Check("Logger input freshness", fresh, "logger files changed recently", warning=not fresh))

    producer_ok = config.bool("HCR_RUN_POLLER") or bool(config.get("HCR_LEGACY_LOGGER_COMMAND")) or bool(list_value(config.get("HCR_LEGACY_LOGGER_UNITS")))
    result.checks.append(
        Check(
            "Logger producer",
            producer_ok,
            "built-in poller enabled or external producer configured",
            warning=not producer_ok,
        )
    )

    playlist_id = bool(config.get("HCR_SPOTIFY_PLAYLIST_ID"))
    result.checks.append(Check("Spotify playlist ID", playlist_id, "configured", warning=not playlist_id))
    token_cache = config.path("HCR_SPOTIFY_TOKEN_CACHE")
    result.checks.append(
        Check(
            "Spotify token cache",
            token_cache.exists(),
            "OK" if token_cache.exists() else "missing, run `python -m hcr_sync spotify auth`",
            warning=not token_cache.exists(),
        )
    )

    active_downloaders = active_user_units(list_value(config.get("HCR_LEGACY_DOWNLOADER_UNITS")))
    allowed = config.bool("HCR_ALLOW_LEGACY_DOWNLOADER_RUNNING")
    result.checks.append(
        Check(
            "Legacy downloader preflight",
            not active_downloaders or allowed,
            "active: " + ", ".join(active_downloaders) if active_downloaders else "no configured legacy downloader active",
            warning=bool(active_downloaders and allowed),
        )
    )

    rendered_dir = Path("systemd/rendered")
    rendered_ok = True
    if rendered_dir.exists():
        rendered_ok = not any("{{" in path.read_text(encoding="utf-8", errors="ignore") for path in rendered_dir.glob("*"))
    result.checks.append(Check("Rendered systemd placeholders", rendered_ok, "no unresolved placeholders"))

    for path in [".env", "hcr_music.db", "hcr_music.db-wal", "seen-tracks.jsonl", "played-tracks.tsv", "youtube-download-catalog.jsonl", "youtube-download-success.tsv"]:
        ignored = _git_check_ignore(path)
        if ignored is not None:
            result.checks.append(Check(f"gitignore {path}", ignored, "ignored" if ignored else "not ignored"))

    return result


def format_doctor(result: DoctorResult) -> str:
    lines = [f"Config file: {result.config_file}"]
    for check in result.checks:
        status = "OK" if check.ok else ("WARN" if check.warning else "FAIL")
        lines.append(f"{check.name}: {status} - {check.detail}")
    return "\n".join(lines)
