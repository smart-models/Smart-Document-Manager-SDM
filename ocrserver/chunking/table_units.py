"""Turn mapped table renderings into the structural engine's fragments."""

from dataclasses import replace

from .errors import ChunkError
from .rendering import source_fragment
from .tables import Rendered, parse_table, render_cell, render_rows
from .types import Fragment, budget_details, diagnostic, generated_span


def _fragment(source, rendered: Rendered, *, protected=None, diagnostics=()):
    primary, context = [], []
    for span in rendered.spans:
        if span.start is None:
            mapped = generated_span(span.text_start, span.text_end, span.role)
        else:
            mapped = {
                **source.reference(),
                "start": span.start,
                "end": span.end,
                "text_start": span.text_start,
                "text_end": span.text_end,
                "role": span.role,
            }
        mapped["table"] = dict(span.table_position)
        if span.role == "table_cell":
            primary.append(mapped)
        else:
            mapped.pop("identifiers", None)
            context.append(mapped)
    return Fragment(
        rendered.text,
        tuple(primary),
        tuple(context),
        kind="table",
        protected=protected,
        diagnostics=tuple(diagnostics),
        ignored_source_ranges=tuple(
            (source.key, start, end) for start, end in rendered.ignored_source_ranges
        ),
    )


def _raw_fallback(source, reason):
    raw = source_fragment(
        source, role="table_cell", kind="table", protected="protected_table"
    )
    representation = "html" if source.content.lstrip().startswith("<") else "markdown"
    spans = tuple(
        {
            **span,
            "table": {
                "row": None,
                "column": None,
                "rowspan": None,
                "colspan": None,
                "representation": representation,
                "header_method": "none",
            },
        }
        for span in raw.source_spans
    )
    return replace(
        raw,
        source_spans=spans,
        diagnostics=(diagnostic("table_parse_fallback", [source], reason=reason),),
    )


def _cell_fragments(source, table, cells, section, options, work, diagnostics):
    """Render oversized row groups as exact-token cell fragments with provenance.

    Include selected header context and preserve split diagnostics. Classify
    exhausted candidates as context or content failures only after measured
    splitting fails; prefix estimates cannot prove that no candidate fits.
    """
    from .splitting import _count, split_value

    diagnostics = (
        *diagnostics,
        diagnostic(
            "table_representation",
            [source],
            representation="cells",
        ),
    )
    result = []
    for cell in cells:

        def make(left, right):
            return _fragment(
                source,
                render_cell(
                    table,
                    cell,
                    (left, right),
                    header_policy=options.table_header_policy,
                ),
                diagnostics=diagnostics,
            )

        whole = make(0, len(cell.value))
        required = _count(section, whole, work)
        if required <= options.max_tokens:
            result.append(whole)
        elif not cell.value:
            raise ChunkError(
                "context_exceeds_budget",
                "Empty cell coordinates exceed the budget.",
                details=budget_details(source.reference(), options, required),
            )
        else:
            try:
                result.extend(split_value(cell.value, make, section, options, work))
            except ChunkError as error:
                if error.code not in (
                    "unsplittable_content",
                    "context_exceeds_budget",
                ):
                    raise
                # This classification happens only after all permitted value
                # candidates failed. A prefix count never prunes candidates.
                context = "".join(
                    whole.text[span["text_start"] : span["text_end"]]
                    for span in whole.context_spans
                )
                if section.title:
                    context = section.title + "\n\n" + context
                headers = any(
                    span["role"] == "table_header" for span in whole.context_spans
                )
                code = error.code
                work.consume("split_evaluations")
                if work.count(context) >= options.max_tokens:
                    code = "context_exceeds_budget"
                details = {
                    **error.details,
                    "source_key": source.key,
                    "page": source.page,
                    "type": "table",
                    "required_tokens": required,
                    "max_tokens": options.max_tokens,
                    "conflicting_option": "table_header_policy"
                    if headers
                    else "max_tokens",
                }
                raise ChunkError(code, error.message, details=details) from error
    return result


def table_fragments(source, section, options, work):
    """Prepare a table; the packer alone decides protected overflow admission."""
    remaining = work.limits.max_table_cells - work.counters.get("table_cells", 0)
    try:
        table = parse_table(source.content, max_logical_cells=remaining)
    except ChunkError as error:
        if error.code == "chunk_work_limit":
            raise ChunkError(
                error.code,
                error.message,
                status_code=error.status_code,
                details={
                    **error.details,
                    "source_key": source.key,
                    "maximum": work.limits.max_table_cells,
                    "required": work.counters.get("table_cells", 0)
                    + error.details["required"],
                },
            ) from error
        if error.code != "unsplittable_table" or not options.preserve_tables:
            raise
        return [_raw_fallback(source, error.details.get("reason", "unsupported_table"))]
    work.consume("table_cells", table.logical_cells)
    whole = _fragment(
        source,
        render_rows(table, 0, table.row_count, header_policy="none"),
        protected="protected_table" if options.preserve_tables else None,
    )
    if options.preserve_tables:
        return [whole]
    from .splitting import _count

    if _count(section, whole, work) <= options.max_tokens:
        return [whole]
    try:
        return _divide_table(source, table, section, options, work)
    except ChunkError as error:
        if error.code not in {
            "context_exceeds_budget",
            "unsplittable_content",
        }:
            raise
        return [replace(whole, failure=error)]


def _divide_table(source, table, section, options, work):
    from .splitting import _count

    declared = any(cell.header for cell in table.cells)
    diagnostics = []
    if not declared and options.table_header_policy == "declared":
        diagnostics.append(diagnostic("table_header_missing", [source]))
    elif not declared and options.table_header_policy == "first_row":
        diagnostics.append(diagnostic("table_header_first_row", [source]))
    fragments = []
    position = 0
    for start, end in table.row_groups:
        rows = _fragment(
            source,
            render_rows(table, start, end, header_policy=options.table_header_policy),
            diagnostics=diagnostics,
        )
        first_cell = position
        while position < len(table.cells) and table.cells[position].row < end:
            position += 1
        if _count(section, rows, work) <= options.max_tokens:
            fragments.append(rows)
        else:
            fragments.extend(
                _cell_fragments(
                    source,
                    table,
                    table.cells[first_cell:position],
                    section,
                    options,
                    work,
                    diagnostics,
                )
            )
    return fragments
