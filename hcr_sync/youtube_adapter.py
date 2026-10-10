"""Bounded yt-dlp execution without implicit configuration or retries."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

from .config import Config

SEARCH_SECONDS = 45
DOWNLOAD_SECONDS = 120
TERMINATE_SECONDS = 5
OUTPUT_LIMIT = 2 * 1024 * 1024
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
RECEIPT_TEMPLATE = 'after_move:{"video_id":%(id)j,"title":%(title)j,"artist":%(artist|null)j,"artist_names":%(artists|null)j,"track":%(track|null)j,"duration":%(duration)j,"filepath":%(filepath)j}'


@dataclass(frozen=True)
class YouTubeCandidate:
    title: str
    url: str
    video_id: str
    channel: str
    duration: int | None
    description: str = ""
    is_live: bool = False
    confidence: float = 0.0
    artist_names: tuple[str, ...] = ()
    artist: str = ""
    track: str = ""


class YouTubeFailure(RuntimeError):
    def __init__(self, category: str, detail: str, retry_at: str | None = None):
        self.category, self.detail, self.retry_at = category, detail, retry_at
        super().__init__(detail)


def _failure(stderr: str, *, video=False) -> YouTubeFailure:
    text = stderr.casefold()
    if any(word in text for word in ("no such option", "unsupported option", "ffmpeg not found", "ffprobe not found", "javascript runtime")):
        return YouTubeFailure("configuration", "yt-dlp requires a supported local tool configuration")
    if video and any(word in text for word in ("age-restricted", "age restricted", "geo-restricted", "not available in your country")):
        return YouTubeFailure("video_unavailable", "YouTube video access is unavailable")
    if any(word in text for word in ("sign in", "not a bot", "age-restricted", "age restricted", "geo-restricted", "http error 403", "http error 429", "http error 401", "cookies")):
        return YouTubeFailure("restriction", "YouTube access is restricted")
    if any(word in text for word in ("video unavailable", "private video", "deleted video", "removed by", "copyright")):
        return YouTubeFailure("video_unavailable", "YouTube video is unavailable")
    if any(word in text for word in ("timed out", "timeout", "connection", "network is unreachable", "http error 5", "certificate", "dns", "unable to download webpage")):
        return YouTubeFailure("transport", "yt-dlp transport failed")
    return YouTubeFailure("transient", "yt-dlp did not complete successfully")


def _terminate(process: subprocess.Popen) -> None:
    # Stop reading before termination: communicate() could capture unbounded
    # output from a writer ignoring SIGTERM during the grace period.
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # A terminated leader can leave a child ignoring SIGTERM behind.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_process(command, timeout, *, lock_handle=None, on_spawn=None, video=False):
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
        return _run_process(command, timeout, lock_handle=lock_handle, on_spawn=on_spawn,
                            video=video, stdout_file=stdout_file, stderr_file=stderr_file)


def _run_process(command, timeout, *, lock_handle, on_spawn, video, stdout_file, stderr_file):
    pass_fds = (lock_handle.fileno(),) if lock_handle is not None else ()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, pass_fds=pass_fds)
    except FileNotFoundError as exc:
        raise YouTubeFailure("missing_tool", "Required local executable is unavailable") from exc
    except OSError as exc:
        raise YouTubeFailure("configuration", "Cannot start required local executable") from exc
    try:
        deadline = time.monotonic() + timeout
        if on_spawn is not None:
            on_spawn(process.pid)
        with selectors.DefaultSelector() as selector:
            for stream, output in ((process.stdout, stdout_file), (process.stderr, stderr_file)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, output)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output = key.data
                    allowance = OUTPUT_LIMIT - output.tell()
                    output.write(chunk[:allowance])
                    if len(chunk) > allowance:
                        raise YouTubeFailure("invalid_response", "Local tool output exceeds the safe response limit")
            process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate(process)
        raise YouTubeFailure("transport", "yt-dlp operation exceeded its execution deadline") from exc
    except BaseException:
        _terminate(process)
        raise
    finally:
        for stream in (process.stdout, process.stderr):
            stream.close()
    for output in (stdout_file, stderr_file):
        output.seek(0)
    stdout = stdout_file.read().decode("utf-8", errors="replace")
    stderr = stderr_file.read().decode("utf-8", errors="replace")
    if process.returncode:
        raise _failure(stderr or stdout, video=video)
    return stdout


def local_tool_check(config: Config) -> None:
    command = YtDlpClient(config)._base_command()
    node = command[command.index("--js-runtimes") + 1].removeprefix("node:")
    for tool, argument in ((command[0], "--version"), (node, "--version"),
                           (shutil.which("ffmpeg"), "-version"), (shutil.which("ffprobe"), "-version")):
        if not tool:
            raise YouTubeFailure("missing_tool", "Required local media tool is unavailable")
        if not run_process([tool, argument], 5).strip():
            raise YouTubeFailure("configuration", "Required local media tool did not identify its version")


class YtDlpClient:
    def __init__(self, config: Config, lock_handle=None):
        self.config, self.lock_handle = config, lock_handle
        self.search_invocations = self.download_invocations = 0

    def _search_spawned(self,pid):
        self.search_invocations += 1

    def _base_command(self) -> list[str]:
        tool = shutil.which(self.config.get("HCR_YTDLP_BIN"))
        if not tool:
            raise YouTubeFailure("missing_tool", "yt-dlp executable is unavailable")
        node = self.config.get("HCR_NODE_RUNTIME") or shutil.which("node")
        if not node or not Path(node).is_file() or not os.access(node, os.X_OK):
            raise YouTubeFailure("configuration", "A managed executable Node runtime is required")
        return [tool, "--ignore-config", "--js-runtimes", f"node:{node}",
                "--socket-timeout", "15", "--retries", "0", "--extractor-retries", "0",
                "--fragment-retries", "0", "--file-access-retries", "0", "--abort-on-unavailable-fragments"]

    def search(self, artist: str, title: str) -> list[YouTubeCandidate]:
        query = f"{artist} - {title}" if artist else title
        stdout = run_process([*self._base_command(), "--dump-single-json", "--skip-download", "--no-playlist",
                              f"ytsearch10:{query}"], SEARCH_SECONDS, lock_handle=self.lock_handle,on_spawn=self._search_spawned)
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise YouTubeFailure("invalid_response", "yt-dlp search returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("_type") != "playlist" or not isinstance(result.get("entries"), list) or len(result["entries"]) > 10:
            raise YouTubeFailure("invalid_response", "yt-dlp search did not return playlist entries")
        candidates = []
        for entry in result["entries"]:
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("id")
            title = entry.get("title")
            if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id) or not isinstance(title, str) or not title.strip():
                continue
            duration = entry.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                try:
                    duration = int(duration)
                except (ValueError, OverflowError):
                    duration = None
            else:
                duration = None
            artists = entry.get("artists") or []
            candidates.append(YouTubeCandidate(
                title=title, url=f"https://www.youtube.com/watch?v={video_id}", video_id=video_id,
                channel=str(entry.get("channel") or entry.get("uploader") or ""), duration=duration,
                description=str(entry.get("description") or ""),
                is_live=bool(entry.get("is_live") or entry.get("was_live") or entry.get("live_status") in {"is_live", "is_upcoming", "post_live"}),
                artist_names=tuple(value for value in artists if isinstance(value, str)) if isinstance(artists, list) else (),
                artist=str(entry.get("artist") or ""), track=str(entry.get("track") or ""),
            ))
        if result["entries"] and not candidates:
            raise YouTubeFailure("invalid_response", "yt-dlp search contained no usable entries")
        return candidates

    def download(self, candidate: YouTubeCandidate, *, work: dict | None = None) -> Path:
        from .youtube_download import UnsafeDownloadOutput, prepare_download, prepare_partials, read_receipt, record_process, record_paths
        if work is None:
            raise YouTubeFailure("configuration", "Download requires a durable work intent")
        work = dict(work)
        if not VIDEO_ID_RE.fullmatch(candidate.video_id) or work["video_id"] != candidate.video_id:
            raise UnsafeDownloadOutput("Download identity does not match its work intent")
        stage = prepare_download(self.config, work["work_id"])
        output = stage / "output.mp3"
        if work.get("output_path") and Path(work["output_path"]) != output:
            raise UnsafeDownloadOutput("Published work requires offline recovery before downloading")
        record_paths(self.config, work["work_id"], stage, output)
        receipt = read_receipt(self.config, work)
        if output.exists():
            if receipt is None:
                raise UnsafeDownloadOutput("An unverified final stage file requires recovery")
            return output
        archive = self.config.path("HCR_YOUTUBE_DOWNLOAD_ARCHIVE")
        if archive.is_symlink():
            raise UnsafeDownloadOutput("Download archive must not be a symlink")
        try:
            archived = archive.exists() and any(line.split() and line.split()[-1] == candidate.video_id
                                                for line in archive.read_text().splitlines())
        except OSError as exc:
            raise YouTubeFailure("configuration", "Cannot read download archive") from exc
        if archived:
            raise YouTubeFailure("archive_inconsistent", "Archived YouTube identity has no verified local output")
        archive.parent.mkdir(parents=True, exist_ok=True)

        command = [*self._base_command(), "--no-playlist", "--download-archive", str(archive),
                   "--no-overwrites", "--continue", "--no-simulate", "-f", "ba/b", "-x", "--audio-format", "mp3",
                   "--audio-quality", "0", "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg", "--match-filter", "!is_live & duration >= 120 & duration <= 480",
                   "--paths", f"temp:{prepare_partials(self.config, work['work_id'])}", "-o", str(stage / "output.%(ext)s"),
                   "--print-to-file", RECEIPT_TEMPLATE, str(stage / "receipt.jsonl"),
                   f"https://www.youtube.com/watch?v={candidate.video_id}"]
        def spawned(pid):
            self.download_invocations += 1
            record_process(self.config,work['work_id'],pid)
        run_process(command, DOWNLOAD_SECONDS, lock_handle=self.lock_handle, video=True,
                    on_spawn=spawned)
        if not output.exists() or read_receipt(self.config, work) is None:
            raise UnsafeDownloadOutput("yt-dlp completed without its exact output and receipt")
        return output
