"""Validate upstream response shapes without changing their public payloads."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

from ocr_mcp.errors import ToolError
from ocr_mcp.inputs_rag_storage import RagPlanEntryField

Nonnegative = Annotated[FiniteFloat, Field(ge=0)]
Box = Annotated[list[FiniteFloat], Field(min_length=4, max_length=4)]


class ResponseModel(BaseModel):
    """Validate known upstream fields strictly while retaining extension fields."""

    model_config = ConfigDict(strict=True, extra="allow")


class WarningPayload(ResponseModel):
    """Validate a public warning and optional diagnostic context."""

    code: str
    severity: str
    message: str
    target: dict | None = None
    details: dict | None = None


class BlockPayload(ResponseModel):
    """Validate OCR block text, geometry, layout type and optional identities."""

    type: str
    bbox: list[FiniteFloat]
    content: str
    order: int | None
    block_id: int | None = None
    group_id: int | None = None
    global_block_id: int | None = None
    global_group_id: int | None = None
    polygon: list[list[FiniteFloat]] | None = None


class PagePayload(ResponseModel):
    """Validate one page result or its isolated OCR failure."""

    page: int = Field(ge=1)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    markdown: str | None = None
    blocks: list[BlockPayload] | None = None
    images: list[str] | None = None
    image_names: list[str] | None = None
    error: str | None = None


class ParsePayload(ResponseModel):
    """Validate ordered document pages and OCR response metadata."""

    pages: list[PagePayload]
    model_name: str
    pipeline_version: str
    processing_time_ms: Nonnegative
    warnings: list[WarningPayload]


class OcrPayload(ResponseModel):
    """Validate element recognition text, optional boxes and response metadata."""

    result: str
    task: Literal["ocr", "table", "formula", "chart", "spotting", "seal"]
    boxes: list[Box] | None = None
    model_name: str
    processing_time_ms: Nonnegative
    warnings: list[WarningPayload]


class AutotunePayload(ResponseModel):
    """Validate the reported startup calibration action."""

    action: str


class HealthPayload(ResponseModel):
    """Validate API readiness, mode, model identity and backend reachability.

    ``mode`` and ``ocr_enabled`` default to the OCR mode so an older API that
    does not report them still validates; ``vllm="disabled"`` is the
    archive-only API's deliberate absence of a model server, not an outage.
    """

    status: Literal["healthy", "degraded"]
    mode: Literal["ocr", "archive_only"] = "ocr"
    vllm: Literal["reachable", "unreachable", "disabled"]
    ocr_enabled: bool = True
    model: str
    pipeline_version: str
    version: str
    autotune: AutotunePayload


class StatsPayload(ResponseModel):
    """Validate nonnegative queued and active request counts."""

    queue_size: Nonnegative
    active: Nonnegative


class StorageArchiveSummaryPayload(ResponseModel):
    """Validate an archive listing entry and its completeness indicators."""

    prefix: str
    job_id: str
    filename: str | None = None
    kind: Literal["parse", "ocr", "parse-chunks", "unknown"]
    created_at: str | None
    object_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    partial_ocr: bool
    unprocessed_pages: list[int] = Field(default_factory=list)
    incomplete: bool = False
    input_key: str | None = None


class StorageArchiveListPayload(ResponseModel):
    """Validate archive entries, pagination and the skipped-entry count."""

    items: list[StorageArchiveSummaryPayload]
    next_cursor: str | None
    skipped_archives: int = Field(default=0, ge=0)


class StorageFilePayload(ResponseModel):
    """Validate an archived object key, display name, MIME type and size."""

    key: str
    name: str
    size: int = Field(ge=0)
    content_type: str


class StorageArchiveDetailPayload(ResponseModel):
    """Validate an archive manifest and its file inventory."""

    prefix: str
    manifest: dict
    files: list[StorageFilePayload]


class StorageDeletePayload(ResponseModel):
    """Validate the number of objects removed by a deletion request."""

    deleted_objects: int = Field(ge=0)


class StorageRagRefinePayload(ResponseModel):
    """Validate the newly archived refinement file and chunk count."""

    prefix: str
    file: StorageFilePayload
    chunk_count: int = Field(ge=0)


SafeInteger = Annotated[int, Field(ge=0, le=9007199254740991)]
DiagnosticCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class SplitLocationPayload(ResponseModel):
    """Validate a source block address and optional character interval."""

    source_key: Annotated[str, Field(pattern=r"^p[1-9][0-9]*:b[0-9]+$")]
    start: SafeInteger | None
    end: SafeInteger | None


class SplitParameterPayload(ResponseModel):
    """Validate one named, finite diagnostic parameter."""

    name: DiagnosticCode
    value: str | FiniteFloat | bool | None


class SplitNotePayload(ResponseModel):
    """Validate a split diagnostic with source targets and parameters."""

    code: DiagnosticCode
    severity: Literal["info", "warning"]
    targets: list[SplitLocationPayload]
    parameters: list[SplitParameterPayload]


class SplitFragmentPayload(ResponseModel):
    """Validate proposed plan entries, token measurements and split status."""

    entries: list[RagPlanEntryField]
    token_count: SafeInteger | None = None
    status: Literal["within", "above", "below", "unknown"]
    notes: list[SplitNotePayload] = Field(default_factory=list)
    member_token_counts: list[SafeInteger | None] | None = None


class StorageRagSplitPayload(ResponseModel):
    """Validate read-only split proposals and retained upstream warnings."""

    fragments: list[SplitFragmentPayload]
    warnings: list[WarningPayload] = Field(default_factory=list)


STORAGE_RESPONSE_MODELS = {
    ("GET", "/storage/archives"): StorageArchiveListPayload,
    ("GET", "/storage/archive"): StorageArchiveDetailPayload,
    ("DELETE", "/storage/archive"): StorageDeletePayload,
    ("DELETE", "/storage/file"): StorageDeletePayload,
    ("POST", "/storage/rag/refine"): StorageRagRefinePayload,
    ("POST", "/storage/rag/split"): StorageRagSplitPayload,
}


def validate_payload(payload: object, path: str, *, method: str = "GET") -> dict:
    """Return the unchanged object or a safe diagnostic for invalid data."""
    if path in {"/chunk", "/parse/chunks"}:
        from ocr_mcp.rag_contract import RAG_VALIDATOR, RagResponse

        if not RAG_VALIDATOR.is_valid(payload):
            raise ToolError(
                f"Unexpected {path} payload: invalid RAG schema. Check API version."
            )
        RagResponse.model_validate(payload)
        return payload
    models = {
        "/parse": ParsePayload,
        "/ocr": OcrPayload,
        "/health": HealthPayload,
        "/stats": StatsPayload,
    }
    model = STORAGE_RESPONSE_MODELS.get((method.upper(), path)) or models[path]
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        raise ToolError(
            f"Unexpected {path} payload: required fields are missing or invalid. "
            "Check the OCR API version and upstream configuration."
        ) from exc
    return payload
