from __future__ import annotations
import json,os,re,subprocess,sys,tempfile,time,urllib.parse,xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
import httpx
from .common import ROOT,CN,NY,stamp,now,parse_time,cn_time,stock_id,ts_code,number,uid,clean
from .models import Record

class SourceError(RuntimeError):pass

@dataclass
class ProviderSpec:
    id:str
    title:str
    dimension:str
    interval:int
    access:str
    note:str

SPECS=[
 ProviderSpec('em_up','东方财富飙升榜','发现',300,'公开网页接口 / AKShare','当前飙升榜'),
 ProviderSpec('em_members','板块成员目录','关联',3600,'公开网页接口 / AKShare','行业与概念成员'),
 ProviderSpec('em_daily','全池轮转日线','后续观察',3600,'公开网页接口 / AKShare','候选与基准日线'),
 ProviderSpec('em_rank','东方财富人气榜','关注',300,'公开网页接口 / AKShare','前100名；榜位不是账号数；榜外不赋虚构末位'),
 ProviderSpec('xq_follow','雪球关注存量','关注',900,'公开网页接口 / AKShare','保留关注存量；净变化另算；无法识别单个浏览者'),
 ProviderSpec('xq_tweet','雪球讨论榜','关注',900,'公开网页接口 / AKShare','平台口径讨论量与榜位，不是独立参与者'),
 ProviderSpec('cls_news','财联社电报','信息/媒体',180,'公开网页接口 / AKShare','近期约20条；轮询有漏采风险；不标全量覆盖'),
 ProviderSpec('em_news','东方财富财经快讯','信息/媒体',300,'公开网页接口 / AKShare','区分原始来源与聚合分发'),
 ProviderSpec('em_quotes','沪深京行情','价格观察',600,'公开网页接口 / AKShare','当前全市场请求；日内采样而非逐笔行情，失败不补零'),
 ProviderSpec('em_boards','行业与概念目录','结构',1800,'公开网页接口 / AKShare','涨幅只用于发现；板块名不代表已确认业务关联'),
 ProviderSpec('em_keyword','个股热门关键词','发现/关注',900,'公开网页接口 / AKShare','轮换深查候选；平台叙事归因，非公司业务证明'),
 ProviderSpec('em_research','个股研报及EPS预测','机构/信息',1800,'公开网页接口 / AKShare','轮换候选；同机构同年度同口径比较'),
 ProviderSpec('em_surveys','公开机构调研','机构',21600,'公开网页接口 / AKShare','参会机构，不能冒称每家都明确关注某个题材'),
 ProviderSpec('em_notices','上市公司公告目录','信息',3600,'公开网页接口 / AKShare','保留公告链接；不自动声称读懂PDF正文'),
 ProviderSpec('tushare_hot','Tushare · 同花顺热榜','关注',7200,'Token + 对应积分/权限','低频历史快照，不伪装为30分钟实时流'),
 ProviderSpec('tushare_news','Tushare · 财经新闻','信息/媒体',600,'Token + 独立新闻权限','分页受限；按发布时间与取得时间归档'),
 ProviderSpec('tushare_reports','Tushare · 全市场盈利预测','机构/信息',21600,'Token + 对应积分/权限','保留机构、作者、预测年度与利润口径'),
 ProviderSpec('x_recent','X 官方 Recent Search','海外讨论',900,'X API Bearer Token + 授权','真实 author_id；固定查询面板；限页会显式标不完整'),
 ProviderSpec('reddit','Reddit 官方 OAuth API','海外讨论',900,'OAuth Bearer Token + 授权','帖子与选取评论；固定subreddit面板，不声称全站'),
 ProviderSpec('rss','官方/产业 RSS 或 Atom','产业/海外',1800,'由用户配置获准读取的公开Feed','只读取摘要，发布组织与读者分列'),
 ProviderSpec('licensed_http','获授权舆情 JSON 接口','讨论/扩散',300,'供应商或自有数据服务','标准化行为明细合同；不绕过登录、验证码或付费墙')]
SPECS += [
 ProviderSpec('a_universe','沪深京证券主表','全市场覆盖',86400,'AKShare 证券目录','时点化证券范围'),
 ProviderSpec('cninfo_irm','互动易提问与公司回复','参与/信息',1800,'AKShare 互动易','稳定提问者ID与公司回复分列'),
 ProviderSpec('em_attention_detail','关注历史与粉丝结构','关注',3600,'AKShare 东方财富','日级结构与盘中序位独立面板'),
 ProviderSpec('business_segments','公司主营构成','业务',86400,'AKShare 东方财富','披露期间、产品、收入与占比'),
 ProviderSpec('em_minutes','五分钟价格序列','后续观察',900,'AKShare 东方财富','后复权价格面板'),
 ProviderSpec('disclosure_text','公告与研报正文','事实',1800,'公开原始文档','文本提取与事实切分'),
 ProviderSpec('local_feed','本地结构化数据目录','行为/扩展',300,'CSV / JSON / JSONL','稳定主体、面板、窗口与时间字段')]

for _spec in SPECS:
    _spec.note={'em_rank': '平台返回的人气序位', 'xq_follow': '关注存量及其30分钟净变化', 'xq_tweet': '平台讨论指数与序位', 'cls_news': '电报标题、正文与发布时刻', 'em_news': '财经快讯及原始发布组织', 'em_quotes': '沪深京A股价格与活跃度快照', 'em_boards': '行业和概念目录', 'em_keyword': '公司语境中的热门关键词', 'em_research': '机构预测与研报目录', 'em_surveys': '公开机构调研参与信息', 'em_notices': '公告目录及原文地址'}.get(_spec.id,_spec.note)

REGISTRY={s.id:s for s in SPECS}

class Fetcher:
    def __init__(self,settings,raw_dir):
        self.settings=settings;self.raw_dir=Path(raw_dir);self.raw_dir.mkdir(parents=True,exist_ok=True)
    def ak(self,fn,**params):
        with tempfile.TemporaryDirectory(prefix='radar-ak-') as td:
            a=Path(td)/'input.json';b=Path(td)/'result.json'
            a.write_text(json.dumps(dict(function=fn,params=params),ensure_ascii=False),encoding='utf-8')
            env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
            try:
                proc=subprocess.run([sys.executable,'-m','radar.ak_worker',str(a),str(b)],cwd=ROOT,
                    env=env,capture_output=True,timeout=self.settings.get('worker_timeout_seconds',90))
            except subprocess.TimeoutExpired as exc:raise SourceError(fn+' 超时，子进程已终止') from exc
            if proc.returncode or not b.exists():raise SourceError(fn+' 子进程失败 '+proc.stderr.decode('utf-8',errors='replace')[-500:])
            r=json.loads(b.read_text(encoding='utf-8'))
            if not r['ok']:raise SourceError(r['error'])
            rows=r['rows']
            self.archive(fn,params,rows)
            return rows
    def archive(self,source,params,data):
        # Parameters are intentionally not logged: may contain tokens or user identifiers.
        import gzip
        p=self.raw_dir/(now().strftime('%Y%m%dT%H%M%S')+'-'+uid(source,params,time.time_ns())[:14]+'.json.gz')
        with gzip.open(p,'wt',encoding='utf-8') as f:json.dump(dict(source=source,observed_at=stamp(),data=data),f,ensure_ascii=False)
    def http(self,url,params=None,body=None,headers=None,raw=False):
        if not url.startswith('https://'):raise SourceError('只允许TLS HTTPS数据源')
        hdr={'User-Agent':self.settings.get('user_agent','AshareHeatRadar/1.0'),**(headers or {})}
        for attempt in range(2):
            try:
                with httpx.Client(timeout=self.settings.get('network_timeout_seconds',20),follow_redirects=False) as c:
                    r=c.post(url,json=body,headers=hdr) if body is not None else c.get(url,params=params,headers=hdr)
                if r.status_code in (401,403):raise SourceError('授权不足或来源拒绝访问（不绕过限制）')
                if r.status_code==429:raise SourceError('来源限流；等待下次调度，不高频重试')
                r.raise_for_status()
                if len(r.content)>20_000_000:raise SourceError('单次响应超过20MB上限')
                data=r.content if raw else r.json()
                if raw:
                    import base64
                    self.archive(urllib.parse.urlsplit(url).hostname,{},dict(encoding='base64',body=base64.b64encode(data).decode('ascii')))
                else:self.archive(urllib.parse.urlsplit(url).hostname,{},data)
                return data
            except SourceError:raise
            except (httpx.HTTPError,ValueError) as exc:
                if attempt:raise SourceError(type(exc).__name__+': '+str(exc)[:400]) from exc
                time.sleep(1)
    def tushare(self,api,**params):
        token=self.settings['credentials'].get('tushare_token') or os.getenv('TUSHARE_TOKEN','')
        if not token:raise SourceError('尚未填写Tushare Token')
        r=self.http('https://api.tushare.pro',body=dict(api_name=api,token=token,params=params,fields=''))
        if r.get('code')!=0:raise SourceError('Tushare '+str(r.get('msg','接口错误'))[:500])
        data=r.get('data') or {};fields=data.get('fields',[])
        rows=[dict(zip(fields,x)) for x in data.get('items',[])]
        self.archive('tushare:'+api,params,rows)
        return rows

from .data_sources import ExtendedCollector

class Collector(ExtendedCollector):
    def __init__(self,store,settings,raw_dir):
        self.db=store;self.cfg=settings;self.fetch=Fetcher(settings,raw_dir);self._object_names={x['id']:x['name'] for x in self.db.objects()};self.partial_errors=[];self._active_source='backfill';self._publication_meta={};self.committed_count=0;self._successful_parts=0
    def base(self,kind,source,objects,at,**kw):
        observed=stamp();extra=dict(kw.pop('extra',{}))
        if self._publication_meta.get('effective')==stamp(at):extra.update(self._publication_meta['fields'])
        return Record(kind=kind,source=source,objects=objects,occurred_at=stamp(at),observed_at=observed,extra=extra,**kw).model_dump()
    def publication(self,value,date_only=False):
        raw=value
        if date_only and value is not None:value=str(value)[:10] if '-' in str(value) else str(value)[:8]
        try:
            parsed=cn_time(value);future=parsed>now();t=now() if date_only or future else parsed
            fields=dict(raw_published_at=str(raw),published_at=None if date_only or future else stamp(parsed),timestamp_basis='first_observed' if date_only or future else 'source_time',time_parse_status='date_only' if date_only else ('future_source_time' if future else 'ok'))
        except (ValueError,TypeError):
            t=now();fields=dict(raw_published_at=str(raw),published_at=None,timestamp_basis='first_observed',time_parse_status='unparsed')
        self._publication_meta=dict(effective=stamp(t),fields=fields);return t
    def obj(self,code,name=None):
        oid=stock_id(code);label=clean(name) if name else self._object_names.get(oid,oid)
        if self._object_names.get(oid)!=label:
            self.db.upsert_object(oid,label,kind='stock');self._object_names[oid]=label
        return oid
    def selected(self,limit=None,source=None):
        source=source or self._active_source;n=limit or self.cfg.get('deep_scan_per_run',4);runs=self.db.runs(1)
        candidates=[r['id'] for r in (runs[0].get('items',[]) if runs else []) if r.get('kind')=='stock' and r.get('market','A')=='A']
        pool=list(dict.fromkeys(x for x in self.cfg.get('watchlist',[])+candidates if x.startswith('stock:') and ':US:' not in x))
        if not pool:pool=[x['id'] for x in self.db.objects() if x['kind']=='stock' and x.get('market','A')=='A']
        
        allstocks=[x['id'] for x in self.db.objects() if x['kind']=='stock' and x.get('market','A')=='A']
        controls=(runs[0].get('scan',{}).get('controls',[]) if runs else [])
        reserve=max(1,n//4)
        background=self.db.fetch_candidates(source,list(dict.fromkeys(controls+allstocks)),reserve)
        preferred=self.db.fetch_candidates(source,pool,n-len(background))
        return list(dict.fromkeys(preferred+background))[:n]
    def collect(self,source):
        self._active_source=source
        method=getattr(self,'collect_'+source,None)
        if not method:raise SourceError('没有实现的数据适配器: '+source)
        return method()
    def commit_part(self,rows):
        from .classify import Classifier
        classifier=Classifier(self.db.objects())
        records=[classifier.annotate(r) if r['kind'] in ('content','participation') else r for r in rows]
        self.committed_count+=self.db.append(records);return records
    def partial_error(self,object_id,exc,operation=None):
        message=str(exc)[:500]
        for secret in self.cfg.get('credentials',{}).values():
            if secret:message=message.replace(secret,'[REDACTED]')
        error=dict(object_id=object_id,message=message)
        if operation:error['operation']=operation
        self.partial_errors.append(error)
        return message
    def ak_part(self,object_id,function,**params):
        try:
            rows=self.fetch.ak(function,**params)
            self._successful_parts+=1
            return rows
        except Exception as exc:
            self.partial_error(object_id,exc,operation=function)
            return []
    def per_object(self,source,objects,fn,ttl=21600):
        out=[];successful_objects=0
        for oid in objects:
            try:
                before=len(self.partial_errors);successful_before=self._successful_parts
                part=fn(oid);out.extend(self.commit_part(part))
                errors=self.partial_errors[before:]
                if not errors or self._successful_parts>successful_before:successful_objects+=1
                if errors:self.db.finish_fetch(source,oid,False,message=str(errors)[:500])
                else:self.db.finish_fetch(source,oid,True,ttl)
            except Exception as exc:
                message=self.partial_error(oid,exc)
                self.db.finish_fetch(source,oid,False,message=message)
        if objects and not out and self.partial_errors and not successful_objects:raise SourceError(str(self.partial_errors[:2]))
        return out
    def collect_em_up(self):
        rows=self.fetch.ak('stock_hot_up_em');out=[];t=now()
        for r in rows:
            oid=self.obj(r['代码'],r.get('股票名称'));rank=number(r.get('当前排名'))
            if rank is None:continue
            out.append(self.base('snapshot','em_up',{oid:'explicit'},t,family='eastmoney',metric='rank',value=rank,unit='rank',window_kind='popularity_risers',extra=dict(discovery_trigger=True,rank_change_day=number(r.get('排名较昨日变动')),list_size=len(rows),cadence_seconds=300,timestamp_basis='retrieved_at')))
        if not out:raise SourceError('飙升榜为空')
        return out
    def collect_em_rank(self):
        rows=self.fetch.ak('stock_hot_rank_em');out=[];t=now()
        missing_names=sum(not r.get('股票名称') for r in rows)
        if missing_names:self.partial_errors.append(dict(operation='rank_name_lookup',message=f'{missing_names}只股票缺少名称补充，保留实际榜位与已有名称'))
        for r in rows:
            oid=self.obj(r['代码'],r.get('股票名称'));v=number(r.get('当前排名'))
            if v is None:continue
            out.append(self.base('snapshot','em_rank',{oid:'explicit'},t,family='eastmoney',metric='rank',value=v,unit='rank',
                window_kind='platform_popularity',extra=dict(list_size=len(rows),timestamp_basis='retrieved_at',cadence_seconds=300,transport=r.get('_radar',{}))))
        if not out:raise SourceError('人气榜为空或字段发生变化')
        return out
    def _xq(self,source,fn,metric):
        rows=self.fetch.ak(fn,symbol='最热门');out=[];t=now()
        valid=[]
        for r in rows:
            try:
                oid=stock_id(r['股票代码'])
                # Xueqiu may mix ETFs/B-shares into its universe. These are configured
                # admission ranges, not a claim that every admitted code is listed.
                if not re.fullmatch(r'stock:(?:SH(?:60|68)\d{4}|SZ(?:00|30)\d{4}|BJ(?:43|83|87|88|92)\d{4})',oid):continue
                oid=self.obj(r['股票代码'],r.get('股票简称'))
            except (ValueError,KeyError):continue
            v=number(r.get('关注'))
            if v is not None:valid.append((oid,v))
        valid.sort(key=lambda x:-x[1])
        # Broad discovery is refreshed each call; no ranks are fabricated outside the returned universe.
        ranks={v:1+sum(1 for _,other in valid if other>v)+sum(1 for _,other in valid if other==v)/2-.5 for v in {v for _,v in valid}}
        for oid,v in valid:
            rank=ranks[v]
            kw=dict(family='xueqiu',window_kind='platform_total',extra=dict(cadence_seconds=900,
                timestamp_basis='retrieved_at',returned_universe=len(valid),definition='AKShare平台字段，不等同独立用户'))
            out.append(self.base('snapshot',source,{oid:'explicit'},t,metric=metric,value=v,unit='platform_count',**kw))
            out.append(self.base('snapshot',source,{oid:'explicit'},t,metric='rank',value=rank,unit='rank',**kw))
        if not valid:raise SourceError('雪球没有返回可识别的A股数据')
        return out
    def collect_xq_follow(self):return self._xq('xq_follow','stock_hot_follow_xq','followers_total')
    def collect_xq_tweet(self):return self._xq('xq_tweet','stock_hot_tweet_xq','discussion_index')
    def _news(self,source,fn):
        rows=self.fetch.ak(fn,**({'symbol':'全部'} if fn.endswith('cls') else {}));out=[]
        for r in rows:
            title=clean(r.get('标题') or r.get('title'));text=clean(r.get('内容') or r.get('摘要'))
            datepart=str(r.get('发布日期') or '')[:10];timepart=str(r.get('发布时间') or '')
            dt=timepart if re.match(r'\d{4}-\d{2}-\d{2}',timepart) else (datepart+' '+timepart).strip()
            if not title and not text:continue
            t=self.publication(dt.strip())
            out.append(self.base('content',source,{},t,family='cls' if source=='cls_news' else 'eastmoney',
                title=title,text=text[:4000],url=r.get('链接') or r.get('网址') or '',
                record_id=uid(source,title,text,dt),entity_type='media_org',entity_id='cls' if source=='cls_news' else None,
                panel_id='recent-headlines-v1',extra=dict(coverage_complete=False,original_publisher=r.get('文章来源'),time_precision='source_or_first_observed')))
        if not out:raise SourceError('新闻源为空或字段已变更')
        return out
    def collect_cls_news(self):return self._news('cls_news','stock_info_global_cls')
    def collect_em_news(self):return self._news('em_news','stock_info_global_em')
    def collect_em_quotes(self):
        rows=self.fetch.ak('stock_zh_a_spot_em');out=[];t=now();total=0.0;count=0
        from .calendar import MarketCalendar
        phase='regular_cumulative' if MarketCalendar().is_open(t.date().isoformat()) and '09:30'<=t.strftime('%H:%M')<='15:05' else 'provider_latest_total_unverified_stage'
        for r in rows:
            try:oid=self.obj(r['代码'],r.get('名称'))
            except (KeyError,ValueError):continue
            amount=number(r.get('成交额'));ret=number(r.get('涨跌幅'));price=number(r.get('最新价'))
            if amount is not None:total+=amount;count+=1
            out.append(self.base('quote','em_quotes',{oid:'explicit'},t,family='eastmoney',metric='quote',unit='CNY',window_kind=phase,
                extra=dict(amount_cny=amount,return_decimal=ret/100 if ret is not None else None,
                  price=price,turnover_pct=number(r.get('换手率')),float_cap_cny=number(r.get('流通市值')),
                  volume=number(r.get('成交量')),price_high=number(r.get('最高')),price_low=number(r.get('最低')),
                  tradable=amount is not None and amount>0,time_precision='retrieved_at_source_time_unavailable',
                  phase=phase)))
        if len(out)<100:raise SourceError('行情返回过少，不视为完整A股行情')
        # All-market amount is usable only with a sufficiently large valid returned universe and a documented scope.
        market_complete=count==len(out) and count>=4000
        out.append(self.base('quote','em_quotes',{'market:A':'explicit'},t,family='eastmoney',metric='market_amount',value=total,unit='CNY',window_kind=phase,
            extra=dict(returned_symbols=len(out),valid_amount_symbols=count,complete=market_complete,
             definition='供应商沪深京A股返回集合；包含哪些阶段须以供应商口径为准',
             equal_weight_return=(sum(x['extra']['return_decimal'] for x in out if x['extra'].get('return_decimal') is not None)/sum(x['extra'].get('return_decimal') is not None for x in out)) if any(x['extra'].get('return_decimal') is not None for x in out) else None,universe_version=uid(sorted(o for x in out for o in x['objects'])))))
        self.db.upsert_object('market:A','A股供应商行情集合','market')
        return out
    def collect_em_boards(self):
        out=[];t=now()
        for cat in ('industry','concept'):
            rows=self.fetch.ak('stock_board_'+cat+'_name_em')
            for r in rows:
                code=str(r.get('板块代码',''));name=clean(r.get('板块名称'))
                if not code or not name:continue
                oid='board:em:'+code
                self.db.upsert_object(oid,name,'board',aliases=[name],provider='eastmoney',board_type=cat)
                ret=number(r.get('涨跌幅'))
                out.append(self.base('quote','em_boards',{oid:'explicit'},t,family='eastmoney',metric='board_quote',
                    extra=dict(return_decimal=ret/100 if ret is not None else None,
                        rising=number(r.get('上涨家数')),falling=number(r.get('下跌家数')),rank_is_price_only=True)))
        if not out:raise SourceError('板块目录为空')
        return out
    def collect_em_members(self):
        objects=[o for o in self.db.objects() if o['kind']=='board' and o['id'].startswith('board:em:')]
        latest=self.db.runs(1);priority=[x['id'] for x in latest[0]['items'] if x['kind']=='board'] if latest else []
        lookup={o['id']:o for o in objects};pool=list(dict.fromkeys([x for x in priority if x in lookup]+list(lookup)))
        chosen=self.db.fetch_candidates('em_members',pool,4)
        def one(oid):
            obj=lookup[oid];cat=obj.get('board_type','concept');rows=self.fetch.ak('stock_board_'+cat+'_cons_em',symbol=oid.split(':')[-1]);out=[];symbols=set();t=stamp()
            if not rows:raise SourceError('成员目录为空')
            for r in rows:
                sid=self.obj(r['代码'],r.get('名称'));symbols.add(sid)
                self.db.membership(oid,sid,relation='narrative',source='eastmoney_members',direction='unknown',description=obj['name']+'平台成分',evidence_url='https://quote.eastmoney.com/bk/'+oid.split(':')[-1]+'.html',review_state='platform',known_at=t)
            for edge in self.db.members(oid,t):
                if edge.get('source')=='eastmoney_members' and edge['symbol'] not in symbols:self.db.membership(**{**edge,'effective_to':t,'known_at':t,'revoked':True})
            self.db.set_meta('member_pool:'+oid,dict(at=t,count=len(symbols),version=uid(sorted(symbols))))
            return out
        return self.per_object('em_members',chosen,one,86400)
    def collect_em_keyword(self):
        def one(oid):
            out=[]
            code=oid.removeprefix('stock:');rows=self.fetch.ak('stock_hot_keyword_em',symbol=code)
            for r in rows:
                name=clean(r.get('概念名称'));board='board:em:'+str(r.get('概念代码',''))
                if not name or board.endswith(':'):continue
                self.db.upsert_object(board,name,'board',aliases=[name])
                self.db.membership(board,oid,relation='narrative',source='eastmoney_hot_keyword',note='平台标签，不证明业务关系')
                v=number(r.get('热度'));t=self.publication(r.get('时间'))
                if v is None:continue
                # It is a stock-context keyword metric. Do not add stock-context values into a sector total.
                out.append(self.base('snapshot','em_keyword',{board:'explicit',oid:'explicit'},t,family='eastmoney',
                    panel_id='keyword-context:'+oid,metric='keyword_context_heat',value=v,unit='platform_index',
                    window_kind='platform_keyword',extra=dict(context_stock=oid,attribution='platform_narrative',cadence_seconds=900)))
            return out
        return self.per_object('em_keyword',self.selected(source='em_keyword'),one,21600)
    def collect_em_research(self):
        def one(oid):
            out=[]
            rows=self.fetch.ak('stock_research_report_em',symbol=oid[-6:])
            for r in rows[:100]:
                t=self.publication(r.get('日期'),date_only=True);title=clean(r.get('报告名称'));org=clean(r.get('机构'))
                if not title:continue
                rid=uid('report',oid,org,str(r.get('日期')),title)
                out.append(self.base('content','em_research',{oid:'explicit'},t,family='eastmoney',record_id=rid,
                    panel_id='rotating-report-discovery-v1',title=title,url=r.get('报告PDF链接') or '',entity_id=org or None,entity_type='sellside_org',
                    extra=dict(time_precision='date_only_first_observed',report_id=rid,coverage_complete=False,full_text_read=False)))
                for key,value in r.items():
                    m=re.fullmatch(r'(\d{4})-盈利预测-收益',key);v=number(value)
                    if m and v is not None and org:
                        out.append(self.base('forecast','em_research',{oid:'explicit'},t,family='eastmoney',
                            record_id=uid(rid,key,v),entity_id=org,entity_type='sellside_org',metric='EPS',value=v,unit='CNY/share',
                            extra=dict(forecast_year=m[1],report_id=rid,team_precision='organization_only',forecast_basis='EPS',report_date=str(r.get('日期',''))[:10]))) 
            return out
        return self.per_object('em_research',self.selected(source='em_research'),one,21600)
    def collect_em_surveys(self):
        rows=self.fetch.ak('stock_jgdy_detail_em',date=(now()-timedelta(days=30)).strftime('%Y%m%d'));out=[]
        for r in rows:
            try:oid=self.obj(r['代码'],r.get('名称'))
            except (ValueError,KeyError):continue
            org=clean(r.get('调研机构'));t=self.publication(r.get('公告日期'),date_only=True)
            if not org:continue
            # Participant identity explicitly applies only to an attendance record, not theme interest.
            out.append(self.base('participation','em_surveys',{oid:'context'},t,family='eastmoney',
                record_id=uid('survey',oid,org,r.get('调研日期'),r.get('接待方式')),entity_id=org,entity_type='institution_attendee',
                panel_id='surveys-published-v1',extra=dict(activity_at=str(r.get('调研日期')),relation='attending_not_explicit',
                    organization_type=r.get('机构类型'),coverage_complete=False,circles=[])))
        return out
    def collect_em_notices(self):
        marker=self.db.get_meta('watermark:em_notices');start=parse_time(marker) if marker else now()-timedelta(days=3);day=start.date();last=min(now().date(),day+timedelta(days=7));out=[]
        while day<=last:
            rows=self.fetch.ak('stock_notice_report',symbol='全部',date=day.strftime('%Y%m%d'));part=[]
            for r in rows:
                try:oid=self.obj(r['代码'],r.get('名称'))
                except (ValueError,KeyError):continue
                title=clean(r.get('公告标题'));t=self.publication(r.get('公告日期'),date_only=True)
                if title:part.append(self.base('content','em_notices',{oid:'explicit'},t,family='eastmoney',record_id=uid('notice',oid,title,r.get('公告日期')),title=title,url=r.get('网址') or '',entity_id=oid,entity_type='company',extra=dict(source_type='statutory_disclosure_directory',full_text_read=False,time_precision='date_only_first_observed')))
            out.extend(self.commit_part(part));self.db.set_meta('watermark:em_notices',stamp(datetime.combine(day,datetime.min.time(),CN)));day+=timedelta(days=1)
        return out
    def tushare_hot_day(self,day):
        out=[]
        for market in ('热股','行业板块','概念板块','美股'):
            rows=self.fetch.tushare('ths_hot',trade_date=day.replace('-',''),market=market,is_new='N')
            for r in rows:
                code=str(r.get('ts_code'));name=clean(r.get('ts_name'));data_type=str(r.get('data_type') or market)
                try:oid=stock_id(code) if market=='热股' else ('stock:US:'+code if market=='美股' else 'board:ths:'+code)
                except ValueError:continue
                self.db.upsert_object(oid,name,'stock' if market in ('热股','美股') else 'board',market='US' if market=='美股' else 'A',aliases=[name])
                rt=str(r.get('rank_time') or '')
                if re.fullmatch(r'\d{2}:\d{2}(:\d{2})?',rt):rt=day+' '+rt
                if not rt:continue # No invented snapshot time.
                t=cn_time(rt)
                for metric,key,unit in [('rank','rank','rank'),('native_hot','hot','platform_index')]:
                    v=number(r.get(key))
                    if v is None:continue
                    out.append(self.base('snapshot','tushare_ths',{oid:'explicit'},t,family='10jqka',panel_id='ths-hot:'+data_type,
                        record_id=uid('ths',data_type,oid,metric,rt,v),metric=metric,value=v,unit=unit,window_kind='provider_snapshot',
                        extra=dict(reason=clean(r.get('rank_reason')),concept=r.get('concept'),cadence_seconds=7200,
                            market='US' if market=='美股' else 'A',timestamp_timezone='Asia/Shanghai',time_precision='provider_rank_time')))
        return out
    def collect_tushare_hot(self):return self.tushare_hot_day(now().date().isoformat())
    def collect_tushare_news(self):
        end=now();start=end-timedelta(hours=6);out=[]
        for src in ('sina','cls'):
            rows=self.fetch.tushare('news',src=src,start_date=start.strftime('%Y-%m-%d %H:%M:%S'),end_date=end.strftime('%Y-%m-%d %H:%M:%S'))
            for r in rows:
                title=clean(r.get('title'));text=clean(r.get('content'));t=self.publication(r.get('datetime'))
                if title or text:out.append(self.base('content','tushare_news:'+src,{},t,family=src,
                    record_id=uid(src,r.get('datetime'),title,text),title=title,text=text[:4000],entity_type='media_org',entity_id=src,
                    panel_id='news-sixhour-v1',extra=dict(coverage_complete=len(rows)<1500)))
        return out
    def collect_tushare_reports(self):
        out=[];end=now();start=end-timedelta(days=7)
        rows=self.fetch.tushare('report_rc',start_date=start.strftime('%Y%m%d'),end_date=end.strftime('%Y%m%d'))
        for r in rows:
            try:oid=self.obj(r['ts_code'],r.get('name'))
            except (KeyError,ValueError):continue
            org=clean(r.get('org_name'));author=clean(r.get('author_name'));t=self.publication(r.get('report_date'),date_only=True)
            title=clean(r.get('report_title'));rid=uid('report_rc',oid,org,author,r.get('report_date'),title,r.get('quarter'))
            team=org+'|'+author
            out.append(self.base('content','tushare_reports',{oid:'explicit'},t,family='tushare_research',record_id=uid(rid,'content'),title=title,
                entity_id=team,entity_type='sellside_team',panel_id='report-rc-v1',extra=dict(coverage_complete=len(rows)<3000,full_text_read=False)))
            for key,unit,scale in [('np','CNY',10000),('eps','CNY/share',1)]:
                v=number(r.get(key))
                if v is not None:out.append(self.base('forecast','tushare_reports',{oid:'explicit'},t,family='tushare_research',
                    record_id=uid(rid,key,v),metric=key,value=v*scale,unit=unit,entity_id=team,entity_type='sellside_team',
                    extra=dict(forecast_year=str(r.get('quarter')),report_id=rid,forecast_basis=key,report_date=str(r.get('report_date') or r.get('date') or '')[:10])))
        return out
    def collect_x_recent(self):
        token=self.cfg['credentials'].get('x_bearer') or os.getenv('X_BEARER_TOKEN','')
        if not token:raise SourceError('尚未提供X API授权')
        out=[];end=now()-timedelta(seconds=30);start=end-timedelta(hours=6)
        for query in self.cfg.get('x_queries',[]):
            panel='x-query:'+uid(query);nxt=None;complete=False
            for page in range(3):
                p={'query':query,'start_time':start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                   'end_time':end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'max_results':100,
                   'tweet.fields':'author_id,created_at,conversation_id,referenced_tweets,public_metrics'}
                if nxt:p['next_token']=nxt
                r=self.fetch.http('https://api.x.com/2/tweets/search/recent',params=p,headers={'Authorization':'Bearer '+token})
                if r.get('errors') and not r.get('data'):raise SourceError('X查询错误 '+str(r['errors'])[:300])
                for x in r.get('data',[]):
                    author=x.get('author_id');t=parse_time(x['created_at']);rid='x:'+x['id']
                    out.append(self.base('content','x',{},t,family='x',panel_id=panel,record_id=uid(rid,panel),text=x.get('text',''),url='https://x.com/i/status/'+x['id'],
                        entity_id=author,extra=dict(public_metrics=x.get('public_metrics',{}),references=x.get('referenced_tweets',[]),market='US')))
                    if author:out.append(self.base('participation','x',{},t,family='x',panel_id=panel,record_id=uid('act',rid,panel),entity_id=author,
                        text=x.get('text',''),extra=dict(content_id=rid,circles=[],market='US',coverage_complete=False)))
                nxt=(r.get('meta') or {}).get('next_token')
                if not nxt:complete=True;break
            # Only collection-plan coverage; API availability/retrospective search != complete full platform.
            out.append(self.base('coverage','x',{},end,family='x',panel_id=panel,
                extra=dict(start=stamp(start),end=stamp(end),complete=complete,scope='configured query; API eligible results',entity_type='account')))
        return out
    def collect_reddit(self):
        token=self.cfg['credentials'].get('reddit_bearer') or os.getenv('REDDIT_BEARER_TOKEN','')
        if not token:raise SourceError('尚未提供Reddit OAuth授权')
        out=[]
        for sub in self.cfg.get('reddit_subreddits',[]):
            if not re.fullmatch(r'[A-Za-z0-9_]{2,40}',sub):raise SourceError('subreddit名称无效')
            panel='reddit-new:'+sub;hdr={'Authorization':'Bearer '+token}
            r=self.fetch.http('https://oauth.reddit.com/r/'+sub+'/new',params={'limit':100,'raw_json':1},headers=hdr)
            posts=(r.get('data') or {}).get('children',[])
            for child in posts:
                x=child['data'];t=datetime.fromtimestamp(x['created_utc'],timezone.utc);rid='reddit:'+x['name'];author=x.get('author')
                text=x.get('selftext','');title=x.get('title','');url='https://www.reddit.com'+x.get('permalink','')
                out.append(self.base('content','reddit',{},t,family='reddit',record_id=rid,panel_id=panel,title=title,text=text[:4000],url=url,
                    entity_id=author if author not in ('[deleted]','[removed]') else None,extra=dict(score=x.get('score'),market='US')))
                if author and author not in ('[deleted]','[removed]'):
                    out.append(self.base('participation','reddit',{},t,family='reddit',record_id='act:'+rid,panel_id=panel,
                        entity_id=author,title=title,text=text[:4000],extra=dict(content_id=rid,market='US',circles=[])))
            # Bounded comment discovery. This is not an exhaustive comment census.
            for child in posts[:3]:
                x=child['data'];reply=self.fetch.http('https://oauth.reddit.com/comments/'+x['id'],params={'limit':100,'sort':'new','raw_json':1},headers=hdr)
                stack=list((reply[1].get('data') or {}).get('children',[])) if isinstance(reply,list) and len(reply)>1 else []
                while stack:
                    it=stack.pop();y=it.get('data') or {}
                    if it.get('kind')!='t1':continue
                    author=y.get('author');replies=y.get('replies')
                    if isinstance(replies,dict):stack.extend((replies.get('data') or {}).get('children',[]))
                    if not author or author in ('[deleted]','[removed]'):continue
                    t=datetime.fromtimestamp(y['created_utc'],timezone.utc)
                    out.append(self.base('participation','reddit',{},t,family='reddit',record_id='reddit-comment:'+y['id'],panel_id=panel+':comment-discovery',
                        entity_id=author,text=y.get('body','')[:4000],extra=dict(parent_id=y.get('parent_id'),parent_title=x.get('title'),market='US',circles=[])))
        return out
    def collect_rss(self):
        out=[]
        for feed in self.cfg.get('rss_feeds',[]):
            url=feed['url'];source='rss:'+feed.get('id',uid(url));content=self.fetch.http(url,raw=True)
            # ElementTree doesn't resolve external entities. Reject DTD for additional defense.
            if b'<!DOCTYPE' in content.upper():raise SourceError('不接受带DTD的XML')
            root=ET.fromstring(content);items=root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
            def text_of(node,*names):
                for name in names:
                    el=node.find(name)
                    if el is not None:return ''.join(el.itertext())
                return ''
            for it in items[:100]:
                title=clean(text_of(it,'title','{http://www.w3.org/2005/Atom}title'))
                text=clean(text_of(it,'description','{http://www.w3.org/2005/Atom}summary'))
                pub=text_of(it,'pubDate','{http://www.w3.org/2005/Atom}published','{http://www.w3.org/2005/Atom}updated')
                try:t=parsedate_to_datetime(pub) if ',' in pub else parse_time(pub)
                except (ValueError,TypeError):continue
                link=text_of(it,'link');el=it.find('{http://www.w3.org/2005/Atom}link')
                if not link and el is not None:link=el.get('href','')
                out.append(self.base('content',source,{},t,family=feed.get('publisher',source),record_id=uid(source,link,title,pub),title=title,text=text[:2000],url=link,
                    entity_id=feed.get('publisher',source),entity_type=feed.get('entity_type','industry_org'),panel_id='rss:'+uid(url),
                    extra=dict(market=feed.get('market','US'),coverage_complete=False,source_type='configured_official_feed')))
        return out
    def collect_licensed_http(self):
        url=self.cfg.get('licensed_url','');host=urllib.parse.urlparse(url).hostname
        if not url or host not in self.cfg.get('licensed_allowed_hosts',[]):raise SourceError('授权数据源URL未配置或域名未加入许可名单')
        token=self.cfg['credentials'].get('licensed_bearer','');cursor=self.db.get_meta('licensed_cursor')
        params={'limit':5000}
        if cursor:params['cursor']=cursor
        data=self.fetch.http(url,params=params,headers={'Authorization':'Bearer '+token} if token else {})
        rows=data.get('records')
        if not isinstance(rows,list):raise SourceError('供应商接口必须返回records数组')
        out=[];t=stamp()
        for r in rows:
            r=dict(r);r['observed_at']=t
            out.append(Record.model_validate(r).model_dump())
        # Cursor is committed by the orchestrator only after the records transaction succeeds.
        if data.get('next_cursor'):self.db.set_meta('licensed_pending_cursor',data['next_cursor'])
        else:self.db.set_meta('licensed_pending_cursor',None)
        return out
    def backfill(self,days=25,symbols=None):
        symbols=symbols or self.selected(limit=12,source='backfill')
        def one(oid):
            out=[]
            if not oid.startswith('stock:') or ':US:' in oid:return []
            rows=self.ak_part(oid,'stock_hot_rank_detail_em',symbol=oid.removeprefix('stock:'))
            for r in rows[-max(30,days) :]:
                if not r.get('时间'):continue
                t=cn_time(r['时间']);v=number(r.get('排名'))
                if v is None:continue
                # Daily history has an unknown intra-day cutoff: deliberately kept out of intraday comparisons.
                out.append(self.base('snapshot','em_rank_history',{oid:'explicit'},t,family='eastmoney',panel_id='em-history-daily-v1',
                    record_id=uid('emhist',oid,str(r['时间']),v),metric='rank',value=v,unit='rank',window_kind='provider_daily_history',
                    extra=dict(cadence_seconds=86400,time_precision='provider_daily_history',historical_backfill=True,
                        new_fan_ratio=number(r.get('新晋粉丝')),old_fan_ratio=number(r.get('铁杆粉丝')))))
            bars=self.ak_part(oid,'stock_zh_a_hist',symbol=oid[-6:],period='daily',start_date=(now()-timedelta(days=int(days*2))).strftime('%Y%m%d'),
                               end_date=now().strftime('%Y%m%d'),adjust='hfq')
            for r in bars:
                day=str(r.get('日期',''))[:10];v=number(r.get('涨跌幅'));t=cn_time(day)
                out.append(self.base('quote','em_daily',{oid:'explicit'},t,family='eastmoney',record_id=uid('daily',oid,r),
                    metric='daily_bar',window_kind='regular_close',extra=dict(date=day,open=number(r.get('开盘')),close=number(r.get('收盘')),
                    high=number(r.get('最高')),low=number(r.get('最低')),amount_cny=number(r.get('成交额')),turnover_pct=number(r.get('换手率')),
                    return_decimal=v/100 if v is not None else None,adjust='hfq',historical_backfill=True)))
            return out
        return self.per_object('backfill',symbols,one,21600)
    def collect_em_daily(self):
        pool=[s for s in self.db.historical_stocks() if ':US:' not in s] or self.selected(20,source='em_daily')
        chosen=self.db.fetch_candidates('em_daily',pool,20)
        def one(oid):
            bars=self.fetch.ak('stock_zh_a_hist',symbol=oid[-6:],period='daily',start_date=(now()-timedelta(days=120)).strftime('%Y%m%d'),end_date=now().strftime('%Y%m%d'),adjust='hfq');out=[]
            for r in bars:
                day=str(r.get('日期',''))[:10]
                if len(day)!=10:continue
                t=cn_time(day);ret=number(r.get('涨跌幅'))
                out.append(self.base('quote','em_daily',{oid:'explicit'},t,family='eastmoney',record_id=uid('daily-hfq',oid,r),metric='daily_bar',window_kind='regular_close',extra=dict(date=day,open=number(r.get('开盘')),close=number(r.get('收盘')),return_decimal=ret/100 if ret is not None else None,adjust='hfq')))
            return out
        out=self.per_object('em_daily',chosen,one,21600)
        try:
            rows=self.fetch.ak('stock_zh_index_daily_em',symbol='sh000300')
            for r in rows[-150:]:
                day=str(r.get('date',''))[:10]
                if len(day)!=10:continue
                out.append(self.base('quote','em_index',{'market:CSI300':'explicit'},cn_time(day),family='eastmoney',record_id=uid('csi300',r),metric='daily_bar',window_kind='regular_close',extra=dict(date=day,open=number(r.get('open')),close=number(r.get('close')),adjust='index')))
        except Exception as exc:self.partial_errors.append(dict(object_id='market:CSI300',message=str(exc)[:300]))
        return out
