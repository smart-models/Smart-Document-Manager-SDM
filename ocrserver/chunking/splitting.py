"""Expose finer structure only when mandatory context prevents a whole unit."""

from dataclasses import replace

import regex

from .errors import ChunkError
from .rendering import as_context, render, source_fragment
from .text import Boundary, protected_ranges, split_boundaries
from .types import Fragment, budget_details, diagnostic, generated_span


def _count(section, fragment, work):
    work.consume("split_evaluations")
    return work.count(render(section, [fragment]).text)


def split_value(text, make_fragment, section, options, work, *, code=False):
    """Find a covering partition, evaluating all allowed ends without pruning.

    The coarse-to-fine levels protect paragraphs before sentences and words.
    A failed prefix is never used to reject a longer prefix. Evaluations and
    rendered bytes are request-bounded; exhausting them is a capacity error.
    """
    if not text:
        fragment = make_fragment(0, 0)
        count = _count(section, fragment, work)
        if 0 < count <= options.max_tokens:
            return [fragment]
        raise ChunkError(
            "context_exceeds_budget",
            "Required format exceeds budget",
            details=budget_details(fragment.source_spans[0], options, count),
        )
    boundaries = split_boundaries(
        text,
        max_boundaries=work.limits.max_split_evaluations,
    )
    if code:
        boundaries = tuple(
            Boundary(
                match.end(),
                0 if match.group() == "\n" else 64,
                "line" if match.group() == "\n" else "grapheme",
            )
            for match in regex.finditer(r"\X", text)
        )
        boundaries = (Boundary(0, 0, "block"), *boundaries)
    evaluated = {}
    minimum = None
    for level in (0, 1, 4, 16, 64):
        offered = [
            item
            for item in boundaries
            if item.cost <= level or item.offset == len(text)
        ]
        n = len(offered) - 1
        # Search reachable split positions lazily. In the common case each
        # chosen maximal prefix advances a full chunk; unlike an eager all-
        # positions DP, a long word does not require quadratic tokenizations.
        # Backtracking still explores alternatives if a nonmonotone encoder
        # makes a suffix impossible. No failed prefix prunes a later end.
        dead, candidates = set(), {}
        path = [0]
        while path and path[-1] < n:
            i = path[-1]
            if i not in candidates:
                fitting = []
                for j in range(n, i, -1):
                    if j in dead:
                        continue
                    key = (offered[i].offset, offered[j].offset)
                    if key not in evaluated:
                        fragment = make_fragment(*key)
                        evaluated[key] = _count(section, fragment, work)
                    count = evaluated[key]
                    minimum = count if minimum is None else min(minimum, count)
                    if 0 < count <= options.max_tokens:
                        fitting.append(j)
                candidates[i] = iter(fitting)
            chosen = next((j for j in candidates[i] if j not in dead), None)
            if chosen is None:
                dead.add(i)
                path.pop()
            else:
                path.append(chosen)
        if path:
            result = []
            for i, j in zip(path, path[1:]):
                key = (offered[i].offset, offered[j].offset)
                result.append(
                    replace(
                        make_fragment(*key),
                        boundary_cost=offered[j].cost if j < n else 0,
                    )
                )
            return result
    context_count = 0
    if section.title:
        work.consume("split_evaluations")
        context_count = work.count(section.title)
    code = (
        "context_exceeds_budget"
        if context_count >= options.max_tokens
        else "unsplittable_content"
    )
    whole = make_fragment(0, len(text))
    required = evaluated.get((0, len(text)), minimum)
    raise ChunkError(
        code,
        "No permitted structural partition fits the token budget",
        details=budget_details(whole.source_spans[0], options, required),
    )


def _code_fragments(source, start, end, region, section, options, work):
    """Keep protected code whole or split its body with reconstructed fence context.

    Prefer line-aware boundaries and retain diagnostics when a line must split.
    Defer context/content budget failures to the section packer, which can still
    determine whether a neighboring fragment changes the final token encoding.
    """
    whole = source_fragment(
        source,
        start,
        end,
        role="code",
        kind="code",
        protected="protected_code" if options.preserve_code_blocks else None,
    )
    if (
        options.preserve_code_blocks
        or _count(section, whole, work) <= options.max_tokens
    ):
        return [whole]
    text = source.normalized.text
    body_start = region.body_start if region else start
    body_end = region.body_end if region else end

    def fragment(left, right):
        """Render a body slice with original offsets and repeated fence context."""
        body = source_fragment(
            source,
            body_start + left,
            body_start + right,
            role="code",
            kind="code",
        )
        diagnostics = ()
        if (left and text[body_start + left - 1] != "\n") or (
            body_start + right < body_end and text[body_start + right - 1] != "\n"
        ):
            diagnostics = (diagnostic("split_inside_code_line", [source]),)
        if not region:
            return replace(body, diagnostics=diagnostics)
        opening = source_fragment(source, region.start, body_start)
        closing = source_fragment(source, body_end, region.end)
        separator = "" if body.text.endswith("\n") else "\n"
        offset = len(opening.text)
        spans = tuple(
            {
                **span,
                "text_start": span["text_start"] + offset,
                "text_end": span["text_end"] + offset,
            }
            for span in body.source_spans
        )
        context = [as_context(span, "code_fence") for span in opening.source_spans]
        if separator:
            context.append(
                generated_span(
                    offset + len(body.text),
                    offset + len(body.text) + 1,
                    "code_fence",
                )
            )
        closing_offset = offset + len(body.text) + len(separator)
        context.extend(
            {
                **as_context(span, "code_fence"),
                "text_start": span["text_start"] + closing_offset,
                "text_end": span["text_end"] + closing_offset,
            }
            for span in closing.source_spans
        )
        return Fragment(
            opening.text + body.text + separator + closing.text,
            spans,
            tuple(context),
            kind="code",
            diagnostics=diagnostics,
        )

    try:
        return split_value(
            text[body_start:body_end],
            fragment,
            section,
            options,
            work,
            code=True,
        )
    except ChunkError as error:
        if error.code not in {
            "context_exceeds_budget",
            "unsplittable_content",
        }:
            raise
        return [replace(whole, failure=error)]


def _prose(source, start, end, section, options, work):
    kind = (
        "prose"
        if source.tag
        in {
            "text",
            "abstract",
            "content",
            "vertical_text",
            "reference_content",
            "aside_text",
        }
        else "other"
    )
    text = source.normalized.text

    def make(left, right):
        return source_fragment(source, start + left, start + right, kind=kind)

    whole = make(0, end - start)
    if _count(section, whole, work) <= options.max_tokens:
        return [whole]
    try:
        return split_value(text[start:end], make, section, options, work)
    except ChunkError as error:
        if error.code not in {
            "context_exceeds_budget",
            "unsplittable_content",
        }:
            raise
        # A failed standalone split does not prove that a neighbouring unit
        # cannot make the final encoding fit. The section graph decides.
        return [replace(whole, failure=error)]


def section_fragments(section, options, work):
    """Prepare ordered prose, formula, code and table fragments for packing.

    Preserve original provenance and protected regions while evaluating splits
    against exact token counts and the request's logical work limits.
    """
    fragments = []
    for source in section.sources:
        text = source.normalized.text
        if source.tag == "table":
            from .table_units import table_fragments

            fragments.extend(table_fragments(source, section, options, work))
        elif source.tag in {"display_formula", "inline_formula"}:
            fragments.append(source_fragment(source, role="formula", kind="formula"))
        elif source.tag in {"algorithm", "code"}:
            regions = protected_ranges(
                text,
                max_items=work.limits.max_split_evaluations,
            )
            fence = next(
                (
                    region
                    for region in regions
                    if region.kind == "code"
                    and region.start == 0
                    and region.end == len(text)
                ),
                None,
            )
            fragments.extend(
                _code_fragments(source, 0, len(text), fence, section, options, work)
            )
        else:
            position = 0
            # Formula regions remain in prose here; its splitting boundaries
            # prohibit internal cuts. Code is a separately protected object.
            for region in protected_ranges(
                text,
                max_items=work.limits.max_split_evaluations,
            ):
                if region.kind != "code":
                    continue
                if position < region.start:
                    fragments.extend(
                        _prose(
                            source,
                            position,
                            region.start,
                            section,
                            options,
                            work,
                        )
                    )
                fragments.extend(
                    _code_fragments(
                        source,
                        region.start,
                        region.end,
                        region,
                        section,
                        options,
                        work,
                    )
                )
                position = region.end
            if position < len(text):
                fragments.extend(
                    _prose(source, position, len(text), section, options, work)
                )
    work.consume("units", len(fragments))
    return fragments
