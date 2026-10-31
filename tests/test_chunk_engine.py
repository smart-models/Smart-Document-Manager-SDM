"""Observable chunking contracts, with counting supplied at the model seam."""

import copy
import json
from pathlib import Path

import pytest

from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.errors import ChunkError
from ocrserver.schemas import ChunkOptions


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "intervals,expected",
    [
        ([], ""),
        ([(4, 6), (0, 3)], "abc\n\nef"),
        ([(2, 5), (0, 3), (5, 6), (2, 5)], "abcdef"),
    ],
)
def test_primary_body_unions_ranges_without_repeated_context(intervals, expected):
    from ocrserver.chunking.rendering import primary_body_text
    from ocrserver.chunking.types import Fragment

    spans = tuple(
        {"text_start": start, "text_end": end, "role": "body"}
        for start, end in intervals
    )
    title = {"text_start": 6, "text_end": 7, "role": "section_title"}
    context = {"text_start": 7, "text_end": 8, "role": "overlap"}
    rendered = Fragment("abcdefgh", (*spans, title), (context,))

    assert primary_body_text(rendered) == expected


class WordTokenizer:
    """Small external tokenizer double; punctuation remains part of a word."""

    identity = {
        "repo": "PaddlePaddle/PaddleOCR-VL-1.6",
        "revision": "c5630abae1d940eafe0697512a0325494b02ab42",
        "sha256": "c8a215a59183d0d0781adc33bacd3ce6162716f7fd568fb30234a74d69803a7d",
        "library_version": "0.23.2",
    }

    def count(self, text):
        return len(text.split())

    def offsets(self, text):
        import re

        return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

    def assert_compatible(self, label):
        if label and label != self.identity["repo"] + "@" + self.identity["revision"]:
            raise ChunkError("tokenizer_mismatch", "Different tokenizer")


def document(*blocks):
    return {
        "pages": [
            {
                "page": 1,
                "blocks": [{"type": tag, "content": text} for tag, text in blocks],
            }
        ]
    }


def run(source, max_tokens=512, **options):
    return chunk_document(
        source, ChunkOptions(max_tokens=max_tokens, **options), WordTokenizer()
    )


def test_configured_text_capacity_applies_before_inline_relation_collection():
    from ocrserver.chunking.types import ChunkLimits

    with pytest.raises(ChunkError) as raised:
        chunk_document(
            document(("text", "context $a$ " * 6)),
            ChunkOptions(max_tokens=512),
            WordTokenizer(),
            limits=ChunkLimits(max_split_evaluations=10),
        )
    assert raised.value.status_code == 413
    assert raised.value.details["limit"] == "structural_protected_items"
    assert raised.value.details["maximum"] == 10


def test_formula_label_capacity_is_shared_across_source_blocks():
    from ocrserver.chunking.types import ChunkLimits

    with pytest.raises(ChunkError) as raised:
        chunk_document(
            document(*[("display_formula", r"\tag{1}" * 4)] * 3),
            ChunkOptions(max_tokens=512),
            WordTokenizer(),
            limits=ChunkLimits(max_split_evaluations=10),
        )
    assert raised.value.status_code == 413
    assert raised.value.details["limit"] == "structural_formula_labels"


def test_code_region_scan_uses_configured_text_capacity():
    from ocrserver.chunking.types import ChunkLimits

    with pytest.raises(ChunkError) as raised:
        chunk_document(
            document(("code", "```\nx\n```\n" * 6)),
            ChunkOptions(max_tokens=512),
            WordTokenizer(),
            limits=ChunkLimits(max_split_evaluations=10),
        )
    assert raised.value.details["limit"] == "structural_protected_items"


def test_overlap_suffix_uses_configured_atom_capacity():
    from ocrserver.chunking.overlap import choose_overlap
    from ocrserver.chunking.rendering import source_fragment
    from ocrserver.chunking.types import ChunkLimits, Section, Source, Work

    first = Source(1, 0, {"type": "text", "content": "漢" * 20})
    second = Source(1, 1, {"type": "text", "content": "next"})
    with pytest.raises(ChunkError) as raised:
        choose_overlap(
            Section("s", None, None, []),
            [source_fragment(first)],
            [source_fragment(second)],
            ChunkOptions(max_tokens=512, overlap_tokens=1),
            Work(WordTokenizer(), ChunkLimits(max_split_evaluations=10)),
            {first.key: first, second.key: second},
        )
    assert raised.value.details["limit"] == "structural_overlap_atoms"


def test_complete_contract_example_matches_canonical_file():
    from ocrserver.chunking.canonical import canonical_bytes

    request = json.loads(
        (ROOT / "tests/fixtures/ocr-rag-chunks.input.example.json").read_text()
    )
    source = copy.deepcopy(request["parsed_document"])
    result = chunk_document(
        source,
        ChunkOptions(**request["options"]),
        WordTokenizer(),
        source_id=request["source_id"],
        lang=request["lang"],
    )
    expected = (ROOT / "tests/fixtures/ocr-rag-chunks.example.json").read_bytes()
    assert canonical_bytes(result) == expected
    assert source == request["parsed_document"]


def test_identical_heading_occurrences_are_separate_sections():
    source = document(
        ("paragraph_title", "Titolo"),
        ("text", "uno due"),
        ("paragraph_title", "Titolo"),
        ("text", "tre quattro"),
    )
    chunks = run(source)["chunks"]
    assert [chunk["text"] for chunk in chunks] == [
        "Titolo\n\nuno due",
        "Titolo\n\ntre quattro",
    ]
    assert chunks[0]["section_id"] != chunks[1]["section_id"]
    assert [chunk["token_count"] for chunk in chunks] == [3, 3]


def test_margins_are_metadata_while_footnotes_and_sidebars_remain_text():
    chunks = run(
        document(
            ("header", "Intestazione"),
            ("text", "Corpo"),
            ("footnote", "Nota importante"),
            ("aside_text", "A lato"),
            ("number", "iv"),
        )
    )["chunks"]
    assert chunks[0]["text"] == "Corpo\n\nNota importante\n\nA lato"
    assert chunks[0]["page_context"][0]["page_labels"][0]["text"] == "iv"
    assert chunks[0]["page_context"][0]["headers"][0]["text"] == "Intestazione"


@pytest.mark.parametrize(
    "pages,reason", [([1, 1], "duplicate_page"), ([0], "nonpositive_page")]
)
def test_invalid_page_identity_has_stable_error(pages, reason):
    with pytest.raises(ChunkError) as error:
        run({"pages": [{"page": page, "blocks": []} for page in pages]})
    assert error.value.code == "invalid_structure"
    assert error.value.details["reason"] == reason


def test_split_prose_repeats_title_and_covers_original_characters_once():
    body = "uno due tre. Quattro cinque sei. Sette otto nove."
    chunks = run(document(("paragraph_title", "Titolo"), ("text", body)), max_tokens=5)[
        "chunks"
    ]
    assert len(chunks) > 1
    assert all(chunk["text"].startswith("Titolo\n\n") for chunk in chunks)
    assert all(chunk["token_count"] <= 5 for chunk in chunks)
    intervals = [
        (span["start"], span["end"])
        for chunk in chunks
        for span in chunk["source_spans"]
        if span["type"] == "text"
    ]
    assert "".join(body[start:end] for start, end in intervals) == body
    assert (
        sum(
            span["role"] == "section_title"
            for chunk in chunks
            for span in chunk["source_spans"]
        )
        == 1
    )


def test_protected_code_conflict_is_atomic_and_permissive_is_isolated():
    source = document(
        ("text", "prima"),
        ("algorithm", "uno due tre quattro cinque"),
        ("text", "dopo"),
    )
    with pytest.raises(ChunkError) as failure:
        run(source, max_tokens=3)
    assert failure.value.code == "protected_block_exceeds_budget"
    assert failure.value.details["required_tokens"] == 5
    chunks = run(source, max_tokens=3, strictly=False)["chunks"]
    assert [chunk["text"] for chunk in chunks] == [
        "prima",
        "uno due tre quattro cinque",
        "dopo",
    ]
    assert chunks[1]["overflow_reason"] == "protected_code"
    assert chunks[1]["overflow_tokens"] == 2
    assert sum(chunk["overflow"] for chunk in chunks) == 1


@pytest.mark.parametrize("strictly", [True, False])
def test_formula_atomicity_is_independent_of_strictness(strictly):
    with pytest.raises(ChunkError) as error:
        run(
            document(("display_formula", "a + b = c")),
            max_tokens=3,
            strictly=strictly,
        )
    assert error.value.code == "unsplittable_content"


def test_code_split_preserves_balanced_fences_and_source_language():
    source = document(("text", "```python\none two\nthree four\nfive six\n```"))
    chunks = run(source, max_tokens=4, preserve_code_blocks=False)["chunks"]
    assert len(chunks) == 3
    assert all(
        chunk["text"].startswith("```python\n") and chunk["text"].endswith("```")
        for chunk in chunks
    )
    assert all(chunk["token_count"] <= 4 for chunk in chunks)


def test_nonmonotone_counts_do_not_prune_later_candidate():
    class Nonmonotone(WordTokenizer):
        def count(self, text):
            return {
                "aa": 1,
                "bb": 1,
                "cc": 1,
                "aa\n\nbb": 9,
                "aa\n\nbb\n\ncc": 2,
            }.get(text, 2)

    result = chunk_document(
        document(("text", "aa"), ("text", "bb"), ("text", "cc")),
        ChunkOptions(max_tokens=2),
        Nonmonotone(),
    )
    assert [chunk["text"] for chunk in result["chunks"]] == ["aa\n\nbb\n\ncc"]


def test_overlap_uses_previous_new_prose_only_and_respects_cap():
    source = document(
        ("text", "uno due tre"),
        ("text", "quattro cinque sei"),
        ("text", "sette otto nove"),
    )
    chunks = run(source, max_tokens=5, overlap_tokens=2)["chunks"]
    assert [chunk["text"] for chunk in chunks] == [
        "uno due tre",
        "due tre\n\nquattro cinque sei",
        "cinque sei\n\nsette otto nove",
    ]
    assert [chunk["overlap_token_count"] for chunk in chunks] == [0, 2, 2]
    assert chunks[2]["context_spans"][0]["source_key"] == "p1:b1"


def test_overlap_does_not_cross_section_or_recover_prose_behind_formula():
    source = document(
        ("text", "uno due tre"),
        ("display_formula", "x+y"),
        ("text", "quattro cinque sei dieci"),
        ("paragraph_title", "Titolo"),
        ("text", "sette otto nove"),
    )
    chunks = run(source, max_tokens=5, overlap_tokens=2)["chunks"]
    assert all(chunk["overlap_token_count"] == 0 for chunk in chunks)


def test_explicit_truncation_acceptance_has_source_scope():
    source = document(("text", "testo"))
    source["warnings"] = [
        {
            "code": "pages_truncated",
            "message": "Limited pages",
            "details": {"max_pages": 1, "total_pages": 3},
        }
    ]
    with pytest.raises(ChunkError) as error:
        run(source)
    assert error.value.code == "incomplete_ocr"
    result = chunk_document(
        source,
        ChunkOptions(max_tokens=5),
        WordTokenizer(),
        accept_truncated=True,
    )
    assert result["chunks"][0]["document_context"]["scope"] == {
        "kind": "page_prefix",
        "origin": "accepted_upstream_truncation",
        "selected_pages": [1],
        "requested_max_pages": 1,
        "total_pages": 3,
    }


def test_typed_sections_continue_with_observed_title_as_context_only():
    source = document(
        ("paragraph_title", "Titolo"),
        ("text", "corpo"),
        ("abstract", "riassunto"),
        ("text", "ripresa"),
    )
    chunks = run(source)["chunks"]
    assert [chunk["text"] for chunk in chunks] == [
        "Titolo\n\ncorpo",
        "riassunto",
        "Titolo\n\nripresa",
    ]
    assert len({chunk["section_id"] for chunk in chunks}) == 3
    titles = [
        span
        for chunk in chunks
        for span in chunk["source_spans"]
        if span["role"] == "section_title"
    ]
    assert len(titles) == 1


def test_cross_page_section_keeps_page_context_and_unassigned_pages():
    source = {
        "pages": [
            {"page": 3, "blocks": [{"type": "text", "content": "seconda"}]},
            {"page": 1, "blocks": [{"type": "number", "content": "i"}]},
            {
                "page": 2,
                "blocks": [
                    {"type": "paragraph_title", "content": "Titolo"},
                    {"type": "text", "content": "prima"},
                ],
            },
        ]
    }
    chunks = run(source)["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["pages"] == [2, 3]
    assert chunks[0]["page"] == 2
    assert chunks[0]["document_context"]["unassigned_sources"][0]["content"] == "i"


@pytest.mark.parametrize(
    "tag",
    "abstract algorithm aside_text chart content display_formula "
    "doc_title figure_title footer footer_image footnote formula_number "
    "header header_image image inline_formula number paragraph_title reference "
    "reference_content seal table text vertical_text vision_footnote code "
    "custom".split(),
)
def test_every_layout_tag_has_a_conserving_path(tag):
    content = "<table><tr><td>valore</td></tr></table>" if tag == "table" else "valore"
    chunks = run(document((tag, content), ("text", "corpo")))["chunks"]
    assert any(tag in chunk["block_types"] for chunk in chunks)
    assert any("valore" in chunk["text"] for chunk in chunks) or any(
        "valore" in str(chunk["page_context"]) for chunk in chunks
    )


def test_relation_between_formula_and_number_survives_a_budget_cut():
    source = {
        "pages": [
            {
                "page": 1,
                "width": 100,
                "height": 100,
                "blocks": [
                    {
                        "type": "display_formula",
                        "content": r"a + b \tag{1}",
                        "bbox": [0, 0, 50, 10],
                    },
                    {
                        "type": "formula_number",
                        "content": "(1)",
                        "bbox": [51, 0, 55, 10],
                    },
                ],
            }
        ]
    }
    chunks = run(source, max_tokens=4)["chunks"]
    assert len(chunks) == 2
    assert chunks[0]["relations"][0]["status"] == "across_chunks"
    assert chunks[1]["relations"][0]["id"] == chunks[0]["relations"][0]["id"]


def test_mixed_reading_order_uses_array_and_only_conflicts_warn():
    source = document(("text", "prima"), ("text", "seconda"), ("text", "terza"))
    blocks = source["pages"][0]["blocks"]
    blocks[0]["order"], blocks[2]["order"] = 1, 2
    result = run(source)["chunks"][0]
    assert result["text"] == "prima\n\nseconda\n\nterza"
    assert result["document_context"]["diagnostics"] == []
    blocks[2]["order"] = 0
    result = run(source)["chunks"][0]
    assert result["text"] == "prima\n\nseconda\n\nterza"
    assert (
        result["document_context"]["diagnostics"][0]["code"] == "reading_order_conflict"
    )


def test_preexisting_asset_reference_is_kept_without_fetching_or_copying_crop():
    import base64
    import hashlib

    source = document(("text", "corpo"), ("image", ""))
    source["pages"][0]["image_names"] = ["opaque/figure.png"]
    source["pages"][0]["images"] = [base64.b64encode(b"image bytes").decode()]
    result = run(source)["chunks"][0]
    assert result["assets"][0]["reference"] == "opaque/figure.png"
    assert result["assets"][0]["sha256"] == hashlib.sha256(b"image bytes").hexdigest()
    assert "aW1hZ2UgYnl0ZXM=" not in json.dumps(result)


def test_output_limit_has_no_partial_result_or_tokenizer_dependent_fallback():
    from ocrserver.chunking.types import ChunkLimits

    with pytest.raises(ChunkError) as error:
        chunk_document(
            document(("text", "uno due tre")),
            ChunkOptions(max_tokens=512),
            WordTokenizer(),
            limits=ChunkLimits(max_output_bytes=100),
        )
    assert error.value.code == "chunk_output_limit"
    assert error.value.status_code == 413


def test_token_evaluation_budget_is_enforced_before_next_encode():
    from ocrserver.chunking.types import ChunkLimits

    with pytest.raises(ChunkError) as error:
        chunk_document(
            document(("text", "uno due tre")),
            ChunkOptions(max_tokens=512),
            WordTokenizer(),
            limits=ChunkLimits(max_token_evaluations=1),
        )
    assert error.value.code == "chunk_work_limit"
    assert error.value.details["limit"] == "token_evaluations"


def test_marginal_text_does_not_end_a_contiguous_typed_section():
    source = document(
        ("abstract", "prima"),
        ("header", "margine"),
        ("abstract", "seconda"),
        ("text", "corpo"),
    )
    chunks = run(source, marginal_policy="text")["chunks"]
    assert [chunk["text"] for chunk in chunks] == [
        "prima\n\nmargine\n\nseconda",
        "corpo",
    ]


def test_long_unseparated_text_progresses_with_bounded_split_work():
    from ocrserver.chunking.types import ChunkLimits

    class CharacterTokenizer(WordTokenizer):
        def count(self, text):
            return len(text)

    body = "汉" * 600
    result = chunk_document(
        document(("vertical_text", body)),
        ChunkOptions(max_tokens=64),
        CharacterTokenizer(),
        limits=ChunkLimits(max_split_evaluations=10_000),
    )
    assert "".join(chunk["text"] for chunk in result["chunks"]) == body
    assert all(chunk["token_count"] <= 64 for chunk in result["chunks"])


@pytest.mark.parametrize(
    "tag,content",
    [("algorithm", "aa"), ("table", "<table><tr><td>a</td></tr></table>")],
)
def test_structural_split_failure_still_allows_a_fitting_whole_combination(
    tag, content
):
    class Nonmonotone(WordTokenizer):
        def count(self, text):
            if text == "bb":
                return 1
            return 2 if text.endswith("\n\nbb") else 3

    result = chunk_document(
        document((tag, content), ("text", "bb")),
        ChunkOptions(max_tokens=2, preserve_tables=False, preserve_code_blocks=False),
        Nonmonotone(),
    )
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["token_count"] == 2
