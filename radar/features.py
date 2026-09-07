"""Full-universe, source-local innovations and cross-sectional feature materialization."""
from __future__ import annotations
from collections import defaultdict
from statistics import median, mean
import math, json
import numpy as np
from .common import number, uid, parse_time

FEATURE_NAMES = ['attention_level','attention_velocity','attention_acceleration','market_residual',
    'topic_residual','source_agreement','duration','participation_growth','new_actor_share',
    'effective_authors','count_surprise','event_novelty','event_importance','fact_updates',
    'business_exposure','sector_breadth','sector_entries','forecast_revision',
    'return_1d','turnover','log_cap','attention_share_change','fan_entry_share']
ATTENTION_FEATURES = FEATURE_NAMES[:11]+['attention_share_change','fan_entry_share']
EVENT_FEATURES=['event_novelty','event_importance','fact_updates','forecast_revision']
STRUCTURE_FEATURES=['business_exposure','sector_breadth','sector_entries']
PRICE_FEATURES=['return_1d','turnover','log_cap']
FEATURE_DESCRIPTIONS = {
 'attention_level':'家族内合并后的历史异常分位','attention_velocity':'保留幅度的来源内关注变化',
 'attention_acceleration':'来源内升温速度变化','market_residual':'扣除同来源股票共同变化后的关注增量',
 'topic_residual':'相对冻结题材成员的关注增量','source_agreement':'支持关注升温的来源家族数',
 'duration':'连续观测高热时长的对数','participation_growth':'参与主体的异常变化',
 'new_actor_share':'当日首次参与者比例','effective_authors':'有效作者数的对数',
 'count_surprise':'计数波动尺度下的关注增量','event_novelty':'新增经营事件数的对数',
 'event_importance':'已出现事件的规则重要度','fact_updates':'发生数值变化的事实数',
 'business_exposure':'业务阶段、关系强度与已知收入占比','sector_breadth':'固定成员池内升温广度',
 'sector_entries':'新增升温成员数','forecast_revision':'同主体同期间预测修订中位数',
 'return_1d':'截至信号时点的当日涨跌','turnover':'截至信号时点的换手率',
 'fan_entry_share':'来源公布的新晋粉丝占比，与独立新参与者分列','log_cap':'已知流通市值对数','attention_share_change':'同面板交集内注意力份额的对数变化'}


def finite(value):
    x=number(value)
    return x if x is not None and math.isfinite(x) else None


def aggregate_family(records, key):
    groups=defaultdict(list)
    for e in records:
        val=finite(e.get(key))
        if val is not None:groups[e['family']].append(val)
    values=[median(v) for v in groups.values()]
    return median(values) if values else None


def residualize(y, controls, ridge=1e-6):
    """Current cross-sectional residual; coefficients never use future labels."""
    y=np.asarray(y,float);x=np.asarray(controls,float)
    if x.ndim==1:x=x[:,None]
    mask=np.isfinite(y)&np.all(np.isfinite(x),axis=1)
    result=np.full(len(y),np.nan)
    if mask.sum()<=x.shape[1]+2:return result
    design=np.column_stack([np.ones(mask.sum()),x[mask]])
    coef=np.linalg.solve(design.T@design+ridge*np.eye(design.shape[1]),design.T@y[mask])
    result[mask]=y[mask]-design@coef
    return result


def build_features(items, members, cfg=None):
    cfg=cfg or {};panels=defaultdict(list);byid={x['id']:x for x in items};peers=defaultdict(set)
    for edge in members:peers[edge['topic_id']].add(edge['symbol'])
    source_diagnostics=[]
    for item in items:
        for e in item.get('attention',[]):
            if item['kind']!='stock' or not (e.get('model_eligible') or e.get('eligible')) or e.get('stale') or e.get('market','A')!='A':continue
            panel=(e['source'],e.get('panel_id'),e['metric'],e.get('unit'),e.get('window_kind'))
            panels[panel].append((item,e))
    for key,series in panels.items():
        # Only matched objects with a comparable history define the common component.
        logq=[math.log(e['q']) for _,e in series if e.get('q') is not None and e['q']>0]
        common=median(logq) if len(logq)>=cfg.get('min_cross_section',5) else None
        velocities=[float(e['velocity']) for _,e in series if e.get('velocity') is not None]
        velocity_common=median(velocities) if len(velocities)>=cfg.get('min_cross_section',5) else None
        matched=[(x,e) for x,e in series if e.get('value') is not None and e.get('previous') is not None and e['value']>=0 and e['previous']>=0]
        raw_total=sum(e['value'] for _,e in matched);old_total=sum(e['previous'] for _,e in matched)
        comparable_count=len(matched)
        for item,e in series:
            e['common_log_attention']=common
            e['market_attention_residual']=(math.log(e['q'])-common if common is not None and e.get('q') is not None and e['q']>0
                else (float(e['velocity'])-velocity_common if velocity_common is not None and e.get('velocity') is not None else None))
            e['cross_section_n']=len(series)
            e['attention_share_change']=None
            if key[2]!='rank' and comparable_count>=cfg.get('min_cross_section',5) and raw_total>0 and old_total>0 and e.get('previous') is not None and e['previous']>=0 and e.get('value',-1)>=0:
                smooth=cfg.get('share_smoothing',1.0)
                p=(e['value']+smooth)/(raw_total+smooth*comparable_count)
                p0=(e['previous']+smooth)/(old_total+smooth*comparable_count)
                e['attention_share_change']=math.log(p/p0)
                e['share_universe_hash']=uid(sorted(x['id'] for x,_ in matched))
        source_diagnostics.append({'source':key[0],'panel':key[1],'metric':key[2], 'objects':len(series),
            'history_objects':len(logq),'matched_objects':comparable_count,'common_log_attention':common,
            'common_velocity':velocity_common,'matched_total':raw_total if key[2]!='rank' else None})
    for item in items:
        att=[e for e in item.get('attention',[]) if (e.get('model_eligible') or e.get('eligible')) and not e.get('stale') and e.get('market','A')=='A']
        participants=[e for e in att if e.get('basis')=='participant_id']
        info=item.get('information',{});trade=item.get('trading',{});m=item.get('member_heat_proxy',{})
        revisions=[p['revision_ratio'] for p in info.get('forecast_revisions',[]) if p.get('revision_ratio') is not None]
        aliases=defaultdict(list)
        for e in att:aliases[e['family']].append(e)
        role_features={}
        for role in ('retail','institutional'):
            es=[e for e in att if ('institutional' if e.get('entity_type') in ('sellside_team','sellside_org','buyside_org','industry_org') else 'retail')==role]
            role_features[role]={'family_count':len({e['family'] for e in es}),'level':aggregate_family(es,'anomaly_percentile'),'velocity':aggregate_family(es,'velocity')}
        n=sum(e.get('value',0) for e in participants);new=sum(e.get('new_today',0) for e in participants)
        cap=trade.get('float_cap_cny') or item.get('float_cap_cny')
        features={
            'attention_level':aggregate_family(att,'anomaly_percentile'),
            'attention_velocity':aggregate_family(att,'velocity'),
            'attention_acceleration':aggregate_family(att,'acceleration'),
            'market_residual':aggregate_family(att,'market_attention_residual'),
            'topic_residual':None,
            'source_agreement':len({e['family'] for e in att if e.get('high') or e.get('rising')}) if att else None,
            'duration':math.log1p(max((e.get('duration_minutes') or 0 for e in att),default=0)) if att else None,
            'participation_growth':aggregate_family(participants,'velocity'),
            'new_actor_share':new/n if n else None,
            'effective_authors':math.log1p(sum(e.get('effective_authors') or 0 for e in participants)) if participants else None,
            'count_surprise':aggregate_family(att,'count_z'),
            'event_novelty':math.log1p(info.get('material_new_event_count',0)) if info.get('current_records',0) else None,
            'event_importance':info.get('event_importance',0) if info.get('current_records',0) else None,
            'fact_updates':info.get('fact_update_count',0) if info.get('current_records',0) else None,
            'business_exposure':item.get('business_exposure'),
            'sector_breadth':m.get('breadth') if m.get('rankable') else None,
            'sector_entries':m.get('entrants'),
            'forecast_revision':median(revisions) if revisions else None,
            'return_1d':trade.get('equal_weight_return'),
            'turnover':trade.get('turnover'),
            'log_cap':math.log(cap) if cap and cap>0 else None,
            'attention_share_change':aggregate_family(att,'attention_share_change'),
            'fan_entry_share':next((e['value'] for e in item.get('attention',[]) if e['metric']=='new_fan_share' and not e.get('stale')),None)}
        item['features']={k:finite(v) for k,v in features.items()}
        item['attention_roles']=role_features;item['attention_observed']=bool(att)
        item['feature_observed_fraction']=sum(v is not None for v in item['features'].values())/len(FEATURE_NAMES)
    # Retain both the common theme movement and company-specific movement.
    topic_values={}
    for topic,symbols in peers.items():
        vals=[byid[s]['features']['market_residual'] for s in symbols if s in byid and byid[s]['features']['market_residual'] is not None]
        topic_values[topic]=median(vals) if len(vals)>=3 else None
    for item in items:
        if item['kind']!='stock':continue
        own=item['features']['market_residual'];topic_deltas=[];links=[]
        for topic,symbols in peers.items():
            if item['id'] not in symbols:continue
            # Leave-self-out prevents a tiny theme from subtracting its own signal.
            vals=[byid[s]['features']['market_residual'] for s in symbols-{item['id']} if s in byid and byid[s]['features']['market_residual'] is not None]
            base=median(vals) if len(vals)>=3 else None
            if base is not None and own is not None:topic_deltas.append(own-base)
            links.append({'topic_id':topic,'common_attention':topic_values[topic], 'leave_self_baseline':base,'peer_count':len(vals)})
        item['features']['topic_residual']=median(topic_deltas) if topic_deltas else None
        item['topic_attention_context']=links
        topicrows=[byid[t] for t in peers if item['id'] in peers[t] and t in byid]
        breadth=[x.get('member_heat_proxy',{}).get('breadth') for x in topicrows if x.get('member_heat_proxy',{}).get('rankable')]
        if breadth:item['features']['sector_breadth']=max(breadth)
    return source_diagnostics


def save_panel(db,run):
    rows=[]
    for item in run['items']:
        valid=[e for e in item.get('attention',[]) if (e.get('model_eligible') or e.get('eligible')) and not e.get('stale')]
        measurements={e['source']+'|'+str(e.get('panel_id',''))+'|'+e['metric']+'|'+str(e.get('window_kind','')):
            {'source':e['source'],'family':e['family'],'metric':e['metric'],'value':e.get('value'),'unit':e.get('unit'),
             'panel_version':e.get('pool_version'),'at':e['at'],'ids':e.get('record_ids',[])} for e in valid}
        family_features={}
        for family in {e['family'] for e in valid}:
            es=[e for e in valid if e['family']==family];actors=[e for e in es if e.get('basis')=='participant_id']
            count=sum(e.get('value',0) for e in actors)
            family_features[family]={
                'attention_level':aggregate_family(es,'anomaly_percentile'),'attention_velocity':aggregate_family(es,'velocity'),
                'attention_acceleration':aggregate_family(es,'acceleration'),'market_residual':aggregate_family(es,'market_attention_residual'),
                'source_agreement':int(any(e.get('high') or e.get('rising') for e in es)),
                'duration':math.log1p(max((e.get('duration_minutes') or 0 for e in es),default=0)),
                'participation_growth':aggregate_family(actors,'velocity'),
                'new_actor_share':sum(e.get('new_today',0) for e in actors)/count if count else None,
                'effective_authors':math.log1p(sum(e.get('effective_authors') or 0 for e in actors)) if actors else None,
                'count_surprise':aggregate_family(es,'count_z'),
                'attention_share_change':aggregate_family(es,'attention_share_change')}
        payload={'id':item['id'],'name':item['name'],'kind':item['kind'],'at':run['as_of'],'run_id':run['run_id'],
            'episode_id':item.get('episode_id'),'group':item.get('selection_group'),'features':item.get('features',{}),
            'scores':item.get('scores',{}),'ranks':item.get('ranks',{}),'attention_observed':item.get('attention_observed',False),
            'industry':item.get('industry'),'synthetic':run.get('synthetic',False),'evaluation_type':run.get('evaluation_type'),
            'source_scores':{e['source']+'/'+e['metric']:(-e['value'] if e['metric']=='rank' else e['value']) for e in item.get('attention',[]) if (e.get('model_eligible') or e.get('eligible')) and not e.get('stale') and e.get('value') is not None},
            'family_levels':{f:median([e['anomaly_percentile'] for e in item.get('attention',[]) if e.get('family')==f and (e.get('model_eligible') or e.get('eligible')) and not e.get('stale') and e.get('anomaly_percentile') is not None])
                for f in {e['family'] for e in item.get('attention',[]) if (e.get('model_eligible') or e.get('eligible')) and not e.get('stale') and e.get('anomaly_percentile') is not None}},
            'measurements':measurements,'family_features':family_features,'model_id':item.get('model_id'),'source_record_ids':item.get('source_record_ids',[])}
        rows.append((run['run_id'],item['id'],parse_time(run['as_of']).timestamp(),item.get('episode_id'),json.dumps(payload,ensure_ascii=False,allow_nan=False)))
    with db.lock,db.connect() as c:c.executemany('INSERT OR REPLACE INTO feature_panels VALUES(?,?,?,?,?)',rows)


def load_panel(db,as_of=None,kind='stock'):
    sql='SELECT payload FROM feature_panels';args=[]
    if as_of is not None:sql+=' WHERE asof_ts<=?';args=[parse_time(as_of).timestamp()]
    sql+=' ORDER BY asof_ts,run_id,object_id'
    with db.connect() as c:rows=[json.loads(r[0]) for r in c.execute(sql,args)]
    return [r for r in rows if not kind or r['kind']==kind]
