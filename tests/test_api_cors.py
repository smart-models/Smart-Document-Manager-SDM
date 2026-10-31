"""Opt-in CORS: preflights and ACAO headers only when origins are configured."""

import httpx
from test_mcp_schema import FakePipeline, FakeRecognizer

from ocr_server import create_app
from ocrserver.config import Settings


async def test_no_cors_headers_by_default():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/health", headers={"Origin": "http://example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_for_null_origin_bypasses_auth():
    app = create_app(
        Settings(cors_origins="null", api_tokens="secret:test"),
        FakePipeline(),
        FakeRecognizer(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.options(
            "/parse",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


async def test_wildcard_origin_allows_any():
    app = create_app(Settings(cors_origins="*"), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/health", headers={"Origin": "http://example"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


async def test_storage_delete_preflight_allows_authorization():
    app = create_app(
        Settings(cors_origins="http://browser", api_tokens="secret:test"),
        FakePipeline(),
        FakeRecognizer(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.options(
            "/storage/archive",
            headers={
                "Origin": "http://browser",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


async def test_origin_list_filters_unknown_origins():
    app = create_app(
        Settings(cors_origins="http://a, http://b"),
        FakePipeline(),
        FakeRecognizer(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        allowed = await client.get("/health", headers={"Origin": "http://b"})
        denied = await client.get("/health", headers={"Origin": "http://c"})
    assert allowed.headers["access-control-allow-origin"] == "http://b"
    assert "access-control-allow-origin" not in denied.headers
