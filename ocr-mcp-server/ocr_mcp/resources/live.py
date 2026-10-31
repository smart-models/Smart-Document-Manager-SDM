"""Live diagnostics; failed reads propagate as protocol errors."""

from mcp.server import MCPServer

from ocr_mcp.context import ToolContext


def register(mcp: MCPServer, ctx: ToolContext) -> None:
    """Register the authenticated upstream Prometheus metrics resource."""

    @mcp.resource(
        "ocr://metrics",
        name="ocr_metrics",
        title="OCR Prometheus metrics",
        description=(
            "Live raw counters and gauges. Use ocr_get_stats for a compact "
            "capacity check."
        ),
        mime_type="text/plain",
    )
    async def read_metrics() -> str:
        return await ctx.client.text("/metrics")
