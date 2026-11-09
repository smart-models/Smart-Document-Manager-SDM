"""An archive-only upstream is healthy without vLLM: only inference is refused."""

import httpx
import pytest
from test_chunk_mcp import INPUT, RAG
from test_mcp_rag_storage import RAG_REFINE, RAG_SPLIT, REFINE_PARAMS
from test_mcp_storage import (
    ARCHIVE_DETAIL,
    ARCHIVE_LIST,
    KEY,
    _client,
    storage_harness,
)
from test_mcp_tools import HEALTH, PNG, harness

ARCHIVE_ONLY_HEALTH = {
    **HEALTH,
    "mode": "archive_only",
    "vllm": "disabled",
    "ocr_enabled": False,
}
PREFIX = ARCHIVE_LIST["items"][0]["prefix"]
REFUSAL = {
    "code": "ocr_disabled_archive_only",
    "message": "OCR inference is disabled in archive-only mode.",
}
SPLIT_PARAMS = {
    "source_key": REFINE_PARAMS["source_key"],
    "source_sha256": REFINE_PARAMS["source_sha256"],
    "options": REFINE_PARAMS["options"],
    "group": [0, [0, 0, 10]],
}


def archive_only_upstream(request):
    """Answer like an archive-only API: health fine, storage works, OCR gone."""
    path = request.url.path
    canned = {
        "/health": ARCHIVE_ONLY_HEALTH,
        "/stats": {"queue_size": 0, "active": 0},
        "/storage/archives": ARCHIVE_LIST,
        "/storage/archive": ARCHIVE_DETAIL,
        "/storage/rag/refine": RAG_REFINE,
        "/storage/rag/split": RAG_SPLIT,
        "/chunk": RAG,
    }
    if path in canned:
        return httpx.Response(200, json=canned[path])
    if path == "/storage/file":
        return httpx.Response(
            200,
            content=b"# page one\n",
            headers={"content-type": "text/markdown; charset=utf-8"},
        )
    if path == "/storage/archive.zip":
        return httpx.Response(
            200,
            content=b"PK\x05\x06" + bytes(18),
            headers={"content-type": "application/zip"},
        )
    if path in {"/parse", "/ocr", "/parse/chunks"}:
        return httpx.Response(503, json={"detail": REFUSAL})
    return httpx.Response(404, json={"detail": {"code": "unexpected_route"}})


async def test_health_reports_archive_only_as_successful_diagnostic_data():
    async with harness(archive_only_upstream) as (client, calls):
        result = await client.call_tool(
            "ocr_check_health", {"params": {"response_format": "json"}}
        )
        assert not result.is_error, result.content[0].text
        health = result.structured_content
        assert health["status"] == "healthy"
        assert health["mode"] == "archive_only"
        assert health["vllm"] == "disabled"
        assert health["ocr_enabled"] is False
        assert "mcp" in health
        assert [request.url.path for request in calls] == ["/health"]


@pytest.mark.parametrize(
    "tool,params",
    [
        ("ocr_parse_document", {"image_base64": PNG}),
        ("ocr_recognize_image", {"image_base64": PNG}),
        (
            "ocr_create_rag_chunks",
            {"image_base64": PNG, "options": {"max_tokens": 512}},
        ),
    ],
)
async def test_inference_is_refused_before_any_post_without_retry_hints(tool, params):
    async with harness(archive_only_upstream) as (client, calls):
        result = await client.call_tool(tool, {"params": params})
        assert result.is_error and result.structured_content == {}
        text = result.content[0].text
        assert "archive-only" in text and "disabled" in text
        assert "retry" not in text.lower() and "wait for" not in text.lower()
        assert "ocr_check_health" not in text
        assert [(r.method, r.url.path) for r in calls] == [("GET", "/health")]


async def test_a_degraded_archive_only_api_is_still_explained_as_archive_only():
    def degraded(request):
        return httpx.Response(200, json={**ARCHIVE_ONLY_HEALTH, "status": "degraded"})

    async with harness(degraded) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"image_base64": PNG}}
        )
        assert result.is_error
        assert "archive-only" in result.content[0].text
        assert "OCR backend unavailable" not in result.content[0].text
        assert len(calls) == 1


async def test_parsed_document_chunking_and_stats_never_ask_for_model_health():
    async with harness(archive_only_upstream) as (client, calls):
        chunks = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"parsed_document": INPUT, "options": {"max_tokens": 512}}},
        )
        assert not chunks.is_error, chunks.content[0].text
        stats = await client.call_tool("ocr_get_stats", {"params": {}})
        assert not stats.is_error, stats.content[0].text
        assert [(r.method, r.url.path) for r in calls] == [
            ("POST", "/chunk"),
            ("GET", "/stats"),
        ]


@pytest.mark.parametrize(
    "tool,params",
    [
        ("ocr_list_storage_archives", {"limit": 10}),
        ("ocr_get_storage_archive", {"prefix": PREFIX}),
        ("ocr_download_storage_file", {"key": KEY}),
        ("ocr_download_storage_zip", {"prefix": PREFIX}),
        ("ocr_refine_rag_storage", REFINE_PARAMS),
        ("ocr_split_rag_group", SPLIT_PARAMS),
    ],
)
async def test_storage_tools_work_and_never_consult_model_health(tool, params):
    server, ctx, calls = storage_harness(archive_only_upstream)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            tool, {"params": {**params, "response_format": "json"}}
        )
        assert not result.is_error, result.content[0].text
    assert calls and all(r.url.path.startswith("/storage/") for r in calls)


@pytest.mark.parametrize(
    "tool,params",
    [
        ("ocr_delete_storage_archive", {"prefix": PREFIX, "confirm": "wrong"}),
        ("ocr_delete_storage_file", {"key": KEY, "confirm": "page-1.md "}),
    ],
)
async def test_delete_confirmation_gates_still_refuse_before_http(tool, params):
    server, ctx, calls = storage_harness(archive_only_upstream)
    async with _client(server, ctx) as client:
        result = await client.call_tool(tool, {"params": params})
        assert result.is_error
    assert calls == []


async def test_a_direct_rest_refusal_reads_as_a_deliberate_mode_not_an_outage():
    # The preflight normally stops first; when the API switches mode between
    # the preflight and the POST, the 503 code still names the state.
    def switched(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        return httpx.Response(503, json={"detail": REFUSAL})

    async with harness(switched) as (client, calls):
        result = await client.call_tool(
            "ocr_recognize_image", {"params": {"image_base64": PNG}}
        )
        assert result.is_error
        text = result.content[0].text
        assert "ocr_disabled_archive_only" in text and "archive-only" in text
        assert [r.url.path for r in calls] == ["/health", "/ocr"]
