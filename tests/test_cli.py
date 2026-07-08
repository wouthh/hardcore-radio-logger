from pathlib import Path
from types import SimpleNamespace

import pytest

from hcr_sync.cli import cmd_exclude, cmd_run_once, fatal_reconcile_refusals
from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, init_db
from hcr_sync.reconcile import ReconcileSummary
from hcr_sync.system import LegacyDownloaderActive


def make_config(tmp_path: Path, **overrides: str) -> Config:
    values = dict(DEFAULTS)
    values.update(
        {
            "HCR_DB_PATH": str(tmp_path / "hcr_music.db"),
            "HCR_MUSIC_DIR": str(tmp_path / "music"),
            "HCR_TRASH_DIR": str(tmp_path / "music" / ".hcr-trash"),
            "HCR_SPOTIFY_PLAYLIST_ID": "playlist",
            "HCR_SPOTIFY_TOKEN_CACHE": str(tmp_path / "token-cache"),
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


def test_spotify_reconcile_refusals_are_nonfatal_for_run_once():
    assert fatal_reconcile_refusals(["spotify: playlist fetch failed"]) == []


def test_local_reconcile_refusals_remain_fatal_for_run_once():
    assert fatal_reconcile_refusals(["local: music dir does not exist"]) == ["local: music dir does not exist"]


def test_exclude_dry_run_with_artist_title_does_not_create_track(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    args = SimpleNamespace(track_id=None, artist="Artist", title="Title", reason="manual", apply=False)

    assert cmd_exclude(args, config) == 0

    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) AS count FROM tracks").fetchone()["count"] == 0


def test_run_once_preflights_legacy_downloader_before_mutating(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls = []

    def fail_preflight(_config):
        raise LegacyDownloaderActive("legacy downloader active")

    monkeypatch.setattr("hcr_sync.cli.assert_legacy_downloader_safe", fail_preflight)
    monkeypatch.setattr("hcr_sync.cli.import_logger", lambda *_args, **_kwargs: calls.append("import_logger"))
    args = SimpleNamespace(apply=True, force_mass_delete=False, force_confirm_deletions=False, complete_idless_local=None)

    with pytest.raises(LegacyDownloaderActive):
        cmd_run_once(args, config)

    assert calls == []


def test_run_once_apply_skips_external_sync_after_local_reconcile_refusal(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    external_calls = []

    monkeypatch.setattr("hcr_sync.cli.assert_legacy_downloader_safe", lambda _config: None)
    monkeypatch.setattr("hcr_sync.cli.import_logger", lambda *_args, **_kwargs: SimpleNamespace(files_read=0))
    monkeypatch.setattr("hcr_sync.cli.import_local_files", lambda *_args, **_kwargs: SimpleNamespace(files_seen=0))
    monkeypatch.setattr("hcr_sync.cli.scan_spotify_playlist", lambda *_args, **_kwargs: SimpleNamespace(skipped=1))
    monkeypatch.setattr(
        "hcr_sync.cli.reconcile",
        lambda *_args, **_kwargs: ReconcileSummary(refused=["local: music dir does not exist"]),
    )
    monkeypatch.setattr("hcr_sync.cli.sync_youtube", lambda *_args, **_kwargs: external_calls.append("youtube"))
    monkeypatch.setattr("hcr_sync.cli.sync_spotify", lambda *_args, **_kwargs: external_calls.append("spotify"))
    monkeypatch.setattr("hcr_sync.cli.build_report", lambda _config: SimpleNamespace())
    monkeypatch.setattr("hcr_sync.cli.format_report", lambda _report: "report")
    args = SimpleNamespace(apply=True, force_mass_delete=False, force_confirm_deletions=False, complete_idless_local=None)

    assert cmd_run_once(args, config) == 1
    assert external_calls == []


def test_run_once_continues_after_spotify_prescan_failure(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    external_calls = []

    monkeypatch.setattr("hcr_sync.cli.assert_legacy_downloader_safe", lambda _config: None)
    monkeypatch.setattr("hcr_sync.cli.import_logger", lambda *_args, **_kwargs: SimpleNamespace(files_read=0))
    monkeypatch.setattr("hcr_sync.cli.import_local_files", lambda *_args, **_kwargs: SimpleNamespace(files_seen=0))

    def fail_spotify_scan(*_args, **_kwargs):
        raise RuntimeError("temporary spotify failure")

    monkeypatch.setattr("hcr_sync.cli.scan_spotify_playlist", fail_spotify_scan)
    monkeypatch.setattr("hcr_sync.cli.reconcile", lambda *_args, **_kwargs: ReconcileSummary())
    monkeypatch.setattr("hcr_sync.cli.sync_youtube", lambda *_args, **_kwargs: external_calls.append("youtube") or SimpleNamespace(downloaded=0))
    monkeypatch.setattr("hcr_sync.cli.sync_spotify", lambda *_args, **_kwargs: external_calls.append("spotify") or SimpleNamespace(added=0))
    monkeypatch.setattr("hcr_sync.cli.build_report", lambda _config: SimpleNamespace())
    monkeypatch.setattr("hcr_sync.cli.format_report", lambda _report: "report")
    args = SimpleNamespace(apply=True, force_mass_delete=False, force_confirm_deletions=False, complete_idless_local=None)

    assert cmd_run_once(args, config) == 0
    assert external_calls == ["youtube", "spotify"]
