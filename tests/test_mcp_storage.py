"""MCP storage browse, read and download tools with fake upstream S3."""

import base64
import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.client import Client
from ocr_mcp.config import Settings
from ocr_mcp.server import build_server

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
    "A8AAQUBAScY42YAAAAASUVORK5CYII="
)

ARCHIVE_LIST = {
    "items": [
        {
            "prefix": "invoice.pdf__20261012T100000000000Z__job123AB/",
            "job_id": "job123AB",
            "filename": "invoice.pdf",
            "kind": "parse",
            "created_at": "2026-10-12T10:00:00Z",
            "object_count": 4,
            "total_bytes": 12345,
            "partial_ocr": False,
            "incomplete": False,
        }
    ],
    "next_cursor": None,
    "skipped_archives": 0,
}

ARCHIVE_DETAIL = {
    "prefix": "invoice.pdf__20261012T100000000000Z__job123AB/",
    "manifest": {"job_id": "job123AB", "kind": "parse", "complete": True},
    "files": [
        {
            "key": "invoice.pdf__20261012T100000000000Z__job123AB/page-1.md",
            "name": "page-1.md",
            "size": 12,
            "content_type": "text/plain; charset=utf-8",
        }
    ],
}


def storage_harness(handler, **overrides):
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


@asynccontextmanager  # noqa: F821
async def _client(server, ctx):
    try:
        async with Client(server) as client:
            yield client
    finally:
        await ctx.client.aclose()


async def test_list_storage_archives_forwards_query_params():
    def respond(request):
        assert request.url.path == "/storage/archives"
        assert request.url.query == b"kind=parse&date=2026-10-12&limit=10"
        return httpx.Response(200, json=ARCHIVE_LIST)

    server, ctx, _ = storage_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_list_storage_archives",
            {
                "params": {
                    "kind": "parse",
                    "date": "2026-10-12",
                    "limit": 10,
                    "response_format": "json",
                }
            },
        )
        assert not result.is_error
        assert result.structured_content == ARCHIVE_LIST


async def test_list_storage_archives_reports_the_archived_source():
    item = ARCHIVE_LIST["items"][0]
    key = item["prefix"] + "input.pdf"
    listing = {**ARCHIVE_LIST, "items": [item, {**item, "input_key": key}]}
    server, ctx, _ = storage_harness(lambda request: httpx.Response(200, json=listing))
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_list_storage_archives", {"params": {"limit": 10}}
        )
        assert not result.is_error, result.content[0].text
        assert result.structured_content == listing
        assert result.content[0].text.count("source ") == 1
        assert f"source {key}" in result.content[0].text


async def test_get_storage_archive_forwards_prefix():
    def respond(request):
        assert request.url.path == "/storage/archive"
        assert (
            request.url.query
            == b"prefix=invoice.pdf__20261012T100000000000Z__job123AB%2F"
        )
        return httpx.Response(200, json=ARCHIVE_DETAIL)

    server, ctx, _ = storage_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_get_storage_archive",
            {
                "params": {
                    "prefix": "invoice.pdf__20261012T100000000000Z__job123AB/",
                    "response_format": "json",
                }
            },
        )
        assert not result.is_error
        assert result.structured_content == {
            **ARCHIVE_DETAIL,
            "files": [{**f, "sha256": None} for f in ARCHIVE_DETAIL["files"]],
        }


KEY = "invoice.pdf__20261012T100000000000Z__job123AB/page-1.md"
PNG_BYTES = base64.b64decode(PNG)


async def download(content, content_type, key=KEY, **overrides):
    """Call ocr_download_storage_file against one canned upstream response."""

    def respond(request):
        assert request.url.path == "/storage/file"
        return httpx.Response(
            200, content=content, headers={"content-type": content_type}
        )

    server, ctx, calls = storage_harness(respond, **overrides)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_download_storage_file",
            {"params": {"key": key, "response_format": "json"}},
        )
    return result, calls


async def test_download_returns_utf8_text_without_base64():
    content = "# Titolo \u00e8\n".encode()
    result, calls = await download(content, "text/markdown; charset=utf-8")
    assert calls[0].url.query == (
        b"key=invoice.pdf__20261012T100000000000Z__job123AB%2Fpage-1.md&preview=false"
    )
    assert not result.is_error
    sc = result.structured_content
    assert sc["text"] == "# Titolo \u00e8\n" and "base64" not in sc
    assert sc["size"] == 12
    assert sc["sha256"] == hashlib.sha256(content).hexdigest()
    assert sc["key"] == KEY and sc["content_type"] == "text/markdown; charset=utf-8"


async def test_download_returns_a_json_artifact_as_text():
    result, _ = await download(b'{"chunks":[]}', "application/json")
    assert result.structured_content["text"] == '{"chunks":[]}'
    assert "base64" not in result.structured_content


async def test_download_returns_png_as_an_image_block():
    result, _ = await download(PNG_BYTES, "image/png")
    assert not result.is_error
    assert [block.type for block in result.content] == ["text", "image"]
    assert result.content[1].mime_type == "image/png"
    assert base64.b64decode(result.content[1].data) == PNG_BYTES
    assert "base64" not in result.structured_content
    assert "text" not in result.structured_content


async def test_download_falls_back_to_base64_for_invalid_utf8_text():
    result, _ = await download(b"\xff\xfe\x00", "text/plain")
    assert (
        result.structured_content["base64"]
        == base64.b64encode(b"\xff\xfe\x00").decode()
    )
    assert "text" not in result.structured_content


@pytest.mark.parametrize(
    "content,content_type",
    [(b"%PDF-1.7 bytes", "application/pdf"), (b"x" * 11, "image/png")],
)
async def test_download_keeps_base64_for_pdf_and_oversized_png(
    monkeypatch, content, content_type
):
    from ocr_mcp.tools import storage

    monkeypatch.setattr(storage, "IMAGE_CONTENT_MAX_BYTES", 10)
    result, _ = await download(content, content_type)
    assert len(result.content) == 1
    assert result.structured_content["base64"] == base64.b64encode(content).decode()


DETAIL_PREFIX = "invoice.pdf__20261012T100000000000Z__job123AB/"


async def archive_detail(payload):
    server, ctx, _ = storage_harness(lambda _: httpx.Response(200, json=payload))
    async with _client(server, ctx) as client:
        return await client.call_tool(
            "ocr_get_storage_archive", {"params": {"prefix": DETAIL_PREFIX}}
        )


async def test_archive_detail_drops_duplicate_manifest_lists_and_adds_file_hashes():
    payload = {
        "prefix": DETAIL_PREFIX,
        "manifest": {
            "job_id": "job123AB",
            "artifacts": [
                {
                    "name": "json_rag.json",
                    "key": DETAIL_PREFIX + "json_rag.json",
                    "size": 3,
                    "sha256": "b" * 64,
                    "content_type": "application/json",
                }
            ],
            "images": [
                {
                    "page": 1,
                    "original_name": "imgs/a.jpg",
                    "path": "images/p0001-0001.png",
                    "key": DETAIL_PREFIX + "images/p0001-0001.png",
                }
            ],
        },
        "files": [
            {
                "key": DETAIL_PREFIX + name,
                "name": name,
                "size": 3,
                "content_type": "application/json",
            }
            for name in ("json_rag.json", "manifest.json")
        ],
    }
    result = await archive_detail(payload)
    sc = result.structured_content
    assert "artifacts" not in sc["manifest"]
    assert sc["manifest"]["images"] == [
        {"page": 1, "original_name": "imgs/a.jpg", "path": "images/p0001-0001.png"}
    ]
    assert {f["name"]: f["sha256"] for f in sc["files"]} == {
        "json_rag.json": "b" * 64,
        "manifest.json": None,
    }


async def test_archive_detail_without_artifacts_keeps_null_hashes():
    result = await archive_detail(ARCHIVE_DETAIL)
    assert not result.is_error
    assert result.structured_content["manifest"] == ARCHIVE_DETAIL["manifest"]
    assert all(f["sha256"] is None for f in result.structured_content["files"])


async def test_download_storage_zip_returns_base64():
    content = b"fake-zip-bytes"

    def respond(request):
        assert request.url.path == "/storage/archive.zip"
        assert (
            request.url.query
            == b"prefix=invoice.pdf__20261012T100000000000Z__job123AB%2F"
        )
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/zip"},
        )

    server, ctx, _ = storage_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_download_storage_zip",
            {
                "params": {
                    "prefix": "invoice.pdf__20261012T100000000000Z__job123AB/",
                    "response_format": "json",
                }
            },
        )
        assert not result.is_error
        assert result.structured_content["content_type"] == "application/zip"
        assert result.structured_content["base64"] == base64.b64encode(content).decode()


async def test_download_storage_file_refuses_oversized_binary():
    content = b"x" * 2048

    def respond(request):
        return httpx.Response(
            200, content=content, headers={"content-type": "application/octet-stream"}
        )

    server, ctx, _ = storage_harness(respond, chunk_max_result_bytes=1024)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_download_storage_file",
            {
                "params": {
                    "key": "invoice.pdf__20261012T100000000000Z__job123AB/big.bin"
                }
            },
        )
        assert result.is_error
        assert result.structured_content == {}
        assert "/storage/file" in result.content[0].text


@pytest.mark.parametrize(
    "tool,params,response",
    [
        ("ocr_list_storage_archives", {}, httpx.Response(200, json=ARCHIVE_LIST)),
        (
            "ocr_download_storage_zip",
            {"prefix": "p/"},
            httpx.Response(200, content=b"zip"),
        ),
    ],
)
async def test_storage_reads_are_not_bound_by_the_diagnostic_deadline(
    tool, params, response
):
    import asyncio

    async def respond(request):
        await asyncio.sleep(0.3)
        return response

    server, ctx, _ = storage_harness(respond, diagnostic_timeout=0.1)
    async with _client(server, ctx) as client:
        result = await client.call_tool(tool, {"params": params})
        assert not result.is_error, result.content[0].text


async def test_storage_download_stops_reading_past_the_limit():
    class Endless(httpx.AsyncByteStream):
        sent = 0

        async def __aiter__(self):
            while self.sent < 10 * 1024 * 1024:
                self.sent += 4096
                yield b"x" * 4096

    stream = Endless()
    server, ctx, _ = storage_harness(
        lambda request: httpx.Response(200, stream=stream), chunk_max_result_bytes=8192
    )
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_download_storage_zip", {"params": {"prefix": "p/"}}
        )
        assert result.is_error
        assert "/storage/archive.zip" in result.content[0].text
    assert stream.sent < 64 * 1024


async def test_archive_delete_tool_does_not_claim_read_only_refusal():
    server, ctx, _ = storage_harness(lambda _: httpx.Response(500))
    async with _client(server, ctx) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    # Whole-archive deletion works in browse-only mode; file deletion does not.
    assert "read-only" not in tools["ocr_delete_storage_archive"].description
    assert "read-only" in tools["ocr_delete_storage_file"].description


async def test_delete_storage_archive_requires_correct_confirm():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/storage/archive":
            body = json.loads(request.content)
            assert (
                request.url.query
                == b"prefix=invoice.pdf__20261012T100000000000Z__job123AB%2F"
            )
            if body["confirm"] != "job123AB":
                return httpx.Response(
                    409, json={"code": "storage_confirmation_required"}
                )
            return httpx.Response(200, json={"deleted_objects": 5})
        return httpx.Response(404)

    server, ctx, _ = storage_harness(respond)
    async with _client(server, ctx) as client:
        bad = await client.call_tool(
            "ocr_delete_storage_archive",
            {
                "params": {
                    "prefix": "invoice.pdf__20261012T100000000000Z__job123AB/",
                    "confirm": "wrong",
                }
            },
        )
        assert bad.is_error
        assert bad.structured_content == {}

        good = await client.call_tool(
            "ocr_delete_storage_archive",
            {
                "params": {
                    "prefix": "invoice.pdf__20261012T100000000000Z__job123AB/",
                    "confirm": "job123AB",
                }
            },
        )
        assert not good.is_error
        assert good.structured_content == {"deleted_objects": 5}


async def test_delete_storage_file_requires_correct_confirm():
    key = "invoice.pdf__20261012T100000000000Z__job123AB/page-1.md"

    def respond(request):
        if request.url.path == "/storage/file":
            body = json.loads(request.content)
            assert (
                request.url.query
                == b"key=invoice.pdf__20261012T100000000000Z__job123AB%2Fpage-1.md"
            )
            if body["confirm"] != "page-1.md":
                return httpx.Response(
                    409, json={"code": "storage_confirmation_required"}
                )
            return httpx.Response(200, json={"deleted_objects": 1})
        return httpx.Response(404)

    server, ctx, _ = storage_harness(respond)
    async with _client(server, ctx) as client:
        bad = await client.call_tool(
            "ocr_delete_storage_file",
            {"params": {"key": key, "confirm": "wrong"}},
        )
        assert bad.is_error
        assert bad.structured_content == {}

        good = await client.call_tool(
            "ocr_delete_storage_file",
            {"params": {"key": key, "confirm": "page-1.md"}},
        )
        assert not good.is_error
        assert good.structured_content == {"deleted_objects": 1}


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (404, "storage_not_found", "ocr_list_storage_archives"),
        (404, "storage_not_found", "incomplete"),
        (409, "storage_confirmation_required", "confirm"),
        (503, "storage_write_disabled", "whole-archive deletion"),
        (503, "storage_delete_partial", "deleted_objects"),
        (422, "storage_invalid_key", "manifest.json"),
        (409, "rag_source_changed", "source_sha256"),
        (422, "rag_invalid_source", "json_rag.json"),
        (413, "storage_file_too_large", "REST"),
        (413, "storage_file_too_large", "preview=false"),
        (400, "office_disabled", "PDF"),
        (409, "rag_execution_conflict", "UUID"),
    ],
)
def test_storage_rag_and_office_codes_get_specific_guidance(status, code, expected):
    from ocr_mcp.errors import UpstreamError, describe_error

    text = describe_error(
        UpstreamError(status=status, detail={"code": code}, path="/storage/x")
    )
    assert text.startswith(f"Error: {code}.")
    assert expected in text
    assert "MCP_UPSTREAM_URL" not in text


def test_partial_delete_reports_the_safe_count_only():
    from ocr_mcp.errors import UpstreamError, describe_error

    detail = {"code": "storage_delete_partial", "deleted_objects": 3, "secret": "x"}
    text = describe_error(
        UpstreamError(status=503, detail=detail, path="/storage/archive")
    )
    assert text.endswith("deleted_objects=3.")
    assert "secret" not in text


def test_delete_input_models_require_confirm():
    from ocr_mcp.inputs_storage import DeleteStorageArchiveInput, DeleteStorageFileInput

    for model, params in [
        (DeleteStorageArchiveInput, {"prefix": "p/"}),
        (DeleteStorageFileInput, {"key": "p/f"}),
    ]:
        with pytest.raises(Exception):
            model(**params)


def test_archive_summary_payload_accepts_unprocessed_pages():
    from ocr_mcp.responses import StorageArchiveSummaryPayload

    base = ARCHIVE_LIST["items"][0]
    assert StorageArchiveSummaryPayload.model_validate(base).unprocessed_pages == []
    full = {**base, "unprocessed_pages": [3]}
    assert StorageArchiveSummaryPayload.model_validate(full).unprocessed_pages == [3]


async def test_delete_uses_the_storage_deadline():
    import asyncio

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"deleted_objects": 1})

    server, ctx, _ = storage_harness(slow, storage_timeout=0.05)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_delete_storage_archive",
            {"params": {"prefix": DETAIL_PREFIX, "confirm": "job123AB"}},
        )
    assert result.is_error
    assert "storage request timed out" in result.content[0].text


async def test_download_recovery_route_encodes_the_key():
    key = "a&b.pdf__20261012T100000000000Z__job123AB/x.pdf"
    result, _ = await download(
        b"x" * 2048, "application/pdf", key=key, chunk_max_result_bytes=1024
    )
    assert result.is_error
    assert "key=a%26b.pdf" in result.content[0].text


async def test_binary_error_body_is_bounded():
    class Counting(httpx.AsyncByteStream):
        yielded = 0

        async def __aiter__(self):
            for _ in range(64):
                self.yielded += 1
                yield b"x" * 65536

    stream = Counting()
    server, ctx, _ = storage_harness(lambda request: httpx.Response(500, stream=stream))
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_download_storage_file", {"params": {"key": KEY}}
        )
    assert result.is_error
    assert stream.yielded <= 2
