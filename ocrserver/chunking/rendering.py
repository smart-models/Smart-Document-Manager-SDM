"""Render source text and explicit generated context with code-point maps."""

from .relation_mapping import _merge
from .types import Fragment, generated_span


def source_fragment(
    source,
    start=0,
    end=None,
    *,
    role="body",
    kind="prose",
    protected=None,
    boundary_cost=0,
):
    """Slice normalized source text and retain original code-point boundaries."""
    normalized = source.normalized
    if end is None:
        end = len(normalized.text)
    text = normalized.text[start:end]
    span = {
        **source.reference(),
        "start": normalized.offsets[start],
        "end": normalized.offsets[end],
        "text_start": 0,
        "text_end": len(text),
        "role": role,
    }
    return Fragment(
        text,
        (span,),
        kind=kind,
        protected=protected,
        boundary_cost=boundary_cost,
    )


def as_context(span, role):
    """Copy a primary span as context, removing primary-only identifiers."""
    return {
        **{key: value for key, value in span.items() if key != "identifiers"},
        "role": role,
    }


def render(section, fragments, *, first=True, overlap=None):
    """Join title, overlap and primary fragments with offset-mapped separators.

    Repeated titles become context; the first primary title retains its source
    ownership. Input span dictionaries are copied before output offsets shift.
    """
    texts, primary, context = [], [], []
    length = 0

    def append(fragment):
        nonlocal length
        if texts:
            texts.append("\n\n")
            context.append(generated_span(length, length + 2))
            length += 2
        texts.append(fragment.text)
        for spans, target in (
            (fragment.source_spans, primary),
            (fragment.context_spans, context),
        ):
            target.extend(
                {
                    **span,
                    "text_start": span["text_start"] + length,
                    "text_end": span["text_end"] + length,
                }
                for span in spans
            )
        length += len(fragment.text)

    if section.title:
        title = source_fragment(
            section.title_source, role="section_title", kind="title"
        )
        if not first or not section.title_primary:
            title = Fragment(
                title.text,
                (),
                tuple(as_context(span, "section_title") for span in title.source_spans),
            )
        append(title)
    if overlap:
        append(overlap)
    for fragment in fragments:
        append(fragment)
    ignored = tuple(
        interval
        for fragment in fragments
        for interval in fragment.ignored_source_ranges
    )
    return Fragment(
        "".join(texts),
        tuple(primary),
        tuple(context),
        ignored_source_ranges=ignored,
    )


def primary_body_text(rendered):
    """Observed new body for the small-chunk cost, excluding repeated context."""
    intervals = _merge(
        (span["text_start"], span["text_end"])
        for span in rendered.source_spans
        if span["role"] != "section_title"
    )
    return "\n\n".join(rendered.text[start:end] for start, end in intervals)
