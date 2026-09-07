"""Additional A-share behavioral, business and time-series source adapters."""
from __future__ import annotations
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import csv, io, json, re
from .common import now, stamp, cn_time, parse_time, number, uid, clean
from .models import Record


def import_rows(rows,default_source='local_feed',observed_at=None):
    result=[];observed_at=observed_at or stamp()
    for index,raw in enumerate(rows):
        r=dict(raw)
        if 'objects' not in r:
            oid=r.pop('object_id',None)
            if not oid:raise ValueError(f'第{index+1}行缺少 objects 或 object_id')
            r['objects']={oid:r.pop('attribution','explicit')}
        elif isinstance(r['objects'],str):r['objects']=json.loads(r['objects'])
        if isinstance(r.get('extra'),str):r['extra']=json.loads(r['extra'] or '{}')
        r.setdefault('source',default_source);r.setdefault('family',r['source'])
        r['observed_at']=r.get('observed_at') or observed_at
        for key in ('value',):
            if r.get(key)=='':r[key]=None
        for key in ('entity_id',):
            if r.get(key)=='':r[key]=None
        r.setdefault('record_id',uid('file-import-v2',r))
        result.append(Record.model_validate(r).model_dump())
    return result


class ExtendedCollector:
    """Mixin. Collector supplies fetch/base/obj/per_object/store facilities."""
    def collect_a_universe(self):
        rows=self.fetch.ak('stock_info_a_code_name');at=now();out=[];ids=[]
        for row in rows:
            oid=self.obj(row['code'],row.get('name'));ids.append(oid)
            self.db.upsert_object(oid,row.get('name'),'stock',universe_member=True,universe_seen_at=stamp(at))
            out.append(self.base('claim','a_universe',{oid:'explicit'},at,family='exchange_directory',metric='security_master',
                record_id=uid('master',oid,at.date().isoformat(),row),extra={'name':row.get('name'),'scope':'沪深京A股证券目录'}))
        if not ids:raise ValueError('证券目录没有有效记录')
        previous=self.db.get_meta('universe_snapshot',{})
        for retired in set(previous.get('ids',[]))-set(ids):
            self.db.upsert_object(retired,universe_member=False,universe_seen_at=stamp(at))
        version=uid(sorted(ids));self.db.set_meta('universe_snapshot',{'at':stamp(at),'ids':ids,'version':version})
        return out

    def collect_cninfo_irm(self):
        def one(oid):
            rows=self.fetch.ak('stock_irm_cninfo',symbol=oid[-6:]);out=[];at=now();times=[];invalid=0
            start=at-timedelta(hours=6);panel='cninfo-question:'+oid
            for r in rows:
                try:t=cn_time(str(r['提问时间']))
                except (KeyError,ValueError):invalid+=1;continue
                if t>at:invalid+=1;continue
                times.append(t);actor=clean(r.get('提问者编号'));qid=clean(r.get('问题编号'))
                if not qid:invalid+=1;continue
                text=clean(r.get('问题'));answer=clean(r.get('回答内容'))
                industry=clean(r.get('行业'))
                if industry:self.db.upsert_object(oid,industry=industry,industry_code=clean(r.get('行业代码')))
                extra={'question_id':qid,'page_object':oid,'source_role':'retail','market':'A','question_text':text}
                # Questions are attention/information requests, not company-confirmed facts.
                out.append(self.base('content','cninfo_irm',{oid:'context'},t,family='cninfo',panel_id=panel,
                    record_id=uid('cninfo-question',qid),title=text[:100],text=text,entity_id=actor or None,
                    url='https://irm.cninfo.com.cn/',extra={**extra,'content_role':'investor_question'}))
                if actor:
                    out.append(self.base('participation','cninfo_irm',{oid:'context'},t,family='cninfo',panel_id=panel,
                        record_id=uid('cninfo-actor',qid),entity_id=actor,entity_type='account',text=text,extra=extra))
                else:invalid+=1
                if answer:
                    try:updated=cn_time(str(r.get('更新时间')))
                    except (ValueError,TypeError):updated=at
                    if updated>at:updated=at
                    out.append(self.base('content','cninfo_answers',{oid:'explicit'},updated,family='cninfo',
                        record_id=uid('cninfo-answer',r.get('回答ID'),qid,answer),title=answer[:100],text=answer,
                        entity_id=oid,entity_type='company',url='https://irm.cninfo.com.cn/',
                        extra={'question_id':qid,'content_role':'company_answer','question_text':text}))
            complete=bool(times and min(times)<=start and invalid==0)
            out.append(self.base('coverage','cninfo_irm',{oid:'context'},at,family='cninfo',panel_id=panel,
                extra={'start':stamp(start),'end':stamp(at),'complete':complete,'entity_type':'account','scope':'指定公司公开提问记录',
                       'returned':len(rows),'invalid':invalid}))
            return out
        return self.per_object('cninfo_irm',self.selected(source='cninfo_irm'),one,1800)

    def collect_em_attention_detail(self):
        def one(oid):
            out=[]
            history=self.ak_part(oid,'stock_hot_rank_detail_em',symbol=oid.removeprefix('stock:'))
            for r in history[-90:]:
                day=str(r.get('时间',''))[:10]
                if len(day)!=10:continue
                t=cn_time(day)
                if t>now():continue
                for metric,key in [('new_fan_share','新晋粉丝'),('loyal_fan_share','铁杆粉丝')]:
                    value=number(r.get(key))
                    if value is None or not 0<=value<=1:continue
                    out.append(self.base('snapshot','em_fans',{oid:'explicit'},t,family='eastmoney',metric=metric,value=value,
                        unit='fraction',window_kind='provider_daily_history',record_id=uid('fan-mix',oid,day,metric,value),
                        extra={'cadence_seconds':86400,'measure_type':'share','definition':'平台披露的粉丝结构占比'}))
            focus=self.ak_part(oid,'stock_comment_detail_scrd_focus_em',symbol=oid[-6:])
            for r in focus:
                day=str(r.get('交易日',''))[:10];value=number(r.get('用户关注指数'))
                if len(day)!=10 or value is None:continue
                t=cn_time(day)
                if t>now():continue
                out.append(self.base('snapshot','em_focus',{oid:'explicit'},t,family='eastmoney',metric='attention_index',value=value,
                    unit='platform_index',window_kind='provider_daily_history',record_id=uid('focus',oid,day,value),
                    extra={'cadence_seconds':86400,'definition':'平台用户关注指数'}))
            # Intraday history is a separate panel, with the actual acquisition time retained.
            realtime=self.ak_part(oid,'stock_hot_rank_detail_realtime_em',symbol=oid.removeprefix('stock:'))
            for r in realtime:
                try:t=cn_time(r['时间']);value=number(r.get('排名'))
                except (ValueError,KeyError):continue
                if value is None or t>now():continue
                out.append(self.base('snapshot','em_rank_detail',{oid:'explicit'},t,family='eastmoney',panel_id='rank-detail-10m',
                    metric='rank',value=value,unit='rank',window_kind='provider_snapshot',record_id=uid('rank-detail',oid,r['时间'],value),
                    extra={'cadence_seconds':600,'timestamp_basis':'provider_snapshot','historical_backfill':True}))
            return out
        return self.per_object('em_attention_detail',self.selected(source='em_attention_detail'),one,3600)

    def collect_business_segments(self):
        from .classify import has_term
        topics=[o for o in self.db.objects() if o['kind'] in ('topic','board')]
        def one(oid):
            rows=self.ak_part(oid,'stock_zygc_em',symbol=oid.removeprefix('stock:'));out=[];at=now()
            dates=sorted({str(r.get('报告日期',''))[:10] for r in rows if r.get('报告日期')})
            latest=dates[-1] if dates else None
            for r in rows:
                day=str(r.get('报告日期',''))[:10];product=clean(r.get('主营构成'));category=clean(r.get('分类类型'))
                if not product or len(day)!=10:continue
                revenue=number(r.get('主营收入'));share=number(r.get('收入比例'))
                if share is not None and not 0<=share<=1:share=None
                ex={'period':day,'category':category,'product':product,'revenue':revenue,'revenue_share':share,
                    'profit':number(r.get('主营利润')),'margin':number(r.get('毛利率')),'measurement':'reported_business_segment'}
                url='https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/Index?type=web&code='+oid.removeprefix('stock:')
                out.append(self.base('claim','business_segments',{oid:'explicit'},at,family='eastmoney',metric='business_segment',
                    record_id=uid('business-segment',oid,ex),value=revenue,unit='CNY',title=product,url=url,extra=ex))
                if day!=latest or category not in ('按产品分类','按行业分类'):continue
                for topic in topics:
                    hits=[t for t in topic.get('aliases',[topic['name']]) if len(t)>=2 and has_term(product,t)]
                    if not hits:continue
                    self.db.membership(topic['id'],oid,relation='direct_business',source='reported_segment',known_at=stamp(at),
                        direction='unknown',product=product,commercial_stage='主营收入',evidence_url=url,evidence_date=day,
                        revenue_share=share,description=f'{day} {category}：{product}',review_state='reported_segment_match',
                        segment_category=category,segment_period=day,matched_terms=hits)
            # Industry, capitalization and listing date are independently supplied fields.
            profile=self.ak_part(oid,'stock_individual_info_em',symbol=oid[-6:])
            fields={str(r.get('item')):r.get('value') for r in profile}
            if fields:
                self.db.upsert_object(oid,industry=clean(fields.get('行业')),listing_date=str(fields.get('上市时间') or ''),
                    float_cap_cny=number(fields.get('流通市值')),profile_transport=profile[0].get('_radar',{}))
            return out
        return self.per_object('business_segments',self.selected(source='business_segments'),one,86400)

    def collect_em_minutes(self):
        pool=self.selected(limit=self.cfg.get('minute_scan_per_run',16),source='em_minutes')
        def one(oid):
            rows=self.fetch.ak('stock_zh_a_hist_min_em',symbol=oid[-6:],period='5',adjust='hfq',
                start_date=(now()-timedelta(days=10)).strftime('%Y-%m-%d 09:30:00'),end_date=now().strftime('%Y-%m-%d %H:%M:%S'))
            out=[]
            for r in rows:
                try:t=cn_time(r['时间'])
                except (ValueError,KeyError):continue
                if t>now():continue
                price=number(r.get('收盘'))
                if price is None or price<=0:continue
                out.append(self.base('quote','em_minutes',{oid:'explicit'},t,family='eastmoney',metric='minute_bar',value=price,
                    unit='CNY',window_kind='5m_bar',record_id=uid('minute-hfq',oid,r),
                    extra={'open':number(r.get('开盘')),'close':price,'high':number(r.get('最高')),'low':number(r.get('最低')),
                           'amount_cny':number(r.get('成交额')),'adjust':'hfq','period_minutes':5,'date':t.date().isoformat()}))
            return out
        return self.per_object('em_minutes',pool,one,900)

    def collect_disclosure_text(self):
        records=self.db.read(kind='content',start=now()-timedelta(days=30),as_of=now())
        candidates=[r for r in records if r['source'] in ('em_notices','em_research','tushare_reports') and r.get('url')]
        byid={r['record_id']:r for r in candidates}
        chosen=self.db.fetch_candidates('disclosure_text',list(byid),self.cfg.get('fulltext_per_run',6))
        def one(rid):
            record=byid[rid];url=record['url'];blob=self.fetch.http(url,raw=True);text='';document_url=url
            if blob.startswith(b'%PDF'):
                from pypdf import PdfReader
                document=PdfReader(io.BytesIO(blob));text='\n'.join(page.extract_text() or '' for page in document.pages[:150])
            else:
                from bs4 import BeautifulSoup
                soup=BeautifulSoup(blob,'html.parser')
                links=[urljoin(url,a['href']) for a in soup.find_all('a',href=True) if re.search(r'\.pdf(?:$|\?)',a['href'],re.I)]
                if links:
                    document_url=links[0];pdf=self.fetch.http(document_url,raw=True)
                    if pdf.startswith(b'%PDF'):
                        from pypdf import PdfReader
                        doc=PdfReader(io.BytesIO(pdf));text='\n'.join(p.extract_text() or '' for p in doc.pages[:150])
                if not text:
                    node=soup.select_one('.detail-body,.Body,.newsContent,.txtinfos,#ContentBody,article')
                    if node:text=node.get_text('\n',strip=True)
            if len(text.strip())<40:raise ValueError('正文提取未得到足够文本')
            return [self.base('content','disclosure_text',record['objects'],now(),family=record['family'],
                record_id=uid('fulltext-v2',rid,text),title=record['title'],text=text[:150000],url=document_url,
                entity_id=record.get('entity_id'),entity_type=record.get('entity_type','company'),
                extra={'parent_record_id':rid,'raw_published_at':record.get('extra',{}).get('raw_published_at') or record['occurred_at'],
                       'full_text_read':True,'text_length':len(text),'content_role':'disclosure_fulltext'} )]
        return self.per_object('disclosure_text',chosen,one,86400*30)

    def collect_local_feed(self):
        directory=Path(self.cfg.get('local_feed_dir') or self.db.path.parent.parent/'inbox');directory.mkdir(parents=True,exist_ok=True)
        processed=self.db.get_meta('local_feed_processed',{});out=[]
        for path in sorted(directory.glob('*')):
            if path.suffix.lower() not in ('.csv','.jsonl','.json'):continue
            fingerprint=uid(path.name,path.stat().st_size,path.read_bytes().hex())
            if processed.get(path.name)==fingerprint:continue
            raw=path.read_text(encoding='utf-8-sig')
            if path.suffix.lower()=='.csv':rows=list(csv.DictReader(io.StringIO(raw)))
            elif path.suffix.lower()=='.jsonl':rows=[json.loads(line) for line in raw.splitlines() if line.strip()]
            else:
                payload=json.loads(raw);rows=payload.get('records',[]) if isinstance(payload,dict) else payload
            normalized=import_rows(rows)
            if any(r.get('extra',{}).get('synthetic') for r in normalized):raise ValueError('演示面板使用独立演示数据库')
            self.commit_part(normalized);out.extend(normalized);processed[path.name]=fingerprint
            self.db.set_meta('local_feed_processed',processed)
        return out
