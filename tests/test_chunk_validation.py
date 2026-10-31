"""Export validation rejects corrupted provenance at its public boundary."""

from dataclasses import replace

import pytest

from ocrserver.chunking.engine import chunk_document
from ocrserver.chunking.canonical import canonical_hash
from ocrserver.chunking.errors import ChunkError
from ocrserver.chunking.provenance import OverlapReceipt, validate_result
from ocrserver.chunking.structure import prepare_document
from ocrserver.chunking.types import ChunkLimits, PROFILE, VERSION, Work
from ocrserver.chunking.validation import validate_output
from ocrserver.schemas import ChunkOptions, ChunkRequest
from test_chunk_engine import WordTokenizer, document


def sample():
    source = document(
        ("paragraph_title", "A"),
        ("text", "alpha beta"),
        ("paragraph_title", "B"),
        ("text", "gamma delta"),
    )
    options = ChunkOptions(max_tokens=10)
    tokenizer = WordTokenizer()
    output = chunk_document(source, options, tokenizer)
    normalized = ChunkRequest(
        parsed_document=source,
        options=options,
    ).parsed_document.model_dump()
    work = Work(tokenizer, ChunkLimits())
    prepared = prepare_document(normalized, options, work)
    return output, prepared, options, work


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_fingerprint", "f" * 64),
        ("policy_hash", "f" * 64),
        ("tokenizer", {**WordTokenizer.identity, "sha256": "f" * 64}),
        ("source_id", "different-source"),
        ("lang", "en"),
    ],
)
def test_export_rejects_different_common_identities(field, value):
    output, _, _, _ = sample()
    output["chunks"][1][field] = value

    with pytest.raises(ChunkError):
        validate_output(output)


@pytest.mark.parametrize(
    "change",
    [
        lambda chunks: [chunk.update(source_fingerprint="f" * 64) for chunk in chunks],
        lambda chunks: [chunk.update(policy_hash="f" * 64) for chunk in chunks],
        lambda chunks: [
            chunk.update(tokenizer={**WordTokenizer.identity, "sha256": "f" * 64})
            for chunk in chunks
        ],
        lambda chunks: chunks[0]["document_context"]["chunking_options"].update(
            max_tokens=9
        ),
        lambda chunks: chunks[0]["document_context"]["scope"].update(
            selected_pages=[99]
        ),
        lambda chunks: chunks[1].update(page=99, pages=[99]),
        lambda chunks: chunks[1].update(page=99),
        lambda chunks: chunks[1].update(page_context=[]),
    ],
)
def test_final_validation_binds_metadata_to_actual_source_and_options(change):
    output, prepared, options, work = sample()
    change(output["chunks"])

    with pytest.raises(ChunkError):
        validate_result(prepared, output["chunks"], options, work)


def test_final_validation_rejects_overlap_count_without_observed_overlap():
    output, prepared, options, work = sample()
    output["chunks"][1]["overlap_token_count"] = 999

    with pytest.raises(ChunkError):
        validate_result(prepared, output["chunks"], options, work)


def test_nonzero_overlap_requires_the_exact_counted_receipt():
    source = document(
        *[
            ("text", text)
            for text in (
                "one two three",
                "four five six",
                "seven eight nine",
            )
        ]
    )
    options = ChunkOptions(max_tokens=5, overlap_tokens=2)
    output = chunk_document(source, options, WordTokenizer())
    assert any(chunk["overlap_token_count"] for chunk in output["chunks"])
    work = Work(WordTokenizer(), ChunkLimits())
    normalized = ChunkRequest(
        parsed_document=source,
        options=options,
    ).parsed_document.model_dump()
    prepared = prepare_document(normalized, options, work)

    with pytest.raises(ChunkError):
        validate_result(prepared, output["chunks"], options, work)


@pytest.mark.parametrize(
    "blocks,reason",
    [
        ([("text", "one two three")], "protected_code"),
        (
            [("table", "<table>A B</table>"), ("text", "two three")],
            "protected_table",
        ),
    ],
)
def test_overflow_cannot_authorize_prose_or_neighbours_of_a_protected_unit(
    blocks, reason
):
    source = document(*blocks)
    options = ChunkOptions(max_tokens=2, strictly=False)
    output = chunk_document(source, ChunkOptions(max_tokens=100), WordTokenizer())
    chunk = output["chunks"][0]
    chunk.update(
        overflow=True,
        overflow_tokens=chunk["token_count"] - 2,
        overflow_reason=reason,
        policy_hash=canonical_hash(
            {
                "chunker_version": VERSION,
                "algorithm_profile": PROFILE,
                "options": options.model_dump(),
            }
        ),
    )
    chunk["document_context"]["chunking_options"] = options.model_dump()
    work = Work(WordTokenizer(), ChunkLimits())
    normalized = ChunkRequest(
        parsed_document=source,
        options=options,
    ).parsed_document.model_dump()
    prepared = prepare_document(normalized, options, work)

    with pytest.raises(ChunkError):
        validate_result(prepared, output["chunks"], options, work)


def overlap_sample():
    source = document(
        *[
            ("text", text)
            for text in (
                "one two three",
                "four five six",
                "seven eight nine",
            )
        ]
    )
    options = ChunkOptions(max_tokens=5, overlap_tokens=2)
    tokenizer = WordTokenizer()
    output = chunk_document(source, options, tokenizer)
    receipts = {
        1: OverlapReceipt.capture("two three", 0, 2),
        2: OverlapReceipt.capture("five six", 0, 2),
    }
    work = Work(tokenizer, ChunkLimits())
    normalized = ChunkRequest(
        parsed_document=source,
        options=options,
    ).parsed_document.model_dump()
    prepared = prepare_document(normalized, options, work)
    return output, receipts, prepared, options, work


def test_counted_overlap_receipts_allow_validation_without_an_additional_encode():
    output, receipts, prepared, options, work = overlap_sample()

    assert (
        validate_result(
            prepared,
            output["chunks"],
            options,
            work,
            overlap_receipts=receipts,
        )
        == output
    )
    assert work.counters["token_evaluations"] == len(output["chunks"])


@pytest.mark.parametrize(
    "change",
    [
        lambda chunks, receipts: receipts.update(
            {1: replace(receipts[1], token_count=1)}
        ),
        lambda chunks, receipts: receipts.update(
            {1: replace(receipts[1], text_sha256="f" * 64)}
        ),
        lambda chunks, receipts: chunks[1].update(section_id="s:p1:b1"),
        lambda chunks, receipts: chunks[0]["source_spans"][0].update(
            role="section_title"
        ),
        lambda chunks, receipts: chunks[1].update(overlap_token_count=0),
    ],
)
def test_overlap_validation_rejects_corrupted_counts_sources_and_sections(
    change,
):
    output, receipts, prepared, options, work = overlap_sample()
    change(output["chunks"], receipts)

    with pytest.raises(ChunkError):
        validate_result(
            prepared,
            output["chunks"],
            options,
            work,
            overlap_receipts=receipts,
        )


def test_relation_endpoint_must_resolve_to_an_existing_source_interval():
    source = document(("text", "body"), ("footnote", "A note"))
    options = ChunkOptions(max_tokens=10)
    output = chunk_document(source, options, WordTokenizer())
    output["chunks"][0]["relations"][0]["from"]["source_key"] = "p99:b0"
    normalized = ChunkRequest(
        parsed_document=source,
        options=options,
    ).parsed_document.model_dump()
    work = Work(WordTokenizer(), ChunkLimits())
    prepared = prepare_document(normalized, options, work)

    with pytest.raises(ChunkError):
        validate_result(prepared, output["chunks"], options, work)


def test_metadata_text_cannot_claim_source_coverage_after_changing_the_footer():
    source = document(("text", "body"), ("footer", "Original footer"))
    options = ChunkOptions(max_tokens=10)
    output = chunk_document(source, options, WordTokenizer())
    output["chunks"][0]["page_context"][0]["footers"][0]["text"] = "Changed"
    normalized = ChunkRequest(
        parsed_document=source,
        options=options,
    ).parsed_document.model_dump()
    work = Work(WordTokenizer(), ChunkLimits())
    prepared = prepare_document(normalized, options, work)

    with pytest.raises(ChunkError):
        validate_result(prepared, output["chunks"], options, work)
