"""Immutable, source-preserving text operations for structural chunking.

Offsets are Python Unicode code-point boundaries, never UTF-8 byte offsets.
The policy in this module is versioned with the chunker; it uses no model or
language detection and never repairs OCR content.
"""

from bisect import bisect_right
from array import array
from collections.abc import Sequence
from dataclasses import dataclass

import regex

from .errors import ChunkError


_FENCE_LINE = regex.compile(
    r"(?m)^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$",
)
_MATH_DELIMITER = regex.compile(r"\\[()[\]]|\$+")
_MATH_SIGNAL = regex.compile(r"\\[A-Za-z]+|[_^{}=+*/<>≤≥±×÷∑∫√]")
_CLOSER = {r"\(": r"\)", r"\[": r"\]", "$$": "$$", "$": "$"}
_GRAPHEME = regex.compile(r"\X")
_NONSEPARATED = regex.compile(
    r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}\p{Thai}\p{Lao}"
    r"\p{Khmer}\p{Myanmar}\p{Extended_Pictographic}\p{Regional_Indicator}]",
)
_WORD_END = regex.compile(r"(?<=\S)\s+")
_PARAGRAPH_END = regex.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")
_LIST_START = regex.compile(
    r"(?m)^[ \t]{0,3}(?:[-+*•]|\d+[.)]|[A-Za-z][.)])[ \t]+",
)
_SENTENCE_END = regex.compile(
    r"(?<![.!?。！？])(?P<punctuation>[.!?。！？]++)[\"'”’)\]}]*"
    r"(?:\s+|$|(?=[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]))",
)
_CLAUSE_END = regex.compile(
    r"[,;:，；：][\"'”’)\]}]*"
    r"(?:\s+|$|(?=[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]))",
)
_ABBREVIATION_END = regex.compile(r"(?P<word>\p{L}+(?:\.\p{L}+)*)\.$")
_ABBREVIATIONS = frozenset(
    {
        "art",
        "dott",
        "dr",
        "e.g",
        "ecc",
        "eq",
        "es",
        "etc",
        "fig",
        "i.e",
        "jr",
        "mr",
        "mrs",
        "ms",
        "no",
        "pag",
        "pp",
        "prof",
        "sig",
        "sig.ra",
        "sr",
        "st",
        "vol",
        "vs",
    }
)


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """Text with one original-source boundary per normalized boundary."""

    text: str
    offsets: Sequence[int]


@dataclass(frozen=True, slots=True)
class TextRegion:
    """A balanced source region, including delimiters but not the next LF."""

    start: int
    end: int
    kind: str
    body_start: int
    body_end: int
    language: str | None
    fence: str


@dataclass(frozen=True, slots=True)
class Boundary:
    """A legal cut and its structural loss cost; lower costs are preferred."""

    offset: int
    cost: int
    kind: str


@dataclass
class _ScanBudget:
    maximum: int | None
    name: str
    used: int = 0

    def take(self) -> None:
        if self.maximum is not None and self.used >= self.maximum:
            raise ChunkError(
                "chunk_work_limit",
                "Structural text capacity exceeded",
                status_code=413,
                details={
                    "limit": self.name,
                    "maximum": self.maximum,
                    "required": self.used + 1,
                },
            )
        self.used += 1


def normalize_text(content: str) -> NormalizedText:
    """Normalize CRLF/CR to LF while preserving every other code point."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) == len(content):
        return NormalizedText(text, range(len(content) + 1))
    offsets = array("I", [0])
    position = 0
    while position < len(content):
        character = content[position]
        position += 1
        if character == "\r":
            if position < len(content) and content[position] == "\n":
                position += 1
        offsets.append(position)
    return NormalizedText(text, memoryview(offsets.tobytes()).cast("I"))


def protected_ranges(
    text: str,
    *,
    max_items: int = 100_000,
) -> tuple[TextRegion, ...]:
    """Find balanced fences/formulas, ignoring escapes and math inside code.

    Single-dollar math may span a newline or precede a digit. It permits
    edge whitespace only for bodies with an explicit math signal,
    including OCR's ``$ ^{*} $``. Ordinary ``$5 and $10`` prices are prose;
    numeric content with explicit ``$5$`` delimiters is math.
    """
    budget = _ScanBudget(max_items, "structural_protected_items")
    regions = []
    opening = None
    for match in _FENCE_LINE.finditer(text):
        budget.take()
        fence = match.group("fence")
        info = match.group("info")
        if opening is None:
            if fence[0] == "`" and "`" in info:
                continue
            opening = match
            continue
        original = opening.group("fence")
        if fence[0] != original[0] or len(fence) < len(original) or info.strip():
            continue
        regions.append(
            TextRegion(
                start=opening.start(),
                end=match.end(),
                kind="code",
                body_start=min(opening.end() + 1, len(text)),
                body_end=match.start(),
                language=opening.group("info").strip() or None,
                fence=original,
            )
        )
        opening = None
    combined = []
    position = 0
    for region in regions:
        combined.extend(_formula_regions(text, position, region.start, budget))
        combined.append(region)
        position = region.end
    combined.extend(_formula_regions(text, position, len(text), budget))
    return tuple(combined)


def _escaped(text: str, position: int) -> bool:
    preceding = position - 1
    while preceding >= 0 and text[preceding] == "\\":
        preceding -= 1
    return (position - preceding - 1) % 2 == 1


def _formula_regions(
    text: str,
    start: int,
    end: int,
    budget: _ScanBudget,
) -> list[TextRegion]:
    """Find nonoverlapping formula regions within a bounded code-point interval.

    Charge delimiter scans to the structural budget, respect escaped delimiters
    and avoid treating whitespace-delimited currency text as dollar math unless
    its body contains a recognized mathematical signal.
    """
    delimiters = []
    closers: dict[str, list[int]] = {}
    for match in _MATH_DELIMITER.finditer(text, start, end):
        position, stop = match.span()
        delimiter = match.group()
        if _escaped(text, position):
            if not delimiter.startswith("$"):
                continue
            position += 1
            delimiter = delimiter[1:]
        if not delimiter or (delimiter.startswith("$") and len(delimiter) > 2):
            continue
        budget.take()
        delimiters.append((position, stop, delimiter))
        closers.setdefault(delimiter, []).append(position)

    regions = []
    consumed = start
    for position, stop, delimiter in delimiters:
        if position < consumed or delimiter not in _CLOSER:
            continue
        candidates = closers.get(_CLOSER[delimiter], [])
        index = bisect_right(candidates, position)
        if index == len(candidates):
            continue
        closing = candidates[index]
        body = text[stop:closing]
        if not body.strip():
            continue
        if delimiter == "$":
            if (body[0].isspace() or body[-1].isspace()) and not (
                _MATH_SIGNAL.search(body)
            ):
                continue
        consumed = closing + len(_CLOSER[delimiter])
        regions.append(
            TextRegion(
                position,
                consumed,
                "formula",
                stop,
                closing,
                None,
                delimiter,
            )
        )
    return regions


def split_boundaries(
    text: str,
    *,
    max_boundaries: int | None = None,
) -> tuple[Boundary, ...]:
    """Return ordered cut candidates, never inside code, math or a grapheme.

    Paragraph/list cuts cost 0, sentences 1, clauses 4, words 16, and
    grapheme fallback 64. Separators stay on the preceding fragment; list
    indentation stays with the list item. Coincident cuts use the lowest
    cost, then the explicitly applied structural rule priority.
    """
    boundaries = {}
    for match in _GRAPHEME.finditer(text):
        if max_boundaries is not None and len(boundaries) >= max_boundaries:
            raise ChunkError(
                "chunk_work_limit",
                "Structural boundary capacity exceeded",
                status_code=413,
                details={
                    "limit": "structural_boundaries",
                    "maximum": max_boundaries,
                    "required": max_boundaries + 1,
                },
            )
        boundaries[match.end()] = Boundary(match.end(), 64, "grapheme")
    graphemes = set(boundaries) | {0}

    def offer(offset: int, cost: int, kind: str) -> None:
        if offset not in graphemes:
            return
        current = boundaries.get(offset)
        if current is None or cost <= current.cost:
            boundaries[offset] = Boundary(offset, cost, kind)

    for match in _WORD_END.finditer(text):
        offer(match.end(), 16, "word")
    for match in _CLAUSE_END.finditer(text):
        offer(match.end(), 4, "clause")
    for match in _SENTENCE_END.finditer(text):
        punctuation = match.group("punctuation")
        if punctuation == ".":
            abbreviation = _ABBREVIATION_END.search(
                text,
                max(0, match.start() - 32),
                match.end("punctuation"),
            )
            if abbreviation:
                word = abbreviation.group("word")
                if word.casefold() in _ABBREVIATIONS or (
                    len(word) == 1 and word.isupper()
                ):
                    continue
        offer(match.end(), 1, "sentence")
    for match in _LIST_START.finditer(text):
        offer(match.start(), 0, "list")
    for match in _PARAGRAPH_END.finditer(text):
        offer(match.end(), 0, "paragraph")

    regions = protected_ranges(
        text,
        max_items=max_boundaries if max_boundaries is not None else 100_000,
    )
    starts = [region.start for region in regions]
    for region in regions:
        offer(region.start, 0, "protected")
        offer(region.end, 0, "protected")
    offer(0, 0, "block")
    offer(len(text), 0, "block")

    result = []
    for offset in sorted(boundaries):
        index = bisect_right(starts, offset) - 1
        if index >= 0 and regions[index].start < offset < regions[index].end:
            continue
        result.append(boundaries[offset])
    return tuple(result)


def lexical_boundaries(
    text: str,
    *,
    max_items: int = 100_000,
) -> tuple[int, ...]:
    """Partition suffix atoms without losing separators or splitting glyphs.

    Whitespace follows the preceding atom, except leading whitespace, which
    belongs to the first atom. Nonseparated CJK/Southeast Asian writing and
    emoji use complete graphemes; ordinary words retain their punctuation.
    An empty input has the sole boundary 0; whitespace-only input is one atom.
    Callers supply the final prose run, excluding protected regions.
    """
    if not text:
        return (0,)
    budget = _ScanBudget(max_items, "structural_overlap_atoms")
    ends: list[int] = []
    separated = True
    previous_word = False
    for match in _GRAPHEME.finditer(text):
        grapheme = match.group()
        if grapheme.isspace():
            if ends:
                ends[-1] = match.end()
            separated = True
            continue
        current_word = _NONSEPARATED.search(grapheme) is None
        if ends and not separated and previous_word and current_word:
            ends[-1] = match.end()
        else:
            budget.take()
            ends.append(match.end())
        previous_word = current_word
        separated = False
    return (0, *(ends or [len(text)]))
