"""Stable domain errors shared by CPU chunking and its transports."""

from __future__ import annotations


class ChunkError(Exception):
    """An atomic RAG failure, without including document text in messages."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: dict | None = None,
    ) -> None:
        """Store a transport status and structured details alongside the message.

        Args:
            code: Stable machine-readable failure category.
            message: Public explanation without document text or credentials.
            status_code: HTTP status used when translating the domain failure.
            details: Additional JSON-safe context, retained without copying.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = {} if details is None else details

    def to_detail(self) -> dict:
        """The JSON-safe body inside FastAPI's conventional detail property."""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
