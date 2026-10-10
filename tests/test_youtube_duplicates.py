from pathlib import Path

import pytest
import wave

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, ensure_track, init_db, transaction, upsert_youtube_asset
from hcr_sync.youtube_sync import YouTubeCandidate, sync_youtube


def make_config(tmp_path: Path, **overrides: str) -> Config:
    values = dict(DEFAULTS)
    values.update(
        {
            "HCR_DB_PATH": str(tmp_path / "hcr_music.db"),
            "HCR_MUSIC_DIR": str(tmp_path / "music"),
            "HCR_TRASH_DIR": str(tmp_path / "music" / ".hcr-trash"),
            "HCR_SPOTIFY_ENABLED": "false",
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


def audio_file(path):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0\0' * (8000 * 180))
    return path


class FakeYouTube:
    def __init__(self):
        self.searches = []
        self.downloads = []

    def search(self, artist, title):
        self.searches.append((artist, title))
        return [
            YouTubeCandidate(
                title="Angerfist - Gathering Of Gods [Extended Mix]",
                url="https://www.youtube.com/watch?v=newid123456",
                video_id="newid123456",
                channel="Example",
                duration=180,
            )
        ]

    def download(self, candidate):
        self.downloads.append(candidate)
        return Path("/tmp/should-not-download.mp3")


def test_youtube_sync_skips_existing_local_near_duplicate_before_search(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    old_file = config.music_dir / "Angerfist - Gathering Of Gods (Official Music Video) [oldid123456].mp3"
    audio_file(old_file)
    with connect(config) as con:
        with transaction(con):
            wanted = ensure_track(con, artist="Angerfist", title="Gathering Of Gods (Extended Mix)", status="wanted")
            existing = ensure_track(con, artist="Angerfist", title="Gathering Of Gods (Official Music Video)", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=existing["id"],
                youtube_video_id="oldid123456",
                file_path=str(old_file),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )

    client = FakeYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.already_local == 2
    assert client.searches == []
    assert client.downloads == []
    with connect(config) as con:
        assert con.execute("SELECT count(*) FROM youtube_schedule WHERE phase='held' AND hold_origin='local_satisfied'").fetchone()[0] == 2


def test_youtube_sync_marks_unknown_placeholder_review_without_search(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Unknown Artist #01", title="Unknown Title #01 (Original Mix)", status="wanted")

    client = FakeYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.searches == []
    assert client.downloads == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets WHERE status='review'").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type='ambiguous_youtube_match'").fetchone()
    assert asset is not None
    assert event is not None


def test_youtube_sync_marks_source_non_track_review_without_search(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Various Artists", title="Dominator Festival 25.07.2009", status="wanted")

    client = FakeYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.searches == []
    assert client.downloads == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets WHERE status='review'").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type='ambiguous_youtube_match'").fetchone()
    assert asset is not None
    assert event is not None


def test_youtube_sync_rejects_multi_title_candidate(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Drokz", title="Only The Strong Survive", status="wanted")

    class MultiTitleYouTube(FakeYouTube):
        def search(self, artist, title):
            self.searches.append((artist, title))
            return [
                YouTubeCandidate(
                    title="DROKZ - B2 - ONLY THE STRONG SURVIVE - I GOT TO BE ME - AA10",
                    url="https://www.youtube.com/watch?v=E50h8DmX0LA",
                    video_id="E50h8DmX0LA",
                    channel="Example",
                    duration=266,
                )
            ]

    client = MultiTitleYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.downloads == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets WHERE status='review'").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type='ambiguous_youtube_match'").fetchone()
    assert asset is not None
    assert asset["match_confidence"] > .9
    assert event is not None
    import json
    with connect(config) as con:
        evidence = json.loads(con.execute('SELECT decision_json FROM youtube_schedule').fetchone()[0])
    assert evidence['decision']['reason'] == 'multi_title'
    assert not evidence['decision']['accepted']


def test_youtube_sync_skips_existing_review_asset_without_search(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Ambiguous Track", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_exists=False,
                match_confidence=0.5,
                status="review",
            )

    client = FakeYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.searches == []
    assert client.downloads == []


def test_youtube_sync_skips_idless_local_audio_by_default(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    local_audio = config.music_dir / "EQUAL2 & PSYCHOWEAPON - HARDCORE LIFESTYLE.m4a"
    local_audio.write_bytes(b"existing audio")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="EQUAL2 & PSYCHOWEAPON", title="HARDCORE LIFESTYLE", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(local_audio),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )

    client = FakeYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.already_local == 1
    assert summary.downloaded == 0
    assert client.searches == []
    assert local_audio.exists()


def test_youtube_sync_downloads_when_idless_local_completion_is_enabled(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    local_audio = config.music_dir / "EQUAL2 & PSYCHOWEAPON - HARDCORE LIFESTYLE.m4a"
    local_audio.write_bytes(b"existing audio")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="EQUAL2 & PSYCHOWEAPON", title="HARDCORE LIFESTYLE", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=track["id"],
                file_path=str(local_audio),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )

    class CompletingYouTube:
        def __init__(self):
            self.searches = []
            self.downloads = []

        def search(self, artist, title):
            self.searches.append((artist, title))
            return [
                YouTubeCandidate(
                    title="EQUAL2 & PSYCHOWEAPON - HARDCORE LIFESTYLE",
                    url="https://www.youtube.com/watch?v=hardcore123",
                    video_id="hardcore123",
                    channel="EQUAL2",
                    duration=180,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate)
            path = config.music_dir / "EQUAL2 & PSYCHOWEAPON - HARDCORE LIFESTYLE [hardcore123].mp3"
            audio_file(path)
            return path

    client = CompletingYouTube()
    summary = sync_youtube(config, apply=True, client=client, complete_idless_local=True)

    assert summary.downloaded == 1
    assert summary.already_local == 0
    assert client.searches == [("EQUAL2 & PSYCHOWEAPON", "HARDCORE LIFESTYLE")]
    assert len(client.downloads) == 1
    assert local_audio.exists()
    with connect(config) as con:
        assets = list(con.execute("SELECT * FROM youtube_assets ORDER BY id"))
        assert len(assets) == 2
        assert assets[0]["youtube_video_id"] is None
        assert assets[1]["youtube_video_id"] == "hardcore123"


def test_youtube_sync_rejects_blank_artist_title_embedded_in_longer_candidate(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="", title="GET HYPE", status="wanted")

    class EmbeddedTitleYouTube:
        def __init__(self):
            self.downloads = []

        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Martin Ikin - Headnoise (Get Hype)",
                    url="https://www.youtube.com/watch?v=hVgNH8A9kso",
                    video_id="hVgNH8A9kso",
                    channel="PROFOUND",
                    duration=344,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate)
            return config.music_dir / "bad.mp3"

    client = EmbeddedTitleYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.downloads == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets WHERE status = 'review'").fetchone()
        assert asset is not None
        assert asset["match_confidence"] == 0.0


def test_youtube_sync_rejects_short_blank_artist_exact_title_candidate(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="", title="GET HYPE", status="wanted")

    class ShortBlankArtistYouTube:
        def __init__(self):
            self.downloads = []

        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Discrepancies - Get Hype (Official Audio)",
                    url="https://www.youtube.com/watch?v=Wgl2GsUOPD8",
                    video_id="Wgl2GsUOPD8",
                    channel="DISCREPANCIES TV",
                    duration=190,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate)
            return config.music_dir / "bad.mp3"

    client = ShortBlankArtistYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.downloads == []


def test_youtube_sync_records_download_failure_and_continues(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Fail Artist", title="Fail Title", status="wanted")
            ensure_track(con, artist="Ok Artist", title="Ok Title", status="wanted")

    class PartiallyFailingYouTube:
        def __init__(self):
            self.downloads = []

        def search(self, artist, title):
            video_id = "fail1234567" if artist == "Fail Artist" else "ok123456789"
            return [
                YouTubeCandidate(
                    title=f"{artist} - {title}",
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    video_id=video_id,
                    channel=artist,
                    duration=180,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate.video_id)
            if candidate.video_id == "fail1234567":
                raise RuntimeError("download failed")
            path = config.music_dir / "Ok Artist - Ok Title [ok123456789].mp3"
            audio_file(path)
            return path

    client = PartiallyFailingYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.skipped == 1 and summary.downloaded == 0
    assert client.downloads == ["fail1234567"]
    second = sync_youtube(config, apply=True, client=client)
    assert second.downloaded == 1
    assert client.downloads == ["fail1234567", "ok123456789"]
    with connect(config) as con:
        error_asset = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'fail1234567'").fetchone()
        downloaded_asset = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'ok123456789'").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type = 'youtube_download_failed'").fetchone()
        assert error_asset["status"] == "error"
        assert error_asset["file_exists"] == 0
        assert downloaded_asset["status"] == "downloaded"
        assert event is not None


def test_youtube_sync_rejects_download_output_outside_music_dir(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    outside = tmp_path / "outside.mp3"
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    class OutsidePathYouTube:
        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=outside1234",
                    video_id="outside1234",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            outside.write_bytes(b"audio")
            return outside

    with pytest.raises(RuntimeError, match="outside HCR_MUSIC_DIR"):
        sync_youtube(config, apply=True, client=OutsidePathYouTube())
    assert outside.exists()
    with connect(config) as con:
        assert con.execute("SELECT count(*) FROM youtube_assets WHERE file_exists=1").fetchone()[0] == 0
        assert con.execute("SELECT state FROM youtube_pending_work").fetchone()[0] == 'held'


def test_youtube_sync_rejects_missing_download_output(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    missing = config.music_dir / "Artist - Title [missing1234].mp3"
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    class MissingOutputYouTube:
        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=missing1234",
                    video_id="missing1234",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            return missing

    with pytest.raises(RuntimeError, match="download output was not created"):
        sync_youtube(config, apply=True, client=MissingOutputYouTube())
    with connect(config) as con:
        assert con.execute("SELECT count(*) FROM youtube_assets WHERE file_exists=1").fetchone()[0] == 0
        assert con.execute("SELECT state FROM youtube_pending_work").fetchone()[0] == 'held'


def test_youtube_sync_rejects_download_output_file_linked_to_other_track(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    existing_file = config.music_dir / "Noise Maker - Completely Different [other123456].mp3"
    audio_file(existing_file)
    with connect(config) as con:
        with transaction(con):
            other = ensure_track(con, artist="Noise Maker", title="Completely Different", status="wanted")
            ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=other["id"],
                youtube_video_id="other123456",
                youtube_url="https://www.youtube.com/watch?v=other123456",
                file_path=str(existing_file),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )

    class ExistingFileYouTube:
        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=new12345678",
                    video_id="new12345678",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            return existing_file

    summary = sync_youtube(config, apply=True, client=ExistingFileYouTube())

    assert summary.review == 1
    assert summary.downloaded == 0
    with connect(config) as con:
        rows = list(con.execute("SELECT * FROM youtube_assets ORDER BY track_id"))
        event = con.execute("SELECT * FROM events WHERE event_type = 'youtube_candidate_already_linked'").fetchone()
        assert len(rows) == 2
        assert rows[0]["file_path"] == str(existing_file)
        assert rows[0]["file_exists"] == 1
        assert rows[1]["status"] == "review"
        assert rows[1]["file_path"] is None
        assert event is not None


def test_youtube_sync_reviews_candidate_video_id_linked_to_other_track_without_downloading(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    existing_file = config.music_dir / "Noise Maker - Completely Different [samevid1234].mp3"
    audio_file(existing_file)
    with connect(config) as con:
        with transaction(con):
            other = ensure_track(con, artist="Noise Maker", title="Completely Different", status="wanted")
            ensure_track(con, artist="Drokz", title="Karma", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=other["id"],
                youtube_video_id="samevid1234",
                youtube_url="https://www.youtube.com/watch?v=samevid1234",
                file_path=str(existing_file),
                file_exists=True,
                match_confidence=1.0,
                status="downloaded",
            )

    class DuplicateVideoYouTube:
        def __init__(self):
            self.downloads = []

        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Drokz - Karma",
                    url="https://www.youtube.com/watch?v=samevid1234",
                    video_id="samevid1234",
                    channel="Drokz",
                    duration=180,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate)
            path = config.music_dir / "Drokz - Karma [samevid1234].mp3"
            audio_file(path)
            return path

    client = DuplicateVideoYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.downloads == []
    with connect(config) as con:
        rows = list(con.execute("SELECT * FROM youtube_assets ORDER BY track_id"))
        event = con.execute("SELECT * FROM events WHERE event_type = 'youtube_candidate_already_linked'").fetchone()
        assert len(rows) == 2
        assert rows[0]["youtube_video_id"] == "samevid1234"
        assert rows[0]["file_exists"] == 1
        assert rows[1]["status"] == "review"
        assert rows[1]["youtube_video_id"] is None
        assert event is not None
