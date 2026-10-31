"""Public table rendering contracts used by the RAG engine."""

import pytest

from ocrserver.chunking.tables import parse_table, render_cell, render_rows
from ocrserver.chunking.errors import ChunkError


def test_merged_cells_keep_logical_coordinates_and_source_text():
    source = (
        '<table><tr><td rowspan="2">A</td><td>B</td></tr><tr><td>C</td></tr></table>'
    )
    table = parse_table(source, max_logical_cells=100)
    rendered = render_rows(table, 0, 2, header_policy="declared")

    assert table.row_groups == ((0, 2),)
    assert table.row_count == 2
    assert table.format == "html"
    assert [(cell.row, cell.column) for cell in table.cells] == [
        (0, 0),
        (0, 1),
        (1, 1),
    ]
    assert rendered.text == source
    primary = [span for span in rendered.spans if span.role == "table_cell"]
    assert [source[span.start : span.end] for span in primary] == [
        "A",
        "B",
        "C",
    ]
    assert [rendered.text[span.text_start : span.text_end] for span in primary] == [
        "A",
        "B",
        "C",
    ]


def test_declared_headers_repeat_observed_text_with_entity_and_newline_offsets():
    source = (
        "<table><thead><tr><td>Name &amp; type</td></tr></thead>"
        "<tbody><tr><td>A\r\nB</td></tr></tbody></table>"
    )
    table = parse_table(source, max_logical_cells=100)
    rendered = render_rows(table, 1, 2, header_policy="declared")

    assert rendered.text == "Name & type\n\n<table><tr><td>A\nB</td></tr></table>"
    headers = [span for span in rendered.spans if span.role == "table_header"]
    assert (
        "".join(rendered.text[s.text_start : s.text_end] for s in headers)
        == "Name & type"
    )
    amp = next(span for span in headers if source[span.start : span.end] == "&amp;")
    assert rendered.text[amp.text_start : amp.text_end] == "&"
    newline = next(
        span for span in rendered.spans if source[span.start : span.end] == "\r\n"
    )
    assert rendered.text[newline.text_start : newline.text_end] == "\n"
    assert table.cells[1].value == "A\nB"


def test_cell_fragments_repeat_first_row_and_map_a_decoded_value_slice():
    source = "<table><tr><td>Label</td></tr><tr><td>AB😀&amp;CD</td></tr></table>"
    table = parse_table(source, max_logical_cells=100)
    rendered = render_cell(table, table.cells[1], (2, 4), header_policy="first_row")

    assert rendered.text == "r=1; c=0; rowspan=1; colspan=1\nLabel\n\n😀&"
    body = [span for span in rendered.spans if span.role == "table_cell"]
    assert [source[span.start : span.end] for span in body] == ["😀", "&amp;"]
    assert [rendered.text[span.text_start : span.text_end] for span in body] == [
        "😀",
        "&",
    ]
    assert dict(body[0].table_position) == {
        "row": 1,
        "column": 0,
        "rowspan": 1,
        "colspan": 1,
        "representation": "cells",
        "header_method": "first_row",
    }
    assert rendered.spans[0].role == "table_coordinates"
    assert rendered.spans[0].start is None


def test_markdown_keeps_code_fences_escaped_pipes_and_header_alignment():
    source = "| Name | Snippet |\r\n| :--- | ---: |\r\n| A\\|B | `x|y` |\r\n| C | z |"
    table = parse_table(source, max_logical_cells=100)
    rendered = render_rows(table, 0, 3, header_policy="declared")

    assert table.format == "markdown"
    assert table.row_count == 3
    assert [cell.value for cell in table.cells] == [
        "Name",
        "Snippet",
        "A\\|B",
        "`x|y`",
        "C",
        "z",
    ]
    assert rendered.text == source.replace("\r\n", "\n")
    fragment = render_cell(table, table.cells[3], None, header_policy="declared")
    assert fragment.text == "r=1; c=1; rowspan=1; colspan=1\nSnippet\n\n`x|y`"
    body = next(span for span in fragment.spans if span.role == "table_cell")
    assert source[body.start : body.end] == "`x|y`"


@pytest.mark.parametrize(
    "source",
    [
        "<table><tbody><tr><td>A</td></tr></tfoot></table>",
        "<table><tr><td><![custom]></td></tr></table>",
        "<table><tr><td><?processing value?></td></tr></table>",
        '<table><tr><td rowspan="2">A</td></tr></table>',
        "<table><tr><td><table><tr><td>A</td></tr></table></td></tr></table>",
    ],
)
def test_ambiguous_tables_are_rejected_with_a_stable_domain_error(source):
    with pytest.raises(ChunkError) as error:
        parse_table(source, max_logical_cells=100)
    assert error.value.code == "unsplittable_table"
    assert error.value.status_code == 422
    assert error.value.details["reason"]


def test_an_empty_cell_is_preserved_but_a_nonempty_cell_cannot_emit_an_empty_slice():
    source = "<table><tr><td></td><td>value</td></tr></table>"
    table = parse_table(source, max_logical_cells=2)
    rendered = render_cell(table, table.cells[0], None, header_policy="none")
    empty = next(span for span in rendered.spans if span.role == "table_cell")
    assert empty.start == empty.end == 15
    assert empty.text_start == empty.text_end == len(rendered.text)
    with pytest.raises(ValueError, match="empty"):
        render_cell(table, table.cells[1], (2, 2), header_policy="none")


def test_unambiguous_optional_html_end_tags_are_canonicalized():
    source = "<table><thead><tr><th>H<tbody><tr><td>A<td>B</table>"
    table = parse_table(source, max_logical_cells=3)
    rendered = render_rows(table, 0, 2, header_policy="none")
    assert (
        rendered.text
        == "<table><tr><th>H</th></tr><tr><td>A</td><td>B</td></tr></table>"
    )


def test_html_line_break_remains_html_and_becomes_a_mapped_cell_newline():
    source = "<table><tr><td>A<br/>B</td></tr></table>"
    table = parse_table(source, max_logical_cells=1)
    assert table.cells[0].value == "A\nB"
    assert render_rows(table, 0, 1, header_policy="none").text == source.replace(
        "<br/>", "<br>"
    )
    fragment = render_cell(table, table.cells[0], (1, 2), header_policy="none")
    body = next(span for span in fragment.spans if span.role == "table_cell")
    assert source[body.start : body.end] == "<br/>"
    assert fragment.text[body.text_start : body.text_end] == "\n"


def test_markdown_does_not_treat_unicode_line_separator_as_a_source_newline():
    source = "| H |\n| --- |\n| a\u2028b |"
    table = parse_table(source, max_logical_cells=2)
    assert table.cells[1].value == "a\u2028b"
    assert render_rows(table, 0, 2, header_policy="none").text == source


@pytest.mark.parametrize(
    "policy, prefix, method",
    [
        ("declared", "H\n\n", "declared"),
        ("first_row", "H\n\n", "declared"),
        ("none", "", "none"),
    ],
)
def test_explicit_headers_take_precedence_and_none_disables_only_repetition(
    policy, prefix, method
):
    source = "<table><tr><th>H</th></tr><tr><td>X</td></tr></table>"
    table = parse_table(source, max_logical_cells=2)
    assert render_rows(table, 0, 2, header_policy=policy).text == source
    fragment = render_rows(table, 1, 2, header_policy=policy)
    assert fragment.text == prefix + "<table><tr><td>X</td></tr></table>"
    assert all(
        span.table_position["header_method"] == method for span in fragment.spans
    )


def test_source_coverage_censuses_markup_and_keeps_rendered_maps_immutable():
    source = " \n<table><tr><td>A &amp; B</td><td></td></tr></table>\n"
    table = parse_table(source, max_logical_cells=2)
    rendered = render_rows(table, 0, 1, header_policy="none")
    represented = set()
    for span in rendered.spans:
        if span.start is not None:
            represented.update(range(span.start, span.end))
    for start, end in rendered.ignored_source_ranges:
        represented.update(range(start, end))
    assert represented == set(range(len(source)))
    with pytest.raises(AttributeError):
        rendered.text = "changed"
    with pytest.raises(TypeError):
        rendered.spans[0].table_position["row"] = 5


def test_expanded_spans_obey_the_limit_before_large_grid_allocation():
    source = '<table><tr><td rowspan="2" colspan="2">X</td></tr><tr></tr></table>'
    assert parse_table(source, max_logical_cells=4).logical_cells == 4
    with pytest.raises(ChunkError) as error:
        parse_table(source, max_logical_cells=3)
    assert error.value.status_code == 413
    assert error.value.details["required"] == 4
    enormous = '<table><tr><td rowspan="1000000000">X</td></tr></table>'
    with pytest.raises(ChunkError) as error:
        parse_table(enormous, max_logical_cells=100000)
    assert error.value.code == "chunk_work_limit"


def test_rendering_cannot_cut_through_a_rowspan_group():
    table = parse_table(
        '<table><tr><td rowspan="2">A</td></tr><tr><td>B</td></tr></table>',
        max_logical_cells=3,
    )
    with pytest.raises(ChunkError) as error:
        render_rows(table, 0, 1, header_policy="none")
    assert error.value.details["reason"] == "row_range_splits_rowspan"
