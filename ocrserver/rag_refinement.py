"""Materialize a reviewed partition of stored raw chunks; commit its manifest last."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC

from fastapi import HTTPException

from ocrserver.archive_names import parse_prefix
from ocrserver.storage_browser import TEXT_PREVIEW_BYTES, _error

from ocrserver.rag_split import (
    is_repeat_framing,
    repeat_framing_kind,
    render_fragment,
    sum_counts,
)
from ocrserver.chunking.text import protected_ranges, split_boundaries
from ocrserver.rag_structure import (
    MAX_SAFE_INTEGER,
    canonical,
    document_of,
    integer as _integer,
    structure_boundaries,
    text_separator,
)
from ocrserver.schemas import (
    RAG_SPLIT_NOTE_PARAMETER,
    RAG_SPLIT_NOTE_SEVERITY,
    normalize_entry,
)


# ASCII whitespace only: str.strip and String.trim disagree on the rest.
_SPACE = " \t\n\r\f\v"
_FACT_CODES = {
    "page_boundary": "page_boundary_forced",
    "document": "document_forced",
    "failed_gap": "failed_gap_forced",
    "protected_join": "table_split",
}
# The two size codes a console may declare for a gesture-affected group;
# the server re-measures the group before accepting the declaration (O1-1).
_SIZE_CODES = {"above_maximum_fragment", "below_minimum_fragment"}
_NOTE_DETAIL = {
    "page_boundary_forced": "forced page boundary",
    "document_forced": "forced document change",
    "failed_gap_forced": "forced failed-page gap",
    "table_split": "table split",
    "code_split": "code split",
    "grapheme_split": "text split",
    "math_atomic": "formula kept atomic",
    "above_maximum_fragment": "group above the maximum",
    "below_minimum_fragment": "group below the minimum",
    "token_count_unknown": "token count unknown",
}


def plan_has_fragments(groups):
    """True when any group holds a partial or ``repeat`` entry."""
    return any(not isinstance(entry, int) for group in groups for entry in group)


def _entry_parts(entry):
    """``(index, start, end_or_None, repeat)``; a whole chunk has no end."""
    if isinstance(entry, int):
        return entry, 0, None, False
    if len(entry) == 4:
        return entry[0], entry[1], entry[2], True
    return entry[0], entry[1], entry[2], False


def _group_indices(group):
    """Distinct chunk indices of a group, in first-appearance order."""
    indices = []
    for entry in group:
        index = _entry_parts(entry)[0]
        if not indices or indices[-1] != index:
            indices.append(index)
    return indices


def _chunk_entries(group):
    """Consecutive entries bundled by the chunk they slice."""
    bundled = []
    for entry in group:
        index = _entry_parts(entry)[0]
        if bundled and bundled[-1][0] == index:
            bundled[-1][1].append(entry)
        else:
            bundled.append((index, [entry]))
    return bundled


def _validate_plan(chunks, groups):
    """Reject plans that lose, reorder, overlap or leave gaps in raw content.

    Chunk references advance without gaps across the groups, and for every
    chunk the non-``repeat`` ranges of all groups, in plan order, partition
    ``[0, len(text))`` exactly once: no gap, no overlap and no offset outside
    the text. Every ``repeat`` slice must be a real framing span of the source
    text (FR-018).
    """
    position = 0
    coverage = {}
    for group in groups:
        if not group:
            raise _error(422, "rag_invalid_plan")
        indices = _group_indices(group)
        # A group opens the next chunk, or continues the chunk a previous
        # group left open when a split spread one chunk over several groups
        # (FR-008); the per-chunk coverage check below still proves the plan
        # is an exact partition. A negative index is rejected outright: Python
        # would read ``chunks[-1]`` and hide the reference from the coverage
        # loop, so ``position - 1`` must never admit the first group's ``-1``.
        if (
            indices[0] < 0
            or indices != list(range(indices[0], indices[0] + len(indices)))
            or indices[0] not in (position, position - 1)
        ):
            raise _error(422, "rag_invalid_plan")
        position = max(position, indices[-1] + 1)
        if position > len(chunks):
            raise _error(422, "rag_invalid_plan")
        for entry in group:
            index, start, end, repeat = _entry_parts(entry)
            text = chunks[index]["text"]
            if end is None:
                coverage.setdefault(index, []).append((0, len(text)))
                continue
            if start < 0 or start > end or end > len(text):
                raise _error(422, "rag_invalid_plan")
            if repeat:
                if not is_repeat_framing(text, start, end):
                    raise _error(422, "rag_invalid_plan")
            else:
                coverage.setdefault(index, []).append((start, end))
    if position != len(chunks):
        raise _error(422, "rag_invalid_plan")
    for index, chunk in enumerate(chunks):
        expected = 0
        for start, end in coverage.get(index, []):
            if start != expected:
                raise _error(422, "rag_invalid_plan")
            expected = end
        if expected != len(chunk["text"]):
            raise _error(422, "rag_invalid_plan")


def forced_facts(chunks, metadata, options, groups) -> dict[int, set[str]]:
    """Recompute, per group, the forced boundaries its members really cross.

    Facts come from the source and from :func:`structure_boundaries`, never
    from the plan: ``page_boundary``, ``document``, ``failed_gap`` and
    ``protected_join``.
    """
    structure = structure_boundaries(chunks, metadata, options)
    groups_by_index = {}
    for group_index, group in enumerate(groups):
        for index in _group_indices(group):
            groups_by_index[index] = group_index
    facts = {group_index: set() for group_index in range(len(groups))}
    for group_index, group in enumerate(groups):
        indices = _group_indices(group)
        for previous, following in zip(indices, indices[1:]):
            if previous not in structure["hard"]:
                continue
            if canonical(document_of(chunks[previous], metadata)) != canonical(
                document_of(chunks[following], metadata)
            ):
                facts[group_index].add("document")
            if previous in structure["gaps"]:
                facts[group_index].add("failed_gap")
            if options.page_boundary and (
                not _integer(chunks[previous].get("page"), 1)
                or not _integer(chunks[following].get("page"), 1)
                or chunks[previous].get("page") != chunks[following].get("page")
            ):
                facts[group_index].add("page_boundary")
    # A protected join broken between two groups is a fact of its left group.
    for boundary in structure["joins"]:
        left = groups_by_index.get(boundary)
        if left is not None and groups_by_index.get(boundary + 1) != left:
            facts[left].add("protected_join")
    return facts


def _framing_codes(chunks, group) -> set[str]:
    """Split codes proven by a ``repeat`` entry's framing span (R16)."""
    codes = set()
    for entry in group:
        index, start, end, repeat = _entry_parts(entry)
        if not repeat:
            continue
        kind = repeat_framing_kind(chunks[index]["text"], start, end)
        if kind == "table":
            codes.add("table_split")
        elif kind == "code":
            codes.add("code_split")
    return codes


def _join_is_table(chunks, index) -> bool:
    low = chunks[index]["text"].lower()
    return low.count("<table") > low.count("</table")


def _cut_codes(chunks, group) -> set[str]:
    """A cut that is not a lexical boundary offered by ``split_boundaries``."""
    codes = set()
    for index, entries in _chunk_entries(group):
        if len(entries) == 1 and isinstance(entries[0], int):
            continue
        text = chunks[index]["text"]
        offered = {
            boundary.offset: boundary.kind for boundary in split_boundaries(text)
        }
        offsets = {
            offset
            for _, start, end, repeat in map(_entry_parts, entries)
            if not repeat and end is not None
            for offset in (start, end)
        }
        # A lexical cut is a word/sentence/paragraph/list/structural boundary;
        # a grapheme-only (or unoffered, inside-protected) offset is not.
        if any(
            0 < offset < len(text) and offered.get(offset, "grapheme") == "grapheme"
            for offset in offsets
        ):
            codes.add("grapheme_split")
    return codes


def _has_formula(chunks, group) -> bool:
    """True when a covered span overlaps a formula region from the scan (R16)."""
    for index, entries in _chunk_entries(group):
        text = chunks[index]["text"]
        spans = [
            (start, len(text) if end is None else end)
            for _, start, end, repeat in map(_entry_parts, entries)
            if not repeat
        ]
        for region in protected_ranges(text):
            if region.kind == "formula" and any(
                start < region.end and region.start < end for start, end in spans
            ):
                return True
    return False


def _note_value(code, chunks, sources, tokens, text, metadata):
    """The per-code parameter value, or ``None`` when the fact is not known."""
    name = RAG_SPLIT_NOTE_PARAMETER.get(code)
    if name == "tokens":
        return tokens
    if name == "pages":
        if code == "failed_gap_forced":
            pages = [
                page
                for page in (chunk.get("page") for chunk in sources)
                if _integer(page, 1)
            ]
            failed = [
                page
                for page in (metadata.get("failed_pages") or [])
                if _integer(page, 1)
            ]
            if not pages:
                return None
            low, high = min(pages), max(pages)
            return sum(1 for page in failed if low < page < high)
        return len(
            [
                page
                for page in (chunk.get("page") for chunk in sources)
                if _integer(page, 1)
            ]
        )
    if name == "documents":
        return len({canonical(document_of(chunk, metadata)) for chunk in sources})
    if name == "rows":
        return text.lower().count("<tr")
    if name == "lines":
        return text.count("\n") + 1
    return None


def _targets(chunks, group):
    """``p<page>:b<id>`` of the first member, else an empty list."""
    first = chunks[_group_indices(group)[0]]
    if _integer(first.get("page"), 1) and _integer(first.get("id")):
        return [
            {
                "source_key": f"p{int(first['page'])}:b{int(first['id'])}",
                "start": None,
                "end": None,
            }
        ]
    return []


def _diagnostics(
    chunks,
    group,
    real_facts,
    join_code,
    repair,
    tokens,
    text,
    options,
    sources,
    metadata,
    declared=frozenset(),
):
    """The saved ``diagnostics`` of one group, one note per code."""
    codes = [
        _FACT_CODES[fact]
        for fact in ("page_boundary", "document", "failed_gap")
        if fact in real_facts
    ]
    if join_code is not None:
        codes.append(join_code)
    if repair:
        codes.extend(
            sorted(
                _framing_codes(chunks, group)
                | _cut_codes(chunks, group)
                | (
                    {"math_atomic"}
                    if tokens is not None
                    and tokens > options.max_tokens
                    and _has_formula(chunks, group)
                    else set()
                )
            )
        )
        if tokens is None:
            codes.append("token_count_unknown")
        elif tokens < options.min_tokens:
            codes.append("below_minimum_fragment")
        elif tokens > options.max_tokens:
            codes.append("above_maximum_fragment")
    # A whole-chunk gesture (a glue that lands above the maximum) has no
    # partial entry, so the declared size code is the only signal; it was
    # verified against the measurement above and is recorded here.
    for code in sorted(declared & _SIZE_CODES):
        if code not in codes:
            codes.append(code)
    notes, seen = [], set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        parameters = [{"name": "detail", "value": _NOTE_DETAIL.get(code, code)}]
        name = RAG_SPLIT_NOTE_PARAMETER.get(code)
        if name is not None:
            value = _note_value(code, chunks, sources, tokens, text, metadata)
            if value is not None:
                parameters.append({"name": name, "value": value})
        notes.append(
            {
                "code": code,
                "severity": RAG_SPLIT_NOTE_SEVERITY.get(code, "info"),
                "targets": _targets(chunks, group),
                "parameters": parameters,
            }
        )
    return notes


def materialize(source, request, check=lambda: None, counter=None):
    """Use the exact reviewed groups, rejecting lost/reordered blocks or boundaries.

    Sizes are advisory sums of saved estimates. Source payload fields are merged
    into a list of distinct values in first-appearance order, even when only one
    remains, so a list-valued field is never confused with several scalars. No
    per-source copies are kept.
    """
    chunks = source.get("chunks") if isinstance(source, dict) else None
    if (
        not isinstance(chunks, list)
        or not chunks
        or any(
            not isinstance(chunk, dict)
            or not isinstance(chunk.get("text"), str)
            or not _integer(chunk.get("id"))
            for chunk in chunks
        )
    ):
        raise _error(422, "rag_invalid_source")
    options = request.options
    if options.min_tokens > options.max_tokens:
        raise _error(422, "rag_invalid_plan")
    metadata = source.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise _error(422, "rag_invalid_source")
    groups = [[normalize_entry(entry) for entry in group] for group in request.groups]
    _validate_plan(chunks, groups)
    structure = structure_boundaries(chunks, metadata, options)
    facts = forced_facts(chunks, metadata, options, groups)
    groups_by_index = {}
    for group_index, group in enumerate(groups):
        for index in _group_indices(group):
            groups_by_index[index] = group_index
    join_codes = {}
    for boundary in structure["joins"]:
        left = groups_by_index.get(boundary)
        if left is None or groups_by_index.get(boundary + 1) == left:
            continue
        join_codes[left] = (
            "table_split"
            if _join_is_table(chunks, boundary) or _join_is_table(chunks, boundary + 1)
            else "code_split"
        )
    overrides = (
        request.overrides if request.overrides is not None else [[] for _ in groups]
    )
    if len(overrides) != len(groups):
        raise _error(422, "rag_invalid_plan")
    group_counts = request.counts
    if group_counts is not None and len(group_counts) != len(groups):
        raise _error(422, "rag_invalid_plan")
    needs_counter = plan_has_fragments(groups)
    if needs_counter and counter is None:
        raise _error(503, "rag_tokenizer_unavailable")
    declared_summary = []
    for group_index, group in enumerate(groups):
        required = {
            _FACT_CODES[fact] for fact in facts[group_index] if fact != "protected_join"
        }
        if group_index in join_codes:
            required.add(join_codes[group_index])
        # O1-1: the console declares the two size codes it displays for a
        # gesture-affected group; the server still derives facts itself (R21),
        # so only those two framing-independent size codes widen ``allowed``.
        allowed = required | _framing_codes(chunks, group) | _SIZE_CODES
        declared = set(overrides[group_index])
        # R21: the forced facts are derived from the source and the structure and
        # recorded as diagnostics; only a declaration that invents a fact that
        # does not exist is a forgery and refused (PRD 11).
        if declared - allowed:
            raise _error(422, "rag_invalid_plan")
        declared_summary.append(declared)
    position, result = 0, []
    counts = dict(within=0, below=0, above=0, unknown=0)
    for group_index, group in enumerate(groups):
        check()
        indices = _group_indices(group)
        position = max(position, indices[-1] + 1)
        if position > len(chunks):
            raise _error(422, "rag_invalid_plan")
        sources = [chunks[index] for index in indices]
        contributions, member_counts = [], []
        for chunk_index, entries in _chunk_entries(group):
            text = chunks[chunk_index]["text"]
            if len(entries) == 1 and isinstance(entries[0], int):
                contributions.append(text)
                value = chunks[chunk_index].get("token_count")
                member_counts.append(int(value) if _integer(value) else None)
            else:
                rendered = render_fragment(text, entries)
                contributions.append(rendered)
                member_counts.append(counter.count(rendered))
        merged_text = contributions[0] + "".join(
            text_separator(
                contributions[offset - 1],
                contributions[offset],
                indices[offset - 1] in structure["continuations"],
            )
            + contributions[offset]
            for offset in range(1, len(contributions))
        )
        tokens = sum_counts(member_counts)
        # R24: when the browser declares the member counts it displays, the
        # server compares them with its own per-member measurement and refuses a
        # real discordance; on agreement the shared sum is the saved count.
        declared_counts = (
            group_counts[group_index] if group_counts is not None else None
        )
        if declared_counts is not None:
            if len(declared_counts) != len(member_counts) or any(
                declared != measured
                for declared, measured in zip(declared_counts, member_counts)
            ):
                raise _error(422, "rag_invalid_plan")
            tokens = sum_counts(declared_counts)
        if tokens is not None and tokens > MAX_SAFE_INTEGER:
            tokens = None
        status = (
            "unknown"
            if tokens is None
            else "below"
            if tokens < options.min_tokens
            else "above"
            if tokens > options.max_tokens
            else "within"
        )
        # O1-1: a declared size code must match the server's own measurement;
        # a group measuring within range may not claim either one.
        for code in sorted(declared_summary[group_index] & _SIZE_CODES):
            expected = "above" if code == "above_maximum_fragment" else "below"
            if status != expected:
                raise _error(422, "rag_invalid_plan")
        counts[status] += 1
        merged = {"id": len(result) + 1, "text": merged_text}
        # Empty fields are absent, not null, as in the raw ingest file.
        if tokens is not None:
            merged["token_count"] = tokens
        document = document_of(sources[0], metadata)
        if document is not None:
            merged["document"] = document
        fields = {}
        for chunk in sources:
            source_page = chunk.get("page")
            for key, value in chunk.items():
                # Null raw fields stay absent, as in the ingest contract.
                if key in ("id", "text", "token_count", "document") or value is None:
                    continue
                # A printed folio repeating the physical index is redundant.
                if (
                    key == "page_number"
                    and _integer(source_page, 1)
                    and isinstance(value, str)
                    and value.strip(_SPACE) == str(int(source_page))
                ):
                    continue
                values = fields.setdefault(key, {})
                values.setdefault(canonical(value), value)
        for key, values in fields.items():
            merged[key] = list(values.values())
        repair = any(not isinstance(entry, int) for entry in group)
        diagnostics = _diagnostics(
            chunks,
            group,
            facts[group_index],
            join_codes.get(group_index),
            repair,
            tokens,
            merged_text,
            options,
            sources,
            metadata,
            declared_summary[group_index],
        )
        if diagnostics:
            merged["diagnostics"] = diagnostics
        result.append(merged)
    if position != len(chunks):
        raise _error(422, "rag_invalid_plan")
    tokens = [chunk.get("token_count") for chunk in result]
    # Exact half-up hundredths, reproducible with the browser preview's integer math;
    # beyond the bound the browser's doubles lose exactness, so both report unknown.
    complete = (
        all(token is not None for token in tokens)
        and 200 * sum(tokens) <= MAX_SAFE_INTEGER
    )
    average = (
        (200 * sum(tokens) + len(tokens)) // (2 * len(tokens)) / 100
        if complete
        else None
    )
    refinement = {
        "source_key": request.source_key,
        "source_sha256": request.source_sha256,
        "execution_id": str(request.execution_id),
        "created_at": request.created_at.astimezone(UTC).isoformat(),
        "options": options.model_dump(),
        "range_counts": counts,
        "token_count_method": (
            "sum_of_member_counts" if needs_counter else "sum_of_saved_estimates"
        ),
    }
    # The key is absent, not empty, when no override was declared: a
    # gesture-free plan must stay byte-identical to the previous output.
    declared_overrides = [
        {"group": index + 1, "codes": sorted(codes)}
        for index, codes in enumerate(declared_summary)
        if codes
    ]
    if declared_overrides:
        refinement["overrides"] = declared_overrides
    return {
        **source,
        "chunks": result,
        "metadata": {
            **metadata,
            "n_chunks": len(result),
            "total_tokens": sum(tokens) if complete else None,
            "avg_tokens": average,
            "min_tokens": min(tokens) if complete else None,
            "max_tokens": max(tokens) if complete else None,
            "refinement": refinement,
        },
    }


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _issued(manifest):
    """Highest refinement number ever handed out in this archive."""
    value = manifest.get("rag_seq")
    return value if type(value) is int and value >= 0 else 0


def save_refinement(browser, reader, prefix, request, check, counter=None):
    """Write with writer credentials and merge the manifest with an ETag guard.

    Content dedupe and conditional creates make retries safe, including a
    timeout after the object write but before the manifest was committed.
    """
    settings = browser.settings
    manifest_info = {}
    manifest, _ = browser._manifest(reader, prefix, check, metadata=manifest_info)
    if not any(item.get("key") == request.source_key for item in manifest["artifacts"]):
        raise _error(422, "rag_invalid_source")
    raw = browser._read(reader, request.source_key, settings.storage_max_bytes, check)
    if hashlib.sha256(raw).hexdigest() != request.source_sha256:
        raise _error(409, "rag_source_changed")
    try:
        source = json.loads(raw)
        output = materialize(source, request, check, counter)
        body = _encode(output)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _error(422, "rag_invalid_source") from error
    if len(body) > settings.storage_max_bytes:
        raise _error(413, "storage_file_too_large")
    digest = hashlib.sha256(body).hexdigest()

    def verify_artifact(artifact):
        try:
            stored = browser._read(
                reader,
                artifact["key"],
                settings.storage_max_bytes,
                check,
            )
        except HTTPException as error:
            if error.status_code == 404:
                raise _error(409, "storage_archive_incomplete") from error
            raise
        if (
            len(stored) != artifact["size"]
            or hashlib.sha256(stored).hexdigest() != digest
        ):
            raise _error(409, "storage_archive_incomplete")

    job = parse_prefix(prefix)["job"]
    execution = str(request.execution_id)
    # Names are json_rag_<job>_<seq>.json: the archive code scopes a sequence
    # that always takes the next free number, so two saves can never collide.
    pattern = re.compile(rf"json_rag_{re.escape(job)}_(\d+)\.json\Z")
    twin = next(
        (
            a
            for a in manifest["artifacts"]
            if pattern.match(a.get("name", "")) and a.get("sha256") == digest
        ),
        None,
    )
    if twin is not None:
        # A retried execution produces identical bytes; answer with the file it
        # already committed instead of allocating a second name for it.
        verify_artifact(twin)
        return {
            "prefix": prefix,
            "file": {
                "name": twin["name"],
                "key": twin["key"],
                "size": twin["size"],
                "content_type": twin.get("content_type", "application/json"),
            },
            "chunk_count": len(output["chunks"]),
        }
    if any(a.get("execution_id") == execution for a in manifest["artifacts"]):
        raise _error(409, "rag_execution_conflict")
    records = browser._objects(reader, prefix, check)
    seq = _issued(manifest)
    for candidate in [item.get("name", "") for item in manifest["artifacts"]]:
        found = pattern.match(candidate)
        if found:
            seq = max(seq, int(found[1]))
    for record in records:
        found = (
            pattern.match(record["Key"][len(prefix) :])
            if record["Key"].startswith(prefix)
            else None
        )
        if found:
            seq = max(seq, int(found[1]))
    # Refuse an unguarded manifest replacement when the store supplies no ETag.
    if not manifest_info.get("etag"):
        raise _error(503, "storage_unavailable")
    writer = browser._writer_factory()
    try:
        from ocrserver.storage_index import touch_index

        touch_index(writer, settings.s3_bucket, prefix, check, reader=reader)
        listed = {item["key"] for item in manifest["artifacts"]}
        key = None
        # An orphaned object from a timed-out execution is adopted when its
        # bytes match, not duplicated under the next sequence number.
        for record in records:
            check()
            candidate = record["Key"]
            if (
                not candidate.startswith(prefix)
                or candidate in listed
                or not pattern.match(candidate[len(prefix) :])
            ):
                continue
            if (
                browser._read(reader, candidate, settings.storage_max_bytes, check)
                == body
            ):
                key = candidate
                break
        for _ in range(10):
            if key is not None:
                break
            seq += 1
            name = f"json_rag_{job}_{seq:03d}.json"
            candidate = prefix + name
            if len(records) >= settings.storage_max_objects and not any(
                record["Key"] == candidate for record in records
            ):
                raise _error(413, "storage_too_many_objects")
            check()
            try:
                writer.put_object(
                    Bucket=settings.s3_bucket,
                    Key=candidate,
                    Body=body,
                    ContentType="application/json",
                    IfNoneMatch="*",
                )
                key = candidate
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") not in (
                    "PreconditionFailed",
                    "412",
                ):
                    raise
                if (
                    browser._read(reader, candidate, settings.storage_max_bytes, check)
                    == body
                ):
                    key = candidate
        if key is None:
            raise _error(409, "rag_execution_conflict")
        name = key[len(prefix) :]
        artifact = {
            "name": name,
            "key": key,
            "size": len(body),
            "content_type": "application/json",
            "sha256": digest,
            "execution_id": execution,
        }
        response = {
            "prefix": prefix,
            "file": artifact,
            "chunk_count": len(output["chunks"]),
        }

        def committed():
            existing = next((a for a in manifest["artifacts"] if a["key"] == key), None)
            if existing and existing.get("sha256") != artifact["sha256"]:
                raise _error(409, "rag_execution_conflict")
            if existing:
                verify_artifact(existing)
            return existing is not None

        for _ in range(5):
            check()
            if committed():
                return response
            # rag_seq outlives deletions, so a removed number is never reissued.
            issued = max(_issued(manifest), int(pattern.match(name)[1]))
            updated = _encode(
                {
                    **manifest,
                    "rag_seq": issued,
                    "artifacts": [*manifest["artifacts"], artifact],
                }
            )
            if len(updated) > TEXT_PREVIEW_BYTES:
                raise _error(413, "storage_file_too_large")
            if not manifest_info.get("etag"):
                raise _error(503, "storage_unavailable")
            try:
                writer.put_object(
                    Bucket=settings.s3_bucket,
                    Key=prefix + "manifest.json",
                    Body=updated,
                    ContentType="application/json",
                    IfMatch=manifest_info["etag"],
                )
                return response
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") not in (
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                    "409",
                ):
                    raise
                manifest, _ = browser._manifest(
                    reader, prefix, check, metadata=manifest_info
                )
        raise _error(409, "storage_archive_busy")
    finally:
        browser._close_client(writer)
