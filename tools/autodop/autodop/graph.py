import math

import torch

from .data import fragment_operator_signature, normalized_operator_counts, transformed_numeric


def sample_from_request(request):
    raw_fragments = request.get("fragments", [])
    if not raw_fragments:
        raise ValueError("request.fragments is empty")
    ordered = sorted(raw_fragments, key=lambda fragment: int(fragment["fragment_index"]))
    fragment_ids = [str(fragment["fragment_id"]) for fragment in ordered]
    if len(fragment_ids) != len(set(fragment_ids)):
        raise ValueError("request contains duplicate fragment ids")
    id_to_index = {fragment_id: index for index, fragment_id in enumerate(fragment_ids)}
    fragments = []
    for fragment in ordered:
        workers = [int(worker) for worker in fragment.get("worker_ids", [])]
        fragments.append({
            **fragment,
            "operator_counts": normalized_operator_counts(fragment),
            "operator_signature": str(
                fragment.get("operator_signature", "") or fragment_operator_signature(fragment)),
            "worker_ids": workers,
            "worker_count": len(set(workers)),
        })
    edges = []
    for edge in request.get("edges", []):
        source = str(edge["source_fragment_id"])
        destination = str(edge["dest_fragment_id"])
        if source not in id_to_index or destination not in id_to_index:
            raise ValueError("edge references unknown fragment")
        edges.append({
            "source": id_to_index[source],
            "destination": id_to_index[destination],
            "partition_type": str(edge.get("partition_type", "")),
        })
    return {"fragments": fragments, "edges": edges, "fragment_ids": fragment_ids}


def _competition(fragments):
    size = len(fragments)
    matrix = torch.zeros((size, size), dtype=torch.float32)
    workers = [set(fragment.get("worker_ids", [])) for fragment in fragments]
    for left in range(size):
        for right in range(left + 1, size):
            union = workers[left] | workers[right]
            if not union:
                continue
            weight = len(workers[left] & workers[right]) / len(union)
            matrix[left, right] = weight
            matrix[right, left] = weight
    return matrix


def build_graph(sample, metadata, unknown_dop=False, device="cpu"):
    fragments = sample["fragments"]
    size = len(fragments)
    if size == 0:
        raise ValueError("graph has no fragments")

    categorical = []
    for fragment in fragments:
        row = []
        for feature in metadata["category_features"]:
            vocab = metadata["category_vocab"][feature]
            row.append(vocab.get(str(fragment.get(feature, "")), 0))
        categorical.append(row)

    numerical = []
    for fragment in fragments:
        values = transformed_numeric(fragment)
        numerical.append([
            (value - mean) / std
            for value, mean, std in zip(values, metadata["feature_mean"], metadata["feature_std"])
        ])

    dependency = torch.zeros((size, size), dtype=torch.float32)
    for edge in sample.get("edges", []):
        dependency[int(edge["source"]), int(edge["destination"])] = 1.0

    dop_classes = metadata["dop_classes"]
    dop_to_id = {dop: index + 1 for index, dop in enumerate(dop_classes)}
    dop_config = sample.get("dop_config", [0] * size)
    if len(dop_config) != size:
        raise ValueError("dop_config size does not match fragment count")
    if unknown_dop:
        dop_ids = [0] * size
    else:
        try:
            dop_ids = [dop_to_id[int(dop)] for dop in dop_config]
        except KeyError as exc:
            raise ValueError(f"unsupported DOP {exc.args[0]}") from exc
    candidate_mask = [
        [dop in set(int(value) for value in fragment.get("candidate_dops", dop_classes)) for dop in dop_classes]
        for fragment in fragments
    ]
    if any(not any(row) for row in candidate_mask):
        raise ValueError("fragment has no supported DOP candidate")

    root_index = next(
        (index for index, fragment in enumerate(fragments) if fragment.get("is_gather_root", False)), 0)
    graph = {
        "categorical": torch.tensor(categorical, dtype=torch.long),
        "numerical": torch.tensor(numerical, dtype=torch.float32),
        "dependency": dependency,
        "competition": _competition(fragments),
        "dop_ids": torch.tensor(dop_ids, dtype=torch.long),
        "candidate_mask": torch.tensor(candidate_mask, dtype=torch.bool),
        "root_index": root_index,
    }
    return {
        key: value.to(device) if isinstance(value, torch.Tensor) else value
        for key, value in graph.items()
    }


def dependency_degree(sample):
    degree = [0] * len(sample["fragments"])
    for edge in sample.get("edges", []):
        degree[int(edge["source"])] += 1
        degree[int(edge["destination"])] += 1
    return degree


def fragment_order(sample):
    degree = dependency_degree(sample)
    scores = []
    for index, fragment in enumerate(sample["fragments"]):
        score = (
            math.log1p(max(0.0, float(fragment.get("fragment_cost", 0.0))))
            + 0.5 * math.log1p(max(0, int(fragment.get("scan_range_count", 0))))
            + 0.2 * degree[index]
        )
        scores.append((score, index))
    return [index for _, index in sorted(scores, reverse=True)]
