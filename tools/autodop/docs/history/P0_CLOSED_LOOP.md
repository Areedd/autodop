# AutoDOP P0: training to serving

This workflow produces current v2 model artifacts from recorded execution data. It fixes
training/evaluation boundaries and teacher lineage; it does not change P1 engine constraints,
load-correction rounding, or reproduce the full paper experiments.

## Run the complete workflow

From the repository root:

```bash
python3 tools/autodop/scripts/run_pipeline.py \
  --dataset tools/autodop/runs/trusted_dataset_20260905/dataset/train_valid \
  --split-manifest tools/autodop/runs/trusted_dataset_20260905/dataset/split_manifest.json \
  --output-dir tools/autodop/runs/my_new_training_run \
  --external-dataset tools/autodop/runs/trusted_dataset_20260905/dataset/test \
  --cost-epochs 50 --policy-epochs 150 --iterations 500 \
  --seed 0 --threads 1
```

Use the rebuilt corpus described in [TRUSTED_DATASET.md](TRUSTED_DATASET.md). Older
SF100 sensitivity artifacts demonstrate pipeline execution only: their raw measurements
predate the fact-column repair and their labels mixed warmup/probe runs. Do not treat them
as verified performance evidence. `--split-manifest` preserves the new corpus's preselected
template split; omit it only for datasets that intentionally need a new random plan split.

Use a new output directory for every attempt. Alternatively use `--trace-dir PATH` in place
of `--dataset` to build from recorded FE traces. CPU threads default to one because these
are small variable-sized graphs. PyTorch is the only model dependency. HTTP verification
requires permission to open an ephemeral loopback port; it does not contact a remote service.

The runner snapshots input data and saves:

- `cost.pt`, `cost.split.json`: current cost model and reproducible plan split.
- `labels.jsonl`: MCTS labels for training plans only, with exact teacher identity.
- `policy.pt`: distilled policy with shared training features and teacher/label provenance.
- `evaluation_valid.json`: held-out ranking accuracy; `evaluation_external_*.json`: transfer results.
- `serving.json`: HTTP feature/output parity, legal decisions and confidence acceptance.
- `manifest.json`: commands, source/data/artifact checksums, stage status and logs.

A completed manifest means the pipeline and HTTP checks passed. It is **not** a deployment
or an assertion that the model improves database latency. Low confidence is recorded; FE
retains its existing fallback behavior. `--accuracy-gate` can require minimum held-out cost
accuracy; its default zero reports quality without gating artifacts.

## Independent evaluation and serving

```bash
python3 tools/autodop/scripts/evaluate_cost.py \
  --checkpoint tools/autodop/runs/my_new_training_run/cost.pt \
  --dataset tools/autodop/runs/my_new_training_run/dataset \
  --partition valid --output /tmp/autodop-validation.json

OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 tools/autodop/autodop_server.py \
  --policy-model tools/autodop/runs/my_new_training_run/policy.pt
```

Evaluation defaults to the persisted held-out split for a matching corpus. Use
`--partition external` explicitly for a different corpus; overlapping training plan keys
are rejected. `--partition all` explicitly reports a potentially in-sample metric. A v2
checkpoint without a split can still be scored with `all`, but cannot provide held-out
validation or seed a new teacher/student workflow; retrain it first.

The checkpoint's vocabulary and normalization are used for every evaluation and request.
Rebuilding test metadata does not change the features the model sees. Historical processed
fragments are normalized to current extraction defaults while preserving recorded identities.

## Automatic retraining

```bash
python3 tools/autodop/scripts/auto_retrain.py \
  --trace-dir /path/to/fe/traces \
  --dataset-dir /tmp/autodop-current-corpus \
  --output-dir tools/autodop/runs/retraining \
  --min-new-traces 50 --with-policy
```

`--with-policy` regenerates MCTS labels using the newly trained cost model; the old external
`--labels` option is intentionally removed. Successful runs are recorded in `latest_run`
in `retrain_state.json`; no live server is restarted automatically. Trace accounting assumes
append-only daily FE JSONL files. Pending lines accumulate below the threshold; state only
consumes them after successful training. A truncated file is counted from its new beginning.

## Validation

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tools/autodop/tests -q
```

The pipeline integration test uses localhost sockets. In environments that prohibit all
sockets, run with the appropriate local-network permission; do not substitute a mocked
HTTP result for the serving acceptance check.
