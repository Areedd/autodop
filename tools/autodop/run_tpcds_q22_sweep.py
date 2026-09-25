#!/usr/bin/env python3
#
# Copyright 2021-present StarRocks, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import subprocess
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from autodop_server import AutoDopHandler
from autodop.fixed_engine import FixedEngine


DEFAULT_QUERY = "/home/shaokun/starrocks_deploy/tpcds_sf10/sql/queries/query22.sql"


def trace_file(trace_dir):
    return Path(trace_dir) / f"autodop_trace_{datetime.now().date().isoformat()}.jsonl"


def default_query_label(query_path):
    return Path(query_path).stem.replace("-", "_")


def read_trace_records(path):
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def wait_for_new_trace(path, old_count, timeout_s):
    deadline = time.monotonic() + timeout_s
    last_records = []
    while time.monotonic() < deadline:
        last_records = read_trace_records(path)
        if len(last_records) > old_count:
            return last_records[-1], len(last_records)
        time.sleep(0.2)
    raise TimeoutError(
        f"trace record did not appear in {timeout_s}s; "
        f"path={path}, old_count={old_count}, current_count={len(last_records)}"
    )


def make_sql(database, query_sql, auto_dop_max_dop, query_timeout_s, enable_spill):
    settings = [
        f"USE {database}",
        "SET enable_auto_dop = true",
        f"SET auto_dop_max_dop = {auto_dop_max_dop}",
        "SET pipeline_dop = 4",
        "SET enable_runtime_adaptive_dop = false",
        "SET enable_query_cache = false",
        f"SET query_timeout = {query_timeout_s}",
    ]
    if enable_spill:
        settings.append("SET enable_spill = true")
    return ";\n".join(settings) + ";\n" + query_sql.strip() + "\n"


def run_mysql(args, sql):
    cmd = [
        args.mysql,
        "-h",
        args.mysql_host,
        "-P",
        str(args.mysql_port),
        f"-u{args.mysql_user}",
        "--batch",
        "--raw",
        "--silent",
        "--connect-timeout=10",
    ]
    result = subprocess.run(
        cmd,
        input=sql,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=args.query_timeout_s,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mysql failed with code {result.returncode}: {result.stderr.strip()}")


def assert_trace_matches(record, expected):
    auto_dop = record.get("auto_dop", {})
    installed = auto_dop.get("installed_dop", {})
    actual = auto_dop.get("actual_instance_dop", {})
    mismatches = []
    for fragment_id, dop in expected.items():
        if installed.get(fragment_id) != dop:
            mismatches.append(f"{fragment_id} installed={installed.get(fragment_id)} expected={dop}")
        instance_dops = actual.get(fragment_id, [])
        if not instance_dops:
            mismatches.append(f"{fragment_id} has no actual instance dop")
        elif any(value != dop for value in instance_dops):
            mismatches.append(f"{fragment_id} actual={instance_dops} expected all {dop}")
    if mismatches:
        raise RuntimeError("; ".join(mismatches))


def describe_fragment(fragment):
    root = fragment.get("root_node", "")
    leftmost = fragment.get("leftmost_node", "")
    scan_ranges = fragment.get("scan_range_count", 0)
    instances = fragment.get("instance_count", 0)
    return f"{root} / {leftmost}, scan_ranges={scan_ranges}, instances={instances}"


def start_server(args):
    server = ThreadingHTTPServer((args.autodop_host, args.autodop_port), AutoDopHandler)
    server.engine = FixedEngine({"*": args.base_dop})
    server.verbose = args.verbose_server
    thread = threading.Thread(target=server.serve_forever, name="autodop-sweep-server", daemon=True)
    thread.start()
    return server, thread


def run_query_and_collect(args, server, sql, path, old_count, mapping):
    server.engine = FixedEngine(mapping)
    run_mysql(args, sql)
    return wait_for_new_trace(path, old_count, args.trace_timeout_s)


def print_fragment_summary(fragment_infos):
    print("\nFragments:")
    for fragment_id, info in fragment_infos.items():
        print(f"- {fragment_id}: {describe_fragment(info)}")

    print("\nFragment features:")
    print("| fragment | root | leftmost | scan_ranges | instances | est_cardinality | fragment_cost |")
    print("|---|---|---|---:|---:|---:|---:|")
    for fragment_id, info in fragment_infos.items():
        print(
            f"| {fragment_id} | {info.get('root_node', '')} | {info.get('leftmost_node', '')} | "
            f"{info.get('scan_range_count', 0)} | {info.get('instance_count', 0)} | "
            f"{info.get('estimated_cardinality', 0)} | {info.get('fragment_cost', 0)} |"
        )


def print_markdown(fragment_infos, results):
    print_fragment_summary(fragment_infos)

    target_dops = []
    for rows in results.values():
        target_dops = [row["target_dop"] for row in rows]
        break

    print("\nAverage latency by target fragment (ms):")
    print("| fragment | " + " | ".join(f"dop={dop}" for dop in target_dops) + " |")
    print("|---|" + "|".join("---:" for _ in target_dops) + "|")
    for fragment_id, rows in results.items():
        values = {row["target_dop"]: row["avg_latency_ms"] for row in rows}
        rendered = " | ".join(str(values.get(dop, "")) for dop in target_dops)
        print(f"| {fragment_id} | {rendered} |")

    print("\nTrial latencies (ms):")
    print("| fragment | " + " | ".join(f"dop={dop}" for dop in target_dops) + " |")
    print("|---|" + "|".join("---" for _ in target_dops) + "|")
    for fragment_id, rows in results.items():
        values = {
            row["target_dop"]: "/".join(str(value) for value in row["trial_latency_ms"])
            for row in rows
        }
        rendered = " | ".join(values.get(dop, "") for dop in target_dops)
        print(f"| {fragment_id} | {rendered} |")

    print("\nBest observed DOP:")
    print("| fragment | best_dop | avg_latency_ms |")
    print("|---|---:|---:|")
    for fragment_id, rows in results.items():
        best = min(rows, key=lambda row: row["avg_latency_ms"])
        print(f"| {fragment_id} | {best['target_dop']} | {best['avg_latency_ms']} |")


def parse_fragment_dops(value):
    mapping = {}
    if not value.strip():
        return mapping
    for item in value.split(","):
        fragment_dop = item.strip()
        if not fragment_dop:
            continue
        if "=" not in fragment_dop:
            raise ValueError(f"invalid fragment DOP mapping: {fragment_dop}")
        fragment_id, dop = fragment_dop.split("=", 1)
        mapping[fragment_id.strip()] = int(dop.strip())
    return mapping


def run_fixed_fragment_dops(args, server, sql, path, old_count, fragments, fragment_infos, fixed_dops):
    missing = [fragment_id for fragment_id in fixed_dops if fragment_id not in fragment_infos]
    if missing:
        raise RuntimeError(f"fixed DOP fragments not found in query plan: {', '.join(missing)}")

    mapping = {fragment_id: args.base_dop for fragment_id in fragments}
    mapping.update(fixed_dops)

    for warmup_index in range(1, args.warmup + 1):
        print(f"Warmup {warmup_index}/{args.warmup} with fixed fragment DOPs...", flush=True)
        record, old_count = run_query_and_collect(args, server, sql, path, old_count, mapping)
        assert_trace_matches(record, mapping)

    trials = []
    for repetition in range(1, args.repetitions + 1):
        print(f"Running fixed fragment DOPs ({repetition}/{args.repetitions})...", flush=True)
        record, old_count = run_query_and_collect(args, server, sql, path, old_count, mapping)
        assert_trace_matches(record, mapping)
        trials.append({
            "latency_ms": record.get("latency_ms", -1),
            "query_id": record.get("query_id", ""),
            "state": record.get("state", ""),
            "scan_rows": record.get("query_statistics", {}).get("scan_rows", -1),
            "scan_bytes": record.get("query_statistics", {}).get("scan_bytes", -1),
            "cpu_cost_ns": record.get("query_statistics", {}).get("cpu_cost_ns", -1),
        })

    latencies = [trial["latency_ms"] for trial in trials]
    result = {
        "mapping": mapping,
        "overrides": fixed_dops,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
        "trial_latency_ms": latencies,
        "trials": trials,
    }
    return result, old_count


def main():
    parser = argparse.ArgumentParser(description="Run TPCDS AutoDOP fragment sweep.")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--database", default="tpcds_sf10")
    parser.add_argument("--query-label", default="")
    parser.add_argument("--trace-dir", default="log/autodop")
    parser.add_argument("--output", default="")
    parser.add_argument("--mysql", default="mysql")
    parser.add_argument("--mysql-host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=9030)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--autodop-host", default="127.0.0.1")
    parser.add_argument("--autodop-port", type=int, default=18080)
    parser.add_argument("--base-dop", type=int, default=4)
    parser.add_argument("--target-dops", default="1,2,4,8,16,32")
    parser.add_argument("--target-fragments", default="")
    parser.add_argument("--fixed-fragment-dops", default="")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--query-timeout-s", type=int, default=300)
    parser.add_argument("--trace-timeout-s", type=int, default=10)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verbose-server", action="store_true")
    args = parser.parse_args()

    query_label = args.query_label or default_query_label(args.query)
    query_sql = Path(args.query).read_text(encoding="utf-8")
    path = trace_file(args.trace_dir)
    target_dops = [int(value) for value in args.target_dops.split(",") if value.strip()]
    auto_dop_max_dop = max([args.base_dop] + target_dops)
    sql = make_sql(args.database, query_sql, auto_dop_max_dop, args.query_timeout_s, False)

    server, thread = start_server(args)
    try:
        print(f"AutoDOP sweep server listening on http://{args.autodop_host}:{args.autodop_port}", flush=True)
        old_count = len(read_trace_records(path))

        print(f"Discovering {query_label} fragments with base DOP {args.base_dop}...", flush=True)
        discovery_mapping = {"*": args.base_dop}
        discovery_record, old_count = run_query_and_collect(args, server, sql, path, old_count, discovery_mapping)
        fragments = [fragment["fragment_id"] for fragment in discovery_record.get("request", {}).get("fragments", [])]
        fragment_infos = {
            fragment["fragment_id"]: fragment
            for fragment in discovery_record.get("request", {}).get("fragments", [])
        }
        if not fragments:
            raise RuntimeError("no fragments discovered from AutoDOP trace")
        print(f"Discovered fragments: {', '.join(fragments)}", flush=True)
        if args.discover_only:
            print_fragment_summary(fragment_infos)
            return

        fixed_dops = parse_fragment_dops(args.fixed_fragment_dops)
        if fixed_dops:
            result, old_count = run_fixed_fragment_dops(
                args, server, sql, path, old_count, fragments, fragment_infos, fixed_dops)
            output = {
                "query": args.query,
                "database": args.database,
                "base_dop": args.base_dop,
                "fixed_fragment_dops": fixed_dops,
                "repetitions": args.repetitions,
                "warmup": args.warmup,
                "auto_dop_max_dop": max([args.base_dop] + list(fixed_dops.values())),
                "discovery_query_id": discovery_record.get("query_id", ""),
                "fragments": fragment_infos,
                "fixed_result": result,
            }
            output_path = args.output
            if not output_path:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = f"/tmp/autodop_tpcds_{query_label}_fixed_{timestamp}.json"
            Path(output_path).write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print_fragment_summary(fragment_infos)
            print("\nFixed fragment DOP result:")
            print("| avg_latency_ms | trial_latency_ms | overrides |")
            print("|---:|---|---|")
            overrides = ",".join(f"{fragment_id}={dop}" for fragment_id, dop in sorted(fixed_dops.items()))
            trials = "/".join(str(value) for value in result["trial_latency_ms"])
            print(f"| {result['avg_latency_ms']} | {trials} | {overrides} |")
            print(f"\nVerification: OK ({args.repetitions} query runs)")
            print(f"Result JSON: {output_path}")
            return

        sweep_fragments = fragments
        if args.target_fragments.strip():
            sweep_fragments = [
                value.strip()
                for value in args.target_fragments.split(",")
                if value.strip()
            ]
            missing = [fragment_id for fragment_id in sweep_fragments if fragment_id not in fragment_infos]
            if missing:
                raise RuntimeError(f"target fragments not found in query plan: {', '.join(missing)}")
        print(f"Sweeping target fragments: {', '.join(sweep_fragments)}", flush=True)

        base_mapping = {fragment_id: args.base_dop for fragment_id in fragments}
        for warmup_index in range(1, args.warmup + 1):
            print(f"Warmup {warmup_index}/{args.warmup} with all fragments at DOP {args.base_dop}...", flush=True)
            record, old_count = run_query_and_collect(args, server, sql, path, old_count, base_mapping)
            assert_trace_matches(record, base_mapping)

        results = {fragment_id: [] for fragment_id in sweep_fragments}
        for fragment_id in sweep_fragments:
            for dop in target_dops:
                mapping = {candidate: args.base_dop for candidate in fragments}
                mapping[fragment_id] = dop
                trials = []
                for repetition in range(1, args.repetitions + 1):
                    print(
                        f"Running {query_label} with {fragment_id}={dop}, others={args.base_dop} "
                        f"({repetition}/{args.repetitions})...",
                        flush=True,
                    )
                    record, old_count = run_query_and_collect(args, server, sql, path, old_count, mapping)
                    assert_trace_matches(record, mapping)
                    trials.append({
                        "latency_ms": record.get("latency_ms", -1),
                        "query_id": record.get("query_id", ""),
                        "state": record.get("state", ""),
                        "scan_rows": record.get("query_statistics", {}).get("scan_rows", -1),
                        "scan_bytes": record.get("query_statistics", {}).get("scan_bytes", -1),
                        "cpu_cost_ns": record.get("query_statistics", {}).get("cpu_cost_ns", -1),
                    })
                latencies = [trial["latency_ms"] for trial in trials]
                results[fragment_id].append({
                    "target_fragment": fragment_id,
                    "target_dop": dop,
                    "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
                    "trial_latency_ms": latencies,
                    "trials": trials,
                })

        output = {
            "query": args.query,
            "database": args.database,
            "base_dop": args.base_dop,
            "target_dops": target_dops,
            "target_fragments": sweep_fragments,
            "repetitions": args.repetitions,
            "warmup": args.warmup,
            "auto_dop_max_dop": auto_dop_max_dop,
            "discovery_query_id": discovery_record.get("query_id", ""),
            "fragments": fragment_infos,
            "results": results,
        }
        output_path = args.output
        if not output_path:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"/tmp/autodop_tpcds_{query_label}_sweep_{timestamp}.json"
        Path(output_path).write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        print_markdown(fragment_infos, results)
        combo_count = sum(len(rows) for rows in results.values())
        print(f"\nVerification: OK ({combo_count} combinations, {combo_count * args.repetitions} query runs)")
        print(f"Result JSON: {output_path}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
