"""Bounded operational measurements kept outside deterministic RAG files."""

from bisect import bisect_left
from collections import Counter, defaultdict
from functools import lru_cache
from threading import Lock

import psutil
from prometheus_client import (
    REGISTRY,
    Counter as PrometheusCounter,
    Histogram,
)
from prometheus_client.core import HistogramMetricFamily


TOKEN_BUCKETS = (64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
WORK_COUNTERS = frozenset(
    {
        "input_bytes",
        "blocks",
        "units",
        "dp_edges",
        "split_evaluations",
        "token_evaluations",
        "rendered_bytes",
        "table_cells",
        "output_chunks",
        "output_bytes",
    }
)
EVENTS = frozenset(
    {
        "overflow_chunks",
        "missing_ranks",
        "conflicting_pages",
        "unresolved_relations",
        "geometry_unavailable",
        "split_inside_code_line",
        "code_blocks_split",
        "table_rows_split",
        "table_cells_split",
    }
)
PHASES = frozenset(
    {
        "total",
        "tokenization",
        "preparation",
        "relations",
        "splitting",
        "packing",
        "validation",
    }
)
OUTCOMES = frozenset(
    {
        "success",
        "cancelled",
        "invalid_chunk_options",
        "invalid_structure",
        "missing_structured_blocks",
        "incomplete_ocr",
        "tokenizer_mismatch",
        "tokenization_failed",
        "protected_block_exceeds_budget",
        "context_exceeds_budget",
        "unsplittable_content",
        "unsplittable_table",
        "no_indexable_content",
        "chunk_input_limit",
        "chunk_work_limit",
        "chunk_output_limit",
        "tokenizer_unavailable",
        "chunk_capacity_exceeded",
        "chunk_timeout",
        "chunk_worker_failed",
        "other",
    }
)


def work_statistics(work, result=None) -> dict:
    """Summarize real work without document text, IDs or per-chunk arrays.

    RSS is a sample at completion/failure, not a process or request peak.
    Token buckets are noncumulative, keeping IPC overhead independent of
    document size even when protected chunks exceed their requested budget.
    """
    chunks = (result or {}).get("chunks", [])
    events = Counter()
    bins = [0] * (len(TOKEN_BUCKETS) + 1)
    total_tokens = 0
    relations = set()
    divided = defaultdict(set)
    code_parts = defaultdict(list)
    for index, chunk in enumerate(chunks):
        tokens = chunk["token_count"]
        bins[bisect_left(TOKEN_BUCKETS, tokens)] += 1
        total_tokens += tokens
        events["overflow_chunks"] += int(chunk.get("overflow", False))
        context = chunk.get("document_context", {})
        summary = context.get("order_summary", {})
        events["missing_ranks"] += summary.get("missing_ranks", 0)
        events["conflicting_pages"] += len(summary.get("conflicting_pages", []))
        for diagnostic in context.get("diagnostics", []):
            if diagnostic["code"] == "geometry_unavailable":
                events["geometry_unavailable"] += 1
        for relation in chunk.get("relations", []):
            if relation["status"] == "unresolved":
                relations.add(relation["id"])
        for span in chunk.get("source_spans", []):
            source = span["source_key"]
            if span["role"] == "code":
                end = span["text_end"]
                code_parts[source].append(
                    (
                        span["start"],
                        span["end"],
                        index,
                        chunk["text"][end - 1 : end] == "\n",
                    )
                )
            table = span.get("table", {})
            if table and table.get("row") is not None:
                row, column = table["row"], table["column"]
                divided[("table_rows_split", source, row)].add(index)
                divided[("table_cells_split", source, row, column)].add(index)
    events["unresolved_relations"] = len(relations)
    for intervals in code_parts.values():
        previous = None
        owners = set()
        for start, end, owner, newline in sorted(intervals):
            if previous is None or previous[0] != start:
                events["code_blocks_split"] += int(len(owners) > 1)
                owners = {owner}
            else:
                owners.add(owner)
                if previous[1] != owner and not previous[2]:
                    events["split_inside_code_line"] += 1
            previous = (end, owner, newline)
        events["code_blocks_split"] += int(len(owners) > 1)
    for key, owners in divided.items():
        events[key[0]] += int(len(owners) > 1)
    try:
        rss = psutil.Process().memory_info().rss
    except (psutil.Error, OSError):
        rss = None
    return {
        "counters": dict(work.counters),
        "physical_token_attempts": work.physical_token_attempts,
        "cpu_seconds": dict(work.cpu_seconds),
        "rss_bytes": rss,
        "chunk_count": len(chunks),
        "events": dict(events),
        "tokens": {"buckets": bins, "sum": total_tokens},
    }


class _TokenHistogram:
    """Merge exact worker histograms through the public collector interface."""

    def __init__(self):
        self._lock = Lock()
        self._bins = [0] * (len(TOKEN_BUCKETS) + 1)
        self._sum = 0

    def add(self, distribution):
        bins = distribution.get("buckets", [])
        total = distribution.get("sum", 0)
        if len(bins) != len(self._bins):
            return
        with self._lock:
            self._bins = [left + right for left, right in zip(self._bins, bins)]
            self._sum += total

    def collect(self):
        with self._lock:
            bins, total = list(self._bins), self._sum
        accumulated = 0
        buckets = []
        for upper, count in zip((*TOKEN_BUCKETS, float("inf")), bins):
            accumulated += count
            label = "+Inf" if upper == float("inf") else str(float(upper))
            buckets.append((label, accumulated))
        yield HistogramMetricFamily(
            "ocr_chunk_tokens",
            "Exact token counts of emitted chunks.",
            buckets=buckets,
            sum_value=total,
        )


class ChunkMetrics:
    """Parent-process collector with fixed, document-independent labels."""

    def __init__(self, *, registry=REGISTRY):
        """Register fixed-label counters and histograms with the given registry."""
        self._work = PrometheusCounter(
            "ocr_chunk_work_total",
            "Logical chunking work, including hits.",
            ["kind"],
            registry=registry,
        )
        self._physical = PrometheusCounter(
            "ocr_chunk_physical_token_attempts_total",
            "Actual tokenizer invocations, including failed invocations.",
            registry=registry,
        )
        self._events = PrometheusCounter(
            "ocr_chunk_events_total",
            "Structural chunking events.",
            ["event"],
            registry=registry,
        )
        self._outcomes = PrometheusCounter(
            "ocr_chunk_outcomes_total",
            "Chunk runtime outcomes.",
            ["outcome"],
            registry=registry,
        )
        self._duration = Histogram(
            "ocr_chunk_runtime_seconds",
            "Runtime latency including queue.",
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 120),
            registry=registry,
        )
        self._cpu = Histogram(
            "ocr_chunk_cpu_seconds",
            "Worker CPU by overlapping phase.",
            ["phase"],
            buckets=(0.001, 0.01, 0.1, 1, 5, 10, 30, 60, 120),
            registry=registry,
        )
        self._rss = Histogram(
            "ocr_chunk_worker_rss_sample_bytes",
            "Worker RSS sampled at completion/failure; not a memory peak.",
            buckets=tuple(2**exponent for exponent in range(20, 35)),
            registry=registry,
        )
        self._chunks = Histogram(
            "ocr_chunk_output_chunks",
            "Chunks per completed request.",
            buckets=(1, 2, 4, 8, 16, 32, 64, 128, 512, 1024, 10000),
            registry=registry,
        )
        self._tokens = _TokenHistogram()
        registry.register(self._tokens)

    def record_statistics(self, statistics: dict) -> None:
        """Import bounded worker measurements without exposing source fields."""
        if not statistics:
            return
        for name, value in statistics.get("counters", {}).items():
            if name in WORK_COUNTERS:
                self._work.labels(kind=name).inc(value)
        self._physical.inc(statistics.get("physical_token_attempts", 0))
        for name, value in statistics.get("events", {}).items():
            if name in EVENTS:
                self._events.labels(event=name).inc(value)
        for name, value in statistics.get("cpu_seconds", {}).items():
            if name in PHASES:
                self._cpu.labels(phase=name).observe(value)
        if statistics.get("rss_bytes") is not None:
            self._rss.observe(statistics["rss_bytes"])
        self._chunks.observe(statistics.get("chunk_count", 0))
        self._tokens.add(statistics.get("tokens", {}))

    def record_outcome(self, code: str, duration_seconds: float) -> None:
        """Classify unknown codes without creating unbounded label values."""
        self._outcomes.labels(
            outcome=code if code in OUTCOMES else "other",
        ).inc()
        self._duration.observe(duration_seconds)


@lru_cache(maxsize=1)
def default_metrics() -> ChunkMetrics:
    """Register once in the API process, not independently in every worker."""
    return ChunkMetrics()
