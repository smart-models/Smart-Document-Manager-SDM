"""Packaged normative output schema and transport-independent validation."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError as SchemaError
from pydantic import ValidationError

from ocrserver.chunking.canonical import canonical_bytes
from ocrserver.chunking.errors import ChunkError
from ocrserver.schemas import RagResponse


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads(
        Path(__file__).with_name("rag.schema.json").read_text(encoding="utf-8")
    )


def output_schema() -> dict:
    """Return a detached copy suitable for OpenAPI or MCP outputSchema."""
    return copy.deepcopy(_schema())


def validate_output(payload: dict) -> dict:
    """Validate schema and common semantics without silently rewriting output.

    The engine additionally verifies source coverage, offsets and token counts
    with its original document and exact tokenizer before exporting this value.
    """
    try:
        Draft202012Validator(_schema()).validate(payload)
        RagResponse.model_validate(payload)
    except (ValidationError, SchemaError) as exc:
        raise ChunkError(
            "invalid_structure",
            "Generated chunks violate the RAG contract.",
            details={"reason": "invalid_rag_output"},
        ) from exc
    canonical_bytes(payload)
    return payload
