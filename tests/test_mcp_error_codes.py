"""Every error code the OCR API can send has MCP guidance, or a stated reason."""

import json
import re
from pathlib import Path

from ocr_mcp.errors import (
    CODE_GUIDANCE,
    RAG_GUIDANCE,
    UpstreamError,
    describe_error,
    error_result,
)

ROOT = Path(__file__).resolve().parents[1]
API_CODE_PATTERNS = (
    r'"code":\s*"([a-z][a-z0-9_]*)"',
    r'_error\(\s*\d{3},\s*"([a-z][a-z0-9_]*)"',
    r'ChunkError\(\s*"([a-z][a-z0-9_]*)"',
    r'_Admission\([^,]+,\s*"([a-z][a-z0-9_]*)"',
)
EXEMPT_API_CODES = {
    "duplicate_form_field": "multipart only; MCP sends JSON",
    "invalid_boolean": "multipart only; MCP sends JSON",
    "invalid_integer": "multipart only; MCP sends JSON",
    "invalid_file": "multipart only; MCP sends JSON",
    "invalid_base64": "refused locally before HTTP",
    "invalid_input_count": "refused locally before HTTP",
    "invalid_json_body": "MCP always sends a valid JSON body",
    "unsupported_media_type": "MCP always sends application/json",
    "invalid_progress_id": "MCP generates canonical UUIDs",
    "progress_not_found": "the progress relay ignores progress errors",
    "image_unavailable": "archive warning, not an HTTP error",
    "input_not_saved": "archive warning, not an HTTP error",
    "unauthorized": "covered by the 401 status guidance",
    "rate_limited": "covered by the 429 status guidance",
    "request_body_too_large": "covered by the 413 status guidance",
    "internal_error": "covered by the generic 500 guidance",
}


def api_error_codes() -> set[str]:
    sources = [ROOT / "ocr_server.py", *sorted((ROOT / "ocrserver").rglob("*.py"))]
    codes = set()
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for pattern in API_CODE_PATTERNS:
            codes.update(re.findall(pattern, text))
    return codes


def test_every_api_error_code_has_mcp_guidance_or_a_reason():
    codes = api_error_codes()
    assert len(codes) >= 80  # the scan still finds the API's codes
    unexplained = codes - set(CODE_GUIDANCE) - set(RAG_GUIDANCE)
    assert unexplained - set(EXEMPT_API_CODES) == set()
    assert set(EXEMPT_API_CODES) <= codes  # no stale exemption


def test_pixel_limits_get_specific_remedies():
    pdf = UpstreamError(
        status=413, detail={"code": "pdf_pixel_limit", "message": "x"}, path="/parse"
    )
    image = UpstreamError(
        status=400, detail={"code": "image_too_large", "max_pixels": 1}, path="/ocr"
    )
    assert "lower dpi" in describe_error(pdf)
    assert "downscale" in describe_error(image)


def test_max_pages_error_names_the_configured_limit():
    error = UpstreamError(
        status=422,
        detail={"code": "max_pages_out_of_range", "max": 300},
        path="/parse",
    )
    assert "OCR_MAX_PAGES=300." in describe_error(error)
    without = UpstreamError(
        status=422, detail={"code": "max_pages_out_of_range"}, path="/parse"
    )
    assert "OCR_MAX_PAGES=" not in describe_error(without)


def test_chunk_worker_failures_get_rag_guidance():
    for code in ("chunk_worker_failed", "chunk_validation_failed"):
        result = error_result(
            UpstreamError(
                status=500,
                detail={"code": code, "message": "x", "details": {}},
                path="/chunk",
            )
        )
        assert json.loads(result.content[0].text)["message"] == RAG_GUIDANCE[code]
