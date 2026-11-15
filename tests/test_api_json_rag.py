"""/parse json_rag flag: independent of output and include_images."""

import base64

import httpx

import ocr_server
from ocr_server import create_app
from ocrserver.config import Settings
from ocrserver.schemas import Block
from test_api_office import b64, converter  # noqa: F401  (pytest fixture)
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG
from test_office_utils import ooxml


class BlockPipeline(FakePipeline):
    """Emit body and page-number blocks for JSON-RAG API tests."""

    async def parse_document(
        self,
        images,
        lanes,
        page_timeout,
        timeout,
        *,
        close_images=False,
        dpi=None,
        reread=False,
    ):
        pages = await super().parse_document(
            images, lanes, page_timeout, timeout, close_images=close_images
        )
        for page in pages:
            page["blocks"] = [
                Block(type="text", bbox=[0, 0, 10, 10], content=" Hello ", order=1),
                Block(type="number", bbox=[0, 90, 10, 100], content="1", order=None),
            ]
        return pages


def app():
    return create_app(Settings(), BlockPipeline(), FakeRecognizer())


async def post(application, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        return await client.post("/parse", **kwargs)


def upload(name="scan.png"):
    return {"file": (name, base64.b64decode(PNG), "image/png")}


async def test_json_rag_is_null_unless_requested():
    response = await post(app(), files=upload())
    assert response.status_code == 200, response.text
    assert response.json()["json_rag"] is None


async def test_multipart_json_rag_is_independent_from_include_images():
    rag = (await post(app(), files=upload(), data={"json_rag": " TRUE "})).json()
    assert rag["json_rag"]["chunks"][0]["text"] == "Hello"
    assert not rag["pages"][0]["images"]
    images = (await post(app(), files=upload(), data={"include_images": "on"})).json()
    assert images["json_rag"] is None


async def test_chunks_carry_upload_name_even_when_output_is_markdown():
    response = await post(
        app(), files=upload(), data={"output": "markdown", "json_rag": "yes"}
    )
    body = response.json()
    assert body["pages"][0]["blocks"] is None
    chunk = body["json_rag"]["chunks"][0]
    # A printed folio equal to the physical page index is redundant and omitted.
    assert chunk == {
        "id": 1,
        "text": "Hello",
        "type": "text",
        "document": "scan.png",
        "page": 1,
    }
    meta = body["json_rag"]["metadata"]
    assert meta["model_name"] == body["model_name"]
    assert meta["pipeline_version"] == body["pipeline_version"]
    assert meta["dpi"] is None
    assert meta["table_format"] == "html"
    assert meta["n_chunks"] == 1


async def test_json_body_uses_filename_base_name():
    response = await post(
        app(),
        json={"image_base64": PNG, "json_rag": True, "filename": "C:\\in\\scan.png"},
    )
    assert response.json()["json_rag"]["metadata"]["document"] == "scan.png"
    unnamed = await post(app(), json={"image_base64": PNG, "json_rag": True})
    assert unnamed.json()["json_rag"]["metadata"]["document"] == "document"


async def test_office_upload_keeps_original_name_and_dpi(converter):  # noqa: F811
    settings = Settings(office_soffice_path="/opt/soffice", office_timeout_seconds=7)
    response = await post(
        create_app(settings, BlockPipeline(), FakeRecognizer()),
        json={
            "document_base64": b64(ooxml("word/document.xml")),
            "json_rag": True,
            "filename": "report.docx",
            "dpi": 150,
        },
    )
    assert response.status_code == 200, response.text
    meta = response.json()["json_rag"]["metadata"]
    assert meta["document"] == "report.docx"
    assert meta["dpi"] == 150


async def test_builder_failure_keeps_the_ocr_response(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("builder bug")

    monkeypatch.setattr(ocr_server, "build_json_rag", boom)
    response = await post(app(), files=upload(), data={"json_rag": "1"})
    assert response.status_code == 200
    body = response.json()
    assert body["json_rag"] is None
    assert "json_rag_failed" in [w["code"] for w in body["warnings"]]
    assert body["pages"][0]["markdown"] == "# Fixture"
