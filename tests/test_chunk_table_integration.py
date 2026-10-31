"""Table options observed through the complete chunking entry point."""

import copy

import pytest

from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.errors import ChunkError
from ocrserver.chunking.types import ChunkLimits
from ocrserver.schemas import ChunkOptions


class CharacterTokenizer:
    """Non-model test double: the count is exactly the exported text length."""

    identity = {
        "repo": "PaddlePaddle/PaddleOCR-VL-1.6",
        "revision": "c5630abae1d940eafe0697512a0325494b02ab42",
        "sha256": "c8a215a59183d0d0781adc33bacd3ce6162716f7fd568fb30234a74d69803a7d",
        "library_version": "0.23.2",
    }

    def count(self, text):
        return len(text)

    def offsets(self, text):
        return [(index, index + 1) for index in range(len(text))]

    def assert_compatible(self, label):
        assert label is None


def document(*blocks):
    return {
        "pages": [
            {
                "page": 1,
                "blocks": [
                    {"type": tag, "content": content} for tag, content in blocks
                ],
            }
        ]
    }


def run(source, **options):
    return chunk_document(source, ChunkOptions(**options), CharacterTokenizer())


def test_protected_table_conflict_is_atomic_and_permissive_overflow_is_isolated():
    table = "<table><tr><td>" + "one two three four " * 5 + "</td></tr></table>"
    source = document(("text", "before"), ("table", table), ("text", "after"))
    original = copy.deepcopy(source)
    with pytest.raises(ChunkError) as error:
        run(source, max_tokens=60)
    assert error.value.code == "protected_block_exceeds_budget"
    chunks = run(source, max_tokens=60, strictly=False)["chunks"]
    assert [chunk["text"] for chunk in chunks] == ["before", table, "after"]
    assert chunks[1]["overflow_reason"] == "protected_table"
    assert chunks[1]["overflow_tokens"] == len(table) - 60
    assert len(chunks[1]["source_spans"]) == 1
    assert chunks[1]["source_spans"][0]["role"] == "table_cell"
    assert chunks[1]["source_spans"][0]["table"]["row"] == 0
    assert source == original


def test_divided_rows_keep_titles_first_row_headers_and_primary_cells_once():
    table = (
        "<table><tr><td>Column</td></tr>"
        + "".join("<tr><td>" + letter * 25 + "</td></tr>" for letter in "ABC")
        + "</table>"
    )
    chunks = run(
        document(("paragraph_title", "Section"), ("table", table)),
        max_tokens=110,
        preserve_tables=False,
        table_header_policy="first_row",
    )["chunks"]
    assert len(chunks) > 1
    assert all(chunk["text"].startswith("Section\n\n") for chunk in chunks)
    assert all(chunk["token_count"] == len(chunk["text"]) <= 110 for chunk in chunks)
    primary = [
        span
        for chunk in chunks
        for span in chunk["source_spans"]
        if span["type"] == "table"
    ]
    assert [table[span["start"] : span["end"]] for span in primary] == [
        "Column",
        "A" * 25,
        "B" * 25,
        "C" * 25,
    ]
    headers = [
        span
        for chunk in chunks
        for span in chunk["context_spans"]
        if span["role"] == "table_header"
    ]
    assert len(headers) == 3
    assert all(table[span["start"] : span["end"]] == "Column" for span in headers)
    assert all(span["table"]["header_method"] == "first_row" for span in headers)


def test_oversized_rowspan_group_uses_cells_then_splits_values_without_losing_data():
    value = "alpha beta gamma delta epsilon. " * 3
    table = (
        '<table><tr><td rowspan="2">Left</td><td>'
        + value
        + "</td></tr><tr><td>Last</td></tr></table>"
    )
    chunks = run(
        document(("table", table)),
        max_tokens=90,
        preserve_tables=False,
        table_header_policy="none",
    )["chunks"]
    assert len(chunks) >= 3
    assert all(chunk["token_count"] == len(chunk["text"]) <= 90 for chunk in chunks)
    primary = [span for chunk in chunks for span in chunk["source_spans"]]
    assert (
        "".join(table[span["start"] : span["end"]] for span in primary)
        == "Left" + value + "Last"
    )
    assert all(span["table"]["representation"] == "cells" for span in primary)
    assert primary[0]["table"]["rowspan"] == 2
    assert all(
        any(item["code"] == "table_representation" for item in chunk["diagnostics"])
        for chunk in chunks
    )


def test_required_repeated_header_fails_explicitly_instead_of_disappearing():
    table = "<table><tr><th>" + "H" * 90 + "</th></tr><tr><td>data</td></tr></table>"
    source = document(("table", table))
    with pytest.raises(ChunkError) as error:
        run(
            source,
            max_tokens=70,
            preserve_tables=False,
            table_header_policy="declared",
        )
    assert error.value.code == "context_exceeds_budget"
    assert error.value.details["source_key"] == "p1:b0"
    assert error.value.details["conflicting_option"] == "table_header_policy"
    chunks = run(
        source,
        max_tokens=70,
        preserve_tables=False,
        table_header_policy="none",
    )["chunks"]
    assert all(chunk["token_count"] <= 70 for chunk in chunks)


@pytest.mark.parametrize("strictly", [True, False])
def test_malformed_tables_are_kept_only_when_protected(strictly):
    table = "<table><tr><td>Original unclosed value"
    source = document(("table", table))
    chunks = run(source, max_tokens=100, strictly=strictly)["chunks"]
    assert chunks[0]["text"] == table
    assert chunks[0]["source_spans"][0]["start"] == 0
    assert chunks[0]["source_spans"][0]["end"] == len(table)
    assert chunks[0]["diagnostics"][0]["code"] == "table_parse_fallback"
    with pytest.raises(ChunkError) as error:
        run(source, max_tokens=100, strictly=strictly, preserve_tables=False)
    assert error.value.code == "unsplittable_table"


@pytest.mark.parametrize("strictly", [True, False])
def test_explicit_formula_inside_an_oversized_cell_is_never_cut(strictly):
    table = r"<table><tr><td>\(" + "x + " * 30 + r"y\)</td></tr></table>"
    with pytest.raises(ChunkError) as error:
        run(
            document(("table", table)),
            max_tokens=60,
            strictly=strictly,
            preserve_tables=False,
            table_header_policy="none",
        )
    assert error.value.code == "unsplittable_content"


def test_logical_grid_limit_is_cumulative_across_tables():
    table = "<table><tr><td>A</td><td>B</td></tr></table>"
    with pytest.raises(ChunkError) as error:
        chunk_document(
            document(("table", table), ("table", table)),
            ChunkOptions(max_tokens=100),
            CharacterTokenizer(),
            limits=ChunkLimits(max_table_cells=3),
        )
    assert error.value.code == "chunk_work_limit"
    assert error.value.status_code == 413
    assert error.value.details["limit"] == "table_cells"
    assert error.value.details["maximum"] == 3
    assert error.value.details["required"] == 4


def test_missing_declared_headers_are_diagnosed_only_when_the_table_is_divided():
    table = (
        "<table>"
        + "".join("<tr><td>" + char * 25 + "</td></tr>" for char in "ABC")
        + "</table>"
    )
    source = document(("table", table))
    complete = run(source, max_tokens=300, preserve_tables=False)["chunks"]
    assert all(
        item["code"] != "table_header_missing" for item in complete[0]["diagnostics"]
    )
    divided = run(source, max_tokens=80, preserve_tables=False)["chunks"]
    assert all(
        any(item["code"] == "table_header_missing" for item in chunk["diagnostics"])
        for chunk in divided
    )
    assert all(
        not any(span["role"] == "table_header" for span in chunk["context_spans"])
        for chunk in divided
    )
