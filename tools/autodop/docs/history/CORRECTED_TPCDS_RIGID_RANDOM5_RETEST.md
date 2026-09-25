# Corrected TPC-DS rigid-query random-5 retest

Date: 2026-07-23  
Database: `tpcds_sf100` (24 canonical tables, post-repair audit valid)  
Source suite: `structural_100_20260722/selected_100_v5`  
Sampling: Python `random.Random(20260723).shuffle`, first five queries

## Result

None of the five randomly selected queries retained the previously certified
30% advantage on the corrected physical data. Fragment AutoDOP was close to
global DOP=1 and substantially slower than global DOP=2/4/8. Global DOP=8 was
the best fixed setting for all five queries.

The retest used two independent runs. Each run used one warmup and four measured
executions per query and mode. The modes were global DOP=1/2/4/8 and fragment
AutoDOP, giving eight measured samples per query and mode after pooling. Query
cache, runtime adaptive DOP and spill were disabled; execution order was rotated
and alternately reversed. Unqualified TPC-DS names were resolved directly in
the corrected `tpcds_sf100` database.

## Random sample

| Query | Historical stable minimum | Corrected fragment median | Corrected DOP=8 median | Fragment median vs DOP=8 | Corrected stable minimum |
|---|---:|---:|---:|---:|---:|
| `extra_union_distinct_dimension_join_stage_audit` | 56.123% | 14,155.832 ms | 7,062.865 ms | -100.426% | -104.108% |
| `extra_having_semijoin_stage_audit` | 41.157% | 14,666.772 ms | 7,488.560 ms | -95.856% | -97.777% |
| `extra_three_scalar_cross_join_stage_audit` | 34.192% | 14,287.777 ms | 7,053.878 ms | -102.552% | -102.916% |
| `tpcds_q71_stage_audit` | 48.090% | 15,057.585 ms | 8,026.099 ms | -87.608% | -88.552% |
| `extra_three_fact_inner_join_stage_audit` | 37.755% | 14,874.861 ms | 7,744.676 ms | -92.066% | -92.066% |

The stable minimum is the worst mean/median/P99 improvement across both
independent runs, the pooled samples and all four fixed DOP baselines. All five
queries failed the 30% threshold in both independent runs.

## Five-query average latency

| Mode | Mean | Median | P90 | P99 |
|---|---:|---:|---:|---:|
| Global DOP=1 | 14,332.297 ms | 14,294.660 ms | 14,559.243 ms | 14,721.671 ms |
| Global DOP=2 | 10,176.193 ms | 10,171.504 ms | 10,433.563 ms | 10,547.751 ms |
| Global DOP=4 | 7,872.752 ms | 7,939.123 ms | 8,120.061 ms | 8,187.797 ms |
| Global DOP=8 | 7,490.526 ms | 7,475.216 ms | 7,663.399 ms | 7,696.586 ms |
| Fragment AutoDOP | 14,630.465 ms | 14,608.565 ms | 14,816.665 ms | 14,893.588 ms |

Relative to DOP=8, fragment AutoDOP changed mean/median/P90/P99 by
`-95.320% / -95.427% / -93.343% / -93.509%`. Relative to DOP=1, the changes
were `-2.080% / -2.196% / -1.768% / -1.168%`.

## Attribution

The old composite envelope contains a three-channel date/customer
`DISTINCT/EXCEPT` reconciliation branch. With the formerly displaced fact-table
keys, its dimension joins produced an empty or nearly empty accepted set, so
high DOP mostly added driver, exchange and hash-state overhead. The old policy
therefore assigned DOP=1 to the `EXCEPT` fragment and its complete input
subgraph.

After the fact tables were repaired, those joins process the real matching
sales population. The reconciliation branch is now throughput-bound and scales
positively through DOP=8. The unchanged policy still forces that large input
closure to DOP=1, so the fragment run behaves like global DOP=1 while every
higher fixed DOP is faster. The same reversal across five different business
cores shows that the shared rigid envelope, rather than the individual core
plan, dominated the historical advantage.

## Validation

- Both runs produced identical result digests and result shapes for every mode.
- All 50 fragment traces reached EOF; errors and spill records were zero.
- All 50 traces used mixed fragment DOP values `{1, 4, 8}`.
- Decided, installed and BE actual instance DOP mismatches were zero.
- Each trace directory contains five unique plans and five traces per plan.
- The failed initial launch attempt was excluded because the isolated decision
  server could not bind its port under sandboxing; the two official runs were
  executed after a successful health check in a new output directory.

## Artifacts

- Run A: `runs/corrected_tpcds_random5_20260723_retry/run_a.json`
- Run B: `runs/corrected_tpcds_random5_20260723_retry/run_b.json`
- Pooled result: `runs/corrected_tpcds_random5_20260723_retry/aggregate.json`
- Trace validation:
  `runs/corrected_tpcds_random5_20260723_retry/trace_validation.json`

