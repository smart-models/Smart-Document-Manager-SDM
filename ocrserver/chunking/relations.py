"""Resolve only observed labels or unambiguous local layout relationships."""

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from itertools import islice
from math import isfinite

from .canonical import canonical_bytes, canonical_hash
from .errors import ChunkError
from .structure import Document, HEADINGS
from .text import protected_ranges
from .types import ChunkLimits, Source, Work


_NUMBER = r"\d+(?:\.\d+)*[A-Za-z]?"
_FORMULA_NUMBER = re.compile(
    rf"^\s*(?:\((?P<round>{_NUMBER})\)|\[(?P<square>{_NUMBER})\]"
    rf"|(?P<bare>{_NUMBER}))\s*$",
)
_FORMULA_TAG = re.compile(rf"\\tag\*?\{{\s*(?P<label>{_NUMBER})\s*\}}")
_CAPTIONS = {"figure_title", "vision_footnote"}
_OBJECTS = {"table", "image", "chart"}
_CAPTION_LABEL = re.compile(
    rf"\s*(?P<kind>table|tabella|tab\.?|figure|figura|fig\.?|chart|grafico)"
    rf"\s+(?P<label>{_NUMBER})\b",
    re.IGNORECASE,
)
_HTML_CAPTION = re.compile(
    r"<caption\b[^>]*>(?P<body>[^<]*)</caption\s*>",
    re.I,
)
_PROSE = {"text", "abstract", "aside_text", "vertical_text"}
_NOTE_PREFIX = re.compile(
    rf"^\s*(?P<marker>\[\^?{_NUMBER}\]|\({_NUMBER}\)|[*†‡§¶¹²³⁴⁵⁶⁷⁸⁹])"
    r"(?=\s|$)",
)
_CONTINUED = re.compile(
    r"\b(?:continued|continuation|continua|continuazione|segue)\b",
    re.I,
)


@dataclass
class _OutputBudget:
    maximum: int
    structural_maximum: int
    used: int = 0
    formula_labels: int = 0

    def include_formula_label(self):
        if self.formula_labels >= self.structural_maximum:
            raise ChunkError(
                "chunk_work_limit",
                "Formula label capacity exceeded",
                status_code=413,
                details={
                    "limit": "structural_formula_labels",
                    "maximum": self.structural_maximum,
                    "required": self.formula_labels + 1,
                },
            )
        self.formula_labels += 1

    def check(self, size):
        if self.used + size > self.maximum:
            raise ChunkError(
                "chunk_work_limit",
                "Relation metadata exceeds output limit",
                status_code=413,
                details={
                    "limit": "output_bytes",
                    "maximum": self.maximum,
                    "required": self.used + size,
                },
            )

    def include(self, relation, candidates):
        size = len(canonical_bytes(relation))
        self.check(size)
        for index, candidate in enumerate(candidates):
            size += len(canonical_bytes(candidate)) + (1 if index else 0)
            self.check(size)
        self.used += size


class _GeometryIndex:
    """Page/section/type-local vertical interval index with stable ties."""

    def __init__(self, sources):
        records = [(source, _bbox(source)) for source in sources]
        records = [(source, box) for source, box in records if box is not None]
        self.starts = sorted(
            records,
            key=lambda item: (item[1][1], item[0].index),
        )
        self.ends = sorted(
            records,
            key=lambda item: (item[1][3], item[0].index),
        )
        self.start_values = [box[1] for _, box in self.starts]
        self.end_values = [box[3] for _, box in self.ends]

    def candidates(self, box, distance):
        stop = bisect_right(self.start_values, box[3] + distance)
        start = bisect_left(self.end_values, box[1] - distance)
        if stop <= len(self.ends) - start:
            return islice(self.starts, stop)
        return islice(self.ends, start, None)


def _location(source: Source, start=None, end=None) -> dict:
    return {"source_key": source.key, "start": start, "end": end}


def _location_order(location: dict | None) -> tuple:
    if location is None:
        return ("", -1, -1)
    return (
        location["source_key"],
        -1 if location["start"] is None else location["start"],
        -1 if location["end"] is None else location["end"],
    )


def _relation(
    kind,
    origin,
    target=None,
    *,
    method="none",
    candidates=(),
    status="unresolved",
    budget=None,
) -> dict:
    endpoints = sorted([origin, target], key=_location_order)
    relation = {
        "id": canonical_hash(
            {
                "type": kind,
                "endpoints": endpoints,
                "method": method,
            }
        ),
        "type": kind,
        "from": origin,
        "to": target,
        "method": method,
        "status": status,
        "candidate_targets": [],
    }
    if budget is not None:
        budget.include(relation, candidates)
    relation["candidate_targets"] = sorted(candidates, key=_location_order)
    return relation


def _bbox(source: Source) -> tuple | None:
    value = source.block.get("bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not isfinite(item)
        for item in value
    ):
        return None
    x0, y0, x1, y1 = value
    return tuple(value) if x0 < x1 and y0 < y1 else None


def _nearby(origin, indexes, pages, headings, budget):
    page = pages[origin.page]
    dimensions = (page.get("width"), page.get("height"))
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
        for value in dimensions
    ):
        return []
    first = _bbox(origin)
    if first is None:
        return []
    result = []
    size = 0
    for index in indexes:
        if index is None:
            continue
        for candidate, second in index.candidates(first, 0.05 * dimensions[1]):
            left, right = sorted((origin.index, candidate.index))
            markers = headings[origin.page]
            if bisect_right(markers, left) < bisect_left(markers, right):
                continue
            gap_x = max(0, first[0] - second[2], second[0] - first[2])
            gap_y = max(0, first[1] - second[3], second[1] - first[3])
            overlap_x = min(first[2], second[2]) > max(first[0], second[0])
            overlap_y = min(first[3], second[3]) > max(first[1], second[1])
            if (overlap_x or overlap_y) and max(gap_x, gap_y) <= 0.05 * dimensions[1]:
                size += len(canonical_bytes(_location(candidate)))
                size += 1 if result else 0
                budget.check(size)
                result.append(candidate)
    return result


def _sections(document: Document) -> dict[str, str | None]:
    observed = {}
    for section in document.sections:
        if section.title_source:
            observed.setdefault(section.title_source.key, section.id)
        for source in section.sources:
            observed[source.key] = section.id
    current = None
    result = {}
    for source in document.sources:
        current = observed.get(source.key, current)
        result[source.key] = current
    following = None
    for source in reversed(document.sources):
        if source.tag in HEADINGS:
            following = None
        elif result[source.key] is not None:
            following = result[source.key]
        elif following is not None:
            result[source.key] = following
    return result


def _formula_labels(source: Source, budget) -> list[tuple[str, dict]]:
    if source.tag == "formula_number":
        match = _FORMULA_NUMBER.fullmatch(source.content)
        if not match:
            return []
        budget.include_formula_label()
        group = next(name for name, value in match.groupdict().items() if value)
        return [(match.group(group), _location(source, *match.span(group)))]
    labels = []
    for match in _FORMULA_TAG.finditer(source.content):
        budget.include_formula_label()
        labels.append((match.group("label"), _location(source, *match.span("label"))))
    return labels


def _caption_labels(source: Source) -> list[tuple[tuple[str, str], dict]]:
    match = _CAPTION_LABEL.match(source.content)
    shift = 0
    if not match and source.tag == "table":
        caption = _HTML_CAPTION.search(source.content)
        if caption:
            match = _CAPTION_LABEL.match(caption.group("body"))
            shift = caption.start("body")
    if not match:
        return []
    observed = match.group("kind").casefold()
    kind = (
        "table"
        if observed.startswith("tab")
        else "chart"
        if observed in {"chart", "grafico"}
        else "figure"
    )
    start, end = match.span()
    return [
        (
            (kind, match.group("label")),
            _location(source, shift + start, shift + end),
        )
    ]


def _continuation(source: Source) -> bool:
    if source.tag != "table" or not _caption_labels(source):
        return False
    caption = _HTML_CAPTION.search(source.content)
    text = caption.group("body") if caption else source.content.split("\n", 1)[0]
    return _CONTINUED.search(text) is not None


def _choose(kind, origin, candidates, method, sections, budget):
    section = sections[origin["source_key"]]
    local = [
        candidate
        for candidate in candidates
        if sections[candidate["source_key"]] == section
    ]
    targets = local or candidates
    if len(targets) != 1:
        return _relation(kind, origin, method=method, candidates=targets, budget=budget)
    target = targets[0]
    status = (
        "across_chunks"
        if sections[target["source_key"]] == sections[origin["source_key"]]
        else "cross_section"
    )
    return _relation(kind, origin, target, method=method, status=status, budget=budget)


def _note_targets(marker, sources, budget):
    targets = []
    size = 0
    pattern = re.compile(re.escape(marker))
    for source in sources:
        regions = protected_ranges(
            source.content,
            max_items=budget.structural_maximum,
        )
        for match in pattern.finditer(source.content):
            if any(region.start <= match.start() < region.end for region in regions):
                continue
            if len(marker) == 1 and (
                match.start() == 0
                or not source.content[match.start() - 1].isalpha()
                or (
                    match.end() < len(source.content)
                    and source.content[match.end()].isalnum()
                )
            ):
                continue
            target = _location(source, *match.span())
            size += len(canonical_bytes(target)) + (1 if targets else 0)
            budget.check(size)
            targets.append(target)
    return targets


def _inline_relations(source, document, sections, positions, budget):
    """Link inline formulas to explicit adjacent prose within structural limits.

    Typed formula blocks use same-page, same-section neighbors; embedded formula
    regions use surrounding nonempty text intervals from their own source block.
    """
    relations = []
    if source.tag == "inline_formula":
        position = positions[source.key]
        for neighbor in (position - 1, position + 1):
            if not 0 <= neighbor < len(document.sources):
                continue
            target = document.sources[neighbor]
            if (
                target.page == source.page
                and target.tag in _PROSE
                and sections[target.key] == sections[source.key]
                and target.content.strip()
            ):
                relations.append(
                    _relation(
                        "inline_formula_context",
                        _location(source),
                        _location(target, 0, len(target.content)),
                        method="explicit",
                        status="across_chunks",
                        budget=budget,
                    )
                )
        return relations or [
            _relation(
                "inline_formula_context",
                _location(source),
                budget=budget,
            ),
        ]
    if source.tag not in _PROSE:
        return []
    regions = protected_ranges(
        source.content,
        max_items=budget.structural_maximum,
    )
    for index, region in enumerate(regions):
        if region.kind != "formula":
            continue
        before = regions[index - 1].end if index else 0
        after = (
            regions[index + 1].start
            if index + 1 < len(regions)
            else len(source.content)
        )
        for start, end in ((before, region.start), (region.end, after)):
            if source.content[start:end].strip():
                relations.append(
                    _relation(
                        "inline_formula_context",
                        _location(source, region.start, region.end),
                        _location(source, start, end),
                        method="explicit",
                        status="across_chunks",
                        budget=budget,
                    )
                )
    return relations


def resolve_relations(
    document: Document,
    *,
    work: Work | None = None,
) -> list[dict]:
    """Return schema-shaped relations; packing later refines chunk status.

    Inputs and their source order are left unchanged. Unresolved sources
    retain a relation with a null target; ambiguity never selects a nearest
    candidate. IDs depend on type, source endpoints and method, not status.
    A local byte guard bounds relation output before candidate copying; it
    does not charge Work.output_bytes, which belongs to final serialization.
    """
    relations = []
    limits = work.limits if work else ChunkLimits()
    budget = _OutputBudget(
        limits.max_output_bytes,
        limits.max_split_evaluations,
    )
    sections = _sections(document)
    prose = [source for source in document.sources if source.tag in _PROSE]
    positions = {source.key: index for index, source in enumerate(document.sources)}
    pages = {page["page"]: page for page in document.pages}
    headings = defaultdict(list)
    formula_labels, caption_labels, continuation = {}, {}, {}
    formula_targets, caption_targets = defaultdict(list), defaultdict(list)
    tables, geometry, note_targets = defaultdict(list), defaultdict(list), {}
    for source in document.sources:
        if source.tag in HEADINGS:
            headings[source.page].append(source.index)
        if source.tag in {"display_formula", "formula_number"}:
            formula_labels[source.key] = _formula_labels(source, budget)
            if source.tag == "display_formula":
                for label, location in formula_labels[source.key]:
                    formula_targets[label].append(location)
                if not formula_labels[source.key]:
                    key = (source.page, sections[source.key], source.tag)
                    geometry[key].append(source)
        if source.tag in _CAPTIONS | _OBJECTS:
            labels = _caption_labels(source)
            caption_labels[source.key] = labels
            continuation[source.key] = _continuation(source)
            if source.tag in _OBJECTS:
                for label, location in labels:
                    caption_targets[(label, source.tag)].append(location)
                    if source.tag == "table" and not continuation[source.key]:
                        tables[label].append(location)
                if not labels:
                    key = (source.page, sections[source.key], source.tag)
                    geometry[key].append(source)
    geometry = {key: _GeometryIndex(value) for key, value in geometry.items()}
    for source in document.sources:
        relations.extend(
            _inline_relations(
                source,
                document,
                sections,
                positions,
                budget,
            )
        )
        if continuation.get(source.key):
            label, origin = caption_labels[source.key][0]
            candidates = [
                location
                for location in tables[label]
                if positions[location["source_key"]] < positions[source.key]
            ]
            relations.append(
                _choose(
                    "table_continuation",
                    origin,
                    candidates,
                    "explicit",
                    sections,
                    budget,
                )
            )
            continue
        if source.tag == "footnote":
            marker = _NOTE_PREFIX.match(source.content)
            origin = (
                _location(source, *marker.span("marker"))
                if marker
                else _location(source)
            )
            targets = []
            if marker:
                value = marker.group("marker")
                key = (value, sections[source.key])
                if key not in note_targets:
                    local = [
                        item
                        for item in prose
                        if sections[item.key] == sections[source.key]
                    ]
                    note_targets[key] = _note_targets(value, local, budget)
                    if not note_targets[key]:
                        note_targets[key] = _note_targets(value, prose, budget)
                targets = note_targets[key]
            relations.append(
                _choose(
                    "footnote_reference",
                    origin,
                    targets,
                    "explicit" if marker else "none",
                    sections,
                    budget,
                )
            )
            continue
        if source.tag in _CAPTIONS:
            labels = caption_labels[source.key]
            label = labels[0][0] if labels else None
            compatible = (
                {"table"}
                if label and label[0] == "table"
                else {"chart"}
                if label and label[0] == "chart"
                else {"image", "chart"}
                if label
                else _OBJECTS
            )
            explicit = [
                location
                for tag in sorted(compatible)
                for location in caption_targets[(label, tag)]
            ]
            if explicit:
                relations.append(
                    _choose(
                        "caption_object",
                        labels[0][1],
                        explicit,
                        "explicit",
                        sections,
                        budget,
                    )
                )
                continue
            candidates = _nearby(
                source,
                [
                    geometry.get((source.page, sections[source.key], tag))
                    for tag in sorted(compatible)
                ],
                pages,
                headings,
                budget,
            )
            relations.append(
                _choose(
                    "caption_object",
                    _location(source),
                    [_location(item) for item in candidates],
                    "geometry" if candidates else "none",
                    sections,
                    budget,
                )
            )
            continue
        if source.tag != "formula_number":
            continue
        labels = formula_labels[source.key]
        label = labels[0][0] if labels else None
        explicit = formula_targets[label]
        if explicit:
            relations.append(
                _choose(
                    "formula_number",
                    labels[0][1],
                    explicit,
                    "explicit",
                    sections,
                    budget,
                )
            )
            continue
        candidates = _nearby(
            source,
            [geometry.get((source.page, sections[source.key], "display_formula"))],
            pages,
            headings,
            budget,
        )
        relations.append(
            _choose(
                "formula_number",
                _location(source),
                [_location(item) for item in candidates],
                "geometry" if candidates else "none",
                sections,
                budget,
            )
        )
    return sorted(relations, key=lambda relation: relation["id"])
