#!/usr/bin/env python3
"""Build a reproducible v2 CostModel -> MCTS -> policy -> HTTP verification bundle."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autodop.training import file_sha256


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", help="existing processed execution dataset")
    source.add_argument("--trace-dir", help="build a dataset from recorded FE traces")
    parser.add_argument("--output-dir", required=True, help="new/empty run directory; historical runs are never overwritten")
    parser.add_argument("--external-dataset", action="append", default=[])
    parser.add_argument("--split-manifest", help="fixed train/valid/test plan-key manifest; overrides valid-fraction")
    parser.add_argument("--cost-epochs", type=int, default=50)
    parser.add_argument("--cost-checkpoint-selection", choices=("final", "best-valid"), default="final")
    parser.add_argument("--policy-epochs", type=int, default=150)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--hidden-dim", type=int, default=64)
    parser.add_argument("--valid-fraction", type=float, default=0.2)
    parser.add_argument("--accuracy-gate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args(argv)
    if min(args.cost_epochs, args.policy_epochs, args.iterations, args.batch_size, args.threads) < 1:
        parser.error("epochs, iterations, batch size and threads must be positive")
    if not args.split_manifest and not 0 < args.valid_fraction < 1:
        parser.error("the complete pipeline requires a held-out split: 0 < valid-fraction < 1")
    output = Path(args.output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be empty; choose a new run directory")
    output.mkdir(parents=True, exist_ok=True)
    dataset = output / "dataset"
    scripts = ROOT / "scripts"
    source_files = sorted((ROOT / "autodop").glob("*.py")) + sorted(scripts.glob("*.py")) + [ROOT / "autodop_server.py"]
    manifest = {
        "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
        "arguments": vars(args), "dataset": str(dataset), "stages": [],
        "source_sha256": {str(path.relative_to(ROOT)): file_sha256(path) for path in source_files},
        "python_version": sys.version,
    }
    manifest_path = output / "manifest.json"
    write_json(manifest_path, manifest)
    env = {**os.environ, "OMP_NUM_THREADS": str(args.threads), "MKL_NUM_THREADS": str(args.threads),
           "PYTHONUNBUFFERED": "1"}

    def stage(name, script, arguments):
        command = [sys.executable, str(scripts / script), *map(str, arguments)]
        log = output / (name + ".log")
        record = {"name": name, "command": command, "log": log.name}
        manifest["stages"].append(record)
        write_json(manifest_path, manifest)
        print(f"stage={name} log={log}", flush=True)
        with log.open("w", encoding="utf-8") as handle:
            result = subprocess.run(command, env=env, stdout=handle, stderr=subprocess.STDOUT)
        record["returncode"] = result.returncode
        write_json(manifest_path, manifest)
        if result.returncode:
            raise RuntimeError(f"{name} failed; see {log}")

    try:
        def snapshot_dataset(source, destination):
            source = Path(source).resolve()
            destination.mkdir(parents=True, exist_ok=True)
            hashes = {name: file_sha256(source / name)
                      for name in ("samples.jsonl", "pairs.jsonl", "metadata.json")}
            for name, digest in hashes.items():
                shutil.copyfile(source / name, destination / name)
                if file_sha256(destination / name) != digest or file_sha256(source / name) != digest:
                    raise RuntimeError("dataset changed while taking snapshot")
            return hashes

        if args.dataset:
            snapshot_dataset(args.dataset, dataset)
        if args.trace_dir:
            stage("build_dataset", "build_dataset.py", ["--input", Path(args.trace_dir).resolve(), "--output", dataset])
        manifest["dataset_files_sha256"] = {
            name: file_sha256(dataset / name) for name in ("samples.jsonl", "pairs.jsonl", "metadata.json")}
        split_arguments = []
        if args.split_manifest:
            split_path = output / "input_split_manifest.json"
            source_digest = file_sha256(args.split_manifest)
            shutil.copyfile(args.split_manifest, split_path)
            if file_sha256(split_path) != source_digest or file_sha256(args.split_manifest) != source_digest:
                raise RuntimeError("split manifest changed while taking snapshot")
            manifest["split_manifest_sha256"] = source_digest
            split_arguments = ["--split-manifest", split_path]
        cost, labels, policy = output / "cost.pt", output / "labels.jsonl", output / "policy.pt"
        common = ["--dataset", dataset, "--seed", args.seed, "--threads", args.threads]
        stage("train_cost", "train_cost.py", [*common, *split_arguments, "--output", cost,
              "--epochs", args.cost_epochs, "--batch-size", args.batch_size,
              "--checkpoint-selection", args.cost_checkpoint_selection,
              "--hidden-dim", args.hidden_dim, "--valid-fraction", args.valid_fraction,
              "--accuracy-gate", args.accuracy_gate])
        stage("evaluate_valid", "evaluate_cost.py", ["--dataset", dataset, "--checkpoint", cost,
              "--partition", "valid", "--threads", args.threads, "--output", output / "evaluation_valid.json"])
        stage("search_mcts", "search_mcts.py", [*common, "--cost-model", cost,
              "--iterations", args.iterations, "--output", labels])
        stage("train_policy", "train_policy.py", [*common, "--cost-model", cost,
              "--labels", labels, "--epochs", args.policy_epochs, "--output", policy])
        stage("verify_policy", "verify_policy.py", ["--dataset", dataset, "--policy-model", policy,
              "--labels", labels, "--threads", args.threads, "--output", output / "serving.json"])
        for index, external in enumerate(args.external_dataset):
            external_snapshot = output / f"external_dataset_{index}"
            snapshot_dataset(external, external_snapshot)
            stage(f"evaluate_external_{index}", "evaluate_cost.py", ["--dataset", external_snapshot,
                  "--checkpoint", cost, "--partition", "external", "--threads", args.threads,
                  "--output", output / f"evaluation_external_{index}.json"])
        # Detect data/code changes during a long run rather than claiming one coherent build.
        if any(file_sha256(ROOT / name) != digest for name, digest in manifest["source_sha256"].items()):
            raise RuntimeError("pipeline source changed during execution; rerun with a stable source snapshot")
        if any(file_sha256(dataset / name) != digest for name, digest in manifest["dataset_files_sha256"].items()):
            raise RuntimeError("dataset changed during execution")
        if args.split_manifest and file_sha256(split_path) != manifest["split_manifest_sha256"]:
            raise RuntimeError("split manifest changed during execution")
        manifest["artifacts_sha256"] = {str(path.relative_to(output)): file_sha256(path) for path in sorted(output.rglob("*"))
                                        if path.is_file() and path != manifest_path}
        manifest["status"] = "complete"
        manifest["database_executed"] = False
        print(f"complete={output}")
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(manifest_path, manifest)
    return manifest


if __name__ == "__main__":
    main()
