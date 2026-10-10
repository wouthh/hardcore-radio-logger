"""Real disposable audio proves staged output and crash-safe publication."""
from dataclasses import asdict, replace
import json
import os
import shutil
import subprocess
import sys

import pytest

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.db import connect, ensure_track, init_db, now_utc, transaction
from hcr_sync.youtube_adapter import YouTubeCandidate, YouTubeFailure, YtDlpClient
from hcr_sync.youtube_download import (UnsafeDownloadOutput, prepare_download, prepare_partials,
    publish_output, read_receipt, verify_output, process_is_alive)


CANDIDATE = YouTubeCandidate('Artist - Song', 'https://www.youtube.com/watch?v=abcdefghijk',
                             'abcdefghijk', 'Channel', 125)


@pytest.fixture
def setup(tmp_path):
    config = Config(dict(DEFAULTS, HCR_DB_PATH=str(tmp_path / 'db.sqlite'),
        HCR_MUSIC_DIR=str(tmp_path / 'music'), HCR_DOWNLOAD_TMP_DIR=str(tmp_path / 'partials'),
        HCR_YOUTUBE_DOWNLOAD_ARCHIVE=str(tmp_path / 'archive'), HCR_NODE_RUNTIME=sys.executable))
    init_db(config)
    config.music_dir.mkdir()
    audio = tmp_path / 'fixture.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono',
        '-t', '125', '-b:a', '16k', '-metadata', 'artist=Artist', '-metadata', 'title=Song', str(audio)],
        check=True, timeout=15)
    with connect(config) as con, transaction(con):
        track = ensure_track(con, artist='Artist', title='Song')
        payload = json.dumps({'candidate': asdict(CANDIDATE), 'source_artist': 'Artist', 'source_title': 'Song'})
        con.execute('INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            ('job1', track['id'], CANDIDATE.video_id, payload, 'dispatched', now_utc(), now_utc()))
    return config, audio


def work(config):
    with connect(config) as con:
        return dict(con.execute('SELECT * FROM youtube_pending_work WHERE work_id="job1"').fetchone())


def stage(config, audio):
    path = prepare_download(config, 'job1') / 'output.mp3'
    shutil.copyfile(audio, path)
    receipt = {'video_id': CANDIDATE.video_id, 'title': 'Artist - Song', 'artist': 'Artist',
               'artist_names': ['Artist'], 'track': 'Song', 'duration': 125, 'filepath': str(path)}
    (path.parent / 'receipt.jsonl').write_text(json.dumps(receipt) + '\n')
    with connect(config) as con, transaction(con):
        con.execute('UPDATE youtube_pending_work SET stage_dir=?,output_path=?', (str(path.parent), str(path)))
    return path, receipt


def fake_tool(config, audio, mode='success'):
    executable = audio.parent / 'fixture-ytdlp'
    body = f'''import json,sys,shutil
from pathlib import Path
args=sys.argv
Path({str(audio.parent / 'args.json')!r}).write_text(json.dumps(args))
out=Path(args[args.index('-o')+1].replace('%(ext)s','mp3'))
receipt=Path(args[args.index('--print-to-file')+2])
mode={mode!r}
if mode=='partial':
    Path(str(out)+'.part').write_text('unfinished')
    print('HTTP Error 503',file=sys.stderr)
    sys.exit(1)
if mode=='wrong':
    shutil.copyfile({str(audio)!r},out.parent/'unexpected.mp3')
    sys.exit(0)
shutil.copyfile({str(audio)!r},out)
receipt.write_text(json.dumps(dict(video_id='abcdefghijk',title='Artist - Song',artist='Artist',artist_names=['Artist'],track='Song',duration=125,filepath=str(out)))+'\\n')
'''
    executable.write_text('#!/usr/bin/python3\n' + body)
    executable.chmod(0o700)
    config.values['HCR_YTDLP_BIN'] = str(executable)


def test_real_download_records_pid_exact_receipt_and_configured_partials(setup):
    config, audio = setup
    fake_tool(config, audio)
    output = YtDlpClient(config).download(CANDIDATE, work=work(config))
    stored = work(config)
    assert output == prepare_download(config, 'job1') / 'output.mp3'
    assert read_receipt(config, stored)['video_id'] == CANDIDATE.video_id
    assert stored['process_pid'] and not process_is_alive(stored['process_pid'], stored['process_start'])
    args = json.loads((audio.parent / 'args.json').read_text())
    assert '--continue' in args and '--no-simulate' in args and '--embed-thumbnail' in args
    assert args[args.index('--paths') + 1] == f'temp:{prepare_partials(config,"job1")}'
    assert verify_output(config, CANDIDATE, output, 'Artist', 'Song', read_receipt(config, stored))['duration'] >= 125


@pytest.mark.parametrize('after_link', [False, True])
def test_crash_before_or_after_publication_recovers_exact_destination(setup, monkeypatch, after_link):
    config, audio = setup
    source, _ = stage(config, audio)
    real_link = os.link
    def crash(*args, **kwargs):
        if after_link:
            real_link(*args, **kwargs)
        raise KeyboardInterrupt('synthetic crash')
    monkeypatch.setattr('hcr_sync.youtube_download.os.link', crash)
    with pytest.raises(KeyboardInterrupt):
        publish_output(config, CANDIDATE, work(config), 'Artist', 'Song')
    recorded = work(config)
    assert recorded['output_path'] == str(config.music_dir / 'Artist - Song [abcdefghijk].mp3')
    assert json.loads(recorded['payload_json'])['verified_recording']['video_id']==CANDIDATE.video_id
    assert json.loads(recorded['payload_json'])['candidate']==json.loads(json.dumps(asdict(CANDIDATE)))
    monkeypatch.setattr('hcr_sync.youtube_download.os.link', real_link)
    result = publish_output(config, CANDIDATE, recorded, 'Artist', 'Song')
    assert result.exists() and os.path.samefile(source, result)
    assert len(list(config.music_dir.glob('*.mp3'))) == 1


def test_publication_preserves_unrelated_existing_destination(setup):
    config, audio = setup
    stage(config, audio)
    destination = config.music_dir / 'Artist - Song [abcdefghijk].mp3'
    destination.write_bytes(b'existing owner data')
    with pytest.raises(UnsafeDownloadOutput, match='overwrite'):
        publish_output(config, CANDIDATE, work(config), 'Artist', 'Song')
    assert destination.read_bytes() == b'existing owner data'


def test_partial_failure_and_wrong_output_never_complete(setup):
    config, audio = setup
    fake_tool(config, audio, 'partial')
    with pytest.raises(YouTubeFailure) as error:
        YtDlpClient(config).download(CANDIDATE, work=work(config))
    assert error.value.category == 'transport'
    assert not (prepare_download(config, 'job1') / 'output.mp3').exists()
    fake_tool(config, audio, 'wrong')
    with pytest.raises(UnsafeDownloadOutput, match='exact output'):
        YtDlpClient(config).download(CANDIDATE, work=work(config))


def test_archive_without_verified_output_holds_before_provider_execution(setup):
    config, audio = setup
    fake_tool(config, audio)
    config.path('HCR_YOUTUBE_DOWNLOAD_ARCHIVE').write_text('youtube abcdefghijk\n')
    with pytest.raises(YouTubeFailure) as error:
        YtDlpClient(config).download(CANDIDATE, work=work(config))
    assert error.value.category == 'archive_inconsistent'
    assert not (audio.parent / 'args.json').exists()


def test_symlink_stages_partials_and_external_files_are_rejected(setup):
    config, audio = setup
    hidden = config.music_dir / '.hcr-youtube-work'
    hidden.symlink_to(audio.parent, target_is_directory=True)
    with pytest.raises(UnsafeDownloadOutput):
        prepare_download(config, 'job1')
    hidden.unlink()
    config.path('HCR_DOWNLOAD_TMP_DIR').symlink_to(audio.parent, target_is_directory=True)
    with pytest.raises(UnsafeDownloadOutput):
        prepare_partials(config, 'job1')
    with pytest.raises(UnsafeDownloadOutput):
        verify_output(config, CANDIDATE, audio, 'Artist', 'Song')


def test_receipt_identity_and_audio_metadata_must_match_actual_recording(setup):
    config, audio = setup
    output, receipt = stage(config, audio)
    with pytest.raises(UnsafeDownloadOutput):
        verify_output(config, CANDIDATE, output, 'Other Artist', 'Other Song', receipt)
    (output.parent / 'receipt.jsonl').write_text(json.dumps(dict(receipt, video_id='zyxwvutsrqp')))
    with pytest.raises(UnsafeDownloadOutput, match='identity'):
        read_receipt(config, work(config))
    output.write_bytes(b'not audio')
    with pytest.raises(UnsafeDownloadOutput, match='decoded'):
        verify_output(config, CANDIDATE, output, 'Artist', 'Song', receipt)


def test_legacy_exact_attributed_file_uses_real_audio_tags(setup):
    config, audio = setup
    output = config.music_dir / 'Artist - Song [abcdefghijk].mp3'
    shutil.copyfile(audio, output)
    recorded = work(config)
    recorded['output_path'] = str(output)
    assert publish_output(config, CANDIDATE, recorded, 'Artist', 'Song') == output
    assert json.loads(work(config)['payload_json'])['verified_recording']['artist']=='Artist'


def test_truncated_audio_cannot_use_original_duration_header(setup):
    config, audio = setup
    output, receipt = stage(config, audio)
    raw = output.read_bytes()
    output.write_bytes(raw[:len(raw) // 2])
    with pytest.raises(UnsafeDownloadOutput):
        verify_output(config, CANDIDATE, output, 'Artist', 'Song', receipt)


def three_credit_output(config,audio,tag_artist='North Tone, Signal MC, Orbit',tag_title='Infinity'):
    candidate=replace(CANDIDATE,title='North Tone, Signal MC, Orbit - Infinity',artist='North Tone, Signal MC, Orbit',
                      artist_names=('North Tone','Signal MC','Orbit'),track='Infinity')
    tagged=audio.parent/'tagged.mp3'
    subprocess.run(['ffmpeg','-v','error','-i',str(audio),'-c:a','copy','-metadata',f'artist={tag_artist}',
                    '-metadata',f'title={tag_title}',str(tagged)],check=True,timeout=15)
    path=prepare_download(config,'job1')/'output.mp3'
    shutil.copyfile(tagged,path)
    receipt={'video_id':candidate.video_id,'title':candidate.title,'artist':candidate.artist,
             'artist_names':candidate.artist_names,'track':candidate.track,'duration':125,'filepath':str(path)}
    (path.parent/'receipt.jsonl').write_text(json.dumps(receipt)+'\n')
    with connect(config) as con,transaction(con):
        con.execute('UPDATE youtube_pending_work SET stage_dir=?,output_path=?',(str(path.parent),str(path)))
    return candidate,path,receipt


@pytest.mark.parametrize('stale_search', [False,True])
@pytest.mark.parametrize('tag_separator', [', ','; '])
def test_three_verified_credits_publish_and_satisfy_second_run_without_new_provider_work(setup,stale_search,tag_separator):
    from hcr_sync.db import upsert_youtube_asset
    from hcr_sync.youtube_queue import source_fingerprint
    from hcr_sync.youtube_sync import sync_youtube
    from hcr_sync.youtube_matching import evaluate_candidate
    config,audio=setup
    candidate,path,receipt=three_credit_output(config,audio,tag_separator.join(('North Tone','Signal MC','Orbit')))
    artist='North Tone & Signal MC' if stale_search else 'North Tone & Orbit & Signal MC'
    selected=replace(candidate,artist_names=('North Tone','Signal MC'),artist='North Tone, Signal MC',title='North Tone, Signal MC - Infinity') if stale_search else candidate
    assert evaluate_candidate(artist,'Infinity',selected).accepted
    assert evaluate_candidate(artist,'Infinity',candidate).accepted
    assert verify_output(config,selected,path,artist,'Infinity',receipt)['artist']==candidate.artist
    with connect(config) as con,transaction(con):
        con.execute("UPDATE tracks SET status='excluded'")
        source=ensure_track(con,artist=artist,title='Infinity')
        payload={'candidate':asdict(selected),'source_artist':artist,'source_title':'Infinity',
                 'fingerprint':source_fingerprint(source)}
        con.execute("UPDATE youtube_pending_work SET track_id=?,payload_json=?",(source['id'],json.dumps(payload)))
    published=publish_output(config,selected,work(config),artist,'Infinity')
    stored=json.loads(work(config)['payload_json'])
    assert stored['candidate']==json.loads(json.dumps(asdict(selected)))
    assert stored['verified_recording']['artist_names']==['North Tone','Signal MC','Orbit']
    if tag_separator=='; ':
        from mutagen.easyid3 import EasyID3
        tags=EasyID3(published)
        tags['artist']=['North Tone','Signal MC','Orbit']
        tags.save(v2_version=4)
        from hcr_sync.local_files import _tag_values
        assert _tag_values(published)[0]=='North Tone; Signal MC; Orbit'
    with connect(config) as con,transaction(con):
        con.execute("UPDATE youtube_pending_work SET state='completed'")
        upsert_youtube_asset(con,track_id=source['id'],youtube_video_id=candidate.video_id,file_path=str(published),
                             file_exists=True,status='downloaded',match_confidence=1)
    class NoProvider:
        def search(self,*args): pytest.fail('verified local output must satisfy without search')
        def download(self,*args): pytest.fail('verified local output must satisfy without download')
    assert sync_youtube(config,apply=True,client=NoProvider()).already_local==1
    assert sync_youtube(config,apply=True,client=NoProvider()).already_local==1
    from hcr_sync.db import get_state
    with connect(config) as con:
        asset=con.execute('SELECT id FROM youtube_assets WHERE track_id=?',(source['id'],)).fetchone()
        evidence=json.loads(get_state(con,f'youtube_local_evidence:{asset["id"]}'))
        assert evidence['artist']==tag_separator.join(('North Tone','Signal MC','Orbit'))
        assert evidence['title']=='Infinity'


@pytest.mark.parametrize('tag_artist,tag_title', [('North Tone, Other Artist, Orbit','Infinity'),
    ('North Tone, Orbit','Infinity'),('North Tone, Signal MC, Orbit, Extra Artist','Infinity'),
    ('North Tone, Signal MC, Orbit','Infinity (Other Remix)'),
    ('North Tone; Other Artist; Orbit','Infinity'),('North Tone; Signal MC; Orbit; Extra Artist','Infinity')])
def test_receipt_credit_list_cannot_hide_conflicting_real_mp3_tags(setup,tag_artist,tag_title):
    config,audio=setup
    candidate,path,receipt=three_credit_output(config,audio,tag_artist,tag_title)
    with pytest.raises(UnsafeDownloadOutput,match='requested recording'):
        verify_output(config,candidate,path,'North Tone & Orbit & Signal MC','Infinity',receipt)


def test_crash_before_link_rejects_compatible_destination_with_different_inode(setup, monkeypatch):
    config, audio = setup
    source, _ = stage(config, audio)
    def crash(*args, **kwargs):
        raise KeyboardInterrupt('synthetic crash after destination commit')
    real_link = os.link
    monkeypatch.setattr('hcr_sync.youtube_download.os.link', crash)
    with pytest.raises(KeyboardInterrupt):
        publish_output(config, CANDIDATE, work(config), 'Artist', 'Song')
    recorded = work(config)
    destination = config.music_dir / 'Artist - Song [abcdefghijk].mp3'
    shutil.copyfile(audio, destination)
    # Same audio, metadata and intended filename are not publication provenance.
    original = destination.read_bytes()
    original_inode = destination.stat().st_ino
    assert not os.path.samefile(source, destination)
    monkeypatch.setattr('hcr_sync.youtube_download.os.link', real_link)
    with pytest.raises(UnsafeDownloadOutput, match='staged hard link'):
        publish_output(config, CANDIDATE, recorded, 'Artist', 'Song')
    assert destination.read_bytes() == original
    assert destination.stat().st_ino == original_inode
    assert source.is_file() and not os.path.samefile(source, destination)
    assert work(config) == recorded
    with connect(config) as con:
        assert not con.execute('SELECT 1 FROM youtube_assets').fetchone()


def test_configured_threshold_is_shared_by_verified_completion_local_tags_and_cached_proof(setup):
    from hcr_sync.db import upsert_youtube_asset
    from hcr_sync.youtube_local import local_satisfaction
    from hcr_sync.youtube_matching import evaluate_candidate
    from hcr_sync.youtube_queue import source_fingerprint
    config,audio=setup
    config.values['HCR_YOUTUBE_MATCH_THRESHOLD']='0.88'
    source_title='Silver Midnight Signals Drift Transformation'
    recording_title='Silver Midnight Signals Drift'
    candidate=replace(CANDIDATE,title=f'Artist - {recording_title}',artist='Artist',
                      artist_names=('Artist',),track=recording_title)
    decision=evaluate_candidate('Artist',source_title,candidate,threshold=.88)
    assert decision.accepted and decision.score==pytest.approx(.89)
    assert not evaluate_candidate('Artist',source_title,candidate).accepted
    tagged=audio.parent/'threshold.mp3'
    subprocess.run(['ffmpeg','-v','error','-i',str(audio),'-c:a','copy','-metadata',f'title={recording_title}',str(tagged)],
                   check=True,timeout=15)
    output=prepare_download(config,'job1')/'output.mp3'
    shutil.copyfile(tagged,output)
    receipt={'video_id':candidate.video_id,'title':candidate.title,'artist':'Artist',
             'artist_names':['Artist'],'track':recording_title,'duration':125,'filepath':str(output)}
    (output.parent/'receipt.jsonl').write_text(json.dumps(receipt)+'\n')
    with connect(config) as con,transaction(con):
        source=ensure_track(con,artist='Artist',title=source_title)
        payload={'candidate':asdict(candidate),'source_artist':'Artist','source_title':source_title,
                 'fingerprint':source_fingerprint(source)}
        con.execute('UPDATE youtube_pending_work SET track_id=?,payload_json=?,stage_dir=?,output_path=?',
                    (source['id'],json.dumps(payload),str(output.parent),str(output)))
    published=publish_output(config,candidate,work(config),'Artist',source_title)
    with connect(config) as con,transaction(con):
        con.execute("UPDATE youtube_pending_work SET state='completed'")
        upsert_youtube_asset(con,track_id=source['id'],youtube_video_id=candidate.video_id,file_path=str(published),
                             file_exists=True,status='downloaded',match_confidence=decision.score)
    cache={}
    for threshold,expected in (('.88','satisfied'),('.90','ambiguous'),('.95','ambiguous')):
        config.values['HCR_YOUTUBE_MATCH_THRESHOLD']=threshold
        with connect(config) as con:
            assert local_satisfaction(config,con,source,cache=cache)[0]==expected
        proofs=[value for key,value in cache.items() if key[0]=='completed_recording' and key[-1]==float(threshold)]
        assert len(proofs)==1
        assert (tuple(proofs[0].artist_names) if proofs[0] else None)==(('Artist',) if expected=='satisfied' else None)
    conflicting=replace(candidate,title=candidate.title+' (Other Remix)',track=recording_title+' (Other Remix)')
    assert not evaluate_candidate('Artist',source_title,conflicting,threshold=.50).accepted
    # The same policy also applies to legacy raw tags without completion proof.
    with connect(config) as con,transaction(con):
        payload=json.loads(work(config)['payload_json'])
        payload.pop('verified_recording')
        con.execute('UPDATE youtube_pending_work SET payload_json=?',(json.dumps(payload),))
    for threshold,expected in (('.88','satisfied'),('.90','ambiguous'),('.95','ambiguous')):
        config.values['HCR_YOUTUBE_MATCH_THRESHOLD']=threshold
        with connect(config) as con:
            assert local_satisfaction(config,con,source)[0]==expected
    # A lower score threshold never permits a contradictory recording version.
    from mutagen.easyid3 import EasyID3
    tags=EasyID3(published)
    tags['title']=[recording_title+' (Other Remix)']
    tags.save(v2_version=4)
    config.values['HCR_YOUTUBE_MATCH_THRESHOLD']='0.50'
    with connect(config) as con:
        assert local_satisfaction(config,con,source)[0]=='ambiguous'


@pytest.mark.parametrize('current_threshold,legacy', [('.88',False),('.90',False),('.95',False),('.88',True)])
def test_reconcile_absence_requires_the_threshold_that_verified_local_audio(setup,current_threshold,legacy):
    from hcr_sync.db import get_state, set_state, upsert_youtube_asset
    from hcr_sync.reconcile import reconcile
    from hcr_sync.youtube_local import local_satisfaction
    from hcr_sync.youtube_matching import evaluate_candidate
    config,audio=setup
    source_title='Silver Midnight Signals Drift Transformation'
    recording_title='Silver Midnight Signals Drift'
    candidate=replace(CANDIDATE,title=f'Artist - {recording_title}',artist='Artist',
                      artist_names=('Artist',),track=recording_title)
    decision=evaluate_candidate('Artist',source_title,candidate,threshold=.88)
    assert decision.accepted and decision.score==pytest.approx(.89)
    assert not evaluate_candidate('Artist',source_title,candidate,threshold=.90).accepted
    path=config.music_dir/f'Artist - {recording_title} [{candidate.video_id}].mp3'
    subprocess.run(['ffmpeg','-v','error','-i',str(audio),'-c:a','copy','-metadata',f'title={recording_title}',str(path)],
                   check=True,timeout=15)
    # Keep the scan healthy independently of the removed association.
    present=config.music_dir/'Unrelated - Present.mp3'
    shutil.copyfile(audio,present)
    config.values['HCR_YOUTUBE_MATCH_THRESHOLD']='.88'
    with connect(config) as con,transaction(con):
        source=ensure_track(con,artist='Artist',title=source_title)
        upsert_youtube_asset(con,track_id=source['id'],youtube_video_id=candidate.video_id,file_path=str(path),
                             file_exists=True,status='downloaded',match_confidence=decision.score)
        assert local_satisfaction(config,con,source,persist=True)[0]=='satisfied'
        asset=con.execute('SELECT * FROM youtube_assets WHERE track_id=?',(source['id'],)).fetchone()
        evidence_key=f'youtube_local_evidence:{asset["id"]}'
        evidence=json.loads(get_state(con,evidence_key))
        assert evidence['threshold']==.88
        if legacy:
            evidence.pop('threshold')
            set_state(con,evidence_key,json.dumps(evidence))
        set_state(con,'local_baseline_complete','true')
        set_state(con,'last_local_scan_count','1')
    path.unlink()
    config.values['HCR_YOUTUBE_MATCH_THRESHOLD']=current_threshold
    first=reconcile(config,apply=True,skip_spotify=True)
    second=reconcile(config,apply=True,skip_spotify=True)
    assert not first.refused and not second.refused
    authorized=current_threshold=='.88' and not legacy
    assert first.suspected_local==int(authorized)
    assert second.suspected_local==0
    assert second.excluded_local==int(authorized)
    assert bool(first.planned)==bool(second.planned)==authorized
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?',(source['id'],)).fetchone()[0]==('excluded' if authorized else 'wanted')
        asset=con.execute('SELECT * FROM youtube_assets WHERE track_id=?',(source['id'],)).fetchone()
        if not authorized:
            assert asset['suspected_missing_at'] is None and asset['file_exists']==1
            assert asset['status']=='downloaded' and asset['match_confidence']==decision.score
            assert not con.execute('SELECT 1 FROM exclusions WHERE track_id=?',(source['id'],)).fetchone()
            assert not con.execute("SELECT 1 FROM events WHERE event_type IN ('suspected_local_delete','local_file_deleted_by_user','local_file_deleted','local_file_moved_to_trash')").fetchone()
        else:
            assert asset['file_exists']==0 and asset['status']=='deleted'
        assert json.loads(get_state(con,evidence_key))==evidence
    assert present.is_file()


@pytest.mark.parametrize('title', [
    'Étoile Astral Lumière Cosmos Horizon Crépuscule Mémoire Sillage Océan Vibrations '
    'Galaxie Énergie Aurore Constellation Fréquence Harmonie Nuages Résonance Solstice '
    'Métamorphose Infini / Beyond',
    'Infinity / Beyond',
])
def test_published_sanitized_or_truncated_filename_uses_attributed_tags_without_redundant_search(setup,title):
    from hcr_sync.db import upsert_youtube_asset
    from hcr_sync.youtube_local import local_satisfaction
    from hcr_sync.youtube_queue import source_fingerprint
    from hcr_sync.youtube_sync import sync_youtube
    from mutagen.easyid3 import EasyID3
    config,audio=setup
    names=('North/Tone','Signal MC','Orbit')
    artist='North/Tone & Orbit & Signal MC'
    tagged_artist=', '.join(names)
    candidate=replace(CANDIDATE,title=f'{tagged_artist} - {title}',artist=tagged_artist,
                      artist_names=names,track=title)
    tagged=audio.parent/'publication-tags.mp3'
    subprocess.run(['ffmpeg','-v','error','-i',str(audio),'-c:a','copy','-metadata',f'artist={tagged_artist}',
                    '-metadata',f'title={title}',str(tagged)],check=True,timeout=15)
    output=prepare_download(config,'job1')/'output.mp3'
    shutil.copyfile(tagged,output)
    receipt={'video_id':candidate.video_id,'title':candidate.title,'artist':tagged_artist,
             'artist_names':list(names),'track':title,'duration':125,'filepath':str(output)}
    (output.parent/'receipt.jsonl').write_text(json.dumps(receipt)+'\n')
    with connect(config) as con,transaction(con):
        con.execute("UPDATE tracks SET status='excluded'")
        owner=ensure_track(con,artist=artist,title=title)
        foreign=ensure_track(con,artist=tagged_artist,title=title)
        remix=ensure_track(con,artist=tagged_artist,title=title+' (Other Remix)')
        assert foreign['id']!=owner['id']
        payload={'candidate':asdict(candidate),'source_artist':artist,'source_title':title,
                 'fingerprint':source_fingerprint(owner)}
        con.execute('UPDATE youtube_pending_work SET track_id=?,payload_json=?,stage_dir=?,output_path=?',
                    (owner['id'],json.dumps(payload),str(output.parent),str(output)))
    published=publish_output(config,candidate,work(config),artist,title)
    assert '/' not in published.name
    assert published.name!=f'{artist} - {title} [{candidate.video_id}].mp3'
    if title.startswith('Étoile'):
        assert len(published.stem.removesuffix(f' [{candidate.video_id}]').encode('utf-8'))>=178
        assert 'Beyond' not in published.name
    with connect(config) as con,transaction(con):
        con.execute("UPDATE youtube_pending_work SET state='completed'")
        upsert_youtube_asset(con,track_id=owner['id'],youtube_video_id=candidate.video_id,file_path=str(published),
                             file_exists=True,status='downloaded',match_confidence=1)
        asset=dict(con.execute('SELECT * FROM youtube_assets').fetchone())
        assert local_satisfaction(config,con,owner,persist=True)[0]=='satisfied'
        assert local_satisfaction(config,con,foreign)[0]=='satisfied'
        assert local_satisfaction(config,con,remix)[0]=='none'
        con.execute("UPDATE tracks SET status='excluded' WHERE id=?",(remix['id'],))
    class NoProvider:
        def search(self,*args): pytest.fail('attributed published tags must avoid redundant searches')
        def download(self,*args): pytest.fail('attributed published tags must avoid redundant downloads')
    for _ in range(2):
        result=sync_youtube(config,apply=True,client=NoProvider())
        assert result.already_local==2 and result.searched==result.download_attempts==0
    with connect(config) as con:
        assert dict(con.execute('SELECT * FROM youtube_assets').fetchone())==asset
    # Current contradictory or missing tags cannot borrow completed credits.
    tags=EasyID3(published)
    for field,value in [('title',title+' (Other Remix)'),('artist','Wrong Artist, Signal MC, Orbit'),('artist',None)]:
        tags['artist']=[tagged_artist]
        tags['title']=[title]
        if value is None:
            del tags[field]
        else:
            tags[field]=[value]
        tags.save(v2_version=4)
        with connect(config) as con:
            assert local_satisfaction(config,con,owner)[0]=='ambiguous'
            assert local_satisfaction(config,con,foreign)[0]!='satisfied'
    tags['artist']=[tagged_artist]
    tags['title']=[title]
    tags.save(v2_version=4)
    # Exact work owner, path, video and current source fingerprint stay required.
    original=work(config)
    for column,value in [('track_id',foreign['id']),('output_path',str(published)+'wrong'),
                         ('video_id','wrongvideo12'),('payload_json',json.dumps(dict(json.loads(original['payload_json']),fingerprint='stale')))]:
        with connect(config) as con,transaction(con):
            con.execute(f'UPDATE youtube_pending_work SET {column}=?',(value,))
            assert local_satisfaction(config,con,owner)[0]!='satisfied'
            assert local_satisfaction(config,con,foreign)[0]!='satisfied'
            con.execute(f'UPDATE youtube_pending_work SET {column}=?',(original[column],))


@pytest.mark.parametrize('artist',['Artist/Collective','Sound, Vision'])
def test_flat_verified_completion_recognizes_long_publication_without_inventing_credits(setup,artist):
    from hcr_sync.db import upsert_youtube_asset
    from hcr_sync.youtube_local import local_satisfaction
    from hcr_sync.youtube_queue import source_fingerprint
    from hcr_sync.youtube_sync import sync_youtube
    from mutagen.easyid3 import EasyID3
    config,audio=setup
    title='Étoile Astral Lumière Cosmos Horizon Crépuscule Mémoire Sillage Océan Vibrations Galaxie Énergie Aurore Constellation Fréquence Harmonie Nuages Résonance Solstice Métamorphose Infini / Beyond'
    candidate=replace(CANDIDATE,title=f'{artist} - {title}',artist=artist,artist_names=(),track=title)
    tagged=audio.parent/'flat-tags.mp3'
    subprocess.run(['ffmpeg','-v','error','-i',str(audio),'-c:a','copy','-metadata',f'artist={artist}',
                    '-metadata',f'title={title}',str(tagged)],check=True,timeout=15)
    output=prepare_download(config,'job1')/'output.mp3'
    shutil.copyfile(tagged,output)
    receipt={'video_id':candidate.video_id,'title':candidate.title,'artist':artist,
             'artist_names':None,'track':title,'duration':125,'filepath':str(output)}
    (output.parent/'receipt.jsonl').write_text(json.dumps(receipt)+'\n')
    with connect(config) as con,transaction(con):
        con.execute("UPDATE tracks SET status='excluded'")
        owner=ensure_track(con,artist=artist,title=title)
        payload={'candidate':asdict(candidate),'source_artist':artist,'source_title':title,
                 'fingerprint':source_fingerprint(owner)}
        con.execute('UPDATE youtube_pending_work SET track_id=?,payload_json=?,stage_dir=?,output_path=?',
                    (owner['id'],json.dumps(payload),str(output.parent),str(output)))
    published=publish_output(config,candidate,work(config),artist,title)
    assert 'Beyond' not in published.name
    assert json.loads(work(config)['payload_json'])['verified_recording']['artist_names']==[]
    with connect(config) as con,transaction(con):
        con.execute("UPDATE youtube_pending_work SET state='completed'")
        upsert_youtube_asset(con,track_id=owner['id'],youtube_video_id=candidate.video_id,file_path=str(published),
                             file_exists=True,status='downloaded',match_confidence=1)
        cache={}
        assert local_satisfaction(config,con,owner,cache=cache,persist=True)[0]=='satisfied'
        proof=next(value for key,value in cache.items() if key[0]=='completed_recording')
        assert not proof.artist_names  # Flat compound names do not become a structured list.
    class NoProvider:
        def search(self,*args): pytest.fail('flat verified publication must not repeat search')
        def download(self,*args): pytest.fail('flat verified publication must not repeat download')
    for _ in range(2):
        result=sync_youtube(config,apply=True,client=NoProvider())
        assert result.already_local==1 and result.searched==result.download_attempts==0
    tags=EasyID3(published)
    conflicting_artist=artist.replace(',', ' &') if ',' in artist else 'Other Artist'
    for field,value in [('artist',conflicting_artist),('title',title+' (Other Remix)'),('artist',None),('title',None)]:
        tags['artist']=[artist]
        tags['title']=[title]
        if value is None:
            del tags[field]
        else:
            tags[field]=[value]
        tags.save(v2_version=4)
        with connect(config) as con:
            assert local_satisfaction(config,con,owner)[0]=='ambiguous'


@pytest.mark.parametrize('heading,metadata,source_artist,source_title,accepted,expected_artist,expected_title',[
    ('Artist - Song',{},'Artist','Song',True,'Artist','Song'),
    ('Artist – Song',{},'Artist','Song',True,'Artist','Song'),
    ('Artist — Song',{},'Artist','Song',True,'Artist','Song'),
    ('Sound, Vision - Song',{},'Sound, Vision','Song',True,'Sound, Vision','Song'),
    ('Artist & MC Voice - Song (Alpha Remix)',{},'Artist & MC Voice','Song (Alpha Remix)',True,'Artist & MC Voice','Song (Alpha Remix)'),
    ('Artist - Song',{'artist':'Artist','track':'Song'},'Artist','Song',True,'Artist','Song'),
    ('Artist & MC Voice - Song',{'artists':['Artist','MC Voice'],'track':'Song'},'Artist & MC Voice','Song',True,'Artist, MC Voice','Song'),
    ('Song',{},'Artist','Song',False,'','Song'),
    ('Artist - Song',{'artist':'Wrong Artist'},'Artist','Song',False,'Wrong Artist','Song'),
    ('Artist - Song',{'track':'Song (Other Remix)'},'Artist','Song',False,'Artist','Song (Other Remix)'),
    ('Other Artist - Song',{},'Artist','Song',False,'Other Artist','Song'),
    ('Artist - Song (Alpha Remix)',{},'Artist','Song (Beta Remix)',False,'Artist','Song (Alpha Remix)'),
    ('Artist & Collaborator - Song',{},'Artist, Collaborator','Song',False,'Artist & Collaborator','Song'),
])
def test_installed_download_cli_embeds_actual_heading_preserving_raw_metadata_and_conflicts(
        setup,heading,metadata,source_artist,source_title,accepted,expected_artist,expected_title):
    import shlex
    from pathlib import Path
    from mutagen.easyid3 import EasyID3
    from hcr_sync.db import upsert_youtube_asset
    from hcr_sync.youtube_local import local_satisfaction
    from hcr_sync.youtube_queue import source_fingerprint
    executable=shutil.which('yt-dlp')
    if not executable:
        pytest.skip('installed yt-dlp is required for its offline download fixture')
    config,audio=setup
    interpreter=shlex.split(Path(executable).read_text().splitlines()[0][2:])
    fixture=audio.parent/'offline-download-ytdlp'
    fixture.write_text('#!'+' '.join(interpreter)+'\n'+f'''
import sys
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.postprocessor.metadataparser import MetadataParserPP
from pathlib import Path
original_run=MetadataParserPP.run
def capture(self,info):
    Path({str(audio.parent/'actual-metadata.json')!r}).write_text(__import__('json').dumps({{key:info.get(key) for key in ('title','artist','artists','track')}}))
    return original_run(self,info)
MetadataParserPP.run=capture
class FixtureIE(InfoExtractor):
    _VALID_URL=r'https://www.youtube.com/watch\\?v=(?P<id>abcdefghijk)'
    def _real_extract(self,url):
        return dict({metadata!r},id='abcdefghijk',title={heading!r},duration=125,uploader='Artist',
                    formats=[{{'url':{audio.as_uri()!r},'ext':'mp3','format_id':'1','acodec':'mp3','vcodec':'none'}}])
yt_dlp.YoutubeDL.add_default_info_extractors=lambda self:self.add_info_extractor(FixtureIE())
# Only this controlled extractor is registered; file access is fixture-local.
sys.argv.insert(1,'--enable-file-urls')
yt_dlp.main()
''')
    fixture.chmod(0o700)
    config.values['HCR_YTDLP_BIN']=str(fixture)
    candidate=replace(CANDIDATE,title=f'{source_artist} - {source_title}')
    with connect(config) as con,transaction(con):
        con.execute("UPDATE tracks SET status='excluded'")
        owner=ensure_track(con,artist=source_artist,title=source_title)
        con.execute("UPDATE tracks SET status='wanted' WHERE id=?",(owner['id'],))
        owner=con.execute('SELECT * FROM tracks WHERE id=?',(owner['id'],)).fetchone()
        payload={'candidate':asdict(candidate),'source_artist':source_artist,'source_title':source_title,
                 'fingerprint':source_fingerprint(owner)}
        con.execute('UPDATE youtube_pending_work SET track_id=?,payload_json=?',(owner['id'],json.dumps(payload)))
    output=YtDlpClient(config).download(candidate,work=work(config))
    receipt=read_receipt(config,work(config))
    extracted=json.loads((audio.parent/'actual-metadata.json').read_text())
    assert receipt['title']==extracted['title']==heading
    assert receipt['artist']==extracted['artist']
    assert receipt['artist_names']==extracted['artists']
    assert receipt['track']==extracted['track']
    tags=EasyID3(output)
    assert tags.get('artist',[''])[0]==expected_artist
    assert tags['title']==[expected_title]
    if not accepted:
        with pytest.raises(UnsafeDownloadOutput,match='requested recording'):
            publish_output(config,candidate,work(config),source_artist,source_title)
        assert output.is_file()
        assert not list(config.music_dir.glob('*.mp3'))
        return
    published=publish_output(config,candidate,work(config),source_artist,source_title)
    with connect(config) as con,transaction(con):
        con.execute("UPDATE youtube_pending_work SET state='completed'")
        upsert_youtube_asset(con,track_id=owner['id'],youtube_video_id=candidate.video_id,file_path=str(published),
                             file_exists=True,status='downloaded',match_confidence=1)
        assert local_satisfaction(config,con,owner,persist=True)[0]=='satisfied'
        verified=json.loads(con.execute('SELECT payload_json FROM youtube_pending_work').fetchone()[0])['verified_recording']
        assert verified['title']==heading
        assert verified['artist']==(receipt['artist'] or '')
        assert verified['track']==(receipt['track'] or '')
