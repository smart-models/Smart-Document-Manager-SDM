"""PaddleOCRVL pipeline wrapper and blocks normalization."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
from PIL import Image

from ocrserver.config import Settings
from ocrserver.element_ocr import check_pixel_limit, upright_rgb
from ocrserver.schemas import Block

logger = logging.getLogger(__name__)


def to_bgr_ndarray(image: bytes | Image.Image | np.ndarray) -> np.ndarray:
    """Normalize a page to BGR; ndarray inputs must already be BGR uint8."""
    if isinstance(image, np.ndarray):
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("page arrays must be BGR uint8 with three channels")
        return image
    if isinstance(image, bytes):
        with Image.open(io.BytesIO(image)) as decoded:
            check_pixel_limit(decoded)
            return to_bgr_ndarray(decoded)
    converted = upright_rgb(image)
    try:
        return np.array(converted)[:, :, ::-1].copy()
    finally:
        converted.close()


def normalize_blocks(raw_blocks: list[dict]) -> list[Block]:
    """Map pipeline JSON blocks to the stable Block schema.

    Unknown labels pass through verbatim (non-exhaustive enum by design).
    """
    blocks: list[Block] = []
    for i, raw in enumerate(raw_blocks):
        # PaddleOCR-VL emits block_label/block_content/block_bbox; keep the
        # generic label/coordinate/bbox keys as fallbacks.
        coord = raw.get("block_bbox") or raw.get("coordinate") or raw.get("bbox") or []
        if "block_order" in raw:
            order = raw["block_order"]
        else:
            order = raw.get("order", i)
        blocks.append(
            Block(
                type=raw.get("block_label") or raw.get("label", "unknown"),
                bbox=[float(c) for c in coord],
                content=raw.get("block_content", raw.get("content", "")) or "",
                order=int(order) if order is not None else None,
                block_id=raw.get("block_id"),
                group_id=raw.get("group_id"),
                global_block_id=raw.get("global_block_id"),
                global_group_id=raw.get("global_group_id"),
                polygon=raw.get("block_polygon_points"),
            )
        )
    return blocks


def image_to_png_bytes(image: Any) -> bytes:
    """Encode a Paddle markdown crop as PNG without changing its colours."""
    if isinstance(image, bytes):
        pil_image = Image.open(io.BytesIO(image))
        owns_image = True
    elif isinstance(image, Image.Image):
        pil_image = image
        owns_image = False
    elif isinstance(image, np.ndarray):
        array = image
        if array.ndim == 3 and array.shape[2] == 3:
            array = array[:, :, ::-1]
        elif array.ndim == 3 and array.shape[2] == 4:
            array = array[:, :, [2, 1, 0, 3]]
        pil_image = Image.fromarray(np.ascontiguousarray(array))
        owns_image = True
    else:
        raise TypeError(f"unsupported markdown image type: {type(image)!r}")
    output = io.BytesIO()
    try:
        pil_image.save(output, format="PNG", optimize=True)
    finally:
        if owns_image:
            pil_image.close()
    return output.getvalue()


def _page_dimension(value: Any) -> int | None:
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class PipelineBackend:
    """Wraps PaddleOCRVL with vl_rec_backend='vllm-server'."""

    def __init__(
        self,
        settings: Settings,
        vllm_url: str,
        vl_inflight: asyncio.Semaphore | None = None,
        _pipeline: Any | None = None,
        vl_lanes: int | None = None,
        layout_threads: int | None = None,
    ) -> None:
        """Configure lazy pipeline construction and serialized native inference.

        Args:
            settings: API limits and default pipeline concurrency settings.
            vllm_url: OpenAI-compatible endpoint used by the VL recognizer.
            vl_inflight: Optional shared admission semaphore for native workers.
            _pipeline: Optional injected pipeline for isolated inference tests.
            vl_lanes: Explicit block-recognition concurrency, overriding settings.
            layout_threads: Explicit local layout thread count, overriding settings.
        """
        self._settings = settings
        self._vllm_url = vllm_url
        self._vl_inflight = vl_inflight
        self._pipeline = _pipeline
        # Real concurrency knobs. vl_lanes bounds how many block-level VL
        # requests paddleocr fires at vLLM concurrently (its genai client uses
        # asyncio.Semaphore(vl_rec_max_concurrency)); layout_threads sets paddle
        # CPU intra-op threads for the local layout model.
        self._vl_lanes = vl_lanes if vl_lanes is not None else settings.vl_lanes
        self._layout_threads = (
            layout_threads if layout_threads is not None else settings.layout_threads
        )
        # paddle's AnalysisPredictor (local layout model) is NOT thread-safe;
        # concurrent predict() calls on the shared pipeline segfault. Serialize
        # every predict through this lock.
        self._predict_lock = threading.Lock()
        self._workers: set[asyncio.Task] = set()
        self._closing = False

    async def _start_worker(self, work: Callable[[], Any]) -> asyncio.Task:
        """Admit native work and retain its slot until its thread exits.

        A daemon thread permits bounded application shutdown even when a
        native library hangs. Cancellation of a caller never cancels work.
        """
        if self._closing:
            raise RuntimeError("pipeline is closing")
        if self._vl_inflight is not None:
            await self._vl_inflight.acquire()
        if self._closing:
            if self._vl_inflight is not None:
                self._vl_inflight.release()
            raise RuntimeError("pipeline is closing")
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def finish(outcome: tuple[bool, Any]) -> None:
            if self._vl_inflight is not None:
                self._vl_inflight.release()
            if not completed.done():
                completed.set_result(outcome)

        def run() -> None:
            try:
                outcome = (True, work())
            except BaseException as exc:  # noqa: BLE001 - forward native worker failures
                outcome = (False, exc)
            try:
                loop.call_soon_threadsafe(finish, outcome)
            except RuntimeError:
                # The bounded shutdown expired and the event loop is closed.
                pass

        async def wait_for_worker() -> Any:
            success, value = await asyncio.shield(completed)
            if not success:
                raise value
            return value

        def consume(task: asyncio.Task) -> None:
            self._workers.discard(task)
            if not task.cancelled():
                task.exception()

        task = asyncio.create_task(wait_for_worker())
        self._workers.add(task)
        task.add_done_callback(consume)
        try:
            threading.Thread(target=run, daemon=True, name="ocr-pipeline").start()
        except BaseException as exc:
            finish((False, exc))
            raise
        return task

    async def aclose(self, timeout: float) -> bool:
        """Stop admission; return whether native workers drained within timeout."""
        self._closing = True
        if self._workers:
            _, pending = await asyncio.wait(tuple(self._workers), timeout=timeout)
            if pending:
                logger.warning(
                    "shutdown left %d native OCR workers running", len(pending)
                )
            return not pending
        return True

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        from paddleocr import PaddleOCRVL

        # PaddleOCR forwards this common option to the local PaddleX CPU
        # engine. Keep it per pipeline so tuning candidates remain independent;
        # omitting it preserves PaddleOCR's own default (currently 10).
        options = {}
        if self._layout_threads is not None:
            options["cpu_threads"] = self._layout_threads
        self._pipeline = PaddleOCRVL(
            pipeline_version="v1.6",
            vl_rec_backend="vllm-server",
            vl_rec_server_url=self._vllm_url,
            # Must match vLLM's --served-model-name; otherwise paddleocr sends
            # the local model name ("PaddleOCR-VL-1.6-0.9B") and vLLM 404s, which
            # crashes the native vlm worker under concurrency.
            vl_rec_api_model_name=self._settings.vllm_served_model,
            # Bound concurrent VL requests to vLLM. Default (200) floods an
            # OCR-sized vLLM (max_num_seqs 16) and triggers KV preemption.
            vl_rec_max_concurrency=self._vl_lanes,
            **options,
        )
        return self._pipeline

    @staticmethod
    def _extract_result(res: Any) -> dict:
        markdown_result = getattr(res, "markdown", "") or ""
        markdown_images: dict = {}
        if isinstance(markdown_result, dict):
            # PaddleOCR-VL returns a MarkdownResult dict; the text is
            # under "markdown_texts".
            markdown = markdown_result.get("markdown_texts", "") or ""
            markdown_images = markdown_result.get("markdown_images", {}) or {}
        else:
            markdown = str(markdown_result)
        raw_json = getattr(res, "json", {}) or {}
        res_data = raw_json.get("res", raw_json) if isinstance(raw_json, dict) else {}
        raw_blocks = res_data.get("parsing_res_list") or res_data.get("blocks", [])
        images: list[dict] = []
        for name, image in markdown_images.items():
            try:
                images.append({"name": str(name), "png": image_to_png_bytes(image)})
            except (OSError, TypeError, ValueError):
                logger.warning("could not encode Paddle markdown image %s", name)
        return {
            "markdown": markdown,
            "blocks": normalize_blocks(raw_blocks),
            "images": images,
            "width": _page_dimension(res_data.get("width")),
            "height": _page_dimension(res_data.get("height")),
        }

    async def parse_document(
        self,
        images: list[bytes | Image.Image | np.ndarray],
        lanes: int,
        page_timeout: float,
        total_budget: float,
        *,
        close_images: bool = False,
        on_page: Callable[[dict], None] | None = None,
    ) -> list[dict]:
        """Run the pipeline on all pages with ONE streaming predict call.

        A single multi-image call lets paddleocr's internal queued pipeline
        (use_queues) overlap layout analysis of one 8-page layout batch with VL
        recognition of the previous one — per-page calls serialize the stages.

        PaddleOCR 3.6.0 predict() materializes the complete result list;
        predict_iter() yields each page as VL recognition finishes it, so it is
        preferred and on_page is called (on the event loop) for every page as it
        arrives. The returned list keeps page order. Each page gets
        {"markdown", "blocks", "elapsed"} or {"error", "elapsed"}.

        Timeouts: the queued pipeline may buffer stages and deliver the first
        result long after later pages started, so arrival gaps are bounded by
        the AGGREGATE budget min(total_budget, page_timeout * n_pages) — never
        by page_timeout alone (for a single page that reduces to the old
        per-page semantics). On timeout/error the remaining pages are marked
        failed. The worker retains its slot until native work ends. With
        close_images=True, ownership of PIL inputs transfers to this call.
        """
        if not images:
            return []
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        abandoned = threading.Event()
        allowed = min(total_budget, page_timeout * len(images))
        deadline = time.monotonic() + allowed

        def deliver(kind: str, payload: Any) -> None:
            if not abandoned.is_set():
                queue.put_nowait((kind, payload))

        def emit(kind: str, payload: Any = None) -> None:
            if not abandoned.is_set():
                try:
                    loop.call_soon_threadsafe(deliver, kind, payload)
                except RuntimeError:
                    abandoned.set()

        def _worker() -> None:
            try:
                with self._predict_lock:
                    if abandoned.is_set():
                        return
                    pipeline = self._ensure_pipeline()
                    if abandoned.is_set():
                        return
                    arrays = [to_bgr_ndarray(img) for img in images]
                    if abandoned.is_set():
                        return
                    iterate = (
                        getattr(pipeline, "predict_iter", None) or pipeline.predict
                    )
                    stream = iterate(arrays, temperature=0.0)
                    try:
                        for res in stream:
                            if abandoned.is_set():
                                # A departed caller: stop instead of running
                                # layout and VL for the rest of the block.
                                break
                            emit("page", self._extract_result(res))
                    finally:
                        close = getattr(stream, "close", None)
                        if close is not None:
                            close()
            except Exception as exc:  # noqa: BLE001 — forwarded to the caller
                emit("error", exc)
            finally:
                if close_images:
                    for image in images:
                        if isinstance(image, Image.Image):
                            image.close()
                emit("done")

        try:
            async with asyncio.timeout(allowed):
                await self._start_worker(_worker)
        except TimeoutError:
            if close_images:
                for image in images:
                    if isinstance(image, Image.Image):
                        image.close()
            return [
                {"error": "request timeout exceeded", "elapsed": allowed}
                for _ in images
            ]
        except BaseException:
            if close_images:
                for image in images:
                    if isinstance(image, Image.Image):
                        image.close()
            raise
        try:
            pages: list[dict] = []
            failure: str | None = None
            for _ in images:
                page_start = time.monotonic()
                timeout = deadline - page_start
                if failure is None and timeout > 0:
                    try:
                        kind, payload = await asyncio.wait_for(queue.get(), timeout)
                    except asyncio.TimeoutError:
                        failure = f"page timed out after {timeout:.1f}s"
                    else:
                        if kind == "error":
                            failure = str(payload)
                        elif kind == "done":
                            failure = "pipeline ended before all pages were returned"
                    if failure is None:
                        payload["elapsed"] = time.monotonic() - page_start
                        pages.append(payload)
                        if on_page is not None:
                            on_page(payload)
                        continue
                elif failure is None:
                    failure = "request timeout exceeded"
                pages.append(
                    {"error": failure, "elapsed": time.monotonic() - page_start}
                )
            return pages
        finally:
            abandoned.set()
            while not queue.empty():
                queue.get_nowait()

    async def parse_page(
        self,
        image: bytes | Image.Image | np.ndarray,
        lanes: int,
        page_timeout: float,
        *,
        close_image: bool = False,
    ) -> dict:
        """Run the full pipeline on a single page image.

        Acquires one shared API admission slot for the native worker. Its
        internal VL fan-out is bounded separately by vl_lanes.

        Returns {"markdown": str, "blocks": list[Block]}.
        """
        abandoned = threading.Event()
        deadline = time.monotonic() + page_timeout

        def _sync() -> dict:
            try:
                with self._predict_lock:
                    if abandoned.is_set():
                        return {"markdown": "", "blocks": []}
                    pipeline = self._ensure_pipeline()
                    if abandoned.is_set():
                        return {"markdown": "", "blocks": []}
                    arr = to_bgr_ndarray(image)
                    if abandoned.is_set():
                        return {"markdown": "", "blocks": []}
                    results = list(pipeline.predict(arr, temperature=0.0))
                    if not results:
                        return {"markdown": "", "blocks": []}
                    return self._extract_result(results[0])
            finally:
                if close_image and isinstance(image, Image.Image):
                    image.close()

        try:
            async with asyncio.timeout(page_timeout):
                worker = await self._start_worker(_sync)
        except BaseException:
            if close_image and isinstance(image, Image.Image):
                image.close()
            raise
        try:
            return await asyncio.wait_for(
                asyncio.shield(worker), timeout=max(0, deadline - time.monotonic())
            )
        finally:
            abandoned.set()
