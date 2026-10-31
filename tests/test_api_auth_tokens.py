"""OCR_SERVER_API_TOKENS takes token:label entries; the service token joins them."""

import io
import logging
from contextlib import contextmanager

import httpx
import pytest
from test_mcp_schema import FakePipeline, FakeRecognizer

from ocr_server import create_app
from ocrserver.config import Settings, load_settings, parse_api_tokens


def test_parse_api_tokens_maps_each_token_to_its_label():
    assert parse_api_tokens(" one:alice , two:bob.v2 ,") == {
        "one": "alice",
        "two": "bob.v2",
    }
    assert parse_api_tokens("") == {}


@pytest.mark.parametrize(
    "raw",
    [
        "secret-value",
        "secret-value:",
        ":label",
        "secret-value:@mcp",
        "secret-value:bad label",
        "secret-value:a,secret-value:b",
        "secret-value:a,other-secret:a",
    ],
)
def test_malformed_entries_are_refused_without_echoing_the_token(raw):
    with pytest.raises(ValueError) as error:
        parse_api_tokens(raw)
    assert "secret-value" not in str(error.value)
    assert "other-secret" not in str(error.value)


def test_legacy_single_token_variable_fails_fast():
    with pytest.raises(ValueError, match="OCR_SERVER_API_TOKENS"):
        load_settings({"OCR_SERVER_API_TOKEN": "old-secret"}, None)
    assert load_settings({"OCR_SERVER_API_TOKEN": " "}, None).auth_tokens == {}


def test_settings_load_list_and_service_token_from_environment():
    settings = load_settings(
        {
            "OCR_SERVER_API_TOKENS": "console-secret:chicco",
            "UPSTREAM_CLIENT_TOKEN": "mcp-secret",
        },
        None,
    )
    assert settings.auth_tokens == {
        "console-secret": "chicco",
        "mcp-secret": "@mcp",
    }
    assert "console-secret" not in repr(settings)
    assert "mcp-secret" not in repr(settings)


def test_service_token_listed_by_the_operator_is_refused():
    with pytest.raises(ValueError, match="UPSTREAM_CLIENT_TOKEN"):
        Settings(api_tokens="mcp-secret:agent", service_token="mcp-secret")


def test_empty_list_disables_auth_even_with_a_service_token():
    assert Settings(service_token="mcp-secret").auth_tokens == {}


async def _status(settings: Settings, authorization: str | None) -> int:
    app = create_app(settings, FakePipeline(), FakeRecognizer())
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        return (await client.get("/stats", headers=headers)).status_code


@pytest.mark.parametrize(
    "authorization,status",
    [
        ("Bearer one", 200),
        ("Bearer two", 200),
        ("Bearer mcp-secret", 200),
        ("Bearer one:alice", 401),
        ("Bearer three", 401),
        ("bearer one", 401),
        (None, 401),
    ],
)
async def test_every_listed_token_and_the_service_token_authenticate(
    authorization, status
):
    settings = Settings(api_tokens="one:alice,two:bob", service_token="mcp-secret")
    assert await _status(settings, authorization) == status


async def test_empty_list_serves_without_authentication():
    assert await _status(Settings(), None) == 200


@contextmanager
def printed_by(logger_name: str):
    """Capture what the handler create_app attaches actually prints.

    caplog listens on the root logger with its own handler, so it would pass
    even when these loggers print nothing -- the very bug guarded here.
    """
    create_app(Settings(), FakePipeline(), FakeRecognizer())
    handlers = logging.getLogger(logger_name).handlers
    assert handlers, f"{logger_name} has no handler: INFO lines would be dropped"
    buffer = io.StringIO()
    previous = handlers[0].setStream(buffer)
    try:
        yield buffer
    finally:
        handlers[0].setStream(previous)


@pytest.mark.parametrize(
    "package,name", [("ocr_server", "ocr_server"), ("ocrserver", "ocrserver.storage")]
)
def test_create_app_prints_info_lines_of_its_own_loggers(package, name):
    # Paddle attaches a handler to the root logger inside the API process; a
    # propagating record would print twice.
    root_printed = io.StringIO()
    root_handler = logging.StreamHandler(root_printed)
    logging.getLogger().addHandler(root_handler)
    try:
        with printed_by(package) as printed:
            logging.getLogger(name).info("probe line")
    finally:
        logging.getLogger().removeHandler(root_handler)
    assert "probe line" in printed.getvalue()
    assert "probe line" not in root_printed.getvalue()


async def test_authenticated_requests_log_the_label_never_the_token():
    settings = Settings(api_tokens="tok-alice:alice", service_token="tok-mcp")
    with printed_by("ocr_server") as printed:
        assert await _status(settings, "Bearer tok-mcp") == 200
        assert await _status(settings, "Bearer tok-alice") == 200
        assert await _status(settings, "Bearer tok-wrong") == 401
    lines = printed.getvalue()
    assert "request authenticated: @mcp GET /stats" in lines
    assert "request authenticated: alice GET /stats" in lines
    assert lines.count("request authenticated") == 2
    assert "tok-" not in lines


def test_access_log_drops_the_query_string():
    # Storage queries carry archive prefixes, which include the original name.
    create_app(Settings(), FakePipeline(), FakeRecognizer())
    access = logging.getLogger("uvicorn.access")
    printed = io.StringIO()
    handler = logging.StreamHandler(printed)
    access.addHandler(handler)
    level = access.level
    access.setLevel(logging.INFO)
    try:
        # The exact call Uvicorn's HTTP protocols make.
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "172.19.0.1:5000",
            "DELETE",
            "/storage/archive?prefix=private-name.pdf__20261029T085143Z__8TiUxmkk%2F",
            "1.1",
            200,
        )
    finally:
        access.removeHandler(handler)
        access.setLevel(level)
    line = printed.getvalue()
    assert '172.19.0.1:5000 - "DELETE /storage/archive HTTP/1.1" 200' in line
    assert "private-name" not in line
