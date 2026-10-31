"""Image inventories must follow direct requirements instead of silently drifting."""

from pathlib import Path
import json
import shutil

import pytest

from scripts.verify_dependency_inventory import direct_pins, inventory_errors

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inventory_repo(tmp_path):
    for name in (
        "requirements",
        "compose.yml",
        "Dockerfile.vllm",
        "ocr-mcp-server/pyproject.toml",
    ):
        source, target = ROOT / name, tmp_path / name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return tmp_path


def test_committed_image_inventories_match_runtime_requirements():
    assert inventory_errors(ROOT) == []


def test_changed_direct_pin_requires_a_new_inventory(inventory_repo):
    path = inventory_repo / "requirements/api.txt"
    path.write_text(path.read_text().replace("httpx==0.28.1", "httpx==99.0.0"))
    assert any(
        "direct pin differs for httpx" in error
        for error in inventory_errors(inventory_repo)
    )


def test_modified_snapshot_and_changed_vllm_image_are_refused(inventory_repo):
    path = next((inventory_repo / "requirements/locks").glob("app-*.txt"))
    path.write_bytes(path.read_bytes() + b"made-up-package==1.0.0\n")
    compose = inventory_repo / "compose.yml"
    manifest = json.loads((inventory_repo / "requirements/inventory.json").read_text())
    compose.write_text(
        compose.read_text().replace(manifest["vllm"]["image"], "ocr-vllm:other")
    )
    errors = inventory_errors(inventory_repo)
    assert any("hash mismatch" in error for error in errors)
    assert any("default Compose image" in error for error in errors)


def test_changed_vllm_base_requires_a_new_inventory(inventory_repo):
    path = inventory_repo / "Dockerfile.vllm"
    path.write_text(path.read_text().replace("vllm/vllm-openai:", "vllm/other:"))
    assert any("base image" in error for error in inventory_errors(inventory_repo))


def test_unpinned_runtime_dependencies_cannot_escape_the_drift_check():
    with pytest.raises(ValueError, match="exact"):
        direct_pins(["fastapi>=0.115.6"])
