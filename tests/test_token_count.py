"""Content token counting is best-effort and must never fail a parse request."""

import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG

from ocr_server import create_app
from ocrserver.config import Settings, load_settings
from ocrserver.schemas import Block
from ocrserver.token_count import ContentTokenCounter, resolve_tokenizer_path

COMMIT = "c5630abae1d940eafe0697512a0325494b02ab42"
MODEL_DIR = "models--PaddlePaddle--PaddleOCR-VL-1.6"


class WordTokenizer:
    """One token per whitespace-separated word; enough to verify wiring."""

    def encode(self, text, *, add_special_tokens=True):
        return SimpleNamespace(ids=text.split())


def make_cache(
    root: Path,
    *,
    snapshot: str = COMMIT,
    ref: tuple[str, str] | None = None,
    tokenizer: bool = True,
) -> Path:
    repo = root / "hub" / MODEL_DIR
    directory = repo / "snapshots" / snapshot
    directory.mkdir(parents=True)
    if tokenizer:
        (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    if ref is not None:
        name, commit = ref
        (repo / "refs").mkdir()
        (repo / "refs" / name).write_text(commit, encoding="utf-8")
    return root


def make_counter(root: Path, loader):
    calls = []

    def recording_loader(path):
        calls.append(path)
        return loader(path)

    return ContentTokenCounter(str(root), COMMIT, loader=recording_loader), calls


def test_resolve_tokenizer_path_direct_commit(tmp_path):
    root = make_cache(tmp_path)
    expected = root / "hub" / MODEL_DIR / "snapshots" / COMMIT / "tokenizer.json"
    assert resolve_tokenizer_path(str(root), COMMIT) == expected


def test_resolve_tokenizer_path_follows_refs(tmp_path):
    root = make_cache(tmp_path, ref=("main", COMMIT))
    expected = root / "hub" / MODEL_DIR / "snapshots" / COMMIT / "tokenizer.json"
    assert resolve_tokenizer_path(str(root), "main") == expected


def test_resolve_tokenizer_path_missing(tmp_path):
    make_cache(tmp_path, tokenizer=False)
    assert resolve_tokenizer_path(str(tmp_path), COMMIT) is None
    assert resolve_tokenizer_path(str(tmp_path), "unknown-revision") is None
    assert resolve_tokenizer_path("", COMMIT) is None
    assert resolve_tokenizer_path(str(tmp_path), "") is None


def test_counter_disabled_leaves_blocks_untouched():
    calls = []
    counter = ContentTokenCounter("", COMMIT, loader=lambda path: calls.append(path))
    block = Block(type="text", bbox=[], content="hello world", order=0)
    assert counter.annotate([block]) is None
    assert block.content_tokens is None
    assert calls == []


def test_annotate_counts_content_and_caches_tokenizer(tmp_path):
    make_cache(tmp_path)
    counter, calls = make_counter(tmp_path, loader=lambda _: WordTokenizer())
    blocks = [
        Block(type="text", bbox=[], content="hello world", order=0),
        Block(type="image", bbox=[], content="", order=1),
    ]
    usage = counter.annotate(blocks)
    assert usage is not None
    assert usage.tokenizer == f"PaddlePaddle/PaddleOCR-VL-1.6@{COMMIT}"
    assert (usage.content_tokens, usage.blocks) == (2, 2)
    assert [block.content_tokens for block in blocks] == [2, 0]
    expected = tmp_path / "hub" / MODEL_DIR / "snapshots" / COMMIT / "tokenizer.json"
    assert calls == [expected]

    # A later page reuses the loaded tokenizer instead of reading the file again.
    assert counter.annotate([blocks[0]]).content_tokens == 2
    assert len(calls) == 1


def test_counter_enabled_requires_cache_and_revision(tmp_path):
    assert not ContentTokenCounter("", COMMIT).enabled
    assert not ContentTokenCounter(str(tmp_path), "").enabled
    assert ContentTokenCounter(str(tmp_path), COMMIT).enabled


@pytest.fixture
def caplog(caplog, monkeypatch):
    """Let caplog's root handler see ocrserver, which create_app stops propagating."""
    monkeypatch.setattr(logging.getLogger("ocrserver"), "propagate", True)
    return caplog


def test_missing_file_disables_once(tmp_path, caplog):
    counter = ContentTokenCounter(str(tmp_path), COMMIT, loader=lambda path: None)
    block = Block(type="text", bbox=[], content="hello", order=0)
    with caplog.at_level(logging.WARNING, logger="ocrserver.token_count"):
        assert counter.annotate([block]) is None
        assert counter.annotate([block]) is None
    assert block.content_tokens is None
    warnings = [
        record
        for record in caplog.records
        if "Token counting disabled" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_loader_failure_disables_once(tmp_path, caplog):
    make_cache(tmp_path)
    calls = []

    def loader(path):
        calls.append(path)
        raise RuntimeError("no tokenizers wheel")

    counter = ContentTokenCounter(str(tmp_path), COMMIT, loader=loader)
    block = Block(type="text", bbox=[], content="hello", order=0)
    with caplog.at_level(logging.WARNING, logger="ocrserver.token_count"):
        assert counter.annotate([block]) is None
        assert counter.annotate([block]) is None
    assert block.content_tokens is None
    assert len(calls) == 1
    failures = [
        record for record in caplog.records if "could not load" in record.getMessage()
    ]
    assert len(failures) == 1


def test_encode_failure_keeps_page_unannotated(tmp_path):
    make_cache(tmp_path)

    class BrokenTokenizer:
        def encode(self, text, *, add_special_tokens=True):
            raise ValueError("undecodable content")

    counter, _ = make_counter(tmp_path, loader=lambda _: BrokenTokenizer())
    blocks = [
        Block(type="text", bbox=[], content="first", order=0),
        Block(type="text", bbox=[], content="second", order=1),
    ]
    assert counter.annotate(blocks) is None
    assert [block.content_tokens for block in blocks] == [None, None]


def test_settings_read_token_counting_environment():
    settings = load_settings(
        {"OCR_HF_CACHE_DIR": "/cache", "OCR_VLLM_MODEL_REVISION": "main"}, None
    )
    assert settings.hf_cache_dir == "/cache"
    assert settings.vllm_model_revision == "main"
    assert Settings().hf_cache_dir == ""
    assert Settings().vllm_model_revision == COMMIT


class TokenPipeline:
    """Emit text and image blocks while recording token-annotation parse calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def parse_document(
        self, images, lanes, page_timeout, timeout, *, close_images=False
    ):
        self.calls += 1
        if close_images:
            for image in images:
                if hasattr(image, "close"):
                    image.close()
        return [
            {
                "markdown": "# Fixture",
                "blocks": [
                    Block(
                        type="text", bbox=[0, 0, 1, 1], content="Hello world", order=0
                    ),
                    Block(type="image", bbox=[0, 0, 1, 1], content="", order=1),
                ],
                "images": [],
                "width": 1,
                "height": 1,
                "elapsed": 0.001,
            }
            for _ in images
        ]


async def test_parse_reports_content_token_usage(tmp_path):
    make_cache(tmp_path)
    counter, calls = make_counter(tmp_path, loader=lambda _: WordTokenizer())
    app = create_app(
        Settings(), TokenPipeline(), FakeRecognizer(), token_counter=counter
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse", json={"image_base64": PNG, "output": "json"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["usage"] == {
        "tokenizer": f"PaddlePaddle/PaddleOCR-VL-1.6@{COMMIT}",
        "content_tokens": 2,
        "blocks": 2,
    }
    assert [block["content_tokens"] for block in body["pages"][0]["blocks"]] == [2, 0]
    assert len(calls) == 1


async def test_parse_usage_is_null_when_counting_disabled():
    app = create_app(Settings(), FakePipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/parse", json={"image_base64": PNG, "output": "json"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["usage"] is None
    assert body["pages"][0]["blocks"] == []
