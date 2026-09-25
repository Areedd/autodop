#!/usr/bin/env python3
"""Incremental ledger consistency and pseudonymous-event audit workloads."""
import argparse
from dataclasses import replace
from datetime import date
import json
from pathlib import Path

from generate_mixed_operator_suite import CHANNELS, generate, render_query, sql


FAMILIES = {
    'row_signature': 'Numeric sales-ledger row consistency signature',
    'partition_signature': 'Daily ledger export consistency signatures',
    'entity_signature': 'Customer ledger group signatures and row-count skew',
    'json_signature': 'Canonical field-named ledger serialization consistency',
    'pseudonymous_event': 'Pseudonymous transaction export consistency',
    'return_signature': 'Refund-ledger row consistency signature',
    'scan_control': 'Single-channel financial reporting control',
    'star_control': 'Category and month revenue control',
}


def date_key(month_seq):
    """TPC-DS date surrogate is the Julian day; verify against the deployment audit."""
    return date(1900 + month_seq // 12, month_seq % 12 + 1, 1).toordinal() + 1721425


def fingerprint(expression):
    # Exact 60-bit contribution summed in 128 bits. This is an audit indicator,
    # not a cryptographic proof of multiset equality or a uniqueness constraint.
    return f"CAST(CONV(SUBSTR(SHA2({expression}, 256), 1, 15), 16, 10) AS LARGEINT)"


def render_integrity(p):
    render_query(replace(p, family='scan_control'))
    if p.family not in FAMILIES:
        raise ValueError('unknown integrity family')
    if p.family.endswith('_control'):
        return render_query(p)
    c = CHANNELS[p.channel]
    columns = [c.order, c.item, c.customer, c.sold_date, c.quantity,
               c.sales_price, c.net_paid, c.profit, c.discount, c.tax, c.coupon, c.promo]
    table, date_column = c.table, c.sold_date
    if p.family == 'return_signature':
        date_column = c.prefix[0] + 'r_returned_date_sk'
        table = c.returns
        columns = [c.return_order, c.return_item, date_column, c.return_amount,
                   c.prefix[0] + 'r_return_quantity']
    cells = [f"COALESCE(CAST(s.{col} AS VARCHAR), '<NULL>')" for col in columns]
    joins = ''
    if p.family == 'pseudonymous_event':
        # Hex encoding keeps the numeric tuple delimiter unambiguous for text.
        cells += ["COALESCE(HEX(LOWER(TRIM(c.c_email_address))), '<NULL>')"]
        joins = f' LEFT JOIN customer c ON s.{c.customer} = c.c_customer_sk'
    encoded = "CONCAT_WS('|', " + ', '.join(cells) + ')'
    if p.family == 'json_signature':
        fields = ', '.join(f"'{col}', CAST(s.{col} AS VARCHAR)" for col in columns)
        encoded = f'CAST(JSON_OBJECT({fields}) AS VARCHAR)'
    value = fingerprint(encoded)
    source = f"""FROM {table} s{joins}
WHERE s.{date_column} >= {date_key(p.start_month)}
  AND s.{date_column} < {date_key(p.start_month + p.months)}"""
    if p.family == 'partition_signature':
        return sql(f"SELECT s.{date_column} AS date_key, COUNT(*) AS rows_in_partition, SUM({value}) AS signature\n{source}\nGROUP BY s.{date_column}")
    if p.family == 'entity_signature':
        return sql(f"""WITH grouped AS (
SELECT s.{c.customer} AS customer_sk, COUNT(*) AS rows_in_group, SUM({value}) AS signature
{source} GROUP BY s.{c.customer})
SELECT COUNT(*) AS customer_groups, SUM(rows_in_group) AS ledger_rows,
       MAX(rows_in_group) AS largest_group, SUM(signature) AS signature FROM grouped""")
    return sql(f'SELECT COUNT(*) AS ledger_rows, SUM({value}) AS signature\n{source}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--seed', type=int, default=20260909)
    parser.add_argument('--test-variants', type=int, default=5)
    args = parser.parse_args()
    manifest = generate(args.output_dir, args.database, args.seed, args.test_variants,
                        families=FAMILIES, renderer=render_integrity, source_path=Path(__file__))
    print(json.dumps({'queries': manifest['query_count'], 'output': str(args.output_dir)}))


if __name__ == '__main__':
    main()
