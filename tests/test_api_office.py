"""Office input and the generic document_base64 source on /parse."""

import base64
import io

import httpx
import pytest
from PIL import Image

import ocr_server
from ocr_server import create_app
from ocrserver import office_utils
from ocrserver.config import Settings
from ocrserver.office_utils import OfficeError
from test_chunk_api import setup
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG
from test_office_utils import ooxml


async def post(app, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        return await client.post("/parse", **kwargs)


async def test_document_base64_accepts_images():
    pipeline = FakePipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    response = await post(app, json={"document_base64": PNG})
    assert response.status_code == 200, response.text
    assert len(pipeline.calls) == 1
    form = await post(app, data={"document_base64": PNG})
    assert form.status_code == 200, form.text


@pytest.mark.parametrize(
    "body",
    [
        {"document_base64": PNG, "image_base64": PNG},
        {"document_base64": PNG, "pdf_base64": PNG},
    ],
)
async def test_document_base64_counts_as_one_source(body):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    response = await post(app, json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_input_count"


async def test_document_base64_rejects_invalid_base64():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    response = await post(app, json={"document_base64": "!"})
    assert response.json()["detail"]["code"] == "invalid_base64"


async def test_parse_openapi_lists_document_source():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    schema = app.openapi()["paths"]["/parse"]["post"]["requestBody"]["content"]
    for content_type in ("application/json", "multipart/form-data"):
        choices = schema[content_type]["schema"]["oneOf"]
        assert any(choice["required"] == ["document_base64"] for choice in choices)


async def test_chunks_accept_document_base64():
    app, runtime, pipeline = setup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse/chunks",
            json={"document_base64": PNG, "options": {"max_tokens": 512}},
        )
        both = await client.post(
            "/parse/chunks",
            json={
                "document_base64": PNG,
                "image_base64": PNG,
                "options": {"max_tokens": 512},
            },
        )
    assert response.status_code == 200, response.text
    assert len(pipeline.calls) == 1
    assert both.status_code == 422
    chunk_schema = app.openapi()["components"]["schemas"]["ParseChunksJsonRequest"]
    assert any(
        choice["required"] == ["document_base64"] for choice in chunk_schema["oneOf"]
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def pdf_bytes(pages: int) -> bytes:
    images = [Image.new("RGB", (20, 20), "white") for _ in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


DOCX = b64(ooxml("word/document.xml"))


@pytest.fixture
def converter(monkeypatch):
    calls = []

    def convert(data, kind, *, soffice, timeout, deadline=None):
        calls.append((kind, soffice, timeout))
        if isinstance(converter.result, Exception):
            raise converter.result
        return converter.result

    converter.result = pdf_bytes(1)
    monkeypatch.setattr(ocr_server, "convert_to_pdf", convert)
    converter.calls = calls
    return converter


async def test_office_document_is_converted_and_parsed(converter):
    pipeline = FakePipeline()
    settings = Settings(office_soffice_path="/opt/soffice", office_timeout_seconds=7)
    app = create_app(settings, pipeline, FakeRecognizer())
    response = await post(app, json={"document_base64": DOCX})
    assert response.status_code == 200, response.text
    assert converter.calls == [("docx", "/opt/soffice", 7)]
    assert len(pipeline.calls[0]) == 1
    [warning] = response.json()["warnings"]
    assert warning["code"] == "office_converted"
    assert warning["severity"] == "info"
    assert warning["target"] == {"field": "file"}
    assert warning["details"] == {"source_format": "docx"}
    upload = await post(app, files={"file": ("report.bin", ooxml("word/document.xml"))})
    assert upload.status_code == 200, upload.text
    assert converter.calls[-1][0] == "docx"


async def test_converted_pdf_is_truncated_like_native_pdf(converter):
    converter.result = pdf_bytes(3)
    pipeline = FakePipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    response = await post(app, json={"document_base64": DOCX, "max_pages": 2})
    assert response.status_code == 200, response.text
    codes = [warning["code"] for warning in response.json()["warnings"]]
    assert codes == ["office_converted", "pages_truncated"]
    assert len(pipeline.calls[0]) == 2


async def test_office_disabled(converter):
    app = create_app(Settings(office_enabled=False), FakePipeline(), FakeRecognizer())
    response = await post(app, json={"document_base64": DOCX})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "office_disabled"
    assert converter.calls == []


async def test_office_converter_unavailable(monkeypatch):
    monkeypatch.setattr(office_utils.shutil, "which", lambda name: None)
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    response = await post(app, json={"document_base64": DOCX})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "office_converter_unavailable"


@pytest.mark.parametrize(
    "error,code",
    [
        (
            OfficeError("LibreOffice exited with status 1: boom"),
            "office_conversion_failed",
        ),
        (OfficeError("slow", "office_conversion_timeout"), "office_conversion_timeout"),
    ],
)
async def test_office_conversion_errors(converter, error, code):
    converter.result = error
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    response = await post(app, json={"document_base64": DOCX})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message"] == str(error)


async def test_oversized_converted_pdf_is_rejected(converter):
    converter.result = pdf_bytes(3)
    limit = len(base64.b64decode(DOCX)) + 10
    app = create_app(Settings(max_file_bytes=limit), FakePipeline(), FakeRecognizer())
    response = await post(app, json={"document_base64": DOCX})
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large"


async def test_office_document_through_chunks(converter):
    app, runtime, pipeline = setup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse/chunks",
            json={"document_base64": DOCX, "options": {"max_tokens": 512}},
        )
    assert response.status_code == 200, response.text
    assert converter.calls[0][0] == "docx"


async def post_preview(app, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        return await client.post("/office/pdf", **kwargs)


async def test_office_pdf_preview_returns_converted_pdf(converter):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    response = await post_preview(
        app, files={"file": ("report.docx", base64.b64decode(DOCX))}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == converter.result
    assert converter.calls[0][0] == "docx"


@pytest.mark.parametrize(
    "kwargs,status,code",
    [
        (
            {"files": {"file": ("page.png", base64.b64decode(PNG))}},
            400,
            "not_office_document",
        ),
        ({"files": {"file": ("doc.pdf", b"%PDF-1.7\n")}}, 400, "not_office_document"),
        ({"data": {"other": "x"}}, 400, "invalid_input_count"),
    ],
)
async def test_office_pdf_preview_refuses_other_input(converter, kwargs, status, code):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    response = await post_preview(app, **kwargs)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert converter.calls == []


async def test_office_pdf_preview_errors_match_parse(converter):
    converter.result = OfficeError("boom")
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    failed = await post_preview(app, files={"file": ("r.docx", base64.b64decode(DOCX))})
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "office_conversion_failed"
    disabled = create_app(
        Settings(office_enabled=False), FakePipeline(), FakeRecognizer()
    )
    refused = await post_preview(
        disabled, files={"file": ("r.docx", base64.b64decode(DOCX))}
    )
    assert refused.json()["detail"]["code"] == "office_disabled"
    small = create_app(Settings(max_file_bytes=10), FakePipeline(), FakeRecognizer())
    large = await post_preview(
        small, files={"file": ("r.docx", base64.b64decode(DOCX))}
    )
    assert large.status_code == 413
