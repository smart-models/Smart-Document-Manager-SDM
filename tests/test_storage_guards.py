"""Storage failures and indexed listing, with isolated S3 fakes."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException

from ocr_server import create_app
from ocrserver.archive_names import archive_prefix
from ocrserver.storage_browser import StorageBrowser
from ocrserver.storage_index import (
    _REVERSE_DIGITS,
    INDEX_ENTRIES,
    INDEX_READY,
    index_key,
    index_archive,
    rebuild_index,
    sort_stamp,
)
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_storage_browser import MemoryS3, configured


def entry_listings(s3):
    """Count the listings of the temporal index recorded by the fake client."""
    return len(
        [c for op, c in s3.calls if op == "list" and c["Prefix"] == INDEX_ENTRIES]
    )


async def test_partial_deletion_cannot_produce_a_successful_incomplete_zip():
    s3 = MemoryS3()
    prefix = s3.archive(1)
    browser = StorageBrowser(configured(), client=s3)
    s3.delete_errors.add(prefix + "document.md")
    with pytest.raises(HTTPException) as caught:
        await browser.delete(prefix, f"{1:032x}")
    assert caught.value.detail["code"] == "storage_delete_partial"
    with pytest.raises(HTTPException) as caught:
        await browser.archive_zip(prefix)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "storage_archive_incomplete"
    assert prefix + "manifest.json" in s3.objects
    assert not browser._downloads
    s3.delete_errors.clear()
    assert (await browser.delete(prefix, f"{1:032x}"))["deleted_objects"] == 2
    await browser.close()


async def test_disappearing_file_during_zip_is_an_incomplete_archive():
    class VanishingS3(MemoryS3):
        def get_object(self, **kwargs):
            if kwargs["Key"].endswith("document.md"):
                self.objects.pop(kwargs["Key"], None)
            return super().get_object(**kwargs)

    s3 = VanishingS3()
    prefix = s3.archive(1)
    browser = StorageBrowser(configured(), client=s3)
    with pytest.raises(HTTPException) as caught:
        await browser.archive_zip(prefix)
    assert caught.value.status_code == 409
    assert not browser._downloads
    await browser.close()


@pytest.mark.parametrize("path", ["/storage/archive", "/storage/file"])
async def test_slow_confirmation_times_out_before_any_s3_operation(monkeypatch, path):
    monkeypatch.setattr("ocrserver.storage_browser.IO_TIMEOUT_SECONDS", 0.02)
    s3 = MemoryS3()
    prefix = s3.archive(1)
    browser = StorageBrowser(configured(), client=s3)
    app = create_app(
        configured(), FakePipeline(), FakeRecognizer(), storage_browser=browser
    )

    release = asyncio.Event()

    async def slow():
        yield b'{"confirm":"'
        await release.wait()
        pytest.fail("confirmation should stop before EOF")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.request(
                "DELETE",
                path,
                params={"prefix": prefix, "key": prefix + "document.md"},
                content=slow(),
            ),
            1,
        )
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "storage_confirmation_timeout"
    assert not s3.calls
    assert len(s3.objects) == 3
    await browser.close()


def test_index_sorts_like_existing_timestamp_and_prefix_order():
    prefixes = [
        "a.pdf__20260926T120000000000Z__Abcd123_/",
        "b.pdf__20260926T120000000000Z__Abcd123_/",
        "a.pdf__20260927T120000000000Z__Abcd123_/",
        f"parse/2026/09/26/{1:032x}/",
    ]
    prefixes.append("a" * 96 + "__20260926T120000000000Z__Abcd123_/")
    assert all(
        len(component) <= 255
        for prefix in prefixes
        for component in index_key(prefix).split("/")
    )
    expected = sorted(prefixes, key=lambda p: (sort_stamp(p), p), reverse=True)
    assert [index_archive(key) for key in sorted(map(index_key, prefixes))] == expected
    with pytest.raises(ValueError):
        index_archive(INDEX_ENTRIES + "bad")


async def test_index_backfill_preserves_all_layouts_and_newest_first_pagination():
    s3 = MemoryS3()
    prefixes = [s3.archive(1), s3.archive(2, "ocr"), s3.current_archive()]
    # Orphan/incomplete archives also remain addressable after migration.
    incomplete = "abandoned.pdf__20260925T120000000000Z__Dead123_/"
    s3.objects[incomplete + "document.md"] = b"abandoned"
    assert rebuild_index(s3, "ocr-outputs") == 4
    assert rebuild_index(s3, "ocr-outputs") == 4
    browser = StorageBrowser(configured(), client=s3)
    found, cursor = [], None
    while True:
        page = await browser.archives(limit=1, cursor=cursor)
        found.extend(item["prefix"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert found == [prefixes[2], prefixes[0], prefixes[1], incomplete]
    assert (await browser.archives(kind="ocr"))["items"][0]["prefix"] == prefixes[1]
    await browser.close()


async def test_indexed_day_does_not_scan_50001_other_archives():
    s3 = MemoryS3()
    target = s3.current_archive()
    s3.objects[INDEX_READY] = b"1"
    s3.objects[index_key(target)] = b""
    for i in range(50001):
        prefix = f"old{i}.pdf__20260925T120000000000Z__Abcd123_/"
        s3.objects[index_key(prefix)] = b""
    browser = StorageBrowser(configured(), client=s3)
    response = await browser.archives(date_from="2026-09-26", date_to="2026-09-26")
    assert [r["prefix"] for r in response["items"]] == [target]
    # Scanning the other 50001 entries would take at least 51 listings.
    assert entry_listings(s3) <= 2
    empty = await browser.archives(date_from="2026-10-11", date_to="2026-10-11")
    assert empty["items"] == [] and empty["next_cursor"] is None
    await browser.close()


async def test_indexed_unfiltered_pages_do_not_scan_all_entries_and_skip_retention():
    s3 = MemoryS3()
    prefixes = [s3.archive(i) for i in range(1, 4)]
    rebuild_index(s3, "ocr-outputs")
    # Simulate retention removing an entire archive, leaving its index entry.
    for key in list(s3.objects):
        if key.startswith(prefixes[2]):
            del s3.objects[key]
    browser = StorageBrowser(configured(), client=s3)
    s3.calls.clear()
    first = await browser.archives(limit=1)
    assert first["items"][0]["prefix"] == prefixes[1]
    assert entry_listings(s3) == 1
    second = await browser.archives(limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["prefix"] == prefixes[0]
    assert second["next_cursor"] is None
    await browser.close()


async def test_deleted_archives_leave_no_index_entries_or_empty_pages(monkeypatch):
    monkeypatch.setattr("ocrserver.storage_browser.MAX_MANIFEST_READS", 2)
    s3 = MemoryS3()
    prefixes = [s3.archive(i) for i in range(1, 6)]
    rebuild_index(s3, "ocr-outputs")
    browser = StorageBrowser(configured(), client=s3)
    for number in (2, 3, 4):
        await browser.delete(prefixes[number - 1], f"{number:032x}")
        assert index_key(prefixes[number - 1]) not in s3.objects
    pages, cursor = [], None
    while True:
        page = await browser.archives(limit=1, cursor=cursor)
        pages.append([item["prefix"] for item in page["items"]])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == [[prefixes[4]], [prefixes[0]]]
    await browser.close()


def test_backfill_prunes_vanished_archives_but_not_ones_being_written():
    s3 = MemoryS3()
    live = s3.archive(1)
    vanished = "gone.pdf__20260925T120000000000Z__Gone123_/"
    # Writers index before reserving: a fresh entry may not have objects yet.
    writing = archive_prefix("new.pdf", datetime.now(UTC), "Fresh12_") + "/"
    for prefix in (vanished, writing):
        s3.objects[index_key(prefix)] = b""
    assert rebuild_index(s3, "ocr-outputs") == 1
    assert {
        index_archive(key) for key in s3.objects if key.startswith(INDEX_ENTRIES)
    } == {live, writing}


def test_backfill_does_not_renew_existing_entries_past_retention():
    s3 = MemoryS3()
    prefix = s3.archive(1)
    rebuild_index(s3, "ocr-outputs")
    s3.calls.clear()
    rebuild_index(s3, "ocr-outputs")
    assert not [
        call
        for op, call in s3.calls
        if op == "put" and call["Key"].startswith(INDEX_ENTRIES)
    ]
    assert index_key(prefix) in s3.objects


def test_backfill_writes_one_entry_per_archive():
    s3 = MemoryS3()
    s3.archive(1)
    rebuild_index(s3, "ocr-outputs")
    puts = [call["Key"] for op, call in s3.calls if op == "put"]
    assert len([key for key in puts if key.startswith(INDEX_ENTRIES)]) == 1


@pytest.mark.parametrize(
    "age,pruned",
    [
        (timedelta(minutes=10), True),
        (timedelta(minutes=10) - timedelta(microseconds=1), False),
    ],
)
def test_orphan_window_is_exactly_ten_minutes(monkeypatch, age, pruned):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 26, 12, tzinfo=UTC) + age

    monkeypatch.setattr("ocrserver.storage_index.datetime", Clock)
    s3 = MemoryS3()
    key = index_key("gone.pdf__20260926T120000000000Z__Gone123_/")
    s3.objects[key] = b""
    rebuild_index(s3, "ocr-outputs")
    assert (key not in s3.objects) is pruned


def test_backfill_prunes_orphans_in_batches_of_1000():
    s3 = MemoryS3()
    keys = [
        index_key(f"gone{i}.pdf__20260925T120000000000Z__Gone123_/")
        for i in range(2001)
    ]
    s3.objects.update(dict.fromkeys(keys, b""))
    rebuild_index(s3, "ocr-outputs")
    sizes = [len(c["Delete"]["Objects"]) for op, c in s3.calls if op == "delete"]
    assert sizes == [1000, 1000, 1]
    assert not any(key.startswith(INDEX_ENTRIES) for key in s3.objects)


@pytest.mark.parametrize("name", ["bad", "x__zz", "x__00"])
def test_backfill_prunes_unreadable_index_keys(name):
    s3 = MemoryS3()
    s3.objects[INDEX_ENTRIES + name] = b""
    assert rebuild_index(s3, "ocr-outputs") == 0
    assert INDEX_ENTRIES + name not in s3.objects
    assert INDEX_READY in s3.objects


@pytest.mark.parametrize(
    "page",
    [
        {"Contents": [], "IsTruncated": True},
        {
            "Contents": [{"Key": INDEX_ENTRIES + "b"}, {"Key": INDEX_ENTRIES + "a"}],
            "IsTruncated": False,
        },
    ],
)
def test_backfill_refuses_a_listing_that_does_not_advance(page):
    class Stuck(MemoryS3):
        def list_objects_v2(self, **kwargs):
            return page

    gone = index_key("gone.pdf__20260925T120000000000Z__Gone123_/")
    s3 = Stuck()
    s3.objects[gone] = b""
    with pytest.raises(ValueError, match="did not advance"):
        rebuild_index(s3, "ocr-outputs")
    assert gone in s3.objects and INDEX_READY not in s3.objects


async def test_delete_succeeds_when_unindexing_fails_and_backfill_prunes_later():
    class Failing(MemoryS3):
        fail = True

        def delete_objects(self, **kwargs):
            key = kwargs["Delete"]["Objects"][0]["Key"]
            if self.fail and key.startswith(INDEX_ENTRIES):
                raise RuntimeError("index unavailable")
            return super().delete_objects(**kwargs)

    s3 = Failing()
    prefix = s3.archive(1)
    rebuild_index(s3, "ocr-outputs")
    browser = StorageBrowser(configured(), client=s3)
    assert await browser.delete(prefix, f"{1:032x}") == {"deleted_objects": 3}
    assert index_key(prefix) in s3.objects
    await browser.close()
    s3.fail = False
    rebuild_index(s3, "ocr-outputs")
    assert index_key(prefix) not in s3.objects


async def test_partial_delete_keeps_the_index_entry():
    s3 = MemoryS3()
    prefix = s3.archive(1)
    rebuild_index(s3, "ocr-outputs")
    browser = StorageBrowser(configured(), client=s3)
    s3.delete_errors.add(prefix + "document.md")
    with pytest.raises(HTTPException):
        await browser.delete(prefix, f"{1:032x}")
    assert index_key(prefix) in s3.objects
    await browser.close()


def test_backfill_refuses_a_listing_outside_the_index():
    class Stray(MemoryS3):
        def list_objects_v2(self, **kwargs):
            if kwargs["Prefix"] != INDEX_ENTRIES:
                return super().list_objects_v2(**kwargs)
            keys = sorted(key for key in self.objects if key.startswith("parse/"))
            return {
                "Contents": [{"Key": key, "Size": 0} for key in keys],
                "IsTruncated": False,
            }

    s3 = Stray()
    s3.archive(1)
    before = set(s3.objects)
    with pytest.raises(ValueError):
        rebuild_index(s3, "ocr-outputs")
    assert set(s3.objects) == before


def test_backfill_survives_a_failed_prune_and_still_publishes_readiness():
    class Failing(MemoryS3):
        def delete_objects(self, **kwargs):
            raise TimeoutError("read timeout")

    s3 = Failing()
    s3.archive(1)
    orphan = index_key("gone.pdf__20260925T120000000000Z__Gone123_/")
    s3.objects[orphan] = b""
    assert rebuild_index(s3, "ocr-outputs") == 1
    assert INDEX_READY in s3.objects
    assert orphan in s3.objects


def test_job_codes_containing_double_underscores_sort_by_their_timestamp():
    prefix = "a.pdf__20260926T120000000000Z__38__XqSe/"
    assert sort_stamp(prefix) == "20260926T120000000000Z"
    assert index_archive(index_key(prefix)) == prefix


def test_backfill_replaces_an_entry_indexed_under_a_misread_stamp():
    s3 = MemoryS3()
    prefix = s3.current_archive(code="38__XqSe")
    stamp = sort_stamp(prefix).translate(_REVERSE_DIGITS)
    # Earlier releases read the stamp "38" from the code "38__XqSe".
    misread = index_key(prefix).replace(stamp, "38".translate(_REVERSE_DIGITS), 1)
    s3.objects[misread] = b""
    rebuild_index(s3, "ocr-outputs")
    assert misread not in s3.objects
    assert index_key(prefix) in s3.objects


def test_failed_backfill_does_not_publish_readiness():
    class Failing(MemoryS3):
        def put_object(self, **kwargs):
            if kwargs["Key"].startswith(INDEX_ENTRIES):
                raise RuntimeError("failed")
            return super().put_object(**kwargs)

    s3 = Failing()
    s3.archive(1)
    with pytest.raises(RuntimeError):
        rebuild_index(s3, "ocr-outputs")
    assert INDEX_READY not in s3.objects


def test_backfill_crosses_listing_boundaries_without_losing_prefixes():
    s3 = MemoryS3()
    prefixes = [s3.archive(i) for i in range(1, 401)]
    assert rebuild_index(s3, "ocr-outputs") == len(prefixes)
    assert {
        index_archive(key) for key in s3.objects if key.startswith(INDEX_ENTRIES)
    } == set(prefixes)


async def test_index_failure_happens_before_archive_reservation():
    from ocrserver.storage import ArtifactStore, ArtifactCapture
    from ocrserver.schemas import OcrResponse

    class Failing(MemoryS3):
        def put_object(self, **kwargs):
            if kwargs["Key"].startswith(INDEX_ENTRIES):
                raise RuntimeError("index unavailable")
            return super().put_object(**kwargs)

    s3 = Failing()
    store = ArtifactStore(configured(), client=s3)
    result = await store.save(
        ArtifactCapture("ocr", configured()),
        OcrResponse(result="text", task="ocr", processing_time_ms=1),
    )
    assert result.status == "failed"
    assert not s3.objects
    await store.close()
