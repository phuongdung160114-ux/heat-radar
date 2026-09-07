"""Attention flow derivation, contiguous episodes, and observable onset ordering."""
from __future__ import annotations
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median
import math
from .common import parse_time, stamp, uid, number, CN


def derive_flows(rows: list[dict]) -> list[dict]:
    """Derive signed net follows over an actual 30-minute observation interval."""
    grouped = defaultdict(list)
    for r in rows:
        if r['kind'] == 'snapshot' and r['metric'] == 'followers_total' and r.get('value') is not None:
            key = (r['source'], r['panel_id'], r['pool_version'], tuple(sorted(r['objects'])))
            grouped[key].append(r)
    derived = []
    for series in grouped.values():
        series.sort(key=lambda r: parse_time(r['occurred_at']))
        for index, current in enumerate(series):
            t = parse_time(current['occurred_at']); target = t - timedelta(minutes=30)
            cadence = number(current.get('extra', {}).get('cadence_seconds')) or 900
            available = [r for r in series[max(0,index-12):index] if target-timedelta(seconds=max(300,cadence*.25)) <= parse_time(r['occurred_at']) <= target
                         and parse_time(r['observed_at']) <= parse_time(current['observed_at'])
                         and parse_time(r['occurred_at']).date() == t.date()]
            if not available: continue
            previous = available[-1]; interval = (t-parse_time(previous['occurred_at'])).total_seconds()/60
            extra = {**current.get('extra', {}), 'input_record_ids': [previous['record_id'],current['record_id']],
                     'interval_minutes': interval, 'definition': 'signed net change in observed follow stock',
                     'derived': True, 'measure_type': 'net_flow'}
            derived.append({**current, 'record_id': uid('net-follow-v2',*extra['input_record_ids']),
                'metric': 'followers_net_30', 'window_kind': 'derived_30m', 'unit': 'net_accounts',
                'value': current['value']-previous['value'], 'extra': extra})
    return list(rows) + derived


def count_surprise(value: float, history: list[float], prior_strength: float = 20.0) -> dict:
    """Gamma-Poisson mean shrinkage and an overdispersion-aware descriptive residual."""
    h = [float(v) for v in history if v is not None and v >= 0]
    if not h or value is None or value < 0:
        return {'smoothed_ratio': None, 'count_z': None, 'absolute_excess': None}
    mu = mean(h)
    variance = sum((v-mu)**2 for v in h)/max(1,len(h)-1)
    # A pseudo-count is deliberately explicit and configurable; it is not a fitted coefficient.
    return {'smoothed_ratio': (value+prior_strength)/(mu+prior_strength),
            'count_z': (value-mu)/math.sqrt(max(mu,variance,1.0)*(1+1/len(h))),
            'absolute_excess': value-mu, 'baseline_mean': mu, 'baseline_variance': variance,
            'prior_count': prior_strength}


def enrich_series(e: dict, series: list[dict], at, calendar, cfg, known_at) -> dict:
    t = parse_time(e['at']); day = t.astimezone(CN).date(); metric=e['metric']
    historical = defaultdict(list)
    for r in series:
        d=parse_time(r['occurred_at']).astimezone(CN)
        if d.date()<day and parse_time(r['observed_at'])<=known_at:
            historical[(d.hour,d.minute//5)].append((d.date().isoformat(),r['value']))
    expected=set(calendar.open_dates(e.get('market','A'),before=day.isoformat(),limit=20))
    def baseline(dt):
        pairs=historical.get((dt.hour,dt.minute//5),[])
        h={date:value for date,value in pairs if date in expected}
        return list(h.values())
    current_hist=baseline(t.astimezone(CN))
    if metric!='rank' and e.get('eligible'):
        e.update(count_surprise(e.get('value'),current_hist,cfg.get('count_prior',20)))
    points=[]
    intraday=[r for r in series if parse_time(r['occurred_at']).astimezone(CN).date()==day]
    for r in intraday:
        dt=parse_time(r['occurred_at']).astimezone(CN);value=r['value'];h=baseline(dt)
        q=value/mean(h) if h and mean(h)>0 else None
        if metric=='rank':
            pct=1-(sum(v<value for v in h)+.5*sum(v==value for v in h))/len(h) if h else None
            hot=value<=cfg.get('top_rank',20) or (pct is not None and pct>=.8 and mean(h)>value)
            score=-math.log(max(value,.5))
        else:
            hot=q is not None and q>=cfg.get('ratio20',1.5)
            score=math.log(max(q,1e-8)) if q is not None else None
        points.append({'at':r['occurred_at'],'observed_at':r['observed_at'],'value':value,'q':q,'score':score,'hot':bool(hot),'record_id':r['record_id']})
    # Identical observation times are one point, regardless of repeated calculations.
    points=list({p['at']:p for p in points}.values())
    points.sort(key=lambda p:parse_time(p['at']))
    cadence=number(series[-1].get('extra',{}).get('cadence_seconds')) or 300
    run=[]
    for point in points:
        if not point['hot']: run=[]; continue
        if run and (parse_time(point['at'])-parse_time(run[-1]['at'])).total_seconds()>max(900,cadence*2.5): run=[]
        run.append(point)
    left_censored=True
    if run:
        first_index=points.index(run[0])
        if first_index>0:
            before=points[first_index-1]
            gap=(parse_time(run[0]['at'])-parse_time(before['at'])).total_seconds()
            left_censored=before['hot'] or gap>max(900,cadence*2.5)
    duration=(parse_time(run[-1]['at'])-parse_time(run[0]['at'])).total_seconds()/60 if run else 0.0
    auc=0.0
    for a,b in zip(run,run[1:]):
        if a['q'] is not None and b['q'] is not None:
            auc += max(0,(a['q']+b['q'])/2-1)*(parse_time(b['at'])-parse_time(a['at'])).total_seconds()/60
    validq=[p['q'] for p in run if p['q'] is not None]
    e.update(observation_windows=len(run),duration_minutes=duration,onset_at=run[0]['at'] if run else None,
        onset_observed_at=run[0]['observed_at'] if run else None,onset_left_censored=left_censored,
        excess_attention_auc=auc,peak_q=max(validq) if validq else None,
        peak_retention=validq[-1]/max(validq) if validq and max(validq)>0 else None,
        trace=points[-300:],list_state='observed',normalization_version='aligned-count-v2')
    # Keep the legacy two-window field while publishing the actual episode length independently.
    e['measure_type']=series[-1].get('extra',{}).get('measure_type',
        'rank' if metric=='rank' else ('stock' if metric=='followers_total' else ('share' if metric.endswith('_share') else 'index')))
    return e


def onset_state(attention, quote_records, object_id, threshold=.01) -> dict:
    """Use confirmed transitions in two time series; one static point has no ordering."""
    eligible=[e for e in attention if e.get('eligible') and not e.get('stale') and e.get('onset_at') and not e.get('onset_left_censored',True)]
    a=min((e['onset_at'] for e in eligible),key=parse_time,default=None)
    if not object_id.startswith('stock:'):
        return {'attention_onset_at':a,'response_onset_at':None,'lead_minutes':None,'state':'关注已启动' if a else '启动时刻待形成'}
    groups=defaultdict(dict)
    for r in quote_records:
        if r['metric'] not in ('quote','market_amount') or r['window_kind']!='regular_cumulative':continue
        dt=parse_time(r['occurred_at']).astimezone(CN)
        key=(r['source'],dt.date().isoformat(),dt.hour,dt.minute//5)
        for oid in r['objects']:groups[key][oid]=r
    points=[]
    for key,rs in sorted(groups.items(),key=lambda kv:kv[0][1:]):
        own,market=rs.get(object_id),rs.get('market:A')
        if not own or not market:continue
        value=own['extra'].get('return_decimal');benchmark=market['extra'].get('equal_weight_return')
        if value is None or benchmark is None:continue
        points.append((own['occurred_at'],value-benchmark,key[0]))
    p=None
    for previous,current in zip(points,points[1:]):
        gap=(parse_time(current[0])-parse_time(previous[0])).total_seconds()
        if current[2]==previous[2] and previous[1]<=threshold<current[1] and 0<gap<=1800:p=current[0];break
    lead=(parse_time(p)-parse_time(a)).total_seconds()/60 if a and p else None
    state=('关注先行' if lead>0 else ('价格先行' if lead<0 else '同窗启动')) if lead is not None else ('关注已启动 · 价格待确认' if a else ('价格已启动 · 关注待确认' if p else '启动时刻待形成'))
    return {'attention_onset_at':a,'response_onset_at':p,'lead_minutes':lead,'state':state,
            'attention_series_count':len(eligible),'price_points':len(points),'onset_definition':'observed_low_to_high_transition_v2'}


def episode_summary(item, previous_runs, at) -> dict:
    """Episode continuity uses real evidence timestamps, not the number of service calls."""
    traces=[e for e in item.get('attention',[]) if e.get('eligible') and not e.get('stale')]
    current=max((float(e.get('duration_minutes') or 0) for e in traces),default=0)
    onset=min((e['onset_at'] for e in traces if e.get('onset_at')),key=parse_time,default=None)
    history=[]
    for run in previous_runs:
        row=next((x for x in run.get('items',[]) if x['id']==item['id']),None)
        if row and row.get('episode_id')==item.get('episode_id'):history.append((run,row))
    evidence_times={item.get('last_evidence_at')}
    evidence_times.update(x.get('last_evidence_at') for _,x in history)
    evidence_times.discard(None)
    days={parse_time(t).date().isoformat() for t in evidence_times}
    participant=[e for e in traces if e.get('basis')=='participant_id']
    return {'onset_at':onset,'duration_minutes':current,'observed_sessions':len(days),
        'observation_windows':max((e.get('observation_windows',0) for e in traces),default=0),
        'distinct_evidence_times':len(evidence_times),
        'excess_attention_auc':max((e.get('excess_attention_auc',0) for e in traces),default=0),
        'new_participants':sum(e.get('new_today',0) for e in participant),
        'effective_authors':sum(e.get('effective_authors') or 0 for e in participant) if participant else None,
        'onset_left_censored':all(e.get('onset_left_censored',True) for e in traces)}
