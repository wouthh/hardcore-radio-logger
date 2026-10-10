"""Local recording evidence cannot be replaced by a database association."""
from pathlib import Path

import pytest

from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_youtube_asset
from hcr_sync.reconcile import reconcile
from hcr_sync.youtube_local import association_allows_absence, local_satisfaction
from hcr_sync import youtube_local
from test_youtube_duplicates import make_config


def seed(tmp_path, filename, source_artist='Synthetic Artist', source_title='Night Signal', *, foreign=False, video_id='local123456'):
    config = make_config(tmp_path)
    init_db(config)
    config.music_dir.mkdir()
    path = config.music_dir / filename
    path.write_bytes(b'local fixture')
    with connect(config) as con, transaction(con):
        source = ensure_track(con, artist=source_artist, title=source_title)
        owner = ensure_track(con, artist='Different Owner', title='Different Recording') if foreign else source
        upsert_youtube_asset(con, track_id=owner['id'], file_path=str(path), file_exists=True,
                             status='downloaded', youtube_video_id=video_id, match_confidence=1.0)
        return config, source['id'], owner['id'], path


def satisfaction(config, source_id, **kwargs):
    with connect(config) as con, transaction(con):
        source = con.execute('SELECT * FROM tracks WHERE id=?', (source_id,)).fetchone()
        return local_satisfaction(config, con, source, **kwargs)


@pytest.mark.parametrize('filename,source_title,expected', [
    ('Synthetic Artist - Night Signal (Original Mix) [local123456].mp3', 'Night Signal', 'satisfied'),
    ('Synthetic Artist - Night Signal [local123456].mp3', 'Night Signal (Extended Mix)', 'satisfied'),
    ('Synthetic Artist - Night Signal (Alpha Remix) [local123456].mp3', 'Night Signal (Beta Remix)', 'different'),
    ('Synthetic Artist - Night Signal (Hard Refix) [local123456].mp3', 'Night Signal', 'different'),
    ('Night Signal [local123456].mp3', 'Night Signal', 'ambiguous'),
    ('Unrelated Producer - Night Signal [local123456].mp3', 'Night Signal', 'ambiguous'),
])
def test_actual_local_metadata_controls_satisfaction_even_for_own_association(tmp_path, filename, source_title, expected):
    config, source_id, _, path = seed(tmp_path, filename, source_title=source_title)
    status, asset = satisfaction(config, source_id, persist=True)
    assert status == expected and asset is not None
    with connect(config) as con:
        assert association_allows_absence(con, asset) is (expected == 'satisfied')
        assert con.execute('SELECT status FROM tracks WHERE id=?', (source_id,)).fetchone()[0] == 'wanted'
    assert path.exists()


def test_actual_recording_can_satisfy_another_source_without_changing_owner(tmp_path):
    config, source_id, owner_id, path = seed(tmp_path,
        'Synthetic Artist - Night Signal (Original Mix) [local123456].mp3', foreign=True)
    status, asset = satisfaction(config, source_id)
    assert status == 'satisfied' and asset['track_id'] == owner_id
    with connect(config) as con:
        assert con.execute('SELECT track_id FROM youtube_assets').fetchone()[0] == owner_id
        assert con.execute('SELECT count(*) FROM tracks').fetchone()[0] == 2
    assert path.exists()


def test_idless_opt_in_requires_recording_evidence_and_provider_identity(tmp_path):
    config, source_id, _, path = seed(tmp_path,
        'Synthetic Artist - Night Signal.mp3', video_id='')
    assert satisfaction(config, source_id)[0] == 'satisfied'
    assert satisfaction(config, source_id, require_youtube_id=True)[0] == 'none'
    assert path.exists()


@pytest.mark.parametrize('missing,symlink', [(True, False), (False, True)])
def test_missing_files_and_symlinks_do_not_prove_local_satisfaction(tmp_path, missing, symlink):
    config, source_id, _, path = seed(tmp_path, 'Synthetic Artist - Night Signal [local123456].mp3')
    if missing:
        path.unlink()
    if symlink:
        target = tmp_path / 'target.mp3'
        path.rename(target)
        path.symlink_to(target)
    assert satisfaction(config, source_id)[0] == ('ambiguous' if symlink else 'none')


def test_stat_cache_reuses_unchanged_files_and_reinspects_changed_files(tmp_path, monkeypatch):
    config, source_id, _, path = seed(tmp_path, 'Synthetic Artist - Night Signal [local123456].mp3')
    original = youtube_local.inspect_audio_file
    calls = []
    def inspect(current):
        calls.append(current)
        return original(current)
    monkeypatch.setattr(youtube_local, 'inspect_audio_file', inspect)
    cache = {}
    assert satisfaction(config, source_id, cache=cache)[0] == 'satisfied'
    assert satisfaction(config, source_id, cache=cache)[0] == 'satisfied'
    assert calls == [path]
    path.write_bytes(b'changed local fixture content')
    assert satisfaction(config, source_id, cache=cache)[0] == 'satisfied'
    assert calls == [path, path]


@pytest.mark.parametrize('filename', ['Synthetic Artist - Night Signal (Alpha Remix) [local123456].mp3',
                                    'Night Signal [local123456].mp3'])
def test_false_or_ambiguous_local_association_cannot_advance_removal_confirmation(tmp_path, filename):
    config, source_id, _, path = seed(tmp_path, filename, source_title='Night Signal (Beta Remix)')
    assert satisfaction(config, source_id, persist=True)[0] in {'different', 'ambiguous'}
    with connect(config) as con, transaction(con):
        set_state(con, 'local_baseline_complete', 'true')
        set_state(con, 'last_local_file_count', '1')
    path.unlink()
    for _ in range(2):
        result = reconcile(config, apply=True, force_mass_delete=True, skip_spotify=True)
        assert result.suspected_local == 0
    with connect(config) as con:
        asset = con.execute('SELECT * FROM youtube_assets WHERE track_id=?', (source_id,)).fetchone()
        assert asset['suspected_missing_at'] is None and asset['file_exists'] == 1
        assert con.execute('SELECT status FROM tracks WHERE id=?', (source_id,)).fetchone()[0] == 'wanted'
        assert con.execute("SELECT count(*) FROM events WHERE event_type='suspected_local_delete'").fetchone()[0] == 0


def test_legacy_unknown_missing_association_cannot_confirm_user_removal(tmp_path):
    config,source_id,_,path=seed(tmp_path,'Unknown local metadata [local123456].mp3')
    path.unlink()
    (config.music_dir/'Unrelated - Present.mp3').write_bytes(b'fixture')
    with connect(config) as con,transaction(con):
        set_state(con,'local_baseline_complete','true')
        set_state(con,'last_local_scan_count','1')
    from hcr_sync.reconcile import reconcile
    first=reconcile(config,apply=True,skip_spotify=True)
    second=reconcile(config,apply=True,skip_spotify=True)
    assert first.suspected_local==second.suspected_local==second.excluded_local==0
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?',(source_id,)).fetchone()[0]=='wanted'
        assert not con.execute('SELECT suspected_missing_at FROM youtube_assets').fetchone()[0]


def test_automatic_exclusion_does_not_trash_wrong_recording(tmp_path):
    config,source_id,_,path=seed(tmp_path,'Synthetic Artist - Night Signal (Alpha Remix) [local123456].mp3',source_title='Night Signal (Beta Remix)')
    from hcr_sync.db import mark_excluded
    from hcr_sync.reconcile import _cascade_local,ReconcileSummary
    with connect(config) as con,transaction(con):
        mark_excluded(con,track_id=source_id,source='spotify_removed',reason='confirmed source removal')
        summary=ReconcileSummary()
        _cascade_local(con,config,source_id,summary)
        assert con.execute('SELECT file_exists FROM youtube_assets').fetchone()[0]==1
    assert path.exists() and summary.local_trashed==0


def test_one_removed_copy_cannot_exclude_source_still_confirmed_locally(tmp_path):
    config,source_id,_,first=seed(tmp_path,'Synthetic Artist - Night Signal [local123456].mp3')
    second=config.music_dir/'Synthetic Artist - Night Signal (Extended Mix) [other123456].mp3'
    second.write_bytes(b'fixture')
    with connect(config) as con,transaction(con):
        upsert_youtube_asset(con,track_id=source_id,file_path=str(second),file_exists=True,status='downloaded',youtube_video_id='other123456',match_confidence=1)
        source=con.execute('SELECT * FROM tracks WHERE id=?',(source_id,)).fetchone()
        local_satisfaction(config,con,source,persist=True)
        set_state(con,'local_baseline_complete','true')
    first.unlink()
    reconcile(config,apply=True,skip_spotify=True)
    result=reconcile(config,apply=True,skip_spotify=True)
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?',(source_id,)).fetchone()[0]=='wanted'
    assert result.excluded_local==0 and second.exists()


def test_failed_candidate_evidence_never_replaces_established_asset(tmp_path):
    config,source_id,_,path=seed(tmp_path,'Synthetic Artist - Night Signal (Alpha Remix) [local123456].mp3',source_title='Night Signal (Beta Remix)')
    from hcr_sync.youtube_sync import YouTubeCandidate,_mark_youtube_error,_ownership_conflict
    candidate=YouTubeCandidate('Synthetic Artist - Night Signal (Beta Remix)','https://www.youtube.com/watch?v=local123456','local123456','Channel',180)
    with connect(config) as con,transaction(con):
        original=dict(con.execute('SELECT * FROM youtube_assets').fetchone())
        assert _ownership_conflict(con,source_id,candidate)['id']==original['id']
        _mark_youtube_error(con,source_id,candidate,score=.90,error=RuntimeError('synthetic retry failure'))
        assert dict(con.execute('SELECT * FROM youtube_assets').fetchone())==original
    assert path.exists()


def test_unverified_error_is_not_exclusive_recording_ownership(tmp_path):
    config,source_id,_,path=seed(tmp_path,'Synthetic Artist - Night Signal [local123456].mp3')
    from hcr_sync.youtube_sync import YouTubeCandidate,_ownership_conflict
    candidate=YouTubeCandidate('Other Artist - Other Song','https://www.youtube.com/watch?v=error123456','error123456','Channel',180)
    with connect(config) as con,transaction(con):
        owner=ensure_track(con,artist='Prior Artist',title='Prior Song')
        upsert_youtube_asset(con,track_id=owner['id'],youtube_video_id=candidate.video_id,file_exists=False,status='error',match_confidence=.9)
        assert _ownership_conflict(con,source_id,candidate) is None


def test_deleted_established_recording_retains_ownership_hold(tmp_path):
    config,source_id,_,path=seed(tmp_path,'Synthetic Artist - Night Signal [local123456].mp3')
    from hcr_sync.youtube_sync import YouTubeCandidate,_ownership_conflict
    candidate=YouTubeCandidate('Synthetic Artist - Night Signal','https://www.youtube.com/watch?v=local123456','local123456','Channel',180)
    with connect(config) as con,transaction(con):
        other=ensure_track(con,artist='Other Artist',title='Another Song')
        con.execute("UPDATE youtube_assets SET status='deleted',file_exists=0 WHERE track_id=?",(source_id,))
        assert _ownership_conflict(con,other['id'],candidate)['track_id']==source_id


def test_cross_source_filter_avoids_unrelated_full_comparisons_without_skipping_versions(tmp_path, monkeypatch):
    config, source_id, _, path = seed(tmp_path,
        'Synthetic Artist - Unrelated Recording [local123456].mp3', foreign=True)
    original = youtube_local.compare_recordings
    calls = []
    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(youtube_local, 'compare_recordings', counted)
    assert satisfaction(config, source_id)[0] == 'none'
    assert calls == []
    renamed = path.with_name('Synthetic Artist - Night Signal (Hard Refix) [local123456].mp3')
    path.rename(renamed)
    with connect(config) as con, transaction(con):
        con.execute('UPDATE youtube_assets SET file_path=?', (str(renamed),))
    assert satisfaction(config, source_id)[0] == 'none'
    assert calls and not original(*calls[-1]).accepted
