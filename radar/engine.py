from __future__ import annotations
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import asdict
from datetime import date,timedelta
from pathlib import Path
import logging,threading,time,copy,json
from .common import ROOT,CN,NY,now,stamp,parse_time,uid,read_json,atomic_json,ratio,number
from .store import Store
from .settings import Settings
from .calendar import MarketCalendar
from .classify import Classifier,discover_terms
from .providers import Collector,SPECS,REGISTRY
from .analytics import snapshot_evidence,participant_evidence,info_metrics,choose_stage,STAGES,summaries
from .trade import trading_state
from .models import Record
from .selection import attach_selection,GROUP_NAMES
from . import __version__

LOG=logging.getLogger('radar')
BAND_ORDER={'confirmed':0,'discovery':1,'monitor':2,'risk':3,'insufficient':4}

class Engine:
    def __init__(self,data_dir,mode='live'):
        self.root=Path(data_dir);self.root.mkdir(parents=True,exist_ok=True)
        self.settings=Settings(self.root);self.calendar=MarketCalendar()
        self.mode=mode;self.db=Store(self.root/mode/'radar.sqlite');self.analysis_lock=threading.Lock()
        self.load_dictionary();self.latest=self.db.runs(limit=1)
    def load_dictionary(self):
        existing={o['id'] for o in self.db.objects()}
        for o in read_json(ROOT/'config/topics.json',[]):
            if o['id'] in existing:continue
            d=dict(o);oid=d.pop('id');name=d.pop('name');kind=d.pop('kind');market=d.pop('market','A')
            self.db.upsert_object(oid,name,kind,market,**d)
        mapping=read_json(ROOT/'config/mappings.json',{});version=mapping.get('version','v1')
        if self.db.get_meta('business_seed_version')!=version:
            for stock in mapping.get('stocks',[]):
                if stock['id'] not in existing:self.db.upsert_object(stock['id'],stock['name'],'stock',aliases=stock.get('aliases',[]))
            for edge in mapping.get('edges',[]):self.add_mapping(edge,persist=False)
            self.db.set_meta('business_seed_version',version)
    def add_mapping(self,edge,persist=True):
        required=('topic_id','symbol','relation','evidence_url','direction','description')
        if not isinstance(edge,dict) or any(not isinstance(edge.get(k),str) or not edge[k].strip() for k in required):raise ValueError('映射需主题、股票、关联类型、方向、说明和证据URL，且各项必须是非空文本')
        if edge['relation'] not in ('direct_business','industry_transmission','narrative'):raise ValueError('关联类型无效')
        if edge['direction'] not in ('demand_positive','cost_negative','competition_negative','technology_switch','sentiment','unknown'):raise ValueError('方向标签错误')
        if not str(edge['evidence_url']).startswith(('http://','https://')):raise ValueError('证据URL必须是HTTP(S)')
        e={k:v for k,v in edge.items() if k in ('topic_id','symbol','relation','evidence_url','direction','description','effective_from','effective_to','trigger_terms','us_object_id','known_at','edge_id','revoked','source','stock_name','product','commercial_stage','evidence_date','review_state','revenue_share','event_sensitivity','customer_share','profit_sensitivity')}
        for field in ('stock_name','product','commercial_stage','review_state'):
            if field not in e:continue
            if e[field] is None:e[field]=''
            if not isinstance(e[field],str):raise ValueError(field+'必须是文本')
        for field in ('edge_id','source','us_object_id'):
            if field in e and e[field] is not None and (not isinstance(e[field],str) or not e[field].strip()):
                raise ValueError(field+'必须是非空文本或空值')
        if 'revoked' in e and type(e['revoked']) is not bool:raise ValueError('revoked必须是布尔值')
        if 'trigger_terms' in e:
            terms=e['trigger_terms']
            if not isinstance(terms,list) or len(terms)>100 or any(not isinstance(t,str) or not t.strip() or len(t)>200 for t in terms):
                raise ValueError('trigger_terms必须是最多100个非空词语的列表')
        for field in ('revenue_share','customer_share'):
            value=e.get(field)
            if value is not None and (type(value) not in (int,float) or number(value) is None or not 0<=value<=1):
                raise ValueError(field+'必须是0至1的有限数值或空值')
        value=e.get('profit_sensitivity')
        if value is not None and (type(value) not in (int,float) or number(value) is None):
            raise ValueError('profit_sensitivity必须是有限数值或空值')
        if 'event_sensitivity' in e:
            values=e['event_sensitivity']
            if not isinstance(values,dict) or any(not isinstance(k,str) or not k.strip() or type(v) not in (int,float) or number(v) is None for k,v in values.items()):
                raise ValueError('event_sensitivity必须是事件名称与有限数值组成的JSON对象')
        for field in ('effective_from','effective_to','evidence_date'):
            value=e.get(field)
            if value is None:continue
            if not isinstance(value,str):raise ValueError(field+'必须是日期文本或空值')
            try:
                if len(value)==10:e[field]=date.fromisoformat(value).isoformat()
                else:e[field]=stamp(parse_time(value))
            except (ValueError,TypeError):raise ValueError(field+'须为YYYY-MM-DD或带时区的ISO时间') from None
        # UI additions may not pretend to have been known before import.
        e['known_at']=stamp()
        objects={o['id']:o for o in self.db.objects()}
        if e['topic_id'] not in objects or objects[e['topic_id']]['kind'] not in ('topic','board'):raise ValueError('请选择已有主题')
        import re
        if not re.fullmatch(r'stock:(SH|SZ|BJ)\d{6}',e['symbol']):raise ValueError('股票ID格式为 stock:SH600000')
        if e['symbol'] not in objects:self.db.upsert_object(e['symbol'],e.get('stock_name') or e['symbol'],'stock')
        e=self.db.membership(**e)
        if persist:self.db.set_meta('mapping_last_updated',stamp())
        return e
    def add_topic(self,payload):
        name=str(payload.get('name','')).strip();aliases=payload.get('aliases',[])
        if not name or len(name)>60 or not isinstance(aliases,list) or len(aliases)>50 or any(not isinstance(a,str) or not 2<=len(a)<=100 for a in aliases):raise ValueError('主题名称/别名格式错误')
        oid='topic:custom:'+uid(name)[:12];known=stamp()
        self.db.upsert_object(oid,name,'topic',known_at=known,aliases=list(dict.fromkeys([name]+aliases)),
            rule='user-dictionary',parent=payload.get('parent'),provisional=False)
        return oid
    def ingest(self,rows,preserve_observed_at=False):
        validated=[];t=stamp();objects=self.db.objects();classifier=Classifier(objects)
        for raw in rows:
            d=raw.model_dump() if isinstance(raw,Record) else dict(raw)
            if self.mode=='live' and d.get('extra',{}).get('synthetic'):raise ValueError('合成数据只能进入独立演示库，不能导入真实监测库')
            if not preserve_observed_at:d['observed_at']=t
            d=Record.model_validate(d).model_dump()
            if d['kind'] in ('content','participation'):d=classifier.annotate(d)
            for oid in d['objects']:
                if not any(o['id']==oid for o in objects):
                    kind='stock' if oid.startswith('stock:') else ('market' if oid.startswith('market:') else 'topic')
                    self.db.upsert_object(oid,oid,kind,market='US' if ':US:' in oid else 'A',known_at=d['observed_at'],aliases=[])
            validated.append(d)
        return self.db.append(validated)
    def provider_status(self):
        health=self.db.health()
        return [{**asdict(s),**health.get(s.id,{}),'enabled':bool(self.settings.data['sources'].get(s.id)),
                 'verification':'最近采集状态'} for s in SPECS]
    def collect(self,force=False,progress=None):
        if self.mode!='live':raise ValueError('演示库禁止采集真实数据')
        cfg=copy.deepcopy(self.settings.data);health=self.db.health();due=[];t=now()
        for s in SPECS:
            if not cfg['sources'].get(s.id):continue
            h=health.get(s.id,{});interval=s.interval
            # Avoid hammering snapshot endpoints all night. Information still advances in natural time.
            if s.id in ('em_rank','em_up','xq_follow','xq_tweet','em_keyword','em_quotes','em_boards'):
                clock=t.strftime('%H:%M');opened=self.calendar.is_open(t.date().isoformat())
                if not opened or not '09:00'<=clock<='15:10':interval=max(interval,3600)
                if s.id=='em_quotes' and not force and (not opened or not '09:30'<=clock<='15:05' or '11:35'<clock<'13:00'):continue
            if not force and h.get('last_attempt') and (t-parse_time(h['last_attempt'])).total_seconds()<interval*min(8,2**min(h.get('consecutive_failures',0),3)):continue
            due.append(s)
        totals={'sources':len(due),'inserted':0,'success':0,'failed':0};completed=0
        fast_ids={'em_rank','em_up','xq_follow','xq_tweet','cls_news','em_news','em_quotes'};fast_pending={x.id for x in due if x.id in fast_ids}
        def one(s):
            begin=stamp();collector=Collector(self.db,cfg,self.root/'live/raw')
            try:
                rows=collector.collect(s.id)
                classifier=Classifier(self.db.objects())
                rows=[classifier.annotate(r) if r['kind'] in ('content','participation') else r for r in rows]
                inserted=self.db.append(rows)+collector.committed_count
                if s.id=='licensed_http':
                    pending=self.db.get_meta('licensed_pending_cursor')
                    if pending:self.db.set_meta('licensed_cursor',pending)
                state=dict(last_attempt=begin,last_success=stamp(),status='partial' if collector.partial_errors else ('ok' if rows or s.id=='em_members' else 'empty'),records=len(rows),inserted=inserted,
                    consecutive_failures=0,message=f'更新 {len(rows)} 条',partial_errors=collector.partial_errors)
                self.db.set_health(s.id,state);return True,inserted
            except Exception as exc:
                message=str(exc)[:700]
                # Never persist token values even if an upstream exception contains request context.
                for secret in cfg['credentials'].values():
                    if secret:message=message.replace(secret,'[REDACTED]')
                old=health.get(s.id,{})
                self.db.set_health(s.id,dict(last_attempt=begin,last_success=old.get('last_success'),status='error',message=message,
                    consecutive_failures=old.get('consecutive_failures',0)+1,records=0))
                LOG.warning('source %s: %s',s.id,message)
                return False,0
        with ThreadPoolExecutor(max_workers=cfg.get('max_parallel_sources',2)) as pool:
            futures={pool.submit(one,s):s for s in due}
            for f in as_completed(futures):
                ok,count=f.result();fast_pending.discard(futures[f].id);completed+=1;totals['success' if ok else 'failed']+=1;totals['inserted']+=count
                if progress:progress(f'已完成 {completed}/{len(due)} 个来源',completed,len(due))
                if not fast_pending and futures[f].id in fast_ids:
                    try:self.analyze()
                    except ValueError:pass
        self.db.set_meta('last_collection',dict(at=stamp(),**totals))
        return totals
    def backfill(self,days=25,symbols=None):
        if self.mode!='live':raise ValueError('演示库不允许实网回填')
        collector=Collector(self.db,self.settings.data,self.root/'live/raw')
        rows=collector.backfill(days,symbols)
        inserted=self.ingest(rows)+collector.committed_count
        return dict(records=len(rows),inserted=inserted,status='partial' if collector.partial_errors else ('ok' if rows else 'empty'),
            partial_errors=collector.partial_errors,note='回填记录的observed_at为现在；日榜历史不会伪装成盘中同刻历史。')
    def _candidates(self,at):
        from .universe import candidate_plan
        return candidate_plan(self,at)
    def analyze(self,as_of=None,phase='auto',save=True):
        if not self.analysis_lock.acquire(blocking=False):raise ValueError('正在计算，不能重叠重写运行')
        try:return self._analyze(as_of,phase,save)
        finally:self.analysis_lock.release()
    def _analyze(self,as_of=None,phase='auto',save=True):
        at=parse_time(as_of or self.db.get_meta('demo_as_of') or stamp()).astimezone(CN)
        if self.mode=='live' and at>now()+timedelta(seconds=5):raise ValueError('不能对未来时点生成真实运行记录')
        plan=self.calendar.plan(at,phase);phase=plan['phase'];cfg=self.settings.data['selection']
        from .discovery import update_topics
        from .signals import onset_state
        from .features import build_features,save_panel
        from .ranking import rank_items
        from .graphs import networks
        terms=update_topics(self,at,save=save and (self.mode=='demo' or abs((now()-at).total_seconds())<300))
        candidates,scan,recent=self._candidates(at)
        deep_ids=set(scan['deep_ids'])
        previous_runs=[r for r in self.db.run_contexts(before=at) if r['phase']==phase and r.get('settings_version')==uid(cfg) and (self.mode=='demo' or r.get('evaluation_type')!='point_in_time_recalculation')]
        previous=previous_runs[0] if previous_runs else {};prev_rows={x['id']:x for x in previous.get('items',[])}
        if phase=='premarket':
            bridge=plan.get('overseas_bridge') or plan['preopen_information'];info_start=parse_time(bridge.get('start') or plan['preopen_information']['start']);metric_at=at
        else:
            if not plan.get('regular_info_window'):raise ValueError('该时点尚无常规盘内窗口，请使用自动时段或读取历史运行。')
            info_start=parse_time(plan['regular_info_window']['start']);metric_at=parse_time(plan['regular_info_window']['end'])
        info_end=at+timedelta(microseconds=1) if phase=='postmarket' else metric_at;coverage=self.db.read(kind='coverage',as_of=at,end=at+timedelta(microseconds=1))
        quote_cache=self.db.read(kind='quote',start=metric_at.replace(hour=0,minute=0,second=0,microsecond=0),end=metric_at+timedelta(seconds=1),as_of=at)
        quotes_by_object=defaultdict(list)
        for qrecord in quote_cache:
            for qoid in qrecord['objects']:quotes_by_object[qoid].append(qrecord)
        market_quotes=quotes_by_object.get('market:A',[])
        items=[];members=self.db.all_members(at);member_map=defaultdict(list)
        for m in members:member_map[m['topic_id']].append(m)
        for obj in candidates:
            rows=self.db.read(kind=['snapshot','participation','content','forecast','claim'],object_id=obj['id'],start=at-timedelta(days=self.settings.data.get('analysis_history_days',45)),end=at+timedelta(microseconds=1),as_of=at)
            # Rows fetched later than an earlier live run remain unavailable to its point-in-time replay.
            att=snapshot_evidence(rows,metric_at,self.calendar,cfg,known_at=at)
            participant_rows=[r for r in rows if r['kind']=='participation']
            if obj.get('parent'):
                parent_rows=self.db.read(kind='participation',object_id=obj['parent'],start=at-timedelta(days=100),end=at+timedelta(microseconds=1),as_of=at)
                merged={r['record_id']:r for r in participant_rows+parent_rows};participant_rows=list(merged.values())
            att+=participant_evidence(participant_rows,coverage,obj['id'],metric_at,self.calendar,cfg,parent=obj.get('parent'),known_at=at)
            infos=[r for r in rows if r['kind']=='content'];forecasts=[r for r in rows if r['kind']=='forecast']
            # Deep objects receive full event history; the light panel keeps the recent fact window.
            if obj['id'] not in deep_ids:infos=[r for r in infos if parse_time(r['occurred_at'])>=at-timedelta(days=7)]
            info=info_metrics(infos,forecasts,info_start,info_end,obj['id'])
            stage,band,why=choose_stage(att,info,cfg,prev_rows.get(obj['id'],{}).get('stage'),obj['name'])
            symbol_ids={obj['id']} if obj['kind']=='stock' else {e['symbol'] for e in member_map.get(obj['id'],[])}
            relevant_quotes=market_quotes+[q for sid in symbol_ids for q in quotes_by_object.get(sid,[])]
            trade=trading_state(self.db,obj,metric_at,at,relevant_quotes,member_map.get(obj['id'],[]),cfg.get('min_member_coverage',.6))
            signals=onset_state(att,relevant_quotes,obj['id'],cfg.get('response_threshold',.01))
            # A date-only announcement is not allowed to become an alleged pre-open fact retroactively.
            after=info_metrics(infos,forecasts,metric_at,at+timedelta(microseconds=1),obj['id']) if phase=='postmarket' else None
            if after and after['negative_candidate_clusters']>=cfg['min_negative_items'] and (after['negative_candidate_ratio'] or 0)>=cfg['negative_ratio']:
                why='盘后新增负面事件';band='risk';stage='negative_attention'
            changes=[e for e in att if e.get('market','A')=='A' and e.get('eligible') and (e.get('rising') or e.get('high')) and not e.get('stale')]
            primary=sorted(att,key=lambda e:(not e.get('stale'),e.get('eligible',False),e.get('history_ok',False),e.get('rising',False),e.get('basis')=='participant_id'),reverse=True)
            row={**obj,'analysis_depth':'deep' if obj['id'] in deep_ids else 'light','signals':signals,'stage':stage,'stage_name':STAGES[stage],'band':band,'why':why,'attention':att,'primary':primary[0] if primary else None,
                'diffusion':[{k:e.get(k) for k in ('source','panel_id','new_today','cohorts','circles','parent_share','parent_n','hhi','coverage')} for e in att if e.get('basis')=='participant_id'],
                'trading':trade,'information':info,'afterclose_information':after,'fast_family_count':len({e['family'] for e in changes}),
                'measured_basis':'含真实ID样本' if any(e.get('basis')=='participant_id' for e in att) else '平台代理 / 内容供给',
                'state_transition':prev_rows.get(obj['id'],{}).get('stage')!=stage,'previous_stage':prev_rows.get(obj['id'],{}).get('stage'),
                'source_record_ids':[r['record_id'] for r in rows],
                'first_detected_at':(prev_rows.get(obj['id'],{}).get('first_detected_at') or stamp(at)) if band in ('confirmed','discovery') else None}
            items.append(row)
        items=attach_selection(items,members,previous_runs,at,cfg)
        source_diagnostics=build_features(items,members,self.settings.data.get('features',{}))
        ranking=rank_items(items,self.db,at,self.mode=='demo')
        graph=networks(items,members,recent,at)
        observation={x['id']:x.get('attention_observed',False) for x in items}
        for record in scan['manifest']:record['attention_observation']='observed' if observation.get(record['id']) else 'unobserved'
        scan['attention_observed_stocks']=sum(x['kind']=='stock' and x.get('attention_observed',False) for x in items)
        latest_by_phase={}
        for r in previous_runs:latest_by_phase.setdefault(r['phase'],r['run_id'])
        run=dict(schema_version='heat-selection-v2.0.0',method_origin='source-local-selection',code_version=__version__,run_id=phase+'-'+at.strftime('%Y%m%dT%H%M%S')+'-'+uid(time.time_ns())[:6],
            phase=phase,as_of=stamp(at),created_at=stamp(),evaluation_type=('synthetic_demo' if self.mode=='demo' else ('point_in_time_recalculation' if (now()-at).total_seconds()>300 else 'live_snapshot')),mode=self.mode,synthetic=self.mode=='demo',target_session=plan['target_session'],previous_a_session=plan['previous_a_session'],
            calendar_version=plan['calendar_version'],calendar_valid_to=self.calendar.data['valid_to'],metric_cutoff=stamp(metric_at),
            parent_run_ids=latest_by_phase,settings_version=uid(cfg),selection_parameters=copy.deepcopy(cfg),scan=scan,items=items,selection_groups=dict(Counter(x['selection_group'] for x in items)),
            dictionary_version=uid(self.db.objects(as_of=at)),membership_version=uid(members),memberships=members,source_config_version=uid({k:v for k,v in self.settings.data.items() if k!='credentials'}),record_set_hash=uid(sorted({i for x in items for i in x['source_record_ids']}|{r['record_id'] for r in coverage+quote_cache})),
            counts=dict(Counter(x['band'] for x in items)),plan={k:v for k,v in plan.items() if k not in ('same_clock_history','us_history_dates','us_history_sessions')},
            providers=self.provider_status(),term_discovery=terms,source_diagnostics=source_diagnostics,ranking=ranking,graphs=graph,
            limitations=[])
        run['review_batches']={'premarket':phase=='premarket','postmarket':phase=='postmarket'}
        run['overseas_mapping']=self.overseas_mapping(items,at,plan)
        run['intraday_path']=self.paths(previous_runs,run)
        if save:
            self.db.save_run(run);save_panel(self.db,run);self.db.set_meta('latest_'+phase,run['run_id']);self.db.set_meta('latest_run',run['run_id'])
            self.latest=[run]
        return run
    def overseas_mapping(self,items,at,plan):
        out=[]
        for x in items:
            overseas=[e for e in x['attention'] if e.get('market')=='US']
            us_information=[i for i in x['information'].get('items',[]) if i.get('classification',{}).get('market')=='US']
            if not overseas and not us_information:continue
            edges=self.db.members(x['id'],at)
            out.append(dict(object_id=x['id'],name=x['name'],overseas_attention=overseas,overseas_information=us_information,domestic_stage=x['stage_name'],edges=edges,
                classification='海外加速/国内未确认' if any(e.get('accelerating') for e in overseas) and x['band']!='confirmed' else ('海外内容供给线索' if not overseas else '海外观察'),
                note='只有带证据的传导边可解释A股关系；无边仅列海外发现。美股价格不替代讨论热度。'))
        return out
    def paths(self,runs,current):
        day=current['target_session'];sequence=[r for r in reversed(runs) if r['target_session']==day and r['phase']=='intraday']
        if current['phase']=='intraday':sequence.append(current)
        out={}
        for row in current['items']:
            points=[]
            for r in sequence:
                item=next((x for x in r['items'] if x['id']==row['id']),None)
                if item:
                    e=item.get('primary') or {}
                    if points and points[-1].get('evidence_at')==e.get('at') and points[-1]['source']==e.get('source') and points[-1]['metric']==e.get('metric'):continue
                    points.append(dict(at=r['as_of'],evidence_at=e.get('at'),stage=item['stage'],source=e.get('source'),panel_id=e.get('panel_id'),metric=e.get('metric'),q=e.get('q'),value=e.get('value')))
            if not points:continue
            # Do not connect different data series into one apparent continuous Q line.
            last=points[-1];qs=[p['q'] for p in points if p['q'] is not None and (p['source'],p['panel_id'],p['metric'])==(last['source'],last['panel_id'],last['metric'])]
            out[row['id']]=dict(points=points,observed_active_share=ratio(sum(q>1 for q in qs),len(qs)),tail_q_retention=ratio(qs[-1],max(qs)) if qs else None,
                note='按实际保存的运行时点，不是连续分钟完整窗口占比；缺口不插值。')
        return out
