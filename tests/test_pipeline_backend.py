"""Verify pipeline image conversion, inference orchestration and lifecycle."""

import asyncio
import io
import sys
import threading
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest
from PIL import Image

from ocrserver.config import Settings
from ocrserver.pipeline_backend import (
    PipelineBackend,
    image_to_png_bytes,
    to_bgr_ndarray,
)


def test_layout_threads_are_constructor_options_not_global_paddle_state(monkeypatch):
    options = []
    pipelines = []

    def create_pipeline(**kwargs):
        options.append(kwargs)
        pipeline = SimpleNamespace()
        pipelines.append(pipeline)
        return pipeline

    monkeypatch.setitem(sys.modules, "paddle", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "paddleocr", SimpleNamespace(PaddleOCRVL=create_pipeline)
    )
    for index, threads in enumerate((2, 4, None)):
        backend = PipelineBackend(Settings(), "http://unused", layout_threads=threads)
        assert backend._ensure_pipeline() is pipelines[index]
        assert backend._ensure_pipeline() is pipelines[index]
    assert len(options) == 3
    assert options[0]["cpu_threads"] == 2
    assert options[1]["cpu_threads"] == 4
    assert "cpu_threads" not in options[2]


def test_layout_threads_from_settings_reach_pipeline_constructor(monkeypatch):
    options = {}

    def create_pipeline(**kwargs):
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "paddle", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "paddleocr", SimpleNamespace(PaddleOCRVL=create_pipeline)
    )
    backend = PipelineBackend(Settings(layout_threads=3), "http://unused")
    backend._ensure_pipeline()
    assert options["cpu_threads"] == 3


def test_extract_result_preserves_paddle_metadata_and_images():
    class Result:
        markdown: ClassVar[dict] = {
            "markdown_texts": "# Result",
            "markdown_images": {
                "imgs/figure.jpg": np.array([[[0, 0, 255]]], dtype=np.uint8)
            },
        }
        json: ClassVar[dict] = {
            "res": {
                "width": 1200,
                "height": 1684,
                "parsing_res_list": [
                    {
                        "block_label": "image",
                        "block_content": "",
                        "block_bbox": [1, 2, 30, 40],
                        "block_id": 7,
                        "block_order": None,
                        "group_id": 3,
                        "global_block_id": 9,
                        "global_group_id": 4,
                        "block_polygon_points": [[1, 2], [30, 2], [30, 40], [1, 40]],
                    }
                ],
            }
        }

    result = PipelineBackend._extract_result(Result())

    assert result["markdown"] == "# Result"
    assert (result["width"], result["height"]) == (1200, 1684)
    assert result["images"][0]["name"] == "imgs/figure.jpg"
    encoded = result["images"][0]["png"]
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(encoded)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 0, 0)
    block = result["blocks"][0]
    assert block.block_id == 7
    assert block.order is None
    assert block.group_id == 3
    assert block.global_block_id == 9
    assert block.global_group_id == 4
    assert block.polygon == [[1, 2], [30, 2], [30, 40], [1, 40]]


def test_image_to_png_bytes_converts_bgr_to_rgb():
    bgr_red = np.array([[[0, 0, 255]]], dtype=np.uint8)

    encoded = image_to_png_bytes(bgr_red)
    with Image.open(io.BytesIO(encoded)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_bgr_arrays_are_not_swapped_and_invalid_arrays_are_rejected():
    bgr = np.array([[[0, 0, 255]]], dtype=np.uint8)
    assert to_bgr_ndarray(bgr) is bgr
    for bad in (np.zeros((2, 2)), np.zeros((2, 2, 4), dtype=np.uint8)):
        with pytest.raises(ValueError, match="BGR uint8"):
            to_bgr_ndarray(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_worker_retains_slot_after_timeout_or_cancel(batch, cancel):
    entered = threading.Event()
    release = threading.Event()
    sem = asyncio.Semaphore(1)

    class Pipeline:
        def predict(self, *args, **kwargs):
            entered.set()
            release.wait(5)
            yield SimpleNamespace(markdown="done", json={})

    backend = PipelineBackend(Settings(), "http://unused", sem, _pipeline=Pipeline())
    image = np.zeros((1, 1, 3), dtype=np.uint8)
    call = (
        backend.parse_document([image], 1, 0.05, 0.05)
        if batch
        else backend.parse_page(image, 1, 0.05)
    )
    task = asyncio.create_task(call)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif batch:
            assert "error" in (await task)[0]
        else:
            with pytest.raises(TimeoutError):
                await task
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sem.acquire(), 0.03)
        await backend.aclose(0.01)
        assert sem.locked()
    finally:
        release.set()
        await backend.aclose(2)
    assert not sem.locked()
    assert not backend._workers


@pytest.mark.asyncio
async def test_short_batch_reports_failure_without_waiting_for_timeout():
    class Pipeline:
        def predict(self, *args, **kwargs):
            yield SimpleNamespace(markdown="first", json={})

    backend = PipelineBackend(Settings(), "http://unused", _pipeline=Pipeline())
    image = np.zeros((1, 1, 3), dtype=np.uint8)
    pages = await asyncio.wait_for(backend.parse_document([image, image], 1, 5, 10), 1)
    assert pages[0]["markdown"] == "first"
    assert "ended before" in pages[1]["error"]
    await backend.aclose(1)


@pytest.mark.asyncio
async def test_worker_exception_releases_slot():
    class Pipeline:
        def predict(self, *args, **kwargs):
            raise RuntimeError("fake error")

    sem = asyncio.Semaphore(1)
    backend = PipelineBackend(Settings(), "http://unused", sem, _pipeline=Pipeline())
    with pytest.raises(RuntimeError, match="fake error"):
        await backend.parse_page(np.zeros((1, 1, 3), dtype=np.uint8), 1, 1)
    assert not sem.locked()
    await backend.aclose(1)
    with pytest.raises(RuntimeError, match="closing"):
        await backend.parse_page(np.zeros((1, 1, 3), dtype=np.uint8), 1, 1)


@pytest.mark.asyncio
async def test_transferred_images_close_only_after_native_worker_finishes():
    release = threading.Event()

    class Pipeline:
        def predict(self, *args, **kwargs):
            release.wait(5)
            yield SimpleNamespace(markdown="done", json={})

    image = Image.new("RGB", (2, 2), "red")
    backend = PipelineBackend(Settings(), "http://unused", _pipeline=Pipeline())
    try:
        pages = await backend.parse_document([image], 1, 0.02, 0.02, close_images=True)
        assert "error" in pages[0]
        assert image.getpixel((0, 0)) == (255, 0, 0)
    finally:
        release.set()
        await backend.aclose(2)
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


@pytest.mark.asyncio
async def test_cancellation_before_worker_admission_closes_transferred_images():
    backend = PipelineBackend(Settings(), "http://unused", asyncio.Semaphore(0))
    image = Image.new("RGB", (1, 1))
    task = asyncio.create_task(
        backend.parse_document([image], 1, 1, 1, close_images=True)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
    assert not backend._workers


@pytest.mark.asyncio
async def test_lazy_pipeline_creation_is_serialized_and_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    calls = []
    backend = PipelineBackend(Settings(), "http://unused")

    class Pipeline:
        def predict(self, *args, **kwargs):
            yield SimpleNamespace(markdown="ok", json={})

    def ensure():
        assert threading.get_ident() != main_thread
        assert backend._predict_lock.locked()
        if backend._pipeline is None:
            calls.append(True)
            backend._pipeline = Pipeline()
        return backend._pipeline

    monkeypatch.setattr(backend, "_ensure_pipeline", ensure)
    image = np.zeros((1, 1, 3), dtype=np.uint8)
    await asyncio.gather(*(backend.parse_page(image, 1, 2) for _ in range(4)))
    await backend.aclose(1)
    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_admission_wait_obeys_budget_and_closes_owned_image(batch):
    backend = PipelineBackend(Settings(), "http://unused", asyncio.Semaphore(0))
    image = Image.new("RGB", (1, 1))
    if batch:
        pages = await asyncio.wait_for(
            backend.parse_document([image], 1, 0.02, 0.02, close_images=True), 0.5
        )
        assert "error" in pages[0]
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                backend.parse_page(image, 1, 0.02, close_image=True), 0.5
            )
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
    assert not backend._workers


@pytest.mark.asyncio
async def test_abandoned_worker_waiting_for_predict_lock_skips_predict():
    calls = []

    class Pipeline:
        def predict(self, *args, **kwargs):
            calls.append(True)
            return []

    backend = PipelineBackend(Settings(), "http://unused", _pipeline=Pipeline())
    backend._predict_lock.acquire()
    try:
        pages = await backend.parse_document(
            [np.zeros((1, 1, 3), dtype=np.uint8)], 1, 0.02, 0.02
        )
        assert "error" in pages[0]
    finally:
        backend._predict_lock.release()
        await backend.aclose(1)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_timeout_during_initialization_does_not_start_inference(
    monkeypatch, batch
):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Pipeline:
        def predict(self, *args, **kwargs):
            calls.append(True)
            return []

    backend = PipelineBackend(Settings(), "http://unused")

    def initialize():
        entered.set()
        release.wait(2)
        return Pipeline()

    monkeypatch.setattr(backend, "_ensure_pipeline", initialize)
    image = Image.new("RGB", (1, 1))
    try:
        if batch:
            result = await backend.parse_document(
                [image], 1, 0.02, 0.02, close_images=True
            )
            assert "error" in result[0]
        else:
            with pytest.raises(TimeoutError):
                await backend.parse_page(image, 1, 0.02, close_image=True)
        assert entered.is_set()
    finally:
        release.set()
        await backend.aclose(2)
    assert calls == []
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_request", [False, True])
async def test_http_parse_retains_native_slot_and_pdf_images(cancel_request):
    import base64

    import httpx

    from ocr_server import create_app

    entered, release = threading.Event(), threading.Event()
    sem = asyncio.Semaphore(1)
    captured = []

    class NativePipeline:
        def predict(self, *args, **kwargs):
            entered.set()
            release.wait(3)
            yield SimpleNamespace(markdown="done", json={})

    class Backend(PipelineBackend):
        async def parse_document(self, images, *args, **kwargs):
            captured.extend(images)
            return await super().parse_document(images, *args, **kwargs)

    settings = Settings(request_timeout=2, page_timeout=0.2, parse_dpi=72)
    backend = Backend(settings, "http://unused", sem, _pipeline=NativePipeline())
    app = create_app(settings, pipeline=backend, vllm_client=SimpleNamespace())
    pdf = io.BytesIO()
    with Image.new("RGB", (20, 20), "red") as image:
        image.save(pdf, "PDF", resolution=72)
    source = base64.b64encode(pdf.getvalue()).decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.post("/parse", json={"pdf_base64": source})
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get("/health")).status_code == 200
            if cancel_request:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                response = await request
                assert response.status_code == 200
                assert response.json()["pages"][0]["error"]
            assert sem.locked()
            assert captured[0].getpixel((10, 10))
        finally:
            release.set()
            await backend.aclose(2)
        assert not sem.locked()
        with pytest.raises(ValueError):
            captured[0].getpixel((10, 10))


@pytest.mark.asyncio
async def test_cancelled_preparation_closes_late_result_before_releasing_admission():
    from ocr_server import _Admission

    entered, release = threading.Event(), threading.Event()
    image = Image.new("RGB", (1, 1))
    sem = asyncio.Semaphore(1)
    admission = _Admission(sem, "queue_full", set())
    await admission.acquire(1)

    def work():
        entered.set()
        release.wait(3)
        return [image], []

    task = asyncio.create_task(admission.prepare(work, 2))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        admission.finish()
        assert sem.locked()
        assert image.getpixel((0, 0)) == (0, 0, 0)
    finally:
        release.set()
        await asyncio.wait_for(sem.acquire(), 2)
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


@pytest.mark.asyncio
async def test_parse_document_streams_pages_and_reports_each_one():
    class Pipeline:
        def predict(self, *args, **kwargs):
            raise AssertionError("predict() materializes; predict_iter() streams")

        def predict_iter(self, *args, **kwargs):
            yield SimpleNamespace(markdown="first", json={})
            yield SimpleNamespace(markdown="second", json={})

    seen = []
    backend = PipelineBackend(Settings(), "http://unused", _pipeline=Pipeline())
    image = np.zeros((1, 1, 3), dtype=np.uint8)
    pages = await backend.parse_document(
        [image, image], 1, 5, 10, on_page=lambda page: seen.append(page["markdown"])
    )
    assert [p["markdown"] for p in pages] == ["first", "second"]
    assert seen == ["first", "second"]
    await backend.aclose(1)


@pytest.mark.asyncio
async def test_abandoned_stream_is_closed_without_running_remaining_pages():
    produced = []
    closed = threading.Event()

    class Pipeline:
        def predict_iter(self, *args, **kwargs):
            try:
                produced.append("first")
                yield SimpleNamespace(markdown="first", json={})
                closed.wait(0.3)  # the caller times out meanwhile
                produced.append("second")
                yield SimpleNamespace(markdown="second", json={})
                produced.append("third")
                yield SimpleNamespace(markdown="third", json={})
            finally:
                closed.set()

    seen = []
    backend = PipelineBackend(Settings(), "http://unused", _pipeline=Pipeline())
    image = np.zeros((1, 1, 3), dtype=np.uint8)
    pages = await backend.parse_document(
        [image] * 3, 1, 0.1, 0.1, on_page=lambda page: seen.append(page["markdown"])
    )
    assert seen == ["first"]
    assert pages[0]["markdown"] == "first" and "error" in pages[1]
    await backend.aclose(2)
    assert closed.is_set()
    assert "third" not in produced  # the generator was closed, not drained
