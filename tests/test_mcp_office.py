"""MCP routing of Office bytes to the API's generic document_base64 source."""

import base64
import json

import httpx
import pytest

from test_chunk_mcp import RAG
from test_mcp_tools import HEALTH, PNG, harness, successful
from test_office_utils import ole2, ooxml

DOCX = base64.b64encode(ooxml("word/document.xml")).decode()
DOC = base64.b64encode(ole2("WordDocument")).decode()
PDF = base64.b64encode(b"%PDF-1.7\nfixture").decode()


@pytest.mark.parametrize(
    "params,key,value",
    [
        ({"document_base64": DOCX}, "document_base64", DOCX),
        ({"document_base64": DOC}, "document_base64", DOC),
        ({"image_base64": DOCX}, "document_base64", DOCX),
        ({"document_base64": PDF}, "pdf_base64", PDF),
        ({"document_base64": PNG}, "image_base64", PNG),
        (
            {"document_base64": f"data:application/octet-stream;base64,{DOCX}"},
            "document_base64",
            DOCX,
        ),
    ],
)
async def test_parse_routes_by_content(params, key, value):
    async with harness(successful) as (client, calls):
        result = await client.call_tool("ocr_parse_document", {"params": params})
        assert not result.is_error, result.content[0].text
        body = json.loads(calls[-1].content)
        assert body[key] == value
        assert {"image_base64", "pdf_base64", "document_base64"} & set(body) == {key}


async def test_office_file_path_is_sent_as_document(tmp_path):
    file = tmp_path / "report.docx"
    file.write_bytes(base64.b64decode(DOCX))
    async with harness(successful, input_roots_raw=str(tmp_path)) as (client, calls):
        result = await client.call_tool(
            "ocr_parse_document", {"params": {"file_path": str(file)}}
        )
        assert not result.is_error
        assert json.loads(calls[-1].content)["document_base64"] == DOCX


@pytest.mark.parametrize(
    "tool,params,message",
    [
        ("ocr_recognize_image", {"image_base64": DOCX}, "images only"),
        ("ocr_recognize_image", {"image_base64": DOC}, "images only"),
        (
            "ocr_parse_document",
            {"document_base64": DOCX, "image_base64": PNG},
            "document_base64",
        ),
        ("ocr_parse_document", {"pdf_base64": DOCX}, "must contain a PDF"),
    ],
)
async def test_office_source_refusals(tool, params, message):
    async with harness(successful) as (client, calls):
        result = await client.call_tool(tool, {"params": params})
        assert result.is_error
        assert message in result.content[0].text
        assert calls == []


async def test_rag_chunks_accept_office_documents():
    def upstream(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        assert request.url.path == "/parse/chunks"
        assert json.loads(request.content)["document_base64"] == DOCX
        return httpx.Response(200, json=RAG)

    async with harness(upstream) as (client, calls):
        result = await client.call_tool(
            "ocr_create_rag_chunks",
            {"params": {"document_base64": DOCX, "options": {"max_tokens": 512}}},
        )
        assert not result.is_error, result.content[0].text
        both = await client.call_tool(
            "ocr_create_rag_chunks",
            {
                "params": {
                    "document_base64": DOCX,
                    "image_base64": PNG,
                    "options": {"max_tokens": 512},
                }
            },
        )
        assert both.is_error


def test_office_filename_is_bounded():
    from ocr_mcp.inputs_office import ConvertOfficeToPdfInput
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConvertOfficeToPdfInput(filename="a" * 1025)
