"""Project-local adapters for Eastmoney rank and company-profile requests."""
from __future__ import annotations

import re
import httpx
from .common import number, stamp

RANK_URL='https://emappdata.eastmoney.com/stockrank/getAllCurrentList'
QUOTE_HOSTS=('push2.eastmoney.com','push2delay.eastmoney.com')
HEADERS={'User-Agent':'HEAT_RADAR/2.0.0','Referer':'https://quote.eastmoney.com/'}


class EastmoneyDataError(ValueError):
    pass


def _json(method,url,timeout=8,**kwargs):
    response=httpx.request(method,url,headers=HEADERS,timeout=timeout,follow_redirects=False,**kwargs)
    response.raise_for_status()
    if len(response.content)>4_000_000:raise EastmoneyDataError('东方财富响应超出大小限制')
    payload=response.json()
    if not isinstance(payload,dict):raise EastmoneyDataError('东方财富返回格式错误')
    return payload


def _quote(path,params,validate,timeout=8):
    failures=[]
    for host in QUOTE_HOSTS:
        url='https://'+host+'/api/qt/'+path
        try:
            payload=_json('GET',url,timeout=timeout,params=params)
            data=payload.get('data')
            if payload.get('rc')!=0 or not isinstance(data,dict) or not validate(data):
                raise EastmoneyDataError('东方财富返回空数据或证券字段不匹配')
            return data,{'endpoint':url,'observed_at':stamp(),
                         'delayed_host':host=='push2delay.eastmoney.com','failed_attempts':failures}
        except httpx.HTTPStatusError as exc:
            # An explicit access denial or rate limit is not a transport failure.
            if exc.response.status_code<500:raise
            failures.append({'endpoint':url,'error':'HTTP '+str(exc.response.status_code)})
        except (httpx.TransportError,ValueError) as exc:
            failures.append({'endpoint':url,'error':type(exc).__name__})
    raise EastmoneyDataError('东方财富行情请求失败：'+'; '.join(x['endpoint']+' '+x['error'] for x in failures))


def stock_hot_rank_em():
    payload=_json('POST',RANK_URL,json={'appId':'appId01','globalId':'786e4c21-70dc-435a-93bb-38',
                  'marketType':'','pageNo':1,'pageSize':100})
    ranks=payload.get('data')
    if not isinstance(ranks,list) or not ranks:raise EastmoneyDataError('东方财富人气榜为空或格式改变')
    seen=set()
    for row in ranks:
        if not isinstance(row,dict):raise EastmoneyDataError('东方财富人气榜记录格式改变')
        code=row.get('sc');rank=number(row.get('rk'))
        if not isinstance(code,str) or not re.fullmatch(r'(SH|SZ|BJ)\d{6}',code) or code in seen or rank is None or rank<1 or not rank.is_integer():
            raise EastmoneyDataError('东方财富人气榜证券代码或名次无效')
        seen.add(code)
    rank_at=stamp()
    secid=lambda code:('1.' if code.startswith('SH') else '0.')+code[2:]
    quotes={};quote_error=None
    try:
        data,transport=_quote('ulist.np/get',{'fltt':'2','invt':'2','fields':'f12,f13,f14,f124',
                              'secids':','.join(secid(r['sc']) for r in ranks)},
                              lambda d:isinstance(d.get('diff'),list) and bool(d['diff']))
        for row in data['diff']:
            if not isinstance(row,dict):continue
            if row.get('f13') in (0,1) and re.fullmatch(r'\d{6}',str(row.get('f12',''))):
                quotes[str(row['f13'])+'.'+row['f12']]=row
    except (httpx.HTTPError,ValueError) as exc:
        # A name lookup cannot invalidate ranks already obtained from the rank API.
        transport={'endpoint':None,'observed_at':stamp(),'delayed_host':None}
        quote_error=type(exc).__name__+': '+str(exc)[:400]
    result=[]
    for row in ranks:
        quote=quotes.get(secid(row['sc']),{});name=quote.get('f14')
        if not isinstance(name,str) or name in ('','-'):name=None
        meta={'rank_endpoint':RANK_URL,'rank_observed_at':rank_at,'name_lookup':transport,
              'quote_timestamp':quote.get('f124') if (number(quote.get('f124')) or 0)>0 else None,'name_available':bool(name)}
        if quote_error:meta['name_lookup_error']=quote_error
        result.append({'当前排名':int(number(row['rk'])),'代码':row['sc'],'股票名称':name,'_radar':meta})
    return result


def stock_individual_info_em(symbol='603777',timeout=None):
    if not isinstance(symbol,str) or not re.fullmatch(r'\d{6}',symbol):raise ValueError('股票代码须为6位数字')
    wait=8 if timeout is None else float(timeout)
    if not 0<wait<=30:raise ValueError('请求超时须大于0且不超过30秒')
    fields={'f57':'股票代码','f58':'股票简称','f84':'总股本','f85':'流通股','f127':'行业',
            'f116':'总市值','f117':'流通市值','f189':'上市时间','f43':'最新'}
    params={'fltt':'2','invt':'2','fields':','.join(fields)+',f124',
            'secid':('1.' if symbol.startswith('6') else '0.')+symbol}
    data,transport=_quote('stock/get',params,
        lambda d:d.get('f57')==symbol and isinstance(d.get('f58'),str) and d['f58'] not in ('','-'),timeout=wait)
    transport={**transport,'quote_timestamp':data.get('f124') if (number(data.get('f124')) or 0)>0 else None}
    result=[]
    for key,label in fields.items():
        value=data.get(key)
        if value in ('','-'):value=None
        if key in ('f84','f85','f116','f117','f43'):value=number(value)
        result.append({'item':label,'value':value,'_radar':transport})
    return result
