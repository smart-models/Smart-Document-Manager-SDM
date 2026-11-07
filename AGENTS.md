# AGENTS.md — repository guide for coding agents

This is the canonical instruction file for any AI coding agent working in this
repository. It covers what spans the whole repo; each subsystem's own
`README.md` covers that subsystem in depth.

**Precedence:** a direct instruction from the user beats this file; this file
beats an agent's defaults.

---

## Working agreement

- Start by checking `git status --short` and preserve changes that were already
  present. Do not reset, checkout, stash, commit, or push unrelated work.
- Work from the repository root. Keep service boundaries intact: communication
  between `ocr-mcp` and `ocr-api`, and between `ocr-api` and `vllm-paddleocr` or
  the optional storage gateway, goes through explicit HTTP/S3 clients and API
  contracts — never shared runtime imports across service images.
- Read the relevant subsystem `README.md` before changing an API, setting,
  Compose shape, model/autotune behaviour, MCP tool contract, or live verifier.
  Use `rg` to inspect the smallest relevant section; do not load `ocr_server.py`
  end to end when a targeted search suffices.
- Never place real tokens, `.env` values, model caches, generated archives, or
  local scratch files in a patch. Do not print credentials while diagnosing a
  problem.
- A change is not complete until the focused tests and checks appropriate to
  its risk have run, or the final hand-off says exactly what could not run.

## Repository shape

Three **independently-imaged application services** (plus an optional three-part
`storage` profile), unified by one root Docker Compose file, one `.env`
contract and one launcher planner — not a shared runtime package between API
and MCP.

```
Client / MCP client → ocr-mcp (MCP, 8021) ┐
Client / Console ─────────────────────────┴→ ocr-api (FastAPI, 8020) → vllm-paddleocr (vLLM OpenAI API, 8080; host docs 8081)  [VL inference]
                                                                     → storage-gateway (8333) → seaweedfs [optional S3 archive]
```

| Path | What it is |
|---|---|
| `ocr_server.py` / `ocrserver/` | The FastAPI application clients and the browser console talk to. Owns `/parse`, `/parse/progress/{id}`, `/ocr`, `/chunk`, `/parse/chunks`, `/office/pdf`, `/storage/*`, `/health`, `/stats`, `/metrics`, auth, rate limiting, backpressure, PaddleOCR layout + VL orchestration, deterministic RAG chunking, Office-to-PDF conversion via headless LibreOffice, and S3 archive browsing/retention. |
| `autotune_entrypoint.py` / `ocrserver/autotune/` | Pre-start hardware fingerprinter and calibration runner for `ocr-api`. Probes GPU tier and runtime capacity, persists the hardware profile, and launches Uvicorn. |
| `vllm/` / `Dockerfile.vllm` | `stage1_entrypoint.py` builds the vLLM model-server command for `PaddleOCR-VL-1.6` (served as `PaddleOCR-VL-0.9B`); `Dockerfile.vllm` pins the upstream `vllm/vllm-openai` digest and applies security package remediation. |
| `ocr-mcp-server/` | **Optional** MCP server (`ocr_mcp/`) exposing the OCR API as 14 tools, 11 resources (`ocr://metrics`, 4 `docs://ocr/*` skill references, 6 `docs://guide/*` HTML field guides) and 3 workflow prompts (`ocr_extract_document`, `ocr_diagnose`, `ocr_prepare_rag`). Built as the `ocr-mcp` target of the root `Dockerfile`; packaged via `ocr-mcp-server/pyproject.toml`. |
| `ocr-skill/` | Two client-side agent skills: `smart-document-manager` (drives the `ocr_*` MCP tools) and `smart-document-reader` (interprets `/parse` JSON or its stored archive — blocks, reading order, tables, captions and provenance — without re-running OCR). Prompt-side documentation only — no runtime role. |
| `guide/` | Six standalone offline HTML field guides — `getting-started`, `cli`, `environment`, `architecture`, `document-pipeline`, `rag-chunking` — plus `FONT-LICENSES.txt`, `README.md`, and `guide/tests/`. Each page embeds its own CSS, classic JS and WOFF2 fonts, so it opens over `file://` with no server, build step or network. **Copied verbatim into the MCP server — see [Copied documents](#copied-documents).** |
| `frontend/` | Prebuilt static web console in `frontend/dist/`, Node unit tests (`frontend/tests/*.test.mjs`), mocked/live Playwright suites (`frontend/tests/*.spec.mjs`), offline guide Playwright specs (`frontend/guide-tests/`), and the E2E acceptance book (`frontend/tests/testbook/`). |
| `storage/` | Configuration templates and initialization logic for the optional `storage` Compose profile (SeaweedFS S3 archive + nginx `storage-gateway` + one-shot `storage-init`). |
| `calibration/` | Deterministic calibration fixtures, synthetic generators, and `ground_truth.json` used by startup autotuning and quality regression tests. |
| `benchmark.py` / `benchmarks/` | Host-side OmniDocBench / Real5 evaluation harness, dataset split manifests, and vendored evaluator configs. |
| `scripts/` | What the launchers, operators and CI call: the shared planner `stack_core.py`; preflight verifiers `verify_token_config.py` and `verify_compose_security.py`; `configure_storage.py` and the archive migration `split_archive_markdown.py`; dependency/SBOM/audit gates (`verify_dependencies.py`, `verify_dependency_inventory.py`, `security_constraints.py`, `render_dependency_audit_issue.py`, `release_evidence.py`); offline wheel/asset verifiers (`verify_frontend_assets.py`, `verify_mcp_wheel.py`, `verify_model_alignment.py`); and opt-in live verifiers (`verify_api_live.py`, `verify_autotune_live.py`, `verify_mcp_live.py`, `verify_storage_live.py`, `verify_rag_smoke.py`, `verify_rag_real_corpus.py`). |
| `stack.ps1` / `stack.sh` | **At the repository root.** The supported cross-platform entry points to verify, build, start, inspect, and stop the stack. |
| `requirements/` | Direct dependency manifests (`api.txt`, `test.txt`, `benchmark.txt`, `dev.txt`, `ci.txt`, `vllm-security.txt`), `security-constraints.txt`, `audit-triage.json`, `inventory.json`, and captured Linux image package inventories under `locks/`. [`requirements/README.md`](requirements/README.md) is its reference. |
| `.github/` | Three workflows (`workflows/ci.yml`, `workflows/dependency-audit.yml`, `workflows/release-check.yml`), `dependabot.yml`, [`.github/AGENTS.md`](.github/AGENTS.md), and [`.github/RELEASE_CHECKLIST.md`](.github/RELEASE_CHECKLIST.md). |
| `compose.yml` / `Dockerfile` / `Dockerfile.vllm` | Root Compose definition (`ocr-api`, `vllm-paddleocr`, `ocr-mcp`, plus optional `storage` profile services `seaweedfs`, `storage-gateway`, `storage-init`), multi-target `Dockerfile` (`ocr-api` and `ocr-mcp`), and the vLLM security image build (`Dockerfile.vllm`). |
| `tests/` | Central offline pytest suite (~80 test files) covering API endpoints, chunking, autotune, calibration, storage, MCP tools/schemas/resources, skills, launchers, and CI/dependency contracts. |
| `docs/` | **Gitignored working material** — untracked specs, PRDs, local verification artifacts and agent notes live there; never `git add -f` them, and do not assume the directory exists. `ocr-mcp-server/ocr_mcp/resources/docs/` is the one exception: it is **versioned** and ships inside the MCP package. |

## Where to look first

Read the right document instead of the whole repo. Each is the single source of
truth for its subject; none repeats another.

| Question | Document |
|---|---|
| How do I build/start/stop the stack? Which flag does what? | [`scripts/README.md`](scripts/README.md) |
| How is the FastAPI backend structured? Endpoints, admission, autotune, RAG chunking, Office, storage. | [`ocrserver/README.md`](ocrserver/README.md) and [`README.md`](README.md) |
| How does Stage-1 GPU tiering and the `vllm-paddleocr` entrypoint work? | [`vllm/README.md`](vllm/README.md) |
| How does the optional SeaweedFS S3 archive and least-privilege IAM bootstrap work? | [`storage/README.md`](storage/README.md) |
| How are startup calibration fixtures and ground truth maintained? | [`calibration/README.md`](calibration/README.md) |
| How do I run or extend the web console and Node/Playwright suites? | [`frontend/README.md`](frontend/README.md) |
| How is the offline pytest suite organized and what fixtures does it use? | [`tests/README.md`](tests/README.md) |
| How do I run OmniDocBench / Real5 benchmarks or offline RAG qualification? | [`benchmarks/README.md`](benchmarks/README.md) and [`benchmarks/rag_eval/README.md`](benchmarks/rag_eval/README.md) |
| Environment variables, precedence, token rules, and security boundaries. | [`.env.example`](.env.example) and [`guide/environment.html`](guide/environment.html) |
| MCP tools, transports, input locality, confirmation gates, resources, prompts. | [`ocr-mcp-server/README.md`](ocr-mcp-server/README.md) |
| Client-side agent skills (`smart-document-manager` and `smart-document-reader`). | [`ocr-skill/README.md`](ocr-skill/README.md), with [`smart-document-manager/SKILL.md`](ocr-skill/smart-document-manager/SKILL.md) and [`smart-document-reader/SKILL.md`](ocr-skill/smart-document-reader/SKILL.md) |
| Offline HTML field guides: startup, CLI, environment, architecture. | [`guide/README.md`](guide/README.md) |
| Dependency manifests, captured image inventories, OSV audit, triage decisions. | [`requirements/README.md`](requirements/README.md) |
| CI jobs, scheduled dependency audit, release gate and evidence bundle. | [`.github/AGENTS.md`](.github/AGENTS.md) and [`.github/RELEASE_CHECKLIST.md`](.github/RELEASE_CHECKLIST.md) |
| Every environment variable, with its shipped default. | `.env.example` (versioned, exhaustive) |

Each subsystem `README.md` (`ocrserver/`, `vllm/`, `storage/`, `calibration/`,
`frontend/`, `tests/`, `benchmarks/`, `benchmarks/rag_eval/`, `scripts/`,
`ocr-mcp-server/`, `ocr-skill/`, `guide/`, `requirements/`) and
`.github/AGENTS.md` follows one fixed skeleton — `Purpose
and boundaries`, `Mental model`, `Map of the code`, `Interface reference`,
`Configuration`, `Behaviour contracts`, `Capabilities and limits`, `Recipes`,
`Verification`, `Failure modes`, `Invariants`, `Extending it`, `Related
documents` — so the right section is findable without reading the file end to
end. `Capabilities and limits` answers "can it do X?"; `Invariants` answers
"may I change this?".

The root `README.md` is written for the human operator and is **owned by the
repository owner** — do not rewrite its structure unless asked directly.

### Copied documents

The `ocr-mcp` container image and wheel ship `ocr_mcp/` alone, so the MCP
server carries its own **byte-for-byte copies** of ten repository documents
defined in `COPIED_DOCUMENTS` (`ocr-mcp-server/ocr_mcp/resources/docs.py`) and
serves them as `docs://` resources. `tests/test_mcp_server.py`
(`test_skill_yaml_length_references_and_packaged_documents`) compares every
copy against its canonical source on every run: editing an original without
re-copying it **fails CI**.

| Canonical source | Copy under `ocr-mcp-server/ocr_mcp/resources/docs/` | Resource URI |
|---|---|---|
| `ocr-skill/smart-document-manager/SKILL.md` | `skill.md` | `docs://ocr/skill` |
| `ocr-skill/README.md` | `skill-readme.md` | `docs://ocr/skill-readme` |
| `ocr-skill/smart-document-manager/references/reference.md` | `reference.md` | `docs://ocr/reference` |
| `ocr-skill/smart-document-manager/references/playbooks.md` | `playbooks.md` | `docs://ocr/playbooks` |
| `guide/getting-started.html` | `guide/getting-started.html` | `docs://guide/getting-started` |
| `guide/cli.html` | `guide/cli.html` | `docs://guide/cli` |
| `guide/environment.html` | `guide/environment.html` | `docs://guide/environment` |
| `guide/architecture.html` | `guide/architecture.html` | `docs://guide/architecture` |
| `guide/document-pipeline.html` | `guide/document-pipeline.html` | `docs://guide/document-pipeline` |
| `guide/rag-chunking.html` | `guide/rag-chunking.html` | `docs://guide/rag-chunking` |

Re-copy in the **same commit** as the edit, then confirm with
`python -m pytest tests/test_mcp_server.py guide/tests -q`. This is the one
place where "each fact lives in exactly one document" does not apply: the
duplication is deliberate and mechanically enforced, not drift.

## Common commands

Run everything from the repository root. Copy `.env.example` to `.env` and
configure tokens before any `up`.

### Running the stack

```powershell
pwsh stack.ps1 verify               # check tokens, storage and Compose security; touch no Docker state
pwsh stack.ps1 up                   # verify, build, start in readiness order and print URLs
pwsh stack.ps1 up -NoMcp -DryRun    # print the planned steps without MCP, run nothing
pwsh stack.ps1 down
pwsh stack.ps1 help                 # the authoritative command surface
```

```bash
bash stack.sh verify
bash stack.sh up --wait
bash stack.sh down
bash stack.sh help
```

[`scripts/README.md`](scripts/README.md) is the launcher reference — every
command, flag, precedence rule, recipe and exit code. Do not reproduce a
command list elsewhere: `tests/test_stack_launchers.py` and
`tests/test_stack_core.py` enforce parity between `stack.ps1`, `stack.sh`, and
`stack_core.py`.

### Offline test suites

Install lightweight test dependencies (faked inference; no Paddle or GPU wheels
required) from `requirements/ci.txt` or `requirements/test.txt`:

Windows (PowerShell):

```powershell
python -m pip install -r requirements/ci.txt
python -m pytest tests -q --ignore=tests/test_stack_core.py --ignore=tests/test_stack_launchers.py --ignore=tests/test_stack_verifiers.py
python -m pytest tests/test_stack_core.py tests/test_stack_launchers.py tests/test_stack_verifiers.py -q
python -m pytest guide/tests -q
```

Unix-like shells:

```bash
python3 -m pip install -r requirements/ci.txt
python3 -m pytest tests -q --ignore=tests/test_stack_core.py --ignore=tests/test_stack_launchers.py --ignore=tests/test_stack_verifiers.py
python3 -m pytest tests/test_stack_core.py tests/test_stack_launchers.py tests/test_stack_verifiers.py -q
python3 -m pytest guide/tests -q
```

Note that `tests/test_chunk_contract.py` includes a Node-backed planner parity
check, so `node` (Node 24+) and `frontend/node_modules` (`npm ci --ignore-scripts`
in `frontend/`) must be present for the full `tests/` pass.

### Frontend and browser suites

Three browser/Node test families live under `frontend/`:

| Family | Needs a live stack? | How to run (from `frontend/`) |
|---|---|---|
| `tests/*.test.mjs` — pure Node unit tests (planners, math, state, helpers) | No | `npm test` (i.e. `node --test tests/*.test.mjs`) |
| Mocked Playwright suite (`--project=mock`) | No — intercepts API routes with deterministic mocks | `npm run test:e2e` (i.e. `npx playwright test --project=mock`) |
| Offline HTML field-guide Playwright suite (`guide-tests/`) | No — opens `guide/*.html` over `file://` with no server or network | `npx playwright test --config playwright.guides.config.js` |
| Live Playwright suite (`--project=live`) | **Yes** — requires running stack and `OCR_UI_BASE_URL` | `npm run test:live` |

### Opt-in live verifiers

These scripts require a running stack (and GPU inference or SeaweedFS where
applicable) and are **never** part of an offline pass:

- `python scripts/verify_api_live.py`
- `python scripts/verify_autotune_live.py`
- `python scripts/verify_mcp_live.py`
- `python scripts/verify_storage_live.py`
- `python scripts/verify_rag_smoke.py` / `python scripts/verify_rag_real_corpus.py`

### Full verification pass

The check to run before opening or completing a change. `.github/workflows/ci.yml`
runs this on every push to `main` and every pull request across seven required
jobs (`lint`, `unit`, `frontend-unit`, `guides`, `launchers`, `compose`, `wheel`):

```powershell
python -m compileall -q ocr_server.py autotune_entrypoint.py ocrserver vllm calibration scripts ocr-mcp-server/ocr_mcp
python -m ruff check . --select E9,F63,F7,F82
python -m ruff check scripts/verify_dependencies.py scripts/security_constraints.py scripts/release_evidence.py scripts/render_dependency_audit_issue.py scripts/verify_dependency_inventory.py tests/test_ci_workflow.py tests/test_verify_dependencies.py tests/test_security_constraints.py tests/test_release_evidence.py tests/test_render_dependency_audit_issue.py tests/test_dependency_inventory.py guide/tests/test_guide_links.py guide/tests/test_guide_contract.py
python -m ruff format --check scripts/verify_dependencies.py scripts/security_constraints.py scripts/release_evidence.py scripts/render_dependency_audit_issue.py scripts/verify_dependency_inventory.py tests/test_ci_workflow.py tests/test_verify_dependencies.py tests/test_security_constraints.py tests/test_release_evidence.py tests/test_render_dependency_audit_issue.py tests/test_dependency_inventory.py guide/tests/test_guide_links.py guide/tests/test_guide_contract.py
python -m scripts.verify_dependency_inventory
python scripts/security_constraints.py
actionlint
shellcheck stack.sh
Invoke-ScriptAnalyzer -Path stack.ps1 -Severity Warning,Error
python -m pytest tests -q --ignore=tests/test_stack_core.py --ignore=tests/test_stack_launchers.py --ignore=tests/test_stack_verifiers.py
python -m pytest tests/test_stack_core.py tests/test_stack_launchers.py tests/test_stack_verifiers.py -q
python -m pytest guide/tests -q
Push-Location frontend
npm test
npx playwright test --project=mock
npx playwright test --config playwright.guides.config.js
Pop-Location
docker build --check --target ocr-api .
docker build --check --target ocr-mcp .
docker build --check -f Dockerfile.vllm .
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example --profile storage config --quiet
python scripts/verify_compose_security.py --env-file .env.example
pwsh stack.ps1 verify -NoMcp -EnvFile .env.example
python scripts/verify_mcp_wheel.py
```

`Invoke-ScriptAnalyzer` signals findings by **printing** them rather than
setting a non-zero exit code; assert that its output list is empty.

## Architecture notes

### OCR API and RAG chunker (`ocr_server.py`, `ocrserver/`)

- **App factory:** `ocr_server.create_app(settings, ...)` builds an isolated
  FastAPI instance per `Settings` object (used across `tests/` with fake
  backends, fake stores, and fake token counters). Module-level `ocr_server.app`
  loads the hardware profile and settings for Uvicorn.
- **Settings resolution (`ocrserver/config.py`):** frozen `Settings` dataclass
  resolved with strict precedence: explicit environment variable → validated
  autotune profile → built-in default. Invalid numeric ranges, malformed URLs,
  or partial S3 credential pairs fail fast at startup.
- **Middleware stack (`ocr_server.py`):**
  `StorageDownloadMiddleware` (outermost, retains download memory admission
  until the HTTP response finishes streaming) → optional `CORSMiddleware` (when
  `CORS_ORIGINS` is set, answering `OPTIONS` preflights before auth/rate-limit)
  → `@app.middleware("http")` handling shutdown guard (`503 shutting_down` on
  `POST`), per-IP token-bucket rate limiting (`429 rate_limited`, with `GET
  /stats`, `GET /metrics` and `GET /parse/progress/{id}` exempt so console
  polling cannot starve callers),
  bearer authentication (`401 unauthorized` on non-public paths), `Cache-Control:
  no-cache` on static console assets, and Prometheus request duration/counter
  recording.
- **Public vs authenticated routes:** only `/`, `/health`, `/assets/*`, and
  (when `EXPOSE_SWAGGER=true`) `/docs`, `/docs/oauth2-redirect`, `/redoc`,
  `/openapi.json` are unauthenticated when `OCR_SERVER_API_TOKENS` is set.
  `/stats`, `/metrics` and `/parse/progress/{id}` require the bearer token
  whenever API authentication is enabled.
- **Document vs element pipelines:** `/parse` and `/parse/chunks` run the
  PP-DocLayoutV3 + PaddleOCR-VL document pipeline (`pipeline_backend.py`) with
  bounded queue admission (`MAX_QUEUE_SIZE`) and PDF rasterization limits
  (`MAX_PAGES`, `PDF_MAX_PAGE_PIXELS`, `PARSE_DPI`).
  `/ocr` runs single-image element tasks (`ocr`, `table`, `formula`, `chart`,
  `spotting`, `seal` in `element_ocr.py`) bounded by `ELEMENT_MAX_SLOTS` and
  `ELEMENT_MEMORY_BYTES`.
- **Office conversion (`ocrserver/office_utils.py`):** converts `.docx`,
  `.pptx`, `.xlsx`, `.doc`, `.ppt`, `.xls`, `.odt`, `.odp`, `.ods` to PDF via
  headless LibreOffice (`soffice`) in an isolated temporary profile directory
  with timeout and output size guards before handing the PDF to `/parse` or
  returning it from `/office/pdf`.
- **RAG chunking (`ocrserver/chunking/`, `rag_*.py`, `json_rag.py`):**
  distinguishes **OCR blocks** (layout regions with PP-DocLayoutV3 tags and
  bounding boxes), **document sections** (heading-delimited spans across
  blocks/pages), and **RAG chunks** (retrieval units with exact Paddle
  tokenizer counts from `token_count.py` and full provenance). Protected blocks
  (tables, code fences) stay intact unless split policy allows otherwise;
  marginal text (headers, footers, printed page numbers/labels) is kept distinct
  from body text and footnotes. Manual refinement (`rag_refinement.py`,
  `rag_split.py`, `rag_structure.py`) shares its planner semantics with the
  browser UI (enforced by `tests/test_chunk_contract.py`).

### Autotuning and vLLM runtime (`autotune_entrypoint.py`, `ocrserver/autotune/`, `vllm/`)

- `autotune_entrypoint.py` fingerprints GPU/CPU/software state, loads or runs
  calibration against `calibration/`, writes the profile atomically, and starts
  Uvicorn. A hardware or model fingerprint change invalidates the cached
  profile.
- `vllm/stage1_entrypoint.py` selects GPU tier parameters (`ocrserver/autotune/tiers.py`)
  and launches the OpenAI-compatible vLLM server for the pinned revision
  `DEFAULT_MODEL_REVISION` (`c5630abae1d940eafe0697512a0325494b02ab42`).
- `ocrserver/vllm_client.py` wraps upstream calls with a circuit breaker so
  repeated vLLM connection failures fail fast (`503 backend_unavailable`)
  while `_poll_vllm` updates the `VLLM_UP` Prometheus gauge.

### MCP server (`ocr-mcp-server/ocr_mcp/`)

- A thin, stateless translation layer: every tool call becomes one or more HTTP
  requests to `ocr-api` via `client.py`. It never imports `ocrserver` and never
  talks to vLLM or SeaweedFS directly.
- Exposes **14 tools** (`tools/*.py`), **11 resources** (`resources/live.py` for
  `ocr://metrics` and `resources/docs.py` for the 10 [copied documents](#copied-documents)),
  and **3 prompts** (`prompts.py`). `tool_map.py` maps every API route to its
  owning tool or explicit exclusion, enforced by `tests/test_mcp_coverage.py`.
- **Input locality (`inputs*.py`):** accepts either base64 content or server-visible
  file paths under explicit `MCP_INPUT_ROOTS` (semicolon-separated, literal
  whitespace preserved, symlinks resolved before containment check).
- **Destructive confirmation gates (`guardrails.py`):** storage deletion tools
  validate the `confirm` parameter locally (exact case- and whitespace-sensitive
  match on job ID or relative artifact path) and make **zero HTTP calls** on
  mismatch.
- **Health preflight & retry boundaries:** inference tools check `/health`
  first under `MCP_DIAGNOSTIC_TIMEOUT` (with bounded GET retries); inference
  `POST` requests **never** retry automatically, and an HTTP timeout never
  claims that upstream work stopped or that an archive was saved.

### Optional S3 storage (`storage/`, `ocrserver/storage*.py`)

- Enabled via the `storage` Compose profile and `scripts/configure_storage.py`,
  which generates three distinct credential pairs: S3 admin/init, API writer/reader
  (`OCR_S3_ACCESS_KEY`), and console/MCP browser (`OCR_S3_BROWSER_ACCESS_KEY`).
- `storage-gateway` (nginx) fronts SeaweedFS on internal network
  `ocr-storage-internal`; `storage-init` provisions buckets/policies before
  `ocr-api` starts.

### Cross-cutting

- All Docker/Compose configuration, named volumes and environment variables are
  unified at the repo root. `.env` (gitignored) holds local secrets;
  `.env.example` (versioned) is the exhaustive template tested against
  `guide/environment.html` and `scripts/verify_token_config.py`.
- Application containers run as non-root `1000:1000` (`appuser`) with
  `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, and bounded
  memory/PIDs.
- The static console (`frontend/dist/`) is **baked into `ocr-api`**. Rebuild
  `ocr-api` after changing files under `frontend/dist/`, and run
  `python scripts/verify_frontend_assets.py` to verify asset integrity.

## Code style and conventions

Python targets **3.11**. Follow PEP 8 with 4-space indentation, double quotes,
and 88-character lines (`ruff format` / `ruff check`).

| Rule | Value |
|---|---|
| Python version | 3.11 (`list[str]`, `X \| Y` union syntax, `from __future__ import annotations` where used) |
| Formatter & linter | `ruff format` and `ruff check` |
| Naming | `snake_case` modules/functions/variables/test files; `PascalCase` classes and Pydantic models; `UPPER_SNAKE_CASE` constants and env vars |
| Schemas & settings | Request/response models in `ocrserver/schemas.py` (or `ocrserver/chunking/`); env parsing centralized in `ocrserver/config.py`, `ocr_mcp/config.py`, or `ocrserver/autotune/tiers.py` |
| Tests | Files `tests/test_<area>.py` and `guide/tests/test_<area>.py`, functions `test_<expected_behaviour>()` |

`pyproject.toml` sets `testpaths = ["tests"]`,
`pythonpath = [".", "ocr-mcp-server"]`, and `asyncio_mode = "auto"`, so
`ocrserver.*`, `ocr_mcp.*`, and `scripts.*` import cleanly from the repository
root.

### Testing

Add focused regression tests in `tests/` beside the area you touch. Isolate
network, vLLM, PaddleOCR, LibreOffice, and S3 calls with fakes — every offline
test in `tests/` runs without a GPU, live container, or network connection.
When changing OCR quality, layout normalization, or autotune heuristics, also
validate against `calibration/ground_truth.json`.

### Documentation

- Subsystem READMEs follow the 13-section skeleton described in
  [Where to look first](#where-to-look-first).
- **`guide/environment.html` is checked against `.env.example`** by
  `guide/tests/test_guide_contract.py` (`test_environment_entries_match_every_template_assignment_exactly`),
  and all local links/heading anchors across `guide/` and linked Markdown files
  are checked by `guide/tests/test_guide_links.py`.
- **`scripts/README.md` and launcher help output** must stay aligned with
  `stack.ps1`, `stack.sh`, and `scripts/stack_core.py` (enforced by
  `tests/test_stack_launchers.py` and `tests/test_stack_core.py`).
- Where this file and a subsystem `README.md` disagree, the subsystem
  `README.md` wins — fix the discrepancy in the same change.

## Change-risk matrix

Use the smallest verification set that proves the change, then expand it when
the change crosses a boundary:

| Change | Minimum verification |
|---|---|
| Python logic in `ocr_server.py` or `ocrserver/` | Focused `pytest tests/test_<area>.py -q`, `compileall`, and `ruff check` on touched files. |
| REST route, schema, auth, rate limit, or error code | API tests plus `tests/test_mcp_coverage.py`, `tests/test_mcp_tools.py`, and `tests/test_mcp_schema.py` if MCP surface is affected. |
| RAG chunking or manual refinement (`ocrserver/chunking/`, `rag_*.py`) | `pytest tests/test_chunk_*.py tests/test_api_json_rag.py -q` and `npm test` in `frontend/` (for Python/JS planner parity). |
| MCP tool, input schema, guardrail, resource, or prompt | `pytest tests/test_mcp_*.py tests/test_smart_document_reader_skill.py -q`, `python scripts/verify_mcp_wheel.py`, and update/re-copy `ocr-skill/` docs if affected. |
| Any of the ten [copied documents](#copied-documents) | Re-copy into `ocr-mcp-server/ocr_mcp/resources/docs/` in the same commit, then `pytest tests/test_mcp_server.py guide/tests -q`. |
| `.env.example` or `guide/*.html` | Update `guide/environment.html` (and its MCP copy) if assignments changed; run `pytest guide/tests tests/test_mcp_server.py -q` and `npx playwright test --config playwright.guides.config.js` in `frontend/`. |
| Launcher (`stack.ps1`, `stack.sh`, `scripts/stack_core.py`), Compose, or security verifiers | `pytest tests/test_stack_core.py tests/test_stack_launchers.py tests/test_stack_verifiers.py -q`, `shellcheck stack.sh`, `Invoke-ScriptAnalyzer -Path stack.ps1 -Severity Warning,Error`, and both Compose config checks. |
| `Dockerfile`, `Dockerfile.vllm`, `requirements/*`, or `.github/workflows/*` | `pytest tests/test_dependency_inventory.py tests/test_security_constraints.py tests/test_verify_dependencies.py tests/test_ci_workflow.py tests/test_release_evidence.py -q`, `python -m scripts.verify_dependency_inventory`, `python scripts/security_constraints.py`, and `docker build --check`. |
| Frontend console (`frontend/dist/` or `frontend/tests/`) | `python scripts/verify_frontend_assets.py`, `npm test`, and `npm run test:e2e` inside `frontend/`. |

## Commit and pull request conventions

Use concise, imperative commit subjects starting with an uppercase verb (matching
`git log`, e.g. `Stabilize chunk runtime timeout test`, `Publish offline HTML
field guides through MCP resources`, `Harden MCP tool contracts and refresh OCR
skills`), and keep unrelated changes in separate commits.

Pull requests should explain behaviour and configuration changes, list the
validation commands actually run, link relevant issues, and include screenshots
for frontend/guide changes or sample OCR output for parsing/chunking changes.

**Commit or push only when asked.**

## Security and configuration

These are invariants, not preferences. Breaking one is a bug even when unit
tests pass.

- **Published ports bind to loopback by default.** Compose binds published host
  ports (`OCR_SERVER_PORT`, `OCR_VLLM_DOCS_PORT`, `MCP_PORT`, `OCR_S3_PORT`,
  `OCR_STORAGE_CONSOLE_PORT`) to `127.0.0.1` (or validated private bind host for
  MCP where configured). `scripts/verify_compose_security.py` enforces container
  hardening and port bindings across both base and `storage` profiles.
- **Keep API, MCP and storage credentials distinct and consistent.**
  `MCP_API_TOKENS` and `OCR_SERVER_API_TOKENS` (both inbound `token:label`
  lists) must not share tokens; `UPSTREAM_CLIENT_TOKEN` (plain service token,
  auto-accepted by `ocr-api` as `@mcp`) is required when API auth is enabled and
  must appear in neither list; S3 writer and browser keys
  must be distinct. `scripts/verify_token_config.py` enforces these rules before
  startup.
- **Introduce configuration in `.env.example`, never in a local `.env`.** Never
  commit `.env`, real tokens, model caches, or confidential documents.
- **Image dependency inventories (`requirements/locks/*.txt`) are audited
  snapshots, not Docker `-c` build constraints.** `tests/test_security_constraints.py`
  explicitly forbids referencing `requirements/locks/` or
  `requirements/security-constraints.txt` inside `Dockerfile` or
  `Dockerfile.vllm`. When `requirements/api.txt`,
  `ocr-mcp-server/pyproject.toml`, or `Dockerfile.vllm` changes, rebuild the
  image, recapture the inventory, and update `requirements/inventory.json`
  together (see [`requirements/README.md`](requirements/README.md)).
- **Never echo a secret in output, logs, or error messages.** `stack_core.py`
  redacts credentials in `config` output unless `--show-secrets` /
  `-ShowSecrets` is explicitly passed.

## Traps

Things that have already cost time here.

| Trap | Reality |
|---|---|
| Editing `ocr-skill/` or `guide/*.html` and stopping there | Ten documents have byte-for-byte copies inside `ocr-mcp-server/ocr_mcp/resources/docs/` enforced by `tests/test_mcp_server.py`. Re-copy in the same commit — see [Copied documents](#copied-documents). |
| Editing `.env.example` without updating `guide/environment.html` | `guide/tests/test_guide_contract.py` asserts exact 1:1 parity between every active/commented assignment in `.env.example` and `data-env-*` entries in `guide/environment.html` (which in turn must be re-copied to `ocr_mcp/resources/docs/guide/environment.html`). |
| Passing `-c requirements/locks/*.txt` in a `Dockerfile` | In this repo, `requirements/locks/` are post-build image audit inventories checked by SHA-256 in `requirements/inventory.json`. `tests/test_security_constraints.py` fails if a Dockerfile references them. |
| Editing a launcher flag in one shell only | `stack.ps1` and `stack.sh` are held at parity by `tests/test_stack_launchers.py`, and all planning decisions live in `scripts/stack_core.py`. |
| "Simplifying" `down`, `logs`, `status`, `version`, `help` to call Python | Recovery commands deliberately bypass Python so stopping or inspecting a stack works with `.env` absent/malformed and no Python on `PATH`. |
| Running `pytest` without Node 24 / `frontend/node_modules` | `tests/test_chunk_contract.py` runs Node to verify Python/browser chunk refinement parity. Run `npm ci --ignore-scripts` in `frontend/` once first. |
| Testing console edits without rebuilding `ocr-api` | `frontend/dist/` is copied into the image at build time, and `scripts/verify_frontend_assets.py` checks asset hashes. |
| Multi-source `COPY a/ b/ ./` in `Dockerfile` | Docker copies the *contents* of each source directory into `./`, flattening Python packages. Keep one `COPY` instruction per directory. |

## Related files

| File | Role |
|---|---|
| `README.md` | The human operator's entry point and API/benchmark reference. |
| `LICENSE` | Repository license (GNU AGPL v3). |
| `.env.example` | The exhaustive, versioned configuration template. |
| `pyproject.toml` | Unified Ruff lint/format and pytest configuration. |
| `scripts/README.md` | The cross-platform launcher and verifier reference. |
| `requirements/README.md` | Dependency manifests, image audit inventories, and OSV triage reference. |
| `ocr-mcp-server/README.md` | MCP server tools, resources, prompts, and security boundaries. |
| `ocr-skill/README.md` | Client-side agent skills installation and maintenance guide. |
| `guide/README.md` | Offline HTML field guides reference and verification rules. |
| `.github/AGENTS.md` | CI workflows, dependency audit automation, and release evidence rules. |
| `.gitattributes` | Line-ending contract (LF forced across source, scripts, and configs). |
