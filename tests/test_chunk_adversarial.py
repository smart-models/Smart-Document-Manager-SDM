"""Independent PRD regressions discovered by adversarial specification review."""

import pytest

from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.errors import ChunkError
from ocrserver.schemas import ChunkOptions
from test_chunk_engine import WordTokenizer, document


def test_inline_formula_relations_report_actual_split_source_intervals():
    source = document(("text", "alpha beta gamma $x+y$ delta epsilon zeta"))
    chunks = chunk_document(source, ChunkOptions(max_tokens=3), WordTokenizer())[
        "chunks"
    ]
    assert len(chunks) > 1
    relations = {
        relation["id"]: relation for chunk in chunks for relation in chunk["relations"]
    }
    assert relations
    for relation in relations.values():
        both_endpoints_together = any(
            all(
                any(
                    span["source_key"] == endpoint["source_key"]
                    and span["start"] <= endpoint["start"]
                    and span["end"] >= endpoint["end"]
                    for span in chunk["source_spans"]
                )
                for endpoint in (relation["from"], relation["to"])
            )
            for chunk in chunks
        )
        if not both_endpoints_together:
            assert relation["status"] == "across_chunks"


def test_copied_page_asset_does_not_hide_a_cross_section_relation():
    source = document(
        ("paragraph_title", "A"),
        ("image", "Figure 1"),
        ("paragraph_title", "B"),
        ("figure_title", "Figure 1: caption"),
    )
    chunks = chunk_document(source, ChunkOptions(max_tokens=512), WordTokenizer())[
        "chunks"
    ]
    assert len(chunks) == 2
    relations = [relation for chunk in chunks for relation in chunk["relations"]]
    assert relations
    assert all(relation["status"] == "cross_section" for relation in relations)


def test_oversized_text_atom_does_not_prune_a_fitting_later_block_combination():
    class NonmonotoneTokenizer(WordTokenizer):
        def count(self, text):
            return {"aa": 3, "a": 3, "bb": 1, "aa\n\nbb": 2}.get(text, 3)

    chunks = chunk_document(
        document(("text", "aa"), ("text", "bb")),
        ChunkOptions(max_tokens=2),
        NonmonotoneTokenizer(),
    )["chunks"]
    assert [chunk["text"] for chunk in chunks] == ["aa\n\nbb"]


def test_block_wide_relation_requires_the_entire_split_object_in_one_chunk():
    source = {
        "pages": [
            {
                "page": 1,
                "width": 100,
                "height": 100,
                "blocks": [
                    {
                        "type": "image",
                        "content": "one two three four five",
                        "bbox": [0, 0, 50, 10],
                    },
                    {
                        "type": "figure_title",
                        "content": "caption",
                        "bbox": [0, 12, 50, 15],
                    },
                ],
            }
        ]
    }
    chunks = chunk_document(source, ChunkOptions(max_tokens=2), WordTokenizer())[
        "chunks"
    ]
    assert len(chunks) > 1
    relations = [relation for chunk in chunks for relation in chunk["relations"]]
    assert relations
    assert all(relation["status"] == "across_chunks" for relation in relations)


@pytest.mark.parametrize("formula", ["$x +\ny$", "$x$2"])
def test_balanced_dollar_formula_is_not_divided_by_prose_fallback(formula):
    class CharacterTokenizer(WordTokenizer):
        def count(self, text):
            return len(text)

    budget = 2 if formula == "$x$2" else 6
    with pytest.raises(ChunkError) as raised:
        chunk_document(
            document(("text", formula)),
            ChunkOptions(max_tokens=budget),
            CharacterTokenizer(),
        )
    assert raised.value.code == "unsplittable_content"


def test_asset_only_page_preserves_its_existing_reference_in_document_context():
    source = {
        "pages": [
            {
                "page": 1,
                "blocks": [{"type": "image", "content": ""}],
                "image_names": ["figures/page-one.png"],
            },
            {"page": 2, "blocks": [{"type": "text", "content": "Body"}]},
        ]
    }
    chunks = chunk_document(source, ChunkOptions(max_tokens=512), WordTokenizer())[
        "chunks"
    ]
    unassigned = chunks[0]["document_context"]["unassigned_sources"]
    assert len(unassigned) == 1
    assert unassigned[0]["source"]["source_key"] == "p1:b0"
    assert unassigned[0]["reference"] == "figures/page-one.png"
