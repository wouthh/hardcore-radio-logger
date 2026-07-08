"""Command line interface for hcr_sync."""

from __future__ import annotations

import argparse
import sys

from .config import Config, load_config
from .db import connect, ensure_track, init_db, track_by_id, transaction, unexclude_track
from .doctor import format_doctor, run_doctor
from .local_files import import_local_files
from .logger_importer import import_logger
from .poller import poll_radio
from .reconcile import manual_exclude, reconcile
from .report import build_report, format_report
from .spotify_sync import backfill_spotify, spotify_auth, sync_spotify
from .system import LegacyDownloaderActive, sync_lock
from .youtube_sync import sync_youtube


def add_apply_args(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview actions without writing external/destructive changes.")
    mode.add_argument("--apply", action="store_true", help="Apply changes. Required for destructive actions.")


def is_apply(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "apply", False))


def print_kv(prefix: str, obj) -> None:
    values = getattr(obj, "__dict__", {})
    rendered = " ".join(f"{key}={value}" for key, value in values.items() if not key.startswith("_"))
    print(f"{prefix} {rendered}".strip())


def fatal_reconcile_refusals(refusals: list[str]) -> list[str]:
    return [refusal for refusal in refusals if not refusal.startswith("spotify:")]


def cmd_db(args: argparse.Namespace, config: Config) -> int:
    if args.db_command == "init":
        init_db(config)
        print(f"DB initialized: {config.db_path}")
        return 0
    return 2


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    result = run_doctor(config)
    print(format_doctor(result))
    return 0 if result.ok else 1


def cmd_poll_radio(args: argparse.Namespace, config: Config) -> int:
    changed, track = poll_radio(config, apply=is_apply(args))
    print(f"poll_radio changed={changed} track={track}")
    return 0


def cmd_import_logger(args: argparse.Namespace, config: Config) -> int:
    summary = import_logger(config, apply=is_apply(args))
    print_kv("import_logger", summary)
    return 0


def cmd_backfill_local(args: argparse.Namespace, config: Config) -> int:
    summary = import_local_files(config, apply=is_apply(args), establish_baseline=True)
    print_kv("backfill_local", summary)
    return 0


def cmd_scan_local(args: argparse.Namespace, config: Config) -> int:
    summary = import_local_files(config, apply=is_apply(args), establish_baseline=False)
    print_kv("scan_local", summary)
    return 0


def cmd_spotify(args: argparse.Namespace, config: Config) -> int:
    if args.spotify_command == "auth":
        user = spotify_auth(config)
        print(f"Spotify authenticated: {user}")
        return 0
    if args.spotify_command == "backfill":
        summary = backfill_spotify(config, apply=is_apply(args))
        print_kv("spotify_backfill", summary)
        return 0
    if args.spotify_command == "sync":
        summary = sync_spotify(config, apply=is_apply(args))
        print_kv("spotify_sync", summary)
        return 0
    return 2


def cmd_youtube(args: argparse.Namespace, config: Config) -> int:
    if args.youtube_command == "sync":
        summary = sync_youtube(config, apply=is_apply(args))
        print_kv("youtube_sync", summary)
        return 0
    return 2


def cmd_reconcile(args: argparse.Namespace, config: Config) -> int:
    summary = reconcile(
        config,
        apply=is_apply(args),
        force_mass_delete=args.force_mass_delete,
        force_confirm_deletions=args.force_confirm_deletions,
    )
    print_kv("reconcile", summary)
    for refusal in summary.refused:
        print(f"REFUSED {refusal}")
    for action in summary.planned:
        print(f"PLANNED track_id={action.track_id} reason={action.reason} detail={action.detail}")
    return 1 if summary.refused and is_apply(args) else 0


def _track_id_for_exclude(config: Config, args: argparse.Namespace) -> int:
    if args.track_id:
        return args.track_id
    if not args.artist and not args.title:
        raise RuntimeError("provide --track-id or --artist/--title")
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist=args.artist or "", title=args.title or "", status="wanted")
            return int(track["id"])


def cmd_exclude(args: argparse.Namespace, config: Config) -> int:
    track_id = _track_id_for_exclude(config, args)
    summary = manual_exclude(config, track_id=track_id, reason=args.reason, apply=is_apply(args))
    print_kv("exclude", summary)
    return 0


def cmd_unexclude(args: argparse.Namespace, config: Config) -> int:
    if not args.track_id:
        raise RuntimeError("--track-id is required for unexclude")
    if not is_apply(args):
        print(f"unexclude dry_run track_id={args.track_id}")
        return 0
    with connect(config) as con:
        with transaction(con):
            if not track_by_id(con, args.track_id):
                raise RuntimeError(f"track id not found: {args.track_id}")
            unexclude_track(con, track_id=args.track_id)
    print(f"unexclude applied track_id={args.track_id}")
    return 0


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    print(format_report(build_report(config)))
    return 0


def cmd_run_once(args: argparse.Namespace, config: Config) -> int:
    apply = is_apply(args)
    with sync_lock(config.sync_lock_path):
        if config.bool("HCR_RUN_POLLER"):
            changed, track = poll_radio(config, apply=apply)
            print(f"poll_radio changed={changed} track={track}")
        import_summary = import_logger(config, apply=apply)
        print_kv("import_logger", import_summary)
        local_summary = import_local_files(config, apply=apply, establish_baseline=False)
        print_kv("scan_local", local_summary)
        rec_summary = reconcile(
            config,
            apply=apply,
            force_mass_delete=args.force_mass_delete,
            force_confirm_deletions=args.force_confirm_deletions,
        )
        print_kv("reconcile", rec_summary)
        for refusal in rec_summary.refused:
            print(f"REFUSED {refusal}")
        yt_summary = sync_youtube(config, apply=apply)
        print_kv("youtube_sync", yt_summary)
        sp_summary = sync_spotify(config, apply=apply)
        print_kv("spotify_sync", sp_summary)
        print(format_report(build_report(config)))
    return 1 if apply and fatal_reconcile_refusals(rec_summary.refused) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hardcore Radio 3-way music sync.")
    parser.add_argument("--config", help="Path to hcr-sync env file.")
    sub = parser.add_subparsers(dest="command", required=True)

    db_parser = sub.add_parser("db")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init")
    db_parser.set_defaults(func=cmd_db)

    doctor_parser = sub.add_parser("doctor")
    doctor_parser.set_defaults(func=cmd_doctor)

    poll_parser = sub.add_parser("poll-radio")
    add_apply_args(poll_parser)
    poll_parser.set_defaults(func=cmd_poll_radio)

    import_parser = sub.add_parser("import-logger")
    add_apply_args(import_parser)
    import_parser.set_defaults(func=cmd_import_logger)

    backfill_parser = sub.add_parser("backfill-local")
    add_apply_args(backfill_parser)
    backfill_parser.set_defaults(func=cmd_backfill_local)

    scan_parser = sub.add_parser("scan-local")
    add_apply_args(scan_parser)
    scan_parser.set_defaults(func=cmd_scan_local)

    spotify_parser = sub.add_parser("spotify")
    spotify_sub = spotify_parser.add_subparsers(dest="spotify_command", required=True)
    spotify_sub.add_parser("auth")
    spotify_backfill = spotify_sub.add_parser("backfill")
    add_apply_args(spotify_backfill)
    spotify_sync_parser = spotify_sub.add_parser("sync")
    add_apply_args(spotify_sync_parser)
    spotify_parser.set_defaults(func=cmd_spotify)

    youtube_parser = sub.add_parser("youtube")
    youtube_sub = youtube_parser.add_subparsers(dest="youtube_command", required=True)
    youtube_sync_parser = youtube_sub.add_parser("sync")
    add_apply_args(youtube_sync_parser)
    youtube_parser.set_defaults(func=cmd_youtube)

    rec_parser = sub.add_parser("reconcile")
    add_apply_args(rec_parser)
    rec_parser.add_argument("--force-mass-delete", action="store_true")
    rec_parser.add_argument("--force-confirm-deletions", action="store_true")
    rec_parser.set_defaults(func=cmd_reconcile)

    exclude_parser = sub.add_parser("exclude")
    add_apply_args(exclude_parser)
    exclude_parser.add_argument("--track-id", type=int)
    exclude_parser.add_argument("--artist", default="")
    exclude_parser.add_argument("--title", default="")
    exclude_parser.add_argument("--reason", default="manual")
    exclude_parser.set_defaults(func=cmd_exclude)

    unexclude_parser = sub.add_parser("unexclude")
    add_apply_args(unexclude_parser)
    unexclude_parser.add_argument("--track-id", type=int, required=True)
    unexclude_parser.set_defaults(func=cmd_unexclude)

    report_parser = sub.add_parser("report")
    report_parser.set_defaults(func=cmd_report)

    run_parser = sub.add_parser("run-once")
    add_apply_args(run_parser)
    run_parser.add_argument("--force-mass-delete", action="store_true")
    run_parser.add_argument("--force-confirm-deletions", action="store_true")
    run_parser.set_defaults(func=cmd_run_once)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    try:
        return args.func(args, config)
    except LegacyDownloaderActive as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
