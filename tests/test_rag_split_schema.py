"""Verify RAG split request normalization and schema validation."""

import pytest
from pydantic import ValidationError
from ocrserver.schemas import (
    RagParameter,
    RagRefinementOptions,
    RagSplitNote,
    StorageRagRefineRequest,
    StorageRagSplitRequest,
    normalize_entry,
)

OPTIONS = RagRefinementOptions(min_tokens=10, max_tokens=100)
SPLIT = {
    "source_key": "archive/json_rag.json",
    "source_sha256": "a" * 64,
    "options": OPTIONS,
}
REFINE = {
    **SPLIT,
    "execution_id": "a0000000-0000-4000-a000-000000000001",
    "created_at": "2026-10-28T12:00:00Z",
}


def test_entries_accept_integer_triple_and_repeat_quadruple():
    assert normalize_entry(3) == 3
    assert normalize_entry([3, 1, 9]) == (3, 1, 9)
    assert normalize_entry([3, 1, 9, "repeat"]) == (3, 1, 9, "repeat")


@pytest.mark.parametrize(
    "bad", [[3, 1], [3, 1, 9, "other"], [-1, 0, 5], [3, 2, 2], [3, 0.5, 5]]
)
def test_invalid_entries_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_entry(bad)


def test_split_request_identifies_the_row_by_its_entries():
    value = StorageRagSplitRequest(**SPLIT, group=[2, [3, 0, 40]])
    assert value.group == [2, (3, 0, 40)]


@pytest.mark.parametrize(
    "code",
    [
        "table_split",
        "math_atomic",
        "page_boundary_forced",
        "above_maximum_fragment",
        "search_budget_exhausted",
    ],
)
def test_notes_carry_the_frozen_diagnostic_shape(code):
    note = RagSplitNote(
        code=code,
        severity="warning",
        targets=[],
        parameters=[RagParameter(name="detail", value="testo")],
    )
    assert set(note.model_dump()) == {"code", "severity", "targets", "parameters"}


def test_refine_request_keeps_integer_plans_valid_and_accepts_overrides():
    old = StorageRagRefineRequest(**REFINE, groups=[[0, 1], [2]])
    assert old.groups == [[0, 1], [2]] and old.overrides is None
    new = StorageRagRefineRequest(
        **REFINE, groups=[[0, [1, 0, 4, "repeat"]]], overrides=[["table_split"]]
    )
    assert new.overrides == [["table_split"]]


def test_overrides_length_must_match_groups():
    with pytest.raises(ValidationError):
        StorageRagRefineRequest(
            **REFINE, groups=[[0], [1]], overrides=[["table_split"]]
        )
