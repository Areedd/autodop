#!/usr/bin/env python3
"""Incrementally trigger training; advance trace consumption only after successful training."""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autodop.data import DOP_CLASSES, read_jsonl, trace_files

SCRIPTS = ROOT / "scripts"


def parse_dop_classes(value):
    return [int(dop) for dop in value.split(",") if dop.strip()]


def count_new_traces(trace_dir, consumed_counts):
    counts = {str(path.resolve()): sum(1 for _ in read_jsonl(path)) for path in trace_files(trace_dir)}
    # Append-only FE JSONL files: count only appended records; a truncated file starts anew.
    new = sum(count - consumed_counts.get(path, 0) if count >= consumed_counts.get(path, 0) else count
              for path, count in counts.items())
    return new, counts


def run_script(script, args):
    subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--state", default="")
    parser.add_argument("--min-new-traces", type=int, default=50)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--policy-epochs", type=int, default=150)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--valid-fraction", type=float, default=0.2)
    parser.add_argument("--accuracy-gate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--dop-classes", default=",".join(str(dop) for dop in DOP_CLASSES))
    parser.add_argument("--with-policy", action="store_true", help="regenerate MCTS labels, distill and verify HTTP serving")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if min(args.min_new_traces, args.epochs, args.policy_epochs, args.iterations, args.batch_size, args.threads) < 1:
        parser.error("threshold, epochs, iterations, batch size and threads must be positive")
    output = Path(args.output_dir)
    state_path = Path(args.state) if args.state else output / "retrain_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"consumed_counts": {}, "traces_seen": 0}
    new, counts = count_new_traces(args.trace_dir, state.get("consumed_counts", {}))
    if args.dry_run:
        print(f"dry-run: new_traces={new} min_new_traces={args.min_new_traces}")
        print("actions: build_dataset -> train_cost" + (" -> MCTS -> train_policy -> HTTP verification" if args.with_policy else ""))
        return
    if new < args.min_new_traces:
        print(f"skip: new_traces={new} < min_new_traces={args.min_new_traces}")
        # Do not consume sub-threshold increments: they must accumulate across invocations.
        if not state_path.exists():
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return
    print(f"retrain: new_traces={new} >= min_new_traces={args.min_new_traces}", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    dataset = Path(args.dataset_dir)
    run_script("build_dataset.py", ["--input", args.trace_dir, "--output", dataset,
               "--dop-classes", ",".join(map(str, parse_dop_classes(args.dop_classes)))])
    if args.with_policy:
        # Each attempt has immutable artifacts. Failed runs cannot replace the last good bundle.
        runs = output / "runs"
        runs.mkdir(exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="retrain-", dir=runs))
        run_script("run_pipeline.py", ["--dataset", dataset, "--output-dir", run_dir,
                   "--cost-epochs", args.epochs, "--policy-epochs", args.policy_epochs,
                   "--iterations", args.iterations, "--batch-size", args.batch_size,
                   "--valid-fraction", args.valid_fraction, "--accuracy-gate", args.accuracy_gate,
                   "--seed", args.seed, "--threads", args.threads])
        state["latest_run"] = str(run_dir.resolve())
    else:
        run_script("train_cost.py", ["--dataset", dataset, "--output", output / "cost_checkpoint.pt",
                   "--epochs", args.epochs, "--batch-size", args.batch_size,
                   "--valid-fraction", args.valid_fraction, "--accuracy-gate", args.accuracy_gate,
                   "--seed", args.seed, "--threads", args.threads])
    state["consumed_counts"] = counts
    state["traces_seen"] = state.get("traces_seen", 0) + new
    state["last_retrain"] = datetime.now(timezone.utc).isoformat()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)
    print(f"done: state={state_path}")


if __name__ == "__main__":
    main()
