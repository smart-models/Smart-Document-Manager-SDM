"""Offline checks for vendor reproducibility and path confinement."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_frontend_assets import verify_assets


def write_manifest(root: Path, path: str, sha256: str, license_file: str) -> None:
    """Write a one-asset manifest in the current schema."""
    manifest = {
        "schema_version": 2,
        "assets": [
            {
                "name": "sample",
                "version": "1",
                "path": path,
                "origin": "https://example.com/release",
                "license": "MIT",
                "license_file": license_file,
                "sha256": sha256,
            }
        ],
    }
    (root / "vendor-manifest.json").write_text(json.dumps(manifest), "utf-8")


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
    write_manifest(tmp_path, escape, "0" * 64, escape)
    with pytest.raises(ValueError, match="inside frontend root"):
        verify_assets(tmp_path)


def test_vendor_manifest_detects_modified_content(tmp_path):
    write_manifest(
        tmp_path, "asset.js", hashlib.sha256(b"original").hexdigest(), "asset.js"
    )
    (tmp_path / "asset.js").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_assets(tmp_path)


def test_vendor_manifest_requires_a_listed_licence_text(tmp_path):
    write_manifest(
        tmp_path, "asset.js", hashlib.sha256(b"code").hexdigest(), "LICENSE.txt"
    )
    (tmp_path / "asset.js").write_bytes(b"code")
    (tmp_path / "LICENSE.txt").write_bytes(b"unlisted licence")
    with pytest.raises(ValueError, match="licence text is not listed: asset.js"):
        verify_assets(tmp_path)
