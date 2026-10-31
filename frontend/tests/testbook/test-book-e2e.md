# Comprehensive E2E Test Book — Smart Document Manager (SDM) web console

Console served by `ocr-api` at `http://127.0.0.1:8020`, exercised through
browser actions, agent-driven MCP/skill workflows, direct API checks, and
explicitly simulated responses.
Review date: 2026-10-26 · Revision 2

> **Executable form:** [`../`](../) — mocked Playwright specs run against
> `static-server.mjs` on `http://127.0.0.1:8129`; `*-live.spec.mjs` requires a
> real stack via `OCR_UI_BASE_URL` (`npm run test:e2e` / `npm run test:live`).
> Node unit coverage runs with `npm test`; guide coverage uses
> `npx playwright test --config playwright.guides.config.js`.
>
> **Coverage contract:** [`coverage-contract.json`](coverage-contract.json)
> disposes every case ID. `ratchet.maxUnassigned` may only be lowered;
> exclusions need a written reason and fail the gate once a case gains
> executable evidence.
>
> **Executable coverage remains partial.** UI-mock specs prove frontend
> behaviour without proving the backend; `*-live` specs prove a small real
> slice against a running stack. Historical results do not apply to new cases.

This book inventories **all five console tabs** (PARSE, OCR, RAG, STORAGE,
MONITOR), the connection bar, authentication surfaces, math/table rendering,
the six offline field guides, and the MCP/agent acceptance layer.

## Reading and execution order

Read [`../../README.md`](../../README.md) for launch commands, then follow the
shared procedures in [§0](#status) and the case tables for the feature under
test.

| Subject | Acceptance section |
|---|---|
| Setup, layers, procedures, fixtures | [§0 Isolation and sandbox](#sandbox) |
| PARSE tab | [§1 Series SDM-PAR](#parse) |
| OCR tab | [§2 Series SDM-OCR](#ocr) |
| RAG tab | [§3 Series SDM-RAG](#rag) |
| STORAGE tab | [§4 Series SDM-STO](#storage) |
| MONITOR tab | [§5 Series SDM-MON](#monitor) |
| Connection bar, auth, rendering, a11y | [§6 Series SDM-XUI](#crosscutting) |
| Agent workflows (MCP + skill) | [§7 Series SDM-AGT](#agent) |
| Offline field guides | [§8 Series SDM-GUI](#guides) |
| Backend contract evidence | [§9 Series SDM-API](#api) |
| Gap-to-case map and run record | [§10 Closure](#closure) |

---

<a id="sandbox"></a>

## 0. Isolation and sandbox

- **UI** is the default interaction layer. **[API]** marks a direct contract
  check; **[MOCK]** identifies intercepted/simulated API responses, which prove
  frontend behaviour without proving the corresponding backend operation.
- The console's only mutable server state is the **S3 archive** (optional
  `storage` profile). Case-owned archives are created inside the run through
  the real `/ocr` or `/parse` flow and deleted — through the UI confirmation
  path where that is the behaviour under test, or through `DELETE
  /storage/archive` with `confirm: <job_id>` as cleanup — before the run ends.
- **Protected fixtures (NEVER mutated):** everything under `calibration/`,
  `tests/fixtures/`, `frontend/tests/fixtures/`, the document corpus in
  `frontend/tests/testbook/documents/`, and the stored `rag-split.json`
  example. Read-only oracles; never upload, edit or delete them as part of a
  case's "finish" step.
- **Secrets:** live specs set `trace: "off"`; tokens enter the browser only via
  `sessionStorage` (`ocr_api_key`) and never appear in screenshots, traces or
  the written `browser.json` run evidence.

### 0.1 Reproducible fixtures and independent oracles

| Fixture | Role | Oracle |
|---|---|---|
| `calibration/plain_text.png` | Canonical single-page OCR input | `ground_truth.json` text contains "quick brown fox" |
| `calibration/simple_table.png`, `formula.png`, `dense_page.png` | Element OCR / OTSL / LaTeX inputs | Ground truth in `calibration/ground_truth.json` |
| `calibration/multipage.pdf`, `form943.pdf` | Multi-page PDF parse inputs | Per-page ground truth entries |
| `tests/fixtures/ocr-rag-chunks.input.example.json` | `/chunk` request oracle | Expected output `ocr-rag-chunks.example.json`, schema `ocr-rag-chunks.schema.json` |
| `frontend/tests/fixtures/rag-split.json` | Stored JSON-RAG sample for split/refine workflows | Shared shape with `rag-workflows.mjs` |
| `frontend/tests/fixtures.mjs` + `rag-workflows.mjs` | Mock API responses for `static-server.mjs` runs | Declared bodies; never hit a live server |
| `frontend/tests/testbook/documents/` | Live document corpus (table below) | `manifest.json` `expect` strings; SHA-256 per file |

#### 0.1.1 Document corpus

Nine files are verbatim copies of `tests/test_files` from
[PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) at the
commit pinned in `manifest.json` (Apache-2.0, `LICENSE-PaddleOCR.txt`). Four
cover what no licensed upstream file does and are written by
`python frontend/tests/testbook/documents/generate.py`, which also refreshes the
manifest hashes. `testbook-coverage.test.mjs` fails on an unlisted file, a hash
drift, or a document no row below names. An empty oracle means the case asserts
completion only.

| Document | Origin | Case | Oracle (raw output contains) |
|---|---|---|---|
| `documents/textline.png` | PaddleOCR | SDM-OCR-011 `ocr` | `绿洲仕格维花园公寓` |
| `documents/medal_table.png` | PaddleOCR | SDM-OCR-011 `table` | `CHN`, `112` |
| `documents/table.jpg` | PaddleOCR | SDM-OCR-011 `table` | `CRuncover`, `744.78` |
| `documents/formula.png` | PaddleOCR | SDM-OCR-011 `formula` | `\zeta` |
| `documents/chart.png` | generated | SDM-OCR-011 `chart` | `Q4`, `210` |
| `documents/book.jpg` | PaddleOCR | SDM-OCR-011 `spotting` | `Veronica` |
| `documents/seal.png` | PaddleOCR | SDM-OCR-011 `seal` | `发票专用章` |
| `documents/doc_with_formula.png` | PaddleOCR | SDM-PAR-026 | `Soltan` |
| `documents/sample.docx` | generated | SDM-PAR-027 | `office sample`, `Oslo` |
| `documents/multiframe.tif` | generated | SDM-PAR-028 | `amber`, `cobalt`, `violet` |
| `documents/book_rot180.jpg` | PaddleOCR | SDM-PAR-029 parse | — |
| `documents/textline_rot180.jpg` | PaddleOCR | SDM-PAR-029 `ocr` | — |
| `documents/long.pdf` | generated | SDM-PAR-030 | — (30 pages, two blocks at any DPI) |

The demo images of the PaddleOCR-VL model cards (spotting, curving, skew,
lighting) are hosted without a licence and are deliberately not copied.

### 0.2 Layer table and required evidence

| Layer | What is exercised | Required evidence |
|---|---|---|
| UI-live | Browser actions against a running stack (`*-live.spec.mjs`) | Response status, rendered state, independent API reads; `errors === []` |
| UI-mock | Real console, intercepted API responses (`*.spec.mjs`, mock project) | Observed requests, DOM/downloads, zero live mutations |
| API-live | Python/HTTP contract against a running stack (`scripts/verify_*_live.py`) | Exact status/body; before/after archive or metric snapshots |
| MCP-live | Agent calls the real `smart-document-mcp` tools/resources (`scripts/verify_mcp_live.py`) | Sanitized tool arguments/results and postconditions |
| Skill-agent | Agent loads `ocr-skill/` and follows its playbook through MCP | Skill identity, task prompt, workflow trace, evaluated outcome |
| Backend | `pytest tests/` offline suite | Exact serialized values, fakes for GPU/S3/network |
| Model | `node --test tests/*.test.mjs` pure validators | Exact parsed/validated values; no UI or backend claim |
| Static | Case inventory, link/schema contract (`guide/tests`, `testbook-coverage`) | Explicit mapping; no behavioural claim |

**Current contract** is the default expectation. A screenshot or visibility
check alone never proves a write; a console-mock PASS never proves backend
behaviour; a missed `BUSY`/`Cutting…` state is not a PASS via the terminal
state. No row may be implemented as `expect(true)`, a source-text search, or
an unconditional skip.

<a id="status"></a>

### 0.3 Common procedures

1. **Prepare:** live cases assert `/health` → `"healthy"` first; read-only
   fixtures are hashed, not modified. Storage cases create exactly the owned
   archive they need via the real API before touching the UI.
2. **Exercise UI:** navigate by tab (`#tab-*`), fill controls by accessible
   label or stable `#id`, arm confirmations where applicable, observe the
   request before clicking the action.
3. **Compare reads:** assert rendered text/downloads against the fixture
   oracle — never just "something appeared". Downloads compare byte-for-byte
   with an independent API read (`storage-live.spec.mjs` pattern).
4. **Compare writes:** after a UI mutation, independently re-read the target
   (archive GET, metrics counter); negative write cases require no change.
5. **Observe asynchronous work:** deadlines — mock UI 10 s after releasing the
   response; live parse/ocr `requestTimeoutSeconds + 65 s`; live storage
   240 s; live RAG repair per saved-plan polling until `saved`/`failed`.
6. **Finish:** disarm confirmations, delete only case-owned archives
   (`confirm: job_id`), record `browser.json` evidence where the spec writes
   it, and assert the collected `errors` array is empty.

### 0.4 Known limitations / out of UI scope

- **GPU autotune timing** (`full` run, 10–20 min) is a startup property, not a
  per-run UI case: MONITOR surfaces its *result* (profile card), the run itself
  is verified by `scripts/verify_autotune_live.py`.
- **vLLM internal flags** (tier T0–T4) have no UI surface; covered by
  `tests/test_autotune*.py`, `vllm/README.md` and §9.
- **Office rendering fidelity** of LibreOffice conversion is a backend
  contract (`tests/test_office_*`); the console cases cover upload acceptance,
  preview and result display.
- **Orientation correction and unwarping** keep the pipeline's disabled
  defaults (root `README.md`), so the rotated corpus inputs prove completion
  without an error, never text accuracy.
- The console has **no collection/point model** — there is no equivalent of
  the 3S `e2e_` collection sandbox; the mutable boundary is the S3 archive
  only.
- **`OCR_UI_BASE_URL` gating:** `*-live.spec.mjs` files throw without it; a
  path argument alone cannot bypass `testIgnore` in `playwright.config.mjs`.

---

<a id="parse"></a>

## 1. Series SDM-PAR — PARSE tab

Layer key: M = UI-mock · L = UI-live · B = Backend · S = Static.

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-PAR-001 | M | `console.spec.mjs` | Upload validation: file required, type/size guards, `dpi` omitted for images |
| SDM-PAR-002 | M | `console.spec.mjs` | Server config hydration: `maxPages`, `maxFileBytes`, nondefault limits populate controls and help |
| SDM-PAR-003 | M | `console.spec.mjs` | Office upload acceptance for `/parse` (docx/pptx/xlsx/doc/ppt/xls/odt/odp/ods) |
| SDM-PAR-004 | M | `console.spec.mjs` | Result viewer: block overlay, page tabs, hidden-tab completion refit, figure resolution |
| SDM-PAR-005 | M | `console.spec.mjs` | OTSL rebuild: complete tables rendered, unparseable markers stay literal in raw |
| SDM-PAR-006 | M | `console.spec.mjs` | Markdown table output renders as cells; OTSL cells escaped, no markup execution |
| SDM-PAR-007 | M | `console.spec.mjs` | Truncation and incomplete-coordinate warnings shown without retrying |
| SDM-PAR-008 | M | `console.spec.mjs` | Malformed response → error envelope; later successful submit recovers |
| SDM-PAR-009 | M | `console.spec.mjs` | Submit button restored after network/abort/malformed failure |
| SDM-PAR-010 | M | `console.spec.mjs` | Downloads: `markdown`, `json_ocr`, `json_rag` named without extension; failed `json_rag` export downloads nothing |
| SDM-PAR-011 | M | `audit-console.spec.mjs` | Stale marking: file/option change during or after a run marks previous results; replacement file detected even with same name |
| SDM-PAR-012 | M | `audit-console.spec.mjs` | Running parse cancellable; focus lands on a control after cancel |
| SDM-PAR-013 | M | `audit-console.spec.mjs` | Results stay usable while backend degraded; error codes read as sentences and survive option changes |
| SDM-PAR-014 | M | `audit-console.spec.mjs` | Dropped file becomes the upload instead of navigating away |
| SDM-PAR-015 | M | `audit-console.spec.mjs` | Multi-frame images show results without an incorrect preview (truncated flag) |
| SDM-PAR-016 | M | `compliance.spec.mjs` | Hash/URL state and keyboard navigation preserved across tabs |
| SDM-PAR-017 | L | `live.spec.mjs` | Real `/parse` POST on `plain_text.png`: `waitForResponse` 200 + "quick brown fox" rendered |
| SDM-PAR-018 | L | `model-alignment-live.spec.mjs` | Real OTSL renders as table; LaTeX preview; downloads preserve exact API bytes |
| SDM-PAR-019 | L | `corpus-live.spec.mjs` | Real multi-page `calibration/form943.pdf` at default DPI: 3 pages, no page error, "Form 943" in the markdown, viewer reads `Page 1 / 3` |
| SDM-PAR-020 | M | `info-style-console.spec.mjs` | Validation/deletion error messages share uppercase typography and semantic colors |
| SDM-PAR-021 | M | `api.spec.mjs` | Missing/invalid uploads return envelope without a POST reaching the server |
| SDM-PAR-022 | M | `console.spec.mjs` | Live progress badges: block `3/3 (4 pages)`, status announces completed blocks only, upload carries a canonical-UUID `progress_id` |
| SDM-PAR-023 | M | `console.spec.mjs` | Progress poll resilience: 404 → dashes + elapsed; one failed poll keeps badges; three failures show dashes while elapsed counts; polls never overlap; a late poll cannot replace results |
| SDM-PAR-024 | M | `console.spec.mjs` | Storage tile from `X-OCR-Storage-Status`/`X-OCR-Job-Id`: stored (± job id), failed (down), disabled (off), missing header (—) |
| SDM-PAR-025 | M | `console.spec.mjs` | Completion respects a cleared file and a degraded service state |
| SDM-PAR-026 | L | `corpus-live.spec.mjs` | Real two-column paper page: >10 layout blocks returned and overlaid, oracle text in the markdown, KaTeX display formulas rendered |
| SDM-PAR-027 | L | `corpus-live.spec.mjs` | Real Office document: `/office/pdf` preview 200 drawn on a canvas, LibreOffice-converted parse contains the oracle text |
| SDM-PAR-028 | L | `corpus-live.spec.mjs` | Real multi-frame TIFF: `/parse` returns one page per frame with every frame word; `/ocr` answers 422 `multiframe_image_not_supported`, the message is shown and submit is restored |
| SDM-PAR-029 | L | `corpus-live.spec.mjs` | Real 180°-rotated page (parse) and text line (ocr) complete without an error (§0.4: accuracy not asserted) |
| SDM-PAR-030 | L | `corpus-live.spec.mjs` | Real 30-page PDF: status announces `block 1 of N done`, Cancel shows "Parse cancelled", independent `GET /parse/progress/{id}` reads `cancelled` with N ≥ 2 and 0 < `pages_done` < `pages_total` |

<a id="ocr"></a>

## 2. Series SDM-OCR — OCR tab

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-OCR-001 | M | `console.spec.mjs` | Rich OCR output renders through the sanitizer; Markdown table output as cells |
| SDM-OCR-002 | M | `console.spec.mjs` | Result mirrors Parse: badges, image viewer, spotting regions |
| SDM-OCR-003 | M | `console.spec.mjs` | Discarded LOC geometry keeps following region label and scale |
| SDM-OCR-004 | M | `audit-console.spec.mjs` | Changed file/task/parameters mark results stale after completion |
| SDM-OCR-005 | M | `audit-console.spec.mjs` | Export failure retains results; success/failure does not wait for bitmap decoding |
| SDM-OCR-006 | M | `math.spec.mjs` | Formula task: KaTeX preview, protected delimiters/matrix/comparisons, wide formulas scroll on mobile |
| SDM-OCR-007 | M | `math.spec.mjs` | Malformed/untrusted TeX cannot execute HTML or erase surrounding content |
| SDM-OCR-008 | M | `math.spec.mjs` | Formula preview downloads its text and raw JSON |
| SDM-OCR-009 | M | `console.spec.mjs` | Malformed response → envelope; submit restored after failure |
| SDM-OCR-010 | L | `live.spec.mjs` | Real `/ocr` POST on `plain_text.png`: 200 + "quick brown fox" rendered |
| SDM-OCR-011 | L | `corpus-live.spec.mjs` | Real six-task matrix on seven corpus documents (§0.1.1): each 200 with its task, oracle strings in the raw result, tables rendered, KaTeX formula preview, spotting regions overlaid |

<a id="rag"></a>

## 3. Series SDM-RAG — RAG tab

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-RAG-001 | M | `rag.spec.mjs` | Listing: JSON-RAG files across bucket pages, parent files, search, OCR archives skipped, `parse-chunks` read |
| SDM-RAG-002 | M | `rag.spec.mjs` | Refined files listed beside source, selectable, deletable after confirmation |
| SDM-RAG-003 | M | `rag.spec.mjs` | Partial/unreadable archives reported; refresh recovers; inclusive UTC date filters, reversed range rejected |
| SDM-RAG-004 | M | `rag.spec.mjs` | Disabled storage vs empty inventory distinguished |
| SDM-RAG-005 | M | `rag.spec.mjs` | Stale listing discarded after tab navigation; legacy parents resolve without inventing filenames |
| SDM-RAG-006 | M | `rag.spec.mjs` | Section 02: selected stored JSON-RAG loads without file picker or OCR request; invalid JSON cannot replace a valid selection |
| SDM-RAG-007 | M | `rag.spec.mjs` | Preflight predicts range/page outcomes, previews payloads, invalidates changed settings, paginates all groups, counts out-of-range groups |
| SDM-RAG-008 | M | `rag.spec.mjs` | Protection controls and forced pages expose conflicting settings in preflight |
| SDM-RAG-009 | M | `rag.spec.mjs` | Section 03 executes reviewed plan; saved file named beside parent; execution retries reuse plan execution id |
| SDM-RAG-010 | M | `rag.spec.mjs` | Split/glue/undo reachable with reasons; below-minimum split note readable; in-range rows hide scissors |
| SDM-RAG-011 | M | `rag.spec.mjs` | Setting change clears table + warns + removes undo; consumed gesture hides undo; successful save invalidates gestures |
| SDM-RAG-012 | M | `rag.spec.mjs` | Tokenizer 503 hides scissors, preserves glue; slow split shows Cutting, row unchanged on timeout; only target row locked |
| SDM-RAG-013 | M | `rag.spec.mjs` | Save identity: modified requests get new id after save; obsolete split cannot invalidate new preflight; late saved-file preview cannot replace new plan preview |
| SDM-RAG-014 | M | `rag.spec.mjs` | Tab silent until file selected; parameter reference explains desktop saved-file refinement controls; source without chunks cannot be refined |
| SDM-RAG-015 | M | `rag.spec.mjs` | Manual group changes refresh preflight counts and token distribution; table busy on select, re-select is a no-op |
| SDM-RAG-016 | M | `rag.spec.mjs` | Local download failure at url/click stage reported and cleaned up |
| SDM-RAG-017 | L | `rag-repair-live.spec.mjs` | Real split → View → undo → recut → save → reload roundtrip on a stored archive |
| SDM-RAG-018 | L — excluded | see `coverage-contract.json` | Real `/chunk` from a saved `/parse` JSON at max document size |
| SDM-RAG-019 | B | `tests/test_chunk_*.py` | Deterministic packing, strict/protection semantics, RFC 8785 canonical hashes — backend oracle |
| SDM-RAG-020 | M | `rag.spec.mjs` | Selected JSON-RAG with unprocessed pages shows the partial tile |
| SDM-RAG-021 | M | `rag.spec.mjs` | A refined file activates only Selected JSON RAG and the original restores refinement; a saved refinement is listed and Refresh keeps the selection |
| SDM-RAG-022 | M | `rag.spec.mjs` | Execute waits for a running cut and saves the completed partition |

<a id="storage"></a>

## 4. Series SDM-STO — STORAGE tab

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-STO-001 | M | `storage.spec.mjs` | Browse files, safe preview, download, API authentication forwarded |
| SDM-STO-002 | M | `storage.spec.mjs` | Current-layout archives labelled with filename + short code |
| SDM-STO-003 | M | `storage.spec.mjs` | Filters, per-page search, explicit deletion confirmation preserved |
| SDM-STO-004 | M | `storage.spec.mjs` | Bounded-scan empty pages stay navigable; corrupt archives reported without hiding readable ones |
| SDM-STO-005 | M | `storage.spec.mjs` | Incomplete deletion reported with retry; OCR unavailable → storage outage state; refresh error survives successful deletion |
| SDM-STO-006 | M | `storage.spec.mjs` | Mobile fit + keyboard-accessible controls; icon actions; file pagination keeps selection |
| SDM-STO-007 | M | `storage.spec.mjs` | Page-size/filter changes reset cursor; delayed details preserve interaction |
| SDM-STO-008 | M | `storage.spec.mjs` | Parameter reference parity with Parse/OCR; local download errors reported; download completion names original archive; invalid dates cancel loading |
| SDM-STO-009 | L | `storage-live.spec.mjs` | Real archive: browse → preview → download bytes equal → cancel preserves → confirm deletes → 404 (cleanup via `confirm: job_id`) |
| SDM-STO-010 | L — excluded | see `coverage-contract.json` | Real retention/lifecycle expiry of an archived job |
| SDM-STO-011 | M | `storage.spec.mjs` | "Download source of …" appears only when the row has an `input_key`; it reads that key and saves the uploaded name with the detected extension |
| SDM-STO-012 | L | `storage-live.spec.mjs` | Real archived source downloads byte-for-byte equal to the uploaded `plain_text.png` |

<a id="monitor"></a>

## 5. Series SDM-MON — MONITOR tab

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-MON-001 | M | `console.spec.mjs` | Baselines survive independent counter resets; process restart detected |
| SDM-MON-002 | M | `console.spec.mjs` | Reset requires fresh snapshot; leaving tab cancels stale work |
| SDM-MON-003 | M | `console.spec.mjs` | Unexpected refresh errors shown, polling resumes; zero-delta endpoints kept after Reset |
| SDM-MON-004 | M | `console.spec.mjs` | Chart crosshair and tooltip follow pointer |
| SDM-MON-005 | M | `console.spec.mjs` | Autotune fields/decisions rendered as cards, no raw JSON; storage section shows health + lifetime counters |
| SDM-MON-006 | M | `console.spec.mjs` | Refresh caption reports last good refresh and failures |
| SDM-MON-007 | M | `monitor-console.spec.mjs` | Profile choices separated from effective settings |
| SDM-MON-008 | M | `monitor-console.spec.mjs` | Malformed metrics rejected without zero samples; recovery verified |
| SDM-MON-009 | M | `monitor-console.spec.mjs` | Invalid stats/health rejected even with HTTP 200 |
| SDM-MON-010 | M | `monitor-console.spec.mjs` | Tooltip and keyboard selection cleared after refresh/restart |
| SDM-MON-011 | M | `audit-console.spec.mjs` | Reset keeps keyboard focus across refreshes |
| SDM-MON-012 | M | `console.spec.mjs` | Health retests share probe; invalid nested metadata locks submissions; valid probe recovers; unexpected health rejection closes gate |
| SDM-MON-013 | L | `live.spec.mjs` | Real `#conn-health-status` HEALTHY; `#mon-stats`/`#mon-table` populated after real parse+ocr |

<a id="crosscutting"></a>

## 6. Series SDM-XUI — Connection bar, auth, rendering, accessibility

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-XUI-001 | M | `compliance.spec.mjs` | Canonical UI: keyboard navigation, URL state, responsive layout |
| SDM-XUI-002 | M | `compliance.spec.mjs` | Degraded backend explains accessible locks; diagnostics available |
| SDM-XUI-003 | M | `compliance.spec.mjs` | Legacy keys migrate to session storage; shared popover preserves focus; popover stays inside narrow viewport |
| SDM-XUI-004 | M | `compliance.spec.mjs` | API key indicator follows protected responses, not stored values |
| SDM-XUI-005 | M | `compliance.spec.mjs` | Network failures resolve to the documented API envelope |
| SDM-XUI-006 | M | `api.spec.mjs` | Diagnostic timeout covers body reads, differs from voluntary cancellation |
| SDM-XUI-007 | M | `api.spec.mjs` | Request deadlines follow configuration; successful bodies clear timers |
| SDM-XUI-008 | M | `api.spec.mjs` | Storage disabled by browser policy leaves console and active token usable |
| SDM-XUI-009 | M | `console.spec.mjs` | Desktop parameter references readable at all viewports; long filenames do not widen mobile tabs |
| SDM-XUI-010 | M | `console.spec.mjs` | Rich Markdown rejects executable URLs, event attributes, clobbering, deep templates |
| SDM-XUI-011 | M | `info-style-console.spec.mjs` | Console messages share uppercase typography and semantic colors |
| SDM-XUI-012 | M | `math.spec.mjs` | Attention excerpt renders with local fonts, source preserved; code/escaped dollars/prices stay readable |

<a id="agent"></a>

## 7. Series SDM-AGT — Agent workflows (MCP + skill)

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-AGT-001 | B | `tests/test_mcp_*.py` | All 14 tools, 11 resources, 3 prompts registered; OpenAPI coverage parity; `COPIED_DOCUMENTS` byte parity |
| SDM-AGT-002 | MCP-live — excluded | see `coverage-contract.json` | Agent calls `ocr_check_health` → `ocr_parse_document` → `ocr_create_rag_chunks` on a live stack through `scripts/verify_mcp_live.py` |
| SDM-AGT-003 | Skill-agent — excluded | see `coverage-contract.json` | Agent loads `smart-document-manager` skill and follows the extraction playbook end-to-end |
| SDM-AGT-004 | Skill-agent — excluded | see `coverage-contract.json` | Agent loads `smart-document-reader` and interprets saved `/parse` JSON without re-running OCR |

<a id="guides"></a>

## 8. Series SDM-GUI — Offline field guides

Executed by `npx playwright test --config playwright.guides.config.js`
(`guide-tests/guides.spec.js`, 73 tests) plus `guide/tests` link/contract
suites. Each page opens over `file://`, embeds its own CSS/JS/WOFF2, and makes
zero network requests.

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-GUI-001 | S | `guide-tests/guides.spec.js` | One H1 per page, current-page nav (`aria-current`), skip link, landmarks, footer nav |
| SDM-GUI-002 | S | `guide-tests/guides.spec.js` | Embedded Barlow Condensed + JetBrains Mono; copy buttons; print reveal/restore; hash disclosures |
| SDM-GUI-003 | S | `guide-tests/guides.spec.js` | Responsive at 320/390/768/1440 px; no-JS readability |
| SDM-GUI-004 | S | `guide-tests/guides.spec.js` | CLI command preview (PowerShell/Bash), clipboard fallback |
| SDM-GUI-005 | S | `guide-tests/guides.spec.js` | Environment page: filter + hash reveal, `data-env-*` parity with `.env.example` |
| SDM-GUI-006 | S | `guide-tests/guides.spec.js` | Architecture page: saved-JSON/CPU chunking vs GPU inference distinction |
| SDM-GUI-007 | S | `guide/tests` | Local links/anchors resolve; env parity count exact; no external URLs/scripts/storage |

<a id="api"></a>

## 9. Series SDM-API — Backend contract evidence

| Case | Layer | Executable | Scenario / oracle |
|---|---|---|---|
| SDM-API-001 | B | `tests/test_api_*.py` | Endpoint auth, rate limiting, admission queues, payload guards |
| SDM-API-002 | B | `tests/test_pdf_utils.py`, `test_office_utils.py`, `test_element_ocr.py` | PDF/image/Office validation, pixel budgets, LibreOffice conversion |
| SDM-API-003 | B | `tests/test_chunk_*.py` | RAG chunking engine, runtime workers, schema parity, golden vectors |
| SDM-API-004 | B | `tests/test_storage*.py`, `test_api_storage.py` | S3 bootstrap, IAM separation, archive browse/delete gates |
| SDM-API-005 | B | `tests/test_autotune*.py`, `test_calibration.py` | GPU tiers, fingerprint, CER gates, manifest hashes |
| SDM-API-006 | B | `tests/test_stack_*.py`, `test_ci_workflow.py` | Launcher parity, compose security verifiers, CI contract |
| SDM-API-007 | API-live — excluded | see `coverage-contract.json` | Live stack smoke: `scripts/verify_api_live.py` + `verify_rag_smoke.py` + `verify_storage_live.py` |
| SDM-API-008 | API-live — excluded | see `coverage-contract.json` | Live autotune profile proof: `scripts/verify_autotune_live.py` |
| SDM-API-009 | B | `tests/test_parse_blocks.py`, `test_pdf_blocks.py`, `test_progress.py` | Block planning within pixel/page budgets, partial results with `pages_unprocessed`, cancellation within a block, disconnect skips the archive, progress registry states, ETA and retention |
| SDM-API-010 | B | `tests/test_storage.py`, `test_storage_browser.py`, `test_api_storage.py` | Original input archived as `input.<ext>` and listed as `input_key`; outputs kept with a warning when the input exceeds the archive budget |

<a id="closure"></a>

## 10. Gap-to-case map and run record

### 10.1 Current executable map

| Series | Mocked browser | Live browser | Node unit | Python backend |
|---|---|---|---|---|
| SDM-PAR | console/audit/compliance/api/info-style | live, model-alignment-live, corpus-live | parse-progress | test_api_*, test_pdf/office/element, test_parse_blocks, test_pdf_blocks, test_progress |
| SDM-OCR | console/audit/math | live, corpus-live | — | test_element_ocr, test_ocr_guards |
| SDM-RAG | rag (40 cases) | rag-repair-live | rag-*.test.mjs | test_chunk_*, test_rag_* |
| SDM-STO | storage | storage-live | — | test_storage*, test_api_storage |
| SDM-MON | console/monitor-console/audit | live | monitor-validation | test_api_metrics |
| SDM-XUI | compliance/api/info-style/math/console | — | models, playwright-config | test_api_cors, test_api_docs |
| SDM-AGT | — | — | — | test_mcp_*, test_chunk_mcp, test_smart_document_reader_skill |
| SDM-GUI | — | guide-tests (73) | — | guide/tests link/contract |

### 10.2 Specified gaps (SPECIFIED / NOT RUN by default)

- **SDM-RAG-018, SDM-STO-010, SDM-AGT-002..004, SDM-API-007..008**: excluded
  in `coverage-contract.json` — they need a large real corpus, bucket expiry,
  a real agent, or operator time that the browser run cannot produce. They
  become PASS only with recorded live evidence.
- **RAG live breadth**: `rag-repair-live.spec.mjs` covers the split/save
  roundtrip for the real stored file; preflight/execute on real corpus
  volumes is an excluded operator campaign, not a default case.
- **Console live breadth**: `live.spec.mjs` and `corpus-live.spec.mjs` prove
  real parse, ocr (all six tasks), Office, multi-frame, rotated input,
  block progress, cancellation and monitoring. Storage-disabled and
  backend-outage states remain mock-only, because provoking them would stop
  the shared stack.
- **Live archive cleanup**: `corpus-live.spec.mjs` and `storage-live.spec.mjs`
  delete every archive they create; `live.spec.mjs` and
  `model-alignment-live.spec.mjs` do not, so the operator deletes their
  `plain_text`, `simple_table` and `formula` archives (`confirm: job_id`)
  after the run.

### 10.3 Implementation order for new cases

1. Prefer **mock coverage first** in `tests/*.spec.mjs` — deterministic,
   CI-runnable, no stack needed.
2. Add a **live case** only when the behaviour needs real GPU/storage; put it
   in `*-live.spec.mjs` and require `OCR_UI_BASE_URL`.
3. Add a **node unit** for any pure validator/model before adding browser
   coverage (`tests/*.test.mjs`).
4. Register the case here and in `coverage-contract.json`; move it out of
   `exclusions` only with the spec that produces its evidence.

### 10.4 Last recorded run

Revision 2, 2026-10-26, against a healthy local stack (`PaddleOCR-VL-1.6`,
storage profile on, `OCR_UI_BASE_URL=http://127.0.0.1:8020`):

- `npm test` — 146 tests, PASS (Node unit + testbook coverage and corpus gate).
- `npx playwright test --project=mock` — 150 tests, PASS.
- `npx playwright test --config playwright.guides.config.js` — 73 tests, PASS.
- `npx playwright test --project=live` on `corpus-live`, `storage-live`,
  `live` and `model-alignment-live` — 10 tests, PASS; corpus and storage
  archives deleted by the specs, the four `live`/`model-alignment-live`
  archives deleted by the operator (§10.2).
- `rag-repair-live.spec.mjs` — **NOT RUN**: its three stored benchmark archives
  (`irs-1040-2025`, `bert`, `istat-rapporto-2025-cap1-pages001-020`) are not in
  this stack's storage.
