"""The smart-document-reader skill must stay aligned with the layout tags."""

import re
from pathlib import Path

import yaml
from test_smart_document_manager_skill import assert_what_and_when

from ocrserver.chunking.structure import MARGINAL, TAGS

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "ocr-skill/smart-document-reader"
# PaddleX paddleocr_vl/result.py SKIP_ORDER_LABELS + pipeline.py markdown_ignore_labels.
NULL_ORDER = {
    "figure_title",
    "vision_footnote",
    "image",
    "chart",
    "table",
    "header",
    "header_image",
    "footer",
    "footer_image",
    "footnote",
    "aside_text",
    "number",
}
# pipeline.py markdown_ignore_labels + formula_number (show_formula_number=False).
MARKDOWN_NO = {
    "number",
    "footnote",
    "header",
    "header_image",
    "footer",
    "footer_image",
    "aside_text",
    "formula_number",
}
ROW = re.compile(r"^\|\s*`(?P<tag>[a-z_]+)`\s*\|(?P<cells>.*)\|\s*$")


def _rows():
    text = (SKILL / "references/block-types.md").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        if match := ROW.match(line):
            cells = [c.strip() for c in match["cells"].split("|")]
            rows[match["tag"]] = cells
    return rows


def test_block_types_cover_every_tag_with_correct_order():
    rows = _rows()
    assert set(rows) == set(TAGS)
    for tag, cells in rows.items():
        role, order, content, markdown, treatment = cells
        assert role and content and treatment, tag
        if tag == "reference":
            assert order == markdown == "—" and "never" in treatment.lower()
        else:
            assert order == ("no" if tag in NULL_ORDER else "yes"), tag
            assert markdown == ("no" if tag in MARKDOWN_NO else "yes"), tag


def test_skill_frontmatter_length_links_and_rules():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    assert match is not None
    frontmatter = yaml.safe_load(match.group(1))
    assert frontmatter["name"] == "smart-document-reader"
    assert_what_and_when(frontmatter["description"])
    assert 120 <= len(text.splitlines()) <= 240
    assert "references/" in text
    for reference in ("block-types.md", "document-types.md"):
        assert reference in text
        assert "references/" + reference not in text
        assert (SKILL / "references" / reference).is_file()
    for rule in ("block_id", "group_id", "untrusted", "pages_truncated"):
        assert rule in text, rule
    assert not (SKILL / "questions.md").exists()
    assert "smart-document-reader" in (ROOT / "ocr-skill/README.md").read_text(
        encoding="utf-8"
    )


def test_skill_rules_match_observed_pipeline_behaviour():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    types = (SKILL / "references/block-types.md").read_text(encoding="utf-8")
    # merge_blocks sets group_id from pre-reorder indexes: it is not a block_id.
    assert "equals an earlier block's `block_id`" not in skill
    assert "shares its `group_id` with an earlier non-empty block" in skill
    assert "shares its `group_id` with an earlier non-empty block" in types
    # Figure captions often come after their object at the bottom of a page.
    assert "possible continuation" in skill
    assert "check the next page" in skill
    assert "unresolved" in skill
    assert "= object continues on next page" not in types
    # output="markdown" returns blocks=None: markdown is then the only source.
    assert "`blocks` is null" in skill and "null blocks" in skill
    # A second doc_title is often a subtitle or subject line, not a new document.
    for text in (skill, types):
        assert "subject line" in text
        assert "= several articles" not in text


def test_reading_json_does_not_trigger_unrequested_chunking():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "If `ocr_create_rag_chunks` is available, pass" not in text
    assert "only when explicitly requested" in text
    assert "options.max_tokens" in text


def test_marginal_tags_that_the_chunker_keeps_in_the_body_are_flagged():
    rows = _rows()
    skill_marginal = {
        "header",
        "footer",
        "number",
        "aside_text",
        "header_image",
        "footer_image",
    }
    assert MARGINAL <= skill_marginal
    for tag in skill_marginal - MARGINAL:
        assert "chunker" in rows[tag][-1], tag


def test_partial_and_legacy_results_are_covered():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for rule in (
        "pages_unprocessed",
        "page_failed",
        "error pages",
        "legacy",
        "smart-document-manager",
        "warnings[]",
    ):
        assert rule in text, rule


def test_archive_files_and_manifest_fields_are_covered():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for rule in (
        "## Archive files",
        "`manifest.kind`",
        "`json_ocr.json`",
        "`requested.task`",
        "`boxes` are not archived",
        "`json_chunks.json` and, by default, `parsed.json`",
        "Read the manifest first",
        "`complete` only says",
        "`archive_warnings`",
        "`requested` keeps",
        "`files[].sha256`",
        "`images[].key`",
        "lack `pages` and may lack `unprocessed_pages`",
        "(parsed count), `partial_ocr`",
        "`<stem>.md`",
        "`document/<stem>_pNNNN.md`",
        "`json_rag_<job>_<n>.json`",
        "`input.<ext>`",
        "`requested.filename`",
        "your own visual reading",
    ):
        assert rule in text, rule
    # Every manifest field the skill names is one the archive builder writes.
    builder = (ROOT / "ocrserver/artifacts.py").read_text(encoding="utf-8")
    for key in (
        "complete",
        "json_chunks.json",
        "parsed.json",
        "partial_ocr",
        "unprocessed_pages",
        "archive_warnings",
        "requested",
        "effective",
        "model_revision",
        "original_name",
        "input_not_saved",
        "image_unavailable",
    ):
        assert f'"{key}"' in builder, key
    assert (SKILL / "evals/archive-partial-with-manifest/files/manifest.json").is_file()


def test_heuristics_are_marked_as_heuristics():
    rows = _rows()
    for tag in (
        "footnote",
        "figure_title",
        "vision_footnote",
        "formula_number",
        "reference_content",
    ):
        treatment = rows[tag][-1].lower()
        assert "usually" in treatment or "often" in treatment, tag
    footnote = rows["footnote"][-1].lower()
    assert "plain number" in footnote and "circled" in footnote


def test_generic_genre_exists():
    text = (SKILL / "references/document-types.md").read_text(encoding="utf-8")
    assert "## Generic document" in text
