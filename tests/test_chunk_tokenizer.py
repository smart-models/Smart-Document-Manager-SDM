"""Mandatory Paddle counts are isolated from legacy best-effort OCR state."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ocrserver.chunking.errors import ChunkError
from ocrserver.chunking.tokenizer import PaddleTokenizer

COMMIT = "c5630abae1d940eafe0697512a0325494b02ab42"
REPO = "PaddlePaddle/PaddleOCR-VL-1.6"


def cache_file(root: Path) -> Path:
    path = root / "hub" / "models--PaddlePaddle--PaddleOCR-VL-1.6"
    snapshot = path / "snapshots" / COMMIT
    snapshot.mkdir(parents=True)
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "refs").mkdir()
    (path / "refs" / "main").write_text(COMMIT, encoding="utf-8")
    return snapshot / "tokenizer.json"


class UnicodeTokenizer:
    """Encode one token per Unicode code point with exact character offsets."""

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        return SimpleNamespace(
            ids=list(range(len(text))),
            offsets=[(index, index + 1) for index in range(len(text))],
        )


def test_local_paddle_identity_uses_resolved_commit_and_codepoint_offsets(
    tmp_path,
):
    cache_file(tmp_path)
    tokenizer = PaddleTokenizer(
        str(tmp_path), "main", loader=lambda _: UnicodeTokenizer()
    )
    assert tokenizer.count("é😀a") == 3
    assert tokenizer.offsets("é😀a") == [(0, 1), (1, 2), (2, 3)]
    assert tokenizer.identity == {
        "repo": REPO,
        "revision": COMMIT,
        "sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "library_version": "0.23.2",
    }
    tokenizer.assert_compatible(None)
    tokenizer.assert_compatible(f"{REPO}@main")
    tokenizer.assert_compatible(f"{REPO}@{COMMIT}")


def test_failed_local_load_can_recover_without_process_restart(tmp_path):
    tokenizer = PaddleTokenizer(
        str(tmp_path), COMMIT, loader=lambda _: UnicodeTokenizer()
    )
    with pytest.raises(ChunkError) as raised:
        tokenizer.count("first")
    assert (raised.value.code, raised.value.status_code) == (
        "tokenizer_unavailable",
        503,
    )
    cache_file(tmp_path)
    assert tokenizer.count("next") == 4


def test_encoding_failure_does_not_disable_subsequent_rag_requests(tmp_path):
    class TemporaryFailure(UnicodeTokenizer):
        def encode(self, text, *, add_special_tokens):
            if text == "bad input":
                raise ValueError("sensitive text must not appear in domain errors")
            return super().encode(text, add_special_tokens=add_special_tokens)

    cache_file(tmp_path)
    tokenizer = PaddleTokenizer(
        str(tmp_path), COMMIT, loader=lambda _: TemporaryFailure()
    )
    with pytest.raises(ChunkError) as raised:
        tokenizer.count("bad input")
    assert (raised.value.code, raised.value.status_code) == (
        "tokenization_failed",
        422,
    )
    assert "sensitive" not in str(raised.value.to_detail())
    assert tokenizer.count("next") == 4


@pytest.mark.parametrize(
    "label",
    [
        f"{REPO}@{'a' * 40}",
        "other/model@main",
        f"{REPO}@../../secret",
        f"{REPO}@/absolute/path",
        f"{REPO}@refs\\main",
        f"{REPO}@",
    ],
)
def test_incompatible_or_path_shaped_labels_are_rejected(tmp_path, label):
    cache_file(tmp_path)
    tokenizer = PaddleTokenizer(
        str(tmp_path), COMMIT, loader=lambda _: UnicodeTokenizer()
    )
    with pytest.raises(ChunkError) as raised:
        tokenizer.assert_compatible(label)
    assert raised.value.code == "tokenizer_mismatch"
