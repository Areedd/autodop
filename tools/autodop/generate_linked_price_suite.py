#!/usr/bin/env python3
"""Product/month price distributions linked to independently aggregated business facts."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import CHANNELS, generate, render_query, sql
from generate_statistical_analytical_suite import taxonomy_fields

FAMILIES = {
    'price_inventory_profile': 'Price-position sales mix and observed inventory snapshots',
    'price_returns_profile': 'Price-position sales mix and same-month return exposure',
    'price_channel_profile': 'Price-position sales mix and other-channel monthly sales',
    'scan_control': 'Single-channel sales scan control',
}


def linked_components(p):
    if p.family not in FAMILIES or p.family == 'scan_control':
        raise ValueError('expected linked price family')
    render_query(replace(p, family='scan_control'))
    if p.months != p.history_months or p.months not in (3, 6):
        raise ValueError('linked reports require equal 3/6 month windows')
    c = CHANNELS[p.channel]
    low, high = date_key(p.start_month), date_key(p.start_month + p.months)
    main = f"""lines AS (
SELECT s.{c.item} AS item_id, d.d_month_seq AS month_seq,
       s.{c.quantity} AS quantity, s.{c.net_paid} AS revenue, s.{c.profit} AS profit,
       CAST(s.{c.net_paid} AS DOUBLE) / s.{c.quantity} AS unit_price
FROM {c.table} s JOIN date_dim d ON s.{c.sold_date} = d.d_date_sk
WHERE s.{c.sold_date} >= {low} AND s.{c.sold_date} < {high}
  AND s.{c.item} IS NOT NULL AND s.{c.quantity} > 0 AND s.{c.net_paid} IS NOT NULL),
ranked AS (
SELECT *, CUME_DIST() OVER (PARTITION BY item_id, month_seq ORDER BY unit_price) AS price_fraction
FROM lines),
main AS (
SELECT item_id, month_seq, COUNT(*) AS sales_lines, SUM(quantity) AS quantity,
       SUM(revenue) AS revenue, SUM(profit) AS profit, COUNT(profit) AS profit_present,
       SUM(CASE WHEN price_fraction <= 0.2 THEN 1 ELSE 0 END) AS lower_price_lines,
       SUM(CASE WHEN price_fraction > 0.8 THEN 1 ELSE 0 END) AS upper_price_lines,
       SUM(CASE WHEN price_fraction > 0.8 THEN revenue ELSE 0 END) AS upper_price_revenue
FROM ranked GROUP BY item_id, month_seq)"""
    if p.family == 'price_inventory_profile':
        branch = f"""snapshot_totals AS (
SELECT inv_item_sk AS item_id, inv_date_sk AS snapshot_date,
       SUM(inv_quantity_on_hand) AS observed_units,
       COUNT(*) - COUNT(inv_quantity_on_hand) AS unknown_quantities
FROM inventory WHERE inv_date_sk >= {low} AND inv_date_sk < {high}
  AND inv_item_sk IS NOT NULL AND inv_warehouse_sk IS NOT NULL
GROUP BY inv_item_sk, inv_date_sk),
branch AS (
SELECT s.item_id, d.d_month_seq AS month_seq, COUNT(*) AS snapshot_dates,
       COUNT(s.observed_units) AS valued_snapshot_dates,
       AVG(s.observed_units) AS average_observed_units,
       SUM(s.unknown_quantities) AS unknown_quantities
FROM snapshot_totals s JOIN date_dim d ON s.snapshot_date = d.d_date_sk
GROUP BY s.item_id, d.d_month_seq)"""
        metrics = ['snapshot_dates', 'valued_snapshot_dates', 'average_observed_units', 'unknown_quantities']
    else:
        if p.family == 'price_returns_profile':
            prefix = c.prefix[0] + 'r'
            table, item, date = c.returns, c.return_item, prefix + '_returned_date_sk'
            quantity, amount = prefix + '_return_quantity', c.return_amount
            label = 'return'
        else:
            other = CHANNELS[(p.channel + 1) % 3]
            table, item, date = other.table, other.item, other.sold_date
            quantity, amount = other.quantity, other.net_paid
            label = 'other'
        metrics = [label + suffix for suffix in ('_lines', '_quantity', '_quantity_present', '_amount', '_amount_present')]
        branch = f"""branch AS (
SELECT b.{item} AS item_id, d.d_month_seq AS month_seq,
       COUNT(*) AS {metrics[0]}, SUM(b.{quantity}) AS {metrics[1]},
       COUNT(b.{quantity}) AS {metrics[2]}, SUM(b.{amount}) AS {metrics[3]},
       COUNT(b.{amount}) AS {metrics[4]}
FROM {table} b JOIN date_dim d ON b.{date} = d.d_date_sk
WHERE b.{date} >= {low} AND b.{date} < {high} AND b.{item} IS NOT NULL
GROUP BY b.{item}, d.d_month_seq)"""
    return main + ',\n' + branch, metrics


def render_linked_price(p):
    if p.family == 'scan_control':
        if p.months != p.history_months or p.months not in (3,6):
            raise ValueError('linked reports require equal 3/6 month windows')
        return render_query(p)
    ctes, metrics = linked_components(p)
    keys = ', '.join('i.' + field for field in taxonomy_fields(p.group))
    main_metrics = ['sales_lines','quantity','revenue','profit','profit_present','lower_price_lines','upper_price_lines','upper_price_revenue']
    sums = ',\n       '.join([*(f'SUM(m.{name}) AS {name}' for name in main_metrics),
                             *(f'SUM(b.{name}) AS {name}' for name in metrics)])
    return sql(f"""WITH {ctes}
SELECT m.month_seq, {keys}, COUNT(*) AS product_months,
       COUNT(b.item_id) AS matched_product_months,
       {sums}
FROM main m LEFT JOIN branch b ON m.item_id = b.item_id AND m.month_seq = b.month_seq
LEFT JOIN item i ON m.item_id = i.i_item_sk
GROUP BY m.month_seq, {keys}""")


def generate_linked_price(output, database, seed=20270108, test_variants=5):
    output = Path(output)
    generate(output,database,seed,test_variants,families=FAMILIES,
             renderer=render_linked_price,source_path=Path(__file__),window_months=(3,6))
    for split in ('train','valid','test','all'):
        path=output/f'{split}.json';data=json.loads(path.read_text())
        data['helper_source_sha256']={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('generate_integrity_operator_suite.py','generate_statistical_analytical_suite.py')}
        data['contract'].update(
            family_count=4,controls_fraction=.25,query_dop_domain=[1,2,4,8,16],learned_max_dop=16,
            hypothesis='Required line ordering and independent business aggregation meet at unique item/month; no artificial repeated work',
            representativeness='Three linked retail reports sharing one price-distribution branch, plus control; not production frequencies or independent mechanisms',
            design_provenance='Development-informed after negative single-chain pilots; no latency input or winner filtering',
            main_population='Known item, positive quantity, nonnull net paid lines; missing taxonomy retained. Profit presence counted. CUME_DIST peer upper boundaries do not split ties; bands need not contain20% of rows.',
            join_semantics='Both sides unique item/month before LEFT JOIN; branch sums occur once per matched main product/month, never once per price band. Unmatched branch sums remainNULL, matched count explicit.',
            inventory_semantics='Sum observed warehouse balances per snapshot then average nonnull snapshot totals per month. Unknown quantities/valued dates counted. Not complete available stock; averages are not additive over months.',
            returns_semantics='Return-date month exposure, not same-sale cohort return rate or causal price effect.',
            channel_semantics='Same-window next-channel observations; no causal or customer-cohort equivalence claim.',
            inactive_parameters=['top_k'],stage='Semanticfixtures and branch-availability guards then fourfixedtrainprepass; no automatic expansion')
        path.write_text(json.dumps(data,indent=2)+'\n')
    return data


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--database',required=True);a=p.parse_args()
    print(json.dumps({'queries':generate_linked_price(a.output_dir,a.database)['query_count']}))
