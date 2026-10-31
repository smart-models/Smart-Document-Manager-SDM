"""Stage-1 vLLM entrypoint: fingerprint GPU, select tier, exec vllm serve."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocrserver.autotune.tiers import (
    _env_int,
    build_vllm_command,
    detect_gpu,
    select_tier,
)

logger = logging.getLogger(__name__)
MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
DEFAULT_PORT = 8080


def select_and_build(
    env: Mapping[str, str], *, write_manifest: bool = False
) -> list[str]:
    """Select GPU launch settings and optionally persist the runtime manifest."""
    gpu = detect_gpu()
    tier = select_tier(gpu, env)
    port = _env_int(env, "OCR_VLLM_PORT")
    if port is None:
        port = DEFAULT_PORT
    cmd = build_vllm_command(tier, MODEL, port, dict(env))
    logger.info("Stage-1 tier: %s", tier)
    logger.info("vLLM command: %s", " ".join(cmd))
    if write_manifest:
        _write_runtime_manifest(gpu, tier, cmd)
    return cmd


def _write_runtime_manifest(gpu, tier: dict, command: list[str]) -> None:
    """Atomically publish public GPU and launch metadata for the API container.

    Flush the temporary file and make it readable by the API's separate user
    before replacement. Warn on filesystem failure and always remove temporary
    files; metadata publication failure does not prevent the model from starting.
    """
    path = Path(
        os.environ.get("OCR_VLLM_HARDWARE_FILE", "/app/runtime/vllm-hardware.json")
    )
    try:
        vllm_version = version("vllm")
    except PackageNotFoundError:
        vllm_version = "unknown"
    manifest = {
        "schema_version": 1,
        "gpu": asdict(gpu) if gpu else None,
        "tier": tier,
        "model": MODEL,
        "vllm_version": vllm_version,
        "command": command,
        "model_runner_v2": os.environ.get("VLLM_USE_V2_MODEL_RUNNER", "0"),
    }
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(manifest, sort_keys=True, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        # vLLM writes as root; the API reads these public metadata as UID 1000.
        # NamedTemporaryFile starts at 0600, which os.replace would preserve.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except OSError as exc:
        logger.warning("Could not write vLLM runtime manifest %s: %s", path, exc)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    """Persist Stage-1 runtime metadata and replace this process with vLLM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    values = dict(os.environ)
    cmd = select_and_build(values, write_manifest=True)
    os.execvp(cmd[0], cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
