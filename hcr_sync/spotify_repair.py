"""Evidence-only repair of source scheduling, with an explicit verified backup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from .db import connect, transaction


def repair_scheduling(config, *, apply=False, backup=None):
    changes, skipped, ownership_holds = [], [], []
    hold_keys = set()
    with sqlite3.connect(config.db_path.resolve().as_uri() + '?mode=ro', uri=True) as source:
        source.row_factory = sqlite3.Row
        latest = {}
        for event in source.execute("SELECT * FROM events WHERE event_type IN ('spotify_search_completed','ambiguous_spotify_match','spotify_request_failed') ORDER BY id"):
            payload = json.loads(event['payload_json'])
            playlist = payload.get('playlist_id')
            candidate_id = payload.get('spotify_track_id')
            if candidate_id:
                for owner in source.execute('SELECT track_id,playlist_id,spotify_track_id,match_confidence,status,added_at FROM spotify_assets WHERE spotify_track_id=? AND track_id!=?', (candidate_id, event['track_id'])):
                    key = (event['track_id'], owner['track_id'], owner['playlist_id'], candidate_id)
                    if key not in hold_keys:
                        hold_keys.add(key)
                        ownership_holds.append({'source_track_id': event['track_id'], 'event_id': event['id'],
                            'current_asset': dict(owner), 'reason': 'cross-source candidate evidence; provenance repair remains unproven'})
            timestamp = payload.get('spotify_search_last_at')
            attempts = payload.get('spotify_search_attempts')
            if event['dedupe_key'] or not playlist or not timestamp or type(attempts) is not int or attempts < 0 or event['track_id'] is None:
                skipped.append({'event_id': event['id'], 'reason': 'insufficient source/playlist/attempt evidence'})
                continue
            # Timestamp parsing is a trust boundary, not inferred repair data.
            from datetime import datetime
            try:
                datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                next_at = payload.get('spotify_search_next_at')
                if next_at:
                    datetime.fromisoformat(next_at.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                skipped.append({'event_id': event['id'], 'reason': 'invalid scheduling timestamp'})
                continue
            key = (event['track_id'], playlist)
            proposal = {'track_id': key[0], 'playlist_id': playlist, 'search_last_at': timestamp,
                        'search_attempts': attempts, 'search_next_at': next_at, 'event_id': event['id']}
            if key not in latest or timestamp > latest[key]['search_last_at']:
                latest[key] = proposal
        for key, proposal in latest.items():
            asset = source.execute('SELECT search_last_at,search_attempts,search_next_at FROM spotify_assets WHERE track_id=? AND playlist_id=?', key).fetchone()
            if asset and asset['search_last_at'] and asset['search_last_at'] >= proposal['search_last_at']:
                continue
            changes.append({**proposal, 'before': dict(asset) if asset else None})
        if apply:
            if not backup:
                raise ValueError('--backup is required with --apply')
            backup_path = Path(backup).expanduser()
            descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or destination.execute('PRAGMA foreign_key_check').fetchall():
                    raise RuntimeError('Scheduling repair backup verification failed')
    if apply and changes:
        from .spotify_sync import _save_search_schedule
        with connect(config) as con:
            with transaction(con):
                for change in changes:
                    current = con.execute('SELECT search_last_at FROM spotify_assets WHERE track_id=? AND playlist_id=?', (change['track_id'], change['playlist_id'])).fetchone()
                    if current and current['search_last_at'] and current['search_last_at'] >= change['search_last_at']:
                        continue
                    _save_search_schedule(con, change['playlist_id'], change['track_id'], searched_at=change['search_last_at'],
                        attempts=change['search_attempts'], next_at=change['search_next_at'])
    return {'apply': apply, 'changes': changes, 'skipped': skipped, 'ownership_holds': ownership_holds}
