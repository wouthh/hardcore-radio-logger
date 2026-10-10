"""Local recording evidence cannot be replaced by a database association."""
from pathlib import Path
import json

import pytest

from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_youtube_asset
from hcr_sync.reconcile import reconcile
from hcr_sync.youtube_local import association_allows_absence, local_satisfaction
from hcr_sync.youtube_queue import source_fingerprint
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


def credited_candidate():
    return {'video_id':'local123456','title':'North Tone, Signal MC, Orbit - Infinity',
            'url':'https://www.youtube.com/watch?v=local123456','channel':'','duration':180,
            'artist':'North Tone, Signal MC, Orbit','track':'Infinity',
            'artist_names':['North Tone','Signal MC','Orbit']}


@pytest.mark.parametrize('bad_proof', [None,'owner','path','video','candidate_video','source','state',
    'candidate_heading','candidate_artist','candidate_version','fingerprint','malformed'])
def test_structured_local_credits_require_exact_completed_owned_work(tmp_path,monkeypatch,bad_proof):
    config,source_id,_,path=seed(tmp_path,'North Tone & Orbit & Signal MC - Infinity [local123456].mp3',
        source_artist='North Tone & Orbit & Signal MC',source_title='Infinity')
    monkeypatch.setattr(youtube_local,'_tag_values',lambda path: ('North Tone, Signal MC, Orbit','Infinity'))
    with connect(config) as con,transaction(con):
        owner=ensure_track(con,artist='Other Owner',title='Other Recording')['id'] if bad_proof=='owner' else source_id
        payload={'source_artist':'North Tone & Orbit & Signal MC','source_title':'Infinity',
                 'candidate':credited_candidate()}
        payload['fingerprint']=source_fingerprint(con.execute('SELECT * FROM tracks WHERE id=?',(source_id,)).fetchone())
        if bad_proof=='candidate_video': payload['candidate']['video_id']='wrong123456'
        if bad_proof=='source': payload['source_artist']='Other Owner'
        if bad_proof=='candidate_heading': payload['candidate']['title']='Other Artist - Infinity'
        if bad_proof=='candidate_artist': payload['candidate']['artist']='North Tone, Other Artist, Orbit'
        if bad_proof=='candidate_version': payload['candidate']['title']='North Tone, Signal MC, Orbit - Infinity (Other Remix)'
        if bad_proof=='fingerprint': payload['fingerprint']='unverified'
        con.execute('INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,state,created_at,updated_at,output_path) VALUES (?,?,?,?,?,?,?,?)',
            ('finished',owner,'wrong123456' if bad_proof=='video' else 'local123456','malformed' if bad_proof=='malformed' else json.dumps(payload),
             'prepared' if bad_proof=='state' else 'completed','fixture','fixture',str(path)+'wrong' if bad_proof=='path' else str(path)))
    assert satisfaction(config,source_id)[0]==('satisfied' if bad_proof is None else 'ambiguous')


@pytest.mark.parametrize('tag_artist,tag_title', [('North Tone, Other Artist, Orbit','Infinity'),
    ('North Tone, Orbit','Infinity'),('North Tone, Signal MC, Orbit, Extra Artist','Infinity'),
    ('North Tone, Signal MC, Orbit','Infinity (Other Remix)')])
def test_completed_work_cannot_hide_contradictory_actual_tags(tmp_path,monkeypatch,tag_artist,tag_title):
    config,source_id,_,path=seed(tmp_path,'North Tone & Orbit & Signal MC - Infinity [local123456].mp3',
        source_artist='North Tone & Orbit & Signal MC',source_title='Infinity')
    monkeypatch.setattr(youtube_local,'_tag_values',lambda path: (tag_artist,tag_title))
    payload={'source_artist':'North Tone & Orbit & Signal MC','source_title':'Infinity',
             'candidate':credited_candidate()}
    with connect(config) as con,transaction(con):
        payload['fingerprint']=source_fingerprint(con.execute('SELECT * FROM tracks WHERE id=?',(source_id,)).fetchone())
        con.execute('INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,state,created_at,updated_at,output_path) VALUES (?,?,?,?,?,?,?,?)',
            ('finished',source_id,'local123456',json.dumps(payload),'completed','fixture','fixture',str(path)))
    assert satisfaction(config,source_id,persist=True)[0]=='ambiguous'


@pytest.mark.parametrize('target_title,expected', [('Infinity (Original Mix)','satisfied'),('Infinity (Other Remix)','none')])
def test_completed_owner_credit_proof_can_satisfy_equivalent_foreign_source_without_transfer(tmp_path,monkeypatch,target_title,expected):
    config,owner_id,_,path=seed(tmp_path,'North Tone & Orbit & Signal MC - Infinity [local123456].mp3',
        source_artist='North Tone & Orbit & Signal MC',source_title='Infinity')
    monkeypatch.setattr(youtube_local,'_tag_values',lambda path: ('North Tone, Signal MC, Orbit','Infinity'))
    with connect(config) as con,transaction(con):
        owner=con.execute('SELECT * FROM tracks WHERE id=?',(owner_id,)).fetchone()
        target=ensure_track(con,artist='Orbit, Signal MC, North Tone',title=target_title)
        payload={'source_artist':owner['display_artist'],'source_title':owner['display_title'],
                 'fingerprint':source_fingerprint(owner),
                 'candidate':credited_candidate()}
        con.execute('INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,state,created_at,updated_at,output_path) VALUES (?,?,?,?,?,?,?,?)',
            ('finished',owner_id,'local123456',json.dumps(payload),'completed','fixture','fixture',str(path)))
    status,asset=satisfaction(config,target['id'])
    assert status==expected
    if asset:
        assert asset['track_id']==owner_id
    with connect(config) as con:
        assert con.execute('SELECT track_id FROM youtube_assets').fetchone()[0]==owner_id


def test_credit_cache_does_not_reuse_proof_after_owner_input_changes(tmp_path,monkeypatch):
    config,source_id,_,path=seed(tmp_path,'North Tone & Orbit & Signal MC - Infinity [local123456].mp3',
        source_artist='North Tone & Orbit & Signal MC',source_title='Infinity')
    monkeypatch.setattr(youtube_local,'_tag_values',lambda path: ('North Tone, Signal MC, Orbit','Infinity'))
    with connect(config) as con,transaction(con):
        source=con.execute('SELECT * FROM tracks WHERE id=?',(source_id,)).fetchone()
        payload={'source_artist':source['display_artist'],'source_title':source['display_title'],
                 'fingerprint':source_fingerprint(source),'candidate':credited_candidate()}
        con.execute('INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,state,created_at,updated_at,output_path) VALUES (?,?,?,?,?,?,?,?)',
            ('finished',source_id,'local123456',json.dumps(payload),'completed','fixture','fixture',str(path)))
    cache={}
    assert satisfaction(config,source_id,cache=cache)[0]=='satisfied'
    with connect(config) as con,transaction(con):
        con.execute('UPDATE tracks SET display_artist=? WHERE id=?',('Orbit & North Tone & Signal MC',source_id))
    assert satisfaction(config,source_id,cache=cache)[0]=='ambiguous'


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


@pytest.mark.parametrize('absolute_asset_path', [True, False])
def test_relative_music_directory_preserves_present_published_asset_across_reconciliation(tmp_path, monkeypatch, absolute_asset_path):
    from hcr_sync.db import upsert_spotify_asset
    from hcr_sync.spotify_adapter import SpotifyTrack
    from test_spotify_reconcile_youtube import FakeSpotify
    from test_youtube_duplicates import audio_file
    config, source_id, _, path = seed(tmp_path, 'Synthetic Artist - Night Signal [local123456].mp3')
    audio_file(path)
    monkeypatch.chdir(tmp_path)
    config.values.update(HCR_MUSIC_DIR='./music', HCR_SPOTIFY_ENABLED='true', HCR_SPOTIFY_PLAYLIST_ID='playlist')
    stored_path = str(path) if absolute_asset_path else str(path.relative_to(tmp_path))
    spotify = FakeSpotify(snapshot_tracks=[SpotifyTrack('spotify:track:present', 'present', 'Synthetic Artist', 'Night Signal')])
    with connect(config) as con, transaction(con):
        con.execute('UPDATE youtube_assets SET file_path=?', (stored_path,))
        set_state(con, 'local_baseline_complete', 'true')
        set_state(con, 'last_local_scan_count', '1')
        set_state(con, 'spotify_baseline_complete', 'true')
        set_state(con, 'last_spotify_playlist_count', '1')
        upsert_spotify_asset(con, track_id=source_id, playlist_id='playlist', spotify_track_id='present',
            spotify_track_uri='spotify:track:present', spotify_artist='Synthetic Artist', spotify_title='Night Signal',
            in_playlist=True, match_confidence=1, status='added')
    for _ in range(2):
        result = reconcile(config, apply=True, spotify_client=spotify)
        assert result.suspected_local == result.excluded_local == result.spotify_removed == 0
        assert not any(action.kind == 'local_deleted' for action in result.planned)
    with connect(config) as con:
        asset = con.execute('SELECT * FROM youtube_assets WHERE track_id=?', (source_id,)).fetchone()
        assert asset['file_path'] == stored_path and asset['file_exists'] == 1 and asset['status'] == 'downloaded'
        assert asset['suspected_missing_at'] is None and asset['match_confidence'] == 1
        assert con.execute('SELECT status FROM tracks WHERE id=?', (source_id,)).fetchone()[0] == 'wanted'
        assert con.execute("SELECT count(*) FROM events WHERE event_type IN ('suspected_local_delete','local_file_deleted_by_user')").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM spotify_pending_work WHERE kind='remove'").fetchone()[0] == 0
    assert spotify.removed == [] and path.exists()


def test_relative_music_directory_still_confirms_genuinely_missing_verified_asset(tmp_path, monkeypatch):
    config, source_id, _, path = seed(tmp_path, 'Synthetic Artist - Night Signal [local123456].mp3')
    assert satisfaction(config, source_id, persist=True)[0] == 'satisfied'
    monkeypatch.chdir(tmp_path)
    config.values['HCR_MUSIC_DIR'] = './music'
    with connect(config) as con, transaction(con):
        set_state(con, 'local_baseline_complete', 'true')
        set_state(con, 'last_local_scan_count', '1')
    path.unlink()
    (config.music_dir/'Unrelated - Still Present.mp3').write_bytes(b'fixture')
    first = reconcile(config, apply=True, force_mass_delete=True, skip_spotify=True)
    second = reconcile(config, apply=True, force_mass_delete=True, skip_spotify=True)
    assert first.suspected_local == 1 and first.excluded_local == 0
    assert second.excluded_local == 1
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?', (source_id,)).fetchone()[0] == 'excluded'
        asset = con.execute('SELECT * FROM youtube_assets WHERE track_id=?', (source_id,)).fetchone()
        assert asset['file_exists'] == 0 and asset['status'] == 'deleted'
