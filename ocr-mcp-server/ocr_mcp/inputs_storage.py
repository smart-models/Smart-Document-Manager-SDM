"""Input models for storage browser tools."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ocr_mcp.inputs import RawString, ToolInput

ExactConfirm = RawString


class ListStorageArchivesInput(ToolInput):
    """Filter and paginate the archive listing using an opaque server cursor."""

    kind: Literal["parse", "ocr", "parse-chunks"] | None = Field(
        default=None, description="Filter by archive operation (e.g. 'parse')"
    )
    date: str | None = Field(
        default=None,
        description=(
            "Exact UTC date YYYY-MM-DD, exclusive with a range (e.g. '2026-10-18')"
        ),
    )
    date_from: str | None = Field(
        default=None,
        description="Inclusive UTC range start YYYY-MM-DD (e.g. '2026-10-01')",
    )
    date_to: str | None = Field(
        default=None,
        description="Inclusive UTC range end YYYY-MM-DD (e.g. '2026-10-18')",
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Opaque next_cursor from the previous page; keep filters "
            "unchanged (e.g. 'eyJ2IjoxfQ')"
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum archives in this page, from 1 to 50 (e.g. 20)",
    )


class GetStorageArchiveInput(ToolInput):
    """Select an archive by its exact server-returned prefix."""

    prefix: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Archive prefix copied verbatim from a listing, including "
            "trailing slash (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/')"
        ),
    )


class DownloadStorageFileInput(ToolInput):
    """Select an archived file for download or a bounded text/image preview."""

    key: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Artifact key copied from archive details (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/invoice.md')"
        ),
    )
    preview: bool = Field(
        default=False,
        description=(
            "true returns the whole file only when it is text/JSON up to 1 MiB or "
            "PNG up to 10 MiB (413 above, never a truncated view); false downloads "
            "any type up to the storage limit (e.g. false)"
        ),
    )


class DownloadStorageZipInput(ToolInput):
    """Select an archive for a complete ZIP download."""

    prefix: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Archive prefix to download as ZIP, copied from a listing (e.g."
            " 'invoice.pdf__20261018T120000000000Z__job123AB/')"
        ),
    )


class DeleteStorageArchiveInput(ToolInput):
    """Select an archive and require its exact job ID as deletion confirmation."""

    prefix: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Archive prefix selected for irreversible deletion (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/')"
        ),
    )
    confirm: ExactConfirm = Field(
        min_length=1,
        max_length=256,
        description=(
            "Exact job_id read from the same archive; whitespace and case "
            "are preserved (e.g. 'job123AB')"
        ),
    )


class DeleteStorageFileInput(ToolInput):
    """Select an artifact and require its exact relative name for deletion."""

    key: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Artifact key selected for irreversible deletion; never "
            "manifest.json (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/images/page-1.png')"
        ),
    )
    confirm: ExactConfirm = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Exact relative name from archive files, including directories;"
            " whitespace/case are preserved (e.g. 'images/page-1.png')"
        ),
    )
