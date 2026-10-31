"""MCP tools for stored RAG refinement and split preview."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pytest
from mcp.client import Client
from ocr_mcp.config import Settings
from ocr_mcp.server import build_server


RAG_REFINE = {
    "prefix": "parse/2026/10/12/invoice__job123/",
    "file": {
        "key": "parse/2026/10/12/invoice__job123/refined.json_rag.json",
        "name": "refined.json_rag.json",
        "size": 1024,
        "content_type": "application/json",
    },
    "chunk_count": 3,
}

RAG_SPLIT = {
    "fragments": [
        {
            "entries": [0],
            "token_count": 100,
            "status": "within",
            "notes": [],
            "member_token_counts": [100],
        }
    ],
    "warnings": [],
}


def rag_harness(handler, **overrides):
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


async def test_refine_rag_storage_forwards_body_and_returns_file():
    def respond(request):
        if request.url.path == "/storage/rag/refine":
            body = json.loads(request.content)
            assert body["source_key"].endswith("json_rag.json")
            assert body["options"]["max_tokens"] == 512
            assert len(body["groups"]) == 1
            return httpx.Response(200, json=RAG_REFINE)
        return httpx.Response(404)

    server, ctx, _ = rag_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_refine_rag_storage",
            {
                "params": {
                    "source_key": "p1:b1/json_rag.json",
                    "source_sha256": "a" * 64,
                    "execution_id": "550e8400-e29b-41d4-a716-446655440000",
                    "created_at": "2026-10-12T10:00:00Z",
                    "options": {"max_tokens": 512, "min_tokens": 1},
                    "groups": [[0, [0, 0, 10]]],
                    "response_format": "json",
                }
            },
        )
        assert not result.is_error
        assert result.structured_content == {
            **RAG_REFINE,
            "execution_id": "550e8400-e29b-41d4-a716-446655440000",
            "created_at": "2026-10-12T10:00:00Z",
        }


async def test_split_rag_group_returns_fragments():
    def respond(request):
        if request.url.path == "/storage/rag/split":
            body = json.loads(request.content)
            assert body["group"] == [0, [0, 0, 10]]
            return httpx.Response(200, json=RAG_SPLIT)
        return httpx.Response(404)

    server, ctx, _ = rag_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool(
            "ocr_split_rag_group",
            {
                "params": {
                    "source_key": "p1:b1/json_rag.json",
                    "source_sha256": "a" * 64,
                    "options": {"max_tokens": 512, "min_tokens": 1},
                    "group": [0, [0, 0, 10]],
                    "response_format": "json",
                }
            },
        )
        assert not result.is_error
        assert result.structured_content == RAG_SPLIT


def test_rag_input_models_reject_malformed_plan_entries():
    from ocr_mcp.inputs_rag_storage import RefineRagStorageInput

    base = {
        "source_key": "p1:b1/json_rag.json",
        "source_sha256": "a" * 64,
        "execution_id": "550e8400-e29b-41d4-a716-446655440000",
        "created_at": "2026-10-12T10:00:00Z",
        "options": {"max_tokens": 512, "min_tokens": 1},
    }
    with pytest.raises(Exception):
        RefineRagStorageInput(**base, groups=[[True]])
    with pytest.raises(Exception):
        RefineRagStorageInput(**base, groups=[[-1]])
    with pytest.raises(Exception):
        RefineRagStorageInput(**base, groups=[[0, 0, 10, "wrong", "extra"]])


REFINE_PARAMS = {
    "source_key": "invoice.pdf__20261012T100000000000Z__job123AB/json_rag.json",
    "source_sha256": "a" * 64,
    "options": {"max_tokens": 512, "min_tokens": 1},
    "groups": [[0]],
}


async def refine_call(params):
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=RAG_REFINE)

    server, ctx, _ = rag_harness(respond)
    async with _client(server, ctx) as client:
        result = await client.call_tool("ocr_refine_rag_storage", {"params": params})
    return result, sent[0]


async def test_refine_fills_missing_execution_id_and_created_at():
    result, sent = await refine_call(REFINE_PARAMS)
    assert not result.is_error
    assert uuid.UUID(sent["execution_id"]).version == 4
    assert datetime.fromisoformat(sent["created_at"]).tzinfo is not None
    assert result.structured_content["execution_id"] == sent["execution_id"]
    assert result.structured_content["created_at"] == sent["created_at"]


async def test_refine_keeps_the_callers_execution_id_and_created_at():
    ids = {
        "execution_id": "550e8400-e29b-41d4-a716-446655440000",
        "created_at": "2026-10-18T12:00:00Z",
    }
    result, sent = await refine_call({**REFINE_PARAMS, **ids})
    assert {name: sent[name] for name in ids} == ids
    assert {name: result.structured_content[name] for name in ids} == ids
