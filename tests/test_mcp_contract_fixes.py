"""Regression coverage for exact confirmations and agent-facing MCP contracts."""

import json
from copy import deepcopy

import httpx
import pytest
from ocr_mcp.tools.storage import slim_archive
from test_mcp_storage import ARCHIVE_DETAIL, ARCHIVE_LIST, _client, storage_harness

CURRENT = "invoice.pdf__20261018T120000000000Z__job123AB/"
LEGACY = "parse/2026/10/18/" + "a" * 32 + "/"


@pytest.mark.parametrize("fmt", ["markdown", "json"])
@pytest.mark.parametrize("padding", ["left", "right", "case", "wrong"])
@pytest.mark.parametrize("archive", [True, False])
async def test_invalid_confirm_is_refused_before_http(fmt, padding, archive):
    expected = "job123AB" if archive else "images/page-1.png"
    confirm = {
        "left": " " + expected,
        "right": expected + " ",
        "case": expected.upper(),
        "wrong": "wrong-confirmation",
    }[padding]
    server, ctx, calls = storage_harness(
        lambda _: httpx.Response(200, json={"deleted_objects": 1})
    )
    tool = "ocr_delete_storage_archive" if archive else "ocr_delete_storage_file"
    target = {"prefix": CURRENT} if archive else {"key": CURRENT + expected}
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            tool, {"params": {**target, "confirm": confirm, "response_format": fmt}}
        )
    assert result.is_error
    assert result.structured_content == {}
    assert calls == []


@pytest.mark.parametrize("prefix,confirm", [(CURRENT, "job123AB"), (LEGACY, "a" * 32)])
async def test_valid_archive_confirm_is_forwarded_once(prefix, confirm):
    server, ctx, calls = storage_harness(
        lambda _: httpx.Response(200, json={"deleted_objects": 1})
    )
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_delete_storage_archive",
            {"params": {"prefix": prefix, "confirm": confirm}},
        )
    assert not result.is_error
    assert len(calls) == 1
    assert json.loads(calls[0].content) == {"confirm": confirm}


@pytest.mark.parametrize("name", ["images/page-1.png", "nested/" + "a" * 260 + ".txt"])
async def test_nested_file_confirm_uses_relative_artifact_name(name):
    server, ctx, calls = storage_harness(
        lambda _: httpx.Response(200, json={"deleted_objects": 1})
    )
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_delete_storage_file",
            {
                "params": {
                    "key": CURRENT + name,
                    "confirm": name,
                }
            },
        )
    assert not result.is_error
    assert len(calls) == 1


@pytest.mark.parametrize(
    "key", [CURRENT + "manifest.json", CURRENT + "../secret.txt", "arbitrary/file.txt"]
)
async def test_invalid_delete_targets_never_reach_http(key):
    server, ctx, calls = storage_harness(
        lambda _: httpx.Response(200, json={"deleted_objects": 1})
    )
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_delete_storage_file",
            {"params": {"key": key, "confirm": key.rsplit("/", 1)[-1]}},
        )
    assert result.is_error
    assert calls == []


@pytest.mark.parametrize("fmt", ["markdown", "json"])
@pytest.mark.parametrize(
    "tool,params",
    [
        ("ocr_list_storage_archives", {}),
        ("ocr_get_storage_archive", {"prefix": CURRENT}),
        ("ocr_delete_storage_archive", {"prefix": CURRENT, "confirm": "job123AB"}),
        ("ocr_delete_storage_file", {"key": CURRENT + "page.md", "confirm": "page.md"}),
    ],
)
@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "private-payload-do-not-echo"},
        {"items": "private-payload-do-not-echo"},
    ],
)
async def test_malformed_storage_payload_is_a_sanitized_tool_error(
    fmt, tool, params, payload
):
    server, ctx, _ = storage_harness(lambda _: httpx.Response(200, json=payload))
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            tool, {"params": {**params, "response_format": fmt}}
        )
    assert result.is_error
    assert result.structured_content == {}
    assert "private-payload-do-not-echo" not in result.content[0].text
    assert "payload" in result.content[0].text


@pytest.mark.parametrize(
    "tool,params,payload",
    [
        ("ocr_list_storage_archives", {}, ARCHIVE_LIST),
        ("ocr_get_storage_archive", {"prefix": CURRENT}, ARCHIVE_DETAIL),
    ],
)
async def test_valid_storage_payload_preserves_extra_fields(tool, params, payload):
    expected = {**payload, "future_field": {"value": 42}}
    server, ctx, _ = storage_harness(lambda _: httpx.Response(200, json=expected))
    async with _client(server, ctx) as client:
        result = await client.call_tool(tool, {"params": params})
    assert not result.is_error
    if tool == "ocr_get_storage_archive":
        expected = slim_archive(expected)
    assert result.structured_content == expected
    assert result.structured_content["future_field"] == {"value": 42}


async def test_every_registered_tool_has_a_complete_discoverable_contract():
    server, ctx, _ = storage_harness(lambda _: httpx.Response(500))
    async with _client(server, ctx) as client:
        tools = (await client.list_tools()).tools
    assert len(tools) == 14
    for tool in tools:
        for section in (
            "Args:",
            "Returns:",
            "Use when:",
            "Don't use when:",
            "Error Handling:",
        ):
            assert section in tool.description, (tool.name, section)
        schema = tool.input_schema
        pending = [schema]
        while pending:
            node = pending.pop()
            if not isinstance(node, dict):
                continue
            for field, spec in node.get("properties", {}).items():
                if field == "params":
                    continue
                assert spec.get("description"), (tool.name, field)
                assert "e.g." in spec["description"], (tool.name, field)
            pending.extend(node.get("$defs", {}).values())


RAG_PARAMS = {
    "source_key": CURRENT + "json_rag.json",
    "source_sha256": "a" * 64,
    "options": {"min_tokens": 1, "max_tokens": 512},
}


@pytest.mark.parametrize("fmt", ["markdown", "json"])
@pytest.mark.parametrize(
    "tool,params",
    [
        (
            "ocr_refine_rag_storage",
            {
                **RAG_PARAMS,
                "execution_id": "550e8400-e29b-41d4-a716-446655440000",
                "created_at": "2026-10-18T12:00:00Z",
                "groups": [[0]],
            },
        ),
        ("ocr_split_rag_group", {**RAG_PARAMS, "group": [0]}),
    ],
)
async def test_malformed_stored_rag_response_is_sanitized(fmt, tool, params):
    server, ctx, calls = storage_harness(
        lambda _: httpx.Response(
            200, json={"unexpected": "private-payload-do-not-echo"}
        )
    )
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            tool, {"params": {**params, "response_format": fmt}}
        )
    assert result.is_error
    assert result.structured_content == {}
    assert "private-payload-do-not-echo" not in result.content[0].text
    assert len(calls) == 1


@pytest.mark.parametrize(
    "prefix",
    [
        CURRENT,
        LEGACY,
        LEGACY.replace("parse/", "ocr/"),
        LEGACY.replace("parse/", "parse-chunks/"),
        CURRENT.replace("invoice.pdf", "x" * 96),
        CURRENT.replace("20261018", "20260230"),
        LEGACY.replace("2026/10/18", "2026/02/30"),
        CURRENT + "nested/",
        CURRENT.rstrip("/"),
        "../" + CURRENT,
    ],
)
def test_archive_guard_tracks_api_layout_validation(prefix):
    from ocr_mcp.errors import ToolError
    from ocr_mcp.guardrails import require_archive_confirm
    from ocrserver.archive_names import parse_prefix

    parsed = parse_prefix(prefix)
    if parsed:
        require_archive_confirm(prefix, parsed["job"])
    else:
        with pytest.raises(ToolError):
            require_archive_confirm(prefix, "job123AB")


@pytest.mark.parametrize("prefix", [CURRENT, LEGACY])
@pytest.mark.parametrize(
    "name",
    [
        "page.md",
        "images/nested/page-1.png",
        "_private.txt",
        "-file.txt",
        "images/../page.png",
        "./page.png",
        "images//page.png",
        ".hidden",
        "manifest.json",
        "folder/manifest.json",
        "x" * 1024,
    ],
)
def test_file_guard_tracks_api_relative_name_validation(prefix, name):
    from ocr_mcp.errors import ToolError
    from ocr_mcp.guardrails import require_file_confirm
    from ocrserver.storage_browser import _key
    from fastapi import HTTPException

    try:
        _, relative = _key(prefix + name)
        allowed = relative != "manifest.json"
    except HTTPException:
        allowed = False
    if allowed:
        require_file_confirm(prefix + name, name)
    else:
        with pytest.raises(ToolError):
            require_file_confirm(prefix + name, name)


def test_storage_response_models_follow_api_fields_and_accept_wire_results():
    from ocr_mcp import responses
    from ocrserver import schemas
    from test_mcp_rag_storage import RAG_REFINE, RAG_SPLIT

    pairs = [
        (
            responses.StorageArchiveSummaryPayload,
            schemas.StorageArchiveSummary,
            ARCHIVE_LIST["items"][0],
        ),
        (responses.StorageArchiveListPayload, schemas.StorageArchiveList, ARCHIVE_LIST),
        (responses.StorageFilePayload, schemas.StorageFile, ARCHIVE_DETAIL["files"][0]),
        (
            responses.StorageArchiveDetailPayload,
            schemas.StorageArchiveDetail,
            ARCHIVE_DETAIL,
        ),
        (
            responses.StorageDeletePayload,
            schemas.StorageDeleteResponse,
            {"deleted_objects": 1},
        ),
        (
            responses.StorageRagRefinePayload,
            schemas.StorageRagRefineResponse,
            RAG_REFINE,
        ),
        (responses.StorageRagSplitPayload, schemas.StorageRagSplitResponse, RAG_SPLIT),
        (
            responses.SplitFragmentPayload,
            schemas.RagSplitFragmentModel,
            RAG_SPLIT["fragments"][0],
        ),
        (
            responses.SplitNotePayload,
            schemas.RagSplitNote,
            {
                "code": "table_split",
                "severity": "warning",
                "targets": [{"source_key": "p1:b0", "start": 0, "end": 10}],
                "parameters": [{"name": "rows", "value": 2}],
            },
        ),
        (
            responses.SplitLocationPayload,
            schemas.RagLocation,
            {"source_key": "p1:b0", "start": None, "end": None},
        ),
        (
            responses.SplitParameterPayload,
            schemas.RagParameter,
            {"name": "detail", "value": "table"},
        ),
    ]
    for mirror, api, sample in pairs:
        assert set(mirror.model_fields) == set(api.model_fields)
        assert {k for k, v in mirror.model_fields.items() if v.is_required()} == {
            k for k, v in api.model_fields.items() if v.is_required()
        }
        wire = api.model_validate(sample).model_dump(mode="json")
        mirror.model_validate(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("entries", [True]),
        ("entries", [-1]),
        ("entries", [[0, 2, 2]]),
        ("entries", [[0, 0, 2, "wrong"]]),
        ("entries", [[0, 0.5, 2]]),
        ("token_count", "100"),
        ("token_count", -1),
        ("status", "ok"),
        ("member_token_counts", [True]),
        ("notes", [{"code": "table_split"}]),
    ],
)
async def test_invalid_split_fragment_does_not_masquerade_as_success(field, value):
    from test_mcp_rag_storage import RAG_SPLIT

    payload = deepcopy(RAG_SPLIT)
    payload["fragments"][0][field] = value
    server, ctx, _ = storage_harness(lambda _: httpx.Response(200, json=payload))
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_split_rag_group",
            {
                "params": {**RAG_PARAMS, "group": [0]},
            },
        )
    assert result.is_error
    assert result.structured_content == {}


@pytest.mark.parametrize(
    "path",
    [
        ".github/AGENTS.md",
        "benchmarks/README.md",
        "benchmarks/rag_eval/README.md",
        "calibration/README.md",
        "frontend/README.md",
        "guide/README.md",
        "ocr-mcp-server/README.md",
        "ocr-skill/README.md",
        "ocrserver/README.md",
        "requirements/README.md",
        "scripts/README.md",
        "storage/README.md",
        "tests/README.md",
        "vllm/README.md",
    ],
)
def test_unit_readmes_keep_routing_preamble_and_ordered_sections(path):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    preamble = [line for line in text.splitlines() if line.startswith("> **")]
    assert len(preamble) == 4
    for line, label in zip(
        preamble, ("Read this when", "Audience", "Source of truth", "Prerequisite")
    ):
        assert line.startswith("> **" + label + "**")
    headings = [
        "Purpose and boundaries",
        "Mental model",
        "Map of the code",
        "Interface reference",
        "Configuration",
        "Behaviour contracts",
        "Capabilities and limits",
        "Recipes",
        "Verification",
        "Failure modes",
        "Invariants",
        "Extending it",
        "Related documents",
    ]
    positions = [text.index("## " + heading + "\n") for heading in headings]
    assert positions == sorted(positions)
    # The retired evals directory of the reader skill's former name stays out.
    assert "ocr-parse-json-evals/" not in text
    assert "five task tools" not in text
