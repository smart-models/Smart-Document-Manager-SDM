# ocrserver — Core FastAPI service, RAG chunker, autotuner, and S3 storage

> **Read this when** looking up an HTTP endpoint, request/response schema, environment variable, admission/memory limit, RAG chunking rule, S3 storage workflow, or hardware autotuning gate, or when modifying `ocr_server.py`, `autotune_entrypoint.py`, or `ocrserver/`.
> **Audience** — an AI agent working in this repository, and any human reading over its shoulder.
> **Source of truth** — this file for the HTTP and module architecture; `ocrserver/config.py` and `ocrserver/autotune/tiers.py` for settings and hardware tiers; `ocrserver/schemas.py` and `ocrserver/chunking/rag.schema.json` for JSON contracts; [`.env.example`](../.env.example) for deployment defaults.
> **Prerequisite** — read the root [`AGENTS.md`](../AGENTS.md) and [`scripts/README.md`](../scripts/README.md).

---

## Purpose and boundaries

`ocrserver/` is the core Python package served by [`../ocr_server.py`](../ocr_server.py)
inside the `ocr-api` (`smart-document-manager`) container. It sits between callers
(browser console, REST clients, and [`ocr-mcp-server`](../ocr-mcp-server/README.md))
and the GPU-backed `vllm-paddleocr` model server plus optional SeaweedFS S3 storage.

```
Client / Console / MCP ──► ocr_server.py + ocrserver/ (FastAPI, :8020) ──► vllm-paddleocr (:8080)  [GPU VLM]
                                                                       ──► storage-gateway (:8333) [Optional S3]
```

**What it owns:**

1. **Document parsing (`/parse`)** — validate PDF, image, or Office/OpenDocument
   bytes, convert Office files via headless LibreOffice, rasterize PDF pages
   within pixel budgets, run layout analysis (`PP-DocLayoutV3`, on the GPU via
   `paddlepaddle-gpu`, capped by `OCR_LAYOUT_GPU_MEMORY_MB`) and
   block recognition against `vllm-paddleocr`, normalize Markdown/tables, and
   optionally build `json_rag` structure.
2. **Focused element recognition (`/ocr`)** — recognize a single cropped image
   element (`ocr`, `table`, `formula`, `chart`, `spotting`, `seal`) under
   dedicated slot and memory admission budgets.
3. **Deterministic RAG chunking (`/chunk`, `/parse/chunks`)** — segment parsed
   OCR documents into token-bounded, section-aware RAG chunks using the local
   PaddleOCR-VL tokenizer on CPU, without calling the GPU model server when
   existing `/parse` JSON is supplied.
4. **S3 archive & RAG refinement (`/storage/*`)** — persist outputs to S3 when
   enabled, browse/download/delete stored archives using separated IAM roles,
   and refine or split stored RAG chunk plans (`rag_refinement.py`, `rag_split.py`).
5. **Pre-start hardware calibration (`autotune/`)** — benchmark candidate
   concurrency/lane/thread settings against [`../calibration/`](../calibration/README.md)
   before `exec`-ing Uvicorn, persisting a fingerprint-bound profile.

**What it is not:**

- Not the VLM weight server. Generative vision-language inference runs in the
  separate `vllm-paddleocr` container (`vllm/`).
- Not an MCP server. The MCP protocol layer lives in `ocr-mcp-server/` and
  communicates with this package exclusively over HTTP.
- Not a vector database or embedding service. It produces normative RAG chunks
  ready for downstream ingestion.

## Mental model

1. **App factory isolation.** `create_app()` in `ocr_server.py` constructs an
   independent FastAPI instance with its own `Settings`, `TokenBucket`, queues,
   `VllmClient`, `ChunkRuntime`, and `ArtifactStore`. Tests instantiate isolated
   apps via `create_app()` with fakes.
2. **Settings precedence is explicit.** `load_settings()` resolves
   **explicit environment variable > valid autotune profile (`profile.json`) >
   dataclass defaults**. Leaving concurrency variables
   empty in `.env` lets the autotuner's measured profile win. Explicit lane/thread
   overrides stay fixed during calibration; page parallelism supports only 1.
   A fallback or disabled entrypoint never applies a previously saved profile.
3. **Two separate admission gates protect memory and GPU saturation.**
   `/parse` and `/parse/chunks` use `max_queue_size` (`OCR_MAX_QUEUE_SIZE=16`).
   `/ocr` uses both `element_max_slots` (`OCR_ELEMENT_MAX_SLOTS=32`) and an
   estimated shared byte budget `element_memory_bytes` (`512 MiB`). Overflow
   returns HTTP `429`/`503` immediately rather than queuing unboundedly.
4. **Pixel and byte caps fail closed before inference.** PDF pages exceeding
   `OCR_PDF_MAX_PAGE_PIXELS` (`25M`) are rejected with HTTP `413`—never
   silently downscaled. PDF pages are grouped into blocks of up to 24 pages and
   100 M pixels (16 A4 pages at 250 DPI), at least two 8-page layout batches,
   so layout of one batch overlaps VL recognition of the previous one; pages
   stream back per page through `predict_iter`, driving per-page progress.
5. **RAG chunking (`ocrserver/chunking/`) is CPU-bound and deterministic.**
   Given identical `/parse` JSON and chunking options, dynamic-programming
   packing (`packing.py`) produces byte-identical chunks and canonical RFC 8785
   hashes (`canonical.py`).
6. **Two distinct S3 clients enforce least privilege in-process.** When storage
   is enabled, the pipeline writes archives using `OCR_S3_ACCESS_KEY` (`writer`),
   while `/storage/*` browser routes use `OCR_S3_BROWSER_ACCESS_KEY` (`browser`).
   The provisioning `admin` key is never passed to `ocr-api`.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| [`../ocr_server.py`](../ocr_server.py) | FastAPI app factory, auth, rate limiting, admission queues, `/health`, `/stats`, `/metrics`, `/parse`, `/parse/progress/{id}`, `/ocr`, `/office/pdf`, static console mount | HTTP middleware, admission, or core route handlers change |
| [`../autotune_entrypoint.py`](../autotune_entrypoint.py) | Container entrypoint: runs or reuses `autotune/` calibration, then `os.execvp` into Uvicorn | Pre-start calibration lifecycle or vLLM metric polling changes |
| `config.py` | `Settings` dataclass, validation invariants, and environment/profile precedence | Any `OCR_*` or `MAX_FILE_BYTES` setting is added or changed |
| `schemas.py` | Pydantic request/response models (`ParseJsonRequest`, `ParseResponse`, `OcrJsonRequest`, `OcrResponse`), warning builders, OpenAPI multipart helpers | Public REST JSON contracts or task enums change |
| `pipeline_backend.py` | `PipelineBackend`: orchestrates `PP-DocLayoutV3` layout detection, reading order, crop dispatch to `vllm-paddleocr`, and page assembly | Layout-to-VLM orchestration or concurrency lanes change |
| `vllm_client.py` | Async OpenAI-compatible client for `vllm-paddleocr`, circuit breaker, retries, and token usage extraction | Upstream vLLM request parameters or circuit-breaker rules change |
| `element_ocr.py` | Image decoding, DecompressionBomb/multiframe guards, spotting upscaling/parsing, and element task prompts | `/ocr` image handling or spotting coordinates change |
| `pdf_utils.py` | `pypdfium2` PDF signature detection (including BOM/leading whitespace), page count, and pixel-budget rasterization | PDF validation or rasterization DPI logic changes |
| `office_utils.py` | Headless LibreOffice (`soffice`) conversion of `.docx/.pptx/.xlsx/.doc/.ppt/.xls/.odt/.odp/.ods` in an isolated temporary profile | Supported Office formats or conversion timeouts change |
| `markdown_normalizer.py`, `table_md.py` | Post-processing of VLM Markdown and HTML-to-GFM table conversion | Markdown formatting or table normalization changes |
| `json_rag.py`, `rag_structure.py`, `rag_refinement.py`, `rag_split.py` | Inline `json_rag` hierarchy builder and stored-archive RAG structure refinement/splitting | Hierarchical section trees or stored RAG repair workflows change |
| `token_count.py` | Local Hugging Face `tokenizers` wrapper for exact PaddleOCR-VL token counting | Tokenizer loading or token counting changes |
| `storage.py`, `storage_browser.py`, `storage_index.py`, `artifacts.py`, `archive_names.py` | S3 `ArtifactStore` writer, `/storage/*` browser/download/delete routes, ZIP streaming, temporal archive index reconciled by `rebuild_index`, and archive key naming | S3 object layout, retention headers, or `/storage/*` routes change |
| `metrics.py` | Prometheus counters, histograms, and gauges (`ocr_*`) | Telemetry metric names or labels change |
| `autotune/` | `tiers.py` (GPU tier detection & model revision pin), `fingerprint.py`, `gates.py` (CER, latency, preemption, KV-cache gates), `profile.py`, `runner.py` | Hardware tier thresholds, model revision pin, or autotune gates change |
| `chunking/` | `api.py` (`/chunk`, `/parse/chunks`), `runtime.py` (worker pool), `engine.py`, `packing.py` (DP packer), `splitting.py`, `tables.py`, `table_units.py`, `relations.py`, `relation_mapping.py`, `overlap.py`, `provenance.py`, `canonical.py`, `rendering.py`, `validation.py`, `rag.schema.json` | RAG chunking algorithm, budget limits, or `rag.schema.json` contract changes |

## Interface reference

| Method & Path | Auth required? | Purpose |
|---|---|---|
| `GET /health` | No | Liveness and upstream `vllm-paddleocr` reachability (`healthy` vs `degraded`), active autotune profile, and storage status |
| `GET /stats` | Yes (when token set) | Queue depths, active requests, circuit-breaker state, and cumulative counters |
| `GET /metrics` | Yes (when token set) | Prometheus text metrics (`ocr://metrics` in MCP) |
| `POST /parse` | Yes (when token set) | Parse a PDF, image, or Office document (multipart upload or JSON base64) into structured pages, blocks, Markdown, and optional `json_rag` |
| `GET /parse/progress/{progress_id}` | Yes (when token set) | In-memory progress of a multipart `/parse` that sent `progress_id` (state, page/block counters, ETA); rate-limit exempt for console polling; `404` unknown/expired, `422` malformed id |
| `POST /ocr` | Yes (when token set) | Focused recognition on one image element (`ocr`, `table`, `formula`, `chart`, `spotting`, `seal`) |
| `POST /chunk` | Yes (when token set) | Build normative RAG chunks from existing `/parse` JSON (`parsed_document` + explicit `options.max_tokens`) without GPU inference |
| `POST /parse/chunks` | Yes (when token set) | Parse a document and immediately build RAG chunks in one request |
| `POST /office/pdf` | Yes (when token set) | Convert an Office/OpenDocument file to PDF via headless LibreOffice and return PDF bytes |
| `GET /storage/archives` | Yes + `OCR_STORAGE_BROWSER_ENABLED` | List archived `parse`, `ocr`, or `parse-chunks` jobs in S3, one cursor page at a time |
| `GET /storage/archive` | Yes + `OCR_STORAGE_BROWSER_ENABLED` | Inspect manifest and artifact metadata for the archive at `prefix` |
| `GET /storage/file` | Yes + `OCR_STORAGE_BROWSER_ENABLED` | Download a single artifact by key with content-disposition headers |
| `GET /storage/archive.zip` | Yes + `OCR_STORAGE_BROWSER_ENABLED` | Stream the archive at `prefix` as a ZIP bundle |
| `DELETE /storage/archive` | Yes + exact `confirm` | Delete the entire archive at `prefix` |
| `DELETE /storage/file` | Yes + exact `confirm` | Delete a single archived artifact |
| `POST /storage/rag/refine`, `POST /storage/rag/split` | Yes + `OCR_STORAGE_BROWSER_ENABLED` | Refine section hierarchy or preview/apply manual group splits on a stored archive |

Whenever an HTTP route is added, removed, or re-parameterized, update
[`../ocr-mcp-server/ocr_mcp/tool_map.py`](../ocr-mcp-server/ocr_mcp/tool_map.py)
in the same commit (`tests/test_mcp_coverage.py` compares the live FastAPI
OpenAPI schema against `TOOL_ROUTES`).

## Configuration

`ocrserver/config.py` owns all runtime settings. Shipped defaults and section
descriptions live in [`.env.example`](../.env.example) and
[`../guide/environment.html`](../guide/environment.html):

- **Authentication & exposure**: `OCR_SERVER_API_TOKENS` (`token:label` list;
  empty disables bearer auth), `UPSTREAM_CLIENT_TOKEN` (service token accepted as
  `@mcp`), `EXPOSE_OCR_SWAGGER`, `EXPOSE_FRONTEND`, `OCR_CORS_ORIGINS`.
- **Admission & timeouts**: `OCR_MAX_QUEUE_SIZE` (`16`), `OCR_ELEMENT_MAX_SLOTS`
  (`32`), `OCR_ELEMENT_MEMORY_BYTES` (`536870912`), `OCR_REQUEST_TIMEOUT` (`600`),
  `OCR_PAGE_TIMEOUT` (`120`), `MAX_FILE_BYTES` (`31457280`), `OCR_MAX_PAGES` (`50`).
- **PDF & Office**: `OCR_PARSE_DPI` (`250`), `OCR_PDF_MAX_PAGE_PIXELS` (`25000000`),
  `OCR_OFFICE_ENABLED` (`true`),
  `OCR_OFFICE_TIMEOUT_SECONDS` (`120`).
- **RAG chunking (`OCR_CHUNK_*`)**: `OCR_CHUNK_MAX_INPUT_BYTES` (`32 MiB`),
  `OCR_CHUNK_MAX_BLOCKS` (`20000`), `OCR_CHUNK_MAX_TOKENS` (`32768`),
  `OCR_CHUNK_MAX_WORKERS` (`2`), `OCR_CHUNK_TIMEOUT_SECONDS` (`120`).
- **Concurrency & vLLM (`OCR_VL_*`, `OCR_VLLM_*`)**: `OCR_VL_LANES`,
  `OCR_PAGE_PARALLELISM`, `OCR_LAYOUT_THREADS`, `OCR_VL_MAX_INFLIGHT`,
  `OCR_LAYOUT_GPU_MEMORY_MB` (`3584`, layout VRAM cap), `OCR_API_CPUS` (`4.0`),
  `OCR_VLLM_URL`, `OCR_VLLM_SERVED_MODEL`, `OCR_VLLM_MODEL_REVISION`.
- **S3 storage (`OCR_STORAGE_*`, `OCR_S3_*`)**: `OCR_STORAGE_ENABLED`,
  `OCR_STORAGE_BROWSER_ENABLED`, `OCR_S3_ENDPOINT`, `OCR_S3_BUCKET`, writer and
  browser credentials.

## Behaviour contracts

- **Bearer authentication (`OCR_SERVER_API_TOKENS`)** parses `token:label`
  entries at startup (malformed or duplicate entries fail fast without echoing the
  value; a leftover `OCR_SERVER_API_TOKEN` is refused) and compares the bearer
  against every accepted token with `secrets.compare_digest`. While the list is
  non-empty, `UPSTREAM_CLIENT_TOKEN` is also accepted under the label `@mcp`;
  only labels are logged: the accepted labels at startup, then one
  `request authenticated: <label> <METHOD> <path>` line per authenticated
  request. Every `POST` and `DELETE` also ends with one
  `operation <METHOD> <path>: caller= status= pages= job= duration=<s.sss>s`
  line (`-` when unknown); it never carries the query string, the file name or
  document content. Uvicorn's access lines drop the query string too, since
  storage prefixes and keys include the original file name. Only `/`, `/health`, `/assets/*` (when frontend is enabled),
  and `/docs`, `/redoc`, `/openapi.json` (when Swagger is enabled) bypass bearer auth.
- **Per-page failures do not crash `/parse`.** If one page times out or fails VLM
  recognition, `/parse` records a structured page error and warning while
  returning results for the remaining pages.
- **`/parse` works block by block and keeps finished work.** Documents run one
  at a time in arrival order (a waiting request reports `queued`). A block
  starts only if its pages can finish at the per-page pace measured so far;
  pages never reached, or lost to the request deadline, come back as
  `page_unprocessed` with one `pages_unprocessed` warning, the archive is
  marked `partial_ocr` with `unprocessed_pages`, and `json_rag` lists them too.
  A block that exceeds its own `OCR_PAGE_TIMEOUT` share with budget left yields
  `page_failed` pages and the next block runs. A client disconnect interrupts
  the running block, starts no other and skips the archive.
- **RAG chunking requires an explicit `options.max_tokens` budget.** In strict mode
  (`strictly: true`), an un-splittable protected table or code block exceeding
  `max_tokens` returns an actionable `422` budget conflict rather than silently
  breaking protection rules.
- **Storage archival is non-blocking to OCR responses when disabled, and
  explicitly reported via `X-OCR-Storage-*` headers and `storage` metadata when
  enabled.**
- **Whole-archive deletion unindexes best effort.** Once every object is gone the
  API removes the archive's index entry and ignores a failure: a leftover entry
  only costs listings one manifest read until the next `stack.* up` prunes it. A
  partial deletion keeps the entry, so the archive stays listed for a retry.

## Capabilities and limits

**You can:** parse PDFs, images, and 9 Office/OpenDocument formats; extract
layout-ordered blocks, HTML/Markdown tables, LaTeX formulas, charts, seals, and
2D text spotting coordinates; chunk documents for RAG with mathematical token
bounds and provenance; and archive outputs to S3.

**You cannot:** run `ocr-api` without an NVIDIA GPU (layout analysis uses
`paddlepaddle-gpu` 3.2.1 built for CUDA 12.6, which cannot import without
`libcuda`; verified on compute capability 8.9 — CUDA 12.6 has no Blackwell
`sm_120` target, so RTX 50-series cards need a newer Paddle build), run layout
on a different GPU from vLLM (both reserve the one `OCR_GPU_DEVICE` card), share a
card under 8 GB with vLLM at the default `OCR_LAYOUT_GPU_MEMORY_MB`, restart
`vllm-paddleocr` alone on an 8 GB card while `ocr-api` holds layout VRAM, run
`/parse` or `/ocr` without a reachable `vllm-paddleocr`
backend, process multi-frame TIFF/GIF animations on `/ocr`, exceed configured
pixel/byte/page budgets without receiving `413`/`422`, or mutate S3 archives
without the browser role enabled and exact `confirm` strings.

## Recipes

**Run a syntax check across all backend packages without starting the stack:**

```powershell
python -m compileall ocr_server.py autotune_entrypoint.py ocrserver vllm calibration
```

**Run an isolated FastAPI test client against faked inference:**

```powershell
python -m pytest tests/test_api_config.py tests/test_chunk_contract.py tests/test_storage.py -q
```

## Verification

From the repository root, using a Python 3.11 environment with
`requirements/test.txt` installed:

```powershell
python -m pytest tests/ -q
python -m ruff check pyproject.toml
```

Offline tests fake `PipelineBackend`, `VllmClient`, and S3 endpoints so no GPU
or live container is required. When changing `ocrserver/schemas.py` or routes,
always include `tests/test_mcp_coverage.py` and `tests/test_mcp_schema.py`.

## Failure modes

| Symptom | Next check |
|---|---|
| `/health` returns HTTP 200 with `"status": "degraded"` | `vllm-paddleocr` is unreachable or circuit breaker is open; inspect `vllm-paddleocr` container logs and `OCR_VLLM_URL` |
| `413 Payload Too Large` on `/parse` or `/ocr` | Check `MAX_FILE_BYTES`, `OCR_MAX_PAGES`, `OCR_PDF_MAX_PAGE_PIXELS`, or `OCR_ELEMENT_MEMORY_BYTES` |
| `422 Unprocessable Entity` on `/chunk` | Inspect the structured chunking error code (e.g. missing `options.max_tokens` or strict protected-unit overflow) |
| `503 Service Unavailable` under load | Admission queue (`OCR_MAX_QUEUE_SIZE` or `OCR_ELEMENT_MAX_SLOTS`) is full, or `ChunkRuntime` worker queue timed out |
| `/storage/*` returns `404` or `503` | Confirm `OCR_STORAGE_ENABLED=true`, `OCR_STORAGE_BROWSER_ENABLED=true`, and `OCR_S3_BROWSER_*` credentials are set |

## Invariants

- Every public route in `ocr_server.py` is either mapped in
  `ocr_mcp/tool_map.py` or explicitly listed as non-tool surface: enforced by
  `tests/test_mcp_coverage.py`.
- Model revision default `DEFAULT_MODEL_REVISION` (`c5630abae1d940eafe0697512a0325494b02ab42`)
  in `ocrserver/autotune/tiers.py` matches `compose.yml` and `.env.example`.
- `ocrserver/chunking/rag.schema.json` and `tests/fixtures/ocr-rag-chunks.schema.json`
  stay synchronized with `ocr_mcp/rag.schema.json`: enforced by
  `tests/test_chunk_contract.py` and `tests/test_chunk_mcp.py`.

## Extending it

1. **Adding or modifying a setting:** add the field and validation to
   `ocrserver/config.py`, document it in `.env.example` and `guide/environment.html`
   (plus MCP copy), and add tests in `tests/test_api_config.py` and `guide/tests/`.
2. **Adding or modifying an endpoint:** define models in `ocrserver/schemas.py`,
   register the route in `ocr_server.py` (or `chunking/api.py` /
   `storage_browser.py`), map it in `ocr-mcp-server/ocr_mcp/tool_map.py`, and
   update `ocr-skill/` if agent-facing.

## Related documents

- [Repository agent guide](../AGENTS.md)
- [MCP server reference](../ocr-mcp-server/README.md)
- [vLLM entrypoint and model tiering](../vllm/README.md)
- [SeaweedFS S3 storage subsystem](../storage/README.md)
- [Startup calibration fixtures](../calibration/README.md)
- [Web console and browser tests](../frontend/README.md)
