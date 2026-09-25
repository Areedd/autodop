"""Three/four distinct computational branches with several low-DOP mechanisms."""
import hashlib
import json
from pathlib import Path

from generate_composable_branch_suite import Node, bank as original_bank
from generate_range_branch_compositions import bank as range_bank
from generate_range_topology_suite import generate as generate_four


def bank():
    return [*range_bank(), original_bank()[1]]


def topologies():
    a, threshold, span, cohort = [Node('leaf', i) for i in range(4)]
    inner = Node('inner', left=a, right=threshold)
    return [
        ('inner_then_left', Node('left', left=inner, right=span)),
        ('left_then_full', Node('full', left=Node('left', left=a, right=threshold), right=span)),
        ('balanced_four', Node('full', left=inner, right=Node('inner', left=span, right=cohort))),
        ('deep_four', Node('left', left=Node('full', left=inner, right=span), right=cohort)),
    ]


def generate(output, database):
    manifest = generate_four(output, database, components=bank(), trees=topologies(), query_prefix='multi_low')
    manifest['contract']['scope'] = (
        'Four frozen development probes: two three-branch and two four-branch trees. '
        'A product-day pairs, T amount thresholds, S observed spans, B observed-cohort retention. '
        'Distinct computational branches, no repeated leaves or removed output metrics. '
        'Actual physical structure and benefits require separate checks. All four outcomes retained; no training.')
    sources = [Path(__file__), *[Path(__file__).with_name(name) for name in (
        'generate_three_way_keyed_suite.py', 'generate_month_keyed_branch_suite.py',
        'generate_temporal_activity_suite.py', 'generate_opposed_branch_stress.py',
        'generate_pair_grain_stress.py')]]
    manifest['component_source_sha256'].update({str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    (Path(output) / 'train.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    generate(args.output, args.database)
