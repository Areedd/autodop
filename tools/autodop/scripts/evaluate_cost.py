#!/usr/bin/env python3

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import load_dataset
from autodop.graph import build_graph
from autodop.model import load_cost_model, pair_rank_accuracy
from autodop.training import dataset_fingerprint, file_sha256, select_partition


def evaluate(checkpoint_path, dataset_path, partition="auto", device="cpu", plan_prefixes=()):
    samples, pairs, _ = load_dataset(dataset_path)
    model, checkpoint = load_cost_model(checkpoint_path, device)
    model.eval()
    training = checkpoint.get("training")
    fingerprint = dataset_fingerprint(samples, pairs)
    if partition == "auto":
        if training is None:
            partition = "all"
        elif fingerprint == training["dataset_fingerprint"]:
            partition = "valid"
        else:
            raise ValueError("different evaluation corpus: specify --partition external explicitly")
    if partition in ("train", "valid"):
        if training is None:
            raise ValueError("checkpoint has no training split; retrain before held-out evaluation")
        samples, pairs = select_partition(samples, pairs, training, partition)
    elif partition == "external" and training:
        overlap = {sample["plan_key"] for sample in samples} & set(training["train_plan_keys"])
        if overlap:
            raise ValueError("external evaluation contains training plans")
    elif partition not in ("all", "external"):
        raise ValueError(f"unsupported partition: {partition}")
    if plan_prefixes:
        samples = [sample for sample in samples
                   if any(sample["plan_key"].startswith(prefix) for prefix in plan_prefixes)]
        pairs = [pair for pair in pairs
                 if any(pair["plan_key"].startswith(prefix) for prefix in plan_prefixes)]
    if not pairs:
        raise ValueError("selected evaluation partition contains no ranking pairs")

    # Vocabulary ids and normalization belong to the trained checkpoint, including
    # when independently built evaluation datasets assign different category ids.
    graphs = {
        (sample["plan_key"], sample["config_key"]):
            build_graph(sample, checkpoint["metadata"], device=device)
        for sample in samples
    }
    by_plan = defaultdict(list)
    for pair in pairs:
        by_plan[pair["plan_key"]].append(pair)
    plans = []
    for key, plan_pairs in sorted(by_plan.items()):
        accuracy = pair_rank_accuracy(model, graphs, plan_pairs)
        plans.append({"plan_key": key, "pairs": len(plan_pairs),
                      "correct": int(round(accuracy * len(plan_pairs))), "rank_accuracy": accuracy})
    correct = sum(plan["correct"] for plan in plans)
    return {
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "dataset_fingerprint": fingerprint,
        "partition": partition, "feature_metadata_source": "checkpoint",
        "pairs": len(pairs), "correct": correct,
        "rank_accuracy": correct / len(pairs), "plans": plans,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate AutoDOP using checkpoint features and split.")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--partition", choices=("auto", "train", "valid", "external", "all"), default="auto")
    parser.add_argument("--plan-keys", default="", help="comma-separated plan_key prefixes")
    parser.add_argument("--output", default="", help="optional JSON report")
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("--threads must be positive")
    torch.set_num_threads(args.threads)
    result = evaluate(args.checkpoint, args.dataset, args.partition, args.device,
                      [prefix.strip() for prefix in args.plan_keys.split(",") if prefix.strip()])
    print(f"rank_accuracy={result['rank_accuracy']:.4f} ({result['rank_accuracy'] * 100:.2f}%) "
          f"pairs={result['pairs']} partition={result['partition']}")
    print("plan_key,pairs,correct,rank_accuracy")
    for plan in result["plans"]:
        print(f"{plan['plan_key']},{plan['pairs']},{plan['correct']},{plan['rank_accuracy']:.4f}")
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    main()
