"""Cheap diagnostics that remain usable while inference is unavailable."""

from mcp.server import MCPServer

from ocr_mcp.config import Settings
from ocr_mcp.context import ToolContext
from ocr_mcp.errors import ToolError, error_result
from ocr_mcp.formatting import StructuredToolResult, bullet, render
from ocr_mcp.inputs import ToolInput
from ocr_mcp.responses import validate_payload


class CheckHealthInput(ToolInput):
    """Health presentation options."""


class GetStatsInput(ToolInput):
    """Capacity presentation options."""


def local_limits(settings: Settings) -> dict:
    """Describe what this MCP process accepts: where files may live, how big."""
    return {
        "input_roots": [str(root) for root in settings.input_roots],
        "max_file_bytes": settings.max_file_bytes,
        "max_request_body_bytes": settings.max_request_body_bytes,
    }


async def require_healthy(ctx: ToolContext) -> None:
    """Use the console's readiness rule before starting expensive inference."""
    health = await ctx.client.request("GET", "/health")
    validate_payload(health, "/health")
    if health.get("status") != "healthy":
        raise ToolError(
            "OCR backend unavailable — parsing and recognition are paused "
            "until /health reports healthy. Call ocr_check_health for "
            "details."
        )


def register(mcp: MCPServer, ctx: ToolContext) -> None:
    """Register readiness and capacity tools with compact result presentation."""

    @mcp.tool(
        name="ocr_check_health",
        annotations={
            "title": "Check OCR health",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def check_health(params: CheckHealthInput) -> StructuredToolResult:
        """Check OCR API health and inference readiness without running OCR.

        Reads the current vLLM poll and autotune summary. HTTP 200 can report
        degraded; only status='healthy' permits inference. This does not load
        models or test document recognition quality.

        Args:
            params (CheckHealthInput): response_format ('markdown' or 'json').

        Returns:
            Markdown verdict plus identical structured content, or JSON:
            {"status": str, "vllm": str, "model": str,
             "pipeline_version": str, "version": str, "autotune": dict,
             "storage": dict, "mcp": {"input_roots": [str],
             "max_file_bytes": int, "max_request_body_bytes": int}}
            status is healthy/degraded; vllm is reachable/unreachable.
            autotune always has action and may have decisions, mode, objective.
            storage reports the archive backend when the API has one. mcp is
            this MCP server: file_path must sit inside one of input_roots (an
            empty list means file_path is disabled; send base64 instead).

        Examples:
            Use when: checking readiness before a long document request.
            Use when: investigating a backend_unavailable response.
            Don't use when: measuring capacity; use ocr_get_stats.
            Don't use when: reading counters; read ocr://metrics.

        Error Handling:
            Degraded health is successful diagnostic data. Transport failures
            and invalid API responses return is_error=true and structured {}.
        """
        try:
            payload = await ctx.client.request("GET", "/health")
            validate_payload(payload, "/health")
            return render(
                {**payload, "mcp": local_limits(ctx.settings)},
                params.response_format,
                title="OCR health",
                rows=lambda p: [bullet(k, v) for k, v in p.items()],
            )
        except Exception as exc:  # noqa: BLE001 - sanitize errors at the tool boundary
            return error_result(exc)

    @mcp.tool(
        name="ocr_get_stats",
        annotations={
            "title": "Check OCR capacity",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_stats(params: GetStatsInput) -> StructuredToolResult:
        """Read current document queue and active request gauges.

        Reads /stats without running inference. queue_size counts acquired
        parse slots; active includes active parse and recognition requests.
        Neither value is a durable job ID or a completion notification.

        Args:
            params (GetStatsInput): response_format ('markdown' or 'json').

        Returns:
            Markdown fact lines plus structured content, or JSON:
            {"queue_size": float, "active": float}

        Examples:
            Use when: deciding whether to wait after queue_full or slots_full.
            Use when: checking activity after an ambiguous timeout.
            Don't use when: determining readiness; use ocr_check_health.
            Don't use when: reading historical counters; read ocr://metrics.

        Error Handling:
            401/403 require fixing UPSTREAM_CLIENT_TOKEN. Transport and API
            failures return is_error=true and structured {}; no model call.
        """
        try:
            payload = await ctx.client.request("GET", "/stats")
            validate_payload(payload, "/stats")
            return render(
                payload,
                params.response_format,
                title="OCR capacity",
                rows=lambda p: [bullet(k, v) for k, v in p.items()],
            )
        except Exception as exc:  # noqa: BLE001 - sanitize errors at the tool boundary
            return error_result(exc)
