"""Add bounded observed prose suffixes after the primary partition is fixed."""

from bisect import bisect_left

from .rendering import as_context, render
from .text import lexical_boundaries, normalize_text, protected_ranges
from .types import Fragment, Section


def _suffix(fragment, offset, sources):
    """Slice rendered text and remap its provenance into overlap context.

    The offset is relative to rendered text. Source offsets remain relative to
    the original block, including any characters removed by normalization.
    """
    context = []
    for span in (*fragment.source_spans, *fragment.context_spans):
        if span["text_end"] <= offset:
            continue
        start = max(offset, span["text_start"])
        copied = {
            **span,
            "text_start": start - offset,
            "text_end": span["text_end"] - offset,
        }
        if span["source_key"]:
            source = sources[span["source_key"]]
            normalized = normalize_text(source.content[span["start"] : span["end"]])
            copied["start"] = (
                span["start"] + normalized.offsets[start - span["text_start"]]
            )
            copied = as_context(copied, "overlap")
        context.append(copied)
    return Fragment(fragment.text[offset:], (), tuple(context))


def choose_overlap(section, previous, current, options, work, sources):
    """Select the longest eligible suffix among bounded overlap candidates.

    Args:
        section: Section whose title is included when rendering the next chunk.
        previous: Primary fragments of the preceding chunk in the same section.
        current: Primary fragments being packed into the next chunk.
        options: Requested overlap and total token budgets.
        work: Request work accountant and tokenizer; evaluations consume limits.
        sources: Original blocks indexed by source key for offset reconstruction.

    Returns:
        The overlap fragment, its token count, and the next chunk's total token
        count. Returns ``(None, 0, None)`` when no candidate fits.

    Raises:
        ChunkError: If tokenization or provenance work exceeds request limits.
    """
    if not options.overlap_tokens or not previous or previous[-1].kind != "prose":
        return None, 0, None
    start = len(previous) - 1
    while start and previous[start - 1].kind == "prose":
        start -= 1
    tail = render(Section("overlap", None, None, []), previous[start:])
    regions = protected_ranges(
        tail.text,
        max_items=work.limits.max_split_evaluations,
    )
    offset = regions[-1].end if regions else 0
    if not tail.text[offset:].strip():
        return None, 0, None
    eligible = _suffix(tail, offset, sources)
    boundaries = lexical_boundaries(
        eligible.text,
        max_items=work.limits.max_split_evaluations,
    )
    atoms = len(boundaries) - 1
    candidates = {max(0, atoms - amount) for amount in (1, 2, 4, 8, 16, 32, 64, 128)}
    token_offsets = work.offsets(eligible.text)
    estimate_offset = (
        token_offsets[max(0, len(token_offsets) - options.overlap_tokens)][0]
        if token_offsets
        else 0
    )
    estimate = bisect_left(boundaries, estimate_offset)
    candidates.update(range(max(0, estimate - 3), min(atoms, estimate + 3) + 1))
    candidates.add(0)
    selected, selected_count, selected_total = None, 0, None
    # Ascending starts mean decreasing suffix length. Every selected candidate
    # is still counted; a failed shorter/longer suffix implies nothing else.
    for index in sorted(candidates):
        if index >= atoms:
            continue
        suffix = _suffix(eligible, boundaries[index], sources)
        count = work.count(suffix.text)
        rendered = render(section, current, first=False, overlap=suffix)
        total = work.count(rendered.text)
        if (
            selected is None
            and 0 < count <= options.overlap_tokens
            and total <= options.max_tokens
        ):
            selected, selected_count, selected_total = suffix, count, total
    return selected, selected_count, selected_total
