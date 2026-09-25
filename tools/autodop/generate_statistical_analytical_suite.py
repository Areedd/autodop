#!/usr/bin/env python3
"""Development-informed statistical and basket analytics, without export padding."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import CHANNELS, generate, render_query, sql


FAMILIES = {
    'basket_category_affinity': 'Receipt-level co-purchase counts and paired-category value',
    'product_margin_volatility': 'Daily product margin volatility relative to category peers',
    'product_multimetric_trend': 'Product rolling seven active-day quantity, revenue and profit',
    'scan_control': 'Single-channel sales scan control',
}


def taxonomy_fields(group):
    """Business-label hierarchy, not an assumed globally unique surrogate code."""
    return {'i_category': ['i_category'],
            'i_class': ['i_category', 'i_class'],
            'i_brand': ['i_category', 'i_class', 'i_brand']}[group]


def render_statistical(p):
    if p.family not in FAMILIES:
        raise ValueError('unknown statistical analytical family')
    render_query(replace(p, family='scan_control'))
    if p.months != p.history_months or p.months not in (3, 6):
        raise ValueError('statistical analytics require equal 3/6 month windows')
    if p.family == 'scan_control':
        return render_query(p)
    c = CHANNELS[p.channel]
    fields = taxonomy_fields(p.group)
    aliases = [f'group_{index}' for index in range(len(fields))]
    key_select = ', '.join(f'i.{field} AS {alias}' for field, alias in zip(fields, aliases))
    key_group = ', '.join(f'i.{field}' for field in fields)
    bounds = f's.{c.sold_date} >= {date_key(p.start_month)} AND s.{c.sold_date} < {date_key(p.start_month + p.months)}'
    if p.family == 'basket_category_affinity':
        ordered_pair = ' OR '.join(
            '(' + ' AND '.join([*(f'a.{key} = b.{key}' for key in aliases[:index]),
                                 f'a.{alias} < b.{alias}']) + ')'
            for index, alias in enumerate(aliases))
        return sql(f"""WITH receipt_categories AS (
SELECT s.{c.order} AS receipt_id, {key_select},
       SUM(s.{c.net_paid}) AS revenue, SUM(s.{c.quantity}) AS quantity
FROM {c.table} s JOIN item i ON s.{c.item} = i.i_item_sk
WHERE {bounds} AND s.{c.order} IS NOT NULL AND {' AND '.join(f'i.{field} IS NOT NULL' for field in fields)}
GROUP BY s.{c.order}, {key_group})
SELECT {', '.join(f'a.{key} AS first_{key}, b.{key} AS second_{key}' for key in aliases)},
       COUNT(*) AS co_purchase_receipts,
       SUM(a.revenue + b.revenue) AS paired_category_revenue,
       SUM(a.quantity + b.quantity) AS paired_category_quantity
FROM receipt_categories a JOIN receipt_categories b
  ON a.receipt_id = b.receipt_id AND (
     {ordered_pair})
GROUP BY {', '.join(f'a.{key}, b.{key}' for key in aliases)}""")
    daily = f"""SELECT s.{c.item} AS item_id, s.{c.sold_date} AS sale_date,
       SUM(s.{c.net_paid}) AS revenue, SUM(s.{c.profit}) AS profit,
       SUM(s.{c.quantity}) AS quantity
FROM {c.table} s WHERE {bounds}
GROUP BY s.{c.item}, s.{c.sold_date}"""
    if p.family == 'product_margin_volatility':
        return sql(f"""WITH daily AS ({daily}),
margin_days AS (
SELECT item_id, sale_date, revenue, profit, quantity,
       CAST(profit AS DECIMAL(38, 6)) / NULLIF(revenue, 0) AS margin
FROM daily),
product_stats AS (
SELECT item_id, COUNT(margin) AS margin_days, SUM(revenue) AS revenue,
       SUM(profit) AS profit, SUM(quantity) AS quantity, AVG(margin) AS average_margin,
       STDDEV_POP(margin) AS margin_volatility
FROM margin_days GROUP BY item_id),
peers AS (
SELECT p.*, {key_select},
       AVG(p.average_margin) OVER (PARTITION BY {key_group}) AS peer_margin,
       AVG(p.margin_volatility) OVER (PARTITION BY {key_group}) AS peer_volatility
FROM product_stats p LEFT JOIN item i ON p.item_id = i.i_item_sk)
SELECT {', '.join(aliases)}, COUNT(*) AS products, SUM(revenue) AS revenue,
       SUM(profit) AS profit, SUM(quantity) AS quantity, SUM(margin_days) AS observed_margin_days,
       ROUND(AVG(average_margin), 4) AS average_product_margin,
       ROUND(AVG(margin_volatility), 4) AS average_product_volatility,
       SUM(CASE WHEN margin_volatility > peer_volatility AND average_margin < peer_margin
                THEN 1 ELSE 0 END) AS volatile_low_margin_products
FROM peers GROUP BY {', '.join(aliases)}""")
    window = 'PARTITION BY item_id ORDER BY sale_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW'
    return sql(f"""WITH daily AS ({daily}),
rolling AS (
SELECT *, SUM(revenue) OVER ({window}) AS rolling_revenue,
       SUM(profit) OVER ({window}) AS rolling_profit, SUM(quantity) OVER ({window}) AS rolling_quantity,
       LAG(revenue, 1) OVER (PARTITION BY item_id ORDER BY sale_date) AS previous_revenue
FROM daily)
SELECT sale_date, COUNT(*) AS active_products, SUM(revenue) AS revenue,
       SUM(profit) AS profit, SUM(quantity) AS quantity,
       SUM(rolling_revenue) AS rolling_revenue, SUM(rolling_profit) AS rolling_profit,
       SUM(rolling_quantity) AS rolling_quantity,
       SUM(CASE WHEN revenue > previous_revenue THEN 1 ELSE 0 END) AS growing_products
FROM rolling GROUP BY sale_date""")


def generate_statistical(output, database, seed=20261024, test_variants=5):
    output = Path(output)
    generate(output, database, seed, test_variants, families=FAMILIES,
             renderer=render_statistical, source_path=Path(__file__), window_months=(3, 6))
    for split in ('train', 'valid', 'test', 'all'):
        path = output / f'{split}.json'
        data = json.loads(path.read_text())
        data['date_helpers_source_sha256'] = hashlib.sha256(
            Path(__file__).with_name('generate_integrity_operator_suite.py').read_bytes()).hexdigest()
        data['contract'].update(
            family_count=4, controls_fraction=.25,
            representativeness='Three additional retail analytics mechanisms and one control; not production frequency',
            design_provenance='Grammar chosen after earlier negative development results; parameter generation does not read latency',
            query_dop_domain=[1, 2, 4, 8, 16], learned_max_dop=16,
            diagnostic_baselines=['uniform1', 'uniform2', 'uniform8', 'uniform16'],
            basket_semantics='One unordered category pair per receipt, after receipt/category preaggregation; missing receipt/category excluded. Paired value is not total ledger revenue.',
            taxonomy_semantics='Named hierarchy: category; category/class; category/class/brand. Surrogate class IDs are not global business keys. Labels define reporting groups, not stable entity identity.',
            rolling_semantics='Seven observed active dates per product, not seven calendar days; all dated sale rows included',
            volatility_semantics='Population standard deviation of daily margin, null for zero daily revenue; average over observed product days, not revenue-weighted',
            stage='Fixed four-query smoke before any larger evaluation; preserve earlier failed families')
        path.write_text(json.dumps(data, indent=2) + '\n')
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--seed', type=int, default=20261024)
    parser.add_argument('--test-variants', type=int, default=5)
    args = parser.parse_args()
    result = generate_statistical(args.output_dir, args.database, args.seed, args.test_variants)
    print(json.dumps({'queries': result['query_count']}))
