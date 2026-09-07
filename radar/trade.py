"""Trading is an independent observation dimension, never a heat eligibility gate."""
from collections import defaultdict
from statistics import mean
from datetime import timedelta
from .common import parse_time,CN,ratio,uid

def trading_state(db,obj,at,known_at,quote_cache=None,memberships=None,min_coverage=.6):
    at=parse_time(at);known_at=parse_time(known_at)
    membership=(memberships if memberships is not None else db.members(obj['id'],at)) if obj['kind']!='stock' else []
    business=[m for m in membership if m['relation']!='narrative' and m.get('direction') not in ('cost_negative','competition_negative')]
    platform=[m for m in membership if m['relation']=='narrative']
    membership=platform or business
    symbols=sorted({m['symbol'] for m in membership}) if membership else ([obj['id']] if obj['kind']=='stock' else [])
    result=dict(available=False,member_count=len(symbols),member_coverage=0,pool_version=uid(symbols),amount_cny=None,
        amount_share=None,equal_weight_return=None,breadth=None,ex_leader_return=None,turnover=None,
        source=None,note='没有同一来源、同一有效时点的可比交易面板；不以板块涨幅代替讨论热度。',members=membership)
    if not symbols:return result
    records=quote_cache if quote_cache is not None else db.read(kind='quote',start=at.replace(hour=0,minute=0,second=0,microsecond=0),end=at+timedelta(seconds=1),as_of=known_at)
    groups=defaultdict(dict)
    for r in records:
        if r['window_kind']!='regular_cumulative':continue
        ex=r['extra'];dt=parse_time(r['occurred_at']).astimezone(CN)
        if dt.date()!=at.astimezone(CN).date() or dt>at or parse_time(r['observed_at'])>known_at or (at-dt).total_seconds()>900:continue
        # Provider snapshot time, not fictitious exchange timestamp; compare only same 5-minute bucket.
        key=(r['source'],dt.strftime('%H:')+f'{dt.minute//5*5:02d}')
        for sid in r['objects']:groups[key][sid]=r
    candidates=[]
    for key,rs in groups.items():
        sample=[rs[s] for s in symbols if s in rs]
        if sample:candidates.append((len(sample),key,sample,rs.get('market:A')))
    if not candidates:return result
    _,key,sample,market=max(candidates,key=lambda x:(x[0],x[1][1]))
    vals=[r['extra'].get('return_decimal') for r in sample];vals=[v for v in vals if v is not None]
    amts=[r['extra'].get('amount_cny') for r in sample];complete_amount=all(v is not None for v in amts)
    amount=sum(amts) if complete_amount else None
    total=market.get('value') if market and market['extra'].get('complete') else None
    # Some normalized vendors carry their market amount in extra as well.
    if total is None and market and market['extra'].get('complete'):total=market['extra'].get('amount_cny')
    exleader=sorted(vals)[:-1]
    coverage=len(sample)/len(symbols);rankable=len(symbols)==1 or (coverage>=min_coverage and len(sample)>=3)
    benchmark=market.get('extra',{}).get('equal_weight_return') if market else None
    response=mean(vals)-benchmark if vals and benchmark is not None else None
    result.update(available=True,coverage=coverage,rankable=rankable,benchmark_return=benchmark,relative_return=response if rankable else None,relative_breadth=ratio(sum(v>benchmark for v in vals),len(vals)) if benchmark is not None and rankable else None,member_coverage=len(sample),amount_cny=amount,amount_share=ratio(amount,total),
        equal_weight_return=mean(vals) if vals else None,breadth=ratio(sum(v>0 for v in vals),len(vals)),
        ex_leader_return=mean(exleader) if exleader else None,turnover=sample[0]['extra'].get('turnover_pct') if len(symbols)==1 else None,
        float_cap_cny=sample[0]['extra'].get('float_cap_cny') if len(symbols)==1 else None,source=key[0],cutoff_key=key[1],note='成员样本' if not rankable else '成员池行情')
    return result
