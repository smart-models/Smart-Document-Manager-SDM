"""Exact, local deletion guards for the API's current and historical key layouts."""

from __future__ import annotations

from datetime import date, datetime
import re

from ocr_mcp.errors import ToolError

# Keep these HTTP-boundary rules aligned with ocrserver.archive_names through tests.
_CURRENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}__"
    r"(?P<stamp>\d{8}T\d{12}Z)__(?P<job>[A-Za-z0-9_-]{8})/"
)
_LEGACY = re.compile(
    r"(?:ocr|parse|parse-chunks)/(?P<year>\d{4})/"
    r"(?P<month>\d{2})/(?P<day>\d{2})/(?P<job>[0-9a-f]{32})/"
)
_ARTIFACT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._/-]*")


def _address(value: str) -> tuple[str, str]:
    for pattern in (_CURRENT, _LEGACY):
        match = pattern.match(value)
        if match:
            try:
                if pattern is _CURRENT:
                    datetime.strptime(match["stamp"], "%Y%m%dT%H%M%S%fZ")
                else:
                    date(int(match["year"]), int(match["month"]), int(match["day"]))
            except ValueError:
                break
            return match[0], match["job"]
    raise ToolError(
        "Invalid archive address. Copy the prefix or key from storage results."
    )


def require_archive_confirm(prefix: str, confirm: str) -> None:
    """Refuse a malformed target or nonexact job ID before any HTTP request."""
    archive, job = _address(prefix)
    if archive != prefix:
        raise ToolError(
            "Invalid archive prefix. Copy prefix from ocr_list_storage_archives."
        )
    if confirm != job:
        raise ToolError(
            (
                "confirm must exactly equal the archive job_id, without "
                "trimming or case changes. Read it from storage results."
            )
        )


def require_file_confirm(key: str, confirm: str) -> None:
    """Compare the full relative artifact name, including any nested directory."""
    archive, _ = _address(key)
    name = key[len(archive) :]
    if (
        len(key) > 1024
        or not _ARTIFACT.fullmatch(name)
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise ToolError("Invalid artifact key. Copy key from ocr_get_storage_archive.")
    if name == "manifest.json":
        raise ToolError(
            "manifest.json cannot be deleted alone; delete its archive instead."
        )
    if confirm != name:
        raise ToolError(
            (
                "confirm must exactly equal the file name from archive details,"
                " including nested directories; no trimming or case changes."
            )
        )
