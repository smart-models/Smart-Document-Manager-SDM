"""Evaluate supplied retrieval runs; this module never runs a model."""

from collections import Counter, defaultdict
from itertools import product
from pathlib import Path

import numpy as np

from .gates import qualification_gates
from .manifest import (
    _artifact,
    _time,
    read_json,
    source_blocks,
    valid_span,
    validate_manifest,
)
from .metrics import evidence_metrics, macro_by_document, ranking_metrics


METRICS = (
    "evidence_recall",
    "evidence_precision",
    "evidence_iou",
    "ndcg@10",
    "mrr",
    "evidence_recall@10",
    "evidence_precision@10",
    "evidence_iou@10",
)


def _record_key(record):
    return tuple(
        record[name]
        for name in (
            "document_id",
            "query_id",
            "algorithm",
            "configuration_id",
            "max_tokens",
            "track",
        )
    )


def _score(record, query, blocks, context_budget):
    """Score one validated retrieval result against original source evidence.

    Failed runs require an error code and receive zero metrics. Successful runs
    must fit the context budget and contain valid source intervals and context
    selections before ranking and deduplicated evidence metrics are calculated.
    """
    if not isinstance(record.get("success"), bool):
        raise ValueError("invalid_success")
    if not record["success"]:
        if not isinstance(record.get("error_code"), str) or not record["error_code"]:
            raise ValueError("missing_failure_reason")
        return {name: 0.0 for name in METRICS}
    used = record.get("context_tokens")
    if (
        isinstance(used, bool)
        or not isinstance(used, int)
        or not 0 <= used <= context_budget
    ):
        raise ValueError("context_budget_exceeded_or_missing")
    ranking = record.get("ranking")
    if not isinstance(ranking, list):
        raise ValueError("missing_ranking")
    top_ten, contexts = [], []
    identifiers = []
    for index, hit in enumerate(ranking):
        identifier = hit.get("chunk_id")
        spans = hit.get("spans")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(spans, list)
            or any(not valid_span(span, blocks) for span in spans)
        ):
            raise ValueError("invalid_retrieved_source_span")
        identifiers.append(identifier)
        if index < 10:
            top_ten.extend(spans)
        if identifier in record.get("context_chunk_ids", [identifier]):
            contexts.extend(spans)
    selection = record.get("context_chunk_ids", identifiers)
    if not isinstance(selection, list) or any(
        identifier not in identifiers for identifier in selection
    ):
        raise ValueError("invalid_context_selection")
    return {
        **evidence_metrics(query["evidence"], contexts),
        **{
            name + "@10": value
            for name, value in evidence_metrics(query["evidence"], top_ten).items()
        },
        **ranking_metrics(identifiers, record.get("qrels")),
    }


def _supporting_evidence(runs, run_directory, manifest_hash):
    reasons = []
    references = runs.get("supporting_evidence", {})
    for kind in ("feasibility", "error_analysis", "ablation"):
        reference = references.get(kind)
        if not reference:
            reasons.append(f"missing_{kind}_evidence")
            continue
        try:
            artifact = _artifact(reference, run_directory)
            if artifact["manifest_sha256"] != manifest_hash or artifact["kind"] != kind:
                raise ValueError("Evidence belongs to another frozen study")
            _time(artifact["created_at"])
            if kind == "feasibility" and _time(artifact["created_at"]) >= _time(
                runs["started_at"]
            ):
                raise ValueError("Feasibility must precede retrieval")
            if not artifact.get("validator_version") or not artifact.get("result"):
                raise ValueError("Missing evidence result or validator version")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            reasons.append(f"invalid_{kind}_evidence")
    return reasons


def _aggregate(scored):
    values = defaultdict(lambda: defaultdict(list))
    for record, scores in scored:
        group = (record["track"], record["algorithm"])
        metrics = {**scores, "success_rate": float(record["success"])}
        for metric, value in metrics.items():
            values[group][metric].append({**record, "value": value})
    summaries, by_document = {}, {}
    for (track, algorithm), metrics in values.items():
        summary = {}
        for metric, rows in metrics.items():
            document_values = macro_by_document(rows)
            by_document[(track, algorithm, metric)] = document_values
            summary[metric] = float(np.mean(list(document_values.values())))
        summaries.setdefault(track, {})[algorithm] = summary
    return summaries, by_document


def evaluate(manifest_path, runs_path) -> dict:
    """Audit matrix coverage and compute scores only for measured records."""
    report = validate_manifest(manifest_path)
    report.update(scores={}, matrix={"complete": False}, gates={})
    if not report["valid"]:
        return report
    manifest, _ = read_json(manifest_path)
    runs, run_sha = read_json(runs_path)
    report["run_sha256"] = run_sha
    errors = []
    if runs.get("schema_version") != 1:
        errors.append({"code": "invalid_run_schema_version"})
    if runs.get("manifest_sha256") != report["manifest_sha256"]:
        errors.append({"code": "run_manifest_mismatch"})
    try:
        if _time(runs["started_at"]) < _time(manifest["frozen_at"]):
            raise ValueError("Run preceded freeze")
    except (ValueError, TypeError, KeyError, AttributeError):
        errors.append({"code": "run_before_freeze_or_missing_start"})
    protocol = manifest["protocol"]
    if runs.get("algorithm_versions") != protocol["algorithms"]:
        errors.append({"code": "run_algorithm_versions_mismatch"})
    versions = {item["id"]: item["versions"] for item in protocol["configurations"]}
    if runs.get("configuration_versions") != versions:
        errors.append({"code": "run_configuration_versions_mismatch"})
    if runs.get("options_by_track") != protocol.get("options_by_track"):
        errors.append({"code": "run_options_mismatch"})
    configurations = {item["id"]: item for item in protocol["configurations"]}
    documents = {
        item["document_id"]: item
        for item in manifest["documents"]
        if item["split"] == "heldout"
    }
    queries, blocks, expected = {}, {}, set()
    for document_id, document in documents.items():
        data, _ = read_json(Path(manifest_path).parent / document["ocr"]["path"])
        blocks[document_id] = source_blocks(data)
        for query in document["queries"]:
            queries[(document_id, query["query_id"])] = query
            for algorithm, configuration, budget, track in product(
                protocol["algorithms"],
                configurations,
                protocol["budgets"],
                protocol["tracks"],
            ):
                expected.add(
                    (
                        document_id,
                        query["query_id"],
                        algorithm,
                        configuration,
                        budget,
                        track,
                    )
                )
    observed, scored, failures = set(), [], Counter()
    for record in runs.get("records", []):
        try:
            key = _record_key(record)
            if key not in expected or key in observed:
                raise ValueError("unexpected_or_duplicate_run_record")
            observed.add(key)
            query = queries[(record["document_id"], record["query_id"])]
            configuration = configurations[record["configuration_id"]]
            score = _score(
                record,
                query,
                blocks[record["document_id"]],
                configuration["retrieval"]["context_tokens"],
            )
            scored.append((record, score))
            if not record["success"]:
                failure = (
                    record["track"],
                    record["algorithm"],
                    record["error_code"],
                )
                failures[failure] += 1
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            code = str(error) if isinstance(error, ValueError) else "invalid_run_record"
            errors.append({"code": code})
    report["matrix"] = {
        "expected": len(expected),
        "observed": len(observed),
        "missing": len(expected - observed),
        "complete": not errors and expected == observed,
    }
    report["scores"], document_scores = _aggregate(scored)
    report["failures"] = [
        {"track": track, "algorithm": algorithm, "code": code, "count": count}
        for (track, algorithm, code), count in sorted(failures.items())
    ]
    reasons = set(report["qualification_reasons"])
    reasons.update(
        _supporting_evidence(
            runs,
            Path(runs_path).parent,
            report["manifest_sha256"],
        )
    )
    if not report["matrix"]["complete"]:
        reasons.add("incomplete_run_matrix")
    if not runs.get("runtime"):
        reasons.add("missing_runtime_evidence")
    gates = qualification_gates(
        document_scores,
        protocol,
        report["selected_baseline"],
        runs.get("runtime"),
        len(documents),
    )
    report["gates"] = gates
    for name, gate in gates.items():
        if gate["passed"] is None:
            reasons.add(f"unmeasured_{name}")
        elif not gate["passed"]:
            reasons.add(f"failed_{name}")
    report["errors"].extend(errors)
    report["valid"] = not report["errors"]
    report["qualification_reasons"] = sorted(reasons)
    report["qualified"] = (
        report["valid"]
        and not reasons
        and all(gate["passed"] is True for gate in gates.values())
    )
    report["categories"] = {}
    for category in sorted(
        {category for item in documents.values() for category in item["categories"]}
    ):
        subset = [
            (record, score)
            for record, score in scored
            if category in documents[record["document_id"]]["categories"]
        ]
        report["categories"][category] = _aggregate(subset)[0]
    report["evaluator_version"] = "rag-eval-v1"
    report["numpy_version"] = np.__version__
    return report
