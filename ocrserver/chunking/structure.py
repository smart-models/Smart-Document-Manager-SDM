"""Interpret OCR arrays without inventing geometry, section levels or text."""

import base64
import hashlib
import re
from dataclasses import dataclass

from .canonical import canonical_hash
from .errors import ChunkError
from .types import Section, Source, Work, diagnostic


TAGS = frozenset(
    "abstract algorithm aside_text chart content display_formula "
    "doc_title figure_title footer footer_image footnote "
    "formula_number header header_image image inline_formula number "
    "paragraph_title reference reference_content seal table text "
    "vertical_text vision_footnote".split()
)
MARGINAL = {"header", "footer", "number", "header_image", "footer_image"}
ASSETS = {"image", "chart", "seal", "header_image", "footer_image"}
HEADINGS = {"doc_title", "paragraph_title", "reference"}
TYPED = {"abstract", "content", "reference_content"}


@dataclass
class Document:
    """Retain validated sources, sections, context and deterministic identity."""

    pages: list[dict]
    sources: list[Source]
    sections: list[Section]
    scope: dict
    page_context: dict[int, dict]
    assets: dict[int, list[dict]]
    titles: list[dict]
    diagnostics: list[dict]
    order_summary: dict
    fingerprint: str


def _scope(pages, warnings, accept_truncated, explicit_scope):
    failed = [page["page"] for page in pages if page.get("error")]
    if failed:
        raise ChunkError(
            "incomplete_ocr",
            "OCR contains failed pages",
            details={"pages": failed},
        )
    truncations = [
        warning for warning in warnings if warning.get("code") == "pages_truncated"
    ]
    if (
        truncations
        and not accept_truncated
        and not (explicit_scope and explicit_scope["origin"] == "explicit_max_pages")
    ):
        raise ChunkError(
            "incomplete_ocr",
            "OCR truncation was not accepted",
            details={"reason": "pages_truncated"},
        )
    if explicit_scope:
        return {**explicit_scope, "selected_pages": [p["page"] for p in pages]}
    details = truncations[0].get("details", {}) if truncations else {}
    return {
        "kind": "page_prefix" if truncations else "provided_document",
        "origin": "accepted_upstream_truncation" if truncations else "provided_json",
        "selected_pages": [page["page"] for page in pages],
        "requested_max_pages": details.get("max_pages"),
        "total_pages": details.get("total_pages"),
    }


def _image_hash(value):
    try:
        encoded = value.split(",", 1)[1] if value.startswith("data:") else value
        return hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
    except (ValueError, TypeError):
        raise ChunkError(
            "invalid_structure",
            "Invalid inline image encoding",
            details={"reason": "invalid_crop"},
        ) from None


def source_fingerprint(pages, scope):
    """Hash the canonical source projection, scope and decoded image hashes."""
    projection = []
    fields = (
        "type",
        "content",
        "bbox",
        "order",
        "block_id",
        "group_id",
        "global_block_id",
        "global_group_id",
        "polygon",
    )
    for page in pages:
        projection.append(
            {
                "page": page["page"],
                "width": page.get("width"),
                "height": page.get("height"),
                "blocks": [
                    {key: block.get(key) for key in fields} for block in page["blocks"]
                ],
                "image_names": page.get("image_names") or [],
                "image_sha256": [
                    _image_hash(value) for value in page.get("images") or []
                ],
            }
        )
    return canonical_hash({"pages": projection, "scope": scope})


def _attach_asset_references(pages, sources, page_context, assets, diagnostics):
    """Keep opaque references; only a unique explicit correspondence is used."""
    by_page = {page["page"]: page for page in pages}
    asset_sources = {source.key: source for source in sources if source.tag in ASSETS}
    for number, page in by_page.items():
        names = page.get("image_names") or []
        images = page.get("images") or []
        candidates = [
            *assets[number],
            *page_context[number]["marginal_assets"],
        ]
        assigned = set()
        for asset in candidates:
            source = asset_sources[asset["source"]["source_key"]]
            explicit = [
                index
                for index, name in enumerate(names)
                if name and (source.content == name or f"]({name})" in source.content)
            ]
            if not explicit and len(candidates) == len(names) == 1:
                explicit = [0]
            if len(explicit) != 1 or names.count(names[explicit[0]]) != 1:
                continue
            index = explicit[0]
            asset["reference"] = names[index]
            asset["sha256"] = (
                _image_hash(images[index]) if index < len(images) else None
            )
            assigned.add(index)
        for index, name in enumerate(names):
            if index not in assigned:
                diagnostics.append(
                    diagnostic(
                        "asset_reference_unassigned",
                        severity="info",
                        page=number,
                        reference=name,
                        sha256=_image_hash(images[index])
                        if index < len(images)
                        else None,
                    )
                )


def _sections(sources, marginal_policy):
    """Build source-ordered sections without inventing heading hierarchy.

    Derive ancestry only from unambiguous numbered headings. Keep typed spans
    separate, retain enclosing heading context and apply the marginal-text policy
    without changing original source identities.
    """
    sections = []
    current = None
    typed = None
    container = None
    ancestry = {}

    def start(source, title_source=None):
        title = title_source.normalized.text if title_source else None
        if title == "":
            title = None
        section = Section(
            f"s:{source.key}",
            title_source,
            title,
            [],
            title_primary=title_source is None or title_source.key == source.key,
        )
        node = {"id": section.id, "title": title}
        match = re.match(r"^\s*(\d+(?:\.\d+)*)(?:[.)]?\s+|$)", title or "")
        path = []
        if match:
            number = match.group(1)
            parts = number.split(".")
            ancestors = [".".join(parts[:index]) for index in range(1, len(parts))]
            if all(key in ancestry and ancestry[key] is not None for key in ancestors):
                path = [ancestry[key] for key in ancestors]
            if section.title_primary:
                ancestry[number] = node if number not in ancestry else None
        section.path = [*path, node]
        sections.append(section)
        return section

    for source in sources:
        if source.tag in HEADINGS:
            current = start(source, source)
            typed = None
            container = current
            continue
        if source.tag in MARGINAL:
            if marginal_policy == "text" and source.content:
                if current is None:
                    current = start(source)
                    container = current
                current.sources.append(source)
            continue
        if not source.content:
            continue
        source_type = source.tag if source.tag in TYPED else None
        if source_type != typed:
            if typed is not None:
                current = start(source, container.title_source if container else None)
            if source_type:
                container = current
                if current is None or current.sources:
                    current = start(source)
            typed = source_type
        if current is None:
            current = start(source)
            container = current
        current.sources.append(source)
    return sections


def prepare_document(
    parsed, options, work: Work, *, accept_truncated=False, scope=None
) -> Document:
    """Validate OCR pages and build ordered sources, sections and context.

    Reject failed pages, unaccepted truncation, duplicate page numbers or missing
    structured blocks. Charge source preparation to the supplied work budget.
    """
    pages = parsed["pages"]
    numbers = [page["page"] for page in pages]
    if any(page <= 0 for page in numbers):
        raise ChunkError(
            "invalid_structure",
            "Page numbers must be positive",
            details={"reason": "nonpositive_page"},
        )
    if len(set(numbers)) != len(numbers):
        raise ChunkError(
            "invalid_structure",
            "Page numbers must be unique",
            details={"reason": "duplicate_page"},
        )
    if not pages or any(page.get("blocks") is None for page in pages):
        raise ChunkError(
            "missing_structured_blocks", "Structured OCR blocks are required"
        )
    pages = sorted(pages, key=lambda page: page["page"])
    selected_scope = _scope(pages, parsed.get("warnings", []), accept_truncated, scope)
    sources = [
        Source(page["page"], index, block)
        for page in pages
        for index, block in enumerate(page["blocks"])
    ]
    work.consume("blocks", len(sources))
    page_context, assets, titles, diagnostics = {}, {}, [], []
    summary = {
        "pages": len(pages),
        "blocks": len(sources),
        "ranked_blocks": 0,
        "missing_ranks": 0,
        "conflicting_pages": [],
    }
    for page in pages:
        number = page["page"]
        context = {
            "page": number,
            "headers": [],
            "footers": [],
            "page_labels": [],
            "marginal_assets": [],
        }
        page_context[number] = context
        assets[number] = []
        ranked = [
            block["order"] for block in page["blocks"] if block.get("order") is not None
        ]
        summary["ranked_blocks"] += len(ranked)
        summary["missing_ranks"] += len(page["blocks"]) - len(ranked)
        if any(left >= right for left, right in zip(ranked, ranked[1:])):
            summary["conflicting_pages"].append(number)
            diagnostics.append(diagnostic("reading_order_conflict", page=number))
    for source in sources:
        value = {"text": source.normalized.text, "source": source.reference()}
        if source.tag in {"header", "footer", "number"} or (
            source.tag in {"header_image", "footer_image"} and source.content
        ):
            field = {
                "header": "headers",
                "header_image": "headers",
                "footer": "footers",
                "footer_image": "footers",
                "number": "page_labels",
            }[source.tag]
            page_context[source.page][field].append(value)
        if source.tag in ASSETS:
            asset = {
                "source": source.reference(),
                "reference": None,
                "sha256": None,
                "relation_ids": [],
            }
            if source.tag in MARGINAL:
                page_context[source.page]["marginal_assets"].append(asset)
            else:
                assets[source.page].append(asset)
        if source.tag == "doc_title":
            titles.append(value)
        if not source.content and source.tag not in ASSETS:
            code = "empty_section_title" if source.tag in HEADINGS else "empty_content"
            diagnostics.append(diagnostic(code, [source]))
        if source.tag not in TAGS and source.tag != "code" and source.content:
            diagnostics.append(diagnostic("unknown_tag", [source], tag=source.tag))
        recorded = source.block.get("content_tokens")
        if recorded is not None:
            work.consume("split_evaluations")
            actual = work.count(source.content) if source.content else 0
            if recorded != actual:
                diagnostics.append(
                    diagnostic(
                        "content_tokens_mismatch",
                        [source],
                        recorded=recorded,
                        actual=actual,
                    )
                )
    _attach_asset_references(pages, sources, page_context, assets, diagnostics)
    return Document(
        pages,
        sources,
        _sections(sources, options.marginal_policy),
        selected_scope,
        page_context,
        assets,
        titles,
        diagnostics,
        summary,
        source_fingerprint(pages, selected_scope),
    )
