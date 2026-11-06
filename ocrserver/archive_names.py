"""Names and validation for current and historical storage archives."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, timezone

KINDS = ("ocr", "parse", "parse-chunks")
# Microsecond UTC stamp embedded in current archive addresses.
STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_OLD = re.compile(
    r"(?P<kind>ocr|parse|parse-chunks)/(?P<year>\d{4})/"
    r"(?P<month>\d{2})/(?P<day>\d{2})/(?P<job>[0-9a-f]{32})/"
)
_NEW = re.compile(
    r"(?P<filename>[A-Za-z0-9][A-Za-z0-9._-]{0,95})__"
    r"(?P<stamp>\d{8}T\d{12}Z)__(?P<job>[A-Za-z0-9_-]{8})/"
)


def safe_filename(original: str | None, extension: str = "bin") -> str:
    """Keep a readable basename while excluding path separators and unsafe keys."""
    extension = re.sub(r"[^A-Za-z0-9]", "", extension or "")[:12].lower() or "bin"
    base = re.split(r"[/\\]", original or "")[-1].strip()
    ascii_name = (
        unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    )
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name)
    clean = re.sub(r"_+", "_", clean).strip("._-")
    if not clean:
        clean = f"input.{extension}"
    stem, dot, suffix = clean.rpartition(".")
    if dot and stem and len(suffix) <= 12:
        clean = stem[: 95 - len(suffix) - 1].rstrip("._-") + dot + suffix
    return clean[:96].rstrip("._-") or f"input.{extension}"


def archive_prefix(filename: str, when: datetime, code: str) -> str:
    """One root folder per request: filename, UTC timestamp, eight-char code."""
    stamp = when.astimezone(timezone.utc).strftime(STAMP_FORMAT)
    return f"{filename}__{stamp}__{code}"


def parse_prefix(value: str) -> dict[str, str | None] | None:
    """Return verified address fields for both archive layouts, or None.

    Fields are kind (legacy layout only), date (ISO), job, filename (current
    layout only) and stamp, whose format is STAMP_FORMAT. Legacy addresses carry
    only a day, so their stamp is that day's midnight.
    """
    old = _OLD.fullmatch(value)
    if old:
        try:
            day = date(int(old["year"]), int(old["month"]), int(old["day"]))
        except ValueError:
            return None
        return {
            "kind": old["kind"],
            "date": day.isoformat(),
            "job": old["job"],
            "filename": None,
            "stamp": day.strftime("%Y%m%d") + "T000000000000Z",
        }
    new = _NEW.fullmatch(value)
    if new:
        try:
            stamp = datetime.strptime(new["stamp"], STAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            return None
        return {
            "kind": None,
            "date": stamp.date().isoformat(),
            "job": new["job"],
            "filename": new["filename"],
            "stamp": new["stamp"],
        }
    return None


def prefix_from_key(key: str) -> str | None:
    """Extract the archive prefix from an object key, if it is well formed."""
    for pattern in (_NEW, _OLD):
        match = pattern.match(key)
        if match and parse_prefix(match[0]):
            return match[0]
    return None
