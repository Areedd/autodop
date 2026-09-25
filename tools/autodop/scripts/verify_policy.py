#!/usr/bin/env python3
"""Replay recorded plan features through the real HTTP handler; no database execution."""

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import load_dataset, read_jsonl, supported_dops
from autodop.engine import PolicyEngine
from autodop.graph import build_graph, sample_from_request
from autodop.model import load_policy_model
from autodop.training import file_sha256, select_partition
from autodop_server import AutoDopHandler, AutoDopHttpServer


def request_from_sample(sample):
    ids = [f"F{index}" for index in range(len(sample["fragments"]))]
    return {
        "version": 1, "query_id": sample["plan_key"], "worker_loads": {},
        "fragments": [{**fragment, "fragment_id": ids[index], "fragment_index": index}
                      for index, fragment in enumerate(sample["fragments"])],
        "edges": [{"source_fragment_id": ids[edge["source"]],
                   "dest_fragment_id": ids[edge["destination"]],
                   "partition_type": edge["partition_type"]} for edge in sample["edges"]],
    }


def verify(policy_path, dataset_path, labels_path=None, min_confidence=0.5):
    samples, pairs, _ = load_dataset(dataset_path)
    model, checkpoint = load_policy_model(policy_path)
    model.eval()
    training = checkpoint["training"]
    select_partition(samples, pairs, training, "all")  # Corpus identity must match.
    labels = {row["plan_key"]: row["best_dop_config"] for row in read_jsonl(labels_path)} if labels_path else {}
    if labels_path and file_sha256(labels_path) != training.get("labels_sha256"):
        raise ValueError("verification labels do not match policy lineage")
    by_plan = {}
    for sample in samples:
        by_plan.setdefault(sample["plan_key"], sample)
    results = []
    server = AutoDopHttpServer(("127.0.0.1", 0), AutoDopHandler)
    server.engine = PolicyEngine(policy_path, {"cpu_threshold": 0.4, "mem_threshold": 0.65, "floor": 0.1})
    server.verbose = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint + "/health", timeout=10) as response:
            if json.load(response) != {"status": "ok"}:
                raise ValueError("health check failed")
        for key, sample in sorted(by_plan.items()):
            request = request_from_sample(sample)
            offline = build_graph(sample, checkpoint["metadata"], unknown_dop=True)
            online = build_graph(sample_from_request(request), checkpoint["metadata"], unknown_dop=True)
            for field, value in offline.items():
                if isinstance(value, torch.Tensor) and not torch.equal(value, online[field]):
                    raise ValueError(f"offline/serving features differ for {key}: {field}")
            with torch.no_grad():
                classes = model(offline).argmax(dim=-1).tolist()
            expected = [checkpoint["metadata"]["dop_classes"][index] for index in classes]
            payload = json.dumps(request).encode("utf-8")
            started = time.perf_counter()
            with opener.open(urllib.request.Request(
                    endpoint + "/v1/decide", data=payload,
                    headers={"Content-Type": "application/json"}), timeout=30) as response:
                actual = json.load(response)
            elapsed = (time.perf_counter() - started) * 1000
            ids = [fragment["fragment_id"] for fragment in request["fragments"]]
            if actual.get("version") != 1 or actual.get("query_id") != key:
                raise ValueError("HTTP response protocol mismatch")
            if [decision["fragment_id"] for decision in actual["decisions"]] != ids:
                raise ValueError("HTTP response fragment mismatch")
            dops = [decision["dop"] for decision in actual["decisions"]]
            if dops != expected:
                raise ValueError("HTTP policy output differs from offline inference")
            if any(dop not in supported_dops(fragment, checkpoint["metadata"]["dop_classes"])
                   for dop, fragment in zip(dops, sample["fragments"])):
                raise ValueError("HTTP policy returned illegal candidate")
            confidence = actual["confidence"]
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("invalid confidence")
            results.append({
                "plan_key": key, "partition": "train" if key in training["train_plan_keys"] else "valid",
                "dop_config": dops, "confidence": confidence,
                "would_pass_fe_confidence": confidence >= min_confidence,
                "http_latency_ms": elapsed,
                "teacher_config": labels.get(key),
                "matches_teacher": dops == labels[key] if key in labels else None,
            })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    taught = [row for row in results if row["matches_teacher"] is not None]
    return {
        "policy_sha256": file_sha256(policy_path), "scope": "local_http_feature_replay",
        "database_executed": False, "feature_parity": True,
        "min_confidence": min_confidence, "plans": results,
        "http_verified_plans": len(results),
        "confidence_accepted_plans": sum(row["would_pass_fe_confidence"] for row in results),
        "teacher_exact_plan_accuracy": sum(row["matches_teacher"] for row in taught) / len(taught) if taught else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--labels")
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args(argv)
    if args.threads < 1 or not 0 <= args.min_confidence <= 1:
        parser.error("threads must be positive and confidence must be in [0,1]")
    torch.set_num_threads(args.threads)
    result = verify(args.policy_model, args.dataset, args.labels, args.min_confidence)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"HTTP verified={result['http_verified_plans']} confidence accepted={result['confidence_accepted_plans']}")
    return result


if __name__ == "__main__":
    main()
