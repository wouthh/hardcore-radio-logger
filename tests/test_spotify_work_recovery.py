"""Durable intent survives uncertain remote writes and interrupted local commits."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import sqlite3

import pytest

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.db import connect, ensure_track, init_db, mark_excluded, transaction, upsert_spotify_asset
from hcr_sync.spotify_adapter import RequestBudget, SpotifyTrack
from hcr_sync import spotify_work
from test_spotify_adapter import item, metadata, response, spotify_http


CANDIDATE = SpotifyTrack('spotify:track:target', 'target', 'Artist', 'Song')


def snapshot_responses(present, version):
    total = int(present)
    return [response(metadata(total, version)),
            response({'total': total, 'offset': 0, 'next': None,
                      'items': [item(id='target', uri=CANDIDATE.uri, name='Song')] if present else []}),
            response(metadata(total, version))]


def seed(tmp_path, *, kind='add', state='ready', excluded=False):
    config = Config(dict(DEFAULTS, HCR_DB_PATH=str(tmp_path / 'music.sqlite'),
                         HCR_SPOTIFY_PLAYLIST_ID='playlist', HCR_SPOTIFY_ENABLED='true'))
    init_db(config)
    with connect(config) as con, transaction(con):
        track = ensure_track(con, artist='Artist', title='Song', status='wanted')
        track_id = track['id']
        if kind == 'remove' or excluded:
            upsert_spotify_asset(con, track_id=track_id, playlist_id='playlist',
                                 spotify_track_id='target', spotify_track_uri=CANDIDATE.uri,
                                 spotify_artist='Artist', spotify_title='Song', in_playlist=kind == 'remove',
                                 match_confidence=1.0, status='added' if kind == 'remove' else 'review')
        if kind == 'remove' or excluded:
            mark_excluded(con, track_id=track_id, source='manual', reason='synthetic exclusion')
        payload = ({'candidate': asdict(CANDIDATE), 'score': 1.0, 'confident': True,
                    'searched_at': '2026-01-01T00:00:00Z'} if kind == 'add'
                   else {'uri': CANDIDATE.uri, 'reason': 'local/global exclusion cascade'})
        spotify_work.save_work(con, 'playlist', track_id, kind, payload, work_key=CANDIDATE.uri, state=state)
    return config, track_id


def work_state(config):
    with connect(config) as con:
        row = con.execute('SELECT kind,state,track_id FROM spotify_pending_work').fetchone()
        return tuple(row) if row else None


def assert_membership(config, track_id, present, status='wanted'):
    with connect(config) as con:
        assert con.execute('SELECT status FROM tracks WHERE id=?', (track_id,)).fetchone()[0] == status
        row = con.execute('SELECT in_playlist FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone()
        assert bool(row and row[0]) is present
        assert con.execute('SELECT count(*) FROM spotify_playlist_recordings WHERE track_id!=?', (track_id,)).fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['add', 'remove'])
def test_successful_write_with_lost_response_recovers_without_duplicate(tmp_path, kind):
    config, track_id = seed(tmp_path, kind=kind)
    before = kind == 'remove'
    responses = snapshot_responses(before, 'before') + [{'drop': True}]
    with spotify_http(tmp_path, responses) as (client, calls), connect(config) as con:
        first = spotify_work.recover_pending(con, config, client)
        assert first.failure is not None and first.pending == 1
        assert client.budget.used == len(calls) == 4
        assert calls[-1]['method'] == ('POST' if kind == 'add' else 'DELETE')
    assert work_state(config) == (kind, 'dispatched', track_id)
    assert_membership(config, track_id, before, 'excluded' if kind == 'remove' else 'wanted')
    with spotify_http(tmp_path, snapshot_responses(not before, 'after')) as (client, calls), connect(config) as con:
        second = spotify_work.recover_pending(con, config, client)
        assert (second.added, second.removed, second.pending) == ((1, 0, 0) if kind == 'add' else (0, 1, 0))
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    assert_membership(config, track_id, not before, 'excluded' if kind == 'remove' else 'wanted')


@pytest.mark.parametrize('state', ['dispatched', 'acknowledged'])
def test_restart_observed_presence_finishes_add_without_post(tmp_path, state):
    config, track_id = seed(tmp_path, state=state)
    with spotify_http(tmp_path, snapshot_responses(True, 'after')) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.added == 1 and result.pending == 0
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    assert_membership(config, track_id, True)


def test_verification_budget_exhaustion_retains_acknowledged_intent(tmp_path):
    config, track_id = seed(tmp_path)
    budget = RequestBudget(9, used=5)
    responses = snapshot_responses(False, 'before') + [response({'snapshot_id': 'after'})]
    with spotify_http(tmp_path, responses, budget=budget) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.added == 0 and result.acknowledged == result.pending == result.deferred == 1
        assert budget.used == 9 and len(calls) == 4
        assert sum(call['method'] == 'POST' for call in calls) == 1
    assert work_state(config) == ('add', 'acknowledged', track_id)
    assert_membership(config, track_id, False)
    with spotify_http(tmp_path, snapshot_responses(True, 'after')) as (client, calls), connect(config) as con:
        assert spotify_work.recover_pending(con, config, client).added == 1
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    assert_membership(config, track_id, True)


@pytest.mark.parametrize('crash', [False, True])
def test_acknowledgement_commit_failure_preserves_dispatched_intent(tmp_path, monkeypatch, crash):
    config, track_id = seed(tmp_path)
    original_transaction = spotify_work.transaction

    @contextmanager
    def interrupted_transaction(con):
        with original_transaction(con):
            yield con
            acknowledged = con.execute("SELECT 1 FROM spotify_pending_work WHERE state='acknowledged'").fetchone()
            if acknowledged:
                if crash:
                    con.rollback()
                    raise SystemExit('synthetic process crash')
                raise sqlite3.OperationalError('synthetic commit failure')

    responses = snapshot_responses(False, 'before') + [response({'snapshot_id': 'after'})]
    with monkeypatch.context() as patch:
        patch.setattr(spotify_work, 'transaction', interrupted_transaction)
        with spotify_http(tmp_path, responses) as (client, calls), connect(config) as con:
            if crash:
                with pytest.raises(SystemExit, match='synthetic process crash'):
                    spotify_work.recover_pending(con, config, client)
            else:
                result = spotify_work.recover_pending(con, config, client)
                assert isinstance(result.failure, sqlite3.OperationalError) and result.pending == 1
            assert client.budget.used == len(calls) == 4
    assert work_state(config) == ('add', 'dispatched', track_id)
    assert_membership(config, track_id, False)
    with spotify_http(tmp_path, snapshot_responses(True, 'after')) as (client, calls), connect(config) as con:
        assert spotify_work.recover_pending(con, config, client).added == 1
        assert all(call['method'] == 'GET' for call in calls)
        assert client.budget.used == len(calls) == 3
    assert_membership(config, track_id, True)


def test_failed_compensation_preserves_positive_membership_and_exclusion(tmp_path):
    config, track_id = seed(tmp_path, state='acknowledged', excluded=True)
    with spotify_http(tmp_path, snapshot_responses(True, 'after-add')) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.added == 0 and result.pending == 1
        assert all(call['method'] == 'GET' for call in calls)
        assert client.budget.used == len(calls) == 3
    assert work_state(config) == ('remove', 'ready', track_id)
    # The verified remote addition remains real until compensation is verified.
    assert_membership(config, track_id, True, 'excluded')
    responses = [response(metadata(1, 'after-add')), response({'error': {'message': 'synthetic'}}, status=503)]
    with spotify_http(tmp_path, responses) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.removed == 0 and result.pending == 1 and result.failure is not None
        assert client.budget.used == len(calls) == 2
        assert calls[-1]['method'] == 'DELETE'
    assert_membership(config, track_id, True, 'excluded')
    with connect(config) as con, transaction(con):
        row = con.execute('SELECT id,payload_json FROM spotify_pending_work').fetchone()
        payload = json.loads(row['payload_json'])
        payload['retry_after_verification'] = '2001-01-01T00:00:00Z'
        con.execute('UPDATE spotify_pending_work SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))
    with spotify_http(tmp_path, [*snapshot_responses(True, 'after-add'), response({'snapshot_id': 'after-remove'}),
                                *snapshot_responses(False, 'after-remove')]) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.removed == 1 and result.pending == 0
        assert client.budget.used == len(calls) == 7
    assert_membership(config, track_id, False, 'excluded')


@pytest.mark.parametrize('kind', ['add', 'remove'])
def test_finalization_commit_failure_does_not_report_success(tmp_path, monkeypatch, kind):
    config, track_id = seed(tmp_path, kind=kind, state='acknowledged')
    original_transaction = spotify_work.transaction

    @contextmanager
    def failed_finalization(con):
        with original_transaction(con):
            had_intent = con.execute('SELECT 1 FROM spotify_pending_work').fetchone()
            yield con
            if had_intent and not con.execute('SELECT 1 FROM spotify_pending_work').fetchone():
                raise sqlite3.OperationalError('synthetic finalization commit failure')

    with monkeypatch.context() as patch:
        patch.setattr(spotify_work, 'transaction', failed_finalization)
        with spotify_http(tmp_path, snapshot_responses(kind == 'add', 'after')) as (client, calls), connect(config) as con:
            result = spotify_work.recover_pending(con, config, client)
            assert result.added == result.removed == 0
            assert isinstance(result.failure, sqlite3.OperationalError) and result.pending == 1
            assert client.budget.used == len(calls) == 3
            assert all(call['method'] == 'GET' for call in calls)
    assert work_state(config) == (kind, 'acknowledged', track_id)
    assert_membership(config, track_id, kind == 'remove', 'excluded' if kind == 'remove' else 'wanted')
    with spotify_http(tmp_path, [response(metadata(int(kind == 'add'), 'after'))]) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert (result.added, result.removed, result.pending) == ((1, 0, 0) if kind == 'add' else (0, 1, 0))
        assert client.budget.used == len(calls) == 1
    assert_membership(config, track_id, kind == 'add', 'excluded' if kind == 'remove' else 'wanted')


def test_restarted_intent_cannot_reassign_another_tracks_recording(tmp_path):
    config, track_id = seed(tmp_path, state='acknowledged')
    with connect(config) as con, transaction(con):
        owner = ensure_track(con, artist='Other Artist', title='Other Song', status='wanted')
        owner_id = owner['id']
        upsert_spotify_asset(con, track_id=owner_id, playlist_id='playlist', spotify_track_id='target',
                             spotify_track_uri=CANDIDATE.uri, spotify_artist='Other Artist', spotify_title='Other Song',
                             in_playlist=True, match_confidence=1.0, status='added')
    with spotify_http(tmp_path, []) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.deferred == result.pending == 1 and result.added == 0
        assert client.budget.used == len(calls) == 0
        assert con.execute("SELECT track_id FROM spotify_assets WHERE spotify_track_id='target'").fetchone()[0] == owner_id
        assert con.execute("SELECT track_id FROM spotify_playlist_recordings WHERE spotify_track_id='target'").fetchone()[0] == owner_id
        assert con.execute('SELECT 1 FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone() is None
    assert work_state(config) == ('add', 'acknowledged', track_id)


@pytest.mark.parametrize('status', [403, 404])
def test_failed_recovery_read_preserves_uncertain_write_after_exclusion(tmp_path, status):
    config, track_id = seed(tmp_path, state='dispatched')
    with spotify_http(tmp_path, [response({'error': {'message': 'synthetic'}}, status=status)]) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.pending == 1 and result.added == 0
        assert client.budget.used == len(calls) == 1
        assert calls[0]['method'] == 'GET'
        assert con.execute('SELECT 1 FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone() is None
    assert work_state(config) == ('add', 'dispatched', track_id)
    with connect(config) as con, transaction(con):
        mark_excluded(con, track_id=track_id, source='manual', reason='synthetic exclusion after lost response')
    with spotify_http(tmp_path, snapshot_responses(True, 'after-add')) as (client, calls), connect(config) as con:
        result = spotify_work.recover_pending(con, config, client)
        assert result.added == 0 and result.pending == 1
        assert all(call['method'] == 'GET' for call in calls)
        assert client.budget.used == len(calls) == 3
    assert work_state(config) == ('remove', 'ready', track_id)
    assert_membership(config, track_id, True, 'excluded')


def test_scan_preserves_unavailable_owner_and_valid_positives_without_local_absence(tmp_path):
    from hcr_sync.db import set_state
    from hcr_sync.reconcile import reconcile
    from hcr_sync.spotify_sync import scan_spotify_playlist

    config, track_id = seed(tmp_path, kind='remove')
    config.values['HCR_MUSIC_DIR'] = str(tmp_path / 'music')
    config.values['HCR_RECONCILE_MIN_LOCAL_SCAN_RATIO'] = '0.4'
    config.music_dir.mkdir()
    with connect(config) as con, transaction(con):
        con.execute('DELETE FROM spotify_pending_work')
        con.execute("DELETE FROM exclusions WHERE track_id=?", (track_id,))
        con.execute("UPDATE tracks SET status='wanted' WHERE id=?", (track_id,))
        missing = ensure_track(con, artist='Missing', title='Song')
        missing_id = missing['id']
        upsert_spotify_asset(con, track_id=missing_id, playlist_id='playlist', spotify_track_id='missing',
                             spotify_track_uri='spotify:track:missing', spotify_artist='Missing', spotify_title='Song',
                             in_playlist=True, match_confidence=1.0, status='added', added_at='2001-01-01T00:00:00Z')
        con.execute("UPDATE spotify_playlist_recordings SET suspected_missing_at='2001-01-01T00:00:00Z',last_seen_at='2001-01-01T00:00:00Z',first_seen_at='2001-01-01T00:00:00Z'")
        con.execute("UPDATE spotify_assets SET suspected_missing_at='2001-01-01T00:00:00Z',last_seen_at='2001-01-01T00:00:00Z'")
        set_state(con, 'spotify_baseline_complete', 'true')
        set_state(con, 'local_baseline_complete', 'true')
        set_state(con, 'last_spotify_scan_at', '2002-01-01T00:00:00Z')
        set_state(con, 'last_spotify_playlist_count', '2')
        set_state(con, 'last_spotify_snapshot_id', 'previous')
    entries = [item(id='target', uri=CANDIDATE.uri, name=None, artists=[]),
               item(id='positive', uri='spotify:track:positive', name='New Song'),
               item(id=None, uri='spotify:local:Artist:Album:Local:123', name='Local Song'),
               {'item': None}]
    responses = [response(metadata(4, 'partial-identity')),
                 response({'total': 4, 'offset': 0, 'next': None, 'items': entries}),
                 response(metadata(4, 'partial-identity'))]
    with spotify_http(tmp_path, responses) as (client, calls):
        scanned = scan_spotify_playlist(config, apply=True, client=client)
        assert scanned._snapshot.complete and not scanned._snapshot.identified
        assert {track.track_id for track in scanned._snapshot.tracks} == {'target', 'positive'}
        summary = reconcile(config, apply=True, spotify_client=client, spotify_snapshot=scanned._snapshot,
                            force_confirm_deletions=True)
        assert summary.excluded_spotify == summary.tentative_spotify_removed == summary.suspected_spotify == 0
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    with connect(config) as con:
        unavailable = con.execute("SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id='target'").fetchone()
        assert unavailable['track_id'] == track_id and unavailable['in_playlist'] == 1
        assert unavailable['last_seen_at'] > '2001-01-01T00:00:00Z'
        assert unavailable['spotify_artist'] == 'Artist' and unavailable['spotify_title'] == 'Song'
        assert unavailable['suspected_missing_at'] is None
        missing_row = con.execute("SELECT * FROM spotify_playlist_recordings WHERE spotify_track_id='missing'").fetchone()
        assert missing_row['track_id'] == missing_id and missing_row['in_playlist'] == 1
        assert missing_row['suspected_missing_at'] == '2001-01-01T00:00:00Z'
        assert con.execute("SELECT count(*) FROM spotify_playlist_recordings WHERE spotify_track_id='positive' AND in_playlist=1").fetchone()[0] == 1
        assert con.execute("SELECT value FROM sync_state WHERE key='last_spotify_playlist_count'").fetchone()[0] == '2'
        assert con.execute("SELECT value FROM sync_state WHERE key='last_spotify_snapshot_id'").fetchone()[0] == 'previous'
        assert con.execute("SELECT value FROM sync_state WHERE key='last_spotify_scan_at'").fetchone()[0] == '2002-01-01T00:00:00Z'


def test_identified_unavailable_recordings_count_their_existing_logical_owners(tmp_path):
    from hcr_sync.reconcile import _spotify_snapshot_logical_count
    from hcr_sync.spotify_sync import scan_spotify_playlist

    config, track_id = seed(tmp_path, kind='remove')
    with connect(config) as con, transaction(con):
        con.execute('DELETE FROM spotify_pending_work')
        other = ensure_track(con, artist='Other', title='Song')
        upsert_spotify_asset(con, track_id=other['id'], playlist_id='playlist', spotify_track_id='other',
                             spotify_track_uri='spotify:track:other', spotify_artist='Other', spotify_title='Song',
                             in_playlist=True, match_confidence=1.0, status='added')
    entries = [item(id=identity, uri=f'spotify:track:{identity}', name=None, artists=[])
               for identity in ['target', 'other']]
    responses = [response(metadata(2)), response({'total': 2, 'offset': 0, 'next': None, 'items': entries}),
                 response(metadata(2))]
    with spotify_http(tmp_path, responses) as (client, calls):
        scanned = scan_spotify_playlist(config, apply=True, client=client)
        assert scanned._snapshot.identified
        with connect(config) as con:
            assert _spotify_snapshot_logical_count(con, scanned._snapshot) == 2
            assert con.execute("SELECT track_id FROM spotify_playlist_recordings WHERE spotify_track_id='target'").fetchone()[0] == track_id
        assert client.budget.used == len(calls) == 3


@pytest.mark.parametrize('importer_name', ['scan_spotify_playlist', 'backfill_spotify'])
def test_membership_import_stops_when_pending_owner_finalization_fails(tmp_path, monkeypatch, importer_name):
    from hcr_sync import spotify_sync

    config, track_id = seed(tmp_path, state='acknowledged')
    original_transaction = spotify_work.transaction

    @contextmanager
    def failed_finalization(con):
        with original_transaction(con):
            had_intent = con.execute('SELECT 1 FROM spotify_pending_work').fetchone()
            yield con
            if had_intent and not con.execute('SELECT 1 FROM spotify_pending_work').fetchone():
                raise sqlite3.OperationalError('synthetic owner finalization failure')

    monkeypatch.setattr(spotify_work, 'transaction', failed_finalization)
    with spotify_http(tmp_path, snapshot_responses(True, 'after')) as (client, calls):
        with pytest.raises(sqlite3.OperationalError, match='synthetic owner finalization failure'):
            getattr(spotify_sync, importer_name)(config, apply=True, client=client)
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    assert work_state(config) == ('add', 'acknowledged', track_id)
    assert_membership(config, track_id, False)
    with connect(config) as con:
        assert con.execute("SELECT count(*) FROM events WHERE event_type IN ('spotify_added','spotify_playlist_seen')").fetchone()[0] == 0
        assert con.execute("SELECT value FROM sync_state WHERE key='spotify_baseline_complete'").fetchone() is None


@pytest.mark.parametrize('kind', ['add', 'remove'])
def test_verified_non_effect_keeps_failed_write_backoff_and_allows_other_sources(tmp_path, kind):
    from datetime import datetime, timedelta, timezone
    from hcr_sync.spotify_sync import sync_spotify

    config, track_id = seed(tmp_path, kind=kind)
    before = kind == 'remove'
    responses = snapshot_responses(before, 'before') + [response({'error': {'message': 'synthetic'}}, status=503)]
    with spotify_http(tmp_path, responses) as (client, calls), connect(config) as con:
        failed = spotify_work.recover_pending(con, config, client)
        assert failed.failure is not None and failed.pending == 1
        assert client.budget.used == len(calls) == 4
    assert work_state(config) == (kind, 'dispatched', track_id)
    with connect(config) as con, transaction(con):
        other = ensure_track(con, artist='Other Artist', title='Other Song')
        other_id = other['id']
        payload = json.loads(con.execute('SELECT payload_json FROM spotify_pending_work').fetchone()[0])
        retry_at = payload['retry_after_verification']
        assert datetime.fromisoformat(retry_at.replace('Z', '+00:00')) > datetime.now(timezone.utc) + timedelta(days=6)
    responses = snapshot_responses(before, 'verified-non-effect') + [response({'tracks': {'items': []}})] * 2
    with spotify_http(tmp_path, responses) as (client, calls):
        continued = sync_spotify(config, apply=True, client=client)
        assert continued.first_time == 1 and continued.added == 0 and continued.pending == 1
        assert client.budget.used == len(calls) == 5
        assert all(call['method'] == 'GET' for call in calls)
        assert sum(call['path'].startswith('/search?') for call in calls) == 2
    assert work_state(config) == (kind, 'ready', track_id)
    assert_membership(config, track_id, before, 'excluded' if before else 'wanted')
    with connect(config) as con:
        payload = json.loads(con.execute('SELECT payload_json FROM spotify_pending_work').fetchone()[0])
        assert payload['retry_at'] == retry_at
        assert con.execute('SELECT search_attempts FROM spotify_assets WHERE track_id=?', (other_id,)).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM events WHERE event_type='spotify_search_completed' AND track_id=?", (other_id,)).fetchone()[0] == 1
    # A later pass before the due time performs no scan or duplicate write.
    with spotify_http(tmp_path, []) as (client, calls), connect(config) as con:
        waiting = spotify_work.recover_pending(con, config, client)
        assert waiting.pending == 1 and waiting.added == waiting.removed == 0
        assert client.budget.used == len(calls) == 0
    with connect(config) as con, transaction(con):
        row = con.execute('SELECT id,payload_json FROM spotify_pending_work').fetchone()
        payload = json.loads(row['payload_json'])
        payload['retry_at'] = payload['retry_after_verification'] = '2001-01-01T00:00:00Z'
        con.execute('UPDATE spotify_pending_work SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))
        con.execute("UPDATE spotify_assets SET search_next_at='2001-01-01T00:00:00Z' WHERE track_id=?", (track_id,))
    responses = [response(metadata(int(before), 'verified-non-effect')), response({'snapshot_id': 'retried'})]
    responses += snapshot_responses(not before, 'retried')
    with spotify_http(tmp_path, responses) as (client, calls), connect(config) as con:
        retried = spotify_work.recover_pending(con, config, client)
        assert (retried.added, retried.removed, retried.pending) == ((1, 0, 0) if kind == 'add' else (0, 1, 0))
        assert client.budget.used == len(calls) == 5
        assert sum(call['method'] == ('POST' if kind == 'add' else 'DELETE') for call in calls) == 1
    assert_membership(config, track_id, not before, 'excluded' if before else 'wanted')


def test_uncertain_addition_after_rejected_attempt_preserves_fourteen_day_schedule(tmp_path):
    from datetime import datetime, timedelta, timezone

    config, track_id = seed(tmp_path)
    for status in [400, 503]:
        responses = snapshot_responses(False, f'before-{status}') + [response({'error': {'message': 'synthetic'}}, status=status)]
        with spotify_http(tmp_path, responses) as (client, calls), connect(config) as con:
            failed = spotify_work.recover_pending(con, config, client)
            assert failed.pending == 1 and failed.added == 0
            assert client.budget.used == len(calls) == 4
            assert calls[-1]['method'] == 'POST'
        if status == 400:
            assert work_state(config) == ('add', 'ready', track_id)
            with connect(config) as con, transaction(con):
                row = con.execute('SELECT id,payload_json FROM spotify_pending_work').fetchone()
                payload = json.loads(row['payload_json'])
                payload['retry_at'] = '2001-01-01T00:00:00Z'
                con.execute('UPDATE spotify_pending_work SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))
                con.execute("UPDATE spotify_assets SET search_next_at='2001-01-01T00:00:00Z' WHERE track_id=?", (track_id,))
    assert work_state(config) == ('add', 'dispatched', track_id)
    with connect(config) as con:
        asset = con.execute('SELECT search_attempts,search_next_at FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone()
        assert asset['search_attempts'] == 2
        payload = json.loads(con.execute('SELECT payload_json FROM spotify_pending_work').fetchone()[0])
        assert datetime.fromisoformat(payload['retry_after_verification'].replace('Z', '+00:00')) > datetime.now(timezone.utc) + timedelta(days=13)
    with spotify_http(tmp_path, snapshot_responses(False, 'proved-non-effect')) as (client, calls), connect(config) as con:
        verified = spotify_work.recover_pending(con, config, client)
        assert verified.added == 0 and verified.pending == verified.deferred == 1
        assert client.budget.used == len(calls) == 3
        assert all(call['method'] == 'GET' for call in calls)
    with connect(config) as con:
        payload = json.loads(con.execute('SELECT payload_json FROM spotify_pending_work').fetchone()[0])
        assert datetime.fromisoformat(payload['retry_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc) + timedelta(days=13)
