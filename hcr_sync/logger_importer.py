"""Import legacy Hardcore Radio logger files into SQLite."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import add_event, add_observation, connect, ensure_track_from_raw, set_state, transaction
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


def _input_error(source: str, line_number: int, reason: str) -> ValueError:
    # Only fixed labels/reasons and line numbers belong in CLI errors.
    return ValueError(f"{source} line {line_number}: {reason}")


def _source_lines(path: Path, source: str) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise _input_error(source, 0, "cannot read UTF-8 input") from None


def _source_timestamp(value: object, source: str, line_number: int) -> str:
    timestamp = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)",
        timestamp,
    ):
        raise _input_error(source, line_number, "usable source timestamp required")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise _input_error(source, line_number, "invalid source timestamp") from None
    # Do not normalize spelling: existing observation identities include it.
    return timestamp


def _iter_seen_jsonl(path: Path):
    if not path.exists():
        return
    source = "seen-tracks.jsonl"
    for line_number, line in enumerate(_source_lines(path, source), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            raise _input_error(source, line_number, "JSON object required") from None
        if not isinstance(record, dict):
            raise _input_error(source, line_number, "JSON object required")
        raw_track = record.get("track")
        if not isinstance(raw_track, str) or not compact_text(raw_track):
            raise _input_error(source, line_number, "nonempty track string required")
        yield {
            "observed_at": _source_timestamp(record.get("first_seen_at"), source, line_number),
            "raw_track": compact_text(raw_track),
            "raw_line": stripped,
            "source": source,
            "line_number": line_number,
        }


def _iter_played_tsv(path: Path):
    if not path.exists():
        return
    source = "played-tracks.tsv"
    timestamp_aliases = {"timestamp", "time", "played_at"}
    track_aliases = {"track", "title", "name", "artist_title", "song", "query"}
    first_record = True
    timestamp_index, track_index, width = 0, 1, 2
    for line_number, line in enumerate(_source_lines(path, source), 1):
        if not line.strip():
            continue
        # The poller writes literal tab-separated text, not CSV-escaped fields.
        # A quote at the start of a title is ordinary track text.
        row = line.split("\t")
        if first_record:
            first_record = False
            columns = [column.strip().casefold() for column in row]
            # A timestamp-first data row can itself have a track named "Song".
            if not re.match(r"\d{4}-", row[0].strip()) and (timestamp_aliases | track_aliases) & set(columns):
                times = [i for i, column in enumerate(columns) if column in timestamp_aliases]
                tracks = [i for i, column in enumerate(columns) if column in track_aliases]
                if len(times) != 1 or len(tracks) != 1:
                    raise _input_error(source, line_number, "unambiguous timestamp and track headers required")
                timestamp_index, track_index, width = times[0], tracks[0], len(columns)
                continue
        if len(row) != width:
            raise _input_error(source, line_number, "timestamp and track columns required")
        observed_at = _source_timestamp(row[timestamp_index], source, line_number)
        raw_track = compact_text(row[track_index])
        if not raw_track:
            raise _input_error(source, line_number, "nonempty track string required")
        yield {
            "observed_at": observed_at,
            "raw_track": raw_track,
            "raw_line": line,
            "source": source,
            "line_number": line_number,
        }


def _import_entry(con, config: Config, entry: dict[str, object], summary: ImportSummary, apply: bool) -> None:
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
    observation_added = add_observation(
        con,
        track_id=track["id"],
        observed_at=str(entry["observed_at"]),
        source=str(entry["source"]),
        raw_artist=raw_artist,
        raw_title=raw_title,
        raw_line=str(entry["raw_line"]),
    )
    if observation_added:
        summary.observations_added += 1
    if config.bool("HCR_AUDIT_VERBOSE"):
        add_event(
            con,
            track["id"],
            "logger_entry_imported",
            str(entry["source"]),
            {
                "source": str(entry["source"]),
                "line_number": entry.get("line_number"),
                "observed_at": str(entry["observed_at"]),
                "raw_track": raw_track,
                "raw_line": str(entry["raw_line"]),
                "track_status": track["status"],
                "observation_added": observation_added,
                "duplicate": not observation_added,
                "skipped_excluded": track["status"] == "excluded",
            },
        )
    summary.rows_seen += 1


def import_logger(config: Config, *, apply: bool) -> ImportSummary:
    summary = ImportSummary()
    paths = [
        ("seen_tracks_jsonl", config.seen_tracks_path, _iter_seen_jsonl),
        ("played_tracks_tsv", config.played_tracks_path, _iter_played_tsv),
    ]
    # Validate both files before opening the database or recording any progress.
    # A malformed second file must not partially import the first one.
    inputs = [(prefix, path, list(iterator(path))) for prefix, path, iterator in paths]
    summary.files_read = sum(path.exists() for _, path, _ in inputs)
    if not apply:
        summary.rows_seen = sum(len(entries) for _, _, entries in inputs)
        return summary
    with connect(config) as con:
        with transaction(con):
            for prefix, path, entries in inputs:
                for entry in entries:
                    _import_entry(con, config, entry, summary, apply)
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
