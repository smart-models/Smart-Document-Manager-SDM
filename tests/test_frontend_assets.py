"""Offline checks for vendor reproducibility and path confinement."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_frontend_assets import verify_assets


def test_repository_vendor_assets_match_reviewed_releases():
    root = Path(__file__).resolve().parents[1] / "frontend"
    verified = verify_assets(root)
    assert "dist/assets/vendor/katex/katex.mjs" in verified
    assert "dist/assets/vendor/katex/katex.min.css" in verified
    assert set(verified) == {
        path.relative_to(root).as_posix()
        for path in (root / "dist/assets/vendor").rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("escape", ["../secret.js", "/outside.js"])
def test_vendor_manifest_rejects_escaping_paths(tmp_path, escape):
    manifest = {
        "schema_version": 1,
        "assets": [
            {
                "name": "sample",
                "version": "1",
                "path": escape,
                "origin": "https://example.com/release",
                "license": "MIT",
                "sha256": "0" * 64,
            }
        ],
    }
    (tmp_path / "vendor-manifest.json").write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(ValueError, match="inside frontend root"):
        verify_assets(tmp_path)


def test_vendor_manifest_detects_modified_content(tmp_path):
    manifest = {
        "schema_version": 1,
        "assets": [
            {
                "name": "sample",
                "version": "1",
                "path": "asset.js",
                "origin": "https://example.com/release",
                "license": "MIT",
                "sha256": hashlib.sha256(b"original").hexdigest(),
            }
        ],
    }
    (tmp_path / "vendor-manifest.json").write_text(json.dumps(manifest), "utf-8")
    (tmp_path / "asset.js").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_assets(tmp_path)
