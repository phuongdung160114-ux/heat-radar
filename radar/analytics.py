"""Session-aligned attention, actor panels and information dynamics."""
from __future__ import annotations
from collections import defaultdict,Counter
from datetime import datetime,timedelta
from statistics import mean,median
import math
from .common import CN,NY,parse_time,stamp,ratio,floor_time,number
from .vendor.session_tools import aggregate_window,compare_snapshot
from .vendor.metrics import trading_metrics

def percentile(x,values):
    vals=[v for v in values if v is not None]
    return (sum(v<x for v in vals)+.5*sum(v==x for v in vals))/len(vals) if vals and x is not None else None

def summaries(value,history,expected):
    out={}
    for n in (1,3,5,20,60,250):
        days=expected[-n:];vals=[history[d] for d in days if history.get(d) is not None]
        b=mean(vals) if vals else None
        out[str(n)]=dict(mean=b,n=len(vals),requested=n,missing_dates=[d for d in days if history.get(d) is None][:20],missing_count=sum(history.get(d) is None for d in days),
            complete=len(days)==n and len(vals)==n,ratio=ratio(value,b),delta=value-b if value is not None and b is not None else None,
            percentile=percentile(value,vals))
    sequence=[history.get(d) for d in expected]+[value]
    recent=sequence[-3:];base=sequence[-23:-3]
    out['g_3_20']=ratio(mean(recent),mean(base)) if len(recent)==3 and len(base)==20 and all(v is not None for v in recent+base) else None
    return out

def effective(r):return parse_time(r['occurred_at'])

def robust(value,values):
    values=[v for v in values if v is not None and v>=0]
    if value is None or value<0 or not values:return dict(h=None,percentile=None,n=0)
    vals=[math.log1p(v) for v in values];mid=median(vals);mad=median(abs(v-mid) for v in vals)
    return dict(h=(math.log1p(value)-mid)/(1.4826*mad) if mad>1e-9 and len(vals)>=4 else None,
                percentile=percentile(value,values),n=len(values))

def snapshot_evidence(rows,at,calendar,thresholds,known_at=None):
    from .signals import derive_flows, enrich_series
    rows = derive_flows(rows)
    at=parse_time(at);known_at=parse_time(known_at or at);groups=defaultdict(list);out=[]
    for r in rows:
        if r['kind']!='snapshot' or r.get('value') is None or effective(r)>at or parse_time(r['observed_at'])>known_at:continue
        key=tuple(r.get(k) for k in ('source','panel_id','metric','unit','window_kind','pool_version'));groups[key].append(r)
    for key,series in groups.items():
        series.sort(key=lambda r:(effective(r),parse_time(r['observed_at'])));cur=series[-1];metric=cur['metric'];extra=cur.get('extra',{})
        cadence=max(60,number(extra.get('cadence_seconds')) or 300);w=max(1800,int(cadence));daily=cur['window_kind']=='provider_daily_history';t=effective(cur)
        market=extra.get('market','A');zone=NY if market=='US' and extra.get('timestamp_timezone')!='Asia/Shanghai' else CN
        local=t.astimezone(zone);day=local.date().isoformat();cut=local.strftime('%H:')+f'{local.minute//5*5:02d}';expected=calendar.open_dates(market,before=day,limit=250)
        def history_for(row):
            dt=effective(row).astimezone(zone);clock=(dt.hour,dt.minute//5);hist={}
            for r in series:
                d=effective(r).astimezone(zone);date=d.date().isoformat()
                if date<dt.date().isoformat() and (daily or (d.hour,d.minute//5)==clock):hist[date]=r['value']
            return hist
        def near(target):
            found=[r for r in series if target-timedelta(seconds=max(300,cadence*.25))<=effective(r)<=target and effective(r).astimezone(zone).date()==local.date()]
            return found[-1] if found else None
        older=[r for r in series if effective(r).astimezone(zone).date()<local.date()]
        prev=(older[-1] if older else None) if daily else near(t-timedelta(seconds=w));prev2=(older[-2] if len(older)>1 else None) if daily else near(t-timedelta(seconds=2*w))
        hist=history_for(cur);comp=summaries(cur['value'],hist,expected);vals=[hist[d] for d in expected[-20:] if d in hist];rob=robust(cur['value'],vals)
        pv=prev['value'] if prev else None;pv2=prev2['value'] if prev2 else None
        q=comp['20']['ratio'] if comp['20']['n']>=thresholds['min_history5'] else None
        def prev_q(row):
            if not row:return None
            h=history_for(row);v=[h[d] for d in expected[-20:] if d in h]
            return ratio(row['value'],mean(v)) if len(v)>=thresholds['min_history5'] else None
        qp=prev_q(prev);qp2=prev_q(prev2);velocity=q-qp if q is not None and qp is not None else None
        first=min(parse_time(r['observed_at']) for r in series);observations=len({int(effective(r).timestamp())//900 for r in series if effective(r).astimezone(zone).date()==local.date()})
        e=dict(source=cur['source'],family=cur['family'],panel_id=cur['panel_id'],market=market,metric=metric,unit=cur['unit'],value=cur['value'],previous=pv,previous2=pv2,
            at=cur['occurred_at'],observed_at=cur['observed_at'],first_observed_at=stamp(first),timestamp_basis=extra.get('timestamp_basis',extra.get('time_precision','source_time')),
            pool_version=cur['pool_version'],window_kind=cur['window_kind'],window_minutes=1440 if daily else w/60,cutoff_key=cut,comparisons=comp,history20_n=comp['20']['n'],history5_n=comp['5']['n'],
            history_ok=comp['20']['n']>=thresholds['min_history20'] and comp['5']['n']>=thresholds['min_history5'],h=rob['h'],anomaly_percentile=rob['percentile'],
            delta=cur['value']-pv if pv is not None else None,velocity=velocity,acceleration=q-2*qp+qp2 if None not in (q,qp,qp2) else None,q=q,q_prev=qp,
            rising=False,accelerating=False,persistence=0,high=False,new_entry=False,observations=observations,eligible=False,basis='platform_proxy',
            stale=(at-t).total_seconds()>(4*86400 if daily else max(cadence*2.5,900)),coverage='platform_snapshot',record_ids=[cur['record_id']],list_size=extra.get('list_size') or extra.get('returned_universe'),note='')
        if metric=='rank':
            gain=ratio(pv-cur['value'],pv) if pv is not None else None;oldgain=ratio(pv2-pv,pv2) if pv is not None and pv2 is not None else None
            hist_pct=1-percentile(cur['value'],vals) if vals else None
            high=(hist_pct is not None and hist_pct>=.8 and comp['20']['mean']>cur['value']) or cur['value']<=thresholds.get('top_rank',20)
            new=(at-first).total_seconds()<=3*86400 and cur['value']<=thresholds.get('new_entry_rank',100) and not e['history_ok']
            e.update(delta=pv-cur['value'] if pv is not None else None,velocity=gain,acceleration=gain-oldgain if gain is not None and oldgain is not None else None,q=None,q_prev=None,h=None,
                anomaly_percentile=hist_pct,rank_gain_fraction=gain,rank_vs20=comp['20']['mean']-cur['value'] if vals else None,high=high,
                rising=gain is not None and gain>=thresholds.get('rank_gain_fraction',.15),accelerating=gain is not None and oldgain is not None and gain>oldgain,
                persistence=2 if high and pv is not None and (pv<=thresholds.get('top_rank',20) or (vals and (1-percentile(pv,vals))>=.8)) else (1 if high else 0),new_entry=new or bool(extra.get('discovery_trigger')),eligible=not daily)
            for v in comp.values():
                if isinstance(v,dict):v['ratio']=None
            comp['g_3_20']=None
        elif metric in ('native_hot','search_index','participants_30','followers_net_30','discussion_index','discussion_total','read_count','post_count','unique_visitors','attention_index'):
            high=q is not None and q>=thresholds['ratio20']
            persistence=2 if high and qp is not None and qp>=thresholds['ratio20'] else (1 if high else 0)
            e.update(high=high,rising=high and velocity is not None and velocity>0,accelerating=velocity is not None and velocity>=thresholds['acceleration'],persistence=persistence,eligible=not daily,
                new_entry=not e['history_ok'] and cur['value']>=thresholds['min_absolute_new'] and (pv is None or cur['value']>pv))
        elif metric in ('followers_total','new_fan_share','loyal_fan_share'):
            e.update(q=None,q_prev=None,h=None,anomaly_percentile=None,velocity=None,acceleration=None,note='平台存量' if metric=='followers_total' else '平台讨论指标')
        elif metric=='keyword_context_heat':e.update(q=None,h=None,anomaly_percentile=None,note='个股语境关键词')
        e['model_eligible']=e['eligible'] or (daily and metric in ('native_hot','attention_index','search_index','post_count','participants_30','discussion_index'))
        if daily:e['note']='独立日级关注背景'
        e = enrich_series(e, series, at, calendar, thresholds, known_at)
        out.append(e)
    return out

def coverage_ok(coverage,source,panel,start,end,at,object_id):
    spans=[]
    for r in coverage:
        ex=r.get('extra',{})
        if r['source']!=source or r['panel_id']!=panel or not ex.get('complete') or parse_time(r['observed_at'])>at:continue
        if r.get('objects') and object_id not in r['objects']:continue
        try:a,b=parse_time(ex['start']),parse_time(ex['end'])
        except (ValueError,KeyError):continue
        if b>start and a<end:spans.append((max(a,start),min(b,end)))
    cursor=start
    for a,b in sorted(spans):
        if a>cursor:return False
        cursor=max(cursor,b)
    return cursor>=end

def participant_evidence(records,coverage,object_id,at,calendar,thresholds,parent=None,known_at=None,minutes=30):
    from bisect import bisect_left
    at=parse_time(at);known_at=parse_time(known_at or at)
    records=[r for r in records if parse_time(r['observed_at'])<=known_at and effective(r)<=at]
    groups=defaultdict(list);output=[]
    for r in records:
        if r['kind']=='participation' and object_id in r['objects']:
            groups[(r['source'],r['panel_id'],r['entity_type'],r.get('extra',{}).get('market','A'))].append(r)
    coverage_seeds={}
    for cov in coverage:
        ex=cov.get('extra',{})
        if not ex.get('complete') or (cov.get('objects') and object_id not in cov['objects']) or parse_time(cov['observed_at'])>known_at:continue
        key=(cov['source'],cov['panel_id'],ex.get('entity_type','account'),ex.get('market','A'))
        if not any(k[:2]==key[:2] for k in groups):groups[key]=[]
        coverage_seeds[key]=cov
    for (source,panel,entity_type,market),rows in groups.items():
        latest_row=max(rows,key=lambda r:(effective(r),parse_time(r['observed_at']))) if rows else coverage_seeds[(source,panel,entity_type,market)]
        version=latest_row.get('extra',{}).get('attribution_versions',{}).get(object_id,'provider:'+latest_row.get('pool_version','v1'))
        rows=[r for r in rows if r.get('extra',{}).get('attribution_versions',{}).get(object_id,'provider:'+r.get('pool_version','v1'))==version]
        valid_from=latest_row.get('extra',{}).get('attribution_valid_from_by_object',{}).get(object_id)
        zone=NY if market=='US' else CN; local=at.astimezone(zone)
        end=local.replace(minute=local.minute//5*5,second=0,microsecond=0)
        opening=end.replace(hour=9,minute=30)
        if end<=opening:opening=end.replace(hour=0,minute=0)
        requested=timedelta(minutes=minutes);start=max(end-requested,opening)
        actual_minutes=(end-start).total_seconds()/60
        if actual_minutes<=0:continue
        prev_start=start-requested;prev2_start=prev_start-requested
        rows.sort(key=effective);times=[effective(r).timestamp() for r in rows]
        def segment(a,b):return rows[bisect_left(times,a.timestamp()):bisect_left(times,b.timestamp())]
        def actors(a,b,explicit_only=False):
            return {r['entity_id'] for r in segment(a,b) if not explicit_only or r['objects'][object_id]=='explicit'}
        current=segment(start,end)
        # Only explicit coverage permits a measured zero; absence of records alone is unknown.
        current_complete=coverage_ok(coverage,source,panel,start,end,known_at,object_id)
        if not current and not current_complete:continue
        active=actors(start,end);explicit=actors(start,end,True);context=active-explicit
        prev=actors(prev_start,start);prev2=actors(prev2_start,prev_start)
        cumulative=actors(opening,end);earlier=actors(opening,start);new_today=active-earlier
        expected=calendar.open_dates(market,before=end.date().isoformat(),limit=250)
        hist={};hist_prev={};hist_cum={}
        for day in expected:
            de=datetime.fromisoformat(day+'T'+end.strftime('%H:%M:%S')).replace(tzinfo=zone)
            ds=de-timedelta(minutes=actual_minutes)
            if valid_from and ds<parse_time(valid_from):continue
            if coverage_ok(coverage,source,panel,ds,de,known_at,object_id):hist[day]=len(actors(ds,de))
            if coverage_ok(coverage,source,panel,ds-requested,ds,known_at,object_id):hist_prev[day]=len(actors(ds-requested,ds))
            op=de.replace(hour=opening.hour,minute=opening.minute)
            if coverage_ok(coverage,source,panel,op,de,known_at,object_id):hist_cum[day]=len(actors(op,de))
        comp=summaries(len(active),hist,expected);pc=summaries(len(prev),hist_prev,expected)
        q=comp['20']['ratio'];qp=pc['20']['ratio'] if pc['20']['n']>=thresholds['min_history20'] else None;dq=q-qp if q is not None and qp is not None else None
        prior_by_actor=defaultdict(list);background=defaultdict(set);past_circles=defaultdict(set)
        for r in rows:
            if effective(r)<opening:prior_by_actor[r['entity_id']].append(effective(r))
        for r in records:
            if r['kind']!='participation' or r['source']!=source or r['panel_id']!=panel:continue
            rt=effective(r);actor=r['entity_id']
            if parent and parent in r['objects'] and opening-timedelta(days=90)<=rt<opening:background[actor].add(rt.astimezone(zone).date())
            formed=r.get('extra',{}).get('circle_label_formed_at')
            if rt<opening and parse_time(r['observed_at'])<opening and formed and parse_time(formed)<opening:
                past_circles[actor].update(r.get('extra',{}).get('circles',[]))
        cohorts=Counter();circle_counts=Counter()
        for actor in active:
            past=prior_by_actor[actor]
            label=('continuous' if max(past)>=opening-timedelta(days=60) else 'returning') if past else ('new_in_sample' if len(background[actor])>=3 else 'unknown_history')
            cohorts[label]+=1
            for c in past_circles[actor]:circle_counts[c]+=1
        slots={(r['entity_id'],int(effective(r).timestamp())//1800) for r in current}
        per_actor=Counter(r['entity_id'] for r in current);total=sum(per_actor.values());hhi=sum((v/total)**2 for v in per_actor.values()) if total else None
        parent_actors={r['entity_id'] for r in records if r['kind']=='participation' and r['source']==source and r['panel_id']==panel
            and r['entity_type']==entity_type and parent and r['objects'].get(parent)=='explicit' and start<=effective(r)<end}
        cumcomp=summaries(len(cumulative),hist_cum,expected)
        prev_complete=prev_start>=opening and coverage_ok(coverage,source,panel,prev_start,start,known_at,object_id)
        prev2_complete=prev2_start>=opening and coverage_ok(coverage,source,panel,prev2_start,prev_start,known_at,object_id)
        comparable=actual_minutes==minutes and prev_complete
        persistence=2 if current_complete and prev_complete and q is not None and qp is not None and min(q,qp)>=thresholds['ratio20'] else (1 if q is not None and q>=thresholds['ratio20'] else 0)
        output.append(dict(source=source,family=latest_row['family'],panel_id=panel,market=market,metric='distinct_participants_'+str(minutes),unit='entities',
            value=len(active),explicit=len(explicit),context_only=len(context),previous=len(prev) if prev_complete else None,previous2=len(prev2) if prev2_complete else None,
            delta=len(active)-len(prev) if comparable else None,absolute_new=len(new_today),new_today=len(new_today),new_today_per_minute=len(new_today)/actual_minutes,
            entity_type=entity_type,basis='participant_id',window_kind='rolling_wall',window_minutes=actual_minutes,requested_minutes=minutes,at=stamp(end),observed_at=stamp(known_at),
            start_at=stamp(start),cutoff_key=end.strftime('%H:%M'),timezone=str(zone),
            attribution_version=version,attribution_valid_from=valid_from,comparisons=comp,cumulative=len(cumulative),cumulative_comparisons=cumcomp,q=q,q_prev=qp,velocity=dq if comparable else None,acceleration=None,high=q is not None and q>=thresholds['ratio20'],new_entry=False,
            h=robust(len(active),[hist[d] for d in expected[-20:] if d in hist])['h'],anomaly_percentile=comp['20']['percentile'],
            history20_n=comp['20']['n'],history5_n=comp['5']['n'],
            history_ok=comp['20']['n']>=thresholds['min_history20'] and comp['5']['n']>=thresholds['min_history5'],
            rising=comparable and (q or 0)>=thresholds['ratio20'] and (comp['5']['ratio'] or 0)>=thresholds['ratio5'] and dq is not None and dq>0,
            accelerating=comparable and dq is not None and dq>=thresholds['acceleration'],persistence=persistence,
            eligible=current_complete and entity_type in ('account','sellside_team','sellside_org','buyside_org','industry_org'),stale=False,
            coverage='complete_configured_panel' if current_complete else 'partial_sample',cohorts=dict(cohorts),circles=dict(circle_counts),
            hhi=hhi,post_hhi=hhi,effective_authors=1/hhi if hhi else None,effective_sources=None,unique_accounts=len(active),post_count=len(current),parent_n=len(parent_actors) if parent else None,
            parent_intersection_n=len(explicit&parent_actors) if parent else None,parent_share=ratio(len(explicit&parent_actors),len(parent_actors)) if parent else None))
    return output

def forecast_revisions(rows):
    groups=defaultdict(list)
    def business_date(r):
        ex=r.get('extra',{})
        raw=str(ex.get('report_date') or ex.get('raw_published_at') or r['occurred_at'])
        raw=raw[:10]
        if len(raw)==8 and raw.isdigit():raw=raw[:4]+'-'+raw[4:6]+'-'+raw[6:]
        return raw
    for r in rows:
        if r['kind']!='forecast' or r.get('value') is None:continue
        ex=r.get('extra',{});key=(tuple(sorted(r['objects'])),r['source'],r.get('entity_id'),ex.get('forecast_year'),r['metric'],r['unit'],ex.get('forecast_basis'))
        groups[key].append(r)
    pairs=[]
    for key,rs in groups.items():
        # The report's business date orders forecasts; observed_at defines availability.
        unique={}
        for r in sorted(rs,key=lambda r:(business_date(r),parse_time(r['observed_at']),r['record_id'])):
            unique[(business_date(r),r.get('extra',{}).get('report_id') or r['record_id'])]=r
        rs=list(unique.values())
        if len(rs)<2:continue
        a,b=rs[-2:]
        if business_date(a)==business_date(b):continue
        old,new=a['value'],b['value'];near=abs(old)<1e-6
        pairs.append(dict(source=key[1],team=key[2],year=key[3],metric=key[4],unit=key[5],old=old,new=new,
            change=new-old,revision_ratio=(new-old)/abs(old) if not near and old*new>0 else None,
            transition='loss_to_profit' if old<0<new else ('profit_to_loss' if new<0<old else ('near_zero_base' if near else 'paired_update')),
            old_at=a['occurred_at'],new_at=b['occurred_at'],old_report_date=business_date(a),new_report_date=business_date(b),
            available_at=max(a['observed_at'],b['observed_at']),old_record_id=a['record_id'],new_record_id=b['record_id'],
            team_precision=b.get('extra',{}).get('team_precision','team')))
    return pairs

def info_metrics(contents,forecasts,start,end,object_id=None):
    from .events import event_clusters
    contents=[r for r in contents if effective(r)<end]
    groups=event_clusters(contents);current=[r for r in contents if start<=effective(r)<end]
    exact={r.get('extra',{}).get('exact_cluster') or r['record_id'] for r in current}
    prior_exact={r.get('extra',{}).get('exact_cluster') or r['record_id'] for r in contents if effective(r)<start}
    active=[g for g in groups if any(start<=effective(r)<end for r in g['records'])]
    new=[g for g in active if not any(effective(r)<start for r in g['records'])]
    def negative_event(e):return e['direction']=='negative' and (not object_id or not object_id.startswith('stock:') or object_id in e.get('subject_ids',[]))
    negative=sum(any(negative_event(e) for e in g['events']) for g in active)
    events=[]
    for g in active:
        origins=sorted({r['extra'].get('original_publisher') or (r.get('entity_id') if r.get('entity_type') in ('company','industry_org') else None) for r in g['records']} - {None,''})
        events.append(dict(event_id=g['event_id'],title=g['title'],first_at=min(r['occurred_at'] for r in g['records']),first_observed_at=min(r['observed_at'] for r in g['records']),
            new=g in new,distribution_count=len(g['records']),families=sorted({r['family'] for r in g['records']}),origins=origins,urls=sorted(g['urls']),
            direction='negative' if any(negative_event(e) for e in g['events']) else 'unknown',facts=g['events'][:8],
            event_class=g.get('event_class'),importance=g.get('importance',0),fact_update=g.get('fact_update',False),previous_event_id=g.get('previous_event_id')))
    novelty=Counter(r.get('extra',{}).get('novelty_class','unclassified') for r in current)
    return dict(content_count=len(current),exact_text_clusters=len(exact),independent_origin_count=len({o for e in events for o in e['origins']}) or None,
        current_records=len(current),event_count=len(active),new_event_count=len(new),material_new_event_count=sum(g.get('event_class')=='economic' and g.get('importance',0)>=.45 for g in new),
        fact_update_count=sum(g.get('fact_update',False) for g in new),event_importance=max((g.get('importance',0) for g in active),default=0),
        dissemination_count=sum(len(g['records']) for g in active),routine_event_count=sum(g.get('event_class')=='routine' for g in active),new_text_clusters=len(exact-prior_exact),old_exact_text_clusters=len(exact&prior_exact),old_text_ratio=ratio(len(exact&prior_exact),len(exact)),
        duplicate_event_ratio=max(0,1-len(active)/len(current)) if current else None,negative_candidate_clusters=negative,negative_candidate_ratio=ratio(negative,len(active)),
        originating_organization_count=len({r['entity_id'] for r in current if r.get('entity_id')}),novelty=dict(novelty),events=events[-20:],forecast_revisions=forecast_revisions(forecasts),limitations='',
        items=[dict(title=r.get('title') or r.get('text','')[:70],source=r['source'],family=r.get('family',r['source']),url=r.get('url',''),at=r['occurred_at'],observed_at=r['observed_at'],classification=r.get('extra',{})) for r in current[-12:][::-1]])

def choose_stage(attention,info,thresholds,previous=None,name=''):
    domestic=[e for e in attention if e.get('market','A')=='A' and e.get('eligible') and not e.get('stale')]
    negative=info.get('negative_candidate_clusters',0)>=thresholds['min_negative_items'] and (info.get('negative_candidate_ratio') or 0)>=thresholds['negative_ratio']
    if negative:return 'negative_attention','risk','新增负面事件集中'
    if thresholds.get('exclude_st') and re_st(name):return 'risk_excluded','risk','ST / 退市标识'
    high=[e for e in domestic if e.get('high') or (e.get('q') or 0)>=thresholds['ratio20']]
    rising=[e for e in domestic if e.get('rising')]
    strict=[e for e in high if e.get('history_ok') and e.get('persistence',0)>=thresholds['min_persistence_windows']]
    if len({e['family'] for e in strict})>=thresholds['min_confirming_families']:
        velocity=[e.get('velocity') for e in strict if e.get('velocity') is not None]
        if velocity and all(v<0 for v in velocity):return 'decelerating','monitor','异常关注仍高，速度回落'
        if any(e.get('rising') for e in strict):return 'accelerating','confirmed','多来源升温，连续窗口维持异常'
        return 'high_heat','confirmed','多来源关注持续'
    fresh=[e for e in domestic if e.get('new_entry') and (e.get('source')=='em_up' or e.get('observations',0)>=thresholds.get('new_min_windows',2))]
    if fresh:return 'newborn','discovery','新进入关注池'
    if rising:return 'warming','discovery','异常关注上升'
    if high:
        if all((e.get('velocity') or 0)<0 for e in high):return 'decelerating','monitor','异常关注仍高，速度回落'
        return 'high_heat','monitor','关注保持高位'
    if info.get('material_new_event_count',0)>0:return 'information_new','monitor','新增经营信息，关注状态单独跟踪'
    if any((e.get('velocity') or 0)<0 for e in domestic):return 'fading','monitor','关注减弱'
    if not domestic:return ('stale','insufficient','数据待更新') if attention else ('insufficient','insufficient','待采集')
    if not any(e.get('history_ok') for e in domestic):return 'cold_start','insufficient','历史积累中'
    return 'stable','monitor','关注平稳'

def re_st(name):
    import re
    return bool(re.search(r'(^|\*)ST|退市|退$',name,re.I))

STAGES={'information_new':'新增信息','newborn':'新生发现','diffusing':'关注扩散','related':'关联待跟踪','emerging':'新进入升温','accelerating':'持续升温','returning':'题材回流','early_signal':'早期线索',
 'warming':'正在升温','high_heat':'持续关注','decelerating':'升温减速','fading':'关注减弱','negative_attention':'负面关注',
 'risk_excluded':'风险筛选','stale':'待更新','insufficient':'待采集','cold_start':'历史积累中','stable':'关注平稳'}
