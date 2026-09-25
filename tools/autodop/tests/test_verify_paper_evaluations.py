import hashlib
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_paper_evaluations import verify


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value))


def fixture(tmp_path):
    for name in ('sql', 'raw', 'profiles'):
        (tmp_path / name).mkdir()
    sqlfile = tmp_path / 'sql/q.sql'
    sqlfile.write_text('select 1')
    query = {'id': 'q', 'sql_sha256': sha(sqlfile), 'database': 'db', 'split': 'test',
             'native_result_digest': {'sha256': 'result', 'rows': 1}}
    write(tmp_path / 'manifest.json', {'queries': [query]})
    write(tmp_path / 'trusted.json', {'status': 'finished', 'queries': [dict(query, status='accepted')]})
    events, sql, traces, decisions, rounds = [], [], [], [], []
    for phase in ('warmup', 'measured'):
        qid = phase + '-id'
        request = {'query_id': qid, 'fragments': [{'fragment_id': 'F0', 'candidate_dops': [1, 2]}]}
        response = {'query_id': qid, 'decisions': [{'fragment_id': 'F0', 'dop': 2}]}
        trace = {'query_id': qid, 'state': 'EOF', 'request': request, 'response': response,
                 'auto_dop': {'installed_dop': {'F0': 2}},
                 'actual_fragments': [{'fragment_id': 'F0', 'plan_dop': 2, 'instance_dops': [1, 2]}]}
        event = {'mode': 'policy_static', 'clients': 1, 'phase': phase, 'round': 0, 'client': 0,
                 'sequence': 0, 'id': 'q', 'query_id': qid, 'status': 'verified', 'autodop_enabled': True,
                 'result_digest': query['native_result_digest'], 'sql_sha256': query['sql_sha256'], 'database': 'db'}
        sql.append(dict(event))
        profile = tmp_path / 'profiles' / (qid + '.txt')
        profile.write_text('Query:\n  - Query ID: ' + qid + '\n')
        event.update(trace=trace, dop_validation={'applied': True}, profile_file=str(profile),
                     profile_sha256=sha(profile), profile_bytes=profile.stat().st_size)
        events.append(event)
        traces.append(trace)
        decisions.append({'query_id': qid, 'mode': 'policy_static', 'request': request, 'response': response})
        rounds.append({'mode': 'policy_static', 'clients': 1, 'phase': phase, 'round': 0,
                       'schedule': [['q']], 'expected': 1, 'executed': 1, 'verified': 1, 'failed_or_missing': 0})
    settings = {'manifest': str(tmp_path / 'manifest.json'), 'trusted_collection': str(tmp_path / 'trusted.json'),
                'modes': 'policy_static', 'scenario': 'profiles', 'warmup': 1, 'repetitions': 1, 'seed': 0}
    collection = {'status': 'finished', 'settings': settings, 'manifest_sha256': sha(tmp_path / 'manifest.json'),
                  'trusted_collection_sha256': sha(tmp_path / 'trusted.json'), 'queries': [query], 'rounds': rounds}
    write(tmp_path / 'collection.json', collection)
    for name, rows in [('events.jsonl', events), ('sql_executions.jsonl', sql),
                       ('decisions.jsonl', decisions), ('raw/autodop_trace_x.jsonl', traces)]:
        (tmp_path / name).write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return collection, events, sql


def test_complete_profile_sql_trace_decision_reconciliation(tmp_path):
    fixture(tmp_path)
    result = verify(tmp_path)
    assert result['all_expected_executions_verified']
    assert result['formal_slots'] == result['warmup_slots'] == 1
    assert result['verified_profiles'] == result['verified_applied_executions'] == 2


@pytest.mark.parametrize('mutation', ['profile', 'trace', 'duplicate', 'upgrade', 'digest'])
def test_mutated_evidence_rejected(tmp_path, mutation):
    _, events, sql = fixture(tmp_path)
    if mutation == 'profile':
        Path(events[0]['profile_file']).write_text('corrupt')
    elif mutation == 'trace':
        events[0]['trace']['actual_fragments'][0]['plan_dop'] = 1
    elif mutation == 'duplicate':
        events.append(events[0])
    elif mutation == 'upgrade':
        sql[0]['status'] = 'failed'
    else:
        events[0]['result_digest'] = sql[0]['result_digest'] = {'sha256': 'bad', 'rows': 1}
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
    (tmp_path / 'sql_executions.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in sql))
    with pytest.raises(ValueError):
        verify(tmp_path)


def test_sql_verified_may_be_downgraded_without_certifying_failure(tmp_path):
    collection, events, _ = fixture(tmp_path)
    events[1].update(status='failed', error='intervention verification failed')
    collection['status'] = 'finished_with_errors'
    collection['rounds'][1].update(verified=0, failed_or_missing=1)
    write(tmp_path / 'collection.json', collection)
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
    result = verify(tmp_path)
    assert result['evidence_valid'] and not result['all_expected_executions_verified']
    assert result['failed_slots'] == 1
