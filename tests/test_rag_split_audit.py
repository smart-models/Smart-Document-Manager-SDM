"""Adversarial checks of the manual RAG splitter's correctness contract."""

import regex
import pytest

from ocrserver.rag_split import (
    SplitBudget,
    is_repeat_framing,
    render_fragment,
    split_group,
    split_prose,
)
from ocrserver.schemas import RagRefinementOptions


def cut(text, counter, minimum=1, maximum=80, entries=None):
    return split_group(
        [{"id": 1, "text": text, "token_count": counter.count(text)}],
        [0] if entries is None else entries,
        RagRefinementOptions(min_tokens=minimum, max_tokens=maximum),
        counter,
        SplitBudget(min_tokens=minimum, max_tokens=maximum),
    )


def assert_coverage(text, fragments):
    owned = [
        entry[1:3]
        for fragment in fragments
        for entry in fragment.entries
        if len(entry) == 3
    ]
    cursor = 0
    for start, end in owned:
        assert start == cursor
        cursor = end
    assert cursor == len(text)


def test_tail_can_borrow_more_than_two_graphemes(char_counter):
    text = "x" * 105
    result = split_prose(text, SplitBudget(min_tokens=40, max_tokens=100), char_counter)
    assert all(40 <= end - start <= 100 for start, end in result.pieces)


def test_inline_formula_does_not_split_text_already_within_budget(char_counter):
    text = "A paragraph before $x^2$ and its continuation afterwards."
    fragments = cut(text, char_counter, maximum=len(text))
    assert len(fragments) == 1
    assert render_fragment(text, fragments[0].entries) == text
    assert fragments[0].token_count == len(text)


def test_inferred_colspan_header_keeps_its_year_subheaders(char_counter):
    header = (
        '<tr><td></td><td colspan="2">GDP</td></tr>'
        "<tr><td></td><td>2024</td><td>2025</td></tr>"
    )
    rows = "".join(
        f"<tr><td>country{i}</td><td>1</td><td>2</td></tr>" for i in range(8)
    )
    text = "<table>" + header + rows + "</table>"
    fragments = cut(text, char_counter, maximum=200)
    assert len(fragments) > 1
    assert_coverage(text, fragments)
    assert all(header in render_fragment(text, f.entries) for f in fragments)


def test_within_fragment_does_not_inherit_below_note(char_counter):
    fragments = cut("x" * 105, char_counter, minimum=60, maximum=100)
    assert any(fragment.status == "below" for fragment in fragments)
    assert all(
        "below_minimum_fragment" not in fragment.notes
        for fragment in fragments
        if fragment.status == "within"
    )


def test_code_uses_tokens_instead_of_character_count():
    class TripleCounter:
        def count(self, text):
            return len(text) * 3

    counter = TripleCounter()
    text = "```\n" + "x" * 50 + "\n```"
    fragments = cut(text, counter, maximum=60)
    assert all(fragment.token_count <= 60 for fragment in fragments)
    assert_coverage(text, fragments)


def test_code_split_never_bisects_unicode_graphemes(char_counter):
    text = "```\n" + "👨‍👩‍👧‍👦e\u0301" * 15 + "\n```"
    fragments = cut(text, char_counter, maximum=30)
    boundaries = {0, *(match.end() for match in regex.finditer(r"\X", text))}
    assert all(
        entry[1] in boundaries and entry[2] in boundaries
        for fragment in fragments
        for entry in fragment.entries
    )
    assert_coverage(text, fragments)


def test_fence_in_a_partial_range_uses_absolute_source_offsets(char_counter):
    prefix = "prefix\n\n"
    text = prefix + "```python\nprint(1)\nprint(2)\nprint(3)\n```\n"
    entries = [(0, len(prefix), len(text))]
    fragments = cut(text, char_counter, maximum=30, entries=entries)
    for fragment in fragments:
        rendered = render_fragment(text, fragment.entries)
        assert rendered.startswith("```python\n")
        assert rendered.rstrip().endswith("```")
        assert "prefix" not in rendered


def test_joined_prose_is_counted_like_its_saved_rendering(char_counter):
    text = "aaa bbb ccc ddd eee fff"
    fragments = cut(text, char_counter, maximum=12)
    assert all(
        fragment.token_count == len(render_fragment(text, fragment.entries))
        for fragment in fragments
    )


def test_repeated_header_cannot_smuggle_a_body_row():
    text = "<table>\n<tr><th>h</th></tr>\n<tr><td>payload</td></tr>\n</table>"
    start = text.index("<tr>")
    end = text.index("</table>")
    assert not is_repeat_framing(text, start, end)


def test_incomplete_html_table_is_preserved_instead_of_cut_as_prose(char_counter):
    text = "<table>\n<tr><th>h</th></tr>\n<tr><td>" + "x" * 200 + "</td></tr>\n"
    fragments = cut(text, char_counter, maximum=80)
    assert len(fragments) == 1
    assert fragments[0].status == "above"
    assert render_fragment(text, fragments[0].entries) == text


def test_table_search_budget_is_shared_and_bounded():
    class CountingCounter:
        calls = 0

        def count(self, text):
            self.calls += 1
            return len(text)

    counter = CountingCounter()
    text = "<table>\n<tr><th>h</th></tr>\n" + "<tr><td>x</td></tr>\n" * 80 + "</table>"
    fragments = split_group(
        [{"id": 1, "text": text}],
        [0],
        RagRefinementOptions(min_tokens=1, max_tokens=80),
        counter,
        SplitBudget(min_tokens=1, max_tokens=80, max_evaluations=5),
    )
    assert counter.calls <= 5 + len(fragments)
    assert any("search_budget_exhausted" in fragment.notes for fragment in fragments)
    assert_coverage(text, fragments)


def test_multisource_split_counts_match_the_refinement_contract(char_counter):
    from ocrserver.rag_refinement import materialize
    from ocrserver.schemas import StorageRagRefineRequest

    chunks = [
        {
            "id": i + 1,
            "text": letter * 8,
            "token_count": 8,
            "page": 1,
            "document": "default",
        }
        for i, letter in enumerate("abc")
    ]
    options = RagRefinementOptions(min_tokens=5, max_tokens=20)
    fragments = split_group(
        chunks,
        [0, 1, 2],
        options,
        char_counter,
        SplitBudget(min_tokens=5, max_tokens=20),
    )
    assert len(fragments) == 2
    assert fragments[0].member_token_counts == [8, 8]
    assert fragments[0].token_count == 16
    request = StorageRagRefineRequest(
        source_key="archive/json_rag.json",
        source_sha256="a" * 64,
        execution_id="a0000000-0000-4000-a000-000000000001",
        created_at="2026-10-03T12:00:00Z",
        options=options,
        groups=[fragment.entries for fragment in fragments],
        counts=[fragment.member_token_counts for fragment in fragments],
    )
    saved = materialize(
        {"metadata": {"document": "default"}, "chunks": chunks},
        request,
        counter=char_counter,
    )
    assert [chunk["token_count"] for chunk in saved["chunks"]] == [16, 8]


@pytest.mark.parametrize("seed", range(30))
def test_mixed_structures_have_exact_coverage_and_saved_counts(char_counter, seed):
    import random
    from ocrserver.rag_refinement import materialize
    from ocrserver.schemas import StorageRagRefineRequest

    rng = random.Random(seed)
    header = "<tr><th>Name</th><th>Value</th></tr>\n"
    rows = "".join(
        f"<tr><td>item{i}</td><td>{'界😀' * rng.randint(1, 8)}</td></tr>\n"
        for i in range(rng.randint(2, 9))
    )
    table = "<table>\n" + header + rows + "</table>\n"
    code = "```python\n" + "value = '😀e\u0301'\n" * rng.randint(1, 12) + "```\n"
    text = "Introduction.\n\n" + table + "\n" + code + "\nA final sentence."
    maximum = rng.randint(90, 220)
    fragments = cut(text, char_counter, minimum=maximum // 3, maximum=maximum)
    assert_coverage(text, fragments)
    chunks = [
        {
            "id": 1,
            "text": text,
            "page": 1,
            "document": "default",
            "token_count": len(text),
        }
    ]
    request = StorageRagRefineRequest(
        source_key="archive/json_rag.json",
        source_sha256="a" * 64,
        execution_id="a0000000-0000-4000-a000-000000000001",
        created_at="2026-10-03T12:00:00Z",
        options=RagRefinementOptions(min_tokens=maximum // 3, max_tokens=maximum),
        groups=[fragment.entries for fragment in fragments],
    )
    saved = materialize(
        {"metadata": {"document": "default"}, "chunks": chunks},
        request,
        counter=char_counter,
    )
    for fragment, chunk in zip(fragments, saved["chunks"]):
        assert fragment.token_count == len(chunk["text"])
        assert chunk["text"] == render_fragment(text, fragment.entries)
