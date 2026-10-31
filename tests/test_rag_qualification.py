"""Synthetic test-only data verifies the evaluator, never real qualification."""

import hashlib
import json
import math
import subprocess
import sys

import pytest


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus(tmp_path):
    document = {
        "pages": [
            {
                "page": 1,
                "blocks": [
                    {"type": "text", "content": "abcdefghij"},
                ],
            }
        ]
    }
    documents = []
    for name, split in [("pilot", "pilot"), ("test", "heldout")]:
        document["pages"][0]["blocks"][0]["content"] = (
            "abcdefghij" if split == "pilot" else "klmnopqrst"
        )
        path = tmp_path / f"{name}.ocr.json"
        digest = write_json(path, document)
        documents.append(
            {
                "document_id": name,
                "similarity_group": name,
                "split": split,
                "ocr": {"path": path.name, "sha256": digest},
                "lang": "it" if split == "pilot" else "en",
                "categories": ["manual"],
                "queries": [
                    {
                        "query_id": "q1",
                        "question": "Synthetic question?",
                        "answer": "Synthetic answer.",
                        "evidence": [{"source_key": "p1:b0", "start": 0, "end": 5}],
                    }
                ],
            }
        )
    manifest = {
        "schema_version": 1,
        "benchmark_id": "synthetic-unit-test",
        "frozen_at": "2026-09-22T12:00:00Z",
        "documents": documents,
        "protocol": {
            "budgets": [256, 512, 1024, 2048],
            "seed": 42,
            "bootstrap_samples": 2000,
            "candidate": "candidate",
            "greedy": "greedy",
            "tracks": ["service", "packing"],
            "algorithms": {
                name: "test-version"
                for name in (
                    "candidate",
                    "greedy",
                    "fixed",
                    "recursive",
                    "docling",
                )
            },
            "baseline_roles": {
                "fixed_tokens": "fixed",
                "recursive": "recursive",
                "docling": "docling",
            },
            "options_by_track": {
                "packing": {
                    "strictly": True,
                    "preserve_tables": False,
                    "preserve_code_blocks": False,
                    "overlap_tokens": 0,
                    "table_header_policy": "declared",
                    "marginal_policy": "metadata",
                },
                "service": {
                    "strictly": True,
                    "preserve_tables": True,
                    "preserve_code_blocks": True,
                    "overlap_tokens": 0,
                    "table_header_policy": "declared",
                    "marginal_policy": "metadata",
                },
            },
            "configurations": [
                {
                    "id": name,
                    "retrieval": {"mode": name, "context_tokens": 4096},
                    "versions": {
                        "embedder": "test-v1",
                        "embedding_tokenizer": "test-v1",
                        "retriever": "test-v1",
                        "reranker": "none",
                    },
                }
                for name in ("dense", "hybrid")
            ],
        },
    }
    path = tmp_path / "manifest.json"
    write_json(path, manifest)
    return path, manifest


def test_small_structurally_valid_corpus_is_explicitly_not_qualified(tmp_path):
    from benchmarks.rag_eval import validate_manifest

    path, _ = corpus(tmp_path)
    report = validate_manifest(path)

    assert report["valid"] is True
    assert report["qualified"] is False
    assert report["ready_for_evaluation"] is False
    assert report["counts"] == {
        "pilot": {"documents": 1, "queries": 1},
        "heldout": {"documents": 1, "queries": 1},
    }
    assert "missing_power_analysis" in report["qualification_reasons"]
    assert "insufficient_heldout_documents" in report["qualification_reasons"]


@pytest.mark.parametrize(
    "defect,code",
    [
        ("group", "similarity_group_leakage"),
        ("sha", "ocr_sha256_mismatch"),
        ("span", "invalid_evidence_span"),
        ("duplicate", "duplicate_document_id"),
    ],
)
def test_frozen_manifest_rejects_leakage_or_unverifiable_evidence(
    tmp_path,
    defect,
    code,
):
    from benchmarks.rag_eval import validate_manifest

    path, manifest = corpus(tmp_path)
    if defect == "group":
        manifest["documents"][1]["similarity_group"] = "pilot"
    elif defect == "sha":
        manifest["documents"][0]["ocr"]["sha256"] = "0" * 64
    elif defect == "span":
        manifest["documents"][0]["queries"][0]["evidence"][0]["end"] = 20
    else:
        manifest["documents"][1]["document_id"] = "pilot"
    write_json(path, manifest)

    report = validate_manifest(path)

    assert report["valid"] is False
    assert report["qualified"] is False
    assert code in {error["code"] for error in report["errors"]}


def test_unpinned_protocol_and_a_scalar_power_claim_cannot_qualify(tmp_path):
    from benchmarks.rag_eval import validate_manifest

    path, manifest = corpus(tmp_path)
    manifest["protocol"]["budgets"] = [512]
    manifest["protocol"]["configurations"][0]["versions"] = {}
    manifest["protocol"]["power_analysis"] = {"power_estimate": 0.99}
    manifest["protocol"]["baseline_selection"] = {"selected": "recursive"}
    write_json(path, manifest)

    report = validate_manifest(path)

    assert report["valid"] is False
    assert "invalid_power_analysis" in report["qualification_reasons"]
    assert "invalid_baseline_selection" in report["qualification_reasons"]
    assert "invalid_budgets" in {item["code"] for item in report["errors"]}


def test_evidence_metrics_union_duplicate_and_overlapping_source_intervals():
    from benchmarks.rag_eval.metrics import evidence_metrics

    truth = [{"source_key": "p1:b0", "start": 4, "end": 8}]
    retrieved = [
        {"source_key": "p1:b0", "start": 0, "end": 3},
        {"source_key": "p1:b0", "start": 2, "end": 6},
        {"source_key": "p1:b0", "start": 2, "end": 6},
        {"source_key": "p1:b1", "start": 0, "end": 2},
    ]

    assert evidence_metrics(truth, retrieved) == {
        "evidence_recall": 0.5,
        "evidence_precision": 0.25,
        "evidence_iou": 0.2,
    }


def test_ndcg_uses_complete_qrels_and_never_rewards_duplicate_hits():
    from benchmarks.rag_eval.metrics import ranking_metrics

    actual = ranking_metrics(
        ["bad", "good", "good"],
        {"bad": 0, "good": 3, "missed": 2},
    )

    assert actual["mrr"] == 0.5
    assert actual["ndcg@10"] == pytest.approx(
        (7 / math.log2(3)) / (7 + 3 / math.log2(3)),
    )


def test_document_macro_weight_does_not_depend_on_question_count():
    from benchmarks.rag_eval.metrics import macro_by_document

    rows = [
        {
            "document_id": "many",
            "configuration_id": "dense",
            "max_tokens": 256,
            "query_id": str(index),
            "value": 1.0,
        }
        for index in range(10)
    ] + [
        {
            "document_id": "one",
            "configuration_id": "dense",
            "max_tokens": 256,
            "query_id": "a",
            "value": 0.0,
        }
    ]

    assert macro_by_document(rows) == {"many": 1.0, "one": 0.0}


def test_paired_bootstrap_keeps_document_pairs_and_seed_reproducible():
    from benchmarks.rag_eval.metrics import paired_bootstrap

    candidate = {"a": 0.53, "b": 0.83, "c": 0.23}
    baseline = {"a": 0.5, "b": 0.8, "c": 0.2}
    result = paired_bootstrap(candidate, baseline, seed=3, samples=2000)

    assert result["difference"] == pytest.approx(0.03)
    assert result["ci95"] == pytest.approx([0.03, 0.03])
    assert result == paired_bootstrap(candidate, baseline, seed=3, samples=2000)
    with pytest.raises(ValueError):
        paired_bootstrap(candidate, baseline, seed=3, samples=1999)


def retrieval_run(tmp_path, path, manifest):
    records = []
    for track in manifest["protocol"]["tracks"]:
        for budget in manifest["protocol"]["budgets"]:
            for configuration in manifest["protocol"]["configurations"]:
                for algorithm in manifest["protocol"]["algorithms"]:
                    records.append(
                        {
                            "document_id": "test",
                            "query_id": "q1",
                            "algorithm": algorithm,
                            "configuration_id": configuration["id"],
                            "max_tokens": budget,
                            "track": track,
                            "success": True,
                            "context_tokens": 100,
                            "ranking": [
                                {
                                    "chunk_id": "a",
                                    "spans": [
                                        {
                                            "source_key": "p1:b0",
                                            "start": 0,
                                            "end": 5,
                                        },
                                    ],
                                }
                            ],
                            "qrels": {"a": 1},
                        }
                    )
    data = {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "started_at": "2026-09-22T13:00:00Z",
        "algorithm_versions": manifest["protocol"]["algorithms"],
        "configuration_versions": {
            item["id"]: item["versions"]
            for item in manifest["protocol"]["configurations"]
        },
        "options_by_track": manifest["protocol"]["options_by_track"],
        "records": records,
    }
    run_path = tmp_path / "run.json"
    write_json(run_path, data)
    return run_path, data


def test_evaluator_reports_measured_scores_without_claiming_qualification(
    tmp_path,
):
    from benchmarks.rag_eval import evaluate

    path, manifest = corpus(tmp_path)
    run, _ = retrieval_run(tmp_path, path, manifest)
    report = evaluate(path, run)

    assert report["valid"] is True
    assert report["qualified"] is False
    assert report["scores"]["packing"]["candidate"]["evidence_recall"] == 1.0
    assert report["matrix"]["complete"] is True
    assert "missing_runtime_evidence" in report["qualification_reasons"]


def test_failed_runs_score_zero_while_absent_runs_remain_unmeasured(tmp_path):
    from benchmarks.rag_eval import evaluate

    path, manifest = corpus(tmp_path)
    run, data = retrieval_run(tmp_path, path, manifest)
    for record in data["records"]:
        if record["algorithm"] == "candidate":
            record.update(success=False, error_code="protected_block_exceeds_budget")
    data["records"].pop()
    write_json(run, data)
    report = evaluate(path, run)

    assert report["scores"]["service"]["candidate"]["evidence_recall"] == 0.0
    assert report["matrix"]["complete"] is False
    assert report["qualified"] is False
    assert "incomplete_run_matrix" in report["qualification_reasons"]


def test_measured_gates_keep_quality_latency_invariants_and_ocr_separate():
    from benchmarks.rag_eval.gates import qualification_gates

    scores = {
        ("packing", "candidate", "evidence_recall"): {"a": 0.6, "b": 0.7},
        ("packing", "greedy", "evidence_recall"): {"a": 0.5, "b": 0.6},
        ("packing", "candidate", "ndcg@10"): {"a": 0.8, "b": 0.9},
        ("packing", "recursive", "ndcg@10"): {"a": 0.8, "b": 0.9},
        ("packing", "candidate", "success_rate"): {"a": 1.0, "b": 1.0},
        ("packing", "greedy", "success_rate"): {"a": 1.0, "b": 1.0},
    }
    protocol = {
        "candidate": "candidate",
        "greedy": "greedy",
        "seed": 42,
        "bootstrap_samples": 2000,
    }
    runtime = {
        "chunking": {
            "candidate": {"hardware_id": "same-cpu", "latencies_ms": [4, 4]},
            "greedy": {"hardware_id": "same-cpu", "latencies_ms": [1, 1]},
        },
        "ocr": {
            "hardware_id": "same-gpu",
            "baseline_latencies_ms": [10, 10],
            "concurrent_latencies_ms": [12, 12],
        },
        "invariants": {
            "checked_documents": 2,
            "checked_chunks": 4,
            "unauthorized_overflows": 0,
            "section_crossings": 0,
            "protected_splits": 0,
            "undisclosed_losses": 0,
            "incorrect_token_counts": 0,
            "nondeterministic_results": 0,
            "embedding_truncations": 0,
        },
    }

    gates = qualification_gates(scores, protocol, "recursive", runtime, 2)

    assert gates["recall_gain"]["passed"] is True
    assert gates["ndcg_noninferiority"]["passed"] is True
    assert gates["success_nonregression"]["passed"] is True
    assert gates["invariants"]["passed"] is True
    assert gates["chunking_latency"]["passed"] is False
    assert gates["ocr_latency"]["passed"] is False


def add_preregistration(tmp_path, path, manifest):
    from benchmarks.rag_eval.manifest import pilot_fingerprint

    shared = {
        "created_at": "2026-09-22T11:00:00Z",
        "pilot_sha256": pilot_fingerprint(manifest),
    }
    artifacts = {
        "power_analysis": {
            **shared,
            "method": "paired_document_simulation",
            "power_estimate": 0.85,
            "target_effect": 0.02,
            "simulations": 2000,
            "planned_heldout_documents": 200,
        },
        "baseline_selection": {
            **shared,
            "metric": "ndcg@10",
            "selected_baseline": "recursive",
            "ndcg_by_baseline": {
                "greedy": 0.5,
                "recursive": 0.6,
                "fixed": 0.4,
                "docling": 0.55,
            },
        },
    }
    for name, artifact in artifacts.items():
        file = tmp_path / f"{name}.json"
        manifest["protocol"][name] = {
            "path": file.name,
            "sha256": write_json(file, artifact),
        }
    write_json(path, manifest)


def test_preregistration_is_bound_to_pilot_and_must_precede_the_freeze(
    tmp_path,
):
    from benchmarks.rag_eval import validate_manifest

    path, manifest = corpus(tmp_path)
    add_preregistration(tmp_path, path, manifest)
    valid = validate_manifest(path)

    assert valid["selected_baseline"] == "recursive"
    assert valid["power_estimate"] == 0.85
    assert valid["qualified"] is False
    manifest["documents"][0]["queries"][0]["answer"] = "Changed annotation."
    write_json(path, manifest)
    invalid = validate_manifest(path)
    assert "invalid_power_analysis" in invalid["qualification_reasons"]
    assert "invalid_baseline_selection" in invalid["qualification_reasons"]


def test_cli_writes_an_explicit_unqualified_report_for_synthetic_inputs(
    tmp_path,
):
    path, _ = corpus(tmp_path)
    output = tmp_path / "report.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.rag_eval",
            "validate",
            str(path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["qualified"] is False
    assert report["counts"]["heldout"]["documents"] == 1


def test_mrr_covers_the_supplied_ranking_beyond_the_ndcg_cutoff():
    from benchmarks.rag_eval.metrics import ranking_metrics

    result = ranking_metrics([str(i) for i in range(11)], {"10": 1})

    assert result["ndcg@10"] == 0.0
    assert result["mrr"] == pytest.approx(1 / 11)


def test_baseline_selection_cannot_omit_a_declared_competitor(tmp_path):
    from benchmarks.rag_eval import validate_manifest

    path, manifest = corpus(tmp_path)
    add_preregistration(tmp_path, path, manifest)
    file = tmp_path / "baseline_selection.json"
    data = json.loads(file.read_text())
    del data["ndcg_by_baseline"]["docling"]
    manifest["protocol"]["baseline_selection"]["sha256"] = write_json(file, data)
    write_json(path, manifest)

    report = validate_manifest(path)

    assert "invalid_baseline_selection" in report["qualification_reasons"]


def test_context_selection_is_scored_separately_from_top_ten(tmp_path):
    from benchmarks.rag_eval import evaluate

    path, manifest = corpus(tmp_path)
    run, data = retrieval_run(tmp_path, path, manifest)
    for record in data["records"]:
        record["ranking"].append(
            {
                "chunk_id": "noise",
                "spans": [
                    {"source_key": "p1:b0", "start": 5, "end": 10},
                ],
            }
        )
        record["context_chunk_ids"] = ["noise"]
    write_json(run, data)

    report = evaluate(path, run)

    assert report["scores"]["packing"]["candidate"]["evidence_recall"] == 0.0
    assert report["scores"]["packing"]["candidate"]["evidence_recall@10"] == 1.0


def test_missing_supporting_evidence_blocks_full_qualification(tmp_path):
    from benchmarks.rag_eval import evaluate

    path, manifest = corpus(tmp_path)
    run, _ = retrieval_run(tmp_path, path, manifest)

    report = evaluate(path, run)

    assert "missing_feasibility_evidence" in report["qualification_reasons"]
    assert "missing_error_analysis_evidence" in report["qualification_reasons"]
    assert "missing_ablation_evidence" in report["qualification_reasons"]


def test_packing_track_cannot_silently_reuse_protected_service_options(
    tmp_path,
):
    from benchmarks.rag_eval import validate_manifest

    path, manifest = corpus(tmp_path)
    manifest["protocol"]["options_by_track"]["packing"]["preserve_tables"] = True
    write_json(path, manifest)

    report = validate_manifest(path)

    assert report["valid"] is False
    assert "invalid_track_options" in {item["code"] for item in report["errors"]}


@pytest.mark.parametrize("bad", [None, [], {"documents": []}])
def test_malformed_manifests_return_failed_validation_not_a_success_report(
    tmp_path, bad
):
    from benchmarks.rag_eval import validate_manifest

    path = tmp_path / "bad.json"
    write_json(path, bad)

    report = validate_manifest(path)

    assert report["valid"] is False
    assert report["qualified"] is False


def test_duplicate_ocr_artifacts_cannot_inflate_the_document_sample(tmp_path):
    from benchmarks.rag_eval import validate_manifest

    path, manifest = corpus(tmp_path)
    duplicate = dict(manifest["documents"][1], document_id="duplicate")
    manifest["documents"].append(duplicate)
    write_json(path, manifest)

    report = validate_manifest(path)

    assert report["valid"] is False
    assert "duplicate_ocr_artifact" in {item["code"] for item in report["errors"]}


def test_run_contract_version_must_match_the_supported_schema(tmp_path):
    from benchmarks.rag_eval import evaluate

    path, manifest = corpus(tmp_path)
    run, data = retrieval_run(tmp_path, path, manifest)
    data["schema_version"] = 2
    write_json(run, data)

    report = evaluate(path, run)

    assert report["valid"] is False
    assert "invalid_run_schema_version" in {item["code"] for item in report["errors"]}
