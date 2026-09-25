import sys
import tempfile
import unittest
from pathlib import Path

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autodop.engine import PolicyEngine
from autodop.load_correction import (
    apply_to_decision,
    corrected_dop,
    fragment_factor,
    worker_factor,
)
from autodop.model import PolicyModel, save_checkpoint
from test_autodop import request


def load(cpu, mem, fresh=True):
    return {"cpu_used": cpu, "mem_used_pct": mem, "fresh": fresh}


class WorkerFactorTest(unittest.TestCase):
    def test_dead_zone_factor_is_one(self):
        # below both thresholds (cpu 0.4 / mem 0.65): factor stays 1
        self.assertEqual(1.0, worker_factor(load(0.3, 0.5)))
        self.assertEqual(1.0, worker_factor(load(0.4, 0.65)))
        self.assertEqual(1.0, worker_factor(load(0.0, 0.0)))

    def test_linear_drop_above_threshold(self):
        # cpu 0.7 with threshold 0.4, floor 0.1:
        #   1 - (0.7 - 0.4) / (1 - 0.4) * (1 - 0.1) = 0.55
        self.assertAlmostEqual(0.55, worker_factor(load(0.7, 0.0)), places=12)
        # mem 0.9 with threshold 0.65:
        #   1 - (0.9 - 0.65) / (1 - 0.65) * (1 - 0.1) = 1 - 0.25/0.35*0.9
        self.assertAlmostEqual(1.0 - 0.25 / 0.35 * 0.9, worker_factor(load(0.0, 0.9)), places=12)

    def test_floor_at_full_load(self):
        self.assertEqual(0.1, worker_factor(load(1.0, 0.0)))
        self.assertEqual(0.1, worker_factor(load(0.0, 1.0)))
        self.assertEqual(0.1, worker_factor(load(1.0, 1.0)))

    def test_min_across_cpu_and_mem_dimensions(self):
        # cpu stressed (factor 0.55), mem idle (factor 1): min is the cpu factor
        self.assertAlmostEqual(0.55, worker_factor(load(0.7, 0.1)), places=12)
        # both stressed: min of the two dims
        cpu_factor = 1.0 - (0.7 - 0.4) / 0.6 * 0.9
        mem_factor = 1.0 - (0.8 - 0.65) / 0.35 * 0.9
        self.assertAlmostEqual(min(cpu_factor, mem_factor), worker_factor(load(0.7, 0.8)), places=12)

    def test_missing_or_stale_load_is_neutral(self):
        self.assertEqual(1.0, worker_factor(None))
        self.assertEqual(1.0, worker_factor(load(0.9, 0.9, fresh=False)))

    def test_custom_thresholds_and_floor(self):
        factor = worker_factor(load(0.5, 0.0), cpu_threshold=0.2, mem_threshold=0.65, floor=0.3)
        self.assertAlmostEqual(1.0 - (0.5 - 0.2) / 0.8 * 0.7, factor, places=12)


class FragmentFactorTest(unittest.TestCase):
    def test_weakest_worker_wins(self):
        fragment = {"worker_ids": [10, 11]}
        loads = {"10": load(0.9, 0.0), "11": load(0.1, 0.1)}
        # cpu 0.9 -> 1 - (0.9-0.4)/0.6*0.9 = 0.25; idle worker -> 1; min = 0.25
        self.assertAlmostEqual(0.25, fragment_factor(fragment, loads), places=12)

    def test_missing_worker_counted_as_one(self):
        fragment = {"worker_ids": [10, 11]}
        loads = {"10": load(0.9, 0.0)}
        # worker 11 has no load data: factor 1 from it, so min(0.25, 1) = 0.25
        self.assertAlmostEqual(0.25, fragment_factor(fragment, loads), places=12)

    def test_no_workers_returns_one(self):
        self.assertEqual(1.0, fragment_factor({"worker_ids": []}, {}))


class CorrectedDopTest(unittest.TestCase):
    def test_rounds_to_candidate_without_raising(self):
        candidates = [1, 2, 4, 8, 16]
        # 8 * 0.5 = 4 -> exact candidate
        self.assertEqual(4, corrected_dop(8, 0.5, candidates))
        # 8 * 0.75 = 6 -> closest candidate at or below 6 is 4
        self.assertEqual(4, corrected_dop(8, 0.75, candidates))
        # 16 * 0.9 = 14.4 -> 8
        self.assertEqual(8, corrected_dop(16, 0.9, candidates))
        # factor 1 keeps the base DOP
        self.assertEqual(8, corrected_dop(8, 1.0, candidates))
        self.assertEqual(8, corrected_dop(8, 1.5, candidates))

    def test_never_below_one(self):
        self.assertEqual(1, corrected_dop(1, 0.1, [1, 2, 4]))
        self.assertEqual(1, corrected_dop(4, 0.05, [1, 2, 4]))

    def test_apply_to_decision_marks_reason_when_changed(self):
        fragment = {"worker_ids": [10], "candidate_dops": [1, 2, 4, 8]}
        decision = {"fragment_id": "F00", "dop": 8, "reason": "policy"}
        loads = {"10": load(0.9, 0.0)}
        corrected = apply_to_decision(decision, fragment, loads)
        # factor 0.25 -> 8 * 0.25 = 2 (exact candidate)
        self.assertEqual(2, corrected["dop"])
        self.assertEqual("policy:load_corrected", corrected["reason"])

    def test_apply_to_decision_unchanged_when_idle(self):
        fragment = {"worker_ids": [10], "candidate_dops": [1, 2, 4, 8]}
        decision = {"fragment_id": "F00", "dop": 4, "reason": "policy"}
        corrected = apply_to_decision(decision, fragment, {"10": load(0.1, 0.1)})
        self.assertEqual(4, corrected["dop"])
        self.assertEqual("policy", corrected["reason"])


class EngineIntegrationTest(unittest.TestCase):
    def test_policy_engine_corrected_with_worker_loads(self):
        metadata = {
            "category_features": [],
            "numeric_features": ["fragment_cost"],
            "category_vocab": {},
            "feature_mean": [0.0],
            "feature_std": [1.0],
            "dop_classes": [1, 2, 4, 8],
        }
        model = PolicyModel(metadata, {"hidden_dim": 16, "heads": 4})
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "policy.pt"
            save_checkpoint(checkpoint, "policy", model, metadata)
            engine = PolicyEngine(checkpoint, {
                "cpu_threshold": 0.4,
                "mem_threshold": 0.65,
                "floor": 0.1,
            })
            idle = request()
            idle["worker_loads"] = {"10": load(0.1, 0.1), "11": load(0.1, 0.1)}
            stressed = request()
            stressed["worker_loads"] = {"10": load(0.9, 0.9), "11": load(0.9, 0.9)}
            idle_decisions = engine.decide(idle)["decisions"]
            stressed_decisions = engine.decide(stressed)["decisions"]
            self.assertEqual(2, len(stressed_decisions))
            for fragment, idle_d, stressed_d in zip(
                    idle["fragments"], idle_decisions, stressed_decisions):
                # correction never raises the DOP, and the result stays in the candidate set
                self.assertLessEqual(stressed_d["dop"], idle_d["dop"])
                self.assertIn(stressed_d["dop"], fragment["candidate_dops"])
                if stressed_d["dop"] < idle_d["dop"]:
                    self.assertTrue(stressed_d["reason"].endswith(":load_corrected"))

    def test_policy_engine_without_loads_unchanged(self):
        metadata = {
            "category_features": [],
            "numeric_features": ["fragment_cost"],
            "category_vocab": {},
            "feature_mean": [0.0],
            "feature_std": [1.0],
            "dop_classes": [1, 2, 4, 8],
        }
        model = PolicyModel(metadata, {"hidden_dim": 16, "heads": 4})
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = Path(temp_dir) / "policy.pt"
            save_checkpoint(checkpoint, "policy", model, metadata)
            engine = PolicyEngine(checkpoint)  # no load correction configured
            response = engine.decide(request())
            self.assertEqual(2, len(response["decisions"]))
            self.assertTrue(all("load_corrected" not in d["reason"] for d in response["decisions"]))

if __name__ == "__main__":
    unittest.main()
