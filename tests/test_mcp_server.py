"""Server auth, transport, packaged documents and prompt registration."""

import base64
import hashlib
import io
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import yaml
from ocr_mcp.auth import build_auth_settings
from ocr_mcp.config import ConfigError, Settings, load_settings, repo_env_file
from ocr_mcp.resources.docs import (
    COPIED_DOCUMENTS,
    DOC_RESOURCES,
    docs_directory,
    html_to_text,
)
from ocr_mcp.server import _SERVER_INSTRUCTIONS, configure_package_logging, create_app
from ocr_mcp.tool_map import TOOL_ROUTES
from test_mcp_tools import HEALTH, PNG, harness, successful

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"allow_unauthenticated_local": True, "listen_host": "0.0.0.0"},
        {"api_tokens_raw": "same:test", "upstream_client_token": "same"},
        {"api_tokens_raw": "malformed-secret"},
        {"api_tokens_raw": "a:test", "upstream_url": "https://user:password@test"},
    ],
)
def test_unsafe_or_incomplete_configuration_refused(overrides, monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("MCP_") or name == "UPSTREAM_CLIENT_TOKEN":
            monkeypatch.delenv(name)
    with pytest.raises(ConfigError) as error:
        Settings(_env_file=None, **overrides)
    assert "malformed-secret" not in str(error.value)
    assert "password" not in str(error.value)


async def test_http_session_rejects_missing_wrong_and_non_ascii_tokens():
    settings = Settings(
        _env_file=None,
        api_tokens_raw="session-secret:test",
        upstream_client_token="upstream-test",
    )
    app = create_app(settings, transport=httpx.MockTransport(successful))
    headers = {"Accept": "application/json, text/event-stream"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8021"
        ) as client,
    ):
        for value in (None, b"Bearer wrong", b"Bearer \xff"):
            auth = {b"authorization": value} if value else {}
            response = await client.post(
                "/mcp", json=payload, headers={**headers, **auth}
            )
            assert response.status_code == 401
            assert "session-secret" not in response.text
        good = {**headers, "Authorization": "Bearer session-secret"}
        response = await client.post("/mcp", json=payload, headers=good)
        assert response.status_code == 200
        for extra in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
            denied = await client.post("/mcp", json=payload, headers={**good, **extra})
            assert denied.status_code in {400, 403, 421}


async def test_resources_and_prompts_are_real_and_references_resolve():
    async with harness(successful) as (client, _):
        resources = (await client.list_resources()).resources
        assert {str(resource.uri) for resource in resources} == set(DOC_RESOURCES) | {
            "ocr://metrics"
        }
        for resource in resources:
            read = await client.read_resource(str(resource.uri))
            assert read.contents[0].text
        prompts = (await client.list_prompts()).prompts
        assert {prompt.name for prompt in prompts} == {
            "ocr_extract_document",
            "ocr_diagnose",
            "ocr_prepare_rag",
        }
        rag = next(prompt for prompt in prompts if prompt.name == "ocr_prepare_rag")
        # The budget is the user's choice: the prompt must never default one.
        required = {argument.name: argument.required for argument in rag.arguments}
        assert required["max_tokens"] is True
        for name, arguments in [
            (
                "ocr_extract_document",
                {"source": "invoice-unique.pdf", "objective": "extract totals"},
            ),
            ("ocr_diagnose", {"symptom": "unique timeout symptom"}),
            (
                "ocr_prepare_rag",
                {"source": "unique-structured.json", "max_tokens": "768"},
            ),
        ]:
            rendered = await client.get_prompt(name, arguments)
            text = "\n".join(message.content.text for message in rendered.messages)
            for value in arguments.values():
                assert value in text
            named_tools = set(re.findall(r"\bocr_[a-z][a-z0-9_]*\b", text))
            assert named_tools <= set(TOOL_ROUTES)
            uris = set(re.findall(r"docs://[a-z/]+", text))
            assert uris <= set(DOC_RESOURCES)


@pytest.mark.parametrize(
    "overrides,host,status",
    [
        ({}, "localhost:8021", 200),
        ({}, "localhost", 200),
        ({"bind_host": "localhost"}, "127.0.0.1:8021", 200),
        ({"port": 8123}, "localhost:8123", 200),
        ({"port": 8123}, "localhost:8021", 421),
        ({"allowed_hosts_raw": "proxy.example:8123"}, "localhost:8021", 421),
        (
            {"allowed_hosts_raw": "proxy.example:8123"},
            "proxy.example:8123",
            200,
        ),
        ({"bind_host": "192.0.2.10"}, "localhost:8021", 421),
        ({"bind_host": "192.0.2.10"}, "192.0.2.10:8021", 200),
        ({"bind_host": "::1"}, "[::1]:8021", 200),
        ({"bind_host": "::1"}, "[::1]:8022", 421),
    ],
)
async def test_http_host_aliases_respect_port_and_explicit_allowlist(
    overrides, host, status
):
    values = {
        "api_tokens_raw": "host-test:local",
        "upstream_client_token": "upstream-test",
        "allowed_hosts_raw": "",
        **overrides,
    }
    app = create_app(
        Settings(_env_file=None, **values),
        transport=httpx.MockTransport(successful),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8021"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={
                "Host": host,
                "Authorization": "Bearer host-test",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "host-test", "version": "1"},
                },
            },
        )
        assert response.status_code == status


def test_skill_yaml_length_references_and_packaged_documents():
    skill = ROOT / "ocr-skill/smart-document-manager"
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    assert match is not None, "SKILL.md must start with delimited YAML frontmatter"
    frontmatter = yaml.safe_load(match.group(1))
    assert isinstance(frontmatter, dict), "Skill frontmatter must be a YAML mapping"
    assert frontmatter["name"] == "smart-document-manager"
    assert frontmatter["name"] in _SERVER_INSTRUCTIONS, "server must point at the skill"
    assert len(text.splitlines()) < 500  # Anthropic SKILL.md limit
    for tool in TOOL_ROUTES:
        assert tool in text
    sources = {name: ROOT / source for name, source in COPIED_DOCUMENTS.items()}
    assert {row[0] for row in DOC_RESOURCES.values()} == set(sources)
    assert {f"guide/{path.name}" for path in (ROOT / "guide").glob("*.html")} == {
        name for name in COPIED_DOCUMENTS if name.startswith("guide/")
    }
    for filename, source in sources.items():
        assert (docs_directory() / filename).read_bytes() == (source).read_bytes()
    for directory in (ROOT / "ocr-mcp-server/ocr_mcp", skill):
        for file in directory.rglob("*"):
            if file.suffix in {".py", ".md"}:
                assert not re.search(
                    r"__[A-Z][A-Z_]+__", file.read_text(encoding="utf-8")
                ), file


def test_canonical_frontend_assets_and_endpoint_parity():
    manifest = json.loads(
        (ROOT / "frontend/theme-manifest.json").read_text(encoding="utf-8")
    )
    assert len([name for name in manifest if name.endswith(".woff2")]) == 5
    for name, expected in manifest.items():
        actual = (ROOT / "frontend/dist/assets" / name).read_bytes()
        assert hashlib.sha256(actual).hexdigest() == expected
    api_js = (ROOT / "frontend/dist/assets/api.js").read_text(encoding="utf-8")
    for path in ("/health", "/stats", "/metrics", "/parse", "/ocr"):
        assert f'"{path}"' in api_js
    assert "sessionStorage.setItem" in api_js
    assert "localStorage.setItem" not in api_js


@pytest.mark.parametrize(
    "url",
    [
        "http://private:bad",
        "http://private:0",
        "http://private:70000",
        "http://[::1",
        "http://[bad]/",
        "http://user:private@[::1]:bad",
    ],
)
def test_upstream_url_rejections_are_sanitized(url):
    with pytest.raises(ConfigError, match="MCP_UPSTREAM_URL") as caught:
        load_settings(_env_file=None, api_tokens_raw="a:test", upstream_url=url)
    assert "private" not in str(caught.value)
    assert url not in str(caught.value)


def test_ipv6_metadata_and_unique_token_labels():
    settings = Settings(_env_file=None, api_tokens_raw="a:test", bind_host="::1")
    assert str(build_auth_settings(settings).issuer_url) == "http://[::1]:8021"
    assert "[::1]:8021" in settings.allowed_hosts
    with pytest.raises(ConfigError, match="label") as caught:
        Settings(_env_file=None, api_tokens_raw="private-one:test,private-two:test")
    assert "private" not in str(caught.value)


async def test_static_tokens_advertise_no_oauth_metadata():
    settings = Settings(_env_file=None, api_tokens_raw="meta-fixture:ci")
    app = create_app(settings, transport=httpx.MockTransport(successful))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8021"
        ) as client,
    ):
        metadata = await client.get("/.well-known/oauth-protected-resource")
        assert metadata.status_code == 404
        denied = await client.post("/mcp", json=INIT, headers=ACCEPT)
        assert denied.status_code == 401
        assert "resource_metadata" not in denied.headers["www-authenticate"]
        allowed = await client.post(
            "/mcp",
            json=INIT,
            headers={**ACCEPT, "Authorization": "Bearer meta-fixture"},
        )
        assert allowed.status_code == 200


def test_installed_package_reads_no_implicit_env_file(tmp_path):
    assert repo_env_file(tmp_path) is None
    (tmp_path / "ocr_server.py").write_text("", encoding="utf-8")
    assert repo_env_file(tmp_path) == tmp_path / ".env"


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "body-test", "version": "1"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def degraded_upstream(request):
    if request.url.path == "/health":
        return httpx.Response(200, json={**HEALTH, "status": "degraded"})
    return httpx.Response(500, json={"detail": {"code": "internal_error"}})


async def call_over_http(settings, tool, params, *, ensure_ascii=False):
    """Open a session over the real ASGI app and send one tools/call body."""
    app = create_app(settings, transport=httpx.MockTransport(degraded_upstream))
    token = next(iter(settings.api_tokens))
    headers = {**ACCEPT, "Authorization": f"Bearer {token}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8021"
        ) as client,
    ):
        opened = await client.post("/mcp", json=INIT, headers=headers)
        assert opened.status_code == 200
        session = {
            **headers,
            "Content-Type": "application/json",
            "mcp-session-id": opened.headers["mcp-session-id"],
            "MCP-Protocol-Version": "2025-06-18",
        }
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session,
        )
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"params": params}},
        }
        return await client.post(
            "/mcp",
            content=json.dumps(call, ensure_ascii=ensure_ascii),
            headers=session,
        )


def test_request_body_cap_covers_file_and_rag_limits():
    default = Settings(_env_file=None, allow_unauthenticated_local=True)
    assert default.max_request_body_bytes == 41_943_040 + 65_536
    small = Settings(
        _env_file=None,
        allow_unauthenticated_local=True,
        max_file_bytes=1_048_576,
        chunk_max_argument_bytes=1_048_576,
    )
    assert small.max_request_body_bytes == 3 * 1_048_576 + 65_536


async def test_http_accepts_a_five_mib_base64_upload():
    settings = Settings(_env_file=None, api_tokens_raw="body-fixture:ci")
    image = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)
    response = await call_over_http(
        settings,
        "ocr_recognize_image",
        {"image_base64": base64.b64encode(image).decode()},
    )
    assert response.status_code == 200
    assert "OCR backend unavailable" in response.text


async def test_http_refuses_a_body_over_the_derived_cap():
    settings = Settings(
        _env_file=None,
        api_tokens_raw="body-fixture:ci",
        max_file_bytes=1_048_576,
        chunk_max_argument_bytes=1_048_576,
    )
    response = await call_over_http(
        settings,
        "ocr_recognize_image",
        {"image_base64": "A" * int(3.5 * 1024 * 1024)},
    )
    assert response.status_code == 413


async def test_http_accepts_escaped_non_ascii_parsed_document_under_the_argument_cap():
    settings = Settings(
        _env_file=None,
        api_tokens_raw="body-fixture:ci",
        max_file_bytes=1_048_576,
        chunk_max_argument_bytes=1_048_576,
    )
    block = {"type": "text", "bbox": [0, 0, 1, 1], "content": "文" * 300_000}
    parsed = {"pages": [{"page": 1, "width": 1, "height": 1, "blocks": [block]}]}
    response = await call_over_http(
        settings,
        "ocr_create_rag_chunks",
        {"parsed_document": parsed, "options": {"max_tokens": 512}},
        ensure_ascii=True,
    )
    assert response.status_code == 200
    assert "Request body too large" not in response.text
    assert "mcp_input_limit" not in response.text


def test_html_to_text_keeps_structure_and_drops_assets():
    text = html_to_text(
        "<html><head><style>x{}</style></head><body><h2>Title</h2><p>A  b</p>"
        "<ul><li>one</li></ul><table><tr><td>k</td><td>v</td></tr></table>"
        "<pre>  x = 1\n  y</pre><script>bad()</script></body></html>"
    )
    lines = text.splitlines()
    assert {"## Title", "A b", "- one", "k | v", "  x = 1", "  y"} <= set(lines)
    assert "bad()" not in text and "x{}" not in text and text.endswith("\n")


GUIDES = [
    "getting-started",
    "cli",
    "environment",
    "architecture",
    "document-pipeline",
    "rag-chunking",
]


@pytest.mark.parametrize("name", GUIDES)
async def test_guide_resources_serve_readable_text(name):
    async with harness(successful) as (client, _):
        read = await client.read_resource(f"docs://guide/{name}")
    text = read.contents[0].text
    html = (docs_directory() / f"guide/{name}.html").read_text(encoding="utf-8")
    assert read.contents[0].mime_type == "text/markdown"
    assert not any(
        marker in text for marker in ("<style", "@font-face", "base64,", "<script")
    )
    assert len(text) < len(html) // 4
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
    title = " ".join(re.sub(r"<[^>]+>", " ", h1).split())
    assert f"# {title}" in text.splitlines()


async def test_each_tool_call_logs_name_outcome_and_duration_never_arguments():
    # Read what the package handler prints: caplog listens on the root logger,
    # which this package does not propagate to.
    configure_package_logging()
    handler = logging.getLogger("ocr_mcp").handlers[0]
    printed = io.StringIO()
    previous = handler.setStream(printed)
    try:
        async with harness(successful) as (client, _calls):
            await client.call_tool(
                "ocr_parse_document", {"params": {"image_base64": PNG}}
            )
            await client.call_tool("ocr_parse_document", {"params": {}})
            await client.call_tool(
                "ocr_parse_document", {"params": {"invented_option": True}}
            )
    finally:
        handler.setStream(previous)
    lines = printed.getvalue()
    assert re.search(r"tool 'ocr_parse_document' ok \d+\.\ds", lines), lines
    assert len(re.findall(r"tool 'ocr_parse_document' error \d+\.\ds", lines)) == 2
    assert PNG not in lines
    assert "invented_option" not in lines
