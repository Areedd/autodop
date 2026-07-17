#!/usr/bin/env python3

import argparse
import random
import sys
from pathlib import Path

import torch
import torch.nn.functional as functional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import load_dataset
from autodop.graph import build_graph
from autodop.model import CostModel, save_checkpoint


def main():
    parser = argparse.ArgumentParser(description="Train the AutoDOP pairwise CostModel.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--hidden-dim", type=int, default=64)
    parser.add_argument("--heads", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    random_generator = random.Random(args.seed)
    torch.manual_seed(args.seed)
    samples, pairs, metadata = load_dataset(args.dataset)
    if not pairs:
        raise ValueError("dataset contains no ranking pairs")
    sample_by_key = {(sample["plan_key"], sample["config_key"]): sample for sample in samples}
    graphs = {
        key: build_graph(sample, metadata, device=args.device)
        for key, sample in sample_by_key.items()
    }
    model = CostModel(metadata, {"hidden_dim": args.hidden_dim, "heads": args.heads}).to(args.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    for epoch in range(1, args.epochs + 1):
        random_generator.shuffle(pairs)
        total_loss = 0.0
        for offset in range(0, len(pairs), args.batch_size):
            optimizer.zero_grad()
            losses = []
            for pair in pairs[offset:offset + args.batch_size]:
                plan_key = pair["plan_key"]
                fast = model(graphs[(plan_key, pair["fast_config_key"])])
                slow = model(graphs[(plan_key, pair["slow_config_key"])])
                losses.append(functional.softplus(-(fast - slow)))
            loss = torch.stack(losses).mean()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach()) * len(losses)
        print(f"epoch={epoch} loss={total_loss / len(pairs):.6f}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, "cost", model, metadata)
    print(f"saved={output}")


if __name__ == "__main__":
    main()
