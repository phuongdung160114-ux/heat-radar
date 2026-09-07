"""Observed data capabilities, derived feature activation and source cadence."""
from collections import defaultdict
import json
from datetime import datetime
from .common import CN,stamp,parse_time
from .providers import REGISTRY

MEASUREMENTS={
 'rank':'平台序位','followers_total':'关注存量','followers_net_30':'30分钟关注净变化',
 'discussion_index':'平台讨论指数','native_hot':'平台原生热度','new_fan_share':'平台新晋粉丝占比',
 'loyal_fan_share':'平台持续粉丝占比','attention_index':'平台用户关注指数',
 'minute_bar':'5分钟价格观察','daily_bar':'日级后续价格','business_segment':'披露的业务构成',
 'security_master':'证券目录','quote':'全市场价格观察'}


def capability_matrix(engine,run=None):
    at=parse_time(run['as_of']) if run else None
    where=' WHERE r.observed_ts<=?' if at else '';args=[at.timestamp()] if at else []
    sql='''SELECT r.source,r.kind,r.metric,COUNT(DISTINCT r.id) records,COUNT(DISTINCT o.object_id) objects,
        COUNT(DISTINCT date(r.event_ts,'unixepoch','+8 hours')) history_days,
        MIN(r.event_ts) first_event,MAX(r.event_ts) last_event,MAX(r.observed_ts) last_observed
        FROM records r LEFT JOIN object_records o ON o.record_id=r.id'''+where+' GROUP BY r.source,r.kind,r.metric'
    with engine.db.connect() as c:raw=[dict(row) for row in c.execute(sql,args)]
    active=defaultdict(set);history=defaultdict(list);derived=defaultdict(set)
    for item in (run or {}).get('items',[]):
        for e in item.get('attention',[]):
            if (e.get('model_eligible') or e.get('eligible')) and not e.get('stale'):active[(e['source'],e['metric'])].add(item['id'])
            history[(e['source'],e['metric'])].append(e.get('history20_n',0))
            if e.get('metric')=='followers_net_30':derived[e['source']].add(item['id'])
    rows=[]
    for r in raw:
        key=(r['source'],r['metric']);parent={'em_fans':'em_attention_detail','em_focus':'em_attention_detail','em_rank_detail':'em_attention_detail','cninfo_answers':'cninfo_irm'}.get(r['source'],r['source']);spec=REGISTRY.get(parent);hs=history[key];r['provider_source']=parent
        r.update(measurement=MEASUREMENTS.get(r['metric'],r['kind']),active_objects=len(active[key]),
            median_comparable_days=sorted(hs)[len(hs)//2] if hs else 0,
            last_event_at=stamp(datetime.fromtimestamp(r['last_event'],CN)),last_observed_at=stamp(datetime.fromtimestamp(r['last_observed'],CN)),
            configured_seconds=spec.interval if spec else None,derived_net_flow_objects=len(derived.get(r['source'],set())))
        rows.append(r)
    present={r['source'] for r in rows}
    for sid,spec in REGISTRY.items():
        if sid not in present:rows.append({'source':sid,'kind':'configured','metric':'','records':0,'objects':0,'history_days':0,
            'active_objects':0,'median_comparable_days':0,'measurement':spec.dimension,'configured_seconds':spec.interval})
    for r in rows:r['enabled']=engine.settings.data['sources'].get(r.get('provider_source',r['source']),False)
    return {'as_of':run['as_of'] if run else stamp(),'rows':rows,
        'summary':{'raw_sources':len(present),'effective_series':sum(r['active_objects']>0 for r in rows),
                   'behavioral_sources':len({r['source'] for r in rows if r['kind']=='participation' and r['records']>0}),
                   'objects_with_attention':sum(x.get('attention_observed',False) for x in (run or {}).get('items',[]))}}
