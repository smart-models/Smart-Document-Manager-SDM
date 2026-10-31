"""Independent structure fixtures and real browser-planner/server parity."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from ocrserver.rag_refinement import _validate_plan, materialize
from ocrserver.rag_split import SplitBudget, render_fragment, split_group, sum_counts
from ocrserver.rag_structure import structure_boundaries
from ocrserver.schemas import RagRefinementOptions, StorageRagRefineRequest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads(
    (ROOT / "tests/fixtures/rag-structures.json").read_text(encoding="utf-8")
)


def source_for(fixture):
    metadata = {"document": "default"}
    if "failed_pages" in fixture:
        metadata["failed_pages"] = fixture["failed_pages"]
    # A null type is an absent field: untyped blocks keep the text-only scan.
    types = fixture.get("types", [None] * len(fixture["texts"]))
    return {
        "metadata": metadata,
        "chunks": [
            {
                "id": index + 1,
                "text": text,
                "token_count": fixture.get("tokens", [100] * len(fixture["texts"]))[
                    index
                ],
                "page": fixture.get("pages", [1] * len(fixture["texts"]))[index],
                "document": fixture.get(
                    "documents", ["default"] * len(fixture["texts"])
                )[index],
                **({} if types[index] is None else {"type": types[index]}),
                "custom": {"original": index},
            }
            for index, text in enumerate(fixture["texts"])
        ],
    }


def request_for(options, groups, overrides=None, counts=None):
    return StorageRagRefineRequest(
        source_key="archive/json_rag.json",
        source_sha256="a" * 64,
        execution_id="a0000000-0000-4000-a000-000000000001",
        created_at="2026-09-28T12:00:00Z",
        options=options,
        groups=groups,
        overrides=overrides,
        counts=counts,
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["name"])
def test_protected_boundaries(fixture):
    source = source_for(fixture)
    options = RagRefinementOptions(
        min_tokens=150, max_tokens=250, **fixture.get("options", {})
    )
    result = structure_boundaries(source["chunks"], source["metadata"], options)
    assert result["joins"] == set(fixture["joins"])
    assert result["continuations"] == set(
        fixture.get("continuations", fixture["joins"])
    )
    assert result["hard"] == set(fixture.get("hard", []))
    assert result["gaps"] == set(fixture.get("gaps", []))
    assert result["incomplete"] == fixture.get("incomplete", 0)
    assert result["hidden"] == fixture.get("hidden", 0)
    assert result["conflicts"] == fixture.get("conflicts", 0)
    if result["joins"]:
        # R21: a broken protected join is a real fact derived by the server and
        # recorded as a diagnostic, no longer a 422 for an undeclared override.
        saved = materialize(
            source, request_for(options, [[i] for i in range(len(source["chunks"]))])
        )
        codes = {
            note["code"]
            for chunk in saved["chunks"]
            for note in chunk.get("diagnostics", [])
        }
        assert codes & {"table_split", "code_split"}
    if result["hard"]:
        saved = materialize(
            source, request_for(options, [list(range(len(source["chunks"])))])
        )
        codes = {
            note["code"]
            for chunk in saved["chunks"]
            for note in chunk.get("diagnostics", [])
        }
        assert codes


def run_node(script, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the actual browser planner")
    run = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return json.loads(run.stdout)


def test_actual_preflight_and_saved_text_match_for_every_option_combination():
    inputs = []
    for fixture in FIXTURES:
        for flags in range(8):
            inputs.append(
                {
                    "source": source_for(fixture),
                    "options": {
                        "min_tokens": 150,
                        "max_tokens": 250,
                        "page_boundary": bool(flags & 1),
                        "keep_tables_together": bool(flags & 2),
                        "keep_code_blocks_together": bool(flags & 4),
                    },
                }
            )
    script = """
      import { readFileSync } from 'node:fs';
      import {
        planRagAggregation, ragGroupPreview, ragAggregationPreview
      } from './frontend/dist/assets/rag-preflight.js';
      const inputs = JSON.parse(readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify(inputs.map(({source, options}) => {
        const plan = planRagAggregation(source, options);
        return { plan,
          previews: plan.groups.map(group => ragGroupPreview(source, group)),
          aggregation: ragAggregationPreview(source, plan) };
      })));
    """
    results = run_node(script, inputs)
    for item, result in zip(inputs, results, strict=True):
        plan = result["plan"]
        request = request_for(
            item["options"], [group["source_indices"] for group in plan["groups"]]
        )
        saved = materialize(item["source"], request)
        assert saved["metadata"]["refinement"]["range_counts"] == plan["counts"]
        assert [chunk["id"] for chunk in saved["chunks"]] == list(
            range(1, len(request.groups) + 1)
        )
        for chunk, group, preview in zip(
            saved["chunks"], plan["groups"], result["previews"], strict=True
        ):
            assert chunk["text"] == preview["text"]
            # An unknown size is absent, not null, as in the raw ingest file.
            assert chunk.get("token_count") == group["token_count"]
            # page is a plain payload field: deduplicated raw values, including
            # invalid ones (unlike group pages); null pages stay absent.
            pages = list(
                dict.fromkeys(
                    page
                    for i in group["source_indices"]
                    if (page := item["source"]["chunks"][i].get("page")) is not None
                )
            )
            typed = any(
                "type" in item["source"]["chunks"][i] for i in group["source_indices"]
            )
            assert set(chunk) == {"id", "text", "document", "custom"} | (
                set() if group["token_count"] is None else {"token_count"}
            ) | ({"page"} if pages else set()) | ({"type"} if typed else set())
            assert chunk.get("page") == (pages or None)
            assert chunk["custom"] == [{"original": i} for i in group["source_indices"]]
        # The section 04 preview shows the saved chunks and metadata, without the
        # execution metadata the server adds while writing the file.
        aggregation = result["aggregation"]
        assert aggregation["chunks"] == saved["chunks"]
        assert aggregation["metadata"] == {
            key: value
            for key, value in saved["metadata"].items()
            if key != "refinement"
        }
        assert {
            key: value
            for key, value in aggregation.items()
            if key not in ("chunks", "metadata")
        } == {
            key: value
            for key, value in saved.items()
            if key not in ("chunks", "metadata")
        }


def test_failed_page_gap_is_derived_and_recorded_even_without_page_boundary():
    source = {
        "metadata": {"document": "d", "failed_pages": [2]},
        "chunks": [
            {"id": 1, "text": "A", "token_count": 100, "page": 1},
            {"id": 2, "text": "B", "token_count": 100, "page": 3},
        ],
    }
    options = RagRefinementOptions(min_tokens=150, max_tokens=400, page_boundary=False)
    # R21: the forced failed-page gap is a real fact, derived and recorded.
    saved = materialize(source, request_for(options, [[0, 1]]))
    assert [note["code"] for note in saved["chunks"][0]["diagnostics"]] == [
        "failed_gap_forced"
    ]
    assert len(materialize(source, request_for(options, [[0], [1]]))["chunks"]) == 2
    source["metadata"]["failed_pages"] = [3]
    assert (
        materialize(source, request_for(options, [[0, 1]]))["chunks"][0]["text"]
        == "A\n\nB"
    )


def test_refined_multi_page_chunks_stay_single_under_page_boundary():
    source = {
        "metadata": {"document": "d"},
        "chunks": [
            {"id": 1, "text": "A", "token_count": 100, "page": [1, 2]},
            {"id": 2, "text": "B", "token_count": 100, "page": 2},
        ],
    }
    options = RagRefinementOptions(min_tokens=150, max_tokens=400, page_boundary=True)
    assert materialize(source, request_for(options, [[0], [1]]))["chunks"][0][
        "page"
    ] == [[1, 2]]
    # A merged group across the boundary is accepted with the derived fact.
    merged = materialize(source, request_for(options, [[0, 1]]))
    assert [note["code"] for note in merged["chunks"][0]["diagnostics"]] == [
        "page_boundary_forced"
    ]


def test_merged_chunk_keeps_only_deduplicated_source_fields():
    source = {
        "metadata": {"document": "report.pdf"},
        "chunks": [
            {"id": 1, "text": "A", "token_count": 100, "page": 1, "page_header": "H"},
            {
                "id": 2,
                "text": "B",
                "token_count": 100,
                "page": 1,
                "page_header": "H",
                "footnote": "N",
            },
            {"id": 3, "text": "C", "token_count": 100, "page": 2, "page_header": "H2"},
        ],
    }
    options = RagRefinementOptions(min_tokens=150, max_tokens=400, page_boundary=False)
    saved = materialize(source, request_for(options, [[0, 1, 2]]))
    assert saved["chunks"] == [
        {
            "id": 1,
            "text": "A\n\nB\n\nC",
            "token_count": 300,
            "document": "report.pdf",
            "page": [1, 2],
            "page_header": ["H", "H2"],
            "footnote": ["N"],
        }
    ]


def test_merged_fields_are_always_lists_so_list_values_stay_distinct():
    options = RagRefinementOptions(min_tokens=150, max_tokens=400, page_boundary=False)

    def merged(first, second):
        source = {
            "metadata": {"document": "d"},
            "chunks": [
                {"id": 1, "text": "A", "token_count": 100, "page": 1, "tags": first},
                {"id": 2, "text": "B", "token_count": 100, "page": 1, "tags": second},
            ],
        }
        return materialize(source, request_for(options, [[0, 1]]))["chunks"][0]

    assert merged(["x", "y"], ["x", "y"])["tags"] == [["x", "y"]]
    assert merged("x", "y")["tags"] == ["x", "y"]
    assert merged("x", "x")["page"] == [1]


def test_printed_folio_matching_the_page_index_is_dropped_when_merging():
    source = {
        "metadata": {"document": "report.pdf"},
        "chunks": [
            {"id": 1, "text": "A", "token_count": 100, "page": 2, "page_number": "2"},
            {"id": 2, "text": "B", "token_count": 100, "page": 2, "page_number": "ii"},
        ],
    }
    options = RagRefinementOptions(min_tokens=150, max_tokens=400, page_boundary=False)
    saved = materialize(source, request_for(options, [[0], [1]]))
    assert "page_number" not in saved["chunks"][0]
    assert saved["chunks"][1]["page_number"] == ["ii"]
    merged = materialize(source, request_for(options, [[0, 1]]))
    assert merged["chunks"][0]["page_number"] == ["ii"]


def test_edge_payloads_keep_browser_preview_and_saved_file_identical():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the actual browser planner")
    chunk = '{{"id": {0}, "text": "x", "page": {1}, "token_count": {2}{3}}}'
    raws = [  # Raw JSON text: each side parses it with its own JSON reader.
        [chunk.format(1, 1, 100, ', "k": true'), chunk.format(2, 1, 100, ', "k": 1')],
        [
            chunk.format(1, 1, 100, ', "k": {"a": 1, "b": 2}'),
            chunk.format(2, 1, 100, ', "k": {"b": 2, "a": 1}'),
        ],
        [
            chunk.format(1, 1, 100, ', "k": 2'),
            chunk.format(2, "1.0", "50.0", ', "k": 2.0'),
        ],
        ['{"id": 1, "text": "x", "page": 1}'],
        [
            chunk.format(1, 1, 1, ""),
            chunk.format(2, 2, 1, ""),
            chunk.format(3, 3, 2, ""),
        ],
        # Documents compare by value on both sides, including inside an open fence.
        [
            '{"id": 1, "text": "```", "page": 1, "token_count": 100, '
            '"document": {"a": 1, "b": 2}}',
            '{"id": 2, "text": "x\\n```", "page": 1, "token_count": 100, '
            '"document": {"b": 2, "a": 1}}',
        ],
        [
            chunk.format(1, 1, 100, ', "document": true'),
            chunk.format(2, 1, 100, ', "document": 1'),
        ],
        # Integral ID, null fields, non-ASCII-space folio and "__proto__" key.
        [
            '{"id": 1.0, "text": "a", "page": 3, "token_count": 200, "type": null,'
            ' "page_number": "\\ufeff3", "__proto__": {"x": 1}}'
        ],
    ]
    raws = ['{"metadata": {}, "chunks": [' + ", ".join(item) + "]}" for item in raws]
    options = {"min_tokens": 150, "max_tokens": 250, "page_boundary": True}
    script = """
      import { readFileSync } from 'node:fs';
      import {
        planRagAggregation, ragAggregationPreview
      } from './frontend/dist/assets/rag-preflight.js';
      const { raws, options } = JSON.parse(readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify(raws.map(raw => {
        const source = JSON.parse(raw), plan = planRagAggregation(source, options);
        return { groups: plan.groups.map(group => group.source_indices),
          preview: ragAggregationPreview(source, plan) };
      })));
    """
    run = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps({"raws": raws, "options": options}),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    results = json.loads(run.stdout)
    for raw, result in zip(raws, results, strict=True):
        saved = materialize(
            json.loads(raw),
            request_for(RagRefinementOptions(**options), result["groups"]),
        )
        assert saved["chunks"] == result["preview"]["chunks"]
        assert {
            k: v for k, v in saved["metadata"].items() if k != "refinement"
        } == result["preview"]["metadata"]
    assert [r["preview"]["chunks"][0].get("k") for r in results[:3]] == [
        [True, 1],
        [{"a": 1, "b": 2}],
        [2],
    ]
    assert (
        results[2]["groups"] == [[0, 1]]
        and results[2]["preview"]["chunks"][0]["token_count"] == 150
    )
    assert results[3]["preview"]["chunks"] == [{"id": 1, "text": "x", "page": [1]}]
    assert results[4]["preview"]["metadata"]["avg_tokens"] == 1.33
    assert results[5]["groups"] == [[0, 1]] and results[6]["groups"] == [[0], [1]]
    assert results[7]["preview"]["chunks"] == [
        {
            "id": 1,
            "text": "a",
            "token_count": 200,
            "page": [3],
            "page_number": ["﻿3"],
            "__proto__": [{"x": 1}],
        }
    ]


CHUNKS = [
    {
        "id": 1,
        "text": "alfa beta gamma delta\n\nepsilon zeta",
        "token_count": 100,
        "page": 1,
        "document": "default",
    },
    {"id": 2, "text": "eta theta", "token_count": 40, "page": 2, "document": "default"},
]


def test_ranges_are_sliced_and_member_counts_are_summed(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, 9], [0, 9, full], 1]],
        overrides=[[]],
    )
    result = materialize(source, request, counter=char_counter)
    head = render_fragment(CHUNKS[0]["text"], [(0, 0, 9), (0, 9, full)])
    assert result["chunks"][0]["text"] == head + "\n\n" + "eta theta"
    assert result["chunks"][0]["token_count"] == char_counter.count(head) + 40


def test_a_partial_plan_without_a_counter_is_503():
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, 9], [0, 9, full], 1]],
        overrides=[[]],
    )
    with pytest.raises(HTTPException) as raised:
        materialize(source, request)
    assert (
        raised.value.status_code == 503
        and raised.value.detail["code"] == "rag_tokenizer_unavailable"
    )


def test_a_real_forced_boundary_is_derived_and_recorded_without_a_declaration(
    char_counter,
):
    # R21: the console never sends overrides; the server derives the fact from
    # the source and the structure and records it as a diagnostic.
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000, page_boundary=True),
        [[0, 1]],
    )
    result = materialize(source, request, counter=char_counter)
    assert [note["code"] for note in result["chunks"][0]["diagnostics"]] == [
        "page_boundary_forced"
    ]


def test_the_console_refine_body_glues_across_a_page_without_overrides(char_counter):
    """The exact body rag-selection.js builds: no ``overrides`` field at all."""
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000, page_boundary=True),
        [[0, 1]],
    )
    assert request.overrides is None
    result = materialize(source, request, counter=char_counter)
    codes = [note["code"] for note in result["chunks"][0]["diagnostics"]]
    assert "page_boundary_forced" in codes
    assert (
        result["metadata"]["refinement"]["token_count_method"]
        == "sum_of_saved_estimates"
    )


def test_a_discordant_member_counts_declaration_is_rejected(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    entries = [[0, 0, 9], [0, 9, full], 1]
    measured = char_counter.count(
        render_fragment(CHUNKS[0]["text"], [(0, 0, 9), (0, 9, full)])
    )
    options = RagRefinementOptions(min_tokens=5, max_tokens=1000)
    with pytest.raises(HTTPException) as raised:
        materialize(
            source,
            request_for(options, [entries], counts=[[measured + 1, 40]]),
            counter=char_counter,
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"
    # The same declaration with the real shared sums is accepted.
    shared = materialize(
        source,
        request_for(options, [entries], counts=[[measured, 40]]),
        counter=char_counter,
    )
    assert shared["chunks"][0]["token_count"] == measured + 40


def test_counts_length_must_match_groups():
    with pytest.raises(ValidationError):
        request_for(
            RagRefinementOptions(min_tokens=5, max_tokens=1000),
            [[0], [1]],
            counts=[[1]],
        )


def test_a_declared_forced_page_boundary_is_accepted_and_recorded(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000, page_boundary=True),
        [[0, 1]],
        overrides=[["page_boundary_forced"]],
    )
    result = materialize(source, request, counter=char_counter)
    assert result["chunks"][0]["diagnostics"][0]["code"] == "page_boundary_forced"
    assert result["metadata"]["refinement"]["overrides"] == [
        {"group": 1, "codes": ["page_boundary_forced"]}
    ]


def test_diagnostics_match_the_frozen_schema_shape(char_counter):
    import jsonschema

    schema = json.loads(
        (ROOT / "ocrserver/chunking/rag.schema.json").read_text(encoding="utf-8")
    )
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000, page_boundary=True),
        [[0, 1]],
        overrides=[["page_boundary_forced"]],
    )
    note = materialize(source, request, counter=char_counter)["chunks"][0][
        "diagnostics"
    ][0]
    jsonschema.validate(note, {"$ref": "#/$defs/Diagnostic", "$defs": schema["$defs"]})
    assert note["targets"] == [{"source_key": "p1:b1", "start": None, "end": None}]
    assert {item["name"] for item in note["parameters"]} == {"detail", "pages"}


def test_a_whole_chunk_glue_above_the_maximum_records_the_declared_size_code():
    # O1-1: a glue of two whole-chunk rows has no partial entry, so the size
    # code the console displays is the only signal that reaches the saved file.
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    options = RagRefinementOptions(min_tokens=5, max_tokens=100)
    request = request_for(options, [[0, 1]], overrides=[["above_maximum_fragment"]])
    result = materialize(source, request)
    assert [note["code"] for note in result["chunks"][0]["diagnostics"]] == [
        "above_maximum_fragment"
    ]
    assert result["metadata"]["refinement"]["overrides"] == [
        {"group": 1, "codes": ["above_maximum_fragment"]}
    ]


def test_a_size_code_declared_for_a_within_range_group_is_rejected():
    # O1-1: the server re-measures the group; only a real above/below group may
    # declare its size code.
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    options = RagRefinementOptions(min_tokens=5, max_tokens=1000)
    with pytest.raises(HTTPException) as raised:
        materialize(
            source,
            request_for(options, [[0, 1]], overrides=[["above_maximum_fragment"]]),
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"
    with pytest.raises(HTTPException) as raised:
        materialize(
            source,
            request_for(options, [[0, 1]], overrides=[["below_minimum_fragment"]]),
        )
    assert raised.value.status_code == 422


def test_a_whole_chunk_glue_below_the_minimum_records_the_declared_size_code():
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    options = RagRefinementOptions(min_tokens=1000, max_tokens=2000)
    request = request_for(options, [[0, 1]], overrides=[["below_minimum_fragment"]])
    result = materialize(source, request)
    assert [note["code"] for note in result["chunks"][0]["diagnostics"]] == [
        "below_minimum_fragment"
    ]


def test_a_forgery_of_a_fact_that_does_not_exist_is_rejected(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[0, 1]],
        overrides=[["document_forced"]],
    )
    with pytest.raises(HTTPException) as raised:
        materialize(source, request, counter=char_counter)
    assert raised.value.status_code == 422


def test_a_repeat_entry_must_be_a_real_framing_span(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, 4, "repeat"], [0, 0, 9], 1]],
        overrides=[["table_split"]],
    )
    with pytest.raises(HTTPException) as raised:
        materialize(source, request, counter=char_counter)
    assert raised.value.status_code == 422


def test_a_plan_with_a_gap_inside_a_chunk_is_rejected(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, 9], [0, 10, full], 1]],
        overrides=[[]],
    )
    with pytest.raises(HTTPException) as raised:
        materialize(source, request, counter=char_counter)
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"


def test_a_range_past_the_chunk_text_is_rejected(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, full + 1], 1]],
        overrides=[[]],
    )
    with pytest.raises(HTTPException) as raised:
        materialize(source, request, counter=char_counter)
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"


def test_a_negative_chunk_index_is_rejected():
    # The request schema rejects it too, but _validate_plan must not rely on
    # that: a negative index reads chunks[-1] and hides the reference from the
    # coverage loop, so it is rejected outright with the plan error.
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    with pytest.raises(HTTPException) as raised:
        _validate_plan(source["chunks"], [[-1], [0], [1]])
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"


def test_a_plan_without_gestures_is_byte_identical_to_today():
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000), [[0], [1]]
    )
    result = materialize(source, request)
    assert result["chunks"][0]["text"] == "alfa beta gamma delta\n\nepsilon zeta"
    assert (
        result["metadata"]["refinement"]["token_count_method"]
        == "sum_of_saved_estimates"
    )


def test_an_empty_group_is_rejected_even_when_every_chunk_is_covered():
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000), [[0], [1], []]
    )
    with pytest.raises(HTTPException) as raised:
        materialize(source, request)
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"


def test_a_gesture_free_plan_has_no_overrides_key():
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000), [[0], [1]]
    )
    refinement = materialize(source, request)["metadata"]["refinement"]
    assert "overrides" not in refinement


def test_a_declared_override_still_carries_the_summary(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000, page_boundary=True),
        [[0, 1]],
        overrides=[["page_boundary_forced"]],
    )
    refinement = materialize(source, request, counter=char_counter)["metadata"][
        "refinement"
    ]
    assert refinement["overrides"] == [{"group": 1, "codes": ["page_boundary_forced"]}]


def test_split_plans_declare_the_member_count_method(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, 9], [0, 9, full], 1]],
        overrides=[[]],
    )
    result = materialize(source, request, counter=char_counter)
    assert (
        result["metadata"]["refinement"]["token_count_method"] == "sum_of_member_counts"
    )


def test_split_diagnostics_use_the_existing_shape(char_counter):
    source = {"metadata": {"document": "default"}, "chunks": CHUNKS}
    full = len(CHUNKS[0]["text"])
    request = request_for(
        RagRefinementOptions(min_tokens=5, max_tokens=1000),
        [[[0, 0, 6], [0, 6, full], 1]],
        overrides=[[]],
    )
    note = materialize(source, request, counter=char_counter)["chunks"][0][
        "diagnostics"
    ][0]
    assert set(note) == {"code", "severity", "targets", "parameters"}
    assert note["severity"] in ("info", "warning")


# --- G3: browser slicing and saved text parity over the split fixtures ---
SPLIT_FIXTURES = json.loads(
    (ROOT / "tests/fixtures/rag-split.json").read_text(encoding="utf-8")
)
FLAGS = [
    {
        "keep_tables_together": True,
        "keep_code_blocks_together": True,
        "page_boundary": True,
    },
    {
        "keep_tables_together": True,
        "keep_code_blocks_together": False,
        "page_boundary": True,
    },
    {
        "keep_tables_together": False,
        "keep_code_blocks_together": True,
        "page_boundary": False,
    },
    {
        "keep_tables_together": False,
        "keep_code_blocks_together": False,
        "page_boundary": False,
    },
]


def entries_by_chunk(group):
    """Consecutive entries bundled by the chunk they slice, as the server does."""
    bundled = []
    for entry in group:
        index = entry if isinstance(entry, int) else entry[0]
        if bundled and bundled[-1][0] == index:
            bundled[-1][1].append(entry)
        else:
            bundled.append((index, [entry]))
    return bundled


def group_count(source, group, counter):
    members = []
    for index, entries in entries_by_chunk(group):
        if len(entries) == 1 and isinstance(entries[0], int):
            raw = source["chunks"][index].get("token_count")
            members.append(int(raw) if isinstance(raw, int) else None)
        else:
            members.append(
                counter.count(render_fragment(source["chunks"][index]["text"], entries))
            )
    return sum_counts(members)


def gesture_plan_for(fixture, options, source, counter):
    """The gesture the fixture names: a split of the group, or a glue of two chunks."""
    if fixture["expect"].get("glue"):
        groups = [list(range(len(source["chunks"])))]
        overrides = [["page_boundary_forced"]] if options.page_boundary else [[]]
    else:
        fragments = split_group(
            source["chunks"],
            [0],
            options,
            counter,
            SplitBudget(min_tokens=options.min_tokens, max_tokens=options.max_tokens),
        )
        groups = [list(fragment.entries) for fragment in fragments]
        overrides = [
            [code for code in fragment.notes if code in ("table_split", "code_split")]
            for fragment in fragments
        ]
    counts = [group_count(source, group, counter) for group in groups]
    return {"groups": groups, "overrides": overrides, "counts": counts}


@pytest.mark.parametrize("fixture", SPLIT_FIXTURES, ids=lambda item: item["name"])
@pytest.mark.parametrize("flags", FLAGS)
def test_preflight_and_saved_text_match_for_every_split_fixture(
    fixture, flags, char_counter
):
    source = source_for(fixture)
    options = RagRefinementOptions(**{**fixture["options"], **flags})
    plan = gesture_plan_for(fixture, options, source, char_counter)
    saved = materialize(
        source,
        request_for(options, plan["groups"], overrides=plan["overrides"]),
        counter=char_counter,
    )
    browser_texts = run_node(
        """
      import { readFileSync } from 'node:fs';
      import { groupText } from './frontend/dist/assets/rag-preflight.js';
      import { ragStructureBoundaries } from './frontend/dist/assets/rag-structure.js';
      const { source, groups, options } = JSON.parse(readFileSync(0, 'utf8'));
      const continuations = ragStructureBoundaries(
        source.chunks, source.metadata, options).continuations;
      process.stdout.write(JSON.stringify(
        groups.map(group => groupText(source, group, { continuations }))));
    """,
        {"source": source, "groups": plan["groups"], "options": options.model_dump()},
    )
    assert [chunk["text"] for chunk in saved["chunks"]] == browser_texts
    assert [chunk["token_count"] for chunk in saved["chunks"]] == plan["counts"]


CONTINUATION_CHUNKS = [
    {
        "id": 1,
        "text": "<table>\n<tr><td>a</td></tr>",
        "token_count": 10,
        "page": 1,
        "document": "default",
    },
    {
        "id": 2,
        "text": "<tr><td>b</td></tr>\n</table>\n",
        "token_count": 10,
        "page": 1,
        "document": "default",
    },
]


@pytest.mark.parametrize("flags", FLAGS)
def test_a_glued_group_across_a_continuation_matches_the_saved_text(flags):
    source = {"metadata": {"document": "default"}, "chunks": CONTINUATION_CHUNKS}
    options = RagRefinementOptions(min_tokens=10, max_tokens=1000, **flags)
    saved = materialize(source, request_for(options, [[0, 1]], overrides=[[]]))
    browser = run_node(
        """
      import { readFileSync } from 'node:fs';
      import { groupText } from './frontend/dist/assets/rag-preflight.js';
      import { ragStructureBoundaries } from './frontend/dist/assets/rag-structure.js';
      const { source, options } = JSON.parse(readFileSync(0, 'utf8'));
      const continuations = ragStructureBoundaries(
        source.chunks, source.metadata, options).continuations;
      process.stdout.write(JSON.stringify(
        groupText(source, [0, 1], { continuations })));
    """,
        {"source": source, "options": options.model_dump()},
    )
    assert saved["chunks"][0]["text"] == browser
    # The structural continuation joins compactly, not with a blank line.
    assert (
        browser
        == CONTINUATION_CHUNKS[0]["text"] + "\n" + CONTINUATION_CHUNKS[1]["text"]
    )
