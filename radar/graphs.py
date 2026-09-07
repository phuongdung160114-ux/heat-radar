"""Separate economic exposure and observed attention propagation networks."""
from __future__ import annotations
from collections import defaultdict, Counter
from statistics import mean
from itertools import combinations
import math
from .common import number, parse_time, uid

STAGE_STRENGTH = {'research':.15,'研发':.15,'sample':.3,'送样':.3,'validation':.45,'验证':.45,
    'demonstrated':.4,'已展示':.4,'产品展示':.4,'展示方案':.4,'产品方案':.45,
    '产品':.6,'product':.6,'small_batch':.7,'小批量':.7,'量产':.85,'mass_production':.85,
    'commercial':.85,'revenue':1.0,'形成收入':1.0,'主营收入':1.0}
RELATION_STRENGTH={'direct_business':1.0,'industry_transmission':.55,'narrative':.0}


def edge_exposure(edge: dict) -> dict:
    relation=RELATION_STRENGTH.get(edge.get('relation'),0)
    stage=edge.get('commercial_stage','');stage_weight=STAGE_STRENGTH.get(stage)
    if stage_weight is None:
        stage_weight=next((v for k,v in STAGE_STRENGTH.items() if len(k)>1 and k in stage),None)
    share=number(edge.get('revenue_share'))
    if share is not None and not 0<=share<=1:share=None
    evidential=bool(edge.get('evidence_url'))
    # An unknown revenue share is not silently replaced with an estimated revenue share.
    structural=relation*(stage_weight if stage_weight is not None else .5) if evidential else None
    measured=relation*share if share is not None and evidential else None
    return {'structural_strength':structural,'revenue_exposure':measured,'revenue_share':share,
        'stage_strength':stage_weight,'basis':'reported_revenue_share' if measured is not None else 'explicit_rule_structure',
        'direction':edge.get('direction','unknown'),'version_id':edge.get('version_id'), 'evidence_url':edge.get('evidence_url')}


def event_path(edge, event):
    actions=set(a for fact in event.get('facts',[]) for a in fact.get('actions',[]))
    triggers=set(edge.get('trigger_terms') or [])
    text=event.get('title','')+' '+ ' '.join(f.get('evidence','') for f in event.get('facts',[]))
    explicit=bool(triggers and any(t in text for t in triggers))
    direction=edge.get('direction','unknown')
    sensitivity=edge.get('event_sensitivity',{})
    weights=[number(sensitivity[a]) for a in actions if a in sensitivity and number(sensitivity[a]) is not None]
    return {'event_id':event['event_id'],'symbol':edge['symbol'],'topic_id':edge['topic_id'],
        'actions':sorted(actions),'trigger_matched':explicit,'direction':direction,
        'sensitivity':mean(weights) if weights else None,'exposure':edge_exposure(edge),
        'path_id':uid(edge.get('edge_id'),event['event_id'])}


def networks(items, members, contents, at):
    byid={x['id']:x for x in items};economic=[];paths=[]
    for edge in members:
        if edge.get('relation')=='narrative':continue
        economic.append({'from':edge['topic_id'],'to':edge['symbol'], 'type':edge['relation'],
            'product':edge.get('product',''),'commercial_stage':edge.get('commercial_stage',''),
            **edge_exposure(edge)})
        topic=byid.get(edge['topic_id'],{})
        for event in topic.get('information',{}).get('events',[]):paths.append(event_path(edge,event))
    cooccur=Counter();origins=defaultdict(set);docs=defaultdict(set)
    for r in contents:
        if parse_time(r['observed_at'])>at or parse_time(r['occurred_at'])>at:continue
        symbols=sorted(s for s in r.get('objects',{}) if s.startswith('stock:'))
        fact=r.get('extra',{}).get('exact_cluster') or uid(r.get('title'),r.get('text'))
        for a,b in combinations(symbols[:30],2):
            docs[(a,b)].add(fact);origins[(a,b)].add(r.get('family',r['source']))
    attention=[]
    for (a,b),events in sorted(docs.items(),key=lambda kv:-len(kv[1]))[:2000]:
        ra,rb=byid.get(a,{}),byid.get(b,{})
        ta=ra.get('signals',{}).get('attention_onset_at');tb=rb.get('signals',{}).get('attention_onset_at')
        lag=(parse_time(tb)-parse_time(ta)).total_seconds()/60 if ta and tb else None
        attention.append({'from':a,'to':b,'cooccurrence_events':len(events),'family_count':len(origins[(a,b)]),
            'onset_lag_minutes':lag,'first_observed_order':([a,b] if lag>=0 else [b,a]) if lag is not None else None,
            'edge_type':'observed_coattention'})
    overlaps=[];topic_pools=defaultdict(set)
    for edge in members:topic_pools[edge['topic_id']].add(edge['symbol'])
    for a,b in combinations(sorted(topic_pools),2):
        left,right=topic_pools[a],topic_pools[b];j=len(left&right)/len(left|right) if left|right else 0
        if j>=.5:overlaps.append({'left':a,'right':b,'member_jaccard':j,'shared_members':len(left&right)})
    return {'economic':economic,'attention':attention,'event_paths':paths,'theme_overlaps':overlaps,
        'semantics':{'economic':'evidence-backed product or industrial relationships','attention':'observed co-mentions and onset ordering'}}
