"""Verify autotune validation, runtime isolation and failure handling."""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import math
import os
import shutil
import threading
import weakref
from pathlib import Path

import httpx
import pytest

import autotune_entrypoint
from ocrserver.autotune.fingerprint import (
    _memory_limit_gb,
    _read_runtime_manifest,
    _source_digest,
)
from ocrserver.autotune.gates import (
    memory_gate,
    quality_gate_absolute,
    quality_gate_relative,
    stability_gate,
)
from ocrserver.autotune.profile import AutotuneConfig, load_profile, save_profile
from ocrserver.autotune.runner import run
from ocrserver.autotune.tiers import (
    GpuInfo,
    build_vllm_command,
    dynamic_gpu_mem_util,
    select_tier,
)


def measurement(items, *, latency=10.0, ppm=10.0):
    return {
        "pages_per_min": ppm,
        "cers": [0.1] * len(items),
        "outputs": ["stable output"] * len(items),
        "durations_seconds": [latency] * len(items),
        "p95_seconds": latency,
        "kv_cache_pct": 0.2,
        "metric_samples": 2,
        "had_preemption": False,
        "warmup_seconds": 0.0,
    }


@pytest.fixture
def stage1_module():
    path = Path(__file__).resolve().parents[1] / "vllm" / "stage1_entrypoint.py"
    spec = importlib.util.spec_from_file_location("audit_stage1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_manifest_is_readable_by_the_api_user(
    tmp_path,
    monkeypatch,
    stage1_module,
):
    destination = tmp_path / "hardware.json"
    monkeypatch.setenv("OCR_VLLM_HARDWARE_FILE", str(destination))
    changes = []
    chmod = os.chmod
    replace = os.replace

    def checked_chmod(path, mode):
        changes.append((Path(path), mode))
        chmod(path, mode)

    def checked_replace(source, target):
        assert changes == [(Path(source), 0o644)]
        replace(source, target)

    monkeypatch.setattr(stage1_module.os, "chmod", checked_chmod)
    monkeypatch.setattr(stage1_module.os, "replace", checked_replace)
    stage1_module._write_runtime_manifest(None, {}, ["vllm", "serve", "model"])
    assert json.loads(destination.read_text(encoding="utf-8"))["schema_version"] == 1
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("port", ["0", "-1", "65536", "invalid"])
def test_stage1_port_is_validated_before_launch(port, monkeypatch, stage1_module):
    monkeypatch.setattr(stage1_module, "detect_gpu", lambda: None)
    with pytest.raises(ValueError, match="OCR_VLLM_PORT"):
        stage1_module.select_and_build({"OCR_VLLM_PORT": port})


@pytest.mark.parametrize("fail", [False, True])
def test_measure_releases_unreachable_predictor_cycles(monkeypatch, fail):
    references = []
    drained = []

    class CyclicPredictor:
        def __init__(self):
            # PaddleX BasePredictor stores this same bound-method cycle.
            self.predict = self.__call__

        def __call__(self):
            return None

    async def fake_measure(*_args):
        predictor = CyclicPredictor()
        references.append(weakref.ref(predictor))
        try:
            await asyncio.sleep(0)
            if fail:
                raise ValueError("measurement failed")
            return {"measurement": "ok"}
        finally:
            drained.append(True)

    monkeypatch.setattr(autotune_entrypoint, "_async_measure", fake_measure)
    measure = autotune_entrypoint._make_measure_fn("http://test/v1", "model")
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        if fail:
            with pytest.raises(ValueError, match="measurement failed"):
                measure({}, [])
        else:
            assert measure({}, []) == {"measurement": "ok"}
        assert drained == [True]
        assert references[0]() is None
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()


def test_measure_gc_keeps_predictor_owned_by_live_worker(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    references = []
    threads = []

    class CyclicPredictor:
        def __init__(self):
            self.predict = self.__call__

        def __call__(self):
            entered.set()
            release.wait(2)

    async def fake_measure(*_args):
        predictor = CyclicPredictor()
        references.append(weakref.ref(predictor))
        thread = threading.Thread(target=predictor.predict, daemon=True)
        threads.append(thread)
        thread.start()
        assert entered.wait(1)
        raise TimeoutError("native worker still running")

    monkeypatch.setattr(autotune_entrypoint, "_async_measure", fake_measure)
    measure = autotune_entrypoint._make_measure_fn("http://test/v1", "model")
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(TimeoutError, match="native worker still running"):
            measure({}, [])
        assert threads[0].is_alive()
        assert references[0]() is not None
        release.set()
        threads[0].join(timeout=1)
        assert not threads[0].is_alive()
        gc.collect()
        assert references[0]() is None
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        if gc_was_enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize(
    "values,winner",
    [
        ([10.0, 10.4, 10.8], 2),
        ([10.0, 9.4, 10.0], 4),
        ([10.0, 9.6, 10.0], 2),
        ([10.0, 9.5, 10.0], 2),
    ],
)
def test_latency_promotion_requires_actual_margin(values, winner):
    calls = []

    def measure(candidate, items):
        calls.append(candidate["vl_lanes"])
        return measurement(
            items, latency=dict(zip([2, 4, 8], values))[candidate["vl_lanes"]]
        )

    result = run(AutotuneConfig(objective="latency"), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == winner
    assert calls == [2, 2, 4, 4, 8, 8]


@pytest.mark.parametrize(
    "change",
    [
        {"cers": []},
        {"outputs": []},
        {"outputs": [None] * 5},
        {"pages_per_min": math.nan},
        {"pages_per_min": math.inf},
        {"durations_seconds": [math.nan] * 5},
        {"durations_seconds": []},
        {"cers": [math.inf] * 5},
    ],
)
def test_incomplete_or_nonfinite_baseline_falls_back(change):
    def measure(_candidate, items):
        return measurement(items) | change

    result = run(AutotuneConfig(), {}, {}, measure_fn=measure)
    assert result["action"] == "fallback"


def test_unknown_memory_rejects_candidate():
    def measure(candidate, items):
        result = measurement(items, ppm=10 * candidate["vl_lanes"])
        if candidate["vl_lanes"] != 2:
            result.update(kv_cache_pct=None, metric_samples=0)
        return result

    result = run(AutotuneConfig(objective="throughput"), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == 2
    assert all(not m["accepted"] for m in result["measurements"][1:])


def test_quality_and_stability_require_complete_finite_samples():
    assert not quality_gate_relative([], [0.1]).passed
    assert not quality_gate_relative([0.1], [0.1, 0.1]).passed
    for value in [math.nan, math.inf, -0.1]:
        assert not quality_gate_absolute([value]).passed
    assert not stability_gate([10.0]).passed
    assert not stability_gate([0.0, 0.0]).passed
    assert not memory_gate(None).passed


@pytest.mark.parametrize(
    "env,key",
    [
        ({"OCR_AUTOTUNE": "fast"}, "OCR_AUTOTUNE"),
        ({"OCR_TUNING_OBJECTIVE": "wrong"}, "OCR_TUNING_OBJECTIVE"),
        ({"OCR_AUTOTUNE_FORCE": "typo"}, "OCR_AUTOTUNE_FORCE"),
        ({"OCR_AUTOTUNE_MAX_SECONDS": "0"}, "OCR_AUTOTUNE_MAX_SECONDS"),
    ],
)
def test_autotune_configuration_rejects_invalid_operator_values(env, key):
    with pytest.raises(ValueError, match=key):
        AutotuneConfig.from_env(env)


@pytest.mark.parametrize(
    "key,value",
    [
        ("OCR_VLLM_GPU_MEM_UTIL", "nan"),
        ("OCR_VLLM_GPU_MEM_UTIL", "1.1"),
        ("OCR_VLLM_MAX_NUM_SEQS", "0"),
        ("OCR_VLLM_ENFORCE_EAGER", "typo"),
    ],
)
def test_stage1_rejects_invalid_operator_values(key, value):
    with pytest.raises(ValueError, match=key):
        select_tier(None, {key: value})


def test_known_insufficient_vram_prevents_launch():
    gpu = GpuInfo("small", "id", 8, (8, 0), 1.0, 8.0)
    with pytest.raises(ValueError, match="VRAM"):
        dynamic_gpu_mem_util(gpu)


def test_free_vram_cap_wins_over_floor_and_rounding():
    gpu = GpuInfo("large", "id", 24, (8, 0), 4.2, 24.0)
    assert dynamic_gpu_mem_util(gpu) <= 4.2 * 0.92 / 24


@pytest.mark.parametrize(
    "objective,fast,expected",
    [
        ("throughput", 10.5, 2),
        ("throughput", 10.6, 4),
        ("balanced", 10.5, 2),
        ("balanced", 10.6, 4),
    ],
)
def test_positive_objectives_keep_strict_five_percent_margin(objective, fast, expected):
    def measure(candidate, items):
        return measurement(items, ppm=fast if candidate["vl_lanes"] == 4 else 10)

    result = run(AutotuneConfig(objective=objective), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == expected


def test_candidate_output_must_be_stable_across_replicas():
    calls = {}

    def measure(candidate, items):
        lanes = candidate["vl_lanes"]
        calls[lanes] = calls.get(lanes, 0) + 1
        result = measurement(items, ppm=lanes * 10)
        if lanes != 2 and calls[lanes] == 2:
            result["outputs"] = ["different output"] * len(items)
        return result

    result = run(AutotuneConfig(objective="throughput"), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == 2
    assert all(not m["accepted"] for m in result["measurements"][1:])


def test_latency_can_be_derived_only_from_valid_complete_durations():
    def measure(candidate, items):
        result = measurement(items, latency=20 / candidate["vl_lanes"])
        result.pop("p95_seconds")
        return result

    result = run(AutotuneConfig(objective="latency"), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == 8
    assert result["measurements"][0]["p95_seconds"] == 10.0


def test_expired_budget_retains_verified_baseline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("ocrserver.autotune.runner.time.monotonic", lambda: clock[0])

    def measure(_candidate, items):
        clock[0] += 1
        return measurement(items)

    result = run(AutotuneConfig(max_seconds=3), {}, {}, measure_fn=measure)
    assert result["decisions"]["vl_lanes"] == 2
    assert result["measurements"][1]["accepted"] is False


def test_baseline_finishing_over_budget_falls_back_without_saving(
    tmp_path,
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr("ocrserver.autotune.runner.time.monotonic", lambda: clock[0])
    profile_path = tmp_path / "profile.json"
    profile_path.write_bytes(b"previous profile")
    calls = []

    def measure(candidate, items):
        calls.append(candidate["vl_lanes"])
        clock[0] += 2
        return measurement(items)

    result = run(
        AutotuneConfig(max_seconds=3, profile_path=str(profile_path), force=True),
        {},
        {},
        measure_fn=measure,
    )
    assert result["action"] == "fallback"
    assert "deadline exceeded" in result["reason"]
    assert calls == [2, 2]
    assert profile_path.read_bytes() == b"previous profile"
    assert "outputs" not in result["measurements"][0]


def test_preemption_alone_preserves_documented_nonblocking_policy():
    def measure(_candidate, items):
        return measurement(items) | {"had_preemption": True}

    result = run(AutotuneConfig(), {}, {}, measure_fn=measure)
    assert result["action"] == "calibrated"
    assert result["decisions"]["vl_lanes"] == 2
    assert all(record["had_preemption"] for record in result["measurements"])
    assert all(record["accepted"] for record in result["measurements"])


@pytest.fixture
def valid_profile(tmp_path):
    path = tmp_path / "profile.json"
    run(
        AutotuneConfig(profile_path=str(path)),
        {},
        {},
        measure_fn=lambda _candidate, items: measurement(items),
    )
    return path


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": 2},
        {"fingerprint": None},
        {"mode": []},
        {"decisions": {"vl_lanes": 0, "page_parallelism": 1}},
        {"warmup_seconds": math.nan},
        {"measurements": [None]},
    ],
)
def test_invalid_nested_profiles_are_not_reused(valid_profile, patch):
    value = json.loads(valid_profile.read_text(encoding="utf-8")) | patch
    valid_profile.write_text(json.dumps(value), encoding="utf-8")
    assert load_profile(valid_profile) is None


def test_atomic_profile_replace_failure_preserves_previous(valid_profile, monkeypatch):
    original = valid_profile.read_bytes()
    profile = load_profile(valid_profile)

    def fail_replace(*_args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("ocrserver.autotune.profile.os.replace", fail_replace)
    with pytest.raises(OSError):
        save_profile(valid_profile, profile)
    assert valid_profile.read_bytes() == original
    assert list(valid_profile.parent.glob("*.tmp")) == []


def test_runtime_manifest_rejects_invalid_nested_gpu(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps({"schema_version": 1, "gpu": {"compute_capability": None}})
    )
    assert _read_runtime_manifest({"OCR_VLLM_HARDWARE_FILE": str(path)}) == {}


def test_cgroup_unlimited_sentinel_without_host_ram(monkeypatch):
    def read(path, **kwargs):
        if path.as_posix().endswith("memory.limit_in_bytes"):
            return str((1 << 63) - 4096)
        raise OSError

    monkeypatch.setattr(Path, "read_text", read)
    assert _memory_limit_gb(0) == 0


def test_source_digest_covers_all_ocr_modules_and_required_assets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in ("ocrserver", "calibration", "vllm"):
        shutil.copytree(
            root / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__")
        )
    for name in ("ocr_server.py", "autotune_entrypoint.py"):
        shutil.copyfile(root / name, tmp_path / name)
    original = _source_digest(tmp_path)
    table = tmp_path / "ocrserver/table_md.py"
    table.write_bytes(table.read_bytes() + b"\n# digest regression\n")
    assert _source_digest(tmp_path) != original
    (tmp_path / "calibration/formula.png").unlink()
    with pytest.raises(ValueError, match="missing fixture"):
        _source_digest(tmp_path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("OCR_VLLM_MAX_NUM_BATCHED_TOKENS", "0"),
        ("OCR_VLLM_MAX_MODEL_LEN", "-1"),
        ("OCR_VLLM_MM_PROCESSOR_CACHE_GB", "-1"),
    ],
)
def test_command_validates_model_and_cache_limits(key, value):
    with pytest.raises(ValueError, match=key):
        build_vllm_command(select_tier(None, {}), "model", 8080, {key: value})


@pytest.mark.parametrize("port", [0, 65536, True, "8080"])
def test_command_validates_port(port):
    with pytest.raises(ValueError, match="OCR_VLLM_PORT"):
        build_vllm_command(select_tier(None, {}), "model", port)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (503, ""),
        (200, ""),
        (200, "vllm:kv_cache_usage_perc nan"),
        (200, "vllm:kv_cache_usage_perc -1"),
    ],
)
async def test_failed_metrics_are_unknown_not_zero(monkeypatch, status, body):
    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    monkeypatch.setattr(httpx, "AsyncClient", lambda: factory(transport=transport))
    result = await autotune_entrypoint._fetch_vllm_metrics("http://test/v1")
    assert result["kv_cache_pct"] is None


@pytest.mark.asyncio
async def test_measurement_cancellation_stops_monitor(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def monitor(_url, stop):
        started.set()
        try:
            await stop.wait()
            return {"peak_kv_cache_pct": 0.1, "preemption_total": 0, "samples": 1}
        finally:
            stopped.set()

    async def fetch(_url):
        return {"kv_cache_pct": 0.1, "preemption_total": 0}

    class Pipeline:
        calls = 0

        async def parse_page(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return {"markdown": "warmup"}
            await asyncio.Event().wait()

    monkeypatch.setattr(autotune_entrypoint, "_monitor_vllm_metrics", monitor)
    monkeypatch.setattr(autotune_entrypoint, "_fetch_vllm_metrics", fetch)
    task = asyncio.create_task(
        autotune_entrypoint._measure_workload(
            Pipeline(),
            {"vl_lanes": 2},
            [("text", b"image", "expected")],
            "http://test/v1",
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


def test_empty_normalized_ground_truth_is_invalid():
    with pytest.raises(ValueError, match="ground truth"):
        autotune_entrypoint._cer("output", "<p>!!!</p>")


def test_bit_parallel_levenshtein_matches_dynamic_programming():
    import random

    from ocrserver.autotune.gates import levenshtein

    def reference(a: str, b: str) -> int:
        row = list(range(len(b) + 1))
        for i, x in enumerate(a, 1):
            prev, row[0] = row[0], i
            for j, y in enumerate(b, 1):
                prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
        return row[-1]

    rng = random.Random(0)
    for _ in range(500):
        a = "".join(rng.choice("abcé ") for _ in range(rng.randint(0, 80)))
        b = "".join(rng.choice("abcé ") for _ in range(rng.randint(0, 80)))
        assert levenshtein(a, b) == reference(a, b)
    assert levenshtein("", "abc") == 3 and levenshtein("abc", "") == 3


@pytest.mark.asyncio
async def test_cer_scoring_is_outside_timed_ocr(monkeypatch):
    clock = [0.0]

    async def monitor(_url, stop):
        await stop.wait()
        return {"peak_kv_cache_pct": 0.1, "preemption_total": 0, "samples": 1}

    async def fetch(_url):
        return {"kv_cache_pct": 0.1, "preemption_total": 0}

    class Pipeline:
        async def parse_page(self, *_args):
            clock[0] += 2.0
            return {"markdown": "expected"}

    def slow_cer(predicted, ground_truth):
        clock[0] += 100.0
        return 0.0

    monkeypatch.setattr(autotune_entrypoint.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(autotune_entrypoint, "_monitor_vllm_metrics", monitor)
    monkeypatch.setattr(autotune_entrypoint, "_fetch_vllm_metrics", fetch)
    monkeypatch.setattr(autotune_entrypoint, "_cer", slow_cer)
    result = await autotune_entrypoint._measure_workload(
        Pipeline(),
        {"vl_lanes": 2},
        [("text", b"image", "expected")],
        "http://test/v1",
    )
    assert result["durations_seconds"] == [2.0]
    assert result["cers"] == [0.0]


def test_small_card_keeps_kv_cache_lean_for_gpu_layout():
    # 8 GB card, plenty free: 2.0 weights + 1.5 overhead + 0.5 KV = 4.0 GB, so
    # the PP-DocLayoutV3 layout model sharing the card keeps its headroom.
    gpu = GpuInfo("rtx-4060", "id", 7, (8, 9), 7.5, 8.0)
    assert dynamic_gpu_mem_util(gpu) == 0.5
