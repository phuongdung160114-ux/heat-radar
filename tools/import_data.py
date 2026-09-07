#!/usr/bin/env python3
from pathlib import Path
import argparse,csv,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from radar.engine import Engine
from radar.data_sources import import_rows

def main():
    p=argparse.ArgumentParser(description='导入CSV、JSON或JSONL原始记录')
    p.add_argument('input');p.add_argument('--data-dir',default=str(ROOT/'data'));p.add_argument('--analyze',action='store_true')
    a=p.parse_args();f=Path(a.input)
    with f.open(encoding='utf-8-sig',newline='') as stream:
        if f.suffix.lower()=='.csv':rows=list(csv.DictReader(stream))
        elif f.suffix.lower()=='.jsonl':rows=[json.loads(line) for line in stream if line.strip()]
        else:rows=json.load(stream)
    if not isinstance(rows,list):raise ValueError('文件顶层应为记录数组')
    records=import_rows(rows);engine=Engine(a.data_dir,'live')
    existing={o['id'] for o in engine.db.objects()}
    for record in records:
        for oid in record['objects']:
            if oid.startswith(('stock:','topic:','board:')) and oid not in existing:
                engine.db.upsert_object(oid,record.get('extra',{}).get('object_name',oid),oid.split(':')[0],known_at=record['observed_at']);existing.add(oid)
    added=engine.db.append(records);print(json.dumps({'input_rows':len(records),'inserted':added},ensure_ascii=False))
    if a.analyze:print(engine.analyze()['run_id'])
if __name__=='__main__':main()
