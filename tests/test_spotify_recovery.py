from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread

import pytest

from hcr_sync.config import Config, DEFAULTS
from hcr_sync.db import connect, ensure_track, init_db, set_state, transaction, upsert_spotify_asset
from hcr_sync.poller import PollSourceUnavailable, current_track, track_from_player_page
from hcr_sync.reconcile import _cascade_spotify, ReconcileSummary, reconcile
from hcr_sync.spotify_sync import (
    SpotipyClient, SpotifyTrack, _is_rate_limited, backfill_spotify,
    scan_spotify_playlist, sync_spotify,
)
from hcr_sync.youtube_sync import sync_youtube


def config_for(tmp_path, **overrides):
    values = dict(DEFAULTS)
    values.update(HCR_DB_PATH=str(tmp_path / 'db.sqlite'), HCR_MUSIC_DIR=str(tmp_path / 'music'),
                  HCR_SPOTIFY_PLAYLIST_ID='playlist', HCR_SPOTIFY_TOKEN_CACHE=str(tmp_path / 'cache'))
    values.update(overrides)
    config = Config(values)
    init_db(config)
    config.music_dir.mkdir()
    return config


class EchoSpotify:
    def __init__(self, operation='', status=429):
        self.operation, self.status = operation, status
        self.search_calls, self.added = [], []

    def fail(self):
        exc = RuntimeError('synthetic failure')
        exc.http_status = self.status
        exc.headers = {'Retry-After': '82375'}
        exc.reason = 'QUOTA_EXCEEDED' if self.status == 429 else None
        raise exc

    def search_track(self, artist, title):
        self.search_calls.append((artist, title))
        if artist == 'First' and self.operation == 'search':
            self.fail()
        return [SpotifyTrack(uri='spotify:track:' + artist, track_id=artist, artist=artist, title=title)]

    def add_tracks(self, playlist, uris):
        if uris == ['spotify:track:First'] and self.operation == 'add':
            self.fail()
        self.added.extend(uris)


@pytest.mark.parametrize('operation', ['search', 'add'])
@pytest.mark.parametrize('status', [400, 404, 429, 401, 503])
def test_failed_attempts_rotate_without_losing_provenance(tmp_path, operation, status):
    config = config_for(tmp_path)
    with connect(config) as con:
        with transaction(con):
            first = ensure_track(con, artist='First', title='Song')
            ensure_track(con, artist='Second', title='Song')
            upsert_spotify_asset(con, track_id=first['id'], playlist_id='playlist',
                                 spotify_track_id='stored', spotify_track_uri='spotify:track:stored',
                                 spotify_artist='Stored Artist', spotify_title='Stored Title',
                                 in_playlist=False, match_confidence=0.86, status='review')
    failing = EchoSpotify(operation, status)
    if status in (401, 503):
        with pytest.raises(RuntimeError):
            sync_spotify(config, apply=True, client=failing)
    else:
        summary = sync_spotify(config, apply=True, client=failing)
        assert summary.rate_limited == (status == 429)
        if status in (400, 404):
            assert failing.added == ['spotify:track:Second']
    with connect(config) as con:
        asset = con.execute('SELECT * FROM spotify_assets WHERE track_id=?', (first['id'],)).fetchone()
        assert (asset['spotify_track_id'], asset['spotify_artist'], asset['match_confidence'], asset['status']) == ('stored', 'Stored Artist', 0.86, 'review')
        assert asset['search_last_at'] and asset['search_next_at']
        assert asset['search_attempts'] == (0 if status == 429 else 1)
        if status == 429:
            payload = json.loads(con.execute("SELECT value FROM sync_state WHERE key='spotify_rate_limit_last_response'").fetchone()[0])
            assert payload['retry_after_seconds'] == 82375 and payload['reason'] == 'QUOTA_EXCEEDED'
        set_state(con, 'spotify_rate_limited_until', '2000-01-01T00:00:00Z')
        con.execute("UPDATE spotify_assets SET search_next_at='2000-01-01T00:00:00Z' WHERE track_id=?", (first['id'],))
        con.commit()
    healthy = EchoSpotify()
    sync_spotify(config, apply=True, client=healthy)
    assert healthy.search_calls[0] == (('First', 'Song') if status in (400, 404) else ('Second', 'Song'))


@pytest.mark.parametrize('operation', ['search', 'add'])
def test_rate_limited_dry_run_does_not_record_attempt(tmp_path, operation):
    config = config_for(tmp_path)
    with connect(config) as con:
        ensure_track(con, artist='First', title='Song'); con.commit()
        before = list(con.iterdump())
    assert sync_spotify(config, apply=False, client=EchoSpotify(operation)).rate_limited == (operation == 'search')
    with connect(config) as con:
        assert list(con.iterdump()) == before


@pytest.mark.parametrize('command', [backfill_spotify, scan_spotify_playlist, sync_spotify, reconcile])
@pytest.mark.parametrize('apply', [True, False])
def test_cooldown_prevents_even_client_creation(tmp_path, monkeypatch, command, apply):
    config = config_for(tmp_path)
    with connect(config) as con:
        set_state(con, 'spotify_rate_limited_until', '2099-01-01T00:00:00Z')
    def unexpected(*args, **kwargs):
        pytest.fail('client constructed during cooldown')
    monkeypatch.setattr('hcr_sync.spotify_sync.SpotipyClient', unexpected)
    monkeypatch.setattr('hcr_sync.reconcile.SpotipyClient', unexpected)
    command(config, apply=apply)
    with connect(config) as con:
        assert con.execute("SELECT value FROM sync_state WHERE key='spotify_rate_limited_until'").fetchone()[0] == '2099-01-01T00:00:00Z'


@pytest.mark.parametrize('status', [429, 503])
def test_real_spotipy_transport_preserves_response(tmp_path, status):
    pytest.importorskip('spotipy')
    class Handler(BaseHTTPRequestHandler):
        calls = 0
        def log_message(self, *args):
            pass
        def do_GET(self):
            Handler.calls += 1
            self.send_response(status)
            self.send_header('Retry-After', '82375')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"synthetic","reason":"QUOTA_EXCEEDED"}}')
    config = config_for(tmp_path)
    config.values.update(HCR_SPOTIFY_CLIENT_ID='synthetic', HCR_SPOTIFY_CLIENT_SECRET='synthetic')
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    client = SpotipyClient(config)
    client.sp.auth_manager = None
    client.sp._auth = 'synthetic'
    client.sp.prefix = f'http://127.0.0.1:{server.server_port}/'
    try:
        with pytest.raises(Exception) as caught:
            client.search_track('Artist', 'Song')
        assert caught.value.http_status == status
        assert caught.value.headers['Retry-After'] == '82375'
        assert _is_rate_limited(caught.value) == (status == 429)
        assert Handler.calls == 1
    finally:
        client.sp._session.close()
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('artist,title', [('tijdelijk niet beschikbaar', 'Nummerinformatie'), ('  TIJDELIJK\nNIET BESCHIKBAAR ', ' Nummerinformatie ')])
def test_placeholder_cannot_be_polled_or_searched(tmp_path, artist, title):
    html = f'<div class="track"><span class="artist">{artist}</span><span class="title">{title}</span></div>'
    with pytest.raises(PollSourceUnavailable, match='placeholder_track_metadata'):
        track_from_player_page(html)
    with pytest.raises(PollSourceUnavailable, match='placeholder_track_metadata'):
        current_track({'icestats': {'source': {'artist': artist, 'title': title}}}, 'https://radio.test/stream')
    config = config_for(tmp_path)
    with connect(config) as con:
        ensure_track(con, artist=artist, title=title); con.commit()
    fake = EchoSpotify()
    assert sync_spotify(config, apply=True, client=fake).review == 1
    assert fake.search_calls == []
    assert sync_youtube(config, apply=False, client=fake).review == 1


def test_removal_rate_limit_defers_membership_and_subsequent_calls(tmp_path):
    config = config_for(tmp_path)
    class FailingRemoval(EchoSpotify):
        calls = 0
        def remove_tracks(self, playlist, uris):
            self.calls += 1; self.fail()
    client = FailingRemoval()
    with connect(config) as con:
        with transaction(con):
            track = ensure_track(con, artist='Artist', title='Song')
            upsert_spotify_asset(con, track_id=track['id'], playlist_id='playlist',
                                 spotify_track_id='stored', spotify_track_uri='spotify:track:stored',
                                 in_playlist=True, match_confidence=1.0, status='added')
            summary = ReconcileSummary()
            _cascade_spotify(con, config, track['id'], summary, client)
            _cascade_spotify(con, config, track['id'], summary, client)
        assert con.execute('SELECT in_playlist FROM spotify_assets').fetchone()[0] == 1
        assert con.execute('SELECT in_playlist FROM spotify_playlist_recordings').fetchone()[0] == 1
    assert client.calls == 1 and summary.spotify_removed == 0


@pytest.mark.parametrize('operation', ['search', 'add'])
def test_first_rate_limited_attempt_creates_schedule_and_rotates(tmp_path, operation):
    config = config_for(tmp_path)
    with connect(config) as con:
        ensure_track(con, artist='First', title='Song')
        ensure_track(con, artist='Second', title='Song'); con.commit()
    assert sync_spotify(config, apply=True, client=EchoSpotify(operation)).rate_limited
    with connect(config) as con:
        asset = con.execute('SELECT * FROM spotify_assets').fetchone()
        assert asset['search_last_at'] and asset['search_attempts'] == 0
        set_state(con, 'spotify_rate_limited_until', '2000-01-01T00:00:00Z')
        con.execute("UPDATE spotify_assets SET search_next_at='2000-01-01T00:00:00Z'"); con.commit()
    healthy = EchoSpotify()
    sync_spotify(config, apply=True, client=healthy)
    assert healthy.search_calls == [('Second', 'Song'), ('First', 'Song')]


def test_due_retries_use_oldest_attempt_not_oldest_due_date(tmp_path):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT='1')
    with connect(config) as con:
        for artist, last, due in [('First', '2000-01-01T00:00:00Z', '2000-02-02T00:00:00Z'),
                                  ('Second', '2000-02-01T00:00:00Z', '2000-02-01T00:00:00Z')]:
            track = ensure_track(con, artist=artist, title='Song')
            upsert_spotify_asset(con, track_id=track['id'], playlist_id='playlist', in_playlist=False,
                                 match_confidence=None, status='error', search_last_at=last,
                                 search_next_at=due, search_attempts=1, update_search=True)
        con.commit()
    client = EchoSpotify()
    sync_spotify(config, apply=True, client=client)
    assert client.search_calls == [('First', 'Song')]
