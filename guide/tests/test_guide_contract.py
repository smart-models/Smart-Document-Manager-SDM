"""Keep standalone field guides aligned with the configuration they teach."""

from html.parser import HTMLParser
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
PAGES = (
    "getting-started",
    "cli",
    "environment",
    "architecture",
    "document-pipeline",
    "rag-chunking",
)


class GuideFacts(HTMLParser):
    """Collect configuration entries, resources and accessible navigation."""

    def __init__(self, text):
        super().__init__()
        self.entries = []
        self.assets = []
        self.navigation = []
        self.styles = []
        self.scripts = []
        self._text_tag = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-env-name" in attrs:
            self.entries.append(
                (
                    attrs.get("id"),
                    attrs["data-env-name"],
                    attrs.get("data-env-value"),
                    attrs.get("data-env-state"),
                )
            )
        if tag in {"script", "img", "iframe", "source", "link"}:
            value = attrs.get("src", attrs.get("href", ""))
            if value:
                self.assets.append(value)
        if tag == "a" and attrs.get("aria-current") == "page":
            self.navigation.append(attrs.get("href"))
        if tag in {"script", "style"}:
            self._text_tag = tag

    def handle_data(self, data):
        if self._text_tag == "style":
            self.styles.append(data)
        elif self._text_tag == "script":
            self.scripts.append(data)

    def handle_endtag(self, tag):
        if tag == self._text_tag:
            self._text_tag = None


def test_environment_entries_match_every_template_assignment_exactly():
    expected = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"(#\s*)?([A-Z][A-Z0-9_]*)=(.*)", line)
        if match:
            optional, name, value = match.groups()
            expected.append((name, name, value, "optional" if optional else "active"))
    facts = GuideFacts((ROOT / "guide/environment.html").read_text(encoding="utf-8"))
    assert len({entry[1] for entry in facts.entries}) == len(facts.entries)
    assert sorted(facts.entries) == sorted(expected)


def test_pages_embed_presentation_and_never_execute_network_or_persist_settings():
    for name in PAGES:
        text = (ROOT / f"guide/{name}.html").read_text(encoding="utf-8")
        facts = GuideFacts(text)
        assert facts.navigation == [f"{name}.html"]
        assert not re.search(r"__[A-Z][A-Z_]+__", text)
        assert all(asset.startswith("data:") for asset in facts.assets)
        styles = "".join(facts.styles)
        assert "data:font/woff2;base64," in styles
        assert not re.search(r"@import|url\(\s*['\"]?(?:https?:|//|\.\.?/)", styles)
        scripts = "".join(facts.scripts)
        assert not re.search(
            r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|localStorage|sessionStorage)\b",
            scripts,
        )


def test_all_authored_commands_keep_both_shells():
    for name in PAGES:
        text = (ROOT / f"guide/{name}.html").read_text(encoding="utf-8")
        assert text.count('data-shell="windows"') == text.count('data-shell="linux"')
