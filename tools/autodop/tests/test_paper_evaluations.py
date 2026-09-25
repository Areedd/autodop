import json
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collect_paper_evaluations import RandomEngine, VectorEngine, load_queries, workload


def test_profile_detail_rejected_before_resources_for_timing_runs():
    from types import SimpleNamespace
    import pytest
    from collect_paper_evaluations import collect
    for scenario, level in [('serial', 2), ('concurrent', 2), ('profiles', 3), ('profiles', True)]:
        with pytest.raises(ValueError, match='only valid for the profiles scenario'):
            collect(SimpleNamespace(scenario=scenario, pipeline_profile_level=level))


def test_closed_loop_schedule_equal_modes_and_all_queries():
    queries = [{'id': str(i)} for i in range(36)]
    first = workload(queries, 123, 0, 40)
    assert first == workload(queries, 123, 0, 40)
    assert all(first)
    assert sorted(q['id'] for queue in first for q in queue) == sorted(q['id'] for q in queries * 2)
    assert first != workload(queries, 123, 1, 40)


def request():
    return {'query_id': 'a', 'fragments': [
        {'fragment_id': 'F00', 'fragment_index': 0, 'candidate_dops': [1, 2, 4], 'is_gather_root': True},
        {'fragment_id': 'F01', 'fragment_index': 1, 'candidate_dops': [1, 2, 4]}], 'edges': []}


def test_random_ignores_query_uuid_and_restricts_gather():
    engine = RandomEngine(12)
    req = request()
    first = engine.decide(req)
    req['query_id'] = 'other'
    second = engine.decide(req)
    assert first['decisions'] == second['decisions']
    assert first['decisions'][0]['dop'] == 1


def test_vector_maps_fragment_index_not_ephemeral_id():
    req = request()
    engine = VectorEngine({'q': {'request': req, 'fragment_dops': {'F00': 1, 'F01': 4}}})
    req['fragments'][0]['fragment_id'] = 'new0'
    req['fragments'][1]['fragment_id'] = 'new1'
    assert engine.decide(req)['decisions'][1] == {'fragment_id': 'new1', 'dop': 4, 'reason': 'fixed'}


def test_vector_plan_variants_reject_unseen_and_conflicting_plans():
    from copy import deepcopy
    import pytest
    first = request()
    second = deepcopy(first)
    second['fragments'].append({'fragment_id': 'F02', 'fragment_index': 2, 'candidate_dops': [1, 2, 4]})
    variant = {'request': second, 'fragment_dops': {'F00': 1, 'F01': 2, 'F02': 4}}
    entry = {'request': first, 'fragment_dops': {'F00': 1, 'F01': 4}, 'alternatives': [variant]}
    engine = VectorEngine({'q': entry})
    assert [x['dop'] for x in engine.decide(first)['decisions']] == [1, 4]
    assert [x['dop'] for x in engine.decide(second)['decisions']] == [1, 2, 4]
    unseen = deepcopy(second)
    unseen['fragments'][1]['estimated_cardinality'] = 123
    with pytest.raises(KeyError):
        engine.decide(unseen)
    conflict = deepcopy(variant)
    conflict['fragment_dops']['F01'] = 4
    entry['alternatives'].append(conflict)
    with pytest.raises(ValueError, match='ambiguous'):
        VectorEngine({'q': entry})


def test_manifest_requires_trusted_native_identity(tmp_path):
    sql = tmp_path / 'q.sql'
    sql.write_text('select 1')
    query = {'id': 'q', 'sql_file': str(sql), 'sql_sha256': hashlib.sha256(sql.read_bytes()).hexdigest(),
             'database': 'db', 'split': 'test'}
    manifest, trusted = tmp_path / 'manifest.json', tmp_path / 'collection.json'
    manifest.write_text(json.dumps({'queries': [query]}))
    trusted.write_text(json.dumps({'status': 'finished', 'queries': [dict(query, status='accepted',
                                  native_result_digest={'sha256': 'x', 'rows': 1})]}))
    assert load_queries(manifest, trusted)[0]['native_result_digest']['rows'] == 1
    sql.write_text('select 2')
    import pytest
    with pytest.raises(ValueError, match='identity'):
        load_queries(manifest, trusted)


def test_applied_trace_identity_bounds_and_heterogeneous_caps():
    from copy import deepcopy
    import pytest
    from collect_paper_evaluations import validate_execution_trace
    from test_trusted_dataset import trace
    record = trace({'0': 4, '1': 1})
    record['actual_fragments'][0]['instance_dops'] = [1, 2, 4]
    checked = validate_execution_trace(record, 'query-1')
    assert checked['applied']
    assert checked['actual']['0']['heterogeneous']
    assert checked['actual']['0']['capped']
    for mutation in ('identity', 'plan', 'bounds', 'missing'):
        changed = deepcopy(record)
        if mutation == 'identity':
            changed['request']['query_id'] = 'wrong'
        elif mutation == 'plan':
            changed['actual_fragments'][0]['plan_dop'] = 2
        elif mutation == 'bounds':
            changed['actual_fragments'][0]['instance_dops'] = [8]
        else:
            changed['actual_fragments'].pop()
        with pytest.raises(ValueError):
            validate_execution_trace(changed, 'query-1')
    fallback = {'query_id': 'query-1', 'state': 'EOF', 'skip_reason': 'confidence too low'}
    assert not validate_execution_trace(fallback, 'query-1')['applied']


def test_policy_variant_relative_checkpoint_hash_and_boolean(tmp_path):
    import pytest
    from collect_paper_evaluations import load_policy_variants
    checkpoint = tmp_path / 'model.pt'
    checkpoint.write_bytes(b'frozen checkpoint')
    config = tmp_path / 'policies.json'
    config.write_text(json.dumps({'no_comp': {'checkpoint': 'model.pt', 'load_correction': False}}))
    variants = load_policy_variants(config)
    assert variants['no_comp']['checkpoint'] == str(checkpoint)
    assert variants['no_comp']['checkpoint_sha256'] == hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    assert variants['no_comp']['load_correction'] is False
    config.write_text(json.dumps({'bad': {'checkpoint': 'model.pt', 'load_correction': 'false'}}))
    with pytest.raises(ValueError, match='boolean'):
        load_policy_variants(config)


def test_offline_baselines_reject_native_fallback_and_wrong_installed_vector():
    import pytest
    from collect_paper_evaluations import verify_offline_intervention, validate_execution_trace
    from test_trusted_dataset import trace
    fallback = {'query_id': 'query-1', 'state': 'EOF', 'skip_reason': 'HTTP 500'}
    checked = validate_execution_trace(fallback, 'query-1')
    with pytest.raises(ValueError, match='not installed'):
        verify_offline_intervention(fallback, checked, 'random')
    record = trace({'0': 4})
    record['response'] = {'query_id': 'query-1', 'decisions': [{'fragment_id': '0', 'dop': 4}]}
    checked = validate_execution_trace(record, 'query-1')
    verify_offline_intervention(record, checked, 'random')
    record['response']['decisions'][0]['dop'] = 2
    with pytest.raises(ValueError, match='differs'):
        verify_offline_intervention(record, checked, 'random')
    vector = VectorEngine({'q': {'request': record['request'], 'fragment_dops': {'0': 2}}})
    with pytest.raises(ValueError, match='differs'):
        verify_offline_intervention(record, checked, 'GT_reg', vector)


def test_profile_authentication_retry_and_identity(tmp_path):
    from io import BytesIO
    from unittest.mock import patch
    from urllib.error import HTTPError
    from collect_paper_evaluations import fetch_profile
    profile = b'Query:\n  Summary:\n   - Query ID: exact-query\n   - Query State: Finished\n'
    pending = HTTPError('http://localhost', 404, 'not found', {}, None)
    with patch('collect_paper_evaluations.urlopen', side_effect=[pending, BytesIO(profile)]) as opened, \
            patch('collect_paper_evaluations.time.sleep'):
        evidence = fetch_profile('http://localhost:8030', 'exact-query', tmp_path / 'profile.txt')
    assert opened.call_count == 2
    assert opened.call_args.args[0].get_header('Authorization') == 'Basic cm9vdDo='
    assert evidence['profile_fetch_attempts'] == 2
    assert evidence['profile_sha256'] == hashlib.sha256(profile).hexdigest()
    assert (tmp_path / 'profile.txt').read_bytes() == profile


def test_profile_rejects_auth_errors_html_and_wrong_query(tmp_path):
    from io import BytesIO
    from unittest.mock import patch
    from urllib.error import HTTPError
    import pytest
    from collect_paper_evaluations import fetch_profile
    with patch('collect_paper_evaluations.urlopen', side_effect=HTTPError('url', 401, 'denied', {}, None)) as opened:
        with pytest.raises(HTTPError):
            fetch_profile('http://localhost', 'wanted', tmp_path / 'bad.txt')
        assert opened.call_count == 1
    for body in (b'', b'<html><body> - Query ID: wanted</body></html>',
                 b'Query:\n - Query ID: wrong\n', b'{"status":"error","msg":"wanted"}'):
        with patch('collect_paper_evaluations.urlopen', return_value=BytesIO(body)):
            with pytest.raises(ValueError):
                fetch_profile('http://localhost', 'wanted', tmp_path / 'bad.txt')
    assert not (tmp_path / 'bad.txt').exists()
    with patch('collect_paper_evaluations.urlopen', return_value=BytesIO(b'{"Query":{"Summary":{"Query ID":"wanted"}}}')):
        assert fetch_profile('http://localhost', 'wanted', tmp_path / 'json.txt')['profile_bytes'] > 0


def test_profile_retry_deadline_is_bounded(tmp_path):
    from unittest.mock import patch
    from urllib.error import HTTPError
    import pytest
    from collect_paper_evaluations import fetch_profile
    with patch('collect_paper_evaluations.urlopen', side_effect=HTTPError('url', 404, 'pending', {}, None)) as opened, \
            patch('collect_paper_evaluations.time.monotonic', side_effect=[0, 0, .5, 1.1]), \
            patch('collect_paper_evaluations.time.sleep'):
        with pytest.raises(TimeoutError):
            fetch_profile('http://localhost', 'wanted', tmp_path / 'missing.txt', timeout_s=1)
        assert opened.call_count == 1


def test_collector_resource_sample_reports_process_fields():
    from collect_paper_evaluations import collector_resource_sample
    sample = collector_resource_sample()
    assert sample['monotonic_seconds'] > 0
    assert sample['user_cpu_seconds'] >= 0 and sample['system_cpu_seconds'] >= 0
    assert sample['affinity_cpus']
    assert sample['max_rss_unit'] == 'KiB'
    assert sample['current_rss_bytes'] > 0
    assert sample['thread_count'] >= 1


def test_template_vector_dispatch_disambiguates_features_and_postround_mapping(tmp_path):
    from copy import deepcopy
    import pytest
    from collect_paper_evaluations import (Journal, template_vector_engines, select_engine,
                                          verify_offline_intervention, validate_execution_trace)
    from test_trusted_dataset import trace
    req = trace({'0': 1, '1': 1})['request']
    entries = {'Q42': {'request': req, 'fragment_dops': {'0': 1, '1': 2}},
               'Q52': {'request': req, 'fragment_dops': {'0': 1, '1': 4}}}
    engines = {'GT_pair': template_vector_engines(entries, Journal(tmp_path / 'decisions.jsonl'), 'GT_pair')}
    first = select_engine(engines, 'GT_pair', 'Q42')
    last = select_engine(engines, 'GT_pair', 'Q52')
    assert first.decide(req)['decisions'][1]['dop'] == 2
    assert last.decide(req)['decisions'][1]['dop'] == 4
    actual = trace({'0': 1, '1': 2})
    verify_offline_intervention(actual, validate_execution_trace(actual, 'query-1'), 'GT_pair',
                                select_engine(engines, 'GT_pair', 'Q42').engine)
    with pytest.raises(ValueError, match='differs'):
        verify_offline_intervention(actual, validate_execution_trace(actual, 'query-1'), 'GT_pair', last.engine)
    changed = deepcopy(req)
    changed['fragments'][1]['candidate_dops'] = [1, 2]
    with pytest.raises(KeyError):
        first.decide(changed)


def test_concurrent_fixed_vector_dispatch_rejected_but_policy_allowed():
    import pytest
    from collect_paper_evaluations import validate_vector_scenario
    with pytest.raises(ValueError, match='SQL-specific'):
        validate_vector_scenario('concurrent', ['native', 'GT_pair'], {'GT_pair': {}})
    validate_vector_scenario('serial', ['GT_pair'], {'GT_pair': {}})
    validate_vector_scenario('profiles', ['GT_pair'], {'GT_pair': {}})
    validate_vector_scenario('concurrent', ['policy_static', 'policy_load'], {'GT_pair': {}})
