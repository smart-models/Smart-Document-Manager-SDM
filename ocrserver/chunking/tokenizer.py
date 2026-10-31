"""Mandatory local Paddle tokenization, independent from OCR annotation state."""

from __future__ import annotations

import hashlib
import re
import threading
from importlib.metadata import version
from pathlib import Path
from typing import Any

from ocrserver.chunking.errors import ChunkError
from ocrserver.schemas import RagTokenizer
from ocrserver.token_count import (
    MODEL_REPO,
    TokenizerLoader,
    resolve_tokenizer_path,
)


def _load(path: Path) -> Any:
    from tokenizers import Tokenizer

    return Tokenizer.from_file(str(path))


def _safe_revision(revision: str) -> bool:
    """User-supplied labels must never become paths outside the model cache."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", revision)) and all(
        part not in {"", ".", ".."} for part in revision.split("/")
    )


class PaddleTokenizer:
    """One lazily loaded local encoder with explicit RAG failure semantics.

    Load errors can be retried by the next request. A failure to encode one
    text never discards a successfully loaded encoder. No model download or
    fallback tokenizer is permitted.
    """

    def __init__(
        self,
        cache_dir: str,
        revision: str,
        *,
        loader: TokenizerLoader = _load,
    ) -> None:
        """Configure local loading without reading or downloading model assets.

        Args:
            cache_dir: Local model cache searched when tokenization is requested.
            revision: Configured snapshot revision or safe cache label.
            loader: Encoder factory invoked under the lazy initialization lock.
        """
        self._cache_dir = cache_dir
        self._revision = revision
        self._loader = loader
        self._encoder: Any = None
        self._identity: dict | None = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> dict:
        """Return the actual snapshot/hash identity, never a mutable cache view."""
        self._ensure_loaded()
        return dict(self._identity)

    def count(self, text: str) -> int:
        """Exact token count of the supplied final text, without special tokens."""
        return len(self._encode(text).ids)

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """Tokenizer spans in Python Unicode code points, not UTF-8 bytes."""
        encoded = self._encode(text)
        try:
            offsets = [tuple(pair) for pair in encoded.offsets]
            if any(
                len(pair) != 2
                or any(type(value) is not int for value in pair)
                or not 0 <= pair[0] <= pair[1] <= len(text)
                for pair in offsets
            ):
                raise ValueError("invalid codepoint offsets")
            return offsets
        except Exception as exc:
            raise ChunkError(
                "tokenization_failed",
                "Paddle token offsets are unavailable.",
            ) from exc

    def assert_compatible(self, label: str | None) -> None:
        """Reject an upstream tokenizer revision differing from the local one."""
        identity = self.identity
        if label is None:
            return
        repo, separator, declared = label.partition("@")
        compatible = repo == MODEL_REPO and separator and _safe_revision(declared)
        actual = declared
        if compatible and declared != identity["revision"]:
            path = resolve_tokenizer_path(self._cache_dir, declared)
            actual = path.parent.name if path is not None else None
        if not compatible or actual != identity["revision"]:
            raise ChunkError(
                "tokenizer_mismatch",
                "The OCR tokenizer revision differs from the local tokenizer.",
                details={"expected": f"{MODEL_REPO}@{identity['revision']}"},
            )

    def _ensure_loaded(self) -> Any:
        """Load once under a lock, retaining retryability after loading failures."""
        if self._encoder is not None:
            return self._encoder
        with self._lock:
            if self._encoder is not None:
                return self._encoder
            try:
                if not self._cache_dir or not _safe_revision(self._revision):
                    raise ValueError("missing cache or invalid configured revision")
                path = resolve_tokenizer_path(self._cache_dir, self._revision)
                if path is None:
                    raise FileNotFoundError("configured local tokenizer is absent")
                revision = path.parent.name
                identity = RagTokenizer(
                    repo=MODEL_REPO,
                    revision=revision,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    library_version=version("tokenizers"),
                ).model_dump()
                encoder = self._loader(path)
                if encoder is None:
                    raise ValueError("loader did not return a tokenizer")
            except Exception as exc:
                raise ChunkError(
                    "tokenizer_unavailable",
                    "The configured local Paddle tokenizer could not be loaded.",
                    status_code=503,
                ) from exc
            self._identity = identity
            self._encoder = encoder
        return self._encoder

    def _encode(self, text: str) -> Any:
        """Encode without special tokens and translate backend failures."""
        encoder = self._ensure_loaded()
        try:
            return encoder.encode(text, add_special_tokens=False)
        except Exception as exc:
            raise ChunkError(
                "tokenization_failed",
                "Paddle could not encode this content.",
            ) from exc
