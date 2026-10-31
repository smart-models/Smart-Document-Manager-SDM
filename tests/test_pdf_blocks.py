import inspect

import pytest

from ocrserver.config import Settings, load_settings
from ocrserver.pdf_utils import (
    BLOCK_MAX_PIXELS,
    MAX_BLOCK_PAGES,
    PdfTooLargeError,
    page_pixels,
    plan_blocks,
    rasterize_pdf,
    render_pages,
)
from test_api_office import pdf_bytes

A4 = (595, 842)


def test_page_pixels_a4_at_250_dpi():
    assert page_pixels(*A4, 250) == 2066 * 2924  # ceil(2065.97) x ceil(2923.6)


def test_block_defaults_cover_at_least_two_layout_batches_at_250_dpi():
    # PaddleOCR-VL lays out 8 pages per batch; two batches per block let layout
    # of the second overlap VL recognition of the first.
    assert BLOCK_MAX_PIXELS == 100_000_000 and MAX_BLOCK_PAGES == 24


@pytest.mark.parametrize("dpi,size", [(100, 24), (250, 16), (300, 11)])
def test_plan_blocks_a4_size_follows_dpi(dpi, size):
    blocks = plan_blocks([page_pixels(*A4, dpi)] * 30, 25_000_000)
    assert len(blocks[0]) == size


def test_91_a4_pages_at_250_dpi_make_6_blocks():
    blocks = plan_blocks([page_pixels(*A4, 250)] * 91, 25_000_000)
    assert [len(b) for b in blocks] == [16] * 5 + [11]
    assert blocks[-1] == range(80, 91)


def test_mixed_sizes_close_block_on_budget():
    assert plan_blocks([10, 10, 10, 30, 5], 30, max_block_pixels=30) == [
        range(0, 3),
        range(3, 4),
        range(4, 5),
    ]


def test_page_equal_to_budget_is_its_own_block():
    assert plan_blocks([30, 30], 30, max_block_pixels=30) == [
        range(0, 1),
        range(1, 2),
    ]


def test_block_budget_never_below_one_page():
    assert plan_blocks([30, 30], 30, max_block_pixels=10) == [
        range(0, 1),
        range(1, 2),
    ]


def test_block_page_cap_applies():
    assert plan_blocks([1] * 5, 30, max_pages=2) == [
        range(0, 2),
        range(2, 4),
        range(4, 5),
    ]


def test_single_page_over_page_budget_raises():
    with pytest.raises(PdfTooLargeError):
        plan_blocks([10, 31], 30)


def test_render_pages_renders_only_requested_range():
    images = render_pages(pdf_bytes(3), 72, range(1, 3))
    assert len(images) == 2


def test_rasterize_pdf_has_no_total_pixel_budget():
    assert "max_total_pixels" not in inspect.signature(rasterize_pdf).parameters


def test_settings_no_longer_accept_total_pixels():
    with pytest.raises(TypeError):
        Settings(pdf_max_total_pixels=1)
    env = {"OCR_PDF_MAX_TOTAL_PIXELS": "1"}
    assert load_settings(env, None).pdf_max_page_pixels == 25_000_000
