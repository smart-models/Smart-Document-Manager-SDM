"""Request metrics record real HTTP outcomes, including early rejections."""

import asyncio
import base64
import threading

import httpx
import pytest
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Histogram
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG

import ocr_server
from ocr_server import create_app
from ocrserver.config import Settings
from ocrserver.metrics import ACTIVE_REQUESTS, OCR_DURATION_BUCKETS, REQUESTS
from ocrserver.vllm_client import BackendUnavailable, CompletionResult


class FailedRecognizer:
    """Raise a configured inference error for HTTP and metrics assertions."""

    def __init__(self, error):
        self.error = error

    async def recognize(self, *args):
        raise self.error


@pytest.mark.parametrize(
    "status,settings,body,error",
    [
        (200, {}, {"image_base64": PNG}, None),
        (400, {}, {"image_base64": "invalid"}, None),
        (400, {}, {}, None),
        (400, {}, {"image_base64": None}, None),
        (401, {"api_tokens": "required:test"}, {"image_base64": PNG}, None),
        (413, {"max_file_bytes": 1}, {"image_base64": PNG}, None),
        (422, {}, {"image_base64": PNG, "task": "invalid"}, None),
        (503, {}, {"image_base64": PNG}, BackendUnavailable()),
        (504, {}, {"image_base64": PNG}, TimeoutError()),
        (500, {}, {"image_base64": PNG}, RuntimeError("private failure")),
    ],
)
async def test_ocr_status_is_counted_once(status, settings, body, error):
    recognizer = FailedRecognizer(error) if error else FakeRecognizer()
    app = create_app(Settings(**settings), FakePipeline(), recognizer)
    statuses = (200, 400, 401, 413, 422, 429, 500, 503, 504)
    before = {
        s: REQUESTS.labels(endpoint="ocr", status=str(s))._value.get() for s in statuses
    }
    active = ACTIVE_REQUESTS._value.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/ocr", json=body)
    assert response.status_code == status
    for other in statuses:
        assert REQUESTS.labels(endpoint="ocr", status=str(other))._value.get() - before[
            other
        ] == (other == status)
    assert ACTIVE_REQUESTS._value.get() == active


@pytest.mark.parametrize(
    "task,text",
    [("ocr", "partial text"), ("spotting", "word<|LOC_1|>"), ("ocr", "")],
)
async def test_truncated_ocr_preserves_result_warnings_and_success_metrics(task, text):
    class TruncatedRecognizer:
        def __init__(self):
            self.calls = 0

        async def recognize(self, image, task, timeout):
            self.calls += 1
            return CompletionResult(text, "length")

    recognizer = TruncatedRecognizer()
    app = create_app(Settings(), FakePipeline(), recognizer)
    statuses = (200, 400, 401, 413, 422, 429, 499, 500, 503, 504)
    before = {
        s: REQUESTS.labels(endpoint="ocr", status=str(s))._value.get() for s in statuses
    }
    active = ACTIVE_REQUESTS._value.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/ocr", json={"image_base64": PNG, "task": task})

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"] == text
    assert payload["boxes"] == ([] if task == "spotting" else None)
    warnings = [
        {
            "code": "generation_truncated",
            "severity": "warning",
            "message": "Generation stopped at a token limit; result may be incomplete.",
            "target": {"field": "result"},
            "details": {"finish_reason": "length"},
        }
    ]
    if task == "spotting":
        warnings.append(
            {
                "code": "spotting_incomplete_coordinates",
                "severity": "warning",
                "message": (
                    "Some location tokens could not form a complete quadrilateral."
                ),
                "target": None,
                "details": {"incomplete_tokens": 1},
            }
        )
    assert payload["warnings"] == warnings
    assert recognizer.calls == 1
    for status in statuses:
        assert REQUESTS.labels(
            endpoint="ocr", status=str(status)
        )._value.get() - before[status] == (status == 200)
    assert ACTIVE_REQUESTS._value.get() == active


@pytest.mark.parametrize(
    "status,body,settings",
    [
        (400, {"image_base64": "!"}, {}),
        (413, {"image_base64": PNG}, {"max_file_bytes": 1}),
        (422, {"image_base64": PNG, "max_pages": 0}, {}),
    ],
)
async def test_parse_early_failures_are_not_successes(status, body, settings):
    app = create_app(Settings(**settings), FakePipeline(), FakeRecognizer())
    success = REQUESTS.labels(endpoint="parse", status="200")._value.get()
    failed = REQUESTS.labels(endpoint="parse", status=str(status))._value.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/parse", json=body)
    assert response.status_code == status
    assert REQUESTS.labels(endpoint="parse", status="200")._value.get() == success
    assert (
        REQUESTS.labels(endpoint="parse", status=str(status))._value.get() == failed + 1
    )


async def test_rate_rejection_and_non_ascii_header():
    app = create_app(
        Settings(api_tokens="secret:test", rate_limit_rpm=1, rate_limit_burst=1),
        FakePipeline(),
        FakeRecognizer(),
    )
    before = REQUESTS.labels(endpoint="parse", status="429")._value.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/parse", headers=[(b"authorization", b"Bearer \xff")])
        ).status_code == 401
        assert (await client.post("/parse")).status_code == 429
    assert REQUESTS.labels(endpoint="parse", status="429")._value.get() == before + 1


def test_long_ocr_duration_has_finite_bucket():
    registry = CollectorRegistry()
    metric = Histogram(
        "test_duration", "duration", buckets=OCR_DURATION_BUCKETS, registry=registry
    )
    metric.observe(120)
    samples = next(iter(metric.collect())).samples
    assert any(s.labels.get("le") == "120.0" and s.value == 1 for s in samples)
    assert any(s.labels.get("le") == "60.0" and s.value == 0 for s in samples)


async def test_slow_preparation_keeps_capacity_after_http_timeout(monkeypatch):
    started, finish = threading.Event(), threading.Event()
    original = ocr_server._prepare_element

    def blocked(data, task, capture=None, reserve=None):
        started.set()
        finish.wait(3)
        return original(data, task, capture, reserve)

    monkeypatch.setattr(ocr_server, "_prepare_element", blocked)
    app = create_app(
        Settings(element_max_slots=1, request_timeout=0.05),
        FakePipeline(),
        FakeRecognizer(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        try:
            request = asyncio.create_task(
                client.post("/ocr", json={"image_base64": PNG})
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert started.is_set()
            assert (await client.get("/health")).status_code == 200
            assert (await request).status_code == 504
            assert (
                await client.post("/ocr", json={"image_base64": PNG})
            ).status_code == 503
        finally:
            finish.set()
            await asyncio.sleep(0.05)
        monkeypatch.setattr(ocr_server, "_prepare_element", original)
        assert (
            await client.post("/ocr", json={"image_base64": PNG})
        ).status_code == 200


async def test_static_cache_headers_and_config_timeout():
    app = create_app(Settings(request_timeout=37), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        config = await client.get("/assets/config.js")
        assert config.headers["cache-control"] == "no-store"
        assert '"requestTimeoutSeconds": 37' in config.text
        for path in ("/", "/assets/app.js"):
            assert (await client.get(path)).headers["cache-control"] == "no-cache"


async def test_cancelled_request_is_not_counted_as_success():
    entered = asyncio.Event()

    class WaitingRecognizer:
        async def recognize(self, *args):
            entered.set()
            await asyncio.Event().wait()

    app = create_app(Settings(), FakePipeline(), WaitingRecognizer())
    success = REQUESTS.labels(endpoint="ocr", status="200")._value.get()
    cancelled = REQUESTS.labels(endpoint="ocr", status="499")._value.get()
    active = ACTIVE_REQUESTS._value.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.post("/ocr", json={"image_base64": PNG}))
        await asyncio.wait_for(entered.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert REQUESTS.labels(endpoint="ocr", status="200")._value.get() == success
    assert REQUESTS.labels(endpoint="ocr", status="499")._value.get() == cancelled + 1
    assert ACTIVE_REQUESTS._value.get() == active


async def test_office_pdf_requests_are_instrumented():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    before = REQUESTS.labels(endpoint="office/pdf", status="400")._value.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/office/pdf",
            files={"file": ("page.png", base64.b64decode(PNG))},
        )
    assert response.status_code == 400
    assert (
        REQUESTS.labels(endpoint="office/pdf", status="400")._value.get() == before + 1
    )


async def test_metrics_uses_prometheus_content_type():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(CONTENT_TYPE_LATEST)
    assert CONTENT_TYPE_LATEST.startswith("text/plain; version=")


async def test_health_reports_autotune_reason(monkeypatch):
    monkeypatch.setenv("OCR_AUTOTUNE_REASON", "no GPU detected — kept defaults")
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        autotune = (await client.get("/health")).json()["autotune"]
    assert autotune["reason"] == "no GPU detected — kept defaults"


async def test_health_omits_empty_autotune_reason(monkeypatch):
    monkeypatch.setenv("OCR_AUTOTUNE_REASON", "   ")
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        autotune = (await client.get("/health")).json()["autotune"]
    assert "reason" not in autotune


async def test_image_memory_error_remains_an_internal_failure(monkeypatch):
    def broken(_):
        raise MemoryError("private allocation detail")

    monkeypatch.setattr(ocr_server.Image, "open", broken)
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post("/ocr", json={"image_base64": PNG})
    assert result.status_code == 500
    assert "private allocation" not in result.text


def test_parse_splits_multipage_images_and_names_oversized_ones(monkeypatch):
    import io

    import pytest
    from fastapi import HTTPException
    from PIL import Image

    from ocr_server import _prepare_document

    frames = [Image.new("RGB", (8, 8)) for _ in range(3)]
    tiff = io.BytesIO()
    frames[0].save(tiff, format="TIFF", save_all=True, append_images=frames[1:])
    plan, warnings = _prepare_document(tiff.getvalue(), 200, 2, False, Settings())
    assert plan.pages_total == 2 and plan.dpi is None
    plan.close()
    assert [w.code for w in warnings] == ["pages_truncated"]
    assert warnings[0].details == {"max_pages": 2, "total_pages": 3}

    png = io.BytesIO()
    Image.new("RGB", (20, 20)).save(png, format="PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 300)
    with pytest.raises(HTTPException) as error:
        _prepare_document(png.getvalue(), 200, 2, False, Settings())
    assert error.value.detail["code"] == "image_too_large"


async def test_health_reports_effective_tuning_and_not_run(monkeypatch):
    monkeypatch.delenv("OCR_AUTOTUNE_ACTION", raising=False)
    app = create_app(Settings(vl_lanes=3), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        autotune = (await client.get("/health")).json()["autotune"]
    assert autotune["action"] == "not_run"
    assert autotune["effective"] == {
        "vl_lanes": 3,
        "page_parallelism": 1,
        "layout_threads": None,
    }
