"""The opt-in REST path shares OCR parsing and the standalone chunk engine."""

import json

import httpx

from ocr_server import create_app
from ocrserver.chunking.canonical import canonical_bytes
from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.errors import ChunkError
from ocrserver.config import Settings
from ocrserver.schemas import ChunkOptions
from test_chunk_engine import WordTokenizer, document
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG


class InlineRuntime:
    """Record chunk requests and execute them inline with a word tokenizer."""

    def __init__(self):
        self.calls = []

    async def run(self, request, *, scope=None):
        self.calls.append(request)
        return chunk_document(
            request["parsed_document"],
            ChunkOptions(**request["options"]),
            WordTokenizer(),
            source_id=request.get("source_id"),
            lang=request.get("lang"),
            accept_truncated=request.get("accept_truncated", False),
            scope=scope,
        )

    async def close(self):
        pass


class TextPipeline(FakePipeline):
    """Emit structured prose blocks for document-to-chunk API tests."""

    async def parse_document(self, *args, **kwargs):
        results = await super().parse_document(*args, **kwargs)
        for result in results:
            result["blocks"] = [
                {
                    "type": "text",
                    "content": "Testo del chunk",
                    "bbox": [],
                    "order": 0,
                }
            ]
        return results


def setup(settings=None):
    runtime, pipeline = InlineRuntime(), TextPipeline()
    app = create_app(
        settings or Settings(),
        pipeline,
        FakeRecognizer(),
        chunk_runtime=runtime,
    )
    return app, runtime, pipeline


async def test_existing_json_needs_no_ocr_and_returns_canonical_root():
    app, runtime, pipeline = setup()
    body = {
        "parsed_document": document(("text", "Testo del chunk")),
        "options": {"max_tokens": 512},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/chunk", json=body)
    assert response.status_code == 200
    assert set(response.json()) == {"chunks"}
    assert response.content == canonical_bytes(response.json())
    assert len(runtime.calls) == 1
    assert pipeline.calls == []


async def test_original_parse_does_not_chunk_and_new_parse_returns_rag():
    app, runtime, pipeline = setup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        original = await client.post("/parse", json={"image_base64": PNG})
        assert original.status_code == 200
        assert "pages" in original.json()
        assert runtime.calls == []
        response = await client.post(
            "/parse/chunks",
            json={"image_base64": PNG, "options": {"max_tokens": 512}},
        )
    assert response.status_code == 200, response.text
    assert len(pipeline.calls) == 2
    assert response.json()["chunks"][0]["text"] == "Testo del chunk"
    assert response.json()["chunks"][0]["document_context"]["scope"] == {
        "kind": "complete_document",
        "origin": "full_parse",
        "selected_pages": [1],
        "requested_max_pages": None,
        "total_pages": 1,
    }


async def test_strict_conflict_never_returns_partial_chunks():
    app, _, _ = setup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chunk",
            json={
                "parsed_document": document(
                    ("text", "prima"), ("algorithm", "uno due tre quattro")
                ),
                "options": {"max_tokens": 2},
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "protected_block_exceeds_budget"
    assert "chunks" not in response.json()


async def test_chunk_input_bound_is_enforced_without_content_length():
    app, runtime, _ = setup(Settings(chunk_max_input_bytes=100))

    async def body():
        yield b'{"parsed_document": '
        yield b" " * 100

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chunk",
            content=body(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert runtime.calls == []


async def test_chunk_auth_and_openapi_are_explicit():
    app, runtime, _ = setup(Settings(api_tokens="test-key:test"))
    schema = app.openapi()
    for path in ("/chunk", "/parse/chunks"):
        assert schema["paths"][path]["post"]["security"] == [{"BearerAuth": []}]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/chunk", json={})
    assert response.status_code == 401
    assert runtime.calls == []


async def test_parse_chunks_multipart_options_and_exclusivity():
    import base64

    app, _, _ = setup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse/chunks",
            files={
                "file": ("page.png", base64.b64decode(PNG), "image/png"),
                "options": (None, json.dumps({"max_tokens": 512})),
            },
        )
        invalid = await client.post(
            "/parse/chunks",
            json={
                "image_base64": PNG,
                "pdf_base64": PNG,
                "options": {"max_tokens": 512},
            },
        )
    assert response.status_code == 200, response.text
    assert invalid.status_code == 422


async def test_capacity_failure_is_retryable_without_auto_retry():
    class BusyRuntime(InlineRuntime):
        async def run(self, *args, **kwargs):
            raise ChunkError("chunk_capacity_exceeded", "Busy", status_code=503)

    app = create_app(
        Settings(),
        FakePipeline(),
        FakeRecognizer(),
        chunk_runtime=BusyRuntime(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chunk",
            json={
                "parsed_document": document(("text", "body")),
                "options": {"max_tokens": 5},
            },
        )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


async def test_disconnected_client_cancels_its_chunk_request():
    import asyncio

    class WaitingRuntime(InlineRuntime):
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    runtime = WaitingRuntime()
    app = create_app(
        Settings(), FakePipeline(), FakeRecognizer(), chunk_runtime=runtime
    )
    body = json.dumps(
        {
            "parsed_document": document(("text", "body")),
            "options": {"max_tokens": 5},
        }
    ).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await runtime.started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    task = asyncio.create_task(
        app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/chunk",
                "raw_path": b"/chunk",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            },
            receive,
            send,
        )
    )
    try:
        await asyncio.wait_for(runtime.cancelled.wait(), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_validation_errors_are_documented_and_do_not_echo_ocr_text():
    app, runtime, _ = setup()
    for path in ("/chunk", "/parse/chunks"):
        assert "422" in app.openapi()["paths"][path]["post"]["responses"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chunk",
            json={
                "parsed_document": {"pages": "private OCR content"},
                "options": {"max_tokens": 5},
            },
        )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert "private OCR content" not in response.text
    assert runtime.calls == []


async def test_direct_parse_requires_an_explicit_choice_of_truncated_prefix():
    import base64
    from pathlib import Path

    pdf = Path(__file__).resolve().parents[1] / "calibration/multipage.pdf"
    app, runtime, pipeline = setup(Settings(max_pages=1))
    request = {
        "pdf_base64": base64.b64encode(pdf.read_bytes()).decode(),
        "dpi": 50,
        "options": {"max_tokens": 512},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        refused = await client.post("/parse/chunks", json=request)
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "incomplete_ocr"
        assert refused.json()["detail"]["details"] == {
            "reason": "pages_truncated",
            "max_pages": 1,
            "total_pages": 3,
        }
        # The refusal comes before inference, not after a wasted parse.
        assert pipeline.calls == []
        assert not runtime.calls
        accepted = await client.post("/parse/chunks", json={**request, "max_pages": 1})
    assert accepted.status_code == 200, accepted.text
    scope = accepted.json()["chunks"][0]["document_context"]["scope"]
    assert scope["kind"] == "page_prefix"
    assert scope["origin"] == "explicit_max_pages"
    assert scope["selected_pages"] == [1]
    assert scope["requested_max_pages"] == 1
    assert scope["total_pages"] > 1


async def test_direct_parse_refuses_multiframe_default_truncation_before_inference():
    import base64
    from pathlib import Path

    tif = (
        Path(__file__).resolve().parents[1]
        / "frontend/tests/testbook/documents/multiframe.tif"
    )
    app, runtime, pipeline = setup(Settings(max_pages=2))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        refused = await client.post(
            "/parse/chunks",
            json={
                "image_base64": base64.b64encode(tif.read_bytes()).decode(),
                "options": {"max_tokens": 512},
            },
        )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "incomplete_ocr"
    assert refused.json()["detail"]["details"]["total_pages"] == 3
    assert pipeline.calls == []
    assert not runtime.calls


async def test_direct_parse_refuses_long_office_input_after_conversion_before_inference(
    monkeypatch,
):
    import ocr_server
    from test_api_office import DOCX, pdf_bytes

    converted = []

    def convert(data, kind, *, soffice, timeout, deadline=None):
        converted.append(kind)
        return pdf_bytes(3)

    monkeypatch.setattr(ocr_server, "convert_to_pdf", convert)
    app, runtime, pipeline = setup(
        Settings(max_pages=1, office_soffice_path="/opt/soffice")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        refused = await client.post(
            "/parse/chunks",
            json={"document_base64": DOCX, "options": {"max_tokens": 512}},
        )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "incomplete_ocr"
    assert converted == ["docx"]
    assert pipeline.calls == []
    assert not runtime.calls
