"""System safety helpers."""

from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .config import Config, list_value


class LegacyDownloaderActive(RuntimeError):
    pass


def active_user_units(units: list[str]) -> list[str]:
    active: list[str] = []
    for unit in units:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            active.append(unit)
    return active


def assert_legacy_downloader_safe(config: Config) -> None:
    if config.bool("HCR_ALLOW_LEGACY_DOWNLOADER_RUNNING"):
        return
    units = list_value(config.get("HCR_LEGACY_DOWNLOADER_UNITS"))
    active = active_user_units(units) if units else []
    if active:
        raise LegacyDownloaderActive(
            "configured legacy downloader unit(s) are active: " + ", ".join(active)
        )


@contextmanager
def sync_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
