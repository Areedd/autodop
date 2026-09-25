"""Fresh parameter contexts for legacy pair/cohort/window training coverage."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from generate_composable_branch_suite import Node
from generate_month_keyed_branch_suite import branches
from generate_opposed_branch_stress import HIGH, LOW
from generate_product_window_branch_suite import render, WINDOW_METRICS
from generate_range_topology_suite import generate as generate_four


START_MONTH = 1187


def bank():
    high = replace(HIGH, start_month=START_MONTH)
    low = replace(LOW, start_month=START_MONTH)
    _, window, _ = render(high)
    return [*branches(high, low), dict(sql=window, columns=['report_month', *WINDOW_METRICS],
                                      parameters=asdict(high), family='monthly_product_window')]


def topologies():
    a, b, d = [Node('leaf', i) for i in range(3)]
    return [('pair_control', a), ('cohort_control', b),
            ('left_deep', Node('left', left=Node('left', left=a, right=b), right=d)),
            ('right_deep', Node('left', left=a, right=Node('left', left=b, right=d)))]


def generate(output, database):
    manifest = generate_four(output, database, components=bank(), trees=topologies(),
                             query_prefix='legacy_branch_development_1187')
    for query in manifest['queries']:
        query['control'] = query['tree']['op'] == 'leaf'
        query['template_family'] = 'legacy_branch_development'
    manifest['contract'].update(training_eligible=True,
        scope='Four fresh training-development contexts at months1187..1189: two standalone controls '
              'and left/right nested LEFT compositions of pair/cohort/window branches. Known structures, '
              'not new structural families or source-row independence. No fitting before trusted collection '
              'and protected SQL/raw-plan/split isolation audit. All outcomes retained; no DOP hints.')
    for name in ['generate_legacy_branch_development_suite.py', 'generate_month_keyed_branch_suite.py',
                 'generate_product_window_branch_suite.py', 'generate_opposed_branch_stress.py',
                 'generate_temporal_activity_suite.py', 'generate_pair_grain_stress.py']:
        path = Path(__file__).with_name(name).resolve()
        manifest['component_source_sha256'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    (Path(output) / 'train.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    generate(args.output, args.database)
