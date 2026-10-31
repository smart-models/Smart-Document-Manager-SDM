"""Search cost and promotion safety at the real runner boundary."""

from collections import Counter

import pytest

from ocrserver.autotune.profile import AutotuneConfig, load_profile
from ocrserver.autotune.runner import run


@pytest.mark.parametrize("objective", ["balanced", "throughput", "latency"])
def test_full_search_repeats_only_the_best_contender(tmp_path, objective):
    calls = Counter()

    def measure(candidate, items):
        key = (candidate["vl_lanes"], candidate["layout_threads"])
        calls[key] += 1
        ppm = 13.0 if key == (4, 2) else 10.0
        return {
            "pages_per_min": ppm,
            "cers": [0.0] * len(items),
            "outputs": [item.ground_truth for item in items],
            "durations_seconds": [60 / ppm] * len(items),
            "p95_seconds": 60 / ppm,
            "kv_cache_pct": 0.1,
            "metric_samples": 2,
            "had_preemption": False,
            "warmup_seconds": 1.0,
        }

    path = tmp_path / "profile.json"
    result = run(
        AutotuneConfig(mode="full", objective=objective, profile_path=str(path)),
        {"effective_cpu_count": 4},
        {},
        measure_fn=measure,
    )
    assert result["decisions"] == {
        "vl_lanes": 4,
        "layout_threads": 2,
        "page_parallelism": 1,
    }
    assert sum(calls.values()) == 10
    assert calls[(2, None)] == calls[(4, 2)] == 2
    assert len(calls) == 8  # Includes CPU-only tuning of baseline VL concurrency.
    assert load_profile(path) is not None
    assert all(m["repetitions"] >= 2 for m in result["measurements"] if m["accepted"])
    previous_calls = calls.copy()
    reused = run(
        AutotuneConfig(mode="full", objective=objective, profile_path=str(path)),
        {"effective_cpu_count": 4},
        {},
        measure_fn=measure,
    )
    assert reused["action"] == "reused"
    assert calls == previous_calls


def test_full_search_can_promote_only_a_layout_thread_change():
    calls = Counter()

    def measure(candidate, items):
        key = (candidate["vl_lanes"], candidate["layout_threads"])
        calls[key] += 1
        ppm = 14.0 if key == (2, 2) else 10.0
        return {
            "pages_per_min": ppm,
            "cers": [0.0] * len(items),
            "outputs": ["expected"] * len(items),
            "durations_seconds": [60 / ppm] * len(items),
            "p95_seconds": 60 / ppm,
            "kv_cache_pct": 0.1,
            "metric_samples": 2,
        }

    result = run(
        AutotuneConfig(mode="full"), {"effective_cpu_count": 4}, {}, measure_fn=measure
    )
    assert result["decisions"] == {
        "vl_lanes": 2,
        "page_parallelism": 1,
        "layout_threads": 2,
    }
    assert calls[(2, 2)] == calls[(2, None)] == 2


@pytest.mark.parametrize("failure", ["output", "memory", "unstable", "exception"])
def test_failed_finalist_tries_next_candidate(failure):
    calls = Counter()

    def measure(candidate, items):
        lanes = candidate["vl_lanes"]
        calls[lanes] += 1
        ppm = {2: 10, 4: 16, 8: 14, 16: 9}[lanes]
        result = {
            "pages_per_min": ppm,
            "cers": [0.0] * len(items),
            "outputs": ["expected"] * len(items),
            "durations_seconds": [60 / ppm] * len(items),
            "p95_seconds": 60 / ppm,
            "kv_cache_pct": 0.1,
            "metric_samples": 2,
            "had_preemption": False,
        }
        if lanes == 4 and calls[lanes] == 2:
            if failure == "exception":
                raise RuntimeError("finalist failed")
            if failure == "output":
                result["outputs"] = ["corrupted"] * len(items)
            if failure == "memory":
                result["kv_cache_pct"] = None
            if failure == "unstable":
                result["pages_per_min"] = 1
        return result

    result = run(AutotuneConfig(mode="full"), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == 8
    assert calls == {2: 2, 4: 2, 8: 2, 16: 1}
    assert not result["measurements"][1]["accepted"]


def test_full_budget_reserves_time_to_confirm(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr("ocrserver.autotune.runner.time.monotonic", lambda: clock[0])

    def measure(candidate, items):
        calls.append(candidate["vl_lanes"])
        clock[0] += 10
        return {
            "pages_per_min": candidate["vl_lanes"] * 10,
            "cers": [0.0] * len(items),
            "outputs": ["expected"] * len(items),
            "durations_seconds": [1.0] * len(items),
            "p95_seconds": 1.0,
            "kv_cache_pct": 0.1,
            "metric_samples": 2,
            "had_preemption": False,
        }

    result = run(
        AutotuneConfig(mode="full", max_seconds=41),
        {},
        {},
        measure_fn=measure,
    )
    assert calls == [2, 2, 4, 4]
    assert result["decisions"]["vl_lanes"] == 4


def test_replica_aggregation_uses_total_time_and_worst_quality():
    from ocrserver.autotune.runner import _measure_candidate

    replicas = iter(
        [
            {
                "pages_per_min": 10,
                "cers": [0.0],
                "outputs": ["same"],
                "durations_seconds": [6.0],
                "p95_seconds": 6.0,
                "kv_cache_pct": 0.1,
                "metric_samples": 2,
            },
            {
                "pages_per_min": 20,
                "cers": [0.1],
                "outputs": ["same"],
                "durations_seconds": [3.0],
                "p95_seconds": 3.0,
                "kv_cache_pct": 0.1,
                "metric_samples": 2,
            },
        ]
    )
    result = _measure_candidate(lambda *_: next(replicas), {}, [object()], 2)
    assert result["pages_per_min"] == pytest.approx(2 * 60 / 9)
    assert result["cers"] == [0.1]
    assert result["p95_seconds"] == 6.0
