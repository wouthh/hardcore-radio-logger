"""Evidence-based YouTube comparisons without treating uploader names as credits."""
from dataclasses import dataclass, replace
import math
import re

from .identity import compact_text, duplicate_title_tokens, match_confidence, normalize_for_match, parse_artist_title
from .recording_comparison import (BRACKET_SUFFIX, DASH_SUFFIX, VERSION_MARKER, TitleComparison,
    comparison_title, comparison_artists, compatible_titles)

PRESENTATION_LABELS = frozenset({'official audio', 'official video', 'official music video', 'music video',
    'lyrics', 'lyric', 'lyric video', 'lyrics video', 'hq', 'hd', 'official'})
BAD_VIDEO_RE = re.compile(
    r'\b(full\s+mix|full\s+set|dj\s+set|liveset|mixtape|megamix|yearmix|podcast|radio\s+show|'
    r'compilation|full\s+album|festival\s+set|aftermovie|trailer|interview|documentary|gameplay)\b', re.I)


@dataclass(frozen=True)
class MatchDecision:
    accepted: bool
    score: float
    reason: str
    source_artist: str
    candidate_artist: str
    source_title: TitleComparison
    candidate_title: TitleComparison


def _title(title):
    remaining = compact_text(title)
    labels = []
    while remaining:
        match = BRACKET_SUFFIX.fullmatch(remaining)
        bracketed = match is not None
        if bracketed:
            tail = remaining[len(match[1]):].strip()
            if (tail[0], tail[-1]) not in {('(', ')'), ('[', ']')}:
                break
        else:
            match = DASH_SUFFIX.fullmatch(remaining)
        if not match:
            break
        label = normalize_for_match(match[2])
        if not bracketed and label not in PRESENTATION_LABELS and not VERSION_MARKER.search(label):
            break
        if label not in PRESENTATION_LABELS:
            labels.insert(0, f'({label})')
        remaining = match[1].strip()
    return comparison_title(' '.join([remaining, *labels]))


def _credits(artist, artist_names):
    if artist_names:
        return tuple(artist_names)
    # Only an explicitly labelled MC credit corroborates a heading separator.
    parts = re.split(r'\s+&\s+|\s*,\s*', artist)
    if len(parts) > 1 and all(re.match(r'^MC\s+\S', part, re.I) for part in parts[1:]):
        return tuple(parts)
    return (artist,) if artist else ()


def compare_recordings(source_artist, source_title, candidate_artist, candidate_title, *, artist_names=(), threshold=.90):
    source, target = _title(source_title), _title(candidate_title)
    decision = MatchDecision(False, 0.0, 'missing_artist', normalize_for_match(source_artist),
                             normalize_for_match(candidate_artist), source, target)
    if not compact_text(source_artist) or not (compact_text(candidate_artist) or artist_names):
        return decision
    credits = _credits(candidate_artist, artist_names)
    artists = comparison_artists(source_artist, candidate_artist, credits)
    if artists is None:
        reason = 'ambiguous_credits' if any(separator in source_artist or separator in candidate_artist
                    for separator in (' & ', ',')) else 'artist_evidence'
        return replace(decision, reason=reason)
    decision = replace(decision, source_artist=artists[0], candidate_artist=artists[1])
    if not compatible_titles(source, target):
        return replace(decision, reason='version_mismatch')
    a, b = duplicate_title_tokens(source.base), duplicate_title_tokens(target.base)
    if not a or not b or len(a & b)/len(a) < .75 or len(a & b)/len(b) < .75:
        return replace(decision, reason='title_overlap')
    score = match_confidence(artist=artists[0], title=source.text,
                             candidate_artist=artists[1], candidate_title=target.text)
    return replace(decision, accepted=score >= threshold, score=score,
                   reason='matched' if score >= threshold else 'below_threshold')


def evaluate_candidate(artist, title, candidate, *, threshold=.90):
    parsed_artist, parsed_title = parse_artist_title(candidate.title)
    structured_artist = compact_text(getattr(candidate, 'artist', ''))
    candidate_artist = structured_artist or parsed_artist
    candidate_title = compact_text(getattr(candidate, 'track', '')) or parsed_title or candidate.title
    names = tuple(getattr(candidate, 'artist_names', ()) or ())
    decision = compare_recordings(artist, title, candidate_artist, candidate_title,
                                  artist_names=names, threshold=threshold)
    if compact_text(getattr(candidate, 'track', '')):
        visible = _title(parsed_title or candidate.title)
        structured = _title(candidate_title)
        visible_decision = compare_recordings(artist, title, candidate_artist, parsed_title or candidate.title,
                                             artist_names=names, threshold=threshold)
        if visible.versions != structured.versions or not visible_decision.accepted:
            return replace(decision, accepted=False, reason='metadata_conflict')
    if structured_artist and parsed_artist:
        heading = compare_recordings(artist, title, parsed_artist, parsed_title,
                                     artist_names=names, threshold=threshold)
        if not heading.accepted:
            return replace(decision, accepted=False, reason='metadata_conflict')
    duration = candidate.duration
    if candidate.is_live:
        return replace(decision, accepted=False, reason='live')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 120 <= duration <= 480:
        return replace(decision, accepted=False, reason='duration')
    if BAD_VIDEO_RE.search(f'{candidate.title} {candidate.channel} {candidate.description}'):
        return replace(decision, accepted=False, reason='bad_video')
    return decision
