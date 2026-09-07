"""Chronological experiments, purged labels, paired ablations and model registry."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import defaultdict
from statistics import mean
import math, json, time
import numpy as np
from scipy.stats import spearmanr, rankdata
from sklearn.linear_model import Ridge, LogisticRegression
from sklearn.isotonic import IsotonicRegression
from .common import parse_time, stamp, uid
from .features import FEATURE_NAMES, ATTENTION_FEATURES, EVENT_FEATURES, STRUCTURE_FEATURES, PRICE_FEATURES, load_panel
from .ranking import predict, model_transform

@dataclass
class ExperimentConfig:
    target: str = 'attention_1d'
    algorithm: str = 'logistic'
    min_train_days: int = 60
    validation_days: int = 15
    test_days: int = 15
    embargo_days: int = 3
    step_days: int = 15
    top_k: int = 10
    sampling: str = 'daily'
    bootstrap_samples: int = 500
    seed: int = 20260907
    fit_final: bool = True
    ablations: bool = True
    def validate(self):
        if self.algorithm not in ('ridge','logistic','lambdamart'):raise ValueError('算法可选 ridge / logistic / lambdamart')
        if self.sampling not in ('daily','episode'):raise ValueError('采样可选 daily / episode')
        for field in ('min_train_days','validation_days','test_days','step_days','top_k'):
            if getattr(self,field)<2:raise ValueError(field+'至少为2')
        if not 0<=self.embargo_days<=60:raise ValueError('间隔天数超出范围')
        if not 50<=self.bootstrap_samples<=5000:raise ValueError('重采样次数取50至5000')


def finite_json(value):
    if isinstance(value,dict):return {str(k):finite_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [finite_json(v) for v in value]
    if isinstance(value,(float,np.floating)):return float(value) if math.isfinite(value) else None
    if isinstance(value,np.integer):return int(value)
    return value


def joined_dataset(db,target,as_of=None,sampling='daily'):
    panels=load_panel(db,as_of)
    with db.connect() as c:
        sql='SELECT payload FROM research_labels';args=[]
        if as_of:sql+=' WHERE available_ts<=?';args=[parse_time(as_of).timestamp()]
        labels=[json.loads(r[0]) for r in c.execute(sql,args)]
    labels={(r['run_id'],r['id'],r['target']):r for r in labels}
    result=[]
    for row in panels:
        label=labels.get((row['run_id'],row['id'],target))
        if not label or label.get('value') is None or label.get('status')!='complete':continue
        if row.get('evaluation_type')=='point_in_time_recalculation':continue
        value=float(label['value'])
        if not math.isfinite(value):continue
        if target.startswith('holdout:'):
            family=target.split(':')[1];remaining=[v for k,v in row.get('family_features',{}).items() if k!=family]
            if not remaining:continue
            features={f:None for f in FEATURE_NAMES}
            for f in ATTENTION_FEATURES:
                values=[v[f] for v in remaining if v.get(f) is not None]
                features[f]=(sum(values) if f=='source_agreement' else float(np.median(values))) if values else None
            # Composite topic, event and actor-pool terms are removed unless a source-free decomposition exists.
            for f in PRICE_FEATURES:features[f]=row['features'].get(f)
            row={**row,'features':features,'heldout_family':family}
        result.append({**row,'y':value,'label_end':label['label_end'],'label_available_at':label['available_at'],
                       'label_record_ids':label.get('record_ids',[]),'target':target})
    # One observation per stock/day or first observation per attention episode.
    selected={}
    for row in sorted(result,key=lambda x:parse_time(x['at'])):
        key=(row['id'],row['episode_id']) if sampling=='episode' and row.get('episode_id') else (row['id'],row['at'][:10])
        if sampling=='episode':selected.setdefault(key,row)
        else:selected[key]=row
    return sorted(selected.values(),key=lambda r:(parse_time(r['at']),r['id']))


def purged_folds(rows,cfg):
    days=sorted({r['at'][:10] for r in rows});folds=[];test_start=cfg.min_train_days+cfg.validation_days+2*cfg.embargo_days
    while test_start<len(days):
        val_end=test_start-cfg.embargo_days;val_start=val_end-cfg.validation_days
        train_end=val_start-cfg.embargo_days
        if train_end<cfg.min_train_days:break
        train_days=set(days[:train_end]);val_days=set(days[val_start:val_end]);test_days=set(days[test_start:min(len(days),test_start+cfg.test_days)])
        val_cutoff=min(val_days)+'T00:00:00+08:00';test_cutoff=min(test_days)+'T00:00:00+08:00'
        train=[r for r in rows if r['at'][:10] in train_days and parse_time(r['label_available_at'])<parse_time(val_cutoff) and parse_time(r['label_end'])<parse_time(val_cutoff)]
        validation=[r for r in rows if r['at'][:10] in val_days and parse_time(r['label_available_at'])<parse_time(test_cutoff) and parse_time(r['label_end'])<parse_time(test_cutoff)]
        test=[r for r in rows if r['at'][:10] in test_days]
        if cfg.sampling=='episode':
            validation_episodes={(r['id'],r.get('episode_id')) for r in validation+test if r.get('episode_id')}
            train=[r for r in train if not r.get('episode_id') or (r['id'],r['episode_id']) not in validation_episodes]
            test_episodes={(r['id'],r.get('episode_id')) for r in test if r.get('episode_id')}
            validation=[r for r in validation if not r.get('episode_id') or (r['id'],r['episode_id']) not in test_episodes]
        if train and validation and test:folds.append({'train':train,'validation':validation,'test':test,
            'train_dates':[days[0],days[train_end-1]],'validation_dates':[min(val_days),max(val_days)],
            'test_dates':[min(test_days),max(test_days)],'purged_train':sum(r['at'][:10] in train_days for r in rows)-len(train)})
        test_start+=max(cfg.step_days,cfg.test_days)
    return folds


def fit_preprocessing(rows,features):
    raw=np.array([[r['features'].get(f) if r['features'].get(f) is not None else np.nan for f in features] for r in rows],float)
    medians=[];lower=[];upper=[]
    for column in raw.T:
        valid=column[np.isfinite(column)]
        medians.append(float(np.median(valid)) if len(valid) else 0.)
        lower.append(float(np.quantile(valid,.01)) if len(valid) else 0.)
        upper.append(float(np.quantile(valid,.99)) if len(valid) else 0.)
    missing=~np.isfinite(raw);values=np.where(missing,np.asarray(medians),raw)
    values=np.clip(values,lower,upper);centers=values.mean(axis=0);scales=values.std(axis=0);scales[scales<1e-8]=1
    p={'medians':medians,'lower':lower,'upper':upper,'centers':centers.tolist(),'scales':scales.tolist()}
    return p,np.column_stack([(values-centers)/scales,missing.astype(float)])


def fit_model(train,features,algorithm,parameter,target,seed):
    prep,x=fit_preprocessing(train,features);y=np.asarray([r['y'] for r in train],float)
    model={'features':features,'preprocessing':prep,'algorithm':algorithm,'parameter':parameter,'target':target}
    # Each date gets equal total weight despite variations in the observable universe.
    counts=defaultdict(int)
    for r in train:counts[r['at'][:10]]+=1
    weight=np.array([1/counts[r['at'][:10]] for r in train]);weight*=len(weight)/weight.sum()
    if algorithm=='ridge':
        fitted=Ridge(alpha=parameter);fitted.fit(x,y,sample_weight=weight)
        model.update(coef=fitted.coef_.tolist(),intercept=float(fitted.intercept_))
    elif algorithm=='logistic':
        if len(set(y))<2 or not set(y)<= {0.,1.}:raise ValueError('logistic目标需要包含0与1两类')
        fitted=LogisticRegression(C=parameter,max_iter=800,random_state=seed,solver='lbfgs')
        fitted.fit(x,y,sample_weight=weight)
        model.update(coef=fitted.coef_[0].tolist(),intercept=float(fitted.intercept_[0]))
    else:
        import lightgbm as lgb
        groups=[];relevance=[];ordered=[]
        bydate=defaultdict(list)
        for i,r in enumerate(train):bydate[r['at'][:10]].append(i)
        for date,indices in sorted(bydate.items()):
            groups.append(len(indices));ordered.extend(indices)
            values=y[indices];cuts=np.quantile(values,[.2,.4,.6,.8])
            relevance.extend(np.searchsorted(cuts,values,side='left').tolist())
        ranker=lgb.LGBMRanker(objective='lambdarank',n_estimators=80,num_leaves=int(parameter),learning_rate=.04,
            min_child_samples=20,reg_lambda=1.0,n_jobs=2,random_state=seed,verbosity=-1,deterministic=True,force_col_wise=True)
        ranker.fit(x[ordered],np.array(relevance),group=groups,sample_weight=weight[ordered])
        model['model_text']=ranker.booster_.model_to_string()
    return model


def rank_ic(y,p):
    if len(y)<3 or np.std(y)<1e-12 or np.std(p)<1e-12:return None
    return float(spearmanr(y,p).statistic)


def ndcg(y,p,k):
    if not len(y):return None
    y=np.asarray(y,float);p=np.asarray(p,float);ranks=rankdata(y,method='average')-1
    relevance=ranks/max(1,len(y)-1) if len(set(y))>2 else y
    gains=np.power(2,np.maximum(relevance,0))-1
    order=np.argsort(-p,kind='stable')[:k];ideal=np.sort(gains)[::-1][:k];discount=1/np.log2(np.arange(2,len(order)+2))
    best=float(np.sum(ideal*discount))
    return float(np.sum(gains[order]*discount)/best) if best else None


def prediction_metrics(rows,predictions,k=10):
    bydate=defaultdict(list)
    for row,pred in zip(rows,predictions):bydate[row['at'][:10]].append((row,float(pred)))
    daily=[];binary=all(r['y'] in (0.,1.) for r in rows)
    for date,pairs in sorted(bydate.items()):
        y=np.array([r['y'] for r,_ in pairs]);p=np.array([v for _,v in pairs]);order=np.argsort(-p,kind='stable');top=order[:k]
        positives=int(np.sum(y>0));hit=int(np.sum(y[top]>0))
        daily.append({'date':date,'n':len(y),'rank_ic':rank_ic(y,p),'ndcg':ndcg(y,p,k),'top_mean':float(np.mean(y[top])),
            'top_positive_rate':hit/len(top),'known_positive_recall':hit/positives if positives else None,'k_observed':len(top),
            'mean_target':float(np.mean(y)), 'brier':float(np.mean((np.clip(p,0,1)-y)**2)) if binary else None})
    result={'n':len(rows),'rows':len(rows),'days':len(daily),'daily':daily,'binary':binary}
    for key in ('rank_ic','ndcg','top_mean','top_positive_rate','known_positive_recall','brier'):
        vals=[r[key] for r in daily if r[key] is not None];result[key]=mean(vals) if vals else None
    result['calibration']=[]
    if binary and len(rows):
        y=np.array([r['y'] for r in rows]);p=np.clip(predictions,0,1)
        for left in np.arange(0,1,.1):
            mask=(p>=left)&(p<left+.1 if left<.89 else p<=1)
            if mask.any():result['calibration'].append({'low':float(left),'high':float(left+.1),'n':int(mask.sum()),'predicted':float(p[mask].mean()),'observed':float(y[mask].mean())})
    return result


def moving_block_ci(values,block=5,samples=500,seed=20260907):
    x=np.array([v for v in values if v is not None and math.isfinite(v)],float)
    if len(x)<4:return {'mean':float(x.mean()) if len(x) else None,'low':None,'high':None,'n':len(x),'block':block}
    rng=np.random.default_rng(seed);block=min(block,len(x));rep=[]
    for _ in range(samples):
        starts=rng.integers(0,len(x),size=math.ceil(len(x)/block))
        draw=np.concatenate([x[(start+np.arange(block))%len(x)] for start in starts])[:len(x)]
        rep.append(draw.mean())
    return {'mean':float(x.mean()),'low':float(np.quantile(rep,.025)),'high':float(np.quantile(rep,.975)),'n':len(x),'block':block}


def block_bootstrap_p(values,block=5,samples=500,seed=20260907):
    x=np.array([v for v in values if v is not None and math.isfinite(v)],float)
    if len(x)<4:return 1.0
    observed=abs(float(x.mean()));null=x-x.mean();rng=np.random.default_rng(seed)
    block=min(block,len(x));extreme=0
    for _ in range(samples):
        starts=rng.integers(0,len(x),size=math.ceil(len(x)/block))
        draw=np.concatenate([null[(start+np.arange(block))%len(x)] for start in starts])[:len(x)]
        extreme+=int(abs(float(draw.mean()))>=observed)
    return (extreme+1)/(samples+1)


def bh_adjust(pvalues):
    vals=np.asarray(pvalues,float);order=np.argsort(vals);adjusted=np.empty(len(vals));minimum=1.
    for position in range(len(vals)-1,-1,-1):
        index=order[position];minimum=min(minimum,vals[index]*len(vals)/(position+1));adjusted[index]=minimum
    return adjusted.tolist()


def drift_stats(train,test,features):
    result=[]
    for f in features:
        a=np.array([r['features'][f] for r in train if r['features'].get(f) is not None]);b=np.array([r['features'][f] for r in test if r['features'].get(f) is not None])
        if len(a)<10 or len(b)<10:continue
        bins=np.unique(np.quantile(a,np.linspace(0,1,11)))
        if len(bins)<3:continue
        bins[0],bins[-1]=-np.inf,np.inf
        p=np.histogram(a,bins)[0]+.5;q=np.histogram(b,bins)[0]+.5;p=p/p.sum();q=q/q.sum()
        result.append({'feature':f,'psi':float(np.sum((q-p)*np.log(q/p))), 'train_missing':1-len(a)/len(train),'test_missing':1-len(b)/len(test)})
    return result


def run_experiment(engine,config=None,as_of=None,progress=None):
    cfg=config if isinstance(config,ExperimentConfig) else ExperimentConfig(**(config or {}));cfg.validate()
    clock=as_of or stamp();rows=joined_dataset(engine.db,cfg.target,clock,cfg.sampling)
    days=sorted({r['at'][:10] for r in rows});folds=purged_folds(rows,cfg)
    result={'experiment_id':uid('experiment-v2',stamp(),asdict(cfg)), 'created_at':stamp(),'as_of':clock,
        'config':asdict(cfg),'config_hash':uid(asdict(cfg)),'dataset_hash':uid([(r['run_id'],r['id'],r['y'],r['label_available_at']) for r in rows]),
        'rows':len(rows),'days':len(days),'synthetic':engine.mode=='demo','target':cfg.target,'status':'running',
        'target_population':'observed labels in the frozen full-market panel','folds':[],'variants':{},'model_ids':[]}
    if not folds:
        result.update(status='collecting',required_days=cfg.min_train_days+cfg.validation_days+2*cfg.embargo_days+1)
        _save_experiment(engine.db,result);return result
    variants={'full':FEATURE_NAMES}
    if cfg.ablations:variants.update({'price':PRICE_FEATURES,'attention':ATTENTION_FEATURES,'attention_events':ATTENTION_FEATURES+EVENT_FEATURES,
        'attention_structure':ATTENTION_FEATURES+STRUCTURE_FEATURES,'events':EVENT_FEATURES})
    trials=[];predicted=defaultdict(list);rowsets=defaultdict(list);lastmodels={}
    grid=[.1,1.,10.] if cfg.algorithm in ('ridge','logistic') else [7,15,31]
    for fi,fold in enumerate(folds):
        if progress:progress(f'研究实验 {fi+1}/{len(folds)}：训练、验证与后续区间',fi,len(folds))
        test_predictions={}
        for variant,features in variants.items():
            attempts=[]
            for param in grid:
                try:
                    model=fit_model(fold['train'],features,cfg.algorithm,param,cfg.target,cfg.seed)
                    pv=predict(model,fold['validation']);yv=np.array([r['y'] for r in fold['validation']])
                    if cfg.algorithm=='logistic':objective=float(np.mean((pv-yv)**2))
                    else:
                        stat=prediction_metrics(fold['validation'],pv,cfg.top_k)
                        objective=-(stat['ndcg'] if stat['ndcg'] is not None else -1)
                    attempts.append((objective,model))
                    trials.append({'fold':fi,'variant':variant,'parameter':param,'validation_objective':objective,'status':'complete'})
                except ValueError as exc:trials.append({'fold':fi,'variant':variant,'parameter':param,'status':'no_fit','message':str(exc)})
            if not attempts:continue
            _,best=min(attempts,key=lambda pair:pair[0]);pv=predict(best,fold['validation'])
            if cfg.algorithm=='logistic' and len(fold['validation'])>=100 and len(set(pv))>=10:
                calibrated=IsotonicRegression(out_of_bounds='clip',y_min=0,y_max=1).fit(pv,[r['y'] for r in fold['validation']])
                best['calibration']={'x':calibrated.X_thresholds_.tolist(),'y':calibrated.y_thresholds_.tolist()}
            pt=predict(best,fold['test']);predicted[variant].extend(map(float,pt));rowsets[variant].extend(fold['test'])
            test_predictions[variant]=prediction_metrics(fold['test'],pt,cfg.top_k)
            if variant=='full':lastmodels[variant]=best
        result['folds'].append({'fold':fi,'train_n':len(fold['train']),'validation_n':len(fold['validation']),'test_n':len(fold['test']),
            'train_dates':fold['train_dates'],'validation_dates':fold['validation_dates'],'test_dates':fold['test_dates'],
            'purged_train':fold['purged_train'],'test_metrics':test_predictions,'feature_drift':drift_stats(fold['train'],fold['test'],FEATURE_NAMES)})
    # Fixed transparent baselines use exactly the same held-out rows as the full model.
    if rowsets.get('full'):
        for name,feature in [('baseline_heat_level','attention_level'),('baseline_heat_change','attention_velocity'),('baseline_price_change','return_1d')]:
            rowsets[name]=list(rowsets['full']);predicted[name]=[float(r['features'].get(feature) or 0.) for r in rowsets[name]]
    for variant,preds in predicted.items():
        stats=prediction_metrics(rowsets[variant],np.array(preds),cfg.top_k)
        if cfg.algorithm!='logistic' or variant.startswith('baseline_'):
            stats['brier']=None;stats['calibration']=[]
            for day in stats['daily']:day['brier']=None
        stats['rank_ic_ci']=moving_block_ci([r['rank_ic'] for r in stats['daily']],block=max(3,cfg.embargo_days),samples=cfg.bootstrap_samples,seed=cfg.seed)
        stats['top_mean_ci']=moving_block_ci([r['top_mean'] for r in stats['daily']],block=max(3,cfg.embargo_days),samples=cfg.bootstrap_samples,seed=cfg.seed)
        result['variants'][variant]=stats
    comparisons=[]
    full={r['date']:r for r in result['variants'].get('full',{}).get('daily',[])}
    for variant,stats in result['variants'].items():
        if variant=='full':continue
        deltas=[full[r['date']]['top_mean']-r['top_mean'] for r in stats['daily'] if r['date'] in full]
        n=len(deltas);block=max(3,cfg.embargo_days)
        p=block_bootstrap_p(deltas,block=block,samples=cfg.bootstrap_samples,seed=cfg.seed)
        comparisons.append({'baseline':variant,'matched_dates':n,'top_mean_delta':moving_block_ci(deltas,block=block,samples=cfg.bootstrap_samples,seed=cfg.seed),'paired_p':p,'p_method':'centered_circular_block_bootstrap'})
    qs=bh_adjust([r['paired_p'] for r in comparisons]) if comparisons else []
    for r,q in zip(comparisons,qs):r['bh_q']=q
    result.update(comparisons=comparisons,trials=trials,status='complete' if predicted else 'no_fit')
    if cfg.fit_final and predicted:
        # Final model uses only matured labels; final validation remains later than final training.
        validation_days=set(days[-cfg.validation_days:]);cutoff=min(validation_days)+'T00:00:00+08:00'
        embargo_days=set(days[max(0,len(days)-cfg.validation_days-cfg.embargo_days):len(days)-cfg.validation_days])
        final_train=[r for r in rows if r['at'][:10] not in validation_days|embargo_days and parse_time(r['label_available_at'])<parse_time(cutoff) and parse_time(r['label_end'])<parse_time(cutoff)]
        final_val=[r for r in rows if r['at'][:10] in validation_days]
        choices=[]
        for param in grid:
            try:
                model=fit_model(final_train,FEATURE_NAMES,cfg.algorithm,param,cfg.target,cfg.seed)
                pred=predict(model,final_val);target=np.array([r['y'] for r in final_val])
                objective=float(np.mean((pred-target)**2)) if cfg.algorithm=='logistic' else -(prediction_metrics(final_val,pred,cfg.top_k).get('ndcg') or 0)
                choices.append((objective,model))
                trials.append({'fold':'final','variant':'full','parameter':param,'validation_objective':objective,'status':'complete'})
            except ValueError:continue
        if choices:
            _,model=min(choices,key=lambda p:p[0]);available=max([stamp(),clock]+[r['label_available_at'] for r in final_train+final_val],key=parse_time)
            if cfg.algorithm=='logistic' and len(final_val)>=100:
                pv=predict(model,final_val)
                if len(set(pv))>=10:
                    ir=IsotonicRegression(out_of_bounds='clip',y_min=0,y_max=1).fit(pv,[r['y'] for r in final_val])
                    model['calibration']={'x':ir.X_thresholds_.tolist(),'y':ir.y_thresholds_.tolist()}
            model_id=uid('model-v2',result['experiment_id'],model)
            model.update(model_id=model_id,head='discovery' if cfg.target.startswith('attention') or cfg.target.startswith('holdout') else 'research',
                target=cfg.target,available_at=available,trained_at=stamp(),active=not cfg.target.startswith('holdout:'),synthetic=engine.mode=='demo',
                experiment_id=result['experiment_id'],train_rows=len(final_train),validation_rows=len(final_val),
                train_end=max(r['at'] for r in final_train),label_available_through=max(r['label_available_at'] for r in final_train+final_val),feature_schema=uid(FEATURE_NAMES))
            with engine.db.lock,engine.db.connect() as c:c.execute('INSERT OR REPLACE INTO model_registry VALUES(?,?,?,?,?)',
                (model_id,stamp(),cfg.target,parse_time(available).timestamp(),json.dumps(finite_json(model),ensure_ascii=False,allow_nan=False)))
            result['model_ids'].append(model_id)
    # Source-only ordering and model ordering are compared on a strict common cohort.
    source_comparisons=[]
    fullrows=rowsets.get('full',[]);fullpred=predicted.get('full',[])
    sources=sorted({source for row in fullrows for source in row.get('source_scores',{})})
    for source in sources:
        selected=[i for i,row in enumerate(fullrows) if row.get('source_scores',{}).get(source) is not None]
        if len(selected)<3:continue
        cohort=[fullrows[i] for i in selected];y_source=[r['source_scores'][source] for r in cohort];y_model=[fullpred[i] for i in selected]
        source_comparisons.append({'source':source,'matched_rows':len(cohort),'cohort_hash':uid([(r['run_id'],r['id']) for r in cohort]),
            'source_rank':prediction_metrics(cohort,y_source,cfg.top_k),'model_on_same_cohort':prediction_metrics(cohort,y_model,cfg.top_k)})
    result['source_comparisons']=source_comparisons
    # Store aligned predictions for independently reproducible paired comparisons.
    result['predictions']=[{'run_id':r['run_id'],'id':r['id'],'at':r['at'],'y':r['y'],'prediction':p,'variant':v}
        for v in predicted for r,p in zip(rowsets[v],predicted[v])]
    _save_experiment(engine.db,result)
    return finite_json(result)


def _save_experiment(db,result):
    with db.lock,db.connect() as c:c.execute('INSERT OR REPLACE INTO experiments VALUES(?,?,?)',
        (result['experiment_id'],result['created_at'],json.dumps(finite_json(result),ensure_ascii=False,allow_nan=False)))


def list_experiments(db):
    with db.connect() as c:rows=[json.loads(r[0]) for r in c.execute('SELECT payload FROM experiments ORDER BY created_at DESC')]
    return [{k:v for k,v in r.items() if k not in ('predictions','trials','folds')} for r in rows]
