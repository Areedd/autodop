#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import load_dataset, write_jsonl
from autodop.mcts import search
from autodop.model import load_cost_model
from autodop.training import label_lineage, select_partition


def main():
    parser = argparse.ArgumentParser(description="Search per-plan DOP labels with MCTS.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--cost-model", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--exploration", type=float, default=2 ** 0.5)
    parser.add_argument(
        "--measured-only",
        action="store_true",
        help="restrict MCTS rollouts to configurations measured in the dataset",
    )
    parser.add_argument(
        "--measured-best",
        action="store_true",
        help="label each plan with its lowest-latency measured configuration",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args()
    if args.measured_only and args.measured_best:
        raise ValueError("--measured-only and --measured-best are mutually exclusive")
    if not args.cost_model:
        raise ValueError("--cost-model is required to enforce the training split")
    if args.threads <= 0:
        raise ValueError("threads must be positive")
    torch.set_num_threads(args.threads)
    samples, pairs, _ = load_dataset(args.dataset)
    model, checkpoint = load_cost_model(args.cost_model, args.device)
    training = checkpoint.get("training")
    samples, _ = select_partition(samples, pairs, training, "train")
    metadata = checkpoint["metadata"]
    lineage = label_lineage(training, args.cost_model,
                            "measured_best" if args.measured_best else "mcts")
    by_plan = {}
    samples_by_plan = {}
    configs_by_plan = {}
    for sample in samples:
        by_plan.setdefault(sample["plan_key"], sample)
        samples_by_plan.setdefault(sample["plan_key"], []).append(sample)
        configs_by_plan.setdefault(sample["plan_key"], []).append(sample["dop_config"])

    labels = []
    for index, (plan_key, sample) in enumerate(sorted(by_plan.items())):
        measured_latency_ms = None
        if args.measured_best:
            best = min(samples_by_plan[plan_key], key=lambda row: row["latency_ms"])
            config = best["dop_config"]
            measured_latency_ms = best["latency_ms"]
            score = -measured_latency_ms
        else:
            config, score = search(
                sample,
                model,
                metadata,
                args.iterations,
                args.exploration,
                args.seed + index,
                args.device,
                configs_by_plan[plan_key] if args.measured_only else None,
            )
        label = {"plan_key": plan_key, "best_dop_config": config, "best_score": score,
                 "lineage": lineage, "search": {"seed": args.seed + index,
                 "iterations": args.iterations, "exploration": args.exploration,
                 "measured_only": args.measured_only}}
        if measured_latency_ms is not None:
            label["measured_latency_ms"] = measured_latency_ms
        labels.append(label)
        print(f"plan={plan_key} score={score:.6f} dop={config}")
    write_jsonl(args.output, labels)


if __name__ == "__main__":
    main()
