"""Token counts for parsed block content using the served model tokenizer.

The tokenizer ships inside the vLLM Hugging Face cache, mounted read-only into
the API container. Counting is best-effort: when the cache, the revision, or
the ``tokenizers`` package is unavailable, counters return ``None`` and the API
omits token fields instead of failing a request. Counts describe the parsed
text (HTML tables, LaTeX formulas); they are not the tokens the VL model
actually consumed for each crop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ocrserver.schemas import Block, TokenUsage

logger = logging.getLogger(__name__)

MODEL_REPO = "PaddlePaddle/PaddleOCR-VL-1.6"
TokenizerLoader = Callable[[Path], Any]


def _load_tokenizer(path: Path) -> Any:
    """Import ``tokenizers`` lazily so the API runs without the wheel."""
    from tokenizers import Tokenizer

    return Tokenizer.from_file(str(path))


def resolve_tokenizer_path(cache_dir: str, revision: str) -> Path | None:
    """Locate ``tokenizer.json`` inside a Hugging Face cache.

    ``revision`` may be a commit hash (used directly as snapshot directory) or
    a branch/tag, resolved through the cache's ``refs`` file first.
    """
    if not cache_dir or not revision:
        return None
    repo = Path(cache_dir) / "hub" / f"models--{MODEL_REPO.replace('/', '--')}"
    snapshots = [repo / "snapshots" / revision]
    ref = repo / "refs" / revision
    if ref.is_file():
        try:
            commit = ref.read_text(encoding="utf-8").strip()
        except OSError:
            commit = ""
        if commit:
            snapshots.insert(0, repo / "snapshots" / commit)
    for snapshot in snapshots:
        candidate = snapshot / "tokenizer.json"
        if candidate.is_file():
            return candidate
    return None


class ContentTokenCounter:
    """Lazily loaded tokenizer that annotates blocks with content tokens."""

    def __init__(
        self,
        cache_dir: str = "",
        revision: str = "",
        loader: TokenizerLoader = _load_tokenizer,
    ) -> None:
        """Configure lazy local tokenization without reading model assets yet."""
        self._cache_dir = cache_dir
        self._revision = revision
        self._loader = loader
        self._tokenizer: Any = None
        self._unavailable = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True when a cache directory and a revision were configured."""
        return bool(self._cache_dir and self._revision)

    @property
    def label(self) -> str:
        """Tokenizer identity reported in ``usage``."""
        return f"{MODEL_REPO}@{self._revision}"

    def count(self, text: str) -> int | None:
        """Tokens in ``text``; ``None`` when the tokenizer is unavailable."""
        encoder = self._ensure_tokenizer()
        if encoder is None:
            return None
        try:
            return len(encoder.encode(text, add_special_tokens=False).ids)
        except Exception:
            self._disable("the tokenizer failed to encode block content")
            return None

    def annotate(self, blocks: Iterable[Block]) -> TokenUsage | None:
        """Set ``content_tokens`` on every block and return the totals.

        All counts are computed before any block is mutated so a tokenizer
        failure cannot leave a partially annotated page behind.
        """
        if not self.enabled:
            return None
        blocks = list(blocks)
        counts: list[int] = []
        for block in blocks:
            tokens = 0 if not block.content else self.count(block.content)
            if tokens is None:
                return None
            counts.append(tokens)
        for block, tokens in zip(blocks, counts, strict=True):
            block.content_tokens = tokens
        return TokenUsage(
            tokenizer=self.label,
            content_tokens=sum(counts),
            blocks=len(counts),
        )

    def _ensure_tokenizer(self) -> Any:
        if self._tokenizer is not None or self._unavailable or not self.enabled:
            return self._tokenizer
        with self._lock:
            if self._tokenizer is not None or self._unavailable:
                return self._tokenizer
            path = resolve_tokenizer_path(self._cache_dir, self._revision)
            if path is None:
                self._disable(
                    f"tokenizer.json not found under {self._cache_dir!r} "
                    f"(revision {self._revision})"
                )
                return None
            try:
                self._tokenizer = self._loader(path)
            except Exception:
                self._unavailable = True
                logger.warning(
                    "Token counting disabled: could not load %s", path, exc_info=True
                )
                return None
        return self._tokenizer

    def _disable(self, message: str) -> None:
        self._unavailable = True
        logger.warning("Token counting disabled: %s", message)
