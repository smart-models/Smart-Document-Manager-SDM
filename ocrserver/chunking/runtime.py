"""Bounded CPU workers with asynchronous IPC and actual process cancellation."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass, fields
from time import monotonic
from typing import Any

from ocrserver.chunking.canonical import canonical_bytes
from ocrserver.chunking.errors import ChunkError
from ocrserver.chunking.tokenizer import PaddleTokenizer
from ocrserver.chunking.telemetry import ChunkMetrics, default_metrics
from ocrserver.chunking.types import ChunkLimits
from ocrserver.config import Settings

Worker = Callable[[dict, dict | None, PaddleTokenizer, ChunkLimits], dict]
_FRAME = struct.Struct("!BQ")
_ERROR_LIMIT = 65_536
_IO_BLOCK = 65_536


def execute_chunk_request(
    request: dict,
    scope: dict | None,
    tokenizer: PaddleTokenizer,
    limits: ChunkLimits,
    *,
    telemetry=None,
) -> dict:
    """Public worker seam; only the configured local tokenizer is available."""
    from ocrserver.chunking.engine import chunk_document
    from ocrserver.schemas import ChunkRequest

    parsed = ChunkRequest.model_validate(request)
    return chunk_document(
        request["parsed_document"],
        parsed.options,
        tokenizer,
        source_id=parsed.source_id,
        lang=parsed.lang,
        accept_truncated=parsed.accept_truncated,
        scope=scope,
        limits=limits,
        observer=telemetry,
    )


def limits_from_settings(settings: Settings) -> ChunkLimits:
    """Resolve every engine guard from centralized administrative settings."""
    return ChunkLimits(
        **{
            field.name: getattr(settings, "chunk_" + field.name)
            for field in fields(ChunkLimits)
        }
    )


def _blocking_read(sock: socket.socket, count: int) -> bytes:
    parts = []
    remaining = count
    while remaining:
        part = sock.recv(min(remaining, _IO_BLOCK))
        if not part:
            raise EOFError("worker connection closed")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def _send(sock: socket.socket, status: int, payload: bytes) -> None:
    sock.sendall(_FRAME.pack(status, len(payload)))
    sock.sendall(payload)


def _worker_main(
    sock: socket.socket,
    cache_dir: str,
    revision: str,
    limits: ChunkLimits,
    worker: Worker,
) -> None:
    """Child loop owns its tokenizer and never receives API credentials."""
    sock.setblocking(True)
    tokenizer = PaddleTokenizer(cache_dir, revision)
    try:
        while True:
            status, size = _FRAME.unpack(_blocking_read(sock, _FRAME.size))
            if status != 0 or size > limits.max_input_bytes + _ERROR_LIMIT:
                break
            body = json.loads(_blocking_read(sock, size))
            close_after_error = False
            statistics = {}
            try:
                arguments = (body["request"], body["scope"], tokenizer, limits)
                result = (
                    worker(*arguments, telemetry=statistics.update)
                    if worker is execute_chunk_request
                    else worker(*arguments)
                )
                payload = canonical_bytes(result)
                if len(payload) > limits.max_output_bytes:
                    raise ChunkError(
                        "chunk_output_limit",
                        "RAG output exceeds its byte limit.",
                        status_code=413,
                        details={"maximum": limits.max_output_bytes},
                    )
                payload = canonical_bytes(
                    {
                        "result": result,
                        "statistics": statistics,
                    }
                )
                if len(payload) > limits.max_output_bytes + _ERROR_LIMIT:
                    raise ChunkError(
                        "chunk_output_limit",
                        "Worker IPC frame is too large.",
                        status_code=413,
                    )
            except ChunkError as exc:
                payload = canonical_bytes(
                    {
                        "status_code": exc.status_code,
                        **exc.to_detail(),
                        "statistics": statistics,
                    }
                )
                if len(payload) > _ERROR_LIMIT:
                    payload = canonical_bytes(
                        {
                            "status_code": exc.status_code,
                            "code": exc.code,
                            "message": "RAG operation failed.",
                            "details": {},
                        }
                    )
                _send(sock, 1, payload)
                close_after_error = exc.code == "tokenizer_unavailable"
            except Exception:
                _send(
                    sock,
                    1,
                    canonical_bytes(
                        {
                            "status_code": 500,
                            "code": "chunk_worker_failed",
                            "message": "The RAG worker failed.",
                            "details": {},
                            "statistics": statistics,
                        }
                    ),
                )
                close_after_error = True
            else:
                _send(sock, 0, payload)
            # Keep only the tokenizer between requests, not the last document.
            body = result = payload = arguments = statistics = None
            if close_after_error:
                break
    except (EOFError, OSError, ValueError):
        pass
    finally:
        sock.close()


@dataclass
class _Slot:
    process: Any = None
    socket: socket.socket | None = None
    reusable: bool = False


class ChunkRuntime:
    """Persistent spawn workers admitted independently from OCR inference.

    ``worker`` is an optional top-level callable for isolated tests. The
    production default performs the actual pure chunking operation. Run and
    close the runtime from one asyncio event loop.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        worker: Worker = execute_chunk_request,
        telemetry: ChunkMetrics | None = None,
    ) -> None:
        """Allocate admission slots without starting workers or loading models.

        Args:
            settings: Worker capacity, queue, timeout and chunking work limits.
            worker: Spawn-compatible callable used to execute an admitted job.
            telemetry: Optional collector; defaults to process-wide metrics.
        """
        self._settings = settings
        self._worker = worker
        self._telemetry = telemetry if telemetry is not None else default_metrics()
        self._limits = limits_from_settings(settings)
        self._slots = [_Slot() for _ in range(settings.chunk_max_workers)]
        self._available: asyncio.Queue = asyncio.Queue()
        for slot in self._slots:
            self._available.put_nowait(slot)
        self._closed = False
        self._runs: set[asyncio.Task] = set()

    async def run(self, request: dict, *, scope: dict | None = None) -> dict:
        """Return one complete result; no partial file is exposed on failure."""
        started = monotonic()
        outcome = "success"
        try:
            return await self._run(request, scope=scope)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except ChunkError as exc:
            outcome = exc.code
            raise
        except Exception:
            outcome = "chunk_worker_failed"
            raise
        finally:
            self._telemetry.record_outcome(outcome, monotonic() - started)

    async def _run(self, request: dict, *, scope: dict | None = None) -> dict:
        """Validate transport size and execute one admitted job within its deadline.

        Track active callers, replace unavailable workers and reap a worker after
        timeout or cancellation before returning its admission slot to the queue.
        """
        if self._closed:
            raise ChunkError(
                "chunk_capacity_exceeded",
                "The RAG runtime is closed.",
                status_code=503,
            )
        payload = canonical_bytes({"request": request, "scope": scope})
        # The framing allowance is for labels/scope, never additional OCR text.
        if (
            "parsed_document" in request
            and len(canonical_bytes(request["parsed_document"]))
            > self._limits.max_input_bytes
        ):
            raise ChunkError(
                "chunk_input_limit",
                "Structured OCR exceeds its byte limit.",
                status_code=413,
                details={"maximum": self._limits.max_input_bytes},
            )
        if len(payload) > self._limits.max_input_bytes + _ERROR_LIMIT:
            raise ChunkError(
                "chunk_input_limit",
                "RAG request exceeds its transport limit.",
                status_code=413,
                details={"maximum": self._limits.max_input_bytes},
            )
        task = asyncio.current_task()
        self._runs.add(task)
        slot = None
        try:
            slot = await self._admit()
            try:
                async with asyncio.timeout(self._settings.chunk_timeout_seconds):
                    if slot.process is None or not slot.process.is_alive():
                        await self._stop(slot)
                        self._start(slot)
                    return await self._exchange(slot, payload)
            except TimeoutError as exc:
                slot.reusable = False
                raise ChunkError(
                    "chunk_timeout",
                    "RAG chunking exceeded its time limit.",
                    status_code=504,
                ) from exc
        except asyncio.CancelledError:
            if slot is not None:
                slot.reusable = False
            raise
        finally:
            if slot is not None:
                if not slot.reusable:
                    await self._reap(slot)
                self._available.put_nowait(slot)
            self._runs.discard(task)

    async def close(self) -> None:
        """Cancel admitted requests and join all workers before returning."""
        self._closed = True
        runs = [task for task in self._runs if task is not asyncio.current_task()]
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)
        await asyncio.gather(*(self._reap(slot) for slot in self._slots))

    async def _admit(self) -> _Slot:
        try:
            if self._settings.chunk_queue_wait_seconds == 0:
                return self._available.get_nowait()
            return await asyncio.wait_for(
                self._available.get(),
                self._settings.chunk_queue_wait_seconds,
            )
        except (TimeoutError, asyncio.QueueEmpty) as exc:
            raise ChunkError(
                "chunk_capacity_exceeded",
                "RAG CPU capacity is currently full.",
                status_code=503,
            ) from exc

    def _start(self, slot: _Slot) -> None:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        process = multiprocessing.get_context("spawn").Process(
            target=_worker_main,
            args=(
                child,
                self._settings.hf_cache_dir,
                self._settings.vllm_model_revision,
                self._limits,
                self._worker,
            ),
            daemon=True,
        )
        slot.socket = parent
        slot.process = process
        try:
            process.start()
            slot.reusable = True
        except BaseException:
            parent.close()
            process.close()
            slot.socket = None
            slot.process = None
            raise
        finally:
            child.close()

    async def _read(self, sock: socket.socket, count: int) -> bytes:
        loop = asyncio.get_running_loop()
        parts = []
        remaining = count
        while remaining:
            part = await loop.sock_recv(sock, min(remaining, _IO_BLOCK))
            if not part:
                raise EOFError("worker connection closed")
            parts.append(part)
            remaining -= len(part)
        return b"".join(parts)

    async def _exchange(self, slot: _Slot, payload: bytes) -> dict:
        """Exchange one bounded socket frame and translate structured worker errors.

        Mark the slot reusable only after a complete valid response. Transport
        failures and tokenizer initialization failures require worker replacement.
        """
        slot.reusable = False
        sock = slot.socket
        loop = asyncio.get_running_loop()
        try:
            await loop.sock_sendall(sock, _FRAME.pack(0, len(payload)))
            await loop.sock_sendall(sock, payload)
            status, size = _FRAME.unpack(await self._read(sock, _FRAME.size))
            maximum = (
                self._limits.max_output_bytes + _ERROR_LIMIT
                if status == 0
                else _ERROR_LIMIT
            )
            if status not in (0, 1) or size > maximum:
                raise ChunkError(
                    "chunk_output_limit",
                    "Worker response exceeds its byte limit.",
                    status_code=413,
                    details={"maximum": maximum},
                )
            result = json.loads(await self._read(sock, size))
        except (EOFError, OSError, ValueError) as exc:
            raise ChunkError(
                "chunk_worker_failed",
                "The RAG worker exited unexpectedly.",
                status_code=500,
            ) from exc
        slot.reusable = status == 0 or result["code"] not in {
            "tokenizer_unavailable",
            "chunk_worker_failed",
        }
        self._telemetry.record_statistics(result.get("statistics", {}))
        if status == 1:
            raise ChunkError(
                result["code"],
                result["message"],
                status_code=result["status_code"],
                details=result["details"],
            )
        return result["result"]

    async def _reap(self, slot: _Slot) -> None:
        """Repeated client cancellation cannot abandon a process being reaped."""
        cleanup = asyncio.create_task(self._stop(slot))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    async def _stop(self, slot: _Slot) -> None:
        slot.reusable = False
        if slot.socket is not None:
            slot.socket.close()
            slot.socket = None
        process = slot.process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
        deadline = asyncio.get_running_loop().time() + 0.2
        while process.is_alive():
            if asyncio.get_running_loop().time() >= deadline:
                process.kill()
            await asyncio.sleep(0.01)
        process.join()
        process.close()
        slot.process = None
