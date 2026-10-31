"""Single public entry point for exact, deterministic OCR chunking."""

from ..schemas import REQUIRED_CHUNK_FIELDS, ChunkOptions, ChunkRequest
from .canonical import canonical_bytes, canonical_hash
from .errors import ChunkError
from .overlap import choose_overlap
from .packing import pack_section
from .provenance import OverlapReceipt, check_output_size, validate_result
from .relations import resolve_relations
from .rendering import render
from .splitting import section_fragments
from .structure import prepare_document
from .types import PROFILE, VERSION, ChunkLimits, Work


def _sorted_unique(values):
    return list(dict.fromkeys(values))


def _metadata(
    document,
    section,
    rendered,
    count,
    index,
    options,
    tokenizer,
    source_id,
    lang,
):
    """Assemble one chunk's metadata from measured text and represented sources.

    Include page context for observed/context pages but assets only from primary
    pages. Preserve source-order block types and hash the resolved algorithm and
    options as policy identity; the caller adds final relation/overflow details.
    """
    spans = [*rendered.source_spans, *rendered.context_spans]
    pages = sorted({span["page"] for span in spans if span["page"] is not None})
    primary_pages = {span["page"] for span in rendered.source_spans}
    page_context = [document.page_context[page] for page in pages]
    assets = [
        asset for page in sorted(primary_pages) for asset in document.assets[page]
    ]
    represented = {span["source_key"] for span in spans if span["source_key"]}
    represented.update(asset["source"]["source_key"] for asset in assets)
    for context in page_context:
        for key in ("headers", "footers", "page_labels"):
            represented.update(item["source"]["source_key"] for item in context[key])
        represented.update(
            item["source"]["source_key"] for item in context["marginal_assets"]
        )
    body = [span for span in rendered.source_spans if span["role"] != "section_title"]
    chunk = {
        "id": index,
        "text": rendered.text,
        "token_count": count,
        "page": (body or list(rendered.source_spans))[0]["page"],
        "pages": pages,
        "section_id": section.id,
        "section": section.title,
        "section_path": section.path,
        "block_types": _sorted_unique(
            source.tag for source in document.sources if source.key in represented
        ),
        "source_spans": list(rendered.source_spans),
        "context_spans": list(rendered.context_spans),
        "page_context": page_context,
        "assets": assets,
        "relations": [],
        "overflow": False,
        "overflow_tokens": 0,
        "overlap_token_count": 0,
        "source_fingerprint": document.fingerprint,
        "tokenizer": tokenizer.identity,
        "chunker_version": VERSION,
        "policy_hash": canonical_hash(
            {
                "chunker_version": VERSION,
                "algorithm_profile": PROFILE,
                "options": options.model_dump(),
            }
        ),
        "diagnostics": [],
    }
    if source_id is not None:
        chunk["source_id"] = source_id
    if lang is not None:
        chunk["lang"] = lang
    return chunk


def chunk_document(
    parsed_document: dict,
    options: ChunkOptions,
    tokenizer,
    *,
    source_id=None,
    lang=None,
    accept_truncated=False,
    scope=None,
    limits: ChunkLimits | None = None,
    observer=None,
) -> dict:
    """Produce a complete RAG file or raise a stable atomic domain error."""
    from .telemetry import work_statistics

    limits = limits or ChunkLimits()
    work = Work(tokenizer, limits)
    result = None
    try:
        with work.phase("total"):
            result = _chunk_document(
                parsed_document,
                options,
                tokenizer,
                source_id=source_id,
                lang=lang,
                accept_truncated=accept_truncated,
                scope=scope,
                limits=limits,
                work=work,
            )
        return result
    finally:
        if observer is not None:
            observer(work_statistics(work, result))


def _chunk_document(
    parsed_document,
    options,
    tokenizer,
    *,
    source_id,
    lang,
    accept_truncated,
    scope,
    limits,
    work,
):
    """Pure construction; the public wrapper exports statistics separately."""
    work.consume("input_bytes", len(canonical_bytes(parsed_document)))
    request = ChunkRequest(
        parsed_document=parsed_document,
        options=options,
        source_id=source_id,
        lang=lang,
        accept_truncated=accept_truncated,
    )
    parsed = request.parsed_document.model_dump()
    tokenizer.assert_compatible((parsed.get("usage") or {}).get("tokenizer"))
    if options.max_tokens > limits.max_tokens:
        raise ChunkError(
            "invalid_chunk_options",
            "Token budget exceeds configured maximum",
            details={"max_tokens": limits.max_tokens},
        )
    with work.phase("preparation"):
        document = prepare_document(
            parsed,
            options,
            work,
            accept_truncated=accept_truncated,
            scope=scope,
        )
    chunks, ignored = [], []
    overlap_receipts = {}
    output_size = len(canonical_bytes({"chunks": []}))
    sources = {source.key: source for source in document.sources}
    with work.phase("relations"):
        relations = resolve_relations(document, work=work)
    for section in document.sections:
        with work.phase("splitting"):
            fragments = section_fragments(section, options, work)
        ignored.extend(
            interval
            for fragment in fragments
            for interval in getattr(fragment, "ignored_source_ranges", ())
        )
        previous = []
        previous_overflow = False
        with work.phase("packing"):
            edges = pack_section(section, fragments, options, work, relations)
        for edge in edges:
            work.consume("output_chunks")
            group = fragments[edge.start : edge.end]
            overlap, overlap_count = None, 0
            count = edge.token_count
            if edge.start and not edge.overflow_reason and not previous_overflow:
                overlap, overlap_count, recounted = choose_overlap(
                    section, previous, group, options, work, sources
                )
                if recounted is not None:
                    count = recounted
            rendered = render(section, group, first=edge.start == 0, overlap=overlap)
            chunk = _metadata(
                document,
                section,
                rendered,
                count,
                len(chunks),
                options,
                tokenizer,
                source_id,
                lang,
            )
            chunk["diagnostics"] = [
                item for fragment in group for item in fragment.diagnostics
            ]
            chunk["overlap_token_count"] = overlap_count
            if overlap is not None:
                overlap_receipts[len(chunks)] = OverlapReceipt.capture(
                    overlap.text,
                    len(section.title) + 2 if section.title else 0,
                    overlap_count,
                )
            if edge.overflow_reason:
                chunk.update(
                    {
                        "overflow": True,
                        "overflow_tokens": edge.token_count - options.max_tokens,
                        "overflow_reason": edge.overflow_reason,
                    }
                )
            output_size += len(canonical_bytes(chunk)) + int(bool(chunks))
            check_output_size(output_size, work)
            chunks.append(chunk)
            previous, previous_overflow = group, bool(edge.overflow_reason)
    if not chunks:
        raise ChunkError("no_indexable_content", "No indexable OCR text")
    represented_pages = {page for chunk in chunks for page in chunk["pages"]}
    references = {
        asset["source"]["source_key"]: asset["reference"]
        for page in document.pages
        for asset in (
            *document.assets[page["page"]],
            *document.page_context[page["page"]]["marginal_assets"],
        )
    }
    unassigned = [
        {
            "source": source.reference(),
            "content": source.content,
            "reference": references.get(source.key),
        }
        for source in document.sources
        if source.page not in represented_pages
    ]
    chunks[0]["document_context"] = {
        "titles": document.titles,
        "scope": document.scope,
        "chunking_options": options.model_dump(),
        "order_summary": document.order_summary,
        "algorithm_profile": PROFILE,
        "unassigned_sources": unassigned,
        "diagnostics": document.diagnostics,
    }
    _attach_relations(document, chunks, relations)
    with work.phase("validation"):
        validated = validate_result(
            document,
            chunks,
            options,
            work,
            ignored=ignored,
            overlap_receipts=overlap_receipts,
        )
    selected = set(options.metadata_fields)
    return {
        "chunks": [
            {
                key: value
                for key, value in chunk.items()
                if key in REQUIRED_CHUNK_FIELDS or key in selected
            }
            for chunk in validated["chunks"]
        ]
    }


def _attach_relations(document, chunks, relations):
    """Keep links navigable after partitioning, including unresolved sources."""
    from collections import defaultdict

    from .relation_mapping import RelationIndex

    source_sections = {}
    for section in document.sections:
        if section.title_source:
            source_sections.setdefault(section.title_source.key, section.id)
        for source in section.sources:
            source_sections[source.key] = section.id
    represented = RelationIndex(
        (index, chunk["source_spans"]) for index, chunk in enumerate(chunks)
    )
    asset_owners = defaultdict(set)
    for index, chunk in enumerate(chunks):
        for asset in chunk["assets"]:
            asset_owners[asset["source"]["source_key"]].add(index)

    def owners(endpoint, *, complete=False):
        key = endpoint["source_key"]
        found = represented.owners(endpoint, complete=complete)
        # Only an asset with no textual representation can be wholly present
        # through metadata alone. Copied page metadata cannot rejoin text.
        if not complete or not represented.has_source(key):
            found = found | asset_owners.get(key, set())
        return found

    sources = {source.key: source for source in document.sources}
    page_map = {page["page"]: page for page in document.pages}
    for relation in relations:
        first = relation["from"]["source_key"]
        second = relation["to"]["source_key"] if relation["to"] else None
        targets = {first, second} - {None}
        containing = set(owners(relation["from"]))
        if second is not None:
            containing.update(owners(relation["to"]))
        if second is not None:
            status = "across_chunks"
            if relation["status"] == "cross_section" or (
                source_sections.get(first) != source_sections.get(second)
                and source_sections.get(first)
                and source_sections.get(second)
            ):
                status = "cross_section"
            elif owners(relation["from"], complete=True) & owners(
                relation["to"], complete=True
            ):
                status = "same_chunk"
            relation = {**relation, "status": status}
        for index in sorted(containing):
            chunks[index]["relations"].append(relation)
            for asset in chunks[index]["assets"]:
                if asset["source"]["source_key"] in targets:
                    asset["relation_ids"].append(relation["id"])
        if relation["status"] == "unresolved":
            from .types import diagnostic

            source = sources[first]
            document.diagnostics.append(
                diagnostic(
                    "relation_unresolved",
                    [source],
                    relation_type=relation["type"],
                )
            )
            page = page_map[source.page]
            if relation["type"] in {"caption_object", "formula_number"} and (
                not page.get("width")
                or not page.get("height")
                or not source.block.get("bbox")
            ):
                document.diagnostics.append(
                    diagnostic("geometry_unavailable", [source])
                )
    for chunk in chunks:
        for asset in chunk["assets"]:
            asset["relation_ids"] = sorted(set(asset["relation_ids"]))
