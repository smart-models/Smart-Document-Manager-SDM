"""Small exhaustive oracle and actual-Paddle Unicode/source invariants."""

import itertools
import json
import os
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from ocrserver.chunking.canonical import canonical_bytes
from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.tokenizer import PaddleTokenizer
from ocrserver.schemas import ChunkOptions
from test_chunk_engine import ROOT, WordTokenizer, document


@given(st.lists(st.integers(1, 5), min_size=1, max_size=7), st.integers(5, 12))
@settings(max_examples=60, deadline=None)
def test_small_packing_matches_exhaustive_partition_oracle(lengths, budget):
    """All partitions are enumerated independently of the implementation DP."""
    n = len(lengths)
    feasible = []
    for switches in itertools.product((False, True), repeat=n - 1):
        ends = [index + 1 for index, cut in enumerate(switches) if cut] + [n]
        starts = [0, *ends[:-1]]
        counts = [sum(lengths[start:end]) for start, end in zip(starts, ends)]
        if any(count > budget for count in counts):
            continue
        score = (
            sum(5 * count < budget for count in counts[:-1]),
            len(ends),
            sum((budget - count) ** 2 for count in counts),
            ends,
        )
        feasible.append(score)
    expected = min(feasible)[-1]
    blocks = [
        ("text", " ".join([f"word{index}"] * length))
        for index, length in enumerate(lengths)
    ]
    chunks = chunk_document(
        document(*blocks), ChunkOptions(max_tokens=budget), WordTokenizer()
    )["chunks"]
    actual = [
        max(span["block_index"] for span in chunk["source_spans"]) + 1
        for chunk in chunks
    ]
    assert actual == expected


@pytest.fixture(scope="module")
def paddle():
    cache = os.environ.get("OCR_TEST_TOKENIZER_CACHE") or str(
        ROOT / ".scratch/ocr-rag-implementation/tokenizer-cache"
    )
    if not Path(cache).is_dir():
        pytest.skip("Set OCR_TEST_TOKENIZER_CACHE to the pinned local Paddle snapshot")
    return PaddleTokenizer(cache, "c5630abae1d940eafe0697512a0325494b02ab42")


def test_actual_paddle_matches_normative_canonical_fixture(paddle):
    body = json.loads(
        (ROOT / "tests/fixtures/ocr-rag-chunks.input.example.json").read_text()
    )
    result = chunk_document(
        body["parsed_document"],
        ChunkOptions(**body["options"]),
        paddle,
        source_id=body["source_id"],
        lang=body["lang"],
    )
    assert (
        canonical_bytes(result)
        == (ROOT / "tests/fixtures/ocr-rag-chunks.example.json").read_bytes()
    )


@given(
    st.lists(
        st.sampled_from(
            [
                "Italiano",
                "汉字",
                "日本語",
                "é",
                "e\u0301",
                "👩🏽‍💻",
                "🇮🇹",
                "uno",
                "due",
                "\r\n",
                "\r",
                " ",
            ]
        ),
        min_size=1,
        max_size=35,
    )
)
@settings(max_examples=40, deadline=None)
def test_actual_paddle_preserves_unicode_and_recounts_export(paddle, pieces):
    text = "inizio " + " ".join(pieces) + " fine"
    source = document(("paragraph_title", "Titolo"), ("text", text))
    options = ChunkOptions(max_tokens=32, overlap_tokens=4)
    first = chunk_document(source, options, paddle)
    second = chunk_document(source, options, paddle)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert all(
        chunk["token_count"] == paddle.count(chunk["text"]) <= 32
        for chunk in first["chunks"]
    )
    spans = [
        span
        for chunk in first["chunks"]
        for span in chunk["source_spans"]
        if span["source_key"] == "p1:b1"
    ]
    assert "".join(text[span["start"] : span["end"]] for span in spans) == text
