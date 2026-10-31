"""MCP Office-to-PDF preview tool."""

import base64
from contextlib import asynccontextmanager

import httpx
from mcp.client import Client
from ocr_mcp.config import Settings
from ocr_mcp.server import build_server

OFFICE_BYTES = b"PK\x03\x04fake-docx"
PDF_BYTES = b"%PDF-1.7\nfake-pdf"


def office_harness(handler, **overrides):
    settings = Settings(
        _env_file=None,
        api_tokens_raw="inbound-test:tester",
        upstream_client_token="upstream-test",
        retry_attempts=2,
        retry_backoff=0.001,
        **overrides,
    )
    calls = []

    def record(request):
        calls.append(request)
        return handler(request)

    server, ctx = build_server(settings, transport=httpx.MockTransport(record))
    return server, ctx, calls


@asynccontextmanager
async def _client(server, ctx):
    try:
        async with Client(server) as client:
            yield client
    finally:
        await ctx.client.aclose()


async def test_convert_office_to_pdf_returns_base64(tmp_path):
    def respond(request):
        assert request.url.path == "/office/pdf"
        assert request.headers["content-type"].startswith("multipart/form-data")
        return httpx.Response(
            200, content=PDF_BYTES, headers={"content-type": "application/pdf"}
        )

    server, ctx, _ = office_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_convert_office_to_pdf",
            {
                "params": {
                    "document_base64": base64.b64encode(OFFICE_BYTES).decode(),
                    "filename": "report.docx",
                    "response_format": "json",
                }
            },
        )
        assert not result.is_error
        assert (
            result.structured_content["base64"] == base64.b64encode(PDF_BYTES).decode()
        )


async def test_convert_office_to_pdf_rejects_pdf_source():
    def respond(request):
        return httpx.Response(400, json={"code": "not_office_document"})

    server, ctx, _ = office_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_convert_office_to_pdf",
            {
                "params": {
                    "document_base64": base64.b64encode(PDF_BYTES).decode(),
                    "response_format": "json",
                }
            },
        )
        assert result.is_error
        assert "not_office_document" in result.content[0].text


async def test_convert_office_to_pdf_forwards_image_bytes_for_an_api_refusal():
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def respond(request):
        assert request.url.path == "/office/pdf"
        return httpx.Response(400, json={"detail": {"code": "not_office_document"}})

    server, ctx, calls = office_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_convert_office_to_pdf",
            {"params": {"document_base64": base64.b64encode(png).decode()}},
        )
        assert result.is_error
        assert "not_office_document" in result.content[0].text
    assert len(calls) == 1


async def test_convert_office_to_pdf_accepts_file_path(tmp_path):
    file = tmp_path / "report.docx"
    file.write_bytes(OFFICE_BYTES)

    def respond(request):
        assert request.url.path == "/office/pdf"
        assert request.headers["content-type"].startswith("multipart/form-data")
        return httpx.Response(
            200, content=PDF_BYTES, headers={"content-type": "application/pdf"}
        )

    server, ctx, _ = office_harness(respond, input_roots_raw=str(tmp_path))
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_convert_office_to_pdf",
            {"params": {"file_path": str(file), "response_format": "json"}},
        )
        assert not result.is_error
        assert (
            result.structured_content["base64"] == base64.b64encode(PDF_BYTES).decode()
        )
