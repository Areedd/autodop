#!/usr/bin/env python3
"""Build datasets only from accepted formal executions, preserving template splits."""

import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autodop.data import build_metadata, build_pairs, read_jsonl, sample_from_trace, write_jsonl
from autodop.training import dataset_fingerprint, file_sha256
from collect_trusted_dataset import validate_trace, verification_collection


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def build(input_path, manifest_path, output, repetitions=3, pair_threshold=.03, *, collection_path):
    if repetitions < 3:
        raise ValueError("trusted labels require at least three measured repetitions")
    manifest = json.loads(Path(manifest_path).read_text())
    templates = {row['id']: row for row in manifest['queries']}
    if len(templates) != len(manifest['queries']):
        raise ValueError('duplicate query ids in manifest')
    collection_path = Path(collection_path)
    collection = json.loads(collection_path.read_text())
    verification_only = verification_collection(collection.get('settings', {}))
    if collection.get('status') != 'finished':
        raise ValueError('collection must have finished before building trusted labels')
    if collection.get('manifest_sha256') != file_sha256(manifest_path):
        raise ValueError('collection original manifest hash differs')
    collected = {row['id']: row for row in collection['queries']}
    if len(collected) != len(collection['queries']) or set(collected) != set(templates):
        raise ValueError('collection template coverage differs from selection manifest')
    accepted = {}
    for tid, row in collected.items():
        for key in ('split', 'sql_sha256', 'database'):
            if row.get(key) != templates[tid].get(key):
                raise ValueError('collection template identity differs from selection manifest')
        if row.get('status') not in ('accepted', 'excluded'):
            raise ValueError('unfinished query in collection')
        snapshot = collection_path.parent / 'sql' / (tid + '.sql')
        if file_sha256(snapshot) != row['sql_sha256']:
            raise ValueError('SQL snapshot hash differs from selected template')
        if row['status'] == 'accepted':
            configs = row.get('configs', [])
            if (len(configs) < (1 if verification_only else 2)
                    or (verification_only and len(configs) != 1)
                    or len({digest(c) for c in configs}) != len(configs)):
                raise ValueError('accepted query requires distinct frozen configurations')
            if row.get('measured_executions') != len(configs) * repetitions:
                raise ValueError('accepted query declared execution count differs from frozen plan')
            accepted[tid] = row
    observed = defaultdict(set)
    sql_splits = {}
    for row in templates.values():
        if row['split'] not in ('train', 'valid', 'test'):
            raise ValueError('unknown template split')
        sql_hash = row['sql_sha256']
        if sql_hash in sql_splits and sql_splits[sql_hash] != row['split']:
            raise ValueError('same SQL appears in different splits')
        sql_splits[sql_hash] = row['split']
    grouped = defaultdict(list)
    query_ids = set()
    template_digests = {}
    for record in read_jsonl(input_path):
        provenance = record.get('trusted_provenance', {})
        if provenance.get('phase') != 'measured' or record.get('state') != 'EOF':
            raise ValueError('trusted dataset only accepts successful measured executions')
        if provenance.get('status') != 'verified' or provenance.get('expected_repetitions') != repetitions:
            raise ValueError('unverified execution or repetition contract mismatch')
        tid = provenance['id']
        if tid not in accepted:
            raise ValueError('trace belongs to a query not accepted by the completed collection')
        template = templates[tid]
        config_id = provenance.get('config_id')
        if type(config_id) is not int or not 0 <= config_id < len(accepted[tid]['configs']):
            raise ValueError('unknown frozen configuration id')
        if provenance['requested_dop'] != accepted[tid]['configs'][config_id]:
            raise ValueError('requested DOP differs from frozen configuration')
        repetition = provenance.get('round')
        if type(repetition) is not int or not 0 <= repetition < repetitions:
            raise ValueError('invalid formal repetition index')
        position = (config_id, repetition)
        if position in observed[tid]:
            raise ValueError('duplicate configuration/repetition slot')
        observed[tid].add(position)
        if provenance.get('database') != template.get('database'):
            raise ValueError('execution database differs from selected template')
        if provenance['split'] != template['split'] or provenance['sql_sha256'] != template['sql_sha256']:
            raise ValueError('trace template/split identity differs from manifest')
        query_id = record['query_id']
        if provenance.get('query_id') != query_id:
            raise ValueError('provenance query identity differs')
        actual = validate_trace(record, provenance['requested_dop'], query_id)
        if actual != provenance['actual_dop']:
            raise ValueError('actual DOP provenance differs from execution trace')
        result = provenance['result_digest']
        if not isinstance(result, dict) or result != accepted[tid].get('native_result_digest'):
            raise ValueError('measured result digest differs from independent native baseline')
        if template['id'] in template_digests and template_digests[template['id']] != result:
            raise ValueError('result digest differs between executions of one template')
        template_digests[template['id']] = result
        if query_id in query_ids:
            raise ValueError('duplicate execution query id')
        query_ids.add(query_id)
        sample = sample_from_trace(record)
        if sample['plan_key'] != accepted[tid].get('plan_key'):
            raise ValueError('execution plan differs from frozen probe plan')
        # Namespacing prevents feature-identical SQL templates from crossing held-out boundaries.
        sample['plan_key'] = digest({'sql_sha256': template['sql_sha256'], 'plan': sample['plan_key']})
        sample['template_id'] = template['id']
        sample['template_family'] = template.get('template_family', template['id'])
        sample['database'] = template['database']
        sample['sql_sha256'] = template['sql_sha256']
        sample['split'] = template['split']
        grouped[(sample['plan_key'], sample['config_key'])].append((sample, record))
    for tid, row in accepted.items():
        expected = {(config_id, repetition) for config_id in range(len(row['configs']))
                    for repetition in range(repetitions)}
        if observed[tid] != expected:
            raise ValueError('missing complete configuration or repetition from accepted query: ' + tid)
    if not grouped:
        raise ValueError('no accepted measured executions')
    samples, evidence = [], []
    for (plan, config), rows in sorted(grouped.items()):
        rounds = [row['trusted_provenance']['round'] for _, row in rows]
        if len(rows) != repetitions or sorted(rounds) != list(range(repetitions)):
            raise ValueError(f'incomplete or duplicated repetitions for {plan}/{config}')
        latencies = [sample['latency_ms'] for sample, _ in rows]
        sample = dict(rows[0][0])
        sample['latency_ms'] = statistics.median(latencies)
        samples.append(sample)
        evidence.append({
            'plan_key': plan, 'config_key': config, 'template_id': sample['template_id'],
            'split': sample['split'], 'query_ids': [row['query_id'] for _, row in rows],
            'latencies_ms': latencies, 'median_ms': sample['latency_ms'],
            'min_ms': min(latencies), 'max_ms': max(latencies),
            'relative_spread': (max(latencies)-min(latencies))/sample['latency_ms'],
        })
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('output dataset directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    summary = {'input_sha256': file_sha256(input_path), 'manifest_sha256': file_sha256(manifest_path),
               'collection_sha256': file_sha256(collection_path),
               'measured_executions': len(query_ids), 'label_semantics': 'installed_requested_fragment_dop',
               'pair_threshold': pair_threshold, 'partitions': {}}
    if verification_only:
        summary['collection_purpose'] = 'verification_only'
    intervals = {(row['plan_key'], row['config_key']): (row['min_ms'], row['max_ms'])
                 for row in evidence}
    summary['pair_filter'] = 'median relative gap >= threshold and fast maximum < slow minimum; observed ranges only, not statistical confidence'
    classes = sorted({dop for sample in samples for fragment in sample['fragments'] for dop in fragment['candidate_dops']})
    for name, splits in [('all', ('train','valid','test')), ('train', ('train',)),
                         ('valid', ('valid',)), ('test', ('test',)), ('train_valid', ('train','valid'))]:
        selected = [sample for sample in samples if sample['split'] in splits]
        candidate_pairs = build_pairs(selected, pair_threshold)
        pairs = [pair for pair in candidate_pairs
                 if intervals[(pair['plan_key'], pair['fast_config_key'])][1]
                 < intervals[(pair['plan_key'], pair['slow_config_key'])][0]]
        folder = output / name
        folder.mkdir()
        write_jsonl(folder/'samples.jsonl', selected)
        write_jsonl(folder/'pairs.jsonl', pairs)
        if selected:
            metadata = build_metadata(selected, classes)
            if verification_only:
                metadata['collection_purpose'] = 'verification_only'
            metadata['pair_count'] = len(pairs)
            (folder/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
        summary['partitions'][name] = {'templates': len({s['template_id'] for s in selected}),
             'plans': len({s['plan_key'] for s in selected}), 'samples': len(selected),
             'candidate_pairs': len(candidate_pairs),
             'dropped_unstable_pairs': len(candidate_pairs) - len(pairs),
             'pairs': len(pairs), 'fingerprint': dataset_fingerprint(selected,pairs)}
    write_jsonl(output/'sample_evidence.jsonl', evidence)
    summary['high_variance_configurations'] = sum(row['relative_spread']>.2 for row in evidence)
    summary['missing_templates'] = sorted(set(templates)-{s['template_id'] for s in samples})
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    (output/'split_manifest.json').write_text(json.dumps({
        split: sorted({s['plan_key'] for s in samples if s['split']==split})
        for split in ('train','valid','test')},indent=2)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--collection',required=True, help='completed collection.json with acceptance evidence')
    parser.add_argument('--repetitions',type=int,default=3)
    args=parser.parse_args()
    if args.repetitions < 3:
        parser.error('trusted labels require at least three measured repetitions')
    print(json.dumps(build(args.input,args.manifest,args.output,args.repetitions,collection_path=args.collection),indent=2))


if __name__=='__main__':
    main()
