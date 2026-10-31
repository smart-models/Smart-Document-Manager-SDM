"""Verify spotting coordinate parsing and incomplete region handling."""

from ocrserver.element_ocr import parse_spotting_boxes, parse_spotting_output


def locs(*values):
    return "".join(f"<|LOC_{value}|>" for value in values)


def test_whitespace_wrapped_region_and_legacy_boxes():
    text = "[[1, 2, 3, 4]]\nregion " + locs(1, 2, 5, 2) + "\n " + locs(5, 7, 1, 7)
    boxes, incomplete, invalid = parse_spotting_output(text)
    assert boxes == [[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 5.0, 7.0]]
    assert incomplete == 0
    assert parse_spotting_boxes(text) == boxes


def test_next_regions_text_does_not_complete_previous_fragment():
    text = "first " + locs(1, 2, 3) + "\nsecond " + locs(10, 20, 30, 20, 30, 40, 10, 40)
    boxes, incomplete, invalid = parse_spotting_output(text)
    assert boxes == [[10.0, 20.0, 30.0, 40.0]]
    assert incomplete == 3


def test_each_incomplete_sequence_is_counted_without_deduplication():
    text = locs(1, 2) + " text " + locs(3, 4, 5)
    assert parse_spotting_output(text) == ([], 5, 0)
    quad = locs(1, 2, 3, 2, 3, 4, 1, 4)
    assert parse_spotting_boxes(quad + "\n" + quad) == [[1.0, 2.0, 3.0, 4.0]] * 2


def test_loc_units_and_legacy_values_are_preserved():
    quad = locs(100, 200, 900, 200, 900, 800, 100, 800)
    assert parse_spotting_boxes(quad) == [[100.0, 200.0, 900.0, 800.0]]
    assert parse_spotting_boxes("[[100,200,1800,2200]]") == [
        [100.0, 200.0, 1800.0, 2200.0]
    ]


def test_element_images_follow_exif_orientation():
    """Phone photos store rotation in EXIF: recognize them upright, as shown."""
    import io

    from PIL import Image

    from ocrserver.element_ocr import load_element_image

    source = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways, displayed rotated 90° clockwise
    Image.new("RGB", (40, 20)).save(source, format="JPEG", exif=exif)
    assert load_element_image(source.getvalue()).size == (20, 40)
    plain = io.BytesIO()
    Image.new("RGB", (40, 20)).save(plain, format="PNG")
    assert load_element_image(plain.getvalue()).size == (40, 20)


def _encoded(image, fmt, **kwargs):
    import io

    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def test_parse_pages_follow_exif_like_element_ocr():
    """/parse decodes through to_bgr_ndarray: same upright pixels as /ocr."""
    from PIL import Image

    from ocrserver.pipeline_backend import to_bgr_ndarray

    exif = Image.Exif()
    exif[0x0112] = 6
    data = _encoded(Image.new("RGB", (40, 20)), "JPEG", exif=exif)
    assert to_bgr_ndarray(data).shape[:2] == (40, 20)


def test_transparent_background_is_flattened_to_white():
    from PIL import Image

    from ocrserver.element_ocr import load_element_image
    from ocrserver.pipeline_backend import to_bgr_ndarray

    image = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    image.putpixel((1, 1), (0, 0, 0, 255))
    data = _encoded(image, "PNG")
    loaded = load_element_image(data)
    assert loaded.getpixel((0, 0)) == (255, 255, 255)
    assert loaded.getpixel((1, 1)) == (0, 0, 0)
    assert to_bgr_ndarray(data)[0, 0].tolist() == [255, 255, 255]


def test_multipage_tiff_yields_every_frame_up_to_the_cap():
    from PIL import Image

    from ocrserver.element_ocr import load_image_pages

    frames = [Image.new("RGB", (10, 10), (i * 60, 0, 0)) for i in range(3)]
    data = _encoded(frames[0], "TIFF", save_all=True, append_images=frames[1:])
    pages, total = load_image_pages(data, 2)
    assert total == 3
    assert [page.getpixel((0, 0))[0] for page in pages] == [0, 60]


def test_pixel_limit_is_enforced_before_decoding(monkeypatch):
    import pytest
    from PIL import Image

    from ocrserver.element_ocr import ImageTooLargeError, load_element_image

    data = _encoded(Image.new("RGB", (20, 20)), "PNG")
    # Between 1x and 2x the limit Pillow only warns and would decode it all.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 300)
    with pytest.raises(ImageTooLargeError):
        load_element_image(data)


def test_invalid_geometry_is_dropped_without_losing_valid_boxes():
    text = "[[" + "9" * 400 + ",1,2,3]] [[3,2,1,4]] [[1,2,1800,2200]] bad "
    text += locs(0, 0, 1500, 0, 1500, 1000, 0, 1000)
    text += " good " + locs(1, 2, 3, 2, 3, 4, 1, 4)
    assert parse_spotting_output(text) == (
        [[1.0, 2.0, 1800.0, 2200.0], [1.0, 2.0, 3.0, 4.0]],
        0,
        3,
    )
