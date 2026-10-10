"""Exact staged download evidence and non-overwriting publication."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import time

from .db import connect, now_utc, transaction
from .youtube_adapter import YouTubeCandidate, YouTubeFailure, VIDEO_ID_RE, run_process

VERIFY_SECONDS = 15
WORK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")


class UnsafeDownloadOutput(RuntimeError):
    pass


def _inside(config, path: Path) -> Path:
    root = Path(os.path.abspath(config.music_dir))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UnsafeDownloadOutput("Download evidence is outside the music folder") from exc
    current = root
    if any(component.is_symlink() for component in (*root.parents, root)):
        raise UnsafeDownloadOutput("Download evidence must not follow symlinks")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeDownloadOutput("Download evidence must not follow symlinks")
    return path


def prepare_download(config, work_id: str) -> Path:
    if not WORK_ID_RE.fullmatch(work_id):
        raise UnsafeDownloadOutput("Download work identity is invalid")
    stage = _inside(config, config.music_dir / ".hcr-youtube-work" / work_id)
    stage.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stage.is_dir():
        raise UnsafeDownloadOutput("Download stage is not a directory")
    return stage


def prepare_partials(config, work_id: str) -> Path:
    if not WORK_ID_RE.fullmatch(work_id):
        raise UnsafeDownloadOutput("Download work identity is invalid")
    root = Path(os.path.abspath(config.path("HCR_DOWNLOAD_TMP_DIR")))
    path = root / ".hcr-youtube-work" / work_id
    # The configured partial root may be outside music, but every component must
    # remain a real directory, and only this job's subdirectory is used.
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise UnsafeDownloadOutput("Download partials must not follow symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _stage(config, work) -> Path:
    work = dict(work)
    work_id = work["work_id"]
    if not WORK_ID_RE.fullmatch(work_id):
        raise UnsafeDownloadOutput("Download work identity is invalid")
    expected = _inside(config, config.music_dir / ".hcr-youtube-work" / work_id)
    if work.get("stage_dir") and _inside(config, Path(work["stage_dir"])) != expected:
        raise UnsafeDownloadOutput("Recorded download stage does not match its work identity")
    return expected


def read_receipt(config, work) -> dict | None:
    path = _inside(config, _stage(config, work) / "receipt.jsonl")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 65536:
        raise UnsafeDownloadOutput("Download receipt is not a bounded regular file")
    try:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        data = json.loads(lines[0]) if len(lines) == 1 else None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnsafeDownloadOutput("Download receipt is malformed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("filepath"), str):
        raise UnsafeDownloadOutput("Download receipt is malformed")
    if _inside(config, Path(data["filepath"])) != _stage(config, work) / "output.mp3":
        raise UnsafeDownloadOutput("Download receipt points to an unexpected output")
    if data.get("video_id") != dict(work)["video_id"]:
        raise UnsafeDownloadOutput("Download receipt identity does not match its work intent")
    return data


def _process_identity(pid):
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{stat[19]}", stat[0]
    except (OSError, ValueError, IndexError):
        return None, None


def process_is_alive(pid, start) -> bool:
    if not pid or not start:
        return False
    actual, state = _process_identity(pid)
    return actual == start and state != "Z"


def record_process(config, work_id, pid):
    start, _ = _process_identity(pid)
    if start is None:
        raise YouTubeFailure("configuration", "Cannot record download process identity")
    with connect(config) as con, transaction(con):
        cursor = con.execute("UPDATE youtube_pending_work SET process_pid=?,process_start=?,updated_at=? WHERE work_id=?",
                             (pid, start, now_utc(), work_id))
        if cursor.rowcount != 1:
            raise UnsafeDownloadOutput("Download process has no durable work intent")


def record_paths(config, work_id, stage, output):
    with connect(config) as con, transaction(con):
        cursor = con.execute("UPDATE youtube_pending_work SET stage_dir=?,output_path=?,updated_at=? WHERE work_id=?",
                             (str(stage), str(output), now_utc(), work_id))
        if cursor.rowcount != 1:
            raise UnsafeDownloadOutput("Download paths have no durable work intent")


def _metadata_decision(config, source_artist, source_title, candidate):
    from .youtube_matching import evaluate_candidate
    decision = evaluate_candidate(source_artist, source_title, candidate,
                                  threshold=config.float("HCR_YOUTUBE_MATCH_THRESHOLD"))
    if not decision.accepted:
        raise UnsafeDownloadOutput("Downloaded metadata does not establish the requested recording")
    return decision


def verify_output(config, candidate, path, source_artist, source_title, receipt=None) -> dict:
    path = _inside(config, Path(path))
    if not path.is_file() or path.stat().st_size == 0 or path.suffix.casefold() != ".mp3":
        raise UnsafeDownloadOutput("Download output is not a nonempty MP3 file")
    if not VIDEO_ID_RE.fullmatch(candidate.video_id):
        raise UnsafeDownloadOutput("Downloaded video identity is invalid")
    if receipt is not None:
        if receipt.get("video_id") != candidate.video_id:
            raise UnsafeDownloadOutput("Downloaded video identity does not match its receipt")
        _inside(config, Path(receipt["filepath"]))
    elif f"[{candidate.video_id}]" not in path.stem:
        raise UnsafeDownloadOutput("Downloaded file has no attributable video identity")
    probe, decoder = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not probe or not decoder:
        raise YouTubeFailure("missing_tool", "Audio verification requires ffprobe and ffmpeg")
    started = time.monotonic()
    try:
        raw = run_process([probe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], VERIFY_SECONDS)
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or not 120 <= duration <= 480 or not any(stream.get("codec_type") == "audio" for stream in data["streams"]):
            raise UnsafeDownloadOutput("Downloaded audio duration or stream is invalid")
        remaining = VERIFY_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise UnsafeDownloadOutput("Audio verification exceeded its deadline")
        progress = run_process([decoder, "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0",
                                "-progress", "pipe:1", "-nostats", "-f", "null", "-"], remaining)
        times = [float(line.split("=", 1)[1]) / 1_000_000 for line in progress.splitlines()
                 if line.startswith("out_time_us=") and not line.endswith("=N/A")]
        decoded_duration = max(times, default=0)
        if not 120 <= decoded_duration <= 480 or abs(decoded_duration - duration) > max(3, duration * .02):
            raise UnsafeDownloadOutput("Decoded audio length does not match its duration evidence")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UnsafeDownloadOutput("Audio verification returned malformed evidence") from exc
    except YouTubeFailure as exc:
        if exc.category in {"missing_tool", "configuration"}:
            raise
        raise UnsafeDownloadOutput("Downloaded audio could not be fully decoded") from exc
    if candidate.duration is not None and abs(duration - candidate.duration) > max(3, candidate.duration * 0.02):
        raise UnsafeDownloadOutput("Downloaded duration does not match the selected video")
    tags = {str(key).casefold(): value for key, value in data["format"].get("tags", {}).items()}
    evidence = receipt or {}
    def known(value):
        return value if value not in (None, "NA", "") else None
    artist = str(known(evidence.get("artist")) or tags.get("artist") or "")
    artists = evidence.get("artist_names") or ()
    title = str(known(evidence.get("title")) or tags.get("title") or "")
    if receipt is None and not title:
        title = path.stem.removesuffix(f" [{candidate.video_id}]")
    actual = YouTubeCandidate(title=title, url=candidate.url, video_id=candidate.video_id,
                             channel=candidate.channel, duration=round(duration),
                             artist=artist, track=str(known(evidence.get("track")) or tags.get("title") or ""),
                             artist_names=tuple(value for value in artists if isinstance(value, str)) if isinstance(artists, (list, tuple)) else ())
    _metadata_decision(config, source_artist, source_title, actual)
    if tags.get("artist") and tags.get("title"):
        tagged = YouTubeCandidate(title=f"{tags['artist']} - {tags['title']}", url=candidate.url,
                                  video_id=candidate.video_id, channel="", duration=round(duration),
                                  artist=str(tags["artist"]), track=str(tags["title"]))
        _metadata_decision(config, source_artist, source_title, tagged)
    return {"video_id": candidate.video_id, "path": str(path), "duration": duration,
            "title": title, "artist": artist, "receipt": receipt}


def publish_output(config, candidate, work, source_artist, source_title) -> Path:
    work = dict(work)
    stage = _stage(config, work)
    receipt = read_receipt(config, work)
    source = _inside(config, Path(work.get("output_path") or stage / "output.mp3"))
    name = re.sub(r"[\\/\x00-\x1f]", "_", f"{source_artist} - {source_title}" if source_artist else source_title).strip(" .")
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "Track"
    target = _inside(config, config.music_dir / f"{name} [{candidate.video_id}].mp3")
    if source != stage / "output.mp3" and source != target:
        # Legacy synthetic clients provide an exact, already published file.
        if receipt is not None:
            raise UnsafeDownloadOutput("Recorded download output is unexpected")
        verify_output(config, candidate, source, source_artist, source_title)
        return source
    if source == target and target.exists():
        if work.get("stage_dir") or receipt is not None:
            staged_source = _inside(config, stage / "output.mp3")
            if not staged_source.is_file() or not os.path.samefile(staged_source, target):
                raise UnsafeDownloadOutput("Published destination is not the staged hard link")
        # Explicit direct-output clients have no staged publication to recover.
        verify_output(config, candidate, target, source_artist, source_title, receipt)
        return target
    source = stage / "output.mp3"
    verify_output(config, candidate, source, source_artist, source_title, receipt)
    if target.exists() and not os.path.samefile(source, target):
        raise UnsafeDownloadOutput("Publication would overwrite an existing music file")
    # Commit the deterministic destination before the atomic no-overwrite link.
    with connect(config) as con, transaction(con):
        cursor = con.execute("UPDATE youtube_pending_work SET output_path=?,updated_at=? WHERE work_id=?",
                             (str(target), now_utc(), work["work_id"]))
        if cursor.rowcount != 1:
            raise UnsafeDownloadOutput("Publication has no durable work intent")
    if not target.exists():
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise UnsafeDownloadOutput("Publication destination appeared concurrently") from exc
    return target
