"""Conservative YouTube search/download sync."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .db import add_event, connect, now_utc, transaction, upsert_youtube_asset, wanted_tracks
from .identity import compact_text, duplicate_title_tokens, normalize_for_match, radio_metadata_placeholder
from .local_files import AUDIO_EXTENSIONS, youtube_id_from_path
from .system import assert_legacy_downloader_safe

PLACEHOLDER_RE = re.compile(r"\bunknown\s+(?:artist|title)\b|#\s*0*\d+\b", re.I)
SOURCE_NON_TRACK_RE = re.compile(
    r"\b("
    r"various\s+artists|hardcore\s+radio|festival\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|studio\s+\d+|"
    r"full\s+mix|full\s+set|dj\s+set|live\s+set|liveset|mixtape|megamix|yearmix|podcast|radio\s+show|"
    r"compilation|full\s+album|continuous\s+mix|mix\s+session|festival\s+set|aftermovie|trailer|teaser|"
    r"interview|documentary|recap|artist\s+series|episode"
    r")\b",
    re.I,
)
TITLE_SEGMENT_RE = re.compile(r"\s+(?:-|–|—|\||/)\s+")
CATALOG_SEGMENT_RE = re.compile(
    r"^(?:[ab]\d?|side\s+[ab]\d?|nr\s*\d+|[a-z]{1,5}\s*\d{1,4}|[a-z]+\d+[a-z]*|\d+)$",
    re.I,
)


from .youtube_adapter import YouTubeCandidate, YtDlpClient, YouTubeFailure
from .youtube_matching import evaluate_candidate
from .youtube_local import local_satisfaction, association_is_different
from .youtube_queue import (LANES, advance, choose, decision_json, failed, pause_active,
    pause_state, save_schedule, source_fingerprint, stamp, succeeded)
from .youtube_download import publish_output, process_is_alive, read_receipt, UnsafeDownloadOutput
from .db import get_state, set_state
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import time
import uuid


class YouTubeClientProtocol(Protocol):
    def search(self, artist: str, title: str) -> list[YouTubeCandidate]: ...
    def download(self, candidate: YouTubeCandidate) -> Path: ...


@dataclass
class YouTubeSummary:
    wanted: int = 0
    already_local: int = 0
    downloaded: int = 0
    review: int = 0
    skipped: int = 0
    searched: int = 0
    search_invocations: int = 0
    download_starts: int = 0
    download_attempts: int = 0
    candidate_reused: int = 0
    due: int = 0
    deferred: int = 0
    pending: int = 0
    pause_until: str = ''
    degraded: str = ''


def _suspected_local_delete_track_keys(con) -> set[int]:
    return {
        row["track_id"]
        for row in con.execute(
            "SELECT DISTINCT track_id FROM youtube_assets WHERE suspected_missing_at IS NOT NULL"
        )
    }


def _youtube_candidate_used_by_other_track(
    con,
    *,
    track_id: int,
    youtube_video_id: str = "",
    file_path: str = "",
):
    conditions = []
    params: list[object] = [track_id]
    if youtube_video_id:
        conditions.append("youtube_video_id = ?")
        params.append(youtube_video_id)
    if file_path:
        conditions.append("file_path = ?")
        params.append(file_path)
    if not conditions:
        return None
    return con.execute(
        f"""
        SELECT *
          FROM youtube_assets
         WHERE track_id != ?
           AND (file_exists=1 OR NULLIF(file_path,'') IS NOT NULL OR downloaded_at IS NOT NULL)
           AND ({' OR '.join(conditions)})
         LIMIT 1
        """,
        params,
    ).fetchone()


def _path_is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _validate_download_output(config: Config, output: Path) -> Path:
    output = Path(output)
    if not output.exists():
        raise UnsafeDownloadOutput(f"download output was not created: {output}")
    if not output.is_file():
        raise UnsafeDownloadOutput(f"download output is not a file: {output}")
    if output.suffix.casefold() not in AUDIO_EXTENSIONS:
        raise UnsafeDownloadOutput(f"download output is not a supported audio file: {output}")
    if not _path_is_inside(output, config.music_dir):
        raise UnsafeDownloadOutput(f"download output is outside HCR_MUSIC_DIR: {output}")
    return output


def _looks_like_catalog_segment(segment: str) -> bool:
    normalized = normalize_for_match(segment)
    return not normalized or bool(CATALOG_SEGMENT_RE.fullmatch(normalized.replace(" ", "")))


def _looks_like_artist_segment(segment: str, artist: str) -> bool:
    segment_tokens = duplicate_title_tokens(segment)
    artist_tokens = duplicate_title_tokens(artist)
    return bool(segment_tokens and artist_tokens and segment_tokens <= artist_tokens)


def _looks_like_multi_title_candidate(track, candidate: YouTubeCandidate) -> bool:
    source_tokens = duplicate_title_tokens(track["display_title"])
    if not source_tokens:
        return False
    segments = [compact_text(part) for part in TITLE_SEGMENT_RE.split(candidate.title) if compact_text(part)]
    if len(segments) < 4:
        return False

    matched_source_title = False
    extra_title_like_segments = 0
    for segment in segments:
        if _looks_like_catalog_segment(segment) or _looks_like_artist_segment(segment, track["display_artist"]):
            continue
        segment_tokens = duplicate_title_tokens(segment)
        if not segment_tokens:
            continue
        overlap = len(source_tokens & segment_tokens) / max(1, len(source_tokens))
        reverse_overlap = len(source_tokens & segment_tokens) / max(1, len(segment_tokens))
        if overlap >= 0.75 or reverse_overlap >= 0.75:
            matched_source_title = True
        elif len(segment_tokens) >= 2:
            extra_title_like_segments += 1
    return matched_source_title and extra_title_like_segments > 0


def _candidate_decision(track, candidate, threshold=.90):
    decision = evaluate_candidate(track['display_artist'],track['display_title'],candidate,threshold=threshold)
    if _looks_like_multi_title_candidate(track,candidate):
        from dataclasses import replace
        return replace(decision,accepted=False,reason='multi_title')
    return decision


def _candidate_score(track, candidate):
    decision = _candidate_decision(track,candidate)
    return decision.score if decision.reason in {'matched','below_threshold'} else 0.0


def _mark_youtube_review(con, track_id: int, *, reason: str, score: float | None = None) -> None:
    if not con.execute("SELECT 1 FROM youtube_assets WHERE track_id=? AND status='review' AND file_exists=0",(track_id,)).fetchone():
        upsert_youtube_asset(con,track_id=track_id,match_confidence=score,file_exists=False,status='review')
    add_event(
        con,
        track_id,
        "ambiguous_youtube_match",
        "youtube_sync",
        {"reason": reason, "score": score},
        dedupe_key=f"ambiguous_youtube_match:{track_id}:{reason}",
    )


def _mark_youtube_error(con, track_id: int, candidate: YouTubeCandidate, *, score: float, error: Exception) -> None:
    established=con.execute("SELECT 1 FROM youtube_assets WHERE track_id=? AND youtube_video_id=? AND (file_exists=1 OR NULLIF(file_path,'') IS NOT NULL OR downloaded_at IS NOT NULL)",(track_id,candidate.video_id)).fetchone()
    if not established:
        upsert_youtube_asset(
            con,
            track_id=track_id,
            youtube_video_id=candidate.video_id,
            youtube_url=candidate.url,
            match_confidence=score,
            file_exists=False,
            status="error",
        )
    add_event(
        con,
        track_id,
        "youtube_download_failed",
        "youtube_sync",
        {
            "youtube_video_id": candidate.video_id,
            "youtube_url": candidate.url,
            "candidate_title": candidate.title,
            "error": str(error)[:500],
        },
        dedupe_key=f"youtube_download_failed:{track_id}:{candidate.video_id or candidate.url}",
    )


def _mark_youtube_candidate_conflict(con, track_id: int, candidate: YouTubeCandidate, *, score: float, existing_asset) -> None:
    upsert_youtube_asset(
        con,
        track_id=track_id,
        match_confidence=score,
        file_exists=False,
        status="review",
    )
    add_event(
        con,
        track_id,
        "youtube_candidate_already_linked",
        "youtube_sync",
        {
            "youtube_video_id": candidate.video_id,
            "youtube_url": candidate.url,
            "candidate_title": candidate.title,
            "score": score,
            "existing_track_id": existing_asset["track_id"],
            "existing_asset_id": existing_asset["id"],
            "reason": "candidate YouTube video is already linked to another DB track",
        },
        dedupe_key=f"youtube_candidate_already_linked:{track_id}:{candidate.video_id or candidate.url}:{existing_asset['id']}",
    )


def _candidate_payload(candidate):
    payload=asdict(candidate)
    # Descriptions are filter inputs, not durable diagnostics or provenance.
    payload['description']=''
    return payload


def _current_authorized(con, row):
    track = con.execute("SELECT * FROM tracks WHERE id=?",(row['track_id'],)).fetchone()
    if not track or track['status'] != 'wanted' or source_fingerprint(track) != row['source_fingerprint']:
        return False
    if con.execute("SELECT 1 FROM exclusions WHERE track_id=?",(row['track_id'],)).fetchone():
        return False
    for asset in con.execute("SELECT * FROM youtube_assets WHERE track_id=? AND file_exists=1 AND status='downloaded' AND file_path IS NOT NULL",(row['track_id'],)):
        if not Path(asset['file_path']).is_file() and not association_is_different(con,asset):
            return False
    return not con.execute("SELECT 1 FROM youtube_assets WHERE track_id=? AND (suspected_missing_at IS NOT NULL OR status='deleted')",(row['track_id'],)).fetchone()


def _ownership_conflict(con, source_id, candidate, work_id=''):
    asset = _youtube_candidate_used_by_other_track(con,track_id=source_id,youtube_video_id=candidate.video_id)
    if asset:
        return asset
    established=con.execute("SELECT * FROM youtube_assets WHERE track_id=? AND youtube_video_id=? AND (file_exists=1 OR NULLIF(file_path,'') IS NOT NULL OR downloaded_at IS NOT NULL) LIMIT 1",(source_id,candidate.video_id)).fetchone()
    if established:
        return established
    return con.execute("SELECT track_id,work_id FROM youtube_pending_work WHERE video_id=? AND work_id!=? AND state NOT IN ('completed','cancelled')",(candidate.video_id,work_id)).fetchone()


def _hold(con, row, now, reason):
    save_schedule(con,row['track_id'],now,phase='held',hold_origin=reason,decision_json=decision_json({'outcome':'held','reason':reason}))


def _initialize_queue(config, con, now, summary, require_youtube_id):
    cache = {}
    for track in wanted_tracks(con):
        summary.wanted += 1
        source_id=track['id']
        row=con.execute('SELECT * FROM youtube_schedule WHERE track_id=?',(source_id,)).fetchone()
        protected = None
        local = 'none'
        if radio_metadata_placeholder(track['display_artist'],track['display_title']) or SOURCE_NON_TRACK_RE.search(f"{track['display_artist']} {track['display_title']}") or PLACEHOLDER_RE.search(f"{track['display_artist']} {track['display_title']}"):
            protected='non_track'
        elif source_id in _suspected_local_delete_track_keys(con) or con.execute("SELECT 1 FROM youtube_assets WHERE track_id=? AND status='deleted'",(source_id,)).fetchone():
            protected='local_deletion'
            summary.skipped+=1
            add_event(con,source_id,'youtube_skipped_suspected_local_delete','youtube_sync',{'reason':'local deletion is awaiting confirmation'},dedupe_key=f'youtube_skipped_suspected_local_delete:{source_id}')
        if protected is None:
            local,asset=local_satisfaction(config,con,track,require_youtube_id=require_youtube_id,cache=cache,persist=True)
            if local=='satisfied':
                summary.already_local+=1
                protected='local_satisfied'
                if asset['track_id'] != source_id:
                    add_event(con,source_id,'youtube_skipped_existing_local_match','youtube_sync',{'matched_track_id':asset['track_id'],'matched_asset_id':asset['id']},dedupe_key=f"youtube_skipped_existing_local_match:{source_id}:{asset['id']}")
            elif local=='ambiguous':
                protected='local_ambiguous'
            elif any(not Path(asset['file_path']).is_file() and not association_is_different(con,asset) for asset in con.execute("SELECT * FROM youtube_assets WHERE track_id=? AND file_exists=1 AND status='downloaded' AND file_path IS NOT NULL",(source_id,))):
                protected='local_missing'
                summary.skipped+=1
        if row is None:
            if protected is None and con.execute("SELECT 1 FROM youtube_assets WHERE track_id=? AND status IN ('review','error') AND file_exists=0",(source_id,)).fetchone():
                protected='legacy_unclassified'
            phase='held' if protected else ('search_retry' if local=='different' else 'first_search')
            con.execute('INSERT INTO youtube_schedule(track_id,source_fingerprint,phase,next_eligible_at,hold_origin,updated_at) VALUES (?,?,?,?,?,?)',(source_id,source_fingerprint(track),phase,stamp(now),protected,stamp(now)))
            if protected=='non_track' and not radio_metadata_placeholder(track['display_artist'],track['display_title']):
                _mark_youtube_review(con,source_id,reason='source is a placeholder or non-track',score=0)
        elif row['phase']=='held' and row['hold_origin'] not in {'local_satisfied','local_ambiguous','local_missing','local_deletion'}:
            continue
        elif protected:
            _hold(con, {'track_id':source_id},now,protected)
            if protected=='local_satisfied':
                con.execute("UPDATE youtube_pending_work SET state='cancelled',updated_at=? WHERE track_id=? AND state='prepared'",(stamp(now),source_id))
        elif row['source_fingerprint']!=source_fingerprint(track):
            # Updated inputs invalidate unfinished decisions, never old provenance.
            con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE track_id=? AND state NOT IN ('completed','cancelled')",(stamp(now),source_id))
            if row['hold_origin'] not in {'manual','ownership','legacy_unclassified'}:
                save_schedule(con,source_id,now,source_fingerprint=source_fingerprint(track),phase='search_retry',candidate_json=None,candidate_at=None,hold_origin=None)
        elif row['phase']=='held' and row['hold_origin'] in {'local_satisfied','local_ambiguous','local_missing','local_deletion'}:
            save_schedule(con,source_id,now,phase='search_retry',hold_origin=None)
    summary.review=con.execute("SELECT COUNT(*) FROM youtube_schedule q JOIN tracks t ON t.id=q.track_id WHERE t.status='wanted' AND q.phase='held' AND q.hold_origin!='local_satisfied'").fetchone()[0]


def _work_candidate(work):
    return YouTubeCandidate(**json.loads(work['payload_json'])['candidate'])


def _complete_download(config,con,work,now):
    row=con.execute('SELECT * FROM youtube_schedule WHERE track_id=?',(work['track_id'],)).fetchone()
    candidate=_work_candidate(work)
    payload=json.loads(work['payload_json'])
    if not row or payload.get('fingerprint') != row['source_fingerprint'] or not _current_authorized(con,row) or _ownership_conflict(con,work['track_id'],candidate,work['work_id']):
        with transaction(con):
            con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
            if row:
                _hold(con,row,now,'authorization_or_ownership')
        return False
    track=con.execute('SELECT * FROM tracks WHERE id=?',(work['track_id'],)).fetchone()
    decision=_candidate_decision(track,candidate,config.float('HCR_YOUTUBE_MATCH_THRESHOLD'))
    if not decision.accepted:
        with transaction(con):
            _hold(con,row,now,'candidate_changed')
            con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
        return False
    if work['output_path']:
        conflict=_youtube_candidate_used_by_other_track(con,track_id=work['track_id'],file_path=work['output_path'])
        if conflict:
            with transaction(con):
                _mark_youtube_candidate_conflict(con,work['track_id'],candidate,score=decision.score,existing_asset=conflict)
                _hold(con,row,now,'ownership')
                con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
            return False
    output=publish_output(config,candidate,work,payload['source_artist'],payload['source_title'])
    # Publication is idempotent; a crash before this commit is recovered offline.
    with transaction(con):
        if not _current_authorized(con,row) or _ownership_conflict(con,work['track_id'],candidate,work['work_id']):
            _hold(con,row,now,'authorization_or_ownership')
            con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
            return False
        upsert_youtube_asset(con,track_id=work['track_id'],youtube_video_id=candidate.video_id,youtube_url=candidate.url,file_path=str(output),file_exists=True,match_confidence=decision.score,status='downloaded',downloaded_at=stamp(now))
        save_schedule(con,work['track_id'],now,phase='held',hold_origin='local_satisfied',last_download_at=stamp(now),download_attempts=row['download_attempts']+1,download_failures=0,candidate_json=None,candidate_at=None,decision_json=decision_json({'outcome':'verified_local','candidate':_candidate_payload(candidate),'score':decision.score}))
        con.execute("UPDATE youtube_pending_work SET state='completed',output_path=?,updated_at=? WHERE work_id=?",(str(output),stamp(now),work['work_id']))
        advance(con,2)
        succeeded(con)
        add_event(con,work['track_id'],'youtube_downloaded','youtube_sync',{'youtube_video_id':candidate.video_id,'file_path':str(output)})
    return True


def recover_youtube_pending(config, *, apply=True):
    """Offline recovery before import/reconciliation; never starts a downloader."""
    summary=YouTubeSummary()
    if not apply or not config.db_path.exists():
        return summary
    now=datetime.now(timezone.utc)
    with connect(config) as con:
        for row in con.execute("SELECT * FROM youtube_schedule WHERE phase IN ('first_search','search_retry')").fetchall():
            evidence=json.loads(row['decision_json'] or '{}')
            if evidence.get('outcome')=='search_inflight':
                with transaction(con):
                    failed(con,row,evidence['lane'],YouTubeFailure('transient','interrupted search has no completed response'),now)
        works=con.execute("SELECT * FROM youtube_pending_work WHERE state NOT IN ('completed','cancelled','held') ORDER BY created_at,work_id").fetchall()
        pause = pause_active(con, now)
        if pause and pause.get('operator_required'):
            summary.pending = len(works)
            summary.degraded = pause['kind']
            summary.pause_until = pause.get('until', '')
            return summary
        for work in works:
            if process_is_alive(work['process_pid'],work['process_start']):
                summary.pending+=1
                continue
            receipt=read_receipt(config,work)
            if receipt:
                try:
                    completed=_complete_download(config,con,work,now)
                except (UnsafeDownloadOutput,OSError):
                    with transaction(con):
                        con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
                        _hold(con,{'track_id':work['track_id']},now,'filesystem_safety')
                    raise
                except YouTubeFailure as exc:
                    with transaction(con):
                        if exc.category in {'missing_tool', 'configuration'}:
                            row = con.execute('SELECT * FROM youtube_schedule WHERE track_id=?', (work['track_id'],)).fetchone()
                            failed(con, row, 2, exc, now)
                            con.execute("UPDATE youtube_pending_work SET state='prepared',error_json=?,updated_at=? WHERE work_id=?", (decision_json({'category':exc.category,'reason':exc.detail}), stamp(now), work['work_id']))
                        else:
                            con.execute("UPDATE youtube_pending_work SET state='held',error_json=?,updated_at=? WHERE work_id=?",(decision_json({'category':exc.category,'reason':exc.detail}),stamp(now),work['work_id']))
                            _hold(con,{'track_id':work['track_id']},now,exc.category)
                    summary.pending+=1
                    if exc.category in {'missing_tool', 'configuration'}:
                        summary.degraded = exc.category
                        summary.pause_until = pause_state(con).get('until', '')
                        break
                else:
                    summary.downloaded+=int(completed)
                    summary.review+=int(not completed)
            elif work['state']=='dispatched':
                row=con.execute('SELECT * FROM youtube_schedule WHERE track_id=?',(work['track_id'],)).fetchone()
                with transaction(con):
                    failed(con,row,2,YouTubeFailure('transient','interrupted download has no verified completion receipt'),now)
                    con.execute("UPDATE youtube_pending_work SET state='prepared',process_pid=NULL,process_start=NULL,updated_at=? WHERE work_id=? AND state='dispatched'",(stamp(now),work['work_id']))
                summary.pending+=1
    return summary


def sync_youtube(config: Config, *, apply: bool, client: YouTubeClientProtocol|None=None, complete_idless_local: bool|None=None, lock_handle=None, now=None) -> YouTubeSummary:
    if apply:
        assert_legacy_downloader_safe(config)
    now=now or datetime.now(timezone.utc)
    summary=YouTubeSummary()
    search_limit=config.int('HCR_YOUTUBE_SEARCH_LIMIT')
    download_limit=config.int('HCR_YOUTUBE_DOWNLOAD_LIMIT')
    seconds=config.int('HCR_YOUTUBE_RUN_TIMEOUT_SECONDS')
    if min(search_limit,download_limit)<1 or seconds<140:
        raise ValueError('YouTube limits need positive search/download allowances and at least 140 seconds')
    complete_idless_local=config.bool('HCR_YOUTUBE_COMPLETE_IDLESS_LOCAL') if complete_idless_local is None else complete_idless_local
    # Dry-run previews local queue only; it must not launch live provider work.
    if not apply:
        with connect(config) as con:
            tracks=wanted_tracks(con)
            summary.wanted=len(tracks)
            summary.review=sum(radio_metadata_placeholder(t['display_artist'],t['display_title']) for t in tracks)
            summary.due=con.execute("SELECT COUNT(*) FROM youtube_schedule WHERE phase IN ('first_search','search_retry','download') AND next_eligible_at<=?",(stamp(now),)).fetchone()[0]
        return summary
    deadline=time.monotonic()+seconds
    with connect(config) as con:
        with transaction(con):
            _initialize_queue(config,con,now,summary,complete_idless_local)
        pause=pause_active(con,now)
        if pause:
            summary.pause_until=pause.get('until','')
            summary.degraded=pause.get('kind','provider_pause')
            return summary
        probing=bool(pause_state(con).get('until'))
        client=client or YtDlpClient(config,lock_handle=lock_handle)
        initial_searches=client.search_invocations if isinstance(client,YtDlpClient) else 0
        initial_downloads=client.download_invocations if isinstance(client,YtDlpClient) else 0
        while True:
            lane,row=choose(con,now)
            if row is None:
                break
            reservation=140 if lane==2 else 50
            if time.monotonic()+reservation>deadline or (lane==2 and summary.download_attempts>=download_limit) or (lane!=2 and summary.searched>=search_limit):
                # Persist the selected lane even when empty lanes were yielded.
                with transaction(con):
                    set_state(con,'youtube_queue_cursor',str(lane))
                summary.degraded='work_budget'
                break
            if not _current_authorized(con,row):
                with transaction(con):
                    _hold(con,row,now,'authorization')
                summary.skipped+=1
                continue
            track={'id':row['track_id'],'display_artist':row['display_artist'],'display_title':row['display_title']}
            if lane!=2:
                summary.searched+=1
                with transaction(con):
                    set_state(con,'youtube_queue_cursor',str(lane))
                    save_schedule(con,row['track_id'],now,decision_json=decision_json({'outcome':'search_inflight','lane':lane,'started_at':stamp(now)}))
                try:
                    candidates=client.search(row['display_artist'],row['display_title'])
                except (YouTubeFailure,RuntimeError) as exc:
                    failure=exc if isinstance(exc,YouTubeFailure) else YouTubeFailure('transient',str(exc)[:500])
                    with transaction(con):
                        failed(con,row,lane,failure,now)
                    summary.skipped+=1
                    summary.degraded=failure.category
                    if pause_active(con,now) or probing:
                        break
                    continue
                evaluated=[(candidate,_candidate_decision(track,candidate,config.float('HCR_YOUTUBE_MATCH_THRESHOLD'))) for candidate in candidates]
                accepted=[item for item in evaluated if item[1].accepted]
                best,decision=max(accepted or evaluated,key=lambda item:item[1].score,default=(None,None))
                with transaction(con):
                    fields={'search_attempts':row['search_attempts']+1,'last_search_at':stamp(now),'search_failures':0}
                    if not accepted:
                        count=row['unsuccessful_matches']+1
                        fields.update(phase='search_retry',unsuccessful_matches=count,next_eligible_at=stamp(now+timedelta(days=7 if count==1 else 14)),decision_json=decision_json({'outcome':'no_match','candidate':_candidate_payload(best) if best else None,'decision':asdict(decision) if decision else None}))
                        _mark_youtube_review(con,row['track_id'],reason=decision.reason if decision else 'completed empty search',score=decision.score if decision else None)
                        summary.review+=1
                    elif (conflict := _ownership_conflict(con,row['track_id'],best)):
                        if 'id' in conflict.keys():
                            _mark_youtube_candidate_conflict(con,row['track_id'],best,score=decision.score,existing_asset=conflict)
                        fields.update(phase='held',hold_origin='ownership',decision_json=decision_json({'outcome':'held','reason':'ownership','candidate':_candidate_payload(best)}))
                        summary.review+=1
                    else:
                        payload={'candidate':_candidate_payload(best),'source_artist':row['display_artist'],'source_title':row['display_title'],'fingerprint':row['source_fingerprint'],'score':decision.score,'lane':2}
                        work_id=uuid.uuid4().hex
                        con.execute('INSERT INTO youtube_pending_work(work_id,track_id,video_id,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?)',(work_id,row['track_id'],best.video_id,json.dumps(payload),stamp(now),stamp(now)))
                        fields.update(phase='download',next_eligible_at=stamp(now),candidate_json=json.dumps(_candidate_payload(best)),candidate_at=stamp(now),hold_origin=None,decision_json=decision_json({'outcome':'qualified','candidate':_candidate_payload(best),'decision':asdict(decision)}))
                    save_schedule(con,row['track_id'],now,**fields)
                    advance(con,lane)
                    succeeded(con)
            else:
                candidate=YouTubeCandidate(**json.loads(row['candidate_json']))
                decision=_candidate_decision(track,candidate,config.float('HCR_YOUTUBE_MATCH_THRESHOLD'))
                work=con.execute("SELECT * FROM youtube_pending_work WHERE track_id=? AND state NOT IN ('completed','cancelled','held') ORDER BY created_at LIMIT 1",(row['track_id'],)).fetchone()
                if not work:
                    with transaction(con):
                        _hold(con,row,now,'missing_download_intent')
                    continue
                if not decision.accepted or stamp(now-timedelta(days=7))>row['candidate_at']:
                    with transaction(con):
                        save_schedule(con,row['track_id'],now,phase='search_retry',candidate_json=None,candidate_at=None,next_eligible_at=stamp(now+timedelta(hours=24)))
                        con.execute("UPDATE youtube_pending_work SET state='cancelled',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
                    continue
                if _ownership_conflict(con,row['track_id'],candidate,work['work_id']):
                    with transaction(con):
                        _hold(con,row,now,'ownership')
                    continue
                if process_is_alive(work['process_pid'],work['process_start']):
                    summary.pending+=1
                    break
                with transaction(con):
                    set_state(con,'youtube_queue_cursor',str(lane))
                    con.execute("UPDATE youtube_pending_work SET state='dispatched',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
                summary.download_attempts+=1
                if not isinstance(client,YtDlpClient):
                    summary.download_starts+=1
                summary.candidate_reused+=int(row['download_attempts']>0)
                try:
                    if isinstance(client,YtDlpClient):
                        client.download(candidate,work=dict(work))
                    else:
                        try:
                            output=client.download(candidate)
                        except RuntimeError as exc:
                            raise YouTubeFailure('transient',str(exc)[:500]) from exc
                        output=_validate_download_output(config,output)
                        # Fake adapters use the same verification/publication trust boundary.
                        con.execute('UPDATE youtube_pending_work SET output_path=? WHERE work_id=?',(str(output),work['work_id']))
                        con.commit()
                    work=con.execute('SELECT * FROM youtube_pending_work WHERE work_id=?',(work['work_id'],)).fetchone()
                    completed=_complete_download(config,con,work,now)
                except (UnsafeDownloadOutput,OSError):
                    with transaction(con):
                        _hold(con,row,now,'filesystem_safety')
                        con.execute("UPDATE youtube_pending_work SET state='held',updated_at=? WHERE work_id=?",(stamp(now),work['work_id']))
                    raise
                except YouTubeFailure as exc:
                    with transaction(con):
                        _mark_youtube_error(con,row['track_id'],candidate,score=decision.score,error=exc)
                        failed(con,row,lane,exc,now)
                        con.execute("UPDATE youtube_pending_work SET state='prepared',error_json=?,updated_at=? WHERE work_id=? AND state='dispatched'",(decision_json({'category':exc.category,'reason':exc.detail}),stamp(now),work['work_id']))
                    summary.skipped+=1
                    summary.degraded=exc.category
                    if pause_active(con,now):
                        break
                else:
                    summary.downloaded+=int(completed)
                    summary.review+=int(not completed)
            if probing:
                break
        summary.search_invocations=client.search_invocations-initial_searches if isinstance(client,YtDlpClient) else summary.searched
        if isinstance(client,YtDlpClient):
            summary.download_starts=client.download_invocations-initial_downloads
        summary.due=con.execute("SELECT COUNT(*) FROM youtube_schedule q JOIN tracks t ON t.id=q.track_id WHERE t.status='wanted' AND q.phase IN ('first_search','search_retry','download') AND q.next_eligible_at<=?",(stamp(now),)).fetchone()[0]
        summary.deferred=con.execute("SELECT COUNT(*) FROM youtube_schedule WHERE phase IN ('first_search','search_retry','download') AND next_eligible_at>?",(stamp(now),)).fetchone()[0]
        summary.pending=con.execute("SELECT COUNT(*) FROM youtube_pending_work WHERE state NOT IN ('completed','cancelled')").fetchone()[0]
    return summary
