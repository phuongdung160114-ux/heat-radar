"""Point-in-time full-market scan and attention-independent stratified controls."""
from __future__ import annotations
from collections import defaultdict
from datetime import timedelta
from .common import uid, parse_time, stamp, number


def stratified_controls(objects, quotes, at, n=30):
    lookup={o['id']:o for o in objects if o['kind']=='stock'};values={}
    for r in quotes:
        for oid in r['objects']:
            if oid in lookup:values[oid]=r.get('extra',{})
    caps=sorted(number(values.get(s,{}).get('float_cap_cny')) or number(o.get('float_cap_cny')) or 0 for s,o in lookup.items())
    cuts=[caps[len(caps)//3],caps[2*len(caps)//3]] if caps else [0,0]
    groups=defaultdict(list)
    for oid,obj in lookup.items():
        q=values.get(oid,{});cap=number(q.get('float_cap_cny')) or number(obj.get('float_cap_cny')) or 0
        size=sum(cap>cut for cut in cuts);ret=number(q.get('return_decimal'))
        move='unknown' if ret is None else ('up' if ret>.01 else ('down' if ret<-.01 else 'flat'))
        group=(obj.get('industry') or 'unclassified',size,move)
        groups[group].append(oid)
    for ids in groups.values():ids.sort(key=lambda s:uid(at.date().isoformat(),s))
    keys=sorted(groups,key=lambda g:uid(at.date().isoformat(),g));sample=[]
    # Round-robin strata are frozen before deep-scan eligibility is considered.
    while len(sample)<n:
        advanced=False
        for key in keys:
            if groups[key] and len(sample)<n:sample.append(groups[key].pop(0));advanced=True
        if not advanced:break
    return sample


def candidate_plan(engine,at):
    objects={o['id']:o for o in engine.db.objects(as_of=at)}
    contents=engine.db.read(kind='content',start=at-timedelta(days=2),end=at+timedelta(microseconds=1),as_of=at)
    activity={};new=set();rank={}
    for r in engine.db.candidate_summary(at-timedelta(days=7),at):
        oid=r['object_id'];activity[oid]=r['activity']
        if r.get('last_rank') is not None:
            rank[oid]=min(rank.get(oid,1e9),r['last_rank'])
            if r['source']=='em_up' and at.timestamp()-r['latest_rank_at']<86400:new.add(oid)
    universe={oid for oid,o in objects.items() if o['kind']=='stock' and o.get('market','A')=='A' and o.get('universe_member',True)}
    themes={oid for oid,o in objects.items() if o['kind'] in ('topic','board')}
    members=engine.db.all_members(at);watch=set(engine.settings.data.get('watchlist',[]))
    ids=universe|themes|(watch&set(objects))
    ordered=sorted(ids,key=lambda oid:(oid not in watch,oid not in new,rank.get(oid,1e9),-activity.get(oid,0),oid))
    quotes=engine.db.read(kind='quote',metric='quote',start=at-timedelta(minutes=30),end=at+timedelta(seconds=1),as_of=at)
    cap=engine.settings.data.get('max_candidates',350)
    controls=stratified_controls([objects[s] for s in sorted(universe)],quotes,at,n=min(30,max(1,cap//10)))
    related={e['symbol'] for e in members if e['relation']!='narrative'}
    deep=[]
    for group,quota in [(sorted(watch&ids),40),([x for x in ordered if x in new],cap//4),
        ([x for x in ordered if x in themes],cap//5),([x for x in ordered if x in related],cap//4)]:
        for oid in group:
            if len(deep)>=cap-len(controls) or quota<=0:break
            if oid not in deep:deep.append(oid);quota-=1
    for oid in ordered:
        if len(deep)>=cap-len(controls):break
        if oid not in deep:deep.append(oid)
    for oid in controls:
        if oid not in deep and len(deep)<cap:deep.append(oid)
    deepset=set(deep);controlset=set(controls)
    manifest=[{'id':s,'selected':True,'deep_selected':s in deepset,'control':s in controlset,
        'route':'watch' if s in watch else ('newborn' if s in new else ('business' if s in related else ('market' if s in universe else 'topic'))),
        'reason':'deep_analysis' if s in deepset else 'full_market_light_panel','attention_observation':'pending'} for s in ordered]
    return [objects[s] for s in ordered],{'discovered':len(ordered),'analyzed':len(ordered),'market_stocks':len(universe),
        'deep_analyzed':len(deep),'cap':cap,'deep_ids':deep,'controls':controls,'manifest':manifest,'pool_version':uid(sorted(ids)),
        'universe_version':uid(sorted(universe)),'universe_origin':'security_master' if any(objects[s].get('universe_member') for s in universe) else 'known_object_directory',
        'control_method':'industry-size-current-price stratified deterministic sample','note':''},contents
