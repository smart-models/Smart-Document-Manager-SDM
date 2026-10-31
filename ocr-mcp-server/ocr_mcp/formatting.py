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
