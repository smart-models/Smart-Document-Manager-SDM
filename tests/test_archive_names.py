"""Archive folder naming is stable, safe and mixed-layout compatible."""

from datetime import datetime, timedelta, timezone

import pytest

from ocrserver.archive_names import archive_prefix, parse_prefix, safe_filename


@pytest.mark.parametrize(
    ("original", "extension", "expected"),
    [
        ("C:\\private\\report final.pdf", "pdf", "report_final.pdf"),
        ("../../résumé.pdf", "pdf", "resume.pdf"),
        ("../../", "png", "input.png"),
        ("", "../PDF", "input.pdf"),
        ("unsafe name", "tiff", "unsafe_name"),
        ("x" * 200 + ".pdf", "pdf", "x" * 91 + ".pdf"),
        ("..\\..\\invoice.csv", "pdf", "invoice.csv"),
    ],
)
def test_filename_is_safe_basename_and_bounded(original, extension, expected):
    value = safe_filename(original, extension)

    assert value == expected
    assert len(value) <= 96
    assert "/" not in value and "\\" not in value
    assert value[0].isalnum()


def test_archive_prefix_uses_utc_and_round_trips_folder_components():
    local = datetime(2026, 9, 27, 14, 30, tzinfo=timezone(timedelta(hours=2)))

    prefix = archive_prefix("report.pdf", local, "Abcd123_")

    assert prefix == "report.pdf__20260927T123000000000Z__Abcd123_"
    assert parse_prefix(prefix + "/") == {
        "kind": None,
        "date": "2026-09-27",
        "job": "Abcd123_",
        "filename": "report.pdf",
        "stamp": "20260927T123000000000Z",
    }


@pytest.mark.parametrize(
    "prefix",
    [
        "report.pdf__20260230T123000000000Z__Abcd123_/",
        "report.pdf__20260927T123000000000Z__short/",
        "../report.pdf__20260927T123000000000Z__Abcd123_/",
        "parse/2026/13/01/" + "0" * 32 + "/",
    ],
)
def test_malformed_or_impossible_archive_names_are_rejected(prefix):
    assert parse_prefix(prefix) is None


def test_historical_archive_prefix_remains_valid():
    prefix = "parse/2026/09/27/" + "a" * 32 + "/"

    assert parse_prefix(prefix) == {
        "kind": "parse",
        "date": "2026-09-27",
        "job": "a" * 32,
        "filename": None,
        "stamp": "20260927T000000000000Z",
    }
