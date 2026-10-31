# calibration — Deterministic OCR fixtures and ground truth for startup autotuning

> **Read this when** inspecting or modifying startup calibration fixtures, `calibration/ground_truth.json`, `calibration/manifest.json`, or `calibration/generate.py`.
> **Audience** — an AI agent working in this repository, and any human reading over its shoulder.
> **Source of truth** — `calibration/manifest.json` for fixture SHA-256 hashes and provenance; `calibration/ground_truth.json` for expected text; `calibration/generate.py` for text normalization and synthetic rendering; [`../autotune_entrypoint.py`](../autotune_entrypoint.py) and [`../ocrserver/autotune/`](../ocrserver/README.md) for how calibration candidates are scored.
> **Prerequisite** — read [`../ocrserver/README.md`](../ocrserver/README.md) and [`../vllm/README.md`](../vllm/README.md).

---

## Purpose and boundaries

**What it is:** the self-contained, offline calibration corpus bundled into the
`ocr-api` image (`/app/calibration`). During container startup,
[`../autotune_entrypoint.py`](../autotune_entrypoint.py) runs candidate
concurrency configurations against these fixtures and compares the recognized
text to `ground_truth.json` (Character Error Rate, latency, and vLLM KV-cache
gates, with preemption recorded diagnostically) before starting Uvicorn.

**What it is not:** a general benchmark dataset (see [`../benchmarks/README.md`](../benchmarks/README.md)
for OmniDocBench and Real5). It requires no network access or external fonts at
runtime.

## Mental model

1. **Bundled font and deterministic fixtures.** Synthetic pages (`plain_text.png`,
   `dense_page.png`, `simple_table.png`, `formula.png`, `multipage.pdf`) are
   drawn with the bundled DejaVu Sans 2.37 font (`fonts/DejaVuSans.ttf`), and
   `form943.pdf` is a 3-page real-world government form with ground truth in
   `form943.txt`.
2. **Normalized Character Error Rate (CER).** `generate.normalize_text()`
   applies NFKC normalization, strips LaTeX commands (`\[a-zA-Z]+`) and HTML
   tags (`<[^>]+>`), casefolds alphanumeric characters, and collapses whitespace
   before `ocrserver.autotune.gates.cer()` computes edit distance clamped to
   `[0.0, 1.0]`.
3. **Manifest SHA-256 hashes guard fixture integrity.** `manifest.json` records
   the exact SHA-256 of every calibration file and font asset. Changing any
   fixture byte invalidates `manifest.json` and alters autotune measurements.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `generate.py` | `normalize_text()`, synthetic PNG/PDF generators, and `generate_calibration_set()` | Text normalization rules or synthetic calibration fixtures change |
| `ground_truth.json` | Ordered list of calibration items (`name`, `kind`, `path`, `ground_truth`) consumed by `autotune_entrypoint.py` | Calibration items or expected ground-truth strings change |
| `manifest.json` | SHA-256 hashes and provenance for `ground_truth.json`, all PNG/PDF/TXT fixtures, and `fonts/DejaVuSans.ttf` | Any file in `calibration/` is regenerated or updated |
| `plain_text.png`, `dense_page.png`, `simple_table.png`, `formula.png` | Single-page synthetic image fixtures covering prose, two-column dense text, tables, and formulas | Regenerated via `generate.py` only when intentionally updating the corpus |
| `multipage.pdf`, `form943.pdf`, `form943.txt` | Multi-page synthetic and real-world PDF calibration fixtures | Multi-page PDF calibration coverage changes |
| `fonts/DejaVuSans.ttf`, `fonts/LICENSE` | Bundled DejaVu Sans 2.37 TrueType font and license | Never removed; required by `generate.py` |
| [`../tests/test_calibration.py`](../tests/test_calibration.py), [`../tests/test_autotune.py`](../tests/test_autotune.py) | Offline tests verifying `manifest.json` hashes, `normalize_text()`, and autotune candidate gating | Any change in `calibration/` or `autotune_entrypoint.py` |

## Interface reference

`calibration/generate.py` provides:

- `normalize_text(text: str) -> str`: shared normalizer imported by
  `autotune_entrypoint.py` to compare VLM output with `ground_truth.json`.
- `generate_calibration_set(output_dir: Path | None = None) -> list[CalibrationItem]`:
  regenerates synthetic fixtures and `ground_truth.json` in `output_dir`.

## Configuration

Calibration behavior is controlled by environment variables read by
`autotune_entrypoint.py` and `ocrserver/autotune/profile.py` (see `.env.example`
Section 08):

| Variable | Default | Effect |
|---|---|---|
| `OCR_AUTOTUNE` | `quick` | `off` skips calibration, `quick` probes VL lanes, `full` also screens layout threads and confirms contenders |
| `OCR_TUNING_OBJECTIVE` | `balanced` | Rank by throughput, latency, or throughput/p95 |
| `OCR_AUTOTUNE_FORCE` | `false` | Recalibrate even if a matching profile exists; on failure the old file is preserved but not applied |
| `OCR_AUTOTUNE_MAX_SECONDS` | empty | Admission budget: empty means 600 seconds quick / 1200 full; running native work can overrun |
| `OCR_AUTOTUNE_PROFILE` | `/app/model_cache/autotune/hardware-profile.json` in Compose | Internal persistent profile location, fixed by Compose; empty when running standalone |

Manual `OCR_VL_LANES` and `OCR_LAYOUT_THREADS` overrides are held fixed during
calibration. The measurement uses effective API DPI, PDF limits and timeouts.
`OCR_PAGE_PARALLELISM` accepts only 1 because the predictor is serial.
Without manual overrides, full mode explores lanes 2/4/8/16 with default and
half-CPU layout threads when more than one CPU is available. The already
measured baseline is skipped, so a CPU-only improvement can also be selected.
The admission budget may stop the search before every candidate is explored.

## Behaviour contracts

- `generate._font()` raises `ValueError` immediately if `fonts/DejaVuSans.ttf`
  is missing rather than falling back to a platform-dependent default font.
- `_cer()` in `autotune_entrypoint.py` raises `ValueError` if a ground-truth
  entry normalizes to an empty string.
- Autotune candidates require complete output, mean normalized CER <= 0.60
  (an anticatastrophe guard), per-document CER worsening <= 0.01, output
  equivalence <= 0.002 CER and measured KV-cache usage < 0.95. Confirmed
  candidates need two repetitions with throughput coefficient of variation
  <= 20%; promotion needs a score gain > 5%. Preemption alone does not reject.
- These startup checks do not certify table structure, formula semantics or
  real-traffic p95. Use the benchmark corpus for independent quality and load
  qualification before making SOTA claims.

## Capabilities and limits

**You can:** run `normalize_text()` and verify all fixture hashes offline in
milliseconds, and regenerate synthetic fixtures in a temporary directory during
tests.

**You cannot:** edit `ground_truth.json` or fixture images in place without
updating `manifest.json` hashes and invalidating existing autotune expectations.

## Recipes

**Verify calibration fixture hashes and normalization offline:**

```powershell
python -m pytest tests/test_calibration.py tests/test_autotune.py tests/test_autotune_search.py tests/test_autotune_validation.py -q
```

## Verification

```powershell
python -m pytest tests/test_calibration.py tests/test_autotune.py tests/test_autotune_search.py tests/test_autotune_validation.py -q
```

## Failure modes

| Symptom | Next check |
|---|---|
| `test_calibration.py` reports a SHA-256 mismatch | A fixture or `ground_truth.json` was modified; revert unintended edits or update `manifest.json` intentionally |
| `Bundled calibration font is missing` | Confirm `calibration/fonts/DejaVuSans.ttf` is present and included in `.dockerignore` exceptions |
| All autotune candidates fail quality gate on live GPU | Check `vllm-paddleocr` model health and inspect per-candidate CER logs from `autotune_entrypoint.py` |

## Invariants

- Every file listed in `calibration/manifest.json` exists and matches its
  recorded SHA-256 hash: enforced by `tests/test_calibration.py`.
- `calibration/` is copied into the `ocr-api` runtime image (`/app/calibration`)
  in `Dockerfile` so `autotune_entrypoint.py` always has local access to the
  corpus.

## Extending it

If you add a new calibration item, include its file in `calibration/`, add its
entry to `ground_truth.json`, record its SHA-256 in `manifest.json`, and verify
in `tests/test_calibration.py` and `tests/test_autotune.py` that
the total admission budget (`OCR_AUTOTUNE_MAX_SECONDS`) remains sufficient.

## Related documents

- [Core API and autotune package](../ocrserver/README.md)
- [Stage-1 vLLM entrypoint](../vllm/README.md)
- [Environment template (Section 08)](../.env.example)
