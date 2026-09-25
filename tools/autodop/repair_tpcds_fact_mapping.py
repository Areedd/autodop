#!/usr/bin/env python3
"""Repair TPC-DS fact tables loaded without source-column mappings.

Each replacement is loaded into a LIKE table, validated, and atomically swapped
with the original.  After the swap, the old table remains under the configured
backup suffix.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from audit_tpcds_deployment import FACT_KEYS, source_columns


DEFAULT_TABLES = [
    "web_returns",
    "catalog_returns",
    "store_returns",
    "web_sales",
    "catalog_sales",
    "store_sales",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="tpcds_sf100")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mysql-port", type=int, default=9030)
    parser.add_argument("--http-port", type=int, default=8030)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("/home/shaokun/starrocks_deploy/tpcds-kit/tools/tpcds.sql"),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/home/shaokun/starrocks_deploy/tpcds_sf100"),
    )
    parser.add_argument("--tables", nargs="+", default=DEFAULT_TABLES)
    parser.add_argument("--backup-suffix", default="__misloaded_20260723")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("tools/autodop/runs/tpcds_sf100_fact_repair.json"),
    )
    return parser.parse_args()


class Repair:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.layouts = source_columns(args.schema)
        self.state = {
            "database": args.database,
            "started_at": now(),
            "reference_schema": str(args.schema),
            "data_dir": str(args.data_dir),
            "backup_suffix": args.backup_suffix,
            "tables": {},
        }

    def save(self) -> None:
        self.args.state.parent.mkdir(parents=True, exist_ok=True)
        self.args.state.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")

    def mysql(self, query: str) -> list[list[str]]:
        command = [
            "mysql",
            "-h",
            self.args.host,
            "-P",
            str(self.args.mysql_port),
            f"-u{self.args.user}",
            "-N",
            "-B",
        ]
        if self.args.password:
            command.append(f"-p{self.args.password}")
        command.extend(["-e", query])
        result = subprocess.run(command, check=True, text=True, capture_output=True)
        return [line.split("\t") for line in result.stdout.splitlines() if line]

    def table_exists(self, table: str) -> bool:
        rows = self.mysql(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{self.args.database}' AND TABLE_NAME='{table}'"
        )
        return rows[0][0] == "1"

    def columns(self, table: str) -> list[str]:
        rows = self.mysql(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA='{self.args.database}' AND TABLE_NAME='{table}' "
            "ORDER BY ORDINAL_POSITION"
        )
        return [row[0] for row in rows]

    def count(self, table: str) -> int:
        return int(
            self.mysql(f"SELECT COUNT(*) FROM `{self.args.database}`.`{table}`")[0][0]
        )

    def key_ranges(self, storage_table: str, logical_table: str) -> dict[str, int | None]:
        date_key, item_key = FACT_KEYS[logical_table]
        row = self.mysql(
            f"SELECT MIN(`{date_key}`),MAX(`{date_key}`),"
            f"MIN(`{item_key}`),MAX(`{item_key}`) "
            f"FROM `{self.args.database}`.`{storage_table}`"
        )[0]
        values = [None if value == "NULL" else int(value) for value in row]
        return {
            "date_min": values[0],
            "date_max": values[1],
            "item_min": values[2],
            "item_max": values[3],
        }

    def load(self, table: str, target: str, path: Path) -> dict[str, object]:
        label = f"repair_{self.args.database}_{table}_{time.time_ns()}"
        source_names = [f"__src_{index}" for index in range(len(self.layouts[table]))]
        assignments = [
            f"{column}=nullif({source},'')"
            for column, source in zip(self.layouts[table], source_names)
        ]
        mapping = ",".join(source_names + assignments)
        command = [
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--show-error",
            "--silent",
            "--location-trusted",
            "-u",
            f"{self.args.user}:{self.args.password}",
            "-H",
            f"label:{label}",
            "-H",
            "format:csv",
            "-H",
            "column_separator:|",
            "-H",
            f"columns:{mapping}",
            "-H",
            "strict_mode:true",
            "-H",
            "Expect: 100-continue",
            "-H",
            f"timeout:{self.args.timeout}",
            "-H",
            "max_filter_ratio:0",
            "-T",
            str(path),
            f"http://{self.args.host}:{self.args.http_port}/api/"
            f"{self.args.database}/{target}/_stream_load",
        ]
        result = subprocess.run(command, check=True, text=True, capture_output=True)
        response = json.loads(result.stdout)
        if response.get("Status") != "Success":
            raise RuntimeError(f"Stream Load failed for {table}: {response}")
        total = int(response["NumberTotalRows"])
        loaded = int(response["NumberLoadedRows"])
        filtered = int(response["NumberFilteredRows"])
        if total != loaded or filtered != 0:
            raise RuntimeError(f"Stream Load lost rows for {table}: {response}")
        return response

    def validate_ranges(self, ranges: dict[str, int | None]) -> None:
        date_min, date_max = map(
            int,
            self.mysql(
                f"SELECT MIN(d_date_sk),MAX(d_date_sk) FROM `{self.args.database}`.date_dim"
            )[0],
        )
        item_min, item_max = map(
            int,
            self.mysql(
                f"SELECT MIN(i_item_sk),MAX(i_item_sk) FROM `{self.args.database}`.item"
            )[0],
        )
        if ranges["date_min"] is not None and not (
            date_min <= ranges["date_min"] <= ranges["date_max"] <= date_max
        ):
            raise RuntimeError(f"fact date keys are outside [{date_min}, {date_max}]: {ranges}")
        if ranges["item_min"] is not None and not (
            item_min <= ranges["item_min"] <= ranges["item_max"] <= item_max
        ):
            raise RuntimeError(f"fact item keys are outside [{item_min}, {item_max}]: {ranges}")

    def repair_table(self, table: str) -> None:
        backup = table + self.args.backup_suffix
        path = self.args.data_dir / f"{table}.dat"
        record: dict[str, object] = {
            "source_file": str(path),
            "source_bytes": path.stat().st_size,
            "backup_table": backup,
            "started_at": now(),
        }
        self.state["tables"][table] = record
        self.save()

        if not self.table_exists(table):
            raise RuntimeError(f"missing source table: {self.args.database}.{table}")
        if self.table_exists(backup):
            raise RuntimeError(f"backup/staging table already exists: {backup}")
        deployed = self.columns(table)
        expected = self.layouts[table]
        if set(deployed) != set(expected):
            raise RuntimeError(f"column set does not match TPC-DS schema: {table}")

        old_count = self.count(table)
        record["original_rows"] = old_count
        record["source_columns"] = expected
        print(
            f"[{now()}] loading {table}: {path.stat().st_size / (1024 ** 3):.2f} GiB, "
            f"{old_count} existing rows",
            flush=True,
        )
        self.mysql(
            f"CREATE TABLE `{self.args.database}`.`{backup}` "
            f"LIKE `{self.args.database}`.`{table}`"
        )
        record["staging_created_at"] = now()
        self.save()

        response = self.load(table, backup, path)
        record["stream_load"] = response
        record["loaded_at"] = now()
        self.save()

        new_count = self.count(backup)
        if new_count != old_count:
            raise RuntimeError(
                f"row count changed for {table}: old={old_count}, replacement={new_count}"
            )
        ranges = self.key_ranges(backup, table)
        self.validate_ranges(ranges)
        record["replacement_rows"] = new_count
        record["replacement_key_ranges"] = ranges
        record["validated_at"] = now()
        self.save()

        self.mysql(
            f"ALTER TABLE `{self.args.database}`.`{table}` SWAP WITH `{backup}`"
        )
        record["swapped_at"] = now()
        record["complete"] = True
        self.save()
        print(f"[{now()}] repaired {table}; old table retained as {backup}", flush=True)

    def run(self) -> None:
        if self.args.state.exists():
            raise RuntimeError(f"state file already exists: {self.args.state}")
        unknown = sorted(set(self.args.tables) - set(FACT_KEYS))
        if unknown:
            raise RuntimeError(f"unsupported fact tables: {', '.join(unknown)}")
        missing_files = [
            str(self.args.data_dir / f"{table}.dat")
            for table in self.args.tables
            if not (self.args.data_dir / f"{table}.dat").is_file()
        ]
        if missing_files:
            raise RuntimeError(f"missing source files: {', '.join(missing_files)}")

        self.save()
        for table in self.args.tables:
            self.repair_table(table)
        self.state["completed_at"] = now()
        self.state["complete"] = True
        self.save()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    Repair(parse_args()).run()


if __name__ == "__main__":
    main()
