"""Conservative YouTube search/download sync."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .db import add_event, connect, now_utc, transaction, upsert_youtube_asset, wanted_tracks
from .identity import compact_text, likely_same_recording, match_confidence, parse_artist_title
from .local_files import scan_music_folder, youtube_id_from_path
from .system import assert_legacy_downloader_safe

BAD_VIDEO_RE = re.compile(
    r"\b(full\s+mix|full\s+set|dj\s+set|liveset|mixtape|megamix|yearmix|podcast|radio\s+show|compilation|full\s+album|festival\s+set|aftermovie|trailer|interview|documentary|gameplay)\b",
    re.I,
)
PLACEHOLDER_RE = re.compile(r"\bunknown\s+(?:artist|title)\b|#\s*0*\d+\b", re.I)


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


class YouTubeClientProtocol(Protocol):
    def search(self, artist: str, title: str) -> list[YouTubeCandidate]: ...
    def download(self, candidate: YouTubeCandidate) -> Path: ...


class YtDlpClient:
    def __init__(self, config: Config):
        self.config = config

    def _base_command(self) -> list[str]:
        command = [self.config.get("HCR_YTDLP_BIN")]
        node = self.config.get("HCR_NODE_RUNTIME")
        if node:
            command.extend(["--js-runtimes", f"node:{node}"])
        return command

    def search(self, artist: str, title: str) -> list[YouTubeCandidate]:
        query = f"{artist} - {title}" if artist else title
        command = [
            *self._base_command(),
            "--dump-json",
            "--skip-download",
            "--no-playlist",
            f"ytsearch10:{query}",
        ]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=95)
        candidates: list[YouTubeCandidate] = []
        for line in result.stdout.splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = str(item.get("id") or "")
            url = str(item.get("webpage_url") or item.get("original_url") or "")
            if video_id and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={video_id}"
            duration = item.get("duration")
            try:
                duration = int(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            candidate = YouTubeCandidate(
                title=str(item.get("title") or ""),
                url=url,
                video_id=video_id,
                channel=str(item.get("channel") or item.get("uploader") or ""),
                duration=duration,
                description=str(item.get("description") or ""),
                is_live=bool(item.get("is_live") or item.get("live_status") == "is_live"),
            )
            candidates.append(candidate)
        return candidates

    def download(self, candidate: YouTubeCandidate) -> Path:
        self.config.music_dir.mkdir(parents=True, exist_ok=True)
        self.config.path("HCR_DOWNLOAD_TMP_DIR").mkdir(parents=True, exist_ok=True)
        archive = self.config.path("HCR_YOUTUBE_DOWNLOAD_ARCHIVE")
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.touch(exist_ok=True)
        before = {path.name for path in self.config.music_dir.glob("*.mp3")}
        command = [
            *self._base_command(),
            "--no-playlist",
            "--download-archive",
            str(archive),
            "-f",
            "ba/b",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--match-filter",
            "!is_live & duration >= 120 & duration <= 480",
            "--paths",
            f"temp:{self.config.path('HCR_DOWNLOAD_TMP_DIR')}",
            "-o",
            str(self.config.music_dir / "%(title).200B [%(id)s].%(ext)s"),
            candidate.url,
        ]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=900)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip().splitlines()[-1:])
        for path in self.config.music_dir.glob("*.mp3"):
            if candidate.video_id and f"[{candidate.video_id}]" in path.name:
                return path
        created = [path for path in self.config.music_dir.glob("*.mp3") if path.name not in before]
        if created:
            return created[0]
        raise RuntimeError("download completed but output file was not detected")


@dataclass
class YouTubeSummary:
    wanted: int = 0
    already_local: int = 0
    downloaded: int = 0
    review: int = 0
    skipped: int = 0


def _local_track_keys(config: Config) -> set[int]:
    with connect(config) as con:
        return {
            row["track_id"]
            for row in con.execute(
                "SELECT track_id FROM youtube_assets WHERE file_exists = 1 AND status = 'downloaded'"
            )
        }


def _existing_local_match(con, *, track, artist: str, title: str):
    rows = con.execute(
        """
        SELECT
            y.id AS asset_id,
            y.file_path,
            y.youtube_video_id,
            t.id AS track_id,
            t.display_artist,
            t.display_title
          FROM youtube_assets y
          JOIN tracks t ON t.id = y.track_id
         WHERE y.file_exists = 1
           AND y.status = 'downloaded'
           AND y.file_path IS NOT NULL
           AND t.id != ?
        """,
        (track["id"],),
    ).fetchall()
    for row in rows:
        if likely_same_recording(
            artist=artist,
            title=title,
            other_artist=row["display_artist"],
            other_title=row["display_title"],
        ):
            return row
    return None


def _candidate_score(track, candidate: YouTubeCandidate) -> float:
    if candidate.is_live or candidate.duration is None:
        return 0.0
    if candidate.duration < 120 or candidate.duration > 480:
        return 0.0
    if BAD_VIDEO_RE.search(f"{candidate.title} {candidate.channel} {candidate.description}"):
        return 0.0
    candidate_artist, candidate_title = parse_artist_title(candidate.title)
    if not candidate_title:
        candidate_title = candidate.title
    return match_confidence(
        artist=track["display_artist"],
        title=track["display_title"],
        candidate_artist=candidate_artist or candidate.channel,
        candidate_title=candidate_title,
    )


def _mark_youtube_review(con, track_id: int, *, reason: str, score: float | None = None) -> None:
    upsert_youtube_asset(
        con,
        track_id=track_id,
        match_confidence=score,
        file_exists=False,
        status="review",
    )
    add_event(
        con,
        track_id,
        "ambiguous_youtube_match",
        "youtube_sync",
        {"reason": reason, "score": score},
        dedupe_key=f"ambiguous_youtube_match:{track_id}:{reason}",
    )


def sync_youtube(config: Config, *, apply: bool, client: YouTubeClientProtocol | None = None) -> YouTubeSummary:
    if apply:
        assert_legacy_downloader_safe(config)
    summary = YouTubeSummary()
    client = client or YtDlpClient(config)
    with connect(config) as con:
        tracks = wanted_tracks(con)
        local_ids = _local_track_keys(config)
        for track in tracks:
            summary.wanted += 1
            if PLACEHOLDER_RE.search(f"{track['display_artist']} {track['display_title']}"):
                summary.review += 1
                if apply:
                    with transaction(con):
                        _mark_youtube_review(con, track["id"], reason="placeholder artist/title from logger", score=0.0)
                continue
            if track["id"] in local_ids:
                summary.already_local += 1
                continue
            existing_match = _existing_local_match(
                con,
                track=track,
                artist=track["display_artist"],
                title=track["display_title"],
            )
            if existing_match:
                summary.already_local += 1
                if apply:
                    with transaction(con):
                        add_event(
                            con,
                            track["id"],
                            "youtube_skipped_existing_local_match",
                            "youtube_sync",
                            {
                                "matched_track_id": existing_match["track_id"],
                                "matched_asset_id": existing_match["asset_id"],
                                "matched_file_path": existing_match["file_path"],
                            },
                            dedupe_key=f"youtube_skipped_existing_local_match:{track['id']}:{existing_match['asset_id']}",
                        )
                continue
            candidates = client.search(track["display_artist"], track["display_title"])
            best = None
            best_score = 0.0
            for candidate in candidates:
                score = _candidate_score(track, candidate)
                if score > best_score:
                    best = candidate
                    best_score = score
            threshold = config.float("HCR_YOUTUBE_MATCH_THRESHOLD")
            if best is None or best_score < threshold:
                summary.review += 1
                if apply:
                    with transaction(con):
                        _mark_youtube_review(con, track["id"], reason="below threshold or not found", score=best_score or None)
                continue
            if not apply:
                summary.downloaded += 1
                continue
            candidate_artist, candidate_title = parse_artist_title(best.title)
            candidate_existing_match = _existing_local_match(
                con,
                track=track,
                artist=candidate_artist or track["display_artist"],
                title=candidate_title or best.title,
            )
            if candidate_existing_match:
                summary.already_local += 1
                with transaction(con):
                    add_event(
                        con,
                        track["id"],
                        "youtube_skipped_existing_local_match",
                        "youtube_sync",
                        {
                            "candidate_title": best.title,
                            "candidate_url": best.url,
                            "matched_track_id": candidate_existing_match["track_id"],
                            "matched_asset_id": candidate_existing_match["asset_id"],
                            "matched_file_path": candidate_existing_match["file_path"],
                        },
                        dedupe_key=f"youtube_skipped_candidate_existing_local_match:{track['id']}:{candidate_existing_match['asset_id']}:{best.video_id}",
                    )
                continue
            with transaction(con):
                current = con.execute("SELECT status FROM tracks WHERE id = ?", (track["id"],)).fetchone()
                if not current or current["status"] == "excluded":
                    summary.skipped += 1
                    continue
            output = client.download(best)
            with transaction(con):
                current = con.execute("SELECT status FROM tracks WHERE id = ?", (track["id"],)).fetchone()
                if not current or current["status"] == "excluded":
                    try:
                        output.unlink()
                    except FileNotFoundError:
                        pass
                    summary.skipped += 1
                    continue
                upsert_youtube_asset(
                    con,
                    track_id=track["id"],
                    youtube_video_id=best.video_id or youtube_id_from_path(output),
                    youtube_url=best.url,
                    file_path=str(output),
                    file_exists=True,
                    match_confidence=best_score,
                    status="downloaded",
                    downloaded_at=now_utc(),
                )
                add_event(
                    con,
                    track["id"],
                    "youtube_downloaded",
                    "youtube_sync",
                    {"youtube_video_id": best.video_id, "file_path": str(output)},
                )
                summary.downloaded += 1
    return summary
