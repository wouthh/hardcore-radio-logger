"""Run the sync outside a VPN tunnel through the VPN's own split-tunnel launcher.

Spotify and YouTube often throttle or block VPN exit addresses. Split tunneling is
per process, so the whole sync is relaunched under the launcher before any provider
traffic starts. Without a usable VPN the sync simply runs on the default route.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# name: (command that succeeds only while the VPN daemon runs, launcher prefix)
PROVIDERS: dict[str, tuple[list[str], list[str]]] = {
    "mullvad": (["mullvad", "status"], ["mullvad-exclude"]),
}
OFF = {"off", "false", "none", "0"}
ACTIVE_MARKER = "HCR_VPN_BYPASS_ACTIVE"


def _usable(name: str, *, which=shutil.which, run=subprocess.run) -> bool:
    status, launcher = PROVIDERS[name]
    if not which(status[0]) or not which(launcher[0]):
        return False
    # The launcher can exec fine yet fail to enter the split tunnel (no cgroup support or
    # permission, daemon mismatch) and exit without running the sync, so probe it first.
    probe = [*launcher, sys.executable, "-c", ""]
    try:
        return all(
            run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False).returncode == 0
            for command in (status, probe)
        )
    except (OSError, subprocess.SubprocessError):
        return False


def select_provider(mode: str, *, which=shutil.which, run=subprocess.run) -> str | None:
    """Return the provider to bypass through, or None to stay on the default route."""
    mode = (mode or "auto").strip().lower()
    if mode in OFF:
        return None
    if mode != "auto" and mode not in PROVIDERS:
        raise ValueError(f"HCR_VPN_BYPASS must be auto, off, or one of: {', '.join(sorted(PROVIDERS))}")
    candidates = list(PROVIDERS) if mode == "auto" else [mode]
    return next((name for name in candidates if _usable(name, which=which, run=run)), None)


def relaunch_outside_tunnel(mode: str, argv: list[str]) -> None:
    """Replace this process with argv under the VPN launcher; return to run here instead."""
    if os.environ.get(ACTIVE_MARKER):
        return
    try:
        name = select_provider(mode)
    except ValueError as exc:
        print(f"WARNING vpn_bypass {exc}; running on the default route", file=sys.stderr)
        return
    if name is None:
        if (mode or "auto").strip().lower() not in OFF:
            print(f"vpn_bypass mode={mode or 'auto'} no running VPN detected; running on the default route", file=sys.stderr)
        return
    command = [*PROVIDERS[name][1], *argv]
    print(f"vpn_bypass provider={name}", file=sys.stderr, flush=True)
    try:
        os.execvpe(command[0], command, {**os.environ, ACTIVE_MARKER: name})
    except OSError as exc:
        print(f"WARNING vpn_bypass provider={name} launch failed ({exc}); running on the default route", file=sys.stderr)
