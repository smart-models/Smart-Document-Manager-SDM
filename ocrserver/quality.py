"""Signs that the layout model or the reading model failed on a page.

Pure rules over what PaddleOCR-VL returns: no model, no I/O. They mark a page
whose layout boxes all scored low, and a table or list block that the reading
model looped on until its token cap, which PaddleX then cuts to a stub.

Every threshold was measured on 13,860 Federal Register pages parsed at 200 dpi
(31 looping blocks, 66 healthy ones read again, 1,198 healthy stored ones). The
evidence, with the scripts that reproduce each figure, is kept outside this
repository in the 3S workspace:
``docs/benchmark-federal-register/ocr-diagnosis-2026-11-13/`` and its
``probe-2026-11-14/`` folder. They are constants, not settings, until a second
corpus disagrees with them.
"""

from __future__ import annotations

import html
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence

# Median score of the layout boxes under which a page loses text: 0 of 101
# healthy pages fall below it (their lowest is 0.58).
LAYOUT_SCORE_MIN = 0.5
# Running heads, folios and footers score high even on a page whose body does not.
MARGINAL_LABELS = frozenset({"number", "header", "footer"})

# The reading loops were all found on tables and on lists ("content" blocks).
SUSPECT_LABELS = frozenset({"table", "content"})
# Resolution the areas and the density below were measured at.
REFERENCE_DPI = 200
MIN_MEGAPIXELS = 0.25
# Characters of block content per megapixel: 0 of 1,198 healthy blocks are under.
MIN_DENSITY = 500.0
# Identical rows in a row, or rows at the end that copy earlier ones.
REPEATED_ROWS = 4
# PaddleX reads a crop at this many pixels at most; a smaller crop is sent as is.
READ_MAX_PIXELS = 1_003_520
# A second reading at this size leaves the loop in 29 of 31 cases.
REREAD_PIXELS = 2_007_040

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
# What PaddleX's cut leaves of a dot leader that never stopped: it removes the
# repeated unit five times over, so at most four leader characters stay after
# the label. A printed leader is longer, and a sentence ends without the space.
_CUT_LEADER = re.compile(r"[^\s.]\s+\.(?:\s?\.){0,3}\s*$")


def layout_score(boxes: Iterable[dict]) -> float | None:
    """Median score of a page's layout boxes, marginal labels left out.

    None when the page has no scored body box, so nothing can be said.
    """
    scores = [
        float(box["score"])
        for box in boxes
        if box.get("label") not in MARGINAL_LABELS
        and isinstance(box.get("score"), (int, float))
    ]
    return statistics.median(scores) if scores else None


def megapixels(bbox: Sequence[float], dpi: int | None = None) -> float:
    """Area of a block in megapixels of a 200 dpi page.

    ``dpi`` is the resolution the page was rendered at; an image input has
    none and its pixels are taken as they are.
    """
    if len(bbox) != 4:
        return 0.0
    area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
    if dpi:
        area *= (REFERENCE_DPI / dpi) ** 2
    return area / 1e6


def _rows(content: str) -> list[list[str]]:
    """Rows of cells: of an HTML table, else one cell for each text line."""
    if "<table" in content:
        return [
            [" ".join(html.unescape(_TAG.sub(" ", cell)).split()) for cell in cells]
            for cells in (_CELL.findall(row) for row in _ROW.findall(content))
        ]
    return [[line.strip()] for line in content.splitlines() if line.strip()]


def _repeats(rows: list[list[str]]) -> bool:
    """Whole rows repeated the way a reading loop repeats them.

    A loop runs until the token cap, so its copies are consecutive or sit at
    the end of the text; the last row is ignored because the cap may cut it.
    """
    flat = [" | ".join(row) for row in rows]
    run, previous = 0, None
    for row in flat:
        run = run + 1 if row == previous and row.strip(" |") else 1
        if run >= REPEATED_ROWS:
            return True
        previous = row
    seen = Counter(flat[:-1])
    tail = 0
    for row in reversed(flat[:-1]):
        if seen[row] < 2 or not row.strip(" |"):
            break
        tail += 1
    return tail >= REPEATED_ROWS


def suspect_reason(
    label: str, bbox: Sequence[float], content: str, dpi: int | None = None
) -> str | None:
    """Why a block looks like a reading loop, or None.

    Only table and list blocks of 0.25 megapixels or more are judged.
    ``repeated_rows``: the loop's text is still there. ``cut_repetition``: the
    text ends where PaddleX cut a runaway dot leader. ``low_density``: so few
    characters for the block's size that most of it is missing.
    """
    size = megapixels(bbox, dpi)
    if label not in SUSPECT_LABELS or size < MIN_MEGAPIXELS:
        return None
    rows = _rows(content)
    if _repeats(rows):
        return "repeated_rows"
    cells = [cell for row in rows for cell in row if cell]
    if cells and _CUT_LEADER.search(cells[-1]):
        return "cut_repetition"
    if len(content) / size < MIN_DENSITY:
        return "low_density"
    return None


def reread_options(width: int, height: int) -> dict[str, int]:
    """Pixel bounds of the second reading of a crop: two megapixels.

    A crop within the reader's own limit is enlarged to that size, because
    sent again unchanged it is the same request and loops the same way.
    """
    options = {"max_pixels": REREAD_PIXELS}
    if width * height <= READ_MAX_PIXELS:
        options = {"min_pixels": REREAD_PIXELS, **options}
    return options


def keeps_reread(
    reason: str,
    first: str,
    second: str,
    label: str,
    bbox: Sequence[float],
    dpi: int | None = None,
) -> bool:
    """Whether a second reading replaces the first.

    It must leave every rule silent and say more than the first reading. The
    length is not compared after ``repeated_rows``: there the first reading is
    long because of the loop.
    """
    if suspect_reason(label, bbox, second, dpi) is not None:
        return False
    return reason == "repeated_rows" or len(second) > len(first)
