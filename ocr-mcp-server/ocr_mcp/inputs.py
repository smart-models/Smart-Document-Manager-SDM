"""Bound and validate document sources before contacting the OCR API."""

from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ocr_mcp.config import Settings
from ocr_mcp.errors import ToolError
from ocr_mcp.formatting import ResponseFormat

RawString = Annotated[str, StringConstraints(strip_whitespace=False)]
# ZIP (OOXML/ODF) and OLE2 container magic; the OCR API inspects the container.
_OFFICE_MAGIC = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


class ToolInput(BaseModel):
    """Common wire format; unknown fields fail visibly."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="MCP presentation, independent of parse output (e.g. 'json')",
    )


class SourceInput(ToolInput):
    """Accept server-local or base64 input with an optional original filename."""

    file_path: RawString | None = Field(
        default=None,
        description=(
            "Absolute path of a file inside an input root listed by "
            "ocr_check_health (mcp.input_roots), on the MCP server's "
            "filesystem; spaces are preserved (e.g. '<input root>/scan.png')"
        ),
    )
    image_base64: str | None = Field(
        default=None,
        repr=False,
        description=(
            "Base64 image or image data URI; exclusive with file_path (e.g."
            " 'data:image/png;base64,iVBORw0KGgo=')"
        ),
    )
    filename: RawString | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Original document filename for archive naming; with file_path it "
            "defaults to the basename and an explicit value wins (e.g. 'invoice.pdf')"
        ),
    )


class ParseDocumentInput(SourceInput):
    """Configure full-document parsing with exactly one supplied document source."""

    pdf_base64: str | None = Field(
        default=None,
        repr=False,
        description=(
            "Base64 PDF or PDF data URI; supply exactly one source (e.g. "
            "'JVBERi0xLjcK')"
        ),
    )
    document_base64: str | None = Field(
        default=None,
        repr=False,
        description=(
            "Base64 document (PDF, image, Office .docx/.pptx/.xlsx/.doc/.ppt/"
            ".xls/.odt/.odp/.ods) or data URI; one source only (e.g. "
            "'UEsDBBQABgAIAAAAIQ')"
        ),
    )
    output: Literal["markdown", "json", "both"] = Field(
        default="both",
        description=(
            "Page content returned by OCR: markdown, blocks or both (e.g. 'both')"
        ),
    )
    max_pages: int | None = Field(
        default=None,
        ge=1,
        description="PDF page cap; omit for server OCR_MAX_PAGES, default 50 (e.g. 2)",
    )
    dpi: int | None = Field(
        default=None,
        ge=50,
        le=400,
        description=(
            "PDF raster DPI; omit to use tuned server default; ignored for "
            "images (e.g. 250)"
        ),
    )
    table_format: Literal["html", "markdown"] = Field(
        default="html",
        description=(
            "Table markup; 'markdown' converts every table to pipes, "
            "expanding merged cells into repeated values (e.g. 'markdown')"
        ),
    )
    include_images: bool = Field(
        default=False,
        description=(
            "Include actual figure crops as base64; increases response size"
            " (e.g. false)"
        ),
    )
    json_rag: bool = Field(
        default=False,
        description=(
            "Also return a structural RAG export alongside the parse result"
            " (e.g. false)"
        ),
    )


class RecognizeImageInput(SourceInput):
    """Configure one image element recognition task and its source."""

    task: Literal["ocr", "table", "formula", "chart", "spotting", "seal"] = Field(
        default="ocr",
        description="Element recognition task for a single image (e.g. 'formula')",
    )


def _read_file(raw_path: str, settings: Settings) -> bytes:
    """Read a literal path from trusted roots whose contents remain stable."""
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise ToolError("file_path must be absolute on the MCP server.")
        resolved = path.resolve(strict=True)
        if not any(resolved.is_relative_to(root) for root in settings.input_roots):
            raise ToolError(
                "File is outside MCP_INPUT_ROOTS. Use an allowed input directory, "
                "or base64 produced by a tool."
            )
        if not resolved.is_file():
            raise ToolError("file_path must identify a regular file.")
        with resolved.open("rb") as handle:
            data = handle.read(settings.max_file_bytes + 1)
    except (OSError, ToolError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ToolError)
            else "Cannot read file on the MCP server. Check the mounted input "
            "directory or use base64."
        )
        if raw_path != raw_path.strip():
            message += (
                " Leading and trailing spaces are preserved in file_path; "
                "remove accidental padding and try again."
            )
        raise ToolError(message) from exc
    return data


def prepare_source(params: SourceInput, settings: Settings) -> dict[str, str]:
    """Return one canonical JSON source, with no filesystem or URL output."""
    sources = {
        "file_path": params.file_path,
        "image_base64": getattr(params, "image_base64", None),
        "pdf_base64": getattr(params, "pdf_base64", None),
        "document_base64": getattr(params, "document_base64", None),
    }
    selected = [(name, value) for name, value in sources.items() if value is not None]
    if len(selected) != 1:
        raise ToolError(
            "Supply exactly one source: file_path, image_base64, or (for "
            "parsing) pdf_base64 or document_base64."
        )
    source, value = selected[0]
    if source == "file_path":
        data = _read_file(value, settings)
    else:
        # Models that strip strings (every tool but RAG) already do this; the
        # RAG model keeps raw strings, so a trailing newline must not fail there.
        encoded = value.strip()
        if encoded.startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or not header.endswith(";base64"):
                raise ToolError("Use a base64 data URI or plain base64, not a URL.")
        if len(encoded) > 4 * ((settings.max_file_bytes + 2) // 3):
            raise ToolError("Input exceeds MCP_MAX_FILE_BYTES; split or compress it.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ToolError(
                "Invalid base64 (invalid_base64). Encode the actual file bytes "
                "without whitespace."
            ) from exc
    if not data:
        raise ToolError(
            "Input is empty; supply a nonempty image, PDF or Office document."
        )
    if len(data) > settings.max_file_bytes:
        raise ToolError("Input exceeds MCP_MAX_FILE_BYTES; split or compress it.")
    header = data.lstrip(b" \t\n\r\v\f")
    if header.startswith(b"\xef\xbb\xbf"):
        header = header[3:].lstrip(b" \t\n\r\v\f")
    is_pdf = header.startswith(b"%PDF-")
    is_office = data.startswith(_OFFICE_MAGIC)
    if isinstance(params, RecognizeImageInput) and (is_pdf or is_office):
        raise ToolError(
            "Recognition accepts images only. Use ocr_parse_document for a PDF "
            "or Office document."
        )
    if source == "pdf_base64" and not is_pdf:
        raise ToolError("pdf_base64 must contain a PDF; use image_base64 for images.")
    key = "pdf_base64" if is_pdf else "document_base64" if is_office else "image_base64"
    result = {key: base64.b64encode(data).decode("ascii")}
    filename = params.filename
    if source == "file_path":
        filename = filename or Path(value).name
    if filename:
        result["filename"] = filename
    return result


async def source_body(params: SourceInput, settings: Settings) -> dict:
    """Keep bounded file IO and base64 conversion off the server event loop."""
    source = await asyncio.to_thread(prepare_source, params, settings)
    options = params.model_dump(
        exclude={
            "response_format",
            "file_path",
            "image_base64",
            "pdf_base64",
            "document_base64",
        },
        exclude_none=True,
    )
    return {**source, **options}
