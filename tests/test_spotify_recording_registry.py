"""Regression coverage for recording-level Spotify playlist ownership."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import (
    connect,
    ensure_track,
    init_db,
    mark_excluded,
    migrate_db,
    set_state,
    transaction,
    upsert_spotify_asset,
)
from hcr_sync.reconcile import reconcile
from hcr_sync.spotify_sync import (
    PlaylistSnapshot,
    SpotifyAssociationConflict,
    SpotifyTrack,
    backfill_spotify,
    scan_spotify_playlist,
)


def make_config(tmp_path: Path, **overrides: str) -> Config:
    values = dict(DEFAULTS)
    values.update(
        {
            "HCR_DB_PATH": str(tmp_path / "hcr_music.db"),
            "HCR_MUSIC_DIR": str(tmp_path / "music"),
            "HCR_TRASH_DIR": str(tmp_path / "music" / ".hcr-trash"),
            "HCR_SPOTIFY_PLAYLIST_ID": "playlist",
            "HCR_SPOTIFY_TOKEN_CACHE": str(tmp_path / "token-cache"),
            "HCR_SPOTIFY_ENABLED": "true",
            "HCR_RECONCILE_REQUIRE_TWO_PASSES": "true",
            "HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO": "0.40",
        }
    )
    values.update(overrides)
    return Config(values=values, loaded_files=[])


class FakeSpotify:
    def __init__(self, tracks: list[SpotifyTrack], *, complete: bool = True):
        self.tracks = tracks
        self.complete = complete
        self.removed: list[str] = []

    def playlist_snapshot(self, playlist_id: str) -> PlaylistSnapshot:
        return PlaylistSnapshot(playlist_id, "synthetic-snapshot", self.tracks, self.complete)

    def remove_tracks(self, playlist_id: str, uris: list[str]) -> None:
        self.removed.extend(uris)


def recording(
    track_id: str,
    *,
    artist: str = "Artist",
    title: str = "Song",
    artist_ids: tuple[str, ...] = ("artist-id",),
    album: str = "Album",
    isrc: str = "ISRC",
    duration_ms: int = 180000,
) -> SpotifyTrack:
    return SpotifyTrack(
        uri=f"spotify:track:{track_id}",
        track_id=track_id,
        artist=artist,
        title=title,
        duration_ms=duration_ms,
        artist_ids=artist_ids,
        album=album,
        isrc=isrc,
    )


def test_collision_orders_share_one_logical_track_and_keep_primary_deterministic(tmp_path):
    first = recording("recording-a", album="Album A", isrc="ISRC-A", duration_ms=180000)
    second = recording("recording-b", album="Album B", isrc="ISRC-B", duration_ms=181000)
    primary_ids = []
    for tracks in ([first, second], [second, first]):
        case = tmp_path / str(len(primary_ids))
        case.mkdir()
        config = make_config(case)
        init_db(config)
        summary = scan_spotify_playlist(config, apply=True, client=FakeSpotify(list(tracks)))
        assert summary.linked == 2 and summary.ambiguous == 0
        with connect(config) as con:
            assert con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
            primary = con.execute("SELECT spotify_track_id FROM spotify_assets").fetchone()[0]
            primary_ids.append(primary)
            rows = list(con.execute("SELECT * FROM spotify_playlist_recordings ORDER BY spotify_track_id"))
            assert [row["spotify_track_id"] for row in rows] == ["recording-a", "recording-b"]
            assert {row["track_id"] for row in rows} == {1}
            assert json.loads(rows[0]["spotify_artist_ids"]) == ["artist-id"]
    assert primary_ids == ["recording-a", "recording-a"]


def test_existing_primary_provenance_survives_secondary_recording(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Artist", title="Song", status="wanted")
            primary = upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_id="recording-a",
                spotify_track_uri="spotify:track:recording-a",
                spotify_artist="Artist",
                spotify_title="Song",
                in_playlist=True,
                match_confidence=0.731,
                status="review",
                added_at="2026-01-01T00:00:00Z",
            )
            con.execute(
                "UPDATE spotify_playlist_recordings SET spotify_artist_ids = ?, spotify_album = ?, spotify_isrc = ?, duration_ms = ?",
                ('["artist-id"]', "Original Album", "ISRC-A", 180000),
            )
            primary_id = primary["id"]

    snapshot = [
        recording("recording-b", album="Secondary Album", isrc="ISRC-B", duration_ms=181000),
        recording("recording-a", album="Original Album", isrc="ISRC-A", duration_ms=180000),
    ]
    summary = scan_spotify_playlist(config, apply=True, client=FakeSpotify(snapshot))
    assert summary.linked == 2
    with connect(config) as con:
        primary = con.execute("SELECT * FROM spotify_assets WHERE id = ?", (primary_id,)).fetchone()
        assert (primary["spotify_track_id"], primary["match_confidence"], primary["status"], primary["added_at"]) == (
            "recording-a",
            pytest.approx(0.731),
            "review",
            "2026-01-01T00:00:00Z",
        )
        assert con.execute("SELECT COUNT(*) FROM spotify_playlist_recordings WHERE track_id = 1").fetchone()[0] == 2


def test_ambiguous_recording_is_unassigned_and_unrelated_recording_continues(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    snapshot = [
        recording("anchor"),
        recording("z-ambiguous", artist_ids=("different-artist-id",), album="Different Album", isrc="ISRC-X"),
        recording("unrelated", artist="Other Artist", title="Other Song", artist_ids=("other-id",)),
    ]
    summary = scan_spotify_playlist(config, apply=True, client=FakeSpotify(snapshot))
    assert summary.linked == 2 and summary.ambiguous == 1
    with connect(config) as con:
        ambiguous = con.execute(
            "SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id = 'z-ambiguous'"
        ).fetchone()
        assert ambiguous["track_id"] is None
        assert ambiguous["association_status"] == "ambiguous"
        assert ambiguous["association_reason"] == "ambiguous Spotify recording metadata"
        assert con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
        event = con.execute("SELECT payload_json FROM events WHERE event_type = 'ambiguous_spotify_recording'").fetchone()
        assert set(json.loads(event["payload_json"])) == {"reason", "spotify_track_id"}


def test_recording_ownership_conflict_rolls_back_and_is_redacted(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist="Owner Artist", title="Owner Song", status="wanted")
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_id="owned-recording",
                spotify_track_uri="spotify:track:owned-recording",
                spotify_artist="Owner Artist",
                spotify_title="Owner Song",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )
        before = list(con.iterdump())
    before_bytes = config.db_path.read_bytes()
    with pytest.raises(SpotifyAssociationConflict, match="^Spotify playlist snapshot association conflict$"):
        scan_spotify_playlist(
            config,
            apply=True,
            client=FakeSpotify([recording("owned-recording", artist="Other Artist", title="Other Song")]),
        )
    with connect(config) as con:
        assert list(con.iterdump()) == before
    assert config.db_path.read_bytes() == before_bytes


def test_dry_run_prefers_known_owner_over_unestablished_canonical_duplicate(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            owner = ensure_track(con, artist="Drokz", title="The Mind", status="wanted")
            duplicate = ensure_track(
                con,
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                status="wanted",
            )
            upsert_spotify_asset(
                con,
                track_id=owner["id"],
                playlist_id="playlist",
                spotify_track_id="known-recording",
                spotify_track_uri="spotify:track:known-recording",
                spotify_artist="Drokz",
                spotify_title="The Mind (Signs Of Life)",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )
            mark_excluded(con, track_id=duplicate["id"], source="manual", reason="duplicate local label")
        before_bytes = config.db_path.read_bytes()
        before_dump = list(con.iterdump())

    client = FakeSpotify(
        [
            recording(
                "known-recording",
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                artist_ids=("drokz-id",),
            )
        ]
    )
    summary = scan_spotify_playlist(config, apply=False, client=client)

    assert summary.linked == 1 and summary.ambiguous == 0
    assert client.removed == []
    assert config.db_path.read_bytes() == before_bytes
    with connect(config) as con:
        assert list(con.iterdump()) == before_dump
        owner = con.execute(
            "SELECT * FROM tracks WHERE canonical_key = 'drokz::the mind'"
        ).fetchone()
        duplicate = con.execute(
            "SELECT * FROM tracks WHERE canonical_key = 'drokz::the mind signs of life'"
        ).fetchone()
        assert owner["status"] == "wanted"
        assert duplicate["status"] == "excluded"


def test_dry_run_uses_provider_metadata_when_local_owner_title_differs(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            owner = ensure_track(con, artist="Drokz", title="The Mind - Live", status="wanted")
            duplicate = ensure_track(
                con,
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                status="wanted",
            )
            upsert_spotify_asset(
                con,
                track_id=owner["id"],
                playlist_id="playlist",
                spotify_track_id="known-recording",
                spotify_track_uri="spotify:track:known-recording",
                spotify_artist="Drokz",
                spotify_title="The Mind (Signs Of Life)",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )
            mark_excluded(con, track_id=duplicate["id"], source="manual", reason="duplicate local label")
        before_bytes = config.db_path.read_bytes()
        before_dump = list(con.iterdump())

    client = FakeSpotify(
        [
            recording(
                "known-recording",
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                artist_ids=("drokz-id",),
            )
        ]
    )
    summary = scan_spotify_playlist(config, apply=False, client=client)

    assert summary.linked == 1 and summary.ambiguous == 0
    assert client.removed == []
    assert config.db_path.read_bytes() == before_bytes
    with connect(config) as con:
        assert list(con.iterdump()) == before_dump
        owner = con.execute(
            "SELECT * FROM tracks WHERE canonical_key = 'drokz::the mind live'"
        ).fetchone()
        duplicate = con.execute(
            "SELECT * FROM tracks WHERE canonical_key = 'drokz::the mind signs of life'"
        ).fetchone()
        assert owner["status"] == "wanted"
        assert duplicate["status"] == "excluded"


def test_unestablished_known_owner_does_not_override_canonical_duplicate(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            owner = ensure_track(con, artist="Drokz", title="The Mind", status="wanted")
            ensure_track(
                con,
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                status="wanted",
            )
            upsert_spotify_asset(
                con,
                track_id=owner["id"],
                playlist_id="playlist",
                spotify_track_id="tentative-recording",
                spotify_track_uri="spotify:track:tentative-recording",
                spotify_artist="Drokz",
                spotify_title="The Mind (Signs Of Life)",
                in_playlist=False,
                match_confidence=0.0,
                status="review",
            )
        before_bytes = config.db_path.read_bytes()

    client = FakeSpotify(
        [
            recording(
                "tentative-recording",
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                artist_ids=("drokz-id",),
            )
        ]
    )
    with pytest.raises(SpotifyAssociationConflict, match="^Spotify playlist snapshot association conflict$"):
        scan_spotify_playlist(config, apply=False, client=client)

    assert config.db_path.read_bytes() == before_bytes
    assert client.removed == []


def test_canonical_duplicate_with_established_history_stays_fail_closed(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    with connect(config) as con:
        with transaction(con):
            owner = ensure_track(con, artist="Drokz", title="The Mind", status="wanted")
            duplicate = ensure_track(
                con,
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                status="wanted",
            )
            upsert_spotify_asset(
                con,
                track_id=owner["id"],
                playlist_id="playlist",
                spotify_track_id="known-recording",
                spotify_track_uri="spotify:track:known-recording",
                spotify_artist="Drokz",
                spotify_title="The Mind",
                in_playlist=True,
                match_confidence=1.0,
                status="added",
            )
            upsert_spotify_asset(
                con,
                track_id=duplicate["id"],
                playlist_id="playlist",
                spotify_track_id="duplicate-recording",
                spotify_track_uri="spotify:track:duplicate-recording",
                spotify_artist="Drokz",
                spotify_title="The Mind (Signs Of Life)",
                in_playlist=False,
                match_confidence=1.0,
                status="removed",
            )
        before_bytes = config.db_path.read_bytes()

    client = FakeSpotify(
        [
            recording(
                "known-recording",
                artist="Drokz",
                title="The Mind (Signs Of Life)",
                artist_ids=("drokz-id",),
            )
        ]
    )
    with pytest.raises(SpotifyAssociationConflict, match="^Spotify playlist snapshot association conflict$"):
        scan_spotify_playlist(config, apply=False, client=client)

    assert config.db_path.read_bytes() == before_bytes
    assert client.removed == []


def test_remove_one_recording_keeps_song_and_remove_all_then_reappearance(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / "Artist - Song.mp3"
    path.write_bytes(b"synthetic")
    initial = [recording("recording-a"), recording("recording-b"), recording("keep", artist="Keep", title="Song", artist_ids=("keep-id",))]
    backfill_spotify(config, apply=True, client=FakeSpotify(initial))
    with connect(config) as con:
        with transaction(con):
            track = con.execute("SELECT id FROM tracks WHERE canonical_key = 'artist::song'").fetchone()
            upsert_spotify_asset(
                con,
                track_id=track["id"],
                playlist_id="playlist",
                spotify_track_id="recording-a",
                spotify_track_uri="spotify:track:recording-a",
                spotify_artist="Artist",
                spotify_title="Song",
                in_playlist=True,
                match_confidence=0.5,
                status="review",
            )
            # Keep the initial recording timestamps behind the last scan so a
            # missing row is eligible for the normal two-pass confirmation.
            con.execute(
                "UPDATE spotify_playlist_recordings SET first_seen_at = '2026-01-01T00:00:00Z'"
            )
            set_state(con, "last_spotify_scan_at", "2026-01-02T00:00:00Z")
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "1")

    keep_snapshot = [recording("recording-b"), recording("keep", artist="Keep", title="Song", artist_ids=("keep-id",))]
    first = reconcile(config, apply=True, spotify_client=FakeSpotify(keep_snapshot))
    second = reconcile(config, apply=True, spotify_client=FakeSpotify(keep_snapshot))
    assert first.suspected_spotify == 1
    assert second.excluded_spotify == 0
    with connect(config) as con:
        song = con.execute("SELECT * FROM tracks WHERE canonical_key = 'artist::song'").fetchone()
        a = con.execute("SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id = 'recording-a'").fetchone()
        b = con.execute("SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id = 'recording-b'").fetchone()
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id = ?", (song["id"],)).fetchone()
        assert song["status"] == "wanted"
        assert a["in_playlist"] == 0 and b["in_playlist"] == 1
        assert asset["in_playlist"] == 1

    all_removed_snapshot = [recording("keep", artist="Keep", title="Song", artist_ids=("keep-id",))]
    first_all = reconcile(config, apply=True, spotify_client=FakeSpotify(all_removed_snapshot))
    second_all = reconcile(config, apply=True, spotify_client=FakeSpotify(all_removed_snapshot))
    assert first_all.suspected_spotify == 1
    assert second_all.tentative_spotify_removed == 1
    with connect(config) as con:
        song = con.execute("SELECT * FROM tracks WHERE canonical_key = 'artist::song'").fetchone()
        b = con.execute("SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id = 'recording-b'").fetchone()
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id = ?", (song["id"],)).fetchone()
        assert song["status"] == "wanted"
        assert b["in_playlist"] == 0 and asset["in_playlist"] == 0

    reappeared = scan_spotify_playlist(
        config,
        apply=True,
        client=FakeSpotify([recording("recording-b"), recording("keep", artist="Keep", title="Song", artist_ids=("keep-id",))]),
    )
    assert reappeared.linked == 2
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id = ?", (song["id"],)).fetchone()
        b = con.execute("SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id = 'recording-b'").fetchone()
        assert asset["in_playlist"] == 1 and asset["status"] == "review"
        assert b["in_playlist"] == 1 and b["suspected_missing_at"] is None


def test_incomplete_snapshot_does_not_import_or_reconcile(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    seed = recording("existing")
    scan_spotify_playlist(config, apply=True, client=FakeSpotify([seed]))
    with connect(config) as con:
        before = list(con.iterdump())
        set_state(con, "local_baseline_complete", "true")
        set_state(con, "last_local_scan_count", "0")
        set_state(con, "spotify_baseline_complete", "true")
        set_state(con, "last_spotify_playlist_count", "1")
    before_bytes = config.db_path.read_bytes()
    with pytest.raises(RuntimeError, match="Spotify playlist snapshot was incomplete"):
        scan_spotify_playlist(config, apply=True, client=FakeSpotify([], complete=False))
    with connect(config) as con:
        assert con.execute("SELECT in_playlist FROM spotify_playlist_recordings").fetchone()[0] == 1
    assert config.db_path.read_bytes() == before_bytes

    summary = reconcile(
        config,
        apply=True,
        spotify_client=FakeSpotify([], complete=False),
        spotify_snapshot=PlaylistSnapshot("playlist", "incomplete", [], complete=False),
    )
    assert "spotify: spotify playlist pagination did not complete" in summary.refused
    with connect(config) as con:
        assert con.execute("SELECT in_playlist FROM spotify_playlist_recordings").fetchone()[0] == 1


def test_dry_run_preserves_registry_database_bytes(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    seed = [recording("anchor"), recording("ambiguous", artist_ids=("other-id",))]
    scan_spotify_playlist(config, apply=True, client=FakeSpotify(seed))
    before_bytes = config.db_path.read_bytes()
    with connect(config) as con:
        before_dump = list(con.iterdump())
    summary = scan_spotify_playlist(config, apply=False, client=FakeSpotify(seed))
    assert summary.linked == 1 and summary.ambiguous == 1
    assert config.db_path.read_bytes() == before_bytes
    with connect(config) as con:
        assert list(con.iterdump()) == before_dump


def test_reconcile_mass_removal_guard_uses_logical_snapshot_count(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    many_recordings = [recording(f"many-{index:02d}") for index in range(20)]
    missing_logical_tracks = [
        recording(
            f"missing-{index:02d}",
            artist=f"Missing Artist {index}",
            title="Song",
            artist_ids=(f"missing-artist-id-{index}",),
        )
        for index in range(19)
    ]
    initial_snapshot = many_recordings + missing_logical_tracks
    assert scan_spotify_playlist(config, apply=True, client=FakeSpotify(initial_snapshot)).linked == 39
    with connect(config) as con:
        with transaction(con):
            set_state(con, "local_baseline_complete", "true")
            set_state(con, "last_local_scan_count", "0")
            set_state(con, "spotify_baseline_complete", "true")
            # The old state stored physical provider rows. The guard must
            # compare the current logical owner count with the registry's
            # logical baseline rather than making 20 recordings look healthy.
            set_state(con, "last_spotify_playlist_count", "39")
            set_state(con, "last_spotify_scan_at", "2026-01-01T00:00:00Z")
            con.execute(
                "UPDATE spotify_playlist_recordings SET first_seen_at = '2026-01-01T00:00:00Z'"
            )

    summary = reconcile(config, apply=True, spotify_client=FakeSpotify(many_recordings))

    assert summary.refused == [
        "spotify: spotify playlist count is suspiciously low compared to DB playlist assets"
    ]
    assert summary.suspected_spotify == 0
    assert summary.excluded_spotify == 0
    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) FROM tracks WHERE status = 'excluded'").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM spotify_playlist_recordings WHERE in_playlist = 1"
        ).fetchone()[0] == 39


def test_recording_migration_is_repeatable_and_transactional(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE tracks(
            id INTEGER PRIMARY KEY, normalized_artist TEXT NOT NULL, normalized_title TEXT NOT NULL,
            display_artist TEXT NOT NULL, display_title TEXT NOT NULL, canonical_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE spotify_assets(
            id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL, spotify_track_uri TEXT,
            spotify_track_id TEXT, spotify_artist TEXT NOT NULL DEFAULT '', spotify_title TEXT NOT NULL DEFAULT '',
            playlist_id TEXT NOT NULL DEFAULT '', in_playlist INTEGER NOT NULL DEFAULT 0, match_confidence REAL,
            status TEXT NOT NULL, added_at TEXT, last_seen_at TEXT, suspected_missing_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z');
        INSERT INTO tracks VALUES (1, 'artist', 'song', 'Artist', 'Song', 'artist::song', 'wanted', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        INSERT INTO spotify_assets VALUES (1, 1, 'spotify:track:legacy', 'legacy', 'Artist', 'Song', 'playlist', 1, 0.731, 'review', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', NULL, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
        """
    )
    con.commit()
    con.execute("BEGIN")
    migrate_db(con)
    assert con.execute("SELECT COUNT(*) FROM spotify_playlist_recordings").fetchone()[0] == 1
    con.rollback()
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'spotify_playlist_recordings'").fetchone() is None
    assert con.execute("SELECT 1 FROM schema_migrations WHERE version = 3").fetchone() is None
    migrate_db(con)
    con.commit()
    first_dump = list(con.iterdump())
    row = con.execute("SELECT * FROM spotify_playlist_recordings").fetchone()
    assert (row["spotify_track_id"], row["track_id"], row["status"], row["association_status"]) == (
        "legacy",
        1,
        "review",
        "linked",
    )
    migrate_db(con)
    con.commit()
    assert list(con.iterdump()) == first_dump
    con.close()


def test_synthetic_411_recording_collision_has_one_logical_owner(tmp_path):
    config = make_config(tmp_path)
    init_db(config)
    tracks = [
        recording(
            f"recording-{index:03d}",
            album=f"Album {index}",
            isrc=f"ISRC-{index}",
            duration_ms=180000 + index,
        )
        for index in range(411)
    ]
    summary = scan_spotify_playlist(config, apply=True, client=FakeSpotify(tracks))
    assert summary.seen == summary.linked == 411
    assert summary.ambiguous == 0
    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) FROM spotify_playlist_recordings").fetchone()[0] == 411
        assert con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
        assert con.execute("SELECT spotify_track_id FROM spotify_assets").fetchone()[0] == "recording-000"
