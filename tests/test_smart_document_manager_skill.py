"""The smart-document-manager skill must match its tool contracts and authoring rules."""

import json
import re
from pathlib import Path

import yaml
from ocr_mcp.inputs import ParseDocumentInput, RecognizeImageInput
from ocr_mcp.inputs_office import ConvertOfficeToPdfInput
from ocr_mcp.inputs_rag_storage import RefineRagStorageInput, SplitRagGroupInput
from ocr_mcp.inputs_storage import (
    DeleteStorageArchiveInput,
    DeleteStorageFileInput,
    DownloadStorageFileInput,
    DownloadStorageZipInput,
    GetStorageArchiveInput,
    ListStorageArchivesInput,
)
from ocr_mcp.rag_contract import CreateRagChunksInput
from ocr_mcp.tool_map import TOOL_ROUTES
from ocr_mcp.tools.health import CheckHealthInput, GetStatsInput

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "ocr-skill/smart-document-manager"
MODELS = {
    "ocr_check_health": CheckHealthInput,
    "ocr_get_stats": GetStatsInput,
    "ocr_parse_document": ParseDocumentInput,
    "ocr_recognize_image": RecognizeImageInput,
    "ocr_create_rag_chunks": CreateRagChunksInput,
    "ocr_list_storage_archives": ListStorageArchivesInput,
    "ocr_get_storage_archive": GetStorageArchiveInput,
    "ocr_download_storage_file": DownloadStorageFileInput,
    "ocr_download_storage_zip": DownloadStorageZipInput,
    "ocr_delete_storage_archive": DeleteStorageArchiveInput,
    "ocr_delete_storage_file": DeleteStorageFileInput,
    "ocr_refine_rag_storage": RefineRagStorageInput,
    "ocr_split_rag_group": SplitRagGroupInput,
    "ocr_convert_office_to_pdf": ConvertOfficeToPdfInput,
}
WIRE_SHAPE = {"params": {"response_format": "markdown"}}
FENCE = re.compile(r"```json\r?\n(.*?)\r?\n```", re.DOTALL)


def frontmatter(text):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    assert match is not None
    return yaml.safe_load(match.group(1)), text[match.end() :]


def assert_what_and_when(description):
    """Anthropic rule: third person, what the skill does and when to use it."""
    assert len(description) <= 1024
    assert not description.startswith("Use when"), "say what the skill does first"
    assert "Use when" in description
    assert re.search(r"\b(I|you|your)\b", description, re.IGNORECASE) is None


def documents():
    return [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]


def test_every_json_example_validates_against_its_tool_model():
    assert set(MODELS) == set(TOOL_ROUTES)
    examples = 0
    for path in documents():
        for raw in FENCE.findall(path.read_text(encoding="utf-8")):
            block = json.loads(raw)
            if "name" not in block:
                assert block == WIRE_SHAPE, (path.name, raw)
                continue
            assert block["name"] in TOOL_ROUTES, (path.name, block["name"])
            MODELS[block["name"]].model_validate(block["arguments"]["params"])
            examples += 1
    assert examples >= 5


def test_long_references_open_with_contents():
    for path in sorted((SKILL / "references").glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 100:
            headings = [line for line in lines if line.startswith("#")]
            assert headings[1] == "## Contents", path.name


def test_description_states_what_and_when():
    meta, _ = frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "smart-document-manager"
    assert_what_and_when(meta["description"])
    assert "any `smart-document-mcp` tool" in meta["description"]


def test_skill_body_is_concise():
    _, body = frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert len(body.split()) <= 1350


def test_quality_warnings_and_unmarked_chunks_are_taught():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    reference = (SKILL / "references/reference.md").read_text(encoding="utf-8")
    playbooks = (SKILL / "references/playbooks.md").read_text(encoding="utf-8")
    for text in (skill, reference, playbooks):
        for rule in ("layout_low_confidence", "table_suspect", "vector"):
            assert rule in text, rule
    for text in (skill, reference):
        for rule in ("low_layout_confidence_pages", "suspect_table_pages"):
            assert rule in text, rule
    for word in (
        "`kept`",
        "`rejected`",
        "`skipped`",
        "`off`",
        "`low_density`",
        "`cut_repetition`",
        "`repeated_rows`",
        "OCR_TABLE_REREAD",
    ):
        assert word in reference, word


def test_stored_document_questions_route_by_file_and_size():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    playbooks = (SKILL / "references/playbooks.md").read_text(encoding="utf-8")
    assert "A question about a stored document" in skill
    assert "`files[].size`" in skill
    assert "## Answer a question about a stored document" in playbooks
    for rule in (
        "| none |",
        "`json_ocr.json` up to about 250 KB",
        "`json_rag.json` up to about 350 KB",
        "no page boundaries",
        "your own visual reading",
        "for saving",
    ):
        assert rule in playbooks, rule


def test_tools_are_named_fully_qualified():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for tool in TOOL_ROUTES:
        assert f"smart-document-mcp:{tool}" in text, tool
    assert "mcp__" in text, "explain how clients prefix MCP tool names"


def test_key_operating_rules_are_present():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for rule in (
        "job_id",
        "pages_truncated",
        "pages_unprocessed",
        "MCP_INPUT_ROOTS",
        "never retr",
        "smart-document-reader",
        "preview=false",
        "`counts` or `overrides`",
        "never a hand-written slice",
        "## When in doubt, ask",
    ):
        assert rule in text, rule


def test_refinement_docs_name_every_server_note_code():
    """The reference lists each note code the server can record."""
    from ocrserver.schemas import RAG_SPLIT_NOTE_SEVERITY

    reference = (SKILL / "references/reference.md").read_text(encoding="utf-8")
    playbooks = (SKILL / "references/playbooks.md").read_text(encoding="utf-8")
    for code in RAG_SPLIT_NOTE_SEVERITY:
        assert f"`{code}`" in reference, code
    for name in (
        "ocr_split_rag_group",
        "rag_source_changed",
        "rag_invalid_plan",
        "rag_tokenizer_unavailable",
        "0-based",
    ):
        assert name in playbooks, name


def test_proposed_defaults_match_the_console():
    """The parameter table proposes what the web console preselects."""
    dist = ROOT / "frontend/dist"
    index = (dist / "index.html").read_text(encoding="utf-8")
    rag = (dist / "assets/rag.js").read_text(encoding="utf-8")
    selection = (dist / "assets/rag-selection.js").read_text(encoding="utf-8")
    storage = (dist / "assets/storage.js").read_text(encoding="utf-8")
    assert '<option value="markdown" selected>' in index
    assert 'id="parse-out-rag" checked' in index
    assert 'id="parse-images" checked' in index  # the one deviation, explained
    assert 'id="rag-max" type="number" min="1" max="32768" step="1" value="512"' in rag
    assert 'id="rag-preflight-min" type="number" min="1" step="1" value="512"' in selection
    assert 'id="rag-preflight-max" type="number" min="1" step="1" value="1024"' in selection
    assert '<input id="rag-page-boundary" type="checkbox" checked />' in selection
    assert '<select id="storage-page-size"><option selected>10</option>' in storage
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for setting in (
        '`table_format="markdown"`',
        "`json_rag=true`",
        "`include_images=false` (console on",
        "`options.max_tokens=512`",
        "`options.min_tokens=512`",
        "`max_tokens=1024`",
        "`page_boundary=true`",
        "`limit=10`",
        "wait for a yes",
        "explicit yes",
    ):
        assert setting in text, setting


def test_no_maintainer_section_in_operating_skill():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "Keep the skill aligned" not in text
    assert "Source of truth:" not in text
