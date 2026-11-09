"""Translate failures without echoing upstream internals or submitted data."""

import json
import re

import httpx
from mcp_types import CallToolResult, TextContent


class UpstreamError(Exception):
    """An unsuccessful OCR API response."""

    def __init__(self, *, status: int, detail: object, path: str) -> None:
        """Retain upstream diagnostics while keeping the exception text generic."""
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail
        self.path = path


class ToolError(Exception):
    """A safe, actionable local refusal message."""


class ChunkTransferLimit(ToolError):
    """Bounded local transport refusal with explicit REST recovery."""

    def __init__(self, code, *, actual_bytes, limit_bytes, recovery_route):
        """Record measured transfer limits and explicit, nonautomatic recovery."""
        super().__init__(code)
        self.detail = {
            "code": code,
            "actual_bytes": actual_bytes,
            "limit_bytes": limit_bytes,
            "recovery_route": recovery_route,
            "message": (
                "Use REST from the console or a client that can save the response. "
                "No result file or retrieval ID exists on this MCP server."
                + (
                    " Parsing may need to run again; it is not repeated automatically."
                    if code == "mcp_output_limit" and recovery_route == "/parse/chunks"
                    else " Existing parsed JSON can be reused without OCR."
                    if recovery_route == "/chunk"
                    else ""
                )
            ),
        }


CODE_GUIDANCE = {
    "corrupt_pdf": "Provide a readable, unencrypted PDF.",
    "corrupt_image": "Provide a valid image; use ocr_parse_document for PDFs.",
    "invalid_output": "Set output to markdown, json or both.",
    "invalid_task": "Choose ocr, table, formula, chart, spotting or seal.",
    "invalid_table_format": "Set table_format to html or markdown.",
    "max_pages_out_of_range": "Reduce max_pages to the configured OCR_MAX_PAGES.",
    "dpi_out_of_range": "Choose dpi between 50 and 400, or omit it.",
    "file_too_large": "Split or compress the input before sending it again.",
    "pdf_pixel_limit": (
        "PDF rasterization exceeds the server pixel budget; lower dpi or omit "
        "it, or send fewer pages."
    ),
    "image_too_large": (
        "The image exceeds the server pixel limit; downscale it before sending "
        "it again."
    ),
    "not_office_document": (
        "Provide an Office document (.docx, .xlsx, .pptx, .odt, .ods, "
        ".odp) or legacy .doc/.xls/.ppt."
    ),
    "office_disabled": (
        "The operator disabled Office input; export the file to PDF first."
    ),
    "office_converter_unavailable": (
        "The operator must install LibreOffice or fix OCR_SOFFICE_PATH."
    ),
    "office_conversion_failed": "Repair or re-save the file, or export it to PDF.",
    "office_conversion_timeout": "Repair or simplify the file, or export it to PDF.",
    "multiframe_image_not_supported": "Send multi-frame images to ocr_parse_document.",
    "image_memory_full": (
        "Downscale the image or wait for capacity before a deliberate retry."
    ),
    "storage_not_found": (
        "The archive or file is gone, or the archive is incomplete (listed with "
        "incomplete=true); refresh with ocr_list_storage_archives."
    ),
    "storage_confirmation_required": (
        "Set confirm to the archive job_id or the file's exact name."
    ),
    "storage_invalid_confirmation": (
        "Send confirm as a short string: the job_id or the file name."
    ),
    "storage_invalid_prefix": (
        "Use an archive prefix exactly as returned by ocr_list_storage_archives."
    ),
    "storage_invalid_key": (
        "Use a file key from ocr_get_storage_archive; manifest.json "
        "cannot be deleted alone."
    ),
    "storage_invalid_kind": "Set kind to parse, ocr or parse-chunks, or omit it.",
    "storage_invalid_date": (
        "Use YYYY-MM-DD; date excludes date_from/date_to and date_from <= date_to."
    ),
    "storage_invalid_cursor": (
        "Reuse next_cursor unchanged with the same filters, or restart without cursor."
    ),
    "storage_invalid_limit": "Set limit between 1 and 50.",
    "storage_invalid_preview": "Set preview to true or false.",
    "storage_preview_unsupported": (
        "Preview supports text, JSON, Markdown and PNG; download with preview=false."
    ),
    "storage_file_too_large": (
        "The file exceeds the preview or MCP size limit; retry with preview=false, "
        "or download it via REST outside MCP."
    ),
    "storage_archive_too_large": (
        "The archive exceeds the ZIP limits; download individual files instead."
    ),
    "storage_listing_too_large": "Narrow the listing with kind or a date filter.",
    "storage_archive_incomplete": (
        "The archive has no complete manifest; inspect or delete it, no ZIP exists."
    ),
    "storage_archive_in_progress": (
        "A writer may still be running; wait before deleting this archive."
    ),
    "storage_archive_busy": (
        "Another change is running on this archive; wait and retry deliberately."
    ),
    "storage_write_disabled": (
        "Archiving is off on this server: single-file deletion and RAG "
        "refinement need it; whole-archive deletion and reads still work."
    ),
    "storage_delete_partial": (
        "Some objects were removed (see deleted_objects); repeating the"
        " same delete is safe."
    ),
    "storage_browser_disabled": "The operator disabled the storage browser.",
    "storage_browser_busy": "Storage workers are busy; wait before a deliberate retry.",
    "storage_unavailable": (
        "Object storage is unreachable; ask the operator to check it."
    ),
    "storage_invalid_listing": (
        "Object storage returned an invalid listing; ask the operator to check it."
    ),
    "storage_invalid_manifest": (
        "The archive manifest is unreadable; inspect files individually."
    ),
    "storage_confirmation_timeout": (
        "The confirmation body arrived too slowly; send the delete again."
    ),
    "rag_invalid_source": (
        "source_key must name a stored json_rag.json; a refined "
        "json_rag_<job>_<n>.json cannot be refined again."
    ),
    "rag_invalid_plan": (
        "Correct groups, overrides, counts, execution_id (UUID) or "
        "created_at (with timezone)."
    ),
    "rag_plan_too_large": "The plan exceeds 2 MiB; refine fewer groups per call.",
    "rag_source_changed": (
        "The stored file changed; reread it and send its current source_sha256."
    ),
    "rag_tokenizer_unavailable": (
        "Ask the operator to check the local PaddleOCR tokenizer cache."
    ),
    "rag_request_timeout": "The plan body arrived too slowly; send it again.",
    "rag_execution_conflict": (
        "This execution_id already saved a different plan; use a fresh UUID."
    ),
    "storage_too_many_objects": (
        "The archive reached its object limit; delete unneeded artifacts first."
    ),
    "queue_full": "Call ocr_get_stats and wait for capacity before retrying.",
    "slots_full": "Call ocr_get_stats and reduce recognition concurrency.",
    "backend_unavailable": "Call ocr_check_health and wait for a healthy backend.",
    "ocr_disabled_archive_only": (
        "The OCR API runs in archive-only mode: parsing and recognition are "
        "off by design and waiting changes nothing. Use the storage tools; OCR "
        "comes back only when an operator starts the stack in OCR mode (no "
        "--archive-only / -ArchiveOnly flag, OCR_ARCHIVE_ONLY unset or false)."
    ),
    "shutting_down": "Wait for the OCR API to restart, then call ocr_check_health.",
    "request_timeout": (
        "Work may still be running. Check ocr_get_stats; reduce pages "
        "or DPI before a deliberate retry."
    ),
}

RAG_GUIDANCE = {
    "protected_block_exceeds_budget": (
        "Increase max_tokens, allow splitting that type, or explicitly "
        "disable strictly."
    ),
    "context_exceeds_budget": (
        "Increase the budget; mandatory titles and selected table "
        "headers are not removed."
    ),
    "invalid_chunk_options": (
        "Correct RAG options; overlap_tokens must be below max_tokens."
    ),
    "invalid_structure": (
        "Provide a complete structured /parse JSON with unique positive page numbers."
    ),
    "missing_structured_blocks": "Parse with structured output, then reuse that JSON.",
    "incomplete_ocr": (
        "Failed pages cannot produce a complete RAG file; inspect the original OCR."
    ),
    "tokenizer_mismatch": (
        "Use the same locally pinned PaddleOCR tokenizer as the source JSON."
    ),
    "tokenization_failed": (
        "Inspect the failing source; do not replace exact counts with estimates."
    ),
    "tokenizer_unavailable": (
        "Ask the operator to check the local PaddleOCR tokenizer cache;"
        " OCR remains independent."
    ),
    "unsplittable_content": (
        "Increase the budget; atomic formulas and mandatory context cannot be cut."
    ),
    "unsplittable_table": (
        "Provide unambiguous table markup or explicitly preserve the whole table."
    ),
    "no_indexable_content": (
        "Provide nonempty recognized text; image descriptions are not generated."
    ),
    "chunk_input_limit": (
        "Use a smaller structured input or ask the operator to review "
        "the configured limit."
    ),
    "chunk_work_limit": (
        "Reduce document scope or ask the operator to review CPU work limits."
    ),
    "chunk_output_limit": (
        "Reduce output size or ask the operator to review the output limit."
    ),
    "chunk_capacity_exceeded": (
        "Capacity is busy. Wait and retry deliberately; no POST is "
        "retried automatically."
    ),
    "chunk_timeout": (
        "Chunking was cancelled. Parsing may have run; choose any retry deliberately."
    ),
    "chunk_worker_failed": (
        "The chunking worker failed; retry deliberately once, then ask the "
        "operator to check the API logs."
    ),
    "chunk_validation_failed": (
        "The chunker rejected its own output; ask the operator to check the "
        "API logs and do not retry in a loop."
    ),
}


def _rag_error(exc):
    """Project recognized RAG failures into bounded, allowlisted recovery details.

    Retain only safe numeric limits, source addresses and known option/type
    labels. Never echo document text or arbitrary upstream error messages.
    Return None when the failure does not match the supported RAG contract.
    """
    if not isinstance(exc, UpstreamError) or exc.path not in {
        "/chunk",
        "/parse/chunks",
    }:
        return None
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code")
    if not isinstance(code, str) or code not in RAG_GUIDANCE:
        return None
    raw = detail.get("details")
    raw = raw if isinstance(raw, dict) else {}
    safe = {}
    for name in (
        "page",
        "max_tokens",
        "required_tokens",
        "suggested_max_tokens",
        "minimum_feasible_max_tokens",
    ):
        value = raw.get(name)
        if name in raw and (
            value is None or type(value) is int and 0 <= value <= 9007199254740991
        ):
            safe[name] = value
    if (
        isinstance(raw.get("source_key"), str)
        and len(raw["source_key"]) <= 40
        and re.fullmatch(r"p[1-9][0-9]*:b[0-9]+", raw["source_key"])
    ):
        safe["source_key"] = raw["source_key"]
    if isinstance(raw.get("type"), str) and raw["type"] in {
        "table",
        "algorithm",
        "code",
        "text",
        "paragraph_title",
        "doc_title",
    }:
        safe["type"] = raw["type"]
    if isinstance(raw.get("conflicting_option"), str) and raw["conflicting_option"] in {
        "max_tokens",
        "preserve_tables",
        "preserve_code_blocks",
        "table_header_policy",
        "strictly",
    }:
        safe["conflicting_option"] = raw["conflicting_option"]
    return {
        "code": code,
        "status": exc.status,
        "message": RAG_GUIDANCE[code],
        "details": safe,
    }


def describe_error(exc: Exception) -> str:
    """Give the next step without copying arbitrary API detail into context."""
    if isinstance(exc, ToolError):
        return f"Error: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return (
            "Error: OCR request timed out. Work may still be running; check"
            " ocr_get_stats before a deliberate retry."
        )
    if isinstance(exc, httpx.TransportError):
        return (
            "Error: OCR API unreachable. Check MCP_UPSTREAM_URL and call "
            "ocr_check_health after recovery."
        )
    if not isinstance(exc, UpstreamError):
        return (
            "Error: unexpected OCR response or local failure. Check server "
            "configuration and input; do not retry in a loop."
        )
    status = exc.status
    if status in (401, 403):
        return (
            "Error: OCR API rejected the upstream credential. An operator "
            "must correct UPSTREAM_CLIENT_TOKEN; retrying will not help."
        )
    code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
    if isinstance(code, str) and code in CODE_GUIDANCE:
        count = exc.detail.get("deleted_objects")
        suffix = (
            f" deleted_objects={count}." if type(count) is int and count >= 0 else ""
        )
        limit = exc.detail.get("max")
        if code == "max_pages_out_of_range" and type(limit) is int:
            suffix += f" OCR_MAX_PAGES={limit}."
        return f"Error: {code}. {CODE_GUIDANCE[code]}{suffix}"
    guidance = {
        400: "Check the file encoding and input type in docs://ocr/reference.",
        404: (
            "Check MCP_UPSTREAM_URL and the OCR API version; this route is unavailable."
        ),
        413: "Split or compress the input; repeating this body will fail again.",
        422: "Check fields and server limits in docs://ocr/reference.",
        429: "Wait before retrying; inference calls are never automatically retried.",
        503: "Call ocr_check_health and ocr_get_stats before retrying.",
        504: (
            "Work may still be running; check ocr_get_stats before a deliberate retry."
        ),
    }.get(status, "Check ocr_check_health; report persistent server failures.")
    return f"Error: HTTP {status}. {guidance}"


def error_result(exc: Exception) -> CallToolResult:
    """Errors satisfy the advertised object output schema too."""
    detail = exc.detail if isinstance(exc, ChunkTransferLimit) else _rag_error(exc)
    text = (
        json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        if detail is not None
        else describe_error(exc)
    )
    return CallToolResult(
        content=[TextContent(text=text)],
        structured_content={},
        is_error=True,
    )
