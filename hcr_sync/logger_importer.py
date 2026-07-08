"""Import legacy Hardcore Radio logger files into SQLite."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import add_event, add_observation, connect, ensure_track_from_raw, now_utc, set_state, transaction
from .identity import compact_text, parse_artist_title


@dataclass
class ImportSummary:
    files_read: int = 0
    rows_seen: int = 0
    tracks_created_or_seen: int = 0
    observations_added: int = 0
    skipped_excluded: int = 0
    errors: int = 0


def _file_state(path: Path) -> dict[str, str]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": "false"}
    return {
        "exists": "true",
        "inode": str(getattr(stat, "st_ino", "")),
        "size": str(stat.st_size),
        "mtime": str(stat.st_mtime_ns),
    }


def _remember_file_state(con, prefix: str, path: Path) -> None:
    state = _file_state(path)
    set_state(con, f"last_{prefix}_path", str(path))
    set_state(con, f"last_{prefix}_inode", state.get("inode", ""))
    set_state(con, f"last_{prefix}_size", state.get("size", ""))
    set_state(con, f"last_{prefix}_mtime", state.get("mtime", ""))


def _iter_seen_jsonl(path: Path):
    if not path.exists():
        return
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            raw_track = stripped
            observed_at = ""
        else:
            raw_track = compact_text(record.get("track") if isinstance(record, dict) else stripped)
            observed_at = compact_text(record.get("first_seen_at") if isinstance(record, dict) else "")
        if raw_track:
            yield {
                "observed_at": observed_at or now_utc(),
                "raw_track": raw_track,
                "raw_line": stripped,
                "source": "seen-tracks.jsonl",
                "line_number": line_number,
            }


def _iter_played_tsv(path: Path):
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    header: list[str] | None = None
    data_lines = lines
    first = [col.strip().casefold() for col in lines[0].split("\t")]
    if {"track", "title", "name", "song", "query"} & set(first) or {"timestamp", "time", "played_at"} & set(first):
        header = first
        data_lines = lines[1:]
    track_index = None
    if header:
        for index, column in enumerate(header):
            if column in {"track", "title", "name", "artist_title", "song", "query"}:
                track_index = index
                break
    for offset, line in enumerate(data_lines, 1 + (1 if header else 0)):
        if not line.strip():
            continue
        row = next(csv.reader([line], delimiter="\t"))
        if track_index is not None and track_index < len(row):
            raw_track = row[track_index]
            observed_at = row[0] if row else ""
        elif len(row) >= 2 and row[0].startswith("20"):
            observed_at = row[0]
            raw_track = " ".join(row[1:])
        else:
            observed_at = ""
            raw_track = " ".join(row)
        raw_track = compact_text(raw_track)
        if raw_track:
            yield {
                "observed_at": compact_text(observed_at) or now_utc(),
                "raw_track": raw_track,
                "raw_line": line,
                "source": "played-tracks.tsv",
                "line_number": offset,
            }


def _import_entry(con, entry: dict[str, object], summary: ImportSummary, apply: bool) -> None:
    raw_track = str(entry["raw_track"])
    raw_artist, raw_title = parse_artist_title(raw_track)
    if not apply:
        summary.rows_seen += 1
        return
    track = ensure_track_from_raw(con, raw_track, status="wanted")
    summary.tracks_created_or_seen += 1
    if track["status"] == "excluded":
        summary.skipped_excluded += 1
        add_event(
            con,
            track["id"],
            "skipped_excluded_track",
            str(entry["source"]),
            {"raw_line": str(entry["raw_line"]), "observed_at": str(entry["observed_at"])},
            dedupe_key=f"skipped_excluded:{entry['source']}:{entry['observed_at']}:{entry['raw_line']}",
        )
    if add_observation(
        con,
        track_id=track["id"],
        observed_at=str(entry["observed_at"]),
        source=str(entry["source"]),
        raw_artist=raw_artist,
        raw_title=raw_title,
        raw_line=str(entry["raw_line"]),
    ):
        summary.observations_added += 1
    summary.rows_seen += 1


def import_logger(config: Config, *, apply: bool) -> ImportSummary:
    summary = ImportSummary()
    paths = [
        ("seen_tracks_jsonl", config.seen_tracks_path, _iter_seen_jsonl),
        ("played_tracks_tsv", config.played_tracks_path, _iter_played_tsv),
    ]
    with connect(config) as con:
        with transaction(con):
            for prefix, path, iterator in paths:
                if path.exists():
                    summary.files_read += 1
                for entry in iterator(path) or []:
                    _import_entry(con, entry, summary, apply)
                if apply:
                    _remember_file_state(con, prefix, path)
    return summary


def migration_snapshot(config: Config, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for path in [config.seen_tracks_path, config.played_tracks_path, *config.seen_tracks_path.parent.glob("youtube-download-*.tsv"), *config.seen_tracks_path.parent.glob("youtube-download-*.jsonl")]:
        if not path.exists() or not path.is_file():
            continue
        target = destination / path.name
        if target.exists():
            continue
        target.write_bytes(path.read_bytes())
        copied.append(target)
    return copied
