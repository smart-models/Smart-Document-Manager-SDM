"""Verify token-bounded RAG splitting and provenance-preserving rendering."""

import json
from pathlib import Path

import pytest

from ocrserver.rag_refinement import materialize
from ocrserver.rag_split import (
    SplitBudget,
    is_repeat_framing,
    render_fragment,
    split_group,
    split_prose,
    sum_counts,
)
from ocrserver.schemas import RagRefinementOptions, StorageRagRefineRequest


def test_paragraph_boundary_is_preferred_over_words(char_counter):
    text = "aaaa bbbb cccc.\n\ndddd eeee ffff.\n\ngggg hhhh."
    result = split_prose(text, SplitBudget(min_tokens=10, max_tokens=20), char_counter)
    assert [text[start:end] for start, end in result.pieces] == [
        "aaaa bbbb cccc.\n\n",
        "dddd eeee ffff.\n\n",
        "gggg hhhh.",
    ]
    assert result.notes == [] and result.exhausted is False


def test_a_formula_is_never_cut_inside(char_counter):
    text = "Testo prima.\n\n$$\\int_0^1 x\\,dx = \\frac{1}{2}$$\n\nTesto dopo."
    result = split_prose(text, SplitBudget(min_tokens=5, max_tokens=16), char_counter)
    for start, end in result.pieces:
        cut = text[start:end]
        assert cut.count("$$") in (0, 2)


def test_budget_exhaustion_returns_what_it_has(char_counter):
    result = split_prose(
        "a b c d e f g h",
        SplitBudget(min_tokens=1, max_tokens=4, max_evaluations=3),
        char_counter,
    )
    assert result.exhausted is True
    assert "search_budget_exhausted" in result.notes
    assert (
        result.pieces
        and result.pieces[0][0] == 0
        and result.pieces[-1][1] == len("a b c d e f g h")
    )


def test_tail_below_minimum_is_rebalanced_when_the_previous_piece_can_yield(
    char_counter,
):
    text = "aaaa.\n\nbbbb.\n\ncc."
    result = split_prose(text, SplitBudget(min_tokens=5, max_tokens=8), char_counter)
    assert all(end - start >= 5 for start, end in result.pieces)
    assert "below_minimum_fragment" not in result.notes


def test_unavoidable_below_minimum_is_noted(char_counter):
    text = "aaaa.\n\nbb."
    result = split_prose(text, SplitBudget(min_tokens=6, max_tokens=8), char_counter)
    assert "below_minimum_fragment" in result.notes


def test_sum_counts_is_unknown_when_any_member_is_unknown():
    assert sum_counts([10, 20]) == 30
    assert sum_counts([10, None]) is None
    assert sum_counts([]) is None


def test_minimum_is_measured_with_the_counter_not_character_length():
    class HalfCounter:
        """Two characters per token: a measure distinct from character length."""

        def count(self, text: str) -> int:
            return len(text) // 2

    counter = HalfCounter()
    text = "aaaa.\n\nbbbb.\n\ncc."
    result = split_prose(text, SplitBudget(min_tokens=5, max_tokens=5), counter)
    counts = [counter.count(text[start:end]) for start, end in result.pieces]
    assert all(count <= 5 for count in counts)
    assert min(counts) < 5
    assert "below_minimum_fragment" in result.notes


TABLE = (
    "Testo introduttivo.\n<table>\n<tr><th>Nome</th><th>Valore</th></tr>\n"
    "<tr><td>alfa</td><td>1</td></tr>\n<tr><td>beta</td><td>2</td></tr>\n"
    "<tr><td>gamma</td><td>3</td></tr>\n</table>\n"
)
CODE = "```python\nprint(1)\nprint(2)\nprint(3)\nprint(4)\n```\n"
PROSE_THEN_CODE = "Esempio di codice:\n\n" + CODE
OPTIONS = RagRefinementOptions(
    min_tokens=10,
    max_tokens=120,
    keep_tables_together=True,
    keep_code_blocks_together=True,
)


def test_table_splits_at_row_boundaries_and_repeats_the_header(char_counter):
    fragments = split_group(
        [{"id": 1, "text": TABLE, "token_count": 200}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=120),
    )
    assert len(fragments) >= 2
    assert fragments[0].notes == ["table_split"]
    for fragment in fragments:
        text = render_fragment(TABLE, fragment.entries)
        assert "<th>Nome</th>" in text  # intestazione ripetuta
        assert text.count("<table>") == 1
    assert all(fragment.token_count <= 120 for fragment in fragments)


@pytest.mark.parametrize("keep_tables", [True, False])
@pytest.mark.parametrize(
    "opening,header,rows,closing,maximum",
    [
        (
            "<table>\n",
            "<tr><td>Name</td><td>Value</td></tr>\n",
            [f"<tr><td>item{i}</td><td>{'x' * 20}</td></tr>\n" for i in range(6)],
            "</table>\n",
            135,
        ),
        (
            "<table>\n",
            "<tr><th>Name</th><th>Value</th></tr>\n",
            [f"<tr><th>item{i}</th><td>{'x' * 20}</td></tr>\n" for i in range(6)],
            "</table>\n",
            135,
        ),
        (
            "<table>\n<thead>\n",
            "<tr><td>Group</td><td>Details</td></tr>\n"
            "<tr><td>Name</td><td>Value</td></tr>\n</thead>\n<tbody>\n",
            [f"<tr><td>item{i}</td><td>{'x' * 20}</td></tr>\n" for i in range(6)],
            "</tbody>\n</table>\n",
            210,
        ),
        (
            "<table>\n",
            "<tr><th>Group</th><th>Details</th></tr>\n"
            "<tr><th>Name</th><th>Value</th></tr>\n",
            [f"<tr><td>item{i}</td><td>{'x' * 20}</td></tr>\n" for i in range(6)],
            "</table>\n",
            180,
        ),
        (
            "",
            "| Name | Value |\n| --- | --- |\n",
            [f"| item{i} | {'x' * 12} |\n" for i in range(6)],
            "",
            65,
        ),
    ],
)
def test_complete_headers_repeat_in_preview_and_saved_rag(
    char_counter,
    keep_tables,
    opening,
    header,
    rows,
    closing,
    maximum,
):
    from ocrserver.chunking.tables import parse_table

    text = opening + header + "".join(rows) + closing
    options = RagRefinementOptions(
        min_tokens=1,
        max_tokens=maximum,
        keep_tables_together=keep_tables,
    )
    chunks = [
        {
            "id": 1,
            "text": text,
            "token_count": len(text),
            "page": 1,
            "document": "default",
        }
    ]
    fragments = split_group(
        chunks,
        [0],
        options,
        char_counter,
        SplitBudget(min_tokens=1, max_tokens=maximum),
    )
    previews = [render_fragment(text, fragment.entries) for fragment in fragments]
    assert len(previews) > 1
    for preview, fragment in zip(previews, fragments):
        assert preview.count(header) == 1
        assert fragment.token_count == len(preview) <= maximum
        parse_table(preview, max_logical_cells=100_000)
        for entry in fragment.entries:
            if len(entry) == 4:
                assert is_repeat_framing(text, entry[1], entry[2])
    for row in rows:
        assert sum(preview.count(row) for preview in previews) == 1
    request = StorageRagRefineRequest(
        source_key="archive/json_rag.json",
        source_sha256="a" * 64,
        execution_id="a0000000-0000-4000-a000-000000000001",
        created_at="2026-10-03T12:00:00Z",
        options=options,
        groups=[list(fragment.entries) for fragment in fragments],
    )
    saved = materialize(
        {"metadata": {"document": "default"}, "chunks": chunks},
        request,
        counter=char_counter,
    )
    assert [chunk["text"] for chunk in saved["chunks"]] == previews


def test_code_splits_only_at_line_boundaries_and_reopens_the_fence(char_counter):
    fragments = split_group(
        [{"id": 1, "text": CODE, "token_count": 90}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=40),
    )
    for fragment in fragments:
        text = render_fragment(CODE, fragment.entries)
        # the last fragment also owns the trailing newline after the fence
        assert text.startswith("```python") and text.rstrip("\r\n").endswith("```")
    assert fragments[0].notes == ["code_split"]


def test_recut_table_fragment_keeps_header_and_exact_source_coverage(char_counter):
    header = "<tr><th>Name</th><th>Value</th></tr>\n"
    body = "".join(f"<tr><td>item{i}</td><td>{'x' * 15}</td></tr>\n" for i in range(8))
    text = "<table>\n" + header + body + "</table>\n"
    chunks = [
        {
            "id": 1,
            "text": text,
            "token_count": len(text),
            "page": 1,
            "document": "default",
        }
    ]
    options = RagRefinementOptions(min_tokens=1, max_tokens=240)
    fragments = split_group(
        chunks, [0], options, char_counter, SplitBudget(min_tokens=1, max_tokens=240)
    )
    assert len(fragments) > 1
    original = fragments[1]
    smaller = options.model_copy(update={"max_tokens": 135})
    recut = split_group(
        chunks,
        original.entries,
        smaller,
        char_counter,
        SplitBudget(min_tokens=1, max_tokens=135),
    )
    assert len(recut) > 1
    for fragment in recut:
        assert render_fragment(text, fragment.entries).count(header) == 1
        assert fragment.token_count <= 135
    combined = [fragments[0], *recut, *fragments[2:]]
    request = StorageRagRefineRequest(
        source_key="archive/json_rag.json",
        source_sha256="a" * 64,
        execution_id="a0000000-0000-4000-a000-000000000001",
        created_at="2026-10-03T12:00:00Z",
        options=smaller,
        groups=[list(fragment.entries) for fragment in combined],
    )
    saved = materialize(
        {"metadata": {"document": "default"}, "chunks": chunks},
        request,
        counter=char_counter,
    )
    assert all(header in chunk["text"] for chunk in saved["chunks"])


def test_prose_before_a_fence_keeps_its_own_fragment(char_counter):
    fragments = split_group(
        [{"id": 1, "text": PROSE_THEN_CODE, "token_count": 120}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=45),
    )
    first = render_fragment(PROSE_THEN_CODE, fragments[0].entries)
    assert first.startswith("Esempio di codice:")
    assert not first.startswith("```")
    assert all(
        render_fragment(PROSE_THEN_CODE, fragment.entries).strip()
        for fragment in fragments
    )


def test_header_alone_over_the_maximum_becomes_its_own_fragment(char_counter):
    wide = "<table>\n<tr>" + "".join(f"<th>col{i}</th>" for i in range(40)) + "</tr>\n"
    text = wide + "<tr><td>x</td></tr>\n</table>\n"
    fragments = split_group(
        [{"id": 1, "text": text, "token_count": 300}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=120),
    )
    assert "<th>col0</th>" in render_fragment(text, fragments[0].entries)
    assert fragments[0].status in ("above", "within")


def test_a_single_table_row_over_the_maximum_stays_whole(char_counter):
    row = "<tr><td>" + "x" * 200 + "</td></tr>\n"
    text = "<table>\n<tr><th>h</th></tr>\n" + row + "</table>\n"
    fragments = split_group(
        [{"id": 1, "text": text, "token_count": 400}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=60),
    )
    assert any(fragment.status == "above" for fragment in fragments)
    assert all(
        render_fragment(text, fragment.entries).count("<tr>") >= 1
        for fragment in fragments
    )


def test_single_code_line_over_the_maximum_is_split_at_graphemes(char_counter):
    text = "```python\n" + "x = 1; " * 60 + "\n```\n"
    fragments = split_group(
        [{"id": 1, "text": text, "token_count": 500}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=80),
    )
    assert "grapheme_split" in fragments[0].notes
    assert all(
        fragment.token_count is None or fragment.token_count <= 80
        for fragment in fragments
    )


def test_a_math_region_over_the_maximum_stays_intact_with_a_note(char_counter):
    text = "$$\\sum_{i=0}^{n} \\frac{x_i^2}{\\alpha_i + \\beta_i}$$\n"
    fragments = split_group(
        [{"id": 1, "text": text, "token_count": 400}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=20),
    )
    assert fragments[0].entries == [(0, 0, len(text))]
    assert fragments[0].status == "above" and fragments[0].notes == ["math_atomic"]


def test_repeat_framing_accepts_a_header_row_and_a_fence_line_only():
    assert (
        is_repeat_framing(TABLE, TABLE.index("<tr><th>"), TABLE.index("</tr>") + 5)
        is True
    )
    assert (
        is_repeat_framing(CODE, CODE.index("```python"), CODE.index("```python") + 9)
        is True
    )
    # R11: the opening and closing table tags are framing spans too
    open_start = TABLE.index("<table>")
    assert is_repeat_framing(TABLE, open_start, open_start + len("<table>")) is True
    close_start = TABLE.index("</table>")
    assert is_repeat_framing(TABLE, close_start, close_start + len("</table>")) is True
    # the span must be exactly a table tag in the source
    assert is_repeat_framing("x <table> y", 2, 9) is True
    assert is_repeat_framing("x <table> y", 2, 11) is False
    assert is_repeat_framing("prosa normale", 0, 5) is False


def test_cell_level_table_fragments_use_lexical_levels_only(char_counter):
    text = "Cell content. " * 20
    chunks = [
        {
            "id": 1,
            "text": text,
            "token_count": 100,
            "diagnostics": [{"code": "table_representation"}],
        }
    ]
    fragments = split_group(
        chunks, [0], OPTIONS, char_counter, SplitBudget(min_tokens=10, max_tokens=60)
    )
    assert all(
        fragment.notes in ([], ["below_minimum_fragment"]) for fragment in fragments
    )
    assert all(
        "<table>" not in render_fragment(text, fragment.entries)
        for fragment in fragments
    )


FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "tests/fixtures/rag-split.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["name"])
def test_rag_split_fixtures(fixture, char_counter):
    pages = fixture.get("pages", [1] * len(fixture["texts"]))
    chunks = [
        {"id": index + 1, "text": text, "token_count": 1_000, "page": pages[index]}
        for index, text in enumerate(fixture["texts"])
    ]
    options = RagRefinementOptions(**fixture["options"])
    fragments = split_group(
        chunks,
        list(range(len(chunks))),
        options,
        char_counter,
        SplitBudget(min_tokens=options.min_tokens, max_tokens=options.max_tokens),
    )
    expect = fixture["expect"]
    if "fragments" in expect:
        assert len(fragments) == expect["fragments"]
    if "notes" in expect:
        seen = {code for fragment in fragments for code in fragment.notes}
        assert set(expect["notes"]) <= seen
    if "status" in expect:
        assert any(fragment.status == expect["status"] for fragment in fragments)
    if "max_tokens" in expect:
        assert all(
            fragment.token_count is None or fragment.token_count <= expect["max_tokens"]
            for fragment in fragments
        )
    if expect.get("header_repeated"):
        for fragment in fragments:
            assert "<th>" in render_fragment(chunks[0]["text"], fragment.entries)
    if expect.get("fence_reopened"):
        for fragment in fragments:
            text = render_fragment(chunks[0]["text"], fragment.entries)
            assert text.startswith("```") and text.rstrip("\r\n").endswith("```")
    if expect.get("first_fragment") == "header-only":
        first = render_fragment(chunks[0]["text"], fragments[0].entries)
        assert "<th>" in first and "<td>" not in first
    if "glue" in expect:
        assert expect["glue"] in ("next", "previous")


def test_non_repeat_entries_partition_the_whole_source(char_counter):
    for text, budget in (
        (TABLE, SplitBudget(min_tokens=10, max_tokens=120)),
        (CODE, SplitBudget(min_tokens=10, max_tokens=40)),
    ):
        fragments = split_group(
            [{"id": 1, "text": text, "token_count": 200}],
            [0],
            OPTIONS,
            char_counter,
            budget,
        )
        covered: list[tuple[int, int]] = []
        re_emitted: list[tuple[int, int]] = []
        for fragment in fragments:
            for entry in fragment.entries:
                if isinstance(entry, int):
                    continue
                if len(entry) == 4:
                    assert entry[3] == "repeat"
                    re_emitted.append((entry[1], entry[2]))
                else:
                    covered.append((entry[1], entry[2]))
        # R12: non-repeat entries partition the whole source exactly once
        assert covered[0][0] == 0 and covered[-1][1] == len(text), covered
        cursor = 0
        for start, end in covered:
            assert start == cursor, (start, cursor)
            cursor = end
        assert cursor == len(text)
        # every re-emitted frame slice is a repeat entry
        assert re_emitted
        for start, end in re_emitted:
            assert is_repeat_framing(text, start, end), (start, end, text[start:end])
    # the table test's own render contract still holds
    table_fragments = split_group(
        [{"id": 1, "text": TABLE, "token_count": 200}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=120),
    )
    for fragment in table_fragments:
        assert render_fragment(TABLE, fragment.entries).count("<table>") == 1


def test_a_recut_group_of_two_partial_entries_tiles_and_saves(char_counter):
    # O1-2: a group can hold two partial entries of the same chunk (a fragment
    # row glued to its neighbour and re-cut). The trailing-newline absorption
    # must stop at the entry's own end, or a fragment overlaps the next entry
    # and materialize() rejects the plan the server itself proposed.
    text = "<table>\n<tr><th>h</th></tr>\n<tr><td>a</td></tr>\n</table>\n\n\ntail text"
    close = text.index("</table>") + len("</table>")
    cut = close + 1
    group = [(0, 0, cut), (0, cut, len(text))]
    options = RagRefinementOptions(min_tokens=1, max_tokens=60)
    fragments = split_group(
        [{"id": 1, "text": text, "token_count": 1}],
        group,
        options,
        char_counter,
        SplitBudget(min_tokens=1, max_tokens=60),
    )
    covered = [
        entry[1:3]
        for fragment in fragments
        for entry in fragment.entries
        if not isinstance(entry, int) and len(entry) == 3
    ]
    assert covered and covered[0][0] == 0 and covered[-1][1] == len(text), covered
    cursor = 0
    for start, end in covered:
        assert start == cursor, (start, cursor)
        cursor = end
    assert cursor == len(text)
    request = StorageRagRefineRequest(
        source_key="archive/json_rag.json",
        source_sha256="a" * 64,
        execution_id="a0000000-0000-4000-a000-000000000001",
        created_at="2026-09-28T12:00:00Z",
        options=options,
        groups=[list(fragment.entries) for fragment in fragments],
    )
    saved = materialize(
        {
            "metadata": {"document": "default"},
            "chunks": [
                {
                    "id": 1,
                    "text": text,
                    "token_count": 1,
                    "page": 1,
                    "document": "default",
                },
            ],
        },
        request,
        counter=char_counter,
    )
    assert "".join(chunk["text"] for chunk in saved["chunks"]) == text


def test_a_rowspan_group_over_the_maximum_keeps_the_table_whole(char_counter):
    header = "<table>\n<tr><th>h</th></tr>\n"
    spanned = (
        '<tr><td rowspan="2">' + "x" * 200 + "</td><td>a</td></tr>\n"
        "<tr><td>b</td></tr>\n"
    )
    text = header + spanned + "</table>\n"
    fragments = split_group(
        [{"id": 1, "text": text, "token_count": 400}],
        [0],
        OPTIONS,
        char_counter,
        SplitBudget(min_tokens=10, max_tokens=60),
    )
    assert len(fragments) == 1
    assert "above_maximum_fragment" in fragments[0].notes
    assert "table_split" not in fragments[0].notes
    for fragment in fragments:
        rendered = render_fragment(text, fragment.entries)
        assert "<table>" not in rendered or rendered.count("<table>") == rendered.count(
            "</table>"
        )
        if "<table>" in rendered:
            assert rendered.rstrip().endswith("</table>")
    assert '<td rowspan="2">' in render_fragment(text, fragments[0].entries)
