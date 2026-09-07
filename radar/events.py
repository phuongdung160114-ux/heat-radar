"""Atomic event extraction, dimensional normalization and fact-level propagation."""
from __future__ import annotations
import re
from decimal import Decimal
from difflib import SequenceMatcher
from urllib.parse import urlsplit, parse_qsl, urlunsplit, urlencode
from .common import clean, uid, parse_time

NEG = re.compile(r'立案调查|财务造假|退市|违约|暴雷|爆雷|减值|业绩下修|订单取消|下调.{0,8}(?:指引|预测)|亏损扩大|破产|bankrupt|fraud|guidance cut', re.I)
DENIAL = re.compile(r'否认|辟谣|不属实|并无|未发生|并非|不是|没有|未有|澄清|denies|not true', re.I)
ROUTINE = re.compile(r'股东大会.{0,12}(?:通知|资料|议案|决议)|召开.{0,12}股东大会|董事会.{0,6}(?:决议|会议)|会议资料|制度修订|章程修订|股东名册|法律意见书')
ACTIONS = [('order', '订单|中标|签订|签约|合同'), ('capacity', '投产|扩产|产能|量产'),
           ('qualification', '认证|验证|送样'), ('results', '业绩|净利润|营收|盈利|亏损'),
           ('product', '发布|推出|产品'), ('investigation', '立案|调查|造假'),
           ('price', '涨价|降价|价格'), ('cancel', '取消|终止'), ('impairment', '减值')]
QUANTITY = re.compile(r'(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万亿元|亿元|万元|千元|亿股|万股|万吨|千吨|亿|万|元|%|％|GW|MW|kW|吨|台|套|个|家|股)', re.I)
UNITS = {'万亿元': ('CNY', '1000000000000'), '亿元': ('CNY', '100000000'),
         '万元': ('CNY', '10000'), '千元': ('CNY', '1000'), '元': ('CNY', '1'),
         '%': ('fraction', '.01'), '％': ('fraction', '.01'),
         'gw': ('MW', '1000'), 'mw': ('MW', '1'), 'kw': ('MW', '.001'),
         '万吨': ('tonne', '10000'), '千吨': ('tonne', '1000'), '吨': ('tonne', '1'),
         '亿股': ('share', '100000000'), '万股': ('share', '10000'), '股': ('share', '1'),
         '亿': ('count', '100000000'), '万': ('count', '10000')}
IMPORTANCE = {'order': .75, 'capacity': .7, 'qualification': .55, 'results': .85,
              'product': .45, 'price': .65, 'cancel': .8, 'impairment': .8, 'investigation': .8}


def normalize_quantities(text: str) -> list[dict]:
    result = []
    for match in QUANTITY.finditer(text):
        unit = match['unit']; canonical, multiplier = UNITS.get(unit.lower(), UNITS.get(unit, (unit, '1')))
        value = Decimal(match['num'].replace(',', '')) * Decimal(multiplier)
        # Currency prefixes must remain distinct across otherwise identical numbers.
        prefix = text[max(0, match.start()-4):match.start()]
        if canonical == 'CNY' and ('美元' in prefix or '美' == prefix[-1:]): canonical = 'USD'
        normalized = format(value.normalize(), 'f')
        result.append({'raw': match.group(), 'value': float(value), 'unit': canonical,
                       'key': canonical + ':' + normalized, 'span': [match.start(), match.end()]})
    return result


def quantity_skeleton(text: str) -> str:
    def sub(m):
        q = normalize_quantities(m.group())[0]
        return '<' + q['key'] + '>'
    return re.sub(r'[\s，,。；;：:！!？?]+', '', QUANTITY.sub(sub, text).lower())


def extract_events(title, text='', objects=None):
    content = clean(title) + '。' + clean(text)
    # Commas inside numeric literals are preserved until quantity parsing.
    content = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', content)
    clauses = [x.strip() for x in re.split(r'[。；;，,\n!?！？]|但是|但|然而|不过', content) if x.strip()]
    result, seen = [], set()
    for clause in clauses:
        negative, denied = [], []
        for part in re.split(r'且确认|并确认', clause):
            for match in NEG.finditer(part):
                before, after = part[:match.start()], part[match.end():]
                local_denial = bool(DENIAL.search(before[-14:]) or re.match(r'.{0,6}(?:不属实|被否认|系谣言)', after))
                (denied if local_denial else negative).append(match.group())
        actions = [name for name, pattern in ACTIONS if re.search(pattern, clause, re.I)]
        quantities = normalize_quantities(clause)
        period = re.findall(r'(?:20\d{2}年?(?:第?[一二三四1-4]季度|上半年|下半年|度)?|20\d{2}[-/]\d{1,2}[-/]\d{1,2})', clause)
        routine = bool(ROUTINE.search(clause)); event_class = 'routine' if routine else ('economic' if actions else 'mention')
        key = uid(quantity_skeleton(clause))
        if key in seen: continue
        seen.add(key)
        result.append(dict(event_id=key, subject_ids=sorted(k for k in (objects or {}) if k.startswith('stock:')),
            actions=actions, numbers=[q['key'] for q in quantities], quantities=quantities, period=period,
            direction='negative' if negative else ('denied' if denied else 'unknown'),
            negative_terms=negative, denied_terms=denied, evidence=clause[:1200], event_class=event_class,
            importance=0.0 if routine else max((IMPORTANCE[a] for a in actions), default=.1),
            semantic_version='atomic-quantity-v3'))
    return result


def canonical_url(url):
    try:
        p = urlsplit(url)
        query = [(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith(('utm_', 'spm', 'from'))]
        return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(query), '')).rstrip('/')
    except ValueError: return ''


def event_clusters(records):
    """Cluster atomic facts; retain the record carrying each propagation observation."""
    groups = []
    for record in sorted(records, key=lambda r: (parse_time(r['observed_at']), parse_time(r['occurred_at']), r['record_id'])):
        title = clean(record.get('title') or record.get('text', '')[:160]); extra = record.get('extra', {})
        events = extra.get('events') or extract_events(title, record.get('text', ''), record.get('objects'))
        day = record['occurred_at'][:10]
        url = canonical_url(extra.get('original_url') or record.get('url', ''))
        for raw_event in events:
            event = dict(raw_event)
            if extra.get('content_role') in ('investor_question','question','meme','opinion'):
                event.update(event_class='discussion',importance=0.0)
            # Imported v1 records are normalized at the computational boundary.
            qs = normalize_quantities(event.get('evidence', ''))
            event['numbers'] = [q['key'] for q in qs] if qs else event.get('numbers', [])
            event.setdefault('quantities', qs); event.setdefault('event_class', 'economic' if event.get('actions') else 'mention')
            event.setdefault('importance', max((IMPORTANCE.get(a, .1) for a in event.get('actions', [])), default=.1))
            if ROUTINE.search(event.get('evidence', '')): event.update(event_class='routine', importance=0.0)
            subjects = tuple(sorted(event.get('subject_ids', [])))
            actions = tuple(sorted(event.get('actions', [])))
            numbers = tuple(sorted(set(event.get('numbers', []))))
            period = tuple(event.get('period', [])); direction = event.get('direction', 'unknown')
            norm = quantity_skeleton(event.get('evidence', title)); exact = uid(norm, subjects, direction)
            match = None
            for group in reversed(groups[-2000:]):
                if event['event_class']!=group['event_class'] or subjects != group['subjects'] or actions != group['actions'] or numbers != group['numbers'] or period != group['period'] or direction != group['direction']: continue
                same_link = bool(url and url in group['urls'])
                if exact in group['exact_ids'] or same_link or (day == group['day'] and actions and SequenceMatcher(None, norm, group['norm']).ratio() >= .72):
                    match = group; break
            if match is None:
                match = dict(event_id=uid('fact-v3', day, subjects, norm, numbers, period, direction),
                    title=event.get('evidence', title), norm=norm, day=day, subjects=subjects,
                    actions=actions, numbers=numbers, period=period, direction=direction,
                    event_class=event['event_class'], importance=event['importance'],
                    exact_ids=set(), urls=set(), records=[], events=[])
                groups.append(match)
            match['exact_ids'].add(exact)
            if url: match['urls'].add(url)
            if not any(r['record_id'] == record['record_id'] for r in match['records']): match['records'].append(record)
            if not any(e.get('event_id') == event.get('event_id') for e in match['events']): match['events'].append(event)
    # Each fact update is attached to its preceding compatible fact, independently of reprints.
    history = {}
    for group in groups:
        key = (group['subjects'], group['actions'], group['period'])
        prior = history.get(key)
        group['previous_event_id'] = prior['event_id'] if prior and group['numbers'] != prior['numbers'] else None
        group['fact_update'] = bool(group['previous_event_id'] and group['numbers'] and prior['numbers'])
        history[key] = group
    return groups
