#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import build_dataset


def main():
    parser = argparse.ArgumentParser(description="Build an AutoDOP dataset from FE trace files.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pair-threshold", type=float, default=0.03)
    args = parser.parse_args()

    samples, pairs, _ = build_dataset(args.input, args.output, args.pair_threshold)
    print(f"samples={len(samples)} pairs={len(pairs)} output={args.output}")


if __name__ == "__main__":
    main()
