"""Optional RAG conversion; OCR tools keep their existing behavior."""

import asyncio
import json
from urllib.parse import urlencode

from mcp.server import MCPServer

from ocr_mcp.chunk_limits import check_arguments, rag_result
from ocr_mcp.errors import ToolError, error_result
from ocr_mcp.inputs import prepare_source
from ocr_mcp.rag_contract import CreateRagChunksInput, RagToolResult
from ocr_mcp.responses import validate_payload
from ocr_mcp.tools.health import require_healthy

STORED_PARSE_NAMES = ("json_ocr.json", "parsed.json")


async def _stored_parse(key, ctx):
    """Fetch an archived parse so it never travels through the client's output.

    A model that must retype a parse drops fields and changes characters; the
    server-side copy is read as stored, bounded by the MCP result limit.
    """
    if key.rsplit("/", 1)[-1] not in STORED_PARSE_NAMES:
        raise ToolError("source_key must name a stored json_ocr.json or parsed.json.")
    response = await ctx.client.binary_request(
        "GET",
        "/storage/file",
        params={"key": key, "preview": "false"},
        timeout=ctx.settings.storage_timeout,
        recovery="GET /storage/file?" + urlencode({"key": key, "preview": "false"}),
    )
    try:
        return json.loads(response.content)
    except ValueError as error:
        raise ToolError("source_key does not hold valid JSON.") from error


async def _body(params, ctx):
    """Validate exclusive RAG sources and select the matching upstream route.

    Parsed JSON, given or fetched from storage by key, uses /chunk without
    source preparation. Document inputs use /parse/chunks after bounded local
    preparation; mode-specific options cannot silently leak between the forms.
    """
    settings = ctx.settings
    parsed = params.parsed_document is not None
    stored = params.source_key is not None
    byte_sources = sum(
        value is not None
        for value in (
            params.file_path,
            params.image_base64,
            params.pdf_base64,
            params.document_base64,
        )
    )
    if int(parsed) + int(stored) + byte_sources != 1:
        raise ToolError(
            "Supply exactly one source_key, parsed_document, file_path, "
            "image_base64, pdf_base64 or document_base64."
        )
    if params.options.overlap_tokens >= params.options.max_tokens:
        raise ToolError("overlap_tokens must be less than max_tokens.")
    body = {"options": params.options.model_dump()}
    for name in ("source_id", "lang"):
        value = getattr(params, name)
        if value is not None:
            body[name] = value
    if parsed or stored:
        if params.max_pages is not None or params.dpi is not None:
            raise ToolError(
                "max_pages and dpi apply only to document input, not parsed_document."
            )
        document = (
            params.parsed_document
            if parsed
            else await _stored_parse(params.source_key, ctx)
        )
        body.update(parsed_document=document, accept_truncated=params.accept_truncated)
        return "/chunk", body
    if params.accept_truncated:
        raise ToolError("accept_truncated applies only to parsed_document.")
    body.update(await asyncio.to_thread(prepare_source, params, settings))
    for name in ("max_pages", "dpi"):
        value = getattr(params, name)
        if value is not None:
            body[name] = value
    return "/parse/chunks", body


def register(mcp: MCPServer, ctx) -> None:
    """Register optional RAG creation with explicit source and transfer limits."""

    @mcp.tool(
        name="ocr_create_rag_chunks",
        annotations={
            "title": "Create optional RAG chunks",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def create_rag_chunks(params: CreateRagChunksInput) -> RagToolResult:
        """Create deterministic structural RAG chunks, only when requested.

        Document input may persist OCR artifacts when server storage is enabled;
        repeated calls create separate archives. Existing parsed_document input
        performs no archival. Storage headers are not exposed in this tool's
        result envelope.

        Args:
            params: exactly one source_key (stored json_ocr.json or parsed.json
                key from ocr_get_storage_archive: the MCP downloads it, so the
                parse never passes through the client), parsed_document
                (complete /parse JSON), file_path (allowed MCP server file),
                image_base64, pdf_base64 or document_base64 (PDF, image or
                Office bytes).
                filename: optional original name for base64 document storage;
                    file_path supplies its basename automatically.
                options: max_tokens required; strictly/preserve_tables/
                preserve_code_blocks=true, overlap_tokens=0,
                marginal_policy='metadata', table_header_policy='declared'.
                metadata_fields optionally selects exported metadata; id, text,
                and token_count are always emitted.
                source_id/lang: optional caller metadata, never inferred.
                accept_truncated: parsed JSON only, false by default.
                max_pages/dpi: document source only. response_format: markdown
                or json summary; complete data appears only in structured content.

        Returns:
            {"chunks":[{"id":int,"text":str,"token_count":int,"page":int,
              "pages":[int],"section":str|null,"section_id":str,
              "source_spans":[object],"context_spans":[object],...}]}
            Exact complete closed schema is advertised as outputSchema. Metadata
            stays on each chunk, with no metadata wrapper. Token counts include
            repeated titles and headers, using only the PaddleOCR tokenizer.

        Use when: chunking an archived parse by its json_ocr.json key.
        Use when: converting/rechunking saved OCR JSON without another inference.
        Use when: explicitly parsing a PDF directly into a RAG file server-side.
        Don't use when: ordinary OCR was requested; use ocr_parse_document.
        Don't use when: recognizing a crop; use ocr_recognize_image.

        Error Handling:
            One source and valid options required. JSON mode skips model health;
            document mode performs health preflight. Strict protected overflow
            fails atomically; change budget/protection only with user intent.
            Input/output limits return explicit REST recovery, never partial
            chunks or a fictional server file. 503 capacity can be retried
            deliberately. No POST is automatically retried after errors/timeouts.
            Errors carry is_error=true and structured {}. See docs://ocr/reference.
        """
        try:
            path = (
                "/chunk"
                if params.parsed_document is not None or params.source_key is not None
                else "/parse/chunks"
            )
            check_arguments(params, ctx.settings, path)
            path, body = await _body(params, ctx)
            check_arguments(params, ctx.settings, path, prepared=body)
            if path == "/parse/chunks":
                await require_healthy(ctx)
            payload = await ctx.client.chunk_request(path, json=body)
            validate_payload(payload, path)
            return rag_result(payload, params.response_format, ctx.settings, path)
        except Exception as exc:
            return error_result(exc)
