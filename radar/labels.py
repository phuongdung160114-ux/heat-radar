"""Forward observations on frozen universes; labels never enter contemporary features."""
from __future__ import annotations
from bisect import bisect_left
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median
import json, math
import numpy as np
from .common import parse_time, stamp, now, number, uid, CN
from .features import load_panel, residualize

ATTENTION_HORIZONS={'60m':60,'1d':1,'3d':3}
PRICE_HORIZONS=('30m','60m','eod','1d','3d','5d','10d')


def advance_minutes(calendar,at,minutes):
    """Move forward by open-session minutes, skipping lunch and closed dates."""
    current=parse_time(at).astimezone(CN);remaining=float(minutes)
    for day in sorted(d for m,d in calendar.by_key if m=='A' and d>=current.date().isoformat()):
        for start,end in calendar.spans(day):
            if end<=current:continue
            left=max(start,current);available=(end-left).total_seconds()/60
            if available>=remaining:return left+timedelta(minutes=remaining)
            remaining-=available
    return None


def deadline(calendar,at,horizon):
    t=parse_time(at).astimezone(CN)
    if horizon.endswith('m'):return advance_minutes(calendar,t,int(horizon[:-1]))
    if horizon=='eod':
        days=sorted(d for m,d in calendar.by_key if m=='A' and d>=t.date().isoformat() and calendar.by_key[(m,d)].get('is_open'))
        for day in days:
            end=calendar.spans(day)[-1][1]
            if end>t:return end
        return None
    days=sorted(d for m,d in calendar.by_key if m=='A' and d>t.date().isoformat() and calendar.by_key[(m,d)].get('is_open'))
    n=int(horizon[:-1])
    if len(days)<n:return None
    # Attention comparisons keep the signal clock; return labels end at session close.
    day=days[n-1]
    return parse_time(day+'T'+t.strftime('%H:%M:%S')+'+08:00')


def _matched_attention(left,right,exclude=None,only=None):
    """Use actual within-source count/metric changes and rank improvements, not stage labels."""
    a=left.get('measurements',{});b=right.get('measurements',{});family=defaultdict(list);used=[]
    for key,x in a.items():
        y=b.get(key)
        if not y or x.get('family')==exclude or (only and x.get('family')!=only):continue
        if x.get('panel_version')!=y.get('panel_version'):continue
        if x.get('at') and y.get('at') and parse_time(y['at'])<=parse_time(x['at']):continue
        v,w=number(x.get('value')),number(y.get('value'))
        if v is None or w is None:continue
        if x.get('metric')=='rank':
            if v<=0 or w<=0:continue
            change=math.log(v/w)
        elif x.get('metric') in ('followers_net_30','rank_change','rank_gain'):continue
        elif x.get('unit') in ('ratio','%','share'):continue
        elif v>=0 and w>=0:
            # Counts and provider indexes receive distinct smoothing; units remain explicit.
            smooth=20. if x.get('unit') in ('people','posts','persons','人','条') else 1.
            change=math.log((w+smooth)/(v+smooth))
        else:continue
        family[x['family']].append(change);used.extend(x.get('ids',[])+y.get('ids',[]))
    changes={f:median(v) for f,v in family.items()}
    return changes,sorted(set(used))


def attention_outcome(left,right,only=None):
    changes,ids=_matched_attention(left,right,only=only)
    need=1 if only else 2
    if len(changes)<need:return None,changes,ids
    # Predeclared outcome: matched source-family median growth of at least 25%.
    return float(median(changes.values())>=math.log(1.25)),changes,ids


class PriceIndex:
    def __init__(self,records):
        self.series=defaultdict(lambda:defaultdict(list))
        for r in records:
            metric=r.get('metric');e=r.get('extra',{})
            if metric not in ('daily_bar','minute_bar','spot','quote'):continue
            price=number(e.get('close') or e.get('price') or r.get('value'))
            if not price or price<=0:continue
            at=parse_time(r['occurred_at']);adjust=e.get('adjust','none')
            if metric=='daily_bar':at=parse_time(e.get('date',r['occurred_at'][:10])+'T15:00:00+08:00')
            # A daily close can only be labeled after its closing timestamp is available.
            if parse_time(r['observed_at'])<at:continue
            group=(r['source'],adjust,metric)
            for oid in r['objects']:self.series[oid][group].append({'at':at,'price':price,'record_id':r['record_id'],'observed_at':r['observed_at'],'extra':e})
        for groups in self.series.values():
            for key,rs in groups.items():
                # Last obtained revision at each event time wins within the query's knowledge boundary.
                unique={}
                for r in sorted(rs,key=lambda r:parse_time(r['observed_at'])):unique[r['at']]=r
                groups[key]=sorted(unique.values(),key=lambda r:r['at'])

    def change(self,oid,signal,end,horizon):
        signal=parse_time(signal);end=parse_time(end);choices=[]
        for (source,adjust,metric),rows in self.series.get(oid,{}).items():
            if metric=='daily_bar' and horizon in ('30m','60m','eod'):continue
            times=[r['at'] for r in rows];j=bisect_left(times,signal)
            if metric=='daily_bar':
                # Close reference is explicitly distinct from a signal-time minute reference.
                reference=next((r for r in rows[max(0,j-1):j+3] if r['at'].date()==signal.date()),None)
                if reference is None:continue
                ref_kind='signal_day_close'
                target=end.replace(hour=15,minute=0,second=0,microsecond=0)
                dest=next((r for r in rows if r['at']==target),None)
            else:
                # Use a quote known at/before the signal; no future close masquerades as entry price.
                if j<len(rows) and rows[j]['at']==signal:reference=rows[j]
                else:reference=rows[j-1] if j else None
                if reference is None or signal-reference['at']>timedelta(minutes=10):continue
                target=end if horizon.endswith('m') or horizon=='eod' else end.replace(hour=15,minute=0,second=0,microsecond=0)
                k=bisect_left(times,target);dest=rows[k] if k<len(rows) else None
                if dest is None or dest['at']-target>timedelta(minutes=10):continue
                ref_kind='signal_quote'
            if dest is None or dest['at']<=reference['at']:continue
            # Intraday unadjusted snapshots are only comparable within a date.
            if adjust not in ('hfq','qfq','index') and reference['at'].date()!=dest['at'].date():continue
            choices.append((metric!='daily_bar',adjust in ('hfq','qfq','index'),source,{
                'value':dest['price']/reference['price']-1,'reference_kind':ref_kind,'reference_at':stamp(reference['at']),
                'label_end':stamp(dest['at']),'source':source,'adjust':adjust,
                'available_at':max(reference['observed_at'],dest['observed_at'],key=parse_time),
                'record_ids':[reference['record_id'],dest['record_id']]}))
        return max(choices,key=lambda c:c[:3])[-1] if choices else None


def _label(row,target,end,clock,value=None,status='missing_observation',**extra):
    return {'run_id':row['run_id'],'id':row['id'],'target':target,'signal_at':row['at'],
        'label_end':stamp(end) if end else None,'available_at':stamp(clock),'value':value,
        'status':status,'synthetic':row.get('synthetic',False),**extra}


def update_labels(engine,as_of=None,run_ids=None,progress=None):
    clock=parse_time(as_of or now());panels=load_panel(engine.db,clock)
    byobject=defaultdict(list);byrun=defaultdict(list)
    for r in panels:byobject[r['id']].append(r);byrun[r['run_id']].append(r)
    timeline={oid:[parse_time(r['at']) for r in rs] for oid,rs in byobject.items()}
    price_records=engine.db.read(kind='quote',as_of=clock)
    prices=PriceIndex(price_records);out=[];runs={r['run_id']:r for r in engine.db.runs(100000)}
    ids=set(run_ids) if run_ids else set(byrun)
    for counter,(rid,rows) in enumerate(byrun.items()):
        if rid not in ids:continue
        if progress:progress('生成真实后续观测标签',counter,len(ids))
        run=runs.get(rid,{})
        if run.get('evaluation_type')=='point_in_time_recalculation':continue
        frozen=run.get('memberships',[]);pools=defaultdict(set)
        for e in frozen:pools[e['topic_id']].add(e['symbol'])
        for row in rows:
            if row.get('evaluation_type') in ('point_in_time_recalculation','synthetic_statistical_fixture'):continue
            at=parse_time(row['at']);sequence=byobject[row['id']];times=timeline[row['id']]
            for h in ATTENTION_HORIZONS:
                end=deadline(engine.calendar,at,h);base=_label(row,'attention_'+h,end,clock)
                if end is None or end>clock:base['status']='pending';out.append(base);continue
                idx=bisect_left(times,end);future=sequence[idx] if idx<len(sequence) else None
                # Sparse sessions are not silently stretched across several trading days.
                tolerance=timedelta(minutes=30 if h=='60m' else 90)
                if future is None or parse_time(future['at'])-end>tolerance:out.append(base);continue
                value,changes,ids_used=attention_outcome(row,future)
                base.update(value=value,status='complete' if value is not None else 'source_coverage',family_changes=changes,
                    label_end=future['at'],available_at=future['at'],record_ids=ids_used,definition='matched_family_median_growth_ge_25pct',
                    future_run_id=future['run_id'],matched_family_count=len(changes))
                out.append(base)
                for family in sorted(set(x['family'] for x in row.get('measurements',{}).values())):
                    y,ch,used=attention_outcome(row,future,only=family)
                    out.append({**base,'target':'holdout:'+family+':'+h,'value':y,'status':'complete' if y is not None else 'source_coverage',
                        'heldout_family':family,'record_ids':used,'family_changes':ch})
            for h in PRICE_HORIZONS:
                end=deadline(engine.calendar,at,h)
                if end and h.endswith('d'):end=end.replace(hour=15,minute=0,second=0,microsecond=0)
                target='return_'+h;base=_label(row,target,end,clock)
                if end is None or end>clock:base['status']='pending';out.append(base);continue
                change=prices.change(row['id'],at,end,h)
                if change:base.update(**change,status='complete')
                out.append(base)
        # All comparative outcomes share the frozen feature universe and a common reference convention.
        run_labels=[r for r in out if r['run_id']==rid and r['target'].startswith('return_')]
        featuremap={r['id']:r for r in rows}
        for h in PRICE_HORIZONS:
            raw={r['id']:r for r in run_labels if r['target']=='return_'+h and r['status']=='complete'}
            for oid,label in raw.items():
                compatible={s:r for s,r in raw.items() if r['reference_kind']==label['reference_kind']}
                peers={s for symbols in pools.values() if oid in symbols for s in symbols}-{oid}
                valid=[s for s in peers if s in compatible]
                industry=featuremap[oid].get('industry')
                sector=[s for s in compatible if s!=oid and industry and featuremap[s].get('industry')==industry]
                for prefix,group in [('theme_relative',valid),('industry_relative',sector)]:
                    n=len(group);minimum=3
                    if n<minimum:continue
                    peer=mean(compatible[s]['value'] for s in group)
                    out.append({**label,'target':prefix+'_'+h,'value':label['value']-peer,'peer_count':n,
                        'peer_ids':sorted(group),'available_at':max([label['available_at']]+[compatible[s]['available_at'] for s in group],key=parse_time),
                        'record_ids':sorted(set(label.get('record_ids',[])+[i for s in group for i in compatible[s].get('record_ids',[])]))})
            # Future cross-section regression is a label, never an input. Industry fixed effects + frozen style.
            for ref_kind in sorted({r['reference_kind'] for r in raw.values()}):
                keys=sorted(s for s,r in raw.items() if r['reference_kind']==ref_kind)
                industries=sorted({featuremap[s].get('industry') or 'unknown' for s in keys})
                control=[]
                for s in keys:
                    f=featuremap[s]['features'];values=[number(f.get(k)) for k in ('return_1d','turnover','log_cap')]
                    if any(v is None for v in values):control.append([float('nan')]*(3+max(0,len(industries)-1)));continue
                    control.append(values+[float((featuremap[s].get('industry') or 'unknown')==g) for g in industries[1:]])
                if not keys:continue
                residual=residualize([raw[s]['value'] for s in keys],control)
                all_ids=sorted(set(i for s in keys for i in raw[s].get('record_ids',[])))
                for s,y in zip(keys,residual):
                    if not np.isfinite(y):continue
                    out.append({**raw[s],'target':'style_relative_'+h,'value':float(y),'peer_count':len(keys)-1,
                        'definition':'cross_section_industry_style_residual','label_control_features':['return_1d','turnover','log_cap'],
                        'available_at':max(raw[s]['available_at'] for s in keys),'record_ids':all_ids})
    # Preserve labels' original knowledge times; pending rows are refreshed when new data arrive.
    with engine.db.lock,engine.db.connect() as c:
        c.executemany('INSERT OR REPLACE INTO research_labels VALUES(?,?,?,?,?)',[(r['run_id'],r['id'],r['target'],parse_time(r['available_at']).timestamp(),json.dumps(r,ensure_ascii=False,allow_nan=False)) for r in out])
    counts=defaultdict(lambda:defaultdict(int))
    for r in out:counts[r['target']][r['status']]+=1
    result={'as_of':stamp(clock),'rows':len(out),'runs':len(ids),'targets':dict(counts),'synthetic':engine.mode=='demo'}
    engine.db.set_meta('labels_last',result);return result


def label_status(engine):
    with engine.db.connect() as c:
        labels=[json.loads(r[0]) for r in c.execute('SELECT payload FROM research_labels')]
        panel_count=c.execute('SELECT COUNT(*) FROM feature_panels').fetchone()[0]
    targets=defaultdict(lambda:{'total':0,'complete':0,'pending':0,'dates':set(),'positive':0})
    for r in labels:
        t=targets[r['target']];t['total']+=1
        if r['status']=='complete':t['complete']+=1;t['dates'].add(r['signal_at'][:10]);t['positive']+=int(r.get('value',0)>0)
        elif r['status']=='pending':t['pending']+=1
    for t in targets.values():t['dates']=len(t['dates']);t['coverage']=t['complete']/t['total'] if t['total'] else None
    return {'panel_rows':panel_count,'targets':dict(targets),'last_update':engine.db.get_meta('labels_last',None)}


def research_cycle(engine,progress=None,as_of=None):
    cfg=engine.settings.data.get('research',{});clock=as_of or stamp();result={}
    last_labels=engine.db.get_meta('labels_last',{})
    previous=last_labels.get('as_of')
    if cfg.get('auto_update_labels',True) and (not previous or (parse_time(clock)-parse_time(previous)).total_seconds()>=cfg.get('label_refresh_seconds',1800)):
        result['labels']=update_labels(engine,clock,progress=progress)
    if cfg.get('auto_train',True):
        from .learning import run_experiment
        counts=label_status(engine)['targets'];last=engine.db.get_meta('automatic_training_date','')
        # One daily update; each head must independently have a mature panel.
        if last!=clock[:10]:
            results=[]
            for target,algorithm in [('attention_1d','logistic'),('style_relative_3d','ridge')]:
                if counts.get(target,{}).get('dates',0)<cfg.get('min_train_days',60)+cfg.get('validation_days',15)+2*cfg.get('embargo_days',3)+1:continue
                settings={k:cfg[k] for k in ('min_train_days','validation_days','test_days','embargo_days','step_days','top_k','bootstrap_samples','sampling') if k in cfg}
                r=run_experiment(engine,{**settings,'target':target,'algorithm':algorithm},clock,progress);results.append(r['experiment_id'])
            if results:engine.db.set_meta('automatic_training_date',clock[:10]);result['experiments']=results
    return result
