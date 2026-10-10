"""Restart boundaries exercise the production scheduler and real adapter."""
from dataclasses import asdict
from datetime import datetime,timezone
import json
import sys

import pytest

from hcr_sync.db import connect,transaction,ensure_track,get_state
from hcr_sync.youtube_queue import source_fingerprint,stamp
from hcr_sync.youtube_sync import sync_youtube,recover_youtube_pending
from hcr_sync.youtube_adapter import YtDlpClient
from test_youtube_download_recovery import setup, fake_tool, CANDIDATE


def initialize_schedule(config):
    with connect(config) as con,transaction(con):
        track=con.execute('SELECT * FROM tracks LIMIT 1').fetchone()
        con.execute('INSERT INTO youtube_schedule(track_id,source_fingerprint,phase,next_eligible_at,candidate_json,candidate_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            (track['id'],source_fingerprint(track),'download',stamp(),json.dumps(asdict(CANDIDATE)),stamp(),stamp()))
        payload=json.loads(con.execute('SELECT payload_json FROM youtube_pending_work').fetchone()[0])
        payload['fingerprint']=source_fingerprint(track)
        con.execute('UPDATE youtube_pending_work SET state="prepared",payload_json=?',(json.dumps(payload),))
    return track['id']


def test_real_adapter_scheduler_recovers_remote_completion_before_local_commit(setup,monkeypatch):
    config,audio=setup
    source=initialize_schedule(config)
    fake_tool(config,audio)
    import hcr_sync.youtube_sync as sync
    original=sync._complete_download
    monkeypatch.setattr(sync,'_complete_download',lambda *a: (_ for _ in ()).throw(KeyboardInterrupt('crash before local commit')))
    with pytest.raises(KeyboardInterrupt):
        sync_youtube(config,apply=True,client=YtDlpClient(config))
    with connect(config) as con:
        assert con.execute('SELECT state FROM youtube_pending_work').fetchone()[0]=='dispatched'
        assert con.execute('SELECT COUNT(*) FROM youtube_assets').fetchone()[0]==0
        assert get_state(con,'youtube_queue_cursor')=='2'
    monkeypatch.setattr(sync,'_complete_download',original)
    recovered=recover_youtube_pending(config)
    assert recovered.downloaded==1
    # An executable that would fail proves restart performs no provider work.
    config.values['HCR_YTDLP_BIN']=str(audio.parent/'missing')
    assert recover_youtube_pending(config).downloaded==0
    with connect(config) as con:
        row=con.execute('SELECT * FROM youtube_assets').fetchone()
        assert row['track_id']==source and row['file_exists']==1
        assert con.execute('SELECT state FROM youtube_pending_work').fetchone()[0]=='completed'
        assert con.execute('SELECT download_attempts FROM youtube_schedule').fetchone()[0]==1
        assert get_state(con,'youtube_queue_cursor')=='0'


def test_search_commit_failure_keeps_lane_and_restart_records_failure_not_no_match(setup,monkeypatch):
    config,_=setup
    with connect(config) as con,transaction(con):
        con.execute('DELETE FROM youtube_pending_work')
    class Empty:
        def search(self,*args): return []
    import hcr_sync.youtube_sync as sync
    original=sync._mark_youtube_review
    monkeypatch.setattr(sync,'_mark_youtube_review',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('local commit failed')))
    with pytest.raises(RuntimeError,match='local commit failed'):
        sync_youtube(config,apply=True,client=Empty())
    with connect(config) as con:
        assert con.execute('SELECT search_attempts FROM youtube_schedule').fetchone()[0]==0
        assert get_state(con,'youtube_queue_cursor')=='0'
        assert con.execute('SELECT COUNT(*) FROM youtube_assets').fetchone()[0]==0
    monkeypatch.setattr(sync,'_mark_youtube_review',original)
    recover_youtube_pending(config)
    with connect(config) as con:
        row=con.execute('SELECT * FROM youtube_schedule').fetchone()
        assert row['search_attempts']==row['search_failures']==1
        assert row['unsuccessful_matches']==0
        assert json.loads(row['decision_json'])['outcome']=='failure'


def test_real_adapter_normalized_search_reaches_verified_download(setup):
    config,audio=setup
    with connect(config) as con,transaction(con):
        con.execute('DELETE FROM youtube_pending_work')
        con.execute('UPDATE tracks SET display_title=?',('Song (Original Mix)',))
    fake_tool(config,audio)
    executable=config.get('HCR_YTDLP_BIN')
    from pathlib import Path
    path=Path(executable)
    original=path.read_text().split('\n',1)[1]
    search=json.dumps({'_type':'playlist','entries':[{'id':CANDIDATE.video_id,'title':CANDIDATE.title,'duration':125}]})
    path.write_text('#!/usr/bin/python3\nimport sys\nif "--dump-single-json" in sys.argv:\n print('+repr(search)+')\n sys.exit(0)\n'+original)
    summary=sync_youtube(config,apply=True,client=YtDlpClient(config))
    assert summary.searched==summary.download_starts==summary.downloaded==1
    with connect(config) as con:
        row=con.execute('SELECT * FROM youtube_assets').fetchone()
        assert row['file_exists']==1 and row['match_confidence']==1
        assert con.execute('SELECT state FROM youtube_pending_work').fetchone()[0]=='completed'
        assert con.execute('SELECT search_attempts FROM youtube_schedule').fetchone()[0]==1


def test_real_adapter_failed_search_is_counted_in_degraded_summary(setup):
    config, audio = setup
    with connect(config) as con, transaction(con):
        con.execute('DELETE FROM youtube_pending_work')
    executable = audio.parent / 'failed-search'
    executable.write_text('#!/usr/bin/python3\nimport sys\nsys.exit(1)\n')
    executable.chmod(0o700)
    config.values['HCR_YTDLP_BIN'] = str(executable)
    summary = sync_youtube(config, apply=True, client=YtDlpClient(config))
    assert summary.searched == summary.search_invocations == 1
    assert summary.download_starts == summary.downloaded == 0
    assert summary.degraded and summary.deferred == 1


@pytest.mark.parametrize('tool_failure', ['missing', 'ffprobe', 'ffmpeg'])
def test_verification_tool_failure_preserves_receipt_for_resume_and_offline_recovery(setup, monkeypatch, tool_failure):
    from types import SimpleNamespace
    from test_youtube_download_recovery import stage
    from hcr_sync.cli import cmd_youtube
    from hcr_sync.youtube_queue import pause_state
    import hcr_sync.youtube_download as download
    config, audio = setup
    initialize_schedule(config)
    output, _ = stage(config, audio)
    original = download.shutil.which
    broken = audio.parent / 'incompatible-verifier'
    broken.write_text('#!/usr/bin/python3\nimport sys\nprint("no such option", file=sys.stderr)\nsys.exit(2)\n')
    broken.chmod(0o700)
    def selected_tool(name):
        if tool_failure == 'missing' and name == 'ffprobe':
            return None
        return str(broken) if name == tool_failure else original(name)
    monkeypatch.setattr(download.shutil, 'which', selected_tool)
    category = 'missing_tool' if tool_failure == 'missing' else 'configuration'
    summary = recover_youtube_pending(config)
    assert summary.degraded == category and output.is_file()
    with connect(config) as con:
        assert con.execute('SELECT state FROM youtube_pending_work').fetchone()[0] == 'prepared'
        assert con.execute('SELECT phase FROM youtube_schedule').fetchone()[0] == 'download'
        assert pause_state(con)['operator_required']
    assert recover_youtube_pending(config).degraded == category
    monkeypatch.setattr(download.shutil, 'which', original)
    checks = []
    monkeypatch.setattr('hcr_sync.youtube_adapter.local_tool_check', lambda c: checks.append(c))
    assert cmd_youtube(SimpleNamespace(youtube_command='resume', apply=True), config) == 0
    assert checks == [config]
    # Verification uses the existing receipt and audio, never a new provider client.
    assert recover_youtube_pending(config).downloaded == 1
    with connect(config) as con:
        assert con.execute('SELECT state FROM youtube_pending_work').fetchone()[0] == 'completed'
        assert con.execute('SELECT file_exists FROM youtube_assets').fetchone()[0] == 1
