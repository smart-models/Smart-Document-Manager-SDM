"""Canonical array ordering and atomic validation against original sources."""

from dataclasses import dataclass
from hashlib import sha256

from .canonical import canonical_bytes, canonical_hash
from .errors import ChunkError
from .text import normalize_text, protected_ranges
from .types import PROFILE, VERSION
from .validation import validate_output


@dataclass(frozen=True)
class OverlapReceipt:
    """One already-counted suffix; validation never needs another encode."""

    start: int
    end: int
    token_count: int
    text_sha256: str

    @classmethod
    def capture(cls, text, start, token_count):
        """Record an already-counted overlap's output range and UTF-8 hash."""
        return cls(
            start,
            start + len(text),
            token_count,
            sha256(text.encode("utf-8")).hexdigest(),
        )


def _deduplicate(values, key):
    distinct = {canonical_bytes(value): value for value in values}
    return sorted(distinct.values(), key=key)


def _nullable(value):
    return (0, 0) if value is None else (1, value)


def _span_key(span):
    return tuple(
        _nullable(span.get(key))
        for key in (
            "text_start",
            "text_end",
            "page",
            "block_index",
            "start",
            "end",
            "role",
        )
    )


def _source_key(value):
    source = value["source"]
    return source["page"], source["block_index"]


def _diagnostics(values):
    for item in values:
        item["targets"] = _deduplicate(
            item["targets"],
            lambda target: (
                target["source_key"],
                _nullable(target["start"]),
                _nullable(target["end"]),
            ),
        )
        item["parameters"] = _deduplicate(
            item["parameters"], lambda parameter: parameter["name"]
        )
    return _deduplicate(
        values,
        lambda item: (
            item["code"],
            canonical_bytes(item["targets"]),
            canonical_bytes(item["parameters"]),
        ),
    )


def canonicalize_arrays(chunks):
    """Deduplicate and order chunk provenance and document context in place."""
    for chunk in chunks:
        for key in ("source_spans", "context_spans"):
            chunk[key] = _deduplicate(chunk[key], _span_key)
        chunk["assets"] = _deduplicate(chunk["assets"], _source_key)
        chunk["relations"] = _deduplicate(
            chunk["relations"], lambda relation: relation["id"]
        )
        chunk["page_context"].sort(key=lambda page: page["page"])
        for page in chunk["page_context"]:
            for key in (
                "headers",
                "footers",
                "page_labels",
                "marginal_assets",
            ):
                page[key] = _deduplicate(page[key], _source_key)
        chunk["diagnostics"] = _diagnostics(chunk["diagnostics"])
    context = chunks[0]["document_context"]
    context["titles"] = _deduplicate(context["titles"], _source_key)
    context["unassigned_sources"] = _deduplicate(
        context["unassigned_sources"], _source_key
    )
    context["diagnostics"] = _diagnostics(context["diagnostics"])


def _covered(intervals, length):
    position = 0
    for start, end in sorted(intervals):
        if start > position:
            return False
        position = max(position, end)
    return position >= length


def _invalid(reason, **details):
    raise ChunkError(
        "chunk_validation_failed",
        "Generated RAG provenance is invalid",
        status_code=500,
        details={"reason": reason, **details},
    )


def check_output_size(size, work):
    """Raise a 413 domain error if serialized output exceeds its byte budget."""
    if size > work.limits.max_output_bytes:
        raise ChunkError(
            "chunk_output_limit",
            "RAG file exceeds output limit",
            status_code=413,
            details={
                "maximum": work.limits.max_output_bytes,
                "required": size,
            },
        )


def _validate_overlap(chunk, previous, options, receipt, sources, max_items):
    """Verify overlap against its measured receipt and preceding primary prose.

    Require matching text hashes, token budgets and source-offset reconstruction.
    Reject protected content, propagated context and overlap across section or
    overflow boundaries without invoking the tokenizer again.
    """
    spans = [span for span in chunk["context_spans"] if span["role"] == "overlap"]
    count = chunk["overlap_token_count"]
    if bool(spans) != (count > 0) or not 0 <= count <= options.overlap_tokens:
        _invalid("overlap_count")
    if not spans:
        if receipt is not None:
            _invalid("unexpected_overlap_receipt")
        return
    if (
        receipt is None
        or receipt.token_count != count
        or previous is None
        or previous["overflow"]
        or chunk["overflow"]
        or previous["section_id"] != chunk["section_id"]
        or not 0 <= receipt.start < receipt.end <= len(chunk["text"])
    ):
        _invalid("overlap_receipt")
    text = chunk["text"][receipt.start : receipt.end]
    if sha256(text.encode("utf-8")).hexdigest() != receipt.text_sha256:
        _invalid("overlap_text")
    if not previous["text"].endswith(text):
        _invalid("overlap_not_previous_suffix")
    suffix_start = len(previous["text"]) - len(text)
    if any(
        region.end > suffix_start
        for region in protected_ranges(previous["text"], max_items=max_items)
    ):
        _invalid("overlap_protected_content")
    if any(
        span["source_key"] is not None and span["text_end"] > suffix_start
        for span in previous["context_spans"]
    ):
        _invalid("overlap_propagated_context")
    expected = []
    allowed = {
        "text",
        "abstract",
        "content",
        "vertical_text",
        "reference_content",
        "aside_text",
    }
    for span in previous["source_spans"]:
        if span["text_end"] <= suffix_start:
            continue
        if span["role"] != "body" or span["type"] not in allowed:
            _invalid("overlap_nonprose")
        source = sources[span["source_key"]]
        normalized = normalize_text(source.content[span["start"] : span["end"]])
        left = max(suffix_start, span["text_start"])
        start = span["start"] + normalized.offsets[left - span["text_start"]]
        expected.append(
            (
                span["source_key"],
                start,
                span["end"],
                receipt.start + left - suffix_start,
                receipt.start + span["text_end"] - suffix_start,
            )
        )
    observed = [
        (
            span["source_key"],
            span["start"],
            span["end"],
            span["text_start"],
            span["text_end"],
        )
        for span in spans
    ]
    if observed != expected:
        _invalid("overlap_source_mapping")


def _validate_protected_overflow(chunk, options, sources, ignored, max_items):
    """Require overflow to contain exactly one whole permitted table or code block.

    Account for explicitly ignored table markup when checking coverage. Extra
    observed context or partial protected content invalidates the entire output.
    """
    if not chunk["overflow"]:
        return
    body = [span for span in chunk["source_spans"] if span["role"] != "section_title"]
    keys = {span["source_key"] for span in body}
    if len(keys) != 1:
        _invalid("overflow_not_isolated")
    key = next(iter(keys))
    source = sources[key]
    if any(
        span["source_key"] is not None and span["role"] != "section_title"
        for span in chunk["context_spans"]
    ):
        _invalid("overflow_extra_context")
    ranges = [(span["start"], span["end"]) for span in body]
    reason = chunk.get("overflow_reason")
    if reason == "protected_table":
        if (
            not options.preserve_tables
            or source.tag != "table"
            or any(span["role"] != "table_cell" for span in body)
            or not _covered(ranges + ignored.get(key, []), len(source.content))
        ):
            _invalid("overflow_table_not_whole")
    elif reason == "protected_code":
        if not options.preserve_code_blocks or any(
            span["role"] != "code" for span in body
        ):
            _invalid("overflow_not_protected_code")
        permitted = (
            [(0, len(source.content))]
            if source.tag in {"algorithm", "code"}
            else [
                (
                    source.normalized.offsets[region.start],
                    source.normalized.offsets[region.end],
                )
                for region in protected_ranges(
                    source.normalized.text, max_items=max_items
                )
                if region.kind == "code"
            ]
        )
        start, end = (
            min(left for left, _ in ranges),
            max(right for _, right in ranges),
        )
        if (start, end) not in permitted or not _covered(
            [(left - start, right - start) for left, right in ranges],
            end - start,
        ):
            _invalid("overflow_code_not_whole")
    else:
        _invalid("overflow_reason")


def _validate_location(location, sources):
    if location is None:
        return
    source = sources.get(location["source_key"])
    start, end = location["start"], location["end"]
    if source is None or (start is None) != (end is None):
        _invalid("unresolved_source_reference")
    if start is not None and not 0 <= start <= end <= len(source.content):
        _invalid("relation_source_offset")


def _validate_source_reference(reference, sources):
    source = sources.get(reference["source_key"])
    if source is None or reference != source.reference():
        _invalid("metadata_source_identity")
    return source


def _validate_observed_metadata(chunks, sources):
    for chunk in chunks:
        for asset in chunk["assets"]:
            _validate_source_reference(asset["source"], sources)
        for page in chunk["page_context"]:
            for field in ("headers", "footers", "page_labels"):
                for item in page[field]:
                    source = _validate_source_reference(item["source"], sources)
                    if (
                        source.page != page["page"]
                        or item["text"] != source.normalized.text
                    ):
                        _invalid("observed_metadata_changed")
            for asset in page["marginal_assets"]:
                source = _validate_source_reference(asset["source"], sources)
                if source.page != page["page"]:
                    _invalid("metadata_page")
    context = chunks[0]["document_context"]
    for item in context["titles"]:
        source = _validate_source_reference(item["source"], sources)
        if source.tag != "doc_title" or item["text"] != source.normalized.text:
            _invalid("document_title")
    for item in context["unassigned_sources"]:
        source = _validate_source_reference(item["source"], sources)
        if item["content"] != source.content:
            _invalid("unassigned_content_changed")
    for diagnostic in [
        *context["diagnostics"],
        *(item for chunk in chunks for item in chunk["diagnostics"]),
    ]:
        for target in diagnostic["targets"]:
            _validate_location(target, sources)


def _validate_relations(chunks, sources):
    """Verify relation endpoints, canonical identities and asset references.

    Repeated relation IDs must carry identical payloads. Every referenced asset
    relation must exist in the complete exported chunk set.
    """
    known = {}
    for chunk in chunks:
        for relation in chunk["relations"]:
            for location in (
                relation["from"],
                relation["to"],
                *relation["candidate_targets"],
            ):
                _validate_location(location, sources)
            if (relation["to"] is None) != (relation["status"] == "unresolved"):
                _invalid("relation_status")
            endpoints = sorted(
                [relation["from"], relation["to"]],
                key=lambda location: (
                    ("", -1, -1)
                    if location is None
                    else (
                        location["source_key"],
                        -1 if location["start"] is None else location["start"],
                        -1 if location["end"] is None else location["end"],
                    )
                ),
            )
            expected = canonical_hash(
                {
                    "type": relation["type"],
                    "endpoints": endpoints,
                    "method": relation["method"],
                }
            )
            if relation["id"] != expected:
                _invalid("relation_identity")
            if relation["id"] in known and relation != known[relation["id"]]:
                _invalid("conflicting_relation")
            known[relation["id"]] = relation
    for chunk in chunks:
        assets = [
            *chunk["assets"],
            *(
                asset
                for page in chunk["page_context"]
                for asset in page["marginal_assets"]
            ),
        ]
        if any(
            identifier not in known
            for asset in assets
            for identifier in asset["relation_ids"]
        ):
            _invalid("unresolved_relation_reference")


def validate_result(
    document, chunks, options, work, *, ignored=(), overlap_receipts=None
):
    """Validate coverage, exact recounts and ranges before exposing any chunks."""
    canonicalize_arrays(chunks)
    identity = {
        "source_fingerprint": document.fingerprint,
        "tokenizer": work.tokenizer.identity,
        "chunker_version": VERSION,
        "policy_hash": canonical_hash(
            {
                "chunker_version": VERSION,
                "algorithm_profile": PROFILE,
                "options": options.model_dump(),
            }
        ),
    }
    context = chunks[0]["document_context"]
    if (
        context["scope"] != document.scope
        or context["chunking_options"] != options.model_dump()
        or context["algorithm_profile"] != PROFILE
    ):
        _invalid("document_context")
    output_size = len(canonical_bytes({"chunks": []}))
    for index, chunk in enumerate(chunks):
        output_size += len(canonical_bytes(chunk)) + int(index > 0)
        check_output_size(output_size, work)
    sources = {source.key: source for source in document.sources}
    _validate_observed_metadata(chunks, sources)
    _validate_relations(chunks, sources)
    coverage = {key: [] for key in sources}
    ignored_by_source = {}
    for key, start, end in ignored:
        coverage[key].append((start, end))
        ignored_by_source.setdefault(key, []).append((start, end))
    for index, chunk in enumerate(chunks):
        if any(chunk[field] != value for field, value in identity.items()):
            _invalid("document_identity")
        spans = [*chunk["source_spans"], *chunk["context_spans"]]
        pages = sorted({span["page"] for span in spans if span["page"] is not None})
        body = [
            span for span in chunk["source_spans"] if span["role"] != "section_title"
        ]
        origin = body or chunk["source_spans"]
        if (
            not origin
            or chunk["page"] != origin[0]["page"]
            or chunk["pages"] != pages
            or [page["page"] for page in chunk["page_context"]] != pages
        ):
            _invalid("page_metadata")
        actual = work.count(chunk["text"])
        if actual != chunk["token_count"]:
            _invalid("token_count")
        if chunk["overflow_tokens"] != max(0, actual - options.max_tokens):
            _invalid("overflow_count")
        if actual > options.max_tokens and (options.strictly or not chunk["overflow"]):
            _invalid("unauthorized_overflow")
        output_intervals = []
        for span in (*chunk["source_spans"], *chunk["context_spans"]):
            left, right = span["text_start"], span["text_end"]
            if not 0 <= left <= right <= len(chunk["text"]):
                _invalid("text_offset")
            output_intervals.append((left, right))
            key = span["source_key"]
            if key is None:
                continue
            source = sources.get(key)
            if source is None or not 0 <= span["start"] <= span["end"] <= len(
                source.content
            ):
                _invalid("source_offset", source_key=key)
            if (
                source.page != span["page"]
                or source.index != span["block_index"]
                or source.tag != span["type"]
            ):
                _invalid("source_identity", source_key=key)
            coverage[key].append((span["start"], span["end"]))
            if "table" not in span:
                observed = normalize_text(
                    source.content[span["start"] : span["end"]]
                ).text
                if observed != chunk["text"][left:right]:
                    _invalid("observed_text_changed", source_key=key)
        if not _covered(output_intervals, len(chunk["text"])):
            _invalid("unmapped_output")
        _validate_protected_overflow(
            chunk,
            options,
            sources,
            ignored_by_source,
            work.limits.max_split_evaluations,
        )
        _validate_overlap(
            chunk,
            chunks[index - 1] if index else None,
            options,
            (overlap_receipts or {}).get(index),
            sources,
            work.limits.max_split_evaluations,
        )
        for page in chunk["page_context"]:
            for field in ("headers", "footers", "page_labels"):
                for item in page[field]:
                    key = item["source"]["source_key"]
                    coverage[key].append((0, len(sources[key].content)))
    for item in chunks[0]["document_context"]["unassigned_sources"]:
        key = item["source"]["source_key"]
        coverage[key].append((0, len(sources[key].content)))
    for key, source in sources.items():
        if source.content and not _covered(coverage[key], len(source.content)):
            _invalid("uncovered_source", source_key=key)
    payload = validate_output({"chunks": chunks})
    work.consume("output_bytes", output_size)
    return payload
