"""Acceptance regressions for Spotify-only comparison normalization."""
from dataclasses import asdict
import json

import pytest

from hcr_sync.db import add_event, connect, ensure_track, get_state, mark_excluded, set_state, transaction, upsert_spotify_asset
from hcr_sync.spotify_adapter import SpotifyTrack
from hcr_sync.spotify_matching import comparison_title
from hcr_sync.spotify_sync import _spotify_match_score, _spotify_search_queries, sync_spotify
from hcr_sync.spotify_work import save_work
from test_spotify_budget_rotation import config_for, seed
from test_spotify_reconcile_youtube import FakeSpotify


def candidate(title, artist="Synthetic Artist", *, credits=(), recording="synthetic"):
    return SpotifyTrack(f"spotify:track:{recording}", recording, artist, title, artist_names=credits)


def score(source_title, target_title, source_artist="Synthetic Artist", target_artist="Synthetic Artist", credits=()):
    return _spotify_match_score({"display_artist": source_artist, "display_title": source_title},
                                candidate(target_title, target_artist, credits=credits))


@pytest.mark.parametrize("suffix", ["(Original Mix)", "[Original Version]", "(Extended)",
    "(Extended Mix)", "[Extended Version]", "(Radio Edit)", "[Radio Mix]", " - Radio Version",
    " - Original Mix", " - Extended Mix", "(Extended Mix) (Extended Mix)",
    "(Radio Edit) - Original Mix", "[Extended] (Original Mix)"])
def test_generic_suffixes_are_symmetric_stable_and_confident(suffix):
    source = f"Night Signal {suffix}"
    normalized = comparison_title(source)
    assert normalized == comparison_title(normalized.text)
    assert normalized.versions == ()
    assert score(source, "Night Signal") == score("Night Signal", source) == 1.0


@pytest.mark.parametrize("title", ["Radio Silence", "Original Thought", "Mix Your Memories", "Edit My Heart",
                                  "Extended Horizons", "Night Signal (Uncharted Territory)"])
def test_real_title_words_and_unknown_qualifiers_are_retained(title):
    assert score(title, title) == 1.0
    assert comparison_title(title) == comparison_title(comparison_title(title).text)
    assert comparison_title(title).text
    if "(" in title:
        assert "uncharted" in comparison_title(title).text
        assert score(title, "Night Signal") < .85


@pytest.mark.parametrize("source,target", [
    ("Night Signal (2026 Edit) (Original Mix)", "Night Signal (2026 Mix)"),
    ("Night Signal (Synthetic Remixer Remix Edit)", "Night Signal - Synthetic Remixer Remix"),
    ("Night Signal (Hard Refix) (Original Mix)", "Night Signal - Hard Refix"),
])
def test_equivalent_meaningful_versions_pass_both_directions(source, target):
    assert score(source, target) == score(target, source) == 1.0
    assert comparison_title(source).versions
    assert comparison_title(source) == comparison_title(comparison_title(source).text)


LONG_TITLE = "Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless Distant Stars"
@pytest.mark.parametrize("source_version,target_version", [
    ("(Alpha Remix)", "(Beta Remix)"), ("(2026 Edit)", "(2025 Mix)"),
    ("(Hard Refix)", ""), ("", "(Hard Refix)"),
    ("(Live)", ""), ("", "(Live)"), ("(Acoustic)", ""), ("", "(Cover)"),
    ("(Live)", "(Acoustic)"), ("(Alpha Remix)", ""), ("", "(Alpha Remix)"),
    ("Alpha Remix", "Beta Remix"), ("Hard Refix", ""),
    ("Live", ""), ("Acoustic", ""), ("Cover", ""),
    ("2026 Edit", "2025 Mix"),
])
def test_meaningful_version_conflicts_cannot_pass_long_shared_title(tmp_path, source_version, target_version):
    source, target = f"{LONG_TITLE} {source_version}", f"{LONG_TITLE} {target_version}"
    assert score(source, target) < .85
    config = config_for(tmp_path)
    source_id = seed(config, "Synthetic Artist", source)
    fake = FakeSpotify(search_tracks=[candidate(target)])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.added == summary.tentative_added == 0
    assert fake.added == []
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
        assert asset["in_playlist"] == 0 and asset["status"] == "review"
        assert con.execute("SELECT count(*) FROM spotify_pending_work WHERE kind='add'").fetchone()[0] == 0


@pytest.mark.parametrize("source,target,credits,accepted", [
    ("Synthetic Artist & MC Signal", "Synthetic Artist, MC Signal", ("Synthetic Artist", "MC Signal"), True),
    ("Synthetic Artist, MC Signal", "Synthetic Artist & MC Signal", ("Synthetic Artist", "MC Signal"), True),
    ("Synthetic Artist, MC Signal", "Synthetic Artist & MC Signal", (), False),
    ("Synthetic Artist & MC Signal", "Synthetic Artist", ("Synthetic Artist",), False),
    ("Synthetic Artist & Collaborator", "Synthetic Artist, Somebody Else", ("Synthetic Artist", "Somebody Else"), False),
    ("Synthetic Artist", "Unrelated Producer", ("Unrelated Producer",), False),
    ("Signal & Noise", "Signal & Noise", ("Signal & Noise",), True),
    ("Signal", "Signal & Noise", ("Signal & Noise",), False),
    ("Signal, Noise & Rhythm", "Signal, Noise & Rhythm", ("Signal, Noise & Rhythm",), True),
    ("Signal", "Signal, Noise & Rhythm", ("Signal, Noise & Rhythm",), False),
    ("Synthetic Artist and MC Signal", "Synthetic Artist, MC Signal", ("Synthetic Artist", "MC Signal"), False),
    ("Synthetic Artist x MC Signal", "Synthetic Artist, MC Signal", ("Synthetic Artist", "MC Signal"), False),
])
def test_artist_credit_punctuation_does_not_erase_people_or_compound_names(source, target, credits, accepted):
    actual = score("Night Signal", "Night Signal", source, target, credits)
    assert (actual >= .85) is accepted
    if accepted:
        assert actual == 1.0


@pytest.mark.parametrize("source,target,expected,tentative", [
    ("Silver Midnight Signals Drift", "Silver Midnight Signals", .8625, True),
    (LONG_TITLE, "Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless", .9, False),
    ("Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless Distant", "Silver Midnight Signals Drift Across Frozen Valleys Beneath", .89, True),
    ("Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless Distant Stars Echoes", "Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless Distant", .9083333333333333, False),
])
def test_production_scores_route_automatic_tentative_and_confident_additions(tmp_path, source, target, expected, tentative):
    assert score(source, target) == pytest.approx(expected)
    config = config_for(tmp_path)
    source_id = seed(config, "Synthetic Artist", source)
    fake = FakeSpotify(search_tracks=[candidate(target)])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.added == 1 and summary.tentative_added == int(tentative)
    assert fake.added == ["spotify:track:synthetic"]
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
        assert asset["in_playlist"] == 1
        assert asset["status"] == ("review" if tentative else "added")
        assert asset["match_confidence"] == pytest.approx(expected)
        assert con.execute("SELECT count(*) FROM spotify_pending_work").fetchone()[0] == 0


@pytest.mark.parametrize("hold", [None, "future", "user_removed", "excluded"])
def test_low_confidence_retry_uses_new_matcher_only_when_authorized_and_due(tmp_path, hold):
    config = config_for(tmp_path)
    source_id = seed(config, "Synthetic Artist", "Night Signal (Original Mix)", retry=True)
    with connect(config) as con, transaction(con):
        if hold == "future":
            con.execute("UPDATE spotify_assets SET search_next_at='2099-01-01T00:00:00Z' WHERE track_id=?", (source_id,))
        elif hold == "user_removed":
            add_event(con, source_id, "spotify_tentative_removed_by_user", "synthetic", {})
        elif hold == "excluded":
            mark_excluded(con, track_id=source_id, source="manual", reason="synthetic explicit rejection")
    fake = FakeSpotify(search_tracks=[candidate("Night Signal")])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.added == int(hold is None)
    assert bool(fake.search_calls) is (hold is None)
    with connect(config) as con:
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
        if hold:
            assert asset["search_last_at"] == "2000-01-01T00:00:00Z"
            assert asset["search_attempts"] == 1
            assert not asset["in_playlist"]
        else:
            assert asset["in_playlist"] and asset["search_attempts"] == 0
            assert summary.retries == 1 and summary.first_time == 0


def test_saved_partial_candidate_pool_is_rescored_on_restart_in_original_lane(tmp_path):
    config = config_for(tmp_path, HCR_SPOTIFY_SYNC_LIMIT="1")
    source_id = seed(config, "Synthetic Artist", "Night Signal (Original Mix)", retry=True)
    seed(config, "Other Producer", "Fresh Arrival")
    pool = asdict(candidate("Night Signal"))
    pool.pop("artist_names")  # A pre-normalization payload remains readable.
    source = ["Synthetic Artist", "Night Signal (Original Mix)"]
    with connect(config) as con, transaction(con):
        set_state(con, "spotify_rotation:playlist", "3")
        save_work(con, "playlist", source_id, "search", {"source": source, "lane": "retry",
            "next_query": len(_spotify_search_queries(*source)), "candidates": [pool]})
    class ResumedSpotify(FakeSpotify):
        search_queries = staticmethod(_spotify_search_queries)
        def search_query(self, query):
            pytest.fail("Completed variants must not be requested again")
    fake = ResumedSpotify(search_tracks=[candidate("Night Signal")])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.retries == summary.added == 1 and summary.first_time == 0
    with connect(config) as con:
        assert get_state(con, "spotify_rotation:playlist") == "0"
        asset = con.execute("SELECT * FROM spotify_assets WHERE track_id=?", (source_id,)).fetchone()
        assert asset["in_playlist"] and asset["match_confidence"] == 1.0
        assert con.execute("SELECT count(*) FROM spotify_pending_work WHERE track_id=?", (source_id,)).fetchone()[0] == 0


def test_two_source_versions_cannot_add_one_shared_recording_twice_or_transfer_ownership(tmp_path):
    config = config_for(tmp_path)
    owner = seed(config, "Synthetic Producer", "Night Signal (Radio Edit)")
    second = seed(config, "Synthetic Producer", "Night Signal (Original Mix)")
    shared = candidate("Night Signal", "Synthetic Producer", recording="shared")
    assert score("Night Signal (Radio Edit)", shared.title, "Synthetic Producer", shared.artist) == 1.0
    assert score("Night Signal (Original Mix)", shared.title, "Synthetic Producer", shared.artist) == 1.0
    fake = FakeSpotify(search_tracks=[shared])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.added == 1 and fake.added == [shared.uri]
    with connect(config) as con:
        assert con.execute("SELECT track_id FROM spotify_playlist_recordings WHERE spotify_track_id='shared'").fetchone()[0] == owner
        assert con.execute("SELECT in_playlist FROM spotify_assets WHERE track_id=?", (second,)).fetchone()[0] == 0
        event = con.execute("SELECT payload_json FROM events WHERE track_id=? AND event_type='spotify_candidate_already_linked'", (second,)).fetchone()
        assert event and json.loads(event[0])["existing_track_id"] == owner


@pytest.mark.parametrize("threshold_delta,added", [(0.0, True), (0.000001, False)])
def test_tentative_boundary_uses_real_score_with_inclusive_threshold(tmp_path, threshold_delta, added):
    source, target = "Silver Midnight Signals Drift", "Silver Midnight Signals"
    actual = score(source, target)
    assert actual == pytest.approx(.8625)
    config = config_for(tmp_path, HCR_SPOTIFY_TENTATIVE_ADD_THRESHOLD=str(actual + threshold_delta))
    seed(config, "Synthetic Artist", source)
    fake = FakeSpotify(search_tracks=[candidate(target)])
    summary = sync_spotify(config, apply=True, client=fake)
    assert summary.added == summary.tentative_added == int(added)
    assert bool(fake.added) is added


def test_below_tentative_threshold_does_not_create_addition(tmp_path):
    source, target = "Silver Midnight Signals Drift Across", "Silver Midnight Signals"
    assert score(source, target) < .85
    config = config_for(tmp_path)
    seed(config, "Synthetic Artist", source)
    fake = FakeSpotify(search_tracks=[candidate(target)])
    assert sync_spotify(config, apply=True, client=fake).added == 0
    assert fake.added == []
    with connect(config) as con:
        assert con.execute("SELECT count(*) FROM spotify_pending_work WHERE kind='add'").fetchone()[0] == 0
