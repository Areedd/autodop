# TPC-DS SF100 deployment audit and repair

Date: 2026-07-23  
Database: `tpcds_sf100`  
StarRocks: 3.5.17, one FE and three same-host BEs

## Result

The deployment was not correct before this repair. All 24 canonical tables and
their row counts were present, but six sales/returns fact tables had been loaded
by physical table position even though their StarRocks key columns had been
moved to the front of the DDL. This shifted every source field in those tables.

The six tables were reloaded with explicit TPC-DS source-column mappings,
validated, and atomically swapped into place. The final semantic audit reports
`deployment_valid=true`. All 24 canonical tables also have healthy sampled CBO
statistics. After acceptance, the six old misloaded rollback tables were
deleted; a post-cleanup audit confirms that exactly the 24 canonical tables
remain and the deployment is still valid.

## Root cause

The original Stream Load script did not send a `columns` mapping. For example,
the TPC-DS file order for `store_sales` starts with
`ss_sold_date_sk, ss_sold_time_sk, ss_item_sk, ...`, while the StarRocks table
starts with `ss_item_sk, ss_ticket_number, ss_sold_date_sk, ...` to satisfy the
DUPLICATE KEY prefix rule.

Before repair, `store_sales.ss_item_sk` therefore contained date keys
2450816-2452642 instead of item keys 1-204000, and
`store_sales.ss_sold_date_sk` contained item keys 1-204000. The same failure
affected `catalog_sales`, `web_sales`, and the three returns tables. `inventory`
and all dimension tables used the source order and were correct.

## Repaired tables

| Table | Rows | Date-key range | Item-key range | Filtered rows | Load time |
|---|---:|---:|---:|---:|---:|
| `catalog_returns` | 14,404,374 | 2450821-2452921 | 1-204000 | 0 | 29.714 s |
| `catalog_sales` | 143,997,065 | 2450815-2452654 | 1-204000 | 0 | 439.110 s |
| `store_returns` | 28,795,080 | 2450820-2452822 | 1-204000 | 0 | 46.781 s |
| `store_sales` | 287,997,024 | 2450816-2452642 | 1-204000 | 0 | 678.863 s |
| `web_returns` | 7,197,670 | 2450820-2453002 | 1-204000 | 0 | 13.202 s |
| `web_sales` | 72,001,237 | 2450816-2452642 | 1-204000 | 0 | 222.474 s |

Each source field was first assigned a temporary name and then converted with
`nullif(source, '')`. This preserves the TPC-DS empty-field-as-NULL convention
while keeping `strict_mode=true` and `max_filter_ratio=0`.

For each table the workflow was:

1. Create a shadow table with `CREATE TABLE ... LIKE`.
2. Stream Load with explicit standard source order.
3. Require successful transaction, zero filtered rows, unchanged row count,
   and valid date/item key domains.
4. Atomically install the replacement with `ALTER TABLE ... SWAP WITH ...`.
5. Retain the old table under the suffix `__misloaded_20260723` during
   validation, then delete it after acceptance and a final canonical-table
   check.

## Referential validation

Every non-NULL date and item key in the six repaired tables matched its
dimension row:

| Table | Rows | Non-NULL/matched date keys | Non-NULL/matched item keys |
|---|---:|---:|---:|
| `catalog_returns` | 14,404,374 | 14,404,374 / 14,404,374 | 14,404,374 / 14,404,374 |
| `catalog_sales` | 143,997,065 | 143,277,484 / 143,277,484 | 143,997,065 / 143,997,065 |
| `store_returns` | 28,795,080 | 27,787,616 / 27,787,616 | 28,795,080 / 28,795,080 |
| `store_sales` | 287,997,024 | 275,041,999 / 275,041,999 | 287,997,024 / 287,997,024 |
| `web_returns` | 7,197,670 | 6,873,485 / 6,873,485 | 7,197,670 / 7,197,670 |
| `web_sales` | 72,001,237 | 71,983,315 / 71,983,315 | 72,001,237 / 72,001,237 |

Official TPC-DS queries also completed with non-empty results:

- Q3 (`store_sales` + date + item): 100 result rows; first aggregate
  `1998 / brand 2001001 / 1,339,035.35`.
- Q16 (`catalog_sales` + `catalog_returns`): 4,972 orders,
  shipping cost 22,356,998.78, net profit -4,344,024.33.
- Q80 (all three sales and returns channels): total sales 1,274,648,247.05,
  returns 63,416,660.72, profit -368,898,384.64.

## CBO statistics repair

The cluster's `_statistics_` database lacked the six core statistics tables.
The built-in creator chose replication 3 from the number of BEs, but all three
BEs share one host, so replica placement rejected the tables. The cluster's
runtime and persistent `default_replication_num` were set to 1, and the missing
tables were created with the exact 3.5.17 source schema and replication 1.

After re-running `ANALYZE SAMPLE`, all 24 canonical TPC-DS tables report
`Healthy=100%`. All three BEs report `Alive=true` and `Status=OK`.

## Artifacts

- Audit tool: `tools/autodop/audit_tpcds_deployment.py`
- Repair tool: `tools/autodop/repair_tpcds_fact_mapping.py`
- Statistics schema repair: `tools/autodop/repair_statistics_tables.sql`
- Statistics collection: `tools/autodop/analyze_tpcds_sf100.sql`
- Referential validation: `tools/autodop/validate_tpcds_sf100_repair.sql`
- Before audit: `tools/autodop/runs/tpcds_sf100_deployment_audit_before_semantic.json`
- Final audit: `tools/autodop/runs/tpcds_sf100_deployment_audit_after.json`
- Post-cleanup audit:
  `tools/autodop/runs/tpcds_sf100_deployment_audit_after_backup_cleanup.json`
- Successful load records:
  `tools/autodop/runs/tpcds_sf100_fact_repair_web_returns_retry2.json` and
  `tools/autodop/runs/tpcds_sf100_fact_repair_remaining.json`

After validation and explicit approval, all six `__misloaded_20260723` rollback
tables were deleted. No canonical table was removed; the database now contains
exactly the expected 24 TPC-DS tables.
