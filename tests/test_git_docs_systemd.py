import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_runtime_files_and_fixture_exceptions():
    ignored = [
        ".env",
        "hcr_music.db",
        "hcr_music.db-wal",
        "seen-tracks.jsonl",
        "played-tracks.tsv",
        "youtube-download-catalog.jsonl",
        "youtube-download-success.tsv",
        "hcr-sync.log",
        ".spotipy-cache",
        "LOCAL_MIGRATION.md",
        ".local-migration/example.txt",
        "migration-snapshot/example.jsonl",
        "legacy-archive/example.tsv",
    ]
    for path in ignored:
        result = subprocess.run(["git", "check-ignore", path], cwd=ROOT, check=False)
        assert result.returncode == 0, path
    for path in ["tests/fixtures/example.jsonl", "tests/fixtures/example.tsv"]:
        result = subprocess.run(["git", "check-ignore", path], cwd=ROOT, check=False)
        assert result.returncode != 0, path


def test_docs_do_not_contain_machine_specific_paths_or_private_runbook_title():
    for name in ["README.md", "AGENTS.md"]:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "/home/" not in text
        assert "Migration from existing running service" not in text
        assert "detected OLD_TIMER" not in text


def test_systemd_templates_are_templates_not_rendered_local_units():
    service = (ROOT / "systemd/user/hcr-sync.service.in").read_text(encoding="utf-8")
    timer = (ROOT / "systemd/user/hcr-sync.timer").read_text(encoding="utf-8")
    assert "{{PROJECT_DIR}}" in service
    assert "{{ENV_FILE}}" in service
    assert 'ExecStart="{{PYTHON_BIN}}" -m hcr_sync run-once --apply' in service
    assert "hcr-sync.service" in timer
