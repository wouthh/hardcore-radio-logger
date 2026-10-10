"""Small persistent YouTube queue. Counts start at queue initialization."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .db import get_state, set_state

LANES = ('first_search', 'search_retry', 'download')
BACKOFF_SECONDS = (900, 3600, 14400, 43200, 86400)


def source_fingerprint(track):
    return hashlib.sha256(json.dumps([track['display_artist'],track['display_title']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def stamp(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')


def decision_json(value):
    # Keep structured diagnostic evidence, trimming text fields rather than JSON.
    def bound(item):
        if isinstance(item,str):
            return item[:1200]
        if isinstance(item,dict):
            return {str(k):bound(v) for k,v in item.items()}
        if isinstance(item,(tuple,list)):
            return [bound(v) for v in item[:10]]
        return item
    value = bound(value)
    encoded = json.dumps(value,ensure_ascii=False,separators=(',',':'))
    if len(encoded.encode()) > 12288:
        encoded = json.dumps({'outcome':value.get('outcome'),'reason':'decision evidence exceeded bound'},separators=(',',':'))
    return encoded


def save_schedule(con, track_id, now, **fields):
    fields['updated_at'] = stamp(now)
    con.execute('UPDATE youtube_schedule SET '+','.join(f'{key}=?' for key in fields)+' WHERE track_id=?',(*fields.values(),track_id))


def advance(con, lane):
    set_state(con,'youtube_queue_cursor',str((lane+1)%3))


def choose(con, now):
    cursor = int(get_state(con,'youtube_queue_cursor','0')) % 3
    for offset in range(3):
        lane = (cursor+offset)%3
        row = con.execute('''SELECT q.*,t.display_artist,t.display_title,t.status
            FROM youtube_schedule q JOIN tracks t ON t.id=q.track_id
            WHERE q.phase=? AND q.next_eligible_at<=? AND t.status='wanted'
            ORDER BY q.next_eligible_at,COALESCE(CASE WHEN q.phase='download' THEN q.last_download_at ELSE q.last_search_at END,''),q.track_id LIMIT 1''',(LANES[lane],stamp(now))).fetchone()
        if row:
            return lane, row
    return None, None


def pause_state(con):
    return json.loads(get_state(con,'youtube_provider_pause','{}'))


def pause_active(con, now):
    state = pause_state(con)
    return state if state.get('operator_required') or (state.get('until') and state['until']>stamp(now)) else None


def succeeded(con):
    set_state(con,'youtube_provider_pause','{}')
    set_state(con,'youtube_transport_failures','[]')


def failed(con, row, lane, failure, now):
    category = failure.category
    downloading = lane == 2
    attempt_field = 'download_attempts' if downloading else 'search_attempts'
    streak_field = 'download_failures' if downloading else 'search_failures'
    time_field = 'last_download_at' if downloading else 'last_search_at'
    streak = row[streak_field]+1
    seconds = BACKOFF_SECONDS[min(streak-1,len(BACKOFF_SECONDS)-1)]
    fields = {attempt_field:row[attempt_field]+1,streak_field:streak,time_field:stamp(now)}
    if not downloading:
        fields['phase'] = 'search_retry'
    state = pause_state(con)
    provider = category in {'restriction','missing_tool','configuration'}
    if category == 'transport':
        history = json.loads(get_state(con,'youtube_transport_failures','[]'))
        cutoff = stamp(now-timedelta(minutes=30))
        history = [item for item in history if item['at']>=cutoff]
        history.append({'track_id':row['track_id'],'at':stamp(now)})
        set_state(con,'youtube_transport_failures',json.dumps(history[-2:]))
        provider = len({item['track_id'] for item in history})>=2 or state.get('kind')=='transport'
    if provider:
        previous = state.get('streak',0) if state.get('kind')==category else 0
        if category == 'restriction':
            hours = (12,24,48,72)[min(previous,3)]
        else:
            hours = (1,2,4,6)[min(previous,3)]
        until = stamp(now+timedelta(hours=hours))
        if failure.retry_at and failure.retry_at>until:
            until=failure.retry_at
        set_state(con,'youtube_provider_pause',json.dumps({'kind':category,'until':until,'streak':previous+1,'operator_required':category in {'missing_tool','configuration'},'estimated':not bool(failure.retry_at),'reason':failure.detail}))
        fields['next_eligible_at'] = until
        # Provider restrictions are not candidate-specific failure evidence.
        fields[streak_field] = row[streak_field]
    else:
        fields['next_eligible_at'] = stamp(now+timedelta(seconds=seconds))
    if category in {'video_unavailable','archive_inconsistent'} or (downloading and not provider and streak>=5):
        fields.update(candidate_json=None,candidate_at=None,phase='search_retry',next_eligible_at=stamp(now+timedelta(hours=24)))
        con.execute("UPDATE youtube_pending_work SET state='cancelled',updated_at=? WHERE track_id=? AND state NOT IN ('completed','cancelled')",(stamp(now),row['track_id']))
        if category=='archive_inconsistent':
            fields.update(phase='held',hold_origin='archive_inconsistent')
    fields['decision_json'] = decision_json({'outcome':'failure','category':category,'reason':failure.detail,'next_eligible_at':fields['next_eligible_at'],'candidate_reused':downloading})
    save_schedule(con,row['track_id'],now,**fields)
    advance(con,lane)
