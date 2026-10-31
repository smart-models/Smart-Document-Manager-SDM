"""Generate local storage credentials without displaying or replacing secrets."""

from __future__ import annotations

import argparse
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configure(path: Path) -> list[str]:
    """Enable the Compose storage profile, preserving existing credentials."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match[1]] = match[2].strip("\"'")
    desired = {
        "OCR_S3_ADMIN_ACCESS_KEY": "ocradmin" + secrets.token_hex(8),
        "OCR_S3_ADMIN_SECRET_KEY": secrets.token_hex(32),
        "OCR_S3_ACCESS_KEY": "ocrwriter" + secrets.token_hex(8),
        "OCR_S3_SECRET_KEY": secrets.token_hex(32),
        "OCR_S3_READER_ACCESS_KEY": "ocrreader" + secrets.token_hex(8),
        "OCR_S3_READER_SECRET_KEY": secrets.token_hex(32),
        "OCR_S3_BROWSER_ACCESS_KEY": "ocrbrowser" + secrets.token_hex(8),
        "OCR_S3_BROWSER_SECRET_KEY": secrets.token_hex(32),
        "OCR_SEAWEED_ADMIN_PASSWORD": secrets.token_hex(24),
        "OCR_SEAWEED_READER_PASSWORD": secrets.token_hex(24),
        "OCR_S3_BUCKET": "ocr-outputs",
        "OCR_S3_RETENTION_DAYS": "30",
        "OCR_STORAGE_SAVE_INPUT": "false",
        "OCR_STORAGE_SAVE_PARSED": "true",
    }
    updates = {key: value for key, value in desired.items() if not values.get(key)}
    profiles = [
        value.strip()
        for value in values.get("COMPOSE_PROFILES", "").split(",")
        if value.strip()
    ]
    if "storage" not in profiles:
        profiles.append("storage")
    updates["COMPOSE_PROFILES"] = ",".join(profiles)
    updates["OCR_STORAGE_ENABLED"] = "true"
    updates["OCR_STORAGE_BROWSER_ENABLED"] = "true"
    for key, value in updates.items():
        expression = re.compile(r"^\s*" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
        replacement = key + "=" + value
        if expression.search(text):
            text = expression.sub(lambda _, replacement=replacement: replacement, text)
        else:
            text = text.rstrip("\r\n") + "\n" + replacement + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return sorted(updates)


def main() -> None:
    """Parse CLI arguments and populate missing storage credentials in .env."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    configure(args.env_file)
    print("Storage profile enabled; credentials saved locally (not displayed).")


if __name__ == "__main__":
    main()
