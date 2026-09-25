#!/usr/bin/env python3

import argparse
import json
import math
import statistics
from collections import defaultdict
import random
import sys
from pathlib import Path

import torch
import torch.nn.functional as functional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import build_metadata, load_dataset
from autodop.training import file_sha256, make_training_contract, select_partition
from autodop.graph import build_graph
from autodop.model import (CostModel, LatencyRegressionModel, dynamic_margin_ranking_loss,
                           latency_target, pair_rank_accuracy, save_checkpoint, load_cost_model)


def load_initial_cost(path, training, metadata, hidden_dim, heads, device="cpu", *, allow_appended_training_plans=False):
    """Warm-start only the same frozen plan partition; keep the input coordinate system."""
    model, checkpoint = load_cost_model(path, device=device)
    previous = checkpoint.get("training", {})
    if previous.get("contract_version") != 1:
        raise ValueError("initial checkpoint requires a training contract")
    old_train = set(previous.get("train_plan_keys", []))
    new_train = set(training["train_plan_keys"])
    if allow_appended_training_plans and not old_train < new_train:
        raise ValueError("appended initialization requires a strict training-plan superset")
    fields = ("valid_plan_keys",) if allow_appended_training_plans else ("train_plan_keys", "valid_plan_keys")
    for field in fields:
        if set(previous.get(field, [])) != set(training[field]):
            raise ValueError("initial checkpoint must retain the same frozen plan partition")
    old_test = previous.get("split_source", {}).get("test_plan_keys", [])
    new_test = training.get("split_source", {}).get("test_plan_keys", [])
    if set(old_test) != set(new_test):
        raise ValueError("initial checkpoint must retain the same frozen test partition")
    if new_train & (set(previous.get("valid_plan_keys", [])) | set(old_test)):
        raise ValueError("initialization cannot move held-out plans into training")
    config = checkpoint["model_config"]
    if config["hidden_dim"] != hidden_dim or config["heads"] != heads:
        raise ValueError("initial checkpoint architecture does not match requested dimensions")
    if checkpoint["metadata"]["dop_classes"] != metadata["dop_classes"]:
        raise ValueError("initial checkpoint DOP domain mismatch")
    provenance = {"checkpoint_sha256": file_sha256(path),
                  "source_dataset_fingerprint": previous["dataset_fingerprint"],
                  "metadata_source": "initial_checkpoint_training_partition",
                  "optimizer_state_reused": False,
                  "appended_train_plan_keys": sorted(new_train - old_train)}
    return model, checkpoint["metadata"], provenance


def plan_pair_weights(pairs):
    """Keep all pairs, giving every represented plan equal total objective weight."""
    counts = defaultdict(int)
    for pair in pairs:
        counts[pair["plan_key"]] += 1
    if not counts:
        raise ValueError("plan balancing requires ranking pairs")
    return {plan: len(pairs) / (len(counts) * count) for plan, count in counts.items()}


def split_by_plan(pairs, samples, valid_fraction, random_generator):
    """Splits ranking pairs by plan_key (grouped, no leakage across a plan's pairs)."""
    plan_keys = sorted({pair["plan_key"] for pair in pairs})
    if len(plan_keys) <= 1:
        valid_count = 0
    else:
        valid_count = min(len(plan_keys) - 1, max(0, int(round(len(plan_keys) * valid_fraction))))
    random_generator.shuffle(plan_keys)
    valid_plan_keys = set(plan_keys[:valid_count])
    train_plan_keys = set(plan_keys[valid_count:])
    train_pairs = [pair for pair in pairs if pair["plan_key"] in train_plan_keys]
    valid_pairs = [pair for pair in pairs if pair["plan_key"] in valid_plan_keys]
    train_samples = [sample for sample in samples if sample["plan_key"] in train_plan_keys]
    return train_pairs, valid_pairs, train_samples, valid_plan_keys


def validation_choice_regret(model, graphs, samples):
    """Equal-weight per-plan regret among observed validation configurations only."""
    if not samples:
        raise ValueError("configuration regret requires validation samples")
    groups = defaultdict(list)
    was_training = model.training
    model.eval()
    try:
        with torch.no_grad():
            for sample in samples:
                latency = float(sample["latency_ms"])
                score = float(model(graphs[(sample["plan_key"], sample["config_key"])]))
                if not math.isfinite(score) or not math.isfinite(latency) or latency <= 0:
                    raise ValueError("validation scores/latencies must be finite and latency positive")
                groups[sample["plan_key"]].append((score, sample["config_key"], latency))
    finally:
        model.train(was_training)
    regrets = []
    for candidates in groups.values():
        chosen = min(candidates, key=lambda row: (-row[0], row[1]))
        best_latency = min(row[2] for row in candidates)
        regrets.append(chosen[2] / best_latency - 1)
    return statistics.mean(regrets)


def main():
    parser = argparse.ArgumentParser(description="Train the AutoDOP pairwise CostModel.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--split-manifest", help="fixed train/valid/test plan-key manifest; overrides valid-fraction")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--checkpoint-selection", choices=("final", "best-valid", "best-valid-regret"), default="final",
                        help="best-valid maximizes validation ranking; best-valid-regret minimizes mean per-plan observed-subset regret, then maximizes ranking, then keeps earliest epoch")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--hidden-dim", type=int, default=64)
    parser.add_argument("--heads", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--init-checkpoint", help="Same-partition CostModel warm start; retains its train-only feature metadata")
    parser.add_argument("--allow-appended-training-plans", action="store_true",
                        help="with --init-checkpoint, permit only a strict train-plan superset; freeze validation/test and feature metadata")
    parser.add_argument("--pair-weighting", choices=("uniform", "plan-balanced"), default="uniform",
                        help="plan-balanced retains every pair but equalizes total loss weight per plan")
    parser.add_argument(
        "--valid-fraction",
        type=float,
        default=0.15,
        help="fraction of plans held out as validation set, split by plan_key (default 0.15)",
    )
    parser.add_argument(
        "--accuracy-gate",
        type=float,
        default=0.0,
        help="minimum validation ranking accuracy; 0 = report only (default). "
             "When > 0 and the accuracy is below the gate, training fails and no artifact "
             "is produced",
    )
    parser.add_argument(
        "--with-baseline",
        action="store_true",
        help="additionally train the LatencyRegressionModel (MSE) and compare ranking "
             "accuracy on the same validation pairs",
    )
    args = parser.parse_args()

    if args.allow_appended_training_plans and not args.init_checkpoint:
        raise ValueError("appended training initialization requires --init-checkpoint")
    if args.epochs <= 0 or args.batch_size <= 0 or args.threads <= 0:
        raise ValueError("epochs, batch-size and threads must be positive")
    torch.set_num_threads(args.threads)
    random_generator = random.Random(args.seed)
    torch.manual_seed(args.seed)
    samples, pairs, metadata = load_dataset(args.dataset)
    if not pairs:
        raise ValueError("dataset contains no ranking pairs")
    training = make_training_contract(
        samples, pairs, args.valid_fraction, args.seed, split_manifest=args.split_manifest)
    train_samples, train_pairs = select_partition(samples, pairs, training, "train")
    valid_samples, valid_pairs = select_partition(samples, pairs, training, "valid")
    valid_plan_keys = training["valid_plan_keys"]
    metadata = build_metadata(train_samples, dop_classes=metadata["dop_classes"])
    if not train_pairs:
        raise ValueError("valid-fraction leaves no training pairs")
    pair_weights = plan_pair_weights(train_pairs) if args.pair_weighting == "plan-balanced" else None
    if pair_weights is not None:
        training["pair_weighting"] = {"mode": args.pair_weighting,
                                      "weights_by_plan": pair_weights,
                                      "pair_count": len(train_pairs),
                                      "scope": "Equal weight per plan with ranking pairs; no observations removed"}
    if args.checkpoint_selection in ("best-valid", "best-valid-regret") and not valid_pairs:
        raise ValueError(f"{args.checkpoint_selection} checkpoint selection requires validation pairs")
    if not valid_pairs:
        print(f"WARN: validation set is empty (only {len({p['plan_key'] for p in pairs})} plan(s)); "
              "accuracy gate is not evaluated")

    initial_model, initialization = None, None
    if args.init_checkpoint:
        initial_model, metadata, initialization = load_initial_cost(
            args.init_checkpoint, training, metadata, args.hidden_dim, args.heads, args.device,
            allow_appended_training_plans=args.allow_appended_training_plans)
        training["initialization"] = initialization
    sample_by_key = {(sample["plan_key"], sample["config_key"]): sample for sample in samples}
    graphs = {
        key: build_graph(sample, metadata, device=args.device)
        for key, sample in sample_by_key.items()
    }
    model = (initial_model if initial_model is not None else
             CostModel(metadata, {"hidden_dim": args.hidden_dim, "heads": args.heads}).to(args.device))
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    best_state, best_accuracy, selected_epoch = None, -1.0, args.epochs
    history = []
    best_regret_key = (float("inf"), float("inf"))
    if initial_model is not None and args.checkpoint_selection != "final":
        best_accuracy = pair_rank_accuracy(model, graphs, valid_pairs)
        initial_row = {"epoch": 0, "valid_rank_accuracy": best_accuracy}
        if args.checkpoint_selection == "best-valid-regret":
            initial_regret = validation_choice_regret(model, graphs, valid_samples)
            best_regret_key = (initial_regret, -best_accuracy)
            initial_row["valid_mean_subset_regret"] = initial_regret
        history.append(initial_row)
        selected_epoch = 0
        best_state = {name: tensor.detach().clone() for name, tensor in model.state_dict().items()}

    for epoch in range(1, args.epochs + 1):
        random_generator.shuffle(train_pairs)
        total_loss = 0.0
        for offset in range(0, len(train_pairs), args.batch_size):
            optimizer.zero_grad()
            losses = []
            for pair in train_pairs[offset:offset + args.batch_size]:
                plan_key = pair["plan_key"]
                fast = model(graphs[(plan_key, pair["fast_config_key"])])
                slow = model(graphs[(plan_key, pair["slow_config_key"])])
                fast_sample = sample_by_key[(plan_key, pair["fast_config_key"])]
                slow_sample = sample_by_key[(plan_key, pair["slow_config_key"])]
                pair_loss = dynamic_margin_ranking_loss(
                    fast, slow,
                    pair.get("fast_latency_ms", fast_sample["latency_ms"]),
                    pair.get("slow_latency_ms", slow_sample["latency_ms"]),
                )
                losses.append(pair_loss * pair_weights[plan_key] if pair_weights is not None else pair_loss)
            loss = torch.stack(losses).mean()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach()) * len(losses)
        if valid_pairs:
            valid_accuracy = pair_rank_accuracy(model, graphs, valid_pairs)
            row = {"epoch": epoch, "valid_rank_accuracy": valid_accuracy}
            improved = args.checkpoint_selection == "best-valid" and valid_accuracy > best_accuracy
            if args.checkpoint_selection == "best-valid-regret":
                regret = validation_choice_regret(model, graphs, valid_samples)
                row["valid_mean_subset_regret"] = regret
                regret_key = (regret, -valid_accuracy)
                improved = regret_key < best_regret_key
                if improved:
                    best_regret_key = regret_key
            history.append(row)
            if improved:
                best_accuracy, selected_epoch = valid_accuracy, epoch
                best_state = {name: tensor.detach().clone() for name, tensor in model.state_dict().items()}
            print(f"epoch={epoch} loss={total_loss / len(train_pairs):.6f} "
                  f"valid_rank_acc={valid_accuracy * 100:.2f}%")
        else:
            print(f"epoch={epoch} loss={total_loss / len(train_pairs):.6f}")

    if best_state is not None:
        model.load_state_dict(best_state)
    rank_accuracy = pair_rank_accuracy(model, graphs, valid_pairs) if valid_pairs else None
    if rank_accuracy is not None:
        print(f"final_rank_acc={rank_accuracy * 100:.2f}% "
              f"(valid pairs={len(valid_pairs)}, plans={len(valid_plan_keys)})")

    selected_regret = (validation_choice_regret(model, graphs, valid_samples)
                       if args.checkpoint_selection == "best-valid-regret" else None)

    if args.accuracy_gate > 0.0:
        if rank_accuracy is None:
            print(f"ERROR: --accuracy-gate={args.accuracy_gate} requires a non-empty validation set",
                  file=sys.stderr)
            sys.exit(1)
        if rank_accuracy < args.accuracy_gate:
            print(f"ERROR: validation ranking accuracy {rank_accuracy * 100:.2f}% is below "
                  f"--accuracy-gate {args.accuracy_gate * 100:.2f}%; no artifact is produced",
                  file=sys.stderr)
            sys.exit(1)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, "cost", model, metadata, training=training)
    output.with_suffix(".split.json").write_text(
        json.dumps(training, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.with_suffix(".selection.json").write_text(json.dumps({
        "mode": args.checkpoint_selection, "selected_epoch": selected_epoch,
        "epochs_run": args.epochs, "valid_rank_accuracy": rank_accuracy,
        "valid_mean_subset_regret": selected_regret,
        "history": history, "checkpoint_sha256": file_sha256(output),
        "criterion": ("fixed validation mean per-plan observed-subset regret; lower is better, ranking breaks ties, then earliest epoch"
                      if args.checkpoint_selection == "best-valid-regret" else
                      "fixed internal validation pair accuracy; earliest epoch wins ties"
                      if args.checkpoint_selection == "best-valid" else "last requested epoch"),
        "validation_gradients_used": False,
        "initialization": initialization,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"saved={output}")

    if args.with_baseline:
        baseline = LatencyRegressionModel(
            metadata, {"hidden_dim": args.hidden_dim, "heads": args.heads}).to(args.device)
        baseline_optimizer = torch.optim.Adam(baseline.parameters(), lr=args.lr)
        baseline_samples = list(train_samples)
        for epoch in range(1, args.epochs + 1):
            random_generator.shuffle(baseline_samples)
            total_loss = 0.0
            for offset in range(0, len(baseline_samples), args.batch_size):
                baseline_optimizer.zero_grad()
                losses = []
                for sample in baseline_samples[offset:offset + args.batch_size]:
                    predicted = baseline(graphs[(sample["plan_key"], sample["config_key"])])
                    losses.append(functional.mse_loss(
                        predicted, torch.tensor(latency_target(sample), device=args.device)))
                loss = torch.stack(losses).mean()
                loss.backward()
                baseline_optimizer.step()
                total_loss += float(loss.detach()) * len(losses)
            if valid_pairs:
                print(f"baseline epoch={epoch} loss={total_loss / len(baseline_samples):.6f}")
        if valid_pairs:
            baseline_accuracy = pair_rank_accuracy(baseline, graphs, valid_pairs, baseline=True)
            print(f"rank_acc cost={rank_accuracy * 100:.2f}% "
                  f"baseline={baseline_accuracy * 100:.2f}%")
        else:
            print("rank_acc cost=n/a baseline=n/a (no validation pairs)")


if __name__ == "__main__":
    main()
