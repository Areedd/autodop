# Export workload development

These generators investigate a development-informed hypothesis: distributed
aggregation followed by substantial business-response serialization can benefit
from additional pipeline parallelism. They do not model production query
frequencies and do not establish general superiority on TPC-H or TPC-DS.

| Generator | Output being audited | Population |
|---|---|---|
| `generate_json_summary_suite.py` | Flat named JSON aggregate records | All dated sales in the window |
| `generate_detail_export_suite.py` | Sorted numeric CSV statement details | Identified customers for customer statements; all purchases for order statements |
| `generate_financial_json_suite.py` | Nested financial API responses: identity, totals, ranges, and completeness | All dated sales in the window |
| `generate_financial_quality_suite.py` | Quarterly/semiannual totals, average per present row, and completeness percentages | All dated sales in the window |

Each grammar covers customer, order, customer-product, customer-day,
customer-category, and customer-month outputs across store, catalog, and web.
Two additional scan/star reporting controls retain a 25% weight. These six export
families share an aggregation-oriented shape; semantic diversity is not a claim
of coverage of all relational operators. Earlier join, cohort, ranking, and
reconciliation workloads and their unsuccessful outcomes remain available in the
campaign directory.

Generation freezes 24 development queries (1998), 24 validation queries (1999),
and 120 test queries (2000–2002). The held-out dates share the same grammar; they
are parameter holdouts, not unseen templates. Detail batches and nested financial
responses are bounded to 6/12 months; flat JSON summaries also include 24-month
test windows. Financial quality reports use 3/6-month windows sampled directly
over the allowed calendar. Every manifest
records actual window parameters, SQL/source hashes, seed, family weights, and
baseline identities. Generation never reads execution timings.

For detail exports, numeric cells and the explicit `<NULL>` marker have no comma
or newline ambiguity. `ARRAY_SORT(ARRAY_AGG(...))` preserves duplicate lines and
removes aggregation-order dependence before LF-separated serialization. There
is no latency-dependent limit or row sampling. Financial JSON includes SQL nulls
and encodes numbers as strings with the frozen StarRocks formatter. Summed
60-bit SHA256 prefixes are compact audit indicators, not collision-free equality
proofs or a substitute for the source-data audit.

The predeclared target is at least 20% lower mean, pooled p50, p90, and p99
client latency for `policy_static`, separately against Native and uniform DOP4.
Uniform DOP8 is a diagnostic comparator and must remain visible. A gain over
Native/DOP4 alone cannot establish an advantage over the best fixed DOP.
Measurements are serial, with warmup separate from formal repetitions. Model
fitting, syntax checks, and profiling do not overlap latency runs. Errors,
fallbacks, incomplete slots, and unsuccessful workloads remain in the evidence.

The initial development screen selects one query per family with rotating
channels before timing. Its eight queries are insufficient for a held-out p99
claim. Independent verification and `scripts/assess_workload_target.py` check
completeness and the dual-baseline thresholds. A qualifying final test requires
the frozen full test manifest and at least 1000 formal samples per mode; this
minimum count is not a statistical confidence guarantee.

The initial annual financial eight-query screen passed the target, but expansion
to all channels hit the BE memory limit on a six-month store customer-product
query during the prepass with AutoDOP disabled and DOP4. That version is unsuccessful as a complete
workload and its failure remains recorded. The quality revision reduces aggregate
states from 25 to 13: SUM/COUNT support averages and completeness after grouping,
instead of retaining per-measure MIN/MAX states. Its narrower reporting scope is
declared before any validation/test measurement. It starts with all24 development
queries; the earlier eight-query screen cannot establish its outcome.
The same six-month 1998 failure window is also frozen as a separate resource
boundary check before holdout, because the new store product development sample
uses three months. This check is excluded from target latency statistics; its
failure blocks further holdout testing.

Current artifacts and outcomes are recorded in
`runs/target20_iteration_20260906/DEVELOPMENT_STATUS.md`. No generator is promised
to reach the target: measured outcomes determine whether the hypothesis holds.

The financial-quality frozen test completed on 2026-09-07: all 4800 executions
were independently verified (120 queries, nine formal repeats plus one warmup
per mode). The learned policy reduced mean/p50/p90/p99 by
31.74%/20.62%/34.23%/37.41% against Native and
31.94%/21.89%/33.11%/37.73% against predeclared DOP4. These are point estimates
on the declared date-parameter holdout. Against diagnostic DOP8 the reductions
were only 1.26%/0.61%/0.90%/2.28%; scan/star controls retain small regressions.
The full results, protocol, negative history and reproducible CSV exports are
linked from [the final report](runs/target20_iteration_20260906/RESULT.md).
