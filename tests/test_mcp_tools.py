"""MCP protocol tests with fake upstreams, including refusal and retry paths."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from ocr_mcp.config import Settings
from ocr_mcp.errors import ToolError
from ocr_mcp.formatting import ResponseFormat, render
from ocr_mcp.server import build_server

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
    "A8AAQUBAScY42YAAAAASUVORK5CYII="
)
HEALTH = {
    "status": "healthy",
    "vllm": "reachable",
    "model": "test-model",
    "pipeline_version": "v1.6",
    "version": "1.0.0",
    "autotune": {"action": "off"},
}
PARSE = {
    "pages": [
        {
            "page": 1,
            "width": 1,
            "height": 1,
            "markdown": "# Invoice",
            "blocks": [],
            "images": [PNG],
            "image_names": ["figure.png"],
            "error": None,
        },
        {"page": 2, "markdown": None, "blocks": None, "error": "page timeout"},
    ],
    "model_name": "test-model",
    "pipeline_version": "v1.6",
    "processing_time_ms": 4.2,
    "warnings": [
        {
            "code": "page_failed",
            "severity": "warning",
            "message": "page timeout",
            "target": {"field": "page", "index": 1},
            "details": None,
        }
    ],
}
OCR = {
    "result": "Invoice 123",
    "task": "ocr",
    "boxes": None,
    "model_name": "test-model",
    "processing_time_ms": 2.3,
    "warnings": [],
}

LOCAL_LIMITS = {
    "input_roots": [],
    "max_file_bytes": 31_457_280,
    "max_request_body_bytes": 42_008_576,
}


@asynccontextmanager
async def harness(handler, **overrides):
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
        assert request.headers["authorization"] == "Bearer upstream-test"
        return handler(request)

    server, ctx = build_server(settings, transport=httpx.MockTransport(record))
    try:
        async with Client(server) as client:
            yield client, calls
    finally:
        await ctx.client.aclose()


def successful(request):
    if request.url.path == "/health":
        return httpx.Response(200, json=HEALTH)
    if request.url.path == "/stats":
        return httpx.Response(200, json={"queue_size": 2.0, "active": 3.0})
    if request.url.path == "/parse":
        return httpx.Response(200, json=PARSE)
    if request.url.path == "/ocr":
        task = json.loads(request.content)["task"]
        return httpx.Response(200, json={**OCR, "task": task})
    if request.url.path == "/metrics":
        return httpx.Response(200, text="# HELP ocr_active Active\nocr_active 3\n")
    raise AssertionError(request.url.path)


@pytest.mark.parametrize(
    "tool,params,expected",
    [
        ("ocr_check_health", {}, {**HEALTH, "mcp": LOCAL_LIMITS}),
        ("ocr_get_stats", {}, {"queue_size": 2.0, "active": 3.0}),
        ("ocr_parse_document", {"image_base64": PNG}, PARSE),
        ("ocr_recognize_image", {"image_base64": PNG}, OCR),
    ],
)
async def test_formats_preserve_complete_payload(tool, params, expected):
    async with harness(successful) as (client, _calls):
        for fmt in ("markdown", "json"):
            result = await client.call_tool(
                tool, {"params": {**params, "response_format": fmt}}
            )
            assert not result.is_error
            assert result.structured_content == expected
            text = result.content[0].text
            if fmt == "json":
                assert json.loads(text) == expected
            else:
                assert text.startswith("# ")
                if tool == "ocr_parse_document":
                    assert "Invoice" in text and "Page failed" in text
                    assert "page_failed" in text
                if tool == "ocr_recognize_image":
                    assert "Invoice 123" in text


@pytest.mark.parametrize("fmt", ["markdown", "json"])
@pytest.mark.parametrize("text", ["partial text", ""])
async def test_truncated_ocr_preserves_partial_text_and_warning_without_retry(
    fmt, text
):
    payload = {
        **OCR,
        "result": text,
        "warnings": [
            {
                "code": "generation_truncated",
                "severity": "warning",
                "message": (
                    "Generation stopped at a token limit; result may be incomplete."
                ),
                "target": {"field": "result"},
                "details": {"finish_reason": "length"},
            }
        ],
    }

    def respond(request):
        if request.url.path == "/ocr":
            return httpx.Response(200, json=payload)
        return successful(request)

    async with harness(respond) as (client, calls):
        result = await client.call_tool(
            "ocr_recognize_image",
            {"params": {"image_base64": PNG, "response_format": fmt}},
        )
        assert not result.is_error
        assert result.structured_content == payload
        rendered = result.content[0].text
        if fmt == "json":
            assert json.loads(rendered) == payload
        else:
            assert text in rendered
            assert "generation_truncated" in rendered
        assert [request.url.path for request in calls] == ["/health", "/ocr"]


@pytest.mark.parametrize(
    "task", ["ocr", "table", "formula", "chart", "spotting", "seal"]
)
async def test_all_recognition_tasks_reach_the_api(task):
    async with harness(successful) as (client, calls):
        result = await client.call_tool(
            "ocr_recognize_image",
            {
                "params": {
                    "image_base64": PNG,
                    "task": task,
                }
            },
        )
        assert not result.is_error
        assert result.structured_content["task"] == task
        assert json.loads(calls[-1].content) == {"image_base64": PNG, "task": task}
        assert [request.url.path for request in calls] == ["/health", "/ocr"]


async def test_all_parse_options_and_pdf_source_are_forwarded():
    source = base64.b64encode(b"%PDF-1.7\nfixture").decode()
    params = {
        "pdf_base64": source,
        "output": "json",
        "max_pages": 2,
        "dpi": 150,
        "table_format": "markdown",
        "include_images": True,
        "json_rag": True,
    }
    async with harness(successful) as (client, calls):
        result = await client.call_tool("ocr_parse_document", {"params": params})
        assert not result.is_error
        assert json.loads(calls[-1].content) == params


async def test_default_dpi_and_page_limit_are_not_overridden():
    async with harness(successful) as (client, calls):
        await client.call_tool("ocr_parse_document", {"params": {"image_base64": PNG}})
        body = json.loads(calls[-1].content)
        assert body == {
            "image_base64": PNG,
            "output": "both",
            "table_format": "html",
            "include_images": False,
            "json_rag": False,
        }


async def test_data_uri_is_normalized_without_losing_bytes():
    async with harness(successful) as (client, calls):
        result = await client.call_tool(
            "ocr_recognize_image",
            {"params": {"image_base64": f"data:image/png;base64,{PNG}"}},
        )
        assert not result.is_error
        assert json.loads(calls[-1].content)["image_base64"] == PNG


async def test_health_degraded_is_successful_diagnostic_data():
    def degraded(request):
        return httpx.Response(200, json={**HEALTH, "status": "degraded"})

    async with harness(degraded) as (client, calls):
        result = await client.call_tool("ocr_check_health", {"params": {}})
        assert not result.is_error
        assert result.structured_content["status"] == "degraded"
        assert "degraded" in result.content[0].text
        assert len(calls) == 1


@pytest.mark.parametrize(
    "tool,params,message",
    [
        ("ocr_parse_document", {}, "exactly one"),
        (
            "ocr_parse_document",
            {"image_base64": PNG, "pdf_base64": "QQ=="},
            "exactly one",
        ),
        ("ocr_parse_document", {"image_base64": "not-base64!"}, "Invalid base64"),
        ("ocr_parse_document", {"image_base64": ""}, "empty"),
        ("ocr_parse_document", {"pdf_base64": PNG}, "must contain a PDF"),
        ("ocr_parse_document", {"file_path": "scan.pdf"}, "absolute"),
        ("ocr_recognize_image", {"image_base64": "JVBERi0xLjcK"}, "images only"),
    ],
)
async def test_source_rules_refuse_before_http(tool, params, message):
    async with harness(successful) as (client, calls):
        result = await client.call_tool(tool, {"params": params})
        assert result.is_error
        assert result.structured_content == {}
        assert message in result.content[0].text
        assert calls == []


async def test_local_files_must_be_inside_configured_roots(tmp_path):
    allowed = tmp_path / "input"
    allowed.mkdir()
    file = allowed / "scan.png"
    file.write_bytes(base64.b64decode(PNG))
    outside = tmp_path / "private.png"
    outside.write_bytes(file.read_bytes())
    async with harness(successful, input_roots_raw=str(allowed)) as (client, calls):
        refused = await client.call_tool(
            "ocr_recognize_image", {"params": {"file_path": str(outside)}}
        )
        assert refused.is_error and calls == []
        assert "MCP_INPUT_ROOTS" in refused.content[0].text
        # The remedy must not read as an invitation to retype the file as base64.
        assert "base64 produced by a tool" in refused.content[0].text
        result = await client.call_tool(
            "ocr_recognize_image", {"params": {"file_path": str(file)}}
        )
        assert not result.is_error
        assert json.loads(calls[-1].content)["image_base64"] == PNG


async def test_oversized_base64_refused_before_http():
    async with harness(successful, max_file_bytes=2) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"image_base64": PNG}}
        )
        assert result.is_error and calls == []
        assert "MCP_MAX_FILE_BYTES" in result.content[0].text


@pytest.mark.parametrize("padding", ["leading", "trailing"])
async def test_padded_path_refusal_explains_literal_whitespace(tmp_path, padding):
    path = str(tmp_path / "missing.png")
    path = f"  {path}" if padding == "leading" else f"{path}  "
    async with harness(successful, input_roots_raw=str(tmp_path)) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"file_path": path}}
        )
        assert result.is_error and calls == []
        assert "spaces are preserved" in result.content[0].text
        assert "accidental padding" in result.content[0].text


async def test_file_name_with_spaces_remains_valid(tmp_path):
    file = tmp_path / "invoice scan.png"
    file.write_bytes(base64.b64decode(PNG))
    async with harness(successful, input_roots_raw=str(tmp_path)) as (client, calls):
        result = await client.call_tool(
            "ocr_recognize_image", {"params": {"file_path": str(file)}}
        )
        assert not result.is_error
        assert json.loads(calls[-1].content)["image_base64"] == PNG


@pytest.mark.parametrize("tool", ["ocr_parse_document", "ocr_recognize_image"])
async def test_preflight_deadline_refuses_before_inference(tool):
    async def stalled(request):
        await asyncio.Event().wait()

    async with harness(stalled, diagnostic_timeout=0.03) as (client, calls):
        result = await asyncio.wait_for(
            client.call_tool(tool, {"params": {"image_base64": PNG}}),
            timeout=1,
        )
        assert result.is_error and result.structured_content == {}
        assert "diagnostic request timed out" in result.content[0].text
        assert "may still be running" not in result.content[0].text
        assert [request.url.path for request in calls] == ["/health"]


@pytest.mark.parametrize("tool", ["ocr_parse_document", "ocr_recognize_image"])
async def test_degraded_backend_prevents_inference(tool):
    def degraded(request):
        assert request.url.path == "/health"
        return httpx.Response(200, json={**HEALTH, "status": "degraded"})

    async with harness(degraded) as (client, calls):
        result = await client.call_tool(tool, {"params": {"image_base64": PNG}})
        assert result.is_error
        assert result.structured_content == {}
        assert "OCR backend unavailable" in result.content[0].text
        assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 401, 413, 422, 429, 503, 504])
@pytest.mark.parametrize(
    "tool,path", [("ocr_parse_document", "/parse"), ("ocr_recognize_image", "/ocr")]
)
async def test_inference_failures_are_actionable_and_never_retried(status, tool, path):
    def failure(request):
        if request.url.path == "/health":
            return successful(request)
        return httpx.Response(status, json={"detail": "private-token-must-not-leak"})

    async with harness(failure) as (client, calls):
        result = await client.call_tool(tool, {"params": {"image_base64": PNG}})
        assert result.is_error and result.structured_content == {}
        assert result.content[0].text.startswith("Error:")
        assert "private-token" not in result.content[0].text
        assert [request.url.path for request in calls] == ["/health", path]


@pytest.mark.parametrize("tool", ["ocr_check_health", "ocr_get_stats"])
async def test_read_failure_and_unknown_fields(tool):
    async with harness(lambda r: httpx.Response(503, json={})) as (client, calls):
        result = await client.call_tool(tool, {"params": {}})
        assert result.is_error and result.structured_content == {}
        calls.clear()
        refused = await client.call_tool(tool, {"params": {"invented_option": True}})
        assert refused.is_error and calls == []


async def test_timeout_is_not_retried_and_metrics_read_propagates_failure():
    def timeout(request):
        if request.url.path == "/health":
            return successful(request)
        raise httpx.ReadTimeout("private upstream hostname")

    async with harness(timeout) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"image_base64": PNG}}
        )
        assert result.is_error and "may still be running" in result.content[0].text
        assert "private upstream hostname" not in result.content[0].text
        assert len(calls) == 2

    async with harness(lambda r: httpx.Response(401, json={})) as (client, _):
        with pytest.raises(MCPError, match="Error reading resource ocr://metrics"):
            await client.read_resource("ocr://metrics")


@pytest.mark.parametrize("fmt", ["markdown", "json"])
@pytest.mark.parametrize(
    "tool,path,params,payload",
    [
        ("ocr_check_health", "/health", {}, {**HEALTH, "status": "invented"}),
        ("ocr_check_health", "/health", {}, {**HEALTH, "autotune": []}),
        ("ocr_get_stats", "/stats", {}, {"queue_size": "2", "active": 1}),
        (
            "ocr_parse_document",
            "/parse",
            {"image_base64": PNG},
            {**PARSE, "pages": None},
        ),
        (
            "ocr_parse_document",
            "/parse",
            {"image_base64": PNG},
            {**PARSE, "warnings": ["private"]},
        ),
        (
            "ocr_parse_document",
            "/parse",
            {"image_base64": PNG},
            {**PARSE, "pages": [{"page": "private"}]},
        ),
        (
            "ocr_parse_document",
            "/parse",
            {"image_base64": PNG},
            {
                **PARSE,
                "pages": [
                    {
                        "page": 1,
                        "blocks": [
                            {
                                "type": "text",
                                "bbox": "private",
                                "content": "x",
                                "order": None,
                            }
                        ],
                    }
                ],
            },
        ),
        ("ocr_recognize_image", "/ocr", {"image_base64": PNG}, {**OCR, "result": None}),
        (
            "ocr_recognize_image",
            "/ocr",
            {"image_base64": PNG},
            {**OCR, "boxes": [["private", 2, 3, 4]]},
        ),
        ("ocr_recognize_image", "/ocr", {"image_base64": PNG}, {}),
    ],
)
async def test_malformed_payloads_fail_in_both_formats(
    fmt, tool, path, params, payload
):
    def respond(request):
        return (
            httpx.Response(200, json=payload)
            if request.url.path == path
            else successful(request)
        )

    async with harness(respond) as (client, _):
        result = await client.call_tool(
            tool, {"params": {**params, "response_format": fmt}}
        )
        assert result.is_error and result.structured_content == {}
        assert f"Unexpected {path} payload" in result.content[0].text
        assert "private" not in result.content[0].text


@pytest.mark.parametrize("payload", [None, [], "private", {**HEALTH, "status": None}])
async def test_invalid_health_never_reaches_inference(payload):
    async with harness(lambda _: httpx.Response(200, json=payload)) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"image_base64": PNG}}
        )
        assert result.is_error
        assert "Unexpected /health payload" in result.content[0].text
        assert [request.url.path for request in calls] == ["/health"]


@pytest.mark.parametrize("prefix", [b" \n\t\v\f", b"\xef\xbb\xbf", b" \xef\xbb\xbf\n"])
async def test_pdf_prefixes_match_api_detector(prefix):
    source = base64.b64encode(prefix + b"%PDF-1.7\nfixture").decode()
    async with harness(successful) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"pdf_base64": source}}
        )
        assert not result.is_error
        assert json.loads(calls[-1].content)["pdf_base64"] == source


async def test_parse_document_forwards_json_rag_and_returns_it():
    payload = {**PARSE, "json_rag": {"chunks": [{"id": 0, "text": "x"}]}}

    def respond(request):
        if request.url.path == "/parse":
            body = json.loads(request.content)
            assert body.get("json_rag") is True
            return httpx.Response(200, json=payload)
        return successful(request)

    async with harness(respond) as (client, _):
        result = await client.call_tool(
            "ocr_parse_document",
            {"params": {"image_base64": PNG, "json_rag": True, "output": "json"}},
        )
        assert not result.is_error
        assert result.structured_content == payload


@pytest.mark.parametrize("fmt", list(ResponseFormat))
@pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), (1, 2)])
def test_non_json_values_never_diverge_between_text_and_structured_content(fmt, value):
    with pytest.raises(ToolError, match="not valid JSON"):
        render({"value": value}, fmt, title="test", rows=lambda _: [])


@pytest.mark.parametrize("fmt", ["markdown", "json"])
async def test_valid_extra_fields_and_empty_ocr_text_survive_unchanged(fmt):
    payload = {**OCR, "result": "", "extension": {"version": 2, "values": [None, True]}}

    def respond(request):
        if request.url.path == "/ocr":
            return httpx.Response(200, json=payload)
        return successful(request)

    async with harness(respond) as (client, _):
        result = await client.call_tool(
            "ocr_recognize_image",
            {
                "params": {
                    "image_base64": PNG,
                    "response_format": fmt,
                }
            },
        )
        assert not result.is_error
        assert result.structured_content == payload


def test_json_presentation_is_compact():
    result = render({"a": [1, 2]}, ResponseFormat.JSON, title="t", rows=lambda p: [])
    assert result.content[0].text == '{"a":[1,2]}'


async def test_health_reports_local_input_roots_and_limits(tmp_path):
    async with harness(successful, input_roots_raw=str(tmp_path)) as (client, _):
        result = await client.call_tool("ocr_check_health", {"params": {}})
    assert result.structured_content["mcp"] == {
        "input_roots": [str(tmp_path.resolve())],
        "max_file_bytes": 31_457_280,
        "max_request_body_bytes": 42_008_576,
    }


async def test_health_without_input_roots_reports_an_empty_list():
    async with harness(successful) as (client, _):
        result = await client.call_tool("ocr_check_health", {"params": {}})
    assert result.structured_content["mcp"]["input_roots"] == []


async def test_no_tool_schema_suggests_a_container_mount():
    async with harness(successful) as (client, _):
        tools = (await client.list_tools()).tools
    assert all("/inputs" not in json.dumps(tool.input_schema) for tool in tools)
