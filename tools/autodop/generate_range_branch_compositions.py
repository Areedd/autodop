"""Month-keyed compositions of independently measured range-expansion branches."""
from dataclasses import asdict, replace

from generate_composable_branch_suite import bank as original_bank
from generate_opposed_branch_stress import HIGH
from generate_range_expansion_suite import THRESHOLDS, render_range_expansion


def threshold_monthly(query):
    """Keep overlapping threshold measures separate; absent groups have no members."""
    fields = []
    columns = ['report_month']
    for threshold in THRESHOLDS:
        for metric in ('qualified_customers', 'sales_lines', 'revenue', 'revenue_present'):
            alias = f't{threshold}_{metric}'
            fields.append(f'SUM(CASE WHEN threshold_amount={threshold} '
                          f'THEN {metric} ELSE 0 END) AS {alias}')
            columns.append(alias)
    body = ('SELECT report_month,' + ','.join(fields) + ' FROM ('
            + query.strip().rstrip(';') + ') thresholds GROUP BY report_month')
    return body, columns


def bank():
    branches = [original_bank()[0]]
    p = replace(HIGH, family='monthly_spend_thresholds', channel=2)
    body, columns = threshold_monthly(render_range_expansion(p))
    branches.append(dict(sql=body, columns=columns, parameters=asdict(p)))
    p = replace(HIGH, family='observed_span_coverage', channel=2)
    branches.append(dict(sql=render_range_expansion(p),
                         columns=['report_month', 'covered_customers',
                                  'first_month_customers', 'last_month_customers'],
                         parameters=asdict(p)))
    return branches
