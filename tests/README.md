# tests — Offline Python unit, contract, property, and launcher test suites

> **Read this when** running or adding pytest suites under `tests/`, inspecting shared RAG test fixtures in `tests/fixtures/`, or checking which test file owns a runtime or CI contract.
> **Audience** — an AI agent working in this repository, and any human reading over its shoulder.
> **Source of truth** — [`../pyproject.toml`](../pyproject.toml) for pytest and Ruff configuration; `tests/fixtures/` for canonical RAG chunking JSON/schema fixtures; [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) for CI job partitioning.
> **Prerequisite** — read [`../AGENTS.md`](../AGENTS.md) and [`../ocrserver/README.md`](../ocrserver/README.md).

---

## Purpose and boundaries

**What it is:** the central offline Python test suite (~80 `test_*.py` files)
and canonical RAG JSON fixtures (`tests/fixtures/`). Every test in this
directory runs offline in seconds without an NVIDIA GPU, a running Docker stack,
model weights, or Internet access.

**What it is not:** the live GPU integration suite (live verification scripts
live in `scripts/verify_*_live.py` and `scripts/verify_rag_*.py`) or the HTML
field-guide link/contract suite (which lives in [`../guide/tests/`](../guide/README.md)).

## Mental model

1. **Inference and external services are always faked.** Tests instantiate
   isolated FastAPI apps via `ocr_server.create_app()` or MCP servers via
   `ocr_mcp.server.create_app()` backed by `httpx.MockTransport`, fake
   `PipelineBackend`, and fake S3 clients.
2. **Lightweight CI environment (`requirements/test.txt`).** The test suite
   deliberately avoids installing PaddlePaddle or CUDA wheels; `paddleocr` /
   `paddlex` interactions are isolated behind `PipelineBackend` fakes.
3. **Canonical RAG fixtures live in `tests/fixtures/`.** Because root `docs/` is
   gitignored, the normative RAG example input, output, and JSON Schema live in
   `tests/fixtures/ocr-rag-chunks.{input.example,example,schema}.json` and are
   shared with `frontend/tests/rag-workflows.mjs`.
4. **Property-based testing (`hypothesis`)** is used in chunking and input
   validation tests to verify token bounds, splitting invariants, and canonical
   hashing across arbitrary inputs.

## Map of the code

| Family | Files | What it verifies |
|---|---|---|
| **API & HTTP contracts** | `test_api_config.py`, `test_api_cors.py`, `test_api_docs.py`, `test_api_json_rag.py`, `test_api_metrics.py`, `test_api_office.py`, `test_api_storage.py`, `test_archive_only.py`, `test_element_ocr.py`, `test_pdf_utils.py`, `test_office_utils.py`, `test_markdown_normalizer.py`, `test_table_md.py`, `test_json_rag.py`, `test_quality.py`, `test_parse_quality.py`, `test_ocr_guards.py`, `test_rate_limit.py`, `test_upload_guards.py`, `test_vllm_client.py` | FastAPI endpoints, auth, rate limiting, admission queues, PDF/image/Office limits, Markdown/table normalization, and vLLM client retries/circuit breaker |
| **RAG chunking & repair** | `test_chunk_adversarial.py`, `test_chunk_api.py`, `test_chunk_config.py`, `test_chunk_contract.py`, `test_chunk_engine.py`, `test_chunk_mcp.py`, `test_chunk_metadata_fields.py`, `test_chunk_properties.py`, `test_chunk_relations.py`, `test_chunk_runtime.py`, `test_chunk_table_integration.py`, `test_chunk_tables.py`, `test_chunk_telemetry.py`, `test_chunk_text.py`, `test_chunk_tokenizer.py`, `test_chunk_validation.py`, `test_rag_guards.py`, `test_rag_refinement.py`, `test_rag_split.py`, `test_rag_split_api.py`, `test_rag_split_audit.py`, `test_rag_split_schema.py`, `test_rag_structure.py`, `test_token_count.py` | Deterministic DP packing, section/table/code preservation, overlap, RFC 8785 hashes, schema parity, and stored-archive RAG split/refinement |
| **Autotune & calibration** | `test_autotune.py`, `test_autotune_search.py`, `test_autotune_validation.py`, `test_calibration.py`, `test_pipeline_backend.py` | GPU tier selection, Stage-1 manifest, CER/KV-cache/preemption gates, fingerprint invalidation, and calibration fixture hashes |
| **S3 storage** | `test_storage.py`, `test_api_storage.py`, `test_storage_bootstrap.py`, `test_storage_browser.py`, `test_storage_guards.py`, `test_storage_setup.py`, `test_archive_names.py` | SeaweedFS bootstrap, `configure_storage.py`, least-privilege writer/browser separation, ZIP streaming, confirmation gates, and archive index reconcile |
| **MCP server & skills** | `test_mcp_server.py`, `test_mcp_tools.py`, `test_mcp_coverage.py`, `test_mcp_schema.py`, `test_mcp_client.py`, `test_mcp_office.py`, `test_mcp_office_preview.py`, `test_mcp_rag_storage.py`, `test_mcp_source_names.py`, `test_mcp_storage.py`, `test_mcp_archive_only.py`, `test_mcp_contract_fixes.py`, `test_chunk_mcp.py`, `test_smart_document_manager_skill.py`, `test_smart_document_reader_skill.py`, `test_verify_mcp_live.py` | All 14 MCP tools, OpenAPI route coverage, `COPIED_DOCUMENTS` byte parity, skill frontmatter/rules, and wheel packaging |
| **Launchers & CI gates** | `test_stack_core.py`, `test_stack_verifiers.py`, `test_stack_launchers.py`, `stack_support.py`, `test_branding_contract.py`, `test_ci_workflow.py`, `test_dependency_inventory.py`, `test_verify_dependencies.py`, `test_security_constraints.py`, `test_render_dependency_audit_issue.py`, `test_release_evidence.py`, `test_frontend_assets.py` | PowerShell/Bash launcher parity, fake-Docker recovery, token/Compose security verifiers, product branding, OSV/SBOM audit, and GitHub Actions workflow contracts |
| **Benchmarks & qualification** | `test_benchmark.py`, `test_rag_qualification.py` | OmniDocBench split stratification, atomic benchmark resume, and offline RAG qualification metrics/gates |
| **Shared RAG fixtures** | `fixtures/ocr-rag-chunks.input.example.json`, `fixtures/ocr-rag-chunks.example.json`, `fixtures/ocr-rag-chunks.schema.json` | Canonical input, expected output, and JSON Schema for RAG chunking contract tests |

## Interface reference

Run pytest from the repository root (configuration in [`../pyproject.toml`](../pyproject.toml)
sets `asyncio_mode = "auto"` and `pythonpath = [".", "ocr-mcp-server"]`):

```powershell
python -m pytest tests/ -q
```

Narrow to a domain slice for fast iteration:

```powershell
python -m pytest tests/test_chunk_*.py -q
python -m pytest tests/test_mcp_*.py -q
python -m pytest tests/test_stack_*.py -q
```

## Configuration

Configured in [`../pyproject.toml`](../pyproject.toml):
- `testpaths = ["tests"]` (the `guide/tests` suite is invoked explicitly by path)
- `pythonpath = [".", "ocr-mcp-server"]`
- `asyncio_mode = "auto"`
- Ruff per-file ignores relax docstring rules (`D`) in `tests/*.py` while
  keeping `E`, `W`, and `F` enforced on CI-gated test files.

## Behaviour contracts

- Tests must never read or mutate the repository's real `.env` file; launcher
  and config tests pass `_env_file=None` or isolated `tmp_path` files.
- Tests must never make real outbound network requests or invoke real Docker
  containers (`stack_support.py` injects a stub `docker` binary first on
  `PATH`).
- Any test creating temporary files must use pytest's `tmp_path` fixture so the
  working tree stays clean.

## Capabilities and limits

**You can:** run the entire `tests/` directory offline on Windows, Linux, or
macOS in a lightweight Python 3.11 environment (`requirements/ci.txt`).

**You cannot:** prove live GPU OCR accuracy, real LibreOffice rendering, or
real SeaweedFS S3 network behavior from these fake-backed unit tests alone.

## Recipes

**Set up the lightweight test environment and run the full suite:**

```powershell
python -m pip install -r requirements/ci.txt
python -m pytest
```

**Run the real-shell launcher suite (PowerShell + Git Bash):**

```powershell
python -m pytest tests/test_stack_core.py tests/test_stack_verifiers.py tests/test_stack_launchers.py -q
```

## Verification

In `.github/workflows/ci.yml`:
- The `unit` job runs `pytest --ignore=tests/test_stack_launchers.py --ignore=guide/tests` across Python 3.11.
- The `launchers` job runs `tests/test_stack_core.py`, `tests/test_stack_verifiers.py`, and `tests/test_stack_launchers.py`.
- The `guides` job runs `guide/tests` and `tests/test_mcp_server.py`.

## Failure modes

| Symptom | Next check |
|---|---|
| `ModuleNotFoundError: No module named 'ocr_mcp'` | Run pytest from the repository root (so `pyproject.toml` `pythonpath` applies) or install `-e ./ocr-mcp-server` |
| `test_mcp_server.py` fails on `COPIED_DOCUMENTS` | Re-copy the modified source from `ocr-skill/` or `guide/*.html` into `ocr-mcp-server/ocr_mcp/resources/docs/` |
| `test_chunk_contract.py` fails on schema parity | Synchronize `ocrserver/chunking/rag.schema.json`, `ocr-mcp-server/ocr_mcp/rag.schema.json`, and `tests/fixtures/ocr-rag-chunks.schema.json` |
| `test_stack_launchers.py` skips or fails on Windows | Ensure Git Bash (`bash.exe`) and PowerShell (`pwsh` or `powershell`) are installed |

## Invariants

- All three copies of `rag.schema.json` (`ocrserver/chunking/`, `ocr_mcp/`, and
  `tests/fixtures/`) must remain byte-for-byte identical (`test_chunk_contract.py`).
- Every subsystem `README.md` in the repository keeps the 4-line routing header
  and 13 ordered sections (`test_mcp_contract_fixes.py`).

## Extending it

Place new Python test files in `tests/test_<subject>.py`. Isolate all network,
subprocess, and model calls behind fakes or `monkeypatch`. If a new test file is
part of the lint gate in `.github/workflows/ci.yml`, format it with `ruff format`
and verify `ruff check` passes.

## Related documents

- [Repository agent guide](../AGENTS.md)
- [Core API reference](../ocrserver/README.md)
- [Frontend unit and Playwright tests](../frontend/README.md)
- [Field-guide link and contract tests](../guide/README.md)
