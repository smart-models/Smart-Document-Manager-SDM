"""Source-preserving structural text primitives for RAG chunking."""

import pytest
from hypothesis import given, strategies as st

from ocrserver.chunking.text import normalize_text


def test_large_newline_normalization_has_bounded_auxiliary_memory():
    import tracemalloc

    content = "a\r\n" * 200_000
    tracemalloc.start()
    normalized = normalize_text(content)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert normalized.text == "a\n" * 200_000
    assert normalized.offsets[400_000] == 600_000
    assert peak < 5_000_000


def test_boundary_limit_rejects_before_materializing_a_large_block():
    import tracemalloc

    from ocrserver.chunking.errors import ChunkError
    from ocrserver.chunking.text import split_boundaries

    content = "x" * 1_000_000
    tracemalloc.start()
    with pytest.raises(ChunkError) as raised:
        split_boundaries(content, max_boundaries=100)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert raised.value.code == "chunk_work_limit"
    assert raised.value.details["limit"] == "structural_boundaries"
    assert peak < 1_000_000


def test_protected_region_limit_rejects_before_collecting_all_delimiters():
    import tracemalloc

    from ocrserver.chunking.errors import ChunkError
    from ocrserver.chunking.text import protected_ranges

    content = "$a$ " * 250_000
    tracemalloc.start()
    try:
        with pytest.raises(ChunkError) as raised:
            protected_ranges(content, max_items=100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert raised.value.code == "chunk_work_limit"
    assert raised.value.status_code == 413
    assert raised.value.details["limit"] == "structural_protected_items"
    assert peak < 1_000_000


def test_overlap_atom_limit_rejects_before_collecting_a_large_suffix():
    import tracemalloc

    from ocrserver.chunking.errors import ChunkError
    from ocrserver.chunking.text import lexical_boundaries

    content = "漢" * 1_000_000
    tracemalloc.start()
    try:
        with pytest.raises(ChunkError) as raised:
            lexical_boundaries(content, max_items=100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert raised.value.details["limit"] == "structural_overlap_atoms"
    assert peak < 1_000_000


def test_punctuation_without_sentence_boundary_scans_without_suffix_rescans():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from ocrserver.chunking.text import split_boundaries; "
            "value = split_boundaries('!' * 10000 + 'a', "
            "max_boundaries=20000); "
            "assert value[-1].offset == 10001; "
            "assert all(item.kind != 'sentence' for item in value)",
        ],
        timeout=10,
        check=True,
        capture_output=True,
    )


def test_normalized_newlines_keep_original_source_boundaries():
    source = "A\r\nB\rC\ne\u0301👩🏽‍💻"
    normalized = normalize_text(source)

    assert normalized.text == "A\nB\nC\ne\u0301👩🏽‍💻"
    assert tuple(normalized.offsets) == (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
    assert source[normalized.offsets[1] : normalized.offsets[2]] == "\r\n"


def test_balanced_fence_exposes_body_and_language_without_nested_math():
    from ocrserver.chunking.text import protected_ranges

    source = "Intro\n```python\n# Heading\nprint('$x$')\n```\nEnd"
    regions = protected_ranges(source)

    assert len(regions) == 1
    region = regions[0]
    assert region.kind == "code"
    assert source[region.start : region.end] == (
        "```python\n# Heading\nprint('$x$')\n```"
    )
    assert source[region.body_start : region.body_end] == ("# Heading\nprint('$x$')\n")
    assert region.language == "python"
    assert region.fence == "```"


@pytest.mark.parametrize(
    "formula, body, delimiter",
    [
        (r"\(a + b\)", "a + b", r"\("),
        (r"\[a + b\]", "a + b", r"\["),
        ("$$a\n+ b$$", "a\n+ b", "$$"),
        ("$x^2$", "x^2", "$"),
        ("$starts\non another line$", "starts\non another line", "$"),
    ],
)
def test_balanced_formulas_are_atomic_regions(formula, body, delimiter):
    from ocrserver.chunking.text import protected_ranges

    source = "Before " + formula + " after."
    regions = protected_ranges(source)

    assert len(regions) == 1
    region = regions[0]
    assert region.kind == "formula"
    assert source[region.start : region.end] == formula
    assert source[region.body_start : region.body_end] == body
    assert region.language is None
    assert region.fence == delimiter


def test_split_candidates_prefer_observed_structure_and_keep_separators():
    from ocrserver.chunking.text import split_boundaries

    source = "Alpha, beta. Gamma\n\n- First item\n- Second item"
    candidates = {candidate.offset: candidate for candidate in split_boundaries(source)}

    assert (candidates[0].cost, candidates[0].kind) == (0, "block")
    assert (candidates[7].cost, candidates[7].kind) == (4, "clause")
    assert (candidates[13].cost, candidates[13].kind) == (1, "sentence")
    assert (candidates[20].cost, candidates[20].kind) == (0, "paragraph")
    assert (candidates[33].cost, candidates[33].kind) == (0, "list")
    assert (candidates[len(source)].cost, candidates[len(source)].kind) == (
        0,
        "block",
    )
    assert (candidates[1].cost, candidates[1].kind) == (64, "grapheme")
    assert (candidates[28].cost, candidates[28].kind) == (16, "word")


def test_overlap_atoms_preserve_whitespace_and_nonseparated_graphemes():
    from ocrserver.chunking.text import lexical_boundaries

    source = "  Alpha, beta!\n中文👩🏽‍💻 e\u0301"
    boundaries = lexical_boundaries(source)
    atoms = [source[start:end] for start, end in zip(boundaries, boundaries[1:])]

    assert atoms == ["  Alpha, ", "beta!\n", "中", "文", "👩🏽‍💻 ", "e\u0301"]
    assert boundaries[0] == 0
    assert boundaries[-1] == len(source)


def test_currency_does_not_capture_the_following_explicit_formula():
    from ocrserver.chunking.text import protected_ranges

    source = r"Costs $10 and $20; then $x + y$ and \$literal."

    assert [source[r.start : r.end] for r in protected_ranges(source)] == [
        "$x + y$",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "    x = 1\n    y = 2",
        "```python\nx = 1",
        "Text ``` is not a block fence.",
        r"Escaped \$x\$ and \\(y\\) remain literal.",
        "$5 and $10",
        "$ unbalanced $ and $$$ambiguous$$$",
    ],
)
def test_plain_or_unbalanced_text_is_not_reclassified_as_code_or_math(source):
    from ocrserver.chunking.text import protected_ranges

    assert protected_ranges(source) == ()


def test_tilde_fence_retains_info_and_accepts_a_longer_matching_close():
    from ocrserver.chunking.text import protected_ranges

    source = "   ~~~ c++ {#sample}\n  x < y\n````\n   ~~~~~  \nAfter"
    (region,) = protected_ranges(source)

    assert region.language == "c++ {#sample}"
    assert region.fence == "~~~"
    assert source[region.body_start : region.body_end] == "  x < y\n````\n"
    assert source[region.end :] == "\nAfter"


def test_abbreviations_and_initials_do_not_create_sentence_cuts():
    from ocrserver.chunking.text import split_boundaries

    source = "Dr. A. Rossi uses Eq. 2, e.g. here. Then finishes."
    sentence_ends = [
        candidate.offset
        for candidate in split_boundaries(source)
        if candidate.kind == "sentence"
    ]

    assert [source[:end] for end in sentence_ends] == [
        "Dr. A. Rossi uses Eq. 2, e.g. here. ",
    ]


def test_formula_and_fence_interiors_have_no_cut_candidates():
    from ocrserver.chunking.text import protected_ranges, split_boundaries

    source = "Before \\(a + b\\).\n```python\n# Heading\n\nrow = 1\n```\nAfter"
    candidates = split_boundaries(source)

    for region in protected_ranges(source):
        assert not any(region.start < cut.offset < region.end for cut in candidates)


def test_grapheme_clusters_and_cjk_punctuation_are_not_corrupted():
    from ocrserver.chunking.text import lexical_boundaries, split_boundaries

    source = "e\u0301👩🏽‍💻🇮🇹中。文！"
    candidates = split_boundaries(source)

    assert [cut.offset for cut in candidates] == [0, 2, 6, 8, 9, 10, 11, 12]
    assert {cut.offset: cut.cost for cut in candidates}[10] == 1
    assert lexical_boundaries(source) == (0, 2, 6, 8, 9, 10, 11, 12)


@given(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        max_size=200,
    )
)
def test_normalization_covers_every_original_character_in_order(source):
    normalized = normalize_text(source)

    assert normalized.text == source.replace("\r\n", "\n").replace("\r", "\n")
    assert len(normalized.offsets) == len(normalized.text) + 1
    assert normalized.offsets[0] == 0
    assert normalized.offsets[-1] == len(source)
    for index, character in enumerate(normalized.text):
        fragment = source[normalized.offsets[index] : normalized.offsets[index + 1]]
        assert fragment == character or (
            character == "\n" and fragment in {"\r", "\r\n"}
        )


@given(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        max_size=200,
    )
)
def test_lexical_partition_preserves_all_characters_and_advances(source):
    from ocrserver.chunking.text import lexical_boundaries

    cuts = lexical_boundaries(source)

    assert cuts[0] == 0
    assert cuts[-1] == len(source)
    assert all(left < right for left, right in zip(cuts, cuts[1:]))
    recovered = "".join(source[left:right] for left, right in zip(cuts, cuts[1:]))
    assert recovered == source


def test_escaped_dollar_inside_math_does_not_hide_the_closing_delimiter():
    from ocrserver.chunking.text import protected_ranges

    source = r"Cost symbol $x\$$ is an explicit formula."
    regions = protected_ranges(source)

    assert [source[r.start : r.end] for r in regions] == [r"$x\$$"]
    assert source[regions[0].body_start : regions[0].body_end] == r"x\$"


def test_ocr_latex_with_spaced_delimiters_is_preserved_as_math():
    from ocrserver.chunking.text import protected_ranges

    source = "Vaswani $ ^{*} $: the value is $ x + y $."

    assert [source[r.start : r.end] for r in protected_ranges(source)] == [
        "$ ^{*} $",
        "$ x + y $",
    ]
