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
