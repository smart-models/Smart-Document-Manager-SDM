"""PRD comparison gates distinguish absent measurements from failed ones."""

from math import isfinite

import numpy as np

from .metrics import paired_bootstrap


INVARIANTS = (
    "unauthorized_overflows",
    "section_crossings",
    "protected_splits",
    "undisclosed_losses",
    "incorrect_token_counts",
    "nondeterministic_results",
    "embedding_truncations",
)


def _latencies(values):
    return (
        isinstance(values, list)
        and bool(values)
        and all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and isfinite(value)
            and value > 0
            for value in values
        )
    )


def _ratio(baseline, candidate, limit):
    if not _latencies(baseline) or not _latencies(candidate):
        return {"passed": None, "reason": "missing_latency_samples"}
    before = float(np.quantile(baseline, 0.95))
    after = float(np.quantile(candidate, 0.95))
    return {
        "passed": after <= limit * before,
        "ratio": after / before,
        "baseline_p95_ms": before,
        "candidate_p95_ms": after,
        "maximum_ratio": limit,
    }


def qualification_gates(
    scores, protocol, selected_baseline, runtime, expected_documents
) -> dict:
    """Use preregistered roles; never choose a stronger baseline on heldout."""
    candidate, greedy = protocol["candidate"], protocol["greedy"]
    gates = {}
    for name, metric, comparator in (
        ("recall_gain", "evidence_recall", greedy),
        ("ndcg_noninferiority", "ndcg@10", selected_baseline),
    ):
        current = scores.get(("packing", candidate, metric))
        baseline = scores.get(("packing", comparator, metric))
        if not current or not baseline or current.keys() != baseline.keys():
            gates[name] = {"passed": None, "reason": "missing_paired_scores"}
            continue
        comparison = paired_bootstrap(
            current,
            baseline,
            seed=protocol["seed"],
            samples=protocol["bootstrap_samples"],
        )
        passed = (
            comparison["difference"] >= 0.02 and comparison["ci95"][0] > 0
            if name == "recall_gain"
            else comparison["ci95"][0] > -0.01
        )
        gates[name] = {"passed": passed, **comparison}
    current = scores.get(("packing", candidate, "success_rate"))
    baseline = scores.get(("packing", greedy, "success_rate"))
    if current and baseline and current.keys() == baseline.keys():
        before = float(np.mean(list(baseline.values())))
        after = float(np.mean(list(current.values())))
        gates["success_nonregression"] = {
            "passed": after >= before,
            "candidate": after,
            "greedy": before,
        }
    else:
        gates["success_nonregression"] = {"passed": None}
    runtime = runtime or {}
    chunking = runtime.get("chunking", {})
    first, second = chunking.get(greedy, {}), chunking.get(candidate, {})
    if first.get("hardware_id") and first.get("hardware_id") == second.get(
        "hardware_id"
    ):
        gates["chunking_latency"] = _ratio(
            first.get("latencies_ms"),
            second.get("latencies_ms"),
            3.0,
        )
    else:
        gates["chunking_latency"] = {
            "passed": None,
            "reason": "missing_common_hardware",
        }
    ocr = runtime.get("ocr", {})
    gates["ocr_latency"] = (
        _ratio(
            ocr.get("baseline_latencies_ms"),
            ocr.get("concurrent_latencies_ms"),
            1.1,
        )
        if ocr.get("hardware_id")
        else {"passed": None, "reason": "missing_ocr_hardware"}
    )
    invariants = runtime.get("invariants", {})
    measurements = [invariants.get(name) for name in INVARIANTS]
    complete = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in [
            *measurements,
            invariants.get("checked_documents"),
            invariants.get("checked_chunks"),
        ]
    )
    if complete:
        gates["invariants"] = {
            "passed": all(value == 0 for value in measurements)
            and invariants["checked_documents"] >= expected_documents
            and invariants["checked_chunks"] > 0,
            "counts": invariants,
        }
    else:
        gates["invariants"] = {
            "passed": None,
            "reason": "missing_invariant_measurements",
        }
    return gates
