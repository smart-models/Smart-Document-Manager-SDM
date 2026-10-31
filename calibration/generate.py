"""Deterministic calibration set: PIL-rendered images + 3-page PDF + ground truth.

No model downloads. All content is drawn using the bundled DejaVu Sans font.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from PIL import Image, ImageDraw, ImageFont

CANVAS_W, CANVAS_H = 800, 1000
FONT_SIZE = 20
FONT_PATH = Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"
SOURCE_PDF_PATH = Path(__file__).resolve().parent / "form943.pdf"
SOURCE_GROUND_TRUTH_PATH = Path(__file__).resolve().parent / "form943.txt"


def _font(size: int = FONT_SIZE) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if not FONT_PATH.is_file():
        raise ValueError(f"Bundled calibration font is missing: {FONT_PATH}")
    return ImageFont.truetype(str(FONT_PATH), size)


def normalize_text(text: str) -> str:
    """Normalize ground truth and OCR while retaining non-Latin scripts."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    normalized = "".join(char.casefold() if char.isalnum() else " " for char in text)
    return " ".join(normalized.split())


@dataclass
class CalibrationItem:
    """Describe a named calibration fixture and its expected recognized text."""

    name: str
    kind: str
    path: str
    ground_truth: str


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_text_page(lines: list[str]) -> bytes:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    font = _font()
    y = 40
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += FONT_SIZE + 8
    return _png_bytes(img)


def _draw_table_page() -> tuple[bytes, str]:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    font = _font()
    headers = ["Name", "Value", "Unit"]
    rows = [
        ["Alpha", "100", "kg"],
        ["Beta", "250", "m"],
        ["Gamma", "3.14", "rad"],
    ]
    x_positions = [40, 300, 500]
    y = 40
    for col, header in enumerate(headers):
        draw.text((x_positions[col], y), header, fill="black", font=font)
    y += FONT_SIZE + 12
    draw.line([(30, y - 4), (700, y - 4)], fill="black", width=2)
    for row in rows:
        for col, val in enumerate(row):
            draw.text((x_positions[col], y), val, fill="black", font=font)
        y += FONT_SIZE + 10
    ground_truth = (
        "| Name | Value | Unit |\n|---|---|---|\n"
        "| Alpha | 100 | kg |\n| Beta | 250 | m |\n| Gamma | 3.14 | rad |"
    )
    return _png_bytes(img), ground_truth


def _draw_formula_page() -> tuple[bytes, str]:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    font = _font(28)
    draw.text((100, 400), "E = mc^2", fill="black", font=font)
    draw.text((100, 450), "a^2 + b^2 = c^2", fill="black", font=font)
    return _png_bytes(img), "E = mc^2\na^2 + b^2 = c^2"


def _draw_dense_page() -> tuple[bytes, str]:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(img)
    font = _font(14)
    left_lines = [f"Left column line {i}." for i in range(1, 25)]
    right_lines = [f"Right column line {i}." for i in range(1, 25)]
    y = 40
    for line in left_lines:
        draw.text((40, y), line, fill="black", font=font)
        y += 18
    y = 40
    for line in right_lines:
        draw.text((420, y), line, fill="black", font=font)
        y += 18
    gt = "\n".join(left_lines + right_lines)
    return _png_bytes(img), gt


def _make_multipage_pdf() -> bytes:
    """Render one text line per page and assemble a real multi-page PDF.

    Previously this created blank pages (no text was ever drawn), so the PDF
    rasterized to white and OCR returned nothing.
    """
    pages_text = ["Page one content.", "Page two content.", "Page three content."]
    font = _font()
    images: list[Image.Image] = []
    for text in pages_text:
        img = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
        # Draw in the page body (not the top margin) so layout classifies it as
        # body text; a top-of-page line is tagged "header" and dropped from the
        # document markdown.
        ImageDraw.Draw(img).text((40, CANVAS_H // 2), text, fill="black", font=font)
        images.append(img)
    buf = io.BytesIO()
    images[0].save(
        buf,
        format="PDF",
        save_all=True,
        append_images=images[1:],
        resolution=100.0,
        creationDate=time.gmtime(0),
        modDate=time.gmtime(0),
    )
    for image in images:
        image.close()
    return buf.getvalue()


def generate_calibration_set(output_dir: Path) -> list[CalibrationItem]:
    """Write deterministic image/PDF fixtures and their ground-truth manifest.

    Create the destination directory when absent and overwrite generated fixture
    files. Return the same ordered fixture metadata written to ground_truth.json.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    items: list[CalibrationItem] = []

    # plain_text.png
    text_lines = [
        "The quick brown fox",
        "jumps over the lazy dog.",
        "Lorem ipsum dolor sit amet.",
    ]
    (output_dir / "plain_text.png").write_bytes(_draw_text_page(text_lines))
    items.append(
        CalibrationItem("plain_text", "text", "plain_text.png", "\n".join(text_lines))
    )

    # simple_table.png
    tbl_bytes, tbl_gt = _draw_table_page()
    (output_dir / "simple_table.png").write_bytes(tbl_bytes)
    items.append(CalibrationItem("simple_table", "table", "simple_table.png", tbl_gt))

    # formula.png
    form_bytes, form_gt = _draw_formula_page()
    (output_dir / "formula.png").write_bytes(form_bytes)
    items.append(CalibrationItem("formula", "formula", "formula.png", form_gt))

    # dense_page.png
    dense_bytes, dense_gt = _draw_dense_page()
    (output_dir / "dense_page.png").write_bytes(dense_bytes)
    items.append(CalibrationItem("dense_page", "text", "dense_page.png", dense_gt))

    # multipage.pdf
    pdf_bytes = _make_multipage_pdf()
    (output_dir / "multipage.pdf").write_bytes(pdf_bytes)
    items.append(
        CalibrationItem(
            "multipage",
            "pdf",
            "multipage.pdf",
            "Page one content.\nPage two content.\nPage three content.",
        )
    )

    # Official IRS three-page form with dense fields, tables, and instructions.
    shutil.copyfile(SOURCE_PDF_PATH, output_dir / "form943.pdf")
    form943_ground_truth = SOURCE_GROUND_TRUTH_PATH.read_text(encoding="utf-8")
    items.append(
        CalibrationItem("irs_form_943", "pdf", "form943.pdf", form943_ground_truth)
    )

    gt_path = output_dir / "ground_truth.json"
    gt_path.write_text(
        json.dumps([asdict(i) for i in items], indent=2), encoding="utf-8"
    )
    return items


def load_ground_truth(path: Path) -> list[CalibrationItem]:
    """Load fixture metadata after validating names, kinds and confined paths.

    Raise ValueError for unreadable JSON, invalid records, duplicate names or
    paths that escape the calibration directory.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid calibration ground truth: {path}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path}: expected a nonempty list of fixtures")
    items = []
    names = set()
    for index, value in enumerate(data):
        context = f"{path}: fixture {index}"
        if not isinstance(value, dict):
            raise ValueError(f"{context}: expected an object")  # noqa: TRY004 - invalid JSON contract
        for field in ("name", "kind", "path", "ground_truth"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise ValueError(f"{context}: {field} must be a nonempty string")
        if value["name"] in names:
            raise ValueError(f"{context}: duplicate name {value['name']}")
        if value["kind"] not in {"text", "table", "formula", "pdf"}:
            raise ValueError(f"{context}: unknown kind")
        relative = PurePosixPath(value["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in value["path"]
            or ":" in value["path"]
        ):
            raise ValueError(f"{context}: path must be relative and inside calibration")
        resolved = (path.parent / relative).resolve()
        if not resolved.is_relative_to(path.parent.resolve()):
            raise ValueError(f"{context}: path escapes calibration")
        if not normalize_text(value["ground_truth"]):
            raise ValueError(f"{context}: ground_truth is empty after normalization")
        if not resolved.is_file():
            raise ValueError(f"{context}: missing fixture {value['path']}")
        names.add(value["name"])
        items.append(
            CalibrationItem(
                **{
                    field: value[field]
                    for field in ("name", "kind", "path", "ground_truth")
                }
            )
        )
    return items
