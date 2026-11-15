"""Quality warnings of /parse: where they appear and where they must not."""

import json

import httpx
import pytest
from test_api_storage import archive_files
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG
from test_parse_blocks import parse_pdf, use_engine_doubles
from test_storage import MemoryS3, configured

from ocr_server import create_app
from ocrserver.config import Settings
from ocrserver.schemas import Block
from ocrserver.storage import ArtifactStore

TABLE = "<table><tr><td>Unit 1</td><td>2 bears</td></tr></table>"
LOW = {"layout_score": 0.34, "suspects": []}
CLEAN = {"layout_score": 0.91, "suspects": []}
QUALITY_KEYS = {"low_layout_confidence_pages", "suspect_table_pages"}


def suspect(reread, reason="cut_repetition", block_id=1, block_type="table"):
    return {
        "block_id": block_id,
        "block_type": block_type,
        "reason": reason,
        "megapixels": 1.0,
        "chars": len(TABLE),
        "reread": reread,
    }


def blocks():
    return [
        Block(type="text", bbox=[0, 0, 100, 20], content="Body", order=1, block_id=0),
        Block(
            type="table",
            bbox=[100, 200, 1100, 1200],
            content=TABLE,
            order=2,
            block_id=1,
        ),
    ]


class QualityPipeline(FakePipeline):
    """Return the same two-block page for every image, with a given quality."""

    def __init__(self, *qualities):
        super().__init__()
        self.qualities, self.dpi, self.reread = qualities, [], []

    async def parse_document(self, images, *args, dpi=None, reread=False, **kwargs):
        await super().parse_document(images, *args, **kwargs)
        self.dpi.append(dpi)
        self.reread.append(reread)
        pages = []
        for quality, _ in zip(self.qualities, images, strict=True):
            page = {
                "markdown": f"Body\n\n{TABLE}",
                "blocks": blocks(),
                "images": [],
                "width": 1700,
                "height": 2200,
                "elapsed": 0.001,
            }
            if quality is not None:
                page["quality"] = quality
            pages.append(page)
        return pages


@pytest.fixture(autouse=True)
def engine_doubles(monkeypatch):
    use_engine_doubles(monkeypatch)


def app(*qualities, **kwargs):
    return create_app(
        Settings(), QualityPipeline(*qualities), FakeRecognizer(), **kwargs
    )


async def test_low_layout_score_is_a_page_warning():
    body = (await parse_pdf(app(LOW, CLEAN), 2)).json()

    (warning,) = body["warnings"]
    assert warning["code"] == "layout_low_confidence"
    assert warning["severity"] == "warning"
    assert warning["target"] == {"field": "page", "index": 0}
    assert warning["details"] == {"layout_score": 0.34, "threshold": 0.5}
    assert "Page 1" in warning["message"]


async def test_unknown_layout_score_is_not_a_warning():
    body = (await parse_pdf(app({"layout_score": None, "suspects": []}), 1)).json()

    assert body["warnings"] == []


@pytest.mark.parametrize(
    ("reread", "severity"),
    [
        ("rejected", "warning"),
        ("skipped", "warning"),
        ("off", "warning"),
        ("kept", "info"),
    ],
)
async def test_suspect_block_is_a_table_suspect_warning(reread, severity):
    flagged = {"layout_score": 0.9, "suspects": [suspect(reread)]}

    body = (await parse_pdf(app(CLEAN, flagged), 2)).json()

    (warning,) = body["warnings"]
    assert warning["code"] == "table_suspect"
    assert warning["severity"] == severity
    assert warning["target"] == {"field": "page", "index": 1}
    assert warning["details"] == suspect(reread)
    assert "Page 2" in warning["message"] and "block 1" in warning["message"]


async def test_flagged_pages_are_listed_in_json_rag_metadata_and_never_in_a_chunk():
    flagged = app(
        LOW,
        {"layout_score": 0.9, "suspects": [suspect("rejected")]},
        {"layout_score": 0.9, "suspects": [suspect("kept", "low_density")]},
    )
    unflagged = app(None, None, None)

    marked = (await parse_pdf(flagged, 3, json_rag="true")).json()["json_rag"]
    plain = (await parse_pdf(unflagged, 3, json_rag="true")).json()["json_rag"]

    # What is loaded into a vector store is the same with and without flags.
    assert marked["chunks"] == plain["chunks"]
    assert len(marked["chunks"]) == 6
    for word in ("suspect", "confidence", "reread", "quality"):
        assert word not in json.dumps(marked["chunks"])
    # The flags live in the metadata: the codes and two page lists.
    assert marked["metadata"]["low_layout_confidence_pages"] == [1]
    # Page 3 stays listed although its second reading was kept: a kept
    # reading passed the checks, which does not prove the table is whole.
    assert marked["metadata"]["suspect_table_pages"] == [2, 3]
    assert marked["metadata"]["warnings"] == [
        "layout_low_confidence",
        "table_suspect",
        "table_suspect",
    ]
    assert plain["metadata"]["low_layout_confidence_pages"] == []
    assert plain["metadata"]["suspect_table_pages"] == []
    changing = QUALITY_KEYS | {"warnings", "processing_time_s"}
    assert {k: v for k, v in marked["metadata"].items() if k not in changing} == {
        k: v for k, v in plain["metadata"].items() if k not in changing
    }


async def test_document_without_flagged_blocks_is_unchanged():
    checked = (await parse_pdf(app(CLEAN, CLEAN), 2, json_rag="true")).json()
    unchecked = (await parse_pdf(app(None, None), 2, json_rag="true")).json()

    for body in (checked, unchecked):
        body.pop("processing_time_ms")
        body["json_rag"]["metadata"].pop("processing_time_s")
    assert checked == unchecked
    assert checked["warnings"] == []
    assert all("quality" not in page for page in checked["pages"])
    metadata = checked["json_rag"]["metadata"]
    assert {key: metadata[key] for key in QUALITY_KEYS} == dict.fromkeys(
        QUALITY_KEYS, []
    )
    # The two empty lists are the only addition to the export's metadata.
    assert set(metadata) - QUALITY_KEYS == {
        "document",
        "model_name",
        "pipeline_version",
        "tokenizer",
        "dpi",
        "table_format",
        "warnings",
        "pages",
        "failed_pages",
        "unprocessed_pages",
        "n_chunks",
        "total_tokens",
        "avg_tokens",
        "max_tokens",
        "min_tokens",
        "unattached_blocks",
    }


async def test_page_resolution_reaches_the_backend():
    pipeline = QualityPipeline(CLEAN)
    application = create_app(Settings(), pipeline, FakeRecognizer())

    await parse_pdf(application, 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as http:
        await http.post("/parse", json={"image_base64": PNG})

    assert pipeline.dpi == [250, None]  # an image has no resolution of its own


@pytest.mark.parametrize("wanted", [True, False])
async def test_second_reading_is_requested_as_the_setting_says(wanted):
    pipeline = QualityPipeline(CLEAN)
    application = create_app(Settings(table_reread=wanted), pipeline, FakeRecognizer())

    await parse_pdf(application, 1)

    assert pipeline.reread == [wanted]


async def test_archive_keeps_the_warnings_and_does_not_call_the_ocr_partial():
    settings, s3 = configured(), MemoryS3()
    store = ArtifactStore(settings, client=s3)
    flagged = {"layout_score": 0.34, "suspects": [suspect("rejected")]}
    application = create_app(
        settings, QualityPipeline(flagged), FakeRecognizer(), artifact_store=store
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://test"
        ) as http:
            response = await http.post("/parse", json={"image_base64": PNG})
    finally:
        await store.close()

    files = archive_files(s3)
    manifest = json.loads(files["manifest.json"])
    assert [w["code"] for w in manifest["warnings"]] == [
        "layout_low_confidence",
        "table_suspect",
    ]
    assert manifest["partial_ocr"] is False
    stored = json.loads(files["json_ocr.json"])
    assert stored["warnings"] == response.json()["warnings"]
    assert "quality" not in stored["pages"][0]
