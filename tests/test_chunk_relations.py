"""Conservative, deterministic source relations exposed before packing."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from ocrserver.chunking.structure import Document
from ocrserver.chunking.types import Section, Source


def document(blocks, *, width=1000, height=1000):
    """Build an observed single-page structure with explicit heading events."""
    sources = [Source(1, index, block) for index, block in enumerate(blocks)]
    sections = [Section("s:root", None, None, [])]
    for source in sources:
        if source.tag == "paragraph_title":
            sections.append(Section(source.key, source, source.content, []))
        else:
            sections[-1].sources.append(source)
    return Document(
        pages=[{"page": 1, "width": width, "height": height}],
        sources=sources,
        sections=sections,
        scope={},
        page_context={},
        assets={},
        titles=[],
        diagnostics=[],
        order_summary={},
        fingerprint="",
    )


def block(tag, content, bbox=None, **extra):
    return {"type": tag, "content": content, "bbox": bbox, **extra}


def test_unambiguous_formula_number_uses_nearby_compatible_geometry():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", "E=mc^2", [100, 100, 700, 150]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    relations = resolve_relations(source)

    assert len(relations) == 1
    relation = relations[0]
    assert relation["type"] == "formula_number"
    assert relation["from"]["source_key"] == "p1:b1"
    assert relation["to"]["source_key"] == "p1:b0"
    assert relation["method"] == "geometry"
    assert relation["status"] == "across_chunks"


def test_a_heading_between_blocks_prevents_a_geometric_relationship():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", "E=mc^2", [100, 100, 700, 150]),
            block("paragraph_title", "Second section", [100, 170, 700, 190]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["status"] == "unresolved"
    assert relation["to"] is None
    assert relation["candidate_targets"] == []


def test_explicit_formula_label_has_priority_over_closer_geometry():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", r"a+b\tag{1}", [100, 500, 700, 550]),
            block("display_formula", "c+d", [100, 100, 700, 150]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["method"] == "explicit"
    assert relation["to"]["source_key"] == "p1:b0"
    assert relation["status"] == "across_chunks"


def test_caption_with_two_geometric_candidates_stays_unresolved():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("image", "", [100, 100, 400, 400]),
            block("chart", "", [410, 100, 700, 400]),
            block("vision_footnote", "Observed caption", [100, 410, 700, 430]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["type"] == "caption_object"
    assert relation["status"] == "unresolved"
    assert relation["to"] is None
    assert relation["method"] == "geometry"
    assert [item["source_key"] for item in relation["candidate_targets"]] == [
        "p1:b0",
        "p1:b1",
    ]


def test_explicit_table_caption_works_without_geometry_or_generic_group_ids():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block(
                "table",
                "<table><caption>Table 2</caption></table>",
                group_id=9,
            ),
            block(
                "table",
                "<table><caption>Table 1</caption></table>",
                group_id=8,
            ),
            block("figure_title", "TABLE 1: Experiments", group_id=9),
        ],
        width=None,
        height=None,
    )
    (relation,) = resolve_relations(source)

    assert relation["method"] == "explicit"
    assert relation["to"]["source_key"] == "p1:b1"


def test_footnote_requires_an_observed_unambiguous_marker_in_body_text():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("text", "A fact [1] with evidence."),
            block("footnote", "[1] The observed note."),
            block("footnote", "An unnumbered note next to the body."),
        ]
    )
    relations = resolve_relations(source)
    by_origin = {item["from"]["source_key"]: item for item in relations}

    linked = by_origin["p1:b1"]
    assert linked["type"] == "footnote_reference"
    assert linked["to"] == {"source_key": "p1:b0", "start": 7, "end": 10}
    assert linked["method"] == "explicit"
    assert by_origin["p1:b2"]["status"] == "unresolved"
    assert by_origin["p1:b2"]["to"] is None


def test_table_continuation_requires_an_explicit_label_and_continuation_word():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("table", "<table><caption>Table 1</caption></table>"),
            block(
                "table",
                "<table><caption>Table 1 (continued)</caption></table>",
            ),
            block("table", "<table><tr><td>Similar nearby data</td></tr></table>"),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["type"] == "table_continuation"
    assert relation["from"]["source_key"] == "p1:b1"
    assert relation["to"]["source_key"] == "p1:b0"
    assert relation["method"] == "explicit"


def test_inline_formula_links_only_to_contiguous_observed_prose_context():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("text", "Before $x$ after."),
            block("inline_formula", r"\alpha"),
            block("paragraph_title", "Next section"),
            block("text", "Not contiguous context for the old formula."),
        ]
    )
    relations = resolve_relations(source)
    assert len(relations) == 3
    embedded = [item for item in relations if item["from"]["source_key"] == "p1:b0"]
    assert {
        tuple(item["to"][key] for key in ("start", "end")) for item in embedded
    } == {(0, 7), (10, 17)}
    external = next(item for item in relations if item["from"]["source_key"] == "p1:b1")
    assert external["to"]["source_key"] == "p1:b0"
    assert all(item["method"] == "explicit" for item in relations)


@pytest.mark.parametrize(
    "width,height",
    [(None, 1000), (1000, None), (0, 1000)],
)
def test_missing_page_dimensions_leave_geometry_unresolved(width, height):
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("image", "", [100, 100, 700, 400]),
            block("figure_title", "Caption", [100, 410, 700, 430]),
        ],
        width=width,
        height=height,
    )
    (relation,) = resolve_relations(source)

    assert relation["status"] == "unresolved"
    assert relation["method"] == "none"
    assert relation["candidate_targets"] == []


def test_an_explicit_label_in_the_same_section_beats_other_sections():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", r"a\tag{1}"),
            block("paragraph_title", "New section"),
            block("display_formula", r"b\tag{1}"),
            block("formula_number", "(1)"),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["to"]["source_key"] == "p1:b2"
    assert relation["status"] == "across_chunks"


def test_a_unique_explicit_link_across_sections_is_declared_without_merging():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", r"a\tag{1}"),
            block("paragraph_title", "New section"),
            block("formula_number", "(1)"),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["to"]["source_key"] == "p1:b0"
    assert relation["status"] == "cross_section"


def test_duplicate_labels_remain_ambiguous_even_when_geometry_is_clear():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", r"a\tag{1}", [100, 100, 700, 150]),
            block("display_formula", r"b\tag{1}", [100, 500, 700, 550]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["status"] == "unresolved"
    assert relation["method"] == "explicit"
    assert [item["source_key"] for item in relation["candidate_targets"]] == [
        "p1:b0",
        "p1:b1",
    ]


def test_geometry_does_not_override_a_contradictory_observed_label():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", r"a\tag{2}", [100, 100, 700, 150]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["status"] == "unresolved"
    assert relation["to"] is None


def test_multiple_note_references_are_preserved_as_candidates_not_guessed():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("text", "First [1], then again [1]."),
            block("footnote", "[1] Note."),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["status"] == "unresolved"
    assert len(relation["candidate_targets"]) == 2


def test_schema_and_jcs_identity_are_stable_without_mutating_input():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", "E=mc^2", [100, 100, 700, 150]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    original = deepcopy(source)
    (relation,) = resolve_relations(source)
    normative = json.loads(
        Path(
            "tests/fixtures/ocr-rag-chunks.schema.json",
        ).read_text(encoding="utf-8")
    )
    schema = {"$ref": "#/$defs/Relation", "$defs": normative["$defs"]}
    jsonschema.Draft202012Validator(schema).validate(relation)
    projection = (
        '{"endpoints":[{"end":null,"source_key":"p1:b0","start":null},'
        '{"end":null,"source_key":"p1:b1","start":null}],'
        '"method":"geometry","type":"formula_number"}'
    )

    expected = hashlib.sha256(projection.encode("utf-8")).hexdigest()
    assert relation["id"] == expected
    assert resolve_relations(source) == [relation]
    assert source == original


def test_relation_metadata_is_bounded_without_charging_final_bytes():
    from ocrserver.chunking.errors import ChunkError
    from ocrserver.chunking.relations import resolve_relations
    from ocrserver.chunking.types import ChunkLimits, Work

    source = document(
        [
            block("image", "", [100, 100, 700, 400]),
            block("figure_title", "Caption", [100, 410, 700, 430]),
        ]
    )
    work = Work(None, ChunkLimits(max_output_bytes=100))

    with pytest.raises(ChunkError) as raised:
        resolve_relations(source, work=work)

    assert raised.value.code == "chunk_work_limit"
    assert raised.value.status_code == 413
    assert raised.value.details["limit"] == "output_bytes"
    assert "output_bytes" not in work.counters


def test_relation_byte_guard_counts_ambiguous_targets_exactly_once():
    from ocrserver.chunking.errors import ChunkError
    from ocrserver.chunking.relations import resolve_relations
    from ocrserver.chunking.types import ChunkLimits, Work

    source = document(
        [
            block("image", "", [100, 100, 400, 400]),
            block("chart", "", [410, 100, 700, 400]),
            block("vision_footnote", "Caption", [100, 410, 700, 430]),
        ]
    )
    (relation,) = resolve_relations(source)
    size = len(
        json.dumps(
            relation,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    exact = Work(None, ChunkLimits(max_output_bytes=size))
    too_small = Work(None, ChunkLimits(max_output_bytes=size - 1))

    assert resolve_relations(source, work=exact) == [relation]
    assert exact.counters == {}
    with pytest.raises(ChunkError):
        resolve_relations(source, work=too_small)


@pytest.mark.parametrize("gap,resolved", [(50, True), (51, False)])
def test_geometry_threshold_is_five_percent_of_page_height(gap, resolved):
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", "a+b", [100, 100, 700, 150]),
            block("formula_number", "(1)", [700 + gap, 100, 800 + gap, 150]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert (relation["to"] is not None) is resolved


def test_function_arguments_are_not_treated_as_explicit_equation_numbers():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("display_formula", "f(1)", [100, 500, 700, 550]),
            block("display_formula", "x+y", [100, 100, 700, 150]),
            block("formula_number", "(1)", [710, 100, 740, 150]),
        ]
    )
    (relation,) = resolve_relations(source)

    assert relation["method"] == "geometry"
    assert relation["to"]["source_key"] == "p1:b1"


def test_leading_empty_asset_shares_its_caption_section_without_heading_barrier():
    from ocrserver.chunking.relations import resolve_relations

    source = document(
        [
            block("image", "", [100, 100, 700, 400]),
            block("figure_title", "Caption", [100, 410, 700, 430]),
        ]
    )
    # Structure excludes empty assets from body sections, retaining their
    # sources for metadata. The caption is the first nonempty body block.
    source.sections[0].sources = [source.sources[1]]
    (relation,) = resolve_relations(source)

    assert relation["method"] == "geometry"
    assert relation["to"]["source_key"] == "p1:b0"
