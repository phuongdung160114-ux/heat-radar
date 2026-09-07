"""Auditable dictionary/regex classification. No generative calls or hidden business inference."""
from __future__ import annotations
import re
from collections import Counter,defaultdict
from .common import clean,uid,parse_time
from .events import extract_events

NEGATIVE=re.compile(r'立案调查|财务造假|退市|违约|暴雷|爆雷|减值|业绩下修|订单取消|下调.{0,8}(指引|预测)|亏损扩大|破产|bankrupt|fraud|guidance cut',re.I)
DENIAL=re.compile(r'否认|辟谣|不属实|并无|未发生|澄清|denies|not true',re.I)
RUMOR=re.compile(r'传闻|网传|据传|小作文|未经证实|rumou?r|unconfirmed',re.I)
QUESTION=re.compile(r'[?？]|是什么|为什么|求问|哪家公司|如何|how|why',re.I)
MEME=re.compile(r'电影|打卡|玩梗|名字带|名称梗')
INFERENCE=re.compile(r'预计|推算|测算|可能|假设|如果|意味着|estimate|expect|could',re.I)
FACT_ACTION=re.compile(r'公告|发布|量产|认证|中标|签订|投产|披露|launch|release|guidance|qualification',re.I)
ASCII_TERM=re.compile(r'[a-zA-Z0-9 $/\-]+')

def has_term(text,term):
    # Avoid matching AI in "said" or CPO in an unrelated longer acronym.
    if re.fullmatch(r'[a-zA-Z0-9 $/\-]+',term):
        return bool(re.search(r'(?<![A-Za-z0-9])'+re.escape(term)+r'(?![A-Za-z0-9])',text,re.I))
    return term.lower() in text.lower()

class Classifier:
    def __init__(self,objects):
        self.objects=objects; self.by_id={o['id']:o for o in objects}; self.topics=[o for o in objects if o.get('kind') in ('topic','board')]
        self.stocks=[o for o in objects if o.get('kind')=='stock']
        # Keep compiled expressions on this instance: a full stock universe is
        # much larger than re's shared cache, and events revisit the same terms.
        self._term_matchers={}
        for o in self.topics:
            for term in o.get('aliases',[o['name']]):
                if len(term)>1:self._prepare_term(term)
            for term in o.get('requires_context',[]) or []:
                self._prepare_term(term)
        for o in self.stocks:
            for term in o.get('aliases',[])+[o['name'],o['id'][-6:]]:
                if len(term)>1:self._prepare_term(term)
    def _prepare_term(self,term):
        if term not in self._term_matchers:
            self._term_matchers[term]=(re.compile(r'(?<![A-Za-z0-9])'+re.escape(term)+r'(?![A-Za-z0-9])',re.I)
                                       if ASCII_TERM.fullmatch(term) else term.lower())
        return self._term_matchers[term]
    def _has_term(self,text,lower_text,term):
        matcher=self._term_matchers.get(term)
        if matcher is None:matcher=self._prepare_term(term)
        return matcher in lower_text if isinstance(matcher,str) else bool(matcher.search(text))
    def tag(self,title,text='',page_object=None,at=None):
        text=clean(title)+' '+clean(text);tags={};reasons={}
        lower_text=text.lower()
        for o in self.topics:
            if at and o.get('known_at') and parse_time(o['known_at'])>parse_time(at):continue
            hits=[t for t in o.get('aliases',[o['name']]) if len(t)>1 and self._has_term(text,lower_text,t)]
            if not hits:continue
            ctx=o.get('requires_context',[])
            if ctx and not any(self._has_term(text,lower_text,c) for c in ctx):continue
            tags[o['id']]='explicit';reasons[o['id']]=hits
            # A specific term implies its taxonomic parent, not every business of a stock.
            parent=o.get('parent')
            if parent:tags[parent]='explicit'
        for o in self.stocks:
            terms=o.get('aliases',[])+[o['name'],o['id'][-6:]]
            if any(len(t)>1 and self._has_term(text,lower_text,t) for t in terms):tags[o['id']]='explicit'
        if page_object and page_object not in tags:tags[page_object]='context'
        return tags,reasons
    def annotate(self,r):
        d=dict(r);text=clean(d.get('title',''))+' '+clean(d.get('text',''));extra=dict(d.get('extra',{}))
        original=dict(d.get('objects',{}))
        tags,hits=self.tag(d.get('title',''),d.get('text',''),extra.get('page_object'),d.get('observed_at'))
        for k,v in original.items():
            if v=='explicit' or k not in tags:tags[k]=v
        d['objects']=tags
        versions={};valid_from={}
        for obj in tags:
            if obj in original:
                versions[obj]='provider:'+str(d.get('pool_version','v1'))
                if extra.get('attribution_valid_from'):valid_from[obj]=extra['attribution_valid_from']
            elif obj in self.by_id:
                definition=self.by_id[obj]
                versions[obj]=uid(definition.get('aliases'),definition.get('parent'),definition.get('requires_context'))
                valid_from[obj]=definition.get('rule_known_at') or definition.get('known_at') or d['observed_at']
        extra['attribution_versions']=versions;extra['attribution_valid_from_by_object']=valid_from
        events=extract_events(d.get('title',''),d.get('text',''),tags)
        single=[k for k,v in tags.items() if k.startswith('stock:') and v=='explicit']
        last_subject=[]
        for event in events:
            evidence=event['evidence'];lower_evidence=evidence.lower()
            explicit=[o['id'] for o in self.stocks if any(len(t)>1 and self._has_term(evidence,lower_evidence,t) for t in o.get('aliases',[])+[o['name'],o['id'][-6:]])]
            if explicit:last_subject=explicit
            event['subject_ids']=explicit or last_subject or (single if len(single)==1 else [])
            event['subject_state']='resolved' if event['subject_ids'] else 'review'
        neg=any(e['direction']=='negative' for e in events);denied=any(e['denied_terms'] for e in events)
        extra.update(attribution_method='dictionary-events-v2',matched_terms=hits,events=events,
                     negative_candidate=neg,direction='negative_candidate' if neg else ('denied' if denied else 'unknown'),
                     needs_review=bool(neg or denied or RUMOR.search(text)))
        extra['content_type']='meme' if MEME.search(text) else ('question' if QUESTION.search(text) else ('inference' if INFERENCE.search(text) else 'other'))
        extra['novelty_class']='rumor' if RUMOR.search(text) else ('inference' if INFERENCE.search(text) else ('source_reported_progress' if FACT_ACTION.search(text) else 'unclassified'))
        extra['support_state']=extra.get('support_state','unverified')
        norm=re.sub(r'\s+','',text.lower())
        # Exact text is one content cluster; fuzzy title clusters are candidates only.
        extra['exact_cluster']=uid(norm)
        extra['event_cluster_candidate']=uid(re.sub(r'\W','',clean(d.get('title','')).lower()))
        d['extra']=extra
        return d

def discover_terms(contents,known_terms):
    """Broad candidate discovery; never automatically declares a business relationship."""
    buckets=defaultdict(set); samples={}
    patterns=[r'[A-Z][A-Z0-9\-]{1,10}',r'[\u4e00-\u9fff]{2,8}(?:芯片|光纤|材料|封装|电池|设备|机器人|药物)']
    for r in contents:
        for p in patterns:
            for term in re.findall(p,clean(r.get('title',''))):
                if term in known_terms or term in ('IPO','ETF','ST','CNY','USD'):continue
                buckets[term].add(r['source']);samples.setdefault(term,[]).append(r.get('url') or r.get('title'))
    return [dict(term=k,source_count=len(v),evidence=samples[k][:3],status='待确认词义；不是已确认板块')
            for k,v in sorted(buckets.items(),key=lambda x:-len(x[1]))[:30]]
