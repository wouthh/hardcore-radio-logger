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


def _existing_asset_for_local_file(con, item: LocalAudioFile):
    existing = con.execute(
        "SELECT * FROM youtube_assets WHERE file_path = ? ORDER BY file_exists DESC, id LIMIT 1",
        (str(item.path),),
    ).fetchone()
    if existing or not item.youtube_video_id:
        return existing
    return con.execute(
        "SELECT * FROM youtube_assets WHERE youtube_video_id = ? ORDER BY file_exists DESC, id LIMIT 1",
        (item.youtube_video_id,),
    ).fetchone()


def _missing_idless_asset_for_track(con, track_id: int):
    rows = list(
        con.execute(
            """
            SELECT *
              FROM youtube_assets
             WHERE track_id = ?
               AND file_exists = 1
               AND status = 'downloaded'
               AND file_path IS NOT NULL
               AND NULLIF(youtube_video_id, '') IS NULL
            """,
            (track_id,),
        )
    )
    missing = [row for row in rows if not Path(row["file_path"]).exists()]
    return missing[0] if len(missing) == 1 else None


def _move_local_asset_path(con, asset, item: LocalAudioFile) -> None:
    now = now_utc()
    con.execute(
        """
        UPDATE youtube_assets
           SET file_path = ?,
               file_exists = 1,
               match_confidence = 1.0,
               status = 'downloaded',
               last_seen_at = ?,
               suspected_missing_at = NULL,
               updated_at = ?
         WHERE id = ?
        """,
        (str(item.path), now, now, asset["id"]),
    )
    add_event(
        con,
        asset["track_id"],
        "local_file_path_updated",
        "local_scan",
        {"old_path": asset["file_path"], "new_path": str(item.path), "reason": "single missing idless asset matched current scan"},
        dedupe_key=f"local_file_path_updated:{asset['track_id']}:{asset['id']}:{item.path}",
    )


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
                existing_asset = _existing_asset_for_local_file(con, item)
                moved_asset = None
                if existing_asset:
                    track_id = existing_asset["track_id"]
                else:
                    track = ensure_track(con, artist=item.artist, title=item.title, status="wanted")
                    track_id = track["id"]
                    if not item.youtube_video_id:
                        moved_asset = _missing_idless_asset_for_track(con, track_id)
                summary.tracks_seen += 1
                if moved_asset:
                    _move_local_asset_path(con, moved_asset, item)
                else:
                    upsert_youtube_asset(
                        con,
                        track_id=track_id,
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
                    track_id,
                    "local_file_seen",
                    "local_scan",
                    {"file_path": str(item.path), "youtube_video_id": item.youtube_video_id},
                    dedupe_key=f"local_file_seen:{track_id}:{item.path}",
                )
                summary.assets_upserted += 1
            scan_at = now_utc()
            set_state(con, "last_local_import_scan_count", str(len(scanned)))
            set_state(con, "last_local_import_scan_at", scan_at)
            if establish_baseline:
                set_state(con, "last_local_scan_count", str(len(scanned)))
                set_state(con, "last_local_scan_at", scan_at)
                set_state(con, "local_baseline_complete", "true")
                summary.baseline_complete = True
    return summary
