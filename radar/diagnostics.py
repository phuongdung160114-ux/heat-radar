"""Observed episode and fixed-pool diffusion diagnostics over immutable runs."""
from collections import defaultdict,Counter
from statistics import mean,median
from .common import parse_time

def diagnostics(engine,limit=2000):
    runs=sorted(engine.db.runs(limit),key=lambda r:parse_time(r['as_of']))
    episodes={};sectors=defaultdict(list);snapshots=[]
    for run in runs:
        if run.get('evaluation_type')=='point_in_time_recalculation':continue
        stocks=[x for x in run.get('items',[]) if x.get('kind')=='stock']
        snapshots.append({'run_id':run['run_id'],'at':run['as_of'],'universe':len(stocks),
            'attention_observed':sum(bool(x.get('attention_observed')) for x in stocks),
            'attention_fraction':sum(bool(x.get('attention_observed')) for x in stocks)/len(stocks) if stocks else None})
        for item in run.get('items',[]):
            eid=item.get('episode_id');signal=item.get('signals',{});ev=item.get('evidence',{})
            if eid:
                key=(item['id'],eid)
                row=episodes.setdefault(key,{'id':item['id'],'name':item['name'],'episode_id':eid,'first_detected_at':item.get('first_detected_at'),
                    'first_run_id':run['run_id'],'last_at':run['as_of'],'duration_minutes':0,'lead_minutes':None,'onset_status':None,'groups':[]})
                row['last_at']=run['as_of'];row['duration_minutes']=max(row['duration_minutes'],ev.get('duration_minutes') or 0)
                if signal.get('lead_minutes') is not None:row['lead_minutes']=signal['lead_minutes'];row['onset_status']=signal.get('state')
                group=item.get('selection_group')
                if not row['groups'] or row['groups'][-1]!=group:row['groups'].append(group)
            if item.get('kind') in ('topic','board'):
                m=item.get('member_heat_proxy',{})
                if m.get('known_members'):
                    sectors[item['id']].append({'at':run['as_of'],'run_id':run['run_id'],'name':item['name'],
                        **{k:m.get(k) for k in ('pool_version','pool_basis','known_members','observed_members','coverage','breadth','breadth_lower','breadth_upper','entrants','exits','retention','symbols')}})
    lead=[r['lead_minutes'] for r in episodes.values() if r['lead_minutes'] is not None]
    sector_result=[]
    for oid,rows in sectors.items():
        unique={r['at']:r for r in rows};ordered=[unique[t] for t in sorted(unique)]
        sector_result.append({'id':oid,'name':ordered[-1]['name'],'observations':len(ordered),'history':ordered,
            'positive_entry_windows':sum((r.get('entrants') or 0)>0 for r in ordered),'latest':ordered[-1]})
    return {'synthetic':engine.mode=='demo','frozen_runs':len(runs),'snapshots':snapshots,'episodes':list(episodes.values()),
        'summary':{'episodes':len(episodes),'onset_pairs_observed':len(lead),'median_observed_lead_minutes':median(lead) if lead else None,
            'attention_first_fraction':sum(x>0 for x in lead)/len(lead) if lead else None,
            'median_observed_duration_minutes':median(r['duration_minutes'] for r in episodes.values()) if episodes else None},
        'sectors':sector_result,'definitions':{'lead':'price response onset minus attention onset; requires both actual within-session crossings',
            'episode':'one record per object and episode; no repeated-snapshot multiplication',
            'coverage':'the observed attention subset divided by the frozen full-market stock universe',
            'diffusion':'entrants/exits compare identical frozen membership versions; version changes remain explicit'}}
