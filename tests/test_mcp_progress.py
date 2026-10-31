"""Parse progress relayed from the OCR API as MCP progress notifications.

These tests use the real Streamable HTTP app: the SDK's in-process client
delivers progress through a direct callback and sends no progress token, so
only a JSON-RPC transport exercises what MCP clients actually send.
"""

import asyncio
import json
import time
import uuid

import httpx
import pytest
from ocr_mcp.config import Settings
from ocr_mcp.server import create_app
from ocr_mcp.tools import documents
from test_mcp_tools import HEALTH, PARSE, PNG

TOKEN = "progress-fixture"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {TOKEN}",
}
VERSION = "2025-06-18"


def snapshot(done, total=2, state="ocr"):
    return {
        "state": state,
        "pages_total": total,
        "pages_done": done,
        "pages_failed": 0,
        "pages_unprocessed": 0,
    }


def upstream(progress, *, parse_delay=0.2, parse_status=200):
    """Fake API: health, a progress route fed by ``progress`` and a slow parse.

    ``progress`` returns the response for the n-th poll; ``polls`` records the
    monotonic time of every poll and ``posts`` every parse body.
    """
    polls, posts = [], []

    async def handle(request):
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json=HEALTH)
        if path.startswith("/parse/progress/"):
            polls.append(time.monotonic())
            return progress(len(polls) - 1)
        if path == "/parse":
            posts.append(json.loads(request.content))
            await asyncio.sleep(parse_delay)
            if parse_status != 200:
                return httpx.Response(parse_status, json={"detail": "boom"})
            return httpx.Response(200, json=PARSE)
        raise AssertionError(path)

    return handle, polls, posts


def sequence(*responses):
    """Answer polls in order, then repeat the last answer."""

    def pick(index):
        item = responses[min(index, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, json={"detail": {"code": "progress"}})
        return httpx.Response(200, json=item)

    return pick


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(documents, "PROGRESS_POLL_SECONDS", 0.01)


async def parse_over_http(handler, *, progress_token="parse-1"):
    """Call ocr_parse_document over HTTP; return (notifications, result)."""
    settings = Settings(
        _env_file=None,
        api_tokens_raw=f"{TOKEN}:ci",
        upstream_client_token="upstream-test",
        retry_attempts=2,
        retry_backoff=0.001,
    )
    app = create_app(settings, transport=httpx.MockTransport(handler))
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": VERSION,
            "capabilities": {},
            "clientInfo": {"name": "progress-test", "version": "1"},
        },
    }
    params = {
        "name": "ocr_parse_document",
        "arguments": {"params": {"image_base64": PNG}},
    }
    if progress_token is not None:
        params["_meta"] = {"progressToken": progress_token}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8021"
        ) as client,
    ):
        opened = await client.post("/mcp", json=init, headers=HEADERS)
        session = {
            **HEADERS,
            "mcp-session-id": opened.headers["mcp-session-id"],
            "MCP-Protocol-Version": VERSION,
        }
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session,
        )
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
            headers=session,
        )
    messages = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    notifications = [
        (m["params"]["progress"], m["params"].get("total"), m["params"].get("message"))
        for m in messages
        if m.get("method") == "notifications/progress"
    ]
    result = next(m["result"] for m in messages if "result" in m)
    return notifications, result


async def test_parse_relays_upstream_progress_as_notifications():
    handler, _, posts = upstream(
        sequence(404, snapshot(1), snapshot(1), snapshot(2, state="done"))
    )
    seen, result = await parse_over_http(handler)
    assert [(p, t) for p, t, _ in seen] == [(1, 2), (2, 2)]
    assert seen[0][2] == "ocr: 1/2 pages"
    assert uuid.UUID(posts[0]["progress_id"]).version == 4
    assert not result["isError"]


async def test_parse_without_progress_token_never_polls():
    handler, polls, posts = upstream(sequence(snapshot(1)))
    seen, result = await parse_over_http(handler, progress_token=None)
    assert not result["isError"]
    assert seen == [] and polls == []
    assert "progress_id" not in posts[0]


async def test_parse_failure_stops_the_progress_relay():
    handler, polls, _ = upstream(
        sequence(snapshot(1)), parse_delay=0.05, parse_status=500
    )
    seen, result = await parse_over_http(handler)
    finished = time.monotonic()
    await asyncio.sleep(0.1)
    assert result["isError"]
    assert not [poll for poll in polls if poll > finished]
    assert all(progress < 2 for progress, _, _ in seen)


async def test_progress_with_unknown_total_reports_none():
    handler, _, _ = upstream(sequence(snapshot(1, total=None)))
    seen, _ = await parse_over_http(handler)
    assert seen[0][:2] == (1, None)
    assert seen[0][2] == "ocr: 1 pages"


async def test_parse_reports_completion_when_the_last_snapshot_is_never_polled():
    handler, _, _ = upstream(sequence(snapshot(1)), parse_delay=0.05)
    seen, _ = await parse_over_http(handler)
    progress = [p for p, _, _ in seen]
    assert seen[-1][:2] == (2, 2)
    assert progress == sorted(set(progress))


@pytest.mark.parametrize(
    "failure", [500, httpx.ConnectError("progress route unreachable")]
)
async def test_progress_endpoint_failures_never_change_the_parse_result(failure):
    handler, polls, _ = upstream(sequence(failure), parse_delay=0.1)
    _, result = await parse_over_http(handler)
    assert polls
    assert not result["isError"]
    assert result["structuredContent"] == PARSE
