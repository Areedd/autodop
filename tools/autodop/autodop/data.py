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
    "join_type",
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


def supported_dops(fragment, dop_classes):
    """Learning/search domain shared with the serial collector's gather contract.

    FE advertises a broader domain for gather roots, but trusted training probes
    keep them at 1. Do not extrapolate a fictitious gain on an unmeasured axis.
    Raw features remain unchanged so historical dataset fingerprints stay valid.
    """
    classes = set(map(int, dop_classes))
    return sorted({int(dop) for dop in fragment.get("candidate_dops", dop_classes)
                   if int(dop) in classes
                   and (not fragment.get("is_gather_root", False) or int(dop) == 1)})


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
        "join_type": str(raw.get("join_type", "")),
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


def sample_from_trace(record, skip_errors=False, dop_classes=DOP_CLASSES):
    """Parses one FE trace record into a training sample.

    With ``skip_errors=False`` (strict mode, used by tests and debugging) malformed records
    raise. With ``skip_errors=True`` the record-level errors (missing/type-invalid fields,
    unknown DOP, bad latency) are captured and ``None`` is returned instead of raising, so
    the dataset build can skip the record and count it.
    """
    try:
        return _sample_from_trace(record, dop_classes)
    except (ValueError, KeyError, TypeError) as exc:
        if skip_errors:
            return None
        raise


def _sample_from_trace(record, dop_classes):
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
        if dop not in dop_classes or dop not in fragment["candidate_dops"]:
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


def aggregate_samples(records, skip_errors=False, dop_classes=DOP_CLASSES):
    """Aggregates trace records into per-(plan, config) samples with median latency.

    Returns ``(samples, skipped)`` where ``skipped`` is a list of ``(index, reason)`` for
    records that failed to parse (only collected when ``skip_errors=True``; otherwise the
    first bad record raises).
    """
    grouped = {}
    latencies = defaultdict(list)
    skipped = []
    for index, record in enumerate(records):
        if skip_errors:
            try:
                sample = sample_from_trace(record, dop_classes=dop_classes)
            except (ValueError, KeyError, TypeError) as exc:
                skipped.append((index, str(exc)))
                continue
        else:
            sample = sample_from_trace(record, dop_classes=dop_classes)
        key = (sample["plan_key"], sample["config_key"])
        grouped.setdefault(key, sample)
        latencies[key].append(sample["latency_ms"])

    samples = []
    for key in sorted(grouped):
        sample = dict(grouped[key])
        sample["latency_ms"] = float(median(latencies[key]))
        samples.append(sample)
    return samples, skipped


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
                "fast_latency_ms": fast["latency_ms"],
                "slow_latency_ms": slow["latency_ms"],
                "margin": math.log1p(relative_gap),
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


def build_metadata(samples, dop_classes=DOP_CLASSES):
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
        "dop_classes": list(dop_classes),
        "sample_count": len(samples),
    }


def build_dataset(input_path, output_path, pair_threshold=0.03, dop_classes=DOP_CLASSES,
                  skip_errors=True):
    """Builds a dataset from FE trace files.

    By default malformed records are skipped and counted in ``metadata["skipped_records"]``
    instead of aborting the build (G8). JSON syntax errors in a trace file still abort: they
    indicate file-level corruption rather than a single bad record. Pass ``skip_errors=False``
    for the strict mode used by tests/debugging.
    """
    records = (record for path in trace_files(input_path) for record in read_jsonl(path))
    samples, skipped = aggregate_samples(records, skip_errors=skip_errors, dop_classes=dop_classes)
    if not samples:
        raise ValueError("no valid AutoDOP samples")
    pairs = build_pairs(samples, pair_threshold)
    metadata = build_metadata(samples, dop_classes=dop_classes)
    metadata["pair_count"] = len(pairs)
    if skipped:
        reasons = {}
        for _, reason in skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        metadata["skipped_records"] = {"count": len(skipped), "reasons": reasons}
        print(f"WARN: skipped {len(skipped)} malformed trace record(s): {reasons}")

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
    # Historical corpora predate operator signatures. Normalize them using the
    # trace extractor's defaults so fitting and request-time encoding agree.
    # Keep recorded plan/config identities and any additional sample fields.
    for sample in samples:
        sample["fragments"] = [
            {**fragment, **_fragment(fragment)} for fragment in sample["fragments"]
        ]
    pairs = list(read_jsonl(path / "pairs.jsonl"))
    samples.sort(key=lambda sample: (sample["plan_key"], sample["config_key"]))
    pairs.sort(key=lambda pair: (pair["plan_key"], pair["fast_config_key"], pair["slow_config_key"]))
    metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    # Backwards compatibility: datasets built before dop_classes was stored fall back to the
    # default candidate set (G7).
    metadata.setdefault("dop_classes", list(DOP_CLASSES))
    return samples, pairs, metadata
