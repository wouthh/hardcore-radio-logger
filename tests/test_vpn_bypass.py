"""VPN split-tunnel relaunch selects a running provider and otherwise falls back."""
import subprocess
import sys

import pytest

from hcr_sync import vpn


def fake_run(returncode):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if returncode is None:
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(command, returncode)
    return run, calls


def installed(name):
    return f"/usr/bin/{name}"


def test_auto_selects_running_mullvad_and_off_skips_detection():
    run, calls = fake_run(0)
    assert vpn.select_provider("auto", which=installed, run=run) == "mullvad"
    assert vpn.select_provider("Mullvad", which=installed, run=run) == "mullvad"
    assert calls == [["mullvad", "status"], ["mullvad-exclude", sys.executable, "-c", ""]] * 2
    assert vpn.select_provider("off", which=installed, run=run) is None
    assert len(calls) == 4


@pytest.mark.parametrize("returncode", [1, None])
def test_stopped_or_hanging_daemon_falls_back(returncode):
    run, _ = fake_run(returncode)
    assert vpn.select_provider("auto", which=installed, run=run) is None


def test_launcher_that_cannot_enter_split_tunnel_falls_back():
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[0] == "mullvad-exclude" else 0)
    assert vpn.select_provider("auto", which=installed, run=run) is None


def test_missing_launcher_falls_back_without_running_status():
    run, calls = fake_run(0)
    assert vpn.select_provider("auto", which=lambda name: None if name == "mullvad-exclude" else installed(name), run=run) is None
    assert calls == []


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="HCR_VPN_BYPASS"):
        vpn.select_provider("othervpn", which=installed, run=fake_run(0)[0])


def test_relaunch_execs_launcher_once_with_marker(monkeypatch):
    execs = []
    monkeypatch.delenv(vpn.ACTIVE_MARKER, raising=False)
    monkeypatch.setattr(vpn, "select_provider", lambda mode: "mullvad")
    monkeypatch.setattr(vpn.os, "execvpe", lambda file, args, env: execs.append((file, args, env[vpn.ACTIVE_MARKER])))
    vpn.relaunch_outside_tunnel("auto", ["python", "-m", "hcr_sync", "run-once", "--apply"])
    assert execs == [("mullvad-exclude", ["mullvad-exclude", "python", "-m", "hcr_sync", "run-once", "--apply"], "mullvad")]
    monkeypatch.setenv(vpn.ACTIVE_MARKER, "mullvad")
    vpn.relaunch_outside_tunnel("auto", ["python"])
    assert len(execs) == 1


def test_failed_launch_continues_on_default_route(monkeypatch, capsys):
    monkeypatch.delenv(vpn.ACTIVE_MARKER, raising=False)
    monkeypatch.setattr(vpn, "select_provider", lambda mode: "mullvad")

    def fail(*args):
        raise OSError("synthetic")
    monkeypatch.setattr(vpn.os, "execvpe", fail)
    vpn.relaunch_outside_tunnel("auto", ["python"])
    assert "launch failed" in capsys.readouterr().err


def test_cli_main_only_relaunches_when_asked(monkeypatch, tmp_path):
    from hcr_sync import cli
    seen = []
    monkeypatch.setattr(vpn, "relaunch_outside_tunnel", lambda mode, argv: seen.append(mode))
    monkeypatch.setenv("HCR_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("HCR_VPN_BYPASS", "auto")
    cli.main(["report"])
    assert seen == []
    cli.main(["report"], vpn_bypass=True)
    assert seen == ["auto"]
