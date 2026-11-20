"""Element-level task prompts, spotting preprocessing, and box parsing.

Preprocessing rules follow the PaddleOCR-VL-1.6 model card exactly. The task
prompts and preprocessing rules come from that card,
https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6, published by
PaddlePaddle under the Apache License 2.0 (see NOTICE).
"""

from __future__ import annotations

import io
import math
import re

from PIL import Image, ImageOps

TASK_PROMPTS: dict[str, str] = {
    "ocr": "OCR:",
    "table": "Table Recognition:",
    "formula": "Formula Recognition:",
    "chart": "Chart Recognition:",
    "spotting": "Spotting:",
    "seal": "Seal Recognition:",
}

SPOTTING_UPSCALE_THRESHOLD = 1500
DEFAULT_MIN_PIXELS = 112896
DEFAULT_MAX_PIXELS = 1280 * 28 * 28
SPOTTING_MAX_PIXELS = 2048 * 28 * 28

_BOX_RE = re.compile(
    r"\[\[(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)\]\]"
)
_LOC_RE = re.compile(r"<\|LOC_(\d+)\|>")


def max_pixels_for_task(task: str) -> int:
    """Select the model's larger pixel allowance only for the spotting task."""
    return SPOTTING_MAX_PIXELS if task == "spotting" else DEFAULT_MAX_PIXELS


class ImageTooLargeError(ValueError):
    """Pixel count above Pillow's decompression-bomb limit."""


def check_pixel_limit(image: Image.Image) -> None:
    """Reject from the header, before decoding, what PIL would only warn about."""
    limit = Image.MAX_IMAGE_PIXELS
    if limit and image.width * image.height > limit:
        raise ImageTooLargeError(f"{image.width}x{image.height} exceeds {limit} pixels")


def upright_rgb(image: Image.Image) -> Image.Image:
    """EXIF-upright RGB copy; transparency is flattened onto white.

    Phone photos often store pixels sideways plus a rotation tag, and viewers
    show them upright, so the model (and every bbox) must see the same. A plain
    RGB conversion turns transparent pixels black, hiding black text.
    """
    upright = ImageOps.exif_transpose(image)
    try:
        if upright.mode in ("RGBA", "LA", "PA") or (
            upright.mode == "P" and "transparency" in upright.info
        ):
            rgba = upright.convert("RGBA")
            flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            flat.alpha_composite(rgba)
            rgba.close()
            converted = flat.convert("RGB")
            flat.close()
            return converted
        return upright.convert("RGB")
    finally:
        if upright is not image:
            upright.close()


class MultiframeImageError(ValueError):
    """Element recognition accepts exactly one frame."""


def load_element_image(data: bytes, reserve=None) -> Image.Image:
    """Decode an upload as upright RGB (see upright_rgb)."""
    with Image.open(io.BytesIO(data)) as source:
        check_pixel_limit(source)
        if getattr(source, "n_frames", 1) > 1:
            raise MultiframeImageError("Use /parse for multi-frame images")
        if reserve is not None:
            reserve(source)
        return upright_rgb(source)


def load_image_pages(data: bytes, max_pages: int) -> tuple[list[Image.Image], int]:
    """Decode every frame of a multi-page image (TIFF), up to max_pages.

    Returns the upright RGB pages and the total frame count.
    """
    with Image.open(io.BytesIO(data)) as source:
        check_pixel_limit(source)
        total = max(1, getattr(source, "n_frames", 1))
        pages = []
        try:
            for index in range(min(total, max_pages)):
                source.seek(index)
                check_pixel_limit(source)
                pages.append(upright_rgb(source))
        except BaseException:
            for page in pages:
                page.close()
            raise
        return pages, total


def preprocess_for_spotting(img: Image.Image) -> Image.Image:
    """2x LANCZOS upscale when both sides < 1500 px (model card spec)."""
    w, h = img.size
    if w < SPOTTING_UPSCALE_THRESHOLD and h < SPOTTING_UPSCALE_THRESHOLD:
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        return img.resize((w * 2, h * 2), resample)
    return img


def parse_spotting_boxes(text: str) -> list[list[float]]:
    """Return complete spotting boxes; see parse_spotting_output for warnings."""
    return parse_spotting_output(text)[0]


def parse_spotting_output(text: str) -> tuple[list[list[float]], int, int]:
    """Extract [x1,y1,x2,y2] boxes from spotting output.

    PaddleOCR-VL-1.6 emits one text region per line followed by 8 <|LOC_n|>
    tokens (quadrilateral corners); the box is the axis-aligned envelope of
    the quad in normalized 0-1000 LOC units, not original-image pixels.
    Legacy [[x1,y1,x2,y2]] rectangles retain their original coordinate values.
    Returns boxes, incomplete token count, and discarded invalid box count.
    """
    boxes = []
    invalid = 0
    for match in _BOX_RE.finditer(text):
        box = [float(match[i]) for i in range(1, 5)]
        if all(math.isfinite(v) for v in box) and box[0] <= box[2] and box[1] <= box[3]:
            boxes.append(box)
        else:
            invalid += 1
    incomplete = 0

    def append_sequence(coords: list[float]) -> None:
        nonlocal incomplete, invalid
        incomplete += len(coords) % 8
        for i in range(0, len(coords) - len(coords) % 8, 8):
            if not all(math.isfinite(v) and 0 <= v <= 1000 for v in coords[i : i + 8]):
                invalid += 1
                continue
            xs = coords[i : i + 8 : 2]
            ys = coords[i + 1 : i + 8 : 2]
            boxes.append([min(xs), min(ys), max(xs), max(ys)])

    coords: list[float] = []
    last_end = 0
    for match in _LOC_RE.finditer(text):
        if text[last_end : match.start()].strip():
            append_sequence(coords)
            coords = []
        coords.append(float(match[1]))
        last_end = match.end()
    append_sequence(coords)
    return boxes, incomplete, invalid
