"""RFC 8785 bytes for files, identities and deterministic metadata ordering."""

from __future__ import annotations

import hashlib

import rfc8785

from ocrserver.chunking.errors import ChunkError


def canonical_bytes(value: object) -> bytes:
    """Encode JCS; invalid Unicode/numbers cannot produce a RAG artifact."""
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ChunkError(
            "invalid_structure",
            "Input cannot be represented as canonical JSON.",
            details={"reason": "noncanonical_json_value"},
        ) from exc


def canonical_hash(value: object) -> str:
    """SHA-256 of the normative canonical projection supplied by the caller."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
