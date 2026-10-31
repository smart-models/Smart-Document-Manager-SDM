"""Office-to-PDF preview tool."""

from __future__ import annotations

import asyncio
import base64
import hashlib

from mcp.server import MCPServer

from ocr_mcp.context import ToolContext
from ocr_mcp.errors import ToolError, error_result
from ocr_mcp.formatting import StructuredToolResult, bullet, render
from ocr_mcp.inputs import prepare_source
from ocr_mcp.inputs_office import ConvertOfficeToPdfInput


def _rows(payload: dict) -> list[str]:
    return [
        bullet("Filename", payload.get("filename")),
        bullet("Content type", payload.get("content_type")),
        bullet("SHA-256", payload.get("sha256")),
        bullet("Base64 bytes", len(payload.get("base64", ""))),
    ]


def register(mcp: MCPServer, ctx: ToolContext) -> None:
    """Register Office conversion with bounded binary PDF transfer."""

    @mcp.tool(
        name="ocr_convert_office_to_pdf",
        annotations={
            "title": "Convert an Office document to PDF",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def convert_office_to_pdf(
        params: ConvertOfficeToPdfInput,
    ) -> StructuredToolResult:
        """Convert an Office/OpenDocument file to a preview PDF without running OCR.

        Uses server LibreOffice conversion capacity. Parsing Office documents already
        converts them internally; do not call this first merely to extract text.

        Args:
            params (ConvertOfficeToPdfInput): exactly one file_path (absolute path
                under MCP_INPUT_ROOTS) or document_base64 (Office bytes/data URI);
                filename (optional original name, or basename from file_path);
                response_format (markdown/json).

        Returns:
            Markdown file metadata or JSON, with identical structured content:
            {"filename":str|null,"content_type":"application/pdf",
              "sha256":str,"base64":str}.
            sha256 hashes the raw PDF. MCP_CHUNK_MAX_RESULT_BYTES caps raw PDF bytes;
            base64 adds about one third, and JSON text repeats the encoded payload.

        Examples:
            Use when: the user explicitly wants to preview/download a rendered .docx.
            Use when: obtaining a PDF rendering of a spreadsheet or presentation.
            Don't use when: extracting text/layout; use ocr_parse_document directly.
            Don't use when: previewing a stored PDF; use ocr_download_storage_file.

        Error Handling:
            Source/path/base64 and input-size refusals occur before HTTP. The API
            rejects PDFs/images as not_office_document, disabled conversion as 400,
            conversion failure/timeout as an explicit error. Oversized raw PDFs name
            POST /office/pdf for REST recovery. No POST retry occurs. Execution
            failures return is_error=true with structured {}, never partial bytes.
        """
        try:
            body = await asyncio.to_thread(prepare_source, params, ctx.settings)
            filename = params.filename or body.get("filename")
            # prepare_source returns exactly one *_base64 key for the detected type;
            # non-Office bytes are forwarded so the API answers not_office_document.
            encoded = next(v for k, v in body.items() if k.endswith("_base64"))
            data = base64.b64decode(encoded, validate=True)
            if len(data) > ctx.settings.max_file_bytes:
                raise ToolError("Input exceeds MCP_MAX_FILE_BYTES.")
            response = await ctx.client.binary_request(
                "POST",
                "/office/pdf",
                files={"file": (filename or "document", data)},
                data={"filename": filename or "document"},
                timeout=ctx.settings.http_timeout,
                recovery="POST /office/pdf",
            )
            pdf = response.content
            payload = {
                "filename": filename,
                "content_type": "application/pdf",
                "sha256": hashlib.sha256(pdf).hexdigest(),
                "base64": base64.b64encode(pdf).decode(),
            }
            return render(
                payload,
                params.response_format,
                title="Office to PDF preview",
                rows=_rows,
            )
        except Exception as exc:
            return error_result(exc)
