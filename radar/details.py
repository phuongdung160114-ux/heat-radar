from collections import defaultdict
from datetime import timedelta
from statistics import mean
from .common import stamp,parse_time,CN,ratio
from .analytics import participant_evidence,coverage_ok,summaries,forecast_revisions
from .vendor.session_tools import aggregate_window

def detail(engine,object_id,run):
    item=next((x for x in run.get('items',[]) if x['id']==object_id),None)
    if not item:raise ValueError('该运行中不存在这个对象')
    at=parse_time(run['as_of']);metric_at=parse_time(run['metric_cutoff']);rows=engine.db.read(kind=['snapshot','participation','content','forecast','claim'],object_id=object_id,as_of=at,end=at+timedelta(microseconds=1))
    coverage=engine.db.read(kind='coverage',as_of=at,end=at+timedelta(microseconds=1))
    actor_rows=[r for r in rows if r['kind']=='participation']
    roll={str(m):participant_evidence(actor_rows,coverage,object_id,metric_at,engine.calendar,run['selection_parameters'],item.get('parent'),at,m) for m in (15,30,60)}
    # Daily A-share participation must not merge US sessions or changed attribution rules.
    actor_rows=[r for r in actor_rows if r.get('extra',{}).get('market','A')=='A']
    versions={}
    for e in roll['30']:
        if e.get('market','A')=='A':versions[(e['source'],e['panel_id'],e['entity_type'])]=(e.get('attribution_version'),e.get('attribution_valid_from'))
    def rule_compatible(r):
        key=(r['source'],r['panel_id'],r['entity_type']);v=versions.get(key)
        return not v or r.get('extra',{}).get('attribution_versions',{}).get(object_id,'provider:'+r.get('pool_version','v1'))==v[0]
    actor_rows=[r for r in actor_rows if rule_compatible(r)]
    day=metric_at.astimezone(CN).date().isoformat();opening=metric_at.replace(hour=9,minute=30,second=0,microsecond=0)
    periods={}
    converted=[{**r,'platform':r['source'],'cohort':r.get('extra',{}).get('cohort',{}),'circles':[]} for r in actor_rows]
    for name,left,right in [('regular',opening,metric_at),('morning',opening,min(metric_at,opening.replace(hour=11,minute=30))),
                            ('afternoon',opening.replace(hour=13,minute=0),metric_at)]:
        if right>left:periods[name]=aggregate_window(converted,stamp(left),stamp(right),stamp(at),day_start=stamp(opening))
    groups=defaultdict(list)
    for r in actor_rows:
        if opening<=parse_time(r['occurred_at'])<metric_at:groups[(r['source'],r['panel_id'],r['entity_type'])].append(r)
    full=[]
    for (source,panel,entity),rs in groups.items():
        current={r['entity_id'] for r in rs};morning={r['entity_id'] for r in rs if parse_time(r['occurred_at'])<opening.replace(hour=11,minute=30)}
        afternoon={r['entity_id'] for r in rs if parse_time(r['occurred_at'])>=opening.replace(hour=13,minute=0)}
        hist={};expected=engine.calendar.open_dates('A',before=day,limit=250)
        for d in expected:
            left=parse_time(d+'T09:30:00+08:00');right=parse_time(d+'T'+metric_at.strftime('%H:%M:%S')+'+08:00')
            valid_from=versions.get((source,panel,entity),(None,None))[1]
            if right<=left or (valid_from and left<parse_time(valid_from)):continue
            if coverage_ok(coverage,source,panel,left,right,at,object_id):
                hist[d]=len({r['entity_id'] for r in actor_rows if r['source']==source and r['panel_id']==panel and r['entity_type']==entity and left<=parse_time(r['occurred_at'])<right})
        full.append(dict(source=source,panel_id=panel,entity_type=entity,distinct=len(current),comparisons=summaries(len(current),hist,expected),
                         afternoon_return_ratio=ratio(len(morning&afternoon),len(morning)) if metric_at.hour>=15 else None,
                         afternoon_new=len(afternoon-morning) if metric_at.hour>=13 else None,
                         complete=metric_at.hour>=15 and coverage_ok(coverage,source,panel,opening,opening.replace(hour=15,minute=0),at,object_id)))
    # Genuine 5-trading-day cohort retention: insufficient follow-up remains unknown.
    retention=[]
    latest_dates=engine.calendar.open_dates('A',before=day,limit=10)
    for source,panel,entity in groups:
        relevant=[r for r in actor_rows if r['source']==source and r['panel_id']==panel and r['entity_type']==entity]
        first={}
        for r in relevant:first.setdefault(r['entity_id'],parse_time(r['occurred_at']).astimezone(CN).date().isoformat())
        for d in latest_dates[:5]:
            cohort={a for a,v in first.items() if v==d};future=[x for x in engine.calendar.open_dates('A',before=day,limit=250) if x>d][:5]
            complete=len(future)==5 and all(coverage_ok(coverage,source,panel,parse_time(x+'T09:30:00+08:00'),parse_time(x+'T15:00:00+08:00'),at,object_id) for x in future)
            seen={r['entity_id'] for r in relevant if parse_time(r['occurred_at']).astimezone(CN).date().isoformat() in future}
            if cohort:retention.append(dict(source=source,panel_id=panel,date=d,cohort_n=len(cohort),retained_n=len(cohort&seen) if complete else None,
                retention=ratio(len(cohort&seen),len(cohort)) if complete else None,complete=complete,note='样本内首次出现批次，不自动代表全市场新投资者'))
    chart=defaultdict(list)
    for r in rows:
        if r['kind']=='snapshot' and r.get('value') is not None:
            key=' | '.join((r['source'],r['panel_id'],r['metric']))
            chart[key].append(dict(at=r['occurred_at'],value=r['value'],unit=r['unit'],metric=r['metric']))
    return dict(item=item,rolling=roll,periods=periods,full_day=full,cohort_retention=retention,
                charts=[dict(series=k,points=v[-1500:]) for k,v in chart.items()],path=run.get('intraday_path',{}).get(object_id),
                record_count=len(rows),note='图线仅连接同来源同面板原始读数；没有补历史、没有插值生成观测。')
