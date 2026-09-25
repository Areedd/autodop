#!/usr/bin/env python3
"""Freeze per-query uniform choices on calibration; score independent executions.

The selected baseline is reconstructed from actually executed uniform-mode
records. Selection never uses scoring latency. The additional retrospective
oracle is explicitly a diagnostic and never determines selected-mode results.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import statistics

from assess_workload_target import assess
from autodop.data import DOP_CLASSES

DOPS = tuple(d for d in DOP_CLASSES if d <= 8)


def dop_domain(max_dop=8):
    if max_dop not in (8, 16, 32):
        raise ValueError('maximum DOP must be 8, 16 or 32')
    return tuple(d for d in DOP_CLASSES if d <= max_dop)


def stability(queries, events, baseline, repetitions, bootstrap_samples=5000, seed=20260922):
    """Paired query clusters retain repeated observations; conditional on this grammar."""
    values = {}
    for qid in queries:
        for mode in ('policy_static', baseline):
            values[qid, mode] = [e['client_latency_ms'] for e in events
                                if e.get('phase') == 'measured' and e['id'] == qid and e['mode'] == mode]
    clusters = [(statistics.mean(values[qid, 'policy_static']), statistics.mean(values[qid, baseline]))
                for qid in queries]
    rng = random.Random(seed)
    ratios = []
    for _ in range(bootstrap_samples):
        sample = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        ratios.append(1 - sum(a for a, _ in sample) / sum(b for _, b in sample))
    ratios.sort()
    lower, upper = ratios[int(.025 * bootstrap_samples)], ratios[int(.975 * bootstrap_samples)]
    rounds = []
    for index in range(repetitions):
        totals = {mode: sum(e['client_latency_ms'] for e in events
                            if e.get('phase') == 'measured' and e['round'] == index and e['mode'] == mode)
                  for mode in ('policy_static', baseline)}
        rounds.append(1 - totals['policy_static'] / totals[baseline])
    return {'paired_query_bootstrap_mean_reduction_ci95': [lower, upper],
            'bootstrap_samples': bootstrap_samples, 'seed': seed,
            'per_repetition_mean_reduction': rounds,
            'stable_mean_advantage': len(clusters) >= 24 and lower > 0 and all(value > 0 for value in rounds),
            'scope': 'Query clusters, repeated observations kept together; conditional on shared grammar, not unseen-template confidence'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_verified(directory, required_modes):
    directory = Path(directory).resolve()
    verification = json.loads((directory / 'independent_verification.json').read_text())
    if not verification.get('all_expected_executions_verified') or not verification.get('evidence_valid'):
        raise ValueError('independent evidence incomplete')
    for path, digest in verification['source_sha256'].items():
        if sha(path) != digest:
            raise ValueError('verified source changed: ' + path)
    collection = json.loads((directory / 'collection.json').read_text())
    events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
    manifest = json.loads(Path(collection['settings']['manifest']).read_text())
    if sha(collection['settings']['manifest']) != collection['manifest_sha256']:
        raise ValueError('manifest changed')
    check_manifest = {**manifest, 'contract': {'primary_mode': required_modes[0],
                                            'primary_baselines': list(required_modes[1:])}}
    check = assess(check_manifest, collection, events)
    if check['status'] != 'complete' or collection['settings']['repetitions'] < 3:
        raise ValueError('at least three complete repetitions of every required mode are necessary')
    return manifest, collection, events


def choose_uniform(queries, events, dops=DOPS):
    result = {}
    for query in queries:
        medians = {}
        for dop in dops:
            values = [e['client_latency_ms'] for e in events
                      if e['phase'] == 'measured' and e['id'] == query['id'] and e['mode'] == f'uniform{dop}']
            medians[dop] = statistics.median(values)
        result[query['id']] = {'dop': min(dops, key=lambda dop: (medians[dop], dop)),
                               'database': query['database'], 'sql_sha256': query['sql_sha256'],
                               'calibration_median_ms': medians}
    return result


def freeze(calibration_dir, max_dop=8):
    dops = dop_domain(max_dop)
    manifest, collection, events = load_verified(calibration_dir, [f'uniform{d}' for d in dops])
    return {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'calibration_dir': str(Path(calibration_dir).resolve()),
            'calibration_events_sha256': sha(Path(calibration_dir) / 'events.jsonl'),
            'calibration_finished_at': collection['finished_at'], 'dop_domain': list(dops),
            'selection': 'minimum per-query calibration median; smaller DOP breaks ties',
            'queries': choose_uniform(manifest['queries'], events, dops)}


def score(selection_path, scoring_dir):
    selection_path = Path(selection_path).resolve()
    selected = json.loads(selection_path.read_text())
    selected_domain = selected['dop_domain']
    if not selected_domain or selected_domain != list(dop_domain(max(selected_domain))):
        raise ValueError('query DOP domain differs')
    dops = tuple(selected_domain)
    manifest, collection, events = load_verified(
        scoring_dir, ['policy_static', 'native', *[f'uniform{d}' for d in dops]])
    calibration = Path(selected['calibration_dir'])
    if sha(calibration / 'events.jsonl') != selected['calibration_events_sha256']:
        raise ValueError('calibration changed')
    # Recompute selection from independently verified calibration, never trust arbitrary DOP labels.
    cal_manifest, cal_collection, cal_events = load_verified(calibration, [f'uniform{d}' for d in dops])
    rebuilt = json.loads(json.dumps(choose_uniform(cal_manifest['queries'], cal_events, dops)))
    if rebuilt != selected['queries']:
        raise ValueError('selection is not the calibration minimum')
    if not (datetime.fromisoformat(cal_collection['finished_at']) <= datetime.fromisoformat(selected['created_at'])
            < datetime.fromisoformat(collection['started_at'])):
        raise ValueError('selection must be frozen after calibration and before scoring')
    if {e.get('query_id') for e in cal_events} & {e.get('query_id') for e in events}:
        raise ValueError('calibration and scoring executions overlap')
    queries = {q['id']: q for q in manifest['queries']}
    if set(queries) != set(selected['queries']):
        raise ValueError('query coverage differs')
    for qid, query in queries.items():
        entry = selected['queries'][qid]
        if any(entry[key] != query[key] for key in ('sql_sha256', 'database')):
            raise ValueError('query identity differs')
    oracle = choose_uniform(manifest['queries'], events, dops)
    augmented = list(events)
    for event in events:
        if event.get('phase') != 'measured':
            continue
        for name, assignments in [('query_uniform_selected', selected['queries']),
                                  ('query_uniform_oracle', oracle)]:
            if event['mode'] == f"uniform{assignments[event['id']]['dop']}":
                augmented.append({**event, 'mode': name, 'source_mode': event['mode']})
    contract = {**manifest['contract'], 'primary_mode': 'policy_static',
                'primary_baselines': ['native', 'uniform4'],
                'diagnostic_baselines': [*[f'uniform{d}' for d in dops if d != 4],
                                         'query_uniform_selected', 'query_uniform_oracle']}
    if 16 not in dops and any(e['mode'] == 'uniform16' for e in events):
        contract['diagnostic_baselines'].append('uniform16')
    report = assess({**manifest, 'contract': contract}, collection, augmented)
    if report['status'] != 'complete':
        raise ValueError('incomplete scoring evidence')
    stable = {baseline: stability(queries, augmented, baseline, collection['settings']['repetitions'])
              for baseline in ('query_uniform_selected', 'query_uniform_oracle')}
    preserved = report['primary_observed_target_met']
    policy = report['modes']['policy_static']
    applied = policy['applied_executions'] == policy['verified_executions']
    if not applied:
        report['primary_observed_target_met'] = report['eligible_for_heldout_target_claim'] = False
    report['preserved_native_fixed4_twenty_percent_target'] = preserved and applied
    report['query_level_stability'] = stable
    report['stronger_objective_met'] = (preserved and applied and report['heldout']
                                       and report['minimum_tail_sample_size_met']
                                       and all(s['stable_mean_advantage'] for s in stable.values()))
    report['eligible_for_heldout_target_claim'] = report['stronger_objective_met']
    report.update(selection_sha256=sha(selection_path), all_policy_decisions_applied=applied,
                  query_dop_domain=list(dops),
                  selected_dops={qid: entry['dop'] for qid, entry in selected['queries'].items()},
                  retrospective_oracle_dops={qid: entry['dop'] for qid, entry in oracle.items()},
                  baseline_scope='Reconstructed from independent scored uniform executions; fixed per-query calibration choice; calibration overhead excluded',
                  oracle_scope='Post-hoc best median uniform per query; optimistic diagnostic, not deployable',
                  scoring_events_sha256=sha(Path(scoring_dir) / 'events.jsonl'))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('freeze')
    prepare.add_argument('--calibration-dir', required=True, type=Path)
    prepare.add_argument('--max-dop', type=int, choices=(8, 16, 32), default=8)
    prepare.add_argument('--output', required=True, type=Path)
    evaluate = sub.add_parser('score')
    evaluate.add_argument('--selection', required=True, type=Path)
    evaluate.add_argument('--scoring-dir', required=True, type=Path)
    evaluate.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output already exists')
    report = freeze(args.calibration_dir, args.max_dop) if args.command == 'freeze' else score(args.selection, args.scoring_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'status': report.get('status', 'frozen')}))


if __name__ == '__main__':
    main()
