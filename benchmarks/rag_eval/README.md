# benchmarks/rag_eval — Offline RAG qualification validator and evaluator

> **Read this when** validating a frozen RAG study manifest, evaluating retrieval runs against qualification gates, or modifying `benchmarks/rag_eval/`.
> **Audience** — agents and maintainers working on RAG chunking evaluation and qualification evidence.
> **Source of truth** — this file for manifest, run and gate contracts; `manifest.py`, `metrics.py`, `gates.py` and `evaluation.py` for implementation; [`../../tests/test_rag_qualification.py`](../../tests/test_rag_qualification.py) for test fixtures.
> **Prerequisite** — read the [benchmark workspace guide](../README.md) and [application reference](../../README.md).

---

## Purpose and boundaries

**What it is:** a deterministic, offline Python package (`benchmarks.rag_eval`)
that validates a frozen RAG study manifest and evaluates **existing** retrieval
run records and runtime measurements against the qualification gates.

**What it is not:** a corpus collector, QA annotator, chunking baseline runner,
power simulator, or live embedding/retrieval client. Real corpus qualification is
an external study step; the synthetic fixtures in
`tests/test_rag_qualification.py` verify validation rules, metric arithmetic,
paired bootstrap resampling, and gate logic only.

## Mental model

1. **Two-phase contract (`validate` then `evaluate`).** A study manifest must be
   frozen (`frozen_at`) after its pilot power analysis and baseline selection
   artifacts (`created_at < frozen_at`), and before retrieval runs start
   (`started_at >= frozen_at`).
2. **Evidence offsets map to original OCR blocks.** Evidence and retrieved spans
   use half-open Unicode code-point offsets `[start, end)` in the **original**
   OCR block content (`p{positive_page}:b{zero_based_block_index}`) before
   newline normalization.
3. **Complete matrix or explicit failure.** Every combination of heldout
   document, question, algorithm, retrieval configuration, budget, and track
   must be present. A failed attempt sets `success: false` and scores zero; a
   missing record leaves the matrix incomplete and prevents qualification.
4. **Qualification is fail-closed.** `qualified: true` requires full pilot/heldout
   coverage, valid pre-freeze power and baseline artifacts, valid supporting
   evidence artifacts, and every primary packing/runtime/invariant gate passing.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `__main__.py` | CLI entrypoint (`validate`, `evaluate`), atomic JSON output, exit codes | CLI arguments or exit-code rules change |
| `manifest.py` | Frozen manifest schema validation, OCR SHA-256/span checks, `pilot_fingerprint`, power and baseline artifact verification | Study manifest or pre-freeze artifact rules change |
| `metrics.py` | Span union/IoU, evidence recall/precision/IoU, graded `nDCG@10`, `MRR`, hierarchical aggregation, paired document bootstrap | Retrieval or evidence metric definitions change |
| `gates.py` | Packing recall/nDCG/success gates, chunking/OCR p95 latency gates, invariant violation checks | Qualification thresholds or runtime gate schemas change |
| `evaluation.py` | End-to-end run matrix validation, supporting evidence checks, and qualification report assembly | Run record contracts or qualification reporting change |
| [`../../tests/test_rag_qualification.py`](../../tests/test_rag_qualification.py) | Offline unit and contract tests for manifest validation, metrics, bootstrap, and gates | Any module in `benchmarks/rag_eval/` changes |

## Interface reference

Run from the repository root:

| Command | Arguments | Exit codes |
|---|---|---|
| `python -m benchmarks.rag_eval validate <manifest.json> [--output <path>]` | Validates the frozen manifest, referenced OCR files, and pre-freeze analysis artifacts | `0` ready for qualification, `1` valid for diagnostics but incomplete/unqualified, `2` invalid input |
| `python -m benchmarks.rag_eval evaluate <manifest.json> <runs.json> [--output <path>]` | Validates the manifest and evaluates retrieval runs, runtime evidence, and supporting artifacts | `0` qualified (`qualified: true`), `1` valid but unqualified (`qualified: false`), `2` invalid input |

Omit `--output` to print JSON to stdout. When `--output` is supplied, reports
are written atomically and refuse to overwrite the input manifest or runs file.

## Configuration

### Frozen manifest (`manifest.json`)

Required top-level fields: `schema_version: 1`, `benchmark_id`, timezone-aware
`frozen_at`, `documents`, and `protocol`. Each document entry has:

```json
{
  "document_id": "stable-document-id",
  "similarity_group": "original-and-related-versions",
  "split": "heldout",
  "ocr": {"path": "ocr/document.json", "sha256": "SHA256_OF_FILE_BYTES"},
  "lang": "it",
  "categories": ["manual", "formula"],
  "queries": [{
    "query_id": "q1",
    "question": "Human annotated question",
    "answer": "Human checked sufficient answer",
    "evidence": [{"source_key": "p1:b0", "start": 10, "end": 30}]
  }]
}
```

`protocol` declares:
- `budgets`: `[256, 512, 1024, 2048]` (in PaddleOCR tokens).
- `seed`: nonnegative integer; `bootstrap_samples`: `>= 2000`.
- `candidate`, `greedy`: distinct algorithm IDs.
- `algorithms`: map from algorithm ID to non-empty immutable version/revision
  (never `main`, `head`, `latest`).
- `baseline_roles`: distinct IDs under `fixed_tokens`, `recursive`, `docling`
  (all distinct from `candidate` and `greedy`).
- `tracks`: `["service", "packing"]`.
- `configurations`: at least two entries with unique `id`, `retrieval` (`mode`,
  positive `context_tokens`), and `versions` (`embedder`, `embedding_tokenizer`,
  `retriever`, `reranker`; use `"none"` when no reranker is used).
- `options_by_track`: both tracks require `strictly: true`, `overlap_tokens: 0`,
  `table_header_policy: "declared"`, `marginal_policy: "metadata"`. `service`
  sets `preserve_tables: true` and `preserve_code_blocks: true`; `packing` sets
  both to `false`.
- `power_analysis`, `baseline_selection`: `{path, sha256}` references with
  `created_at < frozen_at` and matching `pilot_sha256` from
  `benchmarks.rag_eval.manifest.pilot_fingerprint(manifest)`.

## Behaviour contracts

### Coverage and pre-freeze artifacts
- Minimum qualification coverage is **60 pilot documents / 300 pilot questions**
  and **200 heldout documents / 1,200 heldout questions**. Heldout languages must
  cover `it` and `en`; categories must cover `paper_columns`, `cv`, `manual`,
  `multipage_table`, `merged_cells`, `formula`, `bibliography`, `footnotes`,
  `sidebar`, `algorithm`.
- Both plain `/parse` responses and UI `{ "response": { "pages": [...] } }`
  export envelopes are accepted. Duplicate document IDs, repeated OCR hashes,
  cross-split similarity groups, modified OCR bytes, and invalid spans are
  rejected.
- `power_analysis` requires `method: "paired_document_simulation"`,
  `target_effect: 0.02`, `simulations >= 2000`, `power_estimate >= 0.8`, and
  `planned_heldout_documents >= 200` (which actual heldout size must meet).
- `baseline_selection` requires `metric: "ndcg@10"`, `selected_baseline`, and
  `ndcg_by_baseline` covering all non-candidate algorithms, with
  `selected_baseline` attaining the highest pilot score.

### Retrieval run contract (`runs.json`)
Top-level fields: `schema_version: 1`, `manifest_sha256`, timezone-aware
`started_at >= frozen_at`, `algorithm_versions`, `configuration_versions`,
`options_by_track`, `records`, `runtime`, and `supporting_evidence`. Each record
in `records`:

```json
{
  "document_id": "stable-document-id",
  "query_id": "q1",
  "algorithm": "candidate",
  "configuration_id": "dense",
  "max_tokens": 512,
  "track": "packing",
  "success": true,
  "context_tokens": 1800,
  "ranking": [
    {"chunk_id": "chunk-a", "spans": [
      {"source_key": "p1:b0", "start": 0, "end": 50}
    ]}
  ],
  "context_chunk_ids": ["chunk-a"],
  "qrels": {"chunk-a": 2, "unretrieved-relevant-chunk": 1}
}
```

- `context_tokens` cannot exceed the configuration's `context_tokens` budget.
- `qrels` must include unretrieved relevant chunks; grades are finite numbers in
  `[0, 10]`. Repeated chunk IDs occupy rank positions without extra gain.
- Primary evidence recall/precision/IoU use the union of selected context's
  original source intervals; `evidence_*@10` uses the top 10 ranked hits;
  `nDCG@10` uses `2^grade - 1`; `MRR` uses the full ranking.
- Aggregation averages questions within `(document, configuration, budget)`,
  weights cells equally within documents, and weights documents equally. Paired
  bootstrap resamples documents with `--seed` and `>= 2000` draws.

### Runtime evidence and gates
`runtime` supplies `chunking` (`candidate` and `greedy` latency arrays on the
same `hardware_id`), `ocr` (`baseline_latencies_ms` and
`concurrent_latencies_ms` on the same `hardware_id`), and `invariants`
(`checked_documents`, `checked_chunks`, and zero counts for
`unauthorized_overflows`, `section_crossings`, `protected_splits`,
`undisclosed_losses`, `incorrect_token_counts`, `nondeterministic_results`,
`embedding_truncations`).

Qualification requires:
1. Packing recall gain vs `greedy` `>= 0.02` with 95% CI lower bound `> 0`.
2. Packing `nDCG@10` difference vs pilot-selected baseline with 95% CI lower
   bound `> -0.01`.
3. No success-rate regression.
4. Candidate chunking p95 latency `<= 3.0x` greedy on the same hardware.
5. Concurrent OCR p95 latency `<= 1.1x` baseline.
6. Zero invariant violations across all heldout documents (`checked_chunks >= 1`).
7. Valid `supporting_evidence` (`feasibility`, `error_analysis`, `ablation`
   JSON artifacts with matching `manifest_sha256`, `kind`, `validator_version`,
   and `feasibility.created_at <= started_at`).

## Capabilities and limits

**You can:** validate small diagnostic manifests (exit `1`), validate full
frozen qualification manifests (exit `0`), and compute complete paired-bootstrap
qualification reports offline from pre-recorded runs.

**You cannot:** qualify a manifest with non-empty `packing_exclusions`, missing
analysis/supporting artifacts, mutable version strings (`main`, `head`,
`latest`), or unmeasured runtime/invariant gates.

## Recipes

**Validate a study manifest and evaluate runs:**

```powershell
python -m benchmarks.rag_eval validate C:\study\manifest.json --output C:\study\validation.json
python -m benchmarks.rag_eval evaluate C:\study\manifest.json C:\study\runs.json --output C:\study\report.json
```

**Compute `pilot_sha256` for pre-freeze analysis artifacts:**

```python
import json
from pathlib import Path
from benchmarks.rag_eval.manifest import pilot_fingerprint

manifest = json.loads(Path("manifest.json").read_text(encoding="utf-8"))
print(pilot_fingerprint(manifest))
```

## Verification

Run the offline qualification test suite from the repository root:

```powershell
python -m pytest tests/test_rag_qualification.py -q
```

## Failure modes

| Symptom | Next check |
|---|---|
| Exit code `2` on `validate` or `evaluate` | Inspect the stderr error message for schema, hash, timestamp ordering, or span bounds violations |
| Exit code `1` with `qualified: false` | Inspect `qualification_reasons` in the output JSON (e.g. pilot/heldout size below minimum, missing supporting artifact, or failed gate) |
| `pilot_sha256` mismatch on power or baseline artifact | Recompute `pilot_fingerprint(manifest)` after any pilot document, OCR hash, or QA edit, and regenerate the pre-freeze artifact before `frozen_at` |
| Output path refused | Choose an `--output` path distinct from both `<manifest.json>` and `<runs.json>` |

## Invariants

- No private corpus or article text is vendored in the repository.
- Evidence and span offsets are always validated against the byte-verified OCR
  JSON before any metric is computed (`manifest.py`, `evaluation.py`).
- Missing records or unmeasured gates never default to passing (`passed: null`
  blocks `qualified: true`).

## Extending it

Update `manifest.py`, `metrics.py`, `gates.py`, or `evaluation.py` together with
synthetic regression cases in `tests/test_rag_qualification.py`. Preserve
`schema_version: 1` invariants and fail-closed qualification behavior.

## Related documents

- [Benchmark workspace guide](../README.md)
- [RAG chunking implementation](../../ocrserver/README.md)
- [Application reference](../../README.md)
