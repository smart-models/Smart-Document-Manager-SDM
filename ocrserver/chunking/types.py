"""Internal source identities, rendered fragments and bounded request work."""

from dataclasses import dataclass, field
from contextlib import contextmanager
from functools import cached_property
from time import process_time
from typing import Protocol

from .errors import ChunkError
from .text import normalize_text


VERSION = "structural-dp-v1.1"
PROFILE = {
    "name": VERSION,
    "max_reach": 128,
    "max_candidates": 16,
    "overlap_candidates": 16,
}


class Tokenizer(Protocol):
    """Provide token counts, character offsets and model compatibility checks."""

    @property
    def identity(self) -> dict:
        """Return the tokenizer identity recorded in chunk metadata."""
        ...

    def count(self, text: str) -> int:
        """Count tokens in text using the configured model vocabulary."""
        ...

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """Return token character ranges for bounded split estimation."""
        ...

    def assert_compatible(self, label: str | None) -> None:
        """Reject a requested model label incompatible with this tokenizer."""
        ...


@dataclass(frozen=True)
class ChunkLimits:
    """Bound input, output and logical work for one chunking request."""

    max_input_bytes: int = 33_554_432
    max_blocks: int = 20_000
    max_units: int = 20_000
    max_tokens: int = 32_768
    max_dp_edges: int = 320_000
    max_split_evaluations: int = 100_000
    max_token_evaluations: int = 1_100_000
    max_rendered_bytes: int = 268_435_456
    max_table_cells: int = 100_000
    max_output_chunks: int = 10_000
    max_output_bytes: int = 67_108_864


@dataclass
class Work:
    """Logical work counts include cache hits and are independent of load."""

    tokenizer: Tokenizer
    limits: ChunkLimits
    counters: dict[str, int] = field(default_factory=dict)
    physical_token_attempts: int = 0
    cpu_seconds: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def phase(self, name: str):
        """Measure actual worker CPU; measurements never affect limits."""
        started = process_time()
        try:
            yield
        finally:
            self.cpu_seconds[name] = (
                self.cpu_seconds.get(name, 0.0) + process_time() - started
            )

    def consume(self, name: str, amount: int = 1) -> None:
        """Charge a named work budget or raise ChunkError before exceeding it."""
        used = self.counters.get(name, 0) + amount
        limit = getattr(self.limits, "max_" + name)
        if used > limit:
            code = {
                "input_bytes": "chunk_input_limit",
                "output_bytes": "chunk_output_limit",
                "output_chunks": "chunk_output_limit",
            }.get(name, "chunk_work_limit")
            raise ChunkError(
                code,
                "Chunking work limit exceeded",
                status_code=413,
                details={"limit": name, "maximum": limit, "required": used},
            )
        self.counters[name] = used

    def count(self, text: str) -> int:
        """Charge rendered bytes and one evaluation before counting tokens."""
        self.consume("rendered_bytes", len(text.encode("utf-8")))
        self.consume("token_evaluations")
        self.physical_token_attempts += 1
        with self.phase("tokenization"):
            return self.tokenizer.count(text)

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """Charge rendered bytes and one evaluation before locating tokens."""
        self.consume("rendered_bytes", len(text.encode("utf-8")))
        self.consume("token_evaluations")
        self.physical_token_attempts += 1
        with self.phase("tokenization"):
            return self.tokenizer.offsets(text)


@dataclass(frozen=True)
class Source:
    """Retain an original OCR block and its document-local identity."""

    page: int
    index: int
    block: dict

    @property
    def key(self) -> str:
        """Return the stable page/block address used by provenance spans."""
        return f"p{self.page}:b{self.index}"

    @property
    def content(self) -> str:
        """Return unnormalized block text for source-relative offsets."""
        return self.block["content"]

    @property
    def tag(self) -> str:
        """Return the original layout block type."""
        return self.block["type"]

    @cached_property
    def normalized(self):
        """One immutable offset map per original block and request."""
        return normalize_text(self.content)

    def reference(self) -> dict:
        """Build provenance metadata, retaining available upstream identifiers."""
        result = {
            "source_key": self.key,
            "page": self.page,
            "block_index": self.index,
            "type": self.tag,
            "bbox": self.block.get("bbox"),
            "polygon": self.block.get("polygon"),
        }
        identifiers = {
            key: self.block[key]
            for key in (
                "block_id",
                "group_id",
                "global_block_id",
                "global_group_id",
            )
            if self.block.get(key) is not None
        }
        if identifiers:
            result["identifiers"] = identifiers
        return result


@dataclass
class Section:
    """Group ordered sources under a heading and its ancestor path."""

    id: str
    title_source: Source | None
    title: str | None
    path: list[dict]
    sources: list[Source] = field(default_factory=list)
    title_primary: bool = True


@dataclass(frozen=True)
class Fragment:
    """Text with local output offsets; only the final renderer shifts them."""

    text: str
    source_spans: tuple[dict, ...]
    context_spans: tuple[dict, ...] = ()
    kind: str = "prose"
    protected: str | None = None
    boundary_cost: int = 0
    diagnostics: tuple[dict, ...] = ()
    ignored_source_ranges: tuple[tuple[str, int, int], ...] = ()
    failure: ChunkError | None = None


def diagnostic(code: str, sources=(), *, severity="warning", **parameters) -> dict:
    """Build a diagnostic with source targets and sorted named parameters."""
    return {
        "code": code,
        "severity": severity,
        "targets": [
            {"source_key": source.key, "start": None, "end": None} for source in sources
        ],
        "parameters": [
            {"name": key, "value": value} for key, value in sorted(parameters.items())
        ],
    }


def generated_span(start: int, end: int, role="separator") -> dict:
    """Describe generated output text with no corresponding source characters."""
    return {
        "source_key": None,
        "page": None,
        "block_index": None,
        "type": None,
        "bbox": None,
        "polygon": None,
        "start": None,
        "end": None,
        "text_start": start,
        "text_end": end,
        "role": role,
    }


def budget_details(
    source, options, required, *, conflicting_option="max_tokens", minimum=None
):
    """Explain one measured conflict without claiming a document-wide minimum."""
    return {
        "page": source["page"],
        "source_key": source["source_key"],
        "type": source["type"],
        "max_tokens": options.max_tokens,
        "required_tokens": required,
        "conflicting_option": conflicting_option,
        "suggested_max_tokens": required,
        "minimum_feasible_max_tokens": minimum,
    }
