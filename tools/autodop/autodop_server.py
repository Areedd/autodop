#!/usr/bin/env python3

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autodop.engine import FixedEngine, OperatorSubgraphEngine, PolicyEngine


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
    engines.add_argument("--operator-subgraph")
    parser.add_argument("--operator-dop", type=int, default=4)
    parser.add_argument("--default-dop", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.fragment_dops is not None:
        engine = FixedEngine(parse_fragment_dops(args.fragment_dops))
    elif args.policy_model is not None:
        engine = PolicyEngine(args.policy_model)
    else:
        engine = OperatorSubgraphEngine(
            args.operator_subgraph, args.operator_dop, args.default_dop)
    server = ThreadingHTTPServer((args.host, args.port), AutoDopHandler)
    server.engine = engine
    server.verbose = args.verbose
    print(f"AutoDOP server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
