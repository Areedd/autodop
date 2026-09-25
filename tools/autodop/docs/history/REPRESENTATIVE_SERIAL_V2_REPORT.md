# TPC-DS SF100 representative serial AutoDOP v2

## Why v1 was rejected

The previous selected set was structurally varied but expression-homogeneous:
49 of 50 statements contained both `CRC32` and `CONCAT_WS`. The measured DOP
effect was real, but the workload was not representative enough. That set is
kept only as historical evidence and is not reused by v2.

## Construction method

The v2 generator starts from business tasks rather than a common CPU
expression. It emits short queries for financial reporting, pricing,
statistical monitoring, customer and geographic segmentation, merchandising,
time series, fulfillment, returns reconciliation, omnichannel analysis, and
data stewardship.

The data-stewardship family uses field-specific rules that occur in production:

- customer names, birth years, email local parts and domains;
- street, city, state, country, and postcode completeness;
- item descriptions, product names, taxonomy, and below-cost checks;
- store and web-page metadata;
- calendar flag consistency.

It deliberately includes negative controls. Plain monetary consistency checks,
simple scans, high-cardinality normalized-string grouping, and several rollups
did not scale by 30% and are not presented as successful queries.

## Diversity audit

- candidates: 144;
- workload classes: 36;
- normalized physical plans: 54;
- average length: 7.23 lines; maximum: 13 lines;
- `CRC32`: 0 occurrences;
- `CONCAT_WS`: 0 occurrences;
- operators include inner/left/right/full/semi/anti joins, `UNION`,
  `EXCEPT`, `INTERSECT`, scalar subplans, grouping sets, rollup, cube, Top-N,
  and 20 window queries;
- expression families include conditional quality rules, approximate
  cardinality, statistical aggregates, `REGEXP_REPLACE`, `SPLIT_PART`,
  `PARSE_URL`, date formatting, substring extraction, and text normalization.

All 144 statements passed `EXPLAIN`; the three approximate-percentile
statements are retained as negative examples but are excluded from certified
sets because their result digest changes with DOP.

## Environment and method

- correct `tpcds_sf100`;
- three original BEs only, heartbeat ports 9050/9051/9052;
- each BE reports eight cores;
- client concurrency is one;
- query cache, spill, and runtime adaptive DOP are disabled;
- baseline is `pipeline_dop=0`, which resolves to DOP 4;
- model maximum DOP is 8;
- one representative physical plan was labelled from measured
  `[1, 8, 1]` versus `[4, 4, 4]` runs;
- the retrained depth-1 decision tree separates fragments by estimated
  fragment cost.

The final trace audit contains 54 AutoDOP executions. Every execution installed
the mixed fragment configuration:

```text
F00 = 8   large fact scan + join + local aggregate/project
F01 = 1   small dimension scan
F03 = 1   final merge/result fragment
```

This is a fragment configuration, not an implicit change to global DOP 8.

## Five-repetition result

The nine-query validation suite used one warmup and five measured repetitions
per mode with alternating execution order.

| metric | DOP=0 / resolved 4 | learned fragment AutoDOP | improvement |
|---|---:|---:|---:|
| mean | 2231.228 ms | 1352.270 ms | 39.393% |
| median | 2103.445 ms | 1299.210 ms | 38.234% |
| P99 | 4572.019 ms | 2649.892 ms | 42.041% |

Eight of nine statements also pass the stricter per-query
mean/median/P99 minimum threshold of 30%. `audit_web_page_metadata` has a
29.744% worst per-query metric but remains in the nine-query suite result
above; it is excluded from `validated_fragment8`.

## Attribution

The positive queries scan a large fact table and apply several independent,
field-specific validation or normalization rules after joining a small
dimension. The expensive fragment has 48 to 192 scan ranges and is
CPU-dominated, so raising only that fragment from 4 to 8 reduces elapsed time.
The small dimension scan and single-row final merge do not benefit from extra
drivers and remain at DOP 1.

The negative controls establish that the result is not caused by query length:
plain decimal arithmetic improves by roughly 0%, while high-cardinality string
grouping can become slower at DOP 8 because of shuffle and merge overhead.

## Fifty-structure wide screen

The retrained fragment model was also applied to the 50 structurally expanded
queries with one warmup and two alternating repetitions. Only four passed the
strict per-query mean/median/P99 threshold:

| query | minimum improvement |
|---|---:|
| `return_inner_reconciliation` | 32.805% |
| `return_full_reconciliation` | 33.573% |
| `channel_three_way_exclusive` | 33.262% |
| `channel_customer_intersection` | 36.408% |

The complete 50-query wide screen improved mean by 10.920% and median by
16.394%, while P99 regressed by 1.147%. This negative result is retained in
`retrained_fragment_expanded50_r1.json`. It shows that ordinary segmentation,
geographic aggregation, windows, and low-cost dimension joins cannot be
presented as 30% successes merely because they have different plans.

## Current limitation

The generator now exceeds 50 candidate physical structures, but this report
does not claim that 50 distinct physical plans have each passed the 30%
per-query threshold. The fully certified data-stewardship set contains eight
SQL instances sharing the same successful three-fragment topology; four
additional relational structures pass in the wide screen. Expanding the
certified physical-topology count remains separate work. The v1 hash queries,
minor literal changes, and repeated audit wrappers must not be used to fill
that count.
