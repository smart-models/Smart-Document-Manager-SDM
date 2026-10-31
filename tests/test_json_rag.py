"""json_rag export: one chunk per markdown-visible block, excluded text as fields."""

import math

import pytest

from ocrserver.json_rag import EXCLUDED, build_json_rag, document_name
from ocrserver.table_md import tables_to_markdown


def block(kind, content, bbox=(0, 0, 100, 10), order=None, tokens=None):
    return {
        "type": kind,
        "content": content,
        "bbox": list(bbox),
        "order": order,
        "content_tokens": tokens,
    }


def build(*pages, **kwargs):
    return build_json_rag(
        [{"page": number, "blocks": blocks} for number, blocks in enumerate(pages, 1)],
        document="report.pdf",
        **kwargs,
    )


def chunks(*pages, **kwargs):
    return build(*pages, **kwargs)["chunks"]


def test_one_chunk_per_visible_block_in_reading_order():
    result = chunks(
        [
            block("text", "second", order=2),
            block("doc_title", "  Title  ", order=1),
            block("text", "unordered", order=None),
            block("image", ""),
            block("chart", "  "),
        ],
        [block("paragraph_title", "Next page", order=1)],
    )
    assert [(c["id"], c["page"], c["text"]) for c in result] == [
        (1, 1, "Title"),
        (2, 1, "second"),
        (3, 1, "unordered"),
        (4, 2, "Next page"),
    ]
    assert [c["type"] for c in result] == [
        "doc_title",
        "text",
        "text",
        "paragraph_title",
    ]
    assert all(c["document"] == "report.pdf" for c in result)


def test_page_without_reading_order_keeps_array_order():
    result = chunks([block("text", "a"), block("text", "b"), block("text", "c")])
    assert [c["text"] for c in result] == ["a", "b", "c"]


def test_unknown_labels_with_text_become_chunks_and_excluded_never_do():
    result = chunks(
        [block("future_label", "kept", order=1)]
        + [block(kind, f"{kind} text") for kind in sorted(EXCLUDED)]
    )
    assert [c["text"] for c in result] == ["kept"]


TABLE = "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"


def test_tables_follow_table_format():
    assert chunks([block("table", TABLE, order=1)])[0]["text"] == TABLE
    converted = chunks([block("table", TABLE, order=1)], table_format="markdown")
    assert (
        converted[0]["text"] == tables_to_markdown(TABLE)[0] == "| A |\n| --- |\n| 1 |"
    )


def test_token_count_describes_the_saved_text():
    blocks = [
        block("table", TABLE, order=1, tokens=40),
        block("text", " a ", order=2, tokens=5),
        block("text", "b", order=3, tokens=1),
    ]
    # A converted or stripped text is recounted; an unchanged one keeps its count.
    counted = chunks(blocks, table_format="markdown", count=len)
    assert [c["token_count"] for c in counted] == [len("| A |\n| --- |\n| 1 |"), 1, 1]
    # Without a counter a changed text is unknown, never the stale raw count.
    assert [c.get("token_count") for c in chunks(blocks, table_format="markdown")] == [
        None,
        None,
        1,
    ]
    assert chunks(blocks)[0]["token_count"] == 40


def test_page_fields_are_copied_to_every_chunk_of_their_page():
    result = chunks(
        [
            block("header", "Head A"),
            block("header", "Head A"),
            block("header_image", "Head B"),
            block("footer", "Foot"),
            block("footer_image", ""),
            block("number", "xii"),
            block("text", "one", order=1),
            block("text", "two", order=2),
        ],
        [block("text", "other page", order=1)],
    )
    for chunk in result[:2]:
        assert chunk["page_header"] == "Head A\nHead B"
        assert chunk["page_footer"] == "Foot"
        assert chunk["page_number"] == "xii"
    assert not {"page_header", "page_footer", "page_number"} & set(result[2])


def test_printed_page_number_matching_the_index_is_dropped():
    result = chunks(
        [block("number", "1"), block("text", "one", order=1)],
        [
            block("number", "iii"),
            block("number", "iii"),
            block("text", "two", order=1),
        ],
        [block("number", "- 3 -"), block("text", "three", order=1)],
    )
    assert "page_number" not in result[0]
    assert result[1]["page_number"] == "iii"
    assert result[2]["page_number"] == "- 3 -"


def test_token_count_is_omitted_when_unknown_and_summarized():
    data = build(
        [
            block("text", "a", order=1, tokens=3),
            block("text", "b", order=2, tokens=None),
            block("text", "c", order=3, tokens=6),
        ],
        info={"model_name": "m", "warnings": ["pages_truncated"]},
    )
    assert [c.get("token_count") for c in data["chunks"]] == [3, None, 6]
    assert "token_count" not in data["chunks"][1]
    assert data["metadata"] == {
        "document": "report.pdf",
        "model_name": "m",
        "warnings": ["pages_truncated"],
        "pages": 1,
        "failed_pages": [],
        "unprocessed_pages": [],
        "n_chunks": 3,
        "total_tokens": 9,
        "avg_tokens": 4.5,
        "max_tokens": 6,
        "min_tokens": 3,
        "unattached_blocks": 0,
    }


def test_summary_tokens_are_null_without_counts():
    meta = build([block("text", "a", order=1)])["metadata"]
    assert meta["total_tokens"] is None
    assert meta["avg_tokens"] is None
    assert meta["max_tokens"] is None
    assert meta["min_tokens"] is None


def test_failed_pages_and_pages_without_chunks():
    data = build_json_rag(
        [
            {
                "page": 2,
                "blocks": [
                    block("image", ""),
                    block("number", "2"),
                    block("footnote", "¹ Lost"),
                    block("header", " "),
                ],
            },
            {"page": 1, "error": "timeout"},
            {"page": 3, "blocks": [block("text", "kept", order=1)]},
        ],
        document="report.pdf",
    )
    assert data["metadata"]["failed_pages"] == [1]
    assert data["metadata"]["unattached_blocks"] == 2
    assert data["metadata"]["pages"] == 3
    assert [(c["id"], c["page"]) for c in data["chunks"]] == [(1, 3)]


def test_output_satisfies_the_ingester_contract():
    result = chunks(
        [block("text", f"t{i}", order=i) for i in range(5)],
        [block("text", "x", order=1), block("number", "2")],
    )
    ids = [c["id"] for c in result]
    assert all(type(i) is int for i in ids)
    assert len(set(ids)) == len(ids)
    assert all(isinstance(c["text"], str) and c["text"].strip() for c in result)
    assert all(not isinstance(v, (list, dict)) for c in result for v in c.values())


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.pdf", "report.pdf"),
        ("relazione è.pdf", "relazione è.pdf"),
        ("C:\\in\\scan.png", "scan.png"),
        ("/tmp/a/b.docx", "b.docx"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("", "document"),
        (None, "document"),
        ("C:", "document"),
        ("x" * 300, "x" * 255),
    ],
)
def test_document_name(raw, expected):
    assert document_name(raw) == expected


INVALID = [
    [],
    [1, 2, 3],
    [0, 0, 1, 1, 2, 2, 3, 3],
    [0, math.nan, 10, 20],
    [0, 50, 10, 20],
    [0, math.inf, 1, 1],
]


def test_formula_number_goes_to_the_formula_on_its_line():
    result = chunks(
        [
            block("text", "Before", bbox=(0, 100, 900, 160), order=1),
            block("display_formula", "$$ a=b $$", bbox=(300, 200, 700, 260), order=2),
            block("formula_number", "(1)", bbox=(800, 215, 840, 245)),
            block("text", "After", bbox=(0, 300, 900, 360), order=3),
        ]
    )
    assert [c.get("formula_number") for c in result] == [None, "(1)", None]


def test_formula_number_prefers_formula_chunks_over_overlapping_text():
    result = chunks(
        [
            block("text", "Wide paragraph", bbox=(0, 100, 900, 400), order=1),
            block("display_formula", "$$ x $$", bbox=(300, 500, 700, 540), order=2),
            block("formula_number", "(2)", bbox=(800, 200, 840, 230)),
        ]
    )
    assert result[1]["formula_number"] == "(2)"
    assert "formula_number" not in result[0]


def test_aside_text_goes_to_the_chunk_with_most_vertical_overlap():
    result = chunks(
        [
            block("aside_text", "arXiv:1706.03762v7", bbox=(40, 700, 120, 1900)),
            block("text", "Authors", bbox=(400, 800, 750, 930), order=1),
            block("abstract", "Abstract body", bbox=(490, 1430, 1630, 2000), order=2),
        ]
    )
    assert "aside_text" not in result[0]
    assert result[1]["aside_text"] == "arXiv:1706.03762v7"


def test_without_overlap_the_vertically_closest_chunk_wins():
    result = chunks(
        [
            block("text", "Top", bbox=(0, 0, 100, 100), order=1),
            block("text", "Bottom", bbox=(0, 500, 100, 600), order=2),
            block("aside_text", "Side", bbox=(900, 430, 950, 460)),
        ]
    )
    assert result[1]["aside_text"] == "Side"


def test_equal_overlap_ties_go_to_reading_order():
    result = chunks(
        [
            block("text", "Right column", bbox=(500, 100, 900, 200), order=2),
            block("text", "Left column", bbox=(0, 100, 400, 200), order=1),
            block("aside_text", "Side", bbox=(950, 120, 990, 180)),
        ]
    )
    assert [c["text"] for c in result] == ["Left column", "Right column"]
    assert result[0]["aside_text"] == "Side"
    assert "aside_text" not in result[1]


@pytest.mark.parametrize("bad", INVALID)
def test_invalid_candidate_bbox_is_never_compared(bad):
    result = chunks(
        [
            block("text", "Broken box", bbox=bad, order=1),
            block("text", "Far but valid", bbox=(0, 900, 100, 950), order=2),
            block("aside_text", "Side", bbox=(0, 0, 10, 10)),
        ]
    )
    assert result[1]["aside_text"] == "Side"
    assert "aside_text" not in result[0]


@pytest.mark.parametrize("bad", INVALID)
def test_invalid_source_bbox_falls_back_to_array_position(bad):
    result = chunks(
        [
            block("text", "Intro", bbox=(0, 0, 100, 10), order=1),
            block("display_formula", "$$ y $$", bbox=(0, 20, 100, 30), order=2),
            block("text", "Middle", bbox=(0, 40, 100, 50), order=3),
            block("formula_number", "(3)", bbox=bad),
            block("display_formula", "$$ z $$", bbox=(0, 60, 100, 70), order=4),
        ]
    )
    assert [c.get("formula_number") for c in result] == [None, "(3)", None, None]


def test_page_without_valid_boxes_uses_previous_then_following_block():
    result = chunks(
        [
            block("aside_text", "Side note", bbox=(0, 0, 10, 10)),
            block("text", "First", bbox=[], order=1),
            block("text", "Second", bbox=[], order=2),
            block("aside_text", "Late note", bbox=(0, 0, 10, 10)),
        ]
    )
    assert result[0]["aside_text"] == "Side note"
    assert result[1]["aside_text"] == "Late note"


def test_footnote_latex_marker_finds_the_symbol_in_the_body():
    result = chunks(
        [
            block(
                "text",
                "Ashish Vaswani*\nGoogle Brain",
                bbox=(400, 800, 750, 930),
                order=1,
            ),
            block(
                "abstract", "The dominant models", bbox=(490, 1430, 1630, 2000), order=2
            ),
            block(
                "footnote", " $ ^{*} $Equal contribution.", bbox=(365, 2077, 1759, 2389)
            ),
        ]
    )
    assert result[0]["footnote"] == "$ ^{*} $Equal contribution."
    assert "footnote" not in result[1]


@pytest.mark.parametrize(
    "note,body",
    [
        (r" $ ^{\dagger} $Work at Brain.", r"Aidan N. Gomez $ ^{\dagger} $"),
        (" $ ^4 $Why dot products grow.", "we suspect that $ ^{4} $ for large values"),
        ("¹ Superscript note.", "Research¹ result"),
        ("[2] Bracket note.", "as shown in [2] before"),
        ("† Plain dagger.", "Kaiser† Brain"),
        ("∗Equal contribution.", "Ashish Vaswani∗ Google Brain"),
        ("∗Equal contribution.", "Ashish Vaswani* Google Brain"),
        ("*Glued ascii star.", "Noam Shazeer* Google Brain"),
    ],
)
def test_footnote_marker_forms(note, body):
    result = chunks(
        [
            block("text", body, bbox=(0, 0, 100, 100), order=1),
            block("text", "Closest above the note", bbox=(0, 800, 100, 900), order=2),
            block("footnote", note, bbox=(0, 950, 100, 990)),
        ]
    )
    assert result[0]["footnote"] == note.strip()
    assert "footnote" not in result[1]


def test_bare_digits_are_not_markers_and_the_note_falls_back_above():
    result = chunks(
        [
            block("text", "Table 1 shows 1 result", bbox=(0, 0, 100, 100), order=1),
            block("text", "Closest above", bbox=(0, 800, 100, 900), order=2),
            block("text", "Below the note", bbox=(0, 1000, 100, 1100), order=3),
            block("footnote", " $ ^{1} $Work at Research.", bbox=(0, 950, 100, 990)),
        ]
    )
    assert [c.get("footnote") for c in result] == [
        None,
        "$ ^{1} $Work at Research.",
        None,
    ]


def test_symbol_at_the_start_of_a_chunk_is_not_a_marker():
    result = chunks(
        [
            block("text", "* bullet item", bbox=(0, 0, 100, 100), order=1),
            block("text", "Closest above", bbox=(0, 800, 100, 900), order=2),
            block("footnote", "* Starred note.", bbox=(0, 950, 100, 990)),
        ]
    )
    assert result[1]["footnote"] == "* Starred note."
    assert "footnote" not in result[0]


@pytest.mark.parametrize("bad", INVALID)
def test_note_without_marker_or_geometry_goes_to_the_last_chunk(bad):
    result = chunks(
        [
            block("text", "First", bbox=(0, 0, 100, 100), order=1),
            block("text", "Last", bbox=(0, 200, 100, 300), order=2),
            block("footnote", "Unmarked note.", bbox=bad),
        ]
    )
    assert result[1]["footnote"] == "Unmarked note."


def test_note_above_every_chunk_goes_to_the_last_chunk():
    result = chunks(
        [
            block("footnote", "Unmarked note.", bbox=(0, 0, 100, 10)),
            block("text", "First", bbox=(0, 100, 100, 200), order=1),
            block("text", "Last", bbox=(0, 300, 100, 400), order=2),
        ]
    )
    assert result[1]["footnote"] == "Unmarked note."


def test_two_notes_on_one_chunk_join_with_newlines_in_array_order():
    result = chunks(
        [
            block("text", "Vaswani* and Kaiser†", bbox=(0, 0, 100, 100), order=1),
            block("footnote", "† Second in body, first in array."),
            block("footnote", "* Starred."),
        ]
    )
    assert result[0]["footnote"] == "† Second in body, first in array.\n* Starred."


def test_exponents_inside_formulas_are_not_note_citations():
    result = chunks(
        [
            block(
                "display_formula",
                "$$ E = mc^{2} + x^* $$",
                bbox=(0, 0, 100, 50),
                order=1,
            ),
            block(
                "text",
                "We use this method $ ^{2} $ here.",
                bbox=(0, 60, 100, 100),
                order=2,
            ),
            block("text", "Star cited $ ^{*} $ too.", bbox=(0, 110, 100, 150), order=3),
            block("footnote", " $ ^{2} $Code at github.", bbox=(0, 900, 100, 950)),
            block("footnote", " $ ^{*} $Starred.", bbox=(0, 960, 100, 990)),
        ]
    )
    assert [c.get("footnote") for c in result] == [
        None,
        "$ ^{2} $Code at github.",
        "$ ^{*} $Starred.",
    ]


def test_unprocessed_pages_are_listed_and_stay_failed():
    from ocrserver.json_rag import build_json_rag

    pages = [
        {"page": 1, "blocks": []},
        {"page": 2, "error": "page_unprocessed: x", "unprocessed": True},
        {"page": 3, "error": "boom"},
    ]
    meta = build_json_rag(pages, document="d")["metadata"]
    assert meta["unprocessed_pages"] == [2] and meta["failed_pages"] == [2, 3]
    empty = build_json_rag([{"page": 1, "blocks": []}], document="d")
    assert empty["metadata"]["unprocessed_pages"] == []
