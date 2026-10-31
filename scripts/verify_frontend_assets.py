"""Check vendored frontend assets against the reviewed local manifest.

Run from any directory with Python 3.11; no network or project dependencies
are needed. The manifest records reproducibility, not a security signature.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def verify_assets(frontend_root: Path) -> list[str]:
    """Validate vendor metadata and hashes, rejecting paths outside the root."""
    root = frontend_root.resolve()
    manifest = json.loads((root / "vendor-manifest.json").read_text("utf-8"))
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("assets"), list)
        or not manifest["assets"]
    ):
        raise ValueError("Invalid vendor manifest schema")
    verified = []
    required = {"name", "version", "path", "origin", "sha256", "license"}
    for entry in manifest["assets"]:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key] for key in required
        ):
            raise ValueError("Invalid vendor asset metadata")
        relative = Path(entry["path"])
        target = (root / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(root):
            raise ValueError("Vendor asset path must stay inside frontend root")
        if entry["path"] in verified:
            raise ValueError("Duplicate vendor asset path")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != entry["sha256"]:
            raise ValueError(f"Vendor asset integrity mismatch: {entry['path']}")
        verified.append(entry["path"])
    return verified


def main() -> None:
    """Verify the checkout, or an explicitly supplied frontend root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frontend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "frontend",
    )
    args = parser.parse_args()
    for asset in verify_assets(args.frontend_root):
        print(f"Verified {asset}")


if __name__ == "__main__":
    main()
