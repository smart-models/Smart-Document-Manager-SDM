"""Verify vLLM request handling, circuit breaking and backend failures."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from ocrserver.config import Settings
from ocrserver.vllm_client import (
    BackendUnavailable,
    CircuitBreaker,
    CompletionResult,
    VllmClient,
)


def completion(content="ok", finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
    )


def client_with(outcomes, semaphore=None):
    breaker = CircuitBreaker(threshold=1, open_seconds=0)
    breaker.record_connection_error()
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome

    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    client = VllmClient(
        Settings(), semaphore or asyncio.Semaphore(1), breaker, _client=sdk
    )
    return client, breaker, calls


def test_only_probe_owner_can_release():
    breaker = CircuitBreaker(threshold=1, open_seconds=0)
    breaker.record_connection_error()
    owner = breaker.before_request()
    assert owner is not None
    breaker.release_probe(None)
    assert breaker._probe_in_flight
    with pytest.raises(BackendUnavailable):
        breaker.before_request()
    breaker.release_probe(owner)
    newer = breaker.before_request()
    breaker.record_success(owner)
    assert breaker.state == "half_open"
    breaker.release_probe(owner)
    assert breaker._probe_in_flight
    breaker.release_probe(newer)
    assert not breaker._probe_in_flight


def test_sdk_retry_is_disabled(monkeypatch):
    options = {}

    def sdk(**kwargs):
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("ocrserver.vllm_client.AsyncOpenAI", sdk)
    VllmClient(Settings(), asyncio.Semaphore(1), CircuitBreaker())
    assert options["max_retries"] == 0


@pytest.mark.asyncio
async def test_close_releases_sdk_client():
    closed = []

    async def close():
        closed.append(True)

    client = VllmClient(
        Settings(),
        asyncio.Semaphore(1),
        CircuitBreaker(),
        _client=SimpleNamespace(close=close),
    )
    await client.aclose()
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task", ["ocr", "table", "formula", "chart", "spotting", "seal"]
)
async def test_task_pixel_limits_reach_processor(task):
    client, _, calls = client_with([completion()])
    await client.recognize("image", task, 1)
    expected_max = 1605632 if task == "spotting" else 1003520
    assert calls[0]["extra_body"] == {
        "mm_processor_kwargs": {
            "min_pixels": 112896,
            "max_pixels": expected_max,
        }
    }
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_length_preserves_text_and_does_not_retry():
    client, breaker, calls = client_with([completion("partial text", "length")])
    result = await client.recognize("image", "ocr", 1)
    assert result.text == "partial text"
    assert result.finish_reason == "length"
    assert len(calls) == 1
    assert breaker.state == "closed"
    assert not breaker._probe_in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,reason",
    [("ok", "stop"), ("ok", None), ("ok", "new_reason"), ("", "length")],
)
async def test_valid_completion_preserves_text_and_reason(text, reason):
    client, breaker, calls = client_with([completion(text, reason)])
    result = await client.recognize("image", "ocr", 1)
    assert result.text == text
    assert result.finish_reason == reason
    assert len(calls) == 1
    assert breaker.state == "closed"


@pytest.mark.asyncio
async def test_missing_finish_reason_defaults_to_none():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
    )
    client, _, calls = client_with([response])
    result = await client.recognize("image", "ocr", 1)
    assert result.text == "ok"
    assert result.finish_reason is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_releases_probe_and_does_not_retry():
    timeout = APITimeoutError(request=httpx.Request("POST", "http://model"))
    client, breaker, calls = client_with([timeout, completion()])
    with pytest.raises(APITimeoutError):
        await client.recognize("image", "ocr", 1)
    assert len(calls) == 1
    assert not breaker._probe_in_flight
    assert await client.recognize("image", "ocr", 1) == CompletionResult("ok", "stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("during_create", [False, True])
async def test_cancellation_releases_probe(during_create):
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    sem = asyncio.Semaphore(1 if during_create else 0)
    client, breaker, _ = client_with([blocked, completion()], sem)
    task = asyncio.create_task(client.recognize("image", "ocr", 1))
    if during_create:
        await entered.wait()
    else:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not breaker._probe_in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        SimpleNamespace(),
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=None),
        SimpleNamespace(choices=[SimpleNamespace()]),
        completion(None),
        completion(3),
        completion(finish_reason=3),
        completion(finish_reason=False),
        completion(finish_reason={}),
    ],
)
async def test_invalid_completion_is_not_success(bad):
    client, breaker, _ = client_with([bad, completion("")])
    with pytest.raises(BackendUnavailable, match="invalid completion"):
        await client.recognize("image", "ocr", 1)
    assert breaker.state != "closed"
    assert await client.recognize("image", "ocr", 1) == CompletionResult("", "stop")


@pytest.mark.asyncio
async def test_status_error_releases_probe():
    response = httpx.Response(500, request=httpx.Request("POST", "http://model"))
    error = APIStatusError("failed", response=response, body=None)
    client, breaker, _ = client_with([error])
    with pytest.raises(BackendUnavailable) as caught:
        await client.recognize("image", "ocr", 1)
    assert caught.value.status_code == 503
    assert not breaker._probe_in_flight


@pytest.mark.asyncio
async def test_retry_releases_slot_but_retains_probe(monkeypatch):
    error = APIConnectionError(request=httpx.Request("POST", "http://model"))
    sem = asyncio.Semaphore(1)
    client, breaker, calls = client_with([error, error], sem)

    async def backoff(_):
        assert not sem.locked()
        with pytest.raises(BackendUnavailable):
            breaker.before_request()

    monkeypatch.setattr("ocrserver.vllm_client.asyncio.sleep", backoff)
    with pytest.raises(BackendUnavailable, match="after retry") as caught:
        await client.recognize("image", "ocr", 1)
    assert caught.value.__cause__ is error
    assert len(calls) == 2
    assert not breaker._probe_in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected", [(429, 503), (503, 503), (400, 502), (401, 502)]
)
async def test_http_failures_are_controlled_without_backend_details(status, expected):
    response = httpx.Response(status, request=httpx.Request("POST", "http://model"))
    error = APIStatusError("private backend details", response=response, body=None)
    client, breaker, calls = client_with([error])
    with pytest.raises(BackendUnavailable) as caught:
        await client.recognize("image", "ocr", 1)
    assert caught.value.status_code == expected
    assert "private" not in str(caught.value)
    assert len(calls) == 1
    assert not breaker._probe_in_flight
