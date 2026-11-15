"""Verify the pure rules that mark a badly read page or block."""

import pytest

from ocrserver import quality

# 1,000 x 1,000 px: one megapixel at the 200 dpi the thresholds were measured at.
BOX = [0, 0, 1000, 1000]
ROW = "<tr><td>Unit {n}</td><td>{n} bears</td><td>July {n}</td></tr>"


def table(*rows: str) -> str:
    return "<table>" + "".join(rows) + "</table>"


def healthy_table(rows: int = 30) -> str:
    return table(*(ROW.format(n=n) for n in range(rows)))


def test_layout_score_is_the_median_of_body_boxes():
    boxes = [
        {"label": "text", "score": 0.31},
        {"label": "text", "score": 0.35},
        {"label": "table", "score": 0.44},
        {"label": "header", "score": 0.99},
        {"label": "number", "score": 0.98},
        {"label": "footer", "score": 0.97},
    ]
    assert quality.layout_score(boxes) == 0.35


def test_layout_score_is_unknown_without_a_scored_body_box():
    assert quality.layout_score([]) is None
    assert quality.layout_score([{"label": "header", "score": 0.9}]) is None
    assert quality.layout_score([{"label": "text"}]) is None


def test_megapixels_are_scaled_to_the_reference_resolution():
    assert quality.megapixels(BOX) == 1.0
    assert quality.megapixels(BOX, 200) == 1.0
    assert quality.megapixels(BOX, 250) == pytest.approx(0.64)
    assert quality.megapixels([10, 10, 5, 5]) == 0.0
    assert quality.megapixels([]) == 0.0


def test_healthy_table_is_not_suspect():
    assert quality.suspect_reason("table", BOX, healthy_table()) is None


def test_only_large_table_and_content_blocks_are_judged():
    stub = table(ROW.format(n=1))
    assert quality.suspect_reason("table", BOX, stub) == "low_density"
    assert quality.suspect_reason("text", BOX, stub) is None
    assert quality.suspect_reason("table", [0, 0, 400, 400], stub) is None


def test_empty_large_table_is_low_density():
    assert quality.suspect_reason("table", BOX, "") == "low_density"


def test_thresholds_follow_the_page_resolution():
    # 600 characters on one megapixel pass at 200 dpi; the same pixels at
    # 100 dpi are four megapixels of the reference page, so the text is thin.
    text = "\n".join(f"Entry {n} of the list" for n in range(30))
    assert 500 <= len(text) < 2000
    assert quality.suspect_reason("content", BOX, text, 200) is None
    assert quality.suspect_reason("content", BOX, text, 100) == "low_density"
    # 0.3 MP at 250 dpi is under the 0.25 MP floor of the reference page.
    small = [0, 0, 600, 500]
    assert quality.suspect_reason("table", small, "") == "low_density"
    assert quality.suspect_reason("table", small, "", 250) is None


@pytest.mark.parametrize(
    "last_cell",
    ["Rule 17ad-12 ..", "Bear, black: 2 bears .", "2021 . . .", "In Effect ...."],
)
def test_cut_dot_leader_at_the_end_is_suspect(last_cell):
    content = healthy_table()[: -len("</table>")] + (
        f"<tr><td>{last_cell}</td><td></td><td></td></tr></table>"
    )
    assert quality.suspect_reason("table", BOX, content) == "cut_repetition"


@pytest.mark.parametrize(
    "last_cell",
    ["Total .................", "Total .....", "It ends here.", "1,234"],
)
def test_legitimate_table_endings_are_not_suspect(last_cell):
    content = healthy_table()[: -len("</table>")] + (
        f"<tr><td>{last_cell}</td><td></td><td></td></tr></table>"
    )
    assert quality.suspect_reason("table", BOX, content) is None


def test_cut_dot_leader_is_found_in_a_plain_list():
    lines = [f"{n} CFR {n}.....{60000 + n}" for n in range(40)] + ["Proposed ..."]
    assert quality.suspect_reason("content", BOX, "\n".join(lines)) == "cut_repetition"


def test_four_identical_rows_in_a_row_are_suspect():
    rows = [ROW.format(n=n) for n in range(30)]
    rows[10:10] = [ROW.format(n=99)] * 4
    assert quality.suspect_reason("table", BOX, table(*rows)) == "repeated_rows"


def test_tail_of_copied_rows_is_suspect():
    head = [ROW.format(n=n) for n in range(30)]
    loop = [ROW.format(n=101), ROW.format(n=102)] * 3
    cut = "<tr><td>Unit</td><td></td><td></td></tr>"
    content = table(*head, *loop, cut)
    assert quality.suspect_reason("table", BOX, content) == "repeated_rows"


def test_identical_cells_in_distinct_rows_are_not_suspect():
    rows = [
        f"<tr><td>Event {n}</td><td>Fireworks Display.</td><td>Do.</td></tr>"
        for n in range(30)
    ]
    assert quality.suspect_reason("table", BOX, table(*rows)) is None


def test_three_identical_rows_are_not_suspect():
    rows = [ROW.format(n=n) for n in range(30)]
    rows[10:10] = [ROW.format(n=99)] * 3
    assert quality.suspect_reason("table", BOX, table(*rows)) is None


def test_literal_backslash_n_inside_a_line_does_not_split_it():
    # LaTeX written out by the reader: one line, not five rows of "abla".
    lines = [f"Entry {n} of the list" for n in range(40)]
    lines[10] = r"$\nabla\nabla\nabla\nabla\nabla f$ is written out"
    assert quality.suspect_reason("content", BOX, "\n".join(lines)) is None


def test_reread_enlarges_small_crops_and_reduces_large_ones():
    assert quality.reread_options(1000, 800) == {
        "min_pixels": 2_007_040,
        "max_pixels": 2_007_040,
    }
    assert quality.reread_options(1120, 896) == {
        "min_pixels": 2_007_040,
        "max_pixels": 2_007_040,
    }  # exactly the reader's own limit: still the same image as the first read
    assert quality.reread_options(1500, 1400) == {"max_pixels": 2_007_040}


def test_reread_is_kept_only_when_silent_and_longer():
    stub = table(ROW.format(n=1))
    full = healthy_table()
    assert quality.keeps_reread("low_density", stub, full, "table", BOX)
    assert not quality.keeps_reread("low_density", stub, stub, "table", BOX)
    assert not quality.keeps_reread("cut_repetition", full, full[:-200], "table", BOX)
    cut = full[: -len("</table>")] + "<tr><td>Unit ..</td><td></td></tr></table>"
    assert not quality.keeps_reread("low_density", stub, cut, "table", BOX)


def test_reread_of_a_row_loop_is_kept_even_when_shorter():
    rows = [ROW.format(n=n) for n in range(30)]
    looping = table(*rows, *[ROW.format(n=99)] * 40)
    assert quality.suspect_reason("table", BOX, looping) == "repeated_rows"
    fixed = table(*rows)
    assert len(fixed) < len(looping)
    assert quality.keeps_reread("repeated_rows", looping, fixed, "table", BOX)
