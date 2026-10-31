"""Real spawned workers prove capacity is released after CPU work stops."""

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from ocrserver.chunking.errors import ChunkError
from ocrserver.chunking.runtime import ChunkRuntime
from ocrserver.config import Settings


def runtime_worker(request, scope, tokenizer, limits):
    """Public worker seam; runs in a real process without an OCR model."""
    if request.get("mode") == "stall":
        while True:
            time.sleep(0.1)
    if request.get("mode") == "domain_error":
        raise ChunkError("unsplittable_content", "Too large.")
    if request.get("mode") == "unavailable":
        raise ChunkError("tokenizer_unavailable", "Cache unavailable.", status_code=503)
    if request.get("mode") == "crash":
        os._exit(17)
    if request.get("mode") == "payload":
        return {"value": "x" * request["size"]}
    if request.get("mode") == "hold":
        time.sleep(0.2)
    return {"pid": os.getpid(), "scope": scope, "value": request.get("value")}


async def test_worker_runs_outside_api_process_and_is_reused_then_joined():
    runtime = ChunkRuntime(Settings(chunk_max_workers=1), worker=runtime_worker)
    try:
        first = await asyncio.wait_for(
            runtime.run({"value": "first"}, scope={"kind": "provided"}), 10
        )
        assert first["value"] == "first"
        assert first["scope"] == {"kind": "provided"}
        assert first["pid"] != os.getpid()
        second = await asyncio.wait_for(runtime.run({"value": "second"}), 10)
        assert second["pid"] == first["pid"]
        assert second["scope"] is None
    finally:
        await runtime.close()
    assert not psutil.pid_exists(first["pid"])


async def test_timeout_joins_stalled_worker_before_a_new_request_gets_capacity(
    monkeypatch,
):
    settings = Settings(chunk_max_workers=1, chunk_timeout_seconds=10)
    runtime = ChunkRuntime(settings, worker=runtime_worker)
    try:
        first = await asyncio.wait_for(runtime.run({}), 10)
        # Exercise the short deadline on warm work, not cold process imports.
        monkeypatch.setattr(
            runtime, "_settings", replace(settings, chunk_timeout_seconds=2)
        )
        with pytest.raises(ChunkError) as raised:
            await asyncio.wait_for(runtime.run({"mode": "stall"}), 5)
        assert (raised.value.code, raised.value.status_code) == (
            "chunk_timeout",
            504,
        )
        assert not psutil.pid_exists(first["pid"])
        monkeypatch.setattr(runtime, "_settings", settings)
        second = await asyncio.wait_for(runtime.run({}), 10)
        assert second["pid"] != first["pid"]
    finally:
        await runtime.close()


async def test_full_capacity_waits_only_the_configured_interval():
    runtime = ChunkRuntime(
        Settings(
            chunk_max_workers=1,
            chunk_queue_wait_seconds=0.05,
        ),
        worker=runtime_worker,
    )
    stalled = None
    try:
        first = await asyncio.wait_for(runtime.run({}), 10)
        stalled = asyncio.create_task(runtime.run({"mode": "stall"}))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(ChunkError) as raised:
            await asyncio.wait_for(runtime.run({}), 1)
        assert (raised.value.code, raised.value.status_code) == (
            "chunk_capacity_exceeded",
            503,
        )
        assert 0.03 <= time.monotonic() - started < 0.5
    finally:
        await runtime.close()
        if stalled is not None:
            await asyncio.gather(stalled, return_exceptions=True)
    assert not psutil.pid_exists(first["pid"])


async def test_client_cancellation_joins_worker_before_returning():
    runtime = ChunkRuntime(Settings(chunk_max_workers=1), worker=runtime_worker)
    try:
        first = await asyncio.wait_for(runtime.run({}), 10)
        stalled = asyncio.create_task(runtime.run({"mode": "stall"}))
        await asyncio.sleep(0.05)
        stalled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stalled, 5)
        assert not psutil.pid_exists(first["pid"])
        next_result = await asyncio.wait_for(runtime.run({}), 10)
        assert next_result["pid"] != first["pid"]
    finally:
        await runtime.close()


async def test_domain_error_keeps_the_healthy_worker_and_loaded_tokenizer():
    runtime = ChunkRuntime(Settings(chunk_max_workers=1), worker=runtime_worker)
    try:
        first = await asyncio.wait_for(runtime.run({}), 10)
        with pytest.raises(ChunkError) as raised:
            await asyncio.wait_for(runtime.run({"mode": "domain_error"}), 5)
        assert raised.value.code == "unsplittable_content"
        next_result = await asyncio.wait_for(runtime.run({}), 10)
        assert next_result["pid"] == first["pid"]
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "mode,code",
    [
        ("unavailable", "tokenizer_unavailable"),
        ("crash", "chunk_worker_failed"),
    ],
)
async def test_unavailable_or_crashed_worker_is_replaced(mode, code):
    runtime = ChunkRuntime(Settings(chunk_max_workers=1), worker=runtime_worker)
    try:
        first = await asyncio.wait_for(runtime.run({}), 10)
        with pytest.raises(ChunkError) as raised:
            await asyncio.wait_for(runtime.run({"mode": mode}), 5)
        assert raised.value.code == code
        assert not psutil.pid_exists(first["pid"])
        second = await asyncio.wait_for(runtime.run({}), 10)
        assert second["pid"] != first["pid"]
    finally:
        await runtime.close()


async def test_output_byte_limit_is_inclusive_and_does_not_truncate():
    runtime = ChunkRuntime(
        Settings(
            chunk_max_workers=1,
            chunk_max_output_bytes=64,
        ),
        worker=runtime_worker,
    )
    try:
        result = await asyncio.wait_for(
            runtime.run({"mode": "payload", "size": 52}), 10
        )
        assert result == {"value": "x" * 52}
        with pytest.raises(ChunkError) as raised:
            await asyncio.wait_for(runtime.run({"mode": "payload", "size": 53}), 5)
        assert (raised.value.code, raised.value.status_code) == (
            "chunk_output_limit",
            413,
        )
    finally:
        await runtime.close()


async def test_full_64_mib_result_crosses_ipc_without_pipe_deadlock():
    runtime = ChunkRuntime(Settings(chunk_max_workers=1), worker=runtime_worker)
    try:
        result = await asyncio.wait_for(
            runtime.run(
                {
                    "mode": "payload",
                    "size": 67_108_864 - 12,
                }
            ),
            30,
        )
        assert len(result["value"]) == 67_108_852
        assert result["value"].startswith("xxx") and result["value"].endswith("xxx")
    finally:
        await runtime.close()


async def test_close_cancels_running_and_queued_calls_then_rejects_new_work():
    runtime = ChunkRuntime(
        Settings(
            chunk_max_workers=1,
            chunk_queue_wait_seconds=10,
        ),
        worker=runtime_worker,
    )
    first = await asyncio.wait_for(runtime.run({}), 10)
    stalled = asyncio.create_task(runtime.run({"mode": "stall"}))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(runtime.run({}))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(runtime.close(), 5)
    outcomes = await asyncio.gather(stalled, queued, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert not psutil.pid_exists(first["pid"])
    with pytest.raises(ChunkError) as raised:
        await runtime.run({})
    assert raised.value.code == "chunk_capacity_exceeded"


async def test_zero_wait_accepts_free_capacity_and_refuses_saturated_capacity():
    runtime = ChunkRuntime(
        Settings(
            chunk_max_workers=1,
            chunk_queue_wait_seconds=0,
        ),
        worker=runtime_worker,
    )
    stalled = None
    try:
        await asyncio.wait_for(runtime.run({}), 10)
        stalled = asyncio.create_task(runtime.run({"mode": "stall"}))
        await asyncio.sleep(0.05)
        with pytest.raises(ChunkError) as raised:
            await asyncio.wait_for(runtime.run({}), 1)
        assert raised.value.code == "chunk_capacity_exceeded"
    finally:
        await runtime.close()
        if stalled is not None:
            await asyncio.gather(stalled, return_exceptions=True)


async def test_two_admitted_requests_use_two_separate_cpu_workers():
    runtime = ChunkRuntime(Settings(chunk_max_workers=2), worker=runtime_worker)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                runtime.run({"mode": "hold", "value": "one"}),
                runtime.run({"mode": "hold", "value": "two"}),
            ),
            10,
        )
        assert len({result["pid"] for result in results}) == 2
        assert [result["value"] for result in results] == ["one", "two"]
    finally:
        await runtime.close()
    assert all(not psutil.pid_exists(result["pid"]) for result in results)


async def test_input_limit_does_not_allow_ocr_text_inside_framing_allowance():
    runtime = ChunkRuntime(
        Settings(
            chunk_max_workers=1,
            chunk_max_input_bytes=16,
        ),
        worker=runtime_worker,
    )
    try:
        with pytest.raises(ChunkError) as raised:
            await runtime.run({"parsed_document": {"text": "too large"}})
        assert (raised.value.code, raised.value.status_code) == (
            "chunk_input_limit",
            413,
        )
    finally:
        await runtime.close()


LOCAL_CACHE = Path(".scratch/ocr-rag-implementation/tokenizer-cache")


@pytest.mark.skipif(not LOCAL_CACHE.is_dir(), reason="local Paddle cache unavailable")
async def test_real_paddle_worker_reproduces_the_normative_export():
    from ocrserver.chunking.canonical import canonical_bytes

    runtime = ChunkRuntime(
        Settings(
            hf_cache_dir=str(LOCAL_CACHE.resolve()),
            chunk_max_workers=1,
        )
    )
    request = json.loads(
        Path("tests/fixtures/ocr-rag-chunks.input.example.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        result = await asyncio.wait_for(runtime.run(request), 20)
        assert (
            canonical_bytes(result)
            == Path("tests/fixtures/ocr-rag-chunks.example.json").read_bytes()
        )
    finally:
        await runtime.close()
