"""PDF rasterization via pypdfium2 — page → PIL image."""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pypdfium2 as pdfium
from PIL import Image

_PDFIUM_LOCK = threading.RLock()
_ASCII_WHITESPACE = b" \t\n\r\v\f"


class PdfError(Exception):
    """Raised on corrupt, encrypted, or unreadable PDF input."""


class PdfTooLargeError(ValueError):
    """Requested PDF rasterization exceeds its pixel budget."""


def is_pdf(data: bytes) -> bool:
    """Recognize PDF magic after ASCII whitespace and an optional UTF-8 BOM."""
    header = data.lstrip(_ASCII_WHITESPACE)
    if header.startswith(b"\xef\xbb\xbf"):
        header = header[3:].lstrip(_ASCII_WHITESPACE)
    return header.startswith(b"%PDF-")


def _open(data: bytes) -> pdfium.PdfDocument:
    if not data:
        raise PdfError("empty input")
    try:
        return pdfium.PdfDocument(io.BytesIO(data))
    except pdfium.PdfiumError as exc:
        raise PdfError(str(exc)) from exc


@contextmanager
def _document(data: bytes, images: list[Image.Image]) -> Iterator[pdfium.PdfDocument]:
    """Open `data` under the PDFium lock; on failure close `images` and map errors.

    The document is always closed. A PDFium failure surfaces as PdfError; any
    other exception (PdfTooLargeError, PdfError) propagates unchanged.
    """
    with _PDFIUM_LOCK:
        doc = _open(data)
        try:
            yield doc
        except BaseException as exc:
            for image in images:
                image.close()
            if isinstance(exc, pdfium.PdfiumError):
                raise PdfError(str(exc)) from exc
            raise
        finally:
            doc.close()


def _iter_page_pixels(doc: pdfium.PdfDocument, count: int, dpi: int) -> Iterator[int]:
    """Yield each page's rendered pixel count, validating its size page by page."""
    for i in range(count):
        page = doc[i]
        try:
            width, height = page.get_size()
        finally:
            page.close()
        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
            raise PdfError("invalid page dimensions")
        yield page_pixels(width, height, dpi)


def _render_into(
    doc: pdfium.PdfDocument, pages: range, dpi: int, images: list[Image.Image]
) -> None:
    """Append one PIL image per page index to `images` (owned by the caller)."""
    scale = dpi / 72.0
    for i in pages:
        page = doc[i]
        try:
            bitmap = page.render(scale=scale)
            try:
                images.append(bitmap.to_pil().copy())
            finally:
                bitmap.close()
        finally:
            page.close()


def pdf_page_count(data: bytes) -> int:
    """Read the page count under the PDFium lock and always close the document."""
    with _document(data, []) as doc:
        return len(doc)


# PaddleOCR-VL lays out 8 pages per batch and only overlaps layout of batch N+1
# with VL recognition of batch N inside one predict call, so a block should hold
# at least two batches. 100 M pixels is 16 A4 pages at 250 DPI: about 600 MB of
# RGB images plus their BGR copies, well inside the 4 GB ocr-api limit.
BLOCK_MAX_PIXELS = 100_000_000
MAX_BLOCK_PAGES = 24


def page_pixels(width_pt: float, height_pt: float, dpi: int) -> int:
    """Pixels of a page of the given size in points rendered at dpi."""
    scale = dpi / 72.0
    return math.ceil(width_pt * scale) * math.ceil(height_pt * scale)


def pdf_page_pixels(data: bytes, dpi: int, max_pages: int) -> list[int]:
    """Rendered pixel count of each page, up to max_pages, without rendering."""
    with _document(data, []) as doc:
        return list(_iter_page_pixels(doc, min(len(doc), max_pages), dpi))


def plan_blocks(
    pixels: list[int],
    max_page_pixels: int,
    max_block_pixels: int = BLOCK_MAX_PIXELS,
    max_pages: int = MAX_BLOCK_PAGES,
) -> list[range]:
    """Greedily group consecutive pages into blocks within the pixel budgets.

    A page over max_page_pixels is rejected; a block never holds less than one
    page, so its budget is at least the page budget.
    """
    budget = max(max_block_pixels, max_page_pixels)
    blocks: list[range] = []
    start, total = 0, 0
    for i, px in enumerate(pixels):
        if px > max_page_pixels:
            raise PdfTooLargeError("PDF rasterization exceeds pixel budget")
        if i > start and (total + px > budget or i - start >= max_pages):
            blocks.append(range(start, i))
            start, total = i, 0
        total += px
    if pixels:
        blocks.append(range(start, len(pixels)))
    return blocks


def render_pages(data: bytes, dpi: int, pages: range) -> list[Image.Image]:
    """Render only the given page indexes; close partial images on failure."""
    images: list[Image.Image] = []
    with _document(data, images) as doc:
        doc.init_forms()
        _render_into(doc, pages, dpi, images)
    return images


def rasterize_pdf(
    data: bytes,
    dpi: int = 250,
    max_pages: int | None = None,
    *,
    max_page_pixels: int = 25_000_000,
) -> list[Image.Image]:
    """Render pages to PIL images, stopping after max_pages.

    Every page is checked against max_page_pixels before any is rendered; there
    is no cumulative cap. Callers feed images directly to the pipeline.
    """
    images: list[Image.Image] = []
    with _document(data, images) as doc:
        doc.init_forms()
        count = len(doc) if max_pages is None else min(len(doc), max_pages)
        for pixels in _iter_page_pixels(doc, count, dpi):
            if pixels > max_page_pixels:
                raise PdfTooLargeError("PDF rasterization exceeds pixel budget")
        _render_into(doc, range(count), dpi, images)
    return images
