import json
import sys
import tempfile
import unittest
from pathlib import Path

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.data import build_dataset, load_dataset, sample_from_trace
from autodop.engine import CostAwareEngine, FixedEngine, OperatorSubgraphEngine, PolicyEngine
from autodop.graph import build_graph, sample_from_request
from autodop.mcts import search
from autodop.model import CostModel, PolicyModel, save_checkpoint
from autodop.tree_policy import TreePolicyEngine, feature_names


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
        aggregated = next(sample for sample in self.samples if sample["dop_config"] == [2, 4])
        self.assertEqual(105.0, aggregated["latency_ms"])
        graph = build_graph(self.samples[0], self.metadata)
        self.assertEqual((2, 2), tuple(graph["dependency"].shape))
        self.assertEqual(0.5, float(graph["competition"][0, 1]))

    def test_models_and_mcts(self):
        cost = CostModel(self.metadata, {"hidden_dim": 16, "heads": 4})
        score = cost(build_graph(self.samples[0], self.metadata))
        self.assertEqual((), tuple(score.shape))
        config, _ = search(self.samples[0], cost, self.metadata, iterations=10, seed=1)
        self.assertEqual(2, len(config))
        self.assertTrue(all(dop in [1, 2, 4, 8] for dop in config))
        measured = [sample["dop_config"] for sample in self.samples]
        config, _ = search(
            self.samples[0], cost, self.metadata, iterations=10, seed=1,
            allowed_configs=measured)
        self.assertIn(config, measured)

    def test_fixed_and_policy_engines(self):
        fixed = FixedEngine({"F00": 4, "F01": 2}).decide(request())
        self.assertEqual([4, 2], [decision["dop"] for decision in fixed["decisions"]])
        wildcard = FixedEngine({"*": 2}).decide(request())
        self.assertEqual([2, 2], [decision["dop"] for decision in wildcard["decisions"]])
        optional_override = FixedEngine({"*": 2, "F99": 8}).decide(request())
        self.assertEqual([2, 2], [decision["dop"] for decision in optional_override["decisions"]])
        with self.assertRaises(ValueError):
            FixedEngine({"F01": 2}).decide(request())

        operator_request = request()
        operator_request["fragments"][1]["operator_counts"] = {"INTERSECT": 1}
        operator = OperatorSubgraphEngine("INTERSECT", 4, 1).decide(operator_request)
        self.assertEqual([1, 4], [decision["dop"] for decision in operator["decisions"]])
        scalar = OperatorSubgraphEngine(
            "INTERSECT", 1, 8, 1).decide(operator_request)
        self.assertEqual([1, 1], [decision["dop"] for decision in scalar["decisions"]])
        operator_request["fragments"][0]["operator_counts"] = {"ANALYTIC": 1}
        parallel = OperatorSubgraphEngine(
            "INTERSECT", 1, 4, parallel_dop=8,
            parallel_operator_counts={"ANALYTIC": 1}).decide(operator_request)
        self.assertEqual(
            [8, 1], [decision["dop"] for decision in parallel["decisions"]])
        with self.assertRaises(ValueError):
            OperatorSubgraphEngine("EXCEPT", 4, 1).decide(operator_request)

        cost_aware = CostAwareEngine(1, 4, 500, 4).decide(request())
        self.assertEqual([1, 4], [decision["dop"] for decision in cost_aware["decisions"]])

        policy = PolicyModel(self.metadata, {
            "hidden_dim": 16,
            "heads": 4,
            "policy_global_context": True,
            "fragment_position_encoding": True,
        })
        checkpoint = Path(self.temp_dir.name) / "policy.pt"
        save_checkpoint(checkpoint, "policy", policy, self.metadata)
        response = PolicyEngine(checkpoint).decide(request())
        self.assertEqual(2, len(response["decisions"]))
        self.assertTrue(all(decision["dop"] in [1, 2, 4, 8] for decision in response["decisions"]))

    def test_online_request_graph(self):
        sample = sample_from_request(request())
        graph = build_graph(sample, self.metadata, unknown_dop=True)
        self.assertTrue(torch.equal(graph["dop_ids"], torch.zeros(2, dtype=torch.long)))

    def test_tree_policy_engine(self):
        names = feature_names(self.metadata)
        fragment_index = names.index("fragment_index")
        checkpoint = {
            "kind": "tree_policy",
            "metadata": self.metadata,
            "feature_names": names,
            "tree": {
                "children_left": [1, -1, -1],
                "children_right": [2, -1, -1],
                "feature": [fragment_index, -2, -2],
                "threshold": [0.5, -2.0, -2.0],
                "prediction": [1, 1, 8],
            },
        }
        path = Path(self.temp_dir.name) / "tree_policy.json"
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        response = TreePolicyEngine(path).decide(request())
        self.assertEqual(
            [1, 8], [decision["dop"] for decision in response["decisions"]])

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

    def test_dataset_rejects_missing_request(self):
        root = Path(self.temp_dir.name)
        trace_path = root / "invalid.jsonl"
        trace_path.write_text(json.dumps({"latency_ms": 1}) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_dataset(trace_path, root / "invalid_dataset")

    def test_sample_from_trace_accepts_three_stage_snapshots(self):
        # Traces produced by FE builds with the three-stage instance DOP snapshots
        # (baseline/adjusted/final, DD04 §1.3) must still be consumable by the offline pipeline.
        record = trace([2, 4], 100)
        record["actual_fragments"] = [
            {
                "fragment_id": "F00",
                "plan_dop": 4,
                "baseline_instance_dops": [4],
                "adjusted_instance_dops": [2],
                "instance_dops": [2],
                "worker_ids": [10],
            },
            {
                "fragment_id": "F01",
                "plan_dop": 4,
                "baseline_instance_dops": [4, 4],
                "adjusted_instance_dops": [4, 4],
                "instance_dops": [4, 4],
                "worker_ids": [10, 11],
            },
        ]
        sample = sample_from_trace(record)
        self.assertEqual([2, 4], sample["dop_config"])

    def test_sample_from_trace_accepts_legacy_trace(self):
        # Traces without the three-stage fields keep working unchanged.
        sample = sample_from_trace(trace([2, 4], 100))
        self.assertEqual([2, 4], sample["dop_config"])


if __name__ == "__main__":
    unittest.main()
