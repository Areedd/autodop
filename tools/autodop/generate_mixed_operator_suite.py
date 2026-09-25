#!/usr/bin/env python3
"""Deterministic retail workload grammar; generation never reads performance data."""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import random
import re

from generate_representative_serial_suite import CHANNELS, sql


FAMILIES = {
    'reconciliation': 'Reconcile customer revenue between sales channels',
    'retention': 'Customers active in history but absent from a recent channel',
    'returns': 'Category revenue and return exposure',
    'ranking': 'Rank category items against a longer historical reference',
    'dashboard': 'Channel revenue at daily and category reporting grains',
    'promotion': 'Customer promotion participation across channels',
    'scan_control': 'Single-channel financial reporting control',
    'star_control': 'Category and month revenue control',
}


@dataclass(frozen=True)
class Parameters:
    family: str
    channel: int
    start_month: int
    months: int
    history_months: int
    group: str
    top_k: int


def sales(c, start, months):
    return f"""SELECT s.{c.customer} AS customer_sk, s.{c.item} AS item_sk,
       s.{c.order} AS order_sk, s.{c.promo} AS promo_sk,
       d.d_date_sk AS date_sk, d.d_month_seq AS month_seq,
       s.{c.net_paid} AS revenue, s.{c.profit} AS profit
FROM {c.table} s JOIN date_dim d ON s.{c.sold_date} = d.d_date_sk
WHERE d.d_month_seq BETWEEN {start} AND {start + months - 1}"""


def render_query(p):
    if p.family not in FAMILIES or p.channel not in range(3):
        raise ValueError('unknown family/channel')
    if p.group not in ('i_category', 'i_class', 'i_brand') or not 1 <= p.top_k <= 100:
        raise ValueError('invalid grouping/top_k')
    if not (1 <= p.months <= p.history_months <= 24 and
            1176 <= p.start_month <= p.start_month + p.history_months - 1 <= 1235):
        raise ValueError('date windows must stay inside loaded 1998-2002 history')
    c, other = CHANNELS[p.channel], CHANNELS[(p.channel + 1) % 3]
    a = sales(c, p.start_month, p.months)
    b = sales(other, p.start_month, p.history_months)
    g = p.group
    if p.family == 'scan_control':
        return sql(f"WITH recent AS ({a}) SELECT COUNT(*) AS lines, SUM(revenue) AS revenue, SUM(profit) AS profit FROM recent")
    if p.family == 'star_control':
        return sql(f"""WITH recent AS ({a})
SELECT i.{g} AS segment, r.month_seq, COUNT(*) AS lines, SUM(r.revenue) AS revenue
FROM recent r JOIN item i ON r.item_sk = i.i_item_sk
GROUP BY i.{g}, r.month_seq""")
    prefix = f"WITH recent AS ({a}), history AS ({b}),\n"
    if p.family == 'reconciliation':
        body = """a AS (SELECT customer_sk, SUM(revenue) AS revenue FROM recent GROUP BY customer_sk),
b AS (SELECT customer_sk, SUM(revenue) AS revenue FROM history GROUP BY customer_sk)
SELECT CASE WHEN a.customer_sk IS NULL THEN 'history_only'
            WHEN b.customer_sk IS NULL THEN 'recent_only' ELSE 'both' END AS cohort,
       COUNT(*) AS customers, SUM(COALESCE(a.revenue, 0)) AS recent_revenue,
       SUM(COALESCE(b.revenue, 0)) AS history_revenue
FROM a FULL OUTER JOIN b ON a.customer_sk = b.customer_sk
GROUP BY cohort"""
    elif p.family == 'retention':
        body = """missing AS (
SELECT customer_sk FROM history WHERE customer_sk IS NOT NULL
EXCEPT SELECT customer_sk FROM recent WHERE customer_sk IS NOT NULL),
retained AS (
SELECT customer_sk FROM history WHERE customer_sk IS NOT NULL
INTERSECT SELECT customer_sk FROM recent WHERE customer_sk IS NOT NULL)
SELECT 'missing' AS cohort, COUNT(*) AS customers FROM missing
UNION ALL SELECT 'retained', COUNT(*) FROM retained"""
    elif p.family == 'returns':
        # Preaggregate each side on the real composite order/item key to avoid fanout.
        body = f"""sold AS (
SELECT order_sk, item_sk, SUM(revenue) AS revenue FROM recent GROUP BY order_sk, item_sk),
returned AS (
SELECT r.{c.return_order} AS order_sk, r.{c.return_item} AS item_sk,
       SUM(r.{c.return_amount}) AS amount
FROM {c.returns} r JOIN date_dim d ON r.{c.prefix[0]}r_returned_date_sk = d.d_date_sk
WHERE d.d_month_seq BETWEEN {p.start_month} AND {p.start_month + p.history_months - 1}
GROUP BY r.{c.return_order}, r.{c.return_item})
SELECT i.{g} AS segment, SUM(s.revenue) AS revenue,
       SUM(COALESCE(r.amount, 0)) AS returned_amount, COUNT(*) AS order_items
FROM sold s LEFT JOIN returned r ON s.order_sk = r.order_sk AND s.item_sk = r.item_sk
JOIN item i ON s.item_sk = i.i_item_sk GROUP BY i.{g}"""
        # This family has its own historical return branch; do not emit an unused CTE.
        prefix = f"WITH recent AS ({a}),\n"
    elif p.family == 'ranking':
        body = f"""item_recent AS (
SELECT item_sk, SUM(revenue) AS revenue FROM recent GROUP BY item_sk),
category_history AS (
SELECT i.{g} AS segment, SUM(h.revenue) AS revenue
FROM history h JOIN item i ON h.item_sk = i.i_item_sk GROUP BY i.{g}),
ranked AS (
SELECT i.{g} AS segment, r.item_sk, r.revenue,
       ROW_NUMBER() OVER (PARTITION BY i.{g} ORDER BY r.revenue DESC, r.item_sk) AS position
FROM item_recent r JOIN item i ON r.item_sk = i.i_item_sk)
SELECT r.segment, COUNT(*) AS top_items, SUM(r.revenue) AS top_revenue,
       MAX(h.revenue) AS historical_revenue
FROM ranked r LEFT JOIN category_history h ON r.segment = h.segment
WHERE r.position <= {p.top_k} GROUP BY r.segment"""
    elif p.family == 'dashboard':
        body = f"""daily AS (
SELECT date_sk, SUM(revenue) AS revenue, COUNT(*) AS lines FROM recent GROUP BY date_sk),
categories AS (
SELECT i.{g}_id AS segment, SUM(h.revenue) AS revenue, COUNT(*) AS lines
FROM history h JOIN item i ON h.item_sk = i.i_item_sk GROUP BY i.{g}_id)
SELECT 1 AS grain, CAST(date_sk AS BIGINT) AS key_value,
       CAST(revenue AS DECIMAL(38, 2)) AS revenue, lines FROM daily
UNION ALL SELECT 2, CAST(segment AS BIGINT),
       CAST(revenue AS DECIMAL(38, 2)), lines FROM categories"""
    else:  # promotion
        body = """promoted AS (
SELECT DISTINCT r.customer_sk FROM recent r JOIN promotion p ON r.promo_sk = p.p_promo_sk
WHERE r.customer_sk IS NOT NULL AND p.p_channel_email = 'Y'),
customer_history AS (
SELECT customer_sk, SUM(revenue) AS revenue, COUNT(DISTINCT item_sk) AS breadth
FROM history WHERE customer_sk IS NOT NULL GROUP BY customer_sk)
SELECT CASE WHEN p.customer_sk IS NULL THEN 'unexposed' ELSE 'exposed' END AS cohort,
       COUNT(*) AS customers, SUM(h.revenue) AS revenue, SUM(h.breadth) AS total_breadth
FROM customer_history h LEFT JOIN promoted p ON h.customer_sk = p.customer_sk
GROUP BY cohort"""
    return sql(prefix + body)


def generate(output, database, seed=20260906, test_variants=5, *, families=None, renderer=None, source_path=None,
             window_months=None):
    families = FAMILIES if families is None else families
    renderer = render_query if renderer is None else renderer
    source_path = Path(__file__) if source_path is None else Path(source_path)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', database):
        raise ValueError('unsafe database identifier')
    if not 1 <= test_variants <= 20:
        raise ValueError('test_variants must be between 1 and 20')
    if window_months is not None and (not window_months or len(set(window_months)) != len(window_months)
                                    or any(type(w) is not int or not 1 <= w <= 12 for w in window_months)):
        raise ValueError('explicit windows must be distinct integer month counts in 1..12')
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('output directory must be new/empty')
    rng = random.Random(seed)
    records = []
    for split, count, lower, upper in [('train', 1, 1176, 1187), ('valid', 1, 1188, 1199), ('test', test_variants, 1200, 1235)]:
        for family in families:
            for channel in range(3):
                seen = set()
                for variant in range(count):
                    while True:
                        if window_months is None:
                            months = rng.choice([1, 3, 6])
                            history = rng.choice([6, 12] if split != 'test' else [6, 12, 24])
                        else:
                            months = history = rng.choice(window_months)
                        start = rng.randint(lower, upper - history + 1)
                        p = Parameters(family, channel, start, months, history,
                                       rng.choice(['i_category', 'i_class', 'i_brand']), rng.choice([5, 10, 20]))
                        text = renderer(p)
                        digest = hashlib.sha256(text.encode()).hexdigest()
                        if digest not in seen:
                            seen.add(digest)
                            break
                    query_id = f'{family}__{CHANNELS[channel].name}__{split}_{variant:02d}'
                    records.append({'id': query_id, 'query_id': query_id, 'split': split,
                                    'database': database, 'template_family': family,
                                    'business_task': families[family], 'parameters': asdict(p),
                                    'sql_file': f'sql/{query_id}.sql', 'sql_sha256': digest,
                                    'sql': text, 'control': family.endswith('_control')})
    output.mkdir(parents=True, exist_ok=True)
    (output / 'sql').mkdir()
    for row in records:
        (output / row['sql_file']).write_text(row.pop('sql'))
    manifest = {'schema_version': 1, 'generator': source_path.name,
                'source_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
                'manifest_builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'channel_source_sha256': hashlib.sha256(Path(__file__).with_name('generate_representative_serial_suite.py').read_bytes()).hexdigest(),
                'seed': seed, 'query_count': len(records), 'queries': records,
                'contract': {'target_latency_reduction': .2, 'primary_mode': 'policy_static',
                             'primary_baselines': ['native', 'uniform4'], 'diagnostic_baselines': ['uniform8'],
                             'family_weight': 'equal', 'latency_based_selection': False,
                             'split_semantics': 'disjoint date parameters, shared grammar; train is development, no fitting here',
                             'representativeness': 'eight retail analytical families, not a production frequency distribution',
                             'controls_fraction': .25, 'concurrency': 1}}
    if window_months is not None:
        manifest['contract']['window_months'] = list(window_months)
    for split in ['train', 'valid', 'test', 'all']:
        selected = records if split == 'all' else [r for r in records if r['split'] == split]
        (output / f'{split}.json').write_text(json.dumps({**manifest, 'query_count': len(selected), 'queries': selected}, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--seed', type=int, default=20260906)
    parser.add_argument('--test-variants', type=int, default=5)
    args = parser.parse_args()
    manifest = generate(args.output_dir, args.database, args.seed, args.test_variants)
    print(json.dumps({'queries': manifest['query_count'], 'output': str(args.output_dir)}))


if __name__ == '__main__':
    main()
