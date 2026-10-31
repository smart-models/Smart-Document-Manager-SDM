"""Sparse section DP with integer cost vectors and a separate reconstruction."""

from bisect import bisect_right
from dataclasses import dataclass

from .errors import ChunkError
from .rendering import primary_body_text, render
from .types import budget_details


@dataclass(frozen=True)
class Edge:
    """Represent a measured candidate chunk and its lexicographic packing cost."""

    start: int
    end: int
    token_count: int
    body_token_count: int
    cost: tuple[int, ...]
    overflow_reason: str | None = None


def candidate_ends(start, n, prefix, title_count, budget):
    """Offer bounded candidate ends using token sums only as an estimate.

    Every offered chunk still requires an exact rendered token count; tokenizer
    nonadditivity prevents the estimate from proving that a candidate fits.
    """
    maximum = min(start + 128, n)
    estimate = (
        bisect_right(
            prefix,
            prefix[start] + max(0, budget - title_count),
            start + 1,
            maximum + 1,
        )
        - 1
    )
    estimate = max(start + 1, estimate)
    offered = {
        start + distance for distance in (1, 2, 3, 4, 5, 6, 7, 8, 16, 32, 64, 128)
    }
    offered.update((estimate - 1, estimate, estimate + 1))
    if n <= maximum:
        offered.add(n)
    return sorted(end for end in offered if start < end <= maximum)


def _relation_cuts(fragments, relations):
    from .relation_mapping import RelationIndex

    occurrences = RelationIndex(
        (index, fragment.source_spans) for index, fragment in enumerate(fragments)
    )
    result = []
    for relation in relations:
        if relation["to"] is None or relation["status"] == "cross_section":
            continue
        first = occurrences.owners(relation["from"])
        second = occurrences.owners(relation["to"])
        if first and second:
            endpoints = first | second
            result.append((min(endpoints), max(endpoints)))
    return result


def _solve(graph, n, allow_overflow):
    costs = [None] * (n + 1)
    costs[n] = (0,) * 6
    for start in range(n - 1, -1, -1):
        for edge in graph[start]:
            if edge.overflow_reason and not allow_overflow:
                continue
            if costs[edge.end] is None:
                continue
            candidate = tuple(a + b for a, b in zip(edge.cost, costs[edge.end]))
            if costs[start] is None or candidate < costs[start]:
                costs[start] = candidate
    if costs[0] is None:
        return None
    chosen, start = [], 0
    while start < n:
        # Ends are ascending: equal suffix costs choose the first end,
        # without storing O(n)-length paths in every DP state.
        for edge in graph[start]:
            if edge.overflow_reason and not allow_overflow:
                continue
            suffix = costs[edge.end]
            if (
                suffix is not None
                and tuple(a + b for a, b in zip(edge.cost, suffix)) == costs[start]
            ):
                chosen.append(edge)
                start = edge.end
                break
    return chosen


def pack_section(section, fragments, options, work, relations=()):
    """Choose a complete deterministic partition using exactly measured edges.

    Charge candidate evaluation to request work limits. Prefer a partition
    without overflow; admit protected overflow only when strict mode is off.
    Raise ChunkError when no permitted partition covers the entire section.
    """
    if not fragments:
        if not section.title:
            return []
        work.consume("split_evaluations")
        count = work.count(section.title)
        if count > options.max_tokens:
            raise ChunkError(
                "context_exceeds_budget",
                "Section title exceeds budget",
                details=budget_details(
                    section.title_source.reference(), options, count
                ),
            )
        return [
            Edge(
                0,
                0,
                count,
                0,
                (0, 0, 0, 0, 1, (options.max_tokens - count) ** 2),
            )
        ]
    unit_counts = [work.count(fragment.text) for fragment in fragments]
    prefix = [0]
    for count in unit_counts:
        prefix.append(prefix[-1] + count)
    title_count = 0
    if section.title:
        work.consume("split_evaluations")
        title_count = work.count(section.title)
    n = len(fragments)
    links = _relation_cuts(fragments, relations)
    graph, conflicts = [[] for _ in range(n)], []
    isolated_counts = {}
    for start in range(n):
        for end in candidate_ends(start, n, prefix, title_count, options.max_tokens):
            work.consume("dp_edges")
            group = fragments[start:end]
            rendered = render(section, group, first=start == 0)
            count = work.count(rendered.text)
            if end == start + 1:
                isolated_counts[start] = count
            body_count = work.count(primary_body_text(rendered))
            overflow = None
            if count > options.max_tokens:
                if end == start + 1 and group[0].protected:
                    overflow = group[0].protected
                    option = (
                        "preserve_tables"
                        if overflow == "protected_table"
                        else "preserve_code_blocks"
                    )
                    conflicts.append(
                        budget_details(
                            group[0].source_spans[0],
                            options,
                            count,
                            conflicting_option=option,
                        )
                    )
                else:
                    continue
            if count < 1:
                continue
            cut = fragments[end - 1].boundary_cost if end < n else 0
            broken = sum(start <= first < end <= second for first, second in links)
            small = int(
                end < n and not overflow and 5 * body_count < options.max_tokens
            )
            cost = (
                int(overflow is not None),
                cut,
                broken,
                small,
                1,
                0 if overflow else (options.max_tokens - count) ** 2,
            )
            graph[start].append(Edge(start, end, count, body_count, cost, overflow))
    result = _solve(graph, n, False)
    if result is None and title_count >= options.max_tokens:
        raise ChunkError(
            "context_exceeds_budget",
            "Mandatory title prevents advancement",
            details=budget_details(
                section.title_source.reference(), options, title_count
            ),
        )
    if result is None and not options.strictly:
        result = _solve(graph, n, True)
    if result is None:
        deferred = next(
            (
                fragment.failure
                for fragment in fragments
                if fragment.failure is not None
            ),
            None,
        )
        if deferred is not None:
            raise deferred
        blocked = next(
            (
                index
                for index, count in isolated_counts.items()
                if count > options.max_tokens
            ),
            0,
        )
        first = fragments[blocked].source_spans[0]
        required = isolated_counts[blocked]
        details = budget_details(first, options, required)
        if conflicts:
            details = {
                **conflicts[0],
                "blocks": conflicts,
                "conflicts_complete": False,
            }
            code = "protected_block_exceeds_budget"
        else:
            code = (
                "context_exceeds_budget"
                if title_count >= options.max_tokens
                else "unsplittable_content"
            )
        raise ChunkError(
            code, "No permitted partition covers this section", details=details
        )
    return result
