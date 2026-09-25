#!/usr/bin/env python3
"""Load immutable dbgen files into a NEW database with explicit source mappings.

Existing databases are never modified. Partial failures retain the new database
and evidence for inspection; reruns require a different, unused database name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from audit_tpcds_deployment import FACT_KEYS
from repair_tpcds_fact_mapping import Repair, now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--source-database", default="tpcds_sf10")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=Path(
        "/home/shaokun/starrocks_deploy/tpcds-kit/tools/tpcds.sql"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=9030)
    parser.add_argument("--http-port", type=int, default=8030)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    for name in (args.database, args.source_database):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            parser.error("database names must be simple SQL identifiers")
    if args.database == args.source_database:
        parser.error("target must differ from the source database")
    return args


def file_evidence(path: Path) -> dict:
    digest = hashlib.sha256()
    rows = 0
    last = b""
    before = path.stat()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
            rows += block.count(b"\n")
            last = block[-1:]
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"source changed during hashing: {path}")
    if not rows or last != b"\n":
        raise RuntimeError(f"expected nonempty newline-terminated dbgen data: {path}")
    return {"source_file": str(path.resolve()), "source_bytes": after.st_size,
            "source_mtime_ns": after.st_mtime_ns, "source_sha256": digest.hexdigest(),
            "source_rows": rows}


def run(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite load evidence: {args.output}")
    args.state = args.output
    args.backup_suffix = "unused_no_repair_or_swap"
    helper = Repair(args)
    layouts = {name: columns for name, columns in helper.layouts.items()
               if name != "dbgen_version"}
    if len(layouts) != 24:
        raise RuntimeError(f"expected 24 canonical TPC-DS tables, got {len(layouts)}")
    state = {"database": args.database, "source_database": args.source_database,
             "started_at": now(), "status": "preflight", "tables": {},
             "reference_schema": str(args.schema.resolve()),
             "reference_schema_sha256": hashlib.sha256(args.schema.read_bytes()).hexdigest(),
             "load_contract": "explicit dbgen source order; nullif(empty,''); strict_mode=true; max_filter_ratio=0"}

    def save() -> None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)

    try:
        if helper.mysql("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                        f"WHERE SCHEMA_NAME='{args.database}'"):
            raise RuntimeError(f"target database already exists: {args.database}")
        for table, columns in layouts.items():
            deployed = helper.mysql("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                                    f"WHERE TABLE_SCHEMA='{args.source_database}' "
                                    f"AND TABLE_NAME='{table}' ORDER BY ORDINAL_POSITION")
            if set(row[0] for row in deployed) != set(columns):
                raise RuntimeError(f"source schema column set mismatch: {table}")
            state["tables"][table] = file_evidence(args.data_dir / f"{table}.dat")
            state["tables"][table]["source_columns"] = columns
            save()
        # Intentionally no IF NOT EXISTS: racing creation must also fail closed.
        helper.mysql(f"CREATE DATABASE `{args.database}`")
        state["status"] = "loading"
        save()
        for table in layouts:
            record = state["tables"][table]
            helper.mysql(f"CREATE TABLE `{args.database}`.`{table}` "
                         f"LIKE `{args.source_database}`.`{table}`")
            path = Path(record["source_file"])
            record["load_started_at"] = now()
            save()
            record["load_response"] = helper.load(table, table, path)
            # Rehash after loading, proving input did not change across the load.
            if file_evidence(path) != {key: record[key] for key in file_evidence_keys()}:
                raise RuntimeError(f"source changed during load: {table}")
            record["actual_rows"] = helper.count(table)
            response = record["load_response"]
            if not (record["source_rows"] == record["actual_rows"]
                    == int(response["NumberLoadedRows"]) == int(response["NumberTotalRows"])):
                raise RuntimeError(f"source/load/table row counts differ: {table}")
            record["verified"] = True
            record["load_finished_at"] = now()
            save()
            print(f"loaded {table}: {record['actual_rows']} rows", flush=True)
        state["status"] = "auditing"
        save()
        checks = {}
        state["semantic_checks"] = checks
        for table, (date_key, item_key) in FACT_KEYS.items():
            ranges = helper.key_ranges(table, table)
            helper.validate_ranges(ranges)
            missing = {}
            for key, dimension, dimension_key in (
                    (date_key, "date_dim", "d_date_sk"),
                    (item_key, "item", "i_item_sk")):
                missing[key] = int(helper.mysql(
                    f"SELECT COUNT(*) FROM `{args.database}`.`{table}` f "
                    f"LEFT JOIN `{args.database}`.`{dimension}` d ON f.`{key}`=d.`{dimension_key}` "
                    f"WHERE f.`{key}` IS NOT NULL AND d.`{dimension_key}` IS NULL")[0][0])
            checks[table] = {"ranges": ranges, "unmatched_nonnull_keys": missing}
            save()
            if any(missing.values()):
                raise RuntimeError(f"unmatched fact dimension keys: {table}: {missing}")
        state["status"] = "complete"
        state["finished_at"] = now()
        save()
    except Exception as error:
        state["status"] = "failed"
        state["error"] = f"{type(error).__name__}: {error}"
        state["finished_at"] = now()
        save()
        raise


def file_evidence_keys() -> tuple[str, ...]:
    return ("source_file", "source_bytes", "source_mtime_ns", "source_sha256", "source_rows")


if __name__ == "__main__":
    run(parse_args())
