import torch

from .graph import build_graph, sample_from_request
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


class OperatorSubgraphEngine:
    def __init__(self, operator, operator_dop, default_dop):
        self.operator = str(operator).upper()
        self.operator_dop = int(operator_dop)
        self.default_dop = int(default_dop)
        if not self.operator:
            raise ValueError("operator is empty")

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

        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            dop = self.operator_dop if fragment_id in selected else self.default_dop
            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            decisions.append({
                "fragment_id": fragment_id,
                "dop": dop,
                "reason": f"{self.operator.lower()}_input_subgraph"
                if fragment_id in selected else "default",
            })
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}


class PolicyEngine:
    def __init__(self, checkpoint_path):
        self.model, checkpoint = load_policy_model(checkpoint_path)
        self.metadata = checkpoint["metadata"]
        self.model.eval()

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
        return {"version": 1, "query_id": request.get("query_id", ""), "decisions": decisions}
