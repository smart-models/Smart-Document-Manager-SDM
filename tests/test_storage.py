"""Archive completion is committed last and timed-out workers retain capacity."""

import asyncio
import json
import re
import subprocess
import sys
import threading
import time
import tracemalloc

import pytest
from botocore.exceptions import ClientError

from ocrserver.config import Settings, load_settings
from ocrserver.schemas import OcrResponse
from ocrserver.storage import ArtifactCapture, ArtifactStore


class MemoryS3:
    """Record object writes and emulate conditional creation and prefix listing."""

    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and any(
            item["Key"] == kwargs["Key"] for item in self.objects
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects.append(kwargs)

    def list_objects_v2(self, **kwargs):
        keys = sorted(
            {
                item["Key"]
                for item in self.objects
                if item["Key"].startswith(kwargs["Prefix"])
            }
        )
        return {"Contents": [{"Key": key} for key in keys[: kwargs["MaxKeys"]]]}

    def close(self):
        pass


def configured(**kwargs):
    return Settings(
        storage_enabled=True,
        s3_access_key="test-access",
        s3_secret_key="test-secret",
        **kwargs,
    )


async def test_success_commits_inventory_last_without_saving_input():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    capture = ArtifactCapture("ocr", settings)
    capture.source(b"original", "image/png")
    result = OcrResponse(result="recognized", task="ocr", processing_time_ms=1)
    try:
        outcome = await store.save(capture, result)
    finally:
        await store.close()
    assert outcome.status == "stored"
    assert outcome.manifest_key == client.objects[-1]["Key"]
    manifest = json.loads(client.objects[-1]["Body"])
    assert manifest["complete"] is True
    assert manifest["pages"] is None  # an /ocr archive has no pages
    assert manifest["source"]["sha256"] == (
        "0682c5f2076f099c34cfdd15a9e063849ed437a49677e6fcc5b4198c76575be5"
    )
    assert [entry["name"] for entry in manifest["artifacts"]] == ["input.md"]
    from ocrserver.storage_index import index_archive

    assert index_archive(client.objects[0]["Key"]) == outcome.manifest_key.removesuffix(
        "manifest.json"
    )
    assert client.objects[1]["Body"] == b'{"complete":false}'
    assert client.objects[2]["Body"] == b"recognized"


@pytest.mark.parametrize("collision", ["listed", "atomic"])
async def test_code_collision_retries_without_overwriting_existing_objects(
    monkeypatch, collision
):
    from ocrserver import storage

    codes = iter(("Collide1", "Unique_2"))
    monkeypatch.setattr(storage.secrets, "token_urlsafe", lambda _: next(codes))

    class CollisionS3(MemoryS3):
        def list_objects_v2(self, **kwargs):
            if collision == "listed" and kwargs["Prefix"].endswith("Collide1/"):
                return {"Contents": [{"Key": kwargs["Prefix"] + "old.txt"}]}
            if collision == "atomic" and kwargs["Prefix"].endswith("Collide1/"):
                return {"Contents": []}
            return super().list_objects_v2(**kwargs)

        def put_object(self, **kwargs):
            if (
                collision == "atomic"
                and kwargs.get("IfNoneMatch") == "*"
                and kwargs["Key"].split("__")[-1].startswith("Collide1/")
            ):
                self.objects.append({**kwargs, "Body": b"foreign reservation"})
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
                )
            return super().put_object(**kwargs)

    settings, client = configured(), CollisionS3()
    store = ArtifactStore(settings, client=client)
    capture = ArtifactCapture("ocr", settings, original_filename="invoice.pdf")
    result = OcrResponse(result="recognized", task="ocr", processing_time_ms=1)
    try:
        outcome = await store.save(capture, result)
    finally:
        await store.close()

    assert outcome.status == "stored"
    assert outcome.job_id == "Unique_2"
    assert outcome.manifest_key.split("__")[-1] == "Unique_2/manifest.json"
    assert client.objects[-1]["Key"] == outcome.manifest_key
    assert not any(
        item["Key"].endswith("Collide1/invoice.md") for item in client.objects
    )


@pytest.mark.parametrize(
    "values",
    [
        {"storage_enabled": True},
        {"storage_timeout_seconds": float("nan")},
        {"storage_timeout_seconds": float("inf")},
        {"storage_max_inflight": True},
        {"storage_max_objects": 0},
        {"storage_max_bytes": 1.5},
        {"s3_endpoint": "https://user:secret@example.org"},
        {"s3_endpoint": "http://example.org/?secret=1"},
        {"s3_endpoint": "http://example.org/path"},
        {"s3_addressing_style": "automatic"},
        {"s3_bucket": "../../invalid"},
    ],
)
def test_invalid_configuration_is_rejected_without_echoing_values(values):
    with pytest.raises((ValueError, TypeError)) as error:
        Settings(**values)
    assert "user:secret" not in str(error.value)


def test_environment_resolution_and_credentials_are_explicit_and_redacted():
    settings = load_settings(
        {
            "OCR_STORAGE_ENABLED": "true",
            "OCR_S3_ACCESS_KEY": "private-access",
            "OCR_S3_SECRET_KEY": "private-secret",
            "OCR_STORAGE_MAX_OBJECTS": "14",
            "OCR_STORAGE_SAVE_INPUT": "true",
            "OCR_STORAGE_TIMEOUT_SECONDS": "1.5",
        },
        None,
    )
    assert settings.storage_enabled and settings.storage_save_input
    assert settings.storage_max_objects == 14
    assert settings.storage_timeout_seconds == 1.5
    assert "private-access" not in repr(settings)
    assert "private-secret" not in repr(settings)


async def test_disabled_does_not_construct_a_client_or_worker(monkeypatch):
    store = ArtifactStore(Settings())
    monkeypatch.setattr(store, "_make_client", lambda: pytest.fail("network"))
    result = await store.save(None, None)
    assert result.headers() == {"X-OCR-Storage-Status": "disabled"}
    assert store.diagnostic()["status"] == "disabled"
    await store.close()


@pytest.mark.parametrize(
    "limits",
    [
        {"storage_max_bytes": 20},
        {"storage_max_objects": 1},
    ],
)
async def test_limits_reject_before_the_first_put(limits):
    settings, client = configured(**limits), MemoryS3()
    store = ArtifactStore(settings, client=client)
    capture = ArtifactCapture("ocr", settings)
    capture.source(b"original", "image/png")
    try:
        result = await store.save(
            capture,
            OcrResponse(result="text", task="ocr", processing_time_ms=1),
        )
    finally:
        await store.close()
    assert result.status == "failed"
    assert not client.objects
    assert store.diagnostic()["status"] == "error"


class BlockingS3(MemoryS3):
    """Gate writes with events to test timeout and worker-capacity ownership."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()

    def put_object(self, **kwargs):
        self.entered.set()
        try:
            if not self.release.wait(3):
                raise TimeoutError("test safety timeout")
            super().put_object(**kwargs)
        finally:
            self.exited.set()


async def wait_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


async def test_timeout_keeps_worker_capacity_and_never_commits_late_manifest():
    settings = configured(storage_timeout_seconds=0.1, storage_max_inflight=1)
    client, capture = BlockingS3(), ArtifactCapture("ocr", settings)
    result = OcrResponse(result="text", task="ocr", processing_time_ms=1)
    store = ArtifactStore(settings, client=client)
    try:
        first = asyncio.create_task(store.save(capture, result))
        await wait_event(client.entered)
        outcome = await first
        assert outcome.status == "failed"
        assert outcome.manifest_key is None
        assert re.fullmatch("[A-Za-z0-9_-]{8}", outcome.job_id)
        second = await store.save(capture, result)
        assert second.status == "failed"
        assert not client.objects
        client.release.set()
        await wait_event(client.exited)
        await asyncio.sleep(0.02)
        third = await store.save(capture, result)
        assert third.status == "stored"
        assert (
            len(client.objects) == 5
        )  # abandoned index + fresh index/reservation/document/manifest
        from ocrserver.storage_index import index_archive

        assert index_archive(client.objects[0]["Key"]).endswith(outcome.job_id + "/")
        assert index_archive(client.objects[1]["Key"]).endswith(third.job_id + "/")
    finally:
        client.release.set()
        await store.close()


async def test_cancellation_propagates_and_close_has_a_bounded_wait():
    settings = configured(storage_timeout_seconds=0.05, storage_max_inflight=1)
    client, capture = BlockingS3(), ArtifactCapture("ocr", settings)
    store = ArtifactStore(settings, client=client)
    result = OcrResponse(result="text", task="ocr", processing_time_ms=1)
    first = asyncio.create_task(store.save(capture, result))
    try:
        await wait_event(client.entered)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await store.save(capture, result)).status == "failed"
        started = time.monotonic()
        await store.close()
        assert time.monotonic() - started < 0.5
        assert (await store.save(capture, result)).status == "failed"
    finally:
        client.release.set()
        await wait_event(client.exited)
    assert len(client.objects) == 1


async def test_put_failure_never_commits_manifest_and_diagnostics_have_no_secrets():
    class Broken(MemoryS3):
        def put_object(self, **kwargs):
            raise RuntimeError("secret-access-key document contents")

    settings = configured()
    store = ArtifactStore(settings, client=Broken())
    assert store.diagnostic() == {
        "status": "unknown",
        "last_operation": None,
        "timestamp": None,
    }
    outcome = await store.save(
        ArtifactCapture("ocr", settings),
        OcrResponse(result="text", task="ocr", processing_time_ms=1),
    )
    await store.close()
    assert outcome.status == "failed"
    assert "secret" not in json.dumps(store.diagnostic())
    assert store.diagnostic()["last_operation"] == "failed"


def test_real_client_uses_explicit_credentials_sigv4_and_bounded_no_retry_policy(
    monkeypatch,
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    store = ArtifactStore(configured())
    client = store._make_client()
    try:
        assert client._request_signer._credentials.access_key == "test-access"
        assert client._request_signer._credentials.secret_key == "test-secret"
        config = client.meta.config
        assert config.signature_version == "s3v4"
        assert config.s3["addressing_style"] == "path"
        assert config.retries["total_max_attempts"] == 1
        assert config.connect_timeout <= 2 and config.read_timeout <= 2
    finally:
        client.close()


async def test_serialization_failure_writes_nothing(monkeypatch):
    import ocrserver.storage

    def bad_serializer(*args, **kwargs):
        raise ValueError("private invalid data")

    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    monkeypatch.setattr(ocrserver.storage, "build_archive", bad_serializer)
    try:
        result = await store.save(ArtifactCapture("ocr", settings), None)
    finally:
        await store.close()
    assert result.status == "failed"
    assert not client.objects


def test_capture_limits_discard_large_references_without_raising():
    capture = ArtifactCapture("parse", configured(storage_max_bytes=10))
    capture.page(1, "body", [{"name": "figure.png", "png": b"12345678901"}])
    assert capture.failed
    assert capture.pages == [] and capture.input_bytes is None
    capture = ArtifactCapture("parse", configured(storage_max_objects=1))
    capture.page(1, "body", [{"name": "figure.png", "png": b"PNG"}])
    assert capture.failed and capture.pages == []


async def test_client_initialization_cannot_block_the_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    store = ArtifactStore(configured(storage_max_inflight=1))

    def slow_client():
        entered.set()
        release.wait(2)
        return MemoryS3()

    monkeypatch.setattr(store, "_make_client", slow_client)
    capture = ArtifactCapture("ocr", store.settings)
    result = OcrResponse(result="text", task="ocr", processing_time_ms=1)
    first = asyncio.create_task(store.save(capture, result))
    try:
        await wait_event(entered)
        started = time.monotonic()
        assert (await store.save(capture, result)).status == "failed"
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        await first
        await store.close()


def test_source_format_matches_accepted_pdf_prefixes():
    from ocrserver.artifacts import source_format

    assert source_format(b" \n\xef\xbb\xbf\t%PDF-1.7") == (
        "application/pdf",
        "pdf",
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"\x00\x00\x00\x0cjP  \r\n\x87\n", ("image/jp2", "jp2")),
        (b"\x00\x00\x00\x1cftypavif", ("image/avif", "avif")),
        (b"\x00\x00\x00\x1cftypavis", ("image/avif", "avif")),
    ],
)
def test_source_format_names_jpeg2000_and_avif_inputs(header, expected):
    from ocrserver.artifacts import source_format

    assert source_format(header + b"\x00" * 16) == expected


def test_capture_drops_only_the_input_when_page_assets_need_the_budget():
    capture = ArtifactCapture(
        "parse", configured(storage_save_input=True, storage_max_bytes=1000)
    )
    capture.source(b"%PDF-1.7" + b"x" * 600, "application/pdf", "pdf")
    assert capture.input_bytes is not None
    capture.page(1, "text", [{"name": "fig.png", "png": b"p" * 500}])
    assert not capture.failed and capture.input_bytes is None
    assert capture.input_dropped and len(capture.pages[0].images) == 1
    # With no input left to release, an overflow still disables archival.
    capture.page(2, "text", [{"name": "big.png", "png": b"p" * 1000}])
    assert capture.failed


def test_capture_skips_an_input_larger_than_the_archive_budget():
    capture = ArtifactCapture(
        "ocr", configured(storage_save_input=True, storage_max_bytes=100)
    )
    capture.source(b"\x89PNG\r\n\x1a\n" + b"x" * 200, "image/png", "png")
    assert not capture.failed and capture.input_bytes is None
    assert capture.input_dropped and capture.source_info["size"] == 208


async def test_archive_keeps_outputs_and_warns_when_the_input_does_not_fit():
    settings, client = (
        configured(storage_save_input=True, storage_max_bytes=4096),
        MemoryS3(),
    )
    store = ArtifactStore(settings, client=client)
    capture = ArtifactCapture("ocr", settings)
    # The input fits the capture budget alone, but not beside the outputs.
    capture.source(b"\x89PNG\r\n\x1a\n" + b"x" * 2500, "image/png", "png")
    result = OcrResponse(result="y" * 2000, task="ocr", processing_time_ms=1)
    try:
        outcome = await store.save(capture, result)
    finally:
        await store.close()
    assert outcome.status == "stored"
    names = {item["Key"].rsplit("/", 1)[-1] for item in client.objects}
    assert "input.md" in names and "input.png" not in names
    manifest = json.loads(
        [i for i in client.objects if i["Key"].endswith("/manifest.json")][-1]["Body"]
    )
    assert manifest["archive_warnings"] == [{"code": "input_not_saved"}]
    assert manifest["source"]["size"] == 2508
    assert all(a["name"] != "input.png" for a in manifest["artifacts"])


def test_process_exit_is_bounded_even_if_a_network_worker_never_exits():
    script = """
import asyncio
import threading
from ocrserver.config import Settings
from ocrserver.schemas import OcrResponse
from ocrserver.storage import ArtifactCapture, ArtifactStore

class HungClient:
    def put_object(self, **kwargs):
        threading.Event().wait()

    def close(self):
        pass

async def run():
    settings = Settings(storage_enabled=True, s3_access_key='test',
                        s3_secret_key='test', storage_timeout_seconds=0.05)
    store = ArtifactStore(settings, client=HungClient())
    outcome = await store.save(ArtifactCapture('ocr', settings),
                              OcrResponse(result='x', task='ocr',
                                          processing_time_ms=1))
    assert outcome.status == 'failed'
    await store.close()

asyncio.run(run())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("location", ["value", "nested", "key"])
def test_oversized_metadata_string_is_rejected_without_large_encoded_copies(
    location,
):
    from ocrserver.artifacts import ArchiveLimit, build_archive

    large = "x" * (8 * 1024 * 1024)
    requested = {
        "value": {"source_id": large},
        "nested": {"source_id": {"nested": [large]}},
        "key": {large: "value"},
    }[location]
    capture = ArtifactCapture(
        "ocr",
        configured(storage_max_bytes=1024),
        requested=requested,
    )
    result = OcrResponse(result="text", task="ocr", processing_time_ms=1)
    tracemalloc.start()
    try:
        with pytest.raises(ArchiveLimit):
            build_archive(capture, result, "ocr/2026/09/26/" + "0" * 32)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024


async def test_json_preflight_preserves_utf8_quotes_and_control_characters():
    settings, client = configured(storage_max_bytes=100_000), MemoryS3()
    store = ArtifactStore(settings, client=client)
    content = '\u3042"\\\x00\n' * 2500
    result = OcrResponse(result=content, task="ocr", processing_time_ms=1)
    try:
        outcome = await store.save(ArtifactCapture("ocr", settings), result)
    finally:
        await store.close()
    assert outcome.status == "stored"
    document = next(
        item for item in client.objects if item["Key"].endswith("/input.md")
    )
    assert document["Body"].decode("utf-8") == content


@pytest.mark.parametrize("reachable", [True, False])
async def test_probe_reports_bucket_reachability_before_any_write(reachable):
    class Probe(MemoryS3):
        def head_bucket(self, **kwargs):
            if not reachable:
                raise ClientError({"Error": {"Code": "503"}}, "HeadBucket")

    store = ArtifactStore(configured(), client=Probe())
    assert store.diagnostic()["status"] == "unknown"
    await store.probe()
    diagnostic = store.diagnostic()
    assert diagnostic["status"] == ("ok" if reachable else "error")
    assert diagnostic["last_operation"] == "probe"
    await store.close()
