import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, init_db, mark_excluded, transaction, upsert_youtube_asset
from hcr_sync.identity import parse_artist_title
from hcr_sync.local_files import import_local_files, inspect_audio_file
from hcr_sync.logger_importer import import_logger
from hcr_sync.poller import poll_radio


@pytest.fixture
def import_clock(monkeypatch):
    clock = [datetime(2026, 2, 1, tzinfo=timezone.utc)]

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    monkeypatch.setattr("hcr_sync.db.datetime", ControlledDatetime)
    return clock


def _logger_input(config, source, timestamps):
    if source == "jsonl":
        path = config.seen_tracks_path
        lines = [json.dumps({"track": "Example Artist - Synthetic Song", **({"first_seen_at": timestamp} if timestamp is not None else {})}) for timestamp in timestamps]
    else:
        path = config.played_tracks_path
        lines = [(timestamp + "\t" if timestamp is not None else "") + "Example Artist - Synthetic Song" for timestamp in timestamps]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("source", ["jsonl", "tsv"])
@pytest.mark.parametrize("apply", [False, True])
def test_logger_rejects_timestamp_less_replays_without_partial_state(tmp_path, import_clock, source, apply):
    config = make_config(tmp_path, HCR_AUDIT_VERBOSE="true")
    init_db(config)
    _logger_input(config, source, [None])
    with connect(config) as con:
        before = list(con.iterdump())
    errors = []
    for _ in range(2):
        with pytest.raises(ValueError) as error:
            import_logger(config, apply=apply)
        errors.append(str(error.value))
        with connect(config) as con:
            assert list(con.iterdump()) == before
        import_clock[0] += timedelta(days=1)
    assert errors[0] == errors[1]
    assert "line 1" in errors[0] and "timestamp" in errors[0]
    assert str(tmp_path) not in errors[0] and "Example Artist" not in errors[0]


@pytest.mark.parametrize("source", ["jsonl", "tsv"])
def test_logger_timestamped_events_keep_identity_and_distinct_history(tmp_path, import_clock, source):
    config = make_config(tmp_path)
    init_db(config)
    timestamps = ["2026-01-01T00:00:00Z", "2026-01-02T01:02:03.123+02:30"]
    _logger_input(config, source, timestamps)
    assert import_logger(config, apply=True).observations_added == 2
    with connect(config) as con:
        before = [dict(row) for row in con.execute("SELECT * FROM radio_observations ORDER BY id")]
    import_clock[0] += timedelta(days=1)
    assert import_logger(config, apply=True).observations_added == 0
    with connect(config) as con:
        assert [dict(row) for row in con.execute("SELECT * FROM radio_observations ORDER BY id")] == before
        assert con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert [row["observed_at"] for row in before] == timestamps
    assert all(row["imported_at"] != row["observed_at"] for row in before)


@pytest.mark.parametrize("source", ["jsonl", "tsv"])
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("second_file", [False, True])
def test_logger_invalid_row_rejects_whole_invocation(tmp_path, source, apply, second_file):
    config = make_config(tmp_path, HCR_AUDIT_VERBOSE="true")
    init_db(config)
    if second_file:
        _logger_input(config, "jsonl", ["2026-01-01T00:00:00Z"])
        _logger_input(config, "tsv", [None])
    else:
        _logger_input(config, source, ["2026-01-01T00:00:00Z", None])
    with connect(config) as con:
        before = list(con.iterdump())
    with pytest.raises(ValueError):
        import_logger(config, apply=apply)
    with connect(config) as con:
        assert list(con.iterdump()) == before


@pytest.mark.parametrize("source", ["jsonl", "tsv"])
@pytest.mark.parametrize("timestamp", ["", "2026-01-01", "2026-01-01T01:02Z", "2026-01-01T01:02:03", "2026-02-30T01:02:03Z", "2026-01-01T25:00:00Z", "2026-01-01T01:02:03+24:00", "2026-01-01T01:02:03+01:60", "not a timestamp"])
def test_logger_rejects_unusable_timestamps_before_opening_database(tmp_path, source, timestamp):
    config = make_config(tmp_path)
    _logger_input(config, source, [timestamp])
    with pytest.raises(ValueError):
        import_logger(config, apply=True)
    assert not config.db_path.exists()


@pytest.mark.parametrize("record", ['bare legacy text', '[]', 'null', '{"track": "Example", "first_seen_at": 123}', '{"track": "", "first_seen_at": "2026-01-01T00:00:00Z"}', '{"track": 123, "first_seen_at": "2026-01-01T00:00:00Z"}'])
def test_logger_rejects_malformed_jsonl_contract(tmp_path, record):
    config = make_config(tmp_path)
    config.seen_tracks_path.write_text(record + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="seen-tracks.jsonl line 1:"):
        import_logger(config, apply=True)
    assert not config.db_path.exists()


@pytest.mark.parametrize("header", ["track\ttimestamp", "artist_title\tplayed_at", "query\ttime", "name\ttimestamp", "song\ttime", "title\tplayed_at"])
def test_logger_tsv_maps_named_columns_and_preserves_timestamp(tmp_path, header):
    config = make_config(tmp_path)
    init_db(config)
    timestamp = "2026-01-01T01:02:03.120-03:00"
    config.played_tracks_path.write_text("\n" + header + "\nExample - Song\t " + timestamp + " \n\n", encoding="utf-8")
    summary = import_logger(config, apply=True)
    assert summary.rows_seen == 1 and summary.observations_added == 1
    with connect(config) as con:
        assert con.execute("SELECT observed_at FROM radio_observations").fetchone()[0] == timestamp
        assert con.execute("SELECT display_title FROM tracks").fetchone()[0] == "Song"


@pytest.mark.parametrize("text", ["track\ttitle\ttimestamp\nExample\tSong\t2026-01-01T00:00:00Z\n", "timestamp\ttime\ttrack\n", "track\tother\n", "timestamp\ttrack\n2026-01-01T00:00:00Z\n", '2026-01-01T00:00:00Z\t"unterminated\n'])
def test_logger_rejects_ambiguous_headers_and_malformed_tsv(tmp_path, text):
    config = make_config(tmp_path)
    config.played_tracks_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="played-tracks.tsv line "):
        import_logger(config, apply=True)
    assert not config.db_path.exists()


def test_logger_valid_dry_run_validates_without_creating_database(tmp_path):
    config = make_config(tmp_path)
    for source in ["jsonl", "tsv"]:
        _logger_input(config, source, ["2026-01-01T00:00:00Z"])
    summary = import_logger(config, apply=False)
    assert summary.files_read == 2 and summary.rows_seen == 2
    assert summary.observations_added == 0
    assert not config.db_path.exists()


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


def test_verbose_logger_import_logs_duplicate_rows(tmp_path):
    config = make_config(tmp_path, HCR_AUDIT_VERBOSE="true")
    init_db(config)
    config.seen_tracks_path.write_text(
        json.dumps({"first_seen_at": "2026-01-01T00:00:00Z", "track": "Artist - Title"}) + "\n",
        encoding="utf-8",
    )

    import_logger(config, apply=True)
    import_logger(config, apply=True)

    with connect(config) as con:
        rows = list(con.execute("SELECT payload_json FROM events WHERE event_type = 'logger_entry_imported' ORDER BY id"))

    payloads = [json.loads(row["payload_json"]) for row in rows]
    assert [payload["observation_added"] for payload in payloads] == [True, False]
    assert [payload["duplicate"] for payload in payloads] == [False, True]


def test_verbose_poll_radio_logs_new_and_duplicate_tracks(monkeypatch, tmp_path):
    config = make_config(
        tmp_path,
        HCR_AUDIT_VERBOSE="true",
        HCR_LOGGER_LOCK_FILE=str(tmp_path / ".logger.lock"),
        HCR_STREAM_URL="https://stream.hardcoreradio.nl:9000/hcr.ogg",
    )
    init_db(config)
    payload = {
        "icestats": {
            "source": {
                "listenurl": "https://stream.hardcoreradio.nl:9000/hcr.ogg",
                "server_type": "audio/ogg",
                "artist": "Artist",
                "title": "Title",
            }
        }
    }
    monkeypatch.setattr("hcr_sync.poller.fetch_status", lambda _status_url: payload)

    first = poll_radio(config, apply=True)
    second = poll_radio(config, apply=True)

    assert first == (True, "Artist - Title")
    assert second == (False, "Artist - Title")
    with connect(config) as con:
        rows = list(con.execute("SELECT payload_json FROM events WHERE event_type = 'radio_poll_seen' ORDER BY id"))
    payloads = [json.loads(row["payload_json"]) for row in rows]
    assert [payload["duplicate"] for payload in payloads] == [False, True]
    assert [payload["changed"] for payload in payloads] == [True, False]


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
