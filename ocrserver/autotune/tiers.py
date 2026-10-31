"""Stage-1 deterministic vLLM launch parameter selection."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

# PaddleOCR-VL-0.9B footprint used to size gpu_memory_utilization dynamically.
MODEL_WEIGHTS_GB = 2.0  # bf16 weights (~1.9 GiB measured at load)
RUNTIME_OVERHEAD_GB = 1.5  # CUDA context + activations + CUDA graphs
FLOOR_GPU_MEM_UTIL = 0.20
CEIL_GPU_MEM_UTIL = 0.90
FREE_VRAM_SAFETY = 0.92  # never target more than 92% of currently-free VRAM
DEFAULT_MODEL_REVISION = "c5630abae1d940eafe0697512a0325494b02ab42"


@dataclass(frozen=True)
class GpuInfo:
    """Describe the selected GPU and optional precise/free VRAM measurements."""

    name: str
    uuid: str
    vram_gb: int
    compute_capability: tuple[int, int]
    vram_free_gb: float | None = None
    vram_total_gb: float | None = None  # precise total; vram_gb is a floored int


def detect_gpu() -> GpuInfo | None:
    """Inspect CUDA device zero, returning None when detection is unavailable."""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        props = torch.cuda.get_device_properties(0)
        free_gb: float | None = None
        try:
            free_bytes, _ = torch.cuda.mem_get_info(0)
            free_gb = round(free_bytes / (1024**3), 2)
        except (RuntimeError, AttributeError):
            free_gb = None
        return GpuInfo(
            name=props.name,
            uuid=str(getattr(props, "uuid", "unknown")),
            vram_gb=int(props.total_memory // (1024**3)),
            compute_capability=(props.major, props.minor),
            vram_free_gb=free_gb,
            vram_total_gb=round(props.total_memory / (1024**3), 2),
        )
    except (ImportError, RuntimeError, AttributeError):
        return None


def _kv_target_gb(vram_gb: int) -> float:
    """KV-cache headroom to reserve, scaled to the card class."""
    if vram_gb >= 24:
        return 5.0
    if vram_gb >= 16:
        return 2.5
    # Every card under 16 GB shares VRAM with the PP-DocLayoutV3 layout model
    # in ocr-api (deliberately including 12 GB cards); OCR-sized requests use
    # ~2% of a 1 GB KV cache.
    return 0.5


def dynamic_gpu_mem_util(gpu: GpuInfo | None) -> float:
    """Size gpu_memory_utilization to fit the model + KV headroom.

    PaddleOCR-VL is tiny (~2 GB), so a fixed 0.8 reserves far more VRAM than
    needed. Target weights + runtime overhead + a tier-scaled KV budget, capped
    by currently-free VRAM, instead of grabbing a fixed fraction of the card.
    """
    if gpu is None or gpu.vram_gb <= 0:
        return 0.80  # no GPU info at stage-1 → conservative default
    # Use the precise total when available; vram_gb is floored and would inflate
    # the fraction (e.g. 7 for an 8 GB card).
    total = gpu.vram_total_gb if gpu.vram_total_gb else float(gpu.vram_gb)
    needed = MODEL_WEIGHTS_GB + RUNTIME_OVERHEAD_GB + _kv_target_gb(gpu.vram_gb)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("GPU total VRAM must be finite and positive")
    cap = CEIL_GPU_MEM_UTIL
    if gpu.vram_free_gb is not None:
        if not math.isfinite(gpu.vram_free_gb) or gpu.vram_free_gb < 0:
            raise ValueError("GPU free VRAM must be finite and nonnegative")
        cap = min(cap, gpu.vram_free_gb * FREE_VRAM_SAFETY / total)
    util = min(cap, max(FLOOR_GPU_MEM_UTIL, needed / total))
    # Round down: nearest rounding can reserve more than the known free budget.
    util = math.floor(util * 1000) / 1000
    if util * total < MODEL_WEIGHTS_GB + RUNTIME_OVERHEAD_GB:
        raise ValueError(
            "Insufficient free VRAM for model weights and runtime overhead"
        )
    return util


def _env_float(env: Mapping[str, str], key: str) -> float | None:
    v = env.get(key, "").strip()
    if not v:
        return None
    try:
        value = float(v)
    except ValueError as exc:
        raise ValueError(f"{key} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return value


def _env_int(env: Mapping[str, str], key: str, *, minimum: int = 1) -> int | None:
    v = env.get(key, "").strip()
    if not v:
        return None
    try:
        value = int(v)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer >= {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return value


def _env_str(env: Mapping[str, str], key: str) -> str | None:
    v = env.get(key, "").strip()
    return v if v else None


def _env_bool(env: Mapping[str, str], key: str) -> bool | None:
    v = env.get(key, "").strip().lower()
    if not v:
        return None
    if v not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        raise ValueError(f"{key} must be a boolean (true/false)")
    return v in ("1", "true", "yes", "on")


def select_tier(gpu: GpuInfo | None, env: Mapping[str, str]) -> dict:
    """Choose launch capacity from GPU resources and validated explicit overrides.

    Use the conservative fallback tier without a detected GPU. Reject explicit
    memory fractions that exceed known free VRAM or cannot cover model overhead.
    """
    vram = gpu.vram_gb if gpu else 8
    cc = gpu.compute_capability if gpu else (7, 0)

    if vram >= 24:
        max_seqs = 64
    elif vram >= 16:
        max_seqs = 32
    else:
        max_seqs = 16
    # gpu_memory_utilization is sized to actual need + free VRAM, not a fixed
    # per-tier fraction (the model is small; don't grab the whole card).
    mem_util = dynamic_gpu_mem_util(gpu)

    dtype = "bfloat16" if cc >= (8, 0) else "float16"
    enforce_eager = False

    override = _env_float(env, "OCR_VLLM_GPU_MEM_UTIL")
    if override is not None:
        if not 0 < override <= 1:
            raise ValueError("OCR_VLLM_GPU_MEM_UTIL must be within (0, 1]")
        if gpu is not None:
            total = gpu.vram_total_gb or gpu.vram_gb
            if override * total < MODEL_WEIGHTS_GB + RUNTIME_OVERHEAD_GB:
                raise ValueError("OCR_VLLM_GPU_MEM_UTIL reserves insufficient VRAM")
            if (
                gpu.vram_free_gb is not None
                and override * total > gpu.vram_free_gb * FREE_VRAM_SAFETY
            ):
                raise ValueError("OCR_VLLM_GPU_MEM_UTIL exceeds the free VRAM budget")
        mem_util = override
    override = _env_int(env, "OCR_VLLM_MAX_NUM_SEQS")
    if override is not None:
        max_seqs = override
    override = _env_bool(env, "OCR_VLLM_ENFORCE_EAGER")
    if override is not None:
        enforce_eager = override

    return {
        "gpu_memory_utilization": mem_util,
        "max_num_seqs": max_seqs,
        "dtype": dtype,
        "enforce_eager": enforce_eager,
        "max_model_len": 16384,
    }


def build_vllm_command(
    tier: dict, model: str, port: int, env: Mapping[str, str] | None = None
) -> list[str]:
    """Build a vLLM argument vector from a selected tier and validated overrides.

    Validate the serving port and preserve the pinned model revision unless
    explicitly overridden. Return arguments without launching a subprocess.
    """
    env = env or {}
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("OCR_VLLM_PORT must be an integer within [1, 65535]")
    max_num_batched_tokens = str(
        _env_int(env, "OCR_VLLM_MAX_NUM_BATCHED_TOKENS") or 16384
    )
    served_model_name = (
        _env_str(env, "OCR_VLLM_SERVED_MODEL_NAME") or "PaddleOCR-VL-0.9B"
    )
    max_model_len = str(
        _env_int(env, "OCR_VLLM_MAX_MODEL_LEN") or tier["max_model_len"]
    )
    mm_processor_cache_gb = str(
        _env_int(env, "OCR_VLLM_MM_PROCESSOR_CACHE_GB", minimum=0) or 0
    )
    revision = _env_str(env, "OCR_VLLM_MODEL_REVISION") or DEFAULT_MODEL_REVISION
    cmd = [
        "vllm",
        "serve",
        model,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--trust-remote-code",
        # The API sends per-request mm_processor_kwargs (min/max pixels); the
        # client is our own trusted ocr-api container on the private network.
        "--trust-request-mm-kwargs",
        "--revision",
        revision,
        "--code-revision",
        revision,
        "--seed",
        "0",
        "--no-enable-prefix-caching",
        "--mm-processor-cache-gb",
        mm_processor_cache_gb,
        "--max-num-batched-tokens",
        max_num_batched_tokens,
        "--served-model-name",
        served_model_name,
        "--gpu-memory-utilization",
        str(tier["gpu_memory_utilization"]),
        "--max-num-seqs",
        str(tier["max_num_seqs"]),
        "--dtype",
        tier["dtype"],
        "--max-model-len",
        max_model_len,
    ]
    if tier.get("enforce_eager"):
        cmd.append("--enforce-eager")
    return cmd
