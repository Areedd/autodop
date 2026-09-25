"""Measured-preference candidates: amount thresholds and observed span coverage.

Thresholds are overlapping reporting cutoffs, not disjoint histogram bins.
Span coverage describes the interval between observed endpoints, not activity.
"""
from dataclasses import replace

from generate_temporal_activity_suite import activity_components

FAMILIES = ('monthly_spend_thresholds', 'observed_span_coverage')
THRESHOLDS = (100, 1000, 10000)


def render_from_monthly(family, monthly_sql, months_sql):
    """Input grain: unique customer_id/month_seq with additive revenue fields."""
    if family == 'monthly_spend_thresholds':
        thresholds = ' UNION ALL '.join(
            f'SELECT {value} AS threshold_amount' for value in THRESHOLDS)
        return f"""WITH monthly AS ({monthly_sql}), thresholds AS ({thresholds})
SELECT m.month_seq AS report_month,t.threshold_amount,
COUNT(*) AS qualified_customers,SUM(m.sales_lines) AS sales_lines,
SUM(m.revenue) AS revenue,SUM(m.revenue_present) AS revenue_present
FROM monthly m JOIN thresholds t ON m.revenue>=t.threshold_amount
GROUP BY m.month_seq,t.threshold_amount"""
    if family == 'observed_span_coverage':
        return f"""WITH monthly AS ({monthly_sql}), spans AS (
SELECT customer_id,MIN(month_seq) AS first_month,MAX(month_seq) AS last_month
FROM monthly GROUP BY customer_id), observed_months AS ({months_sql})
SELECT o.month_seq AS report_month,COUNT(*) AS covered_customers,
SUM(CASE WHEN o.month_seq=s.first_month THEN 1 ELSE 0 END) AS first_month_customers,
SUM(CASE WHEN o.month_seq=s.last_month THEN 1 ELSE 0 END) AS last_month_customers
FROM spans s JOIN observed_months o
ON o.month_seq>=s.first_month AND o.month_seq<=s.last_month
GROUP BY o.month_seq"""
    raise ValueError('unknown range-expansion family')


def render_range_expansion(p):
    if p.family not in FAMILIES:
        raise ValueError('unknown range-expansion family')
    ctes = activity_components(replace(p, family='activity_spells'))
    monthly = f"""WITH {ctes}
SELECT customer_id,month_seq,SUM(sales_lines) AS sales_lines,
SUM(revenue) AS revenue,SUM(revenue_present) AS revenue_present
FROM calendar_days GROUP BY customer_id,month_seq"""
    months = f"""SELECT DISTINCT d_month_seq AS month_seq FROM date_dim
WHERE d_month_seq>={p.start_month} AND d_month_seq<{p.start_month+p.months}"""
    return render_from_monthly(p.family, monthly, months) + ';\n'
