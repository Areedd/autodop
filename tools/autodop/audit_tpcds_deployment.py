#!/usr/bin/env python3
"""Audit a StarRocks TPC-DS deployment against the dbgen file layout."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


FACT_KEYS = {
    "catalog_returns": ("cr_returned_date_sk", "cr_item_sk"),
    "catalog_sales": ("cs_sold_date_sk", "cs_item_sk"),
    "inventory": ("inv_date_sk", "inv_item_sk"),
    "store_returns": ("sr_returned_date_sk", "sr_item_sk"),
    "store_sales": ("ss_sold_date_sk", "ss_item_sk"),
    "web_returns": ("wr_returned_date_sk", "wr_item_sk"),
    "web_sales": ("ws_sold_date_sk", "ws_item_sk"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="tpcds_sf100")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9030)
    parser.add_argument("--user", default="root")
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("/home/shaokun/starrocks_deploy/tpcds-kit/tools/tpcds.sql"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--ignore-table-suffix",
        action="append",
        default=[],
        help="exclude retained backup/staging tables from the deployment table set",
    )
    parser.add_argument(
        "--semantic-checks",
        action="store_true",
        help="scan fact keys and verify that their ranges fit the dimensions",
    )
    return parser.parse_args()


def mysql(args: argparse.Namespace, query: str) -> list[list[str]]:
    command = [
        "mysql",
        "-h",
        args.host,
        "-P",
        str(args.port),
        f"-u{args.user}",
        "-N",
        "-B",
        "-e",
        query,
    ]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def source_columns(schema_path: Path) -> dict[str, list[str]]:
    text = schema_path.read_text()
    tables: dict[str, list[str]] = {}
    pattern = re.compile(
        r"create\s+table\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    for name, body in pattern.findall(text):
        columns = []
        for line in body.splitlines():
            match = re.match(r"\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+", line)
            if match and match.group(1).lower() not in {"primary", "foreign"}:
                columns.append(match.group(1).lower())
        tables[name.lower()] = columns
    return tables


def deployed_columns(args: argparse.Namespace) -> dict[str, list[str]]:
    rows = mysql(
        args,
        "SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA='{args.database}' ORDER BY TABLE_NAME,ORDINAL_POSITION",
    )
    tables: dict[str, list[str]] = {}
    for table, column in rows:
        tables.setdefault(table, []).append(column)
    return tables


def table_rows(args: argparse.Namespace) -> dict[str, int]:
    rows = mysql(
        args,
        "SELECT TABLE_NAME,TABLE_ROWS FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA='{args.database}' ORDER BY TABLE_NAME",
    )
    return {table: int(count) for table, count in rows}


def first_difference(expected: list[str], actual: list[str]) -> dict[str, object] | None:
    for index in range(max(len(expected), len(actual))):
        left = expected[index] if index < len(expected) else None
        right = actual[index] if index < len(actual) else None
        if left != right:
            return {"position": index + 1, "source": left, "table": right}
    return None


def semantic_checks(args: argparse.Namespace, tables: set[str]) -> dict[str, object]:
    dimension_row = mysql(
        args,
        f"SELECT MIN(d_date_sk),MAX(d_date_sk) FROM `{args.database}`.date_dim",
    )[0]
    item_row = mysql(
        args,
        f"SELECT MIN(i_item_sk),MAX(i_item_sk) FROM `{args.database}`.item",
    )[0]
    date_min, date_max = map(int, dimension_row)
    item_min, item_max = map(int, item_row)
    checks: dict[str, object] = {
        "date_dimension_range": [date_min, date_max],
        "item_dimension_range": [item_min, item_max],
        "facts": {},
    }
    for table, (date_key, item_key) in FACT_KEYS.items():
        if table not in tables:
            continue
        row = mysql(
            args,
            "SELECT COUNT(*),"
            f"MIN(`{date_key}`),MAX(`{date_key}`),"
            f"MIN(`{item_key}`),MAX(`{item_key}`) "
            f"FROM `{args.database}`.`{table}`",
        )[0]
        values = [None if value == "NULL" else int(value) for value in row]
        _, fact_date_min, fact_date_max, fact_item_min, fact_item_max = values
        date_valid = (
            fact_date_min is None
            or fact_date_max is None
            or (date_min <= fact_date_min <= fact_date_max <= date_max)
        )
        item_valid = (
            fact_item_min is None
            or fact_item_max is None
            or (item_min <= fact_item_min <= fact_item_max <= item_max)
        )
        checks["facts"][table] = {
            "rows": values[0],
            "date_key": date_key,
            "date_range": [fact_date_min, fact_date_max],
            "date_range_valid": date_valid,
            "item_key": item_key,
            "item_range": [fact_item_min, fact_item_max],
            "item_range_valid": item_valid,
            "valid": date_valid and item_valid,
        }
    return checks


def main() -> None:
    args = parse_args()
    expected = source_columns(args.schema)
    expected.pop("dbgen_version", None)
    actual = deployed_columns(args)
    counts = table_rows(args)
    ignored_tables = sorted(
        table
        for table in actual
        if any(table.endswith(suffix) for suffix in args.ignore_table_suffix)
    )
    actual = {table: columns for table, columns in actual.items() if table not in ignored_tables}
    counts = {table: count for table, count in counts.items() if table not in ignored_tables}

    all_tables = sorted(set(expected) | set(actual))
    details = {}
    for table in all_tables:
        source = expected.get(table, [])
        deployed = actual.get(table, [])
        details[table] = {
            "present_in_source": table in expected,
            "present_in_database": table in actual,
            "row_count": counts.get(table),
            "column_set_matches": set(source) == set(deployed),
            "column_order_matches": source == deployed,
            "first_order_difference": first_difference(source, deployed),
            "source_columns": source,
            "deployed_columns": deployed,
        }

    report: dict[str, object] = {
        "database": args.database,
        "reference_schema": str(args.schema),
        "table_count": len(actual),
        "ignored_tables": ignored_tables,
        "missing_tables": sorted(set(expected) - set(actual)),
        "extra_tables": sorted(set(actual) - set(expected)),
        "column_set_mismatches": [
            table for table in all_tables if not details[table]["column_set_matches"]
        ],
        "column_order_mismatches": [
            table for table in all_tables if not details[table]["column_order_matches"]
        ],
        "tables": details,
    }
    if args.semantic_checks:
        checks = semantic_checks(args, set(actual))
        report["semantic_checks"] = checks
        report["deployment_valid"] = (
            not report["missing_tables"]
            and not report["extra_tables"]
            and not report["column_set_mismatches"]
            and all(fact["valid"] for fact in checks["facts"].values())
        )

    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n")
    if not args.quiet:
        print(output)


if __name__ == "__main__":
    main()
