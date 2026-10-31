"""Authenticated archive browsing through a separate, bounded S3 client."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import hashlib
import json
import re
import threading
import time
import zipfile
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from datetime import date as calendar_date

import anyio
from fastapi import HTTPException, Request, Response

from ocrserver.archive_names import KINDS, parse_prefix, prefix_from_key
from ocrserver.storage_index import (
    INDEX_ENTRIES,
    INDEX_READY,
    index_archive,
    index_key,
    index_start,
    sort_stamp,
    touch_index,
)
from ocrserver.schemas import (
    StorageArchiveDetail,
    StorageArchiveList,
    StorageDeleteRequest,
    StorageDeleteResponse,
    StorageRagRefineRequest,
    StorageRagRefineResponse,
    StorageRagSplitRequest,
    StorageRagSplitResponse,
)

IO_TIMEOUT_SECONDS = 10.0
DOWNLOAD_TIMEOUT_SECONDS = 120.0
SPLIT_TIMEOUT_SECONDS = 30.0
# Listing, detail and preview workers; full-size downloads hold a separate,
# smaller lease because each buffers up to storage_max_bytes.
MAX_INFLIGHT = 6
MAX_DOWNLOADS = 2
SCAN_KEYS = 1000
MAX_LISTED_KEYS = 50_000
MAX_MANIFEST_READS = 100
INCOMPLETE_AFTER = timedelta(minutes=10)
# SeaweedFS keeps listing a deleted archive's folder as an empty prefix.
_GONE = object()
TEXT_PREVIEW_BYTES = 1024 * 1024
PNG_PREVIEW_BYTES = 10 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._/-]*")
_INPUT = re.compile(r"input\.[a-z0-9]+")
_DOWNLOAD_RELEASES = "ocr.storage_download_releases"


def _error(status, code, **details):
    return HTTPException(status, detail={"code": code, **details})


def _prefix(value):
    match = parse_prefix(value)
    if match:
        return match
    raise _error(422, "storage_invalid_prefix")


def _key(value):
    archive = prefix_from_key(value)
    if archive and len(value) <= 1024:
        name = value[len(archive) :]
        if _NAME.fullmatch(name) and all(
            part not in ("", ".", "..") for part in name.split("/")
        ):
            return archive, name
    raise _error(422, "storage_invalid_key")


async def _confirmation(request):
    """Read a deletion confirmation body: a UUID or file name, never an upload."""
    body = bytearray()
    try:
        with anyio.fail_after(IO_TIMEOUT_SECONDS):
            async for part in request.stream():
                if len(body) + len(part) > 1024:
                    raise _error(422, "storage_invalid_confirmation")
                body.extend(part)
    except TimeoutError as error:
        raise _error(504, "storage_confirmation_timeout") from error
    try:
        return StorageDeleteRequest.model_validate_json(body)
    except (ValueError, UnicodeError) as error:
        raise _error(422, "storage_invalid_confirmation") from error


def _content_type(name):
    extension = name.rsplit(".", 1)[-1].lower()
    return {
        "json": "application/json",
        "md": "text/plain; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "png": "image/png",
        "pdf": "application/pdf",
    }.get(extension, "application/octet-stream")


async def _rag_request(request, model):
    body = bytearray()
    try:
        with anyio.fail_after(IO_TIMEOUT_SECONDS):
            async for part in request.stream():
                if len(body) + len(part) > 2 * 1024 * 1024:
                    raise _error(413, "rag_plan_too_large")
                body.extend(part)
    except TimeoutError as error:
        raise _error(504, "rag_request_timeout") from error
    try:
        return model.model_validate_json(body)
    except (ValueError, UnicodeError) as error:
        raise _error(422, "rag_invalid_plan") from error


class StorageDownloadMiddleware:
    """Release buffered downloads only after the outer ASGI response completes.

    Register outside BaseHTTPMiddleware, whose internal response send can finish
    while its downstream send is still blocked on a slow client.
    """

    def __init__(self, app):
        """Wrap the downstream ASGI app to retain download leases until send ends."""
        self.app = app

    async def __call__(self, scope, receive, send):
        """Release storage download admission after the complete HTTP response."""
        if scope["type"] != "http" or scope["path"] not in (
            "/storage/file",
            "/storage/archive.zip",
        ):
            await self.app(scope, receive, send)
            return
        releases = scope[_DOWNLOAD_RELEASES] = []
        try:
            await self.app(scope, receive, send)
        finally:
            for release in releases:
                release()


class _DownloadResponse(Response):
    """Own the lease when used directly, outside the application's middleware."""

    def __init__(self, *args, release, managed, **kwargs):
        super().__init__(*args, **kwargs)
        self._release = release
        self._managed = managed

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.body = b""
            if not self._managed:
                self._release()


class StorageBrowser:
    """Queue-free workers retain admission until the blocking call really exits.

    SDK setup and every body read happen in daemon workers. An HTTP timeout or
    disconnect signals the worker to stop before its next read/write; it cannot
    free admission while the SDK is still using its connection and memory.
    """

    def __init__(self, settings, *, client=None, writer_factory=None):
        """Configure browser admission and lazy reader/writer client creation."""
        self.settings = settings
        self._client = client
        self._lock = threading.RLock()
        self._client_lock = threading.Lock()
        self._active = {}
        self._downloads = set()
        self._closed = False
        self._mutating = set()
        self._rag_counter_instance = None
        self._rag_counter_lock = threading.Lock()
        self._writer_factory = writer_factory or (
            lambda: self._make_client(writer=True)
        )

    def _download_lease(self):
        if not self.settings.storage_browser_enabled:
            raise _error(503, "storage_browser_disabled")
        token = object()
        with self._lock:
            if self._closed or len(self._downloads) >= MAX_DOWNLOADS:
                raise _error(503, "storage_browser_busy")
            self._downloads.add(token)

        def release():
            with self._lock:
                self._downloads.discard(token)

        return release

    def _make_client(self, *, writer=False):
        import boto3
        from botocore.config import Config

        return boto3.session.Session().client(
            "s3",
            endpoint_url=self.settings.s3_endpoint,
            aws_access_key_id=(
                self.settings.s3_access_key
                if writer
                else self.settings.s3_browser_access_key
            ),
            aws_secret_access_key=(
                self.settings.s3_secret_key
                if writer
                else self.settings.s3_browser_secret_key
            ),
            aws_session_token="",
            region_name=self.settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": self.settings.s3_addressing_style},
                connect_timeout=2,
                read_timeout=2,
                retries={"total_max_attempts": 1, "mode": "standard"},
                max_pool_connections=MAX_INFLIGHT,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    @staticmethod
    def _close_client(client):
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110 - SDK errors may contain secrets
            pass

    def _finished(self, future):
        """Free a worker slot; the last one out closes a shut-down client."""
        client = None
        with self._lock:
            self._active.pop(future, None)
            if self._closed and not self._active:
                client, self._client = self._client, None
        if client is not None:
            self._close_client(client)

    @staticmethod
    def _observe(future):
        if not future.cancelled():
            future.exception()

    async def _run(self, operation, *, timeout=None):
        """Execute bounded blocking storage work without releasing admission early.

        Construct the client in a daemon worker, propagate explicit HTTP errors
        and map other failures to storage_unavailable. Cancellation and timeout
        signal the worker to stop; its slot lasts until the blocking call exits.
        """
        if not self.settings.storage_browser_enabled:
            raise _error(503, "storage_browser_disabled")
        stop = threading.Event()
        deadline = time.monotonic() + (
            IO_TIMEOUT_SECONDS if timeout is None else timeout
        )

        def check():
            if stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError

        with self._lock:
            if self._closed or len(self._active) >= MAX_INFLIGHT:
                raise _error(503, "storage_browser_busy")
            future = Future()
            self._active[future] = stop

            def run():
                future.set_running_or_notify_cancel()
                outcome = error = None
                try:
                    check()
                    with self._client_lock:
                        check()
                        if self._client is None:
                            self._client = self._make_client()
                        client = self._client
                    check()
                    outcome = operation(client, check)
                    check()
                except BaseException as caught:  # noqa: BLE001 - forward outcome
                    error = caught
                # The blocking call has exited: free its slot before the waiting
                # request can resume, so a follow-up request is never refused.
                self._finished(future)
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(outcome)

            try:
                threading.Thread(
                    target=run, name="ocr-storage-browser", daemon=True
                ).start()
            except Exception as error:  # noqa: BLE001 - release failed launch
                self._finished(future)
                future.set_exception(error)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(self._observe)
        try:
            return await asyncio.wait_for(
                asyncio.shield(wrapped), max(0, deadline - time.monotonic())
            )
        except asyncio.CancelledError:
            stop.set()
            raise
        except HTTPException:
            raise
        except Exception as error:
            stop.set()
            raise _error(503, "storage_unavailable") from error

    async def close(self):
        """Stop admission and signal active workers, with a bounded cleanup wait."""
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
            await asyncio.wait(observers, timeout=min(2.0, IO_TIMEOUT_SECONDS))
        if client is not None:
            self._close_client(client)

    def _read(self, client, key, maximum, check, *, metadata=None):
        check()
        try:
            result = client.get_object(Bucket=self.settings.s3_bucket, Key=key)
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "NotFound", "404"):
                raise _error(404, "storage_not_found") from error
            raise
        body = result["Body"]
        try:
            if metadata is not None:
                metadata["etag"] = result.get("ETag")
            check()
            if result.get("ContentLength", 0) > maximum:
                raise _error(413, "storage_file_too_large")
            buffer = bytearray()
            while True:
                check()
                block = body.read(min(65536, maximum - len(buffer) + 1))
                check()
                if not block:
                    return bytes(buffer)
                buffer.extend(block)
                if len(buffer) > maximum:
                    raise _error(413, "storage_file_too_large")
        finally:
            body.close()

    def _manifest(self, client, prefix, check, *, metadata=None):
        """Read a bounded complete manifest and validate its archive/object identity.

        Verify the job, operation, artifact sizes and confined keys before callers
        trust the inventory. Optional metadata receives the underlying read's
        object metadata for guarded manifest updates.
        """
        body = self._read(
            client,
            prefix + "manifest.json",
            TEXT_PREVIEW_BYTES,
            check,
            metadata=metadata,
        )
        try:
            manifest = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise _error(503, "storage_invalid_manifest") from error
        match = _prefix(prefix)
        if (
            not isinstance(manifest, dict)
            or manifest.get("complete") is not True
            or manifest.get("job_id") != match["job"]
            or manifest.get("kind") not in KINDS
            or (match["kind"] is not None and manifest["kind"] != match["kind"])
            or not isinstance(manifest.get("artifacts"), list)
            or not isinstance(manifest.get("created_at", ""), (str, type(None)))
        ):
            raise _error(503, "storage_invalid_manifest")
        for artifact in manifest["artifacts"]:
            if (
                not isinstance(artifact, dict)
                or type(artifact.get("size")) is not int
                or artifact["size"] < 0
                or not isinstance(artifact.get("name"), str)
                or artifact.get("key") != prefix + artifact["name"]
            ):
                raise _error(503, "storage_invalid_manifest")
            try:
                _key(artifact["key"])
            except HTTPException as error:
                raise _error(503, "storage_invalid_manifest") from error
        return manifest, len(body)

    def _list(self, client, prefix, after, maximum, check):
        check()
        result = client.list_objects_v2(
            Bucket=self.settings.s3_bucket,
            Prefix=prefix,
            MaxKeys=maximum,
            **({"StartAfter": after} if after else {}),
        )
        check()
        records = result.get("Contents", [])
        # Defend pagination and deletion boundaries even against malformed S3
        # responses: every returned object must advance within the asked prefix.
        previous = after
        for record in records:
            key = record["Key"]
            if not key.startswith(prefix) or key <= previous:
                raise _error(503, "storage_invalid_listing")
            previous = key
        if len(records) > maximum or (result.get("IsTruncated") and not records):
            raise _error(503, "storage_invalid_listing")
        return records, bool(result.get("IsTruncated"))

    def _root_entries(self, client, after, maximum, check):
        """Page one entry per root folder, without scanning every object."""
        check()
        result = client.list_objects_v2(
            Bucket=self.settings.s3_bucket,
            Prefix="",
            Delimiter="/",
            MaxKeys=maximum,
            **({"StartAfter": after} if after else {}),
        )
        check()
        entries = [item["Prefix"] for item in result.get("CommonPrefixes", [])]
        entries.extend(item["Key"] for item in result.get("Contents", []))
        entries.sort()
        if (
            len(entries) > maximum
            or any(key <= after for key in entries)
            or len(set(entries)) != len(entries)
            or (result.get("IsTruncated") and not entries)
        ):
            raise _error(503, "storage_invalid_listing")
        return entries, bool(result.get("IsTruncated"))

    @staticmethod
    def _sort_key(prefix, match):
        """Return the archive's timestamp for newest-first ordering.

        Current layouts carry a microsecond UTC stamp in the name. Legacy
        layouts carry only a day, which sorts as that day's midnight.
        """
        return sort_stamp(prefix)

    @staticmethod
    def _stale(sort_key):
        """An archive without a complete manifest may still be being written."""
        written = datetime.strptime(sort_key, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        return datetime.now(UTC) - written >= INCOMPLETE_AFTER

    def _candidates(self, client, roots, lower_bound, date_from, date_to, check):
        """Every archive prefix in range, from listings only (no manifest reads)."""
        found, listed = [], 0

        def keep(prefix):
            match = parse_prefix(prefix)
            if match is None or not (
                (not date_from or match["date"] >= date_from)
                and (not date_to or match["date"] <= date_to)
            ):
                return
            found.append((self._sort_key(prefix, match), prefix, match))

        for index, root in enumerate(roots):
            start = "" if index == 0 else root + lower_bound
            while True:
                if index == 0:
                    keys, truncated = self._root_entries(
                        client, start, SCAN_KEYS, check
                    )
                else:
                    records, truncated = self._list(
                        client, root, start, SCAN_KEYS, check
                    )
                    keys = [record["Key"] for record in records]
                listed += len(keys)
                if listed > MAX_LISTED_KEYS:
                    raise _error(413, "storage_listing_too_large")
                exhausted = False
                for key in keys:
                    if index == 0:
                        if key.endswith("/") and "__" in key:
                            keep(key)
                    elif key.endswith("/manifest.json"):
                        prefix = key.removesuffix("manifest.json")
                        match = parse_prefix(prefix)
                        if match and date_to and match["date"] > date_to:
                            exhausted = True  # legacy keys are chronological
                            break
                        keep(prefix)
                if exhausted or not truncated or not keys:
                    break
                start = keys[-1]
        return found

    def _indexed_candidates(self, client, date_from, date_to, after, check):
        start = index_start(date_to)
        if after is not None:
            stamp, prefix = after
            try:
                if sort_stamp(prefix) != stamp:
                    raise ValueError
                start = index_key(prefix)
            except ValueError as error:
                raise _error(422, "storage_invalid_cursor") from error
        while True:
            records, truncated = self._list(
                client, INDEX_ENTRIES, start, SCAN_KEYS, check
            )
            for record in records:
                try:
                    prefix = index_archive(record["Key"])
                except (ValueError, UnicodeError) as error:
                    raise _error(503, "storage_invalid_listing") from error
                match = _prefix(prefix)
                if date_from and match["date"] < date_from:
                    return
                if not date_to or match["date"] <= date_to:
                    yield sort_stamp(prefix), prefix, match
            if not truncated:
                return
            start = records[-1]["Key"]

    def _summary(self, client, prefix, match, sort_key, check):
        """Build a listing row or identify an active or empty archive prefix.

        Return an incomplete row for abandoned archives, None for an archive
        that may still be in progress, or _GONE for an empty prefix.
        """
        try:
            manifest, manifest_size = self._manifest(client, prefix, check)
        except HTTPException as error:
            if (
                error.status_code not in (404, 413)
                and error.detail.get("code") != "storage_invalid_manifest"
            ):
                raise
            if (
                error.status_code == 404
                and not self._list(client, prefix, "", 1, check)[0]
            ):
                return _GONE
            if not self._stale(sort_key):
                return None
            written = datetime.strptime(sort_key, "%Y%m%dT%H%M%S%fZ")
            return {
                "prefix": prefix,
                "job_id": match["job"],
                "filename": match["filename"],
                "kind": match["kind"] or "unknown",
                "created_at": written.replace(tzinfo=UTC).isoformat()
                if match["kind"] is None
                else None,
                "object_count": 0,
                "total_bytes": 0,
                "partial_ocr": False,
                "unprocessed_pages": [],
                "incomplete": True,
            }
        artifacts = manifest["artifacts"]
        source = manifest.get("source")
        original = source.get("original_name") if isinstance(source, dict) else None
        unprocessed = manifest.get("unprocessed_pages")
        if not isinstance(unprocessed, list) or not all(
            type(n) is int for n in unprocessed
        ):
            unprocessed = []
        return {
            "prefix": prefix,
            "job_id": manifest["job_id"],
            # The key keeps an ASCII-safe name; show what the user uploaded.
            "filename": original
            if isinstance(original, str) and original
            else match["filename"],
            "kind": manifest["kind"],
            "created_at": manifest.get("created_at"),
            "object_count": len(artifacts) + 1,
            "total_bytes": sum(a["size"] for a in artifacts) + manifest_size,
            "partial_ocr": bool(manifest.get("partial_ocr")),
            "unprocessed_pages": unprocessed,
            "incomplete": False,
            "input_key": next(
                (a["key"] for a in artifacts if _INPUT.fullmatch(a["name"])), None
            ),
        }

    async def archives(
        self,
        *,
        kind=None,
        date=None,
        date_from=None,
        date_to=None,
        cursor=None,
        limit=20,
    ):
        """List filtered archives in newest-first order using validated cursors."""
        if kind is not None and kind not in KINDS:
            raise _error(422, "storage_invalid_kind")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise _error(422, "storage_invalid_limit")
        for bound in (date, date_from, date_to):
            if bound:
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bound):
                        raise ValueError
                    calendar_date.fromisoformat(bound)
                except ValueError as error:
                    raise _error(422, "storage_invalid_date") from error
        if date:
            if date_from or date_to:
                raise _error(422, "storage_invalid_date")
            date_from = date_to = date
        if date_from and date_to and date_from > date_to:
            raise _error(422, "storage_invalid_date")
        single_day = date_from is not None and date_from == date_to
        roots = [""] + [
            f"{root}/" + (date_from.replace("-", "/") + "/" if single_day else "")
            for root in ((kind,) if kind else KINDS)
        ]
        # Legacy kind roots are chronological (YYYY/MM/DD), so a range bounds
        # their scan; current root entries are filtered on their stamp.
        lower_bound = (
            date_from.replace("-", "/") + "/" if date_from and not single_day else ""
        )
        filters = [kind, date_from, date_to]
        after = None
        if cursor:
            try:
                if len(cursor) > 4096:
                    raise ValueError
                saved, stamp, prefix = json.loads(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                if (
                    saved != filters
                    or not isinstance(stamp, str)
                    or not isinstance(prefix, str)
                    or len(prefix) > 1024
                ):
                    raise ValueError
                after = (stamp, prefix)
            except (ValueError, TypeError, binascii.Error, RecursionError) as error:
                raise _error(422, "storage_invalid_cursor") from error

        def encode(position):
            payload = json.dumps([filters, *position], separators=(",", ":"))
            return base64.urlsafe_b64encode(payload.encode()).decode()

        def scan(client, check):
            """Page verified summaries with a bounded number of manifest reads.

            Prefer the temporal index when ready; otherwise scan legacy roots.
            Preserve a continuation cursor even when corrupt/in-progress entries
            exhaust the read budget before filling the requested page.
            """
            ready, _ = self._list(client, INDEX_READY, "", 1, check)
            if any(item["Key"] == INDEX_READY for item in ready):
                pool = self._indexed_candidates(
                    client, date_from, date_to, after, check
                )
            else:
                # Old deployments stay readable until storage-init finishes backfill.
                pool = sorted(
                    self._candidates(
                        client, roots, lower_bound, date_from, date_to, check
                    ),
                    key=lambda entry: entry[:2],
                    reverse=True,
                )
                if after is not None:
                    pool = [entry for entry in pool if entry[:2] < after]
            items, skipped, reads = [], 0, 0
            last_item = last_seen = None

            def page(position):
                return {
                    "items": items,
                    "skipped_archives": skipped,
                    "next_cursor": encode(position) if position else None,
                }

            for sort_key, prefix, match in pool:
                full = len(items) >= limit
                # A known kind proves another page exists without a manifest read.
                if full and (kind is None or match["kind"] == kind):
                    return page(last_item)
                if reads >= MAX_MANIFEST_READS:
                    return page(last_item if full else last_seen)
                reads += 1
                item = self._summary(client, prefix, match, sort_key, check)
                last_seen = (sort_key, prefix)
                if item is _GONE:
                    continue
                if item is None:
                    skipped += 1
                    continue
                if kind and item["kind"] != kind:
                    continue
                if full:
                    return page(last_item)
                items.append(item)
                last_item = (sort_key, prefix)
            return page(None)

        return await self._run(scan)

    def _objects(self, client, prefix, check):
        objects, after = [], ""
        maximum = self.settings.storage_max_objects
        while True:
            records, truncated = self._list(
                client, prefix, after, min(SCAN_KEYS, maximum - len(objects) + 1), check
            )
            for record in records:
                _key(record["Key"])
            objects.extend(records)
            if len(objects) > maximum:
                raise _error(413, "storage_archive_too_large")
            if not truncated:
                return objects
            after = records[-1]["Key"]

    async def archive(self, prefix):
        """Return the validated manifest and current file inventory for a prefix."""
        _prefix(prefix)

        def detail(client, check):
            manifest, _ = self._manifest(client, prefix, check)
            files = [
                {
                    "key": item["Key"],
                    "name": item["Key"][len(prefix) :],
                    "size": item["Size"],
                    "content_type": _content_type(item["Key"]),
                }
                for item in self._objects(client, prefix, check)
            ]
            return {"prefix": prefix, "manifest": manifest, "files": files}

        return await self._run(detail)

    async def archive_zip(self, prefix, *, request_scope=None):
        """Build a bounded complete ZIP while retaining download memory admission."""
        _prefix(prefix)
        release = self._download_lease()
        managed = request_scope is not None and _DOWNLOAD_RELEASES in request_scope
        try:
            if managed:
                request_scope[_DOWNLOAD_RELEASES].append(release)

            def bundle(client, check):
                """Build a bounded ZIP only from a complete current archive listing.

                Reject missing manifest artifacts, invalid sizes and unsafe names.
                Recheck each object's actual size through bounded reads so listing
                metadata alone cannot bypass the aggregate byte limit.
                """
                manifest, _ = self._manifest(client, prefix, check)
                objects = self._objects(client, prefix, check)
                present = {item["Key"] for item in objects}
                if any(item["key"] not in present for item in manifest["artifacts"]):
                    raise _error(409, "storage_archive_incomplete")
                maximum = self.settings.storage_max_bytes
                if not any(item["Key"] == prefix + "manifest.json" for item in objects):
                    raise _error(404, "storage_not_found")
                sizes = [item.get("Size") for item in objects]
                if any(type(size) is not int or size < 0 for size in sizes):
                    raise _error(503, "storage_invalid_listing")
                if sum(sizes) > maximum:
                    raise _error(413, "storage_archive_too_large")
                output = io.BytesIO()
                with zipfile.ZipFile(
                    output, mode="w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    total = 0
                    for item in objects:
                        check()
                        key = item["Key"]
                        name = key[len(prefix) :]
                        if not name or name.startswith("/"):
                            raise _error(503, "storage_invalid_listing")
                        try:
                            data = self._read(client, key, maximum - total, check)
                        except HTTPException as error:
                            if error.status_code == 404:
                                raise _error(
                                    409, "storage_archive_incomplete"
                                ) from error
                            raise
                        total += len(data)
                        archive.writestr(name, data)
                return output.getvalue()

            data = await self._run(bundle, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            filename = prefix.rstrip("/").rsplit("/", 1)[-1] + ".zip"
            return _DownloadResponse(
                data,
                release=release,
                managed=managed,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        except BaseException:
            release()
            raise

    async def file(self, key, *, preview=False, request_scope=None):
        """Download one validated artifact or its size- and type-limited preview."""
        archive, name = _key(key)
        content_type = _content_type(name)
        maximum = self.settings.storage_max_bytes
        if preview:
            if content_type == "image/png":
                maximum = min(maximum, PNG_PREVIEW_BYTES)
            elif (
                content_type.startswith("text/plain")
                or content_type == "application/json"
            ):
                maximum = min(maximum, TEXT_PREVIEW_BYTES)
            else:
                raise _error(415, "storage_preview_unsupported")
        # Previews are bounded to a few MB: only full downloads need a lease.
        release = (lambda: None) if preview else self._download_lease()
        managed = request_scope is not None and _DOWNLOAD_RELEASES in request_scope
        try:
            if managed:
                request_scope[_DOWNLOAD_RELEASES].append(release)

            def fetch(client, check):
                # Only files of complete archives are served, as they are listed.
                self._manifest(client, archive, check)
                data = self._read(client, key, maximum, check)
                return data, hashlib.sha256(data).hexdigest()

            data = await self._run(
                fetch, timeout=None if preview else DOWNLOAD_TIMEOUT_SECONDS
            )
            payload, digest = data
            return _DownloadResponse(
                payload,
                release=release,
                managed=managed,
                media_type=content_type,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{name.rsplit("/", 1)[-1]}"'
                    ),
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                    "X-OCR-Content-SHA256": digest,
                    "Access-Control-Expose-Headers": "X-OCR-Content-SHA256",
                },
            )
        except BaseException:
            release()
            raise

    async def _run_mutation(self, prefix, operation):
        def mutate(client, check):
            with self._lock:
                if prefix in self._mutating:
                    raise _error(409, "storage_archive_busy")
                self._mutating.add(prefix)
            try:
                return operation(client, check)
            finally:
                with self._lock:
                    self._mutating.discard(prefix)

        return await self._run(mutate, timeout=DOWNLOAD_TIMEOUT_SECONDS)

    def _rag_counter(self):
        """Build and reuse the real tokenizer, mapping load failures to 503."""
        with self._rag_counter_lock:
            if self._rag_counter_instance is None:
                from ocrserver.chunking.errors import ChunkError
                from ocrserver.chunking.tokenizer import PaddleTokenizer

                try:
                    tokenizer = PaddleTokenizer(
                        self.settings.hf_cache_dir,
                        self.settings.vllm_model_revision,
                    )
                    tokenizer.identity  # force the cache load here
                except ChunkError as error:
                    raise _error(503, "rag_tokenizer_unavailable") from error
                self._rag_counter_instance = tokenizer
        return self._rag_counter_instance

    async def split_rag(self, value):
        """Preview exact-token splits of a verified stored source without writing."""
        from ocrserver.rag_refinement import _NOTE_DETAIL, _note_value
        from ocrserver.rag_split import (
            SplitBudget,
            SplitFragment,
            render_entries,
            split_group,
        )
        from ocrserver.schemas import (
            RAG_SPLIT_NOTE_PARAMETER,
            RAG_SPLIT_NOTE_SEVERITY,
            RagParameter,
            RagSplitFragmentModel,
            RagSplitNote,
            StorageRagSplitResponse,
        )

        prefix, name = _key(value.source_key)
        if not re.search(r"(^|[/.])json_rag\.json$", name, re.IGNORECASE):
            raise _error(422, "rag_invalid_source")

        def fetch(client, check):
            """Verify source identity and measure read-only split proposals.

            Require a complete archive and matching source hash, then attach the
            same per-code diagnostic parameters used by refinement saves. Keep
            unavailable counts explicit instead of substituting token estimates.
            """
            counter = self._rag_counter()
            budget = SplitBudget(
                max_tokens=value.options.max_tokens,
                min_tokens=value.options.min_tokens,
                max_evaluations=self.settings.chunk_max_split_evaluations,
                timeout_seconds=SPLIT_TIMEOUT_SECONDS,
            )
            self._manifest(client, prefix, check)
            raw = self._read(
                client,
                value.source_key,
                self.settings.storage_max_bytes,
                check,
            )
            if hashlib.sha256(raw).hexdigest() != value.source_sha256:
                raise _error(409, "rag_source_changed")
            try:
                source = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise _error(422, "rag_invalid_source") from error
            chunks = source.get("chunks") if isinstance(source, dict) else None
            if not isinstance(chunks, list):
                raise _error(422, "rag_invalid_source")
            fragments = split_group(
                chunks,
                value.group,
                value.options,
                counter,
                budget,
            )
            if not fragments:
                fragments = [
                    SplitFragment(
                        list(value.group),
                        None,
                        "unknown",
                        ["token_count_unknown"],
                    )
                ]

            metadata = source.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}

            def note(code, fragment):
                # FR-017: the same per-code values the save path computes, so
                # the split response's notes carry a real detail and parameter.
                entries = fragment.entries
                sources, seen = [], set()
                for entry in entries:
                    index = entry if isinstance(entry, int) else entry[0]
                    if index not in seen:
                        seen.add(index)
                        sources.append(chunks[index])
                text = render_entries(chunks, entries)
                parameters = [
                    RagParameter(name="detail", value=_NOTE_DETAIL.get(code, code))
                ]
                extra = RAG_SPLIT_NOTE_PARAMETER.get(code)
                if extra is not None:
                    parameters.append(
                        RagParameter(
                            name=extra,
                            value=_note_value(
                                code,
                                chunks,
                                sources,
                                fragment.token_count,
                                text,
                                metadata,
                            ),
                        )
                    )
                return RagSplitNote(
                    code=code,
                    severity=RAG_SPLIT_NOTE_SEVERITY[code],
                    targets=[],
                    parameters=parameters,
                )

            return StorageRagSplitResponse(
                fragments=[
                    RagSplitFragmentModel(
                        entries=fragment.entries,
                        token_count=fragment.token_count,
                        status=fragment.status,
                        notes=[note(code, fragment) for code in fragment.notes],
                        member_token_counts=fragment.member_token_counts,
                    )
                    for fragment in fragments
                ],
                warnings=[],
            )

        return await self._run(fetch, timeout=SPLIT_TIMEOUT_SECONDS)

    async def refine_rag(self, value):
        """Save a verified refinement plan using separate writer credentials."""
        from ocrserver.rag_refinement import plan_has_fragments, save_refinement

        prefix, name = _key(value.source_key)
        if not re.search(r"(^|[/.])json_rag\.json$", name, re.IGNORECASE):
            raise _error(422, "rag_invalid_source")
        if not self.settings.storage_enabled:
            raise _error(503, "storage_write_disabled")

        # A plan of whole chunks never needs the tokenizer (R13); only a plan
        # with partial or ``repeat`` entries must measure its fragments.
        def save(client, check):
            counter = self._rag_counter() if plan_has_fragments(value.groups) else None
            check()
            return save_refinement(self, client, prefix, value, check, counter)

        return await self._run_mutation(prefix, save)

    async def delete(self, prefix, confirm):
        """Delete an archive only after exact job-ID confirmation and safety checks."""
        match = _prefix(prefix)
        if confirm != match["job"]:
            raise _error(409, "storage_confirmation_required")

        def remove(client, check):
            """Delete bounded batches, retaining the manifest until all data is gone.

            Refuse incomplete archives that may still have an active writer.
            Report confirmed deletions on partial failure so retries remain safe.
            """
            records = self._objects(client, prefix, check)
            manifest_key = prefix + "manifest.json"
            if not records:
                raise _error(404, "storage_not_found")
            try:
                self._manifest(client, prefix, check)
            except HTTPException as error:
                if error.status_code == 503 and error.detail.get("code") != (
                    "storage_invalid_manifest"
                ):
                    raise
                # Without a complete manifest the writer may still be running.
                if not self._stale(self._sort_key(prefix, match)):
                    raise _error(409, "storage_archive_in_progress") from error
            keys = [item["Key"] for item in records if item["Key"] != manifest_key]
            batches = [
                keys[offset : offset + 1000] for offset in range(0, len(keys), 1000)
            ]
            # The manifest goes last: a partial failure stays listable/retryable.
            if any(item["Key"] == manifest_key for item in records):
                batches.append([manifest_key])
            deleted = 0
            for batch in batches:
                check()
                try:
                    result = client.delete_objects(
                        Bucket=self.settings.s3_bucket,
                        Delete={
                            "Objects": [{"Key": key} for key in batch],
                            "Quiet": False,
                        },
                    )
                except Exception as error:
                    raise _error(
                        503, "storage_delete_partial", deleted_objects=deleted
                    ) from error
                completed = {item["Key"] for item in result.get("Deleted", [])} & set(
                    batch
                )
                deleted += len(completed)
                if result.get("Errors") or len(completed) != len(batch):
                    raise _error(503, "storage_delete_partial", deleted_objects=deleted)
                check()
            return {"deleted_objects": deleted}

        return await self._run_mutation(prefix, remove)

    async def delete_file(self, key, confirm):
        """Remove one artifact and its manifest entry; the manifest goes last.

        A failure between the two steps stays retryable: the object delete is
        idempotent and the manifest entry is only dropped once the object is gone.
        """
        prefix, name = _key(key)
        if name == "manifest.json":
            raise _error(422, "storage_invalid_key")
        if confirm != name:
            raise _error(409, "storage_confirmation_required")
        # The manifest update needs the writer; never delete an object it cannot unlist.
        if not self.settings.storage_enabled:
            raise _error(503, "storage_write_disabled")

        def remove(client, check):
            """Delete one artifact and conditionally update its manifest inventory.

            Require an ETag and retry concurrent manifest replacements from fresh
            metadata. Use separate writer credentials for the index/manifest and
            always close that client, including conflict and partial-failure paths.
            """
            manifest_info = {}
            manifest, _ = self._manifest(client, prefix, check, metadata=manifest_info)
            if not any(item.get("key") == key for item in manifest["artifacts"]):
                raise _error(404, "storage_not_found")
            # Refuse an unguarded manifest replacement when the store supplies no ETag.
            if not manifest_info.get("etag"):
                raise _error(503, "storage_unavailable")
            writer = self._writer_factory()
            try:
                touch_index(
                    writer, self.settings.s3_bucket, prefix, check, reader=client
                )
                check()
                try:
                    result = client.delete_objects(
                        Bucket=self.settings.s3_bucket,
                        Delete={"Objects": [{"Key": key}], "Quiet": False},
                    )
                except Exception as error:
                    raise _error(
                        503, "storage_delete_partial", deleted_objects=0
                    ) from error
                if result.get("Errors") or not any(
                    item["Key"] == key for item in result.get("Deleted", [])
                ):
                    raise _error(503, "storage_delete_partial", deleted_objects=0)
                for _ in range(5):
                    check()
                    updated = json.dumps(
                        {
                            **manifest,
                            "artifacts": [
                                item
                                for item in manifest["artifacts"]
                                if item.get("key") != key
                            ],
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                    if len(updated) > TEXT_PREVIEW_BYTES:
                        raise _error(413, "storage_file_too_large")
                    try:
                        writer.put_object(
                            Bucket=self.settings.s3_bucket,
                            Key=prefix + "manifest.json",
                            Body=updated,
                            ContentType="application/json",
                            IfMatch=manifest_info["etag"],
                        )
                        return {"deleted_objects": 1}
                    except Exception as error:
                        if getattr(error, "response", {}).get("Error", {}).get(
                            "Code"
                        ) not in (
                            "PreconditionFailed",
                            "412",
                            "ConditionalRequestConflict",
                            "409",
                        ):
                            raise
                        manifest, _ = self._manifest(
                            client, prefix, check, metadata=manifest_info
                        )
                raise _error(409, "storage_archive_busy")
            finally:
                self._close_client(writer)

        return await self._run_mutation(prefix, remove)


def register_storage_routes(app, browser):
    """Routes inherit the application's existing bearer/rate-limit middleware."""
    refine_schema = StorageRagRefineRequest.model_json_schema()
    option_schema = refine_schema.get("$defs", {}).get("RagRefinementOptions", {})
    refine_schema.pop("$defs", None)
    refine_schema["properties"]["options"] = option_schema

    @app.post(
        "/storage/rag/refine",
        tags=["RAG"],
        response_model=StorageRagRefineResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": refine_schema},
                },
            }
        },
    )
    async def refine_rag(request: Request):
        value = await _rag_request(request, StorageRagRefineRequest)
        return await browser.refine_rag(value)

    @app.post(
        "/storage/rag/split",
        tags=["RAG"],
        response_model=StorageRagSplitResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": StorageRagSplitRequest.model_json_schema(),
                    },
                },
            }
        },
    )
    async def split_rag(request: Request):
        value = await _rag_request(request, StorageRagSplitRequest)
        return await browser.split_rag(value)

    @app.get("/storage/archives", tags=["STORAGE"], response_model=StorageArchiveList)
    async def archives(
        kind: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        cursor: str | None = None,
        limit: str = "20",
    ):
        try:
            count = int(limit)
        except ValueError as error:
            raise _error(422, "storage_invalid_limit") from error
        return await browser.archives(
            kind=kind,
            date=date,
            date_from=date_from,
            date_to=date_to,
            cursor=cursor,
            limit=count,
        )

    @app.get("/storage/archive", tags=["STORAGE"], response_model=StorageArchiveDetail)
    async def archive(prefix: str = ""):
        return await browser.archive(prefix)

    @app.get("/storage/archive.zip", tags=["STORAGE"])
    async def archive_zip(request: Request, prefix: str = ""):
        return await browser.archive_zip(prefix, request_scope=request.scope)

    @app.get("/storage/file", tags=["STORAGE"])
    async def file(request: Request, key: str = "", preview: str = "false"):
        if preview not in ("true", "false"):
            raise _error(422, "storage_invalid_preview")
        return await browser.file(
            key, preview=preview == "true", request_scope=request.scope
        )

    @app.delete(
        "/storage/archive",
        tags=["STORAGE"],
        response_model=StorageDeleteResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": StorageDeleteRequest.model_json_schema()
                    },
                },
            }
        },
    )
    async def delete(request: Request, prefix: str = ""):
        value = await _confirmation(request)
        return await browser.delete(prefix, value.confirm)

    @app.delete(
        "/storage/file",
        tags=["STORAGE"],
        response_model=StorageDeleteResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": StorageDeleteRequest.model_json_schema()
                    },
                },
            }
        },
    )
    async def delete_file(request: Request, key: str = ""):
        value = await _confirmation(request)
        return await browser.delete_file(key, value.confirm)
