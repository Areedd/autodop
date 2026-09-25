import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import (DOP_CLASSES, build_dataset, load_dataset,
                          sample_from_trace)
from autodop.graph import build_graph
from autodop.model import (CostModel, dynamic_margin_ranking_loss,
                           load_cost_model, save_checkpoint)

sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_cost


def request(fragment_cost=1000):
    return {
        "version": 1,
        "query_id": "q1",
        "fragments": [
            {
                "fragment_id": "F00",
                "fragment_index": 0,
                "original_dop": 4,
                "candidate_dops": [1, 2, 4, 8],
                "root_node": "RESULT",
                "leftmost_node": "EXCHANGE",
                "data_partition": "UNPARTITIONED",
                "output_partition": "UNPARTITIONED",
                "sink_type": "ResultSink",
                "fragment_cost": 10,
                "estimated_cardinality": 1,
                "estimated_avg_row_size": 8,
                "scan_range_count": 0,
                "instance_count": 1,
                "worker_ids": [10],
                "is_colocated": False,
                "is_replicated": False,
                "is_bucket_shuffle": False,
                "is_gather_root": True,
            },
            {
                "fragment_id": "F01",
                "fragment_index": 1,
                "original_dop": 4,
                "candidate_dops": [1, 2, 4, 8],
                "root_node": "SCAN",
                "leftmost_node": "SCAN",
                "data_partition": "RANDOM",
                "output_partition": "HASH_PARTITIONED",
                "sink_type": "DataStreamSink",
                "fragment_cost": fragment_cost,
                "estimated_cardinality": 100,
                "estimated_avg_row_size": 16,
                "scan_range_count": 8,
                "instance_count": 2,
                "worker_ids": [10, 11],
                "is_colocated": False,
                "is_replicated": False,
                "is_bucket_shuffle": False,
                "is_gather_root": False,
            },
        ],
        "edges": [
            {
                "source_fragment_id": "F01",
                "dest_fragment_id": "F00",
                "partition_type": "HASH_PARTITIONED",
            }
        ],
    }


def trace(dops, latency, fragment_cost=1000):
    return {
        "query_id": "q1",
        "latency_ms": latency,
        "request": request(fragment_cost=fragment_cost),
        "auto_dop": {"installed_dop": {"F00": dops[0], "F01": dops[1]}},
    }


def write_traces(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def train_tiny_cost_model(dataset_dir, output_path, epochs=300, hidden_dim=8, seed=0):
    samples, pairs, metadata = load_dataset(dataset_dir)
    torch.manual_seed(seed)
    graphs = {
        (sample["plan_key"], sample["config_key"]): build_graph(sample, metadata)
        for sample in samples
    }
    model = CostModel(metadata, {"hidden_dim": hidden_dim, "heads": 4})
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-2)
    for _ in range(epochs):
        optimizer.zero_grad()
        losses = []
        for pair in pairs:
            plan_key = pair["plan_key"]
            fast = model(graphs[(plan_key, pair["fast_config_key"])])
            slow = model(graphs[(plan_key, pair["slow_config_key"])])
            fast_sample = next(sample for sample in samples
                               if sample["plan_key"] == plan_key
                               and sample["config_key"] == pair["fast_config_key"])
            slow_sample = next(sample for sample in samples
                               if sample["plan_key"] == plan_key
                               and sample["config_key"] == pair["slow_config_key"])
            losses.append(dynamic_margin_ranking_loss(
                fast, slow, fast_sample["latency_ms"], slow_sample["latency_ms"]))
        torch.stack(losses).mean().backward()
        optimizer.step()
    save_checkpoint(output_path, "cost", model, metadata)
    return samples, pairs, metadata


class GapImplementationTest(unittest.TestCase):
    """Covers the audit-gap work: G2/G3 evaluation loop, G7 dop_classes, G8 skip semantics,
    G4 auto_retrain, and the ranking-direction regression test (DD02 §6.3)."""

    def setUp(self):
        torch.manual_seed(0)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.trace_path = self.root / "autodop_trace_test.jsonl"
        write_traces(self.trace_path, [
            trace([2, 4], 100),
            trace([2, 4], 110),
            trace([4, 2], 140),
        ])
        self.dataset = self.root / "dataset"
        build_dataset(self.trace_path, self.dataset)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cost_model_ranks_faster_config_higher(self):
        # DD02 §6.3: the trained CostModel must score the faster config above the slower one.
        checkpoint = self.root / "cost.pt"
        samples, pairs, metadata = train_tiny_cost_model(self.dataset, checkpoint)
        self.assertEqual(1, len(pairs))
        pair = pairs[0]
        graphs = {
            (sample["plan_key"], sample["config_key"]): build_graph(sample, metadata)
            for sample in samples
        }
        model, _ = load_cost_model(checkpoint)
        fast = float(model(graphs[(pair["plan_key"], pair["fast_config_key"])]).detach())
        slow = float(model(graphs[(pair["plan_key"], pair["slow_config_key"])]).detach())
        self.assertGreater(fast, slow)

    def test_evaluate_cost_is_reproducible(self):
        checkpoint = self.root / "cost.pt"
        train_tiny_cost_model(self.dataset, checkpoint)

        def run():
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                evaluate_cost.main([
                    "--checkpoint", str(checkpoint),
                    "--dataset", str(self.dataset),
                ])
            return buffer.getvalue()

        first = run()
        second = run()
        self.assertEqual(first, second)
        self.assertIn("rank_accuracy=", first)
        self.assertIn("plan_key,pairs,correct,rank_accuracy", first)

    def test_train_cost_with_baseline_prints_comparison(self):
        # two plans so the validation split (grouped by plan_key) has a non-empty valid set
        plan_a = [trace([2, 4], 100), trace([2, 4], 110), trace([4, 2], 140)]
        plan_b = [trace([2, 4], 300, fragment_cost=5000),
                  trace([2, 4], 320, fragment_cost=5000),
                  trace([4, 2], 420, fragment_cost=5000)]
        write_traces(self.trace_path, plan_a + plan_b)
        build_dataset(self.trace_path, self.dataset)
        output = self.root / "cost_with_baseline.pt"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "train_cost.py"),
             "--dataset", str(self.dataset),
             "--output", str(output),
             "--epochs", "3",
             "--batch-size", "4",
             "--hidden-dim", "8",
             "--valid-fraction", "0.5",
             "--seed", "0",
             "--with-baseline"],
            capture_output=True, text=True, check=True)
        self.assertIn("rank_acc cost=", completed.stdout)
        self.assertIn("baseline=", completed.stdout)
        self.assertTrue(output.exists())

    def test_train_cost_accuracy_gate_blocks_artifact(self):
        plan_a = [trace([2, 4], 100), trace([2, 4], 110), trace([4, 2], 140)]
        plan_b = [trace([2, 4], 300, fragment_cost=5000),
                  trace([2, 4], 320, fragment_cost=5000),
                  trace([4, 2], 420, fragment_cost=5000)]
        write_traces(self.trace_path, plan_a + plan_b)
        build_dataset(self.trace_path, self.dataset)
        checkpoint = self.root / "gated.pt"
        # gate 1.01 sits above the maximum achievable accuracy (1.0), so the block path is
        # exercised deterministically regardless of what the 1-epoch model happens to learn
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "train_cost.py"),
             "--dataset", str(self.dataset),
             "--output", str(checkpoint),
             "--epochs", "1",
             "--hidden-dim", "8",
             "--valid-fraction", "0.5",
             "--accuracy-gate", "1.01",
             "--seed", "0"],
            capture_output=True, text=True)
        self.assertEqual(1, completed.returncode)
        self.assertIn("below --accuracy-gate", completed.stderr)
        self.assertFalse(checkpoint.exists())

    def test_custom_dop_classes_stored_in_metadata(self):
        custom = self.root / "dataset_custom"
        build_dataset(self.trace_path, custom, dop_classes=[1, 2, 4])
        samples, pairs, metadata = load_dataset(custom)
        self.assertEqual([1, 2, 4], metadata["dop_classes"])
        # graph construction consumes the custom candidate set without error
        graph = build_graph(samples[0], metadata)
        self.assertEqual((2, 3), tuple(graph["candidate_mask"].shape))

    def test_bad_record_skipped_and_counted(self):
        bad = {"latency_ms": 123}  # missing request
        mixed = self.root / "autodop_trace_mixed.jsonl"
        write_traces(mixed, [bad, trace([2, 4], 100), trace([2, 4], 110), trace([4, 2], 140)])
        out = self.root / "dataset_mixed"
        samples, pairs, metadata = build_dataset(mixed, out)
        self.assertEqual(2, len(samples))
        self.assertEqual(1, metadata["skipped_records"]["count"])
        self.assertIn("missing trace.request", metadata["skipped_records"]["reasons"])
        # strict mode still fails fast on the malformed record
        with self.assertRaises(ValueError):
            build_dataset(mixed, self.root / "dataset_strict", skip_errors=False)

    def test_sample_from_trace_skip_errors_flag(self):
        bad = {"latency_ms": 123}
        self.assertIsNone(sample_from_trace(bad, skip_errors=True))
        with self.assertRaises(ValueError):
            sample_from_trace(bad)

    def test_old_metadata_without_dop_classes_loads(self):
        metadata_path = self.dataset / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        del metadata["dop_classes"]
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        samples, pairs, loaded = load_dataset(self.dataset)
        self.assertEqual(DOP_CLASSES, loaded["dop_classes"])
        build_graph(samples[0], loaded)  # consumable by the graph/model pipeline

    def test_auto_retrain_triggers_above_threshold_and_is_idempotent(self):
        trace_dir = self.root / "traces"
        trace_dir.mkdir()
        write_traces(trace_dir / "autodop_trace_2026-08-11.jsonl", [
            trace([2, 4], 100), trace([2, 4], 110),
            trace([4, 2], 140), trace([4, 2], 150),
            trace([1, 1], 200), trace([1, 1], 210),
        ])
        dataset_dir = self.root / "retrain_dataset"
        output_dir = self.root / "retrain_output"

        def run_retrain():
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "auto_retrain.py"),
                 "--trace-dir", str(trace_dir),
                 "--dataset-dir", str(dataset_dir),
                 "--output-dir", str(output_dir),
                 "--min-new-traces", "5",
                 "--epochs", "1",
                 "--batch-size", "4",
                 "--seed", "0"],
                capture_output=True, text=True, check=True)

        first = run_retrain()
        self.assertIn("retrain:", first.stdout)
        self.assertTrue((dataset_dir / "metadata.json").exists())
        self.assertTrue((output_dir / "cost_checkpoint.pt").exists())
        state_path = output_dir / "retrain_state.json"
        self.assertTrue(state_path.exists())
        state_after_first = state_path.read_text(encoding="utf-8")

        second = run_retrain()
        self.assertIn("skip:", second.stdout)
        self.assertEqual(state_after_first, state_path.read_text(encoding="utf-8"))

    def test_auto_retrain_skips_below_threshold(self):
        trace_dir = self.root / "traces_small"
        trace_dir.mkdir()
        write_traces(trace_dir / "autodop_trace_2026-08-11.jsonl", [
            trace([2, 4], 100), trace([2, 4], 110),
        ])
        dataset_dir = self.root / "retrain_dataset_small"
        output_dir = self.root / "retrain_output_small"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "auto_retrain.py"),
             "--trace-dir", str(trace_dir),
             "--dataset-dir", str(dataset_dir),
             "--output-dir", str(output_dir),
             "--min-new-traces", "5"],
            capture_output=True, text=True, check=True)
        self.assertIn("skip:", completed.stdout)
        self.assertFalse(dataset_dir.exists())
        # state file still advances the processed mtime (incremental marker)
        self.assertTrue((output_dir / "retrain_state.json").exists())


if __name__ == "__main__":
    unittest.main()
