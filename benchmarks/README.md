# benchmarks — Reproducible OCR quality and performance evaluation workspace

> **Read this when** preparing OmniDocBench or Real5 splits, running a resumable OCR benchmark via `benchmark.py`, generating official evaluator configs, or inspecting benchmark manifests.
> **Audience** — agents and operators evaluating OCR accuracy, latency, and GPU utilization.
> **Source of truth** — [`../benchmark.py`](../benchmark.py) for the CLI harness and resume contract; `manifests/datasets.json` for pinned dataset/evaluator revisions; [`rag_eval/README.md`](rag_eval/README.md) for offline RAG qualification.
> **Prerequisite** — read the [application guide](../README.md) and [launcher reference](../scripts/README.md).

---

## Purpose and boundaries

**What it is:** the reproducible benchmark workspace for Smart Document Manager.
It holds versioned dataset/split manifests (`manifests/`), the offline RAG
qualification package (`rag_eval/`), and the gitignored runtime directories for
downloaded datasets, vendored evaluators, and benchmark outputs.

**What it is not:** a CI unit test or an automatic parameter tuner. Downloaded
datasets (`datasets/`), repository caches (`cache/`), vendored evaluators
(`evaluators/`), and generated run artifacts (`results/`) are intentionally
gitignored and restored from the immutable revisions recorded in
[`manifests/datasets.json`](manifests/datasets.json).

## Mental model

1. **Tuning and holdout splits are strictly separated.** `omnidocbench_split.json`
   partitions the 1,651 OmniDocBench pages into a stratified 160-page `tuning`
   split and a 1,491-page `holdout` split. Never use `holdout` or `full` to
   select tuning parameters.
2. **Completion is atomic and hash-verified.** Each sample record in
   `requests.jsonl` sets `completed: true` only after both the raw JSON response
   (`responses/<stem>.json`) and extracted Markdown (`predictions/<stem>.md`)
   are written atomically and their SHA-256 hashes are recorded.
3. **Resume validates parameters and artifacts.** Resuming checks that split,
   annotation SHA-256, concurrency, input format, and PDF DPI match `run.json`,
   and verifies both artifact hashes before skipping a completed sample.
4. **Empty Markdown is valid; page errors are not.** An HTTP 200 response with
   empty OCR text can be complete, whereas an HTTP 200 payload containing page
   failures is marked incomplete and retried on resume.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| [`../benchmark.py`](../benchmark.py) | CLI harness (`verify`, `prepare`, `prepare-real5`, `run`, `summarize`, `eval-config`) | Benchmark execution, resume rules, or telemetry collection change |
| `manifests/datasets.json` | Pinned Hugging Face dataset revisions, SHA-256 hashes, and evaluator image digests | Dataset or evaluator pins are updated |
| `manifests/omnidocbench_split.json` | Deterministic 160-tuning / 1,491-holdout stratified split | Split seed or tuning size changes |
| `manifests/real5_subset.json` | Deterministic 50-image (10 base pages × 5 conditions) Real5 robustness subset | Real5 subset selection changes |
| `manifests/omnidocbench_cdm_container.yaml` | Containerized evaluator template including the CDM formula metric | Official evaluator config format changes |
| [`rag_eval/`](rag_eval/README.md) | Offline RAG chunking qualification validator and evaluator | RAG study manifest, retrieval metrics, or qualification gates change |
| [`../tests/test_benchmark.py`](../tests/test_benchmark.py) | Offline unit tests for split stratification, atomic persistence, resume, and summaries | `benchmark.py` logic changes |

## Interface reference

Run `benchmark.py` from the repository root:

| Subcommand | Purpose | Key flags |
|---|---|---|
| `verify` | Verify dataset presence and annotation SHA-256 | `--dataset-root` |
| `prepare` | Generate deterministic stratified `tuning`/`holdout` split | `--dataset-root`, `--output`, `--tuning-size`, `--seed`, `--source-revision` |
| `prepare-real5` | Download deterministic 5-condition Real5 subset (`huggingface-hub` required) | `--base-dataset-root`, `--output-root`, `--split-output`, `--base-count` |
| `run` | Execute or resume a benchmark run against `/parse` | `--split`, `--output-dir`, `--url`, `--token`, `--concurrency`, `--input-format`, `--dpi`, `--resume` / `--no-resume` |
| `summarize` | Aggregate latency, throughput, token, and GPU metrics from a run directory | `--run-dir` |
| `eval-config` | Write an OmniDocBench evaluator YAML config for a run's predictions | `--ground-truth`, `--predictions`, `--output`, `--cdm` / `--no-cdm`, `--subset-ground-truth` |

## Configuration

Host-only dataset preparation (`prepare-real5`) uses the pinned downloader in
[`../requirements/benchmark.txt`](../requirements/benchmark.txt). Benchmark
execution (`run`) uses standard library + `httpx` and `Pillow` from the test or
API environment.

| Parameter / Env | Default | Effect |
|---|---|---|
| `UPSTREAM_CLIENT_TOKEN` / `--token` | empty | Bearer token sent to `/parse` when API authentication is enabled |
| `--url` | `http://127.0.0.1:8020` | Target Smart Document Manager API base URL |
| `--vllm-metrics-url` | `http://127.0.0.1:8081/metrics` | Optional vLLM Prometheus endpoint sampled before and after the run |
| `--split` | `tuning` | `tuning` (160), `holdout` (1,491), or `full` (1,651) |
| `--input-format` | `image` | Send raw page images (`image`) or single-page wrapped PDFs (`pdf` with `--dpi` and `--source-dpi`) |
| `--limit` | `0` | Maximum samples to process (`0` means all samples in the selected split) |

## Behaviour contracts

- `verify` and `run` check that `OmniDocBench.json` matches the `annotation_sha256`
  in `omnidocbench_split.json` before sending any request.
- Cancellation or an output write failure stops pending requests and the
  background `nvidia-smi` GPU monitor cleanly; unfinished samples are retried on
  the next `--resume` run.
- `--concurrency`, `--timeout`, `--gpu-sample-interval`, `--dpi`, and
  `--source-dpi` must be positive; `--warmup` and `--limit` may be zero.
- Older run records without a `completed` key are accepted on resume only when
  `status == 200`, no error is present, and both `predictions/<stem>.md` and
  `responses/<stem>.json` exist and match.

## Capabilities and limits

**You can:** verify dataset integrity offline, generate deterministic splits,
run and resume concurrent image or PDF benchmarks, sample GPU memory/utilization
and vLLM Prometheus counters, and generate official OmniDocBench evaluator configs.

**You cannot:** run `benchmark.py run` without a live, healthy stack and local
dataset files, resume a run directory with different `--split`, `--concurrency`,
`--input-format` or `--dpi` settings, or use `holdout` results to tune runtime
parameters without invalidating the holdout evaluation.

## Recipes

**Verify the dataset and prepare the split:**

```powershell
python benchmark.py verify --dataset-root benchmarks/datasets/omnidocbench
python benchmark.py prepare --dataset-root benchmarks/datasets/omnidocbench
```

**Run a resumable tuning evaluation and summarize results:**

```powershell
python benchmark.py run --split tuning --output-dir benchmarks/results/baseline-tuning
python benchmark.py summarize --run-dir benchmarks/results/baseline-tuning
```

**Generate an OmniDocBench evaluator configuration:**

```powershell
python benchmark.py eval-config --predictions benchmarks/results/baseline-tuning/predictions --output benchmarks/results/baseline-tuning/eval.yaml
```

## Verification

Run the offline benchmark harness unit suite from the repository root:

```powershell
python -m pytest tests/test_benchmark.py tests/test_rag_qualification.py -q
```

These offline tests verify split determinism, atomic artifact writes, hash
verification, resume rejection on mismatched parameters, and summary statistics
without a live server or dataset download.

## Failure modes

| Symptom | Next check |
|---|---|
| `dataset annotation hash does not match split manifest` | Check `datasets/omnidocbench/OmniDocBench.json` against `manifests/datasets.json` revision `aa1ee96d...` |
| Resume refuses with mismatched comparable settings | Use the original `--split`, `--concurrency`, `--input-format`, and `--dpi`, or choose a new `--output-dir` |
| Samples re-run unexpectedly on resume | Inspect `requests.jsonl` for page-level OCR errors or modified/missing files under `predictions/` and `responses/` |
| `prepare-real5` fails importing `huggingface_hub` | Install `python -m pip install -r requirements/benchmark.txt` |

## Invariants

- Dataset and evaluator revisions remain pinned in `manifests/datasets.json`.
- `tuning` and `holdout` splits remain disjoint and deterministic for seed
  `personal-smart-ocr-v1`: enforced by `tests/test_benchmark.py`.
- No downloaded dataset images, vendored evaluator repositories, or result
  directories are committed to Git (`.gitignore` excludes `benchmarks/datasets/`,
  `benchmarks/cache/`, `benchmarks/evaluators/`, `benchmarks/results/`).

## Extending it

When adding a benchmark metric or CLI option, update `benchmark.py`, add offline
regression coverage in `tests/test_benchmark.py`, and keep `run.json` schema
compatibility for resumed runs. If a dataset revision changes, update both
`benchmark.py` constants and `manifests/datasets.json` in the same commit.

## Related documents

- [Offline RAG qualification harness](rag_eval/README.md)
- [Application reference](../README.md)
- [Launcher reference](../scripts/README.md)
- [Requirements and benchmark pins](../requirements/README.md)
