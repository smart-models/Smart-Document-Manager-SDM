"""Autotune quality, stability, deadline, and memory gates."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    """Report gate acceptance and a public reason when a measurement fails."""

    passed: bool
    reason: str = ""


def finite_number(value: object, *, minimum: float = 0.0) -> bool:
    """Accept finite int/float measurements above a floor, excluding booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= minimum
    )


def cer(reference: str, candidate: str) -> float:
    """Character error rate (Levenshtein / max(len_ref, 1))."""
    if not reference:
        return 1.0 if candidate else 0.0
    # Clamp to [0, 1]: CER is an error *rate*; insertion-heavy mismatches can
    # exceed 1 otherwise.
    return min(levenshtein(reference, candidate) / len(reference), 1.0)


def levenshtein(pattern: str, text: str) -> int:
    """Exact edit distance, Myers/Hyyrö bit-parallel on Python ints.

    O(len(text)) big-int operations instead of an O(m*n) Python DP: a
    multi-page calibration PDF took ~16 s per comparison with the DP.
    """
    m = len(pattern)
    if m == 0:
        return len(text)
    peq: dict[str, int] = {}
    for index, char in enumerate(pattern):
        peq[char] = peq.get(char, 0) | (1 << index)
    mask = (1 << m) - 1
    last = 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for char in text:
        eq = peq.get(char, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | ~(xh | pv)
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


def quality_gate_absolute(
    candidate_cers: list[float], threshold: float = 0.60
) -> GateResult:
    """Catastrophic-output guard.

    CER here compares full-page markdown to per-item ground truth, so even good
    output is not near zero (formatting differs). The gate only rejects a
    candidate whose mean CER shows the output is essentially broken/empty.
    """
    if not candidate_cers:
        return GateResult(False, "no CER samples")
    if any(not finite_number(value) or value > 1 for value in candidate_cers):
        return GateResult(False, "CER samples must be finite and within [0, 1]")
    mean_cer = sum(candidate_cers) / len(candidate_cers)
    if mean_cer > threshold:
        return GateResult(
            False, f"mean CER {mean_cer:.4f} exceeds catastrophic {threshold:.4f}"
        )
    return GateResult(True)


def quality_gate_relative(
    candidate_cers: list[float],
    baseline_cers: list[float],
    max_worsening: float = 0.05,
) -> GateResult:
    """Reject invalid CER pairs or per-item worsening beyond the tolerance."""
    if not baseline_cers:
        return GateResult(False, "no baseline CER samples")
    if len(candidate_cers) != len(baseline_cers):
        return GateResult(False, "CER sample count differs from baseline")
    if any(
        not finite_number(value) or value > 1
        for value in candidate_cers + baseline_cers
    ):
        return GateResult(False, "CER samples must be finite and within [0, 1]")
    for i, (c, b) in enumerate(zip(candidate_cers, baseline_cers)):
        if c - b > max_worsening + 1e-9:
            return GateResult(
                False,
                f"relative CER worsened by {c - b:.4f} on item {i}, "
                f"max {max_worsening:.4f}",
            )
    return GateResult(True)


def output_equivalence_gate(
    candidate_outputs: list[str],
    baseline_outputs: list[str],
    max_cer: float = 0.002,
) -> GateResult:
    """Reject tuning that changes deterministic OCR content."""
    if not baseline_outputs:
        return GateResult(False, "no baseline output samples")
    if len(candidate_outputs) != len(baseline_outputs):
        return GateResult(False, "output sample count differs from baseline")
    if any(
        not isinstance(value, str) for value in candidate_outputs + baseline_outputs
    ):
        return GateResult(False, "output samples must be strings")
    differences = [
        cer(reference, candidate)
        for reference, candidate in zip(baseline_outputs, candidate_outputs)
    ]
    worst = max(differences, default=0.0)
    if worst > max_cer:
        return GateResult(
            False,
            f"output CER versus baseline {worst:.4f} exceeds {max_cer:.4f}",
        )
    return GateResult(True)


def stability_gate(samples: list[float], max_variance_pct: float = 0.20) -> GateResult:
    """Bound the sample coefficient of variation for positive throughput data."""
    if len(samples) < 2:
        return GateResult(False, "at least two repetitions are required")
    if any(not finite_number(value) or value <= 0 for value in samples):
        return GateResult(False, "throughput samples must be finite and positive")
    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples)
    cv = stdev / mean
    if cv > max_variance_pct:
        return GateResult(
            False, f"variance {cv:.2%} exceeds {max_variance_pct:.0%} threshold"
        )
    return GateResult(True)


def deadline_gate(elapsed: float, budget: float) -> GateResult:
    """Require valid measurements and elapsed time strictly below the budget."""
    if not finite_number(elapsed) or not finite_number(budget) or budget <= 0:
        return GateResult(False, "invalid deadline measurement")
    if elapsed >= budget:
        return GateResult(False, f"deadline exceeded: {elapsed:.1f}s >= {budget:.1f}s")
    return GateResult(True)


def memory_gate(
    kv_cache_pct: float | None, had_preemption: bool = False, kv_limit: float = 0.95
) -> GateResult:
    """Reject only near-saturation of the KV cache.

    Preemption alone is not a failure: with vl_lanes now bounded it is rare, and
    when it happens it already shows up as lower throughput, which the objective
    score penalizes. Hard-failing on any preemption event rejected every
    candidate.
    """
    if not finite_number(kv_cache_pct) or kv_cache_pct > 1:
        return GateResult(False, "KV-cache usage was not measured reliably")
    if kv_cache_pct >= kv_limit:
        return GateResult(
            False, f"KV-cache usage {kv_cache_pct:.0%} >= {kv_limit:.0%} threshold"
        )
    return GateResult(True)
