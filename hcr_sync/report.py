"""Reporting helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db import connect


@dataclass
class Report:
    total_tracks: int = 0
    wanted_tracks: int = 0
    excluded_tracks: int = 0
    review_tracks: int = 0
    tracks_with_local_file: int = 0
    tracks_missing_local_file: int = 0
    tracks_in_spotify: int = 0
    tracks_missing_spotify: int = 0
    tentative_spotify_tracks: int = 0
    ambiguous_youtube: int = 0
    ambiguous_spotify: int = 0
    pending_suspicions: int = 0


def build_report(config: Config) -> Report:
    report = Report()
    with connect(config) as con:
        report.total_tracks = con.execute("SELECT COUNT(*) AS count FROM tracks").fetchone()["count"]
        report.wanted_tracks = con.execute("SELECT COUNT(*) AS count FROM tracks WHERE status = 'wanted'").fetchone()["count"]
        report.excluded_tracks = con.execute("SELECT COUNT(*) AS count FROM tracks WHERE status = 'excluded'").fetchone()["count"]
        report.review_tracks = con.execute("SELECT COUNT(*) AS count FROM tracks WHERE status = 'review'").fetchone()["count"]
        report.tracks_with_local_file = con.execute(
            "SELECT COUNT(DISTINCT track_id) AS count FROM youtube_assets WHERE file_exists = 1 AND status = 'downloaded'"
        ).fetchone()["count"]
        report.tracks_missing_local_file = con.execute(
            """
            SELECT COUNT(*) AS count
              FROM tracks
             WHERE status = 'wanted'
               AND id NOT IN (SELECT track_id FROM youtube_assets WHERE file_exists = 1 AND status = 'downloaded')
            """
        ).fetchone()["count"]
        report.tracks_in_spotify = con.execute(
            "SELECT COUNT(DISTINCT track_id) AS count FROM spotify_assets WHERE in_playlist = 1"
        ).fetchone()["count"]
        report.tracks_missing_spotify = con.execute(
            """
            SELECT COUNT(*) AS count
              FROM tracks
             WHERE status = 'wanted'
               AND id NOT IN (SELECT track_id FROM spotify_assets WHERE in_playlist = 1)
            """
        ).fetchone()["count"]
        report.tentative_spotify_tracks = con.execute(
            "SELECT COUNT(DISTINCT track_id) AS count FROM spotify_assets WHERE in_playlist = 1 AND status = 'review'"
        ).fetchone()["count"]
        report.ambiguous_youtube = con.execute(
            "SELECT COUNT(*) AS count FROM youtube_assets WHERE status = 'review'"
        ).fetchone()["count"]
        report.ambiguous_spotify = con.execute(
            "SELECT COUNT(*) AS count FROM spotify_assets WHERE status = 'review'"
        ).fetchone()["count"]
        report.pending_suspicions = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM youtube_assets WHERE suspected_missing_at IS NOT NULL) +
              (SELECT COUNT(*) FROM spotify_assets WHERE suspected_missing_at IS NOT NULL) AS count
            """
        ).fetchone()["count"]
    return report


def format_report(report: Report) -> str:
    return "\n".join(
        [
            f"total_tracks={report.total_tracks}",
            f"wanted_tracks={report.wanted_tracks}",
            f"excluded_tracks={report.excluded_tracks}",
            f"review_tracks={report.review_tracks}",
            f"tracks_with_local_file={report.tracks_with_local_file}",
            f"tracks_missing_local_file={report.tracks_missing_local_file}",
            f"tracks_in_spotify={report.tracks_in_spotify}",
            f"tracks_missing_spotify={report.tracks_missing_spotify}",
            f"tentative_spotify_tracks={report.tentative_spotify_tracks}",
            f"ambiguous_youtube_matches={report.ambiguous_youtube}",
            f"ambiguous_spotify_matches={report.ambiguous_spotify}",
            f"pending_destructive_confirmations={report.pending_suspicions}",
        ]
    )
