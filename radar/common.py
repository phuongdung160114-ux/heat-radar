from __future__ import annotations
import hashlib, html, json, math, re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CN = ZoneInfo('Asia/Shanghai')
NY = ZoneInfo('America/New_York')
ROOT = Path(__file__).resolve().parent.parent

def now(): return datetime.now(CN)
def parse_time(v):
    if isinstance(v, datetime): d=v
    else: d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('时间必须带时区偏移: '+str(v))
    return d

def stamp(v=None): return (parse_time(v) if v is not None else now()).isoformat()
def number(v):
    try:
        if v is None or isinstance(v, bool): return None
        n=float(str(v).replace(',','').rstrip('%'))
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None

def ratio(a,b): return a/b if a is not None and b is not None and b>0 else None

def uid(*parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:32]

def clean(s): return html.unescape(re.sub('<[^>]+>','',str(s or ''))).strip()
def cn_time(v, *, date_only_end=True):
    """Source date-only records become known at day-end, never fictitious pre-open facts."""
    s=str(v).strip()
    if re.fullmatch(r'\d{8}',s): s=f'{s[:4]}-{s[4:6]}-{s[6:]}'
    if len(s)==10: s += 'T23:59:59' if date_only_end else 'T00:00:00'
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    return d.replace(tzinfo=CN) if d.tzinfo is None else d

def stock_id(code):
    s=str(code).upper().strip()
    m=re.search(r'(\d{6})',s)
    if not m: raise ValueError('不是A股代码: '+s)
    n=m.group(1)
    ex='SH' if n[0]=='6' else ('BJ' if n[0] in '489' else 'SZ')
    for e in ('SH','SZ','BJ'):
        if s.startswith(e) or s.endswith('.'+e): ex=e; break
    return f'stock:{ex}{n}'

def ts_code(obj):
    s=obj.removeprefix('stock:')
    return s[2:]+'.'+s[:2]

def floor_time(d,minutes=5):
    d=parse_time(d).astimezone(CN)
    return d.replace(minute=(d.minute//minutes)*minutes,second=0,microsecond=0)

def atomic_json(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    p=path.with_suffix(path.suffix+'.tmp')
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    p.replace(path)

def read_json(path, default=None):
    p=Path(path)
    return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else default
