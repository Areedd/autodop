import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import build_dataset, load_dataset, sample_from_trace
from autodop.engine import PolicyEngine
from autodop.fixed_engine import FixedEngine
from autodop.graph import COMPETITION_ALPHA, COMPETITION_BASE, build_graph, sample_from_request
from autodop.mcts import attention_guided_groups, equivalence_groups, search
from autodop.model import (CostModel, PolicyModel, dynamic_margin_ranking_loss,
                           dynamic_rank_margin, load_cost_model, save_checkpoint)


def request():
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
                "fragment_cost": 1000,
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


def trace(dops, latency):
    return {
        "query_id": "q1",
        "latency_ms": latency,
        "request": request(),
        "auto_dop": {"installed_dop": {"F00": dops[0], "F01": dops[1]}},
    }


class AutoDopTest(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(0)
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        trace_path = root / "autodop_trace_test.jsonl"
        records = [trace([2, 4], 100), trace([2, 4], 110), trace([4, 2], 140)]
        trace_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.dataset = root / "dataset"
        build_dataset(trace_path, self.dataset)
        self.samples, self.pairs, self.metadata = load_dataset(self.dataset)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dataset_and_graph(self):
        self.assertEqual(2, len(self.samples))
        self.assertEqual(1, len(self.pairs))
        self.assertAlmostEqual(math.log1p(35.0 / 105.0), self.pairs[0]["margin"])
        aggregated = next(sample for sample in self.samples if sample["dop_config"] == [2, 4])
        self.assertEqual(105.0, aggregated["latency_ms"])
        parallel = copy.deepcopy(self.samples[0])
        parallel["fragments"].append(copy.deepcopy(parallel["fragments"][1]))
        parallel["dop_config"].append(parallel["dop_config"][1])
        parallel["edges"].append({"source": 2, "destination": 0, "partition_type": "HASH_PARTITIONED"})
        graph = build_graph(parallel, self.metadata)
        self.assertEqual((3, 3), tuple(graph["dependency"].shape))
        self.assertEqual(0.0, float(graph["competition"][0, 1]))
        self.assertEqual(1.0, float(graph["competition_overlap"][1, 2]))
        self.assertEqual(1.0, float(graph["worker_share"][1, 2]))
        self.assertGreater(float(graph["resource_pressure"][1, 2]), 0.0)
        expected = COMPETITION_BASE + COMPETITION_ALPHA * (
            float(graph["competition_overlap"][1, 2])
            * float(graph["worker_share"][1, 2])
            * float(graph["resource_pressure"][1, 2])
        )
        self.assertAlmostEqual(expected, float(graph["competition"][1, 2]), places=6)

    def test_models_and_mcts(self):
        cost = CostModel(self.metadata, {"hidden_dim": 16, "heads": 4})
        layer = cost.encoder.layers[0]
        self.assertIsNot(layer.dependency_attention, layer.competition_attention)
        score = cost(build_graph(self.samples[0], self.metadata))
        self.assertEqual((), tuple(score.shape))
        importance = cost.fragment_importance(build_graph(self.samples[0], self.metadata))
        self.assertEqual((2,), tuple(importance.shape))
        self.assertAlmostEqual(4.0, float(importance.sum().detach()), places=6)
        config, _ = search(self.samples[0], cost, self.metadata, iterations=10, seed=1)
        self.assertEqual(2, len(config))
        self.assertTrue(all(dop in [1, 2, 4, 8] for dop in config))
        # Historical fixture rows predate the gather-root DOP=1 constraint.
        # Search candidates must obey the current execution domain.
        measured = [[1, sample["dop_config"][1]] for sample in self.samples]
        config, _ = search(
            self.samples[0], cost, self.metadata, iterations=10, seed=1,
            allowed_configs=measured)
        self.assertIn(config, measured)

    def test_dynamic_margin_hinge_loss(self):
        self.assertAlmostEqual(math.log(2.0), dynamic_rank_margin(100.0, 200.0))
        fast = torch.tensor(0.2, requires_grad=True)
        slow = torch.tensor(0.0, requires_grad=True)
        small_loss = dynamic_margin_ranking_loss(fast, slow, 100.0, 110.0)
        large_loss = dynamic_margin_ranking_loss(fast, slow, 100.0, 200.0)
        self.assertEqual(0.0, float(small_loss.detach()))
        self.assertGreater(float(large_loss.detach()), 0.0)

    def test_mcts_orders_groups_by_global_attention(self):
        class ImportanceModel:
            def fragment_importance(self, graph):
                return torch.tensor([0.1, 0.9])

        sample = self.samples[0]
        candidates = [fragment["candidate_dops"] for fragment in sample["fragments"]]
        groups = attention_guided_groups(sample, ImportanceModel(), self.metadata, candidates)
        self.assertEqual((1,), groups[0])

    def test_mcts_preserves_fragment_order_when_collapsing_equivalence_classes(self):
        class ImportanceModel:
            def fragment_importance(self, graph):
                return torch.tensor([0.4, 0.4, 0.7])

        first = copy.deepcopy(self.samples[0]["fragments"][0])
        distinct = copy.deepcopy(self.samples[0]["fragments"][1])
        sample = {
            "fragments": [copy.deepcopy(first), copy.deepcopy(first), distinct],
            "edges": [],
            "dop_config": [2, 2, 4],
        }
        candidates = [fragment["candidate_dops"] for fragment in sample["fragments"]]
        groups = attention_guided_groups(sample, ImportanceModel(), self.metadata, candidates)
        self.assertEqual((2,), groups[0])
        self.assertEqual((0, 1), groups[1])

    def test_mcts_equivalence_classes_share_one_decision(self):
        fragment = copy.deepcopy(self.samples[0]["fragments"][1])
        sample = {"fragments": [copy.deepcopy(fragment), copy.deepcopy(fragment)],
                  "edges": [], "dop_config": [2, 4]}
        candidates = [fragment["candidate_dops"], fragment["candidate_dops"]]
        self.assertEqual([(0, 1)], equivalence_groups(sample, candidates))
        cost = CostModel(self.metadata, {"hidden_dim": 16, "heads": 4})
        config, _ = search(sample, cost, self.metadata, iterations=12, seed=1)
        self.assertEqual(config[0], config[1])

    def test_fixed_offline_helper(self):
        fixed = FixedEngine({"F00": 4, "F01": 2}).decide(request())
        self.assertEqual([4, 2], [decision["dop"] for decision in fixed["decisions"]])
        wildcard = FixedEngine({"*": 2}).decide(request())
        self.assertEqual([2, 2], [decision["dop"] for decision in wildcard["decisions"]])
        optional_override = FixedEngine({"*": 2, "F99": 8}).decide(request())
        self.assertEqual([2, 2], [decision["dop"] for decision in optional_override["decisions"]])
        with self.assertRaises(ValueError):
            FixedEngine({"F01": 2}).decide(request())

    def test_policy_engine(self):
        policy = PolicyModel(self.metadata, {
            "hidden_dim": 16,
            "heads": 4,
        })
        checkpoint = Path(self.temp_dir.name) / "policy.pt"
        save_checkpoint(checkpoint, "policy", policy, self.metadata)
        response = PolicyEngine(checkpoint).decide(request())
        self.assertEqual(2, len(response["decisions"]))
        self.assertGreater(response["confidence"], 0.0)
        self.assertLessEqual(response["confidence"], 1.0)
        self.assertTrue(all(decision["dop"] in [1, 2, 4, 8] for decision in response["decisions"]))

    def test_old_model_checkpoint_is_rejected(self):
        checkpoint = Path(self.temp_dir.name) / "old.pt"
        torch.save({"kind": "cost"}, checkpoint)
        with self.assertRaisesRegex(ValueError, "retraining is required"):
            load_cost_model(checkpoint)

    def test_online_request_graph(self):
        sample = sample_from_request(request())
        graph = build_graph(sample, self.metadata, unknown_dop=True)
        self.assertTrue(torch.equal(graph["dop_ids"], torch.zeros(2, dtype=torch.long)))

    def test_operator_semantics_change_plan_key(self):
        intersect = trace([2, 4], 100)
        intersect["request"]["fragments"][1]["operator_counts"] = {"INTERSECT": 1}
        intersect["request"]["fragments"][1]["operator_signature"] = "INTERSECT=1"
        except_ = trace([2, 4], 100)
        except_["request"]["fragments"][1]["operator_counts"] = {"EXCEPT": 1}
        except_["request"]["fragments"][1]["operator_signature"] = "EXCEPT=1"

        self.assertNotEqual(
            sample_from_trace(intersect)["plan_key"],
            sample_from_trace(except_)["plan_key"],
        )

    def test_dataset_skips_missing_request_record(self):
        root = Path(self.temp_dir.name)
        trace_path = root / "invalid.jsonl"
        trace_path.write_text(
            json.dumps({"latency_ms": 1}) + "\n" + json.dumps(trace([2, 4], 100)) + "\n",
            encoding="utf-8")
        # G8: malformed records are skipped and counted instead of aborting the build
        samples, pairs, metadata = build_dataset(trace_path, root / "dataset_with_bad")
        self.assertEqual(1, len(samples))
        self.assertEqual(1, metadata["skipped_records"]["count"])
        self.assertIn("missing trace.request", metadata["skipped_records"]["reasons"])
        # strict mode (tests/debugging) still fails fast
        with self.assertRaises(ValueError):
            build_dataset(trace_path, root / "dataset_strict", skip_errors=False)

    def test_sample_from_trace_accepts_trace_without_actual_fragment_details(self):
        sample = sample_from_trace(trace([2, 4], 100))
        self.assertEqual([2, 4], sample["dop_config"])


if __name__ == "__main__":
    unittest.main()
