#!/usr/bin/env python3
"""Collect frozen, result-verified paper benchmarks; no failed execution is discarded."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
import resource
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pymysql
import torch

from autodop.engine import PolicyEngine
from autodop.data import sample_from_trace
from autodop.fixed_engine import FixedEngine
from autodop_server import AutoDopHandler, AutoDopHttpServer
from collect_trusted_dataset import fetch_timed, frontend_settings, result_digest, save_json

MODES = ('native', 'uniform1', 'uniform2', 'uniform4', 'uniform8', 'uniform16',
         'random', 'policy_static', 'policy_load')


def now():
    return datetime.now().astimezone().isoformat()


def collector_resource_sample():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    sample = {'at': now(), 'monotonic_seconds': time.monotonic(),
              'user_cpu_seconds': usage.ru_utime, 'system_cpu_seconds': usage.ru_stime,
              'affinity_cpus': sorted(os.sched_getaffinity(0)),
              'max_rss': usage.ru_maxrss, 'max_rss_unit': 'KiB',
              'voluntary_context_switches': usage.ru_nvcsw,
              'involuntary_context_switches': usage.ru_nivcsw}
    try:
        sample['current_rss_bytes'] = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        sample['thread_count'] = len(list(Path('/proc/self/task').iterdir()))
    except (OSError, ValueError, IndexError) as exc:
        sample['proc_read_error'] = str(exc)
    return sample


def fetch_profile(fe_http_url, query_id, destination, user='root', timeout_s=15, retry_interval_s=.25):
    """Authenticated FE profile fetch; only eventual-publication 404s are retried."""
    if timeout_s <= 0 or retry_interval_s <= 0:
        raise ValueError('profile timeouts must be positive')
    url = fe_http_url.rstrip('/') + '/api/profile?' + urlencode({'query_id': query_id})
    request = Request(url, headers={'Authorization': 'Basic ' + base64.b64encode((user + ':').encode()).decode()})
    deadline, attempts = time.monotonic() + timeout_s, 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f'profile unavailable after {attempts} attempts for {query_id}')
        attempts += 1
        try:
            with urlopen(request, timeout=min(10, remaining)) as response:
                body = response.read()
            break
        except HTTPError as exc:
            if exc.code != 404:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'profile unavailable after {attempts} attempts for {query_id}') from exc
            time.sleep(min(retry_interval_s, remaining))
    text = body.decode('utf-8')
    if not text.strip() or re.search(r'<\s*(?:!doctype\s+html|html)\b', text, re.IGNORECASE):
        raise ValueError('profile endpoint returned empty/HTML content')
    if text.lstrip().startswith('{'):
        payload = json.loads(text)

        def identities(node):
            if isinstance(node, dict):
                return ([node['Query ID']] if 'Query ID' in node else []) + [
                    value for child in node.values() for value in identities(child)]
            if isinstance(node, list):
                return [value for child in node for value in identities(child)]
            return []

        found = identities(payload)
    else:
        # RuntimeProfile.DefaultProfileFormatter emits an indented info-string line.
        found = re.findall(r'^\s*- Query ID:\s*([^\s]+)\s*$', text, re.MULTILINE)
    if not found or any(identity != query_id for identity in found):
        raise ValueError('profile does not contain the exact requested Query ID')
    destination = Path(destination)
    destination.write_bytes(body)
    return {'profile_file': str(destination), 'profile_sha256': hashlib.sha256(body).hexdigest(),
            'profile_bytes': len(body), 'profile_fetch_attempts': attempts, 'profile_fetched_at': now()}


def validate_execution_trace(trace, query_id):
    if trace.get('query_id') != query_id or trace.get('state') != 'EOF':
        raise ValueError('trace query identity/state mismatch')
    request = trace.get('request')
    if request is not None and request.get('query_id') != query_id:
        raise ValueError('request query identity mismatch')
    if trace.get('skip_reason'):
        # FE intentionally omits request/response for degraded decisions.
        reason = trace['skip_reason']
        category = ('low_confidence' if 'confidence is below the configured threshold' in reason else
                    'request_failed' if reason.startswith('decision request failed:') else
                    'invalid_response' if reason.startswith('invalid decision response:') else 'other')
        return {'applied': False, 'fallback_reason': reason, 'fallback_category': category}
    if not request:
        raise ValueError('applied trace lacks request')
    response = trace.get('response')
    if response is not None and response.get('query_id') != query_id:
        raise ValueError('response query identity mismatch')
    installed = {str(key): int(value) for key, value in
                 trace.get('auto_dop', {}).get('installed_dop', {}).items()}
    candidates = {str(fragment['fragment_id']): fragment['candidate_dops']
                  for fragment in request['fragments']}
    if set(installed) != set(candidates) or not installed:
        raise ValueError('installed vector has incomplete fragment identity')
    actual = {}
    for fragment in trace.get('actual_fragments', []):
        fid, values = str(fragment['fragment_id']), fragment.get('instance_dops', [])
        if fid in actual or fid not in installed:
            raise ValueError('duplicate/unknown actual fragment')
        if installed[fid] not in candidates[fid] or fragment['plan_dop'] != installed[fid]:
            raise ValueError('installed/actual plan DOP mismatch')
        if not values or any(not isinstance(value, int) or not 1 <= value <= installed[fid] for value in values):
            raise ValueError('actual instance DOP outside installed bounds')
        actual[fid] = {'instance_dops': values, 'heterogeneous': len(set(values)) > 1,
                       'capped': any(value < installed[fid] for value in values)}
    if set(actual) != set(installed):
        raise ValueError('actual fragment coverage incomplete')
    return {'applied': True, 'installed_dop': installed, 'actual': actual}


def verify_offline_intervention(trace, validation, mode, vector_engine=None):
    """An offline baseline must measure its intervention, never native fallback."""
    if not validation['applied']:
        raise ValueError(f'{mode} intervention was not installed: {trace.get("skip_reason", "unknown")}')
    if vector_engine is not None:
        expected_response = vector_engine.decide(trace['request'])
    else:
        expected_response = trace.get('response')
        if not expected_response:
            raise ValueError('random intervention lacks response evidence')
    decisions = expected_response.get('decisions', [])
    expected = {str(decision['fragment_id']): int(decision['dop']) for decision in decisions}
    if len(expected) != len(decisions) or expected != validation['installed_dop']:
        raise ValueError(f'{mode} installed vector differs from preselected/recorded decisions')


class Journal:
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()

    def write(self, record):
        with self.lock, self.path.open('a') as stream:
            stream.write(json.dumps(record, sort_keys=True) + '\n')


class RandomEngine:
    """Seed by graph identity, never timing, query UUID or cross-thread request order."""
    def __init__(self, seed):
        self.seed = seed

    def decide(self, request):
        fragments = request['fragments']
        identity = [{k: v for k, v in fragment.items()
                     if k not in ('worker_ids', 'estimated_rows')}
                    for fragment in fragments]
        rng = random.Random(str(self.seed) + json.dumps(identity, sort_keys=True))
        mapping = {}
        for fragment in fragments:
            candidates = fragment['candidate_dops']
            mapping[str(fragment['fragment_id'])] = (1 if fragment.get('is_gather_root')
                                                     else rng.choice(candidates))
        return FixedEngine(mapping).decide(request)


def request_identity(request):
    installed = {str(fragment['fragment_id']): min(fragment['candidate_dops'])
                 for fragment in request['fragments']}
    return sample_from_trace({'request': request, 'auto_dop': {'installed_dop': installed},
                              'latency_ms': 1})['plan_key']


class VectorEngine:
    def __init__(self, entries):
        self.vectors = {}
        expanded = [(template, variant) for template, entry in entries.items()
                    for variant in [entry, *entry.get('alternatives', [])]]
        for template, entry in expanded:
            request = entry['request']
            signature = request_identity(request)
            vector = {fragment['fragment_index']: int(entry['fragment_dops'][str(fragment['fragment_id'])])
                      for fragment in request['fragments']}
            if signature in self.vectors and self.vectors[signature] != vector:
                raise ValueError('ambiguous graph-to-vector mapping: ' + template)
            self.vectors[signature] = vector

    def decide(self, request):
        vector = self.vectors[request_identity(request)]
        return FixedEngine({str(fragment['fragment_id']): vector[fragment['fragment_index']]
                            for fragment in request['fragments']}).decide(request)


class RecordedEngine:
    def __init__(self, engine, journal, mode):
        self.engine, self.journal, self.mode = engine, journal, mode

    def decide(self, request):
        record = {'at': now(), 'mode': self.mode, 'query_id': request.get('query_id'),
                  'request': request}
        started = time.monotonic()
        try:
            response = self.engine.decide(request)
            record['response'] = response
            return response
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            record['decision_ms'] = (time.monotonic() - started) * 1000
            self.journal.write(record)


def template_vector_engines(entries, journal, mode):
    return {qid: RecordedEngine(VectorEngine({qid: entry}), journal, mode)
            for qid, entry in entries.items()}


def select_engine(engines, mode, template_id):
    engine = engines[mode]
    return engine[template_id] if isinstance(engine, dict) else engine


def validate_vector_scenario(scenario, modes, vectors):
    if scenario == 'concurrent' and set(modes) & set(vectors):
        raise ValueError('SQL-specific fixed vectors require serial/profiles scenario')


class ConcurrentTraces:
    def __init__(self, directory):
        self.directory, self.offsets, self.pending = directory, {}, {}
        self.lock = threading.Lock()

    def wait(self, query_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for path in self.directory.glob('autodop_trace_*.jsonl'):
                    with path.open() as stream:
                        stream.seek(self.offsets.get(str(path), 0))
                        while True:
                            position = stream.tell()
                            line = stream.readline()
                            if not line or not line.endswith('\n'):
                                stream.seek(position)
                                break
                            trace = json.loads(line)
                            self.pending[trace['query_id']] = trace
                        self.offsets[str(path)] = stream.tell()
                if query_id in self.pending:
                    return self.pending.pop(query_id)
            time.sleep(.05)
        raise TimeoutError(f'missing exact trace {query_id}')


def workload(queries, seed, round_index, clients, minimum_per_client=1):
    """Identical preassigned closed-loop client queues for every compared mode."""
    jobs = list(queries) * max(1, (clients * minimum_per_client + len(queries) - 1) // len(queries))
    random.Random(seed + round_index).shuffle(jobs)
    return [jobs[index::clients] for index in range(clients)]


def load_queries(manifest_path, trusted_path):
    manifest = json.loads(manifest_path.read_text())
    trusted = json.loads(trusted_path.read_text())
    if trusted.get('status') != 'finished':
        raise ValueError('trusted collection must be finished')
    accepted = {q['id']: q for q in trusted['queries'] if q['status'] == 'accepted'}
    queries, seen = [], set()
    for original in manifest['queries']:
        query = dict(original)
        if query['id'] in seen or query['id'] not in accepted:
            raise ValueError('manifest must contain unique, accepted trusted templates')
        seen.add(query['id'])
        source = Path(query['sql_file'])
        if not source.is_absolute():
            source = manifest_path.parent / source
        query['sql'] = source.read_text()
        digest = hashlib.sha256(query['sql'].encode()).hexdigest()
        baseline = accepted[query['id']]
        if (digest != query['sql_sha256'] or digest != baseline['sql_sha256'] or
                query['database'] != baseline['database']):
            raise ValueError('SQL/database identity differs from trusted collection')
        query['native_result_digest'] = baseline['native_result_digest']
        queries.append(query)
    if not queries:
        raise ValueError('empty query suite')
    return queries


def load_policy_variants(path):
    if not path:
        return {}
    variants = json.loads(Path(path).read_text())
    for mode, variant in variants.items():
        if not isinstance(variant.get('load_correction'), bool):
            raise ValueError('policy variant load_correction must be boolean: ' + mode)
        checkpoint = Path(variant['checkpoint'])
        if not checkpoint.is_absolute():
            checkpoint = Path(path).resolve().parent / checkpoint
        variant['checkpoint'] = str(checkpoint.resolve())
        variant['checkpoint_sha256'] = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    return variants


def collect(args):
    profile_level = getattr(args, 'pipeline_profile_level', None)
    if profile_level is not None and (
            args.scenario != 'profiles' or type(profile_level) is not int or profile_level not in (0, 1, 2)):
        raise ValueError('pipeline profile level is only valid for the profiles scenario (0/1/2)')
    torch.set_num_threads(args.torch_threads)
    modes = args.modes.split(',')
    vectors = json.loads(Path(args.decision_vectors).read_text()) if args.decision_vectors else {}
    validate_vector_scenario(args.scenario, modes, vectors)
    policy_variants = load_policy_variants(args.policy_variants)
    if set(vectors) & set(policy_variants) or set(policy_variants) & set(MODES):
        raise ValueError('policy variants must not shadow built-in or vector modes')
    if not modes or any(mode not in MODES and mode not in vectors and mode not in policy_variants for mode in modes) or len(set(modes)) != len(modes):
        raise ValueError('invalid or duplicate mode')
    clients = [int(value) for value in args.clients.split(',')]
    if any(value < 1 for value in clients) or args.repetitions < 1 or args.warmup < 1:
        raise ValueError('positive clients/repetitions and >=1 warmup required')
    if any(mode in ('policy_static', 'policy_load') for mode in modes) and not args.policy_model:
        raise ValueError('policy modes require checkpoint')
    queries = load_queries(Path(args.manifest).resolve(), Path(args.trusted_collection).resolve())
    output = Path(args.output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('output must be new/empty')
    for directory in (output, output / 'raw', output / 'sql', output / 'profiles'):
        directory.mkdir(parents=True, exist_ok=True)
    for query in queries:
        (output / 'sql' / (query['id'] + '.sql')).write_text(query['sql'])
    summary = {'status': 'running', 'started_at': now(), 'settings': vars(args),
               'collector_affinity_cpus': sorted(os.sched_getaffinity(0)),
               'torch_intraop_threads': torch.get_num_threads(),
               'manifest_sha256': hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest(),
               'trusted_collection_sha256': hashlib.sha256(Path(args.trusted_collection).read_bytes()).hexdigest(),
               'policy_sha256': hashlib.sha256(Path(args.policy_model).read_bytes()).hexdigest() if args.policy_model else None,
               'queries': [{k: v for k, v in q.items() if k != 'sql'} for q in queries],
               'rounds': [], 'errors': []}
    if args.decision_vectors:
        save_json(output / 'decision_vectors.json', vectors)
        summary['decision_vectors_sha256'] = hashlib.sha256(Path(args.decision_vectors).read_bytes()).hexdigest()
    if args.policy_variants:
        (output / 'models').mkdir()
        for variant in policy_variants.values():
            source = Path(variant['checkpoint'])
            contents = source.read_bytes()
            if hashlib.sha256(contents).hexdigest() != variant['checkpoint_sha256']:
                raise ValueError('policy checkpoint changed during snapshot')
            destination = output / 'models' / (variant['checkpoint_sha256'] + '.pt')
            if not destination.exists():
                destination.write_bytes(contents)
                destination.chmod(0o444)
            variant['source_checkpoint'] = str(source)
            variant['checkpoint'] = str(destination)
        save_json(output / 'policy_variants.json', policy_variants)
        summary['policy_variants'] = policy_variants
        summary['policy_variants_sha256'] = hashlib.sha256(Path(args.policy_variants).read_bytes()).hexdigest()
    save_json(output / 'collection.json', summary)
    events, decisions = Journal(output / 'events.jsonl'), Journal(output / 'decisions.jsonl')
    executions = Journal(output / 'sql_executions.jsonl')
    resources = Journal(output / 'resources.jsonl')
    collector_resources = Journal(output / 'collector_resources.jsonl')
    traces = ConcurrentTraces(output / 'raw')
    stop = threading.Event()

    def sample_resources():
        while not stop.is_set():
            collector_resources.write(collector_resource_sample())
            for endpoint in args.metrics_urls.split(','):
                try:
                    with urlopen(endpoint, timeout=3) as response:
                        body = response.read().decode()
                    resources.write({'at': now(), 'url': endpoint, 'prometheus_raw': body})
                except Exception as exc:
                    resources.write({'at': now(), 'url': endpoint, 'error': str(exc)})
            stop.wait(args.sample_interval_s)
        collector_resources.write(collector_resource_sample())

    def connection():
        return pymysql.connect(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
                               autocommit=True, connect_timeout=10,
                               read_timeout=args.query_timeout_s + 30, write_timeout=30)

    server = AutoDopHttpServer((args.autodop_host, args.autodop_port), AutoDopHandler)
    server.verbose = False
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    sampling = threading.Thread(target=sample_resources, daemon=True)
    sampling.start()
    control = None
    try:
        control = connection()
        with frontend_settings(control.cursor(), {
                'auto_dop_endpoint': f'http://{args.autodop_host}:{args.autodop_port}',
                'auto_dop_trace_dir': str(output / 'raw'), 'auto_dop_fail_on_error': 'false',
                'auto_dop_timeout_ms': str(args.decision_timeout_ms), 'enable_auto_dop': 'false'},
                output / 'frontend_settings.json', restore_connection_factory=connection):
            engines = {}
            for mode in modes:
                if mode in policy_variants:
                    variant = policy_variants[mode]
                    engine = PolicyEngine(variant['checkpoint'],
                                          {'cpu_threshold': .4, 'mem_threshold': .65, 'floor': .1}
                                          if variant['load_correction'] else None)
                elif mode in ('policy_static', 'policy_load'):
                    engine = PolicyEngine(args.policy_model, None if mode == 'policy_static' else
                                          {'cpu_threshold': .4, 'mem_threshold': .65, 'floor': .1})
                elif mode in vectors:
                    engines[mode] = template_vector_engines(vectors[mode], decisions, mode)
                    if not {query['id'] for query in queries} <= set(engines[mode]):
                        raise ValueError('fixed vector mode lacks a selected template: ' + mode)
                    continue
                else:
                    engine = RandomEngine(args.seed)
                engines[mode] = RecordedEngine(engine, decisions, mode)
            for count in ([1] if args.scenario in ('serial', 'profiles') else clients):
                for phase, rounds in (('warmup', args.warmup), ('measured', args.repetitions)):
                    for repetition in range(rounds):
                        mode_order = list(modes)
                        random.Random(args.seed + count * 1000 + repetition +
                                      (10000 if phase == 'warmup' else 0)).shuffle(mode_order)
                        for mode_position, mode in enumerate(mode_order):
                            server.engine = select_engine(engines, mode, queries[0]['id'])
                            queues = workload(queries, args.seed + (10000 if phase == 'warmup' else 0),
                                              repetition, count, args.minimum_per_client)
                            measurement = {}
                            barrier = threading.Barrier(count, action=lambda: measurement.update(start=time.monotonic()))

                            def worker(client_index, queue):
                                completed = []
                                db = None
                                try:
                                    db = connection()
                                    cursor = db.cursor()
                                    auto = mode in ('random', 'policy_static', 'policy_load') or mode in vectors or mode in policy_variants
                                    dop = int(mode[7:]) if mode.startswith('uniform') else (4 if auto else 0)
                                    for setting in ('enable_pipeline_engine=true', f'pipeline_dop={dop}',
                                                    'enable_runtime_adaptive_dop=false', 'enable_query_cache=false',
                                                    'enable_spill=false', f'auto_dop_max_dop={args.auto_dop_max_dop}',
                                                    f'enable_auto_dop={str(auto).lower()}',
                                                    f'query_timeout={args.query_timeout_s}',
                                                    f'enable_profile={str(args.scenario == "profiles").lower()}'):
                                        cursor.execute('SET ' + setting)
                                    if profile_level is not None:
                                        cursor.execute(f'SET pipeline_profile_level={profile_level}')
                                    current_database = queue[0]['database']
                                    cursor.execute('USE `' + current_database.replace('`', '``') + '`')
                                    barrier.wait(timeout=60)
                                    for sequence, query in enumerate(queue):
                                        event = {'scenario': args.scenario, 'mode': mode, 'clients': count,
                                                 'client': client_index, 'sequence': sequence, 'phase': phase,
                                                 'round': repetition, 'id': query['id'], 'database': query['database'],
                                                 'sql_sha256': query['sql_sha256'], 'started_at': now()}
                                        try:
                                            if mode in vectors:
                                                # Only one SQL client is permitted for vector modes.
                                                # SQL identity disambiguates feature-identical plans.
                                                server.engine = select_engine(engines, mode, query['id'])
                                            if query['database'] != current_database:
                                                cursor.execute('USE `' + query['database'].replace('`', '``') + '`')
                                                current_database = query['database']
                                            rows = fetch_timed(cursor, query['sql'], event)
                                            digest = result_digest(rows)
                                            cursor.execute('SELECT /*+ SET_VAR(enable_auto_dop=false) */ last_query_id()')
                                            query_id = str(cursor.fetchone()[0])
                                            event.update(query_id=query_id, result_digest=digest)
                                            if digest != query['native_result_digest']:
                                                raise ValueError('result differs from trusted native multiset')
                                            event['autodop_enabled'] = auto
                                            event['status'] = 'verified'
                                        except Exception as exc:
                                            event.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                                        finally:
                                            event['finished_at'] = now()
                                            executions.write(event)
                                            completed.append(event)
                                except Exception as exc:
                                    barrier.abort()
                                    # A failed client is an explicit missing-work error, never success.
                                    events.write({'mode': mode, 'clients': count, 'client': client_index,
                                                  'phase': phase, 'round': repetition, 'status': 'client_failed',
                                                  'remaining_jobs': len(queue) - len(completed), 'error': str(exc)})
                                finally:
                                    if db is not None:
                                        db.close()
                                return completed

                            started = time.monotonic()
                            with ThreadPoolExecutor(max_workers=count) as executor:
                                futures = [executor.submit(worker, index, queue) for index, queue in enumerate(queues)]
                                records = [record for future in futures for record in future.result()]
                            elapsed = time.monotonic() - started
                            ends = [event['sql_finished_monotonic'] for event in records
                                    if 'sql_finished_monotonic' in event]
                            workload_elapsed = max(ends) - measurement['start'] if ends and 'start' in measurement else None
                            # Fetch diagnostics only after all closed-loop SQL queues finish.
                            # Trace/profile polling must not throttle the measured clients.
                            for event in records:
                                if event['status'] == 'verified':
                                    query_id = event['query_id']
                                    try:
                                        if event['autodop_enabled']:
                                            trace = traces.wait(query_id, args.trace_timeout_s)
                                            event['dop_validation'] = validate_execution_trace(trace, query_id)
                                            event['trace'] = trace
                                            if mode in vectors or mode == 'random':
                                                verify_offline_intervention(
                                                    trace, event['dop_validation'], mode,
                                                    select_engine(engines, mode, event['id']).engine if mode in vectors else None)
                                        if args.scenario == 'profiles':
                                            try:
                                                profile_path = output / 'profiles' / (query_id + '.txt')
                                                event.update(fetch_profile(args.fe_http_url, query_id, profile_path,
                                                                           user=args.mysql_user))
                                            except Exception as exc:
                                                event['profile_error'] = str(exc)
                                    except Exception as exc:
                                        event.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                                events.write(event)
                            expected = sum(map(len, queues))
                            success = sum(event['status'] == 'verified' for event in records)
                            row = {'mode': mode, 'clients': count, 'phase': phase, 'round': repetition,
                                   'expected': expected, 'executed': len(records), 'verified': success,
                                   'failed_or_missing': expected - success, 'control_wall_seconds': elapsed,
                                   'workload_wall_seconds': workload_elapsed,
                                   'verified_queries_per_second': success / workload_elapsed if workload_elapsed else None,
                                   'mode_order': mode_order, 'mode_position': mode_position,
                                   'autodop_applied': sum(e.get('dop_validation', {}).get('applied', False) for e in records),
                                   'autodop_skip_reasons': [e['trace'].get('skip_reason') for e in records if e.get('trace', {}).get('skip_reason')],
                                   'profile_errors': sum('profile_error' in e for e in records),
                                   'schedule': [[q['id'] for q in queue] for queue in queues]}
                            summary['rounds'].append(row)
                            save_json(output / 'collection.json', summary)
                            print(json.dumps({k: v for k, v in row.items() if k != 'schedule'}), flush=True)
        summary['status'] = ('finished' if all(row['failed_or_missing'] == 0 and row['profile_errors'] == 0
                                             for row in summary['rounds']) else 'finished_with_errors')
    except BaseException as exc:
        summary.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        stop.set()
        sampling.join(timeout=15)
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)
        if control:
            control.close()
        summary['finished_at'] = now()
        save_json(output / 'collection.json', summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'trusted-collection', 'output-dir'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--policy-model')
    parser.add_argument('--policy-variants')
    parser.add_argument('--decision-vectors')
    parser.add_argument('--scenario', choices=('serial', 'concurrent', 'profiles'), default='serial')
    parser.add_argument('--pipeline-profile-level', type=int, choices=(0, 1, 2),
                        help='Session-only profile detail level; valid only with --scenario profiles')
    parser.add_argument('--modes', default=','.join(MODES))
    parser.add_argument('--clients', default='1,10,20,40')
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--warmup', type=int, default=1)
    parser.add_argument('--minimum-per-client', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20260905)
    parser.add_argument('--mysql-host', default='127.0.0.1')
    parser.add_argument('--mysql-port', type=int, default=9030)
    parser.add_argument('--mysql-user', default='root')
    parser.add_argument('--autodop-host', default='127.0.0.1')
    parser.add_argument('--autodop-port', type=int, default=18082)
    parser.add_argument('--query-timeout-s', type=int, default=180)
    parser.add_argument('--trace-timeout-s', type=int, default=10)
    parser.add_argument('--auto-dop-max-dop', type=int, default=8)
    parser.add_argument('--torch-threads', type=int, default=1)
    parser.add_argument('--decision-timeout-ms', type=int, default=1000)
    parser.add_argument('--metrics-urls', default='http://127.0.0.1:8040/metrics,http://127.0.0.1:8041/metrics,http://127.0.0.1:8042/metrics')
    parser.add_argument('--sample-interval-s', type=float, default=2)
    parser.add_argument('--fe-http-url', default='http://127.0.0.1:8030')
    args = parser.parse_args()
    if args.scenario == 'concurrent' and '--repetitions' not in __import__('sys').argv:
        args.repetitions = 3
    collect(args)


if __name__ == '__main__':
    main()
