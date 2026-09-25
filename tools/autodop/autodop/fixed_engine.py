"""Explicit DOP-vector helper for offline trace collection and benchmarks.

This helper is intentionally not exposed by ``autodop_server.py``.  It exists so
the offline CostModel/MCTS workflow can execute a requested per-fragment DOP
configuration while collecting labels; it is not an online policy or rule engine.
"""


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
        missing = [
            fragment_id
            for fragment_id in fragment_ids
            if fragment_id not in self.fragment_dops
        ]
        if missing and "*" not in self.fragment_dops:
            raise ValueError(f"fixed DOP misses fragments: {missing}")

        decisions = []
        for fragment in fragments:
            fragment_id = str(fragment["fragment_id"])
            dop = int(self.fragment_dops.get(fragment_id, self.fragment_dops.get("*")))
            if dop not in fragment.get("candidate_dops", []):
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            decisions.append({"fragment_id": fragment_id, "dop": dop, "reason": "fixed"})
        return {
            "version": 1,
            "query_id": request.get("query_id", ""),
            "confidence": 1.0,
            "decisions": decisions,
        }
