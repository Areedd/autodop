"""Persisted data boundaries and teacher lineage for the offline learning pipeline."""

import hashlib
import json
import random
from pathlib import Path

from .data import supported_dops


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


def dataset_fingerprint(samples, pairs):
    """Hash complete records, independent of JSONL ordering and build metadata."""
    return _digest({"samples": sorted(_digest(row) for row in samples),
                    "pairs": sorted(_digest(row) for row in pairs)})


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _explicit_split(samples, pairs, manifest_path):
    payload = Path(manifest_path).read_bytes()
    manifest = json.loads(payload)
    partitions = {}
    for name in ("train", "valid", "test"):
        values = manifest.get(name)
        if not isinstance(values, list) or any(not isinstance(key, str) or not key for key in values):
            raise ValueError(f"split manifest {name} must be a list of plan keys")
        if len(values) != len(set(values)):
            raise ValueError(f"split manifest {name} contains duplicate plans")
        partitions[name] = set(values)
    train, valid, test = (partitions[name] for name in ("train", "valid", "test"))
    if not train or not valid:
        raise ValueError("explicit split requires nonempty train and valid partitions")
    if train & valid or train & test or valid & test:
        raise ValueError("split manifest partitions must be disjoint")
    if train | valid != {sample["plan_key"] for sample in samples}:
        raise ValueError("dataset must contain exactly the manifest train + valid plans")
    if any(pair["plan_key"] not in train | valid for pair in pairs):
        raise ValueError("ranking pair is outside the manifest train + valid plans")
    templates = {"train": set(), "valid": set()}
    template_by_plan = {}
    for sample in samples:
        plan = sample["plan_key"]
        partition = "train" if plan in train else "valid"
        template = sample.get("template_id")
        if not isinstance(template, str) or not template:
            raise ValueError("explicit split requires a template_id on every sample")
        if template_by_plan.setdefault(plan, template) != template:
            raise ValueError("a plan is assigned to multiple query templates")
        if sample.get("split", partition) != partition:
            raise ValueError("sample split disagrees with the split manifest")
        templates[partition].add(template)
    if templates["train"] & templates["valid"]:
        raise ValueError("train and valid query templates must be disjoint")
    return sorted(train), sorted(valid), {
        "method": "manifest", "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "train_template_ids": sorted(templates["train"]),
        "valid_template_ids": sorted(templates["valid"]), "test_plan_keys": sorted(test),
    }


def make_training_contract(samples, pairs, valid_fraction, seed, split_manifest=None):
    if split_manifest is None and not 0 <= valid_fraction < 1:
        raise ValueError("valid-fraction must be in [0, 1)")
    # Every plan is assigned, including plans without sufficiently separated ranking pairs.
    keys = sorted({sample["plan_key"] for sample in samples})
    if not keys:
        raise ValueError("dataset contains no samples")
    split_source = None
    if split_manifest is not None:
        train_keys, valid_keys, split_source = _explicit_split(samples, pairs, split_manifest)
        valid_fraction = None  # Boundaries were supplied, not sampled from a fraction.
    else:
        random.Random(seed).shuffle(keys)
        count = min(len(keys) - 1, max(0, int(round(len(keys) * valid_fraction))))
        train_keys, valid_keys = sorted(keys[count:]), sorted(keys[:count])
    contract = {
        "contract_version": 1,
        "dataset_fingerprint": dataset_fingerprint(samples, pairs),
        "seed": seed,
        "valid_fraction": valid_fraction,
        "train_plan_keys": train_keys,
        "valid_plan_keys": valid_keys,
    }
    if split_source is not None:
        contract["split_source"] = split_source
    contract["split_fingerprint"] = _digest(contract)
    return contract


def select_partition(samples, pairs, training, partition):
    if not training or training.get("contract_version") != 1:
        raise ValueError("checkpoint has no training contract; retrain CostModel")
    split_fields = {key: training[key] for key in (
        "contract_version", "dataset_fingerprint", "seed", "valid_fraction",
        "train_plan_keys", "valid_plan_keys")}
    if "split_source" in training:
        split_fields["split_source"] = training["split_source"]
    if _digest(split_fields) != training.get("split_fingerprint"):
        raise ValueError("invalid training split fingerprint")
    if dataset_fingerprint(samples, pairs) != training["dataset_fingerprint"]:
        raise ValueError("dataset fingerprint differs from CostModel training dataset")
    train = set(training["train_plan_keys"])
    valid = set(training["valid_plan_keys"])
    if train & valid or train | valid != {sample["plan_key"] for sample in samples}:
        raise ValueError("invalid plan split")
    keys = {"train": train, "valid": valid, "all": train | valid}.get(partition)
    if keys is None:
        raise ValueError(f"unsupported partition: {partition}")
    return ([sample for sample in samples if sample["plan_key"] in keys],
            [pair for pair in pairs if pair["plan_key"] in keys])


def label_lineage(training, teacher_path, method="mcts"):
    return {"teacher_sha256": file_sha256(teacher_path),
            "dataset_fingerprint": training["dataset_fingerprint"],
            "split_fingerprint": training["split_fingerprint"], "method": method}


def validate_labels(records, samples, training, teacher_path, dop_classes):
    expected = label_lineage(training, teacher_path)
    sample_by_plan = {sample["plan_key"]: sample for sample in samples}
    labels = {}
    for row in records:
        plan = row["plan_key"]
        if row.get("lineage") != expected:
            raise ValueError("missing, stale or mismatched MCTS label lineage")
        if plan not in training["train_plan_keys"] or plan not in sample_by_plan:
            raise ValueError("MCTS label is outside the training split")
        if plan in labels:
            raise ValueError("duplicate MCTS label")
        config = row["best_dop_config"]
        fragments = sample_by_plan[plan]["fragments"]
        if len(config) != len(fragments) or any(
            isinstance(dop, bool) or not isinstance(dop, int) or dop not in dop_classes
            or dop not in supported_dops(fragment, dop_classes)
            for dop, fragment in zip(config, fragments)
        ):
            raise ValueError("MCTS label has an invalid DOP configuration")
        labels[plan] = config
    if set(labels) != set(training["train_plan_keys"]):
        raise ValueError("MCTS labels must cover every training plan exactly once")
    return labels
