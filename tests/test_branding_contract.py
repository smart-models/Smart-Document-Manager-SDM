"""Regression tests for the product rename and its compatibility boundary."""

from html.parser import HTMLParser
from pathlib import Path
import re

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PRODUCT_NAME = "Smart Document Manager"
MARKDOWN_PRODUCT_NAME = "Smart Document Manager (SDM)"
COMPOSE_PROJECT_NAME = "smart-document-manager"
PRIVATE_NETWORK_NAME = "smart-document-manager-private"
STORAGE_NETWORK_NAME = "smart-document-manager-storage"
EXPECTED_CONTAINER_NAMES = (
    "smart-document-manager",
    "smart-document-mcp",
    "vllm-paddleocr",
    "document-storage-s3",
    "document-storage-gateway",
    "document-storage-init",
)
LEGACY_DISPLAY_NAMES = ("Personal Smart OCR",)
STABLE_COMPOSE_IDENTIFIERS = (
    "ocr-api:latest",
    "personal-smart-ocr-mcp:latest",
    "ocr-vllm:0.32.0-security",
    "ocr-vllm-model-cache",
    "ocr-autotune-profile",
    "ocr-paddlex-cache",
    "ocr-runtime-profile",
    "ocr-output-storage",
)


def _read(relative_path: str) -> str:
    """Read one repository file as UTF-8 text."""
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


class _HtmlTitles(HTMLParser):
    """Collect visible text inside HTML H1 elements, including nested markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titles = []
        self.parts = None

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self.parts = []

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self.parts is not None:
            self.titles.append(" ".join("".join(self.parts).split()))
            self.parts = None


def _readme_titles(markdown: str) -> list[str]:
    """Find the README's visible HTML or ATX level-one title outside code fences."""
    parser = _HtmlTitles()
    parser.feed(markdown)
    prose = re.sub(r"^```.*?^```", "", markdown, flags=re.MULTILINE | re.DOTALL)
    atx = re.findall(r"^#\s+(.+?)\s*#*\s*$", prose, re.MULTILINE)
    return parser.titles + atx


def test_root_markdown_title_uses_the_approved_product_name():
    assert _readme_titles(_read("README.md")) == [MARKDOWN_PRODUCT_NAME]


@pytest.mark.parametrize(
    "markup",
    [
        "# Smart Document Manager (SDM)\n\n## Introduction",
        '<h1 align="center">Smart Document Manager (SDM)</h1>',
        "<h1>Smart Document <em>Manager</em> (SDM)</h1>",
    ],
)
def test_branding_title_accepts_both_heading_formats(markup):
    assert _readme_titles(markup) == [MARKDOWN_PRODUCT_NAME]


@pytest.mark.parametrize(
    "markup",
    [
        "## Smart Document Manager (SDM)",
        "<h1>Personal Smart OCR</h1>",
        '<img alt="Smart Document Manager (SDM)"><h1>Wrong title</h1>',
        "<h1>Smart Document Manager (SDM)</h1><h1>Another title</h1>",
    ],
)
def test_branding_title_does_not_accept_missing_or_wrong_titles(markup):
    assert _readme_titles(markup) != [MARKDOWN_PRODUCT_NAME]


def test_compose_uses_new_project_containers_and_networks_without_renaming_volumes():
    compose = _read("compose.yml")

    assert compose.splitlines()[0] == f"name: {COMPOSE_PROJECT_NAME}"
    assert f"    name: {PRIVATE_NETWORK_NAME}" in compose
    assert f"    name: {STORAGE_NETWORK_NAME}" in compose
    assert "name: ocr-server" not in compose
    for container_name in EXPECTED_CONTAINER_NAMES:
        assert f"    container_name: {container_name}" in compose
    for identifier in STABLE_COMPOSE_IDENTIFIERS:
        assert identifier in compose


def test_root_runtime_copy_uses_the_full_name_without_the_acronym():
    paths = (
        ".env.example",
        "Dockerfile",
        "stack.ps1",
        "stack.sh",
        "scripts/README.md",
    )

    for relative_path in paths:
        text = _read(relative_path)
        assert PRODUCT_NAME in text, relative_path
        assert "(SDM)" not in text, relative_path
        for legacy_name in LEGACY_DISPLAY_NAMES:
            assert legacy_name not in text, relative_path


def test_field_guides_use_current_product_branding():
    pages = sorted((REPOSITORY_ROOT / "guide").glob("*.html"))
    assert pages, "No field guide HTML found"
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert PRODUCT_NAME in text, page.name
        for legacy_name in LEGACY_DISPLAY_NAMES:
            assert legacy_name not in text, page.name
