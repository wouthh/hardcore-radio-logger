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
    executable = tool(tmp_path, f'import json,sys\nopen({str(logfile)!r},"w").write(json.dumps(sys.argv))\nprint({json.dumps({"_type": "playlist", "entries": entries})!r})\n')
    results = YtDlpClient(config(tmp_path, executable)).search('Artist', 'Song')
    assert len(results) == len(entries)
    args = json.loads(logfile.read_text())
    assert '--ignore-config' in args and '--dump-single-json' in args
    for flag in ('--retries', '--extractor-retries', '--fragment-retries', '--file-access-retries'):
        assert args[args.index(flag) + 1] == '0'
    assert args[-1] == 'ytsearch10:Artist - Song'


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


def test_large_output_is_bounded_and_not_completed_empty(tmp_path):
    executable = tool(tmp_path, 'print("x" * (2 * 1024 * 1024 + 1))\n')
    with pytest.raises(YouTubeFailure) as error:
        run_process([str(executable)], 2)
    assert error.value.category == 'invalid_response'


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
