"""Browser permissions, pagination and bounded S3 operations without a service."""

import asyncio
import base64
import dataclasses
import io
import json
import hashlib
import threading
import zipfile
from urllib.parse import urlencode

import httpx
import pytest
from botocore.exceptions import ClientError
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_storage import wait_event

from ocr_server import create_app
from ocrserver.config import Settings, load_settings
from ocrserver.storage_browser import StorageBrowser
from ocrserver.storage_index import index_key


def configured(**kwargs):
    return Settings(
        storage_browser_enabled=True,
        storage_enabled=True,
        s3_access_key="writer-access",
        s3_secret_key="writer-secret",
        s3_browser_access_key="browser-access",
        s3_browser_secret_key="browser-secret",
        **kwargs,
    )


class MemoryS3:
    """Emulate archive objects, browser calls and configured delete failures."""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.delete_errors = set()
        self.closed = False

    def archive(self, number, kind="parse", day="2026/09/26"):
        prefix = f"{kind}/{day}/{number:032x}/"
        self.objects[prefix + "document.md"] = b"# synthetic text"
        self.objects[prefix + "result.json"] = b'{"result":"text"}'
        manifest = {
            "complete": True,
            "kind": kind,
            "job_id": f"{number:032x}",
            "created_at": "2026-09-26T00:00:00+00:00",
            "partial_ocr": False,
            "artifacts": [
                {"name": key[len(prefix) :], "key": key, "size": len(body)}
                for key, body in self.objects.items()
                if key.startswith(prefix)
            ],
        }
        self.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
        return prefix

    def current_archive(self, filename="report.pdf", code="Abcd123_", source=None):
        prefix = f"{filename}__20260926T120000000000Z__{code}/"
        document_key = prefix + "document.md"
        self.objects[document_key] = b"# current layout"
        artifacts = [
            {
                "name": "document.md",
                "key": document_key,
                "size": len(self.objects[document_key]),
            }
        ]
        if source is not None:
            self.objects[prefix + "input.pdf"] = source
            artifacts.append(
                {"name": "input.pdf", "key": prefix + "input.pdf", "size": len(source)}
            )
        manifest = {
            "complete": True,
            "kind": "parse",
            "job_id": code,
            "created_at": "2026-09-26T12:00:00+00:00",
            "partial_ocr": False,
            "source": {"original_name": filename},
            "artifacts": artifacts,
        }
        self.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
        return prefix

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list", kwargs))
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        if kwargs.get("Delimiter"):
            entries = sorted(
                {key.split("/", 1)[0] + "/" if "/" in key else key for key in keys}
            )
            entries = [key for key in entries if key > kwargs.get("StartAfter", "")]
            page = entries[: kwargs["MaxKeys"]]
            return {
                "CommonPrefixes": [
                    {"Prefix": key} for key in page if key.endswith("/")
                ],
                "Contents": [
                    {"Key": key, "Size": len(self.objects[key])}
                    for key in page
                    if not key.endswith("/")
                ],
                "IsTruncated": len(entries) > kwargs["MaxKeys"],
            }
        keys = [key for key in keys if key > kwargs.get("StartAfter", "")]
        return {
            "Contents": [
                {"Key": key, "Size": len(self.objects[key])}
                for key in keys[: kwargs["MaxKeys"]]
            ],
            "IsTruncated": len(keys) > kwargs["MaxKeys"],
        }

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[kwargs["Key"]]
        return {
            "Body": io.BytesIO(body),
            "ContentLength": len(body),
            "ETag": '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"',
        }

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        key, body = kwargs["Key"], kwargs["Body"]
        old = self.objects.get(key)
        etag = (
            '"' + hashlib.md5(old, usedforsecurity=False).hexdigest() + '"'
            if old is not None
            else None
        )
        if kwargs.get("IfNoneMatch") == "*" and old is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if kwargs.get("IfMatch") is not None and kwargs["IfMatch"] != etag:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[key] = body
        return {
            "ETag": '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'
        }

    def delete_objects(self, **kwargs):
        self.calls.append(("delete", kwargs))
        deleted, errors = [], []
        for obj in kwargs["Delete"]["Objects"]:
            key = obj["Key"]
            if key in self.delete_errors:
                errors.append({"Key": key, "Code": "AccessDenied"})
            else:
                self.objects.pop(key, None)
                deleted.append({"Key": key})
        return {"Deleted": deleted, "Errors": errors}

    def close(self):
        self.closed = True


@pytest.fixture
async def browser():
    settings = configured(api_tokens="api-secret:test")
    s3 = MemoryS3()
    storage = StorageBrowser(settings, client=s3, writer_factory=lambda: s3)
    app = create_app(
        settings, FakePipeline(), FakeRecognizer(), storage_browser=storage
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer api-secret"},
    ) as http:
        yield http, s3, storage, app
    await storage.close()


async def test_pages_never_skip_records_and_fetch_only_the_requested_manifests(browser):
    http, s3, _, _ = browser
    expected = [s3.archive(i) for i in range(1, 8)]
    cursor, found = None, []
    while True:
        before = sum(op == "get" for op, _ in s3.calls)
        response = await http.get(
            "/storage/archives",
            params={
                "kind": "parse",
                "limit": 2,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()
        found.extend(item["prefix"] for item in page["items"])
        assert sum(op == "get" for op, _ in s3.calls) - before <= 2
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == expected[::-1]  # newest first


async def test_listing_filters_roots_and_dates_and_bounds_each_scan(browser):
    http, s3, _, _ = browser
    expected = s3.archive(1, "ocr")
    s3.archive(2)
    s3.archive(3, "ocr", "2026/09/25")
    response = await http.get(
        "/storage/archives",
        params={
            "kind": "ocr",
            "date": "2026-09-26",
        },
    )
    (item,) = response.json()["items"]
    assert item["prefix"] == expected
    assert item["object_count"] == 3
    assert item["total_bytes"] == sum(
        len(value) for key, value in s3.objects.items() if key.startswith(expected)
    )
    assert all(call["MaxKeys"] <= 1000 for op, call in s3.calls if op == "list")
    assert {call["Prefix"] for op, call in s3.calls if op == "list"} == {
        "",
        "ocr/2026/09/26/",
        "_archive_index/v1/ready.json",
    }


async def test_current_and_legacy_layouts_are_both_listed_and_filterable(browser):
    http, s3, _, _ = browser
    current = s3.current_archive()
    legacy = s3.archive(1)

    response = await http.get(
        "/storage/archives", params={"kind": "parse", "date": "2026-09-26"}
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["prefix"] for item in items] == [current, legacy]
    assert items[0]["filename"] == "report.pdf"
    assert items[0]["job_id"] == "Abcd123_"


async def test_listing_exposes_the_archived_source_and_the_zip_includes_it(browser):
    http, s3, _, _ = browser
    original = b"%PDF-1.7 original upload"
    with_source = s3.current_archive(source=original)
    without = s3.current_archive("scan.png", "Efgh456_")
    legacy = s3.archive(1)

    response = await http.get("/storage/archives", params={"date": "2026-09-26"})

    assert response.status_code == 200, response.text
    assert {i["prefix"]: i["input_key"] for i in response.json()["items"]} == {
        with_source: with_source + "input.pdf",
        without: None,
        legacy: None,
    }
    bundle = await http.get("/storage/archive.zip", params={"prefix": with_source})
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert archive.read("input.pdf") == original


async def test_listing_filters_date_range_across_days_and_layouts(browser):
    http, s3, _, _ = browser
    s3.archive(1, "parse", "2026/09/24")
    archive2 = s3.archive(2, "parse", "2026/09/25")
    archive3 = s3.archive(3, "parse", "2026/09/26")
    prefix4 = s3.archive(4, "parse", "2026/09/27")
    current = s3.current_archive()
    response = await http.get(
        "/storage/archives",
        params={
            "kind": "parse",
            "date_from": "2026-09-25",
            "date_to": "2026-09-26",
        },
    )
    assert response.status_code == 200, response.text
    assert [item["prefix"] for item in response.json()["items"]] == [
        current,
        archive3,
        archive2,
    ]
    assert any(
        call["Prefix"] == "parse/" and call.get("StartAfter") == "parse/2026/09/25/"
        for op, call in s3.calls
        if op == "list"
    )
    assert not any(
        call["Key"].startswith(prefix4) for op, call in s3.calls if op == "get"
    )


async def test_open_ended_range_bounds(browser):
    http, s3, _, _ = browser
    archive1 = s3.archive(1, "parse", "2026/09/24")
    archive2 = s3.archive(2, "parse", "2026/09/25")
    archive3 = s3.archive(3, "parse", "2026/09/26")
    archive4 = s3.archive(4, "parse", "2026/09/27")
    current = s3.current_archive()
    only_from = (
        await http.get(
            "/storage/archives",
            params={"kind": "parse", "date_from": "2026-09-26"},
        )
    ).json()
    assert [item["prefix"] for item in only_from["items"]] == [
        archive4,
        current,
        archive3,
    ]
    only_to = (
        await http.get(
            "/storage/archives",
            params={"kind": "parse", "date_to": "2026-09-25"},
        )
    ).json()
    assert [item["prefix"] for item in only_to["items"]] == [archive2, archive1]


async def test_date_range_paginates_with_cursor(browser):
    http, s3, _, _ = browser
    s3.archive(1, "parse", "2026/09/24")
    archive2 = s3.archive(2, "parse", "2026/09/25")
    archive3 = s3.archive(3, "parse", "2026/09/26")
    s3.archive(4, "parse", "2026/09/27")
    current = s3.current_archive()
    found, cursor = [], None
    while True:
        params = {
            "kind": "parse",
            "limit": 1,
            "date_from": "2026-09-25",
            "date_to": "2026-09-26",
            **({"cursor": cursor} if cursor else {}),
        }
        page = (await http.get("/storage/archives", params=params)).json()
        found.extend(item["prefix"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == [current, archive3, archive2]


async def test_listing_pages_through_large_archives_without_empty_pages(browser):
    http, s3, _, _ = browser
    older, newer = s3.archive(1), s3.archive(2)
    for number in range(1005):
        s3.objects[older + f"a{number:04d}.txt"] = b""
    first = (
        await http.get("/storage/archives", params={"kind": "parse", "limit": 1})
    ).json()
    assert [item["prefix"] for item in first["items"]] == [newer]
    second = (
        await http.get(
            "/storage/archives",
            params={"kind": "parse", "limit": 1, "cursor": first["next_cursor"]},
        )
    ).json()
    assert [item["prefix"] for item in second["items"]] == [older]
    assert second["next_cursor"] is None  # never a trailing empty page


async def test_kind_filter_fills_pages_and_ends_without_an_empty_page(browser):
    http, s3, _, _ = browser
    parses = [s3.current_archive(f"p{i}.pdf", f"Parse00{i}") for i in range(3)]
    for i in range(3):
        prefix = s3.current_archive(f"o{i}.png", f"Ocr0000{i}")
        manifest = json.loads(s3.objects[prefix + "manifest.json"])
        manifest["kind"] = "ocr"
        s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    pages, cursor = [], None
    while True:
        params = {"kind": "parse", "limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await http.get("/storage/archives", params=params)).json()
        pages.append([item["prefix"] for item in page["items"]])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [len(page) for page in pages] == [2, 1]
    assert sorted(sum(pages, [])) == sorted(parses)


async def test_newest_archives_come_first_across_filenames(browser):
    http, s3, _, _ = browser
    older = s3.current_archive("zz.pdf", "Older000")
    newer = "aa.pdf__20260927T090000000000Z__Newer000/"
    s3.objects[newer + "manifest.json"] = (
        s3.objects[older + "manifest.json"]
        .replace(b"Older000", b"Newer000")
        .replace(older.encode(), newer.encode())
    )
    s3.objects[newer + "document.md"] = b"# newer"
    items = (await http.get("/storage/archives")).json()["items"]
    assert [item["prefix"] for item in items] == [newer, older]


@pytest.mark.parametrize(
    "prefix",
    [
        "other/2026/09/26/" + "0" * 32 + "/",
        "parse/2026/02/30/" + "0" * 32 + "/",
        "parse/2026/09/26/../",
        "parse/2026/09/26/" + "0" * 32,
    ],
)
async def test_invalid_prefix_never_reaches_s3(browser, prefix):
    http, s3, _, _ = browser
    response = await http.get("/storage/archive", params={"prefix": prefix})
    assert response.status_code == 422
    assert not s3.calls


@pytest.mark.parametrize(
    "suffix",
    ["../escape", "images/../../escape", "%2e%2e/x", "x\\y", "x\r\nInjected: 1"],
)
async def test_invalid_keys_never_reach_s3(browser, suffix):
    http, s3, _, _ = browser
    prefix = s3.archive(1)
    response = await http.get("/storage/file", params={"key": prefix + suffix})
    assert response.status_code == 422
    assert not s3.calls


async def test_detail_uses_actual_sizes_includes_manifest_and_missing_is_404(browser):
    http, s3, _, _ = browser
    prefix = s3.archive(1)
    s3.objects[prefix + "document.md"] += b" longer"
    detail = (await http.get("/storage/archive", params={"prefix": prefix})).json()
    assert detail["prefix"] == prefix
    assert {f["key"]: f["size"] for f in detail["files"]} == {
        key: len(value) for key, value in s3.objects.items()
    }
    del s3.objects[prefix + "manifest.json"]
    response = await http.get("/storage/archive", params={"prefix": prefix})
    assert response.status_code == 404


async def test_file_headers_preview_limit_and_body_read_are_bounded(browser):
    http, s3, _, _ = browser
    prefix = s3.archive(1)
    response = await http.get(
        "/storage/file", params={"key": prefix + "document.md", "preview": "true"}
    )
    assert response.content == b"# synthetic text"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="document.md"'
    )
    assert response.headers["content-type"].startswith("text/plain")
    s3.objects[prefix + "document.md"] = b"x" * (1024 * 1024 + 1)
    response = await http.get(
        "/storage/file", params={"key": prefix + "document.md", "preview": "true"}
    )
    assert response.status_code == 413
    assert (
        await http.get("/storage/file", params={"key": prefix + "document.md"})
    ).status_code == 200


async def test_auth_disabled_outage_and_documented_bearer(browser):
    http, s3, _, app = browser
    assert (
        await http.get("/storage/archives", headers={"Authorization": ""})
    ).status_code == 401
    assert not s3.calls
    for path, method in (
        ("/storage/archives", "get"),
        ("/storage/archive", "get"),
        ("/storage/archive", "delete"),
        ("/storage/file", "get"),
    ):
        assert app.openapi()["paths"][path][method]["security"] == [{"BearerAuth": []}]

    def fail(**kwargs):
        raise RuntimeError("secret credential document")

    s3.list_objects_v2 = fail
    response = await http.get("/storage/archives")
    assert response.status_code == 503
    assert "secret" not in response.text
    disabled = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(disabled), base_url="http://test"
    ) as client:
        response = await client.get("/storage/archives")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_browser_disabled"


async def test_delete_requires_exact_confirmation_and_keeps_manifest_on_partial_failure(
    browser,
):
    http, s3, _, _ = browser
    prefix, other = s3.archive(1), s3.archive(2)
    for confirm in ("", "yes", "1", "0" * 32):
        response = await http.request(
            "DELETE",
            "/storage/archive",
            params={"prefix": prefix},
            json={"confirm": confirm},
        )
        assert response.status_code == 409
    assert not s3.calls
    s3.delete_errors.add(prefix + "document.md")
    response = await http.request(
        "DELETE",
        "/storage/archive",
        params={"prefix": prefix},
        json={"confirm": f"{1:032x}"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_delete_partial"
    assert response.json()["detail"]["deleted_objects"] == 1
    assert prefix + "manifest.json" in s3.objects
    s3.delete_errors.clear()
    response = await http.request(
        "DELETE",
        "/storage/archive",
        params={"prefix": prefix},
        json={"confirm": f"{1:032x}"},
    )
    assert response.json() == {"deleted_objects": 2}
    assert all(key.startswith(other) for key in s3.objects)
    deletes = [call["Delete"]["Objects"] for op, call in s3.calls if op == "delete"]
    # The manifest goes after the data, and the index entry after the manifest.
    assert deletes[-2:] == [
        [{"Key": prefix + "manifest.json"}],
        [{"Key": index_key(prefix)}],
    ]


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_retain_worker_capacity(monkeypatch, cancel):
    import ocrserver.storage_browser as module

    monkeypatch.setattr(module, "IO_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "MAX_INFLIGHT", 1)
    entered, release = threading.Event(), threading.Event()
    s3 = MemoryS3()

    def blocked(**kwargs):
        entered.set()
        release.wait(2)
        return {"Contents": [], "IsTruncated": False}

    s3.list_objects_v2 = blocked
    storage = StorageBrowser(configured(), client=s3)
    try:
        task = asyncio.create_task(storage.archives())
        await wait_event(entered)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(Exception) as error:
                await task
            assert error.value.status_code == 503
        with pytest.raises(Exception) as error:
            await storage.archives()
        assert error.value.detail["code"] == "storage_browser_busy"
    finally:
        release.set()
        await storage.close()


def test_browser_configuration_independent_of_writer_and_secret_repr():
    settings = load_settings(
        {
            "OCR_STORAGE_BROWSER_ENABLED": "true",
            "OCR_S3_BROWSER_ACCESS_KEY": "browser-private-access",
            "OCR_S3_BROWSER_SECRET_KEY": "browser-private-secret",
        },
        None,
    )
    assert settings.storage_browser_enabled and not settings.storage_enabled
    assert "browser-private" not in repr(settings)
    with pytest.raises(ValueError):
        Settings(storage_browser_enabled=True)


@pytest.mark.parametrize(
    "bad",
    [b"not json", b"x" * (1024 * 1024 + 1), b"[]"],
    ids=["malformed", "oversized", "wrong-shape"],
)
async def test_bad_manifest_is_skipped_without_hiding_following_archives(browser, bad):
    http, s3, _, _ = browser
    malformed, expected = s3.archive(1), s3.archive(2)
    s3.objects[malformed + "manifest.json"] = bad
    first = await http.get("/storage/archives", params={"kind": "parse", "limit": 1})
    assert first.status_code == 200
    assert [item["prefix"] for item in first.json()["items"]] == [expected]
    second = await http.get(
        "/storage/archives",
        params={
            "kind": "parse",
            "limit": 1,
            "cursor": first.json()["next_cursor"],
        },
    )
    (broken,) = second.json()["items"]
    assert broken["prefix"] == malformed and broken["incomplete"] is True
    deleted = await http.request(
        "DELETE",
        "/storage/archive",
        params={"prefix": malformed},
        json={"confirm": f"{1:032x}"},
    )
    assert deleted.status_code == 200
    assert all(key.startswith(expected) for key in s3.objects)


async def test_cursor_binds_filters_and_all_kinds_paginate(browser):
    http, s3, _, _ = browser
    expected = [
        s3.archive(i, kind)
        for i, kind in enumerate(("ocr", "parse", "parse-chunks"), 1)
    ]
    found, cursor = [], None
    while True:
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = (await http.get("/storage/archives", params=params)).json()
        found.extend(item["prefix"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        wrong_filter = await http.get(
            "/storage/archives", params={"cursor": cursor, "kind": "parse"}
        )
        assert wrong_filter.status_code == 422
    assert found == sorted(expected, reverse=True)
    assert (
        await http.get("/storage/archives", params={"cursor": "%%invalid%%"})
    ).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "other"},
        {"date": "2026-02-30"},
        {"date_from": "2026-09-27", "date_to": "2026-09-26"},
        {"date": "2026-09-26", "date_from": "2026-09-26"},
        {"date_from": "26-09-2026"},
        {"limit": "lots"},
        {"limit": 51},
        {"limit": 0},
    ],
)
async def test_invalid_filters_are_sanitized_without_s3(browser, params):
    http, s3, _, _ = browser
    response = await http.get("/storage/archives", params=params)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], dict)
    assert not s3.calls


async def test_lying_content_length_and_png_preview_have_bounded_reads(browser):
    http, s3, _, _ = browser
    prefix = s3.archive(1)
    streams = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    honest = s3.get_object

    def get_object(**kwargs):
        if kwargs["Key"].endswith("manifest.json"):
            return honest(**kwargs)
        body = Body(b"x" * (1024 * 1024 + 1))
        streams.append(body)
        return {"Body": body, "ContentLength": 1}

    s3.get_object = get_object
    text = await http.get(
        "/storage/file", params={"key": prefix + "document.md", "preview": "true"}
    )
    assert text.status_code == 413
    png = await http.get(
        "/storage/file", params={"key": prefix + "figure.png", "preview": "true"}
    )
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert all(stream.closed for stream in streams)


def test_browser_uses_explicit_separate_credentials_without_retries(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    browser = StorageBrowser(configured())
    client = browser._make_client()
    try:
        assert client._request_signer._credentials.access_key == "browser-access"
        assert client._request_signer._credentials.secret_key == "browser-secret"
        assert client.meta.config.retries["total_max_attempts"] == 1
        assert client.meta.config.connect_timeout <= 2
        assert client.meta.config.read_timeout <= 2
    finally:
        client.close()


async def test_deeply_nested_cursor_is_sanitized_without_s3(browser):
    http, s3, _, _ = browser
    cursor = base64.urlsafe_b64encode(("[" * 1100 + "0" + "]" * 1100).encode()).decode()
    assert len(cursor) < 4096
    response = await http.get("/storage/archives", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "storage_invalid_cursor"}}
    assert not s3.calls


@pytest.mark.parametrize("completion", ["drain", "disconnect", "cancel"])
async def test_download_capacity_lasts_through_outer_asgi_send(browser, completion):
    http, s3, _, app = browser
    key = s3.archive(1) + "document.md"
    entered = [asyncio.Event(), asyncio.Event()]
    drain = [asyncio.Event(), asyncio.Event()]

    async def download(index):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/storage/file",
            "raw_path": b"/storage/file",
            "query_string": urlencode({"key": key}).encode(),
            "root_path": "",
            "headers": [(b"authorization", b"Bearer api-secret")],
            "client": ("127.0.0.1", 1000 + index),
            "server": ("test", 80),
        }
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
            elif message.get("body"):
                entered[index].set()
                await drain[index].wait()
                if index == 0 and completion == "disconnect":
                    raise OSError("Client disconnected")

        await app(scope, receive, send)

    tasks = [asyncio.create_task(download(index)) for index in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 2)
        response = await http.get("/storage/file", params={"key": key})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "storage_browser_busy"
        # Each download reads the manifest, then the file.
        assert sum(op == "get" for op, _ in s3.calls) == 4
        if completion == "cancel":
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
        else:
            drain[0].set()
            if completion == "disconnect":
                with pytest.raises(OSError):
                    await tasks[0]
            else:
                await tasks[0]
        assert not tasks[1].done()
        response = await http.get("/storage/file", params={"key": key})
        assert response.status_code == 200
        assert response.content == s3.objects[key]
    finally:
        for gate in drain:
            gate.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_download_errors_and_response_construction_release_admission(
    browser, monkeypatch
):
    import ocrserver.storage_browser as module

    _, s3, storage, _ = browser
    key = s3.archive(1) + "document.md"
    for _ in range(3):
        with pytest.raises(Exception) as error:
            await storage.file(key + ".missing")
        assert error.value.status_code == 404

    def fail_construction(*args, **kwargs):
        raise ValueError("Response construction failed")

    with monkeypatch.context() as patch:
        patch.setattr(module, "_DownloadResponse", fail_construction)
        for _ in range(3):
            with pytest.raises(ValueError, match="Response construction failed"):
                await storage.file(key)

    # Direct users also release admission when no outer app middleware exists.
    for _ in range(3):
        response = await storage.file(key)
        messages = []

        async def send(message, sink=messages):
            sink.append(message)

        await response({"type": "http"}, None, send)
        assert messages[-1]["body"] == s3.objects[key]


@pytest.mark.parametrize("cancel", [False, True])
async def test_download_timeout_retains_worker_and_releases_download_lease(
    monkeypatch, cancel
):
    import ocrserver.storage_browser as module

    normal_io_timeout = module.IO_TIMEOUT_SECONDS
    normal_download_timeout = module.DOWNLOAD_TIMEOUT_SECONDS
    monkeypatch.setattr(module, "IO_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "DOWNLOAD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "MAX_INFLIGHT", 1)
    s3 = MemoryS3()
    key = s3.archive(1) + "document.md"
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original_get = s3.get_object

    def get_object(**kwargs):
        entered.set()
        try:
            release.wait(2)
            return original_get(**kwargs)
        finally:
            exited.set()

    s3.get_object = get_object
    storage = StorageBrowser(configured(), client=s3)
    task = asyncio.create_task(storage.file(key))
    try:
        await wait_event(entered)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(Exception) as error:
                await task
            assert error.value.status_code == 503
        with pytest.raises(Exception) as error:
            await storage.file(key)
        assert error.value.detail["code"] == "storage_browser_busy"
        release.set()
        await wait_event(exited)
        # S3 exit precedes its future completion callback by a few instructions.
        async with asyncio.timeout(1):
            while storage._active:
                await asyncio.sleep(0.005)
        s3.get_object = original_get
        # The recovery assertion tests released capacity, not a 50 ms thread startup.
        monkeypatch.setattr(module, "IO_TIMEOUT_SECONDS", normal_io_timeout)
        monkeypatch.setattr(module, "DOWNLOAD_TIMEOUT_SECONDS", normal_download_timeout)
        response = await storage.file(key)

        async def send(message):
            pass

        await response({"type": "http"}, None, send)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()


async def test_incomplete_archives_are_deletable_only_once_abandoned(browser):
    http, s3, _, _ = browser
    stale = s3.current_archive("old.pdf", "Stale000")
    s3.objects[stale + "manifest.json"] = b'{"complete":false}'
    fresh = "new.pdf__29990101T000000000000Z__Fresh000/"
    s3.objects[fresh + "document.md"] = b"# still writing"
    items = (await http.get("/storage/archives")).json()["items"]
    assert [(item["prefix"], item["incomplete"]) for item in items] == [(stale, True)]
    in_progress = await http.request(
        "DELETE",
        "/storage/archive",
        params={"prefix": fresh},
        json={"confirm": "Fresh000"},
    )
    assert in_progress.status_code == 409
    removed = await http.request(
        "DELETE",
        "/storage/archive",
        params={"prefix": stale},
        json={"confirm": "Stale000"},
    )
    assert removed.status_code == 200
    assert not any(key.startswith(stale) for key in s3.objects)


async def test_files_are_served_only_from_complete_archives(browser):
    http, s3, _, _ = browser
    prefix = s3.current_archive()
    s3.objects[prefix + "manifest.json"] = b'{"complete":false}'
    response = await http.get("/storage/file", params={"key": prefix + "document.md"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_invalid_manifest"


async def test_back_to_back_requests_never_see_a_released_slot_as_busy(browser):
    _, s3, storage, _ = browser
    key = s3.archive(1) + "document.md"
    for _ in range(20):
        with pytest.raises(Exception) as error:
            await storage.file(key + ".missing")
        assert error.value.status_code == 404
    assert not storage._active


async def test_empty_folder_prefixes_of_deleted_archives_are_ignored(browser):
    http, s3, _, _ = browser
    kept = s3.current_archive()
    ghost = "old.pdf__20200101T000000000000Z__Ghost000/"
    listing = s3.list_objects_v2

    def with_ghost(**kwargs):
        result = listing(**kwargs)
        if kwargs.get("Delimiter") and not kwargs.get("StartAfter"):
            result["CommonPrefixes"].append({"Prefix": ghost})
        return result

    s3.list_objects_v2 = with_ghost
    page = (await http.get("/storage/archives")).json()
    assert [item["prefix"] for item in page["items"]] == [kept]
    assert page["skipped_archives"] == 0


async def test_refinement_saves_identifiable_artifact_in_parent_and_is_idempotent(
    browser,
):
    http, s3, _, _ = browser
    prefix = s3.current_archive()
    source = {
        "chunks": [
            {
                "id": 7,
                "text": "First",
                "page": 1,
                "token_count": 100,
                "page_header": "H",
            },
            {"id": 8, "text": "Second", "page": 1, "token_count": 120, "footnote": "N"},
            {"id": 9, "text": "Third", "page": 2, "token_count": 80},
        ],
        "metadata": {"document": "report.pdf", "n_chunks": 3},
    }
    key = prefix + "json_rag.json"
    s3.objects[key] = json.dumps(source).encode()
    manifest = json.loads(s3.objects[prefix + "manifest.json"])
    manifest["artifacts"].append(
        {"name": "json_rag.json", "key": key, "size": len(s3.objects[key])}
    )
    s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    payload = {
        "source_key": key,
        "source_sha256": hashlib.sha256(s3.objects[key]).hexdigest(),
        "execution_id": "79e33f8f-95ed-48f4-af27-1a9daab51bca",
        "created_at": "2026-09-27T12:30:00Z",
        "options": {"page_boundary": True, "min_tokens": 150, "max_tokens": 250},
        "groups": [[0, 1], [2]],
    }
    first = await http.post("/storage/rag/refine", json=payload)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved["prefix"] == prefix
    assert saved["file"]["name"] == "json_rag_Abcd123__001.json"
    output = json.loads(s3.objects[saved["file"]["key"]])
    assert output["chunks"][0] == {
        "id": 1,
        "text": "First\n\nSecond",
        "token_count": 220,
        "document": "report.pdf",
        "page": [1],
        "page_header": ["H"],
        "footnote": ["N"],
    }
    assert output["chunks"][1] == {
        "id": 2,
        "text": "Third",
        "token_count": 80,
        "document": "report.pdf",
        "page": [2],
    }
    assert output["metadata"]["refinement"]["range_counts"] == {
        "within": 1,
        "below": 1,
        "above": 0,
        "unknown": 0,
    }
    detail = (await http.get("/storage/archive", params={"prefix": prefix})).json()
    assert saved["file"]["key"] in [file["key"] for file in detail["files"]]
    assert (
        len(
            [
                a
                for a in detail["manifest"]["artifacts"]
                if a["key"] == saved["file"]["key"]
            ]
        )
        == 1
    )
    again = await http.post("/storage/rag/refine", json=payload)
    assert again.status_code == 200
    assert again.json() == saved
    assert (
        len(
            [
                call
                for op, call in s3.calls
                if op == "put" and call["Key"] == saved["file"]["key"]
            ]
        )
        == 1
    )
    reused = await http.post(
        "/storage/rag/refine", json={**payload, "groups": [[0], [1], [2]]}
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "rag_execution_conflict"
    payload["execution_id"] = "a0000000-0000-4000-a000-000000000002"
    second = await http.post("/storage/rag/refine", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["file"]["name"] == "json_rag_Abcd123__002.json"


async def test_refinement_rejects_changed_source_and_incomplete_groups(browser):
    http, s3, _, _ = browser
    prefix = s3.current_archive()
    key = prefix + "json_rag.json"
    s3.objects[key] = json.dumps(
        {
            "chunks": [
                {"id": 1, "text": "one", "page": 1, "token_count": 1},
                {"id": 2, "text": "two", "page": 2, "token_count": 1},
            ]
        }
    ).encode()
    manifest = json.loads(s3.objects[prefix + "manifest.json"])
    manifest["artifacts"] = [
        {"name": "json_rag.json", "key": key, "size": len(s3.objects[key])}
    ]
    s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    body = {
        "source_key": key,
        "source_sha256": "0" * 64,
        "execution_id": "79e33f8f-95ed-48f4-af27-1a9daab51bca",
        "created_at": "2026-09-27T12:30:00Z",
        "options": {"page_boundary": True, "min_tokens": 1, "max_tokens": 2},
        "groups": [[0], [1]],
    }
    changed = await http.post("/storage/rag/refine", json=body)
    assert changed.status_code == 409
    body["source_sha256"] = hashlib.sha256(s3.objects[key]).hexdigest()
    body["groups"] = [[0, 1]]
    # R21: a cross-page group is a real fact, derived and recorded by the server.
    cross_page = await http.post("/storage/rag/refine", json=body)
    assert cross_page.status_code == 200, cross_page.text
    saved = json.loads(s3.objects[cross_page.json()["file"]["key"]])
    assert [note["code"] for note in saved["chunks"][0]["diagnostics"]] == [
        "page_boundary_forced"
    ]
    # An incomplete plan is still refused.
    body["execution_id"] = "79e33f8f-95ed-48f4-af27-1a9daab51bcb"
    body["groups"] = [[0]]
    incomplete = await http.post("/storage/rag/refine", json=body)
    assert incomplete.status_code == 422


async def test_archive_delete_works_while_archiving_is_off(browser):
    http, s3, storage, _ = browser
    prefix = s3.archive(1)
    # Browse-only mode: the browser identity deletes whole archives itself.
    storage.settings = dataclasses.replace(storage.settings, storage_enabled=False)
    response = await http.request(
        "DELETE",
        "/storage/archive",
        params={"prefix": prefix},
        json={"confirm": f"{1:032x}"},
    )
    assert response.status_code == 200
    assert response.json()["deleted_objects"] > 0
    detail = await http.get("/storage/archive", params={"prefix": prefix})
    assert detail.status_code == 404


async def test_single_artifact_delete_removes_object_and_manifest_entry(browser):
    http, s3, storage, _ = browser
    prefix = s3.current_archive()
    key = prefix + "json_rag.json"
    s3.objects[key] = json.dumps(
        {
            "chunks": [
                {"id": 1, "text": "one", "page": 1, "token_count": 100},
            ]
        }
    ).encode()
    manifest = json.loads(s3.objects[prefix + "manifest.json"])
    manifest["artifacts"].append(
        {"name": "json_rag.json", "key": key, "size": len(s3.objects[key])}
    )
    s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    payload = {
        "source_key": key,
        "source_sha256": hashlib.sha256(s3.objects[key]).hexdigest(),
        "execution_id": "79e33f8f-95ed-48f4-af27-1a9daab51bca",
        "created_at": "2026-09-27T12:30:00Z",
        "options": {"page_boundary": True, "min_tokens": 1, "max_tokens": 200},
        "groups": [[0]],
    }
    saved = (await http.post("/storage/rag/refine", json=payload)).json()
    refined = saved["file"]
    # Wrong confirmation, a missing artifact and the manifest itself are refused.
    wrong = await http.request(
        "DELETE",
        "/storage/file",
        params={"key": refined["key"]},
        json={"confirm": "other.json"},
    )
    assert wrong.status_code == 409
    missing = await http.request(
        "DELETE",
        "/storage/file",
        params={"key": prefix + "json_rag_Abcd123__042.json"},
        json={"confirm": "json_rag_Abcd123__042.json"},
    )
    assert missing.status_code == 404
    manifest_refusal = await http.request(
        "DELETE",
        "/storage/file",
        params={"key": prefix + "manifest.json"},
        json={"confirm": "manifest.json"},
    )
    assert manifest_refusal.status_code == 422
    # Browser-only mode cannot update the manifest, so nothing is deleted.
    writer_settings = storage.settings
    storage.settings = dataclasses.replace(writer_settings, storage_enabled=False)
    read_only = await http.request(
        "DELETE",
        "/storage/file",
        params={"key": refined["key"]},
        json={"confirm": refined["name"]},
    )
    assert read_only.status_code == 503
    assert read_only.json()["detail"]["code"] == "storage_write_disabled"
    storage.settings = writer_settings
    assert refined["key"] in s3.objects
    # The artifact and its manifest entry go away together; the source stays.
    deleted = await http.request(
        "DELETE",
        "/storage/file",
        params={"key": refined["key"]},
        json={"confirm": refined["name"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted_objects": 1}
    assert refined["key"] not in s3.objects
    detail = (await http.get("/storage/archive", params={"prefix": prefix})).json()
    assert refined["key"] not in [file["key"] for file in detail["files"]]
    assert key in [file["key"] for file in detail["files"]]
    assert all(
        item["key"] != refined["key"] for item in detail["manifest"]["artifacts"]
    )
    assert (await http.get("/storage/file", params={"key": key})).status_code == 200
    # A deleted number is never handed out again, even though it was the highest.
    payload["execution_id"] = "a0000000-0000-4000-a000-000000000002"
    after = await http.post("/storage/rag/refine", json=payload)
    assert after.status_code == 200, after.text
    assert after.json()["file"]["name"] == "json_rag_Abcd123__002.json"


async def test_refinement_skips_taken_numbers_and_adopts_its_own_orphan(browser):
    http, s3, _, _ = browser
    prefix = s3.current_archive()
    key = prefix + "json_rag.json"
    s3.objects[key] = json.dumps(
        {
            "chunks": [
                {"id": 1, "text": "one", "page": 1, "token_count": 100},
            ]
        }
    ).encode()
    manifest = json.loads(s3.objects[prefix + "manifest.json"])
    manifest["artifacts"].append(
        {"name": "json_rag.json", "key": key, "size": len(s3.objects[key])}
    )
    s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
    # _001 is an unlisted foreign object; _002 is taken by a concurrent writer
    # between the listing and the conditional create.
    s3.objects[prefix + "json_rag_Abcd123__001.json"] = b"{}"
    put, raced, failed = s3.put_object, [], []

    def racing_put(**kwargs):
        if kwargs["Key"].endswith("__002.json") and not raced:
            raced.append(True)
            s3.objects[kwargs["Key"]] = b"{}"
        if kwargs["Key"].endswith("manifest.json") and not failed:
            failed.append(True)
            raise ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")
        return put(**kwargs)

    s3.put_object = racing_put
    payload = {
        "source_key": key,
        "source_sha256": hashlib.sha256(s3.objects[key]).hexdigest(),
        "execution_id": "79e33f8f-95ed-48f4-af27-1a9daab51bca",
        "created_at": "2026-09-27T12:30:00Z",
        "options": {"page_boundary": False, "min_tokens": 1, "max_tokens": 200},
        "groups": [[0]],
    }
    assert (await http.post("/storage/rag/refine", json=payload)).status_code >= 500
    orphan = prefix + "json_rag_Abcd123__003.json"
    assert orphan in s3.objects
    # The retry adopts the orphan instead of writing a second copy.
    retry = await http.post("/storage/rag/refine", json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json()["file"]["key"] == orphan
    assert (
        len([call for op, call in s3.calls if op == "put" and call["Key"] == orphan])
        == 1
    )
