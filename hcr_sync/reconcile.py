"""Reconciliation between DB, local files, and Spotify playlist."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import (
    add_event,
    connect,
    get_state,
    mark_excluded,
    now_utc,
    set_state,
    transaction,
    upsert_youtube_asset,
)
from .local_files import AUDIO_EXTENSIONS, audio_paths
from .spotify_sync import SpotifyClientProtocol, SpotipyClient, spotify_enabled


@dataclass
class PlannedAction:
    track_id: int
    reason: str
    detail: str


@dataclass
class ReconcileSummary:
    suspected_local: int = 0
    excluded_local: int = 0
    suspected_spotify: int = 0
    excluded_spotify: int = 0
    tentative_spotify_removed: int = 0
    spotify_removed: int = 0
    local_trashed: int = 0
    refused: list[str] = field(default_factory=list)
    planned: list[PlannedAction] = field(default_factory=list)


def _local_scan_guard(config: Config, con, current_count: int, known_count: int, *, force_mass_delete: bool) -> str:
    if get_state(con, "local_baseline_complete") != "true":
        return "local baseline is not complete"
    if not config.music_dir.exists():
        return "music dir does not exist"
    if not config.music_dir.is_dir():
        return "music dir is not a directory"
    if current_count == 0 and known_count > 0:
        return "local audio scan is empty while DB has known local assets"
    previous = int(get_state(con, "last_local_scan_count", "0") or "0")
    min_ratio = config.float("HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO")
    if previous and current_count < previous * min_ratio:
        return "music dir scan dropped below configured safety ratio"
    missing = list(
        con.execute(
            """
            SELECT COUNT(*) AS count
              FROM youtube_assets
             WHERE file_exists = 1 AND status = 'downloaded' AND file_path IS NOT NULL
            """
        )
    )[0]["count"] - current_count
    if missing > config.int("HCR_RECONCILE_MAX_EXCLUSIONS") and not force_mass_delete:
        return "too many local exclusions would be detected without --force-mass-delete"
    return ""


def _spotify_guard(config: Config, con, snapshot, *, force_mass_delete: bool) -> str:
    if get_state(con, "spotify_baseline_complete") != "true":
        return "spotify baseline is not complete"
    if snapshot is None:
        return "spotify playlist snapshot was not fetched"
    if not snapshot.complete:
        return "spotify playlist pagination did not complete"
    if not snapshot.snapshot_id:
        return "spotify playlist snapshot id is missing"
    if snapshot.tracks and not any(track.track_id for track in snapshot.tracks):
        return "spotify playlist snapshot has no usable track identities"
    previous = int(get_state(con, "last_spotify_playlist_count", "0") or "0")
    if previous and len(snapshot.tracks) < previous * config.float("HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO"):
        return "spotify playlist count is suspiciously low"
    known = con.execute(
        "SELECT COUNT(*) AS count FROM spotify_assets WHERE playlist_id = ? AND in_playlist = 1",
        (config.get("HCR_SPOTIFY_PLAYLIST_ID"),),
    ).fetchone()["count"]
    if known and len(snapshot.tracks) == 0:
        return "spotify playlist snapshot is empty while DB has known playlist assets"
    if known and len(snapshot.tracks) < known * config.float("HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO"):
        return "spotify playlist count is suspiciously low compared to DB playlist assets"
    if known - len(snapshot.tracks) > config.int("HCR_RECONCILE_MAX_EXCLUSIONS") and not force_mass_delete:
        return "too many spotify exclusions would be detected without --force-mass-delete"
    return ""


def _is_recent_self_added_spotify_asset(asset, previous_scan_at: str) -> bool:
    added_at = asset["added_at"]
    if not added_at:
        return False
    return not previous_scan_at or added_at > previous_scan_at


def _is_tentative_spotify_asset(config: Config, asset) -> bool:
    if asset["status"] == "review":
        return True
    score = asset["match_confidence"]
    return score is not None and float(score) < config.float("HCR_SPOTIFY_MATCH_THRESHOLD")


def _trash_file(config: Config, path: Path) -> Path | None:
    if not path.exists():
        return None
    if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
        return None
    if config.get("HCR_DELETE_MODE") == "delete":
        path.unlink()
        return None
    config.trash_dir.mkdir(parents=True, exist_ok=True)
    target = config.trash_dir / path.name
    counter = 1
    while target.exists():
        target = config.trash_dir / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


def _cascade_local(con, config: Config, track_id: int, summary: ReconcileSummary) -> None:
    rows = list(
        con.execute(
            "SELECT * FROM youtube_assets WHERE track_id = ? AND file_exists = 1 AND file_path IS NOT NULL",
            (track_id,),
        )
    )
    for row in rows:
        old_path = Path(row["file_path"])
        moved_to = _trash_file(config, old_path)
        touched_file = moved_to is not None or (old_path.suffix.casefold() in AUDIO_EXTENSIONS and not old_path.exists())
        con.execute(
            """
            UPDATE youtube_assets
               SET file_exists = 0, status = 'deleted', updated_at = ?, suspected_missing_at = NULL
             WHERE id = ?
            """,
            (now_utc(), row["id"]),
        )
        add_event(
            con,
            track_id,
            "local_file_moved_to_trash" if moved_to else ("local_file_deleted" if touched_file else "local_file_left_unmanaged"),
            "reconcile",
            {
                "old_path": row["file_path"],
                "new_path": str(moved_to) if moved_to else "",
                "delete_mode": config.get("HCR_DELETE_MODE"),
                "reason": "local cascade after exclusion",
            },
        )
        if touched_file:
            summary.local_trashed += 1


def _cascade_spotify(con, config: Config, track_id: int, summary: ReconcileSummary, client: SpotifyClientProtocol | None) -> None:
    playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID")
    rows = list(
        con.execute(
            "SELECT * FROM spotify_assets WHERE track_id = ? AND playlist_id = ? AND in_playlist = 1",
            (track_id, playlist_id),
        )
    )
    uris = [row["spotify_track_uri"] for row in rows if row["spotify_track_uri"]]
    if client and uris:
        client.remove_tracks(playlist_id, uris)
        summary.spotify_removed += len(uris)
    for row in rows:
        con.execute(
            """
            UPDATE spotify_assets
               SET in_playlist = 0, status = 'removed', updated_at = ?, suspected_missing_at = NULL
             WHERE id = ?
            """,
            (now_utc(), row["id"]),
        )
        add_event(
            con,
            track_id,
            "removed_from_spotify_due_to_exclusion",
            "reconcile",
            {"playlist_id": playlist_id, "spotify_track_id": row["spotify_track_id"], "reason": "local/global exclusion cascade"},
        )


def _confirm_or_suspect(
    con,
    *,
    table: str,
    asset_id: int,
    track_id: int,
    event_type: str,
    source: str,
    require_two_passes: bool,
    force_confirm: bool,
    apply: bool,
) -> bool:
    row = con.execute(f"SELECT suspected_missing_at FROM {table} WHERE id = ?", (asset_id,)).fetchone()
    if not require_two_passes or force_confirm or (row and row["suspected_missing_at"]):
        return True
    if apply:
        con.execute(
            f"UPDATE {table} SET suspected_missing_at = ?, updated_at = ? WHERE id = ?",
            (now_utc(), now_utc(), asset_id),
        )
        add_event(con, track_id, event_type, source, {"asset_id": asset_id})
    return False


def reconcile(
    config: Config,
    *,
    apply: bool,
    force_mass_delete: bool = False,
    force_confirm_deletions: bool = False,
    spotify_client: SpotifyClientProtocol | None = None,
) -> ReconcileSummary:
    summary = ReconcileSummary()
    with connect(config) as con:
        current_paths = {str(path) for path in audio_paths(config.music_dir)}
        known_local = list(
            con.execute(
                "SELECT * FROM youtube_assets WHERE file_exists = 1 AND status = 'downloaded' AND file_path IS NOT NULL"
            )
        )
        local_refusal = _local_scan_guard(config, con, len(current_paths), len(known_local), force_mass_delete=force_mass_delete)
        if local_refusal:
            summary.refused.append(f"local: {local_refusal}")
        else:
            for asset in known_local:
                if asset["file_path"] in current_paths:
                    continue
                summary.planned.append(PlannedAction(asset["track_id"], "local_deleted", asset["file_path"]))
                if not apply:
                    continue
                with transaction(con):
                    confirmed = _confirm_or_suspect(
                        con,
                        table="youtube_assets",
                        asset_id=asset["id"],
                        track_id=asset["track_id"],
                        event_type="suspected_local_delete",
                        source="local_deleted",
                        require_two_passes=config.bool("HCR_RECONCILE_REQUIRE_TWO_PASSES"),
                        force_confirm=force_confirm_deletions,
                        apply=apply,
                    )
                    if not confirmed:
                        summary.suspected_local += 1
                        continue
                    add_event(
                        con,
                        asset["track_id"],
                        "local_file_deleted_by_user",
                        "local_deleted",
                        {
                            "file_path": asset["file_path"],
                            "reason": "recorded downloaded file missing from healthy local scan",
                        },
                        dedupe_key=f"local_file_deleted_by_user:{asset['track_id']}:{asset['id']}",
                    )
                    mark_excluded(con, track_id=asset["track_id"], source="local_deleted", reason="local file missing")
                    _cascade_spotify(con, config, asset["track_id"], summary, spotify_client)
                    con.execute(
                        "UPDATE youtube_assets SET file_exists = 0, status = 'deleted', updated_at = ? WHERE id = ?",
                        (now_utc(), asset["id"]),
                    )
                    summary.excluded_local += 1
            if apply:
                with transaction(con):
                    set_state(con, "last_local_scan_count", str(len(current_paths)))
                    set_state(con, "last_local_scan_at", now_utc())

        playlist_id = config.get("HCR_SPOTIFY_PLAYLIST_ID") if spotify_enabled(config) else ""
        snapshot = None
        if playlist_id:
            try:
                client = spotify_client or SpotipyClient(config)
                snapshot = client.playlist_snapshot(playlist_id)
                spotify_client = client
            except Exception as exc:
                summary.refused.append(f"spotify: playlist fetch failed: {exc}")
        if playlist_id:
            spotify_refusal = _spotify_guard(config, con, snapshot, force_mass_delete=force_mass_delete)
            if spotify_refusal:
                summary.refused.append(f"spotify: {spotify_refusal}")
            elif snapshot is not None:
                current_ids = {track.track_id for track in snapshot.tracks if track.track_id}
                previous_spotify_scan_at = get_state(con, "last_spotify_scan_at", "")
                known_spotify = list(
                    con.execute(
                        "SELECT * FROM spotify_assets WHERE playlist_id = ? AND in_playlist = 1 AND spotify_track_id IS NOT NULL",
                        (playlist_id,),
                    )
                )
                for asset in known_spotify:
                    if asset["spotify_track_id"] in current_ids:
                        if apply and asset["suspected_missing_at"]:
                            with transaction(con):
                                con.execute(
                                    "UPDATE spotify_assets SET suspected_missing_at = NULL, updated_at = ? WHERE id = ?",
                                    (now_utc(), asset["id"]),
                                )
                                add_event(
                                    con,
                                    asset["track_id"],
                                    "spotify_removal_suspicion_cleared",
                                    "reconcile",
                                    {"spotify_track_id": asset["spotify_track_id"]},
                                    dedupe_key=f"spotify_removal_suspicion_cleared:{asset['track_id']}:{asset['id']}:{snapshot.snapshot_id}",
                                )
                        continue
                    if _is_recent_self_added_spotify_asset(asset, previous_spotify_scan_at):
                        continue
                    summary.planned.append(PlannedAction(asset["track_id"], "spotify_removed", asset["spotify_track_id"]))
                    if not apply:
                        continue
                    with transaction(con):
                        confirmed = _confirm_or_suspect(
                            con,
                            table="spotify_assets",
                            asset_id=asset["id"],
                            track_id=asset["track_id"],
                            event_type="suspected_spotify_remove",
                            source="spotify_removed",
                            require_two_passes=config.bool("HCR_RECONCILE_REQUIRE_TWO_PASSES"),
                            force_confirm=force_confirm_deletions,
                            apply=apply,
                        )
                        if not confirmed:
                            summary.suspected_spotify += 1
                            continue
                        if _is_tentative_spotify_asset(config, asset):
                            add_event(
                                con,
                                asset["track_id"],
                                "spotify_tentative_removed_by_user",
                                "spotify_removed",
                                {
                                    "spotify_track_id": asset["spotify_track_id"],
                                    "match_confidence": asset["match_confidence"],
                                    "reason": "tentative Spotify match removed; track remains wanted",
                                },
                                dedupe_key=f"spotify_tentative_removed_by_user:{asset['track_id']}:{asset['id']}",
                            )
                            con.execute(
                                """
                                UPDATE spotify_assets
                                   SET in_playlist = 0, status = 'removed', suspected_missing_at = NULL, updated_at = ?
                                 WHERE id = ?
                                """,
                                (now_utc(), asset["id"]),
                            )
                            summary.tentative_spotify_removed += 1
                            continue
                        add_event(
                            con,
                            asset["track_id"],
                            "spotify_removed_by_user",
                            "spotify_removed",
                            {
                                "spotify_track_id": asset["spotify_track_id"],
                                "match_confidence": asset["match_confidence"],
                                "reason": "confirmed Spotify playlist removal; global exclusion cascade",
                            },
                            dedupe_key=f"spotify_removed_by_user:{asset['track_id']}:{asset['id']}",
                        )
                        mark_excluded(con, track_id=asset["track_id"], source="spotify_removed", reason="spotify playlist removal")
                        _cascade_local(con, config, asset["track_id"], summary)
                        con.execute(
                            """
                            UPDATE spotify_assets
                               SET in_playlist = 0, status = 'removed', suspected_missing_at = NULL, updated_at = ?
                             WHERE id = ?
                            """,
                            (now_utc(), asset["id"]),
                        )
                        summary.excluded_spotify += 1
                if apply:
                    with transaction(con):
                        set_state(con, "last_spotify_snapshot_id", snapshot.snapshot_id)
                        set_state(con, "last_spotify_playlist_count", str(len(snapshot.tracks)))
                        set_state(con, "last_spotify_scan_at", now_utc())
    return summary


def manual_exclude(
    config: Config,
    *,
    track_id: int,
    reason: str,
    apply: bool,
    spotify_client: SpotifyClientProtocol | None = None,
) -> ReconcileSummary:
    summary = ReconcileSummary(planned=[PlannedAction(track_id, "manual", reason)])
    if not apply:
        return summary
    with connect(config) as con:
        with transaction(con):
            mark_excluded(con, track_id=track_id, source="manual", reason=reason)
            _cascade_local(con, config, track_id, summary)
            _cascade_spotify(con, config, track_id, summary, spotify_client)
    return summary
