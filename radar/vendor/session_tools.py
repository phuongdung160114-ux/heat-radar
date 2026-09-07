#!/usr/bin/env python3
"""Session-aware utilities for collected observations; Python 3.10+ standard library."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import re
from statistics import mean, median
import tempfile
import time as clock
from typing import Any
from zoneinfo import ZoneInfo

PHASES = ('premarket', 'intraday', 'postmarket')
COHORTS = {'continuous', 'returning', 'new_in_sample', 'unknown_history'}
SIGNATURE = ('object_id', 'market', 'platform', 'panel_id', 'entity_type',
             'metric', 'unit', 'window_kind', 'start_key', 'cutoff_key', 'pool_version')


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include an offset: ' + value)
    return result


def numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('Boolean cannot represent an observation')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Observation must be finite')
    return result


def ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b is not None and b > 0 else None


def local_at(session: dict, hhmm: str) -> datetime:
    return datetime.combine(date.fromisoformat(session['date']), time.fromisoformat(hhmm),
                            ZoneInfo(session['timezone']))


def regular_intervals(session: dict) -> list[tuple[datetime, datetime]]:
    spans = [(local_at(session, a), local_at(session, b)) for a, b in session.get('regular', [])]
    spans.sort()
    if any(b <= a for a, b in spans):
        raise ValueError('Regular intervals must have positive duration on the local date')
    if any(spans[i][0] < spans[i-1][1] for i in range(1, len(spans))):
        raise ValueError('Regular intervals overlap')
    return spans


def window(a: datetime | None, b: datetime | None) -> dict | None:
    if a is None or b is None or b <= a:
        return None
    return {'start': a.isoformat(), 'end': b.isoformat(),
            'minutes': (b-a).total_seconds()/60}


def clip_spans(spans: list, a: datetime, b: datetime) -> list:
    return [(max(x, a), min(y, b)) for x, y in spans if min(y, b) > max(x, a)]


def tail_spans(spans: list, end: datetime, minutes: int) -> list:
    remaining = float(minutes * 60)
    result = []
    for a, b in reversed(spans):
        b = min(b, end)
        if b <= a or remaining <= 0:
            continue
        n = min(remaining, (b-a).total_seconds())
        result.append((b-timedelta(seconds=n), b))
        remaining -= n
    return list(reversed(result))


def plan_windows(calendar: dict, phase: str, as_of: str, session_date: str | None = None,
                 last_post_as_of: str | None = None, history_limit: int = 60) -> dict:
    if phase not in PHASES:
        raise ValueError('Unknown phase')
    if history_limit <= 0:
        raise ValueError('history_limit must be positive')
    now = instant(as_of)
    sessions = calendar.get('sessions', [])
    keys = [(s['market'], s['date']) for s in sessions]
    if len(keys) != len(set(keys)):
        raise ValueError('Calendar requires a unique market/date entry')
    active = [s for s in sessions if s.get('is_open', True) and regular_intervals(s)]
    a_days = sorted((s for s in active if s['market'] == 'A'), key=lambda s: s['date'])
    if not a_days:
        raise ValueError('Calendar has no A-share sessions')
    if session_date:
        matches = [s for s in a_days if s['date'] == session_date]
    elif phase == 'premarket':
        matches = [s for s in a_days if regular_intervals(s)[0][0] >= now]
        matches = matches[:1]
    elif phase == 'postmarket':
        matches = [s for s in a_days if regular_intervals(s)[-1][1] <= now][-1:]
    else:
        matches = [s for s in a_days
                   if date.fromisoformat(s['date']) == now.astimezone(ZoneInfo(s['timezone'])).date()]
        if not matches:
            matches = [s for s in a_days if regular_intervals(s)[-1][1] <= now][-1:]
    if not matches:
        raise ValueError('Calendar does not cover the requested target session')
    target = matches[0]
    spans = regular_intervals(target)
    opening, closing = spans[0][0], spans[-1][1]
    previous = [s for s in a_days if s['date'] < target['date']]
    prev = previous[-1] if previous else None
    prev_close = regular_intervals(prev)[-1][1] if prev else None
    if phase == 'postmarket' and now < closing:
        raise ValueError('Postmarket target has not completed regular trading')
    if phase == 'premarket' and now > opening:
        raise ValueError('Premarket cutoff is after the target regular open')
    if now < opening:
        status = 'before_regular_open'
    elif now >= closing:
        status = 'regular_completed'
    elif any(a <= now < b for a, b in spans):
        status = 'regular_trading'
    else:
        status = 'regular_break'
    same_date = now.astimezone(opening.tzinfo).date() == opening.date()
    if phase == 'intraday' and not same_date:
        status = 'non_session_day_last_completed_session'
    main_end = min(now, closing)
    active_spans = clip_spans(spans, opening, main_end)
    result = {
        'phase': phase, 'as_of': now.isoformat(), 'target_session': target['date'],
        'previous_a_session': prev['date'] if prev else None,
        'calendar_version': calendar.get('calendar_version'), 'session_status': status,
        'regular_info_window': window(opening, main_end),
        'regular_trading_spans': [window(a, b) for a, b in active_spans],
        'elapsed_regular_trading_minutes': sum((b-a).total_seconds()/60 for a, b in active_spans),
        'calendar_day_information': window(local_at(target, '00:00'), now) if same_date else None,
        'preopen_information': window(prev_close, min(now, opening)),
        'postclose_information': window(closing, now),
        'target_extra_stages': [], 'rolling_information': [], 'rolling_trading': [],
        'same_clock_history': [],
    }
    for stage in target.get('stages', []):
        a, b = local_at(target, stage['start']), local_at(target, stage['end'])
        result['target_extra_stages'].append({'name': stage['name'],
            'scheduled': window(a, b), 'observed': window(a, min(b, now)), 'complete': now >= b})
    if phase == 'intraday' and main_end > opening:
        for n in (15, 30, 60):
            a = max(opening, main_end-timedelta(minutes=n))
            prior_end = a
            prior_start = max(opening, prior_end-timedelta(minutes=n))
            result['rolling_information'].append({'requested_minutes': n,
                'current': window(a, main_end), 'previous': window(prior_start, prior_end),
                'complete': (main_end-a).total_seconds() >= n*60,
                'previous_complete': (prior_end-prior_start).total_seconds() >= n*60})
            pieces = tail_spans(spans, main_end, n)
            observed = sum((b-a).total_seconds()/60 for a, b in pieces)
            result['rolling_trading'].append({'requested_minutes': n,
                'pieces': [window(a,b) for a,b in pieces], 'observed_minutes': observed,
                'complete': observed >= n})
    if phase in ('intraday', 'postmarket'):
        cutoff = main_end.astimezone(opening.tzinfo).strftime('%H:%M:%S')
        for hist in previous[-history_limit:]:
            hs = regular_intervals(hist)
            h_end = min(local_at(hist, cutoff), hs[-1][1])
            pieces = clip_spans(hs, hs[0][0], h_end)
            result['same_clock_history'].append({'date': hist['date'], 'cutoff_key': cutoff,
                'window': window(hs[0][0], h_end),
                'schedule_matches': hist['regular'] == target['regular'],
                'trading_minutes': sum((b-a).total_seconds()/60 for a,b in pieces)})
    bridge_end = now
    result['overseas_bridge'] = window(prev_close, bridge_end) if phase == 'premarket' else None
    if last_post_as_of:
        prior_cutoff = instant(last_post_as_of)
        if prior_cutoff > now:
            raise ValueError('Previous report cutoff is after this run')
        result['new_since_postmarket'] = window(max(prior_cutoff, prev_close) if prev_close else prior_cutoff, now)
    us = sorted((s for s in active if s['market'] == 'US'), key=lambda s: s['date'])
    us_completed = [s for s in us if regular_intervals(s)[-1][1] <= now]
    result['latest_us_completed_regular'] = us_completed[-1]['date'] if us_completed else None
    result['us_history_sessions'] = [s['date'] for s in us_completed[-history_limit:]]
    result['us_bridge_stages'] = []
    if phase == 'premarket' and prev_close:
        for s in us:
            stages = [('regular', a,b) for a,b in regular_intervals(s)]
            stages += [(r['name'],local_at(s,r['start']),local_at(s,r['end'])) for r in s.get('stages', [])]
            for name,a,b in stages:
                observed = window(max(a,prev_close),min(b,now))
                if observed:
                    result['us_bridge_stages'].append({'us_date':s['date'], 'stage':name,
                        'scheduled':window(a,b), 'observed':observed, 'complete':now >= b,
                        'beijing_start':instant(observed['start']).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
                        'beijing_end':instant(observed['end']).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()})
        result['us_bridge_stages'].sort(key=lambda r: instant(r['observed']['start']))
    return result


def aggregate_window(records: list[dict], start: str, end: str, as_of: str,
                     day_start: str | None = None, slot_minutes: int = 30,
                     parents: dict | None = None) -> dict:
    left, right, known = instant(start), instant(end), instant(as_of)
    beginning = instant(day_start) if day_start else None
    if right <= left or right > known or slot_minutes <= 0:
        raise ValueError('Require start < end <= as_of and positive slot_minutes')
    if beginning is not None and beginning > left:
        raise ValueError('day_start must not be later than the window start')
    current, prior = defaultdict(list), defaultdict(set)
    seen = set()
    for r in records:
        if not r.get('entity_id') or r['entity_id'] in ('[deleted]', '[removed]'):
            continue
        t = instant(r['occurred_at'])
        if instant(r['observed_at']) > known or t >= right:
            continue
        if t < (beginning if beginning is not None else left):
            continue
        base = (r['platform'],r['panel_id'],r['entity_type'])
        for obj, relation in r['objects'].items():
            if relation not in ('explicit','context'):
                continue
            key = base+(obj,)
            token = key+(r['record_id'],r['entity_id'])
            if token in seen:
                continue
            seen.add(token)
            if t < left:
                prior[key].add(r['entity_id'])
            else:
                current[key].append((r,relation,int(t.timestamp())//(slot_minutes*60)))
    explicit_sets = {k:{r['entity_id'] for r,rel,_ in rows if rel=='explicit'} for k,rows in current.items()}
    out = []
    for key, rows in sorted(current.items()):
        explicit = explicit_sets[key]
        context = {r['entity_id'] for r,rel,_ in rows if rel=='context'}-explicit
        actors = explicit|context
        slots = {(r['entity_id'], slot) for r,_,slot in rows}
        counts = Counter(actor for actor,_ in slots)
        hhi = sum((n/len(slots))**2 for n in counts.values()) if slots else None
        labels, circles = defaultdict(set), defaultdict(set)
        for r,_,_ in rows:
            label = (r.get('cohort') or {}).get(key[-1])
            if label in COHORTS:
                labels[r['entity_id']].add(label)
            for circle in r.get('circles',[]):
                circles[circle].add(r['entity_id'])
        cohort_counts = Counter()
        for actor in actors:
            cohort = labels[actor]
            if len(cohort)>1:
                raise ValueError('Conflicting cohort labels for '+actor)
            cohort_counts[next(iter(cohort)) if cohort else 'unlabelled'] += 1
        parent=(parents or {}).get(key[-1])
        parent_key=key[:-1]+(parent,)
        ps=explicit_sets.get(parent_key) if parent else None
        out.append({'platform':key[0], 'panel_id':key[1], 'entity_type':key[2], 'object_id':key[3],
            'explicit_entities':len(explicit), 'context_only_entities':len(context),
            'all_related_entities':len(actors), 'observed_behaviors':len(rows),
            'entity_slots':len(slots), 'slots_per_entity':ratio(len(slots),len(actors)),
            'new_today_in_window':len(actors-prior[key]) if beginning else None,
            'cohorts':dict(cohort_counts), 'circles':{k:len(v) for k,v in circles.items()},
            'effective_sources_by_slots':1/hhi if hhi else None,
            'parent_object':parent, 'parent_entities':len(ps) if ps is not None else None,
            'topic_in_parent_entities':len(explicit&ps) if ps is not None else None,
            'parent_share':ratio(len(explicit&ps),len(ps)) if ps is not None else None})
    return {'start':left.isoformat(),'end':right.isoformat(),'as_of':known.isoformat(),
            'interval':'[start,end)', 'minutes':(right-left).total_seconds()/60,'groups':out}


def compare_snapshot(data: dict) -> dict:
    current=data['current']
    signature=tuple(current.get(k) for k in SIGNATURE)
    today=date.fromisoformat(current['date'])
    value=numeric(current.get('value'))
    knowledge=instant(data['as_of']) if data.get('as_of') else None
    supplied_dates=data.get('expected_history_dates')
    expected=set(supplied_dates or [])
    if any(date.fromisoformat(d)>=today for d in expected):
        raise ValueError('Expected history dates must precede the current date')
    selected={}
    excluded=Counter()
    for row in data.get('history',[]):
        if tuple(row.get(k) for k in SIGNATURE)!=signature:
            excluded['different_signature']+=1
            continue
        if date.fromisoformat(row['date'])>=today:
            excluded['not_prior_date']+=1
            continue
        if supplied_dates is not None and row['date'] not in expected:
            excluded['outside_requested_dates']+=1
            continue
        if supplied_dates is None:
            expected.add(row['date'])
        if not row.get('complete', True) or numeric(row.get('value')) is None:
            excluded['incomplete_or_missing']+=1
            continue
        stamp=instant(row['observed_at']) if row.get('observed_at') else None
        if knowledge and stamp and stamp>knowledge:
            excluded['not_yet_observed']+=1
            continue
        prev=selected.get(row['date'])
        if prev is not None:
            prev_stamp=instant(prev['observed_at']) if prev.get('observed_at') else None
            if stamp and prev_stamp and stamp<prev_stamp:
                continue
            if stamp==prev_stamp and row!=prev:
                raise ValueError('Conflicting same-date observation versions')
        selected[row['date']]=row
    dates=sorted(expected)
    values=[numeric(selected[d]['value']) if d in selected else None for d in dates]
    observed=[v for v in values if v is not None]
    comparisons={}
    for n in (1,3,5,20):
        positions=values[-n:]
        sample=[v for v in positions if v is not None]
        b=mean(sample) if sample else None
        comparisons[str(n)]={'requested':n,'n':len(sample),
            'periods_present':len(positions),
            'complete':len(positions)==n and len(sample)==n,
            'missing_dates':[d for d,v in zip(dates[-n:],positions) if v is None],
            'mean':b,'median':median(sample) if sample else None,'ratio':ratio(value,b),
            'absolute_change':value-b if value is not None and b is not None else None}
    positions={}
    for n in (60,250):
        periods=values[-n:]
        sample=[v for v in periods if v is not None]
        rank=((sum(x<value for x in sample)+0.5*sum(x==value for x in sample))/len(sample)
              if sample and value is not None else None)
        positions[str(n)]={'requested':n,'n':len(sample),
            'periods_present':len(periods), 'complete':len(periods)==n and len(sample)==n,
            'percentile':rank}
    sequence=values+[value]
    recent=sequence[-3:]
    base=sequence[-23:-3]
    rm=mean(recent) if len(recent)==3 and all(v is not None for v in recent) else None
    bm=mean(base) if len(base)==20 and all(v is not None for v in base) else None
    peak=data.get('previous_cycle_peak')
    cycle=None
    if peak:
        if date.fromisoformat(peak['date'])>=today:
            raise ValueError('Previous cycle peak must precede the current date')
        cycle={'date':peak['date'],'value':numeric(peak['value']), 'definition':peak.get('definition'),
               'fraction_of_peak':ratio(value,numeric(peak['value']))}
    return {'current':current,'comparison_signature':dict(zip(SIGNATURE,signature)),
            'prior_dates_expected':dates,'prior_dates_used':[d for d in dates if d in selected],
            'excluded':dict(excluded), 'prior_windows':comparisons,'historical_position':positions,
            'g_3_20':{'recent_mean':rm,'baseline_mean':bm,'ratio':ratio(rm,bm),
                      'complete':rm is not None and bm is not None},
            'observed_history_max':max(observed) if observed else None,'previous_cycle_peak':cycle}


def read_jsonl(path: str) -> list[dict]:
    rows=[]
    for n,line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(),1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f'{path}:{n}: {exc}') from exc
    return rows


def write_json(path: str | Path, obj: Any) -> None:
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=target.parent,delete=False) as f:
        json.dump(obj,f,ensure_ascii=False,indent=2,allow_nan=False)
        f.write('\n')
        name=f.name
    os.replace(name,target)


@contextmanager
def state_lock(root: Path):
    lock=root/'.session-state-lock'
    until=clock.monotonic()+10
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if clock.monotonic()>until:
                raise ValueError('State is in use; retry after the other writer finishes')
            clock.sleep(0.05)
    try:
        yield
    finally:
        lock.rmdir()


def save_handoff(root: str | Path, run: dict) -> dict:
    if run.get('phase') not in PHASES:
        raise ValueError('Handoff requires a known phase')
    run_id=run['run_id']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',run_id):
        raise ValueError('run_id must use letters, numbers, dash, dot or underscore')
    stamp=instant(run['as_of'])
    date.fromisoformat(run['target_session'])
    path=Path(root)
    path.mkdir(parents=True,exist_ok=True)
    with state_lock(path):
        dest=path/'runs'/f'{run_id}.json'
        if dest.exists() and json.loads(dest.read_text(encoding='utf-8'))!=run:
            raise ValueError('run_id already holds a different run; use a new run_id')
        write_json(dest,run)
        state_path=path/'state.json'
        state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        state['schema_version']='session-radar-v2'
        phase_state=state.setdefault('phases',{})
        old=phase_state.get(run['phase'])
        if not old or instant(old['as_of'])<=stamp:
            phase_state[run['phase']]={'run_id':run_id,'as_of':run['as_of'],
                'target_session':run['target_session'],'path':str(dest.relative_to(path))}
        state['latest_by_session']=state.get('latest_by_session',{})
        day=state['latest_by_session'].setdefault(run['target_session'],{})
        old_day=day.get(run['phase'])
        if not old_day or instant(old_day['as_of'])<=stamp:
            day[run['phase']]={'run_id':run_id,'as_of':run['as_of'],'path':str(dest.relative_to(path))}
        write_json(state_path,state)
    return {'run_path':str(dest),'state_path':str(state_path),'phase':run['phase']}


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('plan')
    p.add_argument('--calendar',required=True)
    p.add_argument('--phase',choices=PHASES,required=True)
    p.add_argument('--as-of',required=True)
    p.add_argument('--session-date')
    p.add_argument('--last-post-as-of')
    p.add_argument('--history-limit',type=int,default=60)
    p.add_argument('--out',required=True)
    p=subs.add_parser('window')
    p.add_argument('--input',required=True)
    p.add_argument('--start',required=True)
    p.add_argument('--end',required=True)
    p.add_argument('--as-of',required=True)
    p.add_argument('--day-start')
    p.add_argument('--slot-minutes',type=int,default=30)
    p.add_argument('--config')
    p.add_argument('--out',required=True)
    p=subs.add_parser('compare')
    p.add_argument('--input',required=True)
    p.add_argument('--out',required=True)
    p=subs.add_parser('handoff')
    p.add_argument('--input',required=True)
    p.add_argument('--state-dir',required=True)
    args=parser.parse_args()
    try:
        if args.command=='plan':
            result=plan_windows(json.loads(Path(args.calendar).read_text(encoding='utf-8')),
                args.phase,args.as_of,args.session_date,args.last_post_as_of,args.history_limit)
        elif args.command=='window':
            config=json.loads(Path(args.config).read_text(encoding='utf-8')) if args.config else {}
            result=aggregate_window(read_jsonl(args.input),args.start,args.end,args.as_of,
                args.day_start,args.slot_minutes,config.get('parents'))
        elif args.command=='compare':
            result=compare_snapshot(json.loads(Path(args.input).read_text(encoding='utf-8')))
        else:
            result=save_handoff(args.state_dir,json.loads(Path(args.input).read_text(encoding='utf-8')))
        if args.command=='handoff':
            print(json.dumps(result,ensure_ascii=False,indent=2))
        else:
            write_json(args.out,result)
    except (ValueError,KeyError,OSError) as exc:
        parser.exit(2,str(exc)+'\n')


if __name__=='__main__':
    main()
