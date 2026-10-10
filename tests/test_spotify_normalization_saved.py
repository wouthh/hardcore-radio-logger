"""Sanitized saved rejects must progress through the real adapter and scheduler."""
import json
from pathlib import Path

import pytest

from hcr_sync.db import connect, ensure_track, transaction, upsert_spotify_asset
from hcr_sync.spotify_adapter import SpotifyTrack, SpotipyClient
from hcr_sync.spotify_sync import _spotify_match_score, sync_spotify
from test_spotify_adapter import item, metadata, response, spotify_http
from test_spotify_budget_rotation import config_for

CASES = json.loads((Path(__file__).parent / 'fixtures/spotify_normalization.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: f"saved-{case['case']}")
def test_saved_rejection_automatically_retries_and_verifies_with_real_adapter(tmp_path, case):
    config = config_for(tmp_path)
    with connect(config) as con, transaction(con):
        source = ensure_track(con, artist=case['source_artist'], title=case['source_title'])
        track_id = source['id']
        upsert_spotify_asset(con, track_id=track_id, playlist_id='playlist', in_playlist=False,
            match_confidence=case['old_score'], status='review', search_attempts=1,
            search_last_at='2000-01-01T00:00:00Z', search_next_at='2000-01-08T00:00:00Z', update_search=True)
    recording = item(id=case['recording_id'], uri='spotify:track:' + case['recording_id'],
        name=case['candidate_title'], artists=[{'id': f'artist{i}', 'name': name.strip()}
            for i, name in enumerate(case['candidate_artist'].split(','))])
    queries = SpotipyClient.search_queries(case['source_artist'], case['source_title'])
    responses = [response({'tracks': {'items': [recording['item']]}}) for _ in queries]
    responses += [response(metadata(0, 'before')),
        response({'total': 0, 'offset': 0, 'next': None, 'items': []}),
        response(metadata(0, 'before')), response({'snapshot_id': 'after'}),
        response(metadata(1, 'after')),
        response({'total': 1, 'offset': 0, 'next': None, 'items': [recording]}),
        response(metadata(1, 'after'))]
    with spotify_http(tmp_path, responses) as (client, calls):
        summary = sync_spotify(config, apply=True, client=client)
        assert summary.retries == 1 and summary.first_time == 0
        assert summary.added == 1 and summary.tentative_added == 0
        assert summary.requests == len(calls) == len(queries) + 7 <= 20
        assert [call['body'] for call in calls if call['method'] == 'POST'] == [
            {'uris': ['spotify:track:' + case['recording_id']]}]
        assert responses == []
    with connect(config) as con:
        asset = con.execute('SELECT * FROM spotify_assets WHERE track_id=?', (track_id,)).fetchone()
        assert asset['in_playlist'] == 1 and asset['status'] == 'added'
        assert asset['match_confidence'] == 1.0
        assert asset['spotify_artist'] == case['candidate_artist']
        assert asset['spotify_title'] == case['candidate_title']
        assert con.execute('SELECT count(*) FROM spotify_pending_work').fetchone()[0] == 0
        event = con.execute("SELECT payload_json FROM events WHERE event_type='spotify_added'").fetchone()
        assert json.loads(event[0])['verified'] is True


def test_saved_cases_cover_duplicate_identity_without_production_identifiers():
    assert len(CASES) == 50
    ids = [case['recording_id'] for case in CASES]
    assert len(set(ids)) == 49
    for case in CASES:
        candidate = SpotifyTrack('spotify:track:' + case['recording_id'], case['recording_id'],
            case['candidate_artist'], case['candidate_title'])
        assert case['old_score'] < .85
        assert _spotify_match_score({'display_artist': case['source_artist'],
            'display_title': case['source_title']}, candidate) == 1.0
