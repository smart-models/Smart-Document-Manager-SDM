"""Verify deterministic calibration pixels, metadata and PDF bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from calibration import generate


def test_generation_repeats_pixels_ground_truth_and_pdf_bytes(tmp_path, monkeypatch):
    first = tmp_path / "one"
    second = tmp_path / "two"
    # An arbitrary cwd cannot change font resolution to an installed font.
    monkeypatch.chdir(tmp_path)
    items = generate.generate_calibration_set(first)
    generate.generate_calibration_set(second)
    assert (first / "ground_truth.json").read_bytes() == (
        second / "ground_truth.json"
    ).read_bytes()
    assert generate.load_ground_truth(first / "ground_truth.json") == items
    for item in items:
        if item.kind == "pdf":
            assert (first / item.path).read_bytes() == (second / item.path).read_bytes()
        else:
            with (
                Image.open(first / item.path) as left,
                Image.open(second / item.path) as right,
            ):
                assert left.size == right.size
                assert left.tobytes() == right.tobytes()


def test_missing_bundled_font_does_not_use_system_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "FONT_PATH", tmp_path / "missing.ttf")
    with pytest.raises(ValueError, match="Bundled calibration font"):
        generate._font()


@pytest.mark.parametrize(
    "data,field",
    [
        ({}, "list"),
        ([], "list"),
        ([None], "fixture 0"),
        ([{"name": 1}], "name"),
        (
            [
                {
                    "name": "x",
                    "kind": "text",
                    "path": "x.png",
                    "ground_truth": "<p>!!!</p>",
                }
            ],
            "ground_truth",
        ),
        (
            [{"name": "x", "kind": "bad", "path": "x.png", "ground_truth": "text"}],
            "kind",
        ),
        (
            [{"name": "x", "kind": "text", "path": "../x.png", "ground_truth": "text"}],
            "path",
        ),
        (
            [{"name": "x", "kind": "text", "path": "C:/x.png", "ground_truth": "text"}],
            "path",
        ),
        (
            [{"name": "x", "kind": "text", "path": "x.png", "ground_truth": "text"}]
            * 2,
            "duplicate",
        ),
    ],
)
def test_ground_truth_validation_is_contextual(tmp_path, data, field):
    (tmp_path / "x.png").write_bytes(b"fixture")
    manifest = tmp_path / "ground_truth.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=field) as error:
        generate.load_ground_truth(manifest)
    assert str(manifest) in str(error.value)


def test_manifest_hashes_match_preserved_fixtures_and_bundled_font():
    root = Path(generate.__file__).parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entries = [manifest["font"], *manifest["fixtures"]["files"]]
    for entry in entries:
        assert (
            hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
            == entry["sha256"]
        )
    font = manifest["font"]
    assert (
        hashlib.sha256((root / font["license"]).read_bytes()).hexdigest()
        == font["license_sha256"]
    )
