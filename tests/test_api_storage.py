"""Storage side effects preserve OCR response contracts and inference capacity."""

import asyncio
import base64
import hashlib
import json
import re

import httpx
import pytest
from test_api_auth_tokens import printed_by
from test_api_json_rag import BlockPipeline
from test_api_office import converter  # noqa: F401
from test_chunk_api import InlineRuntime, TextPipeline
from test_chunk_engine import document
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG
from test_office_utils import ooxml
from test_storage import BlockingS3, MemoryS3, configured, wait_event

from ocr_server import create_app
from ocrserver.config import Settings
from ocrserver.storage import ArtifactStore
from ocrserver.storage_browser import StorageBrowser


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/parse", {"image_base64": PNG}),
        ("/ocr", {"image_base64": PNG}),
        ("/parse/chunks", {"image_base64": PNG, "options": {"max_tokens": 512}}),
    ],
)
async def test_original_filename_length_is_bounded_on_every_archival_route(path, body):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as http:
        response = await http.post(path, json={**body, "filename": "x" * 1025})

    assert response.status_code == 422


class ImagePipeline(FakePipeline):
    """Emit a Markdown image with a traversal-like source name."""

    async def parse_document(self, *args, **kwargs):
        pages = await super().parse_document(*args, **kwargs)
        for page in pages:
            page["markdown"] = "# Figure\n![image](../../outside.png)"
            page["images"] = [
                {"name": "../../outside.png", "png": base64.b64decode(PNG)}
            ]
        return pages


async def test_parse_archives_hidden_png_and_rewrites_only_archival_markdown():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, ImagePipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post("/parse", json={"image_base64": PNG})
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    assert response.headers["x-ocr-storage-status"] == "stored"
    page = response.json()["pages"][0]
    assert page["images"] is None
    assert page["markdown"] == "# Figure\n![image](../../outside.png)"
    files = {entry["Key"].split("/", 1)[-1]: entry["Body"] for entry in client.objects}
    assert files["images/p0001-0001.png"] == base64.b64decode(PNG)
    assert files["document.md"] == b"# Figure\n![image](images/p0001-0001.png)"
    archived_page = json.loads(files["json_ocr.json"])["pages"][0]
    assert "images" not in archived_page and "image_names" not in archived_page
    assert (
        json.loads(files["manifest.json"])["images"][0]["original_name"]
        == "../../outside.png"
    )


class HtmlImagePipeline(FakePipeline):
    """Emit an HTML image with a traversal-like source name."""

    async def parse_document(self, *args, **kwargs):
        pages = await super().parse_document(*args, **kwargs)
        for page in pages:
            page["markdown"] = '# Figure\n<img src="../../outside.png">'
            page["images"] = [
                {"name": "../../outside.png", "png": base64.b64decode(PNG)}
            ]
        return pages


async def test_parse_archive_rewrites_images_after_normalization():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings, HtmlImagePipeline(), FakeRecognizer(), artifact_store=store
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post("/parse", json={"image_base64": PNG})
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    assert response.headers["x-ocr-storage-status"] == "stored"
    page = response.json()["pages"][0]
    assert page["images"] is None
    assert page["markdown"] == '# Figure\n<img src="../../outside.png">'
    files = archive_files(client)
    assert files["images/p0001-0001.png"] == base64.b64decode(PNG)
    assert files["document.md"] == b'# Figure\n<img src="images/p0001-0001.png">'
    archived_page = json.loads(files["json_ocr.json"])["pages"][0]
    assert "images" not in archived_page and "image_names" not in archived_page
    assert (
        json.loads(files["manifest.json"])["images"][0]["original_name"]
        == "../../outside.png"
    )


class TablePipeline(FakePipeline):
    """Emit an HTML table for archive table-format assertions."""

    async def parse_document(self, *args, **kwargs):
        pages = await super().parse_document(*args, **kwargs)
        pages[0]["markdown"] = (
            "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        )
        return pages


async def test_parse_archive_converts_tables_when_table_format_markdown():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, TablePipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse",
                json={"image_base64": PNG, "table_format": "markdown"},
            )
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    assert response.headers["x-ocr-storage-status"] == "stored"
    page = response.json()["pages"][0]
    assert page["markdown"] == "| A | B |\n| --- | --- |\n| 1 | 2 |"
    files = archive_files(client)
    assert files["document.md"] == b"| A | B |\n| --- | --- |\n| 1 | 2 |"


class HtmlPipeline(FakePipeline):
    """Emit HTML Markdown and literal HTML within structured OCR blocks."""

    async def parse_document(self, *args, **kwargs):
        pages = await super().parse_document(*args, **kwargs)
        pages[0]["markdown"] = "<p>Hello <strong>world</strong></p>"
        pages[0]["blocks"] = [
            {
                "type": "text",
                "bbox": [0.0, 0.0, 1.0, 1.0],
                "content": "<b>block content</b>",
                "order": 1,
            }
        ]
        return pages


class TableHtmlPipeline(FakePipeline):
    """Emit mixed HTML prose and table markup for archive normalization."""

    async def parse_document(self, *args, **kwargs):
        pages = await super().parse_document(*args, **kwargs)
        pages[0]["markdown"] = (
            "<div>Intro</div>"
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
        )
        return pages


async def test_parse_archive_normalizes_markdown_when_output_json():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, HtmlPipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse",
                json={"image_base64": PNG, "output": "json"},
            )
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    assert response.headers["x-ocr-storage-status"] == "stored"
    page = response.json()["pages"][0]
    assert page["markdown"] is None
    archived = json.loads(archive_files(client)["json_ocr.json"])["pages"][0]
    assert archived["blocks"][0]["content"] == "<b>block content</b>"
    assert archive_files(client)["document.md"] == b"Hello **world**"


async def test_parse_archive_keeps_tables_with_html_format():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings, TableHtmlPipeline(), FakeRecognizer(), artifact_store=store
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse",
                json={"image_base64": PNG, "table_format": "html"},
            )
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    page = response.json()["pages"][0]
    assert page["markdown"] == (
        "Intro<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    files = archive_files(client)
    assert files["document.md"] == (
        "Intro<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    ).encode("utf-8")


async def test_parse_archive_blocks_content_unchanged():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, HtmlPipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse",
                json={"image_base64": PNG},
            )
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    page = response.json()["pages"][0]
    assert page["markdown"] == "Hello **world**"
    assert page["blocks"][0]["content"] == "<b>block content</b>"
    archived = json.loads(archive_files(client)["json_ocr.json"])["pages"][0]
    assert archived["blocks"][0]["content"] == "<b>block content</b>"


def archive_files(client):
    return {entry["Key"].split("/", 1)[-1]: entry["Body"] for entry in client.objects}


@pytest.mark.parametrize("path", ["/parse", "/ocr", "/parse/chunks"])
@pytest.mark.parametrize("multipart", [False, True])
async def test_original_hash_optional_input_and_public_storage_headers(path, multipart):
    settings, client = configured(storage_save_input=True), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings,
        TextPipeline(),
        FakeRecognizer(),
        artifact_store=store,
        chunk_runtime=InlineRuntime(),
    )
    if multipart:
        request = {
            "files": {
                "file": (
                    "../../unsafe.png",
                    base64.b64decode(PNG),
                    "image/png",
                )
            }
        }
        if path == "/parse/chunks":
            request["data"] = {"options": json.dumps({"max_tokens": 512})}
    else:
        request = {"json": {"image_base64": PNG}}
        if path == "/parse/chunks":
            request["json"]["options"] = {"max_tokens": 512}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(path, **request)
            health = await http.get("/health")
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    assert response.headers["x-ocr-storage-status"] == "stored"
    assert response.headers["x-ocr-archive-key"] == client.objects[-1]["Key"]
    assert response.headers["x-ocr-job-id"] in response.headers["x-ocr-archive-key"]
    files = archive_files(client)
    assert files["input.png"] == base64.b64decode(PNG)
    manifest = json.loads(files["manifest.json"])
    assert (
        manifest["source"]["sha256"]
        == hashlib.sha256(base64.b64decode(PNG)).hexdigest()
    )
    assert manifest["source"]["size"] == len(base64.b64decode(PNG))
    assert manifest["source"]["content_type"] == "image/png"
    assert manifest["kind"] == path.lstrip("/").replace("/", "-")
    for entry in manifest["artifacts"]:
        assert hashlib.sha256(files[entry["name"]]).hexdigest() == entry["sha256"]
        assert len(files[entry["name"]]) == entry["size"]
        assert ".." not in entry["key"]
    assert health.json()["storage"]["status"] == "ok"
    assert health.json()["storage"]["timestamp"]
    if path == "/ocr":
        assert "json_ocr.json" not in files
        assert files["document.md"] == response.json()["result"].encode("utf-8")


@pytest.mark.parametrize("save_parsed", [False, True])
async def test_chunks_store_exact_http_bytes_and_full_options_despite_projection(
    save_parsed,
):
    settings = configured(storage_save_parsed=save_parsed)
    client = MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings,
        TextPipeline(),
        FakeRecognizer(),
        artifact_store=store,
        chunk_runtime=InlineRuntime(),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse/chunks",
                json={
                    "image_base64": PNG,
                    "options": {"max_tokens": 512, "metadata_fields": []},
                    "source_id": "projected-out",
                    "lang": "it",
                },
            )
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    files = archive_files(client)
    assert files["json_chunks.json"] == response.content
    assert ("parsed.json" in files) is save_parsed
    assert files["document.md"] == b"# Fixture"
    assert set(response.json()["chunks"][0]) == {"id", "text", "token_count"}
    manifest = json.loads(files["manifest.json"])
    assert manifest["requested"]["options"]["metadata_fields"] == []
    assert manifest["effective"]["source_id"] == "projected-out"
    assert manifest["effective"]["options"]["max_tokens"] == 512
    assert manifest["effective"]["lang"] == "it"


@pytest.mark.parametrize("path", ["/parse", "/ocr", "/parse/chunks"])
async def test_storage_failure_keeps_ocr_success_and_sanitizes_errors(path, caplog):
    class Broken(MemoryS3):
        def put_object(self, **kwargs):
            raise RuntimeError("credential-and-document-secret")

    settings, client = configured(), Broken()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings,
        TextPipeline(),
        FakeRecognizer(),
        artifact_store=store,
        chunk_runtime=InlineRuntime(),
    )
    body = {"image_base64": PNG}
    if path == "/parse/chunks":
        body["options"] = {"max_tokens": 512}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(path, json=body)
    finally:
        await store.close()
    assert response.status_code == 200
    assert response.headers["x-ocr-storage-status"] == "failed"
    assert response.headers["x-ocr-job-id"]
    assert "x-ocr-archive-key" not in response.headers
    if path == "/parse/chunks":
        assert set(response.json()) == {"chunks"}
    else:
        assert response.json()["warnings"][-1]["code"] == "storage_failed"
    assert "credential-and-document-secret" not in response.text + caplog.text


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/parse", {"image_base64": PNG}),
        ("/parse/chunks", {"image_base64": PNG, "options": {"max_tokens": 512}}),
    ],
)
async def test_operation_line_names_caller_pages_job_and_duration_only(path, body):
    settings, client = configured(api_tokens="tok-alice:alice"), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings,
        TextPipeline(),
        FakeRecognizer(),
        artifact_store=store,
        chunk_runtime=InlineRuntime(),
    )
    with printed_by("ocr_server") as printed:
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as http:
                response = await http.post(
                    path,
                    json={**body, "filename": "private-name.png"},
                    headers={"Authorization": "Bearer tok-alice"},
                )
        finally:
            await store.close()
    assert response.status_code == 200, response.text
    job = re.escape(response.headers["x-ocr-job-id"])
    lines = printed.getvalue()
    assert re.search(
        rf"operation POST {path}: caller=alice status=200 pages=1 job={job} "
        r"duration=\d+\.\d{3}s",
        lines,
    ), lines
    assert "private-name" not in lines
    assert PNG not in lines


async def test_partial_ocr_is_a_complete_archive_with_a_separate_indicator():
    class Partial(FakePipeline):
        async def parse_document(self, *args, **kwargs):
            return [{"error": "fixture page failed"}]

    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, Partial(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post("/parse", json={"image_base64": PNG})
    finally:
        await store.close()
    manifest = json.loads(archive_files(client)["manifest.json"])
    assert response.status_code == 200
    assert manifest["complete"] and manifest["partial_ocr"]
    assert manifest["pages"] == 1  # error pages count: the manifest says how many
    assert manifest["warnings"][0]["code"] == "page_failed"


async def test_json_rag_is_saved_as_an_independent_artifact():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, BlockPipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse", json={"image_base64": PNG, "json_rag": True}
            )
    finally:
        await store.close()
    assert (
        json.loads(archive_files(client)["json_rag.json"])
        == response.json()["json_rag"]
    )


async def test_office_capture_hashes_original_before_conversion(converter):  # noqa: F811
    original = ooxml("word/document.xml")
    settings, client = configured(storage_save_input=True), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, FakePipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse",
                json={
                    "document_base64": base64.b64encode(original).decode(),
                    "dpi": 50,
                },
            )
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    files = archive_files(client)
    assert files["input.docx"] == original
    manifest = json.loads(files["manifest.json"])
    assert manifest["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["effective"]["dpi"] == 50


@pytest.mark.parametrize("path", ["/parse", "/ocr"])
async def test_ocr_capacity_is_released_before_storage_wait(path):
    settings = configured(
        storage_timeout_seconds=2,
        storage_max_inflight=1,
        max_queue_size=1,
        element_max_slots=1,
    )
    client = BlockingS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, FakePipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            first = asyncio.create_task(http.post(path, json={"image_base64": PNG}))
            await wait_event(client.entered)
            second = await asyncio.wait_for(
                http.post(path, json={"image_base64": PNG}), 0.5
            )
            assert second.status_code == 200
            assert second.headers["x-ocr-storage-status"] == "failed"
            stats = await http.get("/stats")
            assert stats.json()["queue_size"] == 0
            client.release.set()
            assert (await first).status_code == 200
    finally:
        client.release.set()
        await store.close()


async def test_failed_chunking_and_excluded_endpoints_never_archive(converter):  # noqa: F811
    from ocrserver.chunking.errors import ChunkError

    class BrokenRuntime(InlineRuntime):
        async def run(self, *args, **kwargs):
            raise ChunkError("chunk_capacity_exceeded", "Busy", status_code=503)

    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings,
        FakePipeline(),
        FakeRecognizer(),
        artifact_store=store,
        chunk_runtime=BrokenRuntime(),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            chunks = await http.post(
                "/parse/chunks",
                json={"image_base64": PNG, "options": {"max_tokens": 512}},
            )
            office = await http.post(
                "/office/pdf",
                files={"file": ("file.docx", ooxml("word/document.xml"))},
            )
        app = create_app(
            settings,
            FakePipeline(),
            FakeRecognizer(),
            artifact_store=store,
            chunk_runtime=InlineRuntime(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            standalone = await http.post(
                "/chunk",
                json={
                    "parsed_document": document(("text", "body")),
                    "options": {"max_tokens": 512},
                },
            )
    finally:
        await store.close()
    assert chunks.status_code == 503
    assert office.status_code == standalone.status_code == 200
    assert not client.objects
    assert all(
        "x-ocr-storage-status" not in r.headers for r in (chunks, office, standalone)
    )


async def test_disabled_parity_cors_headers_and_openapi():
    class NeverStore:
        async def save(self, *args, **kwargs):
            pytest.fail("disabled storage must not be called")

        def diagnostic(self):
            return {
                "status": "disabled",
                "last_operation": None,
                "timestamp": None,
            }

    app = create_app(
        Settings(cors_origins="http://browser"),
        FakePipeline(),
        FakeRecognizer(),
        artifact_store=NeverStore(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/parse",
            json={"image_base64": PNG},
            headers={"Origin": "http://browser"},
        )
    assert response.status_code == 200
    assert response.headers["x-ocr-storage-status"] == "disabled"
    assert "x-ocr-job-id" not in response.headers
    assert not response.json()["warnings"]
    assert response.json()["pages"][0]["markdown"] == "# Fixture"
    assert "X-OCR-Archive-Key" in response.headers["access-control-expose-headers"]
    for path in ("/parse", "/ocr", "/parse/chunks"):
        assert set(
            app.openapi()["paths"][path]["post"]["responses"]["200"]["headers"]
        ) == {"X-OCR-Storage-Status", "X-OCR-Job-Id", "X-OCR-Archive-Key"}


async def test_capture_failure_preserves_success(monkeypatch):
    from ocrserver.artifacts import ArtifactCapture

    def broken(*args, **kwargs):
        raise ValueError("private failure")

    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    monkeypatch.setattr(ArtifactCapture, "page", broken)
    app = create_app(settings, FakePipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post("/parse", json={"image_base64": PNG})
    finally:
        await store.close()
    assert response.status_code == 200
    assert response.headers["x-ocr-storage-status"] == "failed"
    assert response.json()["warnings"][-1]["code"] == "storage_failed"
    assert not client.objects


async def test_missing_figures_are_reported_and_image_link_titles_are_preserved():
    class MissingImagePipeline(ImagePipeline):
        async def parse_document(self, *args, **kwargs):
            pages = await super().parse_document(*args, **kwargs)
            pages[0]["markdown"] = (
                '![found](../../outside.png "Caption")\n![missing](missing.png)'
            )
            return pages

    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(
        settings,
        MissingImagePipeline(),
        FakeRecognizer(),
        artifact_store=store,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
        ) as http:
            response = await http.post("/parse", json={"image_base64": PNG})
    finally:
        await store.close()
    files = archive_files(client)
    assert files["document.md"] == (
        b'![found](images/p0001-0001.png "Caption")\n![missing](missing.png)'
    )
    manifest = json.loads(files["manifest.json"])
    assert manifest["archive_warnings"] == [
        {
            "code": "image_unavailable",
            "page": 1,
            "reference": "missing.png",
        }
    ]
    assert response.json()["warnings"] == []


async def test_partial_parse_archive_marks_partial_and_unprocessed_pages(
    monkeypatch,
):
    from test_parse_blocks import BlockPipeline, a4_pdf, sleeper, use_engine_doubles

    use_engine_doubles(monkeypatch)
    pipeline = BlockPipeline(None, sleeper(1.2, "page timeout"))
    from test_storage_browser import MemoryS3 as BrowserS3
    from test_storage_browser import configured as browser_settings

    settings, client = configured(request_timeout=1.0), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, pipeline, FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post(
                "/parse",
                files={"file": ("doc.pdf", a4_pdf(9), "application/pdf")},
                data={"dpi": "250", "json_rag": "true"},
            )
    finally:
        await store.close()
    s3 = BrowserS3()
    s3.objects = {e["Key"]: e["Body"] for e in client.objects}
    settings = browser_settings(request_timeout=1.0)
    storage = StorageBrowser(settings, client=s3, writer_factory=lambda: s3)
    browse = create_app(
        settings, FakePipeline(), FakeRecognizer(), storage_browser=storage
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(browse), base_url="http://test"
    ) as http:
        listing = await http.get("/storage/archives")
    await storage.close()
    assert response.status_code == 200, response.text
    files = archive_files(client)
    manifest = json.loads(files["manifest.json"])
    assert manifest["partial_ocr"] is True
    assert manifest["unprocessed_pages"] == [5, 6, 7, 8, 9]
    rag = json.loads(files["json_rag.json"])
    assert rag["metadata"]["unprocessed_pages"] == [5, 6, 7, 8, 9]
    assert listing.json()["items"][0]["unprocessed_pages"] == [5, 6, 7, 8, 9]
