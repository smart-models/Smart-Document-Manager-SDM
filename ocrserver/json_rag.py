"""Qdrant bulk-ingest export: one chunk per markdown-visible OCR block.

Text hidden from the page markdown (running heads, page labels, notes,
sidebars, equation numbers) is copied as plain string fields onto the chunk it
refers to, so a single retrieved point carries its own context; a printed page
number equal to the physical index is omitted as redundant.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from .chunking.relations import _NOTE_PREFIX
from .table_md import tables_to_markdown

# PaddleX 3.6 markdown_ignore_labels default plus formula_number, which the
# formatter never prints because PaddleOCR-VL uses show_formula_number=False.
EXCLUDED = frozenset(
    {
        "header",
        "header_image",
        "footer",
        "footer_image",
        "number",
        "footnote",
        "aside_text",
        "formula_number",
    }
)
_PAGE_FIELDS = {
    "header": "page_header",
    "header_image": "page_header",
    "footer": "page_footer",
    "footer_image": "page_footer",
    "number": "page_number",
}
_FIELDS = (
    "page_header",
    "page_footer",
    "page_number",
    "footnote",
    "aside_text",
    "formula_number",
)
_SYMBOLS = "*†‡§¶"
_ALIASES = {r"\dagger": "†", r"\ddagger": "‡", r"\ast": "*", "∗": "*"}
_LATEX_NAMES = {"*": [r"\\ast", "∗"], "†": [r"\\dagger"], "‡": [r"\\ddagger"]}
_FROM_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_TO_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
# The pipeline writes note markers as LaTeX: " $ ^{*} $Equal…", " $ ^4 $…".
_LATEX_NOTE = re.compile(r"^\s*\$\s*\^\s*\{?\s*(\\[A-Za-z]+|[^\s{}$\\]+?)\s*\}?\s*\$")
# A leading symbol is a marker even when glued to the text ("∗Equal…");
# _NOTE_PREFIX lacks "∗" and requires whitespace after the marker.
_SYMBOL_NOTE = re.compile(r"^\s*([*∗†‡§¶])")


@dataclass
class _Chunk:
    rank: int
    index: int
    kind: str
    text: str
    tokens: object
    span: tuple[float, float] | None
    fields: dict[str, list[str]] = field(default_factory=dict)


def _span(block: dict) -> tuple[float, float] | None:
    """Vertical extent of a valid [x1, y1, x2, y2] bbox, else None."""
    box = block.get("bbox")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in box
    ):
        return None
    return (box[1], box[3]) if box[3] >= box[1] else None


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _gap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, max(a[0], b[0]) - min(a[1], b[1]))


def _nearest(
    chunks: list[_Chunk],
    index: int,
    span: tuple[float, float] | None,
    *,
    formula_first: bool = False,
) -> _Chunk:
    """Most vertically overlapping chunk, else the nearest in array order."""
    pools = [[c for c in chunks if "formula" in c.kind]] if formula_first else []
    pools = [pool for pool in (*pools, chunks) if pool]
    if span is not None:
        for pool in pools:
            placed = [c for c in pool if c.span is not None]
            if placed:
                return min(
                    placed,
                    key=lambda c: (-_overlap(span, c.span), _gap(span, c.span), c.rank),
                )
    pool = pools[0]
    before = [c for c in pool if c.index < index]
    if before:
        return max(before, key=lambda c: c.index)
    return min(pool, key=lambda c: c.index)


def _note_marker(text: str) -> str | None:
    """Leading note marker as *, †, ‡, §, ¶, digits, [n] or (n)."""
    match = _LATEX_NOTE.match(text) or _SYMBOL_NOTE.match(text)
    raw = match.group(1) if match else None
    if raw is None:
        match = _NOTE_PREFIX.match(text)
        raw = match.group("marker") if match else None
    if raw is None:
        return None
    marker = _ALIASES.get(raw, raw).translate(_FROM_SUPERSCRIPT)
    if marker in _SYMBOLS or marker.isdigit() or marker[0] in "[(":
        return marker
    return None


def _marker_pattern(marker: str) -> re.Pattern:
    """Where a chunk cites the marker; bare digits never count."""
    if marker[0] in "[(":
        return re.compile(re.escape(marker))
    name = "(?:" + "|".join([re.escape(marker), *_LATEX_NAMES.get(marker, [])]) + ")"
    # Only a standalone inline superscript ("$ ^{2} $") cites a note; an
    # exponent inside a formula ("mc^{2}") must not.
    parts = [rf"\$\s*\^\s*(?:\{{\s*{name}\s*\}}|{name}(?![0-9A-Za-z]))\s*\$"]
    if marker in _SYMBOLS:
        symbols = re.escape(marker + ("∗" if marker == "*" else ""))
        parts.append(rf"(?<=\w)[{symbols}](?!\w)")
    else:
        superscript = marker.translate(_TO_SUPERSCRIPT)
        parts.append(rf"(?<![⁰¹²³⁴⁵⁶⁷⁸⁹]){superscript}(?![⁰¹²³⁴⁵⁶⁷⁸⁹])")
    return re.compile("|".join(parts))


def _footnote_target(
    chunks: list[_Chunk], text: str, span: tuple[float, float] | None
) -> _Chunk:
    marker = _note_marker(text)
    if marker is not None:
        pattern = _marker_pattern(marker)
        for chunk in chunks:
            if pattern.search(chunk.text):
                return chunk
    if span is not None:
        above = [c for c in chunks if c.span is not None and c.span[1] <= span[0]]
        if above:
            return min(above, key=lambda c: (span[0] - c.span[1], c.rank))
    return chunks[-1]


def _page_chunks(
    blocks: list[dict],
    table_format: str,
    count: Callable[[str], int | None] | None = None,
) -> list[_Chunk]:
    visible = [
        (index, block)
        for index, block in enumerate(blocks)
        if block.get("type") not in EXCLUDED and (block.get("content") or "").strip()
    ]
    visible.sort(
        key=lambda item: (
            item[1].get("order") is None,
            item[1].get("order") or 0,
            item[0],
        )
    )
    chunks = []
    for rank, (index, block) in enumerate(visible):
        text = block["content"].strip()
        if block.get("type") == "table" and table_format == "markdown":
            text = tables_to_markdown(text)[0].strip()
        tokens = block.get("content_tokens")
        # content_tokens describe the raw content (HTML tables, surrounding
        # whitespace); a changed text is recounted, or unknown rather than wrong.
        if tokens is not None and text != block["content"]:
            tokens = count(text) if count else None
        chunks.append(
            _Chunk(rank, index, block.get("type") or "", text, tokens, _span(block))
        )
    return chunks


def _attach(chunks: list[_Chunk], blocks: list[dict]) -> int:
    """Copy excluded text onto chunks; return how many found no chunk."""
    page_fields: dict[str, list[str]] = {}
    unattached = 0
    for index, block in enumerate(blocks):
        kind = block.get("type")
        text = (block.get("content") or "").strip()
        if kind not in EXCLUDED or not text:
            continue
        if not chunks:
            unattached += 1
        elif kind in _PAGE_FIELDS:
            page_fields.setdefault(_PAGE_FIELDS[kind], []).append(text)
        else:
            span = _span(block)
            if kind == "footnote":
                target = _footnote_target(chunks, text, span)
            else:
                target = _nearest(
                    chunks, index, span, formula_first=kind == "formula_number"
                )
            target.fields.setdefault(kind, []).append(text)
    for chunk in chunks:
        chunk.fields.update(page_fields)
    return unattached


def build_json_rag(
    pages: list[dict],
    *,
    document: str,
    table_format: str = "html",
    info: dict | None = None,
    count: Callable[[str], int | None] | None = None,
) -> dict:
    """Build the ``{chunks, metadata}`` ingest file from parse pages.

    ``pages`` items are ``{"page": n, "blocks": [...]}`` or
    ``{"page": n, "error": "..."}``; blocks are ``Block.model_dump()`` dicts.
    An error page marked ``"unprocessed": True`` was never reached: it is
    listed in ``unprocessed_pages`` and also stays in ``failed_pages``, which
    the refinement planners treat as hard boundaries.
    ``info`` is merged into the informational metadata the ingester ignores.
    ``count`` recounts a chunk whose text differs from its block content.
    """
    items: list[dict] = []
    failed: list[int] = []
    unprocessed: list[int] = []
    unattached = 0
    for page in sorted(pages, key=lambda item: item["page"]):
        if page.get("error"):
            failed.append(page["page"])
            if page.get("unprocessed"):
                unprocessed.append(page["page"])
            continue
        blocks = page.get("blocks") or []
        chunks = _page_chunks(blocks, table_format, count)
        unattached += _attach(chunks, blocks)
        for chunk in chunks:
            item = {"id": len(items) + 1, "text": chunk.text}
            if chunk.kind:
                item["type"] = chunk.kind
            if isinstance(chunk.tokens, int) and not isinstance(chunk.tokens, bool):
                item["token_count"] = chunk.tokens
            item["document"] = document
            item["page"] = page["page"]
            for name in _FIELDS:
                if name not in chunk.fields:
                    continue
                # Identical blocks (e.g. a header printed twice) add nothing.
                values = list(dict.fromkeys(chunk.fields[name]))
                if name == "page_number":
                    # The physical index already carries a folio that repeats it.
                    values = [
                        value for value in values if value.strip() != str(page["page"])
                    ]
                if values:
                    item[name] = "\n".join(values)
            items.append(item)
    counts = [item["token_count"] for item in items if "token_count" in item]
    return {
        "chunks": items,
        "metadata": {
            "document": document,
            **(info or {}),
            "pages": len(pages),
            "failed_pages": failed,
            "unprocessed_pages": unprocessed,
            "n_chunks": len(items),
            "total_tokens": sum(counts) if counts else None,
            "avg_tokens": round(sum(counts) / len(counts), 2) if counts else None,
            "max_tokens": max(counts, default=None),
            "min_tokens": min(counts, default=None),
            "unattached_blocks": unattached,
        },
    }


def document_name(raw: str | None) -> str:
    """Base name of the uploaded file for payloads; client paths never leak."""
    name = PureWindowsPath((raw or "").strip()).name
    return name[:255] or "document"
