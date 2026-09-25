#!/usr/bin/env python3
"""Independently reconcile accepted rows with unmodified FE traces and journals."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_trusted_dataset import build


def rows(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify(collection_dir, dataset_dir):
    collection_dir, dataset_dir = Path(collection_dir), Path(dataset_dir)
    collection = json.loads((collection_dir / 'collection.json').read_text())
    if collection['status'] != 'finished':
        raise ValueError('unfinished collection')
    accepted = {}
    provenance = {}
    for row in rows(collection_dir / 'measured/accepted.jsonl'):
        event = row.pop('trusted_provenance')
        qid = row['query_id']
        if event.get('phase') != 'measured' or event.get('status') != 'verified' or row.get('state') != 'EOF':
            raise ValueError('accepted record is not a verified formal measurement')
        if event.get('latency_ms') != row.get('latency_ms'):
            raise ValueError('accepted provenance latency differs from raw FE latency')
        if qid in accepted or event['query_id'] != qid:
            raise ValueError('duplicate/mismatched accepted query identity')
        accepted[qid] = digest(row)
        provenance[qid] = event
    raw_seen = set()
    raw_count = 0
    for path in sorted((collection_dir / 'raw').glob('autodop_trace_*.jsonl')):
        for row in rows(path):
            raw_count += 1
            qid = row.get('query_id')
            if qid in accepted:
                if qid in raw_seen or digest(row) != accepted[qid]:
                    raise ValueError('accepted row differs from raw FE trace or is duplicated')
                raw_seen.add(qid)
    if raw_seen != set(accepted):
        raise ValueError('accepted row lacks original FE trace')
    event_seen = set()
    phases = Counter()
    statuses = Counter()
    natives = {}
    for event in rows(collection_dir / 'provenance/executions.jsonl'):
        phases[event['phase']] += 1
        statuses[event['status']] += 1
        if event['phase'] == 'native' and event['status'] == 'verified':
            if event['id'] in natives:
                raise ValueError('duplicate native baseline')
            natives[event['id']] = event['result_digest']
        qid = event.get('query_id')
        if qid in provenance:
            if qid in event_seen or event != provenance[qid]:
                raise ValueError('accepted provenance differs from execution journal')
            event_seen.add(qid)
    if event_seen != set(accepted):
        raise ValueError('accepted row lacks original execution event')
    evidence_seen = set()
    labels = {}
    config_count = 0
    for config in rows(dataset_dir / 'sample_evidence.jsonl'):
        ids = config['query_ids']
        if len(ids) != len(set(ids)) or set(ids) & evidence_seen:
            raise ValueError('execution reused between configuration labels')
        events = [provenance[qid] for qid in ids]
        latencies = [event['latency_ms'] for event in events]
        if sorted(latencies) != sorted(config['latencies_ms']):
            raise ValueError('label latency differs from raw provenance')
        for event in events:
            if event['result_digest'] != natives[event['id']]:
                raise ValueError('accepted result differs from native baseline')
        if statistics.median(latencies) != config['median_ms']:
            raise ValueError('median label is not exact')
        key = config['plan_key'], config['config_key']
        if key in labels:
            raise ValueError('duplicate configuration evidence')
        labels[key] = config['median_ms']
        evidence_seen.update(ids)
        config_count += 1
    if evidence_seen != set(accepted):
        raise ValueError('dataset evidence lacks exact accepted execution coverage')
    sample_keys = set()
    for sample in rows(dataset_dir / 'all/samples.jsonl'):
        key = sample['plan_key'], sample['config_key']
        if key in sample_keys or sample['latency_ms'] != labels[key]:
            raise ValueError('training sample label differs from verified median')
        sample_keys.add(key)
    if sample_keys != set(labels):
        raise ValueError('sample/evidence configuration coverage differs')
    # Raw/journal equality does not establish graph/configuration association or
    # partition/pair correctness. This complementary reconstruction shares the
    # canonical extractor; it is not an independent extractor implementation.
    with tempfile.TemporaryDirectory(prefix='autodop-verify-') as temporary:
        reconstructed = Path(temporary)/'dataset'
        build(collection_dir/'measured/accepted.jsonl', collection['settings']['manifest'],
              reconstructed, collection['settings']['repetitions'],
              json.loads((dataset_dir/'summary.json').read_text())['pair_threshold'],
              collection_path=collection_dir/'collection.json')
        for expected_path in reconstructed.rglob('*'):
            if not expected_path.is_file():
                continue
            relative = expected_path.relative_to(reconstructed)
            actual_path = dataset_dir/relative
            if not actual_path.is_file():
                raise ValueError(f'dataset artifact missing: {relative}')
            if expected_path.suffix == '.jsonl':
                expected = sorted(digest(row) for row in rows(expected_path))
                actual = sorted(digest(row) for row in rows(actual_path))
            else:
                expected = json.loads(expected_path.read_text())
                actual = json.loads(actual_path.read_text())
            if actual != expected:
                raise ValueError(f'dataset artifact differs from reconstructed contract: {relative}')
    return {'status': 'verified', 'raw_traces': raw_count, 'accepted_executions': len(accepted),
            'configuration_labels': config_count, 'journal_phases': dict(phases),
            'journal_statuses': dict(statuses),
            'checks': ['raw_trace_equals_accepted_without_provenance',
                       'journal_equals_accepted_provenance', 'native_result_identity',
                       'exact_label_latency_and_median', 'unique_complete_execution_coverage', 'raw_latency_equals_provenance',
                       'full_dataset_artifacts_equal_canonical_reconstruction']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection-dir', required=True)
    parser.add_argument('--dataset-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = verify(args.collection_dir, args.dataset_dir)
    destination = Path(args.output)
    if destination.exists():
        raise ValueError('refusing to overwrite verification evidence')
    destination.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
