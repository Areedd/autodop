#!/usr/bin/env python3

import argparse
import random
import sys
from pathlib import Path

import torch
import torch.nn.functional as functional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import load_dataset, read_jsonl
from autodop.graph import build_graph
from autodop.model import PolicyModel, load_cost_model, save_checkpoint
from autodop.training import file_sha256, label_lineage, select_partition, validate_labels


def main():
    parser = argparse.ArgumentParser(description="Train the AutoDOP online PolicyModel.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument(
        "--cost-model",
        required=True,
        help="CostModel checkpoint used to initialize the shared graph encoder",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args()

    if args.epochs <= 0 or args.batch_size <= 0 or args.threads <= 0:
        raise ValueError("epochs, batch-size and threads must be positive")
    torch.set_num_threads(args.threads)
    random_generator = random.Random(args.seed)
    torch.manual_seed(args.seed)
    samples, pairs, _ = load_dataset(args.dataset)
    cost_model, checkpoint = load_cost_model(args.cost_model, args.device)
    training = checkpoint.get("training")
    samples, _ = select_partition(samples, pairs, training, "train")
    metadata = checkpoint["metadata"]
    labels = validate_labels(list(read_jsonl(args.labels)), samples, training,
                             args.cost_model, metadata["dop_classes"])
    sample_by_plan = {}
    for sample in samples:
        sample_by_plan.setdefault(sample["plan_key"], sample)
    plans = sorted(labels)

    policy_config = dict(checkpoint["model_config"])
    model = PolicyModel(metadata, policy_config).to(args.device)
    model.encoder.load_state_dict(cost_model.encoder.state_dict())
    optimizer = torch.optim.Adam([
        {"params": model.encoder.parameters(), "lr": args.lr * 0.1},
        {"params": model.classifier.parameters(), "lr": args.lr},
    ])
    graphs = {
        plan_key: build_graph(sample_by_plan[plan_key], metadata, unknown_dop=True, device=args.device)
        for plan_key in plans
    }
    class_index = {dop: index for index, dop in enumerate(metadata["dop_classes"])}

    for epoch in range(1, args.epochs + 1):
        random_generator.shuffle(plans)
        total_loss = 0.0
        for offset in range(0, len(plans), args.batch_size):
            optimizer.zero_grad()
            losses = []
            for plan_key in plans[offset:offset + args.batch_size]:
                target = torch.tensor(
                    [class_index[int(dop)] for dop in labels[plan_key]],
                    dtype=torch.long,
                    device=args.device,
                )
                losses.append(functional.cross_entropy(model(graphs[plan_key]), target))
            loss = torch.stack(losses).mean()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach()) * len(losses)
        print(f"epoch={epoch} loss={total_loss / len(plans):.6f}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, "policy", model, metadata, training={
        **training, "teacher": label_lineage(training, args.cost_model),
        "labels_sha256": file_sha256(args.labels), "policy_seed": args.seed,
    })
    print(f"saved={output}")


if __name__ == "__main__":
    main()
