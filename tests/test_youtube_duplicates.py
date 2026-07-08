from pathlib import Path

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


class FakeYouTube:
    def __init__(self):
        self.searches = []
        self.downloads = []

    def search(self, artist, title):
        self.searches.append((artist, title))
        return [
            YouTubeCandidate(
                title="Angerfist - Gathering Of Gods [Extended Mix]",
                url="https://www.youtube.com/watch?v=newid123",
                video_id="newid123",
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
    old_file = config.music_dir / "Gathering Of Gods (Official Music Video) [oldid123].mp3"
    old_file.write_bytes(b"x")
    with connect(config) as con:
        with transaction(con):
            wanted = ensure_track(con, artist="Angerfist", title="Gathering Of Gods (Extended Mix)", status="wanted")
            existing = ensure_track(con, artist="Angerfist", title="Gathering Of Gods (Official Music Video)", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=existing["id"],
                youtube_video_id="oldid123",
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
        event = con.execute("SELECT * FROM events WHERE event_type='youtube_skipped_existing_local_match'").fetchone()
    assert event is not None


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
    assert asset["match_confidence"] is None
    assert event is not None


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
            path.write_bytes(b"mp3")
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
        assert asset["match_confidence"] is None


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
            video_id = "fail123" if artist == "Fail Artist" else "ok123"
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
            if candidate.video_id == "fail123":
                raise RuntimeError("download failed")
            path = config.music_dir / "Ok Artist - Ok Title [ok123].mp3"
            path.write_bytes(b"audio")
            return path

    client = PartiallyFailingYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.skipped == 1
    assert summary.downloaded == 1
    assert client.downloads == ["fail123", "ok123"]
    with connect(config) as con:
        error_asset = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'fail123'").fetchone()
        downloaded_asset = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'ok123'").fetchone()
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
                    url="https://www.youtube.com/watch?v=outside123",
                    video_id="outside123",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            outside.write_bytes(b"audio")
            return outside

    summary = sync_youtube(config, apply=True, client=OutsidePathYouTube())

    assert summary.skipped == 1
    assert summary.downloaded == 0
    assert outside.exists()
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'outside123'").fetchone()
        event = con.execute("SELECT * FROM events WHERE event_type = 'youtube_download_failed'").fetchone()
        assert asset["status"] == "error"
        assert asset["file_exists"] == 0
        assert event is not None


def test_youtube_sync_rejects_missing_download_output(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    missing = config.music_dir / "Artist - Title [missing123].mp3"
    with connect(config) as con:
        with transaction(con):
            ensure_track(con, artist="Artist", title="Title", status="wanted")

    class MissingOutputYouTube:
        def search(self, artist, title):
            return [
                YouTubeCandidate(
                    title="Artist - Title",
                    url="https://www.youtube.com/watch?v=missing123",
                    video_id="missing123",
                    channel="Artist",
                    duration=180,
                )
            ]

        def download(self, candidate):
            return missing

    summary = sync_youtube(config, apply=True, client=MissingOutputYouTube())

    assert summary.skipped == 1
    assert summary.downloaded == 0
    with connect(config) as con:
        asset = con.execute("SELECT * FROM youtube_assets WHERE youtube_video_id = 'missing123'").fetchone()
        assert asset["status"] == "error"
        assert asset["file_exists"] == 0


def test_youtube_sync_rejects_download_output_file_linked_to_other_track(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    existing_file = config.music_dir / "Noise Maker - Completely Different [other123].mp3"
    existing_file.write_bytes(b"audio")
    with connect(config) as con:
        with transaction(con):
            other = ensure_track(con, artist="Noise Maker", title="Completely Different", status="wanted")
            ensure_track(con, artist="Artist", title="Title", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=other["id"],
                youtube_video_id="other123",
                youtube_url="https://www.youtube.com/watch?v=other123",
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
                    url="https://www.youtube.com/watch?v=new123",
                    video_id="new123",
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
    existing_file = config.music_dir / "Noise Maker - Completely Different [samevid123].mp3"
    existing_file.write_bytes(b"audio")
    with connect(config) as con:
        with transaction(con):
            other = ensure_track(con, artist="Noise Maker", title="Completely Different", status="wanted")
            ensure_track(con, artist="Drokz", title="Karma", status="wanted")
            upsert_youtube_asset(
                con,
                track_id=other["id"],
                youtube_video_id="samevid123",
                youtube_url="https://www.youtube.com/watch?v=samevid123",
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
                    url="https://www.youtube.com/watch?v=samevid123",
                    video_id="samevid123",
                    channel="Drokz",
                    duration=180,
                )
            ]

        def download(self, candidate):
            self.downloads.append(candidate)
            path = config.music_dir / "Drokz - Karma [samevid123].mp3"
            path.write_bytes(b"audio")
            return path

    client = DuplicateVideoYouTube()
    summary = sync_youtube(config, apply=True, client=client)

    assert summary.review == 1
    assert client.downloads == []
    with connect(config) as con:
        rows = list(con.execute("SELECT * FROM youtube_assets ORDER BY track_id"))
        event = con.execute("SELECT * FROM events WHERE event_type = 'youtube_candidate_already_linked'").fetchone()
        assert len(rows) == 2
        assert rows[0]["youtube_video_id"] == "samevid123"
        assert rows[0]["file_exists"] == 1
        assert rows[1]["status"] == "review"
        assert rows[1]["youtube_video_id"] is None
        assert event is not None
