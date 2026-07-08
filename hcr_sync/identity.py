"""Track identity and conservative normalization helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata

SEPARATOR_RE = re.compile(r"\s+(?:-|–|—)\s+", re.U)
BRACKET_NOISE_RE = re.compile(
    r"\s*[\(\[]\s*(?:official\s+video|official\s+audio|official|music\s+video|lyrics?|hq|hd)\s*[\)\]]",
    re.I,
)
PUNCT_RE = re.compile(r"[^0-9a-z]+")


def compact_text(value: object) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def parse_artist_title(raw: str) -> tuple[str, str]:
    raw = compact_text(raw)
    match = SEPARATOR_RE.split(raw, maxsplit=1)
    if len(match) == 2 and match[0] and match[1]:
        return compact_text(match[0]), compact_text(match[1])
    return "", raw


def display_from_parts(artist: str, title: str) -> str:
    artist = compact_text(artist)
    title = compact_text(title)
    return f"{artist} - {title}" if artist and title else title or artist


def normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = BRACKET_NOISE_RE.sub(" ", normalized)
    normalized = re.sub(r"\b([a-z0-9]+)'s\b", r"\1s", normalized, flags=re.I)
    normalized = normalized.replace("&", " and ")
    normalized = normalized.casefold()
    normalized = PUNCT_RE.sub(" ", normalized)
    return compact_text(normalized)


def canonical_key(artist: str, title: str) -> str:
    normalized_artist = normalize_for_match(artist)
    normalized_title = normalize_for_match(title)
    if not normalized_artist and not normalized_title:
        normalized_title = normalize_for_match(display_from_parts(artist, title))
    return f"{normalized_artist}::{normalized_title}"


def fingerprint(value: str) -> str:
    return hashlib.sha256(normalize_for_match(value).encode("utf-8")).hexdigest()


def token_set(value: str) -> set[str]:
    return {token for token in normalize_for_match(value).split() if len(token) > 1}


def match_confidence(
    *,
    artist: str,
    title: str,
    candidate_artist: str,
    candidate_title: str,
    duration_delta_seconds: int | None = None,
) -> float:
    source_artist = token_set(artist)
    source_title = token_set(title)
    cand_artist = token_set(candidate_artist)
    cand_title = token_set(candidate_title)

    if not source_title:
        return 0.0

    artist_score = 1.0 if not source_artist else len(source_artist & cand_artist) / max(1, len(source_artist))
    title_score = len(source_title & cand_title) / max(1, len(source_title))
    score = (artist_score * 0.45) + (title_score * 0.55)
    if duration_delta_seconds is not None:
        if duration_delta_seconds <= 3:
            score += 0.05
        elif duration_delta_seconds > 20:
            score -= 0.1
    return max(0.0, min(1.0, score))
