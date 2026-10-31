"""Exercise the real route inventory, including hidden and mounted routes."""

import pytest
from fastapi.routing import APIRoute
from ocr_mcp.tool_map import (
    BROWSER_ONLY_ROUTES,
    FRAMEWORK_PATHS,
    HIDDEN_ROUTES,
    RESOURCE_ONLY_ROUTES,
    STATIC_MOUNTS,
    TOOL_ROUTES,
)
from starlette.routing import Mount
from test_mcp_schema import FakePipeline, FakeRecognizer

from ocr_server import create_app
from ocrserver.config import Settings as ApiSettings


@pytest.fixture(params=[(True, True), (True, False), (False, True), (False, False)])
def app(request):
    frontend, swagger = request.param
    application = create_app(
        ApiSettings(expose_frontend=frontend, expose_swagger=swagger),
        FakePipeline(),
        FakeRecognizer(),
    )
    application.state.expected_frontend = frontend
    return application


def test_every_operation_is_exposed_and_no_route_is_stale(app):
    visible = {
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
        if method.upper()
        in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
    }
    declared = set().union(
        *TOOL_ROUTES.values(), RESOURCE_ONLY_ROUTES, BROWSER_ONLY_ROUTES
    )
    assert visible == declared


def test_every_hidden_route_and_mount_is_acknowledged(app):
    hidden = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and not route.include_in_schema
    }
    expected_frontend = app.state.expected_frontend
    assert hidden == (set(HIDDEN_ROUTES) if expected_frontend else set())
    mounts = {route.path for route in app.routes if isinstance(route, Mount)}
    assert mounts == (STATIC_MOUNTS if expected_frontend else set())
    framework = {
        route.path for route in app.routes if not isinstance(route, (APIRoute, Mount))
    }
    assert framework == (FRAMEWORK_PATHS if app.openapi_url else set())


async def test_tools_are_registered():
    from mcp.client import Client
    from ocr_mcp.config import Settings
    from ocr_mcp.server import build_server

    server, ctx = build_server(
        Settings(
            _env_file=None,
            api_tokens_raw="test:local",
            upstream_client_token="upstream-test",
        )
    )
    try:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == set(TOOL_ROUTES)
    finally:
        await ctx.client.aclose()
