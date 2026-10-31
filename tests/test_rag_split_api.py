"""The read-only split endpoint: measured fragments for one aggregated group."""

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_storage_browser import MemoryS3, configured

from ocr_server import create_app
from ocrserver.archive_names import prefix_from_key
from ocrserver.config import Settings
from ocrserver.schemas import (
    RagRefinementOptions,
    StorageRagRefineRequest,
    StorageRagSplitRequest,
)
from ocrserver.storage_browser import StorageBrowser

JOB = "a" * 32
PREFIX = f"parse/2026/09/26/{JOB}/"
SOURCE_KEY = PREFIX + "json_rag.json"

TABLE = (
    "<table>\n<tr><th>Nome</th><th>Valore</th></tr>\n"
    + "".join(
        f"<tr><td>riga {index}</td><td>{index}</td></tr>\n" for index in range(20)
    )
    + "</table>\n"
)
SOURCE = {
    "metadata": {"document": "default"},
    "chunks": [{"id": 1, "text": TABLE, "token_count": 900, "page": 1}],
}


def request(**over):
    body = {
        "source_key": SOURCE_KEY,
        "source_sha256": "a" * 64,
        "options": RagRefinementOptions(min_tokens=10, max_tokens=120),
        "group": [0],
    }
    body.update(over)
    return StorageRagSplitRequest(**body)


def refine_request(**over):
    body = {
        "source_key": SOURCE_KEY,
        "source_sha256": "a" * 64,
        "execution_id": uuid.uuid4(),
        "created_at": datetime.now(UTC),
        "options": RagRefinementOptions(min_tokens=10, max_tokens=120),
        "groups": [[0]],
    }
    body.update(over)
    return StorageRagRefineRequest(**body)


def monkeypatch_counter(browser, counter):
    browser._rag_counter = lambda: counter


@pytest.fixture
def browser_with_source(char_counter, monkeypatch):
    created = []

    def build(source, *, sha):
        import ocrserver.storage_browser as module

        monkeypatch.setattr(module.hashlib, "sha256", lambda _data: _Digest(sha))
        s3 = MemoryS3()
        s3.objects[SOURCE_KEY] = json.dumps(source).encode()
        prefix = prefix_from_key(SOURCE_KEY)
        manifest = {
            "complete": True,
            "kind": "parse",
            "job_id": JOB,
            "created_at": "2026-09-26T00:00:00+00:00",
            "artifacts": [],
        }
        s3.objects[prefix + "manifest.json"] = json.dumps(manifest).encode()
        browser = StorageBrowser(configured(api_tokens="api-secret:test"), client=s3)
        monkeypatch_counter(browser, char_counter)
        created.append(browser)
        return browser, s3

    yield build
    for browser in created:
        asyncio.run(browser.close())


class _Digest:
    def __init__(self, value):
        self._value = value

    def hexdigest(self):
        return self._value


@pytest.fixture
def client():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    with TestClient(app) as test_client:
        yield test_client


BENCH_TEXT = "parola di prova " * 20_000


@pytest.fixture
def client_for_benchmark():
    """An HTTP client with a real-tokenizer browser, or a skip without the cache."""
    cache_dir = os.environ.get("OCR_HF_CACHE_DIR", Settings().hf_cache_dir)
    settings = configured(api_tokens="api-secret:test", hf_cache_dir=cache_dir)
    source = {
        "metadata": {"document": "default"},
        "chunks": [{"id": 1, "text": BENCH_TEXT, "token_count": 100_000, "page": 1}],
    }
    body = json.dumps(source).encode()
    s3 = MemoryS3()
    s3.objects[SOURCE_KEY] = body
    s3.objects[PREFIX + "manifest.json"] = json.dumps(
        {
            "complete": True,
            "kind": "parse",
            "job_id": JOB,
            "created_at": "2026-09-26T00:00:00+00:00",
            "artifacts": [],
        }
    ).encode()
    browser = StorageBrowser(settings, client=s3)
    try:
        counter = browser._rag_counter()
        counter.count("warm up")  # forces the real cache load
    except HTTPException as error:
        asyncio.run(browser.close())
        if error.status_code == 503:
            pytest.skip("real tokenizer cache is not available")
        raise
    app = create_app(
        settings, FakePipeline(), FakeRecognizer(), storage_browser=browser
    )
    payload = {
        "source_key": SOURCE_KEY,
        "source_sha256": hashlib.sha256(body).hexdigest(),
        "options": {"min_tokens": 100, "max_tokens": 512},
        "group": [0],
    }
    with TestClient(app, headers={"Authorization": "Bearer api-secret"}) as http:
        yield http, payload
    asyncio.run(browser.close())


@pytest.fixture
def split_request_100k_tokens(client_for_benchmark):
    http, payload = client_for_benchmark

    def measure():
        start = time.perf_counter()
        response = http.post("/storage/rag/split", json=payload)
        assert response.status_code == 200, response.text
        return time.perf_counter() - start

    measure()  # warm the tokenizer before the measured calls
    return measure


@pytest.mark.asyncio
async def test_split_returns_measured_fragments(browser_with_source):
    browser, client = browser_with_source(SOURCE, sha="a" * 64)
    result = await browser.split_rag(request())
    assert len(result.fragments) >= 2
    assert all(
        fragment.token_count and fragment.token_count <= 120
        for fragment in result.fragments
    )
    assert all(
        fragment.member_token_counts == [fragment.token_count]
        for fragment in result.fragments
    )
    assert result.fragments[0].notes[0].code == "table_split"


@pytest.mark.asyncio
async def test_split_notes_carry_real_parameters(browser_with_source):
    # O1-4/FR-017: the split response's notes carry the same per-code values the
    # save path computes, not the null placeholders they used to.
    browser, client = browser_with_source(SOURCE, sha="a" * 64)
    result = await browser.split_rag(request())
    note = result.fragments[0].notes[0]
    assert note.code == "table_split"
    values = {parameter.name: parameter.value for parameter in note.parameters}
    assert values["detail"]
    assert values["rows"] is not None


@pytest.mark.asyncio
async def test_split_rejects_a_changed_source(browser_with_source):
    browser, client = browser_with_source(SOURCE, sha="b" * 64)
    with pytest.raises(HTTPException) as raised:
        await browser.split_rag(request())
    assert (
        raised.value.status_code == 409
        and raised.value.detail["code"] == "rag_source_changed"
    )


@pytest.mark.asyncio
async def test_split_reports_a_missing_tokenizer(browser_with_source, monkeypatch):
    browser, client = browser_with_source(SOURCE, sha="a" * 64)
    monkeypatch.setattr(
        browser,
        "_rag_counter",
        lambda: (_ for _ in ()).throw(
            HTTPException(status_code=503, detail={"code": "rag_tokenizer_unavailable"})
        ),
    )
    with pytest.raises(HTTPException) as raised:
        await browser.split_rag(request())
    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_split_of_a_group_without_a_token_count_still_measures(
    browser_with_source,
):
    source = {
        "metadata": {"document": "default"},
        "chunks": [{"id": 1, "text": "Testo senza conteggio. " * 40, "page": 1}],
    }
    browser, client = browser_with_source(source, sha="a" * 64)
    result = await browser.split_rag(request(group=[0]))
    assert result.fragments and all(
        fragment.status != "unknown" for fragment in result.fragments
    )


@pytest.mark.asyncio
async def test_fragments_over_the_maximum_without_any_split_point_are_noted(
    browser_with_source,
):
    source = {
        "metadata": {"document": "default"},
        "chunks": [{"id": 1, "text": "$$x^2+y^2=z^2$$", "page": 1}],
    }
    browser, client = browser_with_source(source, sha="a" * 64)
    result = await browser.split_rag(
        request(options=RagRefinementOptions(min_tokens=1, max_tokens=3))
    )
    assert [note.code for note in result.fragments[0].notes] == ["math_atomic"]
    assert result.fragments[0].status == "above"


@pytest.mark.asyncio
async def test_a_source_without_text_reports_an_unknown_count(browser_with_source):
    source = {
        "metadata": {"document": "default"},
        "chunks": [{"id": 1, "text": "", "token_count": None, "page": 1}],
    }
    browser, client = browser_with_source(source, sha="a" * 64)
    result = await browser.split_rag(request())
    assert result.fragments[0].status == "unknown"
    assert "token_count_unknown" in [note.code for note in result.fragments[0].notes]


def test_a_body_over_two_megabytes_is_refused(client):
    """NFR-008/FR-022: stesso tetto e stesso codice di /storage/rag/refine."""
    oversized = b'{"source_key":"' + b"a" * (2 * 1024 * 1024) + b'"}'
    assert client.post("/storage/rag/split", content=oversized).status_code == 413
    assert (
        client.post("/storage/rag/split", content=oversized).json()["detail"]["code"]
        == "rag_plan_too_large"
    )


def test_split_latency_budget(client_for_benchmark, split_request_100k_tokens):
    """NFR-001/NFR-002: p95 ≤ 5 s con tokenizer caldo, budget rispettato."""
    timings = [split_request_100k_tokens() for _ in range(20)]
    assert sorted(timings)[18] <= 5.0


@pytest.mark.asyncio
async def test_save_and_split_measures_fragments(
    browser_with_source, char_counter, monkeypatch
):
    import hashlib
    import ocrserver.storage_browser as module

    source = {
        "metadata": {"document": "default"},
        "chunks": [
            {"id": 1, "text": TABLE, "token_count": 900, "page": 1},
            {"id": 2, "text": "coda", "token_count": 4, "page": 1},
        ],
    }
    real_sha256 = hashlib.sha256
    browser, client = browser_with_source(source, sha="a" * 64)
    monkeypatch_counter(browser, char_counter)
    # The fixture patches the module's hashlib for the read path; restore the
    # real digest so the writer can sign its requests.
    monkeypatch.setattr(module.hashlib, "sha256", real_sha256)
    browser._writer_factory = lambda: client
    manifest = json.loads(client.objects[PREFIX + "manifest.json"])
    manifest["artifacts"] = [{"name": "json_rag.json", "key": SOURCE_KEY, "size": 1}]
    client.objects[PREFIX + "manifest.json"] = json.dumps(manifest).encode()
    saved = await browser.refine_rag(
        refine_request(
            groups=[[[0, 0, 20], [0, 20, len(TABLE)]], [1]],
            overrides=[[], []],
            source_sha256=real_sha256(json.dumps(source).encode()).hexdigest(),
        )
    )
    assert saved["chunk_count"] == 2
    assert any(key.startswith(PREFIX + "json_rag_") for key in client.objects)


@pytest.mark.asyncio
async def test_an_empty_group_writes_nothing(browser_with_source):
    browser, client = browser_with_source(SOURCE, sha="a" * 64)
    browser._writer_factory = lambda: client
    manifest = json.loads(client.objects[PREFIX + "manifest.json"])
    manifest["artifacts"] = [{"name": "json_rag.json", "key": SOURCE_KEY, "size": 1}]
    client.objects[PREFIX + "manifest.json"] = json.dumps(manifest).encode()
    before = set(client.objects)
    with pytest.raises(HTTPException) as raised:
        await browser.refine_rag(refine_request(groups=[[0], []]))
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "rag_invalid_plan"
    assert set(client.objects) == before  # nessun file scritto


@pytest.mark.asyncio
async def test_save_and_split_without_a_tokenizer_writes_nothing(
    browser_with_source, monkeypatch
):
    browser, client = browser_with_source(SOURCE, sha="a" * 64)
    monkeypatch.setattr(
        browser,
        "_rag_counter",
        lambda: (_ for _ in ()).throw(
            HTTPException(status_code=503, detail={"code": "rag_tokenizer_unavailable"})
        ),
    )
    before = set(client.objects)
    with pytest.raises(HTTPException) as raised:
        await browser.refine_rag(
            refine_request(groups=[[[0, 0, 20], [0, 20, len(TABLE)]]], overrides=[[]])
        )
    assert raised.value.status_code == 503
    assert set(client.objects) == before  # nessun file scritto
