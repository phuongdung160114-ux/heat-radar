"""Compiled official 2025/2026 holiday notices. No extrapolation beyond validity."""
from pathlib import Path
import json
from datetime import date,timedelta
ROOT=Path(__file__).resolve().parent.parent

def date_range(a,b):
 d=date.fromisoformat(a);b=date.fromisoformat(b)
 while d<=b:yield d;d+=timedelta(days=1)

def main():
 a_holidays=set()
 for a,b in [('2025-01-01','2025-01-01'),('2025-01-28','2025-02-04'),('2025-04-04','2025-04-06'),
             ('2025-05-01','2025-05-05'),('2025-05-31','2025-06-02'),('2025-10-01','2025-10-08'),
             ('2026-01-01','2026-01-03'),('2026-02-15','2026-02-23'),('2026-04-04','2026-04-06'),
             ('2026-05-01','2026-05-05'),('2026-06-19','2026-06-21'),('2026-09-25','2026-09-27'),('2026-10-01','2026-10-07')]:
  a_holidays.update(x.isoformat() for x in date_range(a,b))
 us_holidays=set(('2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25 '
                  '2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25').split())
 early=set('2025-07-03 2025-11-28 2025-12-24 2026-11-27 2026-12-24'.split())
 sessions=[]
 for d in date_range('2025-01-01','2026-12-31'):
  ds=d.isoformat()
  for market,holidays,tz in [('A',a_holidays,'Asia/Shanghai'),('US',us_holidays,'America/New_York')]:
   opened=d.weekday()<5 and ds not in holidays
   reg=([['09:30','11:30'],['13:00','15:00']] if market=='A' else [['09:30','13:00' if ds in early else '16:00']]) if opened else []
   # US extended sessions vary by venue, deliberately not assumed here.
   stages=([{'name':'opening_auction','start':'09:15','end':'09:25'}] if market=='A' and opened else [])
   sessions.append(dict(market=market,date=ds,timezone=tz,is_open=opened,regular=reg,stages=stages))
 sources=[
  'https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml',
  'https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml',
  'https://www.nyse.com/markets/hours-calendars',
  'https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx',
  'https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx']
 out=dict(calendar_version='official-notices-2025-2026-compiled-20260906',valid_from='2025-01-01',valid_to='2026-12-31',sources=sources,sessions=sessions)
 (ROOT/'config/calendar.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
