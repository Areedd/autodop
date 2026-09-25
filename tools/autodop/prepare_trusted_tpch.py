#!/usr/bin/env python3
"""Create a new explicitly mapped TPC-H database and preserve import/audit evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from prepare_trusted_tpcds import file_evidence, file_evidence_keys
from repair_tpcds_fact_mapping import Repair, now

FOREIGN_KEYS = [
    ('nation', 'n_regionkey', 'region', 'r_regionkey'),
    ('supplier', 's_nationkey', 'nation', 'n_nationkey'),
    ('customer', 'c_nationkey', 'nation', 'n_nationkey'),
    ('partsupp', 'ps_partkey', 'part', 'p_partkey'),
    ('partsupp', 'ps_suppkey', 'supplier', 's_suppkey'),
    ('orders', 'o_custkey', 'customer', 'c_custkey'),
    ('lineitem', 'l_orderkey', 'orders', 'o_orderkey'),
    ('lineitem', 'l_partkey,l_suppkey', 'partsupp', 'ps_partkey,ps_suppkey'),
]
PRIMARY_KEYS = {
    'region': 'r_regionkey', 'nation': 'n_nationkey', 'part': 'p_partkey',
    'supplier': 's_suppkey', 'partsupp': 'ps_partkey,ps_suppkey',
    'customer': 'c_custkey', 'orders': 'o_orderkey', 'lineitem': 'l_orderkey,l_linenumber',
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database', required=True)
    p.add_argument('--source-database', required=True)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--schema', type=Path, default=Path('/home/shaokun/starrocks_deploy/tpch-dbgen/dss.ddl'))
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--mysql-port', type=int, default=9030)
    p.add_argument('--http-port', type=int, default=8030)
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='')
    p.add_argument('--timeout', type=int, default=7200)
    a = p.parse_args()
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', x) for x in (a.database, a.source_database)):
        p.error('invalid database identifier')
    if a.database == a.source_database:
        p.error('target must be a NEW database')
    return a


def run(args):
    if args.output.exists():
        raise FileExistsError(args.output)
    args.state = args.output
    args.backup_suffix = 'unused_no_repair_or_swap'
    helper = Repair(args)
    if set(helper.layouts) != set(PRIMARY_KEYS):
        raise ValueError('canonical schema must contain all eight TPC-H tables')
    state = {'database': args.database, 'source_database': args.source_database,
             'started_at': now(), 'status': 'preflight', 'deployment_valid': False,
             'reference_schema': str(args.schema.resolve()),
             'reference_schema_sha256': hashlib.sha256(args.schema.read_bytes()).hexdigest(),
             'load_contract': 'explicit canonical dbgen columns; strict_mode=true; max_filter_ratio=0',
             'source_history': 'Existing local dbgen files; original generator invocation and seed not independently attested. Schema version is recorded, not asserted as generator binary version.',
             'tables': {}, 'semantic_checks': {}}
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
        tmp.replace(args.output)
    def sql(query):
        # Long exact audit queries must not use an AutoDOP collection service.
        return helper.mysql('SET enable_auto_dop=false; SET enable_spill=true; SET query_timeout=7200; ' + query)
    try:
        if sql("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA " + f"WHERE SCHEMA_NAME='{args.database}'"):
            raise RuntimeError('target database already exists')
        for table, columns in helper.layouts.items():
            deployed = sql('SELECT COLUMN_NAME FROM information_schema.COLUMNS ' +
                           f"WHERE TABLE_SCHEMA='{args.source_database}' AND TABLE_NAME='{table}' ORDER BY ORDINAL_POSITION")
            if {x[0] for x in deployed} != set(columns):
                raise RuntimeError(f'column mismatch: {table}')
            record = file_evidence(args.data_dir / f'{table}.tbl')
            record['source_columns'] = columns
            state['tables'][table] = record
            save()
        sql(f'CREATE DATABASE `{args.database}`')
        state['status'] = 'loading'
        save()
        for table in helper.layouts:
            record = state['tables'][table]
            sql(f'CREATE TABLE `{args.database}`.`{table}` LIKE `{args.source_database}`.`{table}`')
            record['load_started_at'] = now()
            save()
            record['load_response'] = helper.load(table, table, Path(record['source_file']))
            if file_evidence(Path(record['source_file'])) != {k: record[k] for k in file_evidence_keys()}:
                raise RuntimeError(f'source changed: {table}')
            record['actual_rows'] = int(sql(f'SELECT COUNT(*) FROM `{args.database}`.`{table}`')[0][0])
            response = record['load_response']
            if not record['source_rows'] == record['actual_rows'] == int(response['NumberLoadedRows']) == int(response['NumberTotalRows']):
                raise RuntimeError(f'row count mismatch: {table}')
            record['verified'] = True
            record['load_finished_at'] = now()
            save()
            print(f"loaded {table}: {record['actual_rows']} rows", flush=True)
        state['status'] = 'auditing'
        save()
        for table, keys in PRIMARY_KEYS.items():
            count = int(sql(f'SELECT COUNT(*) FROM (SELECT {keys} FROM `{args.database}`.`{table}` GROUP BY {keys} HAVING COUNT(*)<>1) duplicates')[0][0])
            state['semantic_checks'][f'unique_{table}'] = count
            save()
            if count:
                raise RuntimeError(f'duplicate primary keys: {table}')
        for table, keys, dimension, dimkeys in FOREIGN_KEYS:
            condition = ' AND '.join(f'f.{a}=d.{b}' for a,b in zip(keys.split(','),dimkeys.split(',')))
            count = int(sql(f'SELECT COUNT(*) FROM `{args.database}`.`{table}` f LEFT JOIN `{args.database}`.`{dimension}` d ON {condition} WHERE d.{dimkeys.split(",")[0]} IS NULL')[0][0])
            state['semantic_checks'][f'fk_{table}_{keys}'] = count
            save()
            if count:
                raise RuntimeError(f'unmatched foreign keys: {table}.{keys}')
        for table, columns in [('orders',['o_orderdate']),('lineitem',['l_shipdate','l_commitdate','l_receiptdate'])]:
            for column in columns:
                row = sql(f'SELECT MIN({column}), MAX({column}) FROM `{args.database}`.`{table}`')[0]
                state['semantic_checks'][column] = {'min': row[0], 'max': row[1]}
                save()
                if not '1992-01-01' <= row[0] <= row[1] <= '1998-12-31':
                    raise RuntimeError(f'date outside TPC-H generation domain: {column}')
        state['statistics'] = {}
        for table in helper.layouts:
            state['statistics'][table] = sql(f'ANALYZE SAMPLE TABLE `{args.database}`.`{table}`')
            save()
            if not any('OK' in row for row in state['statistics'][table]):
                raise RuntimeError(f'statistics failed: {table}')
        state['deployment_valid'] = True
        state['status'] = 'complete'
        state['finished_at'] = now()
        save()
    except Exception as error:
        state.update(status='failed', error=f'{type(error).__name__}: {error}', finished_at=now())
        save()
        raise


if __name__ == '__main__':
    run(parse_args())
