import argparse
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect_trusted_dataset as collector


def trace(mapping, query_id="query-1"):
    return {"query_id": query_id, "state": "EOF", "latency_ms": 20,
            "request": {"query_id": query_id, "fragments": [
                {"fragment_id": fid, "fragment_index": index, "candidate_dops": [1, 2, 4, 8]}
                for index, fid in enumerate(mapping)]},
            "auto_dop": {"installed_dop": mapping},
            "actual_fragments": [{"fragment_id": fid, "plan_dop": dop,
                                  "instance_dops": [dop, dop]} for fid, dop in mapping.items()]}


class TrustedDatasetTest(unittest.TestCase):
    def test_client_timing_excludes_digest_and_records_fetch_failure(self):
        clock = [10.0]

        class Cursor:
            fail = False

            def execute(self, statement):
                clock[0] += 0.125

            def fetchall(self):
                clock[0] += 0.25
                if self.fail:
                    raise RuntimeError('fetch failed')
                return [(1,)]

        cursor = Cursor()
        with patch.object(collector.time, 'monotonic', side_effect=lambda: clock[0]):
            event = {}
            rows = collector.fetch_timed(cursor, 'SELECT 1', event)
            clock[0] += 20  # Expensive downstream result verification.
            collector.result_digest(rows)
            self.assertEqual(event['client_latency_ms'], 375)
            self.assertEqual(event['client_timing_contract'], collector.CLIENT_TIMING_CONTRACT)
            cursor.fail = True
            failed = {}
            with self.assertRaisesRegex(RuntimeError, 'fetch failed'):
                collector.fetch_timed(cursor, 'SELECT 1', failed)
            self.assertEqual(failed['client_latency_ms'], 375)

    def test_verification_only_requires_explicit_single_anchor_contract(self):
        valid = {'verification_only': True, 'configs': 1, 'config_design': 'anchor4'}
        self.assertTrue(collector.verification_collection(valid))
        self.assertFalse(collector.verification_collection({'configs': 1}))
        for changes in [{'configs': 2}, {'config_design': 'legacy'}, {'configuration_manifest': 'x'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                collector.verification_collection({**valid, **changes})

    def test_explicit_vectors_remap_indices_and_reject_drift(self):
        request = trace({'F10': 1, 'F99': 1})['request']
        request['fragments'][1]['is_gather_root'] = True
        vectors = [{'0': 2, '1': 1}, {'0': 4, '1': 1}]
        self.assertEqual(collector.explicit_configs(request, vectors),
                         [{'F10': 2, 'F99': 1}, {'F10': 4, 'F99': 1}])
        for invalid in [[vectors[0], vectors[0]], [{'0': 2}, vectors[1]],
                        [{'0': 2, '1': 2}, vectors[1]], [{'0': 3, '1': 1}, vectors[1]]]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                collector.explicit_configs(request, invalid)

    def test_multiset_digest_preserves_duplicates_precision_and_types(self):
        rows = [(Decimal("123456789012345678901234567890.123400"), date(2026, 9, 5), None),
                (Decimal("0.0"), "x", b"x")]
        self.assertEqual(collector.result_digest(rows), collector.result_digest(list(reversed(rows))))
        self.assertNotEqual(collector.result_digest(rows), collector.result_digest(rows + [rows[0]]))
        self.assertEqual(collector.canonical_cell(rows[0][0])[1], "123456789012345678901234567890.1234")
        self.assertNotEqual(collector.result_digest([(1,)]), collector.result_digest([("1",)]))
        with self.assertRaises(ValueError):
            collector.result_digest([(float("nan"),)])

    def test_configs_are_frozen_legal_and_unique(self):
        request = trace({"0": 1, "1": 1, "2": 1})["request"]
        request["fragments"][0]["is_gather_root"] = True
        configs = collector.frozen_configs(request, [1, 2, 4, 8], 16, 10)
        self.assertEqual(len(configs), 16)
        self.assertEqual(configs, collector.frozen_configs(request, [1, 2, 4, 8], 16, 10))
        self.assertEqual(len({tuple(c.values()) for c in configs}), 16)
        self.assertTrue(all(c["0"] == 1 for c in configs))
        for dop in [1, 2, 4, 8]:
            self.assertIn({"0": 1, "1": dop, "2": dop}, configs)
        balanced = collector.frozen_configs(request, [1, 2, 4, 8], 16, 10, "balanced")
        self.assertEqual(balanced, collector.frozen_configs(request, [1, 2, 4, 8], 16, 10, "balanced"))
        self.assertEqual(len({tuple(c.values()) for c in balanced}), 16)
        self.assertTrue(all(c["0"] == 1 for c in balanced))

    def test_trace_installed_vector_and_native_caps(self):
        record = trace({"0": 4})
        record["actual_fragments"][0]["instance_dops"] = [2, 2]
        self.assertEqual(collector.validate_trace(record, {"0": 4}, "query-1"), {"0": 2})
        with self.assertRaises(ValueError):
            collector.validate_trace(record, {"0": 2}, "query-1")
        record["actual_fragments"][0]["instance_dops"] = [1, 2]
        with self.assertRaisesRegex(ValueError, "heterogeneous"):
            collector.validate_trace(record, {"0": 4}, "query-1")
        with self.assertRaises(ValueError):
            collector.validate_trace(record, {"0": 4}, "other-query")

    def test_single_anchor4_configuration_keeps_discovered_gather_domain(self):
        request = trace({'F10': 1, 'F20': 1})['request']
        request['fragments'][1]['is_gather_root'] = True
        self.assertEqual(collector.frozen_configs(request, [1, 2, 4, 8], 1, 0, 'anchor4'),
                         [{'F10': 4, 'F20': 1}])
        self.assertEqual(collector.frozen_configs(request, [1, 2, 4, 8], 1, 0),
                         [{'F10': 1, 'F20': 1}])

    def test_partial_trace_line_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autodop_trace_x.jsonl"
            encoded = json.dumps(trace({"0": 1}))
            path.write_text(encoded[:20])
            reader = collector.TraceReader(Path(directory))
            with self.assertRaises(TimeoutError):
                reader.wait("query-1", 0.01)
            with path.open("a") as handle:
                handle.write(encoded[20:] + "\n")
            self.assertEqual(reader.wait("query-1", 0.5)["query_id"], "query-1")

    def test_settings_restore_after_failure(self):
        class Cursor:
            description = [("Key",), ("Value",)]
            writes = []

            def execute(self, sql, params=None):
                if params:
                    self.key = params[0]
                else:
                    self.writes.append(sql)

            def fetchall(self):
                return [(self.key, "old")]

        cursor = Cursor()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(RuntimeError, "failure"):
                with collector.frontend_settings(cursor, {"auto_dop_endpoint": "new"}, path):
                    raise RuntimeError("failure")
            self.assertIn('"old"', cursor.writes[-1])
            self.assertTrue(json.loads(path.read_text())["restored"])

    def test_settings_restore_uses_fresh_connection_after_idle_expiry(self):
        class Cursor:
            description = [("Key",), ("Value",)]

            def __init__(self):
                self.expired = False
                self.writes = []

            def execute(self, sql, params=None):
                if self.expired:
                    raise RuntimeError("idle connection expired")
                if params:
                    self.key = params[0]
                else:
                    self.writes.append(sql)

            def fetchall(self):
                return [(self.key, "old")]

        class Connection:
            closed = False

            def cursor(self):
                return fresh

            def close(self):
                self.closed = True

        stale, fresh = Cursor(), Cursor()
        connection = Connection()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "workload failed"):
                with collector.frontend_settings(stale, {"auto_dop_endpoint": "new"}, path,
                                                 restore_connection_factory=lambda: connection):
                    stale.expired = True
                    raise ValueError("workload failed")
            self.assertEqual(len(stale.writes), 1)
            self.assertEqual(fresh.writes, ['ADMIN SET FRONTEND CONFIG ("auto_dop_endpoint" = "old")'])
            self.assertTrue(connection.closed)
            self.assertTrue(json.loads(path.read_text())["restored"])

    def test_collection_accepts_only_complete_measured_queries(self):
        for verification_only, fail_at in ((False, None), (False, 9), (True, None), (True, 6)):
            with self.subTest(verification_only=verification_only, fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "q.sql").write_text("select value from fixture")
                (root / "manifest.json").write_text(json.dumps({"queries": [
                    {"id": "q", "sql_file": "q.sql", "database": "fixture", "split": "train",
                     "sql_sha256": hashlib.sha256((root / 'q.sql').read_bytes()).hexdigest()}]}))
                (root / "audit.json").write_text('{"deployment_valid":true,"database":"fixture"}')
                state = {"calls": 0, "query_id": "", "server": None, 'timeouts': []}

                class Server:
                    def __init__(self, *args):
                        state["server"] = self
                    def serve_forever(self):
                        pass
                    def shutdown(self):
                        pass
                    def server_close(self):
                        pass

                class Cursor:
                    description = [("Key",), ("Value",)]
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass
                    def execute(self, sql, params=None):
                        self.sql = sql
                        if sql.startswith('SET query_timeout='):
                            state['timeout'] = int(sql.split('=')[1])
                        if "last_query_id()" in sql and "SET_VAR(enable_auto_dop=false)" not in sql:
                            # A helper SELECT has unrelated fragments; a frozen vector
                            # for the measured query must not be applied to it.
                            state["server"].engine.decide(trace({"helper-fragment": 1})["request"])
                        if params:
                            self.key = params[0]
                        if sql.startswith("select value"):
                            state['timeouts'].append(state['timeout'])
                            state["calls"] += 1
                            state["query_id"] = "q-" + str(state["calls"])
                    def fetchall(self):
                        if self.sql.startswith("ADMIN SHOW"):
                            return [(self.key, "false" if self.key.startswith("enable") else "old")]
                        return [(99 if state["calls"] == fail_at else 1,)]
                    def fetchone(self):
                        return [state["query_id"]]
                    def nextset(self):
                        return None

                class Connection:
                    def cursor(self):
                        return Cursor()
                    def close(self):
                        pass

                def wait(reader, query_id, timeout):
                    mapping = state["server"].engine.fragment_dops
                    if "*" in mapping:
                        mapping = {"0": mapping["*"]}
                    return trace(deepcopy(mapping), query_id)

                args = argparse.Namespace(manifest=str(root / "manifest.json"), audit=str(root / "audit.json"),
                    output_dir=str(root / "run"), repetitions=3, warmup=1, configs=1 if verification_only else 4,
                    verification_only=verification_only, config_design='anchor4' if verification_only else 'legacy',
                    dops="1,2,4,8", seed=1, mysql_host="localhost", mysql_port=9030, mysql_user="root",
                    autodop_host="127.0.0.1", autodop_port=18081, query_timeout_s=60, trace_timeout_s=1,
                    probe_timeout_s=120 if verification_only else None)
                with patch.object(collector, "ThreadingHTTPServer", Server), \
                     patch.object(collector.pymysql, "connect", return_value=Connection()) as connect, \
                     patch.object(collector.TraceReader, "wait", wait):
                    collector.collect(args)
                summary = json.loads((root / "run/collection.json").read_text())
                self.assertEqual(state['timeouts'], [60, 120 if verification_only else 60]
                                 + [60] * (state['calls'] - 2))
                self.assertEqual(connect.call_args_list[0].kwargs['read_timeout'],
                                 135 if verification_only else 75)
                measured = root / "run/measured/accepted.jsonl"
                if fail_at is None:
                    records = [json.loads(line) for line in measured.read_text().splitlines()]
                    self.assertEqual(len(records), 3 if verification_only else 12)
                    self.assertTrue(all(r["trusted_provenance"]["phase"] == "measured" for r in records))
                    self.assertEqual(summary["accepted"], 1)
                    self.assertEqual(state["calls"], 6 if verification_only else 18)
                    if verification_only:
                        from scripts.build_trusted_dataset import build
                        built = build(measured, root / 'manifest.json', root / 'dataset',
                                      collection_path=root / 'run/collection.json')
                        self.assertEqual(built['collection_purpose'], 'verification_only')
                        self.assertEqual(built['partitions']['all']['samples'], 1)
                        self.assertEqual(built['partitions']['all']['pairs'], 0)
                        # Removing the explicit purpose must restore the ranking minimum.
                        summary['settings']['verification_only'] = False
                        (root / 'run/collection.json').write_text(json.dumps(summary))
                        with self.assertRaisesRegex(ValueError, 'distinct frozen configurations'):
                            build(measured, root / 'manifest.json', root / 'bad_dataset',
                                  collection_path=root / 'run/collection.json')
                else:
                    self.assertFalse(measured.exists())
                    self.assertEqual(summary["excluded"], 1)
                    self.assertGreater(summary["queries"][0]["discarded_measured_executions"], 0)
                self.assertTrue(json.loads((root / "run/frontend_settings.json").read_text())["restored"])


if __name__ == "__main__":
    unittest.main()
