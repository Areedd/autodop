#!/usr/bin/env python3

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autodop.engine import PolicyEngine


class AutoDopHttpServer(ThreadingHTTPServer):
    request_queue_size = 4096
    daemon_threads = True


class AutoDopHandler(BaseHTTPRequestHandler):
    server_version = "AutoDOP/3.0"

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
    parser = argparse.ArgumentParser(
        description="AutoDOP distilled-policy decision server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--policy-model", required=True)
    parser.add_argument("--policy-cache-capacity", type=int, default=0,
                        help="Bounded base-decision cache; live load correction still runs on every request")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--load-cpu-threshold", type=float, default=0.4)
    parser.add_argument("--load-mem-threshold", type=float, default=0.65)
    parser.add_argument("--load-factor-floor", type=float, default=0.1)
    parser.add_argument(
        "--no-load-correction",
        action="store_true",
        help="disable fragment-level runtime load correction",
    )
    args = parser.parse_args()

    load_correction = None if args.no_load_correction else {
        "cpu_threshold": args.load_cpu_threshold,
        "mem_threshold": args.load_mem_threshold,
        "floor": args.load_factor_floor,
    }
    server = AutoDopHttpServer((args.host, args.port), AutoDopHandler)
    server.engine = PolicyEngine(args.policy_model, load_correction, cache_capacity=args.policy_cache_capacity)
    server.verbose = args.verbose
    print(f"AutoDOP server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
