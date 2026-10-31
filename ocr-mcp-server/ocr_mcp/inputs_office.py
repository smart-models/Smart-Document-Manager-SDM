"""Input model for Office-to-PDF preview."""

from __future__ import annotations

from pydantic import Field

from ocr_mcp.inputs import RawString, ToolInput


class ConvertOfficeToPdfInput(ToolInput):
    """Identify Office input for bounded conversion to a PDF preview."""

    file_path: RawString | None = Field(
        default=None,
        description=(
            "Absolute Office file inside an input root listed by "
            "ocr_check_health; exclusive with document_base64 (e.g. "
            "'<input root>/report.docx')"
        ),
    )
    document_base64: str | None = Field(
        default=None,
        repr=False,
        description=(
            "Actual Office bytes as base64 or a base64 data URI; exclusive "
            "with file_path (e.g. "
            "'data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,UEsDBA==')"
        ),
    )
    filename: RawString | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Original filename for supplied bytes; file_path uses its "
            "basename (e.g. 'report.docx')"
        ),
    )
