"""Block engine of /parse: per-block budget, partial results, cancellation, FIFO."""

import asyncio
import base64
import io

import httpx
import pypdfium2
import pytest
from PIL import Image

import ocr_server
from ocr_server import create_app
from ocrserver.config import Settings
from test_api_office import DOCX, converter  # noqa: F401,F811 - shared fixture
from test_mcp_schema import FakeRecognizer
from test_mcp_tools import PNG

ID_A = "0f8fad5b-d9cb-469f-a165-70867728950e"
ID_B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def a4_pdf(n: int) -> bytes:
    doc = pypdfium2.PdfDocument.new()
    for _ in range(n):
        doc.new_page(595, 842)
    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    return buffer.getvalue()


def use_engine_doubles(monkeypatch):
    """4-page blocks (A4 at 250 DPI, 25 M pixels) and 1x1 renders.

    Planning still uses the real page sizes; only rendering is faked.
    """
    monkeypatch.setattr(ocr_server, "BLOCK_MAX_PIXELS", 25_000_000)
    monkeypatch.setattr(ocr_server, "MAX_BLOCK_PAGES", 8)
    monkeypatch.setattr(
        ocr_server,
        "render_pages",
        lambda data, dpi, pages: [Image.new("RGB", (1, 1)) for _ in pages],
    )


@pytest.fixture(autouse=True)
def engine_doubles(monkeypatch):
    use_engine_doubles(monkeypatch)


def ok_page():
    return {
        "markdown": "# ok",
        "blocks": [],
        "images": [],
        "width": 1,
        "height": 1,
        "elapsed": 0.001,
    }


class BlockPipeline:
    """Record len(images) per call; behaviour[i](images) may sleep or fail."""

    def __init__(self, *behaviour):
        self.calls = []
        self.behaviour = list(behaviour)

    async def parse_document(
        self,
        images,
        lanes,
        page_timeout,
        timeout,
        *,
        close_images=False,
        on_page=None,
    ):
        index = len(self.calls)
        self.calls.append(len(images))
        hook = self.behaviour[index] if index < len(self.behaviour) else None
        result = (await hook(images)) if hook else None
        if close_images:
            for image in images:
                image.close()
        result = result or [ok_page() for _ in images]
        for page in result:
            if on_page and not page.get("error"):
                on_page(page)
        return result


def sleeper(seconds, error=None):
    async def hook(images):
        await asyncio.sleep(seconds)
        if error:
            return [{"error": error, "elapsed": seconds} for _ in images]
        return None

    return hook


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t")


async def parse_pdf(app, pages, progress_id=None, **data):
    if progress_id:
        data["progress_id"] = progress_id
    async with client(app) as c:
        return await c.post(
            "/parse",
            files={"file": ("doc.pdf", a4_pdf(pages), "application/pdf")},
            data={"dpi": "250", **data},
        )


def codes(body):
    return [w["code"] for w in body["warnings"]]


async def test_pdf_blocks_follow_dpi_budget():
    pipeline = BlockPipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 9)
    assert r.status_code == 200, r.text
    assert pipeline.calls == [4, 4, 1]
    assert [p["page"] for p in r.json()["pages"]] == list(range(1, 10))


async def test_91_a4_pages_at_250_dpi_are_planned_not_rejected():
    pipeline = BlockPipeline()
    app = create_app(Settings(max_pages=100), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 91)
    assert r.status_code == 200, r.text
    assert pipeline.calls == [4] * 22 + [3]


async def test_single_page_over_budget_is_413_without_inference():
    pipeline = BlockPipeline()
    app = create_app(Settings(pdf_max_page_pixels=1), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 1)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "pdf_pixel_limit"
    assert pipeline.calls == []


async def test_budget_exhausted_returns_completed_blocks():
    pipeline = BlockPipeline(None, sleeper(1.2, "page timeout"))
    app = create_app(Settings(request_timeout=1.0), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 9)
    assert r.status_code == 200, r.text
    body = r.json()
    assert pipeline.calls == [4, 4]
    assert all(p["error"] is None for p in body["pages"][:4])
    assert all(
        p["error"] == "page_unprocessed: request time budget exhausted"
        for p in body["pages"][4:]
    )
    assert "page_failed" not in codes(body)
    (warning,) = [w for w in body["warnings"] if w["code"] == "pages_unprocessed"]
    assert warning["details"]["pages"] == [5, 6, 7, 8, 9]


async def test_mean_page_rule_skips_block_that_cannot_finish():
    # 4-page blocks of 1.2 s (0.3 s/page): after two, ~0.6 s remain < 1.2 s.
    pipeline = BlockPipeline(sleeper(1.2), sleeper(1.2), sleeper(1.2))
    app = create_app(Settings(request_timeout=3.0), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 12)
    assert r.status_code == 200, r.text
    assert pipeline.calls == [4, 4]
    assert r.json()["pages"][8]["error"].startswith("page_unprocessed")


async def test_short_final_block_runs_when_its_pages_fit():
    # Same pace, but the last block has 1 page (0.3 s) and ~0.6 s remain.
    pipeline = BlockPipeline(sleeper(1.2), sleeper(1.2), sleeper(0.1))
    app = create_app(Settings(request_timeout=3.0), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 9)
    assert r.status_code == 200, r.text
    assert pipeline.calls == [4, 4, 1]
    assert not any(p.get("error") for p in r.json()["pages"])


async def test_cancel_interrupts_a_running_block(monkeypatch):
    event = asyncio.Event()

    async def watch(request):
        return event, asyncio.create_task(asyncio.sleep(3600))

    async def slow(images):
        event.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(ocr_server, "_watch_disconnect", watch)
    pipeline = BlockPipeline(slow)
    app = create_app(Settings(), pipeline, FakeRecognizer())
    started = asyncio.get_running_loop().time()
    r = await parse_pdf(app, 9, progress_id=ID_A)
    assert asyncio.get_running_loop().time() - started < 5
    assert pipeline.calls == [4]
    assert all(
        p["error"] == "page_unprocessed: request cancelled" for p in r.json()["pages"]
    )
    async with client(app) as c:
        assert (await c.get(f"/parse/progress/{ID_A}")).json()["state"] == "cancelled"


async def test_block_timeout_with_budget_left_is_page_failed_and_continues():
    pipeline = BlockPipeline(sleeper(0, "page timeout"))
    app = create_app(Settings(), pipeline, FakeRecognizer())
    body = (await parse_pdf(app, 9)).json()
    assert pipeline.calls == [4, 4, 1]
    assert [p["error"] for p in body["pages"][:4]] == ["page timeout"] * 4
    assert codes(body).count("page_failed") == 4
    assert "pages_unprocessed" not in codes(body)


async def test_all_blocks_in_time_have_no_unprocessed_warning():
    app = create_app(Settings(), BlockPipeline(), FakeRecognizer())
    body = (await parse_pdf(app, 9)).json()
    assert "pages_unprocessed" not in codes(body)
    assert all(p["error"] is None for p in body["pages"])


async def test_disconnect_stops_after_current_block_and_skips_archive(monkeypatch):
    event = asyncio.Event()

    async def watch(request):
        return event, asyncio.create_task(asyncio.sleep(3600))

    async def set_event(images):
        event.set()

    monkeypatch.setattr(ocr_server, "_watch_disconnect", watch)
    from ocrserver.storage import ArtifactStore
    from test_api_storage import MemoryS3, configured

    settings = configured()
    store = ArtifactStore(settings, client=MemoryS3())
    saves = []

    async def save(*args, **kwargs):
        saves.append(args)

    store.save = save
    pipeline = BlockPipeline(set_event)
    app = create_app(settings, pipeline, FakeRecognizer(), artifact_store=store)
    r = await parse_pdf(app, 9, progress_id=ID_A)
    assert pipeline.calls == [4]
    assert r.json()["pages"][4]["error"] == "page_unprocessed: request cancelled"
    assert saves == []
    async with client(app) as c:
        state = (await c.get(f"/parse/progress/{ID_A}")).json()["state"]
    assert state == "cancelled"


async def test_documents_are_serialized_fifo():
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(images):
        entered.set()
        await release.wait()

    pipeline = BlockPipeline(hold)
    app = create_app(Settings(), pipeline, FakeRecognizer())
    task_a = asyncio.create_task(parse_pdf(app, 5, progress_id=ID_A))
    await asyncio.wait_for(entered.wait(), 5)
    task_b = asyncio.create_task(parse_pdf(app, 6, progress_id=ID_B))
    snapshot = None
    async with client(app) as c:
        for _ in range(200):
            r = await c.get(f"/parse/progress/{ID_B}")
            if r.status_code == 200:
                snapshot = r.json()
                break
            await asyncio.sleep(0.01)
    release.set()
    a, b = await asyncio.gather(task_a, task_b)
    assert a.status_code == b.status_code == 200
    assert snapshot["state"] == "queued"
    assert pipeline.calls == [4, 1, 4, 2]


async def test_progress_reports_partial_and_counters():
    pipeline = BlockPipeline(None, sleeper(1.2, "page timeout"))
    app = create_app(Settings(request_timeout=1.0), pipeline, FakeRecognizer())
    assert (await parse_pdf(app, 9, progress_id=ID_A)).status_code == 200
    async with client(app) as c:
        body = (await c.get(f"/parse/progress/{ID_A}")).json()
    assert body["state"] == "partial"
    assert body["pages_done"] == 4 and body["pages_unprocessed"] == 5
    assert body["blocks_total"] == 3


async def test_progress_unavailable_never_fails_parse():
    app = create_app(Settings(max_queue_size=1), BlockPipeline(), FakeRecognizer())
    app.state.progress.register(ID_A, 250)  # active entry fills the registry
    assert (await parse_pdf(app, 1, progress_id=ID_B)).status_code == 200
    assert (await parse_pdf(app, 1, progress_id=ID_A)).status_code == 200


async def test_image_upload_is_one_block():
    pipeline = BlockPipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    async with client(app) as c:
        r = await c.post(
            "/parse",
            files={"file": ("a.png", base64.b64decode(PNG), "image/png")},
            data={"progress_id": ID_A},
        )
        body = (await c.get(f"/parse/progress/{ID_A}")).json()
    assert r.status_code == 200 and pipeline.calls == [1]
    assert body["blocks_total"] == 1 and body["dpi"] is None


async def test_large_image_frame_is_not_rejected():
    pipeline = BlockPipeline()
    app = create_app(Settings(pdf_max_page_pixels=1), pipeline, FakeRecognizer())
    async with client(app) as c:
        r = await c.post("/parse", json={"image_base64": PNG})
    assert r.status_code == 200, r.text
    assert pipeline.calls == [1]


async def test_office_upload_runs_through_blocks(converter):  # noqa: F811
    pipeline = BlockPipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    async with client(app) as c:
        r = await c.post("/parse", json={"document_base64": DOCX})
    assert r.status_code == 200, r.text
    assert pipeline.calls == [1]


async def test_truncated_pdf_plans_only_effective_pages():
    pipeline = BlockPipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 9, max_pages="5")
    assert pipeline.calls == [4, 1]
    assert "pages_truncated" in codes(r.json())


async def test_rasterize_timeout_marks_block_unprocessed(monkeypatch):
    import threading
    import time

    slow_done = threading.Event()

    def render(data, dpi, pages):
        if pages.start:
            time.sleep(1.3)
            slow_done.set()
        return [Image.new("RGB", (1, 1)) for _ in pages]

    monkeypatch.setattr(ocr_server, "render_pages", render)
    pipeline = BlockPipeline()
    app = create_app(Settings(request_timeout=1.0), pipeline, FakeRecognizer())
    r = await parse_pdf(app, 9)
    assert r.status_code == 200, r.text
    body = r.json()
    assert pipeline.calls == [4]
    assert all(p["error"] is None for p in body["pages"][:4])
    assert all(p["error"].startswith("page_unprocessed") for p in body["pages"][4:])
    # The abandoned render thread holds the global queue gauge until it exits.
    assert await asyncio.to_thread(slow_done.wait, 5)
    await asyncio.sleep(0.1)


async def test_block_images_are_closed_before_next_render(monkeypatch):
    rendered = []

    class Tracked:
        closed = False

        def close(self):
            self.closed = True

    def render(data, dpi, pages):
        assert all(image.closed for image in rendered)
        block = [Tracked() for _ in pages]
        rendered.extend(block)
        return block

    monkeypatch.setattr(ocr_server, "render_pages", render)
    r = await parse_pdf(create_app(Settings(), BlockPipeline(), FakeRecognizer()), 9)
    assert r.status_code == 200, r.text
    assert len(rendered) == 9 and all(image.closed for image in rendered)


async def test_default_blocks_hold_16_a4_pages_at_250_dpi(monkeypatch):
    from ocrserver import pdf_utils

    monkeypatch.setattr(ocr_server, "BLOCK_MAX_PIXELS", pdf_utils.BLOCK_MAX_PIXELS)
    monkeypatch.setattr(ocr_server, "MAX_BLOCK_PAGES", pdf_utils.MAX_BLOCK_PAGES)
    pipeline = BlockPipeline()
    app = create_app(Settings(max_pages=100), pipeline, FakeRecognizer())
    assert (await parse_pdf(app, 91)).status_code == 200
    assert pipeline.calls == [16] * 5 + [11]


async def test_progress_counts_streamed_pages_once():
    pipeline = BlockPipeline()
    app = create_app(Settings(), pipeline, FakeRecognizer())
    assert (await parse_pdf(app, 9, progress_id=ID_A)).status_code == 200
    async with client(app) as c:
        body = (await c.get(f"/parse/progress/{ID_A}")).json()
    assert (body["state"], body["pages_done"], body["pages_failed"]) == ("done", 9, 0)
