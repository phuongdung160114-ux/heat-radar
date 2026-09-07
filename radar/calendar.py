from __future__ import annotations
from datetime import datetime,timedelta
from .common import ROOT,CN,NY,read_json,parse_time,stamp
from .vendor.session_tools import plan_windows

class MarketCalendar:
    def __init__(self,path=None):
        self.data=read_json(path or ROOT/'config/calendar.json')
        if not self.data or not self.data.get('sessions'):raise ValueError('缺少真实交易日历')
        self.by_key={(s['market'],s['date']):s for s in self.data['sessions']}
    def check_date(self,d):
        if not (self.data['valid_from']<=d<=self.data['valid_to']):
            raise ValueError('交易日历覆盖不足：'+d+'。先更新 config/calendar.json；禁止以工作日冒充交易日。')
    def open_dates(self,market='A',before=None,limit=260):
        days=sorted(s['date'] for s in self.data['sessions'] if s['market']==market and s.get('is_open',True))
        if before:days=[d for d in days if d<before]
        return days[-limit:]
    def is_open(self,d,market='A'):
        self.check_date(d)
        return bool(self.by_key.get((market,d),{}).get('is_open',False))
    def phase(self,at):
        d=parse_time(at).astimezone(CN);self.check_date(d.date().isoformat())
        if not self.is_open(d.date().isoformat()):return 'premarket'
        clock=d.strftime('%H:%M')
        if clock<'09:30':return 'premarket'
        if clock<'15:00':return 'intraday'
        return 'postmarket'
    def plan(self,at,phase='auto'):
        dt=parse_time(at).astimezone(CN);self.check_date(dt.date().isoformat())
        phase=self.phase(at) if phase=='auto' else phase
        return plan_windows(self.data,phase,stamp(at),history_limit=260)
    def spans(self,date,market='A'):
        s=self.by_key.get((market,date))
        if not s or not s.get('is_open',True):return []
        zone=CN if market=='A' else NY
        return [(datetime.fromisoformat(date+'T'+a).replace(tzinfo=zone),
                 datetime.fromisoformat(date+'T'+b).replace(tzinfo=zone)) for a,b in s['regular']]
