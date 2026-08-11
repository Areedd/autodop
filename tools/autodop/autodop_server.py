#!/usr/bin/env python3

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autodop.engine import (
    CostAwareEngine,
    FixedEngine,
    HeterogeneousEngine,
    OperatorSubgraphEngine,
    PolicyEngine,
    TopologyAwareEngine,
)
from autodop.tree_policy import TreePolicyEngine


class AutoDopHttpServer(ThreadingHTTPServer):
    request_queue_size = 4096
    daemon_threads = True


def parse_fragment_dops(value):
    result = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        fragment_id, separator, dop = item.partition("=")
        if not separator or not fragment_id.strip():
            raise ValueError(f"invalid fragment DOP item: {item}")
        result[fragment_id.strip()] = int(dop.strip())
    return result


def parse_operator_counts(value):
    result = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        operator, separator, count = item.partition("=")
        if not separator or not operator.strip():
            raise ValueError(f"invalid operator count item: {item}")
        result[operator.strip()] = int(count.strip())
    if not result:
        raise ValueError("operator count list is empty")
    return result


def parse_integer_set(value):
    return {
        int(item.strip())
        for item in value.split(",")
        if item.strip()
    }


class AutoDopHandler(BaseHTTPRequestHandler):
    server_version = "AutoDOP/2.0"

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/decide":
            self._write_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            self._write_json(200, self.server.engine.decide(request))
        except Exception as exc:
            if self.server.verbose:
                print(f"AutoDOP decision failed: {exc}", flush=True)
            self._write_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _write_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="AutoDOP decision server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    engines = parser.add_mutually_exclusive_group(required=True)
    engines.add_argument("--fragment-dops")
    engines.add_argument("--policy-model")
    engines.add_argument("--tree-policy")
    engines.add_argument("--operator-subgraph")
    engines.add_argument("--cost-aware", action="store_true")
    engines.add_argument("--topology-aware", action="store_true")
    engines.add_argument("--heterogeneous", action="store_true")
    parser.add_argument("--operator-dop", type=int, default=4)
    parser.add_argument("--default-dop", type=int, default=1)
    parser.add_argument(
        "--serial-cardinality-threshold", type=float)
    parser.add_argument("--parallel-dop", type=int)
    parser.add_argument(
        "--parallel-operator-counts",
        help="comma-separated minimum counts, for example 'HASH JOIN=3,ANALYTIC=1'")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--low-dop", type=int, default=1)
    parser.add_argument("--high-dop", type=int, default=4)
    parser.add_argument("--high-fragment-cost", type=float, default=1e7)
    parser.add_argument("--high-scan-ranges", type=int, default=16)
    parser.add_argument("--serial-scan-ranges", type=int, default=1)
    parser.add_argument("--serial-operator", default="EXCEPT")
    parser.add_argument("--constrained-worker-ids", default="")
    # Load correction (DD03 §4.2): applied to the policy/tree-policy outputs. Defaults match the
    # FE-side resource view configuration; only policy-style engines are corrected.
    parser.add_argument("--load-cpu-threshold", type=float, default=0.4)
    parser.add_argument("--load-mem-threshold", type=float, default=0.65)
    parser.add_argument("--load-factor-floor", type=float, default=0.1)
    parser.add_argument(
        "--no-load-correction",
        action="store_true",
        help="disable load correction for policy-style engines",
    )
    args = parser.parse_args()

    load_correction = None if args.no_load_correction else {
        "cpu_threshold": args.load_cpu_threshold,
        "mem_threshold": args.load_mem_threshold,
        "floor": args.load_factor_floor,
    }

    if args.fragment_dops is not None:
        engine = FixedEngine(parse_fragment_dops(args.fragment_dops))
    elif args.policy_model is not None:
        engine = PolicyEngine(args.policy_model, load_correction)
    elif args.tree_policy is not None:
        engine = TreePolicyEngine(args.tree_policy, load_correction)
    elif args.cost_aware:
        engine = CostAwareEngine(
            args.low_dop, args.high_dop,
            args.high_fragment_cost, args.high_scan_ranges)
    elif args.topology_aware:
        engine = TopologyAwareEngine(
            args.low_dop, args.high_dop, args.high_fragment_cost,
            args.high_scan_ranges, args.serial_scan_ranges)
    elif args.heterogeneous:
        engine = HeterogeneousEngine(
            args.serial_operator, args.low_dop, args.high_dop,
            args.high_fragment_cost, args.high_scan_ranges,
            parse_integer_set(args.constrained_worker_ids))
    else:
        parallel_operator_counts = (
            parse_operator_counts(args.parallel_operator_counts)
            if args.parallel_operator_counts is not None else None)
        engine = OperatorSubgraphEngine(
            args.operator_subgraph, args.operator_dop, args.default_dop,
            args.serial_cardinality_threshold, args.parallel_dop,
            parallel_operator_counts)
    server = AutoDopHttpServer((args.host, args.port), AutoDopHandler)
    server.engine = engine
    server.verbose = args.verbose
    print(f"AutoDOP server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
