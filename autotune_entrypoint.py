"""Calibrate OCR server in a pre-start process, then exec Uvicorn."""

from __future__ import annotations

import asyncio
import gc
import logging
import math
import os
import time
import traceback
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path

from calibration.generate import normalize_text
from ocrserver.autotune.fingerprint import build_fingerprint
from ocrserver.autotune.gates import cer
from ocrserver.autotune.profile import AutotuneConfig
from ocrserver.autotune.runner import MeasurementIsolationError, run as run_autotune
from ocrserver.autotune.tiers import _env_bool, detect_gpu
from ocrserver.config import Settings, load_settings

logger = logging.getLogger(__name__)

CAL_DIR = Path(__file__).resolve().parent / "calibration"


def _normalize_for_cer(text: str) -> str:
    return normalize_text(text)


def _cer(predicted: str, ground_truth: str) -> float:
    predicted = _normalize_for_cer(predicted)
    ground_truth = _normalize_for_cer(ground_truth)
    if not ground_truth:
        raise ValueError("Calibration ground truth is empty after normalization")
    # Clamped to [0, 1]: insertion-heavy mismatches (e.g. HTML tables / LaTeX
    # formulas vs plain-text ground truth) can push raw CER above 1.
    return cer(ground_truth, predicted)


def _make_measure_fn(
    vllm_url: str, served_model: str, settings: Settings | None = None
) -> Callable:
    def measure_fn(candidate: dict, items: list) -> dict:
        try:
            return asyncio.run(
                _async_measure(candidate, items, vllm_url, served_model, settings)
            )
        except BaseException as exc:
            # Completed coroutine frames can otherwise retain native predictors.
            # Preserve exception identity and traceback locations, not locals.
            traceback.clear_frames(exc.__traceback__)
            raise
        finally:
            # PaddleX stores self.predict = self.__call__, a cycle retaining its
            # native runner. Collect after coroutine cleanup and loop shutdown;
            # workers still running retain strong ownership of their predictors.
            gc.collect()

    return measure_fn


async def _fetch_vllm_metrics(vllm_base: str) -> dict:
    """Fetch kv_cache usage and preemption count from vLLM /metrics."""
    import httpx

    metrics_url = vllm_base.rstrip("/").removesuffix("/v1") + "/metrics"
    unavailable = {"kv_cache_pct": None, "preemption_total": None}
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(metrics_url, timeout=5.0)
            if resp.status_code != 200:
                return unavailable
            text = resp.text
    except httpx.HTTPError:
        return unavailable

    kv_used = None
    preemption_total = None
    for line in text.splitlines():
        if line.startswith(("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")):
            try:
                value = float(line.split()[1])
                if not math.isfinite(value) or not 0 <= value <= 1:
                    return unavailable
                kv_used = max(kv_used or 0.0, value)
            except (IndexError, ValueError):
                return unavailable
        elif line.startswith("vllm:num_preemptions_total"):
            try:
                value = float(line.split()[1])
                if not math.isfinite(value) or value < 0:
                    return unavailable
                preemption_total = (preemption_total or 0.0) + value
            except (IndexError, ValueError):
                return unavailable
    return {"kv_cache_pct": kv_used, "preemption_total": preemption_total}


async def _monitor_vllm_metrics(vllm_base: str, stop: asyncio.Event) -> dict:
    peak_kv = None
    max_preemptions = None
    samples = 0
    failed_samples = 0
    while True:
        final_sample = stop.is_set()
        metrics = await _fetch_vllm_metrics(vllm_base)
        if metrics["kv_cache_pct"] is None:
            failed_samples += 1
        else:
            peak_kv = max(peak_kv or 0.0, metrics["kv_cache_pct"])
            samples += 1
        if metrics["preemption_total"] is not None:
            max_preemptions = max(max_preemptions or 0.0, metrics["preemption_total"])
        if final_sample:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
    if failed_samples:
        logger.warning(
            "Autotune memory metrics unavailable for %d samples", failed_samples
        )
    return {
        "peak_kv_cache_pct": peak_kv if not failed_samples else None,
        "preemption_total": max_preemptions,
        "samples": samples,
    }


async def _async_measure(
    candidate: dict,
    items: list,
    vllm_url: str,
    served_model: str,
    settings: Settings | None = None,
) -> dict:
    """Measure one candidate in a fresh pipeline after a backend health check.

    Load only validated calibration fixtures. Return an error report when the
    backend or workload is unavailable; reject empty normalized ground truth.
    Always close the pipeline and raise MeasurementIsolationError if native work
    survives cleanup, preventing contamination of later candidate measurements.
    """
    import httpx

    from ocrserver.pipeline_backend import PipelineBackend

    vllm_base = vllm_url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(vllm_base + "/health", timeout=3.0)
            if resp.status_code != 200:
                raise ConnectionError("vLLM not healthy")
    except (httpx.HTTPError, ConnectionError):
        return {"error": "vLLM health unavailable"}

    workload: list[tuple[str, bytes, str]] = []
    for item in items:
        if not _normalize_for_cer(item.ground_truth):
            raise ValueError("Calibration ground truth is empty after normalization")
        workload.append(
            (item.kind, (CAL_DIR / item.path).read_bytes(), item.ground_truth)
        )
    if not workload:
        return {"error": "calibration workload is empty"}

    settings = settings or Settings(vllm_url=vllm_url, vllm_served_model=served_model)
    # The candidate's vl_lanes becomes vl_rec_max_concurrency (real block-level
    # VL parallelism); layout_threads sets paddle CPU threads. No external
    # semaphore: concurrency is now internal to paddleocr's predict, and the
    # pipeline's own lock keeps the paddle layout predictor thread-safe.
    pipeline = PipelineBackend(
        settings,
        vllm_url,
        vl_inflight=None,
        vl_lanes=candidate["vl_lanes"],
        layout_threads=candidate.get("layout_threads"),
    )
    try:
        return await _measure_workload(
            pipeline, candidate, workload, vllm_url, settings
        )
    finally:
        if await pipeline.aclose(timeout=5.0) is False:
            raise MeasurementIsolationError(
                "native work remains after pipeline cleanup"
            )


async def _measure_workload(
    pipeline,
    candidate: dict,
    workload: list,
    vllm_url: str,
    settings: Settings | None = None,
) -> dict:
    """Warm the pipeline, time sequential fixtures and sample vLLM memory usage.

    Apply API rasterization and timeout limits to PDFs and reject incomplete
    page results. Stop the metrics monitor before computing normalized output
    and CER so scoring cannot distort throughput or memory sampling. Return
    measured timings, quality, KV-cache peak and per-measurement preemption data.
    """
    from ocrserver.pdf_utils import rasterize_pdf

    settings = settings or Settings(vllm_url=vllm_url)
    warmup_start = time.monotonic()
    warmup_item = next((item for item in workload if item[0] != "pdf"), None)
    if warmup_item is not None:
        await pipeline.parse_page(
            warmup_item[1], candidate["vl_lanes"], settings.page_timeout
        )
    warmup_seconds = time.monotonic() - warmup_start

    pre = await _fetch_vllm_metrics(vllm_url)
    monitor_stop = asyncio.Event()
    monitor_task = asyncio.create_task(_monitor_vllm_metrics(vllm_url, monitor_stop))
    durations: list[float] = []
    markdowns: list[str] = []
    page_count = 0

    # Items run sequentially; the concurrency being tuned lives inside each
    # predict (block-level VL fan-out). Only OCR is timed: CER is scored after
    # the metrics monitor stops, so it neither inflates durations nor blocks
    # KV-cache sampling.
    try:
        for kind, raw, _ in workload:
            start = time.monotonic()
            if kind == "pdf":
                pages = await asyncio.to_thread(
                    rasterize_pdf,
                    raw,
                    dpi=settings.parse_dpi,
                    max_pages=settings.max_pages,
                    max_page_pixels=settings.pdf_max_page_pixels,
                )
                page_count += len(pages)
                results = await pipeline.parse_document(
                    pages,
                    candidate["vl_lanes"],
                    settings.page_timeout,
                    settings.request_timeout,
                    close_images=True,
                )
                if len(results) != len(pages) or not results:
                    raise ValueError("Calibration PDF returned incomplete pages")
            else:
                page_count += 1
                results = [
                    await pipeline.parse_page(
                        raw, candidate["vl_lanes"], settings.page_timeout
                    )
                ]
            if any(
                not isinstance(result, dict)
                or result.get("error")
                or not isinstance(result.get("markdown"), str)
                for result in results
            ):
                raise ValueError("Calibration OCR returned invalid output")
            durations.append(time.monotonic() - start)
            markdowns.append("\n".join(result["markdown"] for result in results))
    finally:
        monitor_stop.set()
        try:
            observed = await asyncio.wait_for(monitor_task, timeout=6.0)
        except (TimeoutError, asyncio.CancelledError):
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
            if asyncio.current_task().cancelling():
                raise
            observed = {
                "peak_kv_cache_pct": None,
                "preemption_total": None,
                "samples": 0,
            }

    # One CER per item: for PDFs the page markdowns are concatenated before
    # comparison to the document ground truth.
    cers = [_cer(md, item[2]) for md, item in zip(markdowns, workload)]
    outputs = [_normalize_for_cer(md) for md in markdowns]

    total_time = sum(durations)
    ppm = (page_count / total_time * 60) if total_time > 0 else 0
    sorted_d = sorted(durations)
    p95 = sorted_d[min(int(len(sorted_d) * 0.95), len(sorted_d) - 1)]

    # Preemption is judged by the delta over THIS measurement, not the vLLM
    # process lifetime counter.
    had_preemption = (
        observed["preemption_total"] > pre["preemption_total"]
        if observed["preemption_total"] is not None
        and pre["preemption_total"] is not None
        else False
    )

    return {
        "pages_per_min": ppm,
        "cers": cers,
        "outputs": outputs,
        "durations_seconds": durations,
        "p95_seconds": p95,
        "kv_cache_pct": observed["peak_kv_cache_pct"],
        "metric_samples": observed["samples"],
        "had_preemption": had_preemption,
        "warmup_seconds": warmup_seconds,
    }


def prepare(env: Mapping[str, str] | None = None) -> dict:
    """Prepare calibration and publish its action and reason in the process env.

    Use the supplied mapping for input settings, or os.environ when omitted.
    Return the startup action report without launching the API server.
    """
    values = os.environ if env is None else env
    if _env_bool(values, "OCR_ARCHIVE_ONLY"):
        # Archive-only serves stored archives: no GPU detection, no vLLM
        # probe, no model load and no calibration pass may run.
        result = {
            "action": "disabled",
            "reason": "archive-only mode: OCR inference is disabled",
        }
    elif AutotuneConfig.from_env(values).mode == "off":
        result = {"action": "disabled", "reason": "autotune is off"}
    else:
        config = AutotuneConfig.from_env(values)
        gpu = detect_gpu()
        # Keep the fingerprint clean: the digest is stored separately by
        # make_profile. Injecting it into the dict makes the saved fingerprint
        # differ from the freshly-built one at load time, so the profile never
        # matches.
        fingerprint = build_fingerprint(gpu, values)

        settings = load_settings(values, None)
        measure_fn = _make_measure_fn(
            settings.vllm_url, settings.vllm_served_model, settings
        )
        result = run_autotune(config, fingerprint, values, measure_fn=measure_fn)
    logger.info("Stage-2 autotune result: %s", result.get("action"))
    os.environ["OCR_AUTOTUNE_ACTION"] = result.get("action", "pending")
    os.environ["OCR_AUTOTUNE_REASON"] = str(result.get("reason", ""))
    return result


def main() -> int:
    """Run startup calibration, then replace this process with one Uvicorn worker."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    result = prepare()
    if result.get("action") in {"fallback"}:
        logger.warning("Autotune fallback: %s", result)
    else:
        logger.info("Autotune result: %s", result)

    command = [
        "uvicorn",
        "ocr_server:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(load_settings(os.environ, None).port),
        "--workers",
        "1",
        "--timeout-graceful-shutdown",
        "10",
    ]
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
