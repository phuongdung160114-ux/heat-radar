from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal
import csv,io,json,logging,secrets,threading,time,os
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel,Field
from .common import ROOT,stamp,uid,read_json
from .engine import Engine
from .details import detail
from .models import IngestBatch,RunRequest,BackfillRequest,Record
from .eventstudy import evaluate

class Service:
    def __init__(self,data_dir,auto=True):
        self.data_dir=Path(data_dir);self.live=Engine(data_dir);self.mode='live';self.csrf=secrets.token_urlsafe(32)
        self.auto=auto;self.stop=threading.Event();self.jobs={};self.job_lock=threading.Lock();self.downloads={}
    @property
    def engine(self):return self.live
    def start_job(self,kind,fn):
        with self.job_lock:
            if any(j['status']=='running' for j in self.jobs.values()):raise ValueError('已有运行中的任务；可在任务栏查看进度')
            jid=uid(time.time_ns(),kind);job={'id':jid,'kind':kind,'status':'running','started_at':stamp(),'message':'任务开始','mode':self.mode}
            self.jobs[jid]=job
        def work():
            try:
                def progress(message,n=0,total=0):job.update(message=message,completed=n,total=total)
                result=fn(progress);job.update(status='done',finished_at=stamp(),message='完成',result=result)
            except Exception as exc:
                message=str(exc)[:1000]
                for v in self.live.settings.data['credentials'].values():
                    if v:message=message.replace(v,'[REDACTED]')
                job.update(status='error',finished_at=stamp(),message=message);logging.getLogger('radar').exception('job failed: %s',kind)
            with self.job_lock:
                for k in list(self.jobs)[:-30]:self.jobs.pop(k,None)
        threading.Thread(target=work,name='radar-'+kind,daemon=True).start();return job.copy()
    def scheduler(self):
        while not self.stop.wait(15):
            if not self.auto or not self.live.settings.data['auto_collect']:continue
            last=self.live.db.get_meta('scheduler_last',0)
            if time.time()-last<self.live.settings.data.get('scheduler_seconds',300):continue
            try:
                def run(progress):
                    collected=self.live.collect(progress=progress)
                    if collected['sources'] or not self.live.db.runs(1):
                        progress('计算注意力与双目标排序');r=self.live.analyze()
                        from .labels import research_cycle
                        research=research_cycle(self.live,progress)
                        return {'collection':collected,'run_id':r['run_id'],'research':research}
                    return {'collection':collected}
                self.start_job('自动监测',run);self.live.db.set_meta('scheduler_last',time.time())
            except ValueError:pass
    def get_run(self,run_id=None):
        if run_id:
            with self.engine.db.connect() as c:r=c.execute('SELECT payload FROM runs WHERE id=?',(run_id,)).fetchone()
            if not r:raise ValueError('找不到该运行')
            return json.loads(r[0])
        r=self.engine.db.runs(limit=1);return r[0] if r else None

def compact_run(run):
    if not run:return None
    d={k:v for k,v in run.items() if k not in ('items','plan','providers','intraday_path','memberships','graphs','source_diagnostics')}
    d['scan']={k:v for k,v in run.get('scan',{}).items() if k not in ('manifest','controls')}
    d['items']=[]
    for row in run['items']:
        r={k:v for k,v in row.items() if k not in ('attention','diffusion','afterclose_information','source_record_ids')}
        r['sources']=list(dict.fromkeys(x['source'] for x in row['attention']))
        r['information']={k:v for k,v in row['information'].items() if k not in ('items','forecast_revisions','events')}
        r['trading']={k:v for k,v in row['trading'].items() if k!='members'}
        d['items'].append(r)
    return d

def report_md(run):
    lines=['# A股热度雷达 · '+run['phase'],f"截止：{run['as_of']}｜实时监测",
           '\n计算类型：'+run.get('evaluation_type','legacy')+'\n', '| 对象 | 阶段 | 分组 | 可观察依据 |','|---|---|---|---|']
    for x in run['items']:
        escape=lambda v:str(v).replace('|','／').replace('\n',' ')
        lines.append('| '+' | '.join(escape(v) for v in (x['name'],x['stage_name'],x['band'],x['why']))+' |')
    for x in run['items']:
        if x['band'] not in ('confirmed','discovery','risk'):continue
        lines.extend(['\n## '+x['name'],x['why']])
        for e in x['attention']:
            lines.append(f"\n{e['source']} / {e['metric']}：当前 {e['value']} {e['unit']}，相邻变化 {e.get('delta')}，Q20 {e.get('q')}，20期有效样本 {e.get('history20_n')}，覆盖 {e.get('coverage')}，有效时刻 {e['at']}。")
        for info in x['information'].get('items',[]):lines.append('\n'+info['at']+' '+info['title']+' '+info.get('url',''))
    return '\n'.join(lines)

def create_app(data_dir=None,auto=True):
    service=Service(data_dir or os.getenv('RADAR_DATA_DIR',str(ROOT/'data')),auto)
    @asynccontextmanager
    async def lifespan(app):
        thread=threading.Thread(target=service.scheduler,name='radar-scheduler',daemon=True);thread.start()
        yield
        service.stop.set()
    app=FastAPI(title='Ashare Heat Radar',version='2.0.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url='/api/openapi.json')
    app.state.service=service
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])
    app.add_middleware(GZipMiddleware,minimum_size=1000)
    @app.middleware('http')
    async def protect(request,call_next):
        origin=request.headers.get('origin')
        if origin:
            parsed=urlparse(origin)
            if parsed.scheme not in ('http','https') or parsed.netloc!=request.headers.get('host'):return JSONResponse({'detail':'拒绝跨站请求'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            if not secrets.compare_digest(request.headers.get('x-radar-token',''),service.csrf):return JSONResponse({'detail':'缺少本地会话令牌'},status_code=403)
            try:size=int(request.headers.get('content-length','0'))
            except ValueError:return JSONResponse({'detail':'无效请求长度'},status_code=400)
            if size>40_000_000:return JSONResponse({'detail':'请求超出40MB'},status_code=413)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff';response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(ValueError)
    async def bad(request,exc):return JSONResponse({'detail':str(exc)},status_code=400)
    @app.get('/')
    def home():return FileResponse(ROOT/'static/index.html')
    @app.get('/favicon.ico',include_in_schema=False)
    def favicon():return Response(status_code=204)
    @app.get('/api/health')
    def health():return {'application':'AshareHeatRadar','version':'2.0.0','mode':'live','ok':True}
    @app.get('/api/bootstrap')
    def bootstrap():return {'token':service.csrf,'mode':'live','settings':service.live.settings.public(),'version':'2.0.0','calendar_valid_to':service.live.calendar.data['valid_to']}
    @app.get('/api/state')
    def state(run_id:str|None=None):
        run=service.get_run(run_id)
        return {'mode':'live','run':compact_run(run),'feedback':service.engine.db.feedback(run['run_id']) if run else [],'stats':service.engine.db.stats(),'jobs':list(service.jobs.values()),'providers':service.engine.provider_status()}
    @app.get('/api/jobs')
    def jobs():return list(service.jobs.values())
    @app.get('/api/runs')
    def runs():return [{k:r[k] for k in ('run_id','phase','as_of','target_session','mode','counts')} for r in service.engine.db.runs(150)]
    @app.get('/api/detail')
    def object_detail(object_id:str,run_id:str|None=None):
        r=service.get_run(run_id)
        if not r:raise ValueError('尚无运行结果')
        return detail(service.engine,object_id,r)
    @app.get('/api/settings')
    def settings():return service.live.settings.public()
    @app.post('/api/settings')
    def update_settings(payload:dict):
        return service.live.settings.update(payload)
    @app.post('/api/collect')
    def collect():
        engine=service.engine
        def work(progress):
            result=engine.collect(force=True,progress=progress);progress('正在比较历史与生成候选');run=engine.analyze()
            return {'collection':result,'run_id':run['run_id']}
        return service.start_job('采集并筛选',work)
    @app.post('/api/analyze')
    def analyze(payload:RunRequest):
        engine=service.engine
        return service.start_job('重算信号',lambda progress:{'run_id':engine.analyze(payload.as_of,payload.phase)['run_id']})
    @app.post('/api/backfill')
    def backfill(payload:BackfillRequest):
        engine=service.engine
        return service.start_job('历史回填',lambda progress:engine.backfill(payload.days,payload.symbols))
    @app.post('/api/import')
    def import_data(payload:IngestBatch):
        n=service.live.ingest(payload.records,payload.preserve_observed_at)
        return {'inserted':n,'observed_at_policy':'按导入文件保留（由导入者证明时间真实性）' if payload.preserve_observed_at else '按本次导入时间记录'}
    @app.get('/api/schema')
    def schema():return Record.model_json_schema()
    @app.post('/api/topic')
    def add_topic(payload:dict):return {'id':service.engine.add_topic(payload)}
    @app.post('/api/mapping')
    def add_mapping(payload:dict):return service.engine.add_mapping(payload)
    @app.get('/api/mappings')
    def mappings():return service.engine.db.all_members(stamp())
    @app.post('/api/mapping/close')
    def close_mapping(payload:dict):
        edge=next((e for e in service.engine.db.all_members(stamp()) if e['edge_id']==payload.get('edge_id')),None)
        if not edge:raise ValueError('关系不存在或已关闭')
        edge.update(effective_to=stamp(),revoked=True)
        return service.engine.add_mapping(edge)
    @app.post('/api/feedback')
    def feedback(payload:dict):
        return service.engine.db.save_feedback(str(payload.get('run_id','')),str(payload.get('object_id','')),str(payload.get('decision','')),str(payload.get('comment','')))
    @app.get('/api/feedback')
    def get_feedback(run_id:str):return service.engine.db.feedback(run_id)
    @app.get('/api/candidate-pool')
    def candidate_pool(run_id:str):
        run=service.get_run(run_id)
        return run.get('scan',{}) if run else {}
    @app.get('/api/objects')
    def objects():return service.engine.db.objects()
    @app.get('/api/eventstudy')
    def eventstudy(run_id:str|None=None):return evaluate(service.engine,run_id)
    @app.get('/api/export')
    def export(format:Literal['json','md','csv']='json',run_id:str|None=None):
        run=service.get_run(run_id)
        if not run:raise ValueError('尚无运行可导出')
        if format=='json':body=json.dumps(run,ensure_ascii=False,indent=2);mime='application/json'
        elif format=='md':body=report_md(run);mime='text/markdown'
        else:
            buffer=io.StringIO();writer=csv.writer(buffer);writer.writerow(['object_id','name','selection_rank','group','stock_category','source_families','anomaly_percentile','new_events','response','as_of','why'])
            for x in run['items']:
                row=[x['id'],x['name'],x.get('selection_rank'),x.get('selection_group_name'),x.get('stock_category'),x.get('evidence',{}).get('family_count'),x.get('evidence',{}).get('anomaly_percentile'),x['information'].get('new_event_count'),x.get('response_state'),run['as_of'],x['why']]
                writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in row])
            body='\ufeff'+buffer.getvalue();mime='text/csv'
        return Response(body,media_type=mime,headers={'Content-Disposition':f'attachment; filename="{run["run_id"]}.{format}"'})
    @app.post('/api/backup')
    def backup():
        path=service.data_dir/'backups'/('radar-live-'+uid(time.time_ns())[:10]+'.sqlite');path.parent.mkdir(parents=True,exist_ok=True)
        service.engine.db.backup(path);token=secrets.token_urlsafe(24);service.downloads[token]=path
        return {'download':'/api/download/'+token,'note':'备份含本库数据，不含settings.json中的Token；映射与运行在数据库内。'}
    @app.get('/api/download/{token}')
    def download(token:str):
        path=service.downloads.get(token)
        if not path:raise HTTPException(404,'下载不存在')
        return FileResponse(path,filename=path.name)
    from .research_api import router
    app.include_router(router(service))
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return app
