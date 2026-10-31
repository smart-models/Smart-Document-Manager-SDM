"""Source interval metrics and paired document-level uncertainty estimates."""

from collections import defaultdict
from math import isfinite, log2

import numpy as np


def _intervals(spans):
    sources = defaultdict(list)
    for span in spans:
        start, end = span["start"], span["end"]
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end
        ):
            raise ValueError("Invalid nonempty source interval")
        sources[span["source_key"]].append((start, end))
    result = {}
    for source, intervals in sources.items():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result[source] = merged
    return result


def evidence_metrics(truth, retrieved) -> dict[str, float]:
    """Measure unique original code points, deduplicating overlap/context."""
    actual, expected = _intervals(retrieved), _intervals(truth)
    actual_size = sum(
        end - start for values in actual.values() for start, end in values
    )
    expected_size = sum(
        end - start for values in expected.values() for start, end in values
    )
    intersection = 0
    for source in actual.keys() & expected.keys():
        left, right = actual[source], expected[source]
        i = j = 0
        while i < len(left) and j < len(right):
            intersection += max(
                0,
                min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]),
            )
            if left[i][1] < right[j][1]:
                i += 1
            else:
                j += 1
    union = actual_size + expected_size - intersection
    return {
        "evidence_recall": (intersection / expected_size if expected_size else 0.0),
        "evidence_precision": (intersection / actual_size if actual_size else 0.0),
        "evidence_iou": intersection / union if union else 0.0,
    }


def ranking_metrics(ranked_ids, qrels, *, k=10) -> dict[str, float]:
    """Compute graded nDCG against all judgments and reciprocal first hit.

    Repeated IDs consume rank positions but have zero gain after the first
    occurrence. An unjudged hit has zero relevance; qrels must include the
    complete independently judged universe, including unretrieved positives.
    """
    if k != 10 or not isinstance(qrels, dict):
        raise ValueError("Qualification requires nDCG@10 and complete qrels")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or not 0 <= value <= 10
        for value in qrels.values()
    ):
        raise ValueError("Qrel grades must be finite in [0, 10]")
    seen, dcg, reciprocal = set(), 0.0, 0.0
    for rank, identifier in enumerate(ranked_ids, 1):
        gain = 0.0 if identifier in seen else qrels.get(identifier, 0.0)
        seen.add(identifier)
        if rank <= k:
            dcg += (2**gain - 1) / log2(rank + 1)
        if gain > 0 and reciprocal == 0:
            reciprocal = 1 / rank
    ideal = sum(
        (2**grade - 1) / log2(rank + 1)
        for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:k], 1)
    )
    return {"ndcg@10": dcg / ideal if ideal else 0.0, "mrr": reciprocal}


def macro_by_document(rows) -> dict[str, float]:
    """Average questions within each configuration/budget, then its cells."""
    cells, seen = defaultdict(list), set()
    for row in rows:
        key = (row["document_id"], row["configuration_id"], row["max_tokens"])
        identity = (*key, row["query_id"])
        if identity in seen or not isfinite(row["value"]):
            raise ValueError("Duplicate query result or nonfinite metric")
        seen.add(identity)
        cells[key].append(row["value"])
    documents = defaultdict(list)
    for key, values in cells.items():
        documents[key[0]].append(float(np.mean(values)))
    return {key: float(np.mean(values)) for key, values in sorted(documents.items())}


def paired_bootstrap(candidate, baseline, *, seed, samples=2000) -> dict:
    """Resample paired documents, retaining their aggregated configurations."""
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 2000:
        raise ValueError("At least 2000 bootstrap draws are required")
    if not candidate or candidate.keys() != baseline.keys():
        raise ValueError("Identical nonempty document sets are required")
    keys = sorted(candidate)
    differences = np.array([candidate[key] - baseline[key] for key in keys])
    if not np.isfinite(differences).all():
        raise ValueError("Nonfinite metric")
    random = np.random.default_rng(seed)
    means = []
    for start in range(0, samples, 128):
        indexes = random.integers(
            0,
            len(keys),
            size=(min(128, samples - start), len(keys)),
        )
        means.extend(differences[indexes].mean(axis=1).tolist())
    return {
        "difference": float(differences.mean()),
        "ci95": np.quantile(means, [0.025, 0.975]).tolist(),
        "documents": len(keys),
        "samples": samples,
        "seed": seed,
    }
