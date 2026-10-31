"""Recognize protected raw text boundaries, matching the browser preflight.

Types alone do not establish continuity: two adjacent typed tables/code blocks
can be independent. Complete raw blocks are always indivisible. The scanner
protects HTML tables, pipe tables and fenced code spanning multiple raw blocks.
A block's type only gates which structures its text may take part in.
"""

import json
import re

MAX_SAFE_INTEGER = 9007199254740991
_RULE_CELL = re.compile(r":?-+:?")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_TAG = re.compile(r"<\/?table(?=[\t\f />])[^<>]*>", re.I)
# OCR block labels able to hold each structure; see _allows().
_TABLE_TYPES = frozenset({"table"})
_CODE_TYPES = frozenset({"algorithm", "code"})


def integer(value, minimum=0):
    """Accept browser-safe integral numbers above a floor, excluding booleans."""
    # The browser's JSON.parse reads 1.0 as 1; judge numbers as the preflight does.
    return (
        type(value) is int or (type(value) is float and value.is_integer())
    ) and minimum <= value <= MAX_SAFE_INTEGER


def canonical(value):
    """Equality key matching the browser: True != 1, 1.0 == 1, key order ignored."""

    def plain(item):
        if type(item) is float and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: plain(inner) for key, inner in item.items()}
        if isinstance(item, list):
            return [plain(inner) for inner in item]
        return item

    return json.dumps(plain(value), sort_keys=True)


def document_of(chunk, metadata):
    """Resolve a chunk's document label, falling back only for missing/null values."""
    value = chunk.get("document")
    return metadata.get("document") if value is None else value


def text_separator(previous, following, continued):
    """Any structural continuation, protected or not, keeps its text compact."""
    if not continued:
        return "\n\n"
    return (
        ""
        if previous.endswith(("\r", "\n")) or following.startswith(("\r", "\n"))
        else "\n"
    )


def failed_pages(metadata):
    """The export skips failed pages; anything but a list of page numbers is ignored."""
    pages = metadata.get("failed_pages")
    return (
        [page for page in pages if integer(page, 1)] if isinstance(pages, list) else []
    )


def _allows(chunk, labels):
    """Check whether a block's type permits joining the requested structure.

    A string or list type needs at least one allowed label. Untyped blocks are
    scanned by text alone.
    """
    kind = chunk.get("type")
    if isinstance(kind, str):
        return kind in labels
    return not isinstance(kind, list) or any(
        isinstance(item, str) and item in labels for item in kind
    )


def _table_cells(text):
    cells, start, index = [], 0, 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "|":
            cells.append(text[start:index].strip(" \t"))
            start = index + 1
        index += 1
    if not cells:
        return None
    cells.append(text[start:].strip(" \t"))
    if cells[0] == "":
        cells.pop(0)
    if cells and cells[-1] == "":
        cells.pop()
    return cells or None


def _is_rule(cells):
    return cells is not None and all(_RULE_CELL.fullmatch(cell) for cell in cells)


def _header(cells, following):
    if cells is None or _is_rule(cells) or following is None:
        return False
    rule = _table_cells(following)
    return _is_rule(rule) and len(rule) == len(cells)


def structure_boundaries(chunks, metadata, options):
    """Return structural continuations, protected joins and hard boundaries.

    Continuations are found whatever the options; joins are the continuations
    whose protection is enabled and that cross no hard boundary. Page boundaries
    win over protection, but scanning state continues on the next page to protect
    its continuation. An unclosed structure conservatively extends to the
    document end or to a typed block of another kind. Shared fixtures check
    browser/server parity.
    """
    continuations, joins, hard, gaps, crossed = set(), set(), set(), set(), set()
    failed = failed_pages(metadata)
    tables, code = options.keep_tables_together, options.keep_code_blocks_together
    fence, depth, table, last = None, 0, False, None
    incomplete, hidden = 0, 0

    def link(index, enabled):
        continuations.add(index)
        if enabled:
            (crossed if index in hard else joins).add(index)

    def close(end):
        # An unclosed structure is incomplete when protected. At its document
        # end, unprotected, its literal content hid structures from the other.
        nonlocal fence, depth, table, last, incomplete, hidden
        if fence is not None:
            if code:
                incomplete += 1
            elif end and tables:
                hidden += 1
        if depth > 0:
            if tables:
                incomplete += 1
            elif end and code:
                hidden += 1
        fence, depth, table, last = None, 0, False, None

    for index, chunk in enumerate(chunks):
        previous = chunks[index - 1] if index else None
        table_ok, code_ok = _allows(chunk, _TABLE_TYPES), _allows(chunk, _CODE_TYPES)
        if previous is not None:
            new_document = canonical(document_of(previous, metadata)) != canonical(
                document_of(chunk, metadata)
            )
            # A failed page strictly between two known pages is never bridged.
            if integer(previous.get("page"), 1) and integer(chunk.get("page"), 1):
                low, high = sorted((previous["page"], chunk["page"]))
                if high - low > 1 and any(low < page < high for page in failed):
                    gaps.add(index - 1)
            if (
                new_document
                or index - 1 in gaps
                or (
                    options.page_boundary
                    and (
                        not integer(previous.get("page"), 1)
                        or not integer(chunk.get("page"), 1)
                        or previous.get("page") != chunk.get("page")
                    )
                )
            ):
                hard.add(index - 1)
            # A typed block of another kind cannot continue an open structure.
            if new_document:
                close(True)
            elif (fence is not None and not code_ok) or (depth > 0 and not table_ok):
                close(False)
        if fence is not None:
            link(index - 1, code)
        elif depth > 0:
            link(index - 1, tables)
        lines = chunk["text"].replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(lines) > 1 and not lines[-1]:
            lines.pop()
        for line_index, line in enumerate(lines):
            marker = code_ok and _FENCE.fullmatch(line)
            if fence is not None:
                if (
                    marker
                    and marker[1][0] == fence[0]
                    and len(marker[1]) >= len(fence)
                    and not marker[2].strip(" \t")
                ):
                    fence = None
                last, table = None, False
                continue
            if depth == 0 and marker and (marker[1][0] != "`" or "`" not in marker[2]):
                fence, last, table = marker[1], None, False
                continue
            html = depth > 0
            for tag in _TAG.finditer(line) if table_ok else ():
                html = True
                if tag[0].startswith("</"):
                    depth = max(0, depth - 1)
                elif not re.search(r"/[ \t]*>$", tag[0]):
                    depth += 1
            if html or not table_ok:
                table, last = False, None
                continue
            cells = _table_cells(line)
            # A header and rule opening the chunk start a new table, not a continuation.
            if line_index == 0 and _header(cells, lines[1] if len(lines) > 1 else None):
                table = False
            if table and cells:
                if line_index == 0 and last and last[1] == index - 1:
                    link(index - 1, tables)
            else:
                table = False
            if (
                _is_rule(cells)
                and last
                and last[0]
                and not _is_rule(last[0])
                and len(cells) == len(last[0])
            ):
                table = True
                if last[1] == index - 1:
                    link(index - 1, tables)
            last = (cells, index)
    close(True)
    return {
        "continuations": continuations,
        "joins": joins,
        "hard": hard,
        "gaps": gaps,
        "incomplete": incomplete,
        "hidden": hidden,
        "conflicts": len(crossed),
    }
