"""Split plain prose into fragments under an injected token budget.

Cuts come from :mod:`ocrserver.chunking.text`, which already keeps code
fences and formulas atomic and orders cuts by structural cost. This module
adds the budgeted cascade (cheapest cut first, then longest prefix), the
search budget and the tail rebalancing of FR-007. It never imports a
tokenizer, the network or any browser code: the counter is injected.
"""

from __future__ import annotations

import re
import time
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import regex

from .chunking.errors import ChunkError
from .chunking.tables import parse_table, render_rows
from .chunking.text import protected_ranges, split_boundaries
from .rag_structure import text_separator


_MIN_BOUNDARIES = 1_024
MAX_TABLE_CELLS = 100_000
TABLE_HEADER_POLICY = "declared"

RagEntry = int | tuple[int, int, int] | tuple[int, int, int, str]

_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.I | re.S)
_FENCE_LINE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})[^\n]*$")
_HEADER_ROW_RE = re.compile(r"<tr\b[^>]*>(?:(?!</tr>).)*?<th\b", re.I | re.S)
_TABLE_TAG_RE = re.compile(r"<table\b[^>]*>|</table\s*>", re.I)
_MARKDOWN_TABLE_RE = re.compile(
    r"^\|[^\n]*\|[^\S\n]*\r?\n"
    r"\|[ :\-\t|]+\|[^\S\n]*\r?\n"
    r"(?:\|[^\n]*\|[^\S\n]*(?:\r?\n|$))+",
    re.M,
)


def _table_layout(text: str, table):
    """Source spans for whole headers and legal row-group boundaries.

    HTML without a declared header uses its first row. Section wrappers are
    included in the framing, so every fragment remains a complete table.
    Markdown repeats both the header and its alignment separator.
    """
    if table.format == "markdown":
        lines = [
            (start, end) for start, end in _line_spans(text) if text[start:end].strip()
        ]
        rows = [lines[0], *lines[2:]]
        header_end = lines[1][1]
        return rows, [(0, header_end), *rows[1:]], 0, (0, header_end), len(text)

    rows = [
        (
            match.start(),
            match.end() + len(re.match(r"\r?\n?", text[match.end() :]).group()),
        )
        for match in _ROW_RE.finditer(text)
    ]
    if len(rows) != table.row_count or not rows:
        raise ValueError("Table rows have no exact source spans.")
    # A body row may use <th> for its row label; it is not a column header.
    declared = {cell.row for cell in table.cells if cell.header} - {
        cell.row for cell in table.cells if not cell.header
    }
    header_rows = 0
    while header_rows in declared:
        header_rows += 1
    if not header_rows:
        # An unmarked statistical header can have column families followed
        # by years/subcolumns. A blank row-label cell on both levels and a
        # spanning parent distinguish this layout from ordinary data rows.
        header_rows = 1
        while header_rows < table.row_count:
            parent = [c for c in table.cells if c.row == header_rows - 1]
            children = [c for c in table.cells if c.row == header_rows]
            blank_parent = any(c.column == 0 and not c.value.strip() for c in parent)
            blank_child = any(c.column == 0 and not c.value.strip() for c in children)
            if not (
                blank_parent and blank_child and any(c.colspan > 1 for c in parent)
            ):
                break
            header_rows += 1
    header_rows = max(1, header_rows)
    # A rowspan connected to the header cannot be separated from it.
    header_rows = next(
        end for start, end in table.row_groups if start < header_rows <= end
    )
    header_end = rows[header_rows][0] if header_rows < len(rows) else rows[-1][1]
    groups = [(rows[0][0], header_end)] + [
        (rows[start][0], rows[end - 1][1])
        for start, end in table.row_groups
        if start >= header_rows
    ]
    return rows, groups, rows[0][0], (rows[0][0], header_end), rows[-1][1]


class Counter(Protocol):
    """Counts the tokens of a candidate fragment."""

    def count(self, text: str) -> int:
        """Return the exact model-token count for the supplied candidate text."""
        ...


@dataclass(frozen=True, slots=True)
class SplitBudget:
    """Token window plus the search limits that bound one split."""

    max_tokens: int
    min_tokens: int
    max_evaluations: int = 100_000
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class SplitResult:
    """Contiguous ``[start, end)`` pieces, note codes and budget state."""

    pieces: list[tuple[int, int]]
    notes: list[str]
    exhausted: bool


@dataclass(frozen=True, slots=True)
class SplitFragment:
    """One output fragment: its plan entries, size, status and note codes."""

    entries: list[RagEntry]
    token_count: int | None
    status: str
    notes: list[str]
    member_token_counts: list[int | None] | None = None


def render_fragment(text: str, entries: list[RagEntry]) -> str:
    """Slice ``text`` by ``entries`` and glue the slices like the server does.

    Entries of the same chunk always continue compactly; ``repeat`` framing
    entries are slices too, so the header row or fence line reappears.
    """
    parts: list[str] = []
    previous: str | None = None
    for entry in entries:
        start, end = (0, len(text)) if isinstance(entry, int) else (entry[1], entry[2])
        piece = text[start:end]
        if previous is not None:
            parts.append(text_separator(previous, piece, True))
        parts.append(piece)
        previous = piece
    return "".join(parts)


def render_entries(chunks: list[dict], entries: list[RagEntry]) -> str:
    """Render ``entries`` across chunks the way a split fragment joins them.

    Used to derive the split response's note parameters (rows, lines) from the
    same fragment text a save would measure.
    """
    parts: list[str] = []
    previous: str | None = None
    for entry in entries:
        index = entry if isinstance(entry, int) else entry[0]
        piece = render_fragment(chunks[index]["text"], [entry])
        if previous is not None:
            parts.append(text_separator(previous, piece, True))
        parts.append(piece)
        previous = piece
    return "".join(parts)


def repeat_framing_kind(text: str, start: int, end: int) -> str | None:
    """``"table"``/``"code"`` for a framing span, ``None`` when not framing.

    A framing span is a header row, a fence line, or an opening/closing
    ``<table>`` tag. FR-004 makes every divided-table fragment a complete
    table, so the tag re-emissions are framing too; the span must still be
    exactly the tag (plus surrounding newlines) in the source. The kind is
    the verifiable discriminator the saved diagnostics report.
    """
    piece = text[start:end].strip("\r\n")
    row = _ROW_RE.match(piece)
    if _TABLE_TAG_RE.fullmatch(piece) or (
        row is not None and row.end() == len(piece) and _HEADER_ROW_RE.match(piece)
    ):
        return "table"
    if _FENCE_LINE_RE.match(piece):
        return "code"
    # Exact source framing also covers inferred headers, multirow thead,
    # section wrappers, and Markdown header + alignment rows.
    for pattern in (_TABLE_RE, _MARKDOWN_TABLE_RE):
        for match in pattern.finditer(text):
            if not (match.start() <= start < end <= match.end()):
                continue
            source = match.group()
            try:
                table = parse_table(source, max_logical_cells=MAX_TABLE_CELLS)
                _, _, opening, header, closing = _table_layout(source, table)
            except (ChunkError, ValueError):
                continue
            span = (start - match.start(), end - match.start())
            if span in ((0, opening), header, (closing, len(source.rstrip("\r\n")))):
                return "table"
    return None


def is_repeat_framing(text: str, start: int, end: int) -> bool:
    """True when the slice is a repeated framing span."""
    return repeat_framing_kind(text, start, end) is not None


def sum_counts(counts: Sequence[int | None]) -> int | None:
    """Total known counts; unknown when empty or when any member is unknown."""
    if not counts:
        return None
    total = 0
    for count in counts:
        if count is None:
            return None
        total += count
    return total


def _note(notes: list[str], code: str) -> None:
    if code not in notes:
        notes.append(code)


class _SearchExhausted(Exception):
    """Stop exploratory measurements while retaining the original entries."""


class _Measurements:
    """Charges every ``counter.count`` call to the single evaluation budget.

    The budget is one number: the search loop, the rebalancing candidates
    and the final minimum check all measure through this object.
    """

    def __init__(
        self,
        counter: Counter,
        limit: int,
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._counter = counter
        self._limit = limit
        self._clock = clock
        self._deadline = clock() + timeout_seconds
        self.count = 0

    @property
    def exhausted(self) -> bool:
        return self.count >= self._limit or self._clock() >= self._deadline

    def measure(self, text: str, *, final: bool = False) -> int:
        if not final:
            if self.exhausted:
                raise _SearchExhausted
            self.count += 1
        return self._counter.count(text)


def split_prose(
    text: str,
    budget: SplitBudget,
    counter: Counter,
    clock: Callable[[], float] = time.monotonic,
    *,
    _meter: _Measurements | None = None,
) -> SplitResult:
    """Partition ``text`` into contiguous pieces of at most ``max_tokens``.

    Offers from ``split_boundaries`` are visited by increasing structural
    cost and, within a level, by longest prefix, so a paragraph cut is
    preferred over a word cut and a formula is never cut inside. Every
    candidate consumes one counter evaluation; running out of evaluations
    or time stops the search and returns the pieces found so far.
    """
    if not text:
        return SplitResult([], [], False)
    try:
        boundaries = split_boundaries(
            text, max_boundaries=max(budget.max_evaluations, _MIN_BOUNDARIES)
        )
    except ChunkError:
        return SplitResult([(0, len(text))], ["search_budget_exhausted"], True)

    by_cost: dict[int, list[int]] = {}
    for boundary in boundaries:
        by_cost.setdefault(boundary.cost, []).append(boundary.offset)
    for offsets in by_cost.values():
        offsets.sort()
    levels = sorted(by_cost)
    all_offsets = sorted(boundary.offset for boundary in boundaries)

    notes: list[str] = []
    pieces: list[tuple[int, int]] = []
    meter = _meter or _Measurements(
        counter, budget.max_evaluations, budget.timeout_seconds, clock
    )
    started = clock()
    exhausted = False
    length = len(text)
    start = 0

    while start < length:
        try:
            if meter.measure(text[start:]) <= budget.max_tokens:
                pieces.append((start, length))
                break
        except _SearchExhausted:
            exhausted = True
            pieces.append((start, length))
            break
        best: int | None = None
        for cost in levels:
            offsets = by_cost[cost]
            for offset in offsets[bisect_left(offsets, start + 1) :]:
                if meter.exhausted or clock() - started > budget.timeout_seconds:
                    exhausted = True
                    break
                try:
                    count = meter.measure(text[start:offset])
                except _SearchExhausted:
                    exhausted = True
                    break
                if count <= budget.max_tokens:
                    best = offset
                    continue
                break
            if exhausted or best is not None:
                break
        if best is None:
            if exhausted:
                pieces.append((start, length))
                break
            index = bisect_left(all_offsets, start + 1)
            best = all_offsets[index] if index < len(all_offsets) else length
            _note(notes, "above_maximum_fragment")
        pieces.append((start, best))
        start = best
        if exhausted:
            if start < length:
                pieces.append((start, length))
            break

    if exhausted:
        _note(notes, "search_budget_exhausted")
    else:
        try:
            _rebalance(
                pieces,
                text,
                budget,
                all_offsets,
                meter,
                {boundary.offset: boundary.cost for boundary in boundaries},
            )
        except _SearchExhausted:
            exhausted = True
            _note(notes, "search_budget_exhausted")

    # The final check measures even past the budget: FR-009's note depends on
    # the token count, and skipping it would hide a genuinely short piece.
    if any(
        meter.measure(text[start:end], final=True) < budget.min_tokens
        for start, end in pieces
    ):
        _note(notes, "below_minimum_fragment")

    return SplitResult(pieces, notes, exhausted)


@dataclass
class _Pending:
    entries: list[RagEntry]
    texts: list[str]
    notes: list[str]
    continued: bool = True
    token_count: int | None = None
    status: str = "within"


def _normalize_entry(entry: RagEntry, chunks: list[dict]) -> tuple[int, int, int, bool]:
    if isinstance(entry, int):
        return entry, 0, len(chunks[entry]["text"]), False
    if len(entry) == 4:
        return entry[0], entry[1], entry[2], True
    return entry[0], entry[1], entry[2], False


def _member_counts(chunks, entries, meter, *, final=False):
    bundles = []
    for entry in entries:
        index = entry if isinstance(entry, int) else entry[0]
        if bundles and bundles[-1][0] == index:
            bundles[-1][1].append(entry)
        else:
            bundles.append((index, [entry]))
    return [
        meter.measure(render_fragment(chunks[index]["text"], bundle), final=final)
        for index, bundle in bundles
    ]


def _finish(
    fragment: _Pending, budget: SplitBudget, meter: _Measurements, chunks: list[dict]
) -> SplitFragment:
    text = "".join(fragment.texts)
    members = _member_counts(chunks, fragment.entries, meter, final=True)
    fragment.token_count = sum_counts(members) if text else 0
    fragment.notes = [
        code
        for code in fragment.notes
        if code not in ("below_minimum_fragment", "above_maximum_fragment")
    ]
    if fragment.token_count > budget.max_tokens:
        fragment.status = "above"
        if "math_atomic" not in fragment.notes:
            _note(fragment.notes, "above_maximum_fragment")
    elif fragment.token_count < budget.min_tokens:
        fragment.status = "below"
        _note(fragment.notes, "below_minimum_fragment")
    else:
        fragment.status = "within"
    return SplitFragment(
        list(fragment.entries),
        fragment.token_count,
        fragment.status,
        fragment.notes,
        members,
    )


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"[^\n]*\n|[^\n]+$", text):
        spans.append((match.start(), match.end()))
    return spans


def _table_fragments(
    chunk_index: int,
    ctext: str,
    tstart: int,
    tend: int,
    budget: SplitBudget,
    meter: _Measurements,
    prefix: _Pending | None = None,
) -> list[_Pending]:
    """Split a table on row-group ends, repeating every re-emitted frame slice.

    Non-``repeat`` entries partition ``[tstart, tend)`` exactly once: the first
    fragment owns the opening tag plus the header row and its rows, each later
    fragment owns only its row group, and the last fragment owns the closing
    tag. Every re-emission (opening tag, header row, closing tag) is a
    ``repeat`` entry, so no character is ever covered twice by non-``repeat``
    entries. A table that no row-group boundary can bring within the maximum
    stays whole with ``above_maximum_fragment`` and is never cut inside.
    """
    table_text = ctext[tstart:tend]
    prefix_entries = list(prefix.entries) if prefix is not None else []
    prefix_notes = list(prefix.notes) if prefix is not None else []
    whole_entry: RagEntry = (chunk_index, tstart, tend)

    def whole() -> list[_Pending]:
        text = render_fragment(ctext, [*prefix_entries, whole_entry])
        notes = list(prefix_notes)
        if meter.measure(text) > budget.max_tokens:
            _note(notes, "above_maximum_fragment")
        return [_Pending([*prefix_entries, whole_entry], [text], notes)]

    if (
        meter.measure(render_fragment(ctext, [*prefix_entries, whole_entry]))
        <= budget.max_tokens
    ):
        return whole()

    try:
        table = parse_table(table_text, max_logical_cells=MAX_TABLE_CELLS)
    except ChunkError:
        return whole()
    try:
        rows, group_spans, open_end, header_span, close_start = _table_layout(
            table_text, table
        )
    except ValueError:
        return whole()
    # Validate the whole document through the same renderer the chunking uses.
    render_rows(table, 0, table.row_count, header_policy=TABLE_HEADER_POLICY)
    # Only row-group ends are legal cut points (FR-004); a merged group
    # (rowspan) is indivisible, so each group is the smallest unit.
    group_ends = {end for _, end in group_spans}
    open_repeat: RagEntry = (chunk_index, tstart, tstart + open_end, "repeat")
    # Section wrappers are framing too; trailing newlines are owned once by
    # the last fragment, rather than repeated in every fragment.
    closing_end = max(close_start, len(table_text.rstrip("\r\n")))
    close_repeat: RagEntry = (
        chunk_index,
        tstart + close_start,
        tstart + closing_end,
        "repeat",
    )

    def frame(first: bool, own_start: int, own_end: int, last: bool) -> list[RagEntry]:
        entries: list[RagEntry] = []
        if not first:
            if open_end:
                entries.append(open_repeat)
            if header_span is not None:
                entries.append(
                    (
                        chunk_index,
                        tstart + header_span[0],
                        tstart + header_span[1],
                        "repeat",
                    )
                )
        entries.append((chunk_index, own_start, own_end))
        if not last and close_start < closing_end:
            entries.append(close_repeat)
        return entries

    def cost(first: bool, own_start: int, own_end: int) -> int:
        last = own_end >= tstart + group_spans[-1][1]
        if last:
            own_end = tend
        entries = frame(first, own_start, own_end, last)
        text = render_fragment(ctext, [*prefix_entries, *entries] if first else entries)
        return meter.measure(text)

    # R4: a header row that already busts the maximum becomes its own fragment.
    header_only = (
        header_span is not None
        and header_span[1] in group_ends
        and meter.measure(
            render_fragment(
                ctext,
                [
                    (chunk_index, tstart, tstart + open_end),
                    (chunk_index, tstart + header_span[0], tstart + header_span[1]),
                    close_repeat,
                ],
            )
        )
        > budget.max_tokens
    )
    if not header_only:
        # R9: if no row-group boundary can bring every fragment within the
        # maximum, the table stays whole -- prose levels never cut a table.
        first_group = group_spans[0]
        if cost(True, tstart, tstart + first_group[1]) > budget.max_tokens or any(
            cost(False, tstart + start, tstart + end) > budget.max_tokens
            for start, end in group_spans[1:]
        ):
            return whole()

    planned: list[tuple[bool, int, int]] = []
    position = 0
    if header_only:
        planned.append((True, 0, header_span[1]))
        position = header_span[1]
    index = next(
        (i for i, (start, _) in enumerate(group_spans) if start >= position),
        len(group_spans),
    )
    while index < len(group_spans):
        is_first = not planned
        own_start = position
        chosen: int | None = None
        while index < len(group_spans):
            own_end = group_spans[index][1]
            if (
                chosen is not None
                and cost(is_first, tstart + own_start, tstart + own_end)
                > budget.max_tokens
            ):
                break
            chosen = own_end
            index += 1
        if chosen is None:
            break
        planned.append((is_first, own_start, chosen))
        position = chosen
    if not planned:
        return whole()
    # Redistribute complete row groups when the final fragment is short.
    if len(planned) >= 2 and not header_only:
        first, previous_start, previous_end = planned[-2]
        _, last_start, last_end = planned[-1]
        if cost(False, tstart + last_start, tstart + last_end) < budget.min_tokens:
            for moved in sorted(group_ends, reverse=True):
                if not previous_start < moved < previous_end:
                    continue
                left = cost(first, tstart + previous_start, tstart + moved)
                right = cost(False, tstart + moved, tstart + last_end)
                if (
                    budget.min_tokens <= left <= budget.max_tokens
                    and budget.min_tokens <= right <= budget.max_tokens
                ):
                    planned[-2] = (first, previous_start, moved)
                    planned[-1] = (False, moved, last_end)
                    break
    fragments: list[_Pending] = []
    last_index = len(planned) - 1
    for index, (is_first, own_start, own_end) in enumerate(planned):
        if index == last_index:
            # the last fragment owns the tail gap and the closing tag
            own_end = len(table_text)
        entries = frame(
            is_first, tstart + own_start, tstart + own_end, index == last_index
        )
        if is_first:
            entries = [*prefix_entries, *entries]
            text = render_fragment(ctext, entries)
            notes = [*prefix_notes, "table_split"]
        else:
            text = render_fragment(ctext, entries)
            notes = ["table_split"]
        fragments.append(_Pending(entries, [text], notes))
    return fragments


def _code_fragments(
    chunk_index: int,
    ctext: str,
    region,
    budget: SplitBudget,
    meter: _Measurements,
    owned_end: int | None = None,
) -> list[_Pending]:
    """Split a fenced block on line ends, repeating the fence lines.

    The opening fence line is owned once by the first fragment and the closing
    line once by the last; every other fragment re-emits them as ``repeat``
    entries, so the non-``repeat`` entries tile the region exactly once.
    ``owned_end`` extends the owned closing slice past the trailing newlines
    that follow the fence (R12); it defaults to the region end.
    """
    if owned_end is None:
        owned_end = region.end
    body = ctext[region.body_start : region.body_end]
    lines = _line_spans(body)
    whole_entries = [(chunk_index, region.start, owned_end)]
    whole_text = render_fragment(ctext, whole_entries)
    if meter.measure(whole_text) <= budget.max_tokens:
        return [_Pending(whole_entries, [whole_text], [])]

    def cost(start, end):
        return meter.measure(
            render_fragment(
                ctext,
                [
                    (chunk_index, region.start, region.body_start, "repeat"),
                    (chunk_index, region.body_start + start, region.body_start + end),
                    (chunk_index, region.body_end, owned_end, "repeat"),
                ],
            )
        )

    owned: list[tuple[list[RagEntry], list[str]]] = []
    line_index = 0
    grapheme = False
    while line_index < len(lines):
        start, end = lines[line_index]
        line = body[start:end]
        if cost(start, end) > budget.max_tokens:
            grapheme = True
            boundaries = [match.end() for match in regex.finditer(r"\X", line)]
            piece_start = 0
            while piece_start < len(line):
                chosen = None
                for piece_end in boundaries[bisect_left(boundaries, piece_start + 1) :]:
                    if cost(start + piece_start, start + piece_end) > budget.max_tokens:
                        break
                    chosen = piece_end
                if chosen is None:
                    chosen = boundaries[bisect_left(boundaries, piece_start + 1)]
                owned.append(
                    (
                        [
                            (
                                chunk_index,
                                region.body_start + start + piece_start,
                                region.body_start + start + chosen,
                            ),
                        ],
                        ["code_split"],
                    )
                )
                piece_start = chosen
            line_index += 1
            continue
        entries: list[RagEntry] = []
        while line_index < len(lines):
            line_start, line_end = lines[line_index]
            trial = entries + [
                (
                    chunk_index,
                    region.body_start + line_start,
                    region.body_start + line_end,
                )
            ]
            probe = render_fragment(
                ctext,
                [
                    (chunk_index, region.start, region.body_start, "repeat"),
                    *trial,
                    (chunk_index, region.body_end, region.end, "repeat"),
                ],
            )
            if meter.measure(probe) > budget.max_tokens:
                break
            entries = trial
            line_index += 1
        owned.append((entries, ["code_split"]))
    if not owned:
        owned.append(
            ([(chunk_index, region.body_start, region.body_end)], ["code_split"])
        )
    fragments: list[_Pending] = []
    last_index = len(owned) - 1
    for position, (entries, notes) in enumerate(owned):
        framed: list[RagEntry] = [
            (chunk_index, region.start, region.body_start)
            if position == 0
            else (chunk_index, region.start, region.body_start, "repeat")
        ]
        framed.extend(entries)
        framed.append(
            (chunk_index, region.body_end, owned_end)
            if position == last_index
            else (chunk_index, region.body_end, region.end, "repeat")
        )
        fragments.append(
            _Pending(framed, [render_fragment(ctext, framed)], list(notes))
        )
    if grapheme:
        _note(fragments[0].notes, "grapheme_split")
    return fragments


def _split_range(
    chunk_index: int,
    ctext: str,
    start: int,
    end: int,
    options,
    budget: SplitBudget,
    meter: _Measurements,
    counter: Counter,
) -> list[_Pending]:
    """Split one source interval into bounded prose and explicitly framed structures.

    Keep an already-fitting interval byte-for-byte intact. Preserve incomplete
    tables and atomic formulas, reconstruct permitted code/table fragments and
    assign trailing newlines only within this interval to avoid overlapping a
    neighboring entry from the same source chunk.
    """
    if start >= end:
        return []
    original = ctext[start:end]
    if meter.measure(original) <= budget.max_tokens:
        # Preserve the original lexical spacing around inline structures.
        # Splitting and rejoining them can add framing separators and make
        # an already admissible interval exceed its original token budget.
        return [_Pending([(chunk_index, start, end)], [original], [])]
    events: list[tuple[int, int, str, object]] = []
    protected = protected_ranges(ctext[start:end])
    depth = 0
    incomplete_table = False
    for tag in _TABLE_TAG_RE.finditer(ctext, start, end):
        if any(
            region.kind == "code"
            and start + region.start <= tag.start() < start + region.end
            for region in protected
        ):
            continue
        depth += -1 if tag.group().startswith("</") else 1
        if depth < 0:
            incomplete_table = True
    if depth or incomplete_table:
        # A table may already span source chunks. Its complete frame cannot
        # be reconstructed from this range: preserve it rather than cutting
        # HTML tags/cells as prose. The final size check reports overflow.
        entries = [(chunk_index, start, end)]
        return [_Pending(entries, [ctext[start:end]], [])]
    for pattern in (_TABLE_RE, _MARKDOWN_TABLE_RE):
        for match in pattern.finditer(ctext, start, end):
            if any(
                region.kind == "code"
                and start + region.start <= match.start() < start + region.end
                for region in protected
            ):
                continue
            events.append((match.start(), match.end(), "table", match))
    for region in protected:
        region_start, region_end = start + region.start, start + region.end
        if any(
            event_start <= region_start < event_end
            for event_start, event_end, _, _ in events
        ):
            continue
        if region.kind == "code" and options.keep_code_blocks_together:
            events.append(
                (
                    region_start,
                    region_end,
                    "code",
                    replace(
                        region,
                        start=region_start,
                        end=region_end,
                        body_start=start + region.body_start,
                        body_end=start + region.body_end,
                    ),
                )
            )
        elif region.kind == "formula":
            events.append((region_start, region_end, "formula", region))
    events.sort()
    fragments: list[_Pending] = []
    position = start
    for event_start, event_end, kind, payload in events:
        if event_start < position:
            continue
        gap: list[_Pending] = []
        if position < event_start:
            prose = split_prose(
                ctext[position:event_start], budget, counter, _meter=meter
            )
            for piece_start, piece_end in prose.pieces:
                entries = [(chunk_index, position + piece_start, position + piece_end)]
                gap.append(
                    _Pending(
                        entries, [render_fragment(ctext, entries)], list(prose.notes)
                    )
                )
        # R12: the newlines right after a table, a closing fence or a formula
        # belong to this entry's frame, never past the entry's own ``end``:
        # a group can hold two partial entries of the same chunk (a glued and
        # re-cut row), and absorbing past ``end`` would overlap the next
        # entry's range and make ``materialize`` reject the plan it proposed.
        owned_end = event_end
        while owned_end < end and ctext[owned_end] in "\r\n":
            owned_end += 1
        if kind == "table":
            prefix = gap.pop() if len(gap) == 1 else None
            fragments.extend(gap)
            # R9: a table is never cut at prose level; when no row-group
            # boundary fits the budget the whole table is one non-repeat entry.
            fragments.extend(
                _table_fragments(
                    chunk_index, ctext, event_start, owned_end, budget, meter, prefix
                )
            )
        elif kind == "code":
            fragments.extend(gap)
            fragments.extend(
                _code_fragments(chunk_index, ctext, payload, budget, meter, owned_end)
            )
        else:
            fragments.extend(gap)
            entries = [(chunk_index, event_start, owned_end)]
            text = render_fragment(ctext, entries)
            count = meter.measure(text)
            notes = ["math_atomic"] if count > budget.max_tokens else []
            fragments.append(_Pending(entries, [text], notes))
        position = owned_end
        while position < end and ctext[position] in "\r\n":
            position += 1
    if position < end:
        prose = split_prose(ctext[position:end], budget, counter, _meter=meter)
        for piece_start, piece_end in prose.pieces:
            entries = [(chunk_index, position + piece_start, position + piece_end)]
            fragments.append(
                _Pending(entries, [render_fragment(ctext, entries)], list(prose.notes))
            )
    return fragments


def _bounded_split_range(
    chunk_index, text, start, end, options, budget, meter, counter
):
    """On exhausted search, preserve the original range and flag the limit."""
    try:
        return _split_range(
            chunk_index, text, start, end, options, budget, meter, counter
        )
    except _SearchExhausted:
        entries = [(chunk_index, start, end)]
        return [_Pending(entries, [text[start:end]], ["search_budget_exhausted"])]


def split_group(
    chunks: list[dict],
    entries: list[RagEntry],
    options,
    counter: Counter,
    budget: SplitBudget,
) -> list[SplitFragment]:
    """Split one group into fragments, keeping tables and fences framed.

    Tables repeat their declared header row, code fences reopen and close
    around every fragment, formulas stay atomic, and the prose between them
    goes through :func:`split_prose`. Fragments are glued back together when
    the combined text still fits the maximum.
    """
    meter = _Measurements(counter, budget.max_evaluations, budget.timeout_seconds)
    normalized = [_normalize_entry(entry, chunks) for entry in entries]
    pending: list[_Pending] = []
    boundary_note: str | None = None
    index = 0
    while index < len(normalized):
        chunk_index, start, end, _repeated = normalized[index]
        ctext = chunks[chunk_index]["text"]
        following = index + 1
        while following < len(normalized) and normalized[following][0] == chunk_index:
            following += 1
        bundle = normalized[index:following]
        if any(repeated for _, _, _, repeated in bundle):
            # Re-cut the rendered frame as a complete table/code block, then
            # map ranges back to the original source. Treating each repeat
            # entry as prose would detach the header from its body rows.
            rendered = ""
            spans = []
            previous = None
            for _, begin, finish, repeated in bundle:
                piece = ctext[begin:finish]
                if previous is not None:
                    rendered += text_separator(previous, piece, True)
                spans.append(
                    (len(rendered), len(rendered) + len(piece), begin, repeated)
                )
                rendered += piece
                previous = piece
            pieces = _bounded_split_range(
                chunk_index, rendered, 0, len(rendered), options, budget, meter, counter
            )
            for fragment in pieces:
                mapped = []
                for entry in fragment.entries:
                    for low, high, source_start, inherited_repeat in spans:
                        begin, finish = max(low, entry[1]), min(high, entry[2])
                        if begin >= finish:
                            continue
                        target = (
                            chunk_index,
                            source_start + begin - low,
                            source_start + finish - low,
                        )
                        mapped.append(
                            (*target, "repeat")
                            if inherited_repeat or len(entry) == 4
                            else target
                        )
                fragment.entries = mapped
                fragment.texts = [render_fragment(ctext, mapped)]
        else:
            pieces = []
            for _, begin, finish, _ in bundle:
                pieces.extend(
                    _bounded_split_range(
                        chunk_index,
                        ctext,
                        begin,
                        finish,
                        options,
                        budget,
                        meter,
                        counter,
                    )
                )
        continued = True
        if index:
            previous = chunks[normalized[index - 1][0]]
            current = chunks[chunk_index]
            if normalized[index - 1][0] != chunk_index:
                if options.page_boundary and previous.get("page") != current.get(
                    "page"
                ):
                    boundary_note, continued = "page_boundary_forced", False
                elif previous.get("document") != current.get("document"):
                    boundary_note, continued = "document_forced", False
        if pieces:
            pieces[0].continued = continued
            if boundary_note:
                _note(pieces[0].notes, boundary_note)
                boundary_note = None
        pending.extend(pieces)
        index = following
    merged: list[_Pending] = []
    for fragment in pending:
        if merged and fragment.continued:
            previous = merged[-1]
            separator = text_separator(
                "".join(previous.texts), "".join(fragment.texts), True
            )
            try:
                combined = meter.measure(
                    "".join(previous.texts) + separator + "".join(fragment.texts)
                )
                joined = [*previous.entries, *fragment.entries]
                if (
                    len(
                        {
                            entry if isinstance(entry, int) else entry[0]
                            for entry in joined
                        }
                    )
                    > 1
                ):
                    combined = max(
                        combined, sum_counts(_member_counts(chunks, joined, meter))
                    )
            except _SearchExhausted:
                _note(fragment.notes, "search_budget_exhausted")
                merged.append(fragment)
                continue
            if combined <= budget.max_tokens:
                previous.entries.extend(fragment.entries)
                previous.texts = [
                    "".join(previous.texts) + separator + "".join(fragment.texts)
                ]
                for code in fragment.notes:
                    _note(previous.notes, code)
                continue
        merged.append(fragment)
    return [_finish(fragment, budget, meter, chunks) for fragment in merged]


def _rebalance(
    pieces: list[tuple[int, int]],
    text: str,
    budget: SplitBudget,
    offsets: list[int],
    meter: _Measurements,
    costs: dict[int, int] | None = None,
) -> None:
    """Move the last cut left while the previous piece can still yield.

    Every candidate is measured with the token counter, and each of those
    measurements is charged to the same evaluation budget as the search.
    """
    if len(pieces) < 2 or meter.exhausted:
        return
    last_start, last_end = pieces[-1]
    if meter.measure(text[last_start:last_end]) >= budget.min_tokens:
        return
    previous_start, previous_end = pieces[-2]
    candidates = offsets[
        bisect_left(offsets, previous_start + 1) : bisect_left(offsets, previous_end)
    ]
    candidates.sort(
        key=lambda moved: ((costs or {}).get(moved, 0), previous_end - moved)
    )
    for moved in candidates:
        if meter.exhausted:
            return
        left = meter.measure(text[previous_start:moved])
        right = meter.measure(text[moved:last_end])
        if (
            budget.min_tokens <= left <= budget.max_tokens
            and budget.min_tokens <= right <= budget.max_tokens
        ):
            pieces[-2] = (previous_start, moved)
            pieces[-1] = (moved, last_end)
            return
