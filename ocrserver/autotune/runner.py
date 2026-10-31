"""Stage-2 measured autotune sweep (pre-start, --dry-run supported).

Model: measure the safe-default configuration first (baseline). A valid
baseline already yields a calibrated profile — the sweep only promotes a
higher-throughput candidate that does not regress quality. Fallback happens
only when the baseline itself cannot be measured (vLLM down / broken output).

Only knobs with a real runtime effect are swept: vl_lanes (paddleocr's
vl_rec_max_concurrency, i.e. concurrent block-level VL requests to vLLM) and
layout_threads (paddle CPU threads). page_parallelism is fixed at 1 — the
server processes pages serially and the paddle layout predictor is not
thread-safe, so it has no effect.
"""

from __future__ import annotations

import hashlib
import logging
import math
import statistics
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from ocrserver.autotune.fingerprint import fingerprint_digest
from ocrserver.autotune.gates import (
    cer,
    deadline_gate,
    finite_number,
    memory_gate,
    output_equivalence_gate,
    quality_gate_absolute,
    quality_gate_relative,
    stability_gate,
)
from ocrserver.autotune.profile import (
    AutotuneConfig,
    load_profile,
    make_profile,
    profile_matches,
    save_profile,
)

logger = logging.getLogger(__name__)

SAFE_DEFAULTS = {"vl_lanes": 2, "page_parallelism": 1, "layout_threads": None}

QUICK_LANES = [4, 8]
FULL_LANES = [2, 4, 8, 16]
PROMOTION_MARGIN = 0.05


class MeasurementIsolationError(RuntimeError):
    """Native work outlived a measurement; no subsequent run is isolated."""


def _layout_thread_candidates(cpu_count: int) -> list[int | None]:
    # None = paddle default. Add a half-core variant to probe CPU-bound layout.
    if cpu_count <= 1:
        return [None]
    return [None, max(1, cpu_count // 2)]


def _search_space(fingerprint: dict, mode: str) -> dict:
    cpu = fingerprint.get("effective_cpu_count", fingerprint.get("cpu_count", 0))
    lanes = FULL_LANES if mode == "full" else QUICK_LANES
    # Each measurement runs the full calibration set (tens of seconds), so quick
    # mode only sweeps vl_lanes; layout_threads is explored in full mode.
    layout = _layout_thread_candidates(cpu) if mode == "full" else [None]
    return {
        "vl_lanes": lanes,
        "page_parallelism": [1],
        "layout_threads": layout,
    }


def _sweep_candidates(space: dict) -> list[dict]:
    candidates: list[dict] = []
    for lanes in space["vl_lanes"]:
        for lt in space["layout_threads"]:
            candidates.append(
                {"vl_lanes": lanes, "page_parallelism": 1, "layout_threads": lt}
            )
    return candidates


def _score_candidate(measurement: dict, objective: str) -> float:
    if objective == "throughput":
        score = measurement["pages_per_min"]
    elif objective == "latency":
        score = -measurement["p95_seconds"]
    else:
        score = measurement["pages_per_min"] / max(measurement["p95_seconds"], 0.001)
    if not math.isfinite(score):
        raise ValueError("candidate score must be finite")
    return score


def _validate_replica(result: object, item_count: int) -> dict:
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("measurement failed or returned an invalid object")
    if item_count <= 0:
        raise ValueError("calibration workload is empty")
    for key in ("outputs", "cers", "durations_seconds"):
        values = result.get(key)
        if not isinstance(values, list) or len(values) != item_count:
            raise ValueError(f"{key} does not cover the complete calibration workload")
    if any(not isinstance(value, str) for value in result["outputs"]):
        raise ValueError("outputs must contain strings")
    if any(not finite_number(value) or value > 1 for value in result["cers"]):
        raise ValueError("CER samples must be finite and within [0, 1]")
    for value in [result.get("pages_per_min"), *result["durations_seconds"]]:
        if not finite_number(value) or value <= 0:
            raise ValueError("throughput and durations must be finite and positive")
    if not finite_number(result.get("warmup_seconds", 0)):
        raise ValueError("warmup_seconds must be finite and nonnegative")
    p95 = result.get("p95_seconds")
    result = dict(result)
    if not finite_number(p95) or p95 <= 0:
        durations = sorted(result["durations_seconds"])
        result["p95_seconds"] = durations[math.ceil(0.95 * len(durations)) - 1]
    return result


def _measure_candidate(
    measure_fn: Callable,
    candidate: dict,
    items: list,
    samples: int,
    *,
    deadline: float | None = None,
    initial_results: list[dict] | None = None,
) -> dict:
    """Complete bounded replicas and aggregate conservative candidate evidence.

    Reuse supplied initial results and check the deadline before each remaining
    replica. Combine throughput harmonically, retain worst per-item CER and
    latency, and trust memory usage only when every replica has valid samples.
    """
    started = time.monotonic()
    results = list(initial_results or [])
    for _ in range(samples - len(results)):
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("time budget exhausted before completing repetitions")
        results.append(_validate_replica(measure_fn(candidate, items), len(items)))
    ppms = [r["pages_per_min"] for r in results]
    agg = dict(results[-1])
    # Every replica covers the same pages: aggregate pages / aggregate time.
    agg["pages_per_min"] = statistics.harmonic_mean(ppms)
    all_durations = [
        float(duration)
        for result in results
        for duration in result.get("durations_seconds", [])
    ]
    agg["p95_seconds"] = max(result["p95_seconds"] for result in results)
    agg["durations_seconds"] = all_durations
    agg["cers"] = [
        max(result["cers"][index] for result in results) for index in range(len(items))
    ]
    baseline_outputs = results[0].get("outputs", [])
    agg["output_variation_cer"] = max(
        (
            cer(reference, candidate_output)
            for result in results[1:]
            for reference, candidate_output in zip(
                baseline_outputs, result.get("outputs", [])
            )
        ),
        default=0.0,
    )
    agg["outputs"] = baseline_outputs
    memory_verified = all(
        finite_number(result.get("kv_cache_pct"))
        and result["kv_cache_pct"] <= 1
        and type(result.get("metric_samples")) is int
        and result["metric_samples"] > 0
        for result in results
    )
    agg["kv_cache_pct"] = (
        max(result["kv_cache_pct"] for result in results) if memory_verified else None
    )
    agg["had_preemption"] = any(r.get("had_preemption", False) for r in results)
    agg["metric_samples"] = (
        sum(result["metric_samples"] for result in results) if memory_verified else 0
    )
    agg["warmup_seconds"] = statistics.mean(
        float(r.get("warmup_seconds", 0.0)) for r in results
    )
    agg["candidate"] = candidate
    agg["_ppm_samples"] = ppms
    agg["_replicas"] = results
    agg["repetitions"] = len(results)
    agg["measurement_seconds"] = time.monotonic() - started
    return agg


def _candidate_rejections(
    measurement: dict,
    baseline: dict,
    elapsed: float,
    budget: float,
    *,
    confirmed: bool,
) -> list[str]:
    gates = [
        quality_gate_absolute(measurement["cers"]),
        quality_gate_relative(
            measurement["cers"], baseline["cers"], max_worsening=0.01
        ),
        output_equivalence_gate(measurement["outputs"], baseline["outputs"]),
        memory_gate(measurement["kv_cache_pct"], measurement["had_preemption"]),
        deadline_gate(elapsed, budget),
    ]
    if confirmed:
        gates.append(stability_gate(measurement["_ppm_samples"]))
    reasons = [gate.reason for gate in gates if not gate.passed]
    if measurement["output_variation_cer"] > 0.002:
        reasons.append("candidate output changed across repetitions")
    return reasons


def _measurement_record(measurement: dict) -> dict:
    """Strip raw OCR text while retaining auditable output identities."""
    record = {
        key: value
        for key, value in measurement.items()
        if key != "outputs" and not key.startswith("_")
    }
    record["output_sha256"] = [
        hashlib.sha256(output.encode("utf-8")).hexdigest()
        for output in measurement.get("outputs", [])
    ]
    return record


def run(
    config: AutotuneConfig,
    fingerprint: dict,
    env: Mapping[str, str],
    *,
    dry_run: bool = False,
    measure_fn: Callable | None = None,
) -> dict:
    """Reuse a compatible profile or calibrate bounded candidates through gates.

    Args:
        config: Calibration mode, objective, deadline and persistence policy.
        fingerprint: Current hardware, runtime and source identity.
        env: Explicit settings that constrain the candidate search space.
        dry_run: Return the proposed search space without running calibration.
        measure_fn: Optional measurement callback for candidate evaluation.

    Returns:
        An action report describing disabled, reused, calibrated or fallback
        behavior, or a dry-run search-space report.
    """
    if config.mode == "off":
        return {"action": "disabled", "reason": "autotune is off"}

    space = _search_space(fingerprint, config.mode)
    from ocrserver.config import load_settings

    settings = load_settings(env, None)
    baseline_candidate = dict(SAFE_DEFAULTS)
    for field, key in (
        ("vl_lanes", "OCR_VL_LANES"),
        ("layout_threads", "OCR_LAYOUT_THREADS"),
    ):
        if env.get(key, "").strip():
            baseline_candidate[field] = getattr(settings, field)
            space[field] = [getattr(settings, field)]

    if dry_run:
        return {
            "action": "dry-run",
            "search_space": space,
            "fingerprint_sha256": fingerprint_digest(fingerprint),
        }

    if not config.force and config.profile_path:
        path = Path(config.profile_path)
        existing = load_profile(path)
        if existing and profile_matches(
            existing, fingerprint, config.mode, config.objective
        ):
            return {
                "action": "reused",
                "decisions": existing["decisions"],
                "profile_path": str(path),
            }

    if measure_fn is None:
        return {
            "action": "fallback",
            "reason": "no measure_fn provided (real measurement requires live vLLM)",
            "decisions": dict(SAFE_DEFAULTS),
        }

    from calibration.generate import load_ground_truth

    cal_dir = Path(__file__).resolve().parent.parent.parent / "calibration"
    gt_path = cal_dir / "ground_truth.json"
    calibration_items = load_ground_truth(gt_path)

    samples = 2
    start = time.monotonic()
    deadline = start + config.max_seconds

    # 1. Baseline: the safe default must be measurable and not catastrophic.
    try:
        baseline = _measure_candidate(
            measure_fn,
            dict(baseline_candidate),
            calibration_items,
            samples,
            deadline=deadline,
        )
        best_score = _score_candidate(baseline, config.objective)
    except Exception as exc:  # noqa: BLE001 - a failed backend measurement rejects tuning
        logger.warning("Baseline measurement rejected (%s)", type(exc).__name__)
        return {
            "action": "fallback",
            "reason": "baseline measurement is incomplete or invalid",
            "decisions": dict(SAFE_DEFAULTS),
        }
    baseline_gates = (
        quality_gate_absolute(baseline["cers"]),
        stability_gate(baseline["_ppm_samples"]),
        memory_gate(baseline["kv_cache_pct"], baseline.get("had_preemption", False)),
        deadline_gate(time.monotonic() - start, config.max_seconds),
    )
    reasons = [gate.reason for gate in baseline_gates if not gate.passed]
    if reasons:
        return {
            "action": "fallback",
            "reason": "baseline failed gates: " + "; ".join(reasons),
            "decisions": dict(SAFE_DEFAULTS),
            "measurements": [_measurement_record(baseline)],
        }

    if baseline.get("output_variation_cer", 0.0) > 0.002:
        return {
            "action": "fallback",
            "reason": "baseline output was not deterministic across repetitions",
            "decisions": dict(SAFE_DEFAULTS),
            "measurements": [_measurement_record(baseline)],
        }
    best = baseline
    baseline["score"] = best_score
    baseline["accepted"] = True
    baseline["promoted"] = True
    baseline["phase"] = "confirmed"
    measurements: list[dict] = [baseline]

    # 2. Sweep: promote a candidate only if it beats the baseline score without
    #    regressing quality/stability/memory, and within the time budget.
    for candidate in _sweep_candidates(space):
        if candidate == baseline_candidate:
            continue
        elapsed = time.monotonic() - start
        if not deadline_gate(elapsed, config.max_seconds).passed:
            logger.warning("autotune deadline reached after %.1fs", elapsed)
            break

        if config.mode == "full":
            # Reserve a second pass before spending the remaining budget on
            # another screening pass. This is admission control, not a hard
            # timeout for a native predictor already running.
            pass_estimate = max(
                m.get("measurement_seconds", 0) / m.get("repetitions", 1)
                for m in measurements
            )
            if config.max_seconds - elapsed < 2 * pass_estimate:
                logger.info("Reserving remaining autotune budget for confirmation")
                break

        try:
            logger.info("Measuring autotune candidate %s", candidate)
            m = _measure_candidate(
                measure_fn,
                candidate,
                calibration_items,
                1 if config.mode == "full" else samples,
                deadline=deadline,
            )
            score = _score_candidate(m, config.objective)
        except MeasurementIsolationError:
            return {
                "action": "fallback",
                "reason": "native work remains after measurement cleanup",
                "decisions": dict(baseline_candidate),
            }
        except Exception as exc:  # noqa: BLE001 - retain the verified baseline on failure
            logger.warning("Candidate measurement rejected (%s)", type(exc).__name__)
            measurements.append(
                {
                    "candidate": candidate,
                    "accepted": False,
                    "promoted": False,
                    "rejection_reasons": ["measurement is incomplete or invalid"],
                }
            )
            continue

        confirmed = config.mode != "full"
        reasons = _candidate_rejections(
            m,
            baseline,
            time.monotonic() - start,
            config.max_seconds,
            confirmed=confirmed,
        )
        m["accepted"] = confirmed and not reasons
        m["phase"] = "confirmed" if confirmed else "screened"
        m["rejection_reasons"] = reasons
        m["score"] = score
        m["promoted"] = False
        measurements.append(m)
        if reasons:
            logger.info("candidate %s rejected: %s", candidate, "; ".join(reasons))
        elif confirmed and score > best_score + abs(best_score) * PROMOTION_MARGIN:
            best["promoted"] = False
            best = m
            best_score = score
            m["promoted"] = True

    # Full mode allocates second passes in score order. Screening always uses
    # the entire corpus, but a single pass can never become a runtime profile.
    # This is a two-stage race, not a statistical confidence guarantee.
    if config.mode == "full":
        contenders = sorted(
            (
                m
                for m in measurements[1:]
                if m.get("_replicas") and not m["rejection_reasons"]
            ),
            key=lambda m: m["score"],
            reverse=True,
        )
        for m in contenders:
            if m["score"] <= best_score + abs(best_score) * PROMOTION_MARGIN:
                m["rejection_reasons"] = ["screening score below promotion margin"]
                continue
            try:
                logger.info("Confirming autotune candidate %s", m["candidate"])
                confirmed = _measure_candidate(
                    measure_fn,
                    m["candidate"],
                    calibration_items,
                    samples,
                    deadline=deadline,
                    initial_results=m["_replicas"],
                )
                score = _score_candidate(confirmed, config.objective)
            except MeasurementIsolationError:
                return {
                    "action": "fallback",
                    "reason": "native work remains after confirmation cleanup",
                    "decisions": dict(baseline_candidate),
                }
            except Exception as exc:  # noqa: BLE001 - retain verified incumbent
                logger.warning("Confirmation rejected (%s)", type(exc).__name__)
                m["rejection_reasons"] = ["confirmation is incomplete or invalid"]
                continue
            confirmed["measurement_seconds"] += m["measurement_seconds"]
            m.update(confirmed)
            m["phase"] = "confirmed"
            m["score"] = score
            reasons = _candidate_rejections(
                m,
                baseline,
                time.monotonic() - start,
                config.max_seconds,
                confirmed=True,
            )
            m["accepted"] = not reasons
            m["rejection_reasons"] = reasons
            if not reasons and score > best_score + abs(best_score) * PROMOTION_MARGIN:
                best["promoted"] = False
                best, best_score = m, score
                m["promoted"] = True

    decisions = {
        "vl_lanes": best["candidate"]["vl_lanes"],
        "page_parallelism": best["candidate"]["page_parallelism"],
        "layout_threads": best["candidate"]["layout_threads"],
    }

    recorded_measurements = [_measurement_record(item) for item in measurements]
    profile = make_profile(
        fingerprint=fingerprint,
        decisions=decisions,
        measurements=recorded_measurements,
        warmup_seconds=best.get("warmup_seconds", 0.0),
        mode=config.mode,
        objective=config.objective,
    )
    if config.profile_path:
        save_profile(Path(config.profile_path), profile)

    return {
        "action": "calibrated",
        "decisions": decisions,
        "measurements": recorded_measurements,
        "warmup_seconds": best.get("warmup_seconds", 0.0),
        "profile_path": config.profile_path,
        "elapsed_seconds": time.monotonic() - start,
    }
