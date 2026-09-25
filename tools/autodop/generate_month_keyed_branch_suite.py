"""Calendar-aligned product-pair and customer-cohort branches, with complete outputs."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from generate_mixed_operator_suite import sql
from generate_opposed_branch_stress import HIGH, LOW, PAIR_COLUMNS
from generate_pair_grain_stress import render_pair_grain
from generate_temporal_activity_suite import render_temporal_activity

PAIR_METRICS = PAIR_COLUMNS[1:]
COHORT_METRICS = ['eligible_customers', 'active_customers', 'sales_lines', 'revenue', 'revenue_present']
JOIN_KINDS = ('inner', 'full_outer')


def branches(high=HIGH, low=None):
    low = replace(LOW, start_month=high.start_month) if low is None else low
    if high.family != 'scan_control' or low.family != 'observed_cohort_retention':
        raise ValueError('invalid branch parameter families')
    if (high.start_month, high.months) != (low.start_month, low.months):
        raise ValueError('month-keyed branches require equal reporting windows')
    a = render_pair_grain(high, 'day', 7, report_grain='anchor_month')
    cohort = render_temporal_activity(low, 'dense_expansion').strip().rstrip(';')
    b = sql(f'''SELECT cohort_month+month_offset AS report_month,
SUM(cohort_customers) AS eligible_customers,SUM(active_customers) AS active_customers,
SUM(sales_lines) AS sales_lines,SUM(revenue) AS revenue,
SUM(revenue_present) AS revenue_present
FROM ({cohort}) cohort_report GROUP BY cohort_month+month_offset''')
    return [{'family': 'monthly_product_pairs', 'sql': a, 'columns': ['report_month', *PAIR_METRICS], 'parameters': asdict(high)},
            {'family': 'monthly_observed_cohorts', 'sql': b, 'columns': ['report_month', *COHORT_METRICS], 'parameters': asdict(low)}]


def compose(children, kind):
    if kind not in JOIN_KINDS or len(children) != 2:
        raise ValueError('expected two branches and supported month join')
    join = 'INNER JOIN' if kind == 'inner' else 'FULL OUTER JOIN'
    fields = ['COALESCE(a.report_month,b.report_month) AS report_month',
              *[f'a.{c} AS pair_{c}' for c in PAIR_METRICS],
              *[f'b.{c} AS cohort_{c}' for c in COHORT_METRICS]]
    a, b = [c['sql'].strip().rstrip(';') for c in children]
    return sql('SELECT '+',\n'.join(fields)+f'\nFROM ({a}) a {join} ({b}) b ON a.report_month=b.report_month')


def assemble(left, right, kind):
    """Client reference join, preserving missing-side NULLs and rejecting duplicate keys."""
    if kind not in JOIN_KINDS:
        raise ValueError('unknown month join')
    def index(rows):
        result = {}
        for row in rows:
            if row[0] is None or row[0] in result:
                raise ValueError('month keys must be nonnull and unique')
            result[row[0]] = tuple(row[1:])
        return result
    a, b = index(left), index(right)
    months = set(a) & set(b) if kind == 'inner' else set(a) | set(b)
    return [(month, *a.get(month, (None,)*len(PAIR_METRICS)),
             *b.get(month, (None,)*len(COHORT_METRICS))) for month in sorted(months)]


def generate(output, database, *, high=HIGH, low=None):
    children = branches(high, low)
    cases = [('month_pair_alone', children[0]['sql'], children[:1], True),
             ('month_cohort_alone', children[1]['sql'], children[1:], True),
             *[(f'month_{kind}_join', compose(children, kind), children, False) for kind in JOIN_KINDS]]
    output = Path(output); output.mkdir(parents=True, exist_ok=False); (output/'sql').mkdir()
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    queries = []
    for name, body, parts, control in cases:
        qid = name+'__train_00'; path = output/'sql'/(qid+'.sql'); path.write_text(body)
        queries.append({'id': qid, 'query_id': qid, 'split': 'train', 'database': database,
                        'template_family': name, 'parameters': parts[0]['parameters'],
                        'input_parameters': [p['parameters'] for p in parts],
                        'business_task': 'Month-aligned product follow-up and observed customer cohort reporting',
                        'control': control, 'sql_file': str(path.relative_to(output)), 'sql_sha256': sha(path)})
    sources = ['generate_month_keyed_branch_suite.py', 'generate_pair_grain_stress.py',
               'generate_opposed_branch_stress.py', 'generate_temporal_activity_suite.py',
               'generate_mixed_operator_suite.py', 'generate_integrity_operator_suite.py']
    manifest = {'schema_version': 1, 'generator': Path(__file__).name, 'query_count': 4, 'queries': queries,
                'component_source_sha256': {s: sha(Path(__file__).with_name(s)) for s in sources},
                'contract': {'training_eligible': False, 'concurrency': 1, 'query_dop_domain': [1,2,4,8,16],
                    'scope': 'Two existing computational mechanisms and two keyed composition types, not four independent business families. Diagnostic data; no assumed performance gain or production frequency.',
                    'output': 'Complete relational metrics, one row per observed branch month. INNER retains overlap; FULL retains union with NULL missing-side metrics. Pair exposures repeat over matches and must not be added to cohort ledger revenue.',
                    'calendar': 'Same three-month window. Pair month is anchor month; complete seven-day anchor cutoff remains. Unique date mapping required. Cohorts refer to first observations in-window, not lifetime acquisition.',
                    'stage': 'Four fixed queries only. Fixtures, complete raw/copy/client-assembly equality and actual plan verification before serial pilot; retain negatives. No automatic expansion or fitting.'}}
    (output/'train.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True); p.add_argument('--database', required=True)
    args = p.parse_args(); generate(args.output_dir, args.database)
