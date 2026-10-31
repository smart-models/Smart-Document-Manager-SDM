"""Diagnostic deadlines bound network waits and retries, not inference."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from ocr_mcp.client import UpstreamClient, _retry_after_delay
from ocr_mcp.config import ConfigError, load_settings
from ocr_mcp.errors import ToolError


def settings(**overrides):
    return load_settings(
        _env_file=None,
        api_tokens_raw="test-inbound:client",
        upstream_client_token="test-upstream",
        **overrides,
    )


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_diagnostic_timeout_must_be_positive(value):
    with pytest.raises(ConfigError):
        settings(diagnostic_timeout=value)


@pytest.mark.parametrize("field", ["http_timeout", "retry_backoff"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_all_time_budgets_are_finite_positive(field, value):
    with pytest.raises(ConfigError):
        settings(**{field: value})


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"2", 2),
        (b"999", 30),
        (b"\xb2", None),
        (b"-1", None),
        (b"9" * 400, None),
        (b"text", None),
        (b"Mon, 21 Sep 2026 10:00:15 GMT", 15),
        (b"Mon, 21 Sep 2026 09:00:00 GMT", 0),
    ],
)
def test_retry_after_values(raw, expected):
    response = httpx.Response(429, headers=[(b"retry-after", raw)])
    now = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
    assert _retry_after_delay(response, now=now) == expected


@pytest.mark.parametrize(
    "content", [b"private-html", b"null", b"[]", b'"private text"', b""]
)
async def test_success_requires_json_object_without_disclosing_body(content):
    client = UpstreamClient(
        settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)),
    )
    try:
        with pytest.raises(ToolError, match="Unexpected /parse payload") as caught:
            await client.request("POST", "/parse")
        assert "private" not in str(caught.value)
    finally:
        await client.aclose()


async def test_diagnostic_and_inference_use_separate_timeouts():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = UpstreamClient(
        settings(diagnostic_timeout=2.5, http_timeout=123),
        transport=httpx.MockTransport(respond),
    )
    try:
        await client.request("GET", "/health")
        await client.request("GET", "/stats")
        await client.text("/metrics")
        await client.request("POST", "/parse", json={})
        await client.request("POST", "/ocr", json={})
    finally:
        await client.aclose()
    for request in calls:
        expected = 2.5 if request.method == "GET" else 123
        assert set(request.extensions["timeout"].values()) == {expected}


@pytest.mark.parametrize("failure", ["stalled_read", "retry_after", "backoff"])
async def test_diagnostic_deadline_covers_requests_and_retry_waits(failure):
    calls = []

    async def respond(request):
        calls.append(request)
        if failure == "stalled_read":
            await asyncio.Event().wait()
        if failure == "retry_after":
            return httpx.Response(429, headers={"Retry-After": "30"})
        raise httpx.ReadTimeout("simulated", request=request)

    client = UpstreamClient(
        settings(diagnostic_timeout=0.03, retry_backoff=10),
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(ToolError, match="diagnostic request timed out"):
            await asyncio.wait_for(client.request("GET", "/health"), timeout=1)
        assert len(calls) == 1
    finally:
        await client.aclose()


async def test_diagnostic_transient_failure_can_recover_within_budget():
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "healthy"})

    client = UpstreamClient(
        settings(diagnostic_timeout=1, retry_backoff=0.001),
        transport=httpx.MockTransport(respond),
    )
    try:
        assert await client.request("GET", "/health") == {"status": "healthy"}
        assert len(calls) == 2
    finally:
        await client.aclose()


async def test_short_diagnostic_budget_does_not_cancel_inference():
    async def respond(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, json={"result": "recognized"})

    client = UpstreamClient(
        settings(diagnostic_timeout=0.001, http_timeout=123),
        transport=httpx.MockTransport(respond),
    )
    try:
        assert await client.request("POST", "/ocr", json={}) == {"result": "recognized"}
    finally:
        await client.aclose()


async def test_exhausted_diagnostic_timeouts_have_diagnostic_guidance():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("private upstream address", request=request)

    client = UpstreamClient(
        settings(diagnostic_timeout=1, retry_attempts=2, retry_backoff=0.001),
        transport=httpx.MockTransport(timeout),
    )
    try:
        with pytest.raises(ToolError, match="diagnostic request timed out") as error:
            await client.request("GET", "/health")
        assert "private upstream" not in str(error.value)
        assert len(calls) == 2
    finally:
        await client.aclose()


async def test_external_cancellation_does_not_become_a_retry_or_tool_error():
    entered = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    client = UpstreamClient(settings(), transport=httpx.MockTransport(respond))
    task = asyncio.create_task(client.request("GET", "/health"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1
    finally:
        task.cancel()
        await client.aclose()
