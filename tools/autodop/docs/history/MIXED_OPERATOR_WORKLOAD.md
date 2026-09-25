# Mixed-operator SQL workload

`generate_mixed_operator_suite.py` generates a deterministic workload against canonical TPC-DS retail tables. It does not read a model, confidence scores, or timings. It targets situations where fragment DOP might matter; it does not guarantee an advantage.

## Coverage

Eight equally weighted families: cross-channel customer revenue reconciliation (full outer join), cohort retention (EXCEPT/INTERSECT), return exposure (preaggregated composite-key left join), category ranking (window plus historical aggregation), multi-grain dashboard (UNION ALL), promotion participation (distinct/left join), scan control, and star-join control. The two controls comprise 25% of queries. All branches feed returned business metrics. No future empty periods, repeated synthetic padding, special optimizer hints, or result LIMIT shortcuts are generated.

Three sales channels rotate as the recent/history sources. Parameters vary recent windows (1/3/6 months), history windows (6/12/24 months), category/class/brand grouping and top-5/10/20 ranking. Returns aggregate the order/item composite key before joining to prevent accidental fanout. Dashboard grain 1 means daily; grain 2 means category/class/brand ID, as recorded by its parameters.

By default there are 24 development (`train`), 24 validation (`valid`), and 120 test queries. Date windows are disjoint across partitions: 1998, 1999, and 2000-2002. Grammar families are shared, so this measures parameter/time-window generalization, not unseen-template generalization. The workload describes retail analytical tasks, not observed production query frequencies.

## Generation

```bash
python3 tools/autodop/generate_mixed_operator_suite.py \
  --database autodop_tpcds_sf10_20260905 \
  --output-dir /tmp/mixed_operator_new --seed 20260906
```

Outputs include `all.json`, `train.json`, `valid.json`, `test.json`, and hashed SQL files. Existing nonempty output directories are refused. `--test-variants` controls test variants per family/channel. Database existence and populated time windows must be verified separately; SQL generation is offline.

## Evaluation contract

Primary method is the frozen current `policy_static`. Primary baselines are Native and predeclared uniform DOP 4; uniform DOP 8 is diagnostic. Use the same SQL, sessions/settings, hardware, schedule and result-digest validation across methods. No concurrent clients. Preserve fallback cases. Evaluate with existing `collect_trusted_dataset.py` and `collect_paper_evaluations.py`; independently verify the latter with `scripts/verify_paper_evaluations.py`.

```bash
python3 tools/autodop/scripts/assess_workload_target.py \
  --manifest /tmp/mixed_operator_new/test.json \
  --collection-dir /path/to/completed_evaluation \
  --output /tmp/target_assessment.json
```

A 20% latency reduction is `method / baseline <= 0.8`, equivalent to at least 1.25x speedup. All four statistics (arithmetic mean, p50, p90, p99) must pass against **both** primary baselines. Metrics use equal-count pooled formal client latencies, with linear-interpolation percentiles; query-median distributions are separately recorded. This is the latency distribution of the frozen workload, not each individual SQL's temporal p99. Missing, failed, duplicated or mismatched executions prevent passing; warmups are excluded.

Development success is never held-out success. The tool requires at least 100 distinct queries and 1,000 formal executions per mode for eligibility to report a held-out observed target. These minimums are descriptive guardrails, not confidence guarantees: repeated measurements share templates and cluster state. Independent replications and uncertainty estimates are still required for a stable tail-latency claim.

If development misses the target, keep the negative data and diagnose policy/teacher ranking rather than filtering queries or changing baseline DOP after seeing the outcome. Model retraining must use development data only, freeze before validation/test evaluation, and retain checkpoint lineage.

## Development extensions and retraining

The compute, cohort, integrity, and grouped-audit generators reuse the same manifest
builder, channel mappings, parameter split and 25% controls. Each is a separate,
development-informed workload profile. Their results do not establish a production
query-frequency distribution. `generate_grouped_audit_suite.py` covers customer,
order, customer-product, customer-day, customer-category and daily-to-customer
ledger rollups. Integrity and grouped-audit signatures are additive indicators,
not proofs of multiset equality. Null numeric fields are explicitly encoded;
text identifiers use delimiter-safe encoding. Date surrogate bounds must be checked
against the loaded `date_dim` before execution.

`scripts/prepare_fragment_headroom.py` freezes uniform, local, random and operator-group
DOP probes using plan structure, without consuming timings. An observed best probe
is diagnostic, not a deployable policy or an exhaustive oracle. Replay it independently
before concluding that a measured advantage is repeatable.

`scripts/merge_development_datasets.py` accepts independently verified development
datasets, selecting the whole source for each SQL by configuration coverage. It does
not select the fastest source or splice favorable observations. On subsequent
iterations, use `--preserve-validation-from` with the earlier pipeline run, then
pass the resulting `model_split.json` to `run_pipeline.py --split-manifest`.
This keeps earlier internal validation SQL excluded even if their physical plans
change. Outer validation and test SQL remain excluded from this merge entirely.

Current continuation artifacts live in `runs/target20_iteration_20260906`. Preserve
all earlier negative profile assessments there and in
`runs/mixed_operator_redesign_20260906`; a single development-query gain does not
satisfy the four-statistic dual-baseline target.

`generate_summary_audit_suite.py` hashes the aggregated export record, so the hash
depends on completed business aggregation. `generate_period_summary_suite.py` uses
6/12 month development/validation periods and 6/12/24 month test periods, including
the two controls; manifests record the interval actually rendered. The optional
named-JSON renderer and `generate_json_summary_suite.py` are a separate development
extension. JSON consistency is scoped to the frozen StarRocks formatter, not a
portable canonical JSON specification.

The period development comparison showed uniform8 reductions exceeding20% on all
four statistics against both primary baselines; see `period_uniform8_diagnostic.json`.
This is a diagnostic opportunity, **not** learned-policy success. The learned policy
still missed the target. Subsequent `--design scan-merge` probes cross scan widths
1/2/4/8 with downstream widths4/8, retain every query, and preserve both uniform4/8
anchors. The JSON performance extension was deferred while this opportunity is pursued.

Training supports `run_pipeline.py --cost-checkpoint-selection best-valid` (default
remains `final`). It chooses the earliest highest-scoring epoch on the fixed internal
validation pairs, uses no validation gradients, and writes the full selection history
and checkpoint hash to `cost.selection.json`. Outer test measurements do not participate.
