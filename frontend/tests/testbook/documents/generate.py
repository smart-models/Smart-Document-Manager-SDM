"""Generate the testbook documents that no licensed upstream file covers.

Run from anywhere: python frontend/tests/testbook/documents/generate.py
Writes chart.png, long.pdf, multiframe.tif and sample.docx, then refreshes every
sha256 in manifest.json from the files on disk. Text is drawn with the bundled
calibration font, so no download is needed.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT_PATH = HERE.parents[3] / "calibration" / "fonts" / "DejaVuSans.ttf"
LONG_PDF_PAGES = 30  # Over MAX_BLOCK_PAGES (24): at least two blocks at any DPI.
FRAME_WORDS = ("amber", "cobalt", "violet")


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _chart() -> bytes:
    """Bar chart whose values are printed on the bars: the chart-task oracle."""
    bars = {"Q1": 120, "Q2": 180, "Q3": 150, "Q4": 210}
    img = Image.new("RGB", (800, 520), "white")
    draw = ImageDraw.Draw(img)
    draw.text((200, 20), "Quarterly revenue 2026", fill="black", font=_font(28))
    base, scale = 450, 1.5
    draw.line([(80, 80), (80, base)], fill="black", width=2)
    draw.line([(80, base), (760, base)], fill="black", width=2)
    for i, (label, value) in enumerate(bars.items()):
        x = 130 + i * 160
        top = base - int(value * scale)
        draw.rectangle([(x, top), (x + 90, base)], fill="#3b6fb6")
        draw.text((x + 22, top - 32), str(value), fill="black", font=_font(22))
        draw.text((x + 26, base + 12), label, fill="black", font=_font(22))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _long_pdf() -> bytes:
    """US Letter pages with a numbered body line, enough for two parse blocks."""
    pages = []
    for number in range(1, LONG_PDF_PAGES + 1):
        img = Image.new("L", (850, 1100), "white")
        draw = ImageDraw.Draw(img)
        # Body, not the top margin: layout tags a top line as a header.
        draw.text(
            (80, 480),
            f"Corpus page {number:02d} of {LONG_PDF_PAGES}",
            fill="black",
            font=_font(28),
        )
        draw.text(
            (80, 530),
            "Block progress and cancellation fixture.",
            fill="black",
            font=_font(20),
        )
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(
        buf,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=100.0,
        creationDate=time.gmtime(0),
        modDate=time.gmtime(0),
    )
    return buf.getvalue()


def _multiframe_tiff() -> bytes:
    """One word per frame: /parse splits the frames into pages, /ocr rejects them."""
    frames = []
    for number, word in enumerate(FRAME_WORDS, start=1):
        img = Image.new("L", (800, 300), "white")
        draw = ImageDraw.Draw(img)
        draw.text(
            (60, 120),
            f"Frame {number} of {len(FRAME_WORDS)}: {word}",
            fill="black",
            font=_font(32),
        )
        frames.append(img)
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="TIFF",
        save_all=True,
        append_images=frames[1:],
        compression="tiff_deflate",
    )
    return buf.getvalue()


_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    ),
}


def _docx() -> bytes:
    """Minimal WordprocessingML: a heading, a paragraph and a bordered table."""

    def para(text: str, size: int = 24, bold: bool = False) -> str:
        props = f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr>'
        return f"<w:p><w:r>{props}<w:t>{text}</w:t></w:r></w:p>"

    border = '<w:{0} w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
    borders = "".join(
        border.format(side)
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    rows = [
        ("City", "Country", "Population"),
        ("Oslo", "Norway", "717710"),
        ("Lisbon", "Portugal", "567131"),
    ]
    table = "".join(
        "<w:tr>"
        + "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
        )
        + "</w:tr>"
        for row in rows
    )
    table_props = f"<w:tblPr><w:tblBorders>{borders}</w:tblBorders></w:tblPr>"
    body = (
        para("Smart Document Manager office sample", size=36, bold=True)
        + para("This Word document is converted to PDF by LibreOffice before parsing.")
        + f"<w:tbl>{table_props}{table}</w:tbl>"
        + para("End of the office sample.")
    )
    parts = {
        **_DOCX_PARTS,
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        ),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), xml
            )
    return buf.getvalue()


def main() -> None:
    for name, build in (
        ("chart.png", _chart),
        ("long.pdf", _long_pdf),
        ("multiframe.tif", _multiframe_tiff),
        ("sample.docx", _docx),
    ):
        (HERE / name).write_bytes(build())
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        entry["sha256"] = hashlib.sha256(
            (HERE / entry["path"]).read_bytes()
        ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
