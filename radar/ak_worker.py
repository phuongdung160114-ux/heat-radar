"""Isolated AKShare calls. Parent enforces a hard timeout; no eval or arbitrary call names."""
from __future__ import annotations
import json,sys,math
from pathlib import Path
ALLOWED={
 'stock_hot_up_em','stock_zh_index_daily_em','stock_hot_rank_em','stock_hot_follow_xq','stock_hot_tweet_xq','stock_info_global_cls',
 'stock_info_global_em','stock_zh_a_spot_em','stock_zh_a_spot','stock_board_industry_name_em',
 'stock_board_concept_name_em','stock_board_industry_cons_em','stock_board_concept_cons_em',
 'stock_hot_keyword_em','stock_hot_rank_detail_em','stock_hot_rank_detail_realtime_em',
 'stock_research_report_em','stock_jgdy_detail_em','stock_notice_report','stock_zh_a_hist',
 'stock_zh_a_hist_min_em','tool_trade_date_hist_sina','stock_info_a_code_name',
 'stock_irm_cninfo','stock_comment_detail_scrd_focus_em','stock_zygc_em',
 'stock_individual_info_em'}

def call(name,params):
    if name not in ALLOWED:raise ValueError('Unsupported AKShare function')
    if name in ('stock_hot_rank_em','stock_individual_info_em'):
        from . import eastmoney
        return getattr(eastmoney,name)(**params)
    import akshare as ak
    df=getattr(ak,name)(**params)
    # pandas JSON handles NaN, dates, numpy scalars without making false zeros.
    return json.loads(df.to_json(orient='records',force_ascii=False,date_format='iso'))

def main():
    job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:
        result={'ok':True,'rows':call(job['function'],job.get('params',{}))}
    except Exception as exc:
        result={'ok':False,'error':type(exc).__name__+': '+str(exc)[:900]}
    Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
if __name__=='__main__':main()
