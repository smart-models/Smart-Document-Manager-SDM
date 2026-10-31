"""Deterministic table rendering with offsets into the original OCR content.

This module does not choose chunk boundaries or count tokens. Row ranges are
half-open logical row indices. ``render_cell`` slices decoded cell values by
Unicode code point; all returned source offsets still refer to the OCR block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape, unescape
from html.parser import HTMLParser
import re
from types import MappingProxyType
from typing import Mapping

from .errors import ChunkError


@dataclass(frozen=True)
class _TextRun:
    text: str
    start: int
    end: int
    html_text: str | None = None


@dataclass(frozen=True)
class TableCell:
    """One anchored cell in the source table's logical grid."""

    row: int
    column: int
    rowspan: int
    colspan: int
    header: bool
    start: int
    end: int
    runs: tuple[_TextRun, ...]

    @property
    def value(self) -> str:
        """Return decoded cell text in source order, excluding table markup."""
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True)
class TableDocument:
    """Retain a parsed grid, indivisible row groups and original source ranges."""

    source: str
    cells: tuple[TableCell, ...]
    row_count: int
    row_groups: tuple[tuple[int, int], ...]
    format: str
    logical_cells: int
    ignored_source_ranges: tuple[tuple[int, int], ...]
    markdown_alignment: tuple[str, ...] = ()
    _rows: tuple[tuple[TableCell, ...], ...] = field(init=False, repr=False)
    _headers: tuple[TableCell, ...] = field(init=False, repr=False)
    _cell_positions: Mapping[tuple[int, int], TableCell] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Index anchored cells by row and position without expanding spans."""
        rows: list[list[TableCell]] = [[] for _ in range(self.row_count)]
        for cell in self.cells:
            rows[cell.row].append(cell)
        object.__setattr__(self, "_rows", tuple(tuple(row) for row in rows))
        object.__setattr__(
            self, "_headers", tuple(cell for cell in self.cells if cell.header)
        )
        object.__setattr__(
            self,
            "_cell_positions",
            MappingProxyType({(cell.row, cell.column): cell for cell in self.cells}),
        )


@dataclass(frozen=True)
class MappedSpan:
    """Map a rendered character interval to source text and table coordinates."""

    start: int | None
    end: int | None
    text_start: int
    text_end: int
    role: str
    table_position: Mapping[str, object]


@dataclass(frozen=True)
class Rendered:
    """Bundle table text with provenance and intentionally omitted source ranges."""

    text: str
    spans: tuple[MappedSpan, ...]
    ignored_source_ranges: tuple[tuple[int, int], ...]


def _error(reason: str) -> None:
    raise ChunkError(
        "unsplittable_table",
        "Table cannot be parsed without ambiguity.",
        details={"reason": reason},
    )


class _TableParser(HTMLParser):
    def __init__(self, source: str, max_cells: int) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.max_cells = max_cells
        self.cells: list[TableCell] = []
        self.rows = 0
        self.logical_cells = 0
        self.closed = False
        self.seen = False
        self.in_row = False
        self.cell: dict | None = None
        self.row_section: str | None = None
        self.next_column = 0
        self.line_offsets = [0] + [
            index + 1 for index, char in enumerate(source) if char == "\n"
        ]
        self.occupied: dict[tuple[int, int], bool] = {}

    def _source_offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs) -> None:
        """Build a bounded HTML grid while rejecting ambiguous or unsupported tags.

        Preserve source positions and explicit spans, allowing supported implicit
        cell/section closure. Reject duplicate attributes, overlapping spans and
        grids exceeding the logical-cell budget before expanding occupancy.
        """
        if tag == "br":
            if self.cell is None:
                _error("break_outside_cell")
            start = self._source_offset()
            self.cell["runs"].append(
                _TextRun(
                    "\n",
                    start,
                    start + len(self.get_starttag_text()),
                    "<br>",
                )
            )
        elif tag == "table":
            if self.seen:
                _error("nested_or_multiple_tables")
            self.seen = True
        elif tag in ("thead", "tbody", "tfoot"):
            implicit_close = self.row_section in (
                "thead",
                "tbody",
            ) and tag in ("tbody", "tfoot")
            if (
                not self.seen
                or self.closed
                or self.row_section is not None
                and not implicit_close
            ):
                _error("invalid_row_section")
            self._close_cell()
            self.in_row = False
            self.row_section = tag
        elif tag == "tr":
            if not self.seen or self.closed:
                _error("row_outside_table")
            self._close_cell()
            self.rows += 1
            self.in_row = True
            self.next_column = 0
        elif tag in ("td", "th"):
            if not self.in_row:
                _error("cell_outside_row")
            self._close_cell()
            attributes = dict(attrs)
            if len(attributes) != len(attrs):
                _error("duplicate_attributes")
            try:
                rowspan = int(attributes.get("rowspan", "1"))
                colspan = int(attributes.get("colspan", "1"))
            except (ValueError, TypeError):
                _error("invalid_span")
            if rowspan < 1 or colspan < 1:
                _error("invalid_span")
            self.logical_cells += rowspan * colspan
            if self.logical_cells > self.max_cells:
                raise ChunkError(
                    "chunk_work_limit",
                    "Table grid exceeds the logical cell limit.",
                    status_code=413,
                    details={
                        "limit": "table_cells",
                        "maximum": self.max_cells,
                        "required": self.logical_cells,
                    },
                )
            row = self.rows - 1
            column = self.next_column
            while (row, column) in self.occupied:
                column += 1
            for cell_row in range(row, row + rowspan):
                for cell_column in range(column, column + colspan):
                    key = (cell_row, cell_column)
                    if key in self.occupied:
                        _error("overlapping_spans")
                    self.occupied[key] = True
            self.next_column = column + colspan
            self.cell = {
                "row": row,
                "column": column,
                "rowspan": rowspan,
                "colspan": colspan,
                "header": tag == "th" or self.row_section == "thead",
                "tag": tag,
                "start": self._source_offset() + len(self.get_starttag_text()),
                "runs": [],
            }
        else:
            _error("unsupported_markup")

    def _close_cell(self) -> None:
        if self.cell is not None:
            cell = self.cell
            self.cells.append(
                TableCell(
                    row=cell["row"],
                    column=cell["column"],
                    rowspan=cell["rowspan"],
                    colspan=cell["colspan"],
                    header=cell["header"],
                    start=cell["start"],
                    end=self._source_offset(),
                    runs=tuple(cell["runs"]),
                )
            )
            self.cell = None

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            if self.cell is None or self.cell["tag"] != tag:
                _error("mismatched_cell_close")
            self._close_cell()
        elif tag in ("thead", "tbody", "tfoot"):
            if self.row_section != tag:
                _error("mismatched_row_section")
            self._close_cell()
            self.in_row = False
            self.row_section = None
        elif tag in ("tr", "table"):
            self._close_cell()
            if tag == "tr" and not self.in_row:
                _error("mismatched_row_close")
            self.in_row = False
            if tag == "table":
                if not self.seen or self.closed:
                    _error("mismatched_table_close")
                self.closed = True
                self.row_section = None
        else:
            _error("unsupported_markup")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag != "br":
            _error("self_closing_table_structure")
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            start = self._source_offset()
            cursor = 0
            for match in re.finditer(r"\r\n?", data):
                if match.start() > cursor:
                    self.cell["runs"].append(
                        _TextRun(
                            data[cursor : match.start()],
                            start + cursor,
                            start + match.start(),
                        )
                    )
                self.cell["runs"].append(
                    _TextRun(
                        "\n",
                        start + match.start(),
                        start + match.end(),
                    )
                )
                cursor = match.end()
            if cursor < len(data):
                self.cell["runs"].append(
                    _TextRun(
                        data[cursor:],
                        start + cursor,
                        start + len(data),
                    )
                )
        elif data.strip():
            _error("text_outside_cells")

    def _entity(self, name: str) -> None:
        start = self._source_offset()
        raw = "&" + name
        if self.source[start + len(raw) : start + len(raw) + 1] == ";":
            raw += ";"
        if self.cell is None:
            if unescape(raw).strip():
                _error("text_outside_cells")
        else:
            self.cell["runs"].append(_TextRun(unescape(raw), start, start + len(raw)))

    def handle_entityref(self, name: str) -> None:
        self._entity(name)

    def handle_charref(self, name: str) -> None:
        self._entity("#" + name)

    def handle_pi(self, data: str) -> None:
        _error("unsupported_processing_instruction")

    def handle_decl(self, decl: str) -> None:
        _error("unsupported_declaration")

    def unknown_decl(self, data: str) -> None:
        _error("unsupported_declaration")


def _ignored_ranges(source: str, cells: tuple[TableCell, ...]) -> tuple:
    represented = sorted((run.start, run.end) for cell in cells for run in cell.runs)
    ignored = []
    position = 0
    for start, end in represented:
        if start > position:
            ignored.append((position, start))
        position = max(position, end)
    if position < len(source):
        ignored.append((position, len(source)))
    return tuple(ignored)


def parse_table(content: str, *, max_logical_cells: int) -> TableDocument:
    """Parse a single source table, preserving logical cells and source offsets."""
    if not content.lstrip().lower().startswith("<table"):
        return _parse_markdown(content, max_logical_cells)
    parser = _TableParser(content, max_logical_cells)
    try:
        parser.feed(content)
        parser.close()
    except AssertionError as exc:
        message = str(exc)
        malformed = (
            message.startswith("unknown status keyword ")
            and message.endswith(" in marked section")
        ) or message.startswith("expected name token at ")
        if not malformed:
            raise
        _error("malformed_marked_section")
    if (
        not parser.closed
        or not parser.rows
        or not parser.cells
        or parser.row_section is not None
    ):
        _error("incomplete_table")
    cells = tuple(parser.cells)
    if any(cell.row + cell.rowspan > parser.rows for cell in cells):
        _error("rowspan_outside_table")
    groups = []
    group_start = 0
    group_end = 0
    row_ends: dict[int, int] = {}
    for cell in cells:
        row_ends[cell.row] = max(row_ends.get(cell.row, 0), cell.row + cell.rowspan)
    for row in range(parser.rows):
        group_end = max(group_end, row + 1, row_ends.get(row, 0))
        if row + 1 == group_end:
            groups.append((group_start, group_end))
            group_start = group_end
    return TableDocument(
        source=content,
        cells=cells,
        row_count=parser.rows,
        row_groups=tuple(groups),
        format="html",
        logical_cells=parser.logical_cells,
        ignored_source_ranges=_ignored_ranges(content, cells),
    )


def _markdown_fields(line: str, offset: int) -> list[tuple[int, int]]:
    """Find cell spans without treating escaped or code-span pipes as borders."""
    stripped = line.strip(" \t")
    left = len(line) - len(line.lstrip(" \t"))
    right = len(line.rstrip(" \t"))
    if not stripped or "|" not in stripped:
        _error("invalid_markdown_row")
    cuts = []
    ticks = 0
    index = left
    while index < right:
        char = line[index]
        if char == "\\" and ticks == 0:
            index += 2
            continue
        if char == "`":
            run_end = index + 1
            while run_end < right and line[run_end] == "`":
                run_end += 1
            width = run_end - index
            if not ticks:
                ticks = width
            elif ticks == width:
                ticks = 0
            index = run_end
            continue
        if char == "|" and not ticks:
            cuts.append(index)
        index += 1
    if ticks:
        _error("unbalanced_markdown_code_span")
    if not cuts:
        _error("invalid_markdown_row")
    boundaries = [left - 1, *cuts, right]
    if cuts[0] == left:
        boundaries.pop(0)
    if cuts[-1] == right - 1:
        boundaries.pop()
    fields = []
    for before, after in zip(boundaries, boundaries[1:]):
        begin, end = before + 1, after
        while begin < end and line[begin] in " \t":
            begin += 1
        while end > begin and line[end - 1] in " \t":
            end -= 1
        fields.append((offset + begin, offset + end))
    return fields


def _parse_markdown(content: str, max_cells: int) -> TableDocument:
    """Parse a pipe table with exact field offsets and preserved alignment syntax.

    Require a valid header separator and consistent column counts. Exclude blank
    surrounding lines and reject logical grids exceeding the cell budget before
    materializing their anchored cells.
    """
    lines = []
    for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", content):
        if match.start() == match.end():
            continue
        line = match.group().rstrip("\r\n")
        lines.append((line, match.start()))
    while lines and not lines[0][0].strip():
        lines.pop(0)
    while lines and not lines[-1][0].strip():
        lines.pop()
    if len(lines) < 2:
        _error("missing_markdown_header_separator")
    fields = [_markdown_fields(line, start) for line, start in lines]
    alignment = tuple(content[start:end] for start, end in fields[1])
    if not alignment or not all(
        re.fullmatch(r":?-{3,}:?", value) for value in alignment
    ):
        _error("missing_markdown_header_separator")
    width = len(fields[0])
    if any(len(row) != width for row in fields):
        _error("inconsistent_markdown_columns")
    logical_cells = (len(lines) - 1) * width
    if logical_cells > max_cells:
        raise ChunkError(
            "chunk_work_limit",
            "Table grid exceeds the logical cell limit.",
            status_code=413,
            details={
                "limit": "table_cells",
                "maximum": max_cells,
                "required": logical_cells,
            },
        )
    cells = []
    rows = [fields[0], *fields[2:]]
    for row, cell_ranges in enumerate(rows):
        for column, (start, end) in enumerate(cell_ranges):
            runs = (_TextRun(content[start:end], start, end),) if start < end else ()
            cells.append(
                TableCell(
                    row,
                    column,
                    1,
                    1,
                    row == 0,
                    start,
                    end,
                    runs,
                )
            )
    return TableDocument(
        source=content,
        cells=tuple(cells),
        row_count=len(rows),
        row_groups=tuple((row, row + 1) for row in range(len(rows))),
        format="markdown",
        logical_cells=logical_cells,
        ignored_source_ranges=_ignored_ranges(content, tuple(cells)),
        markdown_alignment=alignment,
    )


def _position(cell: TableCell | None, representation: str, method: str) -> Mapping:
    return MappingProxyType(
        {
            "row": cell.row if cell else None,
            "column": cell.column if cell else None,
            "rowspan": cell.rowspan if cell else None,
            "colspan": cell.colspan if cell else None,
            "representation": representation,
            "header_method": method,
        }
    )


class _Renderer:
    def __init__(self, table: TableDocument, method: str, representation: str):
        self.table = table
        self.method = method
        self.representation = representation
        self.parts: list[str] = []
        self.spans: list[MappedSpan] = []
        self.length = 0

    def append(
        self,
        text: str,
        *,
        cell: TableCell | None = None,
        start: int | None = None,
        end: int | None = None,
        role: str = "separator",
    ) -> None:
        """Append text and record its output range and optional source position."""
        self.parts.append(text)
        self.spans.append(
            MappedSpan(
                start,
                end,
                self.length,
                self.length + len(text),
                role,
                _position(cell, self.representation, self.method),
            )
        )
        self.length += len(text)

    def value(
        self,
        cell: TableCell,
        *,
        role: str = "table_cell",
        value_slice: tuple[int, int] | None = None,
    ) -> None:
        """Render a decoded cell slice while preserving entity source ranges."""
        begin, end = value_slice if value_slice is not None else (0, len(cell.value))
        offset = 0
        for run in cell.runs:
            next_offset = offset + len(run.text)
            left, right = max(begin, offset), min(end, next_offset)
            if left < right:
                value = run.text[left - offset : right - offset]
                if run.end - run.start == len(run.text):
                    source_start = run.start + left - offset
                    source_end = run.start + right - offset
                else:
                    source_start, source_end = run.start, run.end
                text = (
                    escape(value, quote=False)
                    if role == "table_cell" and self.representation == "html"
                    else value
                )
                if (
                    run.html_text is not None
                    and role == "table_cell"
                    and self.representation == "html"
                ):
                    text = run.html_text
                self.append(
                    text,
                    cell=cell,
                    start=source_start,
                    end=source_end,
                    role=role,
                )
            offset = next_offset
        if not cell.value:
            self.append("", cell=cell, start=cell.start, end=cell.start, role=role)

    def headers(self, cells: tuple[TableCell, ...]) -> None:
        """Prefix repeated header context, separated from primary table text."""
        for index, cell in enumerate(cells):
            if index:
                self.append(" | ")
            self.value(cell, role="table_header")
        if cells:
            self.append("\n\n")

    def finish(self) -> Rendered:
        """Return accumulated text and immutable provenance sequences."""
        return Rendered(
            "".join(self.parts),
            tuple(self.spans),
            self.table.ignored_source_ranges,
        )


def render_rows(
    table: TableDocument, start: int, end: int, *, header_policy: str
) -> Rendered:
    """Render complete logical row groups in the table's original format.

    Args:
        table: Parsed HTML or Markdown table with indexed cells and row groups.
        start: Inclusive logical row index.
        end: Exclusive logical row index.
        header_policy: Header selection: ``declared``, ``first_row`` or ``none``.

    Returns:
        Rendered table text and provenance, with preceding headers as context.

    Raises:
        ValueError: If the header policy or half-open row range is invalid.
        ChunkError: If either row boundary splits a spanning cell's row group.
    """
    if header_policy not in ("declared", "first_row", "none"):
        raise ValueError("Unknown table header policy")
    if not 0 <= start < end <= table.row_count:
        raise ValueError("Invalid half-open table row range")
    boundaries = {0, *(group_end for _, group_end in table.row_groups)}
    if start not in boundaries or end not in boundaries:
        _error("row_range_splits_rowspan")
    declared = table._headers
    method = "declared" if declared and header_policy != "none" else header_policy
    headers = (
        declared
        if method == "declared"
        else (table._rows[0] if method == "first_row" else ())
    )
    renderer = _Renderer(table, method, table.format)
    renderer.headers(tuple(cell for cell in headers if cell.row < start))
    if table.format == "markdown":
        for row in range(start, end):
            if row != start:
                renderer.append("\n")
            renderer.append("| ")
            row_cells = table._rows[row]
            for index, cell in enumerate(row_cells):
                if index:
                    renderer.append(" | ")
                renderer.value(cell)
            renderer.append(" |")
            if row == 0:
                renderer.append("\n| " + " | ".join(table.markdown_alignment) + " |")
        return renderer.finish()
    renderer.append("<table>")
    for row in range(start, end):
        renderer.append("<tr>")
        for cell in table._rows[row]:
            tag = "th" if cell.header else "td"
            attrs = ""
            if cell.rowspan != 1:
                attrs += f' rowspan="{cell.rowspan}"'
            if cell.colspan != 1:
                attrs += f' colspan="{cell.colspan}"'
            renderer.append(f"<{tag}{attrs}>", cell=cell)
            renderer.value(cell)
            renderer.append(f"</{tag}>", cell=cell)
        renderer.append("</tr>")
    renderer.append("</table>")
    return renderer.finish()


def render_cell(
    table: TableDocument,
    cell: TableCell,
    value_slice: tuple[int, int] | None,
    *,
    header_policy: str,
) -> Rendered:
    """Render one cell or decoded value slice, with original grid coordinates."""
    if header_policy not in ("declared", "first_row", "none"):
        raise ValueError("Unknown table header policy")
    if table._cell_positions.get((cell.row, cell.column)) is not cell:
        raise ValueError("Cell does not belong to this table")
    if value_slice is not None:
        begin, end = value_slice
        if not 0 <= begin <= end <= len(cell.value):
            raise ValueError("Invalid half-open cell value slice")
        if begin == end and cell.value:
            raise ValueError("A nonempty cell cannot produce an empty value slice")
    declared = table._headers
    method = "declared" if declared and header_policy != "none" else header_policy
    headers = (
        declared
        if method == "declared"
        else (table._rows[0] if method == "first_row" else ())
    )
    renderer = _Renderer(table, method, "cells")
    renderer.append(
        f"r={cell.row}; c={cell.column}; "
        f"rowspan={cell.rowspan}; colspan={cell.colspan}\n",
        cell=cell,
        role="table_coordinates",
    )
    renderer.headers(
        tuple(
            header
            for header in headers
            if header != cell
            and header.row < cell.row
            and header.column < cell.column + cell.colspan
            and cell.column < header.column + header.colspan
        )
    )
    renderer.value(cell, value_slice=value_slice)
    return renderer.finish()
