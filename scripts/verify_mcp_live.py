"""Exercise deployed MCP with calibration data; never write credentials."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path

import httpx2
from dotenv import dotenv_values
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from ocr_mcp.config import ConfigError, parse_api_tokens
from ocr_mcp.resources.docs import DOC_RESOURCES
from ocr_mcp.tool_map import RESOURCE_ONLY_ROUTES, TOOL_ROUTES

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PROMPTS = {"ocr_extract_document", "ocr_diagnose", "ocr_prepare_rag"}


def require(condition: bool, message: str) -> None:
    """Keep verification enabled even when Python runs with -O."""
    if not condition:
        raise RuntimeError(message)


def live_configuration(
    env: Mapping[str, str] | None = None, env_file: Path | None = None
):
    """Environment overrides optional dotenv; diagnostics never repeat secrets."""
    config = dict(dotenv_values(env_file or ROOT / ".env"))
    config.update(os.environ if env is None else env)
    tokens = parse_api_tokens(config.get("MCP_API_TOKENS") or "")
    require(bool(tokens), "Set MCP_API_TOKENS before running live verification")
    try:
        port = int(config.get("MCP_PORT") or "8021")
        require(1 <= port <= 65535, "MCP_PORT must be between 1 and 65535")
    except (TypeError, ValueError) as exc:
        raise ConfigError("MCP_PORT must be an integer") from exc
    return next(iter(tokens)), f"http://127.0.0.1:{port}/mcp"


async def main() -> None:
    """Verify live MCP inventory, resources, inference and diagnostic workflows."""
    token, url = live_configuration()
    report = {}
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=660,
        ) as http,
        Client(
            streamable_http_client(url, http_client=http),
            read_timeout_seconds=660,
        ) as client,
    ):
        report["tools"] = sorted(
            tool.name for tool in (await client.list_tools()).tools
        )
        require(set(report["tools"]) == set(TOOL_ROUTES), "Tool inventory mismatch")
        health = await client.call_tool("ocr_check_health", {"params": {}})
        require(not health.is_error, "Health tool failed")
        require(
            health.structured_content.get("status") == "healthy",
            "OCR API is not ready",
        )
        report["health_status"] = "healthy"
        stats = await client.call_tool("ocr_get_stats", {"params": {}})
        require(not stats.is_error, "Stats tool failed")
        report["stats_checked"] = True

        resources = (await client.list_resources()).resources
        report["resources"] = sorted(str(item.uri) for item in resources)
        expected_resources = set(DOC_RESOURCES) | set(RESOURCE_ONLY_ROUTES.values())
        require(
            set(report["resources"]) == expected_resources,
            "Resource inventory mismatch",
        )
        for resource in resources:
            body = await client.read_resource(str(resource.uri))
            require(
                bool(body.contents and body.contents[0].text),
                "Resource body missing",
            )

        report["prompts"] = sorted(
            prompt.name for prompt in (await client.list_prompts()).prompts
        )
        require(set(report["prompts"]) == EXPECTED_PROMPTS, "Prompt inventory mismatch")
        for name, args in [
            (
                "ocr_extract_document",
                {"source": "plain_text.png", "objective": "extract text"},
            ),
            ("ocr_diagnose", {"symptom": "verification"}),
            ("ocr_prepare_rag", {"source": "plain_text.png", "max_tokens": "512"}),
        ]:
            prompt = await client.get_prompt(name, args)
            require(bool(prompt.messages), "Prompt messages missing")

        refused = await client.call_tool("ocr_parse_document", {"params": {}})
        require(
            refused.is_error and refused.structured_content == {},
            "Missing source was accepted",
        )
        report["source_refused"] = True
        encoded = base64.b64encode(
            (ROOT / "calibration/plain_text.png").read_bytes()
        ).decode()
        for tool in ["ocr_parse_document", "ocr_recognize_image"]:
            result = await client.call_tool(
                tool,
                {
                    "params": {
                        "image_base64": encoded,
                        "response_format": "json",
                    }
                },
            )
            require(not result.is_error, f"{tool} inference failed")
            payload = result.structured_content
            if tool == "ocr_parse_document":
                require(isinstance(payload.get("pages"), list), "Parse pages missing")
                require(
                    bool(payload["pages"])
                    and all(not page.get("error") for page in payload["pages"]),
                    "Calibration parse had page errors",
                )
                text = "\n".join(
                    page.get("markdown") or "" for page in payload["pages"]
                )
            else:
                text = payload.get("result") or ""
            require("quick brown fox" in text.lower(), "Calibration text missing")
            report[tool] = {
                "calibration_text_found": True,
                "processing_time_ms": payload.get("processing_time_ms"),
            }

    async with httpx2.AsyncClient(timeout=10) as http:
        for label, headers in (
            ("wrong_token", {"Authorization": "Bearer wrong"}),
            ("missing_token", {}),
        ):
            denied = await http.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {},
                },
            )
            require(denied.status_code == 401, f"{label} was not rejected")
            report[f"{label}_status"] = denied.status_code
    target = ROOT / "docs/verification/mcp-live.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(summary_line(report))


def summary_line(report: dict) -> str:
    """One line naming the inventory the run actually verified."""
    return (
        f"Live MCP verified: {len(report['tools'])} tools, "
        f"{len(report['resources'])} resources, {len(report['prompts'])} prompts, "
        "real parse/OCR and rejected authentication."
    )


if __name__ == "__main__":
    asyncio.run(main())
