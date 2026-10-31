"""Swagger groups match the console's capabilities."""

from test_mcp_schema import FakePipeline, FakeRecognizer

from ocr_server import create_app
from ocrserver.config import Settings


def test_all_documented_operations_have_uppercase_sections():
    schema = create_app(
        Settings(api_tokens="test-token:test"),
        FakePipeline(),
        FakeRecognizer(),
    ).openapi()
    sections = {section["name"] for section in schema["tags"]}
    assert sections == {"PARSE", "OCR", "RAG", "CHUNK", "STORAGE", "MONITORING"}
    grouped = {section: set() for section in sections}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            tags = operation["tags"]
            assert len(tags) == 1 and tags[0] in sections
            grouped[tags[0]].add((method, path))
    assert grouped["RAG"] == {
        ("post", "/storage/rag/refine"),
        ("post", "/storage/rag/split"),
    }
    assert grouped["MONITORING"] == {
        ("get", "/health"),
        ("get", "/stats"),
        ("get", "/metrics"),
        ("get", "/parse/progress/{progress_id}"),
    }
    assert grouped["PARSE"] == {
        ("post", "/parse"),
        ("post", "/office/pdf"),
        ("post", "/parse/chunks"),
    }
    for path in ("/storage/rag/refine", "/storage/rag/split"):
        assert schema["paths"][path]["post"]["security"] == [{"BearerAuth": []}]
