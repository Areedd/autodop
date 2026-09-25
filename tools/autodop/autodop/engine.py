"""Online AutoDOP policy engine.

The production decision path is deliberately limited to the distilled
``PolicyModel`` described by the paper.  Offline explicit-DOP trace collection
uses ``autodop.fixed_engine`` and is not exposed by the HTTP service.
"""

from collections import OrderedDict
import json
import threading

import torch

from .graph import build_graph, sample_from_request
from .load_correction import apply_to_decision
from .model import load_policy_model


class PolicyEngine:
    def __init__(self, checkpoint_path, load_correction=None, cache_capacity=0):
        if type(cache_capacity) is not int or cache_capacity < 0:
            raise ValueError('cache_capacity must be a nonnegative integer')
        self.model, checkpoint = load_policy_model(checkpoint_path)
        self.metadata = checkpoint["metadata"]
        self.model.eval()
        self.load_correction = load_correction or {}
        # Cache only the immutable model's base decision. Current worker load
        # is always applied below, even on a hit. A new model needs a new engine.
        self.cache_capacity = cache_capacity
        self._decision_cache = OrderedDict()
        self._cache_lock = threading.Lock()

    def clear_decision_cache(self):
        with self._cache_lock:
            self._decision_cache.clear()

    def decide(self, request):
        sample = sample_from_request(request)
        confidence, base = self._base_decision(sample)
        decisions = [
            {"fragment_id": fragment_id, "dop": dop, "reason": "policy"}
            for fragment_id, dop in base
        ]
        return self._apply_load_correction(request, decisions, confidence)

    def _base_decision(self, sample):
        key = None
        if self.cache_capacity:
            # Entire normalized graph input, including placement and legal DOPs.
            # Query UUID and live worker loads are not model inputs.
            key = json.dumps(sample, sort_keys=True, separators=(',', ':'), allow_nan=False)
            with self._cache_lock:
                cached = self._decision_cache.get(key)
                if cached is not None:
                    self._decision_cache.move_to_end(key)
            if cached is not None:
                return cached
        graph = build_graph(sample, self.metadata, unknown_dop=True)
        with torch.no_grad():
            probabilities = torch.softmax(self.model(graph), dim=-1)
            confidence_by_fragment, classes = probabilities.max(dim=-1)
            confidence = float(confidence_by_fragment.min().cpu())
            classes = classes.tolist()
        base = tuple(
            (fragment_id, int(self.metadata["dop_classes"][class_index]))
            for fragment_id, class_index in zip(sample["fragment_ids"], classes)
        )
        entry = (confidence, base)
        if key is not None:
            with self._cache_lock:
                self._decision_cache[key] = entry
                self._decision_cache.move_to_end(key)
                while len(self._decision_cache) > self.cache_capacity:
                    self._decision_cache.popitem(last=False)
        return entry

    def _apply_load_correction(self, request, decisions, confidence):
        if self.load_correction:
            worker_loads = request.get("worker_loads", {})
            fragment_by_id = {
                str(fragment["fragment_id"]): fragment
                for fragment in request.get("fragments", [])
            }
            decisions = [
                apply_to_decision(
                    decision,
                    fragment_by_id[decision["fragment_id"]],
                    worker_loads,
                    **self.load_correction,
                )
                for decision in decisions
            ]
        return {
            "version": 1,
            "query_id": request.get("query_id", ""),
            "confidence": confidence,
            "decisions": decisions,
        }
