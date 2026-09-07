#!/usr/bin/env python3
import argparse,logging,os,socket,sys,threading,time,urllib.request,json,webbrowser
from pathlib import Path

def main():
    if sys.version_info<(3,11):raise SystemExit('需要 Python 3.11 或更新版本；Windows请使用一键安装入口。')
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8787);parser.add_argument('--no-browser',action='store_true');parser.add_argument('--no-auto',action='store_true');parser.add_argument('--data-dir')
    args=parser.parse_args();root=Path(__file__).resolve().parent;data=Path(args.data_dir or os.getenv('RADAR_DATA_DIR',str(root/'data')))
    (data/'logs').mkdir(parents=True,exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s',handlers=[logging.StreamHandler(),RotatingFileHandler(data/'logs/app.log',maxBytes=5_000_000,backupCount=3,encoding='utf-8')])
    port=None
    for candidate in range(args.port,args.port+20):
        try:
            with socket.socket() as s:s.bind(('127.0.0.1',candidate))
            port=candidate;break
        except OSError:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{candidate}/api/health',timeout=1) as r:health=json.load(r)
                if health.get('application')=='AshareHeatRadar':
                    if not args.no_browser:webbrowser.open(f'http://127.0.0.1:{candidate}')
                    print('热度雷达已在运行。');return
            except Exception:pass
    if port is None:raise SystemExit('本地端口不可用，请用 --port 指定其他端口')
    from radar.webapp import create_app
    import uvicorn
    app=create_app(data,auto=not args.no_auto)
    url=f'http://127.0.0.1:{port}'
    def open_ready():
        for _ in range(60):
            try:
                with urllib.request.urlopen(url+'/api/health',timeout=1):pass
                webbrowser.open(url);return
            except Exception:time.sleep(.5)
    if not args.no_browser:threading.Thread(target=open_ready,daemon=True).start()
    print('\nA股热度雷达  '+url+'\n关闭此窗口或按 Ctrl+C 停止监测；数据保存在 '+str(data)+'\n')
    uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning',access_log=False)
if __name__=='__main__':main()
