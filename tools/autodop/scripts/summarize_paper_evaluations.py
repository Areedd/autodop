#!/usr/bin/env python3
"""Summarize complete benchmark evidence without dropping failures or warmup leakage."""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import random
import re
import statistics


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    return ordered[lower] + (ordered[math.ceil(position)] - ordered[lower]) * (position - lower)


def distribution(values):
    return {'n': len(values), 'mean': statistics.mean(values) if values else None,
            'median': statistics.median(values) if values else None,
            'p90': percentile(values, .9), 'p95': percentile(values, .95)}


def bootstrap_paired(pairs, seed=20260905, iterations=4000):
    """Ratio of means, resampling independent paired rounds, never query pseudo-replicates."""
    if not pairs:
        return {'paired_rounds': 0, 'ratio': None, 'ci95': None}
    rng = random.Random(seed)
    ratios = []
    for _ in range(iterations):
        sample = [rng.choice(pairs) for _ in pairs]
        denominator = sum(pair[1] for pair in sample)
        if denominator > 0:
            ratios.append(sum(pair[0] for pair in sample) / denominator)
    denominator = sum(pair[1] for pair in pairs)
    return {'paired_rounds': len(pairs), 'ratio': sum(pair[0] for pair in pairs) / denominator if denominator else None,
            'ci95': [percentile(ratios, .025), percentile(ratios, .975)],
            'method': f'paired percentile bootstrap of round ratio-of-means; {iterations} resamples',
            'limitation': 'Very few independent rounds; interval is descriptive and may be unstable.' if len(pairs) < 10 else None}


def jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def iter_jsonl(path):
    if path.exists():
        with path.open() as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def summarize_collector_resources(samples):
    windows, invalid = [], 0
    for previous, current in zip(samples, samples[1:]):
        elapsed = current['monotonic_seconds'] - previous['monotonic_seconds']
        cpu = ((current['user_cpu_seconds'] + current['system_cpu_seconds']) -
               (previous['user_cpu_seconds'] + previous['system_cpu_seconds']))
        if elapsed <= 0 or cpu < 0:
            invalid += 1
            continue
        capacity = len(current['affinity_cpus'])
        stable_affinity = current['affinity_cpus'] == previous['affinity_cpus']
        windows.append({'elapsed_seconds': elapsed, 'cpu_seconds': cpu,
                        'cpu_core_equivalents': cpu / elapsed,
                        'affinity_capacity_cpu_count': capacity,
                        'affinity_capacity_fraction': cpu / elapsed / capacity if capacity and stable_affinity else None,
                        'ending_at': current.get('at'), 'affinity_stable': stable_affinity})
    elapsed = sum(window['elapsed_seconds'] for window in windows)
    cpu = sum(window['cpu_seconds'] for window in windows)
    affinity_sets = sorted({tuple(sample['affinity_cpus']) for sample in samples})
    stable_capacity = len(affinity_sets[0]) if len(affinity_sets) == 1 else None
    return {'samples': len(samples), 'valid_windows': len(windows), 'invalid_windows': invalid,
            'observed_elapsed_seconds': elapsed, 'process_cpu_seconds': cpu,
            'cpu_core_equivalents': cpu / elapsed if elapsed else None,
            'affinity_capacity_cpu_count': stable_capacity,
            'affinity_capacity_fraction': cpu / elapsed / stable_capacity if elapsed and stable_capacity else None,
            'affinity_cpu_sets': affinity_sets,
            'max_cpu_sample_window': max(windows, key=lambda window: window['cpu_core_equivalents']) if windows else None,
            'max_observed_rss_bytes': max((sample['current_rss_bytes'] for sample in samples if 'current_rss_bytes' in sample), default=None),
            'max_observed_thread_count': max((sample['thread_count'] for sample in samples if 'thread_count' in sample), default=None),
            'scope': 'Whole collector process including clients, policy HTTP service, warmups and diagnostic work.',
            'interpretation': 'CPU seconds cover all collector threads. Affinity is allowed CPU capacity, not exclusive ownership; these are not BE runnable-driver or wait metrics.'}


def summarize(directory):
    directory = Path(directory)
    collection = json.loads((directory / 'collection.json').read_text())
    if collection['status'] not in ('finished', 'finished_with_errors'):
        raise ValueError('only complete runs can be summarized')
    settings = collection['settings']
    expected_repetitions = settings['repetitions']
    queries = {query['id']: query for query in collection['queries']}
    raw = jsonl(directory / 'events.jsonl')
    formal = [event for event in raw if event.get('phase') == 'measured']
    grouped = defaultdict(list)
    for event in formal:
        if 'id' in event:
            grouped[(event['mode'], event['clients'], event['id'])].append(event)
    modes = settings['modes'].split(',')
    counts = sorted({row['clients'] for row in collection['rounds']})
    templates = []
    for mode in modes:
        for count in counts:
            for template, query in queries.items():
                events = grouped[(mode, count, template)]
                successful = [event for event in events if event['status'] == 'verified']
                # Multiple occurrences per concurrent round are intentional. Serial requires
                # exactly one successful slot per predeclared repetition.
                expected_slots = Counter()
                for row in collection['rounds']:
                    if row['mode'] == mode and row['clients'] == count and row['phase'] == 'measured':
                        for client, queue in enumerate(row['schedule']):
                            for sequence, qid in enumerate(queue):
                                if qid == template:
                                    expected_slots[(row['round'], client, sequence)] += 1
                actual_slots = Counter((event['round'], event['client'], event['sequence']) for event in successful)
                complete = (bool(expected_slots) and expected_slots == actual_slots and
                            len(events) == len(successful) and
                            len({slot[0] for slot in expected_slots}) == expected_repetitions)
                latency = [event['client_latency_ms'] for event in successful]
                templates.append({'mode': mode, 'clients': count, 'template_id': template,
                                  'split': query.get('split', 'unknown'),
                                  'evaluation_scope': ('in_sample' if query.get('split') == 'train' else
                                                       'held_out' if query.get('split') in ('valid', 'test') else 'unknown'),
                                  'family': query.get('family', query.get('benchmark', query.get('database', 'unknown'))),
                                  'expected': sum(expected_slots.values()), 'recorded': len(events),
                                  'verified': len(successful), 'complete': complete,
                                  'formal_latency_ms': distribution(latency),
                                  'median_ms': statistics.median(latency) if complete else None,
                                  'failures': Counter(event.get('error', 'unknown') for event in events if event['status'] != 'verified')})
    lookup = {(row['mode'], row['clients'], row['template_id']): row for row in templates}
    comparisons = []
    for count in counts:
        for mode in modes:
            if mode == 'native':
                continue
            available = []
            excluded = []
            for template in queries:
                baseline, candidate = lookup.get(('native', count, template)), lookup[(mode, count, template)]
                if baseline and baseline['complete'] and candidate['complete']:
                    available.append((baseline, candidate))
                else:
                    excluded.append(template)
            subsets = {'all': available}
            for split in ('train', 'valid', 'test'):
                subsets['split:' + split] = [pair for pair in available if pair[1]['split'] == split]
            for family in {row['family'] for row in templates}:
                subsets['family:' + family] = [pair for pair in available if pair[1]['family'] == family]
            for name, pairs in subsets.items():
                ratios = [base['median_ms'] / candidate['median_ms'] for base, candidate in pairs]
                comparisons.append({'mode': mode, 'clients': count, 'subset': name,
                                    'scope': 'in_sample' if name == 'split:train' else 'mixed' if name == 'all' or name.startswith('family:') else 'held_out',
                                    'paired_templates': len(pairs), 'excluded_unpaired_templates': excluded,
                                    'native_per_template_median_ms': distribution([base['median_ms'] for base, _ in pairs]),
                                    'method_per_template_median_ms': distribution([candidate['median_ms'] for _, candidate in pairs]),
                                    'paired_speedup': distribution(ratios),
                                    'geomean_speedup': math.exp(statistics.mean(map(math.log, ratios))) if ratios else None,
                                    'wins': sum(candidate['median_ms'] <= base['median_ms'] * .99 for base, candidate in pairs),
                                    'regressions': sum(candidate['median_ms'] >= base['median_ms'] * 1.01 for base, candidate in pairs),
                                    'ties': sum(base['median_ms'] * .99 < candidate['median_ms'] < base['median_ms'] * 1.01 for base, candidate in pairs)})
    rounds = []
    for row in collection['rounds']:
        if row['phase'] != 'measured':
            continue
        selected = [event for event in formal if event['mode'] == row['mode'] and
                    event['clients'] == row['clients'] and event['round'] == row['round'] and 'id' in event]
        rounds.append({**row, 'formal_latency_ms': distribution([event['client_latency_ms'] for event in selected if event['status'] == 'verified']),
                       'complete': row['failed_or_missing'] == 0 and row['executed'] == row['expected']})
    throughput = []
    by_round = {(row['mode'], row['clients'], row['round']): row for row in rounds}
    for count in counts:
        for mode in modes:
            if mode == 'native':
                continue
            pairs, latency_pairs, p90_pairs = [], [], []
            excluded = []
            for repetition in range(expected_repetitions):
                base = by_round.get(('native', count, repetition))
                candidate = by_round.get((mode, count, repetition))
                if base and candidate and base['complete'] and candidate['complete'] and base['schedule'] == candidate['schedule']:
                    pairs.append((candidate['verified_queries_per_second'], base['verified_queries_per_second']))
                    latency_pairs.append((base['formal_latency_ms']['mean'], candidate['formal_latency_ms']['mean']))
                    p90_pairs.append((base['formal_latency_ms']['p90'], candidate['formal_latency_ms']['p90']))
                else:
                    excluded.append(repetition)
            throughput.append({'mode': mode, 'clients': count, 'excluded_unpaired_rounds': excluded,
                               'mean_latency_speedup': bootstrap_paired(latency_pairs),
                               'p90_latency_speedup': bootstrap_paired(p90_pairs),
                               **bootstrap_paired(pairs)})
    formal_ids = {event['query_id'] for event in formal if 'query_id' in event}
    decision_records = [record for record in jsonl(directory / 'decisions.jsonl') if record.get('query_id') in formal_ids]
    policy = []
    for mode in modes:
        selected = [event for event in formal if event.get('mode') == mode and 'id' in event and
                    (event.get('autodop_enabled') or (mode != 'native' and not mode.startswith('uniform')))]
        records = [record for record in decision_records if record['mode'] == mode]
        if not selected and not records:
            continue
        applied = sum(event.get('dop_validation', {}).get('applied', False) for event in selected)
        fallback = [event['trace']['skip_reason'] for event in selected if event.get('trace', {}).get('skip_reason')]
        policy.append({'mode': mode, 'formal_autodop_executions': len(selected), 'applied': applied,
                       'failed_executions': sum(event['status'] != 'verified' for event in selected),
                       'unknown_application_evidence': sum('dop_validation' not in event for event in selected),
                       'applied_fraction': applied / len(selected) if selected else None,
                       'fallbacks': Counter(fallback), 'fallback_fraction': len(fallback) / len(selected) if selected else None,
                       'fallback_categories': Counter(event.get('dop_validation', {}).get('fallback_category', 'unknown')
                                                      for event in selected if event.get('trace', {}).get('skip_reason')),
                       'response_confidence': distribution([record['response']['confidence'] for record in records if 'response' in record]),
                       'server_decision_ms': distribution([record['decision_ms'] for record in records]),
                       'fe_decision_latency_ms': distribution([event['trace']['auto_dop']['decision_latency_ms'] for event in selected if 'decision_latency_ms' in event.get('trace', {}).get('auto_dop', {})]),
                       'server_errors': Counter(record['error'] for record in records if 'error' in record)})
    inventory = {}
    resource_errors = Counter()
    # Preserve exact exporter metric identity and HELP/TYPE; no inferred driver/wait proxies.
    for sample in iter_jsonl(directory / 'resources.jsonl'):
        if 'error' in sample:
            resource_errors[(sample['url'], sample['error'])] += 1
            continue
        for line in sample.get('prometheus_raw', '').splitlines():
            meta = re.match(r'^# (HELP|TYPE) ([A-Za-z_:][A-Za-z0-9_:]*) (.*)$', line)
            if meta:
                inventory.setdefault(meta[2], {})[meta[1].lower()] = meta[3]
                continue
            metric = re.match(r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+([^\s]+)', line)
            if metric:
                entry = inventory.setdefault(metric[1], {})
                entry['sample_lines'] = entry.get('sample_lines', 0) + 1
                entry.setdefault('sources', set()).add(sample['url'])
    for entry in inventory.values():
        if 'sources' in entry:
            entry['sources'] = sorted(entry['sources'])
    profiles = [{'file': str(path.resolve()), 'bytes': path.stat().st_size} for path in sorted((directory / 'profiles').glob('*.txt'))]
    mode_summaries = []
    for mode in modes:
        for count in counts:
            selected = [row for row in templates if row['mode'] == mode and row['clients'] == count]
            mode_summaries.append({'mode': mode, 'clients': count,
                                   'per_template_median_ms': distribution([row['median_ms'] for row in selected if row['complete']]),
                                   'incomplete_templates': [row['template_id'] for row in selected if not row['complete']],
                                   'warning': 'Standalone summaries use complete templates only; compare methods using paired_comparisons.'})
    return {'source': str(directory.resolve()), 'status': collection['status'], 'settings': settings,
            'formal_events': len(formal), 'warmup_events_excluded': sum(e.get('phase') == 'warmup' for e in raw),
            'failure_events': [event for event in formal if event.get('status') != 'verified'],
            'templates': templates, 'mode_summaries': mode_summaries, 'paired_comparisons': comparisons, 'rounds': rounds,
            'paired_throughput': throughput, 'policy': policy, 'metric_inventory': inventory,
            'collector_resources': summarize_collector_resources(jsonl(directory / 'collector_resources.jsonl')),
            'resource_errors': [{'url': key[0], 'error': key[1], 'count': count} for key, count in resource_errors.items()],
            'profiles': profiles, 'limitations': ['Latency percentiles summarize the stated population, not censored timeout durations.',
                'Failed or missing repetitions invalidate complete-template and complete-round comparisons.',
                'Resource inventory retains exporter names and descriptions; no CPU-to-driver or wait-time proxy is inferred.',
                'Profiles remain raw evidence; no undefined wait percentage is reported.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', nargs='+', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise ValueError('refusing to overwrite summary')
    result = {'runs': [summarize(path) for path in args.runs]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
