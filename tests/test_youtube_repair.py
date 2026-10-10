from datetime import datetime, timedelta
import json
import sqlite3
import stat

import pytest

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.db import add_event, connect, ensure_track, init_db, transaction, upsert_youtube_asset
from hcr_sync.youtube_queue import source_fingerprint
from hcr_sync.youtube_repair import repair_scheduling

SEED = '2026-01-01T00:00:00+00:00'


def fixture(tmp_path):
    config = Config(dict(DEFAULTS, HCR_DB_PATH=str(tmp_path/'db.sqlite'), HCR_MUSIC_DIR=str(tmp_path/'music')))
    init_db(config)
    config.music_dir.mkdir()
    return config


def held(config, artist='Artist', title='Song', *, actor='youtube_sync', payload=None):
    with connect(config) as con, transaction(con):
        track_id = ensure_track(con, artist=artist, title=title)['id']
        upsert_youtube_asset(con, track_id=track_id, file_exists=False, status='review', match_confidence=.4)
        add_event(con, track_id, 'ambiguous_youtube_match', actor, {'score': .4} if payload is None else payload)
    return track_id


def preserved(config):
    with sqlite3.connect(config.db_path) as con:
        return {table: con.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in ('tracks','events','youtube_assets','exclusions','sync_state','sqlite_sequence')}


def backup_dir(tmp_path):
    parent = tmp_path/'backup'
    parent.mkdir(mode=0o700)
    return parent/'before.sqlite'


def test_preview_readonly_without_queue_schema_and_missing_title(tmp_path):
    config = fixture(tmp_path)
    held(config, title='')
    with sqlite3.connect(config.db_path) as con:
        con.execute('DROP TABLE youtube_schedule')
    before = config.db_path.read_bytes()
    result = repair_scheduling(config, seed_at=SEED)
    assert result['counts']['algorithmic'] == 1
    assert result['backup'] is None
    assert config.db_path.read_bytes() == before
    with sqlite3.connect(config.db_path) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_schedule'").fetchone()


def test_apply_backup_history_and_one_time_seed(tmp_path):
    config = fixture(tmp_path)
    track_id = held(config)
    original = preserved(config)
    backup = backup_dir(tmp_path)
    result = repair_scheduling(config, apply=True, backup_path=backup, seed_at=SEED)
    assert len(result['changes']) == 1
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with sqlite3.connect(backup) as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        for table, rows in original.items():
            assert con.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() == rows
    assert preserved(config) == original
    with connect(config) as con:
        schedule = dict(con.execute('SELECT * FROM youtube_schedule WHERE track_id=?',(track_id,)).fetchone())
        track = con.execute('SELECT * FROM tracks WHERE id=?',(track_id,)).fetchone()
        assert schedule['source_fingerprint'] == source_fingerprint(track)
        assert schedule['phase'] == 'search_retry'
        assert schedule['search_attempts'] == schedule['unsuccessful_matches'] == 0
        due = datetime.fromisoformat(schedule['next_eligible_at'])
        assert datetime.fromisoformat(SEED)+timedelta(days=1) <= due < datetime.fromisoformat(SEED)+timedelta(days=7)
    repeat = repair_scheduling(config, seed_at='2027-01-01T00:00:00+00:00')
    assert repeat['changes'] == []
    assert repeat['counts']['deferred'] == 1


@pytest.mark.parametrize('case', ['manual', 'ownership', 'deletion', 'unknown', 'placeholder', 'nontrack'])
def test_stronger_holds_never_seed(tmp_path, case):
    config = fixture(tmp_path)
    track_id = held(config, title='Unknown Title' if case == 'placeholder' else 'Full Set' if case == 'nontrack' else 'Song')
    with connect(config) as con, transaction(con):
        if case == 'manual':
            con.execute("INSERT INTO exclusions(track_id,reason,source,created_at) VALUES (?,'operator hold','manual',?)",(track_id,SEED))
        if case == 'ownership':
            add_event(con, track_id, 'youtube_candidate_already_linked','youtube_sync',{})
        if case == 'deletion':
            con.execute("UPDATE youtube_assets SET suspected_missing_at=? WHERE track_id=?",(SEED,track_id))
        if case == 'unknown':
            add_event(con,track_id,'ambiguous_youtube_match','untrusted',{'score':.1})
    result = repair_scheduling(config,seed_at=SEED)
    assert not result['changes']
    assert result['counts']['uncertain' if case == 'unknown' else 'manualownership'] == 1


def test_existing_unclassified_row_never_moves_earlier_or_resets_history(tmp_path):
    config = fixture(tmp_path)
    track_id = held(config)
    with connect(config) as con, transaction(con):
        track = con.execute('SELECT * FROM tracks WHERE id=?',(track_id,)).fetchone()
        con.execute("INSERT INTO youtube_schedule(track_id,source_fingerprint,phase,next_eligible_at,hold_origin,search_attempts,updated_at) VALUES (?,?,'held',?,'legacy_unclassified',3,?)",(track_id,source_fingerprint(track),'2028-01-01T00:00:00+00:00',SEED))
    repair_scheduling(config, apply=True,backup_path=backup_dir(tmp_path),seed_at=SEED)
    with connect(config) as con:
        row = con.execute('SELECT * FROM youtube_schedule').fetchone()
        assert row['next_eligible_at'] == '2028-01-01T00:00:00+00:00'
        assert row['search_attempts'] == 3


def test_apply_requires_new_private_backup_and_rolls_back(tmp_path):
    config = fixture(tmp_path)
    held(config)
    held(config,artist='Second')
    original = preserved(config)
    with pytest.raises(ValueError,match='backup_path'):
        repair_scheduling(config,apply=True)
    backup = backup_dir(tmp_path)
    backup.touch()
    with pytest.raises(FileExistsError):
        repair_scheduling(config,apply=True,backup_path=backup,seed_at=SEED)
    backup.unlink()
    with sqlite3.connect(config.db_path) as con:
        con.execute("CREATE TRIGGER fail_second BEFORE INSERT ON youtube_schedule WHEN NEW.track_id=2 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='synthetic failure'):
        repair_scheduling(config,apply=True,backup_path=backup,seed_at=SEED)
    with sqlite3.connect(config.db_path) as con:
        assert con.execute('SELECT COUNT(*) FROM youtube_schedule').fetchone()[0] == 0
    assert preserved(config) == original
    assert backup.is_file()


def test_invalid_backup_evidence_prevents_repair(tmp_path):
    config = fixture(tmp_path)
    held(config)
    with sqlite3.connect(config.db_path) as con:
        con.execute('UPDATE youtube_assets SET track_id=999')
    original = preserved(config)
    backup = backup_dir(tmp_path)
    with pytest.raises(RuntimeError,match='backup verification'):
        repair_scheduling(config,apply=True,backup_path=backup,seed_at=SEED)
    assert preserved(config) == original
    with sqlite3.connect(config.db_path) as con:
        assert con.execute('SELECT COUNT(*) FROM youtube_schedule').fetchone()[0] == 0


def test_backup_directory_must_be_owner_only(tmp_path):
    config = fixture(tmp_path)
    held(config)
    backup = backup_dir(tmp_path)
    backup.parent.chmod(0o755)
    with pytest.raises(ValueError,match='owner-only'):
        repair_scheduling(config,apply=True,backup_path=backup,seed_at=SEED)
    assert not backup.exists()


def test_association_seen_is_not_manual_but_pending_is_deferred(tmp_path):
    config = fixture(tmp_path)
    track_id = held(config)
    with connect(config) as con, transaction(con):
        add_event(con,track_id,'local_file_seen','local_scan',{})
        add_event(con,track_id,'local_file_path_updated','local_scan',{})
    assert repair_scheduling(config,seed_at=SEED)['counts']['algorithmic'] == 1
    with connect(config) as con, transaction(con):
        con.execute("INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,state,created_at,updated_at) VALUES ('pending',?,'candidate','{}','dispatched',?,?)",(track_id,SEED,SEED))
    assert repair_scheduling(config,seed_at=SEED)['counts']['deferred'] == 1


@pytest.mark.parametrize('local_title,local_artist,category', [
    ('Song','Artist','local-satisfied'),
    ('Song','','uncertain'),
    ('Song (Other Remix)','Artist','algorithmic'),
])
def test_local_recording_evidence_controls_repair(tmp_path,monkeypatch,local_title,local_artist,category):
    from hcr_sync.local_files import LocalAudioFile
    import hcr_sync.youtube_local as local
    config=fixture(tmp_path)
    source=held(config)
    path=config.music_dir/'synthetic.mp3'
    path.write_bytes(b'synthetic fixture')
    with connect(config) as con, transaction(con):
        upsert_youtube_asset(con,track_id=source,file_path=str(path),file_exists=True,status='downloaded',match_confidence=.4)
    monkeypatch.setattr(local,'inspect_audio_file',lambda filename:LocalAudioFile(filename,local_artist,local_title))
    original=preserved(config)
    result=repair_scheduling(config,seed_at=SEED)
    assert result['counts'][category]==1
    assert len(result['changes'])==int(category=='algorithmic')
    assert preserved(config)==original
