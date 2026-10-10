"""Recording acceptance requires title versions and actual credited artists."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from hcr_sync.identity import match_confidence, parse_artist_title
from hcr_sync.youtube_matching import compare_recordings, evaluate_candidate


def video(title, **kwargs):
    return SimpleNamespace(**dict(dict(title=title, channel='Upload Channel', duration=180,
        is_live=False, description='', artist_names=(), artist='', track=''), **kwargs))


@pytest.mark.parametrize('suffix', ['(Original Mix)', '[Extended Mix]', '- Radio Edit',
    '(Original Version) (Extended Mix)', '(Official Audio)', '[Official Video]',
    '(Radio Edit) [Official Audio]', '[Official Audio] (Original Mix)', '(Lyrics)', '[HQ]',
    '- Official Audio', '- Official Video (Original Mix)'])
def test_generic_and_presentation_labels_pass_production_acceptance(suffix):
    result = evaluate_candidate('Synthetic Artist', f'Night Signal {suffix}',
                                video('Synthetic Artist - Night Signal'))
    assert result.accepted and result.score == 1.0
    reverse = evaluate_candidate('Synthetic Artist', 'Night Signal',
                                  video(f'Synthetic Artist - Night Signal {suffix}'))
    assert reverse.accepted and reverse.score == 1.0
    assert asdict(result)['source_title']['base'] == 'night signal'


@pytest.mark.parametrize('source,target', [
    ('Night Signal (2026 Edit)', 'Night Signal (2026 Mix)'),
    ('Night Signal (Alpha Remix Edit)', 'Night Signal - Alpha Remix'),
    ('Radio Silence', 'Radio Silence'), ('Original Thought', 'Original Thought'),
    ('Mix Your Memories', 'Mix Your Memories'), ('Edit My Heart', 'Edit My Heart'),
    ('Night Signal (Hard Refix)', 'Night Signal - Hard Refix'),
])
def test_equivalent_versions_and_real_title_words_accept(source, target):
    result = evaluate_candidate('Synthetic Artist', source, video(f'Synthetic Artist - {target}'))
    assert result.accepted and result.score == 1.0


LONG = 'Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless Distant Stars'
@pytest.mark.parametrize('a,b', [('(Alpha Remix)', '(Beta Remix)'), ('(2026 Edit)', '(2025 Mix)'),
    ('(Hard Refix)', ''), ('', '(Hard Refix)'), ('(Live)', ''), ('', '(Acoustic)'),
    ('(Cover)', ''), ('(Live)', '(Acoustic)'), ('(Alpha Remix)', ''),
    ('(Uncharted Territory)', '')])
def test_long_shared_base_does_not_override_recording_distinctions(a, b):
    result = evaluate_candidate('Synthetic Artist', f'{LONG} {a}',
                                  video(f'Synthetic Artist - {LONG} {b}'))
    assert not result.accepted and result.reason == 'version_mismatch'


@pytest.mark.parametrize('source,heading,names,accepted', [
    ('Synthetic Artist & MC Signal', 'Synthetic Artist, MC Signal', (), True),
    ('Synthetic Artist, MC Signal', 'Synthetic Artist & MC Signal', (), True),
    ('Synthetic Artist & Collaborator', 'Synthetic Artist, Collaborator', ('Synthetic Artist', 'Collaborator'), True),
    ('Synthetic Artist, Collaborator', 'Synthetic Artist & Collaborator', (), False),
    ('Synthetic Artist & MC Signal', 'Synthetic Artist', (), False),
    ('Signal & Noise', 'Signal & Noise', ('Signal & Noise',), True),
    ('Signal', 'Signal & Noise', ('Signal & Noise',), False),
    ('Synthetic Artist', 'Unrelated Producer', (), False),
    ('Synthetic Artist and MC Signal', 'Synthetic Artist, MC Signal', ('Synthetic Artist', 'MC Signal'), False),
])
def test_credit_evidence_preserves_compound_artists_and_people(source, heading, names, accepted):
    result = evaluate_candidate(source, 'Night Signal',
                                video(f'{heading} - Night Signal', artist_names=names))
    assert result.accepted is accepted
    if accepted:
        assert result.score == 1.0
    else:
        assert result.reason in {'artist_evidence', 'ambiguous_credits'}


def test_channel_alone_never_supplies_missing_artist():
    result = evaluate_candidate('Synthetic Artist', 'Night Signal',
                                video('Night Signal', channel='Synthetic Artist'))
    assert not result.accepted and result.reason == 'missing_artist'


def test_structured_credits_allow_title_only_video():
    result = evaluate_candidate('Synthetic Artist', 'Night Signal',
        video('Night Signal (Official Audio)', artist_names=('Synthetic Artist',), track='Night Signal'))
    assert result.accepted and result.score == 1.0


@pytest.mark.parametrize('kwargs,reason', [({'is_live': True}, 'live'), ({'duration': None}, 'duration'),
    ({'duration': 119}, 'duration'), ({'duration': 481}, 'duration'), ({'duration': True}, 'duration'),
    ({'duration': float('nan')}, 'duration'), ({'description': 'full DJ set compilation'}, 'bad_video')])
def test_bad_video_live_and_duration_guards(kwargs, reason):
    result = evaluate_candidate('Synthetic Artist', 'Night Signal', video('Synthetic Artist - Night Signal', **kwargs))
    assert not result.accepted and result.reason == reason


@pytest.mark.parametrize('title,kwargs', [
    ('Synthetic Artist - Night Signal (Alpha Remix)', {'artist': 'Synthetic Artist', 'track': 'Night Signal'}),
    ('Unrelated Producer - Night Signal', {'artist': 'Synthetic Artist', 'track': 'Night Signal'}),
    ('Synthetic Artist - Completely Different Recording', {'artist': 'Synthetic Artist', 'track': 'Night Signal'}),
    ('Completely Different Recording', {'artist': 'Synthetic Artist', 'track': 'Night Signal'}),
])
def test_structured_metadata_cannot_hide_visible_version_or_artist_conflict(title, kwargs):
    result = evaluate_candidate('Synthetic Artist', 'Night Signal', video(title, **kwargs))
    assert not result.accepted and result.reason == 'metadata_conflict'


@pytest.mark.parametrize('source,target,score,accepted', [
    (LONG, 'Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless', .9, True),
    ('Silver Midnight Signals Drift Across Frozen Valleys Beneath Endless Distant',
     'Silver Midnight Signals Drift Across Frozen Valleys Beneath', .89, False),
    ('Silver Midnight Signals Drift', 'Silver Midnight Signals', .8625, False),
])
def test_actual_ninety_percent_threshold_has_inclusive_boundary(source, target, score, accepted):
    result = evaluate_candidate('Synthetic Artist', source, video(f'Synthetic Artist - {target}'))
    assert result.score == pytest.approx(score) and result.accepted is accepted


def test_reverse_overlap_rejects_longer_unrelated_compilation_title():
    result = compare_recordings('Synthetic Artist', 'Night Signal', 'Synthetic Artist',
                                'Night Signal Silver Midnight Frozen Valleys')
    assert not result.accepted and result.reason == 'title_overlap'


def test_original_generic_suffix_false_rejection_is_reproduced_before_corrected_acceptance():
    source = 'Night Signal (Original Mix)'
    heading = 'Synthetic Artist - Night Signal'
    artist, title = parse_artist_title(heading)
    old = match_confidence(artist='Synthetic Artist', title=source, candidate_artist=artist, candidate_title=title)
    assert old == pytest.approx(.725) and old < .9
    corrected = evaluate_candidate('Synthetic Artist', source, video(heading))
    assert corrected.accepted and corrected.score == 1.0
