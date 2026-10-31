"""Verify hardware profiles, calibration gates and vLLM command selection."""

from __future__ import annotations

import json

from autotune_entrypoint import _normalize_for_cer
from ocrserver.autotune.fingerprint import (
    _effective_cpu_count,
    _memory_limit_gb,
    build_fingerprint,
)
from ocrserver.autotune.gates import output_equivalence_gate
from ocrserver.autotune.profile import AutotuneConfig, load_profile, profile_matches
from ocrserver.autotune.runner import run
from ocrserver.autotune.tiers import (
    DEFAULT_MODEL_REVISION,
    build_vllm_command,
)


def test_unicode_normalization_preserves_non_latin_text():
    normalized = _normalize_for_cer("<p>中文 OCR — École</p> \\mathbf{速度}")

    assert "中文" in normalized
    assert "école" in normalized
    assert "速度" in normalized


def test_output_equivalence_gate_detects_content_change():
    assert output_equivalence_gate(["same output"], ["same output"]).passed
    assert not output_equivalence_gate(["different"], ["same output"]).passed


def test_autotune_promotes_fast_equivalent_candidate(tmp_path):
    def measure(candidate, items):
        lanes = candidate["vl_lanes"]
        output = "stable unicode 中文" if lanes != 8 else "changed output"
        return {
            "pages_per_min": {2: 10.0, 4: 12.0, 8: 14.0}[lanes],
            "cers": [0.1] * len(items),
            "outputs": [output] * len(items),
            "durations_seconds": [6.0] * len(items),
            "p95_seconds": 6.0,
            "kv_cache_pct": 0.2,
            "had_preemption": False,
            "metric_samples": 2,
            "warmup_seconds": 1.0,
        }

    profile_path = tmp_path / "profile.json"
    config = AutotuneConfig(
        mode="quick",
        objective="throughput",
        profile_path=str(profile_path),
        force=True,
    )
    fingerprint = {"effective_cpu_count": 4, "source_digest": "test"}

    result = run(config, fingerprint, {}, measure_fn=measure)

    assert result["decisions"]["vl_lanes"] == 4
    assert len(result["measurements"]) == 3
    assert result["measurements"][2]["accepted"] is False
    assert result["measurements"][2]["rejection_reasons"]
    assert "outputs" not in result["measurements"][0]
    assert profile_matches(
        load_profile(profile_path), fingerprint, "quick", "throughput"
    )


def test_fingerprint_reads_shared_vllm_manifest(tmp_path):
    manifest_path = tmp_path / "vllm-hardware.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "gpu": {
                    "name": "Test GPU",
                    "uuid": "GPU-1",
                    "vram_gb": 8,
                    "compute_capability": [8, 9],
                },
                "tier": {"max_num_seqs": 8},
                "vllm_version": "0.11.1",
            }
        ),
        encoding="utf-8",
    )

    fingerprint = build_fingerprint(
        None, {"OCR_VLLM_HARDWARE_FILE": str(manifest_path)}
    )

    assert fingerprint["gpu_name"] == "Test GPU"
    assert fingerprint["gpu_compute_capability"] == [8, 9]
    assert fingerprint["vllm_version"] == "0.11.1"
    assert fingerprint["model_revision"] == DEFAULT_MODEL_REVISION


def test_vllm_command_pins_revision_and_seed():
    tier = {
        "gpu_memory_utilization": 0.6,
        "max_num_seqs": 8,
        "dtype": "bfloat16",
        "max_model_len": 16384,
        "enforce_eager": False,
    }

    command = build_vllm_command(tier, "model", 8080)

    assert command[command.index("--revision") + 1] == DEFAULT_MODEL_REVISION
    assert command[command.index("--code-revision") + 1] == DEFAULT_MODEL_REVISION
    assert command[command.index("--seed") + 1] == "0"


def test_resource_limits_support_cgroup_v1(monkeypatch):
    values = {
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "400000",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(4 * 1024**3),
    }

    def fake_read_text(path, *args, **kwargs):
        value = values.get(path.as_posix())
        if value is None:
            raise OSError
        return value

    monkeypatch.setattr("pathlib.Path.read_text", fake_read_text)

    assert _effective_cpu_count(16) == 4
    assert _memory_limit_gb(31) == 4
