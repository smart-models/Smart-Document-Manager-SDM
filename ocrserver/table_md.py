"""Convert inline HTML tables in pipeline markdown to pipe-table markdown.

The PaddleOCR-VL pipeline emits tables as inline HTML because pipe markdown
cannot express merged cells. With table_format=markdown every table is
converted: merged cells (rowspan/colspan) are expanded by repeating the
cell value in every covered grid position, which is lossy and counted in
the returned counter. Structured block content always keeps the original
HTML so downstream consumers can still parse spans losslessly.
"""

from __future__ import annotations

from html.parser import HTMLParser


def _read_html(parser: HTMLParser, source: str) -> bool:
    """Recognize CPython's two input errors for malformed marked sections."""
    try:
        parser.feed(source)
        parser.close()
    except AssertionError as exc:
        message = str(exc)
        malformed_section = (
            message.startswith("unknown status keyword ")
            and message.endswith(" in marked section")
        ) or message.startswith("expected name token at ")
        if not malformed_section:
            raise
        return False
    return True


class _TableRanges(HTMLParser):
    """Locate complete outer tables without matching an inner closing tag."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self.ranges: list[tuple[int, int]] = []
        self._depth = 0
        self._start = 0
        self._line_offsets = [0] + [
            index + 1 for index, character in enumerate(source) if character == "\n"
        ]

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            if self._depth == 0:
                self._start = self._offset()
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                end = self.source.index(">", self._offset()) + 1
                self.ranges.append((self._start, end))

    def close(self) -> None:
        super().close()
        if self._depth:
            self.ranges.append((self._start, len(self.source)))


class _TableReader(HTMLParser):
    """Expand one outer table into an occupancy grid of cell texts."""

    _SEPARATORS = {"br", "p", "div", "li"}

    def __init__(self) -> None:
        super().__init__()
        self.grid: dict[tuple[int, int], str] = {}
        self._occupied: set[tuple[int, int]] = set()
        self._row_index = -1
        self._row_open = False
        self._cell: list[str] | None = None
        self._cell_span: tuple[int, int, int, int] | None = None
        self.row_tags: dict[int, list[str]] = {}
        self.row_spans: dict[int, list[int]] = {}
        self.thead_rows: list[int] = []
        self._in_thead = False
        self._in_caption = False
        self.caption_parts: list[str] = []
        self._depth = 0
        self.spans_expanded = False

    def _separate(self) -> None:
        if self._cell is not None:
            self._cell.append(" ")
        elif self._in_caption:
            self.caption_parts.append(" ")

    def _image(self, attrs) -> None:
        alt = next((value for name, value in attrs if name == "alt"), None)
        text = f" {alt or '[image]'} "
        if self._cell is not None:
            self._cell.append(text)
        elif self._in_caption:
            self.caption_parts.append(text)

    def _open_row(self) -> None:
        self._row_index += 1
        self._row_open = True
        if self._in_thead:
            self.thead_rows.append(self._row_index)

    def _open_cell(self, tag: str, attrs) -> None:
        if not self._row_open:
            self._open_row()
        spans = {"rowspan": 1, "colspan": 1}
        for name, value in attrs:
            if name in spans:
                try:
                    spans[name] = max(1, int((value or "1").strip()))
                except ValueError:
                    spans[name] = 1
        row = self._row_index
        column = 0
        while (row, column) in self._occupied:
            column += 1
        rowspan, colspan = spans["rowspan"], spans["colspan"]
        if rowspan > 1 or colspan > 1:
            self.spans_expanded = True
        for r in range(row, row + rowspan):
            for c in range(column, column + colspan):
                self._occupied.add((r, c))
        self.row_tags.setdefault(row, []).append(tag)
        self.row_spans.setdefault(row, []).append(rowspan)
        self._cell = []
        self._cell_span = (row, column, rowspan, colspan)

    def _flush_cell(self) -> None:
        if self._cell is None:
            return
        text = " ".join("".join(self._cell).split())
        row, column, rowspan, colspan = self._cell_span
        for r in range(row, row + rowspan):
            for c in range(column, column + colspan):
                self.grid[(r, c)] = text
        self._cell = None
        self._cell_span = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth > 1:
                self._separate()
            return
        if self._depth > 1:
            if tag in ("tr", "td", "th") or tag in self._SEPARATORS:
                self._separate()
            elif tag == "img":
                self._image(attrs)
            return
        if tag == "tr":
            self._flush_cell()
            self._open_row()
        elif tag in ("td", "th"):
            self._flush_cell()
            self._open_cell(tag, attrs)
        elif tag == "thead":
            self._in_thead = True
        elif tag == "caption":
            self._in_caption = True
        elif tag == "img":
            self._image(attrs)
        elif tag in self._SEPARATORS:
            self._separate()

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth > 1:
                self._separate()
            elif self._depth == 1:
                self._flush_cell()
            self._depth = max(0, self._depth - 1)
            return
        if self._depth > 1:
            if tag in ("tr", "td", "th") or tag in self._SEPARATORS:
                self._separate()
            return
        if tag in ("td", "th"):
            self._flush_cell()
        elif tag == "tr":
            self._flush_cell()
            self._row_open = False
        elif tag == "thead":
            self._in_thead = False
        elif tag == "caption":
            self._in_caption = False
        elif tag in self._SEPARATORS:
            self._separate()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        elif self._in_caption:
            self.caption_parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush_cell()

    def header_rows(self, n_rows: int) -> list[int]:
        if self.thead_rows:
            return sorted(set(self.thead_rows))
        headers = []
        for r in range(n_rows):
            tags = self.row_tags.get(r)
            if not tags or not all(tag == "th" for tag in tags):
                break
            headers.append(r)
        if headers:
            return headers
        height = max(self.row_spans.get(0) or [1])
        return list(range(min(n_rows, height)))


def _to_pipes(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    esc = [
        [c.replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|") for c in row]
        for row in padded
    ]
    lines = ["| " + " | ".join(esc[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in esc[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render(reader: _TableReader) -> str:
    if not reader.grid:
        return ""
    n_rows = reader._row_index + 1
    n_cols = max(column for _, column in reader.grid) + 1
    headers = reader.header_rows(n_rows)
    header_set = set(headers)
    header = []
    for column in range(n_cols):
        parts = []
        for row in headers:
            text = reader.grid.get((row, column), "")
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        header.append(" ".join(parts))
    data = [
        [reader.grid.get((row, column), "") for column in range(n_cols)]
        for row in range(n_rows)
        if row not in header_set
    ]
    table = _to_pipes([header] + data)
    caption = " ".join("".join(reader.caption_parts).split())
    if caption:
        return caption + "\n\n" + table
    return table


def tables_to_markdown(markdown: str) -> tuple[str, int]:
    """Replace HTML tables with pipe tables, expanding merged cells.

    Returns (converted_markdown, spans_expanded_count) where the count is
    the number of tables whose rowspan/colspan merged cells were expanded
    into repeated values (a lossy conversion reported to the caller).
    """
    locator = _TableRanges(markdown)
    if not _read_html(locator, markdown):
        # Retain the entire original document when its HTML cannot be parsed.
        return markdown, 0
    expanded = 0
    pieces: list[str] = []
    last_end = 0
    for start, end in locator.ranges:
        pieces.append(markdown[last_end:start])
        original = markdown[start:end]
        reader = _TableReader()
        if _read_html(reader, original):
            pieces.append(_render(reader))
            if reader.spans_expanded:
                expanded += 1
        else:
            pieces.append(original)
        last_end = end
    pieces.append(markdown[last_end:])
    return "".join(pieces), expanded
