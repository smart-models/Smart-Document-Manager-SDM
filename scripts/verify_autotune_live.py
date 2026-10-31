"""Measure a real autotune sweep without replacing the active runtime profile.

Run from /app in the API container with PYTHONPATH=/app if the script was copied
to /tmp. Only public calibration measurements and output hashes are persisted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from autotune_entrypoint import _make_measure_fn
from ocrserver.autotune.fingerprint import build_fingerprint
from ocrserver.autotune.profile import AutotuneConfig
from ocrserver.autotune.runner import run
from ocrserver.autotune.tiers import detect_gpu
from ocrserver.config import load_settings


def main() -> int:
    """Measure a live calibration sweep using a temporary, inactive profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("quick", "full"), required=True)
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    values = dict(os.environ)
    settings = load_settings(values, None)
    fingerprint = build_fingerprint(detect_gpu(), values)
    with tempfile.TemporaryDirectory(prefix="ocr-autotune-verification-") as temporary:
        config = AutotuneConfig(
            mode=args.mode,
            objective="balanced",
            profile_path=str(Path(temporary) / "profile.json"),
            max_seconds=args.max_seconds,
            force=True,
        )
        started = time.monotonic()
        result = run(
            config,
            fingerprint,
            values,
            measure_fn=_make_measure_fn(
                settings.vllm_url, settings.vllm_served_model, settings
            ),
        )
        elapsed = time.monotonic() - started
    report = {
        key: result[key]
        for key in (
            "action",
            "reason",
            "decisions",
            "measurements",
            "warmup_seconds",
        )
        if key in result
    }
    report["mode"] = args.mode
    report["objective"] = config.objective
    report["source_digest"] = fingerprint["source_digest"]
    report["elapsed_seconds"] = elapsed
    report["environment"] = {
        key: fingerprint[key]
        for key in (
            "gpu_name",
            "effective_cpu_count",
            "effective_memory_gb",
            "model_revision",
            "paddleocr_version",
            "paddlepaddle_version",
            "vllm_version",
            "vllm_tier",
        )
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"action": result["action"], "mode": args.mode}))
    return 0 if result["action"] == "calibrated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
