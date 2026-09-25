#!/usr/bin/env python3
"""Offline, auditable execution collection. Never serve this helper as a policy.

Only complete queries enter measured/accepted.jsonl; raw/ and provenance/ include
rejected attempts, discovery and warmups and MUST NOT be passed to build_dataset.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import random
import re
import threading
import time

import pymysql

from autodop.data import sample_from_trace
from autodop.fixed_engine import FixedEngine
from autodop_server import AutoDopHandler


CLIENT_TIMING_CONTRACT = 'execute_and_fetch_v2_excludes_setup_and_digest'


def fetch_timed(cursor, statement, event):
    """Time SQL execution and result receipt, excluding setup and verification."""
    started = time.monotonic()
    try:
        cursor.execute(statement)
        return cursor.fetchall()
    finally:
        finished = time.monotonic()
        event['sql_finished_monotonic'] = finished
        event['client_latency_ms'] = (finished - started) * 1000
        event['client_timing_contract'] = CLIENT_TIMING_CONTRACT


def canonical_cell(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        # Avoid Decimal.normalize(), which rounds through the ambient context.
        number = format(value, "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return ["decimal", "0" if value == 0 else number]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite result value")
        return ["float", format(0.0 if value == 0 else value, ".8g")]
    if isinstance(value, (date, datetime)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, timedelta):
        return ["timedelta", str(value.days), str(value.seconds), str(value.microseconds)]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    raise TypeError(f"unsupported result type: {type(value).__name__}")


def result_digest(rows):
    """Order-independent multiset identity; preserves duplicate rows and SQL types."""
    encoded = Counter(json.dumps([canonical_cell(v) for v in row], ensure_ascii=False,
                                 separators=(",", ":")) for row in rows)
    payload = json.dumps(sorted(encoded.items()), ensure_ascii=False, separators=(",", ":"))
    return {"sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "rows": sum(encoded.values()), "float_significant_digits": 8}


def frozen_configs(request, dops, count, seed, design="legacy"):
    fragments = sorted(request["fragments"], key=lambda f: f["fragment_index"])
    ids = [str(f["fragment_id"]) for f in fragments]
    domains = [[d for d in dops if d in f["candidate_dops"] and
                (not f.get("is_gather_root", False) or d == 1)] for f in fragments]
    if not ids or any(not domain for domain in domains):
        raise ValueError("empty fragment candidate domain")
    configs, seen = [], set()

    def add(values):
        key = tuple(values)
        if key not in seen and len(configs) < count:
            seen.add(key)
            configs.append(dict(zip(ids, values)))

    base = [min(domain, key=lambda d: (abs(d - 4), d)) for domain in domains]
    if design == "anchor4":
        add(base)
    for dop in dops:
        add([max(d for d in domain if d <= dop) if min(domain) <= dop else min(domain)
             for domain in domains])
    # Fixed baseline and graph-order perturbations; no latency-dependent selection.
    perturbations = [(i, dop) for i, domain in enumerate(domains) for dop in domain if dop != base[i]]
    if design == "balanced":
        random.Random(seed).shuffle(perturbations)
    perturbation_limit = max(len(dops), count - (max(4, count // 2) if design == "balanced" else 4))
    for i, dop in perturbations:
        if len(configs) >= perturbation_limit:
            break
        values = list(base)
        values[i] = dop
        add(values)
    rng = random.Random(seed)
    for _ in range(count * 100):
        add([rng.choice(domain) for domain in domains])
        if len(configs) == min(count, math.prod(map(len, domains))):
            break
    return configs


def validate_trace(trace, expected, query_id):
    if trace.get("query_id") != query_id or trace.get("state") != "EOF":
        raise ValueError("trace query identity/state mismatch")
    if trace.get("skip_reason") or not trace.get("request"):
        raise ValueError("AutoDOP skipped; no verified intervention")
    if trace["request"].get("query_id") != query_id:
        raise ValueError("request query identity mismatch")
    installed = {str(k): int(v) for k, v in trace["auto_dop"]["installed_dop"].items()}
    if installed != expected:
        raise ValueError(f"installed DOP differs: {installed} != {expected}")
    actual = {}
    fragments = trace.get("actual_fragments", [])
    if len(fragments) != len(expected):
        raise ValueError("incomplete actual fragment trace")
    for fragment in fragments:
        fid = str(fragment["fragment_id"])
        if fid in actual or fid not in expected or fragment["plan_dop"] != expected[fid]:
            raise ValueError("actual plan DOP/fragment differs from installed vector")
        values = fragment.get("instance_dops", [])
        if not values or len(set(values)) != 1:
            raise ValueError(f"ambiguous heterogeneous actual instance DOP for {fid}: {values}")
        if not 1 <= values[0] <= expected[fid]:
            raise ValueError(f"invalid actual DOP for {fid}: {values}")
        actual[fid] = values[0]
    if set(actual) != set(expected):
        raise ValueError("actual fragment identity differs")
    if not isinstance(trace.get("latency_ms"), (int, float)) or trace["latency_ms"] <= 0:
        raise ValueError("non-positive latency")
    return actual


def explicit_configs(request, vectors):
    """Remap a frozen fragment-index corpus to this query's actual fragment IDs."""
    fragments = sorted(request['fragments'], key=lambda f: int(f['fragment_index']))
    indices = {str(f['fragment_index']) for f in fragments}
    configs, seen = [], set()
    for vector in vectors:
        if set(vector) != indices:
            raise ValueError('explicit vector fragment indices differ from discovered plan')
        config = {}
        for fragment in fragments:
            dop = vector[str(fragment['fragment_index'])]
            if type(dop) is not int or dop not in fragment['candidate_dops']:
                raise ValueError('explicit vector contains unsupported DOP')
            if fragment.get('is_gather_root') and dop != 1:
                raise ValueError('explicit vector violates gather root DOP')
            config[str(fragment['fragment_id'])] = dop
        identity = tuple(config.items())
        if identity in seen:
            raise ValueError('duplicate explicit configuration')
        seen.add(identity)
        configs.append(config)
    if len(configs) < 2:
        raise ValueError('explicit corpus requires at least two distinct configurations')
    return configs


def append_jsonl(path, record):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


@contextmanager
def frontend_settings(cursor, updates, evidence_path, *, restore_connection_factory=None):
    original = {}
    for name in updates:
        cursor.execute("ADMIN SHOW FRONTEND CONFIG LIKE %s", (name,))
        columns = [item[0].lower() for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        matches = [row for row in rows if row.get("key", row.get("name")) == name]
        if len(matches) != 1 or "value" not in matches[0]:
            raise RuntimeError(f"cannot snapshot FE config {name}: {rows}")
        original[name] = str(matches[0]["value"])
    evidence = {"original": original, "collection": updates, "restored": False}
    save_json(evidence_path, evidence)
    try:
        for name, value in updates.items():
            cursor.execute("ADMIN SET FRONTEND CONFIG (" + json.dumps(name) + " = " +
                           json.dumps(str(value)) + ")")
        yield
    finally:
        errors = []
        restore_connection = None
        restore_cursor = cursor
        if restore_connection_factory is not None:
            try:
                # The control connection can expire while hours of SQL run on
                # separate worker connections. Restoration needs a live session.
                restore_connection = restore_connection_factory()
                restore_cursor = restore_connection.cursor()
                evidence['restore_connection'] = 'fresh'
            except Exception as exc:
                errors.append(f"restore connection: {exc}")
        for name, value in reversed(list(original.items())):
            try:
                restore_cursor.execute("ADMIN SET FRONTEND CONFIG (" + json.dumps(name) + " = " +
                                       json.dumps(value) + ")")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if restore_connection is not None:
            try:
                restore_connection.close()
            except Exception as exc:
                errors.append(f"close restore connection: {exc}")
        evidence.update(restored=not errors, restore_errors=errors)
        save_json(evidence_path, evidence)
        if errors:
            raise RuntimeError("FE configuration restoration failed: " + "; ".join(errors))


class TraceReader:
    def __init__(self, directory):
        self.directory, self.offsets, self.pending = directory, {}, {}

    def wait(self, query_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in self.directory.glob("autodop_trace_*.jsonl"):
                with path.open() as handle:
                    handle.seek(self.offsets.get(str(path), 0))
                    while True:
                        position = handle.tell()
                        line = handle.readline()
                        if not line or not line.endswith("\n"):
                            handle.seek(position)
                            break
                        record = json.loads(line)
                        key = record.get("query_id")
                        if key in self.pending:
                            raise ValueError(f"duplicate trace query id {key}")
                        self.pending[key] = record
                    self.offsets[str(path)] = handle.tell()
            if query_id in self.pending:
                return self.pending.pop(query_id)
            time.sleep(0.1)
        raise TimeoutError(f"missing exact trace for {query_id}")


def verification_collection(settings):
    """Single-anchor evidence is explicit and cannot silently weaken ranking runs."""
    enabled = settings.get('verification_only', False)
    if enabled and (settings.get('configs') != 1 or settings.get('config_design') != 'anchor4'
                    or settings.get('configuration_manifest')):
        raise ValueError('verification-only requires configs=1, anchor4, and no explicit configuration manifest')
    return enabled


def collect(args):
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text())
    audit = json.loads(Path(args.audit).read_text())
    if audit.get("deployment_valid") is not True:
        raise ValueError("deployment audit must explicitly set deployment_valid=true")
    verification_only = verification_collection(vars(args))
    probe_timeout_s = getattr(args, 'probe_timeout_s', None)
    probe_timeout_s = args.query_timeout_s if probe_timeout_s is None else probe_timeout_s
    if args.query_timeout_s <= 0 or probe_timeout_s <= 0:
        raise ValueError('query and probe timeouts must be positive')
    if args.repetitions < 3 or args.warmup < 1 or (not verification_only and args.configs < 4):
        raise ValueError("require >=3 formal repetitions, >=1 warmup and >=4 configurations unless verification-only")
    dops = sorted(set(int(d) for d in args.dops.split(",")))
    if not dops or dops[0] != 1 or any(d not in [1, 2, 4, 8, 16, 32] for d in dops):
        raise ValueError("invalid DOP domain; must include 1")
    queries, identities, hashes = [], set(), {}
    for query in manifest["queries"]:
        query = dict(query)
        if query["split"] not in ("train", "valid", "test") or query["id"] in identities:
            raise ValueError("duplicate query id or invalid preselected split")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", query["id"]):
            raise ValueError("query id must be a safe filename")
        sql_path = Path(query["sql_file"])
        if not sql_path.is_absolute():
            sql_path = manifest_path.parent / sql_path
        sql = sql_path.read_text()
        sha = hashlib.sha256(sql.encode()).hexdigest()
        if query.get("sql_sha256") is not None and query["sql_sha256"] != sha:
            raise ValueError(f"SQL differs from preselected manifest: {query['id']}")
        if query["database"] != audit.get("database"):
            raise ValueError(f"query database differs from audited database: {query['id']}")
        if sha in hashes:
            raise ValueError(f"duplicate SQL: {query['id']} and {hashes[sha]}")
        identities.add(query["id"])
        hashes[sha] = query["id"]
        query.update(sql=sql, sql_sha256=sha)
        queries.append(query)
    output = Path(args.output_dir).resolve()
    explicit_path = getattr(args, 'configuration_manifest', None)
    configured = json.loads(Path(explicit_path).read_text()) if explicit_path else None
    if configured is not None and set(configured) != {q['id'] for q in queries}:
        raise ValueError('explicit configuration manifest must cover selected queries exactly')
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be new/empty")
    for directory in (output, output / "raw", output / "measured", output / "sql", output / "provenance"):
        directory.mkdir(parents=True, exist_ok=True)
    save_json(output / "audit.json", audit)
    if configured is not None:
        save_json(output / 'configuration_manifest.json', configured)
    for query in queries:
        (output / "sql" / (query["id"] + ".sql")).write_text(query["sql"])
    summary = {"status": "running", "started_at": datetime.now().astimezone().isoformat(),
               "client_timing_contract": CLIENT_TIMING_CONTRACT,
               "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
               "audit_sha256": hashlib.sha256(Path(args.audit).read_bytes()).hexdigest(),
               "settings": vars(args), "dops": dops, "queries": [],
               "selection_manifest": [{k: v for k, v in q.items() if k != "sql"} for q in queries],
               "label_semantics": "installed fragment vector; uniform native caps recorded separately"}
    if configured is not None:
        summary['configuration_manifest_sha256'] = hashlib.sha256(Path(explicit_path).read_bytes()).hexdigest()
    save_json(output / "collection.json", summary)
    server = ThreadingHTTPServer((args.autodop_host, args.autodop_port), AutoDopHandler)
    server.engine, server.verbose = FixedEngine({"*": 1}), False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = control_connection = None
    reader = TraceReader(output / "raw")
    try:
        connection = pymysql.connect(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
                                     autocommit=True, connect_timeout=10,
                                     read_timeout=max(args.query_timeout_s, probe_timeout_s) + 15, write_timeout=30)
        control_connection = pymysql.connect(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
                                             autocommit=True, connect_timeout=10, read_timeout=30)
        with connection.cursor() as cursor, frontend_settings(control_connection.cursor(), {
                "auto_dop_endpoint": f"http://{args.autodop_host}:{args.autodop_port}",
                "auto_dop_trace_dir": str(output / "raw"),
                "auto_dop_fail_on_error": "true", "auto_dop_timeout_ms": "1000",
                "enable_auto_dop": "false"}, output / "frontend_settings.json",
                restore_connection_factory=lambda: pymysql.connect(
                    host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
                    autocommit=True, connect_timeout=10, read_timeout=30)):
            for statement in ("SET enable_pipeline_engine=true", "SET pipeline_dop=4",
                              "SET enable_runtime_adaptive_dop=false", "SET enable_query_cache=false",
                              "SET enable_spill=false", f"SET auto_dop_max_dop={max(dops)}",
                              f"SET query_timeout={args.query_timeout_s}"):
                cursor.execute(statement)
            for index, query in enumerate(queries):
                entry = {k: v for k, v in query.items() if k != "sql"}
                entry.update(status="running", configs=[])
                summary["queries"].append(entry)
                save_json(output / "collection.json", summary)
                measured = []

                def execute(phase, mapping=None, round_index=0, config_id=None, baseline=None):
                    event = {"id": query["id"], "split": query["split"], "database": query["database"],
                             "sql_sha256": query["sql_sha256"], "phase": phase, "round": round_index,
                             "config_id": config_id, "requested_dop": mapping,
                             "expected_repetitions": args.repetitions,
                             "started_at": datetime.now().astimezone().isoformat()}
                    try:
                        cursor.execute(f"SET query_timeout={probe_timeout_s if phase == 'probe' else args.query_timeout_s}")
                        cursor.execute("SET enable_auto_dop=" + ("false" if phase == "native" else "true"))
                        if mapping is not None:
                            server.engine = FixedEngine(mapping)
                        rows = fetch_timed(cursor, query["sql"], event)
                        digest_started = time.monotonic()
                        digest = result_digest(rows)
                        event['result_digest_ms'] = (time.monotonic() - digest_started) * 1000
                        if cursor.nextset():
                            raise ValueError("only one SQL statement allowed")
                        # The helper SELECT has its own fragment IDs. Disable AutoDOP
                        # on that statement without a separate SET (which would replace
                        # the last-query identity we need to retrieve).
                        cursor.execute("SELECT /*+ SET_VAR(enable_auto_dop=false) */ last_query_id()")
                        query_id = str(cursor.fetchone()[0])
                        event.update(query_id=query_id, result_digest=digest)
                        if baseline is not None and digest != baseline:
                            raise ValueError("result multiset differs from native baseline")
                        trace = None
                        if phase != "native":
                            trace = reader.wait(query_id, args.trace_timeout_s)
                            expected = mapping
                            if "*" in mapping:
                                expected = {str(f["fragment_id"]): mapping["*"] for f in trace["request"]["fragments"]}
                            event["requested_dop"] = expected
                            event["actual_dop"] = validate_trace(trace, expected, query_id)
                            event["latency_ms"] = trace["latency_ms"]
                            event["plan_key"] = sample_from_trace(trace, dop_classes=dops)["plan_key"]
                            if phase != "probe" and event["plan_key"] != entry["plan_key"]:
                                raise ValueError("plan features changed after configuration freeze")
                            event["worker_loads"] = trace["request"].get("worker_loads", {})
                        event["status"] = "verified"
                        if trace is not None:
                            trace["trusted_provenance"] = dict(event)
                        return digest, trace
                    except Exception as exc:
                        event.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                        raise
                    finally:
                        append_jsonl(output / "provenance" / "executions.jsonl", event)

                try:
                    cursor.execute("USE `" + query["database"].replace("`", "``") + "`")
                    baseline, _ = execute("native")
                    entry["native_result_digest"] = baseline
                    _, discovery = execute("probe", {"*": 1}, baseline=baseline)
                    entry["plan_key"] = discovery["trusted_provenance"]["plan_key"]
                    if query.get('expected_plan_key') and query['expected_plan_key'] != entry['plan_key']:
                        raise ValueError('discovered plan differs from the model/search source plan')
                    configs = (explicit_configs(discovery['request'], configured[query['id']])
                               if configured is not None else
                               frozen_configs(discovery["request"], dops, args.configs, args.seed + index,
                                              getattr(args, "config_design", "legacy")))
                    if len(configs) < (1 if verification_only else 2):
                        raise ValueError("insufficient distinct legal configurations for collection purpose")
                    entry["configs"] = configs
                    save_json(output / "collection.json", summary)
                    for phase, rounds in (("warmup", args.warmup), ("measured", args.repetitions)):
                        for repetition in range(rounds):
                            order = list(enumerate(configs))
                            random.Random(args.seed + index * 10000 + repetition + (1000 if phase == "measured" else 0)).shuffle(order)
                            for config_id, mapping in order:
                                _, trace = execute(phase, mapping, repetition, config_id, baseline)
                                if phase == "measured":
                                    measured.append(trace)
                    # Commit only complete query batches. Excluded queries never leak labels.
                    for trace in measured:
                        append_jsonl(output / "measured" / "accepted.jsonl", trace)
                    entry.update(status="accepted", measured_executions=len(measured))
                except Exception as exc:
                    entry.update(status="excluded", error=f"{type(exc).__name__}: {exc}",
                                 discarded_measured_executions=len(measured))
                save_json(output / "collection.json", summary)
                print(json.dumps({"query": query["id"], "status": entry["status"],
                                  "configs": len(entry["configs"]), "error": entry.get("error")}), flush=True)
        summary["status"] = "finished"
    except BaseException as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if connection:
            connection.close()
        if control_connection:
            control_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        summary.update(finished_at=datetime.now().astimezone().isoformat(),
                       accepted=sum(q["status"] == "accepted" for q in summary["queries"]),
                       excluded=sum(q["status"] == "excluded" for q in summary["queries"]))
        save_json(output / "collection.json", summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "audit", "output-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=9030)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--autodop-host", default="127.0.0.1")
    parser.add_argument("--autodop-port", type=int, default=18081)
    parser.add_argument("--dops", default="1,2,4,8")
    parser.add_argument("--configs", type=int, default=16)
    parser.add_argument("--config-design", choices=("legacy", "balanced", "anchor4"), default="legacy")
    parser.add_argument("--verification-only", action="store_true",
                        help="single anchor4 result/trace prepass; not a ranking training corpus")
    parser.add_argument("--configuration-manifest", help="frozen query -> fragment-index vectors JSON; overrides config generation")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--query-timeout-s", type=int, default=60)
    parser.add_argument("--probe-timeout-s", type=int,
                        help="discovery-only timeout; defaults to query timeout, never changes measured timeout")
    parser.add_argument("--trace-timeout-s", type=int, default=10)
    collect(parser.parse_args())


if __name__ == "__main__":
    main()
