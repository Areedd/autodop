import json
from pathlib import Path

from .data import transformed_numeric
from .graph import sample_from_request
from .load_correction import apply_to_decision


def _vocabulary(metadata, feature):
    return [
        value
        for value, index in sorted(
            metadata["category_vocab"][feature].items(),
            key=lambda item: item[1])
        if index != 0
    ]


def feature_names(metadata):
    names = [
        f"local_numeric:{feature}"
        for feature in metadata["numeric_features"]
    ]
    names.extend([
        f"plan_mean:{feature}"
        for feature in metadata["numeric_features"]
    ])
    names.extend([
        f"plan_max:{feature}"
        for feature in metadata["numeric_features"]
    ])
    for feature in metadata["category_features"]:
        for value in _vocabulary(metadata, feature):
            names.append(f"local_category:{feature}={value}")
    for feature in metadata["category_features"]:
        for value in _vocabulary(metadata, feature):
            names.append(f"plan_category_count:{feature}={value}")
    names.extend([
        "fragment_index",
        "reverse_fragment_index",
        "fragment_position",
        "fragment_count",
        "edge_count",
        "in_degree",
        "out_degree",
    ])
    return names


def feature_rows(sample, metadata):
    fragments = sample["fragments"]
    if not fragments:
        raise ValueError("sample has no fragments")
    numeric = [transformed_numeric(fragment) for fragment in fragments]
    width = len(numeric[0])
    means = [
        sum(row[column] for row in numeric) / len(numeric)
        for column in range(width)
    ]
    maxima = [
        max(row[column] for row in numeric)
        for column in range(width)
    ]
    category_counts = {}
    for feature in metadata["category_features"]:
        counts = {
            value: 0.0 for value in _vocabulary(metadata, feature)
        }
        for fragment in fragments:
            value = str(fragment.get(feature, ""))
            if value in counts:
                counts[value] += 1.0
        category_counts[feature] = counts

    in_degree = [0.0] * len(fragments)
    out_degree = [0.0] * len(fragments)
    for edge in sample.get("edges", []):
        source = int(edge["source"])
        destination = int(edge["destination"])
        out_degree[source] += 1.0
        in_degree[destination] += 1.0

    rows = []
    for index, fragment in enumerate(fragments):
        row = list(numeric[index]) + means + maxima
        for feature in metadata["category_features"]:
            actual = str(fragment.get(feature, ""))
            row.extend(
                1.0 if actual == value else 0.0
                for value in _vocabulary(metadata, feature)
            )
        for feature in metadata["category_features"]:
            row.extend(category_counts[feature].values())
        row.extend([
            float(index),
            float(len(fragments) - index - 1),
            float(index) / max(1, len(fragments) - 1),
            float(len(fragments)),
            float(len(sample.get("edges", []))),
            in_degree[index],
            out_degree[index],
        ])
        rows.append(row)

    names = feature_names(metadata)
    if any(len(row) != len(names) for row in rows):
        raise RuntimeError("tree policy feature width is inconsistent")
    return rows


def predict_tree(tree, row):
    node = 0
    while int(tree["children_left"][node]) != int(tree["children_right"][node]):
        feature = int(tree["feature"][node])
        node = (
            int(tree["children_left"][node])
            if row[feature] <= float(tree["threshold"][node])
            else int(tree["children_right"][node])
        )
    return int(tree["prediction"][node])


class TreePolicyEngine:
    def __init__(self, checkpoint_path, load_correction=None):
        self.checkpoint = json.loads(
            Path(checkpoint_path).read_text(encoding="utf-8"))
        if self.checkpoint.get("kind") != "tree_policy":
            raise ValueError("checkpoint is not a tree policy")
        self.metadata = self.checkpoint["metadata"]
        expected = feature_names(self.metadata)
        if self.checkpoint["feature_names"] != expected:
            raise ValueError("tree policy feature schema does not match metadata")
        self.load_correction = load_correction or {}

    def decide(self, request):
        sample = sample_from_request(request)
        rows = feature_rows(sample, self.metadata)
        decisions = []
        for fragment, fragment_id, row in zip(
                sample["fragments"], sample["fragment_ids"], rows):
            dop = predict_tree(self.checkpoint["tree"], row)
            candidates = [
                int(value) for value in fragment.get(
                    "candidate_dops", self.metadata["dop_classes"])
            ]
            if dop not in candidates:
                raise ValueError(f"DOP {dop} is not a candidate for {fragment_id}")
            decisions.append({
                "fragment_id": fragment_id,
                "dop": dop,
                "reason": "learned_tree_policy",
            })
        if self.load_correction:
            worker_loads = request.get("worker_loads", {})
            fragment_by_id = {
                str(fragment["fragment_id"]): fragment
                for fragment in request.get("fragments", [])
            }
            decisions = [
                apply_to_decision(decision, fragment_by_id[decision["fragment_id"]],
                                  worker_loads, **self.load_correction)
                for decision in decisions
            ]
        return {
            "version": 1,
            "query_id": request.get("query_id", ""),
            "decisions": decisions,
        }
