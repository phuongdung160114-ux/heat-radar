#!/usr/bin/env python3
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from radar.engine import Engine
from radar.labels import update_labels,label_status
from radar.learning import run_experiment

def main():
    parser=argparse.ArgumentParser(description='HEAT RADAR 2.0 研究命令')
    parser.add_argument('command',choices=['analyze','collect','labels','experiment','status','export'])
    parser.add_argument('--data-dir',default=str(ROOT/'data'))
    parser.add_argument('--as-of');parser.add_argument('--config',help='实验配置JSON文件')
    parser.add_argument('--target',default='attention_1d')
    parser.add_argument('--algorithm',choices=['logistic','ridge','lambdamart'],default='logistic')
    parser.add_argument('--output')
    a=parser.parse_args();engine=Engine(a.data_dir,'live')
    if a.command=='analyze':result=engine.analyze(a.as_of)
    elif a.command=='collect':result=engine.collect(force=True)
    elif a.command=='labels':result=update_labels(engine,a.as_of)
    elif a.command=='experiment':
        config=json.loads(Path(a.config).read_text(encoding='utf-8-sig')) if a.config else {'target':a.target,'algorithm':a.algorithm}
        result=run_experiment(engine,config,a.as_of)
    elif a.command=='export':
        from radar.features import load_panel
        result=load_panel(engine.db,a.as_of)
    else:result=label_status(engine)
    text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if a.output:
        out=Path(a.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(text,encoding='utf-8');print(out)
    else:print(text)
if __name__=='__main__':main()
