"""Candidate lifecycle and fixed-denominator sector diffusion."""
from __future__ import annotations
from collections import defaultdict
from statistics import median
from datetime import timedelta
from .common import parse_time,stamp,uid,ratio
from .graphs import edge_exposure
from .signals import episode_summary

GROUP_NAMES={'newborn':'新生发现','warming':'正在升温','diffusing':'关注扩散','sustained':'持续研究',
    'returning':'题材回流','information':'信息更新','related':'关联跟踪','review':'业务核验',
    'risk':'负面关注','monitor':'持续观察','insufficient':'待采集'}
RELATION_ORDER={'direct_business':0,'industry_transmission':1,'narrative':2}
NEGATIVE_DIRECTIONS={'cost_negative','competition_negative'}


def evidence_summary(attention):
    valid=[e for e in attention if e.get('eligible') and not e.get('stale') and e.get('market','A')=='A']
    families=defaultdict(list);velocity=defaultdict(list)
    for e in valid:
        if e.get('anomaly_percentile') is not None:families[e['family']].append(e['anomaly_percentile'])
        if e.get('velocity') is not None:velocity[e['family']].append(e['velocity'])
    values=[median(v) for v in families.values()];velocities=[median(v) for v in velocity.values()]
    high=[e for e in valid if e.get('high') or e.get('rising')]
    return dict(family_count=len({e['family'] for e in valid}),supporting_families=sorted({e['family'] for e in high}),
        anomaly_percentile=median(values) if values else None,velocity=median(velocities) if velocities else None,
        direction=median([(v>0)-(v<0) for v in velocities]) if velocities else None,
        persistence=max((e.get('persistence',0) for e in valid),default=0),
        duration_minutes=max((e.get('duration_minutes',0) for e in valid),default=0),
        history_days=max((e.get('history20_n',0) for e in valid),default=0),observed=bool(valid),
        latest_evidence_at=max((e['at'] for e in valid),default=None),
        state='多来源' if len({e['family'] for e in high})>=2 else ('单来源' if valid else '待补充'),
        new_entry=any(e.get('new_entry') for e in valid))


def member_pool(symbols, byid, previous, basis, cfg):
    symbols=set(symbols);known=[byid[s] for s in symbols if s in byid]
    observed=[x for x in known if x['evidence']['observed']]
    warm=[x for x in observed if x['band']!='risk' and (x['band'] in ('confirmed','discovery') or x['evidence']['supporting_families'])]
    warming={x['id'] for x in warm};coverage=ratio(len(observed),len(symbols));breadth=ratio(len(warm),len(observed))
    version=uid(sorted(symbols));previous=previous or {};same=previous.get('pool_version')==version
    oldwarm=set(previous.get('symbols',[]));entered=warming-oldwarm;exited=oldwarm-warming;retained=warming&oldwarm
    leader=max(warm,key=lambda x:((x['evidence']['anomaly_percentile'] or 0),len(x['evidence']['supporting_families'])),default=None)
    onsets=sorted((x.get('signals',{}).get('attention_onset_at'),x['id']) for x in warm if x.get('signals',{}).get('attention_onset_at'))
    return {'pool_basis':basis,'pool_version':version,'all_symbols':sorted(symbols),'symbols':sorted(warming),
        'known_members':len(symbols),'analyzed_members':len(known),'observed_members':len(observed),'accelerating_members':len(warm),
        'coverage':coverage,'breadth':breadth,'breadth_lower':ratio(len(warm),len(symbols)),
        'breadth_upper':ratio(len(warm)+len(symbols)-len(observed),len(symbols)),
        'delta':len(warming)-len(oldwarm) if same else None,'entrants':len(entered) if same else None,
        'exits':len(exited) if same else None,'retained':len(retained) if same else None,
        'entered_symbols':sorted(entered) if same else [],'exited_symbols':sorted(exited) if same else [],
        'retention':ratio(len(retained),len(oldwarm)) if same else None,'pool_comparable':same,
        'excluded_leader':leader['id'] if leader else None,'leave_leader_breadth':ratio(max(len(warm)-1,0),max(len(observed)-1,0)),
        'onset_order':[{'at':t,'id':oid} for t,oid in onsets],
        'diffusion_span_minutes':(parse_time(onsets[-1][0])-parse_time(onsets[0][0])).total_seconds()/60 if len(onsets)>1 else None,
        'rankable':coverage is not None and coverage>=cfg.get('min_member_coverage',.6) and len(observed)>=cfg.get('min_member_count',3)}


def attach_selection(items,members,previous_runs,at,cfg):
    byid={x['id']:x for x in items};bytopic=defaultdict(list);bystock=defaultdict(list)
    for edge in members:bytopic[edge['topic_id']].append(edge);bystock[edge['symbol']].append(edge)
    prior={};old_episodes=set()
    for run in previous_runs:
        for row in run['items']:
            prior.setdefault(row['id'],row)
            if row.get('episode_id') and parse_time(run['as_of'])<at-timedelta(days=3):old_episodes.add(row['id'])
    for item in items:
        item['evidence']=evidence_summary(item.get('attention',[]));item['business_links']=bystock.get(item['id'],[])
        old=prior.get(item['id'],{});active=item['band'] in ('discovery','confirmed')
        latest=item['evidence']['latest_evidence_at'] or max((e['first_observed_at'] for e in item['information'].get('events',[])),default=None)
        same_episode=bool(active and old.get('episode_id') and old.get('last_evidence_at') and
            (at-parse_time(old['last_evidence_at'])).total_seconds()<=cfg.get('episode_gap_hours',24)*3600 and old.get('band') in ('discovery','confirmed'))
        first=(old.get('first_detected_at') if same_episode else stamp(at)) if active else None
        item.update(first_detected_at=first,episode_id=(old['episode_id'] if same_episode else uid('episode-v2',item['id'],first)) if first else None,
            last_evidence_at=latest,state_transition=old.get('stage')!=item['stage'],previous_stage=old.get('stage'))
        stage=item['stage']
        group='risk' if item['band']=='risk' else ('newborn' if stage in ('newborn','early_signal','emerging') else
            ('sustained' if stage in ('high_heat','accelerating') else ('warming' if stage=='warming' else
            ('information' if stage=='information_new' else ('insufficient' if item['band']=='insufficient' else 'monitor')))))
        if active and not same_episode and item['id'] in old_episodes:
            group='returning';item.update(stage='returning',stage_name='题材回流')
        item['selection_group']=group
        # Static heat/price combinations never create a lead-time claim.
        signals=item.get('signals',{})
        item['response_state']=signals.get('state','启动时刻待形成')
        item['attention_onset_at']=signals.get('attention_onset_at');item['response_onset_at']=signals.get('response_onset_at')
        evidence=item['evidence']
        item['rank_factors']={'anomaly':evidence['anomaly_percentile'],'source_count':len(evidence['supporting_families']),
            'velocity':evidence['velocity'],'persistence':evidence['persistence'],
            'new_events':item['information'].get('material_new_event_count',item['information'].get('new_event_count',0)), 'business_quality':0}
    for item in items:
        if item['kind'] not in ('topic','board'):continue
        edges=bytopic[item['id']]
        platform={e['symbol'] for e in edges if e['relation']=='narrative'}
        business={e['symbol'] for e in edges if e['relation']!='narrative' and e.get('direction') not in NEGATIVE_DIRECTIONS}
        allsymbols=platform|business;old=prior.get(item['id'],{})
        oldpools=old.get('member_pools') or {}
        pools={basis:member_pool(symbols,byid,oldpools.get(basis) or (old.get('member_heat_proxy') if (old.get('member_heat_proxy') or {}).get('pool_basis')==basis else {}),basis,cfg)
            for basis,symbols in [('platform',platform),('business',business),('union',allsymbols)]}
        item['member_pools']=pools
        # Platform breadth is stable when new product evidence is attached.
        primary=pools['platform'] if platform else pools['business']
        item['member_heat_proxy']=primary
        item['business_mapping_coverage']=ratio(len(business&platform),len(platform)) if platform else None
        if item['band']!='risk' and primary['rankable'] and primary['accelerating_members']>=cfg.get('min_warming_members',3) and (primary['breadth'] or 0)>=cfg.get('min_member_breadth',.3):
            item.update(stage='diffusing',stage_name='关注扩散',selection_group='diffusing',band='discovery',
                why=f"{primary['accelerating_members']}/{primary['observed_members']} 个有效成员升温")
        picks=[]
        for edge in edges:
            stock=byid.get(edge['symbol']);event=stock and stock['information'].get('material_new_event_count',0)>0
            heat=stock and stock['band'] in ('confirmed','discovery');negative=edge.get('direction') in NEGATIVE_DIRECTIONS or (stock and stock['band']=='risk')
            category='risk' if negative else ('review' if edge['relation']=='narrative' else ('change' if event else ('diffusion' if heat else 'core')))
            picks.append({'id':edge['symbol'],'name':stock['name'] if stock else edge.get('stock_name',edge['symbol']),
                'category':category,'relation':edge['relation'],'direction':edge.get('direction','unknown'),
                'description':edge.get('description',''),'evidence_url':edge.get('evidence_url',''),
                'evidence_date':edge.get('evidence_date'),'product':edge.get('product',''),
                'commercial_stage':edge.get('commercial_stage',''),'observed':bool(stock and stock['evidence']['observed']),
                'selection_group':stock.get('selection_group') if stock else 'related',
                'anomaly':stock['evidence']['anomaly_percentile'] if stock else None,'exposure':edge_exposure(edge)})
        dedup={}
        for pick in sorted(picks,key=lambda p:(RELATION_ORDER[p['relation']],-(p['exposure']['structural_strength'] or 0))):dedup.setdefault(pick['id'],pick)
        item['stock_candidates']=list(dedup.values())
    active_topics={x['id'] for x in items if x['kind'] in ('topic','board') and x['band'] in ('confirmed','discovery')}
    for item in items:
        if item['kind']!='stock':continue
        links=[e for e in item['business_links'] if e['topic_id'] in active_topics]
        strong=[e for e in links if e['relation']!='narrative' and e.get('direction') not in NEGATIVE_DIRECTIONS and e.get('evidence_url')]
        exposure=[edge_exposure(e) for e in strong]
        values=[e['revenue_exposure'] if e['revenue_exposure'] is not None else e['structural_strength'] for e in exposure if e['structural_strength'] is not None]
        item['business_exposure']=max(values) if values else None;item['business_exposure_details']=exposure
        item['rank_factors']['business_quality']=2 if any(e['relation']=='direct_business' for e in strong) else (1 if strong else 0)
        item['stock_category']='risk' if item['band']=='risk' else ('change' if strong and item['information'].get('material_new_event_count',0)>0 else
            ('diffusion' if strong and item['selection_group'] in ('newborn','warming') else ('core' if strong else 'review')))
        if strong and item['band'] not in ('confirmed','discovery','risk') and item['stage']!='information_new':
            item.update(selection_group='related',stage='related',stage_name='关联跟踪',band='monitor',why=strong[0].get('description','业务关联'))
    for item in items:
        if item['band'] in ('confirmed','discovery') and not item.get('episode_id'):
            old=prior.get(item['id'],{});first=old.get('first_detected_at') if old.get('band') in ('discovery','confirmed') else None
            item['first_detected_at']=first or stamp(at)
            item['episode_id']=(old.get('episode_id') if first else None) or uid('episode-v2',item['id'],item['first_detected_at'])
        item['episode']=episode_summary(item,previous_runs,at)
        item['state_transition']=prior.get(item['id'],{}).get('stage')!=item['stage']
        item['selection_group_name']=GROUP_NAMES[item['selection_group']]
        item['rank_reasons']=[item['why']]
        if item['rank_factors']['new_events']:item['rank_reasons'].append(f"新增经营事件 {item['rank_factors']['new_events']} 条")
        if item['rank_factors']['business_quality']:item['rank_reasons'].append('产品与业务关系已关联来源')
    # Initial deterministic ordering is retained only until the task-specific ranker is called.
    items.sort(key=lambda x:(-(x['rank_factors']['anomaly'] or 0),-x['rank_factors']['source_count'],x['id']))
    for i,item in enumerate(items,1):item['selection_rank']=i
    return items
