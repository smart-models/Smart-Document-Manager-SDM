"""Every version string SDM reports agrees with the one constant that owns it.

Two constants own the reported versions:

* ``APP_VERSION`` (``ocrserver/schemas.py``, re-exported by ``ocr_server``) for
  the ``ocr-api`` service: ``/health``, the OpenAPI document, the
  ``ocr_server_info`` Prometheus metric, archive ``manifest.json`` files and
  the ``ocr-api`` image label;
* ``ocr_mcp.__version__`` for the MCP server: the wheel metadata in
  ``ocr-mcp-server/pyproject.toml`` and the ``ocr-mcp`` image label.

The checks compare surfaces with their constant and never pin a literal, so a
release edits the constant (and the Dockerfile ARG or ``pyproject.toml`` it
cannot import) without touching this file.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY
from test_api_storage import archive_files
from test_chunk_api import TextPipeline
from test_mcp_schema import FakeRecognizer
from test_mcp_tools import PNG
from test_storage import MemoryS3, configured

import ocr_mcp
import ocr_server
from ocr_server import create_app
from ocrserver.config import Settings
from ocrserver.storage import ArtifactStore

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def dockerfile_version_arg(stage: str) -> str:
    """Return the SMART_DOCUMENT_MANAGER_VERSION default declared in one stage.

    ARG values are stage-scoped in a multi-stage Dockerfile, so each runtime
    target (``ocr-api``, ``ocr-mcp``) carries its own default and image label.
    """
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = re.split(r"(?m)^FROM\s", text)
    body = next(part for part in stages if re.search(rf"\bAS {stage}\s*$", part, re.M))
    return re.search(r"(?m)^ARG SMART_DOCUMENT_MANAGER_VERSION=(\S+)$", body).group(1)


@pytest.mark.parametrize(
    "name,value",
    [
        ("APP_VERSION", ocr_server.APP_VERSION),
        ("ocr_mcp.__version__", ocr_mcp.__version__),
    ],
)
def test_owned_versions_are_plain_semver(name, value):
    assert SEMVER.fullmatch(value), f"{name}={value!r} is not X.Y.Z"


async def test_health_and_openapi_report_app_version():
    app = create_app(Settings(), TextPipeline(), FakeRecognizer())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as http:
        health = (await http.get("/health")).json()
        openapi = (await http.get("/openapi.json")).json()
    assert health["version"] == ocr_server.APP_VERSION
    assert openapi["info"]["version"] == ocr_server.APP_VERSION


async def test_server_info_metric_reports_app_version():
    app = create_app(Settings(), TextPipeline(), FakeRecognizer())
    async with app.router.lifespan_context(app):
        version = REGISTRY.get_sample_value(
            "ocr_server_info",
            {"version": ocr_server.APP_VERSION, "model": ocr_server.MODEL_NAME},
        )
    assert version == 1.0


async def test_archive_manifest_reports_app_version():
    settings, client = configured(), MemoryS3()
    store = ArtifactStore(settings, client=client)
    app = create_app(settings, TextPipeline(), FakeRecognizer(), artifact_store=store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.post("/ocr", json={"image_base64": PNG})
    finally:
        await store.close()
    assert response.status_code == 200, response.text
    manifest = json.loads(archive_files(client)["manifest.json"])
    assert manifest["application_version"] == ocr_server.APP_VERSION


def test_ocr_api_image_label_matches_app_version():
    assert dockerfile_version_arg("ocr-api") == ocr_server.APP_VERSION


def test_mcp_wheel_and_image_label_match_package_version():
    pyproject = tomllib.loads(
        (ROOT / "ocr-mcp-server/pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == ocr_mcp.__version__
    assert dockerfile_version_arg("ocr-mcp") == ocr_mcp.__version__
