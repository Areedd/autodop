# AutoDOP P0 closed-loop result — 2026-09-05

## Outcome

The current v2 training-to-serving workflow completed with real historical execution data.
This is an engineering validation, not a new cluster benchmark or a deployment.
P1 algorithm/engine constraint changes remain deferred as requested.

## Reproduce

```bash
python3 tools/autodop/scripts/run_pipeline.py \
  --dataset tools/autodop/data/processed/sf100_sensitive_v1 \
  --output-dir tools/autodop/runs/another_new_p0_run \
  --external-dataset tools/autodop/data/processed/tpch100_validation_v2 \
  --cost-epochs 50 --policy-epochs 150 --iterations 500 \
  --valid-fraction 0.2 --seed 0 --threads 1
```

The verified bundle is `runs/p0_closed_loop_20260905_final/`. Its manifest contains exact
executed commands, source hashes, dataset snapshot hashes, artifact hashes and successful
stage statuses. Inputs and outputs are retained locally in the ignored runs directory.
Historical models and datasets were preserved. The earlier `p0_closed_loop_20260905` attempt
failed at HTTP verification because the filesystem sandbox also prohibited sockets; it is
retained as a failed attempt and is not the accepted bundle. Final verification ran with
permission to bind an ephemeral localhost port.

## Data and results

| Item | Result |
|---|---|
| Source | Historical TPC-DS SF100 sensitive-query measurements |
| Corpus | 9 plans, 126 measured configurations, 572 ranking pairs |
| Split | 7 training plans; 2 held-out plans |
| Held-out ranking accuracy | 61/157 = 38.85% |
| External TPC-H ranking accuracy | 109/129 = 84.50%, only 3 plans |
| MCTS | 500 surrogate evaluations per training plan; exactly 7 labels |
| Policy exact teacher-vector agreement | 2/7 training plans = 28.57% |
| Policy fragment teacher agreement | 106/115 = 92.17% |
| HTTP serving | All 9 plans passed offline/online feature and output parity; legal DOPs |
| FE default confidence threshold | 6/9 plans reach 0.5; 3/9 would fall back |
| Tests | 70 passed, 6 subtests passed |

The low held-out accuracy and incomplete teacher-vector agreement are real negative results.
The three external test plans do not establish broad TPC-H generalization. Confidence is the
existing masked-softmax statistic, not a calibrated correctness estimate. These artifacts are
usable by the current server but are not certified for latency improvement or production rollout.
No live FE/BE deployment or query-latency measurement was performed in this task.

## Implemented P0 guarantees

- Training-only vocabulary and normalization; normalized historical features and stable row order.
- Persisted plan split and dataset fingerprint across cost training, MCTS and distillation.
- Exact teacher checkpoint hash, split identity and complete training-only label coverage.
- Evaluation uses checkpoint metadata, even with independently rebuilt external vocabularies.
- Reproducible runner stores independent train/external dataset snapshots and all stage logs.
- Automatic retraining regenerates teacher labels and retains sub-threshold/failed pending traces.
- Actual local HTTP handler verification checks feature parity, output parity, candidates and confidence.

## Artifacts

- `runs/p0_closed_loop_20260905_final/cost.pt`
- `runs/p0_closed_loop_20260905_final/cost.split.json`
- `runs/p0_closed_loop_20260905_final/labels.jsonl`
- `runs/p0_closed_loop_20260905_final/policy.pt`
- `runs/p0_closed_loop_20260905_final/evaluation_valid.json`
- `runs/p0_closed_loop_20260905_final/evaluation_external_0.json`
- `runs/p0_closed_loop_20260905_final/serving.json`
- `runs/p0_closed_loop_20260905_final/manifest.json`

See `P0_CLOSED_LOOP.md` for usage and retraining behavior. Future quality work should expand
independent measured plans and investigate ranking generalization and distillation errors before
claiming benchmark gains. Do not substitute historical heuristic/tree results for this model.
