"""Best-effort S3 archival with bounded workers and commit-last manifests."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone

from ocrserver.archive_names import archive_prefix, safe_filename
from ocrserver.artifacts import ArtifactCapture, build_archive
from ocrserver.storage_index import touch_index
from ocrserver.metrics import (
    STORAGE_ATTEMPTS,
    STORAGE_BYTES,
    STORAGE_DURATION,
    STORAGE_INFLIGHT,
)

STORAGE_HEADERS = {
    "X-OCR-Storage-Status": {
        "description": (
            "Independent archive outcome; failed leaves successful OCR intact."
        ),
        "schema": {"type": "string", "enum": ["stored", "failed", "disabled"]},
    },
    "X-OCR-Job-Id": {
        "description": ("Eight-character archive code, when storage is enabled."),
        "schema": {"type": "string"},
    },
    "X-OCR-Archive-Key": {
        "description": "Manifest object key, only when archival is confirmed complete.",
        "schema": {"type": "string"},
    },
}


@dataclass(frozen=True)
class StorageOutcome:
    """Report archival status independently from the successful OCR response."""

    status: str
    job_id: str | None = None
    manifest_key: str | None = None

    def headers(self) -> dict[str, str]:
        """Expose outcome headers, including identifiers only when available."""
        headers = {"X-OCR-Storage-Status": self.status}
        if self.job_id:
            headers["X-OCR-Job-Id"] = self.job_id
        if self.manifest_key:
            headers["X-OCR-Archive-Key"] = self.manifest_key
        return headers


class ArtifactStore:
    """Accept no queued jobs; capacity lasts until the actual worker exits.

    Client construction, response copies, hashing, JSON encoding and S3 calls
    all run in dedicated workers, never the event loop/default executor. Daemon
    threads avoid an unbounded interpreter-exit join if a network call is stuck.
    """

    def __init__(self, settings, *, client=None):
        """Initialize bounded archive state without constructing an S3 client."""
        self.settings = settings
        self._client = client
        self._lock = threading.RLock()
        self._client_lock = threading.Lock()
        self._active = {}
        self._closed = False
        self._diagnostic = {
            "status": "unknown" if settings.storage_enabled else "disabled",
            "last_operation": None,
            "timestamp": None,
        }

    def diagnostic(self) -> dict:
        """Return a locked copy of credential-free storage status metadata."""
        with self._lock:
            return dict(self._diagnostic)

    async def probe(self) -> None:
        """Check bucket reachability so an outage shows before a write fails.

        Runs in a daemon worker like writes; a hung call only leaves a thread
        behind, and the diagnostic reports error after the storage timeout.
        """
        if not self.settings.storage_enabled or self._closed:
            return
        done = Future()

        def run():
            try:
                with self._client_lock:
                    if self._client is None:
                        self._client = self._make_client()
                    client = self._client
                client.head_bucket(Bucket=self.settings.s3_bucket)
            except BaseException as error:  # noqa: BLE001 - reported as status only
                done.set_exception(error)
            else:
                done.set_result(None)

        threading.Thread(target=run, name="ocr-storage-probe", daemon=True).start()
        wrapped = asyncio.wrap_future(done)
        wrapped.add_done_callback(self._observe)
        try:
            await asyncio.wait_for(
                asyncio.shield(wrapped), self.settings.storage_timeout_seconds
            )
            status = "ok"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - SDK errors may contain secrets
            status = "error"
        with self._lock:
            if self._active:
                return  # a running write will report a fresher outcome
            self._diagnostic = {
                "status": status,
                "last_operation": "probe",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def _make_client(self):
        import boto3
        from botocore.config import Config

        settings = self.settings
        # Supplying BOTH credentials prevents provider-chain/metadata fallback.
        return boto3.session.Session().client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            aws_session_token="",
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": settings.s3_addressing_style},
                connect_timeout=min(2.0, settings.storage_timeout_seconds),
                read_timeout=min(2.0, settings.storage_timeout_seconds),
                retries={"total_max_attempts": 1, "mode": "standard"},
                max_pool_connections=settings.storage_max_inflight,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _write(
        self, capture, result, filename, when, first_code, stop, deadline, kwargs
    ):
        """Reserve a unique archive and write its complete manifest last.

        Build bounded objects before reservation, retry conditional-name clashes
        and publish the temporal index before any archive write. Check stop and
        deadline around blocking operations so late workers cannot commit success.
        """
        from botocore.exceptions import ClientError

        def check():
            if stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError

        check()
        # This worker-only lock must never be used by save/diagnostic/close:
        # SDK construction can be slow even with explicit credentials.
        with self._client_lock:
            if self._client is None:
                self._client = self._make_client()
            client = self._client
        for attempt in range(8):
            check()
            code = first_code if attempt == 0 else secrets.token_urlsafe(6)
            prefix = archive_prefix(filename, when, code)
            objects = build_archive(capture, result, prefix, **kwargs)
            check()
            existing = client.list_objects_v2(
                Bucket=self.settings.s3_bucket,
                Prefix=prefix + "/",
                MaxKeys=1,
            )
            check()
            if existing.get("Contents"):
                continue
            manifest_key = objects[-1][0]
            # An index failure must occur before any archive is reserved/written.
            touch_index(client, self.settings.s3_bucket, prefix + "/", check)
            try:
                # Reserve atomically. An interrupted write leaves an incomplete
                # marker; the browser only accepts a complete final manifest.
                client.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=manifest_key,
                    Body=b'{"complete":false}',
                    ContentType="application/json",
                    IfNoneMatch="*",
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {
                    "PreconditionFailed",
                    "ConditionalRequestConflict",
                }:
                    continue
                raise
            STORAGE_BYTES.inc(len(b'{"complete":false}'))
            break
        else:
            raise RuntimeError("archive code collision retries exhausted")
        for key, body, content_type in objects:
            check()
            client.put_object(
                Bucket=self.settings.s3_bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
            STORAGE_BYTES.inc(len(body))
            check()
        return manifest_key, code

    def _finished(self, future):
        client = None
        with self._lock:
            self._active.pop(future, None)
            STORAGE_INFLIGHT.dec()
            if self._closed and not self._active:
                client, self._client = self._client, None
        if client is not None:
            self._close_client(client)

    def _launch(
        self, capture, result, filename, when, first_code, stop, deadline, kwargs
    ):
        """Queue-free executor; caller holds the admission lock and capacity."""
        future = Future()
        self._active[future] = stop
        STORAGE_INFLIGHT.inc()
        future.add_done_callback(self._finished)

        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                manifest = self._write(
                    capture,
                    result,
                    filename,
                    when,
                    first_code,
                    stop,
                    deadline,
                    kwargs,
                )
            except BaseException as error:  # noqa: BLE001 - forward worker outcome
                future.set_exception(error)
            else:
                future.set_result(manifest)

        try:
            threading.Thread(target=run, name="ocr-storage", daemon=True).start()
        except Exception as error:  # noqa: BLE001 - release failed admission
            future.set_exception(error)
        return future

    @staticmethod
    def _close_client(client):
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110 - do not leak SDK secrets in logs
            pass  # Cleanup must not log potentially sensitive SDK exceptions.

    @staticmethod
    def _observe(done):
        if not done.cancelled():
            done.exception()

    def _record(self, outcome, start):
        STORAGE_ATTEMPTS.labels(status=outcome.status).inc()
        STORAGE_DURATION.observe(time.monotonic() - start)
        with self._lock:
            self._diagnostic = {
                "status": "ok" if outcome.status == "stored" else "error",
                "last_operation": outcome.status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        return outcome

    async def save(
        self,
        capture: ArtifactCapture | None,
        result,
        *,
        parsed=None,
        result_bytes: bytes | None = None,
    ) -> StorageOutcome:
        """Attempt bounded archival while preserving successful OCR on failure.

        Perform serialization and S3 work in a dedicated worker. Capacity remains
        reserved until the worker exits, even if the caller times out. Propagate
        caller cancellation after signalling the worker to stop.
        """
        if not self.settings.storage_enabled:
            return StorageOutcome("disabled")
        started = time.monotonic()
        job_id = secrets.token_urlsafe(6)
        failed = StorageOutcome("failed", job_id)
        now = datetime.now(timezone.utc)
        filename = safe_filename(
            capture.original_filename,
            capture.source_info.get("extension", "bin"),
        )
        stop = threading.Event()
        with self._lock:
            if self._closed or len(self._active) >= self.settings.storage_max_inflight:
                return self._record(failed, started)
            try:
                future = self._launch(
                    capture,
                    result,
                    filename,
                    now,
                    job_id,
                    stop,
                    started + self.settings.storage_timeout_seconds,
                    {"parsed": parsed, "result_bytes": result_bytes},
                )
            except Exception:  # noqa: BLE001 - isolate executor failures
                return self._record(failed, started)
        wrapped = asyncio.wrap_future(future)
        # Consume a late failure after HTTP timeout without unhandled-future logs.
        wrapped.add_done_callback(self._observe)
        try:
            (manifest_key, job_id) = await asyncio.wait_for(
                asyncio.shield(wrapped),
                max(
                    0,
                    started + self.settings.storage_timeout_seconds - time.monotonic(),
                ),
            )
        except asyncio.CancelledError:
            stop.set()
            self._record(failed, started)
            raise
        except Exception:  # noqa: BLE001 - isolate serialization/network failures
            stop.set()
            return self._record(failed, started)
        if time.monotonic() >= started + self.settings.storage_timeout_seconds:
            return self._record(failed, started)
        return self._record(StorageOutcome("stored", job_id, manifest_key), started)

    async def close(self):
        """Stop new jobs immediately, and bound the wait for active S3 calls."""
        with self._lock:
            self._closed = True
            pending = list(self._active)
            for stop in self._active.values():
                stop.set()
            client = None
            if not pending:
                client, self._client = self._client, None
        if pending:
            observers = [asyncio.wrap_future(future) for future in pending]
            for observer in observers:
                observer.add_done_callback(self._observe)
            await asyncio.wait(
                observers,
                timeout=min(2.0, self.settings.storage_timeout_seconds),
            )
        if client is not None:
            self._close_client(client)
