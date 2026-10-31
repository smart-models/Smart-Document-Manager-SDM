"""Validate OCR/MCP and storage credentials from Compose-resolved stdin only."""

from __future__ import annotations

import re
import sys
from typing import Mapping

if __package__:
    from .stack_core import ConfigurationError, boolean, parse_input
else:
    from stack_core import ConfigurationError, boolean, parse_input


def _token_entries(key: str, values: Mapping[str, str], errors: list[str]) -> set:
    """Return the tokens of a ``token:label`` list, reporting bad entries."""
    tokens, labels = set(), set()
    for position, entry in enumerate(values.get(key, "").split(","), 1):
        if not entry.strip():
            continue
        token, separator, label = entry.strip().partition(":")
        token, label = token.strip(), label.strip()
        if not separator or not token or not re.fullmatch(r"[A-Za-z0-9._-]+", label):
            errors.append(
                f"{key} entry #{position} must use token:label "
                "with a valid label; value withheld."
            )
            continue
        if token in tokens or label in labels:
            errors.append(
                f"{key} entry #{position} repeats a token or label; values withheld."
            )
        tokens.add(token)
        labels.add(label)
    return tokens


def validate_config(values: Mapping[str, str]) -> list[str]:
    """Return variable names and failure reasons; never include secret values."""
    errors = []

    def enabled(key: str, default: bool = False) -> bool:
        try:
            return boolean(values.get(key, ""), default, key)
        except ConfigurationError as error:
            errors.append(str(error))
            return default

    if values.get("OCR_SERVER_API_TOKEN", "").strip():
        errors.append(
            "OCR_SERVER_API_TOKEN was replaced by OCR_SERVER_API_TOKENS: rewrite "
            "it as token:label; value withheld."
        )
    api = _token_entries("OCR_SERVER_API_TOKENS", values, errors)
    upstream = values.get("UPSTREAM_CLIENT_TOKEN", "")
    if not upstream.strip():
        upstream = ""
    if upstream in api:
        errors.append(
            "UPSTREAM_CLIENT_TOKEN is accepted by ocr-api automatically; do not "
            "also list it in OCR_SERVER_API_TOKENS."
        )
    if enabled("MCP_SERVER_ENABLED", True):
        if api and not upstream:
            errors.append(
                "UPSTREAM_CLIENT_TOKEN is required when OCR_SERVER_API_TOKENS "
                "enables API authentication: ocr-mcp presents it to ocr-api."
            )
        tokens = _token_entries("MCP_API_TOKENS", values, errors)
        if not tokens:
            errors.append(
                "MCP_API_TOKENS is empty: configure inbound tokens or start "
                "with --no-mcp / -NoMcp."
            )
        if tokens.intersection((api | {upstream}) - {""}):
            errors.append(
                "MCP_API_TOKENS must be distinct from OCR_SERVER_API_TOKENS and "
                "UPSTREAM_CLIENT_TOKEN; credentials must not open both services."
            )

    storage = "storage" in {
        item.strip() for item in values.get("COMPOSE_PROFILES", "").split(",")
    }
    required = set()
    if enabled("OCR_STORAGE_ENABLED"):
        required.update({"OCR_S3_ACCESS_KEY", "OCR_S3_SECRET_KEY"})
    if enabled("OCR_STORAGE_BROWSER_ENABLED"):
        required.update({"OCR_S3_BROWSER_ACCESS_KEY", "OCR_S3_BROWSER_SECRET_KEY"})
    if storage:
        required.update({"OCR_SEAWEED_ADMIN_PASSWORD", "OCR_SEAWEED_READER_PASSWORD"})
        for role in ("ADMIN_", "", "READER_", "BROWSER_"):
            required.update(
                f"OCR_S3_{role}{kind}" for kind in ("ACCESS_KEY", "SECRET_KEY")
            )
        retention = values.get("OCR_S3_RETENTION_DAYS", "") or "30"
        if not retention.isascii() or not retention.isdigit():
            errors.append("OCR_S3_RETENTION_DAYS must be an integer >= 0.")
        for kind in ("ACCESS_KEY", "SECRET_KEY"):
            credentials = [
                values.get(f"OCR_S3_{role}{kind}", "")
                for role in ("ADMIN_", "", "READER_", "BROWSER_")
            ]
            present = [value for value in credentials if value]
            if len(present) != len(set(present)):
                errors.append(
                    f"Storage {kind} credentials must be distinct for admin, "
                    "writer, reader and browser; values withheld."
                )
    missing = sorted(key for key in required if not values.get(key, "").strip())
    if missing:
        errors.append(
            "Missing storage credentials: "
            + ", ".join(missing)
            + ". Run python scripts/configure_storage.py --env-file <your-env-file>."
        )
    return errors


def main() -> int:
    """Validate token and storage settings from stdin and return a CLI exit code."""
    values, _ = parse_input(sys.stdin.read())
    errors = validate_config(values)
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)
    if not errors:
        print("Token and storage configuration: OK (credential values withheld).")
        if not values.get("OCR_SERVER_API_TOKENS", "").strip():
            print(
                "[WARN] OCR API authentication is disabled; "
                "keep the host loopback boundary.",
                file=sys.stderr,
            )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
