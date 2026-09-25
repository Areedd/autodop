import math

import torch

from .data import fragment_operator_signature, normalized_operator_counts, operator_count, supported_dops, transformed_numeric


COMPETITION_BASE = 0.05
COMPETITION_ALPHA = 0.95
PIPELINE_START_FRACTION = 0.25


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


def _operator_count(fragment, token):
    token = token.upper()
    count = operator_count(fragment, token)
    if count:
        return count
    names = " ".join([
        str(fragment.get("root_node", "")),
        str(fragment.get("leftmost_node", "")),
        str(fragment.get("operator_signature", "")),
    ]).upper()
    return int(token in names)


def _duration(fragment):
    cardinality = max(0.0, float(fragment.get("estimated_cardinality", 0.0)))
    row_size = max(0.0, float(fragment.get("estimated_avg_row_size", 0.0)))
    data_bytes = cardinality * row_size
    base = (
        1.0
        + math.log1p(max(0.0, float(fragment.get("fragment_cost", 0.0))))
        + 0.5 * math.log1p(data_bytes)
        + 0.25 * math.log1p(max(0, int(fragment.get("scan_range_count", 0))))
    )
    complexity = (
        1.0
        + 0.20 * _operator_count(fragment, "SCAN")
        + 0.45 * _operator_count(fragment, "JOIN")
        + 0.30 * _operator_count(fragment, "AGGREGATE")
        + 0.35 * _operator_count(fragment, "SORT")
    )
    return base * complexity


def _execution_windows(fragments, edges):
    size = len(fragments)
    predecessors = [[] for _ in range(size)]
    successors = [[] for _ in range(size)]
    indegree = [0] * size
    for edge in edges:
        source = int(edge["source"])
        destination = int(edge["destination"])
        predecessors[destination].append(source)
        successors[source].append(destination)
        indegree[destination] += 1

    durations = [_duration(fragment) for fragment in fragments]
    starts = [0.0] * size
    ready = [index for index, degree in enumerate(indegree) if degree == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        if predecessors[node]:
            starts[node] = max(
                starts[parent] + durations[parent] * PIPELINE_START_FRACTION
                for parent in predecessors[node]
            )
        for child in successors[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != size:
        raise ValueError("fragment dependency graph contains a cycle")
    return [(start, start + duration) for start, duration in zip(starts, durations)]


def _reachability(size, edges):
    successors = [[] for _ in range(size)]
    for edge in edges:
        successors[int(edge["source"])].append(int(edge["destination"]))
    reachable = [[False] * size for _ in range(size)]
    for source in range(size):
        stack = list(successors[source])
        while stack:
            destination = stack.pop()
            if reachable[source][destination]:
                continue
            reachable[source][destination] = True
            stack.extend(successors[destination])
    return reachable


def _overlap(left, right):
    overlap = max(0.0, min(left[1], right[1]) - max(left[0], right[0]))
    shorter = min(left[1] - left[0], right[1] - right[0])
    return overlap / shorter if shorter > 0 else 0.0


def _resource_demands(fragments, dop_config, unknown_dop):
    raw_scales = []
    for fragment in fragments:
        cardinality = max(0.0, float(fragment.get("estimated_cardinality", 0.0)))
        row_size = max(0.0, float(fragment.get("estimated_avg_row_size", 0.0)))
        raw_scales.append(
            math.log1p(max(0.0, float(fragment.get("fragment_cost", 0.0))))
            + math.log1p(cardinality * row_size)
            + math.log1p(max(0, int(fragment.get("scan_range_count", 0))))
        )
    scale_max = max(max(raw_scales, default=0.0), 1.0)

    demands = []
    for index, (fragment, raw_scale) in enumerate(zip(fragments, raw_scales)):
        candidates = [int(value) for value in fragment.get("candidate_dops", [])]
        max_dop = max(candidates, default=1)
        width = 1.0 if unknown_dop else min(1.0, max(1, int(dop_config[index])) / max_dop)
        scale = max(0.05, raw_scale / scale_max) * width
        scan = min(1.0, _operator_count(fragment, "SCAN"))
        join = min(1.0, _operator_count(fragment, "JOIN"))
        aggregate = min(1.0, _operator_count(fragment, "AGGREGATE"))
        sort = min(1.0, _operator_count(fragment, "SORT"))
        output_partition = str(fragment.get("output_partition", "")).upper()
        exchange = float(
            "STREAM" in str(fragment.get("sink_type", "")).upper()
            or output_partition not in ("", "UNPARTITIONED")
        )
        demands.append((
            min(1.0, scale * (0.35 + 0.30 * scan + 0.45 * join + 0.30 * aggregate)),
            min(1.0, scale * (0.20 + 0.55 * join + 0.45 * aggregate + 0.60 * sort)),
            min(1.0, scale * (0.10 + 0.75 * exchange + 0.20 * scan)),
            min(1.0, scale * (0.05 + 0.90 * exchange)),
        ))
    return demands


def _competition(fragments, edges, dop_config, unknown_dop):
    size = len(fragments)
    matrices = {
        name: torch.zeros((size, size), dtype=torch.float32)
        for name in ("competition", "competition_overlap", "worker_share", "resource_pressure")
    }
    workers = [set(fragment.get("worker_ids", [])) for fragment in fragments]
    windows = _execution_windows(fragments, edges)
    reachable = _reachability(size, edges)
    demands = _resource_demands(fragments, dop_config, unknown_dop)
    for left in range(size):
        for right in range(left + 1, size):
            if reachable[left][right] or reachable[right][left]:
                continue
            union = workers[left] | workers[right]
            if not union:
                continue
            worker_share = len(workers[left] & workers[right]) / len(union)
            if worker_share == 0.0:
                continue
            overlap = _overlap(windows[left], windows[right])
            pressure = sum(
                min(1.0, demands[left][dimension] + demands[right][dimension])
                for dimension in range(4)
            ) / 4.0
            weight = min(
                1.0,
                COMPETITION_BASE + COMPETITION_ALPHA * overlap * worker_share * pressure,
            )
            values = {
                "competition": weight,
                "competition_overlap": overlap,
                "worker_share": worker_share,
                "resource_pressure": pressure,
            }
            for name, value in values.items():
                matrices[name][left, right] = value
                matrices[name][right, left] = value
    return matrices


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
        [dop in supported_dops(fragment, dop_classes) for dop in dop_classes]
        for fragment in fragments
    ]
    if any(not any(row) for row in candidate_mask):
        raise ValueError("fragment has no supported DOP candidate")

    root_index = next(
        (index for index, fragment in enumerate(fragments) if fragment.get("is_gather_root", False)), 0)
    competition = _competition(fragments, sample.get("edges", []), dop_config, unknown_dop)
    graph = {
        "categorical": torch.tensor(categorical, dtype=torch.long),
        "numerical": torch.tensor(numerical, dtype=torch.float32),
        "dependency": dependency,
        **competition,
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
