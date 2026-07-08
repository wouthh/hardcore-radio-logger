"""Local music folder backfill and scan."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import add_event, connect, ensure_track, now_utc, set_state, transaction, upsert_youtube_asset
from .identity import compact_text, parse_artist_title

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav", ".aac"}
VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,})\](?:\.[^.]+)?$")


@dataclass
class LocalSummary:
    files_seen: int = 0
    tracks_seen: int = 0
    assets_upserted: int = 0
    skipped: int = 0
    baseline_complete: bool = False


@dataclass(frozen=True)
class LocalAudioFile:
    path: Path
    artist: str
    title: str
    youtube_video_id: str = ""


def audio_paths(music_dir: Path) -> list[Path]:
    if not music_dir.exists() or not music_dir.is_dir():
        return []
    return sorted(
        path
        for path in music_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
    )


def youtube_id_from_path(path: Path) -> str:
    match = VIDEO_ID_RE.search(path.name)
    return match.group(1) if match else ""


def _tag_values(path: Path) -> tuple[str, str]:
    try:
        from mutagen import File as MutagenFile
    except Exception:
        return "", ""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return "", ""
    if not audio or not audio.tags:
        return "", ""
    tags = audio.tags
    artist = "; ".join(tags.get("artist", []) or tags.get("albumartist", []) or [])
    title = "; ".join(tags.get("title", []) or [])
    return compact_text(artist), compact_text(title)


def _filename_title(path: Path) -> tuple[str, str]:
    stem = VIDEO_ID_RE.sub("", path.stem).strip()
    stem = compact_text(stem.replace("_", " "))
    return parse_artist_title(stem)


def inspect_audio_file(path: Path) -> LocalAudioFile | None:
    video_id = youtube_id_from_path(path)
    filename_artist, filename_title = _filename_title(path)
    if video_id and filename_artist and filename_title:
        artist, title = filename_artist, filename_title
    else:
        artist, title = _tag_values(path)
        if not title:
            artist, title = filename_artist, filename_title
    if not artist and not title:
        return None
    return LocalAudioFile(path=path, artist=artist, title=title, youtube_video_id=video_id)


def scan_music_folder(config: Config) -> list[LocalAudioFile]:
    files: list[LocalAudioFile] = []
    for path in audio_paths(config.music_dir):
        inspected = inspect_audio_file(path)
        if inspected:
            files.append(inspected)
    return files


def import_local_files(config: Config, *, apply: bool, establish_baseline: bool) -> LocalSummary:
    summary = LocalSummary()
    scanned = scan_music_folder(config)
    summary.files_seen = len(scanned)
    if not apply:
        summary.tracks_seen = len(scanned)
        summary.assets_upserted = len(scanned)
        summary.baseline_complete = establish_baseline
        return summary
    with connect(config) as con:
        with transaction(con):
            for item in scanned:
                track = ensure_track(con, artist=item.artist, title=item.title, status="wanted")
                summary.tracks_seen += 1
                upsert_youtube_asset(
                    con,
                    track_id=track["id"],
                    youtube_video_id=item.youtube_video_id,
                    youtube_url=f"https://www.youtube.com/watch?v={item.youtube_video_id}" if item.youtube_video_id else "",
                    file_path=str(item.path),
                    file_exists=True,
                    match_confidence=1.0,
                    status="downloaded",
                    downloaded_at=None,
                )
                add_event(
                    con,
                    track["id"],
                    "local_file_seen",
                    "local_scan",
                    {"file_path": str(item.path), "youtube_video_id": item.youtube_video_id},
                    dedupe_key=f"local_file_seen:{track['id']}:{item.path}",
                )
                summary.assets_upserted += 1
            set_state(con, "last_local_scan_count", str(len(scanned)))
            set_state(con, "last_local_scan_at", now_utc())
            if establish_baseline:
                set_state(con, "local_baseline_complete", "true")
                summary.baseline_complete = True
    return summary
