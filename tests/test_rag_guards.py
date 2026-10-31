"""RAG requests preserve source order and remain bounded on partial failures."""

import asyncio
import hashlib
import json
import threading

import httpx
import pytest
from fastapi import HTTPException
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_storage_browser import MemoryS3, configured

from ocr_server import create_app
from ocrserver.schemas import StorageRagRefineRequest
from ocrserver.storage_browser import StorageBrowser


class Counter:
    """Count one token per character to exercise exact split bounds."""

    def count(self, text):
        return len(text)


def source_browser():
    s3 = MemoryS3()
    prefix = s3.current_archive()
    key = prefix + "json_rag.json"
    s3.objects[key] = json.dumps(
        {
            "chunks": [
                {"id": 1, "text": "ABCD", "page": 1, "token_count": 4},
            ],
            "metadata": {},
        }
    ).encode()
    manifest = json.loads(s3.objects[prefix + "manifest.json"])
    manifest["artifacts"].append(
        {
            "name": "json_rag.json",
            "key": key,
            "size": len(s3.objects[key]),
        }
    )
    s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    browser = StorageBrowser(configured(), client=s3, writer_factory=lambda: s3)
    browser._rag_counter = lambda: Counter()
    payload = {
        "source_key": key,
        "source_sha256": hashlib.sha256(s3.objects[key]).hexdigest(),
        "execution_id": "79e33f8f-95ed-48f4-af27-1a9daab51bca",
        "created_at": "2026-09-27T12:30:00Z",
        "options": {"min_tokens": 1, "max_tokens": 10},
        "groups": [[0]],
    }
    return s3, browser, payload


@pytest.mark.parametrize(
    "groups",
    [
        [[[0, 2, 4]], [[0, 0, 2]]],
        [[[0, 2, 4], [0, 0, 2]]],
    ],
)
async def test_reordered_ranges_are_rejected_before_any_write(groups):
    s3, browser, payload = source_browser()
    try:
        with pytest.raises(HTTPException) as caught:
            await browser.refine_rag(
                StorageRagRefineRequest(
                    **{**payload, "groups": groups},
                )
            )
        assert caught.value.status_code == 422
        assert caught.value.detail["code"] == "rag_invalid_plan"
        assert not any(op == "put" for op, _ in s3.calls)
    finally:
        await browser.close()


@pytest.mark.parametrize("path", ["/storage/rag/refine", "/storage/rag/split"])
@pytest.mark.parametrize("failure", ["slow", "large", "invalid"])
async def test_rag_body_guards_precede_storage_operations(monkeypatch, path, failure):
    monkeypatch.setattr("ocrserver.storage_browser.IO_TIMEOUT_SECONDS", 0.02)
    s3, browser, _ = source_browser()
    app = create_app(
        configured(),
        FakePipeline(),
        FakeRecognizer(),
        storage_browser=browser,
    )

    release = asyncio.Event()

    async def slow():
        yield b'{"source_key":"'
        await release.wait()
        pytest.fail("the incomplete upload should have timed out")

    body = (
        slow()
        if failure == "slow"
        else b"x" * (2 * 1024 * 1024 + 1)
        if failure == "large"
        else b"{}"
    )
    expected = {
        "slow": (504, "rag_request_timeout"),
        "large": (413, "rag_plan_too_large"),
        "invalid": (422, "rag_invalid_plan"),
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as http:
            response = await asyncio.wait_for(http.post(path, content=body), 1)
        status, code = expected[failure]
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code
        assert not s3.calls
    finally:
        await browser.close()


async def test_tokenizer_loading_uses_an_admitted_worker_and_keeps_loop_responsive():
    _, browser, payload = source_browser()
    entered, release = threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(1), "event loop could not release tokenizer loader"
        return Counter()

    browser._rag_counter = load
    payload["groups"] = [[[0, 0, 4]]]
    task = asyncio.create_task(browser.refine_rag(StorageRagRefineRequest(**payload)))
    try:
        assert await asyncio.to_thread(entered.wait, 0.5)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert len(browser._active) == 1
        release.set()
        assert (await task)["chunk_count"] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await browser.close()


async def test_concurrent_workers_share_one_tokenizer_load(monkeypatch):
    browser = StorageBrowser(configured())
    entered, release = threading.Event(), threading.Event()
    instances = []

    class Tokenizer:
        def __init__(self, *args):
            instances.append(self)

        @property
        def identity(self):
            entered.set()
            assert release.wait(1)
            return {}

    monkeypatch.setattr("ocrserver.chunking.tokenizer.PaddleTokenizer", Tokenizer)
    first = asyncio.create_task(asyncio.to_thread(browser._rag_counter))
    second = asyncio.create_task(asyncio.to_thread(browser._rag_counter))
    try:
        assert await asyncio.to_thread(entered.wait, 0.5)
        release.set()
        assert (await first) is (await second)
        assert len(instances) == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await browser.close()


@pytest.mark.parametrize("corruption", ["partial_delete", "changed_bytes"])
async def test_idempotent_retry_cannot_return_a_missing_or_changed_artifact(corruption):
    s3, browser, payload = source_browser()
    request = StorageRagRefineRequest(**payload)
    try:
        saved = await browser.refine_rag(request)
        key = saved["file"]["key"]
        if corruption == "partial_delete":
            original_put = s3.put_object

            def fail_manifest(**kwargs):
                if kwargs["Key"].endswith("/manifest.json"):
                    raise RuntimeError("synthetic manifest failure")
                return original_put(**kwargs)

            s3.put_object = fail_manifest
            with pytest.raises(HTTPException):
                await browser.delete_file(key, saved["file"]["name"])
            s3.put_object = original_put
            assert key not in s3.objects
        else:
            s3.objects[key] = b"invalid stored bytes"
        writes_before = sum(op == "put" for op, _ in s3.calls)
        with pytest.raises(HTTPException) as caught:
            await browser.refine_rag(request)
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "storage_archive_incomplete"
        assert sum(op == "put" for op, _ in s3.calls) == writes_before
    finally:
        await browser.close()
