"""New optional RAG tool through MCP registration and the HTTP client seam."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from test_mcp_tools import HEALTH, PNG, harness


ROOT = Path(__file__).resolve().parents[1]
RAG = json.loads(
    (ROOT / "tests/fixtures/ocr-rag-chunks.example.json").read_text(encoding="utf-8")
)
INPUT = {"pages": [{"page": 1, "blocks": [{"type": "text", "content": "Body"}]}]}


async def test_rag_tool_reuses_json_without_health_or_duplicate_text():
    def upstream(request):
        assert request.url.path == "/chunk"
        body = json.loads(request.content)
        assert body["parsed_document"] == INPUT
        assert body["options"]["max_tokens"] == 512
        return httpx.Response(200, json=RAG)

    async with harness(upstream) as (client, calls):
        tools = (await client.list_tools()).tools
        tool = next(tool for tool in tools if tool.name == "ocr_create_rag_chunks")
        normative = json.loads(
            (ROOT / "tests/fixtures/ocr-rag-chunks.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert tool.output_schema == normative
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": INPUT,
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert not result.is_error
        assert result.structured_content == RAG
        assert RAG["chunks"][0]["text"] not in result.content[0].text
        assert len(calls) == 1


async def test_rag_tool_chunks_a_stored_parse_by_key_without_client_transfer():
    key = "invoice.pdf__20261018T120000000000Z__job123AB/json_ocr.json"

    def upstream(request):
        if request.url.path == "/storage/file":
            assert request.url.params["key"] == key
            assert request.url.params["preview"] == "false"
            return httpx.Response(200, json=INPUT)
        assert request.url.path == "/chunk"
        body = json.loads(request.content)
        assert body["parsed_document"] == INPUT
        assert body["accept_truncated"] is False
        return httpx.Response(200, json=RAG)

    async with harness(upstream) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"source_key": key, "options": {"max_tokens": 512}}},
        )
        assert not result.is_error
        assert result.structured_content == RAG
        # The stored copy is fetched by the MCP itself; no health preflight.
        assert [call.url.path for call in calls] == ["/storage/file", "/chunk"]
        refused = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"source_key": "x/document.md", "options": {"max_tokens": 512}}},
        )
        assert refused.is_error
        assert "json_ocr.json" in refused.content[0].text
        assert len(calls) == 2


async def test_rag_budget_error_keeps_details_without_echoing_private_text():
    detail = {
        "code": "protected_block_exceeds_budget",
        "message": "private source text",
        "details": {
            "page": 2,
            "source_key": "p2:b3",
            "type": "table",
            "max_tokens": 512,
            "required_tokens": 1598,
            "suggested_max_tokens": 1598,
            "minimum_feasible_max_tokens": None,
            "conflicting_option": "preserve_tables",
            "secret": "private source text",
        },
    }
    async with harness(lambda _: httpx.Response(422, json={"detail": detail})) as (
        client,
        calls,
    ):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": INPUT,
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert result.is_error and result.structured_content == {}
        error = json.loads(result.content[0].text)
        assert error["code"] == "protected_block_exceeds_budget"
        assert error["details"]["required_tokens"] == 1598
        assert error["details"]["conflicting_option"] == "preserve_tables"
        assert "private source text" not in result.content[0].text
        assert len(calls) == 1


async def test_document_mode_preflights_once_and_uses_the_separate_chunk_timeout():
    def upstream(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        assert request.url.path == "/parse/chunks"
        assert request.extensions["timeout"]["read"] == 780
        body = json.loads(request.content)
        assert body["image_base64"] == PNG
        assert body["max_pages"] == 2
        assert body["dpi"] == 150
        assert "output" not in body and "include_images" not in body
        return httpx.Response(200, json=RAG)

    async with harness(upstream, http_timeout=1) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "image_base64": PNG,
                    "options": {"max_tokens": 512},
                    "max_pages": 2,
                    "dpi": 150,
                }
            },
        )
        assert not result.is_error
        assert [request.url.path for request in calls] == [
            "/health",
            "/parse/chunks",
        ]


async def test_argument_limit_counts_utf8_and_refuses_before_any_upstream_call():
    source = {
        "pages": [{"page": 1, "blocks": [{"type": "text", "content": "é" * 300}]}]
    }
    async with harness(
        lambda _: httpx.Response(200, json=RAG), chunk_max_argument_bytes=500
    ) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": source,
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert result.is_error and result.structured_content == {}
        error = json.loads(result.content[0].text)
        assert error["code"] == "mcp_input_limit"
        assert error["limit_bytes"] == 500
        assert error["actual_bytes"] > 500
        assert error["recovery_route"] == "/chunk"
        assert calls == []


@pytest.mark.parametrize("presentation", ["markdown", "json"])
async def test_result_limit_includes_summary_envelope_and_sdk_metadata(
    presentation,
):
    args = {
        "params": {
            "parsed_document": INPUT,
            "options": {"max_tokens": 512},
            "response_format": presentation,
        }
    }
    async with harness(lambda _: httpx.Response(200, json=RAG)) as (client, _):
        result = await client.call_tool("ocr_create_rag_chunks", args)
        encoded = json.dumps(
            result.model_dump(mode="json", by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        limit = len(encoded)
    async with harness(
        lambda _: httpx.Response(200, json=RAG),
        chunk_max_result_bytes=limit - 1,
    ) as (client, calls):
        rejected = await client.call_tool("ocr_create_rag_chunks", args)
        assert rejected.is_error and rejected.structured_content == {}
        error = json.loads(rejected.content[0].text)
        assert error["code"] == "mcp_output_limit"
        assert error["actual_bytes"] == limit
        assert error["recovery_route"] == "/chunk"
        assert len(calls) == 1
    async with harness(
        lambda _: httpx.Response(200, json=RAG), chunk_max_result_bytes=limit
    ) as (client, _):
        accepted = await client.call_tool("ocr_create_rag_chunks", args)
        assert not accepted.is_error
        assert len(accepted.content[0].text.encode("utf-8")) <= 4096


async def test_oversized_upstream_stream_is_closed_before_reading_the_whole_body():
    class Stream(httpx.AsyncByteStream):
        sent = 0
        closed = False

        async def __aiter__(self):
            for _ in range(20):
                self.sent += 1
                yield b"x" * 1024

        async def aclose(self):
            self.closed = True

    stream = Stream()
    async with harness(
        lambda _: httpx.Response(200, stream=stream),
        chunk_max_result_bytes=1024,
    ) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": INPUT,
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert result.is_error and result.structured_content == {}
        assert json.loads(result.content[0].text)["code"] == "mcp_output_limit"
        assert stream.closed and stream.sent < 20
        assert len(calls) == 1


async def test_server_file_is_rechecked_after_base64_preparation(tmp_path):
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF-" + b"x" * 300)
    async with harness(
        lambda _: httpx.Response(200, json=RAG),
        chunk_max_argument_bytes=512,
        input_roots_raw=str(tmp_path),
    ) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "file_path": str(source),
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert result.is_error and result.structured_content == {}
        error = json.loads(result.content[0].text)
        assert error["code"] == "mcp_input_limit"
        assert error["recovery_route"] == "/parse/chunks"
        assert error["actual_bytes"] > 512
        assert calls == []


async def test_source_output_limit_explains_rest_recovery_without_repeating_inference():
    def upstream(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        return httpx.Response(200, json=RAG)

    async with harness(upstream, chunk_max_result_bytes=1024) as (
        client,
        calls,
    ):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "image_base64": PNG,
                    "options": {"max_tokens": 512},
                }
            },
        )
        detail = json.loads(result.content[0].text)
        assert result.is_error and detail["code"] == "mcp_output_limit"
        assert detail["recovery_route"] == "/parse/chunks"
        assert "may need to run again" in detail["message"]
        assert [request.url.path for request in calls] == [
            "/health",
            "/parse/chunks",
        ]


async def test_chunk_capacity_is_actionable_and_never_retried():
    response = {"detail": {"code": "chunk_capacity_exceeded", "message": "private"}}
    async with harness(lambda _: httpx.Response(503, json=response)) as (
        client,
        calls,
    ):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": INPUT,
                    "options": {"max_tokens": 512},
                }
            },
        )
        detail = json.loads(result.content[0].text)
        assert result.is_error and detail["code"] == "chunk_capacity_exceeded"
        assert "retry deliberately" in detail["message"]
        assert "private" not in result.content[0].text
        assert len(calls) == 1


async def test_total_chunk_http_deadline_closes_stream_and_never_retries():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b"{}"

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    async with harness(
        lambda _: httpx.Response(200, stream=stream), chunk_http_timeout=0.01
    ) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": INPUT,
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert result.is_error and result.structured_content == {}
        assert "timed out" in result.content[0].text
        assert stream.closed and len(calls) == 1


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"options": {"max_tokens": 32}}, "exactly one"),
        (
            {
                "parsed_document": INPUT,
                "image_base64": PNG,
                "options": {"max_tokens": 32},
            },
            "exactly one",
        ),
        (
            {
                "parsed_document": INPUT,
                "dpi": 150,
                "options": {"max_tokens": 32},
            },
            "document input",
        ),
        (
            {
                "parsed_document": INPUT,
                "options": {"max_tokens": 32, "overlap_tokens": 32},
            },
            "less than",
        ),
        (
            {
                "image_base64": PNG,
                "accept_truncated": True,
                "options": {"max_tokens": 32},
            },
            "parsed_document",
        ),
    ],
)
async def test_rag_cross_field_refusals_are_tool_errors_before_http(
    arguments, expected
):
    async with harness(lambda _: httpx.Response(200, json=RAG)) as (
        client,
        calls,
    ):
        result = await client.call_tool("ocr_create_rag_chunks", {"params": arguments})
        assert result.is_error and result.structured_content == {}
        assert expected in result.content[0].text
        assert calls == []


def test_rag_contract_bundle_and_inputs_match_the_runtime_api():
    from ocr_mcp.config import Settings
    from ocr_mcp.rag_contract import (
        ChunkOptions,
        CreateRagChunksInput,
        PARSED_DOCUMENT_SCHEMA,
    )
    from ocr_server import create_app
    from ocrserver.config import Settings as ApiSettings

    schema = create_app(ApiSettings()).openapi()
    components = schema["components"]["schemas"]

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(components[value["$ref"].split("/")[-1]])
            return {key: expand(item) for key, item in value.items()}
        return value

    assert (
        ChunkOptions.model_json_schema()["properties"]
        == components["ChunkOptions"]["properties"]
    )
    assert PARSED_DOCUMENT_SCHEMA == expand(components["ParsedDocument"])
    assert (ROOT / "ocr-mcp-server/ocr_mcp/rag.schema.json").read_bytes() == (
        ROOT / "tests/fixtures/ocr-rag-chunks.schema.json"
    ).read_bytes()
    fields = set(CreateRagChunksInput.model_fields)
    api_fields = set(components["ChunkRequest"]["properties"])
    api_fields.update(components["ParseChunksJsonRequest"]["properties"])
    assert fields == api_fields | {"file_path", "response_format", "source_key"}
    settings = Settings(_env_file=None, api_tokens_raw="fixture:test")
    assert (
        settings.chunk_max_argument_bytes == settings.chunk_max_result_bytes == 8388608
    )
    assert settings.chunk_http_timeout == 780


async def test_rag_tool_accepts_base64_with_trailing_newline():
    def degraded(request):
        return httpx.Response(200, json={**HEALTH, "status": "degraded"})

    async with harness(degraded) as (client, _):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"image_base64": PNG + "\n", "options": {"max_tokens": 512}}},
        )
    text = result.content[0].text
    assert "Invalid base64" not in text
    assert "OCR backend unavailable" in text


@pytest.mark.parametrize("fmt", ["markdown", "json"])
async def test_rag_summary_names_the_flagged_pages_of_the_parse_it_chunks(fmt):
    from test_mcp_tools import QUALITY_WARNINGS

    document = {**INPUT, "warnings": QUALITY_WARNINGS}
    async with harness(lambda request: httpx.Response(200, json=RAG)) as (client, _):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": document,
                    "options": {"max_tokens": 512},
                    "response_format": fmt,
                }
            },
        )
        plain = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "parsed_document": INPUT,
                    "options": {"max_tokens": 512},
                    "response_format": fmt,
                }
            },
        )

    # The chunks, what a vector store receives, are the same either way.
    assert not result.is_error
    assert result.structured_content == RAG == plain.structured_content
    text = result.content[0].text
    summary = json.loads(text) if fmt == "json" else None
    if summary:
        assert summary["low_layout_confidence_pages"] == "1 page: 2"
        # Page 5 is named too, although its second reading was kept.
        assert summary["suspect_table_pages"] == "2 pages: 3, 5"
    else:
        assert "- low_layout_confidence_pages: 1 page: 2" in text
        assert "- suspect_table_pages: 2 pages: 3, 5" in text
    assert "no mark" in text
    for word in ("low_layout_confidence_pages", "suspect_table_pages", "no mark"):
        assert word not in plain.content[0].text


async def test_rag_summary_stays_bounded_when_many_pages_are_flagged():
    from test_mcp_tools import QUALITY_WARNINGS

    many = [
        {**QUALITY_WARNINGS[0], "target": {"field": "page", "index": index}}
        for index in range(900)
    ]
    document = {**INPUT, "warnings": many}
    async with harness(lambda request: httpx.Response(200, json=RAG)) as (client, _):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"parsed_document": document, "options": {"max_tokens": 512}}},
        )

    assert not result.is_error
    assert "900 pages, the first 20: 1, 2, 3" in result.content[0].text


async def test_rag_summary_ignores_malformed_warnings():
    document = {
        **INPUT,
        "warnings": [
            {"code": "table_suspect", "message": "no target"},
            {"code": "table_suspect", "message": "x", "target": {"index": "two"}},
            {"code": "layout_low_confidence", "message": "x", "target": None},
        ],
    }
    async with harness(lambda request: httpx.Response(200, json=RAG)) as (client, _):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"parsed_document": document, "options": {"max_tokens": 512}}},
        )

    assert "suspect_table_pages" not in result.content[0].text
