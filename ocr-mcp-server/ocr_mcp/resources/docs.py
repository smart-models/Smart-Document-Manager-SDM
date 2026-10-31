"""Teaching and reference documents, published as MCP resources.

These are static files inside this package, never paths reaching out of it: a
container image copies the package and nothing else, so ``../../docs/`` would
resolve to a file that exists in a checkout and is absent from every deployed
server. When a document duplicates one that lives elsewhere in the repo, copy
it in and add a test comparing the copy byte-for-byte against its original —
the cost of copying is drift, and that test is how you pay it back.

Each description summarises its own document: it is what a client listing
resources sees to tell the documents apart.

The field guides are self-contained HTML pages for people, about 85% CSS,
script and embedded fonts. Agents get their text instead (``html_to_text``);
the packaged HTML files stay byte-identical to their originals.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from mcp.server import MCPServer

GUIDE_SUFFIX = " Served as text extracted from the offline HTML page."

# uri -> (file_name, title, description). The resource ``name`` is derived from
# the file name at registration time (``read_concepts_data_model`` for
# ``concepts-data-model.md``).
DOC_RESOURCES: dict[str, tuple[str, str, str]] = {
    "docs://ocr/skill": (
        "skill.md",
        "OCR operating skill",
        "Tool routing, explicit intent, safe defaults and recovery rules.",
    ),
    "docs://ocr/skill-readme": (
        "skill-readme.md",
        "OCR skill installation and maintenance",
        "Installing both skills, their boundaries and document parity checks.",
    ),
    "docs://ocr/reference": (
        "reference.md",
        "OCR tool reference",
        "Input locality, task selection, output shapes, limits and error recovery.",
    ),
    "docs://ocr/playbooks": (
        "playbooks.md",
        "OCR extraction playbooks",
        "Ordered document extraction, focused recognition and failure diagnosis.",
    ),
    "docs://guide/getting-started": (
        "guide/getting-started.html",
        "Getting started field guide",
        (
            "Offline first-start walkthrough: prerequisites, "
            "local tokens, readiness and first extraction." + GUIDE_SUFFIX
        ),
    ),
    "docs://guide/cli": (
        "guide/cli.html",
        "CLI field guide",
        (
            "Offline PowerShell/Bash command preview, daily "
            "operations and recovery boundaries." + GUIDE_SUFFIX
        ),
    ),
    "docs://guide/environment": (
        "guide/environment.html",
        "Environment field guide",
        (
            "Offline searchable template reference, token "
            "relationships and native/Compose precedence." + GUIDE_SUFFIX
        ),
    ),
    "docs://guide/architecture": (
        "guide/architecture.html",
        "Architecture field guide",
        (
            "Offline service boundaries, illustrative request"
            " paths, persistent state and deployment limits." + GUIDE_SUFFIX
        ),
    ),
    "docs://guide/document-pipeline": (
        "guide/document-pipeline.html",
        "Document pipeline field guide",
        (
            "Offline layout, recognition and reading-order tour "
            "with a block-type reference and red flags." + GUIDE_SUFFIX
        ),
    ),
    "docs://guide/rag-chunking": (
        "guide/rag-chunking.html",
        "RAG chunking field guide",
        (
            "Offline token-budget, protected-unit and provenance "
            "tour with the real chunk schema and routes." + GUIDE_SUFFIX
        ),
    ),
}

# Canonical repository sources; runtime reads only the packaged destinations.
COPIED_DOCUMENTS = {
    "skill.md": "ocr-skill/smart-document-manager/SKILL.md",
    "skill-readme.md": "ocr-skill/README.md",
    "reference.md": "ocr-skill/smart-document-manager/references/reference.md",
    "playbooks.md": "ocr-skill/smart-document-manager/references/playbooks.md",
    **{
        f"guide/{name}.html": f"guide/{name}.html"
        for name in (
            "getting-started",
            "cli",
            "environment",
            "architecture",
            "document-pipeline",
            "rag-chunking",
        )
    },
}


def resource_name(file_name: str) -> str:
    """Derive a resource's advertised ``name`` from its file name.

    ``concepts-data-model.md`` becomes ``read_concepts_data_model`` and
    ``guide/getting-started.html`` becomes ``read_guide_getting_started``, so
    every document is advertised under a name of its own whichever
    subdirectory or extension it came from.
    """
    stem = Path(file_name).with_suffix("").as_posix()
    return "read_" + stem.replace("-", "_").replace("/", "_")


_SKIPPED = frozenset({"head", "style", "script", "svg", "noscript", "template"})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_BLOCKS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "nav", "aside",
        "main", "table", "ul", "ol", "dl", "dt", "dd", "blockquote", "figure",
        "figcaption", "details", "summary", "br", "hr", "li", "pre",
        *_HEADINGS,
    }
)  # fmt: skip


class _TextExtractor(HTMLParser):
    """Collect a page's visible text as one line per block element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.words: list[str] = []
        self.prefix = ""
        self.skipped = 0
        self.heading = False
        self.pre: list[str] | None = None
        self.cells: list[str] | None = None

    def flush(self) -> None:
        text = " ".join("".join(self.words).split())
        self.words = []
        if self.cells is not None:
            if text:
                self.cells.append(text)
            return
        if text:
            if self.prefix.startswith("#") and self.lines:
                self.lines.append("")
            self.lines.append(self.prefix + text)
        self.prefix = ""

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _SKIPPED:
            self.skipped += 1
        if self.skipped:
            return
        if (tag == "br" and self.heading) or tag in ("a", "button"):
            # Adjacent navigation links and buttons carry no whitespace.
            self.words.append(" ")
        elif tag == "tr":
            self.flush()
            self.cells = []
        elif tag in ("td", "th"):
            self.flush()
        elif tag in _BLOCKS:
            self.flush()
            if tag in _HEADINGS:
                self.prefix, self.heading = "#" * _HEADINGS[tag] + " ", True
            elif tag == "li":
                self.prefix = "- "
            elif tag == "pre":
                self.pre = []

    def handle_endtag(self, tag) -> None:
        if tag in _SKIPPED:
            self.skipped = max(0, self.skipped - 1)
            return
        if self.skipped:
            return
        if tag == "pre" and self.pre is not None:
            body = "".join(self.pre).strip("\n")
            self.lines.extend(body.split("\n") if body else [])
            self.pre = None
        elif tag in ("td", "th"):
            self.flush()
        elif tag == "tr" and self.cells is not None:
            self.flush()
            cells, self.cells = self.cells, None
            if cells:
                self.lines.append(" | ".join(cells))
        elif tag in _BLOCKS:
            self.flush()
            self.heading = False

    def handle_data(self, data) -> None:
        if self.skipped:
            return
        if self.pre is not None:
            self.pre.append(data)
        else:
            self.words.append(data)


def html_to_text(source: str) -> str:
    """Return the visible text of an HTML page as lightweight Markdown.

    Headings become ``#`` lines, list items ``- `` lines, table rows cells
    joined by `` | `` and ``pre`` blocks stay verbatim; styles, scripts, SVG
    and the document head are dropped.
    """
    extractor = _TextExtractor()
    extractor.feed(source)
    extractor.close()
    extractor.flush()
    return "\n".join(extractor.lines).strip() + "\n"


def _mime_type(file_name: str) -> str:
    """Every document reaches clients as Markdown text, guides included."""
    return "text/markdown"


def docs_directory() -> Path:
    """Return the directory holding the published documents."""
    return Path(__file__).parent / "docs"


def register(mcp: MCPServer) -> None:
    """Register every published document as a resource."""
    for uri, (file_name, title, description) in DOC_RESOURCES.items():
        _register_one(mcp, uri, file_name, title, description)


def _register_one(
    mcp: MCPServer, uri: str, file_name: str, title: str, description: str
) -> None:
    """Bind one document file to one resource URI.

    A closure factory is required: a bare ``for`` loop over ``@mcp.resource``
    decorators would rebind the same function name each iteration, so every
    URI would serve whichever ``file_name`` the loop last held. The ``name``
    must be passed to the decorator explicitly — the SDK captures it at
    decoration time, so assigning ``__name__`` afterwards changes nothing on
    the wire and every document is advertised under the same name.
    """

    @mcp.resource(
        uri,
        name=resource_name(file_name),
        title=title,
        description=description,
        mime_type=_mime_type(file_name),
    )
    async def _read_document() -> str:
        """Read one document file.

        A missing or unreadable file is left to raise rather than caught and
        turned into "Error: ..." text: a resource read has no ``is_error``
        flag the way a tool call does, so text content is the client's only
        signal, and returning error text *as the document* is
        indistinguishable from the real thing. Letting it propagate gets the
        SDK to turn it into a genuine protocol-level error.
        """
        text = (docs_directory() / file_name).read_text(encoding="utf-8")
        return html_to_text(text) if file_name.endswith(".html") else text
