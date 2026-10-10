"""Recording evidence for local satisfaction; associations alone are not proof."""
from pathlib import Path
import json

from .db import get_state, set_state
from .identity import duplicate_title_tokens
from .local_files import inspect_audio_file, _tag_values
from .youtube_matching import compare_recordings, evaluate_candidate, _title
from .youtube_adapter import YouTubeCandidate
from .youtube_queue import source_fingerprint


def _completed_credits(con, asset, item, owner):
    if not asset['youtube_video_id'] or not item or item.youtube_video_id != asset['youtube_video_id']:
        return ()
    if owner is None:
        return ()
    for work in con.execute("SELECT payload_json FROM youtube_pending_work WHERE state='completed' AND track_id=? AND video_id=? AND output_path=? ORDER BY updated_at DESC,work_id",
                            (owner['id'],asset['youtube_video_id'],asset['file_path'])):
        try:
            payload=json.loads(work['payload_json'])
        except (json.JSONDecodeError,TypeError):
            continue
        if not isinstance(payload,dict) or payload.get('fingerprint') != source_fingerprint(owner) or payload.get('source_artist') != owner['display_artist'] or payload.get('source_title') != owner['display_title']:
            continue
        candidate=payload.get('candidate')
        if not isinstance(candidate,dict) or candidate.get('video_id') != asset['youtube_video_id']:
            continue
        names=candidate.get('artist_names')
        if isinstance(names,(list,tuple)) and names and all(isinstance(name,str) and name.strip() for name in names):
            try:
                recording=YouTubeCandidate(**candidate)
            except TypeError:
                continue
            if evaluate_candidate(owner['display_artist'],owner['display_title'],recording).accepted:
                return tuple(names)
    return ()


def local_satisfaction(config, con, track, *, require_youtube_id=False, cache=None, persist=False):
    cache = cache if cache is not None else {}
    rows = con.execute("SELECT * FROM youtube_assets WHERE file_exists=1 AND status='downloaded' AND file_path IS NOT NULL ORDER BY track_id=? DESC,id", (track['id'],)).fetchall()
    source_tokens = duplicate_title_tokens(_title(track['display_title']).base)
    ambiguous = None
    different = None
    satisfied = None
    for row in rows:
        if row['track_id'] != track['id'] and satisfied is not None:
            return 'satisfied',satisfied
        if require_youtube_id and not row['youtube_video_id']:
            continue
        path = Path(row['file_path'])
        if path.is_symlink():
            if row['track_id']==track['id']:
                ambiguous=row
                if persist:
                    set_state(con,f'youtube_local_evidence:{row["id"]}',json.dumps({'status':'ambiguous','source':[track['display_artist'],track['display_title']]}))
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        key = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key not in cache:
            cache[key] = (inspect_audio_file(path),_tag_values(path))
        item,tags = cache[key]
        own = row['track_id'] == track['id']
        token_key = ('title_tokens', key)
        if token_key not in cache:
            cache[token_key] = duplicate_title_tokens(_title(item.title).base) if item else set()
        candidate_tokens = cache[token_key]
        plausible = bool(source_tokens and candidate_tokens and
                         len(source_tokens & candidate_tokens) / len(source_tokens) >= .75 and
                         len(source_tokens & candidate_tokens) / len(candidate_tokens) >= .75)
        # Full version/credit checks are needed only for plausible cross-source files.
        if not own and not plausible:
            continue
        owner=con.execute('SELECT * FROM tracks WHERE id=?',(row['track_id'],)).fetchone()
        credit_key = ('completed_credits', row['id'], row['track_id'], row['youtube_video_id'], key,
                      source_fingerprint(owner) if owner else None)
        if credit_key not in cache:
            cache[credit_key] = _completed_credits(con,row,item,owner)
        credits = cache[credit_key]
        status = 'different'
        if item and item.artist and item.title:
            decision = compare_recordings(track['display_artist'], track['display_title'], item.artist, item.title, artist_names=credits)
            if decision.accepted:
                status = 'satisfied'
                if tags[0] and tags[1] and not compare_recordings(track['display_artist'],track['display_title'],*tags,artist_names=credits).accepted:
                    status = 'ambiguous'
            elif decision.reason in {'artist_evidence', 'ambiguous_credits', 'missing_artist', 'metadata_conflict'}:
                # Only plausible title evidence can hold unrelated sources.
                if own or plausible:
                    status = 'ambiguous'
        elif own or (item and plausible):
            status = 'ambiguous'
        if persist and own:
            evidence = {'status': status, 'artist': item.artist if item else '', 'title': item.title if item else '', 'source': [track['display_artist'],track['display_title']], 'stat': list(key)}
            set_state(con, f'youtube_local_evidence:{row["id"]}', json.dumps(evidence, separators=(',',':')))
        if status == 'satisfied':
            satisfied = row
            if not own:
                return status,row
        if status == 'ambiguous':
            ambiguous = row
        elif status == 'different' and own:
            different = row
    if satisfied is not None:
        return 'satisfied',satisfied
    if ambiguous is not None:
        return 'ambiguous', ambiguous
    return ('different', different) if different is not None else ('none', None)


def association_allows_absence(con, asset):
    """Negative inspection evidence must not become a destructive removal vote."""
    evidence = get_state(con, f'youtube_local_evidence:{asset["id"]}')
    if evidence:
        evidence=json.loads(evidence)
        track=con.execute('SELECT display_artist,display_title FROM tracks WHERE id=?',(asset['track_id'],)).fetchone()
        return bool(track and evidence.get('status') == 'satisfied' and evidence.get('source') == [track['display_artist'],track['display_title']])
    return False  # A legacy association alone cannot authorize destructive absence.


def association_is_different(con,asset):
    evidence=json.loads(get_state(con,f'youtube_local_evidence:{asset["id"]}','{}'))
    track=con.execute('SELECT display_artist,display_title FROM tracks WHERE id=?',(asset['track_id'],)).fetchone()
    return bool(track and evidence.get('status')=='different' and evidence.get('source')==[track['display_artist'],track['display_title']])
