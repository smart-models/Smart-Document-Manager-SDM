"""Public request/output contracts for optional deterministic RAG exports."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ocrserver.schemas import ChunkOptions, ChunkRequest


def test_historical_document_accepts_missing_geometry_and_resolves_options():
    request = ChunkRequest.model_validate(
        {
            "parsed_document": {
                "pages": [
                    {
                        "page": 1,
                        "blocks": [
                            {"type": "text", "content": "Hello"},
                        ],
                    }
                ]
            },
            "options": {"max_tokens": 512},
            "lang": "zh-Hant-TW",
        }
    )
    assert request.parsed_document.pages[0].blocks[0].bbox is None
    assert request.options.model_dump() == {
        "max_tokens": 512,
        "strictly": True,
        "preserve_tables": True,
        "preserve_code_blocks": True,
        "overlap_tokens": 0,
        "marginal_policy": "metadata",
        "table_header_policy": "declared",
    }
    assert request.accept_truncated is False


@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": True},
        {"max_tokens": "512"},
        {"max_tokens": 0},
        {"max_tokens": 32769},
        {"max_tokens": 5, "overlap_tokens": 5},
        {"max_tokens": 5, "strictly": "false"},
        {"max_tokens": 5, "preserve_table": True},
    ],
)
def test_invalid_or_misspelled_options_are_rejected(options):
    with pytest.raises(ValidationError):
        ChunkOptions.model_validate(options)


def test_normative_export_retains_required_nulls_and_omits_optional_absences():
    from ocrserver.schemas import RagResponse
    from ocrserver.chunking.validation import output_schema, validate_output

    path = Path("tests/fixtures/ocr-rag-chunks.example.json")
    expected = json.loads(path.read_text(encoding="utf-8"))
    actual = RagResponse.model_validate(expected).model_dump(by_alias=True)
    assert actual == expected
    assert validate_output(actual) == expected
    assert output_schema() == json.loads(
        Path("tests/fixtures/ocr-rag-chunks.schema.json").read_text(encoding="utf-8")
    )

    invalid = copy.deepcopy(expected)
    invalid["chunks"][0]["assets"][0]["fabricated_url"] = "/somewhere"
    with pytest.raises(ValidationError):
        RagResponse.model_validate(invalid)


def test_jcs_canonical_bytes_follow_ecmascript_numbers_and_utf16_key_order():
    from ocrserver.chunking.canonical import canonical_bytes

    value = {"\ue000": 1e-7, "😀": 1e20, "a": -0.0, "b": 1e-6}
    assert canonical_bytes(value) == (
        '{"a":0,"b":0.000001,"😀":100000000000000000000,"\ue000":1e-7}'
    ).encode("utf-8")


@pytest.mark.parametrize(
    "lang",
    [
        "it",
        "en-US",
        "de-CH-1901",
        "zh-Hant-TW",
        "en-u-ca-gregory",
        "x-a-a",
        "en-x-private",
        "i-klingon",
        "en-GB-oed",
    ],
)
def test_caller_language_accepts_bcp47_syntax_without_inference(lang):
    request = ChunkRequest.model_validate(
        {
            "parsed_document": {"pages": []},
            "options": {"max_tokens": 1},
            "lang": lang,
        }
    )
    assert request.lang == lang


@pytest.mark.parametrize(
    "lang",
    [
        "123",
        "en_US",
        "",
        "en--US",
        "en-a",
        "en-a-foo-a-bar",
        "de-1901-1901",
        "abcdefghij",
    ],
)
def test_caller_language_rejects_invalid_or_duplicated_subtags(lang):
    with pytest.raises(ValidationError):
        ChunkRequest.model_validate(
            {
                "parsed_document": {"pages": []},
                "options": {"max_tokens": 1},
                "lang": lang,
            }
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, "\ud800"])
def test_unrepresentable_values_cannot_be_exported(value):
    from ocrserver.chunking.canonical import canonical_bytes
    from ocrserver.chunking.errors import ChunkError

    with pytest.raises(ChunkError) as raised:
        canonical_bytes({"value": value})
    assert raised.value.code == "invalid_structure"


@pytest.mark.parametrize(
    "field",
    [
        "page_context",
        "assets",
        "relations",
        "diagnostics",
        "tokenizer",
    ],
)
def test_every_metadata_container_is_closed(field):
    from ocrserver.chunking.errors import ChunkError
    from ocrserver.chunking.validation import validate_output

    payload = json.loads(
        Path("tests/fixtures/ocr-rag-chunks.example.json").read_text(encoding="utf-8")
    )
    chunk = payload["chunks"][0]
    if isinstance(chunk[field], list):
        chunk[field].append({"accidental_property": "x"})
    else:
        chunk[field]["accidental_property"] = "x"
    with pytest.raises(ChunkError):
        validate_output(payload)
