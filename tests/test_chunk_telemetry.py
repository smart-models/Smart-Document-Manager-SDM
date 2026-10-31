"""Telemetry stays outside deterministic exports and uses bounded labels."""

import pytest
from pathlib import Path

from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.errors import ChunkError
from ocrserver.schemas import ChunkOptions


class CountingTokenizer:
    """Count physical tokenizer attempts and optionally fail token counting."""

    identity = {
        "repo": "PaddlePaddle/PaddleOCR-VL-1.6",
        "revision": "0" * 40,
        "sha256": "0" * 64,
        "library_version": "test",
    }

    def __init__(self, fail=False):
        self.attempts = 0
        self.fail = fail

    def count(self, text):
        self.attempts += 1
        if self.fail:
            raise ChunkError("tokenization_failed", "Private input omitted")
        return len(text.split())

    def offsets(self, text):
        import re

        self.attempts += 1
        return [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]

    def assert_compatible(self, label):
        pass


def source():
    return {
        "pages": [
            {
                "page": 1,
                "blocks": [
                    {
                        "type": "text",
                        "content": "Private words for one document.",
                    },
                ],
            }
        ]
    }


def test_observer_receives_real_work_without_changing_export():
    tokenizer = CountingTokenizer()
    observed = []
    options = ChunkOptions(max_tokens=64)

    result = chunk_document(source(), options, tokenizer, observer=observed.append)
    plain = chunk_document(source(), options, CountingTokenizer())

    assert result == plain
    assert len(observed) == 1
    stats = observed[0]
    assert stats["physical_token_attempts"] == tokenizer.attempts > 0
    assert stats["counters"]["token_evaluations"] == tokenizer.attempts
    assert stats["counters"]["dp_edges"] > 0
    assert stats["events"]["missing_ranks"] == 1
    assert stats["chunk_count"] == 1
    assert stats["tokens"]["sum"] == 5
    assert stats["rss_bytes"] > 0
    assert stats["cpu_seconds"]["total"] >= 0
    assert "Private" not in repr(stats)


def test_observer_preserves_failed_physical_attempts_and_domain_error():
    tokenizer = CountingTokenizer(fail=True)
    observed = []

    with pytest.raises(ChunkError) as raised:
        chunk_document(
            source(),
            ChunkOptions(max_tokens=64),
            tokenizer,
            observer=observed.append,
        )

    assert raised.value.code == "tokenization_failed"
    assert observed[0]["physical_token_attempts"] == tokenizer.attempts == 1
    assert observed[0]["counters"]["token_evaluations"] == 1
    assert observed[0]["chunk_count"] == 0


def test_prometheus_collects_exact_histogram_and_bounded_labels():
    from prometheus_client import CollectorRegistry, generate_latest

    from ocrserver.chunking.telemetry import ChunkMetrics

    registry = CollectorRegistry()
    metrics = ChunkMetrics(registry=registry)
    observations = []
    chunk_document(
        source(),
        ChunkOptions(max_tokens=64),
        CountingTokenizer(),
        observer=observations.append,
    )
    stats = observations[0]
    stats["events"]["PRIVATE-DOCUMENT-ID"] = 99
    stats["counters"]["PRIVATE-DOCUMENT-ID"] = 99
    stats["cpu_seconds"]["PRIVATE-DOCUMENT-ID"] = 99

    metrics.record_statistics(stats)
    metrics.record_outcome("PRIVATE-DOCUMENT-ID", 0.25)

    exported = generate_latest(registry).decode()
    assert "PRIVATE-DOCUMENT-ID" not in exported
    assert 'ocr_chunk_outcomes_total{outcome="other"} 1.0' in exported
    assert "ocr_chunk_tokens_count 1.0" in exported
    assert "ocr_chunk_tokens_sum 5.0" in exported
    assert 'ocr_chunk_tokens_bucket{le="64.0"} 1.0' in exported
    assert 'ocr_chunk_events_total{event="missing_ranks"} 1.0' in exported
    assert "ocr_chunk_worker_rss_sample_bytes_count 1.0" in exported


def echo_worker(request, scope, tokenizer, limits):
    """Existing four-argument worker contract remains usable unchanged."""
    if request.get("fail"):
        raise ChunkError("unsplittable_content", "No permitted partition")
    return {"value": request.get("value")}


async def test_runtime_unwraps_statistics_without_changing_custom_results():
    from prometheus_client import CollectorRegistry, generate_latest

    from ocrserver.chunking.runtime import ChunkRuntime
    from ocrserver.chunking.telemetry import ChunkMetrics
    from ocrserver.config import Settings

    registry = CollectorRegistry()
    metrics = ChunkMetrics(registry=registry)
    runtime = ChunkRuntime(
        Settings(chunk_max_workers=1), worker=echo_worker, telemetry=metrics
    )
    try:
        assert await runtime.run({"value": "private"}) == {"value": "private"}
        with pytest.raises(ChunkError):
            await runtime.run({"fail": True})
    finally:
        await runtime.close()

    exported = generate_latest(registry).decode()
    assert 'ocr_chunk_outcomes_total{outcome="success"} 1.0' in exported
    assert 'ocr_chunk_outcomes_total{outcome="unsplittable_content"} 1.0' in exported
    assert "private" not in exported


def test_two_intact_fences_in_one_source_are_not_counted_as_a_split_block():
    document = source()
    document["pages"][0]["blocks"][0]["content"] = (
        "```\na b\n```\n\nMiddle words\n\n```\nc d\n```"
    )
    observations = []

    result = chunk_document(
        document,
        ChunkOptions(max_tokens=4),
        CountingTokenizer(),
        observer=observations.append,
    )

    assert len(result["chunks"]) >= 2
    assert observations[0]["events"].get("code_blocks_split", 0) == 0


@pytest.mark.skipif(
    not Path(".scratch/ocr-rag-implementation/tokenizer-cache").is_dir(),
    reason="local Paddle tokenizer cache unavailable",
)
async def test_production_worker_exports_statistics_only_to_parent_metrics():
    import json

    from prometheus_client import CollectorRegistry, generate_latest

    from ocrserver.chunking.canonical import canonical_bytes
    from ocrserver.chunking.runtime import ChunkRuntime
    from ocrserver.chunking.telemetry import ChunkMetrics
    from ocrserver.config import Settings

    registry = CollectorRegistry()
    runtime = ChunkRuntime(
        Settings(
            chunk_max_workers=1,
            hf_cache_dir=str(
                Path(".scratch/ocr-rag-implementation/tokenizer-cache").resolve()
            ),
        ),
        telemetry=ChunkMetrics(registry=registry),
    )
    request = json.loads(
        Path("tests/fixtures/ocr-rag-chunks.input.example.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        result = await runtime.run(request)
    finally:
        await runtime.close()

    assert (
        canonical_bytes(result)
        == Path("tests/fixtures/ocr-rag-chunks.example.json").read_bytes()
    )
    exported = generate_latest(registry).decode()
    assert 'ocr_chunk_cpu_seconds_count{phase="total"} 1.0' in exported
    assert 'ocr_chunk_cpu_seconds_count{phase="packing"} 1.0' in exported
    assert 'ocr_chunk_work_total{kind="dp_edges"}' in exported
    assert "ocr_chunk_physical_token_attempts_total 0.0" not in exported
    assert "ocr_chunk_worker_rss_sample_bytes_count 1.0" in exported
