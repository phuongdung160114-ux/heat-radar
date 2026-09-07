from __future__ import annotations
import json, sqlite3, threading
from contextlib import contextmanager, closing
from pathlib import Path
from .common import stamp, parse_time, uid
from .models import Record

SCHEMA='''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,logical_id TEXT NOT NULL,kind TEXT NOT NULL,source TEXT NOT NULL,panel_id TEXT NOT NULL,event_ts REAL NOT NULL,observed_ts REAL NOT NULL,metric TEXT,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS record_time ON records(kind,event_ts,observed_ts);
CREATE INDEX IF NOT EXISTS record_metric_time ON records(metric,event_ts,observed_ts);
CREATE INDEX IF NOT EXISTS record_logical ON records(logical_id,observed_ts);
CREATE TABLE IF NOT EXISTS object_records(object_id TEXT,record_id TEXT REFERENCES records(id),PRIMARY KEY(object_id,record_id));
CREATE INDEX IF NOT EXISTS object_record_index ON object_records(object_id);
CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT,market TEXT,known_ts REAL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memberships(id TEXT PRIMARY KEY,topic_id TEXT,symbol TEXT,relation TEXT,effective_from TEXT,effective_to TEXT,known_ts REAL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS object_versions(version_id TEXT PRIMARY KEY,object_id TEXT,known_ts REAL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS object_version_time ON object_versions(object_id,known_ts);
CREATE TABLE IF NOT EXISTS member_versions(version_id TEXT PRIMARY KEY,edge_id TEXT,topic_id TEXT,symbol TEXT,known_ts REAL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS member_version_time ON member_versions(topic_id,edge_id,known_ts);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,phase TEXT,asof_ts REAL,created_at TEXT,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS run_time ON runs(asof_ts);
CREATE TABLE IF NOT EXISTS health(source TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fetch_tasks(source TEXT,object_id TEXT,last_success REAL DEFAULT 0,next_due REAL DEFAULT 0,failures INTEGER DEFAULT 0,message TEXT DEFAULT '',PRIMARY KEY(source,object_id));
CREATE TABLE IF NOT EXISTS run_contexts(id TEXT PRIMARY KEY,asof_ts REAL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY,run_id TEXT,created_at TEXT,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,run_id TEXT,object_id TEXT,created_at TEXT,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feature_panels(run_id TEXT,object_id TEXT,asof_ts REAL,episode_id TEXT,payload TEXT NOT NULL,PRIMARY KEY(run_id,object_id));
CREATE INDEX IF NOT EXISTS features_time ON feature_panels(asof_ts,object_id);
CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,created_at TEXT,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_registry(id TEXT PRIMARY KEY,created_at TEXT,target TEXT,available_ts REAL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_labels(run_id TEXT,object_id TEXT,horizon TEXT,available_ts REAL,payload TEXT NOT NULL,PRIMARY KEY(run_id,object_id,horizon));
CREATE INDEX IF NOT EXISTS research_labels_time ON research_labels(available_ts);
CREATE TABLE IF NOT EXISTS term_snapshots(asof_ts REAL,term_id TEXT,payload TEXT NOT NULL,PRIMARY KEY(asof_ts,term_id));
CREATE TABLE IF NOT EXISTS source_snapshots(run_id TEXT,source TEXT,payload TEXT NOT NULL,PRIMARY KEY(run_id,source));

'''

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock()
        with self.connect() as c:
            c.executescript(SCHEMA)
            migrated=c.execute("SELECT 1 FROM meta WHERE key='migration_102'").fetchone()
            if not migrated:
                for row in c.execute('SELECT * FROM objects').fetchall():
                    d=json.loads(row['payload']);t=max(row['known_ts'],parse_time(d.get('rule_known_at') or d['known_at']).timestamp())
                    c.execute('INSERT OR IGNORE INTO object_versions VALUES(?,?,?,?)',(uid(row['id'],t,d),row['id'],t,row['payload']))
                for row in c.execute('SELECT * FROM memberships ORDER BY known_ts').fetchall():
                    d=json.loads(row['payload']);edge=uid(d['topic_id'],d['symbol'],d['relation'],d.get('source','manual'));d['edge_id']=edge
                    c.execute('INSERT OR IGNORE INTO member_versions VALUES(?,?,?,?,?,?)',(uid(edge,row['known_ts'],d),edge,d['topic_id'],d['symbol'],row['known_ts'],json.dumps(d,ensure_ascii=False)))
                c.execute('INSERT INTO meta VALUES(?,?)',('migration_102',json.dumps(stamp())))
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=30)
        try:
            c.execute('PRAGMA busy_timeout=30000');c.row_factory=sqlite3.Row
            with c:yield c
        finally:c.close()
    def append(self,records):
        n=0
        with self.lock,self.connect() as c:
            for raw in records:
                r=raw if isinstance(raw,Record) else Record.model_validate(raw);d=r.model_dump()
                if c.execute('SELECT 1 FROM records WHERE logical_id=?',(r.record_id,)).fetchone():continue
                rid=uid(r.record_id,r.observed_at)
                c.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',(rid,r.record_id,r.kind,r.source,r.panel_id,parse_time(r.occurred_at).timestamp(),parse_time(r.observed_at).timestamp(),r.metric,json.dumps(d,ensure_ascii=False)))
                c.executemany('INSERT OR IGNORE INTO object_records VALUES(?,?)',[(o,rid) for o in r.objects]);n+=1
        return n
    def read(self,kind=None,object_id=None,start=None,end=None,as_of=None,limit=None,metric=None):
        sql='SELECT r.payload FROM records r ';where=[];args=[]
        if object_id:sql+='JOIN object_records o ON o.record_id=r.id ';where.append('o.object_id=?');args.append(object_id)
        if metric:where.append('r.metric=?');args.append(metric)
        if kind:
            if isinstance(kind,list):where.append('r.kind IN ('+','.join('?' for _ in kind)+')');args.extend(kind)
            else:where.append('r.kind=?');args.append(kind)
        for value,expr in [(start,'r.event_ts>=?'),(end,'r.event_ts<?'),(as_of,'r.observed_ts<=?')]:
            if value is not None:where.append(expr);args.append(parse_time(value).timestamp())
        if where:sql+='WHERE '+' AND '.join(where)
        sql+=' ORDER BY r.event_ts,r.observed_ts,r.id'
        if limit:sql+=' LIMIT ?';args.append(limit)
        with self.connect() as c:return [json.loads(x[0]) for x in c.execute(sql,args)]
    def candidate_summary(self,start,at):
        args=(parse_time(start).timestamp(),parse_time(at).timestamp(),parse_time(at).timestamp())
        sql="""WITH base AS (
            SELECT o.object_id,r.kind,r.source,r.panel_id,r.metric,r.event_ts,r.observed_ts,
                json_extract(r.payload,'$.value') value
            FROM records r JOIN object_records o ON o.record_id=r.id
            WHERE r.kind IN ('snapshot','participation','content','quote') AND r.event_ts>=? AND r.event_ts<=? AND r.observed_ts<=?),
        ranked AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY object_id,source,panel_id ORDER BY event_ts DESC,observed_ts DESC) latest,
            ROW_NUMBER() OVER(PARTITION BY object_id,source,panel_id ORDER BY event_ts,observed_ts) earliest
            FROM base WHERE kind='snapshot' AND metric='rank' AND value IS NOT NULL),
        counts AS (SELECT object_id,COUNT(*) activity,MIN(observed_ts) first_seen FROM base GROUP BY object_id)
        SELECT c.*,r.source,r.panel_id,MIN(r.value) best_rank,
            MAX(CASE WHEN latest=1 THEN value END) last_rank,MAX(CASE WHEN earliest=1 THEN value END) first_rank,
            MAX(r.event_ts) latest_rank_at
        FROM counts c LEFT JOIN ranked r ON r.object_id=c.object_id GROUP BY c.object_id,r.source,r.panel_id"""
        with self.connect() as c:return [dict(r) for r in c.execute(sql,args)]
    def upsert_object(self,oid,name=None,kind=None,market=None,known_at=None,**extra):
        t=known_at or stamp();known=parse_time(t).timestamp()
        with self.lock,self.connect() as c:
            old=c.execute('SELECT * FROM objects WHERE id=?',(oid,)).fetchone();prior=json.loads(old['payload']) if old else {};d=dict(prior)
            old_rule=uid(d.get('aliases'),d.get('parent'),d.get('requires_context'))
            d.update(extra);d.update(id=oid,name=name or d.get('name') or oid,kind=kind or d.get('kind') or oid.split(':')[0],market=market or d.get('market','A'))
            if old and all(d.get(k)==prior.get(k) for k in set(d)|set(prior)):return
            d['rule_known_at']=t if old_rule!=uid(d.get('aliases'),d.get('parent'),d.get('requires_context')) or not old else prior.get('rule_known_at',t)
            d['known_at']=prior.get('known_at',t);d['version_known_at']=t;vid=uid(oid,t,d);d['version_id']=vid
            payload=json.dumps(d,ensure_ascii=False)
            c.execute('INSERT OR IGNORE INTO object_versions VALUES(?,?,?,?)',(vid,oid,known,payload))
            latest=c.execute('SELECT payload FROM object_versions WHERE object_id=? ORDER BY known_ts DESC,rowid DESC LIMIT 1',(oid,)).fetchone();current=json.loads(latest[0])
            c.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?)',(oid,current['name'],current['kind'],current['market'],min(known,old['known_ts']) if old else known,latest[0]))
    def objects(self,as_of=None):
        with self.connect() as c:
            if as_of is None:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM objects ORDER BY id')]
            sql='SELECT payload FROM (SELECT payload,ROW_NUMBER() OVER(PARTITION BY object_id ORDER BY known_ts DESC,rowid DESC) rn FROM object_versions WHERE known_ts<=?) WHERE rn=1'
            return [json.loads(r[0]) for r in c.execute(sql,(parse_time(as_of).timestamp(),))]
    def membership(self,topic_id,symbol,relation='narrative',effective_from=None,effective_to=None,known_at=None,**extra):
        if relation not in ('direct_business','industry_transmission','narrative'):raise ValueError('关联等级错误')
        t=known_at or stamp();edge=extra.pop('edge_id',None) or uid(topic_id,symbol,relation,extra.get('source','manual'))
        with self.lock,self.connect() as c:
            old=c.execute('SELECT payload FROM member_versions WHERE edge_id=? ORDER BY known_ts DESC,rowid DESC LIMIT 1',(edge,)).fetchone();prior=json.loads(old[0]) if old else {}
            d={**prior,**extra,'topic_id':topic_id,'symbol':symbol,'relation':relation,'edge_id':edge,'effective_from':effective_from or prior.get('effective_from') or t[:10],'effective_to':effective_to,'known_at':t}
            if prior and all(d.get(k)==prior.get(k) for k in (set(d)|set(prior))-{'known_at','version_id'}):return prior
            vid=uid(edge,t,d);d['version_id']=vid
            c.execute('INSERT INTO member_versions VALUES(?,?,?,?,?,?)',(vid,edge,topic_id,symbol,parse_time(t).timestamp(),json.dumps(d,ensure_ascii=False)))
        return d
    def all_members(self,at):
        ts=parse_time(at).timestamp();day=parse_time(at).date().isoformat()
        with self.connect() as c:
            rows=c.execute('SELECT payload FROM (SELECT payload,ROW_NUMBER() OVER(PARTITION BY edge_id ORDER BY known_ts DESC,rowid DESC) rn FROM member_versions WHERE known_ts<=?) WHERE rn=1',(ts,)).fetchall()
        result=[]
        for row in rows:
            d=json.loads(row[0]);start=d['effective_from'];end=d.get('effective_to')
            valid_start=parse_time(start).timestamp()<=ts if 'T' in start else start<=day
            valid_end=(parse_time(end).timestamp()>ts if 'T' in end else end>day) if end else True
            if valid_start and valid_end and not d.get('revoked'):result.append(d)
        return result
    def members(self,topic_id,at):return [d for d in self.all_members(at) if d['topic_id']==topic_id]
    def set_meta(self,key,value):
        with self.lock,self.connect() as c:c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    def get_meta(self,key,default=None):
        with self.connect() as c:r=c.execute('SELECT payload FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default
    def set_health(self,source,payload):
        with self.lock,self.connect() as c:c.execute('INSERT OR REPLACE INTO health VALUES(?,?)',(source,json.dumps(payload,ensure_ascii=False)))
    def health(self):
        with self.connect() as c:return {x[0]:json.loads(x[1]) for x in c.execute('SELECT * FROM health')}
    def fetch_candidates(self,source,pool,limit,at=None):
        clock=parse_time(at or stamp()).timestamp()
        with self.lock,self.connect() as c:
            c.executemany('INSERT OR IGNORE INTO fetch_tasks(source,object_id) VALUES(?,?)',[(source,x) for x in pool])
            state={r['object_id']:dict(r) for r in c.execute('SELECT * FROM fetch_tasks WHERE source=?',(source,))}
        position={x:i for i,x in enumerate(pool)}
        return sorted((x for x in pool if state[x]['next_due']<=clock),key=lambda x:(state[x]['last_success'],position[x]))[:limit]
    def finish_fetch(self,source,oid,success,ttl=3600,message=''):
        clock=parse_time(stamp()).timestamp()
        with self.lock,self.connect() as c:
            c.execute('INSERT OR IGNORE INTO fetch_tasks(source,object_id) VALUES(?,?)',(source,oid))
            if success:c.execute('UPDATE fetch_tasks SET last_success=?,next_due=?,failures=0,message=? WHERE source=? AND object_id=?',(clock,clock+ttl,'',source,oid))
            else:c.execute('UPDATE fetch_tasks SET next_due=?,failures=failures+1,message=? WHERE source=? AND object_id=?',(clock+300,message[:500],source,oid))
    def save_run(self,run):
        with self.lock,self.connect() as c:
            c.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(run['run_id'],run['phase'],parse_time(run['as_of']).timestamp(),stamp(),json.dumps(run,ensure_ascii=False,allow_nan=False)))
            c.execute('INSERT INTO run_contexts VALUES(?,?,?)',(run['run_id'],parse_time(run['as_of']).timestamp(),json.dumps(self._context(run),ensure_ascii=False)))
    @staticmethod
    def _context(run):
        keys=('run_id','as_of','target_session','phase','settings_version','evaluation_type')
        fields=('id','kind','stage','band','episode_id','first_detected_at','last_evidence_at','primary','member_heat_proxy','member_pools','selection_group','evidence','signals','features','attention_onset_at','response_onset_at','response_state','ranks','score','attention_observed')
        return {**{k:run.get(k) for k in keys},'items':[{k:x.get(k) for k in fields} for x in run['items']]}
    def run_contexts(self,before,limit=2000):
        with self.lock,self.connect() as c:
            missing=c.execute('SELECT id,asof_ts,payload FROM runs WHERE id NOT IN (SELECT id FROM run_contexts) ORDER BY asof_ts DESC LIMIT 120').fetchall()
            for row in missing:c.execute('INSERT OR IGNORE INTO run_contexts VALUES(?,?,?)',(row['id'],row['asof_ts'],json.dumps(self._context(json.loads(row['payload'])),ensure_ascii=False)))
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM run_contexts WHERE asof_ts<=? ORDER BY asof_ts DESC,rowid DESC LIMIT ?',(parse_time(before).timestamp(),limit))]
    def historical_stocks(self):
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT json_extract(j.value,'$.id') FROM runs r,json_each(r.payload,'$.items') j WHERE json_extract(j.value,'$.kind')='stock'")]

    def run(self,run_id):
        with self.connect() as c:r=c.execute('SELECT payload FROM runs WHERE id=?',(run_id,)).fetchone()
        return json.loads(r[0]) if r else None
    def runs(self,limit=50,before=None):
        with self.connect() as c:
            sql='SELECT payload FROM runs';args=[]
            if before:sql+=' WHERE asof_ts<=?';args.append(parse_time(before).timestamp())
            sql+=' ORDER BY asof_ts DESC,rowid DESC LIMIT ?';args.append(limit)
            return [json.loads(r[0]) for r in c.execute(sql,args)]
    def save_evaluation(self,payload):
        eid=uid(payload);at=stamp()
        with self.lock,self.connect() as c:c.execute('INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?)',(eid,payload.get('run_id'),at,json.dumps(payload,ensure_ascii=False,allow_nan=False)))
        return eid
    def save_feedback(self,run_id,object_id,decision,comment=''):
        if decision not in ('keep','reject','watch'):raise ValueError('选择保留、移除或继续观察')
        run=self.run(run_id)
        if not run or not any(x['id']==object_id for x in run['items']):raise ValueError('候选不存在')
        d=dict(run_id=run_id,object_id=object_id,decision=decision,comment=comment[:1000],at=stamp());fid=uid(run_id,object_id)
        with self.lock,self.connect() as c:c.execute('INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?)',(fid,run_id,object_id,d['at'],json.dumps(d,ensure_ascii=False)))
        return d
    def feedback(self,run_id):
        with self.connect() as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM feedback WHERE run_id=?',(run_id,))]
    def stats(self):
        with self.connect() as c:
            counts=dict(c.execute('SELECT kind,COUNT(*) FROM records GROUP BY kind').fetchall());bounds=c.execute('SELECT MIN(event_ts),MAX(event_ts) FROM records').fetchone()
        return dict(counts=counts,total=sum(counts.values()),first_ts=bounds[0],last_ts=bounds[1],db_bytes=self.path.stat().st_size)
    def backup(self,dest):
        with self.lock,self.connect() as src,closing(sqlite3.connect(dest)) as dst:src.backup(dst)
