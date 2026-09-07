"""Open-vocabulary phrase bursts, semantic aliases and provisional topic memberships."""
from __future__ import annotations
from collections import defaultdict, Counter
from datetime import timedelta
from itertools import combinations
import json, math, re, unicodedata
from .common import clean, parse_time, stamp, uid

STOP = set('公司 股份 有限 有限公司 股份有限公司 公告 今日 昨日 近日 投资 投资者 市场 股票 证券 股东 董事 会议 本次 相关 关于 方面 进行 持续 积极 实现 表示 目前 未来 发展 业务 产品 我们 企业 已经 通过 以及 以上 以下 是否 感谢 问题 回答 年度 报告 可以 可能 预计 据悉 记者 消息 财联社 东方财富 同花顺 新闻 涨停 下跌 上涨'.split())
STOP.update('合成 演示 场景 固定 示例 文本 本条 对应 不对应 真实 实际 同时 共同 进入 阶段 信息 说明 记录 数据 来源 文章 转发 评论 本文 此次 最近 有关 进一步 其中 因此 此前 正在 此后 开始 取得 达到 继续 完成 开展 相关 新的 关于 不是 以及 和 与 的 在 了 为 从 至 到 将 已 对 向 由 或 等 其 这 本'.split())
ALIASES={'cpo':'CPO','共封装光学':'CPO','co-packaged optics':'CPO','hbm':'HBM','高带宽存储':'HBM',
         '液冷散热':'液冷','液冷技术':'液冷','人形機器人':'人形机器人','固態電池':'固态电池'}


def canonical(term):
    t=unicodedata.normalize('NFKC',term).strip()
    return ALIASES.get(t.lower(),t.upper() if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9\-]{1,14}',t) else t)


def phrase_tokens(text):
    text=unicodedata.normalize('NFKC',clean(text))
    tokens=set()
    for word in re.findall(r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{1,14}(?![A-Za-z0-9])',text):
        if word.upper() not in ('IPO','ETF','CNY','USD','HTTP','HTTPS','HTML','PDF','ST','SH','SZ'):tokens.add(canonical(word))
    # All Chinese character n-grams participate, without requiring a fixed industry suffix.
    segmented=text
    for stop in sorted(STOP,key=lambda s:-len(s)):
        segmented=segmented.replace(stop,' ')
    for span in re.findall(r'[\u4e00-\u9fff]{2,}',segmented):
        for length in range(2,min(8,len(span))+1):
            for start in range(len(span)-length+1):
                term=span[start:start+length]
                if term in STOP or any(term.startswith(s) or term.endswith(s) for s in STOP if len(s)>=2):continue
                tokens.add(canonical(term))
    return tokens


def discover(contents,known_terms,at,config=None):
    cfg={'lookback_days':30,'recent_hours':24,'min_documents':3,'min_families':2,'min_stocks':2,'min_burst':2.,'limit':40,**(config or {})}
    at=parse_time(at);cut=at-timedelta(hours=cfg['recent_hours']);start=at-timedelta(days=cfg['lookback_days'])
    docs={}
    for r in contents:
        observed=parse_time(r['observed_at']);event=parse_time(r['occurred_at'])
        if not start<=event<=at or observed>at:continue
        # Reprints retain family reach, but are one independent text observation.
        text=clean(r.get('title',''))+' '+clean(r.get('text',''))[:600]
        exact=uid(re.sub(r'\s+','',text).lower())
        if exact not in docs:docs[exact]={'text':text,'at':event,'families':set(),'stocks':set(),'urls':set(),'id':r['record_id']}
        docs[exact]['families'].add(r.get('family',r['source']));docs[exact]['stocks'].update(s for s in r['objects'] if s.startswith('stock:'))
        if r.get('url'):docs[exact]['urls'].add(r['url'])
    current=[d for d in docs.values() if d['at']>=cut];past=[d for d in docs.values() if d['at']<cut]
    if not current:return []
    candidates=Counter();current_tokens=[]
    for d in current:
        ts=phrase_tokens(d['text']);current_tokens.append(ts);candidates.update(ts)
    known={canonical(t) for t in known_terms};candidates={t:n for t,n in candidates.items() if n>=cfg['min_documents'] and t not in known}
    # Support-equivalent substrings collapse into the longest expression.
    shortlist=sorted(candidates,key=lambda t:(-candidates[t],-len(t),t))[:8000]
    retained=[]
    for term in sorted(shortlist,key=lambda t:-len(t)):
        if any(term in longer and candidates[term]<=candidates[longer]*1.15 for longer in retained):continue
        retained.append(term)
    targets=set(retained);historical=Counter();daily=defaultdict(Counter)
    for d in past:
        ts=phrase_tokens(d['text'])&targets;historical.update(ts)
        daily[d['at'].date().isoformat()].update(ts)
    result=[]
    for term in retained:
        matching=[d for d,ts in zip(current,current_tokens) if term in ts]
        families=set().union(*(d['families'] for d in matching));stocks=set().union(*(d['stocks'] for d in matching))
        current_share=(len(matching)+.5)/(len(current)+1)
        historical_share=(historical[term]+.5)/(len(past)+1)
        expected=historical_share*len(current);burst=(len(matching)+.5)/(expected+.5)
        # Log-likelihood gain over the historical-rate Poisson model is the burst strength.
        deviance=2*(len(matching)*math.log(max(len(matching),1)/max(expected,1e-6))-(len(matching)-expected)) if len(matching)>expected else 0.
        eligible=len(families)>=cfg['min_families'] and len(stocks)>=cfg['min_stocks'] and burst>=cfg['min_burst']
        result.append({'term':term,'term_id':'term:'+uid(term)[:16],'topic_id':'topic:auto:'+uid(term)[:16],
            'document_count':len(matching),'historical_documents':historical[term],'source_count':len(families),
            'families':sorted(families),'symbols':sorted(stocks),'stock_count':len(stocks),
            'burst_ratio':burst,'burst_deviance':deviance,'document_share':current_share,'historical_share':historical_share,
            'first_at':stamp(min(d['at'] for d in matching)),'known_at':stamp(at),
            'evidence':sorted(set().union(*(d['urls'] for d in matching)))[:8],
            'record_ids':[d['id'] for d in matching][:40], 'history':[{'date':day,'documents':counter[term]} for day,counter in sorted(daily.items()) if counter[term]],
            'status':'emerging_topic' if eligible else 'phrase_observation','eligible':eligible,
            'measurement':'information_phrase_burst','aliases':[term]})
    result.sort(key=lambda t:(not t['eligible'],-t['burst_deviance'],-t['source_count'],-t['document_count'],t['term']))
    return result[:cfg['limit']]


def update_topics(engine,at,save=True):
    cfg=engine.settings.data.get('discovery',{})
    if not cfg.get('enabled',True):return []
    contents=engine.db.read(kind='content',start=at-timedelta(days=cfg.get('lookback_days',30)),end=at+timedelta(microseconds=1),as_of=at)
    objects=engine.db.objects(as_of=at)
    terms=discover(contents,{a for o in objects if not o.get('auto_discovered') for a in o.get('aliases',[o['name']])},at,
        {k:v for k,v in cfg.items() if k in ('lookback_days','recent_hours','min_documents','min_families','min_stocks','min_burst','limit')})
    if not save:return terms
    with engine.db.lock,engine.db.connect() as c:
        c.executemany('INSERT OR REPLACE INTO term_snapshots VALUES(?,?,?)',[(at.timestamp(),t['term_id'],json.dumps(t,ensure_ascii=False)) for t in terms])
    object_map={o['id']:o for o in objects}
    if cfg.get('auto_promote',True):
        for term in terms:
            if not term['eligible']:continue
            engine.db.upsert_object(term['topic_id'],term['term'],'topic',known_at=stamp(at),aliases=term['aliases'],
                auto_discovered=True,provisional=True,discovery_method='open_ngram_burst_v2',first_discovered_at=object_map.get(term['topic_id'],{}).get('first_discovered_at',stamp(at)))
            for symbol in term['symbols']:
                engine.db.membership(term['topic_id'],symbol,relation='narrative',source='open_topic_cooccurrence',known_at=stamp(at),
                    direction='sentiment',description=term['term']+' 同时提及',evidence_url=term['evidence'][0] if term['evidence'] else '',
                    discovery_evidence_ids=term['record_ids'],document_count=term['document_count'])
    return terms
