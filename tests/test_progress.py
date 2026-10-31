"""In-memory parse progress registry and its HTTP surface."""

import base64

import httpx
import pytest
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG

from ocr_server import create_app
from ocrserver.config import Settings
from ocrserver.progress import ProgressRegistry, parse_progress_id

PNG_BYTES = base64.b64decode(PNG)
ID1 = "0f8fad5b-d9cb-469f-a165-70867728950e"
ID2 = "1f8fad5b-d9cb-469f-a165-70867728950e"
ID3 = "2f8fad5b-d9cb-469f-a165-70867728950e"


def test_parse_progress_id_accepts_canonical_uuid_only():
    assert (
        parse_progress_id("0F8FAD5B-D9CB-469F-A165-70867728950E")
        == "0f8fad5b-d9cb-469f-a165-70867728950e"
    )
    assert parse_progress_id("") is None and parse_progress_id(None) is None
    for bad in (
        "x",
        "0f8fad5bd9cb469fa16570867728950e",
        "{0f8fad5b-d9cb-469f-a165-70867728950e}",
    ):
        with pytest.raises(ValueError):
            parse_progress_id(bad)


def test_snapshot_has_exact_keys_and_dashes_before_planning():
    snap = ProgressRegistry(4).register(ID1, 250).snapshot(0.0)
    assert set(snap) == {
        "state",
        "pages_total",
        "pages_done",
        "pages_failed",
        "pages_unprocessed",
        "block",
        "blocks_total",
        "block_pages",
        "dpi",
        "elapsed_s",
        "eta_s",
    }
    assert snap["state"] == "queued" and snap["pages_total"] is None
    assert snap["eta_s"] is None


def test_eta_is_per_page_rate_times_remaining_pages():
    t = [0.0]
    registry = ProgressRegistry(4, clock=lambda: t[0])
    entry = registry.register(ID1, 250)
    entry.plan(16, 1)  # one block: the ETA must not wait for it to end
    t[0] = 10.0
    entry.begin_block(1, 16)
    assert entry.snapshot(12.0)["eta_s"] is None  # no page delivered yet
    for _ in range(4):
        entry.page_done()
    snap = entry.snapshot(30.0)  # 20 s of OCR for 4 pages -> 5 s/page x 12 left
    assert (snap["pages_done"], snap["eta_s"]) == (4, 60)


def test_eta_counts_failed_and_skips_unprocessed_pages():
    t = [0.0]
    registry = ProgressRegistry(4, clock=lambda: t[0])
    entry = registry.register(ID1, 250)
    entry.plan(10, 2)
    entry.begin_block(1, 5)
    for _ in range(3):
        entry.page_done()
    entry.end_block(0, 1, 1)  # 3 done, 1 failed, 1 lost
    # 8 s for 4 processed pages -> 2 s/page x (10 - 5) remaining
    assert entry.snapshot(8.0)["eta_s"] == 10


def test_finished_entry_freezes_elapsed_and_expires_after_retention():
    t = [0.0]
    registry = ProgressRegistry(4, clock=lambda: t[0])
    entry = registry.register(ID1, 250)
    t[0] = 5.0
    registry.finish(entry, "done")
    snap = entry.snapshot(50.0)
    assert (snap["state"], snap["elapsed_s"], snap["eta_s"]) == ("done", 5.0, None)
    t[0] = 64.9
    assert registry.get(ID1) is entry
    t[0] = 65.1
    assert registry.get(ID1) is None


def test_full_registry_evicts_oldest_finished_entry():
    t = [0.0]
    registry = ProgressRegistry(2, clock=lambda: t[0])
    a, b = registry.register(ID1, 250), registry.register(ID2, 250)
    t[0] = 1.0
    registry.finish(a, "done")
    t[0] = 2.0
    registry.finish(b, "failed")
    assert registry.register(ID3, 250) is not None
    assert registry.get(ID1) is None and registry.get(ID2) is b


def test_full_registry_of_active_entries_and_duplicates_return_none():
    registry = ProgressRegistry(1)
    a = registry.register(ID1, 250)
    assert registry.register(ID2, 250) is None
    assert registry.register(ID1, 250) is None and registry.get(ID1) is a


def _client(settings=None):
    app = create_app(settings or Settings(), FakePipeline(), FakeRecognizer())
    return app, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def test_progress_route_404_422_and_no_document_fields():
    _, client = _client()
    async with client:
        r = await client.get(f"/parse/progress/{ID1}")
        assert (
            r.status_code == 404 and r.json()["detail"]["code"] == "progress_not_found"
        )
        r = await client.get("/parse/progress/x")
        assert (
            r.status_code == 422 and r.json()["detail"]["code"] == "invalid_progress_id"
        )


async def test_progress_route_requires_token_when_auth_enabled():
    _, client = _client(Settings(api_tokens="secret:test"))
    async with client:
        assert (await client.get(f"/parse/progress/{ID1}")).status_code == 401
        r = await client.get(
            f"/parse/progress/{ID1}", headers={"Authorization": "Bearer secret"}
        )
        assert r.status_code == 404


async def test_progress_route_is_exempt_from_rate_limit():
    _, client = _client(Settings(rate_limit_rpm=1, rate_limit_burst=1))
    async with client:
        for _ in range(5):
            assert (await client.get(f"/parse/progress/{ID1}")).status_code != 429


async def test_parse_rejects_malformed_progress_id_before_inference():
    app = create_app(Settings(), pipeline := FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.post(
            "/parse",
            files={"file": ("a.png", PNG_BYTES, "image/png")},
            data={"progress_id": "x"},
        )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_progress_id"
    assert pipeline.calls == []


async def test_parse_registers_progress_and_finishes_done():
    _, client = _client()
    async with client:
        r = await client.post(
            "/parse",
            files={"file": ("secret-name.png", PNG_BYTES, "image/png")},
            data={"progress_id": ID1},
        )
        assert r.status_code == 200
        r = await client.get(f"/parse/progress/{ID1}")
    body = r.json()
    assert r.status_code == 200 and body["state"] == "done"
    assert "dpi" in body and "pages_done" in body
    assert "secret-name" not in r.text


async def test_parse_json_body_registers_progress():
    _, client = _client()
    async with client:
        r = await client.post("/parse", json={"image_base64": PNG, "progress_id": ID1})
        assert r.status_code == 200
        progress = await client.get(f"/parse/progress/{ID1}")
    assert progress.status_code == 200 and progress.json()["state"] == "done"


async def test_parse_json_body_rejects_malformed_progress_id():
    app = create_app(Settings(), pipeline := FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.post("/parse", json={"image_base64": PNG, "progress_id": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_progress_id"
    assert pipeline.calls == []


def test_progress_id_is_accepted_by_both_upload_bodies_in_openapi():
    app, _ = _client()
    content = app.openapi()["paths"]["/parse"]["post"]["requestBody"]["content"]
    assert "progress_id" in content["multipart/form-data"]["schema"]["properties"]
    assert "progress_id" in content["application/json"]["schema"]["properties"]
