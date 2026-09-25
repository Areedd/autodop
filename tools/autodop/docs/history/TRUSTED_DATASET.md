# Rebuilding verified AutoDOP data

Use a fresh database and an immutable, preselected SQL manifest. Historical raw traces
are not interchangeable with verified formal measurements. The September 2026 run lives
under `runs/trusted_dataset_20260905/`; its result report records the actual acceptance counts.

## Load and audit

`prepare_trusted_tpcds.py` creates a **new** database, clones the existing table schemas,
and streams all 24 dbgen files using explicit source-column mappings. Empty fields become
NULL. Strict loading allows zero filtered rows. The loader saves source SHA256 before/after
loading, source/import/table row counts, key ranges, and non-null date/item foreign-key checks.
Existing source databases are not repaired or overwritten.

```bash
python3 tools/autodop/prepare_trusted_tpcds.py \
  --database YOUR_NEW_DATABASE --source-database tpcds_sf10 \
  --data-dir ../starrocks_deploy/tpcds_sf10 --output /path/to/new/load.json
python3 tools/autodop/audit_tpcds_deployment.py \
  --database YOUR_NEW_DATABASE --semantic-checks --output /path/to/new/audit.json
```

Collect table statistics before measurements. Retain the FE jar checksum, source revision
and dirty-source hashes, BE image and CPU/memory allocations, topology and statistics evidence.
Fix the FE `priority_networks` to its existing metadata identity if Docker bridge enumeration
changes on restart; do not initialize another metadata directory to bypass that mismatch.

## Collect

The JSON manifest contains `queries` with `id`, `sql_file`, `sql_sha256`, `database`, and
`split` (`train`, `valid`, `test`). Freeze membership before measuring. SQL snapshots and hashes
bind the selection to each execution. Collection requires an audit for the same database.

```bash
python3 tools/autodop/collect_trusted_dataset.py \
  --manifest /path/to/query_manifest.json --audit /path/to/new/audit.json \
  --output-dir /path/to/new/collection --configs 16 --dops 1,2,4,8 \
  --warmup 1 --repetitions 3 --seed 20260905
```

The temporary fixed-DOP server is an offline experimental intervention. The collector restores
the prior FE settings afterward. It records native baseline, discovery, warmup and measured
phases separately; the configuration list is frozen without consulting latency. Each formal
execution must finish successfully, match the native result multiset, match its exact FE trace
query ID, retain the frozen plan, and verify installed/actual DOP. Decimal results are exact;
floating-point digests use eight significant digits. Native uniform scan caps are recorded
separately; labels describe the installed requested fragment vector, not an invented actual DOP.

An excluded query contributes no labels, even if some configurations completed. All attempts
remain in `provenance/executions.jsonl` and `raw/`. Only `measured/accepted.jsonl` is a dataset
input. Do not point the generic historical builder or automatic retraining at the raw directory.

## Build and train

```bash
python3 tools/autodop/scripts/build_trusted_dataset.py \
  --input /path/to/new/collection/measured/accepted.jsonl \
  --manifest /path/to/query_manifest.json \
  --collection /path/to/new/collection/collection.json --output /path/to/new/dataset

python3 tools/autodop/scripts/run_pipeline.py \
  --dataset /path/to/new/dataset/train_valid \
  --split-manifest /path/to/new/dataset/split_manifest.json \
  --external-dataset /path/to/new/dataset/test --output-dir /path/to/new/training
```

The builder rechecks the completed collection, accepted-query status, original manifest and
SQL snapshots, native result identity, every frozen configuration and all three repetition
slots. Latency labels use formal-only medians. Ranking pairs require a median gap above 3%
and disjoint observed ranges (`fast.max < slow.min`); this is not a statistical confidence
claim. Overlapping configurations remain in samples but do not supply an unstable pair.

`sample_evidence.jsonl` links each label to query IDs and repetition latencies. `summary.json`
reports sample/pair counts, discarded unstable pairs, missing templates and variance.
`split_manifest.json` preserves the preselected partitions. Use `train_valid` with the explicit
split for training; `test` is external evaluation and `all` is for corpus inspection. Vocabulary
and normalization are refit using training samples only by the cost trainer.

This corpus establishes provenance and repeatability for one hardware, scale and concurrency
setting. It does not establish cross-scale or concurrent-load generalization, and passing the
data checks does not establish that a trained policy improves latency.
