#!/usr/bin/env python3
"""Independently reconcile completed SQL, decision, trace and profile evidence (no network)."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autodop.data import sample_from_trace


def records(path):
    if path.exists():
        with path.open() as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def slot(event):
    return tuple(event[key] for key in ('mode', 'clients', 'phase', 'round', 'client', 'sequence'))


def verify_profile(body, query_id):
    text = body.decode('utf-8')
    require(bool(text.strip()) and not re.search(r'<\s*(?:html|!doctype\s+html)\b', text, re.I), 'invalid profile text')
    if text.lstrip().startswith('{'):
        def find(node):
            if isinstance(node, dict):
                return ([node['Query ID']] if 'Query ID' in node else []) + [value for child in node.values() for value in find(child)]
            if isinstance(node, list):
                return [value for child in node for value in find(child)]
            return []
        identities = find(json.loads(text))
    else:
        identities = re.findall(r'^\s*- Query ID:\s*(\S+)\s*$', text, re.M)
    require(bool(identities) and all(value == query_id for value in identities), 'profile Query ID mismatch')


def verify_trace(trace, query_id):
    require(trace.get('query_id') == query_id and trace.get('state') == 'EOF', 'trace identity/state mismatch')
    request = trace.get('request')
    if request:
        require(request.get('query_id') == query_id, 'trace request identity mismatch')
    if trace.get('skip_reason'):
        return False
    require(bool(request), 'applied trace missing request')
    installed = trace.get('auto_dop', {}).get('installed_dop', {})
    fragments = {str(fragment['fragment_id']): fragment for fragment in request['fragments']}
    require(bool(installed) and set(installed) == set(fragments), 'installed fragment coverage mismatch')
    seen = set()
    for fragment in trace.get('actual_fragments', []):
        fid = str(fragment['fragment_id'])
        require(fid in installed and fid not in seen, 'actual fragment identity mismatch')
        seen.add(fid)
        require(installed[fid] in fragments[fid]['candidate_dops'] and fragment['plan_dop'] == installed[fid], 'actual plan DOP mismatch')
        values = fragment.get('instance_dops', [])
        require(bool(values) and all(type(value) is int and 1 <= value <= installed[fid] for value in values), 'instance DOP bounds mismatch')
    require(seen == set(installed), 'actual fragment coverage mismatch')
    response = trace.get('response')
    require(bool(response) and response.get('query_id') == query_id, 'applied response identity mismatch')
    decisions = response.get('decisions', [])
    vector = {str(item['fragment_id']): item['dop'] for item in decisions}
    require(len(vector) == len(decisions) and vector == installed, 'response/installed vector mismatch')
    return True


def request_plan_key(request):
    installed = {str(fragment['fragment_id']): min(fragment['candidate_dops'])
                 for fragment in request['fragments']}
    return sample_from_trace({'request': request, 'auto_dop': {'installed_dop': installed},
                              'latency_ms': 1})['plan_key']


def verify(directory):
    directory = Path(directory).resolve()
    inventory = {}

    def source(path, expected=None):
        path = Path(path).resolve()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        value = digest.hexdigest()
        inventory[str(path)] = value
        require(expected is None or value == expected, 'source checksum mismatch: ' + str(path))
        return path

    collection = json.loads(source(directory / 'collection.json').read_text())
    require(collection['status'] in ('finished', 'finished_with_errors'), 'collection is not complete')
    settings = collection['settings']
    manifest = json.loads(source(settings['manifest'], collection['manifest_sha256']).read_text())
    trusted = json.loads(source(settings['trusted_collection'], collection['trusted_collection_sha256']).read_text())
    require(trusted['status'] == 'finished', 'trusted collection is not finished')
    native = {query['id']: query for query in trusted['queries'] if query['status'] == 'accepted'}
    queries = {query['id']: query for query in collection['queries']}
    require(len(queries) == len(collection['queries']), 'duplicate selected template')
    require(set(queries) == {query['id'] for query in manifest['queries']}, 'selection/manifest coverage mismatch')
    manifest_queries = {query['id']: query for query in manifest['queries']}
    for qid, query in queries.items():
        require(qid in native, 'query is not trusted/accepted')
        baseline = native[qid]
        require(query['sql_sha256'] == baseline['sql_sha256'] and query['database'] == baseline['database'], 'trusted SQL/database differs')
        require(query['native_result_digest'] == baseline['native_result_digest'], 'trusted native digest differs')
        require(all(query[field] == manifest_queries[qid][field] for field in ('sql_sha256', 'database')), 'selected query differs from manifest')
        source(directory / 'sql' / (qid + '.sql'), query['sql_sha256'])
    if settings.get('policy_model'):
        source(settings['policy_model'], collection['policy_sha256'])
    vectors = {}
    if settings.get('decision_vectors'):
        source(settings['decision_vectors'], collection['decision_vectors_sha256'])
        vectors = json.loads(source(directory / 'decision_vectors.json').read_text())
        require(vectors == json.loads(Path(settings['decision_vectors']).read_text()), 'vector snapshot differs')
    if settings.get('policy_variants'):
        source(settings['policy_variants'], collection['policy_variants_sha256'])
        original_variants = json.loads(Path(settings['policy_variants']).read_text())
        variants = json.loads(source(directory / 'policy_variants.json').read_text())
        require(variants == collection['policy_variants'], 'variant snapshot differs')
        require(set(variants) == set(original_variants), 'variant source coverage differs')
        for mode, variant in variants.items():
            require(variant['load_correction'] == original_variants[mode]['load_correction'], 'variant load correction differs')
            original = Path(original_variants[mode]['checkpoint'])
            if not original.is_absolute():
                original = Path(settings['policy_variants']).resolve().parent / original
            require(Path(variant['source_checkpoint']).resolve() == original.resolve(), 'variant checkpoint source differs')
            source(variant['checkpoint'], variant['checkpoint_sha256'])
    modes = settings['modes'].split(',')
    require(settings['scenario'] != 'concurrent' or not (set(modes) & set(vectors)),
            'SQL-specific fixed vector mode cannot be concurrent')
    counts = [1] if settings['scenario'] in ('serial', 'profiles') else list(map(int, settings['clients'].split(',')))
    expected, schedules = {}, {}
    for clients in counts:
        for phase, repetitions in (('warmup', settings['warmup']), ('measured', settings['repetitions'])):
            for repetition in range(repetitions):
                jobs = list(queries) * max(1, (clients * settings.get('minimum_per_client', 1) + len(queries) - 1) // len(queries))
                random.Random(settings['seed'] + (10000 if phase == 'warmup' else 0) + repetition).shuffle(jobs)
                queues = [jobs[index::clients] for index in range(clients)]
                for mode in modes:
                    schedules[(mode, clients, phase, repetition)] = queues
                    for client, queue in enumerate(queues):
                        for sequence, qid in enumerate(queue):
                            expected[(mode, clients, phase, repetition, client, sequence)] = qid
    round_rows = {}
    for row in collection['rounds']:
        key = tuple(row[field] for field in ('mode', 'clients', 'phase', 'round'))
        require(key not in round_rows and key in schedules, 'duplicate/unplanned round')
        require(row['schedule'] == schedules[key], 'round schedule differs from frozen seeded workload')
        round_rows[key] = row
    event_rows = list(records(source(directory / 'events.jsonl')))
    events, sql = {}, {}
    for rows, target in ((event_rows, events), (records(source(directory / 'sql_executions.jsonl')), sql)):
        for event in rows:
            if 'id' not in event:
                require(event.get('status') == 'client_failed', 'unknown non-SQL event')
                continue
            key = slot(event)
            require(key not in target and key in expected and expected[key] == event['id'], 'duplicate/unplanned SQL slot')
            target[key] = event
    require(set(events) == set(sql), 'SQL and final event slot coverage differs')
    query_ids = {}
    for key, event in sql.items():
        final = events[key]
        for field, value in event.items():
            if field not in ('status', 'error'):
                require(final.get(field) == value, 'SQL/final event changed: ' + field)
        require(event['status'] == 'verified' or final['status'] != 'verified', 'failed SQL upgraded to verified')
        require(event['status'] in ('verified', 'failed') and final['status'] in ('verified', 'failed'), 'invalid event status')
        if event.get('query_id'):
            qid = event['query_id']
            require(qid not in query_ids, 'duplicate execution query ID')
            query_ids[qid] = final
        if event['status'] == 'verified':
            require(event['result_digest'] == native[event['id']]['native_result_digest'], 'SQL native digest mismatch')
        require(event['sql_sha256'] == queries[event['id']]['sql_sha256'] and event['database'] == queries[event['id']]['database'], 'event SQL/database mismatch')
    raw = {}
    for path in sorted((directory / 'raw').glob('autodop_trace_*.jsonl')):
        for trace in records(source(path)):
            qid = trace['query_id']
            require(qid not in raw, 'duplicate raw trace query ID')
            raw[qid] = trace
    decisions = {}
    decision_path = directory / 'decisions.jsonl'
    if decision_path.exists():
        for decision in records(source(decision_path)):
            qid = decision['query_id']
            require(qid not in decisions and decision['request']['query_id'] == qid, 'decision identity duplicate/mismatch')
            if decision.get('response'):
                require(decision['response']['query_id'] == qid, 'decision response identity mismatch')
            if qid in query_ids:
                require(decision['mode'] == query_ids[qid]['mode'], 'decision/event mode mismatch')
            decisions[qid] = decision
    profiles, applied, fallback = 0, 0, Counter()
    for key, event in events.items():
        qid = event.get('query_id')
        if event.get('trace'):
            require(qid in raw and event['trace'] == raw[qid], 'event/raw trace mismatch')
        if event['status'] == 'verified' and event.get('autodop_enabled'):
            require(qid in raw and event.get('trace') == raw[qid], 'successful AutoDOP lacks raw trace evidence')
            trace = raw[qid]
            installed = verify_trace(trace, qid)
            require(event['dop_validation']['applied'] == installed, 'recorded application status differs')
            if installed:
                applied += 1
                require(qid in decisions, 'applied query lacks decision journal')
                decision = decisions[qid]
                require(decision['mode'] == event['mode'] and decision['request'] == trace['request'] and decision.get('response') == trace['response'], 'decision/trace payload mismatch')
                if event['mode'] in vectors:
                    entry = vectors[event['mode']][event['id']]
                    matches = [variant for variant in [entry, *entry.get('alternatives', [])]
                               if request_plan_key(variant['request']) == request_plan_key(trace['request'])]
                    require(len(matches) == 1, 'frozen vector must match exactly one plan variant')
                    entry = matches[0]
                    require(request_plan_key(entry['request']) == request_plan_key(trace['request']),
                            'SQL-specific vector request plan drifted')
                    by_index = {fragment['fragment_index']: entry['fragment_dops'][str(fragment['fragment_id'])]
                                for fragment in entry['request']['fragments']}
                    expected_vector = {str(fragment['fragment_id']): by_index[fragment['fragment_index']]
                                       for fragment in trace['request']['fragments']}
                    require(expected_vector == trace['auto_dop']['installed_dop'], 'installed vector differs from frozen baseline')
            else:
                require(event['mode'] != 'random' and event['mode'] not in vectors, 'offline intervention silently fell back')
                fallback[trace['skip_reason']] += 1
        if event.get('profile_file'):
            path = source(event['profile_file'], event['profile_sha256'])
            body = path.read_bytes()
            require(len(body) == event['profile_bytes'], 'profile byte size mismatch')
            verify_profile(body, qid)
            profiles += 1
        elif settings['scenario'] == 'profiles' and event['status'] == 'verified':
            require(bool(event.get('profile_error')), 'profile missing without explicit failure')
    failed = sum(event['status'] != 'verified' or bool(event.get('profile_error')) for event in events.values())
    missing = len(set(expected) - set(events))
    for key, row in round_rows.items():
        selected = [event for identity, event in events.items() if identity[:4] == key]
        verified = sum(event['status'] == 'verified' for event in selected)
        expected_count = sum(map(len, schedules[key]))
        require(row['expected'] == expected_count and row['executed'] == len(selected) and row['verified'] == verified and
                row['failed_or_missing'] == expected_count - verified, 'round counters differ from execution evidence')
    all_verified = not failed and not missing and set(round_rows) == set(schedules)
    require(collection['status'] != 'finished' or all_verified, 'finished label conceals failures/missing evidence')
    return {'status': 'verified' if all_verified else 'verified_evidence_with_failures', 'evidence_valid': True,
            'all_expected_executions_verified': all_verified, 'collection_status': collection['status'],
            'expected_slots': len(expected), 'recorded_sql_slots': len(sql), 'failed_slots': failed, 'missing_slots': missing,
            'formal_slots': sum(key[2] == 'measured' for key in events), 'warmup_slots': sum(key[2] == 'warmup' for key in events),
            'missing_rounds': [list(key) for key in set(schedules) - set(round_rows)],
            'verified_applied_executions': applied, 'verified_fallback_reasons': fallback,
            'verified_profiles': profiles, 'raw_traces': len(raw), 'decision_records': len(decisions),
            'unlinked_decision_query_ids': sorted(set(decisions) - set(query_ids)),
            'source_sha256': inventory}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    require(not output.exists(), 'output must be new')
    try:
        result = verify(args.collection_dir)
    except Exception as exc:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'status': 'invalid', 'evidence_valid': False, 'error': str(exc)}, indent=2) + '\n')
        raise
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
