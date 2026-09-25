#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import DOP_CLASSES, build_dataset


def parse_dop_classes(value):
    return [int(dop) for dop in value.split(",") if dop.strip()]


def main():
    parser = argparse.ArgumentParser(description="Build an AutoDOP dataset from FE trace files.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pair-threshold", type=float, default=0.03)
    parser.add_argument(
        "--dop-classes",
        default=",".join(str(dop) for dop in DOP_CLASSES),
        help="comma-separated candidate DOPs (default 1,2,4,8,16,32)",
    )
    args = parser.parse_args()

    samples, pairs, _ = build_dataset(
        args.input, args.output, args.pair_threshold,
        dop_classes=parse_dop_classes(args.dop_classes))
    print(f"samples={len(samples)} pairs={len(pairs)} output={args.output}")


if __name__ == "__main__":
    main()
