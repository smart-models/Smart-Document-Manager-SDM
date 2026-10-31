"""MCP document names propagate to API archival without exposing paths."""

import base64

import pytest
from ocr_mcp.config import Settings
from ocr_mcp.inputs import ParseDocumentInput, prepare_source
from ocr_mcp.rag_contract import CreateRagChunksInput
from pydantic import ValidationError
from test_mcp_tools import PNG


def test_base64_source_forwards_optional_original_filename():
    body = prepare_source(
        ParseDocumentInput(image_base64=PNG, filename="quarterly report.png"),
        Settings(_env_file=None, api_tokens_raw="inbound:tester"),
    )

    assert body["filename"] == "quarterly report.png"
    assert base64.b64decode(body["image_base64"]) == base64.b64decode(PNG)


def test_base64_without_name_leaves_format_fallback_to_api():
    body = prepare_source(
        ParseDocumentInput(image_base64=PNG),
        Settings(_env_file=None, api_tokens_raw="inbound:tester"),
    )

    assert "filename" not in body


def test_direct_rag_source_forwards_its_filename():
    params = CreateRagChunksInput(
        image_base64=PNG,
        filename="rag-source.png",
        options={"max_tokens": 512},
    )

    body = prepare_source(
        params, Settings(_env_file=None, api_tokens_raw="inbound:tester")
    )

    assert body["filename"] == "rag-source.png"


def test_file_path_forwards_only_its_basename(tmp_path):
    path = tmp_path / "client-private-directory" / "invoice scan.png"
    path.parent.mkdir()
    path.write_bytes(base64.b64decode(PNG))
    params = ParseDocumentInput(file_path=str(path))
    settings = Settings(
        _env_file=None,
        api_tokens_raw="inbound:tester",
        input_roots_raw=str(tmp_path),
    )

    body = prepare_source(params, settings)

    assert body["filename"] == "invoice scan.png"
    assert base64.b64decode(body["image_base64"]) == base64.b64decode(PNG)


def test_explicit_filename_overrides_file_path_basename(tmp_path):
    path = tmp_path / "invoice.png"
    path.write_bytes(base64.b64decode(PNG))
    params = ParseDocumentInput(file_path=str(path), filename="renamed invoice.png")

    body = prepare_source(
        params,
        Settings(
            _env_file=None,
            api_tokens_raw="inbound:tester",
            input_roots_raw=str(tmp_path),
        ),
    )

    assert body["filename"] == "renamed invoice.png"


def test_mcp_filename_is_bounded():
    with pytest.raises(ValidationError):
        ParseDocumentInput(image_base64=PNG, filename="x" * 1025)
