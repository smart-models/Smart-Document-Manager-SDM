"""Hardware/software fingerprint for autotune profile invalidation."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
from collections.abc import Mapping
from pathlib import Path

from ocrserver.autotune.tiers import DEFAULT_MODEL_REVISION, GpuInfo

MODEL_REPOSITORY = "PaddlePaddle/PaddleOCR-VL-1.6"
logger = logging.getLogger(__name__)


def _version_str(modname: str) -> str:
    try:
        mod = __import__(modname)
        return getattr(mod, "__version__", "unknown")
    except ImportError:
        return "missing"


def _source_digest(root: Path | None = None) -> str:
    """Hash sorted source identities and every declared calibration asset."""
    from calibration.generate import load_ground_truth

    h = hashlib.sha256()
    root = root or Path(__file__).resolve().parents[2]
    paths = set((root / "ocrserver").rglob("*.py"))
    paths.update(
        root / name
        for name in (
            "ocr_server.py",
            "autotune_entrypoint.py",
            "vllm/stage1_entrypoint.py",
            "calibration/generate.py",
            "calibration/ground_truth.json",
            "calibration/manifest.json",
            "calibration/fonts/DejaVuSans.ttf",
            "calibration/fonts/LICENSE",
        )
    )
    items = load_ground_truth(root / "calibration" / "ground_truth.json")
    paths.update(root / "calibration" / item.path for item in items)
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        h.update(name.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _read_runtime_manifest(env: Mapping[str, str]) -> dict:
    """Read validated public Stage-1 metadata, treating absent data as unknown.

    Reject malformed GPU identity, nonfinite memory and invalid launch limits.
    Return an empty mapping for missing/unreadable manifests; warn without
    echoing file contents when an existing manifest cannot be trusted.
    """
    path = Path(env.get("OCR_VLLM_HARDWARE_FILE", "/app/runtime/vllm-hardware.json"))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("invalid runtime manifest schema")
        gpu = value.get("gpu")
        if gpu is not None:
            if not isinstance(gpu, dict):
                raise ValueError("invalid GPU object")
            if any(not isinstance(gpu.get(key), str) for key in ("name", "uuid")):
                raise ValueError("invalid GPU identity")
            cc = gpu.get("compute_capability")
            if (
                not isinstance(cc, list)
                or len(cc) != 2
                or any(type(part) is not int or part < 0 for part in cc)
            ):
                raise ValueError("invalid GPU compute capability")
            for key in ("vram_gb", "vram_free_gb", "vram_total_gb"):
                number = gpu.get(key)
                if number is None and key != "vram_gb":
                    continue
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(number)
                    or number < 0
                ):
                    raise ValueError("invalid GPU memory")
        tier = value.get("tier")
        if not isinstance(tier, dict) or not isinstance(value.get("vllm_version"), str):
            raise ValueError("invalid runtime tier/version")  # noqa: TRY004 - invalid manifest
        for key in ("max_num_seqs", "max_model_len"):
            if key in tier and (type(tier[key]) is not int or tier[key] <= 0):
                raise ValueError("invalid runtime tier limit")
        if "gpu_memory_utilization" in tier:
            utilization = tier["gpu_memory_utilization"]
            if (
                isinstance(utilization, bool)
                or not isinstance(utilization, (int, float))
                or not 0 < utilization <= 1
            ):
                raise ValueError("invalid runtime GPU utilization")
        if "enforce_eager" in tier and type(tier["enforce_eager"]) is not bool:
            raise ValueError("invalid runtime eager flag")
        json.dumps(value, allow_nan=False)
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        logger.warning("Ignoring invalid or unreadable vLLM runtime manifest")
        return {}


def _effective_cpu_count(logical_count: int) -> int:
    quota = None
    period = None
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
    except (OSError, ValueError):
        try:
            quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip()
            period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip()
        except OSError:
            return logical_count

    try:
        if quota not in (None, "max", "-1") and period is not None:
            available = math.ceil(int(quota) / int(period))
            return max(1, min(logical_count, available))
    except (ValueError, ZeroDivisionError):
        return logical_count
    return logical_count


def _memory_limit_gb(host_memory_gb: int) -> int:
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    except OSError:
        try:
            raw = (
                Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text().strip()
            )
        except OSError:
            return host_memory_gb

    try:
        if raw != "max":
            limit_bytes = int(raw)
            # cgroup v1 uses a huge page-aligned signed-long value for unlimited.
            if limit_bytes >= (1 << 60) or limit_bytes <= 0:
                return host_memory_gb
            limit = max(1, math.ceil(limit_bytes / (1024**3)))
            return min(host_memory_gb, limit) if host_memory_gb else limit
    except ValueError:
        return host_memory_gb
    return host_memory_gb


def build_fingerprint(gpu: GpuInfo | None, env: Mapping[str, str]) -> dict:
    """Describe hardware, runtime, source and measurement settings for cache reuse.

    Resolve settings without a saved profile. Use Stage-1 runtime GPU metadata
    when local GPU detection is unavailable, retaining effective resource limits.
    """
    from ocrserver.config import load_settings

    settings = load_settings(env, None)
    try:
        import psutil

        cpu_count = psutil.cpu_count(logical=True) or 0
        memory_gb = int(psutil.virtual_memory().total // (1024**3))
    except ImportError:
        cpu_count = os.cpu_count() or 0
        memory_gb = 0

    runtime = _read_runtime_manifest(env)
    external_gpu = runtime.get("gpu") or {}
    gpu_name = gpu.name if gpu else external_gpu.get("name", "none")
    gpu_uuid = gpu.uuid if gpu else external_gpu.get("uuid", "none")
    gpu_vram = gpu.vram_gb if gpu else external_gpu.get("vram_gb", 0)
    gpu_cc = (
        list(gpu.compute_capability)
        if gpu
        else list(external_gpu.get("compute_capability", [0, 0]))
    )
    return {
        "gpu_name": gpu_name,
        "gpu_uuid": gpu_uuid,
        "gpu_vram_gb": gpu_vram,
        "gpu_compute_capability": gpu_cc,
        "cpu_count": cpu_count,
        "effective_cpu_count": _effective_cpu_count(cpu_count),
        "memory_gb": memory_gb,
        "effective_memory_gb": _memory_limit_gb(memory_gb),
        "model_repository": MODEL_REPOSITORY,
        "model_revision": env.get("OCR_VLLM_MODEL_REVISION", "").strip()
        or DEFAULT_MODEL_REVISION,
        "paddleocr_version": _version_str("paddleocr"),
        "paddlepaddle_version": _version_str("paddle"),
        "python_version": platform.python_version(),
        "vllm_version": runtime.get("vllm_version") or _version_str("vllm"),
        "vllm_tier": runtime.get("tier", {}),
        "vllm_command_sha256": fingerprint_digest(
            {"command": runtime.get("command", [])}
        ),
        "vllm_model_runner_v2": runtime.get("model_runner_v2", "unknown"),
        "measurement_settings": {
            name: getattr(settings, name)
            for name in (
                "parse_dpi",
                "max_pages",
                "pdf_max_page_pixels",
                "page_timeout",
                "request_timeout",
                "vl_lanes",
                "layout_threads",
                "vl_max_inflight",
                "vl_max_num_seqs",
                "vllm_url",
                "vllm_served_model",
            )
        },
        "source_digest": _source_digest(),
    }


def fingerprint_digest(fp: dict) -> str:
    """Hash sorted finite JSON so fingerprint dictionary insertion order is moot."""
    canonical = json.dumps(fp, sort_keys=True, allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def fingerprints_match(a: dict, b: dict) -> bool:
    """Compare canonical fingerprint digests, rejecting nonfinite JSON values."""
    return fingerprint_digest(a) == fingerprint_digest(b)
