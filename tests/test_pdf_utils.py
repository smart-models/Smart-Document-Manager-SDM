"""Verify PDF detection, rasterization limits and asynchronous cleanup."""

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from ocrserver import pdf_utils


@pytest.mark.parametrize(
    "prefix", [b"", b" \r\n\t\f\v", b"\xef\xbb\xbf", b" \xef\xbb\xbf\n"]
)
def test_pdf_detector_accepts_explicit_prefixes(prefix):
    assert pdf_utils.is_pdf(prefix + b"%PDF-1.7")
    assert not pdf_utils.is_pdf(b"junk%PDF-1.7")


def fake_pdf(monkeypatch, failure=None):
    events = []
    bitmap_image = Image.new("RGB", (2, 2), "red")

    class Bitmap:
        def to_pil(self):
            return bitmap_image

        def close(self):
            events.append("bitmap.close")
            bitmap_image.close()

    class Page:
        def __init__(self, index):
            self.index = index

        def render(self, scale):
            if self.index == 1 and failure is not None:
                raise failure
            return Bitmap()

        def get_size(self):
            return 2, 2

        def close(self):
            events.append(f"page{self.index}.close")

    class Document:
        def __init__(self):
            self.forms_initialized = False

        def init_forms(self):
            events.append("forms.init")
            self.forms_initialized = True

        def __len__(self):
            assert self.forms_initialized
            return 2 if failure else 1

        def __getitem__(self, index):
            return Page(index)

        def close(self):
            events.append("document.close")

    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", lambda _: Document())
    return events


def test_bitmap_closed_and_pixels_independent(monkeypatch):
    events = fake_pdf(monkeypatch)
    pages = pdf_utils.rasterize_pdf(b"pdf")
    try:
        assert pages[0].getpixel((0, 0)) == (255, 0, 0)
        assert events == [
            "forms.init",
            "page0.close",
            "bitmap.close",
            "page0.close",
            "document.close",
        ]
    finally:
        pages[0].close()


def test_partial_render_closes_previous_images(monkeypatch):
    error = pdf_utils.pdfium.PdfiumError("corrupt page")
    events = fake_pdf(monkeypatch, error)
    copies = []
    original_copy = Image.Image.copy

    def tracked_copy(image):
        result = original_copy(image)
        copies.append(result)
        return result

    monkeypatch.setattr(Image.Image, "copy", tracked_copy)
    with pytest.raises(pdf_utils.PdfError):
        pdf_utils.rasterize_pdf(b"pdf")
    assert events[-2:] == ["page1.close", "document.close"]
    with pytest.raises(ValueError):
        copies[0].getpixel((0, 0))


@pytest.mark.parametrize("error", [MemoryError(), RuntimeError("bug")])
def test_internal_errors_are_not_invalid_pdf(monkeypatch, error):
    def fail(_):
        raise error

    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", fail)
    with pytest.raises(type(error)):
        pdf_utils.pdf_page_count(b"pdf")


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (pdf_utils.pdfium.PdfiumError("invalid form"), pdf_utils.PdfError),
        (RuntimeError("form initialization bug"), RuntimeError),
    ],
)
def test_form_init_failure_closes_document(monkeypatch, error, expected_error):
    events = []

    class Document:
        def init_forms(self):
            events.append("forms.init")
            raise error

        def __len__(self):
            pytest.fail("page count accessed after form initialization failed")

        def __getitem__(self, index):
            pytest.fail("page accessed after form initialization failed")

        def close(self):
            events.append("document.close")

    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", lambda _: Document())
    with pytest.raises(expected_error) as caught:
        pdf_utils.rasterize_pdf(b"pdf")
    assert str(caught.value) == str(error)
    if isinstance(error, pdf_utils.pdfium.PdfiumError):
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    assert events == ["forms.init", "document.close"]


@pytest.mark.asyncio
async def test_pdfium_calls_are_serialized_across_threads(monkeypatch):
    active = 0
    max_active = 0
    guard = threading.Lock()

    class Document:
        def __init__(self, _):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)

        def __len__(self):
            return 1

        def close(self):
            nonlocal active
            with guard:
                active -= 1

    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", Document)
    counts = await asyncio.gather(
        *[asyncio.to_thread(pdf_utils.pdf_page_count, b"pdf") for _ in range(4)]
    )
    assert counts == [1] * 4
    assert max_active == 1


def test_real_pdf_pixels_survive_pdfium_close():
    output = io.BytesIO()
    with Image.new("RGB", (20, 20), "red") as source:
        source.save(output, "PDF", resolution=72)
    pages = pdf_utils.rasterize_pdf(output.getvalue(), dpi=72)
    try:
        assert pages[0].size == (20, 20)
        red, green, blue = pages[0].convert("RGB").getpixel((10, 10))
        assert red > 240 and green < 10 and blue < 10
    finally:
        pages[0].close()


def test_acroform_value_survives_rasterization():
    data = (Path(__file__).parent / "fixtures" / "acroform.pdf").read_bytes()
    pages = pdf_utils.rasterize_pdf(data, dpi=72)
    try:
        assert len(pages) == 1
        assert pages[0].size == (200, 100)
        with pages[0].convert("L") as gray:
            assert sum(gray.histogram()[:200]) > 100
    finally:
        for page in pages:
            page.close()


def test_pixel_budget_rejects_before_rendering(monkeypatch):
    events = fake_pdf(monkeypatch)
    with pytest.raises(pdf_utils.PdfTooLargeError):
        pdf_utils.rasterize_pdf(b"pdf", dpi=72, max_page_pixels=3)
    assert events == ["forms.init", "page0.close", "document.close"]
