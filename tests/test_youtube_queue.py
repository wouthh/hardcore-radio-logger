"""Persistent scheduling regressions using isolated databases and fake providers."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_youtube_asset
from hcr_sync.youtube_queue import choose, source_fingerprint, stamp

NOW = datetime(2026,1,1,tzinfo=timezone.utc)


def fixture(tmp_path, **overrides):
    values=dict(DEFAULTS,HCR_DB_PATH=str(tmp_path/'db.sqlite'),HCR_MUSIC_DIR=str(tmp_path/'music'),
                HCR_YOUTUBE_SEARCH_LIMIT='20',HCR_YOUTUBE_DOWNLOAD_LIMIT='20')
    values.update(overrides)
    config=Config(values)
    init_db(config)
    config.music_dir.mkdir()
    return config


def track(config, artist='Artist', title='Song'):
    with connect(config) as con, transaction(con):
        return ensure_track(con,artist=artist,title=title)['id']


def schedule(config, track_id, phase, *, due=NOW, **fields):
    with connect(config) as con, transaction(con):
        source=con.execute('SELECT * FROM tracks WHERE id=?',(track_id,)).fetchone()
        con.execute('INSERT INTO youtube_schedule(track_id,source_fingerprint,phase,next_eligible_at,updated_at) VALUES (?,?,?,?,?)',
                    (track_id,source_fingerprint(source),phase,stamp(due),stamp(NOW)))
        if fields:
            con.execute('UPDATE youtube_schedule SET '+','.join(f'{key}=?' for key in fields)+' WHERE track_id=?',(*fields.values(),track_id))


def row(config,track_id):
    with connect(config) as con:
        return dict(con.execute('SELECT * FROM youtube_schedule WHERE track_id=?',(track_id,)).fetchone())


class Fake:
    def __init__(self, failure=None):
        self.failure=failure
        self.searches=[]
        self.downloads=[]

    def search(self,artist,title):
        self.searches.append((artist,title))
        if self.failure:
            raise self.failure
        return []

    def download(self,candidate):
        self.downloads.append(candidate.video_id)
        raise RuntimeError('synthetic download outage')


def run(config,client,now=NOW):
    from hcr_sync.youtube_sync import sync_youtube
    return sync_youtube(config,apply=True,client=client,now=now)


def test_no_match_seven_then_fourteen_days_and_restart(tmp_path):
    config=fixture(tmp_path)
    source=track(config)
    first=Fake()
    run(config,first)
    assert first.searches==[('Artist','Song')]
    saved=row(config,source)
    assert saved['unsuccessful_matches']==saved['search_attempts']==1
    assert saved['next_eligible_at']==stamp(NOW+timedelta(days=7))
    future=Fake()
    run(config,future,NOW+timedelta(days=7)-timedelta(seconds=1))
    assert future.searches==[]
    run(config,future,NOW+timedelta(days=7))
    saved=row(config,source)
    assert saved['unsuccessful_matches']==saved['search_attempts']==2
    assert saved['next_eligible_at']==stamp(NOW+timedelta(days=21))


def test_transient_search_is_fifteen_minutes_without_review(tmp_path):
    from hcr_sync.youtube_adapter import YouTubeFailure
    config=fixture(tmp_path)
    source=track(config)
    run(config,Fake(YouTubeFailure('transient','synthetic')))
    saved=row(config,source)
    assert saved['search_attempts']==saved['search_failures']==1
    assert saved['unsuccessful_matches']==0
    assert saved['next_eligible_at']==stamp(NOW+timedelta(minutes=15))
    with connect(config) as con:
        assert not con.execute("SELECT 1 FROM youtube_assets WHERE status='review'").fetchone()


def test_fair_lane_cycles_survive_restarts_and_empty_lanes(tmp_path):
    config=fixture(tmp_path)
    sources=[track(config,artist=f'Artist {i}') for i in range(6)]
    for i,source in enumerate(sources):
        schedule(config,source,('first_search','search_retry','download')[i%3])
    observed=[]
    from hcr_sync.youtube_queue import advance
    for _ in sources:
        with connect(config) as con, transaction(con):
            lane,selected=choose(con,NOW)
            observed.append(lane)
            con.execute('UPDATE youtube_schedule SET next_eligible_at=? WHERE track_id=?',(stamp(NOW+timedelta(days=1)),selected['track_id']))
            advance(con,lane)
    assert observed==[0,1,2,0,1,2]
    with connect(config) as con, transaction(con):
        con.execute("UPDATE youtube_schedule SET next_eligible_at=? WHERE phase='search_retry'",(stamp(NOW),))
        set_state(con,'youtube_queue_cursor','2')
    with connect(config) as con:
        assert choose(con,NOW)[0]==1


def test_short_runs_keep_selected_cursor_without_counting_deferred_attempt(tmp_path):
    config=fixture(tmp_path,HCR_YOUTUBE_SEARCH_LIMIT='1')
    first=track(config,'First')
    second=track(config,'Second')
    schedule(config,first,'first_search')
    schedule(config,second,'search_retry')
    client=Fake()
    summary=run(config,client)
    assert summary.searched==1
    assert row(config,second)['search_attempts']==0
    with connect(config) as con:
        assert con.execute("SELECT value FROM sync_state WHERE key='youtube_queue_cursor'").fetchone()[0]=='1'
    run(config,client)
    assert client.searches==[('First','Song'),('Second','Song')]


def test_restriction_pauses_twelve_hours_and_one_probe(tmp_path):
    from hcr_sync.youtube_adapter import YouTubeFailure
    config=fixture(tmp_path)
    track(config,'First')
    track(config,'Second')
    denied=Fake(YouTubeFailure('restriction','synthetic'))
    run(config,denied)
    assert len(denied.searches)==1
    with connect(config) as con:
        pause=json.loads(con.execute("SELECT value FROM sync_state WHERE key='youtube_provider_pause'").fetchone()[0])
    assert pause['until']==stamp(NOW+timedelta(hours=12))
    fresh=Fake()
    run(config,fresh,NOW+timedelta(hours=11))
    assert fresh.searches==[]
    run(config,fresh,NOW+timedelta(hours=12))
    assert len(fresh.searches)==1


@pytest.mark.parametrize('count', [1,2])
def test_distinct_transport_sources_control_provider_pause(tmp_path,count):
    from hcr_sync.youtube_adapter import YouTubeFailure
    config=fixture(tmp_path)
    for i in range(count):
        track(config,f'Artist {i}')
    client=Fake(YouTubeFailure('transport','synthetic'))
    run(config,client)
    assert len(client.searches)==count
    with connect(config) as con:
        value=con.execute("SELECT value FROM sync_state WHERE key='youtube_provider_pause'").fetchone()
    assert bool(value and json.loads(value[0]).get('until'))==(count==2)


def test_manual_legacy_placeholder_and_excluded_sources_do_not_search(tmp_path):
    config=fixture(tmp_path)
    manual=track(config,'Manual')
    schedule(config,manual,'held',hold_origin='manual')
    legacy=track(config,'Legacy')
    with connect(config) as con, transaction(con):
        upsert_youtube_asset(con,track_id=legacy,file_exists=False,status='review',match_confidence=.2)
    track(config,'Unknown Artist','Unknown Title')
    excluded=track(config,'Excluded')
    with connect(config) as con, transaction(con):
        con.execute("UPDATE tracks SET status='excluded' WHERE id=?",(excluded,))
    client=Fake()
    run(config,client)
    assert client.searches==[]
    assert row(config,manual)['hold_origin']=='manual'
    assert row(config,legacy)['hold_origin']=='legacy_unclassified'


def qualified():
    from hcr_sync.youtube_adapter import YouTubeCandidate
    return YouTubeCandidate(title='Artist - Song',url='https://www.youtube.com/watch?v=abcdefghijk',
                            video_id='abcdefghijk',channel='Artist',duration=120)


class Qualified(Fake):
    def search(self,artist,title):
        self.searches.append((artist,title))
        return [qualified()]


def test_download_backoff_reuses_candidate_without_search(tmp_path):
    config=fixture(tmp_path)
    source=track(config)
    client=Qualified()
    run(config,client)
    first=row(config,source)
    assert first['phase']=='download'
    assert first['search_attempts']==first['download_attempts']==1
    assert first['download_failures']==1
    assert first['next_eligible_at']==stamp(NOW+timedelta(minutes=15))
    assert client.searches==[('Artist','Song')]
    run(config,client,NOW+timedelta(minutes=15)-timedelta(seconds=1))
    assert len(client.downloads)==1
    summary=run(config,client,NOW+timedelta(minutes=15))
    assert client.searches==[('Artist','Song')]
    assert len(client.downloads)==2
    assert summary.candidate_reused==1
    second=row(config,source)
    assert second['next_eligible_at']==stamp(NOW+timedelta(minutes=75))
    assert second['download_attempts']==2


def test_elapsed_budget_deferral_preserves_attempts_and_lane(tmp_path,monkeypatch):
    import hcr_sync.youtube_sync as module
    config=fixture(tmp_path)
    source=track(config)
    schedule(config,source,'search_retry')
    times=iter([0,10000])
    monkeypatch.setattr(module.time,'monotonic',lambda: next(times))
    client=Fake()
    summary=run(config,client)
    assert summary.degraded=='work_budget'
    assert client.searches==[]
    assert row(config,source)['search_attempts']==0
    with connect(config) as con:
        assert con.execute("SELECT value FROM sync_state WHERE key='youtube_queue_cursor'").fetchone()[0]=='1'


def test_expired_transport_window_does_not_pause_different_source(tmp_path):
    from hcr_sync.youtube_adapter import YouTubeFailure
    config=fixture(tmp_path,HCR_YOUTUBE_SEARCH_LIMIT='1')
    track(config,'First')
    second=track(config,'Second')
    schedule(config,second,'first_search',due=NOW+timedelta(minutes=31))
    run(config,Fake(YouTubeFailure('transport','synthetic')))
    with connect(config) as con, transaction(con):
        con.execute("UPDATE youtube_schedule SET next_eligible_at=? WHERE track_id!=?",(stamp(NOW+timedelta(days=1)),second))
    run(config,Fake(YouTubeFailure('transport','synthetic')),NOW+timedelta(minutes=31))
    with connect(config) as con:
        value=con.execute("SELECT value FROM sync_state WHERE key='youtube_provider_pause'").fetchone()
    assert not (value and json.loads(value[0]).get('until'))


def test_repaired_algorithmic_retry_becomes_due(tmp_path):
    from hcr_sync.db import add_event
    from hcr_sync.youtube_repair import repair_scheduling
    config=fixture(tmp_path)
    source=track(config)
    with connect(config) as con, transaction(con):
        upsert_youtube_asset(con,track_id=source,file_exists=False,status='review',match_confidence=.2)
        add_event(con,source,'ambiguous_youtube_match','youtube_sync',{'score':.2})
    backup_parent=tmp_path/'backup'
    backup_parent.mkdir(mode=0o700)
    result=repair_scheduling(config,apply=True,backup_path=backup_parent/'before.sqlite',seed_at=stamp(NOW))
    due=datetime.fromisoformat(result['changes'][0]['next_eligible_at'])
    client=Fake()
    run(config,client,due-timedelta(seconds=1))
    assert client.searches==[]
    run(config,client,due)
    assert client.searches==[('Artist','Song')]
    assert row(config,source)['search_attempts']==1


def test_qualified_download_requires_real_decodable_tagged_audio(tmp_path):
    import subprocess
    import wave
    config=fixture(tmp_path)
    source=track(config)
    wav=tmp_path/'synthetic.wav'
    with wave.open(str(wav),'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\x00\x00'*(120*8000))
    output=config.music_dir/'Artist - Song [abcdefghijk].mp3'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(wav),'-metadata','artist=Artist',
                    '-metadata','title=Song','-c:a','libmp3lame',str(output)],check=True,timeout=15)
    class Successful(Qualified):
        def download(self,candidate):
            self.downloads.append(candidate.video_id)
            return output
    client=Successful()
    summary=run(config,client)
    assert summary.downloaded==1
    assert client.downloads==['abcdefghijk']
    saved=row(config,source)
    assert saved['phase']=='held' and saved['hold_origin']=='local_satisfied'
    assert saved['download_attempts']==1
    with connect(config) as con:
        assert con.execute("SELECT COUNT(*) FROM youtube_pending_work WHERE state='completed'").fetchone()[0]==1
        asset=con.execute('SELECT * FROM youtube_assets WHERE track_id=?',(source,)).fetchone()
        assert asset['file_exists']==1 and asset['status']=='downloaded'
    fresh=Successful()
    run(config,fresh,NOW+timedelta(days=1))
    assert fresh.searches==fresh.downloads==[]


def test_actual_adapter_configuration_category_requires_operator_pause(tmp_path):
    from hcr_sync.youtube_adapter import _failure
    config=fixture(tmp_path)
    track(config)
    failure=_failure('ffmpeg not found')
    client=Fake(failure)
    run(config,client)
    with connect(config) as con:
        pause=json.loads(con.execute("SELECT value FROM sync_state WHERE key='youtube_provider_pause'").fetchone()[0])
    assert pause['operator_required'] is True
    assert pause['until']==stamp(NOW+timedelta(hours=1))
    fresh=Fake()
    run(config,fresh,NOW+timedelta(days=10))
    assert fresh.searches==[]


def test_runtime_dispatch_rotates_first_retry_download_in_order(tmp_path):
    from dataclasses import asdict
    from hcr_sync.youtube_adapter import YouTubeCandidate
    config=fixture(tmp_path)
    first=track(config,title='First Song')
    retry=track(config,title='Retry Song')
    download=track(config,title='Download Song')
    candidate=YouTubeCandidate(title='Artist - Download Song',url='https://www.youtube.com/watch?v=abcdefghijk',
                              video_id='abcdefghijk',channel='Artist',duration=120)
    schedule(config,first,'first_search')
    schedule(config,retry,'search_retry')
    schedule(config,download,'download',candidate_json=json.dumps(asdict(candidate)),candidate_at=stamp(NOW))
    with connect(config) as con, transaction(con):
        source=con.execute('SELECT * FROM tracks WHERE id=?',(download,)).fetchone()
        payload={'candidate':asdict(candidate),'source_artist':'Artist','source_title':'Download Song',
                 'fingerprint':source_fingerprint(source),'score':1,'lane':2}
        con.execute("INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,created_at,updated_at) VALUES ('existing',?,?,?, ?,?)",(download,candidate.video_id,json.dumps(payload),stamp(NOW),stamp(NOW)))
    trace=[]
    class Traced(Fake):
        def search(self,artist,title):
            trace.append(title)
            return []
        def download(self,candidate):
            trace.append('Download Song')
            raise RuntimeError('synthetic outage')
    run(config,Traced())
    assert trace==['First Song','Retry Song','Download Song']
    assert [row(config,source)['search_attempts'] for source in (first,retry)]==[1,1]
    assert row(config,download)['download_attempts']==1


def test_dry_run_explicit_adapter_does_not_read_previous_run_counters_or_spawn(tmp_path, monkeypatch):
    from hcr_sync.youtube_adapter import YtDlpClient
    from hcr_sync.youtube_sync import sync_youtube
    config = fixture(tmp_path)
    source = track(config)
    schedule(config, source, 'first_search')
    client = YtDlpClient(config)
    client.search_invocations = 3
    client.download_invocations = 2
    monkeypatch.setattr(client, 'search', lambda *a: pytest.fail('dry-run provider search'))
    monkeypatch.setattr(client, 'download', lambda *a: pytest.fail('dry-run provider download'))
    summary = sync_youtube(config, apply=False, client=client, now=NOW)
    assert summary.wanted == summary.due == 1
    assert summary.search_invocations == summary.download_starts == 0
    assert row(config, source)['search_attempts'] == 0
