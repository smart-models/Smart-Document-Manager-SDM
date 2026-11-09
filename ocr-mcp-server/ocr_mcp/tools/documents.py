"""Document parsing and focused recognition with the entire OCR surface."""

import asyncio
import contextlib
import json
import uuid

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from ocr_mcp.client import UpstreamClient
from ocr_mcp.context import ToolContext
from ocr_mcp.errors import error_result
from ocr_mcp.formatting import StructuredToolResult, bullet, render
from ocr_mcp.inputs import ParseDocumentInput, RecognizeImageInput, source_body
from ocr_mcp.responses import validate_payload
from ocr_mcp.tools.health import require_healthy

PROGRESS_POLL_SECONDS = 2.0


def progress_token(mcp_ctx: Context) -> str | int | None:
    """Return the caller's progress token, or None when it asked for none."""
    return (mcp_ctx.request_context.meta or {}).get("progress_token")


async def relay_progress(
    client: UpstreamClient,
    progress_id: str,
    mcp_ctx: Context,
    last: list[int],
    *,
    interval: float = PROGRESS_POLL_SECONDS,
) -> None:
    """Forward the API's page counters as MCP progress until cancelled.

    MCP progress must increase with every notification, so an unchanged
    counter sends nothing; ``last[0]`` holds the value reported so far. A
    failed poll (not yet registered, 5xx, unreachable) is skipped silently.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            snapshot = await client.request("GET", f"/parse/progress/{progress_id}")
            done = snapshot["pages_done"] + snapshot["pages_failed"]
        except Exception:  # noqa: BLE001 - progress is best effort
            continue
        if not isinstance(done, int) or done <= last[0]:
            continue
        total = snapshot.get("pages_total")
        total = total if isinstance(total, int) else None
        state = snapshot.get("state")
        message = (
            f"{state}: {done}/{total} pages"
            if total is not None
            else f"{state}: {done} pages"
        )
        last[0] = done
        await mcp_ctx.report_progress(done, total, message)


async def _stop(relay: asyncio.Task) -> None:
    """Cancel the relay and wait for it; its failures never reach the caller."""
    relay.cancel()
    await asyncio.wait({relay})
    if not relay.cancelled():
        relay.exception()


def _warnings(payload: dict) -> list[str]:
    return [
        f"- WARNING {warning['code']}: {warning['message']}"
        for warning in payload.get("warnings", [])
    ]


def _parse_rows(payload: dict) -> list[str]:
    rows = [bullet("Processing time (ms)", payload["processing_time_ms"])]
    for page in payload["pages"]:
        rows.append(f"\n## Page {page['page']}")
        if page.get("error"):
            rows.append(f"Page failed: {page['error']}")
        if page.get("markdown"):
            rows.append(page["markdown"])
        if page.get("blocks") is not None:
            rows.append(bullet("Blocks", len(page["blocks"])))
            if not page.get("markdown"):
                for block in page["blocks"]:
                    rows.append(f"- [{block['type']}] {block['content']}")
        if page.get("images"):
            rows.append(
                bullet("Figure crops in structured content", len(page["images"]))
            )
    return rows + _warnings(payload)


def _ocr_rows(payload: dict) -> list[str]:
    rows = [bullet("Task", payload["task"]), payload["result"]]
    if payload.get("boxes") is not None:
        rows.append(bullet("Boxes", json.dumps(payload["boxes"])))
    return rows + _warnings(payload)


def register(mcp: MCPServer, ctx: ToolContext) -> None:
    """Register document parsing and element recognition with health preflights."""

    @mcp.tool(
        name="ocr_parse_document",
        annotations={
            "title": "Parse a PDF, page image or Office document",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def parse_document(
        params: ParseDocumentInput, mcp_ctx: Context
    ) -> StructuredToolResult:
        """Extract layout, reading order and content from a PDF, image or Office file.

        Runs the document pipeline once, after a health preflight. Enabled server
        storage persists OCR artifacts; repeated calls create separate archives.
        Storage headers are not exposed in this tool's result envelope. Use this
        for full pages, multipage PDFs, mixed layouts and Office documents
        (.docx .pptx .xlsx .doc .ppt .xls .odt .odp .ods), which the server
        renders to PDF with LibreOffice first. Choose recognition
        for an already cropped element. Input files must exist on the MCP
        server inside MCP_INPUT_ROOTS; remote clients can send base64 bytes.

        Args:
            params (ParseDocumentInput):
                file_path (str|null): absolute allowed server file.
                image_base64 (str|null): image bytes, plain base64/data URI.
                pdf_base64 (str|null): PDF bytes, plain base64/data URI.
                document_base64 (str|null): PDF, image or Office bytes,
                    plain base64/data URI; the server detects the format.
                filename (str|null): original filename for storage naming
                    when sending base64; file_path supplies its own basename.
                output (str): markdown, json (blocks), or both (default).
                max_pages (int|null): cap from 1 to OCR_MAX_PAGES; null uses it.
                dpi (int|null): 50–400, PDF/Office only; null keeps server default.
                table_format (str): html (default, lossless spans) or
                    markdown (every table becomes pipes; merged cells are
                    expanded into repeated values, see tables_spans_expanded).
                include_images (bool): include figure crops; default false.
                json_rag (bool): also return the structural RAG export;
                    default false.
                response_format (str): MCP markdown (default) or json display.

        Returns:
            Markdown pages, errors and warnings, plus the complete structured
            payload; JSON display carries that same object:
            {"pages": [{"page": int, "width": int|null, "height": int|null,
              "markdown": str|null, "blocks": [{"type": str,
                "bbox": [float], "content": str, "order": int|null,
                "block_id": int|null, "group_id": int|null,
                "global_block_id": int|null, "global_group_id": int|null,
                "polygon": list|null}]|null,
              "images": [str]|null, "image_names": [str]|null,
              "error": str|null}], "model_name": str, "pipeline_version": str,
              "processing_time_ms": float, "warnings": [{"code": str,
                "severity": str, "message": str, "target": dict|null,
                "details": dict|null}], "usage": dict, "json_rag": dict|null}
            Page numbers are 1-based; bbox uses raster page coordinates.
            usage counts content tokens of parsed blocks, not model inference;
            json_rag is null unless requested.
            images are base64 figure crops, paired with image_names. A 200
            response may contain failed pages; inspect every error and warning.

        Examples:
            Use when: extracting an invoice PDF's tables and reading order.
            Use when: parsing a scanned page containing text and figures.
            Use when: extracting a .docx report or .pptx deck as Markdown.
            Don't use when: reading a cropped formula; use ocr_recognize_image.
            Don't use when: diagnosing readiness; use ocr_check_health.

        Error Handling:
            Refuses missing/multiple sources, invalid base64, oversized input
            and disallowed paths before HTTP. Degraded health prevents POST;
            an archive-only API (mode='archive_only') is refused without a
            POST and without a retry hint, since OCR is off by design.
            400/422: correct input/options; 413: reduce size; 401: fix upstream
            token; 429/503: inspect health/capacity and wait. 504/timeout may
            leave work running. No POST is automatically retried. Errors carry
            is_error=true and structured {}. See docs://ocr/reference.
            When the client sends a progress token, page progress is relayed as
            MCP progress notifications.
        """
        try:
            body = await source_body(params, ctx.settings)
            await require_healthy(ctx)
            last = [-1]
            relay = None
            if progress_token(mcp_ctx) is not None:
                body["progress_id"] = str(uuid.uuid4())
                relay = asyncio.create_task(
                    relay_progress(
                        ctx.client,
                        body["progress_id"],
                        mcp_ctx,
                        last,
                        interval=PROGRESS_POLL_SECONDS,
                    )
                )
            try:
                payload = await ctx.client.request("POST", "/parse", json=body)
            finally:
                if relay is not None:
                    await _stop(relay)
            validate_payload(payload, "/parse")
            pages = len(payload["pages"])
            if relay is not None and pages > last[0]:
                # The last page often finishes between two polls.
                with contextlib.suppress(Exception):
                    await mcp_ctx.report_progress(
                        pages, pages, f"done: {pages}/{pages} pages"
                    )
            return render(
                payload,
                params.response_format,
                title="Parsed document",
                rows=_parse_rows,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize errors at the tool boundary
            return error_result(exc)

    @mcp.tool(
        name="ocr_recognize_image",
        annotations={
            "title": "Recognize an image element",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def recognize_image(params: RecognizeImageInput) -> StructuredToolResult:
        """Recognize text, a table, formula, chart, text boxes or seal in an image.

        Runs focused recognition once, after a health preflight. Enabled server
        storage persists OCR artifacts; repeated calls create separate archives.
        Storage headers are not exposed in this tool's result envelope. The task
        selects the model prompt; the server does not crop, translate or
        summarize the input. Use document parsing for PDFs and page layout.

        Args:
            params (RecognizeImageInput):
                file_path (str|null): absolute image in server MCP_INPUT_ROOTS.
                image_base64 (str|null): base64 image or image data URI.
                filename (str|null): original filename for storage naming
                    (defaults to the file_path basename).
                task (str): ocr (default), table, formula, chart, spotting, seal.
                response_format (str): markdown (default) or json presentation.

        Returns:
            Markdown recognition text and warnings plus structured content,
            or JSON with the same schema:
            {"result": str, "task": str, "boxes": [[float]]|null,
             "model_name": str, "processing_time_ms": float,
             "warnings": [{"code": str, "severity": str, "message": str,
                            "target": dict|null, "details": dict|null}]}
            result remains the model's text (including HTML/LaTeX). boxes is
            parsed only for spotting; it can be empty. Do not assume boxes
            are pixel coordinates of the original image: spotting preprocesses
            the image and forwards model coordinates without an inverse map.

        Examples:
            Use when: converting a formula crop to LaTeX with task='formula'.
            Use when: recognizing a table, chart or seal crop.
            Don't use when: processing a PDF; use ocr_parse_document.
            Don't use when: diagnosing capacity; use ocr_get_stats.

        Error Handling:
            Exactly one source is required. PDF bytes, invalid base64, input
            over MCP_MAX_FILE_BYTES and paths outside MCP_INPUT_ROOTS are
            refused before HTTP. Degraded health prevents POST; an archive-only
            API is refused without a POST, since OCR is off by design. 400/422
            require corrected inputs; 413 smaller input; 401 an upstream token fix;
            429/503 a capacity/health check; timeout requires a deliberate retry.
            Returns is_error=true and structured {}. No automatic POST retry.
        """
        try:
            body = await source_body(params, ctx.settings)
            await require_healthy(ctx)
            payload = await ctx.client.request("POST", "/ocr", json=body)
            validate_payload(payload, "/ocr")
            return render(
                payload,
                params.response_format,
                title="Image recognition",
                rows=_ocr_rows,
            )
        except Exception as exc:  # noqa: BLE001 - sanitize errors at the tool boundary
            return error_result(exc)
