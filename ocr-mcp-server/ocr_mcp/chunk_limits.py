"""Byte accounting for the new RAG tool only."""

import json

from mcp_types import CallToolResult, TextContent

from ocr_mcp import __version__
from ocr_mcp.errors import ChunkTransferLimit, ToolError
from ocr_mcp.formatting import QUALITY_NOTE, ResponseFormat, pages_text


def compact_bytes(value) -> bytes:
    """Serialize compact UTF-8 JSON, rejecting nonfinite or invalid values."""
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ToolError(
            "RAG arguments and results must be valid finite Unicode JSON."
        ) from exc


def check_arguments(params, settings, route, *, prepared=None):
    """Reject oversized RAG arguments after substituting prepared source bytes.

    Measure the actual params envelope without altering the input model. Local
    refusals include the REST route and make no upstream request.
    """
    values = params.model_dump(mode="json", exclude_unset=True)
    if prepared is not None:
        for name in ("file_path", "image_base64", "pdf_base64", "document_base64"):
            values.pop(name, None)
        values.update(prepared)
    actual = len(compact_bytes({"params": values}))
    if actual > settings.chunk_max_argument_bytes:
        raise ChunkTransferLimit(
            "mcp_input_limit",
            actual_bytes=actual,
            limit_bytes=settings.chunk_max_argument_bytes,
            recovery_route=route,
        )


def rag_result(payload, presentation, settings, route, flagged=None):
    """Build a bounded summary alongside the complete structured RAG payload.

    Measure the serialized MCP result including server metadata. Raise a local
    transfer refusal rather than returning partial structured content.
    ``flagged`` holds the pages the chunked parse warned about (see
    formatting.flagged_pages): they are named in the summary only, because the
    chunks, what a vector store receives, carry no mark.
    """
    summary = {
        "chunks": len(payload["chunks"]),
        "tokens": sum(chunk["token_count"] for chunk in payload["chunks"]),
        "overflow_chunks": sum(chunk["overflow"] for chunk in payload["chunks"]),
        "diagnostics": sum(len(chunk["diagnostics"]) for chunk in payload["chunks"]),
        "data": "Complete RAG JSON is in structured content.",
    }
    named = {
        name: pages_text(pages) for name, pages in (flagged or {}).items() if pages
    }
    if named:
        summary.update(named, quality=QUALITY_NOTE)
    text = (
        compact_bytes(summary).decode("utf-8")
        if presentation is ResponseFormat.JSON
        else "# RAG chunks\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in summary.items())
    )
    if len(text.encode("utf-8")) > 4096:
        raise ToolError("RAG summary exceeds its 4096-byte bound.")
    # MCP 2.0 stamps this into modern protocol results. Supplying it ourselves
    # makes the measured envelope identical before and after SDK serialization.
    result = CallToolResult(
        meta={
            "io.modelcontextprotocol/serverInfo": {
                "name": "ocr_mcp",
                "version": __version__,
            }
        },
        content=[TextContent(text=text)],
        structured_content=payload,
        is_error=False,
    )
    actual = len(
        compact_bytes(result.model_dump(mode="json", by_alias=True, exclude_none=True))
    )
    if actual > settings.chunk_max_result_bytes:
        raise ChunkTransferLimit(
            "mcp_output_limit",
            actual_bytes=actual,
            limit_bytes=settings.chunk_max_result_bytes,
            recovery_route=route,
        )
    return result
