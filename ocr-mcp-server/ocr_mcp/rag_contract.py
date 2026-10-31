"""Packaged RAG contract: no OCR/model dependencies in the MCP image."""

import json
from importlib.resources import files
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from mcp_types import CallToolResult
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
)

from ocr_mcp.inputs import RawString, SourceInput

OPTIONAL_CHUNK_METADATA_FIELDS = (
    "page",
    "pages",
    "section_id",
    "section",
    "section_path",
    "block_types",
    "source_spans",
    "context_spans",
    "page_context",
    "assets",
    "relations",
    "overflow",
    "overflow_tokens",
    "overflow_reason",
    "overlap_token_count",
    "source_fingerprint",
    "tokenizer",
    "chunker_version",
    "policy_hash",
    "source_id",
    "lang",
    "diagnostics",
    "document_context",
)


RAG_SCHEMA = json.loads(
    files("ocr_mcp").joinpath("rag.schema.json").read_text(encoding="utf-8")
)
RAG_VALIDATOR = Draft202012Validator(RAG_SCHEMA)
PARSED_DOCUMENT_SCHEMA = json.loads(
    files("ocr_mcp").joinpath("rag-input.schema.json").read_text(encoding="utf-8")
)


class RagResponse(RootModel[dict[str, Any]]):
    """Permit empty SDK error results; validate success payloads separately."""


RagToolResult = Annotated[CallToolResult, RagResponse]


class ChunkOptions(BaseModel):
    """Typed mirror of the API options, guarded by OpenAPI drift tests."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    max_tokens: int = Field(
        ge=1,
        le=32768,
        description=(
            "Required per-chunk token budget including headings and overlap (e.g. 512)"
        ),
    )
    strictly: bool = Field(
        default=True,
        description=(
            "Reject protected content that cannot meet the "
            "budget rather than silently overflow (e.g. true)"
        ),
    )
    preserve_tables: bool = Field(
        default=True,
        description=(
            "Keep tables whole unless splitting was explicitly requested (e.g. true)"
        ),
    )
    preserve_code_blocks: bool = Field(
        default=True,
        description=(
            "Keep code blocks whole unless splitting was "
            "explicitly requested (e.g. true)"
        ),
    )
    overlap_tokens: int = Field(
        default=0,
        ge=0,
        le=32767,
        description=(
            "Requested neighboring text overlap; must stay below max_tokens (e.g. 32)"
        ),
    )
    marginal_policy: Literal["metadata", "text"] = Field(
        default="metadata",
        description=(
            "Keep page-margin content as metadata or include "
            "it as text (e.g. 'metadata')"
        ),
    )
    table_header_policy: Literal["declared", "first_row", "none"] = Field(
        default="declared",
        description=(
            "Use declared headers, assume a first row, or "
            "repeat none when splitting tables (e.g. "
            "'declared')"
        ),
    )
    metadata_fields: list[str] = Field(
        default_factory=lambda: list(OPTIONAL_CHUNK_METADATA_FIELDS),
        description=(
            "Optional chunk metadata to emit; id, text and "
            "token_count are always present (e.g. "
            "['section_path','relations'])"
        ),
    )

    @field_validator("metadata_fields")
    @classmethod
    def validate_metadata_fields(cls, value):
        """Validate metadata selection and return fields in canonical order."""
        if len(value) != len(set(value)):
            raise ValueError("metadata_fields must not contain duplicates")
        unknown = sorted(set(value) - set(OPTIONAL_CHUNK_METADATA_FIELDS))
        if unknown:
            raise ValueError(f"unknown metadata_fields: {', '.join(unknown)}")
        forbidden = sorted(set(value) & {"id", "text", "token_count"})
        if forbidden:
            raise ValueError(
                "required fields cannot be selected: " + ", ".join(forbidden)
            )
        order = {
            name: index for index, name in enumerate(OPTIONAL_CHUNK_METADATA_FIELDS)
        }
        return sorted(value, key=order.__getitem__)


class CreateRagChunksInput(SourceInput):
    """Request explicit RAG creation from parsed JSON or one document source."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    parsed_document: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Complete structured /parse JSON, exclusive with "
            "document bytes (e.g. "
            "{'pages':[{'page':1,'blocks':[]}]})"
        ),
        json_schema_extra={"anyOf": [PARSED_DOCUMENT_SCHEMA, {"type": "null"}]},
    )
    source_key: RawString | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=(
            "Key of a stored json_ocr.json or parsed.json from "
            "ocr_get_storage_archive; the MCP downloads and chunks it, so no "
            "JSON passes through the client (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/json_ocr.json')"
        ),
    )
    pdf_base64: str | None = Field(
        default=None,
        repr=False,
        description=(
            "Actual PDF bytes as base64; exclusive with other"
            " sources (e.g. 'JVBERi0xLjcK')"
        ),
    )
    document_base64: str | None = Field(
        default=None,
        repr=False,
        description=(
            "Actual PDF/image/Office bytes as base64; "
            "exclusive with other sources (e.g. "
            "'UEsDBBQABgAIAAAAIQ')"
        ),
    )
    options: ChunkOptions = Field(
        description=(
            "Explicit token budget and protection options "
            "chosen for this chunking task (e.g. "
            "{'max_tokens':512})"
        )
    )
    source_id: RawString | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional caller label attached to chunks; not a "
            "retrieval/job ID (e.g. 'invoice-2026-10')"
        ),
    )
    lang: RawString | None = Field(
        default=None,
        description=(
            "Optional metadata language label; does not "
            "change OCR recognition (e.g. 'en')"
        ),
    )
    accept_truncated: bool = Field(
        default=False,
        description=(
            "Explicitly accept chunking a source whose OCR "
            "pages were truncated (e.g. false)"
        ),
    )
    max_pages: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Document-mode cap from the beginning of a PDF; "
            "null uses the API maximum (e.g. 3)"
        ),
    )
    dpi: int | None = Field(
        default=None,
        ge=50,
        le=400,
        description=(
            "Document-mode PDF rasterization DPI; null "
            "preserves the server default (e.g. 250)"
        ),
    )
