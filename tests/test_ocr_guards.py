"""OCR geometry, memory admission and multi-frame regressions."""

import asyncio
import base64
import io
import threading

import httpx
import pytest
from PIL import Image

import ocr_server
from ocr_server import _Admission, _MemoryBudget, create_app
from ocrserver.config import Settings, load_settings
from ocrserver.element_ocr import (
    MultiframeImageError,
    load_element_image,
    load_image_pages,
)
from ocrserver.vllm_client import BackendUnavailable, CompletionResult
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG


def test_memory_setting_is_validated_and_loaded():
    assert (
        load_settings({"OCR_ELEMENT_MEMORY_BYTES": "2048"}, None).element_memory_bytes
        == 2048
    )
    with pytest.raises(ValueError):
        Settings(element_memory_bytes=0)


async def test_memory_remains_reserved_until_timed_out_worker_exits():
    memory = _MemoryBudget(32)
    admission = _Admission(asyncio.Semaphore(1), "slots_full", set())
    released = threading.Event()
    entered = threading.Event()

    def prepare():
        admission.reserve_memory(memory, 32)
        entered.set()
        released.wait(2)

    await admission.acquire(1)
    try:
        with pytest.raises(TimeoutError):
            await admission.prepare(prepare, 0.05)
        admission.finish()
        assert entered.is_set()
        assert memory.used == 32
        with pytest.raises(ocr_server.HTTPException) as caught:
            memory.reserve(1)
        assert caught.value.status_code == 503
    finally:
        released.set()
    for _ in range(100):
        if memory.used == 0:
            break
        await asyncio.sleep(0.01)
    assert memory.used == 0


async def test_parallel_requests_hold_memory_through_inference():
    entered = asyncio.Event()
    released = asyncio.Event()

    class HeldRecognizer(FakeRecognizer):
        async def recognize(self, *args):
            entered.set()
            await released.wait()
            return CompletionResult("fixture", "stop")

    app = create_app(
        Settings(element_memory_bytes=32), FakePipeline(), HeldRecognizer()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post("/ocr", json={"image_base64": PNG}))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            second = await client.post("/ocr", json={"image_base64": PNG})
            assert second.status_code == 503
            assert second.json()["detail"]["code"] == "image_memory_full"
        finally:
            released.set()
            await first
        assert (
            await client.post("/ocr", json={"image_base64": PNG})
        ).status_code == 200


@pytest.mark.parametrize("task,limit", [("ocr", 31), ("spotting", 127)])
async def test_memory_rejection_precedes_rgb_decode(monkeypatch, task, limit):
    monkeypatch.setattr(
        "ocrserver.element_ocr.upright_rgb", lambda _: pytest.fail("decoded")
    )
    app = create_app(
        Settings(element_memory_bytes=limit), FakePipeline(), FakeRecognizer()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/ocr", json={"image_base64": PNG, "task": task})
    assert response.status_code == 503


@pytest.mark.parametrize("format", ["GIF", "TIFF"])
async def test_multiple_frames_are_rejected_but_parse_keeps_them(format):
    with (
        Image.new("RGB", (2, 2), "red") as first,
        Image.new("RGB", (2, 2), "blue") as second,
    ):
        output = io.BytesIO()
        first.save(output, format=format, save_all=True, append_images=[second])
    data = output.getvalue()
    with pytest.raises(MultiframeImageError):
        load_element_image(data)
    pages, total = load_image_pages(data, 10)
    assert total == len(pages) == 2
    for page in pages:
        page.close()
    recognizer = FakeRecognizer()
    app = create_app(Settings(), FakePipeline(), recognizer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ocr", json={"image_base64": base64.b64encode(data).decode()}
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "multiframe_image_not_supported"
    assert not recognizer.tasks


async def test_invalid_spotting_returns_warning_and_preserves_text():
    text = "bad [[" + "9" * 400 + ",1,2,3]] good [[1,2,3,4]]"

    class Recognizer:
        async def recognize(self, *args):
            return CompletionResult(text, "stop")

    app = create_app(Settings(), FakePipeline(), Recognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ocr", json={"image_base64": PNG, "task": "spotting"}
        )
    assert response.status_code == 200
    assert response.json()["result"] == text
    assert response.json()["boxes"] == [[1, 2, 3, 4]]
    assert response.json()["warnings"][0]["code"] == "spotting_invalid_coordinates"


@pytest.mark.parametrize(
    "status,code", [(503, "backend_unavailable"), (502, "backend_error")]
)
async def test_upstream_mapping_is_safe(status, code):
    class Recognizer:
        async def recognize(self, *args):
            raise BackendUnavailable("private details", status)

    app = create_app(Settings(), FakePipeline(), Recognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/ocr", json={"image_base64": PNG})
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code}
