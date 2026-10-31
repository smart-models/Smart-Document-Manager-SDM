"""Verify explicit chunk metadata projection and mandatory output fields."""

import pytest
from test_chunk_engine import WordTokenizer, document

from ocrserver.chunking.engine import chunk_document
from ocrserver.schemas import ChunkOptions


def test_metadata_projection_keeps_only_selected_fields():
    result = chunk_document(
        document(("paragraph_title", "Sezione"), ("text", "Contenuto")),
        ChunkOptions(max_tokens=32, metadata_fields=["page", "section"]),
        WordTokenizer(),
    )

    assert set(result["chunks"][0]) == {"id", "text", "token_count", "page", "section"}


def test_empty_metadata_selection_emits_the_three_required_fields():
    result = chunk_document(
        document(("text", "Contenuto")),
        ChunkOptions(max_tokens=32, metadata_fields=[]),
        WordTokenizer(),
    )

    assert set(result["chunks"][0]) == {"id", "text", "token_count"}


@pytest.mark.parametrize(
    "fields",
    [["id"], ["unknown"], ["page", "page"]],
)
def test_metadata_selection_rejects_required_unknown_and_duplicate_fields(fields):
    with pytest.raises(ValueError):
        ChunkOptions(max_tokens=32, metadata_fields=fields)
