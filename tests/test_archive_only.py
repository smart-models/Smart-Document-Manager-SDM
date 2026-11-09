"""Archive-only API: stored archives stay readable with no vLLM and no GPU work.

Every test runs the real FastAPI app with fake backends; the fakes record
their calls so a refusal can be shown to happen before any layout, VL or
rasterization work rather than merely to return 503.
"""

import httpx
import pytest
from test_chunk_api import InlineRuntime, TextPipeline
from test_chunk_engine import document
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG
from test_storage_browser import MemoryS3 as BrowserS3
from test_storage_browser import configured as browser_settings

import autotune_entrypoint
import ocr_server
from ocr_server import create_app
from ocrserver.config import Settings, load_settings
from ocrserver.storage import ArtifactStore
from ocrserver.storage_browser import StorageBrowser

REFUSAL = {
    "detail": {
        "code": "ocr_disabled_archive_only",
        "message": "OCR inference is disabled in archive-only mode.",
    }
}


def archive_settings(**overrides):
    """Settings an archive-only server accepts: storage writer and browser on."""
    return browser_settings(archive_only=True, **overrides)


class ProbingS3(BrowserS3):
    """A browser fake whose bucket probe can be made to fail on demand."""

    def __init__(self, *, reachable=True):
        super().__init__()
        self.reachable = reachable

    def head_bucket(self, **kwargs):
        if not self.reachable:
            raise RuntimeError("bucket unreachable")
        return {}


def archive_app(s3=None, **overrides):
    """Build an archive-only app over one fake S3 shared by writer and browser."""
    settings = archive_settings(**overrides)
    s3 = s3 or ProbingS3()
    pipeline, recognizer, runtime = FakePipeline(), FakeRecognizer(), InlineRuntime()
    store = ArtifactStore(settings, client=s3)
    browser = StorageBrowser(settings, client=s3, writer_factory=lambda: s3)
    app = create_app(
        settings,
        pipeline,
        recognizer,
        chunk_runtime=runtime,
        artifact_store=store,
        storage_browser=browser,
    )
    return app, pipeline, recognizer, runtime


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t")


# --- settings and entrypoint ------------------------------------------------


def test_archive_only_requires_the_storage_writer_and_browser_roles():
    with pytest.raises(ValueError, match="OCR_STORAGE_BROWSER_ENABLED"):
        Settings(archive_only=True)
    with pytest.raises(ValueError, match="archive-only"):
        Settings(
            archive_only=True,
            storage_enabled=True,
            s3_access_key="w",
            s3_secret_key="s",
        )
    assert archive_settings().archive_only is True
    assert load_settings({}, None).archive_only is False
    with pytest.raises(ValueError):
        load_settings({"OCR_ARCHIVE_ONLY": "maybe"}, None)


def test_entrypoint_skips_gpu_detection_probes_and_calibration(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("archive-only must not touch the GPU or vLLM")

    monkeypatch.setattr(autotune_entrypoint, "detect_gpu", forbidden)
    monkeypatch.setattr(autotune_entrypoint, "run_autotune", forbidden)
    monkeypatch.setattr(autotune_entrypoint, "build_fingerprint", forbidden)
    monkeypatch.delenv("OCR_AUTOTUNE_ACTION", raising=False)
    result = autotune_entrypoint.prepare(
        {"OCR_ARCHIVE_ONLY": "true", "OCR_AUTOTUNE": "full"}
    )
    assert result["action"] == "disabled"
    assert "archive-only" in result["reason"]
    assert autotune_entrypoint.os.environ["OCR_AUTOTUNE_ACTION"] == "disabled"
    # The standard path is untouched: off still means off without probing.
    assert autotune_entrypoint.prepare({"OCR_AUTOTUNE": "off"})["reason"] == (
        "autotune is off"
    )


# --- health, polling and metrics -------------------------------------------


async def test_health_is_healthy_with_vllm_disabled_and_names_the_mode():
    app, *_ = archive_app()
    async with client(app) as http:
        await app.state.artifact_store.probe()
        health = (await http.get("/health")).json()
        metrics = (await http.get("/metrics")).text
    assert health["status"] == "healthy"
    assert health["storage"]["status"] == "ok"
    assert health["mode"] == "archive_only"
    assert health["vllm"] == "disabled"
    assert health["ocr_enabled"] is False
    assert "ocr_archive_only_mode 1.0" in metrics
    assert "ocr_vllm_up 0.0" in metrics


async def test_standard_health_keeps_its_vocabulary_and_adds_the_mode(monkeypatch):
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with client(app) as http:
        ocr_server.VLLM_UP.set(0)
        down = (await http.get("/health")).json()
        ocr_server.VLLM_UP.set(1)
        up = (await http.get("/health")).json()
        metrics = (await http.get("/metrics")).text
    assert (down["status"], down["vllm"]) == ("degraded", "unreachable")
    assert (up["status"], up["vllm"]) == ("healthy", "reachable")
    assert up["mode"] == "ocr" and up["ocr_enabled"] is True
    assert "ocr_archive_only_mode 0.0" in metrics


async def test_archive_only_is_degraded_until_the_first_probe_succeeds():
    # The bucket is the only upstream: before any probe answers, readiness
    # must not let MCP start on an unverified archive.
    app, *_ = archive_app()
    async with client(app) as http:
        before = (await http.get("/health")).json()
        await app.state.artifact_store.probe()
        after = (await http.get("/health")).json()
    assert (before["status"], before["storage"]["status"]) == ("degraded", "unknown")
    assert before["vllm"] == "disabled" and before["mode"] == "archive_only"
    assert (after["status"], after["storage"]["status"]) == ("healthy", "ok")


async def test_unreachable_archive_bucket_degrades_archive_only_health():
    app, *_ = archive_app(ProbingS3(reachable=False))
    async with client(app) as http:
        await app.state.artifact_store.probe()
        health = (await http.get("/health")).json()
    assert health["status"] == "degraded"
    assert health["vllm"] == "disabled"
    assert health["storage"]["status"] == "error"


async def test_lifespan_never_polls_vllm_in_archive_only(monkeypatch):
    polled = []

    async def record(settings, vllm_client):
        polled.append(settings.archive_only)

    monkeypatch.setattr(ocr_server, "_poll_vllm", record)
    ocr_server.VLLM_UP.set(1)
    app, *_ = archive_app()
    async with app.router.lifespan_context(app):
        assert ocr_server.VLLM_UP._value.get() == 0.0
    standard = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with standard.router.lifespan_context(standard):
        pass
    assert polled == [False]


# --- refused and allowed routes ---------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/parse", {"image_base64": PNG}),
        ("/ocr", {"image_base64": PNG}),
        ("/parse/chunks", {"image_base64": PNG, "options": {"max_tokens": 512}}),
    ],
)
async def test_inference_routes_are_refused_before_any_model_work(path, body):
    app, pipeline, recognizer, runtime = archive_app()
    before = ocr_server.REQUESTS_REJECTED.labels(reason="archive_only")._value.get()
    async with client(app) as http:
        response = await http.post(path, json=body)
        # Multipart uploads are refused before the body is parsed as well.
        upload = await http.post(
            path, files={"file": ("a.png", b"not-read", "image/png")}
        )
    assert response.status_code == upload.status_code == 503
    assert response.json() == upload.json() == REFUSAL
    assert pipeline.calls == [] and recognizer.tasks == [] and runtime.calls == []
    assert (
        ocr_server.REQUESTS_REJECTED.labels(reason="archive_only")._value.get()
        == before + 2
    )


async def test_refusal_keeps_auth_first_and_leaves_progress_route_alone():
    app, *_ = archive_app(api_tokens="secret:console")
    async with client(app) as http:
        anonymous = await http.post("/parse", json={"image_base64": PNG})
        authorized = await http.post(
            "/parse",
            json={"image_base64": PNG},
            headers={"Authorization": "Bearer secret"},
        )
        progress = await http.get(
            "/parse/progress/12345678-1234-4123-8123-123456789abc",
            headers={"Authorization": "Bearer secret"},
        )
    assert anonymous.status_code == 401
    assert authorized.status_code == 503 and authorized.json() == REFUSAL
    assert progress.status_code == 404


async def test_cpu_routes_stay_available_in_archive_only():
    app, pipeline, _, runtime = archive_app()
    async with client(app) as http:
        chunks = await http.post(
            "/chunk",
            json={
                "parsed_document": document(("text", "Testo del chunk")),
                "options": {"max_tokens": 512},
            },
        )
        office = await http.post(
            "/office/pdf", files={"file": ("a.png", b"\x89PNG", "image/png")}
        )
        stats = await http.get("/stats")
    assert chunks.status_code == 200, chunks.text
    assert chunks.json()["chunks"]
    assert runtime.calls and pipeline.calls == []
    # The guard let the Office route run; it rejected the bytes on its own terms.
    assert office.status_code == 400
    assert office.json()["detail"]["code"] == "not_office_document"
    assert stats.status_code == 200


async def test_stored_archives_are_listed_inspected_and_downloaded():
    s3 = ProbingS3()
    prefix = s3.current_archive("report.pdf", "Abcd123_")
    app, pipeline, *_ = archive_app(s3)
    async with client(app) as http:
        listing = await http.get("/storage/archives")
        detail = await http.get("/storage/archive", params={"prefix": prefix})
        download = await http.get(
            "/storage/file", params={"key": prefix + "document.md"}
        )
        archive = await http.get("/storage/archive.zip", params={"prefix": prefix})
    assert listing.status_code == 200, listing.text
    assert [item["job_id"] for item in listing.json()["items"]] == ["Abcd123_"]
    assert detail.status_code == 200 and detail.json()["prefix"] == prefix
    assert download.status_code == 200 and download.content == b"# current layout"
    assert archive.status_code == 200
    assert archive.headers["content-type"].startswith("application/zip")
    assert pipeline.calls == []


async def test_standard_mode_still_serves_inference_routes():
    app = create_app(
        Settings(), TextPipeline(), FakeRecognizer(), chunk_runtime=InlineRuntime()
    )
    async with client(app) as http:
        parse = await http.post("/parse", json={"image_base64": PNG})
        ocr = await http.post("/ocr", json={"image_base64": PNG})
    assert parse.status_code == 200 and ocr.status_code == 200
