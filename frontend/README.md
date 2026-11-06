# frontend — Static web console, ES modules, and browser test suites

> **Read this when** changing the web console under `frontend/dist/`, running or adding Node unit tests (`tests/*.test.mjs`), mocked/live Playwright console tests (`tests/*.spec.mjs`), or offline guide browser tests (`guide-tests/guides.spec.js`).
> **Audience** — an AI agent working in this repository, and any human reading over its shoulder.
> **Source of truth** — `frontend/dist/` for the served static console; `frontend/package.json`, `playwright.config.mjs`, and `playwright.guides.config.js` for test runners; [`../scripts/verify_frontend_assets.py`](../scripts/verify_frontend_assets.py) for vendored asset integrity.
> **Prerequisite** — read [`../ocrserver/README.md`](../ocrserver/README.md) and [`../guide/README.md`](../guide/README.md).

---

## Purpose and boundaries

**What it is:** the dependency-free vanilla ES module web console (`frontend/dist/`)
served directly by `ocr_server.py` when `EXPOSE_FRONTEND=true`, together with
three distinct test layers:
1. **Node unit tests (`tests/*.test.mjs`)** — pure model, RAG partition/preflight/repair/structure, and monitoring validation tests run via `node --test`.
2. **Console Playwright suites (`tests/*.spec.mjs`)** — mocked-API browser specs (`--project=mock`) and opt-in live-stack specs (`--project=live`).
3. **Offline field-guide Playwright suite (`guide-tests/guides.spec.js`)** — `file://` verification of `guide/*.html` via `playwright.guides.config.js`.

**What it is not:** a bundler-driven SPA. There is no Webpack/Vite build step:
`frontend/dist/index.html` and `frontend/dist/assets/*.js` are authored directly
as native ES modules and baked into the `ocr-api` image (`/app/frontend/dist`).

## Mental model

1. **`frontend/dist/` is served as-is.** Editing a file under `frontend/dist/`
   changes the runtime console directly; in Docker, rebuild `ocr-api` (`stack.*
   up`) so the updated static files are copied into the container image.
2. **Three browser test projects have strictly separate runtime needs:**
   - `npm test` (`node --test tests/*.test.mjs`): runs in Node without a browser
     or server.
   - `npm run test:e2e` (`playwright test --project=mock`): launches local
     `tests/static-server.mjs` on `http://127.0.0.1:8129` and mocks all API
     routes. Needs Chromium, no live stack.
   - `npm run test:live` (`playwright test --project=live`): runs `*live.spec.mjs`
     against `OCR_UI_BASE_URL` on a live stack.
3. **Offline guide tests (`playwright.guides.config.js`) open `file://` URLs**
   and fail immediately if any `http:` or `https:` network request is attempted.
4. **Vendored frontend assets are hash-verified.** KaTeX, Marked, DOMPurify,
   PDF.js, and WOFF2 fonts under `dist/assets/vendor/` and `dist/assets/fonts/`
   are verified by `scripts/verify_frontend_assets.py`.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `dist/index.html` | Single-page console markup and CSP-compatible asset entry | Console layout, tabs, or landmarks change |
| `dist/assets/app.js`, `api.js`, `models.js`, `ui.js`, `viewer.js`, `math.js` | Core console controller, API client, state models, document viewer, and KaTeX math rendering | Console OCR/parse/monitoring interactions change |
| `dist/assets/rag.js`, `rag-names.js`, `rag-partition.js`, `rag-preflight.js`, `rag-repair.js`, `rag-selection.js`, `rag-storage.js`, `rag-structure.js` | Client-side RAG workspace: chunk inspection, preflight, section structure, and manual split/refine workflows | RAG tab behavior or client-side chunk helpers change |
| `dist/assets/storage.js` | S3 archive browser (loads every cursor page once, then sorts, searches and pages locally), download, confirmation-gated deletion UI, and the `archivePages` listing walk shared with the RAG tab | Storage tab behavior changes |
| `dist/assets/style.css`, `ocr.css`, `rag.css`, `storage.css` | Console stylesheets | Console visual styling or responsive layout changes |
| `dist/assets/fonts/`, `dist/assets/vendor/` | Self-hosted WOFF2 fonts and vendored KaTeX, Marked, DOMPurify, and PDF.js | Vendored library versions change (update `verify_frontend_assets.py` together) |
| `tests/*.test.mjs` | 8 Node unit test files (`models`, `monitor-validation`, `playwright-config`, `rag-names`, `rag-partition`, `rag-preflight`, `rag-repair`, `rag-structure`) | Pure JS logic in `dist/assets/` changes |
| `tests/*.spec.mjs` | 9 mocked Playwright specs (`api`, `audit-console`, `compliance`, `console`, `info-style-console`, `math`, `monitor-console`, `rag`, `storage`) + 5 `*-live.spec.mjs` specs (`live`, `model-alignment-live`, `rag-repair-live`, `storage-live`, `corpus-live`) | Console DOM, accessibility, or API integration changes |
| `tests/testbook/` | E2E acceptance book (`test-book-e2e.md`) and disposition registry (`coverage-contract.json`) mapping every case ID to its executable layer | Any console feature, live-only boundary, or coverage gap changes |
| `tests/testbook/documents/` | Live document corpus: 9 verbatim PaddleOCR `tests/test_files` (Apache-2.0) + 4 files from `generate.py`; `manifest.json` holds origin, SHA-256 and oracle strings read by `corpus-live.spec.mjs` | A live case needs a new document, or the upstream pin changes |
| `tests/testbook-coverage.test.mjs` | `node --test` gate: every `SDM-*` case is owned exactly once, exclusions have ≥40-char reasons, mapped executables exist, every corpus document matches its manifest hash and is named by the book | Testbook rows, `coverage-contract.json` or the corpus change |
| `tests/static-server.mjs`, `tests/fixtures.mjs`, `tests/rag-workflows.mjs` | Local static server (`:8129`) and shared Playwright fixtures/workflows for mocked runs | Mock server routing or shared test helpers change |
| `guide-tests/guides.spec.js`, `playwright.guides.config.js` | Offline Playwright verification of `../guide/*.html` | Field-guide HTML/CSS/JS contracts change |

## Interface reference

Run all commands from `frontend/`:

| Command | Runner | Stack required? | Purpose |
|---|---|---|---|
| `npm test` | `node --test tests/*.test.mjs` | No | Fast pure-JS unit tests (131 assertions) |
| `npm run test:e2e` | `playwright test --project=mock` | No | Browser E2E suite against `tests/static-server.mjs` and mocked API routes |
| `npx --no-install playwright test --config playwright.guides.config.js` | Playwright (`guide-tests/`) | No | Offline `file://` verification of `../guide/*.html` across 320–1440px, print, and no-JS |
| `npm run test:live` | `playwright test --project=live` | **Yes** (`OCR_UI_BASE_URL`) | Live browser verification against a running stack |

## Configuration

| Variable / File | Default | Effect |
|---|---|---|
| `EXPOSE_FRONTEND` (in `.env`) | `true` | Mounts `frontend/dist/` at `/` and `/assets/*` in `ocr_server.py` |
| `OCR_UI_BASE_URL` | unset | Base URL required when running `npm run test:live` (e.g. `http://127.0.0.1:8020`) |
| `playwright.config.mjs` | `http://127.0.0.1:8129` | Selects `mock` project by default and propagates `_OCR_PLAYWRIGHT_LIVE_RUN=1` when `--project=live` is requested |

## Behaviour contracts

- The console never embeds external CDN scripts, stylesheets, or fonts; all
  assets are served from `/assets/` so the UI works offline and under strict CSP.
- Mocked Playwright specs (`--project=mock`) must not depend on a running
  FastAPI server or GPU; `tests/static-server.mjs` serves `dist/` on port `8129`.
- PARSE section 03 sends a `crypto.randomUUID()` as `progress_id` with `/parse`
  and polls `GET /parse/progress/{id}` every second (`parse-progress.js`), never
  overlapping requests, to render state, pages, block, errors, unprocessed, DPI,
  elapsed and ETA badges in place of the `PROCESSING…` timer. The ETA is shown in
  whole minutes (rounded up). A timeout or network failure keeps the last
  snapshot until three in a row fail; a 404 (entry gone) shows dashes at once;
  elapsed always stays local, and a poll reply arriving after the request ends
  is ignored. The screen-reader status line announces completed blocks only.
- PARSE results show metric tiles and the viewer but no warning rows (warnings
  stay in the response and the downloaded `json_ocr`); a `storage` tile reads
  `X-OCR-Storage-Status`/`X-OCR-Job-Id`: `stored · <job id>`, `failed`, `off`,
  or `—` for servers that do not send it.
- A selected json_rag whose `metadata.unprocessed_pages` is non-empty gets a
  `partial` tile in the RAG tab.
- The STORAGE tab loads every `GET /storage/archives` cursor page of the current
  filters (`limit=50`, with progress in the status line) when the tab opens, a
  filter changes or Refresh runs. Everything else works on those rows without a
  request: page size, Previous/Next, column sort (File in natural order, rows
  without a value last, ties newest first, Created descending by default),
  Search archives (any visible column plus the storage prefix) and dropping a row
  after a confirmed deletion. Refresh stays on the viewed page; a filter or
  opening the tab returns to the first. A failed cursor page keeps the rows
  already loaded and reports the list as incomplete; a repeated cursor stops the
  walk instead of looping.
- Live specs (`*-live.spec.mjs`) are excluded from the default `mock` project
  and run only when `--project=live` is explicitly selected (`tests/playwright-config.test.mjs`
  enforces this separation).
- Every E2E acceptance case has an `SDM-<SER>-NNN` ID in
  `tests/testbook/test-book-e2e.md` and a disposition in
  `coverage-contract.json` (`owners` or a reasoned `exclusion`);
  `testbook-coverage.test.mjs` fails `npm test` on drift.
- Corpus documents under `tests/testbook/documents/` are read-only: add or
  regenerate one only together with its `manifest.json` entry
  (`python frontend/tests/testbook/documents/generate.py` refreshes the hashes)
  and the book row that uses it. Copy only files whose licence allows it.

## Capabilities and limits

**You can:** run all unit tests (`npm test`), mocked console E2E tests
(`npm run test:e2e`), and guide browser tests offline without Docker or a GPU.

**You cannot:** use TypeScript/JSX or npm runtime imports inside `dist/assets/*.js`
without vendoring them under `dist/assets/vendor/` and registering their hashes
in `scripts/verify_frontend_assets.py`, or keep the STORAGE tab's load time flat
as archives grow: each load reads every manifest in range (4.6 s measured for 257
archives), so archives in the thousands need server-side sorting first.

## Recipes

**Run the CI frontend unit and guide suites from `frontend/`:**

```powershell
npm ci --ignore-scripts
npm test
npx --no-install playwright test --config playwright.guides.config.js
```

**Run mocked console E2E specs:**

```powershell
npx --no-install playwright test --project=mock
```

**Verify vendored frontend assets from the repository root:**

```powershell
python scripts/verify_frontend_assets.py
```

## Verification

CI runs `npm test` and `scripts/verify_frontend_assets.py` in the `frontend-unit`
job, and `npx --no-install playwright test --config playwright.guides.config.js`
in the `guides` job (`.github/workflows/ci.yml`).

## Failure modes

| Symptom | Next check |
|---|---|
| `verify_frontend_assets.py` fails | Check for missing or modified files under `dist/assets/vendor/` or `dist/assets/fonts/` |
| `npm test` fails in `rag-workflows.mjs` | Verify `../tests/fixtures/ocr-rag-chunks.*.json` fixtures exist and match expected schema |
| Browser changes do not appear on `http://127.0.0.1:8020` | Rebuild `ocr-api` with `stack.* up` (`frontend/dist/` is copied into the image at build time) |
| Port `8129` already in use during `test:e2e` | Stop any stale `node tests/static-server.mjs` process (`reuseExistingServer` is `false` for hermeticity) |

## Invariants

- Node engine is `>=24` in `package.json`; CI uses Node 24 (`actions/setup-node`).
- `tests/playwright-config.test.mjs` enforces that default Playwright runs never
  collect `*-live.spec.mjs` specs.
- `scripts/verify_frontend_assets.py` (tested by `tests/test_frontend_assets.py`)
  enforces exact SHA-256 parity for all vendored frontend assets.

## Extending it

When adding a new ES module under `dist/assets/`, export pure state/validation
functions so they can be unit-tested in `tests/<module>.test.mjs` under
`node --test`, and add UI interaction coverage in `tests/*.spec.mjs`.

## Related documents

- [Core API and static mount](../ocrserver/README.md)
- [Offline HTML field guides](../guide/README.md)
- [Python test suite and RAG fixtures](../tests/README.md)
- [CI workflow architecture](../.github/AGENTS.md)
