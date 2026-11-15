"""Output rendering. Every tool that returns text goes through here.

Markdown is the default because it costs fewer context tokens than JSON for
the same information; JSON exists for callers that post-process the result.
Clients that read structured content (Claude Code) show that instead of this
text, so structured payloads are kept lean and images travel as image blocks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import Enum
from typing import Annotated, Any

from mcp_types import CallToolResult, ImageContent, TextContent

from ocr_mcp.errors import ToolError

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]
"""Tool result with a generic object output schema and dual text/JSON content."""


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


QUALITY_NOTE = (
    "Chunks carry no mark for these pages: check or exclude them before "
    "loading chunks into a vector store; their text may be incomplete."
)


def flagged_pages(warnings: object) -> dict[str, list[int]]:
    """Pages (1-based) that a parse's quality warnings name.

    Mirrors the two page lists the API writes into the json_rag metadata; the
    services share no code, so the rule is repeated here. A page stays listed
    whatever became of its suspect block. Malformed entries are ignored: the
    parse may come from a client.
    """
    found: dict[str, set[int]] = {
        "low_layout_confidence_pages": set(),
        "suspect_table_pages": set(),
    }
    for warning in warnings if isinstance(warnings, list) else []:
        if not isinstance(warning, dict):
            continue
        target = warning.get("target")
        index = target.get("index") if isinstance(target, dict) else None
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if warning.get("code") == "layout_low_confidence":
            found["low_layout_confidence_pages"].add(index + 1)
        elif warning.get("code") == "table_suspect":
            found["suspect_table_pages"].add(index + 1)
    return {name: sorted(pages) for name, pages in found.items()}


def pages_text(pages: list, limit: int = 20) -> str:
    """Render a page list as bounded text, such as ``3 pages: 2, 7, 9``."""
    shown = ", ".join(str(page) for page in pages[:limit])
    if len(pages) > limit:
        return f"{len(pages)} pages, the first {limit}: {shown}"
    return f"{len(pages)} page{'' if len(pages) == 1 else 's'}: {shown}"


def bullet(label: str, value: object) -> str:
    """Render one markdown fact line, showing an em dash for missing values."""
    shown = "—" if value is None or value == "" else value
    return f"- **{label}**: {shown}"


def render(
    payload: dict[str, Any],
    fmt: ResponseFormat,
    *,
    title: str,
    rows: Callable[[dict[str, Any]], list[str]],
    extra_content: list[ImageContent] | None = None,
) -> CallToolResult:
    """Return text for the model plus the same payload as structured content.

    ``extra_content`` blocks follow the text: clients forward image blocks to
    the model even when they show structured content instead of text.
    """
    try:
        serialized = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if json.loads(serialized) != payload:
            raise ValueError("non-JSON payload values")
    except (TypeError, ValueError) as exc:
        raise ToolError("Unexpected OCR payload: content is not valid JSON.") from exc
    if fmt is ResponseFormat.JSON:
        text = serialized
    else:
        lines = [f"# {title}", *rows(payload)]
        text = "\n".join(lines).rstrip() + "\n" if len(lines) > 1 else f"# {title}"
    return CallToolResult(
        content=[TextContent(text=text), *(extra_content or [])],
        structured_content=payload,
        is_error=False,
    )
