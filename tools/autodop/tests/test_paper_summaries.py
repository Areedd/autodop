import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from summarize_paper_evaluations import bootstrap_paired, summarize


def test_formal_pairing_rejects_failed_template_and_round(tmp_path):
    rounds, events = [], []
    for mode in ('native', 'policy_static'):
        for phase, repetitions in (('warmup', 1), ('measured', 5)):
            for repetition in range(repetitions):
                failed = mode == 'policy_static' and phase == 'measured' and repetition == 2
                rounds.append({'mode': mode, 'clients': 1, 'phase': phase, 'round': repetition,
                               'expected': 2, 'executed': 2, 'failed_or_missing': int(failed),
                               'verified_queries_per_second': 1 if mode == 'native' else 2,
                               'schedule': [['good', 'bad']]})
                for sequence, template in enumerate(('good', 'bad')):
                    event_failed = failed and template == 'bad'
                    events.append({'mode': mode, 'clients': 1, 'client': 0, 'sequence': sequence,
                                   'phase': phase, 'round': repetition, 'id': template,
                                   'status': 'failed' if event_failed else 'verified',
                                   'client_latency_ms': 999999 if phase == 'warmup' else 100 if mode == 'native' else 50,
                                   **({'error': 'TimeoutError: deadline'} if event_failed else {})})
    (tmp_path / 'collection.json').write_text(json.dumps({'status': 'finished_with_errors',
        'settings': {'modes': 'native,policy_static', 'repetitions': 5}, 'rounds': rounds,
        'queries': [{'id': 'good', 'split': 'test'}, {'id': 'bad', 'split': 'train'}]}))
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
    report = summarize(tmp_path)
    assert report['warmup_events_excluded'] == 4
    assert len(report['failure_events']) == 1
    comparison = next(row for row in report['paired_comparisons'] if row['subset'] == 'all')
    assert comparison['paired_templates'] == 1
    assert comparison['excluded_unpaired_templates'] == ['bad']
    assert comparison['paired_speedup']['median'] == 2
    assert comparison['method_per_template_median_ms']['p90'] == 50
    assert report['paired_throughput'][0]['paired_rounds'] == 4
    assert report['paired_throughput'][0]['excluded_unpaired_rounds'] == [2]


def test_bootstrap_resamples_round_pairs_not_individual_queries():
    result = bootstrap_paired([(2, 1), (4, 2), (6, 3)], iterations=100)
    assert result['ratio'] == 2
    assert result['ci95'] == [2, 2]
    assert result['paired_rounds'] == 3
    assert 'few' in result['limitation']
    assert bootstrap_paired([])['ci95'] is None


def test_collector_cpu_uses_elapsed_windows_and_affinity_capacity():
    from summarize_paper_evaluations import summarize_collector_resources
    samples = [{'monotonic_seconds': 1, 'user_cpu_seconds': 2, 'system_cpu_seconds': 1,
                'affinity_cpus': [27, 28], 'current_rss_bytes': 100, 'thread_count': 2},
               {'monotonic_seconds': 3, 'user_cpu_seconds': 4, 'system_cpu_seconds': 2,
                'affinity_cpus': [27, 28], 'current_rss_bytes': 120, 'thread_count': 4},
               {'monotonic_seconds': 7, 'user_cpu_seconds': 6, 'system_cpu_seconds': 2,
                'affinity_cpus': [27, 28], 'current_rss_bytes': 110, 'thread_count': 3}]
    result = summarize_collector_resources(samples)
    assert result['observed_elapsed_seconds'] == 6
    assert result['process_cpu_seconds'] == 5
    assert result['cpu_core_equivalents'] == 5 / 6
    assert result['affinity_capacity_fraction'] == 5 / 12
    assert result['max_cpu_sample_window']['cpu_core_equivalents'] == 1.5
    assert result['max_observed_rss_bytes'] == 120
    assert result['max_observed_thread_count'] == 4
    assert summarize_collector_resources([])['cpu_core_equivalents'] is None
