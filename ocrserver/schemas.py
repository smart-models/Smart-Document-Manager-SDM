"""Pydantic models shared by endpoints, pipeline backend, and autotune runner."""

from __future__ import annotations

import re
from uuid import UUID
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)

MODEL_NAME = "PaddlePaddle/PaddleOCR-VL-1.6"
PIPELINE_VERSION = "v1.6"
ELEMENT_TASKS = ("ocr", "table", "formula", "chart", "spotting", "seal")


class ApiWarning(BaseModel):
    """Describe a recoverable condition with optional target and diagnostic details."""

    code: str
    severity: str = "warning"
    message: str
    target: dict | None = None
    details: dict | None = None


class StorageArchiveSummary(BaseModel):
    """Summarize one complete or abandoned archive without loading its files."""

    prefix: str
    job_id: str
    filename: str | None = None
    # "unknown" only for incomplete archives whose manifest cannot be read.
    kind: Literal["parse", "ocr", "parse-chunks", "unknown"]
    created_at: str | None
    object_count: int
    total_bytes: int
    partial_ocr: bool
    # Pages never reached because the request budget ran out or it was cancelled.
    unprocessed_pages: list[int] = Field(default_factory=list)
    # Abandoned write (no complete manifest): listed so it can be deleted.
    incomplete: bool = False
    # Key of the archived original upload (input.<ext>), when one was saved.
    input_key: str | None = None


class StorageArchiveList(BaseModel):
    """Return a cursor-paginated archive listing and skipped-archive count."""

    items: list[StorageArchiveSummary]
    next_cursor: str | None
    skipped_archives: int = 0
    # Archives in the date range, on every page; null when filtered by type
    # (the current layout keeps it in each manifest) or past the listing bound.
    total: int | None = None


class StorageFile(BaseModel):
    """Describe an archived object by storage key, display name, size and MIME type."""

    key: str
    name: str
    size: int
    content_type: str


class StorageArchiveDetail(BaseModel):
    """Return an archive manifest together with its browsable object inventory."""

    prefix: str
    manifest: dict
    files: list[StorageFile]


class StorageDeleteRequest(BaseModel):
    """Require an explicit confirmation string for archive or artifact deletion."""

    model_config = ConfigDict(extra="forbid")
    confirm: str


class StorageDeleteResponse(BaseModel):
    """Report the number of storage objects deleted successfully."""

    deleted_objects: int


RagCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


def normalize_entry(
    value: object,
) -> int | tuple[int, int, int] | tuple[int, int, int, str]:
    """Turn one raw plan entry into its validated internal representation.

    An integer is a whole chunk; ``[index, start, end]`` slices a chunk and
    ``[index, start, end, "repeat"]`` marks a repeated framing span. This is
    the single place that normalizes entries, so it also accepts the tuples
    it has already produced. Malformed shapes, booleans, non-integer offsets,
    negative indices and empty ranges are rejected with ``ValueError``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, list, tuple)):
        raise ValueError("plan entry must be an integer or an entry list")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("plan entry index must not be negative")
        return value
    if len(value) == 3:
        index, start, end = value
        repeat = None
    elif len(value) == 4:
        index, start, end, repeat = value
        if repeat != "repeat":
            raise ValueError("plan entry repeat flag must be 'repeat'")
    else:
        raise ValueError("plan entry must have three or four elements")
    for name, item in (("index", index), ("start", start), ("end", end)):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"plan entry {name} must be an integer")
        if item < 0:
            raise ValueError(f"plan entry {name} must not be negative")
    if start >= end:
        raise ValueError("plan entry range must not be empty")
    if repeat is None:
        return index, start, end
    return index, start, end, repeat


RagPlanEntryValue = int | tuple[int, int, int] | tuple[int, int, int, str]
RagPlanEntryModel = Annotated[RagPlanEntryValue, BeforeValidator(normalize_entry)]


class RagRefinementOptions(BaseModel):
    """Configure refinement token bounds, page boundaries and protected blocks."""

    model_config = ConfigDict(extra="forbid")
    min_tokens: int = Field(ge=1, le=9007199254740991, strict=True)
    max_tokens: int = Field(ge=1, le=9007199254740991, strict=True)
    page_boundary: StrictBool = False
    keep_tables_together: StrictBool = True
    keep_code_blocks_together: StrictBool = True


class StorageRagRefineRequest(BaseModel):
    """Identify an immutable RAG source and an explicitly grouped refinement plan."""

    model_config = ConfigDict(extra="forbid")
    source_key: str = Field(max_length=1024)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_id: UUID
    created_at: AwareDatetime
    options: RagRefinementOptions
    groups: list[list[RagPlanEntryModel]] = Field(min_length=1, max_length=100000)
    overrides: list[list[RagCode]] | None = None
    counts: list[list[int | None]] | None = None

    @model_validator(mode="after")
    def overrides_match_groups(self):
        """Reject override or token-count lists without one entry per group."""
        if self.overrides is not None and len(self.overrides) != len(self.groups):
            raise ValueError("overrides must carry one list per group")
        if self.counts is not None and len(self.counts) != len(self.groups):
            raise ValueError("counts must carry one list per group")
        return self


class StorageRagRefineResponse(BaseModel):
    """Identify the newly archived refinement artifact and its chunk count."""

    prefix: str
    file: StorageFile
    chunk_count: int


class StorageRagSplitRequest(BaseModel):
    """Read-only split call: it identifies a row by its plan entries."""

    model_config = ConfigDict(extra="forbid")
    source_key: str = Field(max_length=1024)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    options: RagRefinementOptions
    group: list[RagPlanEntryModel] = Field(min_length=1, max_length=100000)


def make_warning(
    code: str,
    message: str,
    severity: str = "warning",
    target: dict | None = None,
    details: dict | None = None,
) -> ApiWarning:
    """Build a structured warning without modifying its target or details."""
    return ApiWarning(
        code=code, severity=severity, message=message, target=target, details=details
    )


class Block(BaseModel):
    # Non-exhaustive type enum by design: unknown pipeline labels pass through.
    """Describe one OCR layout region with text, geometry and upstream identities."""

    type: str
    bbox: list[float]
    content: str
    order: int | None
    block_id: int | None = None
    group_id: int | None = None
    global_block_id: int | None = None
    global_group_id: int | None = None
    polygon: list | None = None
    content_tokens: int | None = Field(
        default=None,
        description=(
            "Local tokens of 'content' (model tokenizer, special tokens excluded); "
            "empty content is 0. Null when token counting is disabled or unavailable."
        ),
    )


class PageResult(BaseModel):
    """Collect one page's OCR output or its isolated processing failure."""

    page: int
    width: int | None = None
    height: int | None = None
    markdown: str | None = None
    blocks: list[Block] | None = None
    images: list[str] | None = None
    image_names: list[str] | None = None
    error: str | None = None


class TokenUsage(BaseModel):
    """Tokens of parsed block content; not model inference usage."""

    tokenizer: str = Field(description="Model repository and revision used.")
    content_tokens: int = Field(description="Sum of block content tokens.")
    blocks: int = Field(description="Blocks covered (empty blocks add 0).")


class ParseResponse(BaseModel):
    """Return ordered page results, warnings and optional content-token metadata."""

    pages: list[PageResult]
    model_name: str = MODEL_NAME
    pipeline_version: str = PIPELINE_VERSION
    processing_time_ms: float
    warnings: list[ApiWarning] = Field(default_factory=list)
    usage: TokenUsage | None = Field(
        default=None,
        description="Content token totals; null when counting is unavailable.",
    )
    json_rag: dict | None = Field(
        default=None,
        description=(
            "Qdrant bulk-ingest file {chunks, metadata}; null unless json_rag "
            "was requested or when building it failed (json_rag_failed)."
        ),
    )


class OcrResponse(BaseModel):
    """Return a single element task's text, spotting boxes and processing metadata."""

    result: str
    task: str
    boxes: list[list[float]] | None = Field(
        default=None,
        description=(
            "Spotting boxes [x1,y1,x2,y2]. LOC quadrilaterals become "
            "axis-aligned envelopes in normalized 0-1000 units, not image pixels. "
            "Legacy numeric rectangles retain their original coordinate values."
        ),
    )
    model_name: str = MODEL_NAME
    processing_time_ms: float
    warnings: list[ApiWarning] = Field(default_factory=list)


class ErrorBody(BaseModel):
    """Expose a stable machine-readable error code and a public explanation."""

    code: str
    message: str


class ParseJsonRequest(BaseModel):
    """Document JSON upload contract; one source is required at runtime."""

    image_base64: str | None = None
    pdf_base64: str | None = None
    document_base64: str | None = None
    output: Literal["markdown", "json", "both"] = "both"
    max_pages: int | None = Field(default=None, ge=1)
    dpi: int | None = Field(default=None, ge=50, le=400)
    table_format: Literal["html", "markdown"] = "html"
    include_images: bool = False
    json_rag: bool = False
    filename: str | None = Field(default=None, max_length=1024)
    progress_id: str | None = Field(
        default=None,
        description="Canonical UUID to poll at /parse/progress/{progress_id}",
    )


class OcrJsonRequest(BaseModel):
    """Focused recognition of a single-frame image; use /parse for multiple frames."""

    image_base64: str | None = None
    filename: str | None = Field(default=None, max_length=1024)
    task: Literal["ocr", "table", "formula", "chart", "spotting", "seal"] = "ocr"


def upload_openapi(model: type[BaseModel], *, max_pages: int) -> dict:
    """Describe both accepted bodies without hiding raw-request handlers."""
    schema = model.model_json_schema()
    if model is ParseJsonRequest:
        alternatives = schema["properties"]["max_pages"].get("anyOf", [])
        integer = next(
            (node for node in alternatives if node.get("type") == "integer"), None
        )
        if integer is None:
            raise ValueError("max_pages schema is missing its integer alternative")
        integer["maximum"] = max_pages
    schema["oneOf"] = _source_choices(schema["properties"])
    multipart = {
        **schema,
        "properties": {
            **schema["properties"],
            "file": {"type": "string", "format": "binary"},
        },
    }
    if model is ParseJsonRequest:
        multipart["properties"]["progress_id"] = {"type": "string", "format": "uuid"}
    multipart.pop("required", None)
    multipart["oneOf"] = _source_choices(multipart["properties"])
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": schema},
                "multipart/form-data": {"schema": multipart},
            },
        }
    }


def _source_choices(properties: dict) -> list[dict]:
    """Exactly one useful source, matching null/blank handling in handlers."""
    return [
        {
            "required": [name],
            "properties": {
                name: {
                    "type": "string",
                    **({"pattern": r"\S"} if name != "file" else {}),
                }
            },
        }
        for name in properties
        if name in {"file", "image_base64", "pdf_base64", "document_base64"}
    ]


# RAG models deliberately do not change the legacy OCR coercion/defaults above.
JSON_SAFE_INTEGER = 9007199254740991
RagInteger = Annotated[int, Field(ge=0, le=JSON_SAFE_INTEGER)]
RagPositive = Annotated[int, Field(ge=1, le=JSON_SAFE_INTEGER)]
RagNonempty = Annotated[str, Field(min_length=1)]
RagBox = Annotated[list[FiniteFloat], Field(min_length=4, max_length=4)]
RagPoint = Annotated[list[FiniteFloat], Field(min_length=2, max_length=2)]
RagPolygon = Annotated[list[RagPoint], Field(min_length=3)]

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
REQUIRED_CHUNK_FIELDS = ("id", "text", "token_count")


class RagModel(BaseModel):
    """Closed, strict RAG value objects; legacy OCR models stay unchanged."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class ChunkOptions(RagModel):
    """Resolved user choices shared by REST, MCP and exported metadata."""

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
            "Reject protected content that cannot meet the budget rather than "
            "silently overflow (e.g. true)"
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
            "Keep code blocks whole unless splitting was explicitly requested "
            "(e.g. true)"
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
            "Keep page-margin content as metadata or include it as text (e.g. "
            "'metadata')"
        ),
    )
    table_header_policy: Literal["declared", "first_row", "none"] = Field(
        default="declared",
        description=(
            "Use declared headers, assume a first row, or repeat none when "
            "splitting tables (e.g. 'declared')"
        ),
    )
    metadata_fields: list[str] = Field(
        default_factory=lambda: list(OPTIONAL_CHUNK_METADATA_FIELDS),
        description=(
            "Optional chunk metadata to emit; id, text and token_count are "
            "always present (e.g. ['section_path','relations'])"
        ),
    )

    @field_validator("metadata_fields")
    @classmethod
    def validate_metadata_fields(cls, value):
        """Reject invalid selections and return fields in canonical metadata order."""
        if len(value) != len(set(value)):
            raise ValueError("metadata_fields must not contain duplicates")
        unknown = sorted(set(value) - set(OPTIONAL_CHUNK_METADATA_FIELDS))
        if unknown:
            raise ValueError(f"unknown metadata_fields: {', '.join(unknown)}")
        forbidden = sorted(set(value) & set(REQUIRED_CHUNK_FIELDS))
        if forbidden:
            raise ValueError(
                "required fields cannot be selected: " + ", ".join(forbidden)
            )
        order = {
            name: index for index, name in enumerate(OPTIONAL_CHUNK_METADATA_FIELDS)
        }
        return sorted(value, key=order.__getitem__)

    @model_validator(mode="after")
    def validate_overlap(self):
        """Require overlap to leave space within the total chunk token budget."""
        if self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens must be less than max_tokens")
        return self

    def model_dump(self, *args, **kwargs):
        """Preserve historical option envelopes when metadata selection was omitted."""
        result = super().model_dump(*args, **kwargs)
        # Keep historical option envelopes byte-compatible when the caller did
        # not select metadata explicitly; the effective default remains all
        # optional fields and is still applied by the engine.
        if "metadata_fields" not in self.model_fields_set:
            result.pop("metadata_fields", None)
        return result

    @model_serializer(mode="wrap")
    def serialize_options(self, handler):
        """Omit implicit metadata defaults from nested serialized option envelopes."""
        result = handler(self)
        if "metadata_fields" not in self.model_fields_set:
            result.pop("metadata_fields", None)
        return result


class ChunkBlock(RagModel):
    """Structured input with optional geometry and historical token counts."""

    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)
    type: RagNonempty
    content: str
    bbox: RagBox | None = None
    order: int | None = Field(default=None, ge=0, le=JSON_SAFE_INTEGER)
    block_id: RagInteger | None = None
    group_id: RagInteger | None = None
    global_block_id: RagInteger | None = None
    global_group_id: RagInteger | None = None
    polygon: RagPolygon | None = None
    content_tokens: RagInteger | None = None

    @field_validator("bbox", "polygon", mode="before")
    @classmethod
    def empty_geometry_is_missing(cls, value):
        """Normalize empty geometry arrays to missing geometry."""
        return None if value == [] else value


class ChunkPage(RagModel):
    """Page numbers are checked by the engine for useful domain errors."""

    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)
    page: int = Field(ge=-JSON_SAFE_INTEGER, le=JSON_SAFE_INTEGER)
    width: RagPositive | None = None
    height: RagPositive | None = None
    blocks: list[ChunkBlock] | None = None
    markdown: str | None = None
    images: list[str] | None = None
    image_names: list[str] | None = None
    error: str | None = None


class ParsedDocument(RagModel):
    """The reusable structure from /parse; timings are never required."""

    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)
    pages: list[ChunkPage]
    warnings: list[ApiWarning] = Field(default_factory=list)
    usage: TokenUsage | None = None


_GRANDFATHERED_LANGUAGE_TAGS = frozenset(
    "en-gb-oed i-ami i-bnn i-default i-enochian i-hak i-klingon i-lux "
    "i-mingo i-navajo i-pwn i-tao i-tay i-tsu sgn-be-fr sgn-be-nl sgn-ch-de "
    "art-lojban cel-gaulish no-bok no-nyn zh-guoyu zh-hakka zh-min zh-min-nan "
    "zh-xiang".split()
)
_LANGUAGE_TAG = re.compile(
    r"(?:(?:[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}|[A-Za-z]{4}|[A-Za-z]{5,8})"
    r"(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-x(?:-[A-Za-z0-9]{1,8})+)?|x(?:-[A-Za-z0-9]{1,8})+)",
    re.IGNORECASE,
)


class ChunkLabels(RagModel):
    """Caller labels affect metadata only; a language is never inferred."""

    source_id: RagNonempty | None = None
    lang: str | None = None

    @field_validator("lang")
    @classmethod
    def valid_language_tag(cls, value):
        """Validate BCP 47 syntax without altering the supplied language label."""
        if value is None or value.lower() in _GRANDFATHERED_LANGUAGE_TAGS:
            return value
        if not _LANGUAGE_TAG.fullmatch(value):
            raise ValueError("lang must be a syntactically valid BCP 47 tag")
        if value.lower().startswith("x-"):
            return value
        extensions = []
        variants = []
        for index, part in enumerate(value.lower().split("-")):
            if index and part == "x":
                break
            if index and len(part) == 1:
                if part in extensions:
                    raise ValueError("lang cannot repeat an extension singleton")
                extensions.append(part)
            elif (
                not extensions
                and index
                and (len(part) >= 5 or (len(part) == 4 and part[0].isdigit()))
            ):
                if part in variants:
                    raise ValueError("lang cannot repeat a variant")
                variants.append(part)
        return value


class ChunkRequest(ChunkLabels):
    """Combine a parsed document with explicit chunk options and truncation policy."""

    parsed_document: ParsedDocument
    options: ChunkOptions
    accept_truncated: bool = False


class ParseChunksJsonRequest(ChunkLabels):
    """Dedicated new parse route; structural OCR choices are server-owned."""

    image_base64: str | None = None
    pdf_base64: str | None = None
    document_base64: str | None = None
    filename: str | None = Field(default=None, max_length=1024)
    options: ChunkOptions
    max_pages: RagPositive | None = None
    dpi: int | None = Field(default=None, ge=50, le=400)


RagSourceKey = Annotated[str, Field(pattern=r"^p[1-9][0-9]*:b[0-9]+$")]
RagHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RagSectionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9:._-]+$")]


class RagOutputModel(RagModel):
    """Omit optional absent keys while retaining every required null value."""

    @model_validator(mode="after")
    def absent_is_not_explicit_null(self):
        """Reject explicit null values for fields whose contract requires omission."""
        for name, field in type(self).model_fields.items():
            if (
                not field.is_required()
                and field.default is None
                and name in self.model_fields_set
                and getattr(self, name) is None
            ):
                raise ValueError(f"{name} must be omitted rather than null")
        return self

    @model_serializer(mode="wrap")
    def omit_optional_absences(self, handler):
        """Remove absent optional fields using their serialization aliases."""
        result = handler(self)
        for name, field in type(self).model_fields.items():
            if (
                not field.is_required()
                and field.default is None
                and getattr(self, name) is None
            ):
                result.pop(field.serialization_alias or field.alias or name, None)
        return result


class RagIdentifiers(RagOutputModel):
    """Preserve available pipeline block and group identities in provenance."""

    block_id: RagInteger | None = None
    group_id: RagInteger | None = None
    global_block_id: RagInteger | None = None
    global_group_id: RagInteger | None = None


class RagSource(RagOutputModel):
    """Identify an original OCR block and retain its geometry and upstream IDs."""

    source_key: RagSourceKey
    page: RagPositive
    block_index: RagInteger
    type: RagNonempty
    bbox: RagBox | None
    polygon: RagPolygon | None
    identifiers: RagIdentifiers | None = None


class RagTablePosition(RagOutputModel):
    """Locate a rendered cell within a logical table grid and header policy."""

    row: RagInteger | None
    column: RagInteger | None
    rowspan: RagPositive | None
    colspan: RagPositive | None
    representation: Literal["html", "markdown", "cells"]
    header_method: Literal["declared", "first_row", "none"]


class RagSourceSpan(RagSource):
    """Map a primary output interval to original block text and table coordinates."""

    start: RagInteger
    end: RagInteger
    text_start: RagInteger
    text_end: RagInteger
    role: Literal["body", "section_title", "table_cell", "code", "formula"]
    table: RagTablePosition | None = None


class RagContextSpan(RagOutputModel):
    """Map repeated or generated context to output and optional source intervals."""

    source_key: RagSourceKey | None
    page: RagPositive | None
    block_index: RagInteger | None
    type: RagNonempty | None
    bbox: RagBox | None
    polygon: RagPolygon | None
    start: RagInteger | None
    end: RagInteger | None
    text_start: RagInteger
    text_end: RagInteger
    role: Literal[
        "section_title",
        "overlap",
        "table_header",
        "separator",
        "table_coordinates",
        "code_fence",
    ]
    table: RagTablePosition | None = None

    @model_validator(mode="after")
    def validate_context_source(self):
        """Require complete observed provenance or source-free generated context."""
        values = (self.page, self.block_index, self.type, self.start, self.end)
        if self.source_key is None:
            if any(value is not None for value in (*values, self.bbox, self.polygon)):
                raise ValueError("generated context cannot have source coordinates")
        elif any(value is None for value in values):
            raise ValueError("observed context requires a complete source interval")
        return self


class RagLocation(RagOutputModel):
    """Identify a source block and optional character interval for diagnostics."""

    source_key: RagSourceKey
    start: RagInteger | None
    end: RagInteger | None


class RagParameter(RagOutputModel):
    """Represent one named diagnostic parameter with its JSON-safe value."""

    name: RagCode
    value: str | FiniteFloat | bool | None


class RagDiagnostic(RagOutputModel):
    """Describe a stable diagnostic code, severity, source targets and parameters."""

    code: RagCode
    severity: Literal["info", "warning"]
    targets: list[RagLocation]
    parameters: list[RagParameter]


class RagSplitNote(RagDiagnostic):
    """Diagnostic note returned by the split endpoint (frozen shape)."""


# Single source of truth for split note diagnostics, mirrored by Tasks 4, 5, 7.
RAG_SPLIT_NOTE_SEVERITY: dict[str, Literal["info", "warning"]] = {
    "table_split": "warning",
    "code_split": "warning",
    "grapheme_split": "warning",
    "math_atomic": "warning",
    "page_boundary_forced": "warning",
    "document_forced": "warning",
    "failed_gap_forced": "warning",
    "above_maximum_fragment": "info",
    "below_minimum_fragment": "info",
    "token_count_unknown": "warning",
    "search_budget_exhausted": "info",
}

# Extra parameter (beyond ``detail``) each note code carries, if any.
RAG_SPLIT_NOTE_PARAMETER: dict[str, str | None] = {
    "table_split": "rows",
    "code_split": "lines",
    "grapheme_split": "tokens",
    "math_atomic": "tokens",
    "page_boundary_forced": "pages",
    "document_forced": "documents",
    "failed_gap_forced": "pages",
    "above_maximum_fragment": "tokens",
    "below_minimum_fragment": "tokens",
    "token_count_unknown": None,
    "search_budget_exhausted": "evaluations",
}


class RagSplitFragmentModel(BaseModel):
    """Return one proposed split fragment with plan entries and token evidence."""

    model_config = ConfigDict(extra="forbid")
    entries: list[RagPlanEntryModel]
    token_count: RagInteger | None = None
    status: Literal["within", "above", "below", "unknown"]
    notes: list[RagSplitNote] = Field(default_factory=list)
    member_token_counts: list[RagInteger | None] | None = Field(
        default=None,
        description=(
            "Measured contributions of distinct source chunks in entry order; "
            "their sum is token_count."
        ),
    )


class StorageRagSplitResponse(BaseModel):
    """Return proposed split fragments and warnings without writing an archive."""

    fragments: list[RagSplitFragmentModel]
    warnings: list[ApiWarning] = Field(default_factory=list)


class RagAsset(RagOutputModel):
    """Reference a source asset, optional content hash and associated relations."""

    source: RagSource
    reference: str | None
    sha256: RagHash | None
    relation_ids: list[RagHash]


class RagMarginal(RagOutputModel):
    """Retain marginal OCR text separately from primary retrieval content."""

    text: str
    source: RagSource


class RagPageContext(RagOutputModel):
    """Group page headers, footers, labels and marginal assets as context."""

    page: RagPositive
    headers: list[RagMarginal]
    footers: list[RagMarginal]
    page_labels: list[RagMarginal]
    marginal_assets: list[RagAsset]


class RagRelation(RagOutputModel):
    """Describe a resolved or candidate relation between original OCR blocks."""

    id: RagHash
    type: Literal[
        "formula_number",
        "caption_object",
        "footnote_reference",
        "table_continuation",
        "inline_formula_context",
    ]
    from_: RagLocation = Field(alias="from")
    to: RagLocation | None
    method: Literal["explicit", "geometry", "none"]
    status: Literal["same_chunk", "across_chunks", "cross_section", "unresolved"]
    candidate_targets: list[RagLocation]


class RagSection(RagOutputModel):
    """Identify a heading-delimited document section by stable ID and title."""

    id: RagSectionId
    title: str | None


class RagTokenizer(RagOutputModel):
    """Record the exact tokenizer snapshot, file hash and library version."""

    repo: Literal["PaddlePaddle/PaddleOCR-VL-1.6"]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    sha256: RagHash
    library_version: RagNonempty


class RagScope(RagOutputModel):
    """Describe selected pages and whether document processing was complete."""

    kind: Literal["provided_document", "complete_document", "page_prefix"]
    origin: Literal[
        "provided_json",
        "full_parse",
        "explicit_max_pages",
        "accepted_upstream_truncation",
    ]
    selected_pages: Annotated[list[RagPositive], Field(min_length=1)]
    requested_max_pages: RagPositive | None
    total_pages: RagPositive | None

    @model_validator(mode="after")
    def validate_scope_origin(self):
        """Reject contradictory completeness claims and noncanonical page selections."""
        if self.kind == "provided_document" and (
            self.origin != "provided_json"
            or self.requested_max_pages is not None
            or self.total_pages is not None
        ):
            raise ValueError("provided JSON cannot claim original completeness")
        if self.kind == "complete_document" and (
            self.origin != "full_parse" or self.total_pages is None
        ):
            raise ValueError("complete scope requires full parse and total pages")
        if self.kind == "page_prefix" and self.origin not in {
            "explicit_max_pages",
            "accepted_upstream_truncation",
        }:
            raise ValueError("page prefix requires explicit user acceptance")
        if self.selected_pages != sorted(set(self.selected_pages)):
            raise ValueError("selected_pages must be ascending and unique")
        return self


class RagUnassignedSource(RagOutputModel):
    """Retain source content and references that have no primary chunk assignment."""

    source: RagSource
    content: str
    reference: str | None


class RagOrderSummary(RagOutputModel):
    """Summarize input block ranks, missing ranks and conflicting page ordering."""

    pages: RagInteger
    blocks: RagInteger
    ranked_blocks: RagInteger
    missing_ranks: RagInteger
    conflicting_pages: list[RagPositive]


class RagAlgorithmProfile(RagOutputModel):
    """Record deterministic packing and overlap candidate bounds for the chunker."""

    name: Literal["structural-dp-v1.1"]
    max_reach: Literal[128]
    max_candidates: Literal[16]
    overlap_candidates: Literal[16]


class RagDocumentContext(RagOutputModel):
    """Collect document titles, scope, ordering, policy and global diagnostics."""

    titles: list[RagMarginal]
    scope: RagScope
    chunking_options: ChunkOptions
    order_summary: RagOrderSummary
    algorithm_profile: RagAlgorithmProfile
    unassigned_sources: list[RagUnassignedSource]
    diagnostics: list[RagDiagnostic]


class RagChunk(RagOutputModel):
    """Return retrieval text, exact token counts, provenance and chunking metadata."""

    id: RagInteger
    text: RagNonempty
    token_count: RagPositive
    page: RagPositive | None = None
    pages: Annotated[list[RagPositive], Field(min_length=1)] = Field(
        default_factory=list
    )
    section_id: RagSectionId | None = None
    # A section can be explicitly null in the canonical contract; a missing
    # section is still accepted when clients project metadata fields.
    section: str | None = Field(default_factory=lambda: None)
    section_path: list[RagSection] = Field(default_factory=list)
    block_types: Annotated[list[RagNonempty], Field(min_length=1)] = Field(
        default_factory=list
    )
    source_spans: Annotated[list[RagSourceSpan], Field(min_length=1)] = Field(
        default_factory=list
    )
    context_spans: list[RagContextSpan] = Field(default_factory=list)
    page_context: list[RagPageContext] = Field(default_factory=list)
    assets: list[RagAsset] = Field(default_factory=list)
    relations: list[RagRelation] = Field(default_factory=list)
    overflow: bool = False
    overflow_tokens: RagInteger = 0
    overflow_reason: Literal["protected_table", "protected_code"] | None = None
    overlap_token_count: RagInteger = 0
    source_fingerprint: RagHash | None = None
    tokenizer: RagTokenizer | None = None
    chunker_version: Literal["structural-dp-v1.1"] | None = None
    policy_hash: RagHash | None = None
    source_id: RagNonempty | None = None
    lang: Annotated[str, Field(pattern=r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")] | None = (
        None
    )
    diagnostics: list[RagDiagnostic] = Field(default_factory=list)
    document_context: RagDocumentContext | None = None

    @model_validator(mode="after")
    def validate_chunk_metadata(self):
        """Enforce context ownership, overflow rules and unique page metadata."""
        if self.document_context is not None and self.id != 0:
            raise ValueError("document_context belongs only to chunk zero")
        if self.overflow:
            if (
                self.overflow_reason is None
                or self.overflow_tokens < 1
                or self.overlap_token_count != 0
            ):
                raise ValueError(
                    "overflow requires a reason, positive excess and no overlap"
                )
        elif self.overflow_tokens or self.overflow_reason is not None:
            raise ValueError("ordinary chunks cannot declare overflow")
        if self.pages and self.pages != sorted(set(self.pages)):
            raise ValueError("pages must be ascending and unique")
        if self.block_types and len(self.block_types) != len(set(self.block_types)):
            raise ValueError("block_types must be unique")
        return self


class RagResponse(RagOutputModel):
    """The entire downloadable RAG document, with no response envelope."""

    chunks: Annotated[list[RagChunk], Field(min_length=1)]

    @model_validator(mode="after")
    def contiguous_chunks(self):
        """Require contiguous chunk IDs and shared document and policy identities."""
        if any(chunk.id != index for index, chunk in enumerate(self.chunks)):
            raise ValueError("chunk ids must be contiguous from zero")
        common = (
            "source_fingerprint",
            "policy_hash",
            "tokenizer",
            "chunker_version",
            "source_id",
            "lang",
        )
        first = self.chunks[0]
        for field in common:
            present = [field in chunk.model_fields_set for chunk in self.chunks]
            if any(present) and (
                not all(present)
                or any(
                    getattr(chunk, field) != getattr(first, field)
                    for chunk in self.chunks[1:]
                )
            ):
                raise ValueError("all chunks must share document and policy identities")
        return self
