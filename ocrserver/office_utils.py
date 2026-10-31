"""Office/ODF detection and headless LibreOffice conversion — bytes → PDF."""

from __future__ import annotations

import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_PARTS = (
    ("word/document.xml", "docx"),
    ("ppt/presentation.xml", "pptx"),
    ("xl/workbook.xml", "xlsx"),
)
_ODF_PREFIX = b"application/vnd.oasis.opendocument."
_ODF_KINDS = {b"text": "odt", b"presentation": "odp", b"spreadsheet": "ods"}
# "mimetype" is only recommended by ODF; the manifest root entry is the fallback.
_MANIFEST_ROOT = re.compile(
    rb"""<[\w.:-]*file-entry\b[^>]*full-path\s*=\s*(["'])/\1[^>]*>"""
)
_MANIFEST_TYPE = re.compile(rb"""media-type\s*=\s*(["'])(.*?)\1""")
_MANIFEST_READ = 64 * 1024
# Compound-file directory names are NUL-terminated UTF-16LE; first match wins,
# so a Word file embedding a workbook still reads as Word.
_OLE2_STREAMS = tuple(
    (name.encode("utf-16-le") + b"\x00\x00", kind)
    for name, kind in (
        ("WordDocument", "doc"),
        ("PowerPoint Document", "ppt"),
        ("Workbook", "xls"),
        ("Book", "xls"),
    )
)
_STDERR_EXCERPT = 500
# ponytail: one conversion at a time keeps soffice inside the container's
# pids/memory/tmpfs limits; use a BoundedSemaphore if throughput matters.
_SOFFICE_LOCK = threading.Lock()


class OfficeError(Exception):
    """Raised when an Office document cannot be converted; code is the API code."""

    def __init__(self, message: str, code: str = "office_conversion_failed"):
        """Retain the public conversion explanation and stable API error code."""
        super().__init__(message)
        self.code = code


def office_format(data: bytes) -> str | None:
    """Identify the canonical Office kind from content alone.

    Never extract ZIP member payloads beyond the tiny ODF mimetype entry.
    """
    if data.startswith(_OLE2_MAGIC):
        for marker, kind in _OLE2_STREAMS:
            if marker in data:
                return kind
        return "ole2"
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" in names:
                for part, kind in _OOXML_PARTS:
                    if part in names:
                        return kind
                return None
            if "mimetype" in names:
                with archive.open("mimetype") as member:
                    mimetype = member.read(len(_ODF_PREFIX) + 32).strip()
            elif "META-INF/manifest.xml" in names:
                with archive.open("META-INF/manifest.xml") as member:
                    root = _MANIFEST_ROOT.search(member.read(_MANIFEST_READ))
                media = root and _MANIFEST_TYPE.search(root.group())
                mimetype = media.group(2) if media else b""
            else:
                return None
            if mimetype.startswith(_ODF_PREFIX):
                return _ODF_KINDS.get(mimetype[len(_ODF_PREFIX) :])
    except (
        zipfile.BadZipFile,
        OSError,
        ValueError,
        EOFError,
        NotImplementedError,
        RuntimeError,
    ):
        return None
    return None


def _kill(proc: subprocess.Popen) -> None:
    # soffice forks soffice.bin; killing only the direct child orphans it.
    # The killed grandchild is reaped by the container init (compose init: true).
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()


def convert_to_pdf(
    data: bytes,
    kind: str,
    *,
    soffice: str,
    timeout: float,
    deadline: float | None = None,
) -> bytes:
    """Convert with an isolated profile and a deadline including lock wait."""
    binary = soffice or shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise OfficeError(
            "LibreOffice is not installed", "office_converter_unavailable"
        )
    conversion_deadline = time.monotonic() + timeout
    deadline = (
        min(deadline, conversion_deadline)
        if deadline is not None
        else conversion_deadline
    )
    if not _SOFFICE_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        raise OfficeError("conversion deadline exceeded", "office_conversion_timeout")
    try:
        return _convert_locked(data, kind, binary, deadline)
    finally:
        _SOFFICE_LOCK.release()


def _convert_locked(data: bytes, kind: str, binary: str, deadline: float) -> bytes:
    """Convert Office bytes using an isolated temporary LibreOffice profile.

    Respect the absolute deadline and kill a timed-out process before cleanup.
    Reject missing/empty output and translate execution failures to OfficeError,
    retaining only a bounded stderr excerpt with the temporary path removed.
    """
    with tempfile.TemporaryDirectory(prefix="ocr-office-") as tmp:
        source = Path(tmp, f"input.{kind}")
        source.write_bytes(data)
        cmd = [
            binary,
            f"-env:UserInstallation={Path(tmp, 'louser').as_uri()}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--convert-to",
            "pdf",
            "--outdir",
            tmp,
            str(source),
        ]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OfficeError(
                "conversion deadline exceeded", "office_conversion_timeout"
            )
        try:
            with subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            ) as proc:
                try:
                    _, stderr = proc.communicate(
                        timeout=max(0, deadline - time.monotonic())
                    )
                except subprocess.TimeoutExpired:
                    _kill(proc)
                    proc.communicate()
                    raise OfficeError(
                        "conversion deadline exceeded",
                        "office_conversion_timeout",
                    ) from None
        except (FileNotFoundError, PermissionError) as exc:
            raise OfficeError(
                "LibreOffice binary cannot be executed",
                "office_converter_unavailable",
            ) from exc
        output = source.with_suffix(".pdf")
        if proc.returncode != 0 or not output.is_file() or not output.stat().st_size:
            text = stderr.decode("utf-8", "replace").replace(tmp, "<tmp>")
            excerpt = text.strip()[:_STDERR_EXCERPT]
            status = (
                f"LibreOffice exited with status {proc.returncode}"
                if proc.returncode
                else "LibreOffice produced no PDF"
            )
            raise OfficeError(status + (f": {excerpt}" if excerpt else ""))
        return output.read_bytes()
