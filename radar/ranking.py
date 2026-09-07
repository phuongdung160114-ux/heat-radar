"""Independent discovery and company-research rankings with as-of model selection."""
from __future__ import annotations
from collections import defaultdict
from statistics import mean
import json, math
import numpy as np
from .common import parse_time, uid
from .features import FEATURE_NAMES

DISCOVERY_COMPONENTS = {
 'attention':['attention_level','attention_velocity','attention_acceleration','count_surprise'],
 'relative':['market_residual','topic_residual','attention_share_change'],
 'participation':['source_agreement','participation_growth','new_actor_share','effective_authors','duration']}
RESEARCH_COMPONENTS = {**DISCOVERY_COMPONENTS,
 'information':['event_novelty','event_importance','fact_updates','forecast_revision'],
 'business':['business_exposure','sector_breadth','sector_entries']}


def percentiles(values):
    valid=sorted(v for v in values if v is not None and math.isfinite(v))
    if not valid:return [None]*len(values)
    from bisect import bisect_left,bisect_right
    return [(bisect_left(valid,v)+.5*(bisect_right(valid,v)-bisect_left(valid,v)))/len(valid) if v is not None and math.isfinite(v) else None for v in values]


def rule_scores(items,components):
    ranks={name:percentiles([x.get('features',{}).get(name) for x in items]) for name in {n for names in components.values() for n in names}}
    scores=[];contributions=[]
    for i,item in enumerate(items):
        groups={g:mean(ranks[n][i] for n in names if ranks[n][i] is not None) for g,names in components.items() if any(ranks[n][i] is not None for n in names)}
        scores.append(100*mean(groups.values()) if groups else None)
        contributions.append(groups)
    return scores,contributions


def active_models(db,at,synthetic=False):
    with db.connect() as c:
        raw=[json.loads(r[0]) for r in c.execute('SELECT payload FROM model_registry WHERE available_ts<=? ORDER BY available_ts DESC',(parse_time(at).timestamp(),))]
    selected={}
    for model in raw:
        if model.get('synthetic',False)!=synthetic or not model.get('active'):continue
        head=model.get('head','research')
        selected.setdefault(head,model)
    return selected


def model_transform(model,items):
    f=model['features'];raw=np.array([[x.get('features',{}).get(k) if x.get('features',{}).get(k) is not None else np.nan for k in f] for x in items],dtype=float)
    p=model['preprocessing'];missing=~np.isfinite(raw)
    values=np.where(missing,np.asarray(p['medians']),raw)
    values=np.clip(values,np.asarray(p['lower']),np.asarray(p['upper']))
    values=(values-np.asarray(p['centers']))/np.asarray(p['scales'])
    return np.column_stack([values,missing.astype(float)])


def predict(model,items):
    x=model_transform(model,items)
    if model['algorithm'] in ('ridge','logistic'):
        result=x@np.asarray(model['coef'])+float(model['intercept'])
        if model['algorithm']=='logistic':result=1/(1+np.exp(-np.clip(result,-35,35)))
    elif model['algorithm']=='lambdamart':
        import lightgbm as lgb
        result=lgb.Booster(model_str=model['model_text']).predict(x)
    else:raise ValueError('未知模型算法')
    calibration=model.get('calibration')
    if calibration:result=np.interp(result,calibration['x'],calibration['y'])
    return result


def rank_items(items,db,at,synthetic=False):
    models=active_models(db,at,synthetic);buckets=defaultdict(list)
    for item in items:buckets['stock' if item['kind']=='stock' else 'topic'].append(item)
    used={}
    for kind,rows in buckets.items():
        discovery,dcomponents=rule_scores(rows,DISCOVERY_COMPONENTS)
        research,rcomponents=rule_scores(rows,RESEARCH_COMPONENTS)
        for i,item in enumerate(rows):
            # A content-only candidate retains research information but no measured-heat score.
            measured=item.get('attention_observed',False) or bool(item.get('member_heat_proxy',{}).get('rankable'))
            item['scores']={'discovery':discovery[i] if measured else None,'research':research[i]}
            item['score_methods']={'discovery':'rule_percentile_v2','research':'rule_percentile_v2'}
            item['score_components']={'discovery':dcomponents[i],'research':rcomponents[i]}
            item['model_id']=None;item['predictions']={};item['feature_contributions']={}
        if kind=='stock':
            for head,model in models.items():
                values=predict(model,rows);ranks=percentiles([float(v) for v in values])
                for i,item in enumerate(rows):
                    if head=='discovery' and not item.get('attention_observed'):continue
                    item['predictions'][model['target']]=float(values[i])
                    item['scores'][head]=100*ranks[i];item['score_methods'][head]='trained_'+model['algorithm']
                    item['model_id']=model['model_id'];used[head]=model['model_id']
                    if model['algorithm'] in ('ridge','logistic'):
                        transformed=model_transform(model,[item])[0];coefs=np.asarray(model['coef'])
                        item['feature_contributions'][head]={name:float(transformed[j]*coefs[j]+transformed[j+len(model['features'])]*coefs[j+len(model['features'])]) for j,name in enumerate(model['features'])}
        for head in ('discovery','research'):
            order=sorted(rows,key=lambda x:(x['scores'][head] is None,-(x['scores'][head] or 0),x['id']))
            for rank,item in enumerate(order,1):item.setdefault('ranks',{})[head]=rank if item['scores'][head] is not None else None
            for group in {x['selection_group'] for x in rows}:
                group_items=[x for x in order if x['selection_group']==group]
                for rank,item in enumerate(group_items,1):item.setdefault('group_ranks',{})[head]=rank if item['scores'][head] is not None else None
    # Lifecycle columns and numerical rank are independent; no lifecycle gets unconditional precedence.
    items.sort(key=lambda x:(x['scores']['discovery'] is None,-(x['scores']['discovery'] or 0),x['id']))
    for index,item in enumerate(items,1):
        item['selection_rank']=index;item['score']=item['scores']['discovery']
        item['rank_factors']['discovery_score']=item['scores']['discovery'];item['rank_factors']['research_score']=item['scores']['research']
    return {'active_models':used,'rule_version':'family-group-percentile-v2','heads':['discovery','research'],
        'feature_schema':uid(FEATURE_NAMES),'score_definition':'0-100 cross-sectional priority; model probabilities are separate predictions'}
