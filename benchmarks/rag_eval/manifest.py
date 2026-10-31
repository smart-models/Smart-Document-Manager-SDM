"""Validate frozen evaluation inputs without generating QA or model output."""

import hashlib
import json
from datetime import datetime
from math import isfinite
from pathlib import Path


def read_json(path):
    """Read local JSON and its byte identity; never follow network URLs."""
    data = Path(path).read_bytes()

    def reject_constant(value):
        raise ValueError(f"Nonfinite JSON number: {value}")

    return (
        json.loads(data, parse_constant=reject_constant),
        hashlib.sha256(data).hexdigest(),
    )


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
    )


def _pinned(value):
    return _text(value) and value.casefold() not in {"main", "latest", "head"}


def pilot_fingerprint(manifest):
    """Bind analysis artifacts to the exact pilot annotations and OCR hashes."""
    pilot = sorted(
        (item for item in manifest["documents"] if item["split"] == "pilot"),
        key=lambda item: item["document_id"],
    )
    return hashlib.sha256(
        json.dumps(
            pilot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone is required")
    return result


def _artifact(reference, directory):
    data, digest = read_json(directory / reference["path"])
    if digest != reference["sha256"] or not isinstance(data, dict):
        raise ValueError("Artifact digest or shape is invalid")
    return data


def _protocol(manifest):
    """Collect protocol errors for pinned algorithms and fair comparison tracks.

    Validate budgets, bootstrap sampling, retrieval configurations, distinct
    baselines and exact protection options without silently repairing the input.
    """
    errors = []
    protocol = manifest.get("protocol", {})
    if protocol.get("budgets") != [256, 512, 1024, 2048]:
        errors.append({"code": "invalid_budgets"})
    if (
        not _integer(protocol.get("seed"))
        or protocol["seed"] < 0
        or not _integer(protocol.get("bootstrap_samples"))
        or protocol["bootstrap_samples"] < 2000
    ):
        errors.append({"code": "invalid_bootstrap_protocol"})
    algorithms = protocol.get("algorithms", {})
    if (
        not isinstance(algorithms, dict)
        or not algorithms
        or not all(_pinned(value) for value in algorithms.values())
    ):
        errors.append({"code": "unpinned_algorithms"})
    if (
        protocol.get("candidate") not in algorithms
        or protocol.get("greedy") not in algorithms
        or protocol.get("candidate") == protocol.get("greedy")
    ):
        errors.append({"code": "missing_candidate_or_greedy"})
    configurations = protocol.get("configurations", [])
    if not isinstance(configurations, list) or len(configurations) < 2:
        errors.append({"code": "insufficient_retrieval_configurations"})
    seen = set()
    for configuration in configurations:
        identifier = configuration.get("id")
        versions = configuration.get("versions", {})
        retrieval = configuration.get("retrieval", {})
        if not _text(identifier) or identifier in seen:
            errors.append({"code": "invalid_configuration_id"})
        seen.add(identifier)
        if not all(
            _pinned(versions.get(name))
            for name in (
                "embedder",
                "embedding_tokenizer",
                "retriever",
                "reranker",
            )
        ):
            errors.append({"code": "unpinned_retrieval_versions"})
        if (
            not _text(retrieval.get("mode"))
            or not _integer(retrieval.get("context_tokens"))
            or retrieval["context_tokens"] <= 0
        ):
            errors.append({"code": "invalid_retrieval_configuration"})
    if protocol.get("tracks") != ["service", "packing"]:
        errors.append({"code": "missing_comparison_tracks"})
    roles = protocol.get("baseline_roles", {})
    if (
        set(roles) != {"fixed_tokens", "recursive", "docling"}
        or any(value not in algorithms for value in roles.values())
        or len(set(roles.values())) != 3
        or protocol.get("candidate") in roles.values()
        or protocol.get("greedy") in roles.values()
    ):
        errors.append({"code": "missing_comparison_baselines"})
    for track, protected in (("packing", False), ("service", True)):
        expected = {
            "strictly": True,
            "preserve_tables": protected,
            "preserve_code_blocks": protected,
            "overlap_tokens": 0,
            "table_header_policy": "declared",
            "marginal_policy": "metadata",
        }
        if protocol.get("options_by_track", {}).get(track) != expected:
            errors.append({"code": "invalid_track_options", "track": track})
    return errors


def _preregistration(manifest, directory, counts):
    """Validate frozen pilot-linked power and baseline-selection evidence.

    Require artifacts to precede the freeze and match the pilot fingerprint.
    Return qualification reasons, the selected baseline and valid power evidence;
    invalid artifact fields remain explicit rather than receiving default scores.
    """
    reasons, selected, power = [], None, None
    protocol = manifest["protocol"]
    try:
        frozen = _time(manifest["frozen_at"])
    except (KeyError, ValueError, TypeError, AttributeError):
        reasons.append("missing_or_invalid_freeze_time")
        frozen = None
    pilot_hash = pilot_fingerprint(manifest)
    for field in ("power_analysis", "baseline_selection"):
        reference = protocol.get(field)
        if not reference:
            reasons.append(f"missing_{field}")
            continue
        try:
            artifact = _artifact(reference, directory)
            if not frozen or _time(artifact["created_at"]) >= frozen:
                raise ValueError("Analysis must precede freeze")
            if artifact["pilot_sha256"] != pilot_hash:
                raise ValueError("Pilot changed after analysis")
            if field == "power_analysis":
                power = artifact["power_estimate"]
                if (
                    not _number(power)
                    or not 0 <= power <= 1
                    or artifact["method"] != "paired_document_simulation"
                    or artifact["target_effect"] != 0.02
                    or not _integer(artifact["simulations"])
                    or artifact["simulations"] < 2000
                ):
                    raise ValueError("Incomplete power evidence")
                if power < 0.8:
                    reasons.append("insufficient_planned_power")
                planned = artifact["planned_heldout_documents"]
                if not _integer(planned) or planned < 200:
                    raise ValueError("Invalid planned sample")
                if counts["heldout"]["documents"] < planned:
                    reasons.append("planned_sample_not_met")
            else:
                selected = artifact["selected_baseline"]
                scores = artifact["ndcg_by_baseline"]
                expected = set(protocol["algorithms"]) - {protocol["candidate"]}
                if set(scores) != expected:
                    raise ValueError("All baselines need pilot scores")
                if (
                    artifact["metric"] != "ndcg@10"
                    or selected not in protocol["algorithms"]
                    or selected == protocol["candidate"]
                    or not scores
                    or selected not in scores
                    or any(
                        not _number(value) or not 0 <= value <= 1
                        for value in scores.values()
                    )
                    or scores[selected] != max(scores.values())
                ):
                    raise ValueError("Invalid baseline selection evidence")
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            reasons.append(f"invalid_{field}")
            if field == "power_analysis":
                power = None
            else:
                selected = None
    return reasons, selected, power


def source_blocks(document):
    """Map original OCR source keys to unmodified code-point strings."""
    if isinstance(document, dict) and "response" in document:
        document = document["response"]
    pages = document.get("pages", [])
    if not isinstance(pages, list) or not pages:
        raise ValueError("missing_structured_pages")
    result, seen = {}, set()
    for page in pages:
        number = page.get("page")
        if not _integer(number) or number <= 0 or number in seen:
            raise ValueError("invalid_page_number")
        seen.add(number)
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("missing_structured_blocks")
        for index, block in enumerate(blocks):
            content = block.get("content")
            if not isinstance(content, str):
                raise ValueError("invalid_block_content")
            result[f"p{number}:b{index}"] = content
    return result


def valid_span(span, blocks):
    """Require nonempty half-open offsets in the original OCR block."""
    if not isinstance(span, dict):
        return False
    start, end, key = (
        span.get("start"),
        span.get("end"),
        span.get("source_key"),
    )
    return (
        isinstance(key, str)
        and key in blocks
        and _integer(start)
        and _integer(end)
        and 0 <= start < end <= len(blocks[key])
    )


def _documents(manifest, directory):
    """Validate source artifacts, split isolation and query evidence intervals.

    Collect per-document errors while counting pilot/heldout documents and queries.
    Detect duplicate IDs, OCR hashes and cross-split similarity-group leakage.
    """
    errors, counts, seen, groups, hashes = [], {}, set(), {}, {}
    counts = {split: {"documents": 0, "queries": 0} for split in ("pilot", "heldout")}
    for item in manifest.get("documents", []):
        document_id = item.get("document_id")

        def error(code):
            errors.append({"code": code, "document_id": document_id})

        if not _text(document_id):
            error("invalid_document_id")
        elif document_id in seen:
            error("duplicate_document_id")
        seen.add(document_id)
        split, group = item.get("split"), item.get("similarity_group")
        if split not in counts or not _text(group):
            error("invalid_split_or_group")
            continue
        if group in groups and groups[group] != split:
            error("similarity_group_leakage")
        groups[group] = split
        counts[split]["documents"] += 1
        if not _text(item.get("lang")) or not item.get("categories"):
            error("missing_language_or_categories")
        blocks = {}
        try:
            reference = item["ocr"]
            data, digest = read_json(directory / reference["path"])
            if digest != reference["sha256"]:
                error("ocr_sha256_mismatch")
            if digest in hashes:
                error(
                    "ocr_content_leakage"
                    if hashes[digest] != split
                    else "duplicate_ocr_artifact"
                )
            hashes[digest] = split
            blocks = source_blocks(data)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            error("invalid_ocr_artifact")
        queries, query_ids = item.get("queries"), set()
        if not isinstance(queries, list) or not queries:
            error("missing_queries")
            continue
        counts[split]["queries"] += len(queries)
        for query in queries:
            query_id = query.get("query_id")
            if not _text(query_id) or query_id in query_ids:
                error("invalid_or_duplicate_query_id")
            query_ids.add(query_id)
            if not _text(query.get("question")) or not _text(query.get("answer")):
                error("missing_question_or_answer")
            evidence = query.get("evidence")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not valid_span(span, blocks) for span in evidence)
            ):
                error("invalid_evidence_span")
    return counts, errors


def _validate_manifest(path):
    """Combine structural validity and qualification readiness without evaluating.

    Check corpus size, language/category coverage and preregistration evidence.
    Return the manifest hash, counts and sorted qualification reasons; a valid
    manifest alone never marks the benchmark as qualified.
    """
    manifest, digest = read_json(path)
    if (
        manifest.get("schema_version") != 1
        or not _text(manifest.get("benchmark_id"))
        or not isinstance(manifest.get("documents"), list)
    ):
        raise ValueError("Invalid manifest structure")
    counts, errors = _documents(manifest, Path(path).parent)
    errors.extend(_protocol(manifest))
    reasons = []
    for split, minimum_documents, minimum_queries in (
        ("pilot", 60, 300),
        ("heldout", 200, 1200),
    ):
        if counts[split]["documents"] < minimum_documents:
            reasons.append(f"insufficient_{split}_documents")
        if counts[split]["queries"] < minimum_queries:
            reasons.append(f"insufficient_{split}_queries")
    preregistration, selected, power = _preregistration(
        manifest,
        Path(path).parent,
        counts,
    )
    reasons.extend(preregistration)
    heldout = [item for item in manifest["documents"] if item["split"] == "heldout"]
    languages = {item.get("lang", "").split("-", 1)[0] for item in heldout}
    if not {"it", "en"} <= languages:
        reasons.append("missing_italian_or_english_coverage")
    categories = {
        category for item in heldout for category in item.get("categories", [])
    }
    required = {
        "paper_columns",
        "cv",
        "manual",
        "multipage_table",
        "merged_cells",
        "formula",
        "bibliography",
        "footnotes",
        "sidebar",
        "algorithm",
    }
    if not required <= categories:
        reasons.append("incomplete_document_category_coverage")
    if manifest["protocol"].get("packing_exclusions"):
        reasons.append("packing_exclusions_require_stratified_evaluator")
    return {
        "valid": not errors,
        "qualified": False,
        "ready_for_evaluation": not reasons and not errors,
        "manifest_sha256": digest,
        "counts": counts,
        "selected_baseline": selected,
        "power_estimate": power,
        "errors": errors,
        "qualification_reasons": sorted(reasons),
    }


def validate_manifest(path) -> dict:
    """Report input integrity and missing evidence separately from results."""
    try:
        return _validate_manifest(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {
            "valid": False,
            "qualified": False,
            "ready_for_evaluation": False,
            "manifest_sha256": None,
            "counts": {},
            "selected_baseline": None,
            "power_estimate": None,
            "qualification_reasons": ["invalid_manifest"],
            "errors": [{"code": "invalid_or_unreadable_manifest"}],
        }
