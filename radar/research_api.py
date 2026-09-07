"""Research workspace routes shared by browser and reproducible local scripts."""
from __future__ import annotations
import csv,io,json,time
from fastapi import APIRouter
from fastapi.responses import Response
from .common import stamp,parse_time
from .capabilities import capability_matrix
from .features import load_panel,FEATURE_DESCRIPTIONS,FEATURE_NAMES
from .labels import update_labels,label_status
from .learning import run_experiment,list_experiments


def router(service):
    r=APIRouter(prefix='/api')
    @r.get('/capabilities')
    def capabilities(run_id:str|None=None):return capability_matrix(service.engine,service.get_run(run_id))
    @r.get('/graphs')
    def graphs(run_id:str|None=None):return (service.get_run(run_id) or {}).get('graphs',{})
    @r.get('/terms')
    def terms(run_id:str|None=None):return (service.get_run(run_id) or {}).get('term_discovery',[])
    @r.get('/features/schema')
    def feature_schema():return {'version':'2.0.0','features':[{'name':f,'description':FEATURE_DESCRIPTIONS.get(f,f)} for f in FEATURE_NAMES]}
    @r.get('/features/export')
    def feature_export(format:str='csv'):
        rows=load_panel(service.engine.db)
        if format=='json':return Response(json.dumps(rows,ensure_ascii=False),media_type='application/json',headers={'Content-Disposition':'attachment; filename="feature_panel.json"'})
        stream=io.StringIO();w=csv.writer(stream);w.writerow(['run_id','id','at','episode_id','synthetic']+FEATURE_NAMES)
        for row in rows:w.writerow([row.get(k) for k in ('run_id','id','at','episode_id','synthetic')]+[row['features'].get(f) for f in FEATURE_NAMES])
        return Response('\ufeff'+stream.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="feature_panel.csv"'})
    @r.get('/research/diagnostics')
    def observed_diagnostics():
        from .diagnostics import diagnostics
        return diagnostics(service.engine)
    @r.get('/research/status')
    def status():return {**label_status(service.engine),'experiments':list_experiments(service.engine.db),'models':models()}
    @r.post('/research/labels')
    def labels(payload:dict):
        engine=service.engine
        return service.start_job('更新后续标签',lambda progress:update_labels(engine,payload.get('as_of'),payload.get('run_ids'),progress))
    @r.get('/research/labels')
    def label_rows(target:str='attention_1d',run_id:str|None=None,limit:int=1000):
        with service.engine.db.connect() as c:
            sql='SELECT payload FROM research_labels WHERE horizon=?';args=[target]
            if run_id:sql+=' AND run_id=?';args.append(run_id)
            sql+=' ORDER BY available_ts DESC LIMIT ?';args.append(max(1,min(limit,50000)))
            return [json.loads(x[0]) for x in c.execute(sql,args)]
    @r.post('/research/experiment')
    def experiment(payload:dict):
        engine=service.engine;cfg=dict(payload);clock=cfg.pop('as_of',None)
        return service.start_job('时间滚动研究实验',lambda progress:run_experiment(engine,cfg,clock,progress))
    @r.get('/research/experiment/{experiment_id}')
    def experiment_result(experiment_id:str):
        with service.engine.db.connect() as c:row=c.execute('SELECT payload FROM experiments WHERE id=?',(experiment_id,)).fetchone()
        if not row:raise ValueError('研究实验不存在')
        return json.loads(row[0])
    @r.get('/research/models')
    def models():
        with service.engine.db.connect() as c:rows=[json.loads(x[0]) for x in c.execute('SELECT payload FROM model_registry ORDER BY available_ts DESC')]
        return [{k:v for k,v in x.items() if k not in ('model_text','coef','preprocessing','intercept')} for x in rows]
    @r.post('/research/model/activate')
    def activate(payload:dict):
        mid=payload.get('model_id');active=bool(payload.get('active',True))
        with service.engine.db.lock,service.engine.db.connect() as c:
            row=c.execute('SELECT payload FROM model_registry WHERE id=?',(mid,)).fetchone()
            if not row:raise ValueError('模型不存在')
            model=json.loads(row[0])
            if active and model['target'].startswith('holdout:'):raise ValueError('留出来源实验用于独立观测检验，不直接替换完整来源排序')
            model['active']=active;model['activation_at']=stamp()
            # New activation cannot change an already frozen historical selection.
            if active:model['available_at']=max(stamp(),model['available_at'],key=parse_time)
            c.execute('UPDATE model_registry SET available_ts=?,payload=? WHERE id=?',(parse_time(model['available_at']).timestamp(),json.dumps(model,ensure_ascii=False),mid))
        return {'model_id':mid,'active':active}
    return r
