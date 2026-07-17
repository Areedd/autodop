import hashlib
import itertools
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median


DOP_CLASSES = [1, 2, 4, 8, 16, 32]
CATEGORY_FEATURES = [
    "root_node",
    "leftmost_node",
    "operator_signature",
    "data_partition",
    "output_partition",
    "sink_type",
    "is_colocated",
    "is_replicated",
    "is_bucket_shuffle",
    "is_gather_root",
]
NUMERIC_FEATURES = [
    "fragment_cost",
    "estimated_cardinality",
    "estimated_avg_row_size",
    "scan_range_count",
    "instance_count",
    "worker_count",
    "intersect_node_count",
    "except_node_count",
    "join_node_count",
    "aggregate_node_count",
    "sort_node_count",
]

OPERATOR_NUMERIC_FEATURES = {
    "intersect_node_count": "INTERSECT",
    "except_node_count": "EXCEPT",
    "join_node_count": "JOIN",
    "aggregate_node_count": "AGGREGATE",
    "sort_node_count": "SORT",
}


def read_jsonl(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at {path}:{line_number}") from exc


def write_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def trace_files(path):
    path = Path(path)
    if path.is_file():
        return [path]
    files = sorted(path.rglob("autodop_trace_*.jsonl"))
    if not files:
        files = sorted(path.rglob("*.jsonl"))
    if not files:
        raise ValueError(f"no trace JSONL files found under {path}")
    return files


def _required(mapping, key, context):
    if key not in mapping:
        raise ValueError(f"missing {context}.{key}")
    return mapping[key]


def normalized_operator_counts(fragment):
    return {
        str(name): int(count)
        for name, count in fragment.get("operator_counts", {}).items()
        if int(count) > 0
    }


def fragment_operator_signature(fragment):
    counts = normalized_operator_counts(fragment)
    return "|".join(f"{name}={counts[name]}" for name in sorted(counts))


def operator_count(fragment, token):
    token = token.upper()
    return sum(
        count for name, count in normalized_operator_counts(fragment).items()
        if token in name.upper()
    )


def _fragment(raw):
    workers = [int(worker) for worker in raw.get("worker_ids", [])]
    operator_counts = normalized_operator_counts(raw)
    return {
        "fragment_index": int(_required(raw, "fragment_index", "fragment")),
        "root_node": str(raw.get("root_node", "")),
        "leftmost_node": str(raw.get("leftmost_node", "")),
        "operator_counts": operator_counts,
        "operator_signature": str(
            raw.get("operator_signature", "") or fragment_operator_signature(raw)),
        "data_partition": str(raw.get("data_partition", "")),
        "output_partition": str(raw.get("output_partition", "")),
        "sink_type": str(raw.get("sink_type", "")),
        "fragment_cost": float(raw.get("fragment_cost", 0.0) or 0.0),
        "estimated_cardinality": float(raw.get("estimated_cardinality", 0.0) or 0.0),
        "estimated_avg_row_size": float(raw.get("estimated_avg_row_size", 0.0) or 0.0),
        "scan_range_count": int(raw.get("scan_range_count", 0) or 0),
        "instance_count": int(raw.get("instance_count", len(workers)) or 0),
        "worker_count": len(set(workers)),
        "worker_ids": workers,
        "candidate_dops": [int(dop) for dop in raw.get("candidate_dops", DOP_CLASSES)],
        "is_colocated": bool(raw.get("is_colocated", False)),
        "is_replicated": bool(raw.get("is_replicated", False)),
        "is_bucket_shuffle": bool(raw.get("is_bucket_shuffle", False)),
        "is_gather_root": bool(raw.get("is_gather_root", False)),
    }


def _hash(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sample_from_trace(record):
    request = _required(record, "request", "trace")
    raw_fragments = _required(request, "fragments", "request")
    if not raw_fragments:
        raise ValueError("request.fragments is empty")

    ordered = sorted(raw_fragments, key=lambda fragment: int(fragment["fragment_index"]))
    fragment_ids = [str(fragment["fragment_id"]) for fragment in ordered]
    if len(fragment_ids) != len(set(fragment_ids)):
        raise ValueError("request contains duplicate fragment ids")
    id_to_index = {fragment_id: index for index, fragment_id in enumerate(fragment_ids)}
    fragments = [_fragment(fragment) for fragment in ordered]

    edges = []
    for edge in request.get("edges", []):
        source_id = str(_required(edge, "source_fragment_id", "edge"))
        destination_id = str(_required(edge, "dest_fragment_id", "edge"))
        if source_id not in id_to_index or destination_id not in id_to_index:
            raise ValueError("edge references unknown fragment")
        edges.append({
            "source": id_to_index[source_id],
            "destination": id_to_index[destination_id],
            "partition_type": str(edge.get("partition_type", "")),
        })
    edges.sort(key=lambda edge: (edge["source"], edge["destination"], edge["partition_type"]))

    installed = _required(_required(record, "auto_dop", "trace"), "installed_dop", "auto_dop")
    try:
        dop_config = [int(installed[fragment_id]) for fragment_id in fragment_ids]
    except KeyError as exc:
        raise ValueError(f"installed_dop misses fragment {exc.args[0]}") from exc
    for fragment, dop in zip(fragments, dop_config):
        if dop not in DOP_CLASSES or dop not in fragment["candidate_dops"]:
            raise ValueError(f"installed DOP {dop} is invalid for fragment {fragment['fragment_index']}")

    latency_ms = float(_required(record, "latency_ms", "trace"))
    if not math.isfinite(latency_ms) or latency_ms <= 0:
        raise ValueError("latency_ms must be positive")

    plan_fragments = [
        {key: value for key, value in fragment.items() if key != "worker_ids"}
        for fragment in fragments
    ]
    plan_key = _hash({"fragments": plan_fragments, "edges": edges})
    config_key = _hash(dop_config)
    return {
        "plan_key": plan_key,
        "config_key": config_key,
        "fragments": fragments,
        "edges": edges,
        "dop_config": dop_config,
        "latency_ms": latency_ms,
    }


def aggregate_samples(records):
    grouped = {}
    latencies = defaultdict(list)
    for record in records:
        sample = sample_from_trace(record)
        key = (sample["plan_key"], sample["config_key"])
        grouped.setdefault(key, sample)
        latencies[key].append(sample["latency_ms"])

    samples = []
    for key in sorted(grouped):
        sample = dict(grouped[key])
        sample["latency_ms"] = float(median(latencies[key]))
        samples.append(sample)
    return samples


def build_pairs(samples, threshold=0.03):
    by_plan = defaultdict(list)
    for sample in samples:
        by_plan[sample["plan_key"]].append(sample)

    pairs = []
    for plan_key, plan_samples in sorted(by_plan.items()):
        for left, right in itertools.combinations(plan_samples, 2):
            relative_gap = abs(left["latency_ms"] - right["latency_ms"]) / min(
                left["latency_ms"], right["latency_ms"])
            if relative_gap < threshold:
                continue
            fast, slow = (left, right) if left["latency_ms"] < right["latency_ms"] else (right, left)
            pairs.append({
                "plan_key": plan_key,
                "fast_config_key": fast["config_key"],
                "slow_config_key": slow["config_key"],
            })
    return pairs


def transformed_numeric(fragment):
    values = []
    for feature in NUMERIC_FEATURES:
        value = fragment.get(feature, 0.0)
        if feature in OPERATOR_NUMERIC_FEATURES:
            value = operator_count(fragment, OPERATOR_NUMERIC_FEATURES[feature])
        values.append(math.log1p(max(0.0, float(value))))
    return values


def build_metadata(samples):
    vocab = {}
    for feature in CATEGORY_FEATURES:
        values = sorted({str(fragment.get(feature, "")) for sample in samples for fragment in sample["fragments"]})
        vocab[feature] = {"<UNK>": 0, **{value: index + 1 for index, value in enumerate(values)}}

    rows = [transformed_numeric(fragment) for sample in samples for fragment in sample["fragments"]]
    means = []
    stds = []
    for column in range(len(NUMERIC_FEATURES)):
        values = [row[column] for row in rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        means.append(mean)
        stds.append(max(math.sqrt(variance), 1e-6))
    return {
        "category_features": CATEGORY_FEATURES,
        "numeric_features": NUMERIC_FEATURES,
        "category_vocab": vocab,
        "feature_mean": means,
        "feature_std": stds,
        "dop_classes": DOP_CLASSES,
        "sample_count": len(samples),
    }


def build_dataset(input_path, output_path, pair_threshold=0.03):
    records = (record for path in trace_files(input_path) for record in read_jsonl(path))
    samples = aggregate_samples(records)
    if not samples:
        raise ValueError("no valid AutoDOP samples")
    pairs = build_pairs(samples, pair_threshold)
    metadata = build_metadata(samples)
    metadata["pair_count"] = len(pairs)

    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "samples.jsonl", samples)
    write_jsonl(output / "pairs.jsonl", pairs)
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return samples, pairs, metadata


def load_dataset(path):
    path = Path(path)
    samples = list(read_jsonl(path / "samples.jsonl"))
    pairs = list(read_jsonl(path / "pairs.jsonl"))
    metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    return samples, pairs, metadata
