"""Stored RAG refinement and split tools."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from mcp.server import MCPServer

from ocr_mcp.context import ToolContext
from ocr_mcp.errors import error_result
from ocr_mcp.formatting import StructuredToolResult, bullet, render
from ocr_mcp.inputs_rag_storage import RefineRagStorageInput, SplitRagGroupInput


def _refine_rows(payload: dict) -> list[str]:
    return [
        bullet("Prefix", payload.get("prefix")),
        bullet("File", payload.get("file", {}).get("name")),
        bullet("Chunks", payload.get("chunk_count")),
    ]


def _split_rows(payload: dict) -> list[str]:
    fragments = payload.get("fragments", [])
    rows = [bullet("Fragments", len(fragments))]
    for index, fragment in enumerate(fragments):
        rows.append(
            f"- Fragment {index}: {fragment.get('token_count')} tokens, "
            f"status {fragment.get('status')}"
        )
    return rows


def register(mcp: MCPServer, ctx: ToolContext) -> None:
    """Register stored-RAG refinement writes and read-only split previews."""

    @mcp.tool(
        name="ocr_refine_rag_storage",
        annotations={
            "title": "Save a RAG refinement plan",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def refine_rag_storage(params: RefineRagStorageInput) -> StructuredToolResult:
        """Save a structural refinement plan as a new artifact in an existing archive.

        No OCR runs. Read the source chunks first; its SHA-256 is files[].sha256
        in ocr_get_storage_archive. Preview risky groups with ocr_split_rag_group.
        The original source is not overwritten.

        Args:
            params (RefineRagStorageInput): source_key (str for saved json_rag.json);
                source_sha256 (64 lowercase hex digits of the current source);
                execution_id (UUID str, omitted for a fresh one; reuse only for
                the identical plan); created_at (ISO 8601 str with timezone,
                omitted for the current UTC time); options (min_tokens,
                max_tokens, page_boundary, keep_tables_together,
                keep_code_blocks_together); groups (ordered lists of integer chunk
                indexes or [index,start,end] half-open codepoint slices, optionally
                [index,start,end,"repeat"] for repeated framing);
                overrides (diagnostic-code lists, one per group, or null);
                counts (per-group per-source token contributions or null);
                response_format (markdown/json).

        Returns:
            Markdown artifact summary or JSON, with identical structured content:
            {"prefix":str,"file":{"key":str,"name":str,"size":int,
              "content_type":str},"chunk_count":int,"execution_id":str,
              "created_at":str}.
            file identifies the newly saved refinement; chunk_count is its chunk
            count; execution_id and created_at are the values sent, to reuse for a
            deliberate retry of the identical plan.

        Examples:
            Use when: the user asks to save a reviewed grouping of existing OCR chunks.
            Use when: persisting a plan after a successful split preview.
            Don't use when: only previewing a split; use ocr_split_rag_group.
            Don't use when: creating initial chunks; use ocr_create_rag_chunks only
                for an explicit chunking request.

        Error Handling:
            Malformed shapes are schema errors. API 409 reports stale source hashes
            or execution-ID conflicts; 422 reports invalid plans; 503 can indicate
            read-only storage. POST is never retried automatically. Invalid responses
            and execution failures return is_error=true with structured {}.
        """
        try:
            body = params.model_dump(exclude={"response_format"}, exclude_none=True)
            body.setdefault("execution_id", str(uuid.uuid4()))
            body.setdefault(
                "created_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            payload = await ctx.client.request("POST", "/storage/rag/refine", json=body)
            return render(
                {
                    **payload,
                    "execution_id": body["execution_id"],
                    "created_at": body["created_at"],
                },
                params.response_format,
                title="RAG refinement saved",
                rows=_refine_rows,
            )
        except Exception as exc:
            return error_result(exc)

    @mcp.tool(
        name="ocr_split_rag_group",
        annotations={
            "title": "Preview a RAG group split",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def split_rag_group(params: SplitRagGroupInput) -> StructuredToolResult:
        """Preview a possible split of one stored RAG group without writing storage.

        Uses an existing json_rag source and its exact current SHA-256. No inference
        runs and no refinement artifact is saved.

        Args:
            params (SplitRagGroupInput): source_key (str from archive details);
                source_sha256 (64 lowercase hex digits from a full source download);
                options (min_tokens, max_tokens, page_boundary,
                keep_tables_together, keep_code_blocks_together);
                group (integer chunk indexes or half-open [index,start,end] slices,
                optionally [index,start,end,"repeat"] for framing);
                response_format (markdown/json).

        Returns:
            Markdown fragment summary or JSON, with identical structured content:
            {"fragments":[{"entries":[int|list],"token_count":int|null,
              "status":"within"|"above"|"below"|"unknown",
              "notes":[{"code":str,"severity":"info"|"warning",
                "targets":[{"source_key":str,"start":int|null,"end":int|null}],
                "parameters":[{"name":str,"value":str|float|bool|null}]}],
              "member_token_counts":[int|null]|null}],
              "warnings":[{"code":str,"severity":str,"message":str,
                "target":dict|null,"details":dict|null}]}.
            entries describe proposed fragments; status explains their token budget.
            Member contributions follow distinct source chunks in entry order.

        Examples:
            Use when: testing a risky group before saving a refinement plan.
            Use when: inspecting why a protected table cannot meet a token budget.
            Don't use when: saving the plan; use ocr_refine_rag_storage.
            Don't use when: reading the source; use ocr_download_storage_file.

        Error Handling:
            API 409 reports a stale source hash; 422 reports invalid group/budget;
            503 reports unavailable storage/tokenizer capacity. POST is not retried.
            Malformed responses and execution failures return is_error=true with
            structured {}; a successful preview never means anything was saved.
        """
        try:
            payload = await ctx.client.request(
                "POST",
                "/storage/rag/split",
                json=params.model_dump(exclude={"response_format"}, exclude_none=True),
            )
            return render(
                payload,
                params.response_format,
                title="RAG split preview",
                rows=_split_rows,
            )
        except Exception as exc:
            return error_result(exc)
