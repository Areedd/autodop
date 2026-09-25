#!/usr/bin/env python3
"""Assess a frozen workload's latency target; never select winning SQL."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics

from summarize_paper_evaluations import percentile


def metrics(values):
    return {'mean': statistics.mean(values), 'p50': percentile(values, .5),
            'p90': percentile(values, .9), 'p99': percentile(values, .99)}


def assess(manifest, collection, events, target=.2):
    if not 0 < target < 1:
        raise ValueError('target must be between zero and one')
    contract = manifest['contract']
    method = contract['primary_mode']
    baselines = contract['primary_baselines']
    modes = [method, *baselines, *contract.get('diagnostic_baselines', [])]
    queries = {q['id']: q for q in manifest['queries']}
    if len(queries) != len(manifest['queries']) or not queries:
        raise ValueError('duplicate/empty query manifest')
    repetitions = collection['settings']['repetitions']
    slots = defaultdict(list)
    invalid = []
    for event in events:
        if event.get('phase') != 'measured' or event.get('mode') not in modes:
            continue
        key = (event.get('mode'), event.get('id'), event.get('round'))
        if key[1] not in queries or key[2] not in range(repetitions) or event.get('clients') != 1:
            invalid.append(key)
        else:
            slots[key].append(event)
    by_mode, issues = {}, []
    for mode in modes:
        values, applied, per_query = [], 0, {}
        for qid in queries:
            samples = []
            for index in range(repetitions):
                rows = slots[mode, qid, index]
                if len(rows) != 1:
                    issues.append({'mode': mode, 'id': qid, 'round': index, 'reason': 'missing_or_duplicate'})
                    continue
                row = rows[0]
                latency = row.get('client_latency_ms')
                if (row.get('status') != 'verified' or isinstance(latency, bool) or
                        not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency <= 0 or
                        row.get('sql_sha256') != queries[qid]['sql_sha256']):
                    issues.append({'mode': mode, 'id': qid, 'round': index, 'reason': 'failed_or_invalid'})
                    continue
                samples.append(latency)
                applied += bool(row.get('dop_validation', {}).get('applied'))
            if len(samples) == repetitions:
                per_query[qid] = statistics.median(samples)
            values.extend(samples)
        complete = len(values) == len(queries) * repetitions and not any(i['mode'] == mode for i in issues)
        by_mode[mode] = {'complete': complete, 'verified_executions': len(values),
                         'applied_executions': applied,
                         'workload_latency_ms': metrics(values) if complete else None,
                         'query_median_distribution_ms': metrics(list(per_query.values())) if complete else None}
    comparisons = {}
    for baseline in baselines + contract.get('diagnostic_baselines', []):
        a, b = by_mode[method]['workload_latency_ms'], by_mode[baseline]['workload_latency_ms']
        reduction = {key: 1 - a[key] / b[key] for key in a} if a and b else None
        comparisons[baseline] = {'latency_reduction': reduction,
                                 'all_four_observed_targets_met': all(v >= target or math.isclose(v, target, rel_tol=0, abs_tol=1e-12)
                                                                      for v in reduction.values()) if reduction else False}
    complete = not invalid and not issues and collection['status'] == 'finished'
    heldout = all(q['split'] == 'test' for q in queries.values())
    enough_tail = len(queries) >= 100 and len(queries) * repetitions >= 1000
    observed = complete and all(comparisons[b]['all_four_observed_targets_met'] for b in baselines)
    return {'status': 'complete' if complete else 'incomplete', 'query_count': len(queries),
            'repetitions': repetitions, 'family_counts': dict(Counter(q['template_family'] for q in queries.values())),
            'target_latency_reduction': target, 'modes': by_mode, 'comparisons': comparisons,
            'primary_observed_target_met': observed,
            'heldout': heldout, 'minimum_tail_sample_size_met': enough_tail,
            'eligible_for_heldout_target_claim': observed and heldout and enough_tail,
            'scope': 'Equal-count pooled execution latency distribution; query-median distribution reported separately. Not per-query temporal p99.',
            'statistical_note': 'Point estimates only. Repeated executions are not independent templates; this gate does not establish confidence or generalization.',
            'issues': issues, 'unexpected_events': invalid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--collection-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sources = [args.manifest, args.collection_dir / 'collection.json', args.collection_dir / 'events.jsonl']
    if args.output.exists():
        raise ValueError('output already exists')
    manifest, collection = [json.loads(p.read_text()) for p in sources[:2]]
    if collection['manifest_sha256'] != hashlib.sha256(args.manifest.read_bytes()).hexdigest():
        raise ValueError('manifest differs from measured collection')
    report = assess(manifest, collection, [json.loads(l) for l in sources[2].read_text().splitlines() if l.strip()])
    report['source_sha256'] = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ['status', 'primary_observed_target_met', 'eligible_for_heldout_target_claim']}))


if __name__ == '__main__':
    main()
