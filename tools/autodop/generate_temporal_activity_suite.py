#!/usr/bin/env python3
"""Two-channel observed activity spells, state transitions and cohort follow-up."""
import argparse
from dataclasses import replace
import hashlib
import json
import re
from pathlib import Path
from generate_integrity_operator_suite import date_key
from generate_mixed_operator_suite import CHANNELS, generate, render_query, sql

FAMILIES = {
    'activity_spells': 'Observed activity spells separated by more than seven calendar days',
    'channel_state_transitions': 'Transitions between consecutive observed customer-day channel states',
    'observed_cohort_retention': 'Within-window first-active-month cohorts with observable follow-up',
    'scan_control': 'Single-channel scan control',
}


def activity_components(p):
    render_query(replace(p, family='scan_control'))
    if p.family not in FAMILIES or p.months != p.history_months or p.months not in (3, 6):
        raise ValueError('temporal activity requires a known family and equal 3/6 month windows')
    lo, hi = date_key(p.start_month), date_key(p.start_month + p.months)
    branches = []
    for index, c in enumerate((CHANNELS[p.channel], CHANNELS[(p.channel + 1) % 3])):
        branches.append(f"""SELECT s.{c.customer} AS customer_id, s.{c.sold_date} AS day_id,
       {1 if index == 0 else 0} AS in_a, {1 if index == 1 else 0} AS in_b,
       COUNT(*) AS sales_lines, SUM(s.{c.net_paid}) AS revenue,
       COUNT(s.{c.net_paid}) AS revenue_present
FROM {c.table} s WHERE s.{c.sold_date}>={lo} AND s.{c.sold_date}<{hi}
AND s.{c.customer} IS NOT NULL GROUP BY s.{c.customer},s.{c.sold_date}""")
    return """source_days AS (""" + '\nUNION ALL\n'.join(branches) + """), daily AS (
SELECT customer_id,day_id,MAX(in_a)+2*MAX(in_b) AS channel_state,
       SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM source_days GROUP BY customer_id,day_id), calendar_days AS (
SELECT a.*,d.d_month_seq AS month_seq FROM daily a JOIN date_dim d ON a.day_id=d.d_date_sk)"""


def render_dense_cohort(ctes, observed_months_sql):
    """Aggregate calendar_days(customer_id, month_seq, sales/revenue fields)."""
    return sql(f"""WITH {ctes}, monthly AS (
SELECT customer_id,month_seq,SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM calendar_days GROUP BY customer_id,month_seq), attributed AS (
SELECT *,MIN(month_seq) OVER (PARTITION BY customer_id) AS cohort_month FROM monthly),
observed_months AS (
{observed_months_sql})
SELECT a.cohort_month,o.month_seq-a.cohort_month AS month_offset,
       SUM(CASE WHEN a.month_seq=a.cohort_month THEN 1 ELSE 0 END) AS cohort_customers,
       SUM(CASE WHEN a.month_seq=o.month_seq THEN 1 ELSE 0 END) AS active_customers,
       SUM(CASE WHEN a.month_seq=o.month_seq THEN a.sales_lines ELSE 0 END) AS sales_lines,
       SUM(CASE WHEN a.month_seq=o.month_seq THEN a.revenue ELSE NULL END) AS revenue,
       SUM(CASE WHEN a.month_seq=o.month_seq THEN a.revenue_present ELSE 0 END) AS revenue_present
FROM attributed a JOIN observed_months o ON o.month_seq>=a.cohort_month
GROUP BY a.cohort_month,o.month_seq""")


def render_temporal_activity(p, retention_strategy='reference', *, shared_calendar=None):
    if retention_strategy not in ('reference', 'dense_expansion'):
        raise ValueError('unknown retention strategy')
    ctes = activity_components(p)
    if shared_calendar is not None:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', shared_calendar):
            raise ValueError('shared calendar must be a simple CTE identifier')
        ctes = f'calendar_days AS (SELECT * FROM {shared_calendar})'
    if p.family == 'scan_control':
        return render_query(p)
    if p.family == 'observed_cohort_retention' and retention_strategy == 'dense_expansion':
        return render_dense_cohort(ctes, f"SELECT DISTINCT d_month_seq AS month_seq FROM date_dim\nWHERE d_month_seq>={p.start_month} AND d_month_seq<{p.start_month+p.months}")
    if p.family == 'observed_cohort_retention':
        return sql(f"""WITH {ctes}, monthly AS (
SELECT customer_id,month_seq,SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM calendar_days GROUP BY customer_id,month_seq), first_month AS (
SELECT customer_id,MIN(month_seq) AS cohort_month FROM monthly GROUP BY customer_id),
cohorts AS (SELECT cohort_month,COUNT(*) AS cohort_customers FROM first_month GROUP BY cohort_month),
active AS (
SELECT f.cohort_month,m.month_seq,COUNT(*) AS active_customers,
       SUM(m.sales_lines) AS sales_lines,SUM(m.revenue) AS revenue,
       SUM(m.revenue_present) AS revenue_present
FROM first_month f JOIN monthly m ON f.customer_id=m.customer_id
GROUP BY f.cohort_month,m.month_seq), observed_months AS (
SELECT DISTINCT d_month_seq AS month_seq FROM date_dim
WHERE d_month_seq>={p.start_month} AND d_month_seq<{p.start_month+p.months})
SELECT c.cohort_month,o.month_seq-c.cohort_month AS month_offset,c.cohort_customers,
       COALESCE(a.active_customers,0) AS active_customers,
       COALESCE(a.sales_lines,0) AS sales_lines,a.revenue,
       COALESCE(a.revenue_present,0) AS revenue_present
FROM cohorts c JOIN observed_months o ON o.month_seq>=c.cohort_month
LEFT JOIN active a ON a.cohort_month=c.cohort_month AND a.month_seq=o.month_seq""")
    ctes += """, previous_day AS (
SELECT *,LAG(day_id) OVER (PARTITION BY customer_id ORDER BY day_id) AS previous_day,
       LAG(channel_state) OVER (PARTITION BY customer_id ORDER BY day_id) AS previous_state
FROM calendar_days)"""
    if p.family == 'channel_state_transitions':
        band = """CASE WHEN previous_day IS NULL THEN 'first_observed'
WHEN day_id-previous_day<=7 THEN 'within_7_days' ELSE 'over_7_days' END"""
        return sql(f"""WITH {ctes}
SELECT previous_state,channel_state,{band} AS gap_band,COUNT(*) AS customer_days,
       SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM previous_day GROUP BY previous_state,channel_state,{band}""")
    ctes += """, boundaries AS (
SELECT *,CASE WHEN previous_day IS NULL OR day_id-previous_day>7 THEN 1 ELSE 0 END AS new_spell
FROM previous_day), numbered AS (
SELECT *,SUM(new_spell) OVER (PARTITION BY customer_id ORDER BY day_id
ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS spell_id FROM boundaries), spells AS (
SELECT customer_id,spell_id,COUNT(*) AS active_days,MAX(day_id)-MIN(day_id) AS span_days,
       MAX(CASE WHEN channel_state IN (1,3) THEN 1 ELSE 0 END) AS seen_a,
       MAX(CASE WHEN channel_state IN (2,3) THEN 1 ELSE 0 END) AS seen_b,
       SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM numbered GROUP BY customer_id,spell_id)"""
    return sql(f"""WITH {ctes}
SELECT seen_a,seen_b,CASE WHEN active_days=1 THEN 'one_day' ELSE 'multiple_days' END AS duration_band,
       COUNT(*) AS observed_spells,SUM(CASE WHEN spell_id=1 THEN 1 ELSE 0 END) AS first_observed_spells,
       SUM(active_days) AS active_days,SUM(span_days) AS span_days,
       SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
       SUM(revenue_present) AS revenue_present
FROM spells GROUP BY seen_a,seen_b,CASE WHEN active_days=1 THEN 'one_day' ELSE 'multiple_days' END""")


def generate_temporal_activity(output, database, seed=20270127, test_variants=5, retention_strategy='reference'):
    if retention_strategy not in ('reference', 'dense_expansion'):
        raise ValueError('unknown retention strategy')
    output = Path(output)
    generate(output,database,seed,test_variants,families=FAMILIES,renderer=lambda p: render_temporal_activity(p, retention_strategy),
             source_path=Path(__file__),window_months=(3,6))
    for split in ('train','valid','test','all'):
        path=output/f'{split}.json';manifest=json.loads(path.read_text())
        manifest['date_helpers_source_sha256']=hashlib.sha256(Path(__file__).with_name('generate_integrity_operator_suite.py').read_bytes()).hexdigest()
        manifest['contract'].update(
            retention_strategy=retention_strategy,retention_strategy_semantics='Reference grouped joins or equivalent single-reference monthly-window attribution with at-most-six-month densification. Compare same strategy across DOP modes; SQL rewrite is not heterogeneous DOP gain.',
            family_count=4,controls_fraction=.25,query_dop_domain=[1,2,4,8,16],learned_max_dop=16,
            population='Known customer IDs in anchor and cyclic-next channel; preaggregate each customer/date, then merge channels. Anonymous customers excluded explicitly; IDs need not match current customer dimension.',
            date_semantics='Requires unique complete date_dim mapping and verified consecutive Julian day keys. Shared customers and grammar across disjoint date windows are not unseen-template or unseen-entity evaluation.',
            spell_semantics='Gap strictly greater than7 calendar days starts a new observed spell; first and last spells may be window-truncated. These are shopping activity spells, not web sessions.',
            state_semantics='1=anchor only,2=other only,3=both on same day; aggregate ties before LAG. Consecutive observed days, not ordered within-day visits or causal channel attribution. First observation retained separately.',
            retention_semantics='First active month within window, not first-ever acquisition. Denominator is cohort size. Emit zero-activity cells only for fully observable months through window end; later follow-up censored. Preserve NULL revenue and present-value counts.',
            representativeness='Three temporal reporting questions using two real synthetic retail sales channels, with shared daily preprocessing; not production prevalence or three independent operator mechanisms.',
            hypothesis='Different aggregation, ordered-window and small cohort-grid stages may need different DOP; no performance assumed and no latency input to construction.',
            inactive_parameters=['group','top_k'],stage='Semantic fixtures and calendar/nonempty guards before four-query prepass; no automatic expansion')
        path.write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--database',required=True)
    parser.add_argument('--retention-strategy',choices=('reference','dense_expansion'),default='reference')
    args=parser.parse_args()
    print(json.dumps({'queries':generate_temporal_activity(args.output_dir,args.database,retention_strategy=args.retention_strategy)['query_count']}))
