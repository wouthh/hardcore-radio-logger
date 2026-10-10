"""Recording evidence for local satisfaction; associations alone are not proof."""
from pathlib import Path
import json

from .db import get_state, set_state
from .identity import token_set
from .local_files import inspect_audio_file, _tag_values
from .youtube_matching import compare_recordings


def _plausible_title(left,right):
    a,b=token_set(left),token_set(right)
    return bool(a and b and len(a & b)/max(len(a),len(b))>=.75)


def local_satisfaction(config, con, track, *, require_youtube_id=False, cache=None, persist=False):
    cache = cache if cache is not None else {}
    rows = con.execute("SELECT * FROM youtube_assets WHERE file_exists=1 AND status='downloaded' AND file_path IS NOT NULL ORDER BY track_id=? DESC,id", (track['id'],)).fetchall()
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
        status = 'different'
        if item and item.artist and item.title:
            decision = compare_recordings(track['display_artist'], track['display_title'], item.artist, item.title)
            if decision.accepted:
                status = 'satisfied'
                if tags[0] and tags[1] and not compare_recordings(track['display_artist'],track['display_title'],*tags).accepted:
                    status = 'ambiguous'
            elif decision.reason in {'artist_evidence', 'ambiguous_credits', 'missing_artist'}:
                # Only plausible title evidence can hold unrelated sources.
                a, b = token_set(track['display_title']), token_set(item.title)
                if own or (a and b and len(a & b)/max(len(a),len(b)) >= .75):
                    status = 'ambiguous'
        elif own or (item and _plausible_title(track['display_title'],item.title)):
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
