import torch

from .graph import build_graph, sample_from_request
from .load_correction import apply_to_decision
from .model import load_policy_model


class FixedEngine:
    def __init__(self, fragment_dops):
        self.fragment_dops = dict(fragment_dops)

    def decide(self, request):
        fragments = request.get("fragments", [])
        if not fragments:
            raise ValueError("request.fragments is empty")
        fragment_ids = [str(fragment["fragment_id"]) for fragment in fragments]
        unknown = set(self.fragment_dops) - set(fragment_ids) - {"*"}
        if unknown and "*" not in self.fragment_dops:
            raise ValueError(f"fixed DOP contains unknown fragments: {sorted(unknown)}")
        missing = [fragment_id for fragment_id in fragment_ids if fragment_id not in self.fragment_dops]
        if missing and "*" not in self.fragment_dops:
            raise ValueError(f"fixed DOP misses fragments: {missing}")

        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            dop = int(self.fragment_dops.get(fragment_id, self.fragment_dops.get("*")))
            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            decisions.append({"fragment_id": fragment_id, "dop": dop, "reason": "fixed"})
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}


class CostAwareEngine:
    def __init__(self, low_dop, high_dop, high_fragment_cost, high_scan_ranges):
        self.low_dop = int(low_dop)
        self.high_dop = int(high_dop)
        self.high_fragment_cost = float(high_fragment_cost)
        self.high_scan_ranges = int(high_scan_ranges)
        if self.low_dop < 1 or self.high_dop < 1:
            raise ValueError("DOPs must be positive")
        if self.high_fragment_cost < 0 or self.high_scan_ranges < 0:
            raise ValueError("cost and scan-range thresholds must be non-negative")

    def decide(self, request):
        fragments = request.get("fragments", [])
        if not fragments:
            raise ValueError("request.fragments is empty")
        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            high_cost = float(fragment.get("fragment_cost", 0)) >= self.high_fragment_cost
            many_scan_ranges = int(fragment.get("scan_range_count", 0)) >= self.high_scan_ranges
            dop = self.high_dop if high_cost or many_scan_ranges else self.low_dop
            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            if high_cost and many_scan_ranges:
                reason = "high_cost_and_scan_ranges"
            elif high_cost:
                reason = "high_fragment_cost"
            elif many_scan_ranges:
                reason = "many_scan_ranges"
            else:
                reason = "lightweight_fragment"
            decisions.append({
                "fragment_id": fragment_id,
                "dop": dop,
                "reason": reason,
            })
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}


class TopologyAwareEngine:
    def __init__(
            self, low_dop, high_dop, high_fragment_cost,
            high_scan_ranges, serial_scan_ranges):
        self.low_dop = int(low_dop)
        self.high_dop = int(high_dop)
        self.high_fragment_cost = float(high_fragment_cost)
        self.high_scan_ranges = int(high_scan_ranges)
        self.serial_scan_ranges = int(serial_scan_ranges)
        if self.low_dop < 1 or self.high_dop < 1:
            raise ValueError("DOPs must be positive")
        if min(
                self.high_fragment_cost,
                self.high_scan_ranges,
                self.serial_scan_ranges) < 0:
            raise ValueError("cost and scan-range thresholds must be non-negative")
        if self.serial_scan_ranges >= self.high_scan_ranges:
            raise ValueError(
                "serial scan-range threshold must be below the high threshold")

    def decide(self, request):
        fragments = request.get("fragments", [])
        if not fragments:
            raise ValueError("request.fragments is empty")

        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            scan_nodes = int(fragment.get("scan_node_count", 0))
            scan_ranges = int(fragment.get("scan_range_count", 0))
            fragment_cost = float(fragment.get("fragment_cost", 0))

            if scan_nodes and scan_ranges <= self.serial_scan_ranges:
                dop = self.low_dop
                reason = "limited_scan_parallelism"
            elif (fragment_cost >= self.high_fragment_cost
                    or scan_ranges >= self.high_scan_ranges):
                dop = self.high_dop
                reason = (
                    "high_cost_and_scan_parallelism"
                    if (fragment_cost >= self.high_fragment_cost
                        and scan_ranges >= self.high_scan_ranges)
                    else "high_fragment_cost"
                    if fragment_cost >= self.high_fragment_cost
                    else "many_scan_ranges")
            else:
                dop = self.low_dop
                reason = "lightweight_fragment"

            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            decisions.append({
                "fragment_id": fragment_id,
                "dop": dop,
                "reason": reason,
            })
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}


class HeterogeneousEngine:
    def __init__(
            self, serial_operator, low_dop, high_dop,
            high_fragment_cost, high_scan_ranges,
            constrained_worker_ids=None):
        self.serial_operator = str(serial_operator).upper()
        self.low_dop = int(low_dop)
        self.high_dop = int(high_dop)
        self.high_fragment_cost = float(high_fragment_cost)
        self.high_scan_ranges = int(high_scan_ranges)
        self.constrained_worker_ids = {
            int(worker_id) for worker_id in (constrained_worker_ids or [])
        }
        if not self.serial_operator:
            raise ValueError("serial operator is empty")
        if self.low_dop < 1 or self.high_dop < 1:
            raise ValueError("DOPs must be positive")
        if self.high_fragment_cost < 0 or self.high_scan_ranges < 0:
            raise ValueError("cost and scan-range thresholds must be non-negative")

    def decide(self, request):
        fragments = request.get("fragments", [])
        if not fragments:
            raise ValueError("request.fragments is empty")
        fragment_by_id = {
            str(fragment["fragment_id"]): fragment for fragment in fragments
        }
        if len(fragment_by_id) != len(fragments):
            raise ValueError("request contains duplicate fragment ids")

        serial_fragments = {
            fragment_id
            for fragment_id, fragment in fragment_by_id.items()
            if any(
                self.serial_operator in str(operator).upper()
                for operator in fragment.get("operator_counts", {})
            )
        }
        sources_by_destination = {}
        for edge in request.get("edges", []):
            source = str(edge["source_fragment_id"])
            destination = str(edge["dest_fragment_id"])
            if source not in fragment_by_id or destination not in fragment_by_id:
                raise ValueError("edge references unknown fragment")
            sources_by_destination.setdefault(destination, []).append(source)
        stack = list(serial_fragments)
        while stack:
            destination = stack.pop()
            for source in sources_by_destination.get(destination, []):
                if source not in serial_fragments:
                    serial_fragments.add(source)
                    stack.append(source)

        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            fragment_cost = float(fragment.get("fragment_cost", 0))
            scan_ranges = int(fragment.get("scan_range_count", 0))
            worker_ids = {
                int(worker_id) for worker_id in fragment.get("worker_ids", [])
            }
            constrained_workers = (
                worker_ids
                and worker_ids.issubset(self.constrained_worker_ids)
            )
            if constrained_workers:
                dop = self.low_dop
                reason = "constrained_workers"
            elif fragment_id in serial_fragments:
                dop = self.low_dop
                reason = f"{self.serial_operator.lower()}_input_subgraph"
            elif (fragment_cost >= self.high_fragment_cost
                    or scan_ranges >= self.high_scan_ranges):
                dop = self.high_dop
                reason = (
                    "high_cost_and_scan_parallelism"
                    if (fragment_cost >= self.high_fragment_cost
                        and scan_ranges >= self.high_scan_ranges)
                    else "high_fragment_cost"
                    if fragment_cost >= self.high_fragment_cost
                    else "many_scan_ranges")
            else:
                dop = self.low_dop
                reason = "lightweight_fragment"
            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            decisions.append({
                "fragment_id": fragment_id,
                "dop": dop,
                "reason": reason,
            })
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}


class OperatorSubgraphEngine:
    def __init__(
            self, operator, operator_dop, default_dop,
            serial_cardinality_threshold=None, parallel_dop=None,
            parallel_operator_counts=None):
        self.operator = str(operator).upper()
        self.operator_dop = int(operator_dop)
        self.default_dop = int(default_dop)
        self.serial_cardinality_threshold = (
            None if serial_cardinality_threshold is None
            else float(serial_cardinality_threshold))
        self.parallel_dop = (
            None if parallel_dop is None else int(parallel_dop))
        self.parallel_operator_counts = {
            str(name).upper(): int(count)
            for name, count in (parallel_operator_counts or {}).items()
        }
        if not self.operator:
            raise ValueError("operator is empty")
        if (self.serial_cardinality_threshold is not None
                and self.serial_cardinality_threshold < 0):
            raise ValueError("serial cardinality threshold must be non-negative")
        if self.parallel_dop is None and self.parallel_operator_counts:
            raise ValueError("parallel DOP is required with parallel operator counts")
        if self.parallel_dop is not None and not self.parallel_operator_counts:
            raise ValueError("parallel operator counts are required with parallel DOP")
        if any(count < 1 for count in self.parallel_operator_counts.values()):
            raise ValueError("parallel operator counts must be positive")

    def decide(self, request):
        fragments = request.get("fragments", [])
        if not fragments:
            raise ValueError("request.fragments is empty")
        fragment_by_id = {str(fragment["fragment_id"]): fragment for fragment in fragments}
        if len(fragment_by_id) != len(fragments):
            raise ValueError("request contains duplicate fragment ids")

        selected = {
            fragment_id
            for fragment_id, fragment in fragment_by_id.items()
            if any(
                self.operator in str(name).upper()
                for name in fragment.get("operator_counts", {})
            )
        }
        if not selected:
            raise ValueError(f"operator {self.operator} is absent from the plan")

        sources_by_destination = {}
        for edge in request.get("edges", []):
            source = str(edge["source_fragment_id"])
            destination = str(edge["dest_fragment_id"])
            if source not in fragment_by_id or destination not in fragment_by_id:
                raise ValueError("edge references unknown fragment")
            sources_by_destination.setdefault(destination, []).append(source)

        stack = list(selected)
        while stack:
            destination = stack.pop()
            for source in sources_by_destination.get(destination, []):
                if source not in selected:
                    selected.add(source)
                    stack.append(source)

        parallel_selected = set()
        if self.parallel_operator_counts:
            for fragment_id, fragment in fragment_by_id.items():
                operator_counts = {
                    str(name).upper(): int(count)
                    for name, count in fragment.get("operator_counts", {}).items()
                }
                if any(
                        operator_counts.get(name, 0) >= minimum
                        for name, minimum in self.parallel_operator_counts.items()):
                    parallel_selected.add(fragment_id)
            stack = list(parallel_selected)
            while stack:
                destination = stack.pop()
                for source in sources_by_destination.get(destination, []):
                    if source not in parallel_selected:
                        parallel_selected.add(source)
                        stack.append(source)

        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            estimated_cardinality = float(fragment.get("estimated_cardinality", -1))
            is_scalar = (
                self.serial_cardinality_threshold is not None
                and 0 <= estimated_cardinality <= self.serial_cardinality_threshold)
            if fragment_id in selected:
                dop = self.operator_dop
            elif fragment_id in parallel_selected:
                dop = self.parallel_dop
            elif is_scalar:
                dop = self.operator_dop
            else:
                dop = self.default_dop
            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            if fragment_id in selected:
                reason = f"{self.operator.lower()}_input_subgraph"
            elif fragment_id in parallel_selected:
                reason = "parallel_operator_input_subgraph"
            elif is_scalar:
                reason = "estimated_scalar"
            else:
                reason = "default"
            decisions.append({
                "fragment_id": fragment_id,
                "dop": dop,
                "reason": reason,
            })
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}


class PolicyEngine:
    def __init__(self, checkpoint_path, load_correction=None):
        self.model, checkpoint = load_policy_model(checkpoint_path)
        self.metadata = checkpoint["metadata"]
        self.model.eval()
        self.load_correction = load_correction or {}

    def decide(self, request):
        sample = sample_from_request(request)
        graph = build_graph(sample, self.metadata, unknown_dop=True)
        with torch.no_grad():
            classes = self.model(graph).argmax(dim=-1).tolist()
        decisions = [
            {
                "fragment_id": fragment_id,
                "dop": int(self.metadata["dop_classes"][class_index]),
                "reason": "policy",
            }
            for fragment_id, class_index in zip(sample["fragment_ids"], classes)
        ]
        return self._apply_load_correction(request, decisions)

    def _apply_load_correction(self, request, decisions):
        if not self.load_correction:
            return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}
        worker_loads = request.get("worker_loads", {})
        fragment_by_id = {
            str(fragment["fragment_id"]): fragment
            for fragment in request.get("fragments", [])
        }
        corrected = [
            apply_to_decision(decision, fragment_by_id[decision["fragment_id"]], worker_loads,
                              **self.load_correction)
            for decision in decisions
        ]
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": corrected}
