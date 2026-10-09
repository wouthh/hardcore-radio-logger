"""Durable Spotify work and membership evidence; remote writes follow commits."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .db import add_event, get_state, now_utc, set_state, transaction, upsert_spotify_asset, upsert_spotify_playlist_recording
from .spotify_adapter import BudgetDeferred, PlaylistSnapshot, SpotifyTrack


def save_work(con, playlist_id, track_id, kind, payload, *, work_key='', state='ready'):
    now = now_utc()
    con.execute('''INSERT INTO spotify_pending_work
        (playlist_id,track_id,kind,work_key,state,payload_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(playlist_id,track_id,kind,work_key)
        DO UPDATE SET state=excluded.state,payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
        (playlist_id, track_id, kind, work_key, state, json.dumps(payload, sort_keys=True), now, now))
    return con.execute('SELECT * FROM spotify_pending_work WHERE playlist_id=? AND track_id=? AND kind=? AND work_key=?',
                       (playlist_id, track_id, kind, work_key)).fetchone()


def enqueue_removal(con, playlist_id, track_id, uris, reason):
    for uri in sorted(set(uris)):
        existing = con.execute("SELECT 1 FROM spotify_pending_work WHERE playlist_id=? AND track_id=? AND kind='remove' AND work_key=?", (playlist_id, track_id, uri)).fetchone()
        if not existing:
            save_work(con, playlist_id, track_id, 'remove', {'uri': uri, 'reason': reason}, work_key=uri)


def cached_snapshot(con, playlist_id):
    value = get_state(con, f'spotify_membership:{playlist_id}', '')
    if not value:
        return None
    data = json.loads(value)
    data['tracks'] = [SpotifyTrack(**{**item, 'artist_ids': tuple(item.get('artist_ids', []))}) for item in data['tracks']]
    return PlaylistSnapshot(**data)


def invalidate_snapshot(con, playlist_id):
    con.execute('DELETE FROM sync_state WHERE key=?', (f'spotify_membership:{playlist_id}',))


def obtain_snapshot(con, config, client, *, protected=True, apply=True):
    playlist_id = config.get('HCR_SPOTIFY_PLAYLIST_ID')
    budget = getattr(client, 'budget', None)
    previous_reserve = budget.reserve if budget else 0
    if budget:
        budget.reserve = max(previous_reserve, 6) if protected else previous_reserve
    try:
        if not hasattr(client, 'playlist_metadata'):
            return client.playlist_snapshot(playlist_id)
        meta = client.playlist_metadata(playlist_id)
        cached = cached_snapshot(con, playlist_id)
        if cached and cached.complete and cached.snapshot_id == meta['snapshot_id'] and cached.total == meta['total']:
            return cached
        required = 2 + max(1, (meta['total'] + 49) // 50)
        scan_only_key = f'spotify_scan_only_spent:{playlist_id}'
        scan_only = protected and required + 6 > budget.limit
        if required > budget.limit or (scan_only and get_state(con, scan_only_key, '') == 'true'):
            if apply:
                with transaction(con):
                    set_state(con, f'spotify_degraded:{playlist_id}', json.dumps({
                        'reason': 'snapshot_budget', 'required_scan_budget': required,
                        'required_work_budget': required + 6, 'configured_budget': budget.limit,
                        'snapshot_id': meta['snapshot_id'],
                        'recovery': f'Set HCR_SPOTIFY_REQUEST_BUDGET to at least {required + 6}; run spotify scan --apply',
                    }, sort_keys=True))
            raise BudgetDeferred(required=required + (6 if protected else 0))
        if scan_only:
            budget.reserve = 0
            if apply:
                with transaction(con):
                    set_state(con, scan_only_key, 'true')
        snapshot = client.playlist_snapshot(playlist_id, metadata=meta)
        if snapshot.complete and apply:
            with transaction(con):
                set_state(con, f'spotify_membership:{playlist_id}', json.dumps(asdict(snapshot), sort_keys=True))
                set_state(con, f'spotify_degraded:{playlist_id}', '' if snapshot.identified else 'unidentifiable_entries')
        return snapshot
    finally:
        if budget:
            budget.reserve = previous_reserve


@dataclass
class PendingSummary:
    added: int = 0
    tentative_added: int = 0
    removed: int = 0
    acknowledged: int = 0
    deferred: int = 0
    pending: int = 0
    rate_limited: bool = False
    failure: Exception | None = None


def _finish_add(con, config, row, payload):
    from .spotify_sync import _spotify_asset_has_primary_history
    candidate = SpotifyTrack(**payload['candidate'])
    track_id, playlist_id = row['track_id'], row['playlist_id']
    asset = con.execute('SELECT * FROM spotify_assets WHERE track_id=? AND playlist_id=?', (track_id, playlist_id)).fetchone()
    confident = payload['confident']
    status = 'added' if confident else 'review'
    upsert_spotify_playlist_recording(con, playlist_id=playlist_id, spotify_track_id=candidate.track_id,
        spotify_track_uri=candidate.uri, spotify_artist=candidate.artist, spotify_title=candidate.title,
        artist_ids=candidate.artist_ids, album=candidate.album, isrc=candidate.isrc, duration_ms=candidate.duration_ms,
        track_id=track_id, in_playlist=True, status=status)
    if asset is None or not asset['spotify_track_id'] or asset['spotify_track_id'] == candidate.track_id or not _spotify_asset_has_primary_history(con, asset):
        upsert_spotify_asset(con, track_id=track_id, playlist_id=playlist_id, spotify_track_uri=candidate.uri,
            spotify_track_id=candidate.track_id, spotify_artist=candidate.artist, spotify_title=candidate.title,
            in_playlist=True, match_confidence=payload['score'], status=status, added_at=payload['searched_at'])
    else:
        con.execute('UPDATE spotify_assets SET in_playlist=1,suspected_missing_at=NULL,updated_at=? WHERE id=?', (now_utc(), asset['id']))
    event_type = 'spotify_added' if confident else 'spotify_tentatively_added'
    add_event(con, track_id, event_type, 'spotify_sync', {
        'playlist_id': playlist_id, 'spotify_track_id': candidate.track_id,
        'spotify_artist': candidate.artist, 'spotify_title': candidate.title, 'score': payload['score'],
        'match_threshold': config.float('HCR_SPOTIFY_MATCH_THRESHOLD'),
        'tentative_threshold': config.float('HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD'),
        'match_status': 'added' if confident else 'tentative_review',
        'spotify_search_last_at': payload['searched_at'], 'verified': True,
    }, dedupe_key=f"{event_type}:{track_id}:{candidate.track_id}")


def _finish_remove(con, row, payload):
    recording_id = payload['uri'].split(':')[-1]
    con.execute("UPDATE spotify_playlist_recordings SET in_playlist=0,status='removed',suspected_missing_at=NULL,updated_at=? WHERE playlist_id=? AND spotify_track_id=? AND track_id=?", (now_utc(), row['playlist_id'], recording_id, row['track_id']))
    active = con.execute('SELECT 1 FROM spotify_playlist_recordings WHERE playlist_id=? AND track_id=? AND in_playlist=1', (row['playlist_id'], row['track_id'])).fetchone()
    if not active:
        con.execute("UPDATE spotify_assets SET in_playlist=0,status='removed',suspected_missing_at=NULL,updated_at=? WHERE playlist_id=? AND track_id=?", (now_utc(), row['playlist_id'], row['track_id']))
    add_event(con, row['track_id'], 'removed_from_spotify_due_to_exclusion', 'spotify_recovery', {'playlist_id': row['playlist_id'], 'spotify_track_uri': payload['uri'], 'reason': payload['reason'], 'verified': True})


def _finalize_pending(con, config, row, payload):
    add = row['kind'] == 'add'
    compensated = False
    with transaction(con):
        if add:
            current = con.execute('SELECT status FROM tracks WHERE id=?', (row['track_id'],)).fetchone()
            _finish_add(con, config, row, payload)
            if current['status'] == 'excluded':
                enqueue_removal(con, row['playlist_id'], row['track_id'], [payload['candidate']['uri']], 'excluded_during_add')
                compensated = True
        else:
            _finish_remove(con, row, payload)
        con.execute('DELETE FROM spotify_pending_work WHERE id=?', (row['id'],))
    return not compensated


def recover_pending(con, config, client, snapshot=None, *, dispatch=True):
    from .spotify_sync import _is_rate_limited, _remember_spotify_rate_limit, _remember_failed_spotify_attempt, _spotify_candidate_used_by_other_track, _spotify_cooldown_active
    result = PendingSummary()
    playlist_id = config.get('HCR_SPOTIFY_PLAYLIST_ID')
    if _spotify_cooldown_active(con):
        result.rate_limited = True
        return result
    rows = list(con.execute("SELECT * FROM spotify_pending_work WHERE playlist_id=? AND kind!='search' ORDER BY CASE WHEN state='ready' THEN 1 ELSE 0 END, id", (playlist_id,)))
    for row in rows:
        if row['state'] == 'ready' and con.execute("SELECT 1 FROM spotify_pending_work WHERE playlist_id=? AND kind!='search' AND state!='ready'", (playlist_id,)).fetchone():
            result.deferred += 1
            continue
        payload = json.loads(row['payload_json'])
        retry_at = payload.get('retry_at', '')
        if retry_at and retry_at > now_utc():
            continue
        source = con.execute('SELECT * FROM tracks WHERE id=?', (row['track_id'],)).fetchone()
        add = row['kind'] == 'add'
        candidate = SpotifyTrack(**payload['candidate']) if add else None
        uri = candidate.uri if add else payload['uri']
        target_id = candidate.track_id if add else uri.split(':')[-1]
        # Intent ownership is rechecked even when the provider says it succeeded.
        conflict = _spotify_candidate_used_by_other_track(con, playlist_id=playlist_id, track_id=row['track_id'], spotify_track_id=target_id)
        competing = con.execute("SELECT 1 FROM spotify_pending_work WHERE playlist_id=? AND kind='add' AND work_key=? AND track_id!=? AND id<?", (playlist_id, uri, row['track_id'], row['id'])).fetchone() if add else None
        if add and payload.get('source') and source and payload['source'] != [source['display_artist'], source['display_title']]:
            result.deferred += 1
            continue
        if conflict or competing:
            result.deferred += 1
            continue
        if add and (not source or source['status'] != 'wanted') and row['state'] == 'ready':
            with transaction(con):
                con.execute('DELETE FROM spotify_pending_work WHERE id=?', (row['id'],))
            continue
        if not add and (not source or source['status'] != 'excluded') and row['state'] == 'ready':
            with transaction(con):
                con.execute('DELETE FROM spotify_pending_work WHERE id=?', (row['id'],))
            continue
        write_in_flight = False
        try:
            if snapshot is None:
                snapshot = obtain_snapshot(con, config, client, protected=False)
            if not snapshot.complete or not snapshot.snapshot_id:
                result.deferred += 1
                break
            present = target_id in {item.track_id for item in snapshot.tracks}
            verified = present if add else (snapshot.identified and not present)
            if verified:
                if _finalize_pending(con, config, row, payload):
                    result.added += int(add)
                    result.tentative_added += int(add and not payload['confident'])
                    result.removed += int(not add)
                continue
            if not add and (not source or source['status'] != 'excluded') and present:
                # An unexclude cancels removal; dispatched intents still need evidence.
                with transaction(con):
                    con.execute('DELETE FROM spotify_pending_work WHERE id=?', (row['id'],))
                continue
            if snapshot.identified and row['state'] == 'dispatched':
                # Verified non-effect clears uncertainty, but not the retry delay.
                asset = con.execute('SELECT search_next_at FROM spotify_assets WHERE track_id=? AND playlist_id=?', (row['track_id'], playlist_id)).fetchone()
                payload['retry_at'] = max(payload.get('retry_after_verification') or '', (asset['search_next_at'] or '') if add and asset else '')
                with transaction(con):
                    save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'], state='ready')
                if payload['retry_at'] and payload['retry_at'] > now_utc():
                    result.deferred += 1
                    continue
            if not snapshot.identified or not dispatch or row['state'] == 'acknowledged':
                result.deferred += 1
                continue
            if add and (not source or source['status'] != 'wanted'):
                with transaction(con):
                    con.execute('DELETE FROM spotify_pending_work WHERE id=?', (row['id'],))
                continue
            # A fresh, fully identified scan is required before dispatch/re-dispatch.
            budget = getattr(client, "budget", None)
            if budget and budget.remaining <= budget.reserve:
                raise BudgetDeferred()
            with transaction(con):
                con.execute("UPDATE spotify_pending_work SET state='dispatched',updated_at=? WHERE id=?", (now_utc(), row['id']))
                invalidate_snapshot(con, playlist_id)
            snapshot = None
            write_in_flight = True
            acknowledgement = client.add_tracks(playlist_id, [uri]) if add else client.remove_tracks(playlist_id, [uri])
            write_in_flight = False
            with transaction(con):
                payload['acknowledgement'] = acknowledgement
                save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'], state='acknowledged')
            result.acknowledged += 1
            # Verification shares the invocation budget and can finish next run.
            snapshot = obtain_snapshot(con, config, client, protected=False)
            present = target_id in {item.track_id for item in snapshot.tracks}
            if snapshot.complete and (present if add else snapshot.identified and not present):
                if _finalize_pending(con, config, row, payload):
                    result.added += int(add)
                    result.tentative_added += int(add and not payload['confident'])
                    result.removed += int(not add)
            else:
                result.deferred += 1
                break
        except BudgetDeferred:
            result.deferred += 1
            break
        except Exception as exc:
            status = getattr(exc, 'http_status', None)
            if _is_rate_limited(exc):
                _remember_spotify_rate_limit(con, config, exc, event_source='spotify_sync' if add else 'spotify_recovery')
                result.rate_limited = True
            # Failed reads cannot turn acknowledged writes into rejected writes.
            current = con.execute('SELECT state FROM spotify_pending_work WHERE id=?', (row['id'],)).fetchone()
            if write_in_flight and current and current['state'] == 'dispatched':
                if status is None or status >= 500:
                    from .spotify_sync import _next_spotify_retry_at
                    if add:
                        from .spotify_sync import _spotify_search_attempts
                        asset = con.execute('SELECT * FROM spotify_assets WHERE track_id=? AND playlist_id=?', (row['track_id'], playlist_id)).fetchone()
                        payload['failure_attempts'] = _spotify_search_attempts(asset) + 1
                    else:
                        payload['failure_attempts'] = payload.get('failure_attempts', 0) + 1
                    payload['retry_after_verification'] = _next_spotify_retry_at(datetime.now(timezone.utc), payload['failure_attempts'])
                    with transaction(con):
                        save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'], state='dispatched')
                if status is not None and 400 <= status < 500:
                    with transaction(con):
                        payload['retry_at'] = get_state(con, 'spotify_rate_limited_until', '') if status == 429 else ''
                        save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'], state='ready')
                if not add and status is not None and 400 <= status < 500 and status != 429:
                    from .spotify_sync import _next_spotify_retry_at
                    payload['failure_attempts'] = payload.get('failure_attempts', 0) + 1
                    payload['retry_at'] = _next_spotify_retry_at(datetime.now(timezone.utc), payload['failure_attempts'])
                    with transaction(con):
                        save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'])
                        add_event(con, row['track_id'], 'spotify_request_failed', 'spotify_recovery', {
                            'playlist_id': playlist_id, 'operation': 'remove', 'http_status': status,
                            'retry_at': payload['retry_at'], 'error_type': type(exc).__name__,
                        })
                if add:
                    asset = con.execute('SELECT * FROM spotify_assets WHERE track_id=? AND playlist_id=?', (row['track_id'], playlist_id)).fetchone()
                    _remember_failed_spotify_attempt(con, config, source, asset, exc, operation='add')
                    if status is None or status >= 500:
                        schedule = con.execute('SELECT search_next_at FROM spotify_assets WHERE track_id=? AND playlist_id=?', (row['track_id'], playlist_id)).fetchone()
                        payload['retry_after_verification'] = schedule['search_next_at']
                        with transaction(con):
                            save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'], state='dispatched')
                    if status in {400, 404}:
                        asset = con.execute('SELECT search_next_at FROM spotify_assets WHERE track_id=? AND playlist_id=?', (row['track_id'], playlist_id)).fetchone()
                        with transaction(con):
                            payload['retry_at'] = asset['search_next_at']
                            save_work(con, playlist_id, row['track_id'], row['kind'], payload, work_key=row['work_key'])
            if status in {400, 404}:
                result.deferred += 1
                continue
            result.failure = exc
            break
    result.pending = con.execute("SELECT COUNT(*) FROM spotify_pending_work WHERE playlist_id=? AND kind!='search'", (playlist_id,)).fetchone()[0]
    return result
