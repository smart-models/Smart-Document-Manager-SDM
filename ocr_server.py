"""FastAPI OCR server — endpoints, auth, rate limit, backpressure, shutdown."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from openai import APITimeoutError
from PIL import Image
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile

from ocrserver.artifacts import ArtifactCapture, source_format
from ocrserver.chunking.api import register_chunk_routes
from ocrserver.chunking.errors import ChunkError
from ocrserver.chunking.runtime import ChunkRuntime
from ocrserver.config import Settings, load_settings
from ocrserver.element_ocr import (
    ImageTooLargeError,
    load_element_image,
    MultiframeImageError,
    SPOTTING_UPSCALE_THRESHOLD,
    load_image_pages,
    parse_spotting_output,
    preprocess_for_spotting,
)
from ocrserver.json_rag import build_json_rag, document_name
from ocrserver.metrics import (
    ACTIVE_REQUESTS,
    ARCHIVE_ONLY_MODE,
    AUTOTUNE_INFO,
    PAGE_DURATION,
    PAGES_PROCESSED,
    QUEUE_SIZE,
    REQUEST_DURATION,
    REQUESTS,
    REQUESTS_REJECTED,
    SERVER_INFO,
    VLLM_UP,
)
from ocrserver.office_utils import OfficeError, convert_to_pdf, office_format
from ocrserver.pdf_utils import (
    BLOCK_MAX_PIXELS,
    MAX_BLOCK_PAGES,
    PdfError,
    PdfTooLargeError,
    is_pdf,
    pdf_page_count,
    pdf_page_pixels,
    plan_blocks,
    render_pages,
)
from ocrserver.pipeline_backend import PipelineBackend
from ocrserver.progress import ProgressEntry, ProgressRegistry, parse_progress_id
from ocrserver.schemas import (
    ELEMENT_TASKS,
    MODEL_NAME,
    PIPELINE_VERSION,
    OcrJsonRequest,
    OcrResponse,
    PageResult,
    ParseJsonRequest,
    ParseResponse,
    TokenUsage,
    make_warning,
    upload_openapi,
)
from ocrserver.storage import STORAGE_HEADERS, ArtifactStore, StorageOutcome
from ocrserver.storage_browser import (
    StorageBrowser,
    StorageDownloadMiddleware,
    register_storage_routes,
)
from ocrserver.markdown_normalizer import MarkdownNormalizer
from ocrserver.token_count import ContentTokenCounter
from ocrserver.vllm_client import BackendUnavailable, CircuitBreaker, VllmClient

logger = logging.getLogger(__name__)
APP_VERSION = "1.0.0"
PUBLIC_AUTH_PATHS = {
    "/",
    "/health",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
}
# POST routes that run layout or VL inference. Archive-only refuses them in
# the middleware, before the body is read: no rasterization, no Office
# conversion, no admission slot, no Paddle/VL initialization, no vLLM call.
INFERENCE_PATHS = frozenset({"/parse", "/ocr", "/parse/chunks"})
ARCHIVE_ONLY_DETAIL = {
    "code": "ocr_disabled_archive_only",
    "message": "OCR inference is disabled in archive-only mode.",
}


class TokenBucket:
    """Bounded token buckets; only idle, fully replenished entries expire."""

    def __init__(
        self, rpm, burst, max_clients=10000, idle_seconds=600.0, clock=time.monotonic
    ):
        """Configure refill rate, burst capacity and bounded client tracking."""
        self._capacity = float(burst)
        self._refill_rate = rpm / 60.0
        self._max_clients = max_clients
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._buckets = OrderedDict()

    def allow(self, key: str) -> bool:
        """Consume one available token, expiring only fully replenished idle keys."""
        if self._refill_rate == 0:
            return True  # A configured zero rate disables rate limiting.
        now = self._clock()
        # LRU order keeps eviction proportional to entries actually removed.
        expired = []
        for oldest, (tokens, last) in self._buckets.items():
            idle = now - last
            if idle < self._idle_seconds:
                break
            if tokens + idle * self._refill_rate >= self._capacity:
                expired.append(oldest)
        for oldest in expired:
            del self._buckets[oldest]
        if key not in self._buckets:
            if len(self._buckets) >= self._max_clients:
                return False
            self._buckets[key] = [self._capacity, now]
        tokens, last = self._buckets[key]
        tokens = min(self._capacity, tokens + (now - last) * self._refill_rate)
        allowed = tokens >= 1.0
        self._buckets[key] = [tokens - 1.0 if allowed else tokens, now]
        self._buckets.move_to_end(key)
        return allowed


def _close_images(value) -> None:
    """Release preparation results that no request can consume."""
    if isinstance(value, Image.Image):
        value.close()
    elif isinstance(value, (tuple, list)):
        for item in value:
            _close_images(item)
    elif isinstance(value, _DocumentPlan):
        value.close()


class _MemoryBudget:
    """Thread-safe admission before image decoding allocates native memory."""

    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.lock = threading.Lock()

    def reserve(self, amount):
        with self.lock:
            if amount > self.limit - self.used:
                REQUESTS_REJECTED.labels(reason="image_memory_full").inc()
                raise HTTPException(503, detail={"code": "image_memory_full"})
            self.used += amount

    def release(self, amount):
        with self.lock:
            self.used -= amount


class _Admission:
    """Keep capacity until a non-cancellable preparation thread actually exits."""

    def __init__(self, semaphore, reason, pending, gauge=None):
        self.semaphore = semaphore
        self.reason = reason
        self.pending = pending
        self.gauge = gauge
        self.acquired = False
        self.worker = None
        self.memory = None

    def reserve_memory(self, budget, amount):
        budget.reserve(amount)
        self.memory = (budget, amount)

    async def acquire(self, budget: float) -> None:
        try:
            await asyncio.wait_for(self.semaphore.acquire(), min(0.5, budget))
        except TimeoutError as exc:
            REQUESTS_REJECTED.labels(reason=self.reason).inc()
            raise HTTPException(503, detail={"code": self.reason}) from exc
        self.acquired = True
        ACTIVE_REQUESTS.inc()
        if self.gauge is not None:
            self.gauge.inc()

    async def prepare(self, function, budget: float):
        loop = asyncio.get_running_loop()
        worker = loop.create_future()
        self.worker = worker
        self.pending.add(worker)
        worker.add_done_callback(self.pending.discard)

        def complete(success, value):
            if worker.done():
                if success:
                    _close_images(value)
            elif success:
                worker.set_result(value)
            else:
                worker.set_exception(value)

        def run():
            try:
                outcome = (True, function())
            except BaseException as exc:  # noqa: BLE001 - forward worker failures to its owner
                outcome = (False, exc)
            try:
                loop.call_soon_threadsafe(complete, *outcome)
            except RuntimeError:
                if outcome[0]:
                    _close_images(outcome[1])

        try:
            threading.Thread(target=run, daemon=True, name="ocr-preparation").start()
        except BaseException as exc:
            worker.set_exception(exc)
            raise
        result = await asyncio.wait_for(asyncio.shield(worker), budget)
        self.worker = None
        return result

    def _release(self) -> None:
        if self.memory is not None:
            budget, amount = self.memory
            self.memory = None
            budget.release(amount)
        if self.acquired:
            self.acquired = False
            ACTIVE_REQUESTS.dec()
            self.semaphore.release()
            if self.gauge is not None:
                self.gauge.dec()

    def finish(self) -> None:
        if self.worker is None:
            self._release()
            return

        def completed(worker):
            try:
                if not worker.cancelled() and worker.exception() is None:
                    _close_images(worker.result())
            finally:
                self._release()

        self.worker.add_done_callback(completed)


def _remaining(start: float, timeout: float) -> float:
    remaining = start + timeout - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _convert_office(
    data: bytes,
    kind: str,
    settings: Settings,
    deadline: float | None = None,
) -> bytes:
    if not settings.office_enabled:
        raise HTTPException(400, detail={"code": "office_disabled"})
    try:
        pdf = convert_to_pdf(
            data,
            kind,
            soffice=settings.office_soffice_path,
            timeout=settings.office_timeout_seconds,
            deadline=deadline,
        )
    except OfficeError as exc:
        raise HTTPException(
            422, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    if len(pdf) > settings.max_file_bytes:
        REQUESTS_REJECTED.labels(reason="oversize").inc()
        raise HTTPException(413, detail={"code": "file_too_large"})
    return pdf


@dataclass
class _DocumentPlan:
    """One document's pages grouped into blocks that are rendered on demand."""

    blocks: list[range]
    render: Callable[[range], list]
    pages_total: int
    dpi: int | None
    close: Callable[[], None]


def _prepare_document(
    data: bytes,
    dpi: int,
    max_pages: int,
    explicit_dpi: bool,
    settings: Settings,
    capture: ArtifactCapture | None = None,
    deadline: float | None = None,
    on_truncation: Callable[[int, int], None] | None = None,
):
    """Capture original input, convert Office and load bounded document pages.

    Return a block plan and explicit conversion/truncation/DPI warnings.
    Optional capture failure disables archival without preventing OCR
    preparation. PDF blocks are sized from page dimensions without rendering
    and stay within PDF_MAX_PAGE_PIXELS; a single page over it is a 413. Image
    frames are already decoded, so their plan pixels are capped at the budget
    (no new 413 for images) and `close` releases frames never handed out.
    """
    warnings = []

    def blocks(pixels: list[int]) -> list[range]:
        return plan_blocks(
            pixels, settings.pdf_max_page_pixels, BLOCK_MAX_PIXELS, MAX_BLOCK_PAGES
        )

    kind = None if is_pdf(data) else office_format(data)
    if capture is not None:
        try:
            capture.source(data, *source_format(data, kind))
            capture.effective["dpi"] = dpi if is_pdf(data) or kind else None
        except Exception:  # noqa: BLE001 - optional capture cannot fail OCR
            capture.fail()
    if kind:
        data = _convert_office(data, kind, settings, deadline)
        warnings.append(
            make_warning(
                "office_converted",
                "Office document was rendered to PDF by LibreOffice before OCR; "
                "layout follows LibreOffice rendering.",
                severity="info",
                target={"field": "file"},
                details={"source_format": kind},
            )
        )
    if is_pdf(data):
        total = pdf_page_count(data)
        if total > max_pages and on_truncation is not None:
            on_truncation(max_pages, total)  # refuse before any rasterization
        effective = min(max_pages, total)
        if total > max_pages:
            warnings.append(
                make_warning(
                    "pages_truncated",
                    f"PDF truncated to {effective} of {total} pages.",
                    target={"field": "file"},
                    details={"max_pages": max_pages, "total_pages": total},
                )
            )
        pixels = pdf_page_pixels(data, dpi, effective)
        return _DocumentPlan(
            blocks(pixels),
            lambda block: render_pages(data, dpi, block),
            len(pixels),
            dpi,
            lambda: None,
        ), warnings
    with _image_errors():
        pages, total = load_image_pages(data, max_pages)
    if total > max_pages and on_truncation is not None:
        try:
            on_truncation(max_pages, total)
        except BaseException:
            _close_images(pages)
            raise
    if total > max_pages:
        warnings.append(
            make_warning(
                "pages_truncated",
                f"Image truncated to {max_pages} of {total} pages.",
                target={"field": "file"},
                details={"max_pages": max_pages, "total_pages": total},
            )
        )
    if explicit_dpi:
        warnings.append(
            make_warning(
                "dpi_ignored",
                "dpi applies only to PDF rasterization; "
                "image input keeps its native pixels.",
                target={"field": "dpi"},
            )
        )
    handed: set[int] = set()

    def render(block: range) -> list:
        handed.update(block)
        return pages[block.start : block.stop]

    pixels = [min(p.width * p.height, settings.pdf_max_page_pixels) for p in pages]
    return _DocumentPlan(
        blocks(pixels),
        render,
        len(pages),
        None,
        lambda: _close_images([p for i, p in enumerate(pages) if i not in handed]),
    ), warnings


async def _watch_disconnect(request: Request) -> tuple[asyncio.Event, asyncio.Task]:
    """Set the event when the client disconnects (the body is already read).

    Same pattern as `ocrserver/chunking/api.py::_run_connected`: read
    `request.receive()` until `http.disconnect`. The caller cancels the task.
    """
    event = asyncio.Event()

    async def watch():
        while (await request.receive())["type"] != "http.disconnect":
            pass
        event.set()

    return event, asyncio.create_task(watch())


@contextmanager
def _image_errors():
    """Map decode failures to API errors: too many pixels is not corruption."""
    try:
        yield
    except (ImageTooLargeError, Image.DecompressionBombError) as exc:
        raise HTTPException(
            400,
            detail={"code": "image_too_large", "max_pixels": Image.MAX_IMAGE_PIXELS},
        ) from exc
    except MultiframeImageError as exc:
        raise HTTPException(
            422, detail={"code": "multiframe_image_not_supported"}
        ) from exc
    except (OSError, ValueError, EOFError) as exc:
        raise HTTPException(400, detail={"code": "corrupt_image"}) from exc


def _prepare_element(
    data: bytes,
    task: str,
    capture: ArtifactCapture | None = None,
    reserve=None,
) -> str:
    if capture is not None:
        try:
            capture.source(data, *source_format(data))
        except Exception:  # noqa: BLE001 - optional capture cannot fail OCR
            capture.fail()
    with _image_errors():
        image = load_element_image(data, reserve)
    try:
        if task == "spotting":
            prepared = preprocess_for_spotting(image)
            if prepared is not image:
                image.close()
                image = prepared
        with io.BytesIO() as buffer:
            image.save(buffer, format="PNG")
            return base64.b64encode(buffer.getvalue()).decode("ascii")
    finally:
        image.close()


def _load_and_validate_profile() -> dict | None:
    """Load profile from disk and validate it against the current fingerprint.

    Returns None if no profile path is set, the file is missing, or the
    fingerprint/mode/objective don't match the current environment.
    """
    if os.environ.get("OCR_AUTOTUNE_ACTION") in {"fallback", "disabled"}:
        return None
    profile_path = os.environ.get("OCR_AUTOTUNE_PROFILE", "")
    if not profile_path:
        return None
    from ocrserver.autotune.fingerprint import build_fingerprint
    from ocrserver.autotune.profile import load_profile, profile_matches
    from ocrserver.autotune.tiers import detect_gpu

    existing = load_profile(Path(profile_path))
    if existing is None:
        return None

    gpu = detect_gpu()
    fingerprint = build_fingerprint(gpu, dict(os.environ))
    config_mode = os.environ.get("OCR_AUTOTUNE", "quick").strip().lower() or "quick"
    config_objective = (
        os.environ.get("OCR_TUNING_OBJECTIVE", "balanced").strip().lower() or "balanced"
    )
    if not profile_matches(existing, fingerprint, config_mode, config_objective):
        logger.warning(
            "Autotune profile at %s does not match current "
            "fingerprint/mode/objective — ignoring",
            profile_path,
        )
        return None
    return existing


def _autotune_state(profile: dict | None, settings: Settings | None = None) -> dict:
    # The entrypoint always exports an action; without it the server was
    # started directly (not through autotune_entrypoint), so tuning never ran.
    action = os.environ.get("OCR_AUTOTUNE_ACTION", "not_run")
    reason = os.environ.get("OCR_AUTOTUNE_REASON", "").strip()
    if profile is None:
        state = {"action": action}
    else:
        state = {
            "action": action,
            "decisions": profile.get("decisions", {}),
            "mode": profile.get("mode", ""),
            "objective": profile.get("objective", ""),
        }
        warmup = profile.get("warmup_seconds")
        if isinstance(warmup, (int, float)):
            state["warmup_seconds"] = warmup
        measurements = profile.get("measurements")
        if isinstance(measurements, list):
            state["measured_candidates"] = len(measurements)
    if reason:
        state["reason"] = reason
    if settings is not None:
        # What the server actually runs with: the profile's decisions, env
        # overrides, or the safe defaults after a fallback.
        state["effective"] = {
            "vl_lanes": settings.vl_lanes,
            "page_parallelism": settings.page_parallelism,
            "layout_threads": settings.layout_threads,
        }
    return state


def _as_int(value: object, default: int) -> int:
    """Parse an int from form/JSON, treating None/empty as absent.

    Swagger submits blank optional fields as empty strings, so `int("")` must not
    500 the request.
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError as exc:
        raise HTTPException(
            422, detail={"code": "invalid_integer", "value": text}
        ) from exc


def _form_flag(value: object) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("", "0", "false", "no", "off"):
            return False
    raise HTTPException(422, detail={"code": "invalid_boolean"})


def _clean_str(value: object) -> str | None:
    """Coerce a form/JSON value to a stripped string, or None if empty."""
    if value is None or not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


async def _json_upload(request: Request, model: type[BaseModel]) -> dict:
    """Validate JSON options without returning document bytes in errors."""
    try:
        return model.model_validate(await request.json()).model_dump()
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, detail={"code": "invalid_json_body"}) from exc


async def _upload_fields(
    request: Request,
    settings: Settings,
    model: type[BaseModel] | None = None,
):
    """Receive and parse a bounded request within the remaining request deadline.

    Validate advertised and actual body sizes, reject duplicate multipart fields
    and close uploaded temporary files. Shield bounded local parsing so cleanup
    can finish even when the caller's receive deadline expires.
    """
    maximum = 4 * ((settings.max_file_bytes + 2) // 3) + 65_536
    declared = request.headers.get("content-length")
    if declared and (
        not declared.isdecimal() or len(declared) > 20 or int(declared) > maximum
    ):
        REQUESTS_REJECTED.labels(reason="oversize").inc()
        raise HTTPException(413, detail={"code": "request_body_too_large"})
    try:
        budget = _remaining(request.state.start_time, settings.request_timeout)
        # Match Starlette's AnyIO cancellation scopes while receiving the body.
        with anyio.fail_after(budget):
            raw = bytearray()
            async for part in request.stream():
                if len(raw) + len(part) > maximum:
                    REQUESTS_REJECTED.labels(reason="oversize").inc()
                    raise HTTPException(413, detail={"code": "request_body_too_large"})
                raw.extend(part)

            async def receive():
                return {"type": "http.request", "body": bytes(raw), "more_body": False}

            replay = Request(request.scope, receive)

            async def fields():
                if "application/json" in request.headers.get("content-type", ""):
                    if model is None:
                        raise HTTPException(
                            415, detail={"code": "unsupported_media_type"}
                        )
                    return await _json_upload(replay, model), None, None
                async with replay.form(max_files=1) as form:
                    if len(form.multi_items()) != len(form):
                        raise HTTPException(
                            422, detail={"code": "duplicate_form_field"}
                        )
                    upload = form.get("file")
                    if upload is not None and not isinstance(upload, UploadFile):
                        raise HTTPException(422, detail={"code": "invalid_file"})
                    data = (
                        await upload.read(settings.max_file_bytes + 1)
                        if upload
                        else None
                    )
                    if data is not None and len(data) > settings.max_file_bytes:
                        REQUESTS_REJECTED.labels(reason="oversize").inc()
                        raise HTTPException(413, detail={"code": "file_too_large"})
                    return dict(form), data, upload.filename if upload else None

            # Let bounded local parsing close its temporary files after cancellation.
            parsed = asyncio.create_task(fields())
            parsed.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            return await asyncio.shield(parsed)
    except TimeoutError as exc:
        raise HTTPException(504, detail={"code": "request_timeout"}) from exc


class _DropQueryString(logging.Filter):
    """Strip the query string from Uvicorn access lines.

    Storage queries carry archive prefixes and keys, which include the original
    file name. Uvicorn passes the path as the third of five arguments.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def configure_package_logging() -> None:
    """Attach one stream handler to this service's own loggers, at INFO.

    Uvicorn configures only its own ``uvicorn*`` loggers, so without this every
    ``logger.info`` in ``ocr_server`` and ``ocrserver`` falls through to
    logging's last-resort handler, which drops anything below WARNING. Scoped to
    these loggers so httpx and Paddle keep their defaults. Propagation is off:
    Paddle attaches its own handler to the root logger at runtime, which would
    print every line a second time.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    for name in ("ocr_server", "ocrserver"):
        package_logger = logging.getLogger(name)
        if not package_logger.handlers:
            package_logger.addHandler(handler)
            package_logger.setLevel(logging.INFO)
            package_logger.propagate = False
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _DropQueryString) for f in access.filters):
        access.addFilter(_DropQueryString())


def create_app(
    settings: Settings,
    pipeline: PipelineBackend | None = None,
    vllm_client: VllmClient | None = None,
    *,
    profile: dict | None = None,
    token_counter: ContentTokenCounter | None = None,
    chunk_runtime: ChunkRuntime | None = None,
    artifact_store: ArtifactStore | None = None,
    storage_browser: StorageBrowser | None = None,
) -> FastAPI:
    """Build an isolated API application with owned lifecycle and admission state.

    Args:
        settings: Validated runtime limits, authentication and service settings.
        pipeline: Optional layout/VL backend; otherwise construct the default.
        vllm_client: Optional element inference client; otherwise use HTTP vLLM.
        profile: Optional startup profile exposed in runtime diagnostics.
        token_counter: Optional parsed-content token counter.
        chunk_runtime: Optional CPU chunk worker runtime.
        artifact_store: Optional archive writer with bounded storage admission.
        storage_browser: Optional archive browsing and deletion service.

    Returns:
        A FastAPI application with routes, middleware and lifespan cleanup wired
        to the supplied or newly constructed service instances.
    """
    configure_package_logging()
    auth_tokens = settings.auth_tokens
    limiter = TokenBucket(
        settings.rate_limit_rpm,
        settings.rate_limit_burst,
        settings.rate_limit_max_clients,
        settings.rate_limit_idle_seconds,
    )
    preparation_tasks: set[asyncio.Future] = set()
    parse_sem = asyncio.Semaphore(settings.max_queue_size)
    # asyncio.Lock wakes waiters in FIFO order: one document's blocks at a time.
    document_lock = asyncio.Lock()
    progress_registry = ProgressRegistry(settings.max_queue_size)
    ocr_sem = asyncio.Semaphore(settings.element_max_slots)
    ocr_memory = _MemoryBudget(settings.element_memory_bytes)
    vl_inflight_limit = min(settings.vl_max_inflight, settings.vl_max_num_seqs)
    vl_inflight = asyncio.Semaphore(vl_inflight_limit)
    breaker = CircuitBreaker()
    if chunk_runtime is None:
        chunk_runtime = ChunkRuntime(settings)
    if artifact_store is None:
        artifact_store = ArtifactStore(settings)
    if storage_browser is None:
        storage_browser = StorageBrowser(settings)

    if vllm_client is None:
        vllm_client = VllmClient(settings, vl_inflight, breaker)

    if pipeline is None:
        pipeline = PipelineBackend(settings, settings.vllm_url, vl_inflight)

    if token_counter is None:
        token_counter = ContentTokenCounter(
            settings.hf_cache_dir, settings.vllm_model_revision
        )

    autotune_state = _autotune_state(profile, settings)
    ARCHIVE_ONLY_MODE.set(1 if settings.archive_only else 0)
    if profile and profile.get("decisions"):
        AUTOTUNE_INFO.info(
            {
                "action": autotune_state.get("action", ""),
                "mode": autotune_state.get("mode", ""),
                "objective": autotune_state.get("objective", ""),
            }
        )

    shutdown_event = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        SERVER_INFO.info({"version": APP_VERSION, "model": MODEL_NAME})
        if auth_tokens:
            # Labels only: no token value ever reaches a log line.
            logger.info("API auth labels: %s", ", ".join(sorted(auth_tokens.values())))
        else:
            logger.warning(
                "OCR API authentication is disabled: OCR_SERVER_API_TOKENS is empty"
            )
        if settings.archive_only:
            # No model server exists to poll: the gauge stays 0 by design and
            # /health reports vllm="disabled" instead of an outage.
            VLLM_UP.set(0)
            poll_task = None
        else:
            poll_task = asyncio.create_task(_poll_vllm(settings, vllm_client))
        probe_task = asyncio.create_task(_probe_storage(artifact_store))
        try:
            yield
        finally:
            shutdown_event.set()
            await artifact_store.close()
            await storage_browser.close()
            await chunk_runtime.close()
            for task in (poll_task, probe_task):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if preparation_tasks:
                _, outstanding = await asyncio.wait(preparation_tasks, timeout=10.0)
                if outstanding:
                    logger.warning(
                        "Shutdown: %d preparation worker(s) still running",
                        len(outstanding),
                    )
            close_pipeline = getattr(pipeline, "aclose", None)
            if close_pipeline is not None:
                await close_pipeline(timeout=10.0)
            close_client = getattr(vllm_client, "aclose", None)
            if close_client is not None:
                await close_client()

    app = FastAPI(
        title="OCR Server",
        openapi_tags=[
            {"name": name}
            for name in (
                "PARSE",
                "OCR",
                "RAG",
                "CHUNK",
                "STORAGE",
                "MONITORING",
            )
        ],
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_swagger else None,
        redoc_url="/redoc" if settings.expose_swagger else None,
        openapi_url="/openapi.json" if settings.expose_swagger else None,
    )
    app.state.artifact_store = artifact_store
    app.state.progress = progress_registry
    app.state.storage_browser = storage_browser
    register_storage_routes(app, storage_browser)

    def _capture(
        kind: str, requested: dict, *, original_filename: str | None = None
    ) -> ArtifactCapture | None:
        if not settings.storage_enabled:
            return None
        return ArtifactCapture(
            kind,
            settings,
            requested=requested,
            original_filename=(
                document_name(original_filename) if original_filename else None
            ),
        )

    async def _archive(capture, result, response, **kwargs):
        if capture is None:
            outcome = StorageOutcome("disabled")
        else:
            try:
                outcome = await artifact_store.save(capture, result, **kwargs)
            except Exception:  # noqa: BLE001 - isolate optional storage failures
                # Storage failures must not cross the OCR error boundary or
                # disclose credentials through SDK exception strings/logs.
                outcome = StorageOutcome("failed", uuid.uuid4().hex)
        response.headers.update(outcome.headers())
        if outcome.status == "failed" and isinstance(
            result, (ParseResponse, OcrResponse)
        ):
            result.warnings.append(
                make_warning(
                    "storage_failed",
                    "OCR succeeded, but its archive could not be confirmed complete.",
                )
            )

    # Serve the static frontend (frontend/dist) when present.
    _DIST = Path(__file__).resolve().parent / "frontend" / "dist"
    if settings.expose_frontend and _DIST.is_dir():

        @app.get("/assets/config.js", include_in_schema=False)
        async def _frontend_config() -> Response:
            # The token itself must never reach the browser unauthenticated:
            # the console only learns WHETHER auth is required, and the user
            # registers the key manually from the API KEY popover. Registered
            # before the /assets mount so StaticFiles doesn't swallow it.
            config = {
                "authRequired": bool(auth_tokens),
                "maxFileBytes": settings.max_file_bytes,
                "maxPages": settings.max_pages,
                "requestTimeoutSeconds": settings.request_timeout,
                "chunkTimeoutSeconds": settings.chunk_timeout_seconds,
                "chunkMaxTokens": settings.chunk_max_tokens,
                "chunkMaxInputBytes": settings.chunk_max_input_bytes,
            }
            body = f"window.OCR_CONFIG={json.dumps(config)};"
            return Response(
                content=body,
                media_type="application/javascript",
                headers={"Cache-Control": "no-store"},
            )

        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def _index() -> FileResponse:
            return FileResponse(_DIST / "index.html")

    def _check_auth(request: Request) -> None:
        if not auth_tokens:
            return
        presented = request.headers.get("Authorization", "").encode("utf-8")
        # Compare against every token so timing cannot reveal which one matched.
        label = None
        for token, token_label in auth_tokens.items():
            if secrets.compare_digest(presented, f"Bearer {token}".encode()):
                label = token_label
        if label is None:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        request.state.auth_label = label
        # Label only: no token value ever reaches a log line.
        logger.info(
            "request authenticated: %s %s %s", label, request.method, request.url.path
        )

    def _check_rate_limit(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(ip):
            REQUESTS_REJECTED.labels(reason="rate_limit").inc()
            raise HTTPException(status_code=429, detail={"code": "rate_limited"})

    @app.middleware("http")
    async def middleware(request: Request, call_next):
        """Apply shutdown, rate and auth guards and record endpoint HTTP metrics.

        Keep authenticated GET diagnostics exempt from rate limits. Mark client
        cancellation separately and disable caching for served console assets.
        """
        start = request.state.start_time = time.monotonic()
        path = request.url.path
        endpoint = (
            path.strip("/")
            if path in ("/parse", "/ocr", "/chunk", "/parse/chunks", "/office/pdf")
            else None
        )
        # The console polls /stats and /metrics every 5 s per open tab: exempt
        # them from the per-address rate limit so monitoring cannot exhaust a
        # tight budget. They stay authenticated like every other data endpoint.
        rate_exempt = request.method == "GET" and (
            path in ("/stats", "/metrics") or path.startswith("/parse/progress/")
        )
        status = "500"
        job = "-"
        try:
            try:
                if path not in PUBLIC_AUTH_PATHS and not path.startswith("/assets/"):
                    if shutdown_event.is_set() and request.method == "POST":
                        REQUESTS_REJECTED.labels(reason="shutting_down").inc()
                        raise HTTPException(503, detail={"code": "shutting_down"})
                    if not rate_exempt:
                        _check_rate_limit(request)
                    _check_auth(request)
                    if (
                        settings.archive_only
                        and request.method == "POST"
                        and path in INFERENCE_PATHS
                    ):
                        REQUESTS_REJECTED.labels(reason="archive_only").inc()
                        # Wrapped like a route error: /parse and /ocr clients
                        # read detail.code, so the refusal keeps their shape.
                        raise HTTPException(503, detail={"detail": ARCHIVE_ONLY_DETAIL})
            except HTTPException as exc:
                response = JSONResponse(status_code=exc.status_code, content=exc.detail)
            else:
                response = await call_next(request)
            status = str(response.status_code)
            job = response.headers.get("X-OCR-Job-Id", "-")
            if (
                path == "/" or path.startswith("/assets/")
            ) and path != "/assets/config.js":
                response.headers["Cache-Control"] = "no-cache"
            return response
        except asyncio.CancelledError:
            status = "499"
            raise
        finally:
            if endpoint:
                REQUESTS.labels(endpoint=endpoint, status=status).inc()
                REQUEST_DURATION.labels(endpoint=endpoint).observe(
                    time.monotonic() - start
                )
            # One audit line per operation. The path only: storage queries
            # carry archive prefixes, which include the original file name.
            if request.method in ("POST", "DELETE"):
                logger.info(
                    "operation %s %s: caller=%s status=%s pages=%s job=%s "
                    "duration=%.3fs",
                    request.method,
                    path,
                    getattr(request.state, "auth_label", "-"),
                    status,
                    getattr(request.state, "pages", "-"),
                    job,
                    time.monotonic() - start,
                )

    # CORS is opt-in for browser pages served from another origin (e.g. the
    # standalone ocr-ui opened from file://, whose origin is the literal
    # "null"). Registered after the middleware above so it becomes the
    # outermost layer and answers OPTIONS preflights before auth/rate-limit.
    if settings.cors_origins.strip():
        origins = [
            origin.strip()
            for origin in settings.cors_origins.split(",")
            if origin.strip()
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=list(STORAGE_HEADERS),
            max_age=600,
        )
    # Downloads retain their memory admission through the outer HTTP send,
    # including the middleware above and slow/disconnected clients.
    app.add_middleware(StorageDownloadMiddleware)

    @app.get("/health", status_code=200, tags=["MONITORING"])
    async def health():
        """Readiness for the mode the server runs in.

        OCR mode is healthy while vLLM answers its health poll. Archive-only
        mode has no vLLM by design (``vllm="disabled"`` is not degraded) and
        is healthy only once the archive bucket probe has succeeded: the S3
        bucket is the one upstream it depends on, so ``unknown`` (no probe
        yet) and ``error`` both report degraded.
        """
        storage = artifact_store.diagnostic()
        if settings.archive_only:
            vllm, healthy = "disabled", storage.get("status") == "ok"
        else:
            reachable = VLLM_UP._value.get() == 1.0
            vllm, healthy = ("reachable" if reachable else "unreachable"), reachable
        return {
            "status": "healthy" if healthy else "degraded",
            "mode": "archive_only" if settings.archive_only else "ocr",
            "vllm": vllm,
            "ocr_enabled": not settings.archive_only,
            "model": MODEL_NAME,
            "pipeline_version": PIPELINE_VERSION,
            "version": APP_VERSION,
            "autotune": autotune_state,
            "storage": storage,
        }

    @app.get("/stats", tags=["MONITORING"])
    async def stats():
        return {
            "queue_size": QUEUE_SIZE._value.get(),
            "active": ACTIVE_REQUESTS._value.get(),
        }

    @app.get("/metrics", response_class=PlainTextResponse, tags=["MONITORING"])
    async def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/parse/progress/{progress_id}", tags=["MONITORING"])
    async def parse_progress(progress_id: str):
        """Counters of an in-flight or just concluded /parse; no document data."""
        try:
            key = parse_progress_id(progress_id)
        except ValueError:
            raise HTTPException(422, detail={"code": "invalid_progress_id"}) from None
        entry = progress_registry.get(key) if key else None
        if entry is None:
            raise HTTPException(404, detail={"code": "progress_not_found"})
        return entry.snapshot(time.monotonic())

    @app.post(
        "/parse",
        tags=["PARSE"],
        response_model=ParseResponse,
        openapi_extra=upload_openapi(ParseJsonRequest, max_pages=settings.max_pages),
    )
    async def parse(request: Request, response: Response):
        """Validate document options and return parsed pages with optional archival.

        Normalize shared JSON/form fields once while retaining each transport's
        boolean, DPI and filename rules. Enforce explicit option ranges before
        invoking the shared parse service.
        """
        ct = request.headers.get("content-type", "")
        fields, file_bytes, upload_filename = await _upload_fields(
            request, settings, ParseJsonRequest
        )
        image_b64 = _clean_str(fields.get("image_base64"))
        pdf_b64 = _clean_str(fields.get("pdf_base64"))
        document_b64 = _clean_str(fields.get("document_base64"))
        output = _clean_str(fields.get("output")) or "both"
        max_pages = _as_int(fields.get("max_pages"), settings.max_pages)
        table_format = _clean_str(fields.get("table_format")) or "html"
        if "application/json" in ct:
            dpi_raw = _clean_str(str(fields.get("dpi") or ""))
            include_images = bool(fields.get("include_images", False))
            json_rag = bool(fields.get("json_rag", False))
            original_filename = fields.get("filename")
        else:
            dpi_raw = _clean_str(fields.get("dpi"))
            include_images = _form_flag(fields.get("include_images"))
            json_rag = _form_flag(fields.get("json_rag"))
            original_filename = upload_filename or _clean_str(fields.get("filename"))
        document = document_name(original_filename)
        try:
            progress_id = parse_progress_id(_clean_str(fields.get("progress_id")))
        except ValueError:
            raise HTTPException(422, detail={"code": "invalid_progress_id"}) from None

        if output not in ("markdown", "json", "both"):
            raise HTTPException(422, detail={"code": "invalid_output"})
        if max_pages < 1 or max_pages > settings.max_pages:
            raise HTTPException(
                422,
                detail={"code": "max_pages_out_of_range", "max": settings.max_pages},
            )
        dpi = _as_int(dpi_raw, settings.parse_dpi)
        if dpi < 50 or dpi > 400:
            raise HTTPException(
                422, detail={"code": "dpi_out_of_range", "min": 50, "max": 400}
            )
        if table_format not in ("html", "markdown"):
            raise HTTPException(422, detail={"code": "invalid_table_format"})

        capture = _capture(
            "parse",
            {
                "output": output,
                "max_pages": fields.get("max_pages"),
                "dpi": dpi_raw,
                "table_format": table_format,
                "include_images": include_images,
                "json_rag": json_rag,
                "filename": document,
            },
            original_filename=original_filename,
        )
        entry = progress_registry.register(progress_id, dpi) if progress_id else None
        disconnected, watcher = await _watch_disconnect(request)
        try:
            result = await _parse_service(
                file_bytes,
                image_b64,
                pdf_b64,
                output,
                max_pages,
                dpi,
                dpi_raw,
                table_format,
                include_images,
                request.state.start_time,
                document_b64=document_b64,
                json_rag=json_rag,
                document=document,
                capture=capture,
                progress=entry,
                should_stop=disconnected.is_set,
            )
            request.state.pages = len(result.pages)
            # A disconnected client gets no archive: nobody receives the result.
            if disconnected.is_set():
                state = "cancelled"
            else:
                await _archive(capture, result, response)
                partial = any(w.code == "pages_unprocessed" for w in result.warnings)
                state = "partial" if partial else "done"
        except BaseException:
            if entry:
                progress_registry.finish(entry, "failed")
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if entry:
            progress_registry.finish(entry, state)
        return result

    @app.post(
        "/office/pdf",
        tags=["PARSE"],
        response_class=Response,
        responses={200: {"content": {"application/pdf": {}}}},
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {
                                "file": {"type": "string", "format": "binary"}
                            },
                        }
                    }
                },
            }
        },
    )
    async def office_pdf(request: Request):
        """Office → PDF for browser previews; /parse converts on its own."""
        _, file_bytes, _ = await _upload_fields(request, settings)
        start = request.state.start_time
        admission = _Admission(parse_sem, "queue_full", preparation_tasks, QUEUE_SIZE)
        try:
            data = _decode_input(file_bytes)
            if len(data) > settings.max_file_bytes:
                REQUESTS_REJECTED.labels(reason="oversize").inc()
                raise HTTPException(413, detail={"code": "file_too_large"})
            kind = None if is_pdf(data) else office_format(data)
            if not kind:
                raise HTTPException(400, detail={"code": "not_office_document"})
            await admission.acquire(_remaining(start, settings.request_timeout))
            pdf = await admission.prepare(
                lambda: _convert_office(
                    data, kind, settings, start + settings.request_timeout
                ),
                _remaining(start, settings.request_timeout),
            )
            return Response(pdf, media_type="application/pdf")
        except TimeoutError as exc:
            raise HTTPException(504, detail={"code": "request_timeout"}) from exc
        finally:
            admission.finish()

    async def _parse_service(
        file_bytes,
        image_b64,
        pdf_b64,
        output,
        max_pages,
        dpi,
        dpi_raw,
        table_format,
        include_images,
        start,
        document_b64=None,
        json_rag=False,
        document="document",
        capture=None,
        progress: ProgressEntry | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_truncation: Callable[[int, int], None] | None = None,
    ):
        """Shared OCR execution over the document's blocks.

        One document at a time holds `document_lock` (FIFO) for its whole block
        loop; the wait counts against the request budget. Before each block the
        loop stops on `should_stop()`, when the budget is spent, or when the
        block's pages cannot finish at the per-page pace measured so far. While
        a block runs, `should_stop()` is polled and interrupts it, so the
        backend closes its stream and frees the GPU. An error page returned
        after the deadline, and every page never reached, becomes
        `page_unprocessed` with one `pages_unprocessed` warning; an error page
        with budget left stays `page_failed` and the loop continues. Delivered
        pages update `progress` as they arrive.
        """
        admission = _Admission(parse_sem, "queue_full", preparation_tasks, QUEUE_SIZE)
        plan = None
        try:
            data = _decode_input(file_bytes, image_b64, pdf_b64, document_b64)
            if len(data) > settings.max_file_bytes:
                REQUESTS_REJECTED.labels(reason="oversize").inc()
                raise HTTPException(413, detail={"code": "file_too_large"})

            await admission.acquire(_remaining(start, settings.request_timeout))
            if capture is not None:
                capture.effective.update(
                    {
                        "output": output,
                        "max_pages": max_pages,
                        "table_format": table_format,
                        "include_images": include_images,
                        "json_rag": json_rag,
                    }
                )
            plan, warnings = await admission.prepare(
                lambda: _prepare_document(
                    data,
                    dpi,
                    max_pages,
                    dpi_raw is not None,
                    settings,
                    capture,
                    start + settings.request_timeout,
                    on_truncation,
                ),
                _remaining(start, settings.request_timeout),
            )
            if progress:
                progress.dpi = plan.dpi  # None for images: dpi does not apply
                progress.plan(plan.pages_total, len(plan.blocks))
            pages: list[PageResult] = []
            try:
                deadline = start + settings.request_timeout
                results: list[dict | None] = [None] * plan.pages_total
                reason = "page_unprocessed: request time budget exhausted"
                await asyncio.wait_for(
                    document_lock.acquire(),
                    _remaining(start, settings.request_timeout),
                )
                try:
                    durations: list[float] = []
                    pages_reached = 0
                    for index, block in enumerate(plan.blocks, 1):
                        if should_stop and should_stop():
                            reason = "page_unprocessed: request cancelled"
                            break
                        began = time.monotonic()
                        remaining = deadline - began
                        # Blocks differ in size: skip one only when its pages,
                        # at the pace measured so far, cannot finish in time.
                        if remaining <= 0 or (
                            pages_reached
                            and remaining < sum(durations) / pages_reached * len(block)
                        ):
                            break
                        if progress:
                            progress.begin_block(index, len(block))
                        try:
                            images = await admission.prepare(
                                lambda block=block: plan.render(block), remaining
                            )
                        except TimeoutError:
                            break
                        budget = deadline - time.monotonic()
                        if budget <= 0:
                            _close_images(images)
                            break
                        if progress:
                            progress.set_state("ocr")
                        # One streaming call per block: paddleocr's queued pipeline
                        # overlaps layout of one 8-page batch with VL of the last;
                        # delivered pages count towards progress as they arrive.
                        streamed = (
                            {"on_page": lambda _: progress.page_done()}
                            if progress
                            else {}
                        )
                        call = asyncio.ensure_future(
                            pipeline.parse_document(
                                images,
                                settings.vl_lanes,
                                settings.page_timeout,
                                budget,
                                close_images=True,
                                **streamed,
                            )
                        )
                        # Cancel reacts within the block: interrupting the call
                        # makes the backend close its stream and free the GPU.
                        interrupted = False
                        while should_stop and not call.done():
                            await asyncio.wait({call}, timeout=0.5)
                            if should_stop() and not call.done():
                                interrupted = True
                                call.cancel()
                        try:
                            block_results = await call
                        except asyncio.CancelledError:
                            if not interrupted:
                                raise
                            reason = "page_unprocessed: request cancelled"
                            break
                        expired = time.monotonic() >= deadline
                        failed = lost = 0
                        for page, result in zip(block, block_results):
                            if result.get("error") and expired:
                                lost += 1
                                continue
                            results[page] = result
                            failed += bool(result.get("error"))
                        durations.append(time.monotonic() - began)
                        pages_reached += len(block)
                        if progress:
                            # Delivered pages were already counted by on_page.
                            progress.end_block(0, failed, lost)
                finally:
                    document_lock.release()
                unprocessed = [i + 1 for i, r in enumerate(results) if r is None]
                if progress:
                    # Pages of blocks never completed were not counted yet.
                    progress.end_block(0, 0, plan.pages_total - pages_reached)
                    progress.set_state("finalizing")
                results = [
                    r or {"error": reason, "elapsed": 0.0, "unprocessed": True}
                    for r in results
                ]
                tables_spans_expanded = 0
                usage_tokenizer: str | None = None
                usage_tokens = 0
                usage_blocks = 0
                rag_pages: list[dict] = []
                for i, result in enumerate(results):
                    if result.get("error"):
                        pages.append(PageResult(page=i + 1, error=result["error"]))
                        rag_page = {"page": i + 1, "error": result["error"]}
                        if result.get("unprocessed"):
                            # Covered by the single pages_unprocessed warning.
                            rag_pages.append({**rag_page, "unprocessed": True})
                            continue
                        rag_pages.append(rag_page)
                        warnings.append(
                            make_warning(
                                "page_failed",
                                result["error"],
                                target={"field": "page", "index": i},
                            )
                        )
                        continue
                    page_usage = token_counter.annotate(result["blocks"])
                    if page_usage is not None:
                        usage_tokenizer = page_usage.tokenizer
                        usage_tokens += page_usage.content_tokens
                        usage_blocks += page_usage.blocks
                    if json_rag:
                        rag_pages.append(
                            {
                                "page": i + 1,
                                "blocks": [b.model_dump() for b in result["blocks"]],
                            }
                        )
                    normalized_md, expanded = MarkdownNormalizer.normalize(
                        result["markdown"], table_format, blocks=result["blocks"]
                    )
                    tables_spans_expanded += expanded
                    page_md = normalized_md if output in ("markdown", "both") else None
                    if capture is not None:
                        try:
                            capture.page(
                                i + 1,
                                normalized_md,
                                result.get("images", []),
                            )
                        except Exception:  # noqa: BLE001 - isolate optional capture
                            capture.fail()
                    embedded_images, image_names = _extract_images(
                        result.get("images", []),
                        include_images,
                        settings.max_embedded_image_bytes,
                        warnings,
                        i + 1,
                    )
                    pages.append(
                        PageResult(
                            page=i + 1,
                            width=result.get("width"),
                            height=result.get("height"),
                            markdown=page_md,
                            blocks=result["blocks"]
                            if output in ("json", "both")
                            else None,
                            images=embedded_images,
                            image_names=image_names,
                        )
                    )
                    PAGES_PROCESSED.inc()
                    PAGE_DURATION.observe(result.get("elapsed", 0.0))
                if unprocessed:
                    warnings.append(
                        make_warning(
                            "pages_unprocessed",
                            f"{len(unprocessed)} page(s) were not processed before "
                            "the request ended.",
                            target={"field": "file"},
                            details={"pages": unprocessed},
                        )
                    )
                if tables_spans_expanded:
                    warnings.append(
                        make_warning(
                            "tables_spans_expanded",
                            f"{tables_spans_expanded} table(s) had merged cells "
                            "(rowspan/colspan) expanded into repeated values "
                            "for pipe markdown.",
                            severity="info",
                            target={"field": "table_format"},
                        )
                    )
            finally:
                plan.close()

            elapsed_ms = (time.monotonic() - start) * 1000
            usage = (
                TokenUsage(
                    tokenizer=usage_tokenizer,
                    content_tokens=usage_tokens,
                    blocks=usage_blocks,
                )
                if usage_tokenizer
                else None
            )
            rag = None
            if json_rag:
                # processing_time_s is OCR time; the response time below is
                # re-measured so it also covers building this export.
                try:
                    rag = build_json_rag(
                        rag_pages,
                        document=document,
                        table_format=table_format,
                        count=token_counter.count,
                        info={
                            "model_name": MODEL_NAME,
                            "pipeline_version": PIPELINE_VERSION,
                            "tokenizer": usage_tokenizer,
                            "processing_time_s": round(elapsed_ms / 1000, 3),
                            "dpi": dpi if is_pdf(data) or office_format(data) else None,
                            "table_format": table_format,
                            "warnings": [warning.code for warning in warnings],
                        },
                    )
                except Exception:
                    logger.exception("json_rag export failed")
                    warnings.append(
                        make_warning(
                            "json_rag_failed",
                            "The json_rag export could not be built; OCR results "
                            "are unaffected.",
                            target={"field": "json_rag"},
                        )
                    )
            return ParseResponse(
                pages=pages,
                processing_time_ms=(time.monotonic() - start) * 1000,
                warnings=warnings,
                usage=usage,
                json_rag=rag,
            )
        except (HTTPException, ChunkError):
            raise
        except TimeoutError as exc:
            raise HTTPException(504, detail={"code": "request_timeout"}) from exc
        except PdfTooLargeError as exc:
            raise HTTPException(
                413, detail={"code": "pdf_pixel_limit", "message": str(exc)}
            ) from exc
        except PdfError as exc:
            raise HTTPException(
                400, detail={"code": "corrupt_pdf", "message": str(exc)}
            )
        except BackendUnavailable as exc:
            code = "backend_unavailable" if exc.status_code == 503 else "backend_error"
            raise HTTPException(exc.status_code, detail={"code": code}) from exc
        except Exception:
            logger.exception("parse error")
            raise HTTPException(500, detail={"code": "internal_error"})
        finally:
            admission.finish()

    @app.post(
        "/ocr",
        tags=["OCR"],
        response_model=OcrResponse,
        openapi_extra=upload_openapi(OcrJsonRequest, max_pages=settings.max_pages),
    )
    async def ocr(request: Request, response: Response):
        """Recognize one image element under slot, memory and timeout admission.

        Preserve normalized input and spotting warnings, translate controlled
        backend failures and release admission before best-effort archival.
        """
        ct = request.headers.get("content-type", "")
        fields, file_bytes, upload_filename = await _upload_fields(
            request, settings, OcrJsonRequest
        )
        image_b64 = _clean_str(fields.get("image_base64"))
        task = _clean_str(fields.get("task")) or "ocr"
        if "application/json" in ct:
            original_filename = fields.get("filename")
        else:
            original_filename = upload_filename or _clean_str(fields.get("filename"))

        if task not in ELEMENT_TASKS:
            raise HTTPException(422, detail={"code": "invalid_task", "task": task})

        start = request.state.start_time
        admission = _Admission(ocr_sem, "slots_full", preparation_tasks)
        capture = _capture("ocr", {"task": task}, original_filename=original_filename)
        if capture is not None:
            capture.effective["task"] = task
        try:
            data = _decode_input(file_bytes, image_b64)
            if len(data) > settings.max_file_bytes:
                REQUESTS_REJECTED.labels(reason="oversize").inc()
                raise HTTPException(413, detail={"code": "file_too_large"})

            await admission.acquire(_remaining(start, settings.request_timeout))
            img_b64 = await admission.prepare(
                lambda: _prepare_element(
                    data,
                    task,
                    capture,
                    # Reserve copies and encoding too; spotting can quadruple pixels.
                    lambda image: admission.reserve_memory(
                        ocr_memory,
                        image.width
                        * image.height
                        * 32
                        * (
                            4
                            if task == "spotting"
                            and image.width < SPOTTING_UPSCALE_THRESHOLD
                            and image.height < SPOTTING_UPSCALE_THRESHOLD
                            else 1
                        ),
                    ),
                ),
                _remaining(start, settings.request_timeout),
            )
            budget = _remaining(start, settings.request_timeout)
            try:
                completion = await asyncio.wait_for(
                    vllm_client.recognize(img_b64, task, budget),
                    timeout=budget,
                )
            except BackendUnavailable as exc:
                code = (
                    "backend_unavailable" if exc.status_code == 503 else "backend_error"
                )
                raise HTTPException(exc.status_code, detail={"code": code}) from exc

            result = completion.text
            boxes, incomplete, invalid = (
                parse_spotting_output(result) if task == "spotting" else (None, 0, 0)
            )
            warnings = []
            if completion.finish_reason == "length":
                warnings.append(
                    make_warning(
                        "generation_truncated",
                        (
                            "Generation stopped at a token limit; result may be"
                            " incomplete."
                        ),
                        target={"field": "result"},
                        details={"finish_reason": "length"},
                    )
                )
            if invalid:
                warnings.append(
                    make_warning(
                        "spotting_invalid_coordinates",
                        (
                            "Invalid geometries were discarded; recognized text"
                            " is preserved."
                        ),
                        details={"invalid_boxes": invalid},
                    )
                )
            if incomplete:
                warnings.append(
                    make_warning(
                        "spotting_incomplete_coordinates",
                        "Some location tokens could not form a complete quadrilateral.",
                        details={"incomplete_tokens": incomplete},
                    )
                )
            elapsed_ms = (time.monotonic() - start) * 1000
            result = OcrResponse(
                result=result,
                task=task,
                boxes=boxes,
                processing_time_ms=elapsed_ms,
                warnings=warnings,
            )
        except HTTPException:
            raise
        except (TimeoutError, APITimeoutError) as exc:
            raise HTTPException(504, detail={"code": "request_timeout"}) from exc
        except Exception:
            logger.exception("ocr error")
            raise HTTPException(500, detail={"code": "internal_error"})
        finally:
            admission.finish()
        await _archive(capture, result, response)
        return result

    chunk_schemas = register_chunk_routes(
        app,
        settings,
        _parse_service,
        chunk_runtime,
        capture_factory=_capture,
        archive=_archive,
    )
    original_openapi = app.openapi

    def documented_openapi() -> dict:
        """Expose the deployment's bearer requirement in Swagger and clients."""
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("schemas", {}).update(
            chunk_schemas
        )
        for path in ("/parse", "/ocr", "/parse/chunks"):
            success = schema["paths"][path]["post"]["responses"]["200"]
            success["headers"] = STORAGE_HEADERS
        if auth_tokens:
            schemes = schema.setdefault("components", {}).setdefault(
                "securitySchemes", {}
            )
            schemes["BearerAuth"] = {"type": "http", "scheme": "bearer"}
            for path, method in (
                ("/stats", "get"),
                ("/metrics", "get"),
                ("/parse/progress/{progress_id}", "get"),
                ("/parse", "post"),
                ("/ocr", "post"),
                ("/chunk", "post"),
                ("/parse/chunks", "post"),
                ("/storage/archives", "get"),
                ("/storage/archive", "get"),
                ("/storage/archive.zip", "get"),
                ("/storage/archive", "delete"),
                ("/storage/file", "get"),
                ("/storage/file", "delete"),
                ("/storage/rag/refine", "post"),
                ("/storage/rag/split", "post"),
            ):
                schema["paths"][path][method]["security"] = [{"BearerAuth": []}]
        return schema

    app.openapi = documented_openapi
    return app


def _extract_images(
    image_assets: list[dict],
    include_images: bool,
    max_bytes: int,
    warnings: list,
    page_num: int,
) -> tuple[list[str] | None, list[str] | None]:
    """Return Paddle's real figure crops as base64 PNG plus matching names."""
    if not include_images:
        return None, None
    images: list[str] = []
    names: list[str] = []
    for asset in image_assets:
        try:
            content_bytes = asset["png"]
            if not isinstance(content_bytes, bytes):
                continue
            if len(content_bytes) > max_bytes:
                warnings.append(
                    make_warning(
                        "images_truncated",
                        f"Figure crop on page {page_num} exceeds "
                        f"{max_bytes} bytes — skipped.",
                        target={"field": "page", "page": page_num},
                        details={
                            "name": str(asset.get("name", "")),
                            "bytes": len(content_bytes),
                            "max": max_bytes,
                        },
                    )
                )
                continue
            images.append(base64.b64encode(content_bytes).decode())
            names.append(str(asset.get("name", f"page-{page_num}-image.png")))
        except (KeyError, TypeError, ValueError):
            continue
    if not images:
        return None, None
    return images, names


def _decode_input(file_bytes: bytes | None, *encoded: str | None) -> bytes:
    supplied = sum(value is not None for value in (file_bytes, *encoded))
    if supplied != 1:
        raise HTTPException(400, detail={"code": "invalid_input_count"})
    if file_bytes is not None:
        return file_bytes
    try:
        return base64.b64decode(
            next(value for value in encoded if value is not None), validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, detail={"code": "invalid_base64"}) from exc


async def _probe_storage(artifact_store, interval: float = 60.0) -> None:
    """Keep /health storage status current between archive writes."""
    probe = getattr(artifact_store, "probe", None)
    if probe is None:
        return
    while True:
        await probe()
        await asyncio.sleep(interval)


async def _poll_vllm(settings: Settings, vllm_client) -> None:
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    settings.vllm_url.rstrip("/").removesuffix("/v1") + "/health",
                    timeout=5.0,
                )
                VLLM_UP.set(1 if resp.status_code == 200 else 0)
        except httpx.HTTPError:
            VLLM_UP.set(0)
        await asyncio.sleep(10)


_profile = _load_and_validate_profile()
app = create_app(load_settings(os.environ, _profile), profile=_profile)
