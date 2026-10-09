"""Absence evidence must preserve uncertain recording ownership."""

import pytest

from hcr_sync.config import DEFAULTS, Config
from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_spotify_asset, upsert_youtube_asset
from hcr_sync.reconcile import reconcile
from hcr_sync.spotify_sync import PlaylistSnapshot, SpotifyTrack


class FakeSpotify:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def playlist_snapshot(self, playlist_id):
        return self.snapshot

    def remove_tracks(self, playlist_id, uris):
        raise AssertionError("observed user removal must not dispatch a provider removal")


def seed(tmp_path, statuses, confidence=1.0):
    values = dict(DEFAULTS)
    values.update(HCR_DB_PATH=str(tmp_path / "db.sqlite"), HCR_MUSIC_DIR=str(tmp_path / "music"),
                  HCR_TRASH_DIR=str(tmp_path / "trash"), HCR_SPOTIFY_ENABLED="true",
                  HCR_SPOTIFY_PLAYLIST_ID="playlist", HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO="0.4")
    config = Config(values=values, loaded_files=[])
    init_db(config)
    config.music_dir.mkdir()
    audio = config.music_dir / "Artist - Song.mp3"
    audio.write_bytes(b"synthetic audio")
    with connect(config) as con, transaction(con):
        song = ensure_track(con, artist="Artist", title="Song", status="wanted")
        keep = ensure_track(con, artist="Keep", title="Song", status="wanted")
        upsert_youtube_asset(con, track_id=song["id"], file_path=str(audio), file_exists=True,
                             match_confidence=1.0, status="downloaded")
        for spotify_id, status in statuses:
            upsert_spotify_asset(con, track_id=song["id"], playlist_id="playlist",
                                 spotify_track_id=spotify_id, spotify_track_uri=f"spotify:track:{spotify_id}",
                                 spotify_artist="Artist", spotify_title="Song", in_playlist=True,
                                 match_confidence=confidence, status=status, added_at="2026-01-01T00:00:00Z")
        # A logical aggregate alone must not upgrade uncertain individual recordings.
        con.execute("UPDATE spotify_assets SET status='added', match_confidence=? WHERE track_id=?",
                    (confidence, song["id"]))
        upsert_spotify_asset(con, track_id=keep["id"], playlist_id="playlist", spotify_track_id="keep",
                             spotify_track_uri="spotify:track:keep", spotify_artist="Keep", spotify_title="Song",
                             in_playlist=True, match_confidence=1.0, status="added", added_at="2026-01-01T00:00:00Z")
        set_state(con, "local_baseline_complete", "true")
        set_state(con, "spotify_baseline_complete", "true")
        set_state(con, "last_spotify_scan_at", "2026-01-02T00:00:00Z")
        set_state(con, "last_spotify_snapshot_id", "previous")
        set_state(con, "last_spotify_playlist_count", "3")
    return config, song["id"], audio


@pytest.mark.parametrize("statuses,confidence,excluded", [
    ([('a', 'review'), ('b', 'review')], 1.0, False),
    ([('a', 'added'), ('b', 'added')], None, False),
    ([('a', 'review'), ('b', 'added')], 1.0, True),
    ([('b', 'added'), ('a', 'review')], 1.0, True),
])
def test_missing_recordings_use_original_classifications(tmp_path, statuses, confidence, excluded):
    config, track_id, audio = seed(tmp_path, statuses, confidence)
    snapshot = PlaylistSnapshot("playlist", "current", [SpotifyTrack("spotify:track:keep", "keep", "Keep", "Song")])
    first = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot))
    second = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot))
    assert first.suspected_spotify == 2
    assert second.excluded_spotify == int(excluded)
    assert second.tentative_spotify_removed == int(not excluded)
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks WHERE id=?", (track_id,)).fetchone()[0] == ("excluded" if excluded else "wanted")
        assert con.execute("SELECT count(*) FROM spotify_playlist_recordings WHERE track_id=? AND in_playlist=1", (track_id,)).fetchone()[0] == 0
    assert audio.exists() is not excluded


@pytest.mark.parametrize("complete,identified", [(True, False), (False, True)])
def test_unsafe_snapshot_cannot_confirm_absence(tmp_path, complete, identified):
    config, track_id, audio = seed(tmp_path, [('a', 'added'), ('b', 'added')])
    with connect(config) as con, transaction(con):
        con.execute("UPDATE spotify_playlist_recordings SET suspected_missing_at='2026-01-01' WHERE track_id=?", (track_id,))
        con.execute("UPDATE spotify_assets SET suspected_missing_at='2026-01-01' WHERE track_id=?", (track_id,))
        from hcr_sync.spotify_work import save_work
        save_work(con, 'playlist', track_id, 'remove', {'uri': 'spotify:track:b', 'reason': 'local/global exclusion cascade'},
                  work_key='spotify:track:b', state='dispatched')
    snapshot = PlaylistSnapshot("playlist", "unsafe", [SpotifyTrack("spotify:track:a", "a", "Artist", "Song")],
                                complete=complete, identified=identified)
    summary = reconcile(config, apply=True, spotify_client=FakeSpotify(snapshot), force_confirm_deletions=True)
    assert summary.excluded_spotify == summary.suspected_spotify == summary.tentative_spotify_removed == 0
    with connect(config) as con:
        assert con.execute("SELECT status FROM tracks WHERE id=?", (track_id,)).fetchone()[0] == "wanted"
        rows = dict(con.execute("SELECT spotify_track_id,suspected_missing_at FROM spotify_playlist_recordings WHERE track_id=?", (track_id,)))
        assert rows['b'] == '2026-01-01'
        assert rows['a'] == (None if complete else '2026-01-01')
        assert con.execute("SELECT count(*) FROM spotify_playlist_recordings WHERE track_id=? AND in_playlist=1", (track_id,)).fetchone()[0] == 2
        assert con.execute("SELECT value FROM sync_state WHERE key='last_spotify_snapshot_id'").fetchone()[0] == "previous"
        assert con.execute("SELECT value FROM sync_state WHERE key='last_spotify_playlist_count'").fetchone()[0] == "3"
        assert con.execute("SELECT kind,state FROM spotify_pending_work").fetchone()[:] == ('remove', 'dispatched')
    assert audio.exists()


@pytest.mark.parametrize('force_confirm,two_passes', [(True, 'true'), (False, 'false')])
def test_standalone_reconcile_recovers_own_removal_before_absence_cascade(tmp_path, force_confirm, two_passes):
    from hcr_sync.db import mark_excluded, unexclude_track
    from hcr_sync.spotify_work import save_work
    from test_spotify_adapter import item, metadata, response, spotify_http

    config, track_id, audio = seed(tmp_path, [('a', 'added')])
    config.values['HCR_RECONCILE_REQUIRE_TWO_PASSES'] = two_passes
    with connect(config) as con, transaction(con):
        mark_excluded(con, track_id=track_id, source='manual', reason='synthetic initial exclusion')
        save_work(con, 'playlist', track_id, 'remove', {'uri': 'spotify:track:a', 'reason': 'local/global exclusion cascade'},
                  work_key='spotify:track:a', state='dispatched')
        unexclude_track(con, track_id=track_id)
    entries = [item(id='keep', uri='spotify:track:keep', name='Song', artists=[{'id': 'keep', 'name': 'Keep'}])]
    responses = [response(metadata(1, 'removed')), response({'total': 1, 'offset': 0, 'next': None, 'items': entries}),
                 response(metadata(1, 'removed'))]
    with spotify_http(tmp_path, responses) as (client, calls):
        summary = reconcile(config, apply=True, spotify_client=client, force_confirm_deletions=force_confirm)
        assert summary.excluded_spotify == summary.local_trashed == summary.suspected_spotify == 0
        assert summary.spotify_removed == 1
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    assert audio.exists()
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?', (track_id,)).fetchone()[0] == 'wanted'
        assert con.execute('SELECT in_playlist FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone()[0] == 0
        assert con.execute('SELECT file_exists FROM youtube_assets WHERE track_id=?', (track_id,)).fetchone()[0] == 1
        assert con.execute('SELECT count(*) FROM spotify_pending_work').fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM exclusions WHERE track_id=? AND source='spotify_removed'", (track_id,)).fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM events WHERE event_type='spotify_removed_by_user' AND track_id=?", (track_id,)).fetchone()[0] == 0


def test_pending_recovery_failure_stops_absence_processing(tmp_path, monkeypatch):
    import sqlite3
    from hcr_sync import spotify_work
    from test_spotify_adapter import item, metadata, response, spotify_http

    config, track_id, audio = seed(tmp_path, [('a', 'added')])
    with connect(config) as con, transaction(con):
        spotify_work.save_work(con, 'playlist', track_id, 'remove',
                              {'uri': 'spotify:track:a', 'reason': 'local/global exclusion cascade'},
                              work_key='spotify:track:a', state='dispatched')

    def fail_finalization(*args):
        raise sqlite3.OperationalError('synthetic pending finalization failure')

    monkeypatch.setattr(spotify_work, '_finalize_pending', fail_finalization)
    entries = [item(id='keep', uri='spotify:track:keep', name='Song', artists=[{'id': 'keep', 'name': 'Keep'}])]
    responses = [response(metadata(1, 'removed')), response({'total': 1, 'offset': 0, 'next': None, 'items': entries}),
                 response(metadata(1, 'removed'))]
    with spotify_http(tmp_path, responses) as (client, calls):
        with pytest.raises(sqlite3.OperationalError, match='synthetic pending finalization failure'):
            reconcile(config, apply=True, spotify_client=client, force_confirm_deletions=True)
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    assert audio.exists()
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?', (track_id,)).fetchone()[0] == 'wanted'
        assert con.execute('SELECT in_playlist FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone()[0] == 1
        assert con.execute('SELECT count(*) FROM spotify_pending_work').fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM events WHERE event_type='spotify_removed_by_user'").fetchone()[0] == 0
        assert con.execute("SELECT value FROM sync_state WHERE key='last_spotify_snapshot_id'").fetchone()[0] == 'previous'
