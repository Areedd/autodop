"""Four fixed structural probes using the existing high/low DOP branch bank."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from generate_composable_branch_suite import Node, compile_sql, leaves, schema
from generate_range_branch_compositions import bank


def topologies():
    a, threshold, span = [Node('leaf', i) for i in range(3)]
    return [
        ('left_threshold', Node('left', left=a, right=threshold)),
        ('union_span', Node('union', left=a, right=span)),
        ('inner_then_full', Node('full', left=Node('inner', left=a, right=threshold), right=span)),
        ('join_over_union', Node('inner', left=a, right=Node('union', left=threshold, right=span))),
    ]


def generate(output, database, *, components=None, trees=None, query_prefix='range_topology'):
    components = bank() if components is None else components
    trees = topologies() if trees is None else trees
    if len(trees) != 4:
        raise ValueError('exactly four structural probes required')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'sql').mkdir()
    queries = []
    for name, tree in trees:
        qid = f'{query_prefix}_{name}__train_00'
        path = output / 'sql' / (qid + '.sql')
        path.write_text(compile_sql(tree, components))
        ids = sorted(leaves(tree))
        queries.append(dict(id=qid, query_id=qid, split='train', database=database,
            template_family='range_branch_topology', tree=asdict(tree), branch_ids=ids,
            parameters=components[0]['parameters'], input_parameters=[components[i]['parameters'] for i in ids],
            control=False, sql_file=str(path.relative_to(output)),
            sql_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    sources = [Path(__file__), Path(__file__).with_name('generate_range_branch_compositions.py'),
               Path(__file__).with_name('generate_range_expansion_suite.py'),
               Path(__file__).with_name('generate_composable_branch_suite.py')]
    manifest = dict(schema_version=1, query_count=4, queries=queries, output_columns=schema(components),
        branch_bank=[{k: v for k, v in c.items() if k != 'sql'} for c in components],
        contract=dict(training_eligible=False, concurrency=1,
            scope='Four frozen development-diagnostic structures, not heldout or production-representative sampling. '
                  'All four outcomes retained; no automatic expansion. Same existing source windows and full metrics. '
                  'No repeated leaves, forced DOP hints or padding. Count physical plans separately after EXPLAIN.'),
        component_source_sha256={str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    (output / 'train.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    generate(args.output, args.database)
