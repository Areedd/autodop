import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from assess_query_uniform_target import freeze, score, stability


def fixture_collection(path, times, *, scoring=False, fallback=False):
    path.mkdir()
    manifest = {'queries': [{'id': 'q', 'database': 'db', 'sql_sha256': 'sql-digest',
                             'split': 'train', 'template_family': 'rank'}], 'contract': {}}
    manifest_path = path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    collection = {'status': 'finished', 'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  'settings': {'manifest': str(manifest_path), 'repetitions': 3},
                  'started_at': '2100-01-01T00:00:00+00:00' if scoring else '2000-01-01T00:00:00+00:00',
                  'finished_at': '2100-01-02T00:00:00+00:00' if scoring else '2000-01-02T00:00:00+00:00'}
    (path / 'collection.json').write_text(json.dumps(collection))
    events = [{'mode': mode, 'id': 'q', 'round': i, 'phase': 'measured', 'clients': 1,
               'client_latency_ms': latency, 'sql_sha256': 'sql-digest', 'status': 'verified',
               'query_id': f'{path.name}-{mode}-{i}',
               'dop_validation': {'applied': mode == 'policy_static' and not fallback}}
              for mode, latency in times.items() for i in range(3)]
    (path / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    verification = {'all_expected_executions_verified': True, 'evidence_valid': True,
                    'source_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in (manifest_path, path / 'collection.json', path / 'events.jsonl')}}
    (path / 'independent_verification.json').write_text(json.dumps(verification))


def test_calibration_choice_survives_opposite_scoring_optimum(tmp_path):
    calibration, scored = tmp_path / 'calibration', tmp_path / 'scored'
    fixture_collection(calibration, {'uniform1': 20, 'uniform2': 15, 'uniform4': 10, 'uniform8': 12})
    fixture_collection(scored, {'native': 25, 'uniform1': 20, 'uniform2': 15,
                                'uniform4': 10, 'uniform8': 5, 'policy_static': 7}, scoring=True)
    selection = tmp_path / 'selection.json'
    selection.write_text(json.dumps(freeze(calibration)))
    result = score(selection, scored)
    assert result['selected_dops'] == {'q': 4}
    assert result['retrospective_oracle_dops'] == {'q': 8}
    assert result['comparisons']['query_uniform_selected']['latency_reduction']['mean'] == pytest.approx(.3)
    assert result['comparisons']['query_uniform_oracle']['latency_reduction']['mean'] == pytest.approx(-.4)
    assert result['primary_observed_target_met']
    assert not result['eligible_for_heldout_target_claim']


def test_arbitrary_selection_and_scoring_overlap_rejected(tmp_path):
    calibration, scored = tmp_path / 'calibration', tmp_path / 'scored'
    times = {'uniform1': 20, 'uniform2': 15, 'uniform4': 10, 'uniform8': 12}
    fixture_collection(calibration, times)
    fixture_collection(scored, {**times, 'native': 25, 'policy_static': 7}, scoring=True)
    frozen = freeze(calibration)
    selection = tmp_path / 'selection.json'
    frozen['queries']['q']['dop'] = 8
    selection.write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match='calibration minimum'):
        score(selection, scored)
    frozen = freeze(calibration)
    frozen['created_at'] = '2101-01-01T00:00:00+00:00'
    selection.write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match='before scoring'):
        score(selection, scored)


def test_incomplete_uniform_domain_and_tampered_sources_rejected(tmp_path):
    missing = tmp_path / 'missing'
    fixture_collection(missing, {'uniform1': 20, 'uniform2': 15, 'uniform4': 10})
    with pytest.raises(ValueError, match='every required mode'):
        freeze(missing)
    (missing / 'events.jsonl').write_text('')
    with pytest.raises(ValueError, match='source changed'):
        freeze(missing)


def test_fallback_cannot_establish_learned_target(tmp_path):
    calibration, scored = tmp_path / 'calibration', tmp_path / 'scored'
    times = {'uniform1': 20, 'uniform2': 15, 'uniform4': 10, 'uniform8': 12}
    fixture_collection(calibration, times)
    fixture_collection(scored, {**times, 'native': 25, 'policy_static': 7}, scoring=True, fallback=True)
    selection = tmp_path / 'selection.json'
    selection.write_text(json.dumps(freeze(calibration)))
    result = score(selection, scored)
    assert not result['all_policy_decisions_applied']
    assert not result['primary_observed_target_met']


def test_query_cluster_stability_requires_repeatable_improvement():
    queries = [f'q{i}' for i in range(24)]
    events = [{'id': qid, 'mode': mode, 'round': i, 'phase': 'measured', 'client_latency_ms': latency}
              for qid in queries for i in range(3)
              for mode, latency in [('policy_static', 7), ('query_uniform_selected', 10)]]
    result = stability(queries, events, 'query_uniform_selected', 3, bootstrap_samples=200)
    assert result['stable_mean_advantage']
    assert result['paired_query_bootstrap_mean_reduction_ci95'] == pytest.approx([.3, .3])
    for event in events:
        if event['mode'] == 'policy_static' and event['round'] == 2:
            event['client_latency_ms'] = 11
    result = stability(queries, events, 'query_uniform_selected', 3, bootstrap_samples=200)
    assert result['paired_query_bootstrap_mean_reduction_ci95'][0] > 0
    assert not result['stable_mean_advantage']


def test_extended_uniform_domain_includes_sixteen_in_selection_and_scoring(tmp_path):
    calibration, scored = tmp_path / 'calibration', tmp_path / 'scored'
    times = {'uniform1': 25, 'uniform2': 20, 'uniform4': 15, 'uniform8': 10, 'uniform16': 5}
    fixture_collection(calibration, times)
    fixture_collection(scored, {**times, 'uniform16': 12, 'native': 25, 'policy_static': 7}, scoring=True)
    selection = tmp_path / 'selection.json'
    selection.write_text(json.dumps(freeze(calibration, max_dop=16)))
    result = score(selection, scored)
    assert result['query_dop_domain'] == [1, 2, 4, 8, 16]
    assert result['selected_dops'] == {'q': 16}
    assert result['retrospective_oracle_dops'] == {'q': 8}
    assert result['comparisons']['query_uniform_selected']['latency_reduction']['mean'] == pytest.approx(1-7/12)


def test_extended_uniform_domain_cannot_omit_sixteen(tmp_path):
    fixture_collection(tmp_path / 'calibration', {'uniform1': 25, 'uniform2': 20, 'uniform4': 15, 'uniform8': 10})
    with pytest.raises(ValueError, match='every required mode'):
        freeze(tmp_path / 'calibration', max_dop=16)
