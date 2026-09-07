from __future__ import annotations
from collections import defaultdict
from statistics import mean
from math import sqrt
from .common import now,parse_time,stamp,ratio,uid

HORIZONS=(3,5,10)

def _ranks(values):
    positions=defaultdict(list)
    for i,(_,v) in enumerate(sorted(enumerate(values),key=lambda x:x[1])):positions[v].append(i+1)
    return [mean(positions[v]) for v in values]

def _correlation(xs,ys):
    if len(xs)<3:return None
    a,b=_ranks(xs),_ranks(ys);ma,mb=mean(a),mean(b)
    denom=sqrt(sum((v-ma)**2 for v in a)*sum((v-mb)**2 for v in b))
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/denom if denom else None

def evaluate(engine,run_id=None,horizons=HORIZONS,as_of=None):
    target=engine.db.run(run_id) if run_id else next(iter(engine.db.runs(1)),None)
    if not target:return dict(rows=[],summary=[],status='no_run')
    clock=parse_time(as_of or now());signal=parse_time(target['as_of']);day=signal.date().isoformat()
    sessions=sorted(s['date'] for s in engine.calendar.data['sessions'] if s['market']=='A' and s.get('is_open'))
    # Observation references are fixed by the saved signal phase.
    phase=target['phase'];same=phase in ('premarket','intraday')
    dates=[d for d in sessions if d>=day] if same else [d for d in sessions if d>day]
    field='close' if phase=='intraday' else 'open'
    reference_kind={'premarket':'target_session_open','intraday':'signal_session_close','postmarket':'next_session_open'}[phase]
    start=dates[0] if dates else None
    if phase=='premarket' and target.get('target_session'):
        requested=target['target_session']
        if isinstance(requested,str) and requested in sessions:dates=[d for d in sessions if d>=requested];start=dates[0]
    frozen=target.get('memberships',target.get('membership_snapshot',[]));bytopic=defaultdict(list);bystock=defaultdict(set)
    for edge in frozen:
        bytopic[edge['topic_id']].append(edge)
        bystock[edge['symbol']].add(edge['topic_id'])
    series=defaultdict(lambda:defaultdict(dict))
    for record in engine.db.read(kind='quote',metric='daily_bar',as_of=clock,start=(start+'T00:00:00+08:00') if start else None):
        if record['metric']!='daily_bar':continue
        extra=record['extra'];date=extra.get('date',record['occurred_at'][:10]);closed=parse_time(date+'T15:00:00+08:00')
        if closed>clock or parse_time(record['observed_at'])<closed:continue
        source=(record['source'],extra.get('adjust','none'))
        for oid in record['objects']:series[oid][source][date]={**extra,'record_id':record['record_id']}
    def change(oid,end):
        choices=[]
        for (source,adjust),bars in series.get(oid,{}).items():
            a,b=bars.get(start,{}),bars.get(end,{});left,right=a.get(field),b.get('close')
            if left is not None and right is not None and left>0 and right>0:
                choices.append((adjust in ('hfq','qfq','index'),len(bars),source,adjust,right/left-1,[a['record_id'],b['record_id']]))
        if not choices:return None,None,None,[]
        _,_,source,adjust,value,ids=max(choices)
        return value,source,adjust,ids
    def pool_for(oid):
        edges=bytopic[oid];business=[e for e in edges if e['relation']!='narrative' and e.get('direction') not in ('cost_negative','competition_negative')]
        platform=[e for e in edges if e.get('relation')=='narrative']
        return sorted({e['symbol'] for e in (platform or business)})
    mincov=target.get('selection_parameters',{}).get('min_member_coverage',.6)
    rows=[];feedback={f['object_id']:f for f in engine.db.feedback(target['run_id'])};items=target['items'];controls=set(target.get('scan',{}).get('controls',[]))
    for h in horizons:
        # Intraday close-to-close measures h full following sessions; open references include the first session.
        idx=h if field=='close' else h-1;end=dates[idx] if len(dates)>idx else None
        mature=bool(end and parse_time(end+'T15:00:00+08:00')<=clock)
        market,market_source,_,market_ids=change('market:CSI300',end) if mature else (None,None,None,[])
        returns={oid:change(oid,end) for oid in series} if mature else {}
        for item in items:
            oid=item['id'];kind=item['kind'];is_stock=kind=='stock';pool=[];observed=[]
            value,source,adjust,ids=returns.get(oid,(None,None,None,[])) if is_stock else (None,None,None,[])
            coverage=None;peer=None;peer_n=0
            if not is_stock:
                pool=pool_for(oid);observed=[s for s in pool if returns.get(s,(None,))[0] is not None];coverage=ratio(len(observed),len(pool))
                if observed and coverage>=mincov:
                    value=mean(returns[s][0] for s in observed);source='frozen_members';adjust='member_series';ids=[i for s in observed for i in returns[s][3]]
            elif mature:
                peers=set(s for topic in bystock[oid] for s in pool_for(topic))-{oid};valid=[s for s in peers if returns.get(s,(None,))[0] is not None]
                if len(valid)>=3 and ratio(len(valid),len(peers))>=mincov:peer=mean(returns[s][0] for s in valid);peer_n=len(valid)
            status='pending' if not mature else ('complete' if value is not None else ('no_members' if not is_stock and not pool else ('member_coverage' if not is_stock else 'missing_bars')))
            rows.append(dict(id=oid,name=item['name'],kind=kind,selection_group=item.get('selection_group',item.get('band')),selection_rank=item.get('selection_rank'),
                stage=item['stage'],episode_id=item.get('episode_id'),horizon_sessions=h,signal_at=target['as_of'],reference_kind=reference_kind,reference_date=start,end_date=end,
                status=status,complete=status=='complete',source=source,adjust=adjust,raw_price_change=value,benchmark_change=market,relative_change=value-market if value is not None and market is not None else None,
                peer_relative_change=value-peer if value is not None and peer is not None else None,peer_count=peer_n,member_count=len(pool) if not is_stock else None,member_observed=len(observed) if not is_stock else None,member_coverage=coverage,
                label_record_ids=sorted(set(ids+market_ids)),feedback=feedback.get(oid,{}).get('decision'),control=oid in controls))
    summaries=[];baselines=[];byid={x['id']:x for x in items}
    def metrics(rs):
        valid=[r for r in rs if r['relative_change'] is not None];raw=[r for r in rs if r['complete']]
        return dict(total=len(rs),complete=len(raw),relative_complete=len(valid),coverage=ratio(len(raw),len(rs)),mean_change=mean(r['raw_price_change'] for r in raw) if raw else None,
            mean_relative=mean(r['relative_change'] for r in valid) if valid else None,positive_rate=ratio(sum(r['relative_change']>0 for r in valid),len(valid)))
    for h in horizons:
        hr=[r for r in rows if r['horizon_sessions']==h]
        for kind in sorted({r['kind'] for r in hr}):
            for group in sorted({r['selection_group'] for r in hr if r['kind']==kind}):
                gr=[r for r in hr if r['kind']==kind and r['selection_group']==group]
                summaries.append(dict(kind=kind,group=group,horizon_sessions=h,**metrics(gr)))
        stocks=[r for r in hr if r['kind']=='stock'];ranked=sorted(stocks,key=lambda r:r.get('selection_rank') or 10**9)
        # Baselines use the identical frozen selection universe and raw values within each source.
        orders={'selection':ranked,'price':sorted(stocks,key=lambda r:byid[r['id']].get('trading',{}).get('equal_weight_return') if byid[r['id']].get('trading',{}).get('equal_weight_return') is not None else -100,reverse=True)}
        heat=defaultdict(dict)
        for r in stocks:
            for evidence in byid[r['id']].get('attention',[]):
                if evidence.get('value') is None or evidence.get('stale'):continue
                key=evidence['source']+'/'+evidence['metric'];v=evidence['value'];heat[key][r['id']]=-v if evidence['metric']=='rank' else v
        for source,values in heat.items():
            matched=[r for r in stocks if r['id'] in values]
            orders['heat:'+source]=sorted(matched,key=lambda r:values[r['id']],reverse=True)
            orders['selection_matched:'+source]=sorted(matched,key=lambda r:r.get('selection_rank') or 10**9)
        for label,order in orders.items():
            for k in (5,10):
                top=order[:k];valid=[r for r in order if r['relative_change'] is not None]
                baselines.append(dict(baseline=label,universe_size=len(order),universe_hash=uid(sorted(r['id'] for r in order)),k=k,horizon_sessions=h,**metrics(top),rank_ic=_correlation([-order.index(r) for r in valid],[r['relative_change'] for r in valid])))
    result=dict(schema_version='2.0.0',run_id=target['run_id'],as_of=stamp(clock),synthetic=target.get('synthetic',False),evaluation_type=target.get('evaluation_type'),
        eligible=target.get('evaluation_type')!='point_in_time_recalculation',rows=rows,summary=summaries,baselines=baselines,
        feedback=dict(total=len(feedback),kept=sum(f['decision']=='keep' for f in feedback.values()),rejected=sum(f['decision']=='reject' for f in feedback.values())))
    result['evaluation_id']=engine.db.save_evaluation(result)
    return result
