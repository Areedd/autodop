#!/usr/bin/env python3

import argparse
import itertools
import json
import random
import statistics
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymysql
from pymysql.constants import CLIENT

from autodop.fixed_engine import FixedEngine
from autodop.graph import fragment_order, sample_from_request
from autodop_server import AutoDopHandler
from run_tpcds_q22_sweep import assert_trace_matches, read_trace_records, wait_for_new_trace


DEFAULT_QUERY_DIR = "/home/shaokun/starrocks_deploy/tpcds_sf100/sql/queries"


def query_path(query_dir, query_label):
    suffix = query_label.zfill(2) if query_label.isdigit() else query_label
    return Path(query_dir) / f"query{suffix}.sql"


def parse_args():
    parser = argparse.ArgumentParser(description="Collect multi-configuration AutoDOP training traces.")
    parser.add_argument("--query-dir", default=DEFAULT_QUERY_DIR)
    parser.add_argument("--database", default="tpcds_sf100")
    parser.add_argument("--queries", required=True)
    parser.add_argument("--dops", default="1,2,4,8")
    parser.add_argument("--top-fragments", type=int, default=2)
    parser.add_argument("--random-configs", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--query-timeout-s", type=int, default=300)
    parser.add_argument("--trace-timeout-s", type=int, default=10)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=9030)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--autodop-host", default="127.0.0.1")
    parser.add_argument("--autodop-port", type=int, default=18080)
    return parser.parse_args()


def configure_session(cursor, args):
    trace_dir = str(Path(args.trace_dir).resolve())
    cursor.execute(f'ADMIN SET FRONTEND CONFIG ("auto_dop_trace_dir" = "{trace_dir}")')
    for statement in (
        "SET enable_auto_dop = true",
        "SET enable_pipeline_engine = true",
        "SET pipeline_dop = 4",
        "SET enable_runtime_adaptive_dop = false",
        "SET enable_query_cache = false",
        "SET enable_spill = false",
        f"SET auto_dop_max_dop = {max(args.dops)}",
        f"SET query_timeout = {args.query_timeout_s}",
    ):
        cursor.execute(statement)


def execute_query(cursor, sql):
    cursor.execute(sql)
    row_count = 0
    statement_count = 0
    while True:
        statement_count += 1
        if cursor.description is not None:
            row_count += len(cursor.fetchall())
        if not cursor.nextset():
            break
    if statement_count != 1:
        raise RuntimeError("training query must contain exactly one SQL statement")
    return row_count


def mapping_key(mapping, fragment_ids):
    return tuple(mapping[fragment_id] for fragment_id in fragment_ids)


def important_fragments(request, limit):
    sample = sample_from_request(request)
    order = fragment_order(sample)
    selected = [
        sample["fragment_ids"][index]
        for index in order
        if not sample["fragments"][index].get("is_gather_root", False)
    ]
    return selected[:limit]


def additional_configs(fragment_ids, targets, dops, base_dop, count, seed):
    configs = []
    base = {fragment_id: base_dop for fragment_id in fragment_ids}
    for fragment_id in targets:
        for dop in dops:
            if dop != base_dop:
                configs.append({**base, fragment_id: dop})

    combinations = list(itertools.product(dops, repeat=len(targets)))
    random.Random(seed).shuffle(combinations)
    existing = {mapping_key(config, fragment_ids) for config in configs}
    for values in combinations:
        config = dict(base)
        config.update(zip(targets, values))
        key = mapping_key(config, fragment_ids)
        if key in existing or len(set(values)) == 1:
            continue
        configs.append(config)
        existing.add(key)
        if len(configs) >= len(targets) * (len(dops) - 1) + count:
            break
    return configs


def current_trace_path(trace_dir):
    return Path(trace_dir).resolve() / f"autodop_trace_{datetime.now().date().isoformat()}.jsonl"


def run_config(cursor, server, sql, trace_dir, mapping, expected_rows, trace_timeout_s):
    before_path = current_trace_path(trace_dir)
    old_count = len(read_trace_records(before_path))
    server.engine = FixedEngine(mapping)
    row_count = execute_query(cursor, sql)
    if expected_rows is not None and row_count != expected_rows:
        raise RuntimeError(f"result row count changed: expected={expected_rows}, actual={row_count}")
    trace_path = current_trace_path(trace_dir)
    if trace_path != before_path:
        old_count = 0
    record, old_count = wait_for_new_trace(trace_path, old_count, trace_timeout_s)
    if record.get("state") != "EOF":
        raise RuntimeError(f"query failed: {record.get('error_message', '')}")
    expected = mapping
    if "*" in mapping:
        expected = {
            fragment["fragment_id"]: mapping["*"]
            for fragment in record["request"]["fragments"]
        }
    assert_trace_matches(record, expected)
    return record, row_count


def run_stage(cursor, server, sql, trace_dir, configs, fragment_ids,
              warmup, repetitions, seed, stage, expected_rows, trace_timeout_s):
    records = []
    for _ in range(warmup):
        for config in configs:
            _, expected_rows = run_config(
                cursor, server, sql, trace_dir, config, expected_rows, trace_timeout_s)

    random_generator = random.Random(seed)
    for round_index in range(repetitions):
        order = list(configs)
        random_generator.shuffle(order)
        for config in order:
            trace, expected_rows = run_config(
                cursor, server, sql, trace_dir, config, expected_rows, trace_timeout_s)
            records.append({
                "stage": stage,
                "round": round_index,
                "dop_config": list(mapping_key(config, fragment_ids)),
                "latency_ms": trace["latency_ms"],
                "query_id": trace["query_id"],
            })
    return records, expected_rows


def main():
    args = parse_args()
    args.queries = [item.strip() for item in args.queries.split(",") if item.strip()]
    args.dops = [int(item) for item in args.dops.split(",") if item.strip()]
    if 4 not in args.dops:
        raise ValueError("DOP list must include baseline DOP 4")

    trace_dir = Path(args.trace_dir).resolve()
    existing_traces = list(trace_dir.glob("autodop_trace_*.jsonl"))
    if existing_traces:
        raise RuntimeError(f"trace output already exists: {existing_traces[0]}")

    server = ThreadingHTTPServer((args.autodop_host, args.autodop_port), AutoDopHandler)
    server.engine = FixedEngine({"*": 4})
    server.verbose = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    connection = pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        database=args.database,
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
        connect_timeout=10,
        read_timeout=args.query_timeout_s + 10,
        write_timeout=30,
    )
    output_records = []
    try:
        with connection.cursor() as cursor:
            configure_session(cursor, args)
            for query_index, query_label in enumerate(args.queries):
                sql = query_path(args.query_dir, query_label).read_text(encoding="utf-8")
                discovery, expected_rows = run_config(
                    cursor, server, sql, trace_dir, {"*": 4}, None, args.trace_timeout_s)
                fragment_ids = [
                    fragment["fragment_id"]
                    for fragment in sorted(
                        discovery["request"]["fragments"], key=lambda fragment: fragment["fragment_index"])
                ]
                targets = important_fragments(discovery["request"], args.top_fragments)
                uniform = [
                    {fragment_id: dop for fragment_id in fragment_ids}
                    for dop in args.dops
                ]
                uniform_records, expected_rows = run_stage(
                    cursor, server, sql, trace_dir, uniform, fragment_ids,
                    args.warmup, args.repetitions, args.seed + query_index, "uniform", expected_rows,
                    args.trace_timeout_s)
                medians = {
                    dop: statistics.median(
                        record["latency_ms"] for record in uniform_records
                        if len(set(record["dop_config"])) == 1 and record["dop_config"][0] == dop)
                    for dop in args.dops
                }
                best_dop = min(medians, key=medians.get)
                configs = additional_configs(
                    fragment_ids, targets, args.dops, best_dop, args.random_configs,
                    args.seed + 1000 + query_index)
                local_records, expected_rows = run_stage(
                    cursor, server, sql, trace_dir, configs, fragment_ids,
                    args.warmup, args.repetitions, args.seed + 2000 + query_index, "local", expected_rows,
                    args.trace_timeout_s)
                output_records.append({
                    "query": query_label,
                    "fragment_ids": fragment_ids,
                    "target_fragments": targets,
                    "uniform_median_ms": {str(dop): medians[dop] for dop in args.dops},
                    "best_uniform_dop": best_dop,
                    "records": uniform_records + local_records,
                })
                print(
                    f"Q{query_label} fragments={len(fragment_ids)} targets={targets} "
                    f"best_uniform_dop={best_dop} configs={len(uniform) + len(configs)}",
                    flush=True,
                )
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    output = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "database": args.database,
        "query_dir": str(Path(args.query_dir).resolve()),
        "trace_dir": str(trace_dir),
        "dops": args.dops,
        "warmup": args.warmup,
        "repetitions": args.repetitions,
        "top_fragments": args.top_fragments,
        "random_configs": args.random_configs,
        "queries": output_records,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Trace directory: {trace_dir}", flush=True)
    print(f"Manifest: {output_path}", flush=True)


if __name__ == "__main__":
    main()
