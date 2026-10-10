"""Explicit, evidence-only repair of legacy YouTube scheduling holds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .db import YOUTUBE_QUEUE_SCHEMA, transaction
from .identity import radio_metadata_placeholder


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('seed_at must include a timezone')
    return parsed.astimezone(timezone.utc)


def _plan(config, con, seed):
    from .youtube_local import local_satisfaction
    from .youtube_queue import source_fingerprint
    from .youtube_sync import PLACEHOLDER_RE, SOURCE_NON_TRACK_RE

    schedule_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='youtube_schedule'").fetchone()
    pending_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='youtube_pending_work'").fetchone()
    preview, changes = [], []
    cache = {}
    for track in con.execute('SELECT * FROM tracks ORDER BY id'):
        track_id = track['id']
        category, reason = 'uncertain', 'no trusted algorithmic hold evidence'
        events = con.execute("SELECT * FROM events WHERE track_id=? ORDER BY id", (track_id,)).fetchall()
        assets = con.execute('SELECT * FROM youtube_assets WHERE track_id=?', (track_id,)).fetchall()
        existing = con.execute('SELECT * FROM youtube_schedule WHERE track_id=?', (track_id,)).fetchone() if schedule_exists else None
        pending = con.execute("SELECT 1 FROM youtube_pending_work WHERE track_id=? AND state NOT IN ('completed','cancelled') LIMIT 1", (track_id,)).fetchone() if pending_exists else None
        fingerprint = source_fingerprint(track)
        algorithmic = None
        hold = False
        unknown_hold = False
        for event in events:
            kind, actor = event['event_type'], event['event_source']
            if actor in {'manual', 'manual_exclude', 'manual_repair'} or kind in {'youtube_candidate_already_linked', 'local_file_deleted', 'local_file_deleted_by_user', 'local_file_moved_to_trash', 'suspected_local_delete'}:
                hold = True
            if kind not in {'ambiguous_youtube_match', 'youtube_download_failed'}:
                continue
            try:
                payload = json.loads(event['payload_json'])
            except (ValueError, TypeError):
                payload = None
            trusted = actor == 'youtube_sync' and isinstance(payload, dict)
            if kind == 'youtube_download_failed':
                trusted = trusted and {'youtube_video_id', 'youtube_url', 'candidate_title', 'error'} <= set(payload)
            if kind == 'ambiguous_youtube_match':
                trusted = trusted and (set(payload) == {'score'} or payload.get('reason') == 'below threshold or not found')
                trusted = trusted and (payload.get('score') is None or (type(payload.get('score')) in {int, float} and 0 <= payload['score'] <= 1))
            if trusted:
                algorithmic = event
            else:
                # Unknown or explicit source-quality holds cannot be inferred away.
                if actor == 'youtube_sync' and isinstance(payload, dict) and payload.get('reason') in {'source row looks like mix, set, compilation, or non-track item', 'placeholder artist/title from logger'}:
                    hold = True
                else:
                    unknown_hold = True
        excluded = con.execute('SELECT 1 FROM exclusions WHERE track_id=? LIMIT 1', (track_id,)).fetchone()
        source_text = f"{track['display_artist']} {track['display_title']}"
        if track['status'] != 'wanted' or excluded or hold or any(a['suspected_missing_at'] or a['status'] == 'deleted' for a in assets):
            category, reason = 'manualownership', 'exclusion, deletion, ownership or explicit hold'
        elif radio_metadata_placeholder(track['display_artist'], track['display_title']) or PLACEHOLDER_RE.search(source_text) or SOURCE_NON_TRACK_RE.search(source_text):
            category, reason = 'manualownership', 'placeholder or non-track source'
        elif pending:
            category, reason = 'deferred', 'pending download recovery preserved'
        elif unknown_hold:
            category, reason = 'uncertain', 'unknown hold origin retained'
        else:
            local_status, _ = local_satisfaction(config, con, track, cache=cache, persist=False)
            if local_status == 'satisfied':
                category, reason = 'local-satisfied', 'verified local recording evidence'
            elif local_status == 'ambiguous':
                category, reason = 'uncertain', 'local recording evidence is ambiguous'
            elif existing and not (existing['phase'] == 'held' and existing['hold_origin'] == 'legacy_unclassified' and existing['source_fingerprint'] == fingerprint):
                category, reason = 'deferred', 'existing schedule preserved'
            elif algorithmic:
                category, reason = 'algorithmic', 'trusted historical algorithmic failure'
                jitter = int.from_bytes(hashlib.sha256(track['canonical_key'].encode()).digest(), 'big') % (6 * 86400)
                due = (seed + timedelta(days=1, seconds=jitter)).isoformat()
                if existing:
                    try:
                        due = max(_timestamp(existing['next_eligible_at']), _timestamp(due)).isoformat()
                    except (ValueError, TypeError):
                        category, reason = 'uncertain', 'invalid existing scheduling timestamp'
                if category == 'algorithmic':
                    changes.append({'track_id': track_id, 'source_fingerprint': fingerprint, 'next_eligible_at': due,
                                    'legacy_evidence_at': algorithmic['created_at'], 'event_id': algorithmic['id']})
        preview.append({'track_id': track_id, 'category': category, 'reason': reason})
    return preview, changes


def _backup(source, path):
    parent = path.parent
    parent_stat = parent.stat()
    if parent.is_symlink() or parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise ValueError('backup directory must be owned by the current user and owner-only')
    import shutil
    required = source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0]
    if shutil.disk_usage(parent).free < required:
        raise ValueError('insufficient capacity for the SQLite backup')
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or destination.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('YouTube scheduling backup verification failed')


def repair_scheduling(config, *, apply=False, backup_path=None, seed_at=None):
    seed = _timestamp(seed_at) if seed_at is not None else datetime.now(timezone.utc)
    path = config.db_path.resolve()
    if apply and not backup_path:
        raise ValueError('backup_path is required with apply')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
        source.row_factory = sqlite3.Row
        preview, changes = _plan(config, source, seed)
        if apply:
            _backup(source, Path(backup_path).expanduser().absolute())
    if apply:
        with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True) as con:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA foreign_keys=ON')
            with transaction(con):
                current_preview, current_changes = _plan(config, con, seed)
                if current_preview != preview or current_changes != changes:
                    raise RuntimeError('YouTube scheduling evidence changed after backup; retry under the writer lock')
                con.execute(YOUTUBE_QUEUE_SCHEMA[0])
                con.execute(YOUTUBE_QUEUE_SCHEMA[-1])
                for change in changes:
                    con.execute('''INSERT INTO youtube_schedule
                        (track_id,source_fingerprint,phase,next_eligible_at,hold_origin,legacy_evidence_at,decision_json,updated_at)
                        VALUES (?,?,'search_retry',?,'legacy_algorithmic',?,?,?)
                        ON CONFLICT(track_id) DO UPDATE SET phase=excluded.phase,next_eligible_at=excluded.next_eligible_at,
                        hold_origin=excluded.hold_origin,legacy_evidence_at=excluded.legacy_evidence_at,
                        decision_json=excluded.decision_json,updated_at=excluded.updated_at''',
                        (change['track_id'],change['source_fingerprint'],change['next_eligible_at'],change['legacy_evidence_at'],
                         json.dumps({'origin':'legacy_algorithmic','event_id':change['event_id'],'counts_since_initialization':True},separators=(',',':')),seed.isoformat()))
    counts = {category: sum(item['category'] == category for item in preview)
              for category in ('local-satisfied','algorithmic','deferred','manualownership','uncertain')}
    return {'apply': apply, 'backup': str(backup_path) if apply else None, 'preview': preview, 'counts': counts, 'changes': changes}
