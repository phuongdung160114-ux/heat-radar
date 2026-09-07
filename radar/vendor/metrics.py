#!/usr/bin/env python3
"""Deterministic aggregations for already collected and labelled observations."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

COHORTS = {'continuous', 'returning', 'new_in_sample', 'unknown_history'}


def number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('Boolean is not a numeric observation')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Numeric observations must be finite')
    return result


def ratio(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def percentile(value, history):
    data = [number(x) for x in history if x is not None]
    if value is None or not data:
        return None
    return (sum(x < value for x in data) + 0.5 * sum(x == value for x in data)) / len(data)


def read_jsonl(path):
    if path is None:
        return []
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f'{path}, line {n}: {exc}') from exc
    return rows


def participation_metrics(records, coverage=None, parents=None, timezone='Asia/Shanghai', slot_minutes=30):
    if slot_minutes <= 0:
        raise ValueError('slot_minutes must be positive')
    zone = ZoneInfo(timezone)
    groups = defaultdict(list)
    seen = {}
    for r in records:
        for required in ['record_id', 'platform', 'panel_id', 'entity_type', 'entity_id', 'occurred_at', 'objects']:
            if required not in r:
                raise ValueError(f'Missing participation field: {required}')
        # An unavailable author does not represent one synthetic shared account.
        if not r['entity_id'] or r['entity_id'] in {'[deleted]', '[removed]'}:
            continue
        dt = datetime.fromisoformat(r['occurred_at'].replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('occurred_at requires a timezone offset')
        day = dt.astimezone(zone).date().isoformat()
        slot = int(dt.timestamp()) // (slot_minutes * 60)
        for obj, relation in r['objects'].items():
            if relation not in {'explicit', 'context'}:
                continue
            key = (day, r['platform'], r['panel_id'], r['entity_type'], obj)
            token = (key, r['record_id'], r['entity_id'])
            signature = (relation, r['occurred_at'], (r.get('cohort') or {}).get(obj))
            if token in seen:
                if seen[token] != signature:
                    raise ValueError(f'Conflicting duplicate: {token}')
                continue
            seen[token] = signature
            groups[key].append((r, relation, slot))

    complete = {}
    for c in coverage or []:
        date.fromisoformat(c['date'])
        for obj in c.get('objects', []):
            key = (c['date'], c['platform'], c['panel_id'], c['entity_type'], obj)
            if key in complete and complete[key] != c.get('complete', False):
                raise ValueError(f'Conflicting coverage record: {key}')
            complete[key] = c.get('complete') is True
            if complete[key]:
                groups.setdefault(key, [])

    explicit_sets = {
        k: {r['entity_id'] for r, relation, _ in values if relation == 'explicit'}
        for k, values in groups.items()
    }
    output = []
    for key, values in sorted(groups.items()):
        day, platform, panel, entity_type, obj = key
        explicit = explicit_sets[key]
        context = {r['entity_id'] for r, rel, _ in values if rel == 'context'} - explicit
        entities = explicit | context
        slots = {(r['entity_id'], slot) for r, _, slot in values}
        cohorts = defaultdict(set)
        circles = defaultdict(set)
        actor_cohorts = defaultdict(set)
        for r, _, _ in values:
            label = (r.get('cohort') or {}).get(obj)
            if label in COHORTS:
                actor_cohorts[r['entity_id']].add(label)
            for circle in r.get('circles', []):
                circles[circle].add(r['entity_id'])
        for actor in entities:
            labels = actor_cohorts.get(actor, set())
            if len(labels) > 1:
                raise ValueError(f'Conflicting cohort labels for {actor} in {key}')
            cohorts[next(iter(labels)) if labels else 'unlabelled'].add(actor)
        actor_slots = Counter(actor for actor, _ in slots)
        n_slots = len(slots)
        hhi = sum((n / n_slots) ** 2 for n in actor_slots.values()) if n_slots else None
        parent = (parents or {}).get(obj)
        parent_key = (day, platform, panel, entity_type, parent)
        parent_exists = parent is not None and parent_key in explicit_sets
        parent_set = explicit_sets.get(parent_key, set()) if parent_exists else None
        output.append({
            'date': day, 'platform': platform, 'panel_id': panel, 'entity_type': entity_type,
            'object_id': obj, 'coverage_complete': complete.get(key),
            'explicit_entities': len(explicit), 'context_only_entities': len(context),
            'all_related_entities': len(entities), 'observed_behaviors': len(values),
            'entity_slots': n_slots, 'slots_per_entity': ratio(n_slots, len(entities)),
            'cohorts': {label: len(cohorts[label]) for label in sorted(COHORTS | {'unlabelled'})},
            'circles': {label: len(actors) for label, actors in sorted(circles.items())},
            'slot_weighted_hhi': hhi, 'effective_sources_by_slots': 1 / hhi if hhi else None,
            'parent_object': parent,
            'parent_coverage_complete': complete.get(parent_key) if parent else None,
            'parent_explicit_entities': len(parent_set) if parent_set is not None else None,
            'topic_in_parent_entities': len(explicit & parent_set) if parent_set is not None else None,
            'topic_outside_parent_entities': len(explicit - parent_set) if parent_set is not None else None,
            'parent_share': ratio(len(explicit & parent_set), len(parent_set)) if parent_set is not None else None,
        })
    return output


def series_metrics(rows, recent=3, baseline=20):
    if recent <= 0 or baseline <= 0:
        raise ValueError('Window lengths must be positive')
    rows = sorted(rows, key=lambda x: x['date'])
    dates = [date.fromisoformat(r['date']) for r in rows]
    if len(set(dates)) != len(dates):
        raise ValueError('Series dates must be unique')
    values = [number(r.get('value')) for r in rows]
    current = values[-recent:]
    prior = values[max(0, len(values) - recent - baseline):max(0, len(values) - recent)]
    current_ok = len(current) == recent and all(v is not None for v in current)
    prior_ok = len(prior) == baseline and all(v is not None for v in prior)
    c = mean(current) if current_ok else None
    b = mean(prior) if prior_ok else None
    rolling_history = []
    # History ends before the entire current window begins.
    for end in range(recent, max(0, len(values) - recent) + 1):
        window = values[end - recent:end]
        if all(v is not None for v in window):
            rolling_history.append(mean(window))
    if not current_ok or not prior_ok:
        status = 'insufficient_or_missing_window'
    elif b == 0:
        status = 'baseline_zero'
    elif b < 0:
        status = 'nonpositive_baseline_use_absolute_change'
    else:
        status = 'ok'
    history = rolling_history[-250:]
    observed_peak = max(rolling_history) if rolling_history else None
    return {
        'as_of': rows[-1]['date'] if rows else None,
        'latest': values[-1] if values else None,
        'observations': len(values), 'recent_requested': recent, 'baseline_requested': baseline,
        'recent_observations': len(current), 'baseline_observations': len(prior),
        'recent_mean': c, 'baseline_mean': b,
        'absolute_mean_change': c - b if c is not None and b is not None else None,
        'relative_ratio': ratio(c, b), 'status': status,
        'prior_rolling_percentile': percentile(c, history), 'prior_rolling_observations': len(history),
        'observed_prior_peak': observed_peak, 'fraction_of_observed_prior_peak': ratio(c, observed_peak),
    }


def trading_metrics(data):
    stocks = data.get('stocks', [])
    symbols = [s['symbol'] for s in stocks]
    if len(symbols) != len(set(symbols)):
        raise ValueError('Each symbol must occur once in the daily pool')
    market = number(data.get('market_amount'))
    parent = number(data.get('parent_amount'))
    benchmark = number(data.get('benchmark_return'))
    details = []
    for s in stocks:
        amount = number(s.get('amount'))
        if amount is not None and amount < 0:
            raise ValueError('amount must be nonnegative')
        ret = number(s.get('return'))
        weight = number(s.get('weight_prev'))
        if weight is not None and weight < 0:
            raise ValueError('weight_prev must be nonnegative')
        turnover = number(s.get('turnover'))
        history = s.get('turnover_history', [])
        normal_share = number(s.get('normal_market_share'))
        surplus = amount - market * normal_share if all(x is not None for x in [amount, market, normal_share]) else None
        details.append({
            'symbol': s['symbol'], 'amount': amount, 'return': ret, 'weight_prev': weight,
            'tradable': s.get('tradable', True),
            'excess_return': ret - benchmark if ret is not None and benchmark is not None else None,
            'turnover_percentile': percentile(turnover, history),
            'turnover_history_n': sum(v is not None for v in history),
            'surplus_amount': surplus,
        })
    amounts = [s['amount'] for s in details]
    pool_amount = sum(amounts) if amounts and all(v is not None for v in amounts) else None
    returns = [s['return'] for s in details]
    all_returns = bool(returns) and all(v is not None for v in returns)
    ew = mean(returns) if all_returns else None
    weights_ok = all_returns and all(s['weight_prev'] is not None for s in details)
    weight_sum = sum(s['weight_prev'] for s in details) if weights_ok else None
    cw = sum(s['weight_prev'] * s['return'] for s in details) / weight_sum if weights_ok and weight_sum > 0 else None
    comparable = [s for s in details if s['tradable'] and s['excess_return'] is not None]
    n_win = sum(s['excess_return'] > 0 for s in comparable)
    strongest = max(comparable, key=lambda s: s['excess_return']) if comparable else None
    remaining = [s for s in details if strongest is not None and s['symbol'] != strongest['symbol']]
    remaining_ok = bool(remaining) and all(s['return'] is not None for s in remaining)
    loo = mean(s['return'] for s in remaining) - benchmark if remaining_ok and benchmark is not None else None
    comparable_loo = [s for s in remaining if s['tradable'] and s['excess_return'] is not None]
    surplus_complete = bool(details) and all(s['surplus_amount'] is not None for s in details)
    positive = sorted([max(s['surplus_amount'], 0) for s in details], reverse=True) if surplus_complete else []
    total_positive = sum(positive) if surplus_complete else None
    turnovers = [s['turnover_percentile'] for s in details if s['turnover_percentile'] is not None]
    return {
        'date': data.get('date'), 'pool_size': len(details),
        'pool_amount': pool_amount, 'market_share': ratio(pool_amount, market),
        'parent_share': ratio(pool_amount, parent),
        'equal_weight_return': ew, 'previous_weight_return': cw,
        'previous_weights_sum': weight_sum,
        'equal_weight_excess': ew - benchmark if ew is not None and benchmark is not None else None,
        'previous_weight_excess': cw - benchmark if cw is not None and benchmark is not None else None,
        'outperformers': n_win, 'comparable_stocks': len(comparable),
        'outperformance_breadth': ratio(n_win, len(comparable)),
        'high_turnover_stocks': sum(v >= 0.9 for v in turnovers), 'turnover_comparable_stocks': len(turnovers),
        'removed_strongest': strongest['symbol'] if strongest else None,
        'leave_strongest_out_excess': loo,
        'leave_strongest_out_breadth': ratio(sum(s['excess_return'] > 0 for s in comparable_loo), len(comparable_loo)),
        'positive_surplus_amount': total_positive,
        'negative_surplus_amount': sum(min(s['surplus_amount'], 0) for s in details) if surplus_complete else None,
        'positive_surplus_top1_share': ratio(positive[0], total_positive) if positive else None,
        'positive_surplus_top3_share': ratio(sum(positive[:3]), total_positive) if positive else None,
        'stocks': details,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('participation')
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--coverage', type=Path)
    p.add_argument('--config', type=Path)
    p.add_argument('--timezone', default='Asia/Shanghai')
    p.add_argument('--slot-minutes', type=int, default=30)
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('series')
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--recent', type=int, default=3)
    p.add_argument('--baseline', type=int, default=20)
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('trading')
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'participation':
            config = json.loads(args.config.read_text(encoding='utf-8')) if args.config else {}
            result = participation_metrics(read_jsonl(args.input), read_jsonl(args.coverage), config.get('parents'), args.timezone, args.slot_minutes)
        else:
            data = json.loads(args.input.read_text(encoding='utf-8'))
            result = series_metrics(data, args.recent, args.baseline) if args.command == 'series' else trading_metrics(data)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')
    print(f'Wrote {args.out}')


if __name__ == '__main__':
    main()
