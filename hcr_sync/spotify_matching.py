"""Comparison-only Spotify normalization; never changes stored track identity."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .identity import compact_text, normalize_for_match

GENERIC_VERSIONS = frozenset({
    'original mix', 'original version', 'extended', 'extended mix',
    'extended version', 'radio edit', 'radio mix', 'radio version',
})
BRACKET_SUFFIX = re.compile(r'^(.*\S)\s*[([]([^()[\]]+)[)\]]\s*$')
DASH_SUFFIX = re.compile(r'^(.*\S)\s+[-–—]\s+([^()[\]]+)\s*$')
VERSION_MARKER = re.compile(
    r'\b(?:remix|refix|rework|bootleg|vip|live|acoustic|cover|instrumental|'
    r'remaster(?:ed)?|dub|edit|mix|\d{4})\b', re.I,
)


@dataclass(frozen=True)
class TitleComparison:
    base: str
    versions: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return ' '.join([self.base, *(f'({version})' for version in self.versions)]).strip()


def _version_label(label: str) -> str:
    label = normalize_for_match(label)
    if re.fullmatch(r'\d{4} (?:edit|mix)', label):
        return label.split()[0]
    return re.sub(r'\bremix edit$', 'remix', label)


def comparison_title(title: str) -> TitleComparison:
    """Separate trailing version evidence without discarding arbitrary title text."""
    title = compact_text(title)
    versions = []
    while title:
        match = BRACKET_SUFFIX.fullmatch(title)
        bracketed = match is not None
        if bracketed:
            # A mismatched bracket is literal text, never removable metadata.
            tail = title[len(match[1]):].strip()
            if (tail[0], tail[-1]) not in {('(', ')'), ('[', ']')}:
                break
        else:
            match = DASH_SUFFIX.fullmatch(title)
        if not match:
            break
        base, label = match[1].strip(), normalize_for_match(match[2])
        if not base or not label:
            break
        if label not in GENERIC_VERSIONS:
            if not bracketed and not VERSION_MARKER.search(label):
                break
            versions.append(_version_label(label))
        title = base
    return TitleComparison(normalize_for_match(title), tuple(sorted(set(versions))))


def comparison_artists(source: str, candidate: str, artist_names=()) -> tuple[str, str] | None:
    """Use whole provider credits before interpreting source list punctuation."""
    names = tuple(artist_names) or tuple(re.split(r'\s*,\s*', candidate))
    credits = tuple(normalize_for_match(name) for name in names)
    if not credits or any(not credit for credit in credits):
        return None
    whole_source = normalize_for_match(source)
    if whole_source in credits:
        return whole_source, ' '.join(credits)
    source_credits = []
    for group in re.split(r'\s*,\s*', source):
        normalized = normalize_for_match(group)
        if normalized in credits:
            source_credits.append(normalized)
        else:
            source_credits.extend(normalize_for_match(part) for part in re.split(r'\s+&\s+', group))
    if not source_credits or any(not name or name not in credits for name in source_credits):
        return None
    return ' '.join(source_credits), ' '.join(credits)


def compatible_titles(source: TitleComparison, candidate: TitleComparison) -> bool:
    if source.versions != candidate.versions:
        return False
    # Unmarked version wording cannot be separated safely from the title. Keep
    # it literal and require equality rather than letting a long title hide it.
    if VERSION_MARKER.search(source.base) or VERSION_MARKER.search(candidate.base):
        return source.base == candidate.base
    return True
