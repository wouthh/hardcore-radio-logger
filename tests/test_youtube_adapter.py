"""Owned local executable fixtures exercise the real subprocess boundary."""
import json
import os
import signal
import subprocess
import sys

import pytest

from hcr_sync.config import Config
from hcr_sync.system import sync_lock
from hcr_sync.youtube_adapter import YtDlpClient, YouTubeFailure, local_tool_check, run_process


def tool(tmp_path, body):
    executable = tmp_path / 'yt-dlp-fixture'
    executable.write_text('#!/usr/bin/python3\n' + body)
    executable.chmod(0o700)
    return executable


def config(tmp_path, executable):
    return Config({'HCR_YTDLP_BIN': str(executable), 'HCR_NODE_RUNTIME': sys.executable,
                   'HCR_DB_PATH': str(tmp_path / 'db.sqlite'),
                   'HCR_MUSIC_DIR': str(tmp_path / 'music'),
                   'HCR_DOWNLOAD_TMP_DIR': str(tmp_path / 'partials'),
                   'HCR_YOUTUBE_DOWNLOAD_ARCHIVE': str(tmp_path / 'archive')})


@pytest.mark.parametrize('entries', [[], [{'id': 'abcdefghijk', 'title': 'Artist - Song', 'duration': 125}]])
def test_search_completed_results_and_explicit_boundary_flags(tmp_path, entries):
    logfile = tmp_path / 'args.json'
    executable = tool(tmp_path, f'import json,sys\nopen({str(logfile)!r},"w").write(json.dumps(sys.argv))\nprint({json.dumps({"_type": "playlist", "entries_present":True, "playlist_count":len(entries), "entries": entries})!r})\n')
    results = YtDlpClient(config(tmp_path, executable)).search('Artist', 'Song')
    assert len(results) == len(entries)
    args = json.loads(logfile.read_text())
    assert '--ignore-config' in args and '--print' in args and '--dump-single-json' not in args
    for flag in ('--retries', '--extractor-retries', '--fragment-retries', '--file-access-retries'):
        assert args[args.index(flag) + 1] == '0'
    assert args[-1] == 'ytsearch10:Artist - Song'


@pytest.mark.parametrize('count,entries', [(1, []), (0, [{}]), (False, []), (None, [])])
def test_projected_entries_must_match_completed_playlist_count(tmp_path, count, entries):
    value=json.dumps({'_type':'playlist','entries_present':True,'playlist_count':count,'entries':entries})
    executable=tool(tmp_path,f'print({value!r})\n')
    with pytest.raises(YouTubeFailure) as error:
        YtDlpClient(config(tmp_path,executable)).search('Artist','Song')
    assert error.value.category=='invalid_response'


@pytest.mark.parametrize('response', ['broken', '{}', '{"entries":[]}',
    '{"_type":"playlist","entries":[null]}',
    json.dumps({'_type': 'playlist', 'entries': [{}] * 11})])
def test_malformed_or_nonempty_unusable_search_is_not_no_match(tmp_path, response):
    executable = tool(tmp_path, f'print({response!r})\n')
    with pytest.raises(YouTubeFailure) as error:
        YtDlpClient(config(tmp_path, executable)).search('Artist', 'Song')
    assert error.value.category == 'invalid_response'


@pytest.mark.parametrize('message,category', [('HTTP Error 429 secret-token', 'restriction'),
    ('HTTP Error 503 secret-token', 'transport'), ('no such option secret-token', 'configuration'),
    ('Private video secret-token', 'video_unavailable')])
def test_error_classification_never_retains_provider_text(tmp_path, message, category):
    executable = tool(tmp_path, f'import sys\nprint({message!r},file=sys.stderr)\nsys.exit(1)\n')
    with pytest.raises(YouTubeFailure) as error:
        YtDlpClient(config(tmp_path, executable)).search('Artist', 'Song')
    assert error.value.category == category and 'secret-token' not in str(error.value)


def test_video_specific_restriction_does_not_pause_provider(tmp_path):
    executable = tool(tmp_path, 'import sys\nprint("age-restricted",file=sys.stderr)\nsys.exit(1)\n')
    with pytest.raises(YouTubeFailure) as error:
        run_process([str(executable)], 1, video=True)
    assert error.value.category == 'video_unavailable'


def test_timeout_terminates_and_reaps_owned_process(tmp_path, monkeypatch):
    pidfile = tmp_path / 'pid'
    executable = tool(tmp_path, f'import os,time\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\ntime.sleep(30)\n')
    monkeypatch.setattr('hcr_sync.youtube_adapter.SEARCH_SECONDS', .15)
    with pytest.raises(YouTubeFailure) as error:
        YtDlpClient(config(tmp_path, executable)).search('Artist', 'Song')
    assert error.value.category == 'transport'
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_spawn_callback_failure_still_reaps_owned_process(tmp_path):
    executable = tool(tmp_path, 'import time\ntime.sleep(30)\n')
    pids = []
    def fail(pid):
        pids.append(pid)
        raise RuntimeError('synthetic durable intent failure')
    with pytest.raises(RuntimeError, match='durable intent'):
        run_process([str(executable)], 10, on_spawn=fail)
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_large_output_is_bounded_and_not_completed_empty(tmp_path):
    executable = tool(tmp_path, 'print("x" * (2 * 1024 * 1024 + 1))\n')
    with pytest.raises(YouTubeFailure) as error:
        run_process([str(executable)], 2)
    assert error.value.category == 'invalid_response'


@pytest.mark.parametrize('streams', [(1,), (2,), (1, 2)])
def test_continuous_output_is_capped_before_exit_and_owned_group_is_killed(tmp_path, monkeypatch, streams):
    import fcntl
    import tempfile
    import time
    from hcr_sync.youtube_adapter import OUTPUT_LIMIT
    from hcr_sync.youtube_download import _process_identity, process_is_alive

    sizes = []
    temporary_file = tempfile.TemporaryFile
    class Capture:
        def __init__(self):
            self.file = temporary_file(mode='w+b')
        def __getattr__(self, name):
            return getattr(self.file, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.flush()
            sizes.append(os.fstat(self.file.fileno()).st_size)
            self.file.close()
    monkeypatch.setattr('hcr_sync.youtube_adapter.tempfile.TemporaryFile', lambda **kwargs: Capture())
    monkeypatch.setattr('hcr_sync.youtube_adapter.TERMINATE_SECONDS', .1)
    pidfile = tmp_path / 'writer'
    executable = tool(tmp_path, f'''import os,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=os.fork()
if child:
    while True: time.sleep(1)
open({str(pidfile)!r},'w').write(str(os.getpid()))
while True:
    for fd in {streams!r}:
        try: os.write(fd,b'x'*65536)
        except BrokenPipeError: time.sleep(.01)
''')
    started = time.monotonic()
    lock = tmp_path / 'sync.lock'
    with sync_lock(lock) as handle:
        with pytest.raises(YouTubeFailure) as error:
            run_process([str(executable)], 10, lock_handle=handle)
    assert error.value.category == 'invalid_response'
    assert time.monotonic() - started < 2
    assert len(sizes) == 2 and max(sizes) == OUTPUT_LIMIT
    assert all(size <= OUTPUT_LIMIT for size in sizes)
    pid = int(pidfile.read_text())
    identity, _ = _process_identity(pid)
    deadline = time.monotonic() + 2
    while process_is_alive(pid, identity) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not process_is_alive(pid, identity)
    with lock.open('a') as competing:
        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_timeout_also_kills_child_ignoring_termination(tmp_path):
    import time
    from hcr_sync.youtube_download import _process_identity, process_is_alive
    pidfile = tmp_path / 'child'
    executable = tool(tmp_path, f'''import os,signal,time
pid=os.fork()
if pid==0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    open({str(pidfile)!r},'w').write(str(os.getpid()))
    time.sleep(30)
else:
    time.sleep(30)
''')
    with pytest.raises(YouTubeFailure):
        run_process([str(executable)], .2)
    pid = int(pidfile.read_text())
    start, _ = _process_identity(pid)
    deadline = time.monotonic() + 2
    while process_is_alive(pid, start) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not process_is_alive(pid, start)


def test_child_keeps_lock_when_parent_handle_closes(tmp_path):
    import fcntl
    lock = tmp_path / 'lock'
    with sync_lock(lock) as handle:
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                                 pass_fds=(handle.fileno(),), start_new_session=True)
    try:
        with lock.open('a') as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=2)
    with lock.open('a') as competing:
        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_tool_check_is_local_and_missing_tool_is_classified(tmp_path):
    executable = tool(tmp_path, 'import sys\nassert sys.argv[1:] == ["--version"]\nprint("fixture1")\n')
    local_tool_check(config(tmp_path, executable))
    with pytest.raises(YouTubeFailure) as error:
        local_tool_check(config(tmp_path, tmp_path / 'missing'))
    assert error.value.category == 'missing_tool'


def test_installed_ytdlp_receipt_formatter_preserves_missing_optional_metadata(tmp_path):
    # Formatting is local; no extractor, provider or media URL is invoked.
    import shutil,shlex
    from pathlib import Path
    from hcr_sync.youtube_adapter import RECEIPT_TEMPLATE
    executable=shutil.which('yt-dlp')
    if not executable:
        pytest.skip('installed yt-dlp is required for its offline formatter fixture')
    interpreter=shlex.split(Path(executable).read_text().splitlines()[0][2:])
    program="""import json,sys,yt_dlp
formatter=yt_dlp.YoutubeDL({'quiet':True})
info={'id':'abcdefghijk','title':'Artist - Song','duration':125,'filepath':'/tmp/fixture.mp3'}
print(formatter.evaluate_outtmpl(sys.argv[1].removeprefix('after_move:'),info))
"""
    value=subprocess.check_output([*interpreter,'-c',program,RECEIPT_TEMPLATE],text=True,timeout=10)
    receipt=json.loads(value)
    assert receipt['artist'] is receipt['artist_names'] is receipt['track'] is None
    assert receipt['video_id']=='abcdefghijk' and receipt['title']=='Artist - Song'


@pytest.mark.parametrize('mode', ['complete', 'empty', 'failed', 'null_entry', 'all_null', 'missing_entries'])
def test_installed_cli_full_extraction_prints_only_completed_matching_fields(tmp_path, mode):
    # Run the installed CLI entry point with a local extractor. No external
    # extractor or media access is registered; the adapter's arguments are real.
    import shlex
    import shutil
    from pathlib import Path
    executable = shutil.which('yt-dlp')
    if not executable:
        pytest.skip('installed yt-dlp is required for its offline CLI fixture')
    interpreter = shlex.split(Path(executable).read_text().splitlines()[0][2:])
    fixture = tmp_path / 'offline-ytdlp'
    evidence = tmp_path / 'extraction.json'
    fixture.write_text('#!' + ' '.join(interpreter) + '\n' + f'''
import json
from pathlib import Path
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError
mode={mode!r}
seen=[]
class FixtureIE(InfoExtractor):
    _VALID_URL=r'(?P<id>ytsearch10:.+|fixture:fixture[0-9]+)'
    def _real_extract(self,url):
        if url.startswith('ytsearch10:'):
            entries=[] if mode=='empty' else [self.url_result('fixture:fixture%04d'%i,ie='Fixture') for i in range(10)]
            if mode=='null_entry': entries[0]=None
            if mode=='all_null': entries=[None]*10
            if mode=='missing_entries': return {{'_type':'playlist','id':'fixturelist','playlist_count':0}}
            return self.playlist_result(entries,'fixturelist')
        index=int(url[-4:])
        if mode=='failed' and index==5:
            raise ExtractorError('synthetic transport failure',expected=True)
        seen.append(index)
        info={{'id':url.split(':')[1],'title':'Artist - Song','duration':125,
            'artists':['Artist'],'artist':'Artist','track':'Song','channel':'Fixture Channel',
            'uploader':'Fixture Uploader','description':'full DJ set guard marker',
            'is_live':False,'was_live':False,'live_status':'not_live',
            'formats':[{{'url':'https://fixture.invalid/audio.mp3','ext':'mp3','format_id':'1',
                'acodec':'mp3','vcodec':'none','format_note':'x'*300000}}],
            'thumbnails':[{{'url':'https://fixture.invalid/image.jpg'}}],
            'subtitles':{{'en':[{{'url':'https://fixture.invalid/captions.vtt'}}]}}}}
        Path({str(evidence)!r}).write_text(json.dumps({{'seen':seen,'one_entry_bytes':len(json.dumps(info))}}))
        return info
yt_dlp.YoutubeDL.add_default_info_extractors=lambda self:self.add_info_extractor(FixtureIE())
yt_dlp.main()
''')
    fixture.chmod(0o700)
    cfg = config(tmp_path, fixture)
    if mode in {'failed', 'null_entry', 'all_null', 'missing_entries'}:
        with pytest.raises(YouTubeFailure):
            YtDlpClient(cfg).search('Artist', 'Song')
        if mode == 'failed':
            assert json.loads(evidence.read_text())['seen'] == [i for i in range(10) if i != 5]
            from hcr_sync.youtube_adapter import SEARCH_TEMPLATE
            value=subprocess.run([*YtDlpClient(cfg)._base_command(),'--print',SEARCH_TEMPLATE,
                '--skip-download','--no-playlist','ytsearch10:Artist - Song'],capture_output=True,text=True,timeout=10)
            assert value.returncode != 0
            assert len(json.loads(value.stdout)['entries']) == 9
        return
    candidates = YtDlpClient(cfg).search('Artist', 'Song')
    if mode == 'empty':
        assert candidates == [] and not evidence.exists()
        return
    assert len(candidates) == 10
    resolved = json.loads(evidence.read_text())
    assert resolved['seen'] == list(range(10))
    assert resolved['one_entry_bytes'] * 10 > 2 * 1024 * 1024
    for candidate in candidates:
        assert candidate.title == 'Artist - Song' and candidate.duration == 125
        assert candidate.artist_names == ('Artist',) and candidate.artist == 'Artist' and candidate.track == 'Song'
        assert candidate.channel == 'Fixture Channel' and candidate.description == 'full DJ set guard marker'
        assert candidate.is_live is False
    from hcr_sync.youtube_matching import evaluate_candidate
    assert evaluate_candidate('Artist','Song',candidates[0]).reason == 'bad_video'
