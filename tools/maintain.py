#!/usr/bin/env python3
"""Explicit offline maintenance. Never delete raw records without a backup."""
from pathlib import Path
import sys,argparse,sqlite3
from datetime import timedelta
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from radar.store import Store
from radar.common import now,stamp

def main():
    p=argparse.ArgumentParser(description='请先关闭运行窗口。默认只显示容量；--backup 备份；--prune-quote-days N 备份后删除旧行情采样，保留参与/热度/日线和冻结结果。')
    p.add_argument('--data-dir',default=str(ROOT/'data'));p.add_argument('--backup',action='store_true');p.add_argument('--prune-quote-days',type=int);p.add_argument('--vacuum',action='store_true');a=p.parse_args()
    root=Path(a.data_dir);dbpath=root/'live/radar.sqlite'
    if not dbpath.exists():raise SystemExit('真实数据库尚未建立。')
    db=Store(dbpath);print('操作前：',db.stats())
    if a.prune_quote_days is not None and a.prune_quote_days<120:raise SystemExit('为保留同刻对比余量，清理阈值至少120自然日；不要清理用于研究的历史原始资料。')
    if a.backup or a.prune_quote_days is not None:
        dest=root/'backups'/('before-maintenance-'+now().strftime('%Y%m%d-%H%M%S')+'.sqlite');dest.parent.mkdir(parents=True,exist_ok=True);db.backup(dest);print('已备份：',dest)
    if a.prune_quote_days is not None:
        threshold=(now()-timedelta(days=a.prune_quote_days)).timestamp()
        with db.connect() as c:
            c.execute('CREATE TEMP TABLE remove_ids AS SELECT id FROM records WHERE kind=? AND event_ts<? AND metric IN (?,?)',('quote',threshold,'quote','market_amount'))
            count=c.execute('SELECT COUNT(*) FROM remove_ids').fetchone()[0]
            c.execute('DELETE FROM object_records WHERE record_id IN (SELECT id FROM remove_ids)');c.execute('DELETE FROM records WHERE id IN (SELECT id FROM remove_ids)')
        print('删除旧行情采样：',count,'条。没有删除日线、热度、参与记录或冻结运行。')
    if a.vacuum:
        with db.connect() as c:c.execute('VACUUM')
    print('操作后：',db.stats())
if __name__=='__main__':main()
