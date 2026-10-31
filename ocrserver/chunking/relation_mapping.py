"""Locate relationship endpoints in actual primary source intervals."""

from bisect import bisect_left, bisect_right
from collections import defaultdict


def _merge(intervals):
    """Return sorted unions of overlapping or touching half-open intervals."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


class RelationIndex:
    """Index owners (units or chunks), preserving partial block provenance.

    A location with null offsets denotes the whole represented block. Its
    presence requires all primary intervals, including separately rendered
    cells; merely seeing one fragment cannot prove that the object is whole.
    """

    def __init__(self, owners):
        """Index owner/span pairs for cached intersection and coverage queries."""
        self._records = defaultdict(list)
        for owner, spans in owners:
            for span in spans:
                self._records[span["source_key"]].append(
                    (span["start"], span["end"], owner)
                )
        self._starts = {}
        self._maximum_ends = {}
        self._whole = {}
        self._cache = {}
        for key, records in self._records.items():
            records.sort()
            self._starts[key] = [start for start, _, _ in records]
            maximum, ends = -1, []
            for _, end, _ in records:
                maximum = max(maximum, end)
                ends.append(maximum)
            self._maximum_ends[key] = ends
            self._whole[key] = _merge((start, end) for start, end, _ in records)

    def has_source(self, source_key):
        """Return whether any primary owner represents the source block."""
        return source_key in self._records

    def owners(self, location, *, complete=False):
        """Owners intersecting an endpoint, or containing its entire extent."""
        key, left, right = (location[name] for name in ("source_key", "start", "end"))
        cache_key = (key, left, right, complete)
        if cache_key in self._cache:
            return self._cache[cache_key]
        records = self._records.get(key, ())
        if left is not None and right is not None and records:
            if left == right:
                begin = bisect_left(self._maximum_ends[key], left)
                stop = bisect_right(self._starts[key], right)
            else:
                begin = bisect_right(self._maximum_ends[key], left)
                stop = bisect_left(self._starts[key], right)
            records = records[begin:stop]
        groups = defaultdict(list)
        for start, end, owner in records:
            if left is not None and right is not None:
                start, end = max(start, left), min(end, right)
                if start > end or (start == end and left != right):
                    continue
            groups[owner].append((start, end))
        if complete:
            required = (
                ((left, right),)
                if left is not None and right is not None
                else self._whole.get(key, ())
            )
            result = frozenset(
                owner for owner, spans in groups.items() if _merge(spans) == required
            )
        else:
            result = frozenset(groups)
        self._cache[cache_key] = result
        return result
