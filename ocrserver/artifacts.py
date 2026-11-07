"""Bounded, internal OCR capture and portable archive construction."""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

from ocrserver.archive_names import parse_prefix, safe_filename
from ocrserver.chunking.types import VERSION as CHUNKER_VERSION
from ocrserver.config import Settings
from ocrserver.pdf_utils import is_pdf
from ocrserver.schemas import MODEL_NAME, PIPELINE_VERSION


class ArchiveLimit(ValueError):
    """An archive exceeds its configured memory or object budget."""


@dataclass
class CapturedPage:
    """Retain one page's Markdown and named PNG buffers for archive assembly."""

    number: int
    markdown: str
    images: list[tuple[str, bytes]] = field(default_factory=list)


@dataclass
class ArtifactCapture:
    """Request-private references; never a field of a public response model.

    Capture does no serialization or copying of PNG/source buffers. Its bounded
    references survive pipeline cleanup; a failure discards all large references.
    The optional original input is released first, so it never costs the outputs.
    """

    kind: str
    settings: Settings = field(repr=False)
    original_filename: str | None = None
    requested: dict = field(default_factory=dict)
    effective: dict = field(default_factory=dict)
    source_info: dict = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    pages: list[CapturedPage] = field(default_factory=list)
    input_bytes: bytes | None = field(default=None, repr=False)
    input_dropped: bool = False
    failed: bool = False
    _bytes: int = 0
    _objects: int = 1  # manifest always consumes an object

    def fail(self) -> None:
        """Disable archival for this request and release captured document data."""
        self.failed = True
        self.pages.clear()
        self.input_bytes = None

    def drop_input(self) -> None:
        """Release the retained original so the outputs still fit the archive."""
        self._bytes -= len(self.input_bytes)
        self._objects -= 1
        self.input_bytes = None
        self.input_dropped = True

    def _fits(self, size: int, objects: int = 0) -> bool:
        return (
            self._bytes + size <= self.settings.storage_max_bytes
            and self._objects + objects <= self.settings.storage_max_objects
        )

    def _reserve(self, size: int, objects: int = 0) -> None:
        if not self._fits(size, objects) and self.input_bytes is not None:
            self.drop_input()
        if not self._fits(size, objects):
            raise ArchiveLimit
        self._bytes += size
        self._objects += objects

    def source(self, data: bytes, mime: str, extension: str = "bin") -> None:
        """Run alongside preparation, before Office conversion or rasterization."""
        if self.failed:
            return
        try:
            self.source_info = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "content_type": mime,
                "extension": extension,
            }
            if self.settings.storage_save_input:
                if self._fits(len(data), 1):
                    self._reserve(len(data), 1)
                    self.input_bytes = data
                else:
                    self.input_dropped = True
        except Exception:  # noqa: BLE001 - optional capture cannot fail OCR
            self.fail()

    def page(self, number: int, markdown: str, assets: list[dict]) -> None:
        """Retain valid PNG assets within capture limits, failing archival only."""
        if self.failed:
            return
        try:
            # A UTF-8 character uses at most four bytes. Avoid a large temporary
            # encoded copy in the event loop just to measure capture size.
            self._reserve(len(markdown) * 4)
            page = CapturedPage(number, markdown)
            for asset in assets:
                png = asset.get("png")
                if not isinstance(png, bytes) or not png:
                    continue
                original = str(asset.get("name", ""))
                self._reserve(len(png) + len(original) * 4, 1)
                page.images.append((original, png))
            self.pages.append(page)
        except Exception:  # noqa: BLE001 - optional capture cannot fail OCR
            self.fail()


def source_format(data: bytes, office_kind: str | None = None) -> tuple[str, str]:
    """Infer an allowlisted MIME/extension from decoded bytes, never a filename."""
    office = {
        "docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        "doc": "application/msword",
        "xls": "application/vnd.ms-excel",
        "ppt": "application/vnd.ms-powerpoint",
        "odt": "application/vnd.oasis.opendocument.text",
        "ods": "application/vnd.oasis.opendocument.spreadsheet",
        "odp": "application/vnd.oasis.opendocument.presentation",
    }
    if office_kind in office:
        return office[office_kind], office_kind
    if is_pdf(data):
        return "application/pdf", "pdf"
    for magic, mime, extension in (
        (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
        (b"\xff\xd8\xff", "image/jpeg", "jpg"),
        (b"GIF8", "image/gif", "gif"),
        (b"II*\x00", "image/tiff", "tiff"),
        (b"MM\x00*", "image/tiff", "tiff"),
        (b"BM", "image/bmp", "bmp"),
        (b"\x00\x00\x00\x0cjP  \r\n\x87\n", "image/jp2", "jp2"),
    ):
        if data.startswith(magic):
            return mime, extension
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif", "avif"
    return "application/octet-stream", "bin"


_JSON_ESCAPES = re.compile(r'["\\\x00-\x1f]')


def _check_json_strings(value, remaining: int) -> int:
    """Reject oversized strings before JSONEncoder builds whole fragments.

    Plain ASCII needs only a length/escape scan. Other strings are measured in
    small chunks, so UTF-8 and JSON escaping never need an unbounded temporary.
    Container traversal uses depth-sized state instead of copying child lists.
    """
    if isinstance(value, str):
        if len(value) + 2 > remaining:
            raise ArchiveLimit
        remaining -= 2  # JSON string quotes
        if value.isascii() and _JSON_ESCAPES.search(value) is None:
            remaining -= len(value)
        else:
            for start in range(0, len(value), 4096):
                fragment = json.encoder.encode_basestring(value[start : start + 4096])
                remaining -= len(fragment.encode("utf-8")) - 2
                if remaining < 0:
                    raise ArchiveLimit
    elif isinstance(value, dict):
        remaining -= 2 + max(0, len(value) - 1)  # braces and commas
        for key, item in value.items():
            remaining = _check_json_strings(key, remaining - 1)  # colon
            remaining = _check_json_strings(item, remaining)
    elif isinstance(value, (list, tuple)):
        remaining -= 2 + max(0, len(value) - 1)
        for item in value:
            remaining = _check_json_strings(item, remaining)
    else:
        # A scalar consumes at least one byte. The actual encoder below keeps
        # the exact aggregate limit and validates scalar types/nonfinite floats.
        remaining -= 1
    if remaining < 0:
        raise ArchiveLimit
    return remaining


def _json_bytes(value, maximum: int) -> bytes:
    """Bound serialization before allocating the final contiguous buffer."""
    _check_json_strings(value, maximum)
    buffer, size = io.BytesIO(), 0
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    for fragment in encoder.iterencode(value):
        encoded = fragment.encode("utf-8")
        size += len(encoded)
        if size > maximum:
            raise ArchiveLimit
        buffer.write(encoded)
    return buffer.getvalue()


def _parsed_payload(result):
    return result.model_dump(
        mode="json",
        exclude={
            "json_rag": True,
            "pages": {"__all__": {"images", "image_names"}},
        },
    )


_MARKDOWN_IMAGE = re.compile(
    r"(?P<prefix>!\[[^\]\n]*\]\(\s*)(?:<(?P<angled>[^>\n]+)>|(?P<plain>[^\s)]+))"
)
_HTML_IMAGE = re.compile(
    r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>[\"'])"
    r"(?P<reference>.*?)(?P=quote)",
    re.IGNORECASE,
)


def _rewrite_images(markdown, paths, page, warnings, settings, maximum):
    """Rewrite inline Markdown/HTML images, preserving titles and HTTP text."""
    expanded = len(markdown)
    missing = set()

    def replace(match):
        nonlocal expanded
        groups = match.groupdict()
        original = (
            groups.get("reference") or groups.get("angled") or groups.get("plain")
        )
        relative = paths.get(original)
        if relative is None:
            if original not in missing:
                if len(warnings) >= settings.storage_max_objects:
                    raise ArchiveLimit
                missing.add(original)
                warnings.append(
                    {
                        "code": "image_unavailable",
                        "page": page,
                        "reference": original,
                    }
                )
            return match.group()
        expanded += len(relative) - len(original)
        if expanded * 4 > maximum:
            raise ArchiveLimit
        if groups.get("quote"):
            return f"{groups['prefix']}{groups['quote']}{relative}{groups['quote']}"
        return groups["prefix"] + (
            f"<{relative}>" if groups.get("angled") else relative
        )

    return _HTML_IMAGE.sub(replace, _MARKDOWN_IMAGE.sub(replace, markdown))


def build_archive(capture, result, prefix, *, parsed=None, result_bytes=None):
    """Construct bounded archive objects without performing storage writes.

    Args:
        capture: Request-private source, page assets and effective settings.
        result: Successful OCR, parse or chunk response to serialize.
        prefix: Validated destination prefix without a trailing slash.
        parsed: Original parse response associated with chunk generation.
        result_bytes: Pre-serialized chunk response; unused for parse responses.

    Returns:
        Ordered key, bytes and content-type tuples, with the complete manifest
        last so callers can commit it only after every other write succeeds.

    Raises:
        ArchiveLimit: If capture failed or an object or byte budget is exceeded,
            even after one rebuild without the optional original input.
    """
    try:
        return _build_archive(capture, result, prefix, parsed, result_bytes)
    except ArchiveLimit:
        if capture.input_bytes is None:
            raise
    capture.drop_input()
    return _build_archive(capture, result, prefix, parsed, result_bytes)


def _build_archive(capture, result, prefix, parsed, result_bytes):
    if capture.failed:
        raise ArchiveLimit
    settings = capture.settings
    objects, inventory, mappings, archive_warnings = [], [], [], []
    if capture.input_dropped:
        archive_warnings.append({"code": "input_not_saved"})
    used = 0

    def add(name, payload, content_type):
        nonlocal used
        body = (
            payload
            if isinstance(payload, bytes)
            else _json_bytes(
                payload,
                settings.storage_max_bytes - used,
            )
        )
        used += len(body)
        if (
            used > settings.storage_max_bytes
            or len(objects) >= settings.storage_max_objects
        ):
            raise ArchiveLimit
        key = f"{prefix}/{name}"
        objects.append((key, body, content_type))
        inventory.append(
            {
                "name": name,
                "key": key,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "content_type": content_type,
            }
        )

    source_result = parsed if parsed is not None else result
    if capture.kind != "ocr":
        if capture.kind == "parse":
            result_payload = _parsed_payload(result)
        elif result_bytes is None:
            result_payload = result.model_dump(mode="json")
        else:
            result_payload = result_bytes
        name = "json_ocr.json" if capture.kind == "parse" else "json_chunks.json"
        add(name, result_payload, "application/json")
    if parsed is not None and settings.storage_save_parsed:
        add("parsed.json", _parsed_payload(parsed), "application/json")
    # Markdown objects are named after the sanitized upload, the same name that
    # heads the archive folder: ``<stem>.md`` joins every page, and
    # ``document/<stem>_pNNNN.md`` holds one page each so a reader can fetch a
    # page without loading a document larger than it can hold.
    stem = PurePosixPath(
        safe_filename(
            capture.original_filename, capture.source_info.get("extension", "bin")
        )
    ).stem
    markdown_type = "text/markdown; charset=utf-8"
    markdown_pages = [source_result.result] if capture.kind == "ocr" else []
    for page in capture.pages:
        markdown = page.markdown
        paths = {}
        for index, (original, png) in enumerate(page.images, 1):
            relative = f"images/p{page.number:04d}-{index:04d}.png"
            add(relative, png, "image/png")
            paths[original] = relative
            mappings.append(
                {
                    "page": page.number,
                    "original_name": original,
                    "path": relative,
                    "key": f"{prefix}/{relative}",
                }
            )
        markdown = _rewrite_images(
            markdown,
            paths,
            page.number,
            archive_warnings,
            settings,
            settings.storage_max_bytes - used,
        )
        if markdown:
            markdown_pages.append(markdown)
            # The page file sits one folder below the crops, so its links climb
            # to ``../images/``. The pass above already recorded any missing
            # reference, so this one hands warnings to a throwaway list.
            page_markdown = _rewrite_images(
                page.markdown,
                {original: f"../{path}" for original, path in paths.items()},
                page.number,
                [],
                settings,
                settings.storage_max_bytes - used,
            )
            add(
                f"document/{stem}_p{page.number:04d}.md",
                page_markdown.encode("utf-8"),
                markdown_type,
            )
    if markdown_pages:
        add(f"{stem}.md", "\n\n".join(markdown_pages).encode("utf-8"), markdown_type)
    rag = getattr(source_result, "json_rag", None)
    if rag is not None:
        add("json_rag.json", rag, "application/json")
    if capture.input_bytes is not None:
        # Extension comes from source_format, but keep the key safe even for an
        # injected capture implementation.
        extension = capture.source_info.get("extension", "bin")
        if extension not in {
            "pdf",
            "png",
            "jpg",
            "gif",
            "tiff",
            "bmp",
            "webp",
            "jp2",
            "avif",
            "docx",
            "xlsx",
            "pptx",
            "doc",
            "xls",
            "ppt",
            "odt",
            "ods",
            "odp",
        }:
            extension = "bin"
        add(
            f"input.{extension}",
            capture.input_bytes,
            capture.source_info["content_type"],
        )
    warnings = [warning.model_dump(mode="json") for warning in source_result.warnings]
    partial_codes = {
        "page_failed",
        "pages_unprocessed",
        "pages_truncated",
        "generation_truncated",
        "spotting_incomplete_coordinates",
    }
    unprocessed_pages = next(
        (
            w["details"]["pages"]
            for w in warnings
            if w["code"] == "pages_unprocessed"
            and (w.get("details") or {}).get("pages")
        ),
        [],
    )
    manifest = {
        # 2 adds "pages"; 3 names the Markdown after the source and adds one
        # Markdown object per page under "document/".
        "archive_schema_version": 3,
        "complete": True,
        "kind": capture.kind,
        "endpoint": {
            "parse": "/parse",
            "ocr": "/ocr",
            "parse-chunks": "/parse/chunks",
        }[capture.kind],
        "job_id": parse_prefix(prefix + "/")["job"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Parsed page count (error pages included); an /ocr archive has none.
        "pages": (
            len(getattr(source_result, "pages", None) or [])
            if capture.kind != "ocr"
            else None
        ),
        "source": {
            **capture.source_info,
            **(
                {"original_name": capture.original_filename}
                if capture.original_filename
                else {}
            ),
        },
        "requested": capture.requested,
        "effective": capture.effective,
        "model_name": MODEL_NAME,
        "model_revision": settings.vllm_model_revision,
        "served_model": settings.vllm_served_model,
        "pipeline_version": PIPELINE_VERSION,
        "application_version": "1.0.0",
        "chunker_version": CHUNKER_VERSION if capture.kind == "parse-chunks" else None,
        "processing_time_ms": source_result.processing_time_ms,
        "timings": {
            "ocr_time_ms": source_result.processing_time_ms,
            **capture.timings,
        },
        "warnings": warnings,
        "archive_warnings": archive_warnings,
        "partial_ocr": any(w["code"] in partial_codes for w in warnings)
        or any(page.error for page in getattr(source_result, "pages", [])),
        "unprocessed_pages": unprocessed_pages,
        "images": mappings,
        "artifacts": list(inventory),
    }
    add("manifest.json", manifest, "application/json")
    return objects
