"""Upload boundaries shared by parse, Office preview and OCR."""

import asyncio
import base64

import httpx
import pytest

from ocr_server import create_app
from ocrserver.config import Settings, load_settings
from test_api_office import pdf_bytes
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG
from test_office_utils import ooxml


@pytest.mark.parametrize("path", ["/parse", "/office/pdf", "/ocr"])
async def test_text_file_is_a_validation_error(path):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, data={"file": "not an upload"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_file"


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("path", ["/parse", "/office/pdf"])
async def test_body_limit_rejects_before_inference(path, declared):
    pipeline = FakePipeline()
    app = create_app(Settings(max_file_bytes=1024), pipeline, FakeRecognizer())
    consumed = 0

    async def stream():
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield b"x" * 4096

    headers = {"content-type": "application/json"}
    if declared:
        headers["content-length"] = str(409600)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, content=stream(), headers=headers)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"
    assert consumed < 100
    assert not pipeline.calls


@pytest.mark.parametrize("path", ["/parse", "/office/pdf"])
async def test_slow_upload_expires_before_body_finishes(path):
    pipeline = FakePipeline()
    app = create_app(Settings(request_timeout=0.02), pipeline, FakeRecognizer())
    completed = False
    release = asyncio.Event()

    async def stream():
        nonlocal completed
        yield b"{"
        await release.wait()
        completed = True
        yield b"}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.post(
                path, content=stream(), headers={"content-type": "application/json"}
            ),
            1,
        )
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "request_timeout"
    assert not completed and not pipeline.calls


@pytest.mark.parametrize("field", ["json_rag", "include_images"])
@pytest.mark.parametrize("value", ["invalid", "2"])
async def test_invalid_form_boolean_is_rejected(field, value):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/parse", data={"image_base64": PNG, field: value})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_boolean"


@pytest.mark.parametrize(
    "value,expected",
    [
        (" TRUE ", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        (" FALSE ", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_supported_boolean_values(value, expected):
    from ocr_server import _form_flag

    assert _form_flag(value) is expected


async def test_pdf_pixel_budget_returns_413_without_inference():
    pipeline = FakePipeline()
    app = create_app(Settings(pdf_max_page_pixels=1), pipeline, FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse", files={"file": ("document.pdf", pdf_bytes(1))}
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "pdf_pixel_limit"
    assert not pipeline.calls


async def test_upload_cleanup_on_invalid_options(monkeypatch):
    from starlette.datastructures import UploadFile

    closed = []
    original = UploadFile.close

    async def close(upload):
        await original(upload)
        closed.append(upload.file.closed)

    monkeypatch.setattr(UploadFile, "close", close)
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse",
            files={"file": ("image.png", base64.b64decode(PNG))},
            data={"output": "invalid"},
        )
    assert response.status_code == 422
    assert closed and all(closed)


def test_pdf_budget_environment_settings():
    settings = load_settings({"OCR_PDF_MAX_PAGE_PIXELS": "123"}, None)
    assert settings.pdf_max_page_pixels == 123


async def test_expired_office_request_does_not_start_queued_conversion(monkeypatch):
    from ocrserver import office_utils

    calls = []
    monkeypatch.setattr(
        office_utils.subprocess, "Popen", lambda *a, **kw: calls.append(a)
    )
    office_utils._SOFFICE_LOCK.acquire()
    try:
        app = create_app(
            Settings(request_timeout=0.02, office_soffice_path="fake"),
            FakePipeline(),
            FakeRecognizer(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/office/pdf",
                files={"file": ("document.docx", ooxml("word/document.xml"))},
            )
        assert response.status_code in (422, 504)
    finally:
        office_utils._SOFFICE_LOCK.release()
    await asyncio.sleep(0.05)
    assert not calls


async def test_timed_out_local_parsing_still_closes_upload(monkeypatch):
    from starlette.datastructures import UploadFile

    original_write, original_close = UploadFile.write, UploadFile.close
    started, closed = asyncio.Event(), asyncio.Event()

    async def write(upload, data):
        started.set()
        await asyncio.sleep(0.05)
        await original_write(upload, data)

    async def close(upload):
        await original_close(upload)
        closed.set()

    monkeypatch.setattr(UploadFile, "write", write)
    monkeypatch.setattr(UploadFile, "close", close)
    app = create_app(Settings(request_timeout=0.02), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse", files={"file": ("image.png", base64.b64decode(PNG))}
        )
    assert started.is_set() and response.status_code == 504
    await asyncio.wait_for(closed.wait(), 1)
