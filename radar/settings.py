from pathlib import Path
import os,copy,math
from .common import ROOT,read_json,atomic_json
from .providers import REGISTRY


def validate_parameter_group(group, values):
    boolean_fields={
        'discovery':('enabled','auto_promote'),
        'research':('auto_update_labels','auto_train'),
        'features':(),
    }
    integer_bounds={
        'discovery':{'lookback_days':(1,None),'recent_hours':(1,None),'min_documents':(1,None),
            'min_families':(1,None),'min_stocks':(0,None),'limit':(1,None)},
        'research':{'min_train_days':(2,None),'validation_days':(2,None),'test_days':(2,None),
            'embargo_days':(0,60),'step_days':(2,None),'top_k':(2,None),
            'bootstrap_samples':(50,5000),'label_refresh_seconds':(1,None)},
        'features':{'min_cross_section':(1,None)},
    }
    positive_fields={'discovery':('min_burst',),'research':(),'features':('share_smoothing',)}
    for name in boolean_fields[group]:
        if type(values[name]) is not bool:raise ValueError(group+'.'+name+'必须是布尔值')
    for name,(minimum,maximum) in integer_bounds[group].items():
        value=values[name]
        if type(value) is not int or value<minimum or (maximum is not None and value>maximum):
            bound=f'{minimum}至{maximum}' if maximum is not None else f'不小于{minimum}'
            raise ValueError(group+'.'+name+'必须是'+bound+'的整数')
    for name in positive_fields[group]:
        value=values[name]
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError(group+'.'+name+'必须是大于0的有限数值')
    if group=='research' and values['sampling'] not in ('daily','episode'):
        raise ValueError('research.sampling只能为daily或episode')


class Settings:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'settings.json';self.defaults=read_json(ROOT/'config/settings.default.json')
        self.data=copy.deepcopy(self.defaults)
        saved=read_json(self.path,{})
        for k,v in saved.items():
            if isinstance(v,dict) and isinstance(self.data.get(k),dict):self.data[k].update(v)
            elif k in self.data:self.data[k]=v
    def public(self):
        d=copy.deepcopy(self.data);d['credentials']={k:bool(v or os.getenv({'tushare_token':'TUSHARE_TOKEN','x_bearer':'X_BEARER_TOKEN','reddit_bearer':'REDDIT_BEARER_TOKEN'}.get(k,''))) for k,v in d['credentials'].items()}
        return d
    def update(self,patch):
        allowed={'auto_collect','sources','credentials','selection','watchlist','x_queries','reddit_subreddits','rss_feeds','licensed_url','licensed_allowed_hosts','user_agent','discovery','research','features','local_feed_dir','deep_scan_per_run','minute_scan_per_run','fulltext_per_run','max_candidates','analysis_history_days'}
        if set(patch)-allowed:raise ValueError('不允许修改的配置项')
        d=copy.deepcopy(self.data)
        for k,v in patch.items():
            if k=='sources':
                if not isinstance(v,dict) or set(v)-set(REGISTRY) or any(type(x) is not bool for x in v.values()):raise ValueError('来源配置格式错误')
                d[k].update(v)
            elif k=='credentials':
                if not isinstance(v,dict) or set(v)-set(d[k]) or any(not isinstance(x,str) or len(x)>5000 for x in v.values()):raise ValueError('凭据格式错误')
                d[k].update(v)
            elif k=='selection':
                if not isinstance(v,dict) or set(v)-set(d[k]):raise ValueError('未知筛选参数')
                for name,val in v.items():
                    if name=='exclude_st':
                        if type(val) is not bool:raise ValueError('exclude_st必须是布尔值')
                    elif type(val) not in (int,float) or not math.isfinite(val) or val<0 or val>1000:raise ValueError('筛选阈值超出范围')
                merged={**d[k],**v}
                if not (1<=merged['min_confirming_families']<=10 and 1<=merged['min_history5']<=5 and 1<=merged['min_history20']<=20):raise ValueError('历史/来源数阈值超出范围')
                for count_key in ('min_absolute_new','min_history20','min_history5','min_confirming_families','min_persistence_windows','rank_improvement','min_negative_items','top_rank','new_entry_rank','new_min_windows','new_min_events','min_member_count','min_warming_members'):
                    if type(merged[count_key]) is not int:raise ValueError('数量阈值必须为整数')
                if not 0<=merged['negative_ratio']<=1:raise ValueError('负面比例必须在0到1之间')
                if merged['min_persistence_windows'] not in (1,2):raise ValueError('连续窗口阈值只能为1或2')
                for field in ('min_member_coverage','min_member_breadth','rank_gain_fraction','response_threshold'):
                    if not 0<=merged[field]<=1:raise ValueError(field+'必须在0至1之间')
                if merged['count_prior']<=0:raise ValueError('count_prior必须大于0')
                d[k]=merged
            elif k in ('watchlist','x_queries','reddit_subreddits','licensed_allowed_hosts'):
                if not isinstance(v,list) or len(v)>100 or any(not isinstance(x,str) or len(x)>1000 for x in v):raise ValueError('列表字段格式错误')
                d[k]=v
            elif k=='rss_feeds':
                if not isinstance(v,list) or len(v)>30 or any(not isinstance(x,dict) or not str(x.get('url','')).startswith('https://') for x in v):raise ValueError('RSS需要HTTPS地址')
                d[k]=v
            elif k in ('discovery','research','features'):
                if not isinstance(v,dict) or set(v)-set(d[k]):raise ValueError('未知参数分组字段')
                merged={**d[k],**v}
                validate_parameter_group(k,merged)
                d[k]=merged
            elif k in ('deep_scan_per_run','minute_scan_per_run','fulltext_per_run'):
                if type(v) is not int or not 1<=v<=200:raise ValueError('轮转数量取1至200')
                d[k]=v
            elif k in ('max_candidates','analysis_history_days'):
                maximum=10000 if k=='max_candidates' else 730
                if type(v) is not int or not 1<=v<=maximum:raise ValueError(k+'必须是1至'+str(maximum)+'的整数')
                d[k]=v
            elif k=='auto_collect':
                if type(v) is not bool:raise ValueError('auto_collect必须为布尔值')
                d[k]=v
            else:
                if not isinstance(v,str) or len(v)>5000:raise ValueError('字段格式错误')
                d[k]=v
        atomic_json(self.path,d)
        try:os.chmod(self.path,0o600)
        except OSError:pass
        self.data=d
        return self.public()
