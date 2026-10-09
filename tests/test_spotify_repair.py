import json
import os
import sqlite3
import pytest

from hcr_sync.db import add_event, connect, ensure_track, upsert_spotify_asset
from hcr_sync.spotify_repair import repair_scheduling
from test_spotify_recovery import config_for


def seed(config):
    with connect(config) as con:
        source = ensure_track(con, artist='Source', title='Song')
        owner = ensure_track(con, artist='Owner', title='Song')
        upsert_spotify_asset(con, track_id=owner['id'], playlist_id='playlist', spotify_track_id='owned',
                             in_playlist=True, status='added', match_confidence=1.0)
        payload = {'playlist_id': 'playlist', 'spotify_search_last_at': '2001-01-01T00:00:00Z',
                   'spotify_search_attempts': 1, 'spotify_search_next_at': '2001-01-08T00:00:00Z'}
        add_event(con, source['id'], 'spotify_search_completed', 'spotify_sync', payload)
        add_event(con, source['id'], 'ambiguous_spotify_match', 'spotify_sync', {**payload, 'spotify_search_attempts': 99, 'spotify_track_id': 'owned'}, dedupe_key='ambiguous')
        add_event(con, source['id'], 'spotify_request_failed', 'spotify_sync', {'operation': 'search'})
        con.commit()
        return source['id'], owner['id'], list(con.iterdump())


def test_repair_is_evidence_only_backed_up_and_repeat_safe(tmp_path):
    config = config_for(tmp_path)
    source_id, owner_id, before = seed(config)
    preview = repair_scheduling(config)
    assert len(preview['changes']) == 1 and len(preview['skipped']) == 2
    assert len(preview['ownership_holds']) == 1
    assert preview['ownership_holds'][0]['current_asset']['track_id'] == owner_id
    with connect(config) as con:
        assert list(con.iterdump()) == before
    backup = tmp_path / 'rollback.sqlite'
    repair_scheduling(config, apply=True, backup=backup)
    assert os.stat(backup).st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as con:
        assert list(con.iterdump()) == before
    with connect(config) as con:
        owner = con.execute('SELECT * FROM spotify_assets WHERE track_id=?', (owner_id,)).fetchone()
        assert owner['spotify_track_id'] == 'owned' and owner['in_playlist'] and owner['match_confidence'] == 1.0
        source = con.execute('SELECT * FROM spotify_assets WHERE track_id=?', (source_id,)).fetchone()
        assert source['search_attempts'] == 1 and source['search_last_at'] == '2001-01-01T00:00:00Z'
        assert source['spotify_track_id'] is None and source['match_confidence'] is None
    assert repair_scheduling(config)['changes'] == []
    with pytest.raises(FileExistsError):
        repair_scheduling(config, apply=True, backup=backup)


def test_repair_requires_backup_and_preserves_newer_schedule(tmp_path):
    config = config_for(tmp_path)
    source_id, _, before = seed(config)
    with pytest.raises(ValueError, match='backup'):
        repair_scheduling(config, apply=True)
    with connect(config) as con:
        assert list(con.iterdump()) == before
        upsert_spotify_asset(con, track_id=source_id, playlist_id='playlist', in_playlist=False,
            status='review', match_confidence=0.5, search_last_at='2002-01-01T00:00:00Z',
            search_attempts=2, search_next_at='2002-01-15T00:00:00Z', update_search=True)
        con.commit()
    assert repair_scheduling(config)['changes'] == []


def test_sync_preview_does_not_create_or_migrate_database(tmp_path):
    from hcr_sync.spotify_sync import sync_spotify
    from test_spotify_recovery import EchoSpotify
    config = config_for(tmp_path)
    with sqlite3.connect(config.db_path) as con:
        con.execute('DROP TABLE spotify_pending_work')
        con.execute('DELETE FROM schema_migrations WHERE version=4')
        before = list(con.iterdump())
    sync_spotify(config, apply=False, client=EchoSpotify())
    with sqlite3.connect(config.db_path) as con:
        assert list(con.iterdump()) == before
    config.values['HCR_DB_PATH'] = str(tmp_path / 'absent.sqlite')
    assert sync_spotify(config, apply=False, client=EchoSpotify()).added == 0
    assert not config.db_path.exists()
