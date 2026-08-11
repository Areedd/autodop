"""Load correction for AutoDOP online decisions (DD03 §4.2).

Pure, stateless functions: the FE resource view performs EWMA smoothing and sends the
smoothed per-worker load in the request (``worker_loads``); this module applies the
dead-zone check and the bounded linear scaling on top of the policy output.

Rules implemented (DD03 §4.2):

- factor = 1 while the load stays below the dead-zone thresholds (CPU 0.4 / memory 0.65);
- beyond the threshold the factor drops linearly and bottoms out at the floor (0.1) when
  the dimension is fully loaded;
- per worker the factor is the smaller of the CPU and memory dimensions;
- per fragment the factor is the smallest factor across all target workers (weakest node);
- the corrected DOP is the closest candidate at or below the scaled value, never below 1,
  and never above the base DOP (both consumers only shrink parallelism).
"""


def _dimension_factor(value, threshold, floor):
    if value <= threshold:
        return 1.0
    return max(floor, 1.0 - (value - threshold) / (1.0 - threshold) * (1.0 - floor))


def worker_factor(load, cpu_threshold=0.4, mem_threshold=0.65, floor=0.1):
    """Scaling factor for one worker.

    ``load`` is the request's ``worker_loads`` entry (``{cpu_used, mem_used_pct, fresh}``)
    or None. Missing or stale load data yields factor 1 (no correction).
    """
    if not load or not load.get("fresh", False):
        return 1.0
    cpu = float(load.get("cpu_used", 0.0))
    mem = float(load.get("mem_used_pct", 0.0))
    cpu_factor = _dimension_factor(cpu, cpu_threshold, floor)
    mem_factor = _dimension_factor(mem, mem_threshold, floor)
    return min(cpu_factor, mem_factor)


def fragment_factor(fragment, worker_loads, cpu_threshold=0.4, mem_threshold=0.65, floor=0.1):
    """Scaling factor for a fragment: the minimum factor across its target workers.

    Workers without load data count as factor 1 (no correction from them).
    """
    workers = [str(worker) for worker in fragment.get("worker_ids", [])]
    if not workers:
        return 1.0
    factors = [
        worker_factor(worker_loads.get(worker), cpu_threshold, mem_threshold, floor)
        for worker in workers
    ]
    return min(factors)


def corrected_dop(base_dop, factor, candidate_dops):
    """Corrected DOP: ``base_dop * factor`` rounded to the closest candidate.

    The result is at most ``base_dop`` (correction never raises parallelism), belongs to
    ``candidate_dops``, and is never below 1.
    """
    base_dop = int(base_dop)
    if factor >= 1.0:
        return base_dop
    target = int(round(base_dop * factor))
    candidates = sorted(int(dop) for dop in candidate_dops)
    chosen = None
    for dop in candidates:
        if dop <= target:
            chosen = dop
        else:
            break
    if chosen is None:
        chosen = max(1, min(candidates)) if candidates else 1
    return chosen


def apply_to_decision(decision, fragment, worker_loads,
                      cpu_threshold=0.4, mem_threshold=0.65, floor=0.1):
    """Apply load correction to a single engine decision.

    Returns a new decision dict whose DOP is load-corrected; when the DOP actually changed
    the reason is suffixed with ``:load_corrected`` so traces expose which fragments were
    adjusted. When no correction applies the original decision is returned unchanged.
    """
    factor = fragment_factor(fragment, worker_loads, cpu_threshold, mem_threshold, floor)
    dop = corrected_dop(decision["dop"], factor, fragment.get("candidate_dops", []))
    if dop != int(decision["dop"]):
        return {
            **decision,
            "dop": dop,
            "reason": decision.get("reason", "policy") + ":load_corrected",
        }
    return decision
