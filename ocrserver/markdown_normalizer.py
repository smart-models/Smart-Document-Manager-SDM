"""Normalize residual HTML in PaddleOCR-VL Markdown output."""

from __future__ import annotations

import html
import re

from ocrserver.schemas import Block
from ocrserver.table_md import tables_to_markdown


_PRE_CODE_RE = re.compile(
    r"<pre\b[^>]*>\s*<code\b(?P<attrs>[^>]*)>(?P<body>.*?)</code>\s*</pre>",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_CODE_RE = re.compile(
    r"<code\b[^>]*>(?P<body>.*?)</code>", re.IGNORECASE | re.DOTALL
)


def _delimited_formula(content: str, display: bool = False) -> str:
    content = html.unescape(content).strip()
    if (content.startswith("$$") and content.endswith("$$")) or (
        content.startswith("$") and content.endswith("$")
    ):
        return content
    delimiter = "$$" if display else "$"
    return f"{delimiter}{content}{delimiter}"


def _replace_formula_once(markdown: str, content: str, wrapped: str) -> str:
    """Replace the first standalone, unwrapped occurrence of *content*."""
    escaped = re.escape(content)
    pattern = re.compile(rf"(?<![\w$]){escaped}(?![\w$])")
    pos = 0
    while True:
        match = pattern.search(markdown, pos)
        if not match:
            return markdown
        start, end = match.span()
        before = markdown[start - 1 : start] if start > 0 else ""
        after = markdown[end : end + 1] if end < len(markdown) else ""
        if before == "$" and after == "$":
            pos = end
            continue
        return markdown[:start] + wrapped + markdown[end:]


def normalize_code_blocks(markdown: str) -> str:
    """Convert HTML code elements to Markdown fences and inline code."""

    def replace_block(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        language_match = re.search(
            r"\bclass\s*=\s*(['\"])(?:language-)?([^'\"\s]+)\1",
            attrs,
            re.IGNORECASE,
        )
        language = language_match.group(2) if language_match else ""
        content = html.unescape(match.group("body")).strip("\n")
        fence = "````" if "`" in content else "```"
        return f"{fence}{language}\n{content}\n{fence}"

    markdown = _PRE_CODE_RE.sub(replace_block, markdown)

    def replace_inline(match: re.Match[str]) -> str:
        content = html.unescape(match.group("body"))
        delimiter = "``" if "`" in content else "`"
        return f"{delimiter}{content}{delimiter}"

    return _INLINE_CODE_RE.sub(replace_inline, markdown)


def normalize_formulas(markdown: str, blocks: list[Block] | None = None) -> str:
    """Wrap explicit formula markup and recognized formula block content."""
    markdown = re.sub(
        r"<math\b[^>]*>(.*?)</math>",
        lambda match: _delimited_formula(match.group(1)),
        markdown,
        flags=re.IGNORECASE | re.DOTALL,
    )
    markdown = re.sub(
        r"<span\b[^>]*\bclass\s*=\s*(['\"])[^'\"]*\bmath\b[^'\"]*\1[^>]*>"
        r"(.*?)</span>",
        lambda match: _delimited_formula(match.group(2)),
        markdown,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in blocks or []:
        block_type = getattr(block, "type", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
        if str(block_type or "").lower() not in {
            "formula",
            "display_formula",
            "inline_formula",
        }:
            continue
        content = getattr(block, "content", None)
        if isinstance(block, dict):
            content = block.get("content", content)
        content = str(content or "").strip()
        if not content or content not in markdown:
            continue
        display = str(block_type).lower() != "inline_formula"
        wrapped = _delimited_formula(content, display)
        if wrapped == content:
            continue
        markdown = _replace_formula_once(markdown, content, wrapped)
    return markdown


def normalize_centered_text(markdown: str) -> str:
    """Remove recognized centering wrappers while preserving their content."""
    markdown = re.sub(
        r"</?(?:p|div)\b[^>]*\balign\s*=\s*(['\"])center\1[^>]*>",
        "",
        markdown,
        flags=re.IGNORECASE,
    )
    return re.sub(r"</?center\b[^>]*>", "", markdown, flags=re.IGNORECASE)


def normalize_generic_html(markdown: str) -> str:
    """Convert common inline HTML to Markdown, preserving image/table tags."""
    markdown = re.sub(r"<br\s*/?>", "\n", markdown, flags=re.IGNORECASE)
    for tags, marker in (("b|strong", "**"), ("i|em", "*")):
        markdown = re.sub(
            rf"</?(?:{tags})\b[^>]*>", marker, markdown, flags=re.IGNORECASE
        )

    preserved_tags = {
        "img",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "colgroup",
        "col",
    }

    def remove_tag(match: re.Match[str]) -> str:
        tag_match = re.match(r"</?([a-zA-Z][\w:-]*)", match.group(0))
        if tag_match and tag_match.group(1).lower() in preserved_tags:
            return match.group(0)
        return ""

    markdown = re.sub(
        r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s[^<>]*)?/?>",
        remove_tag,
        markdown,
    )
    return markdown


class MarkdownNormalizer:
    """Orchestrate Markdown normalization without mutating structured blocks."""

    @staticmethod
    def normalize(
        markdown: str,
        table_format: str = "html",
        blocks: list[Block] | None = None,
    ) -> tuple[str, int]:
        """Normalize tables, code, formulas and HTML without mutating OCR blocks.

        Return normalized Markdown and the number of merged table cells expanded
        when Markdown table output was requested.
        """
        spans_expanded = 0
        if table_format == "markdown":
            markdown, spans_expanded = tables_to_markdown(markdown)
        markdown = normalize_code_blocks(markdown)
        markdown = normalize_formulas(markdown, blocks)
        markdown = normalize_centered_text(markdown)
        markdown = normalize_generic_html(markdown)
        return markdown, spans_expanded
