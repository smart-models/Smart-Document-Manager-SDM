# guide — Offline operator field guides

> **Read this when** opening, editing or verifying the HTML operator guides.
> **Audience** — operators and agents maintaining the guide set.
> **Source of truth** — this file for guide behavior; `.env.example` for defaults; the linked service references for runtime contracts.
> **Prerequisite** — read the [repository instructions](../AGENTS.md) and [launcher reference](../scripts/README.md).

---

## Purpose and boundaries

Six standalone English HTML pages teach Smart Document Manager. Open
[getting-started.html](getting-started.html) directly in a browser. No running
application, package install, build step or Internet access is needed to read
them. Each file embeds its CSS, classic JavaScript and WOFF2 fonts.

The guides do not read local `.env`, accept credentials, persist configuration,
start processes or contact services. Command previews are text for the reader to
run separately; diagrams, players and labs are authored illustrations, not
measurements or evidence that a backend operation ran.

## Mental model

Start with the first extraction walkthrough. Use CLI for daily operations,
Environment to find a template setting, and Architecture to choose a request
path and deployment shape. Document pipeline explains how a page becomes
labelled blocks in reading order; RAG chunking explains how blocks become
bounded chunks with provenance. Both PowerShell and Bash command columns remain
visible together.

Relative navigation and links to owning references assume the repository layout.
MCP publishes standalone copies as `docs://guide/<name>`; their presentation is
self-contained, while relative repository links require the canonical checkout.

## Map of the code

| File | Lesson |
|---|---|
| [getting-started.html](getting-started.html) | Prerequisites, local secrets, agent-assisted setup, first start, readiness and the six-step console walkthrough |
| [cli.html](cli.html) | Startup prerequisites, the five first-start steps, command preview, alternate setups and recovery boundaries |
| [environment.html](environment.html) | Editing rules, day-one secrets, precedence, storage profile, security, applying settings and all template parameters |
| [architecture.html](architecture.html) | Six-scene pipeline player, services, request paths, deployment explorer, agents and access boundaries |
| [document-pipeline.html](document-pipeline.html) | Layout, recognition and reading order: six-scene page player, reading-order lab, the 25 block labels and parse red flags |
| [rag-chunking.html](rag-chunking.html) | Token budgets and provenance: six-scene chunking player, budget lab, the real chunk schema, routes and budget conflicts |
| [tests/test_guide_links.py](tests/test_guide_links.py) | Local file/fragment checks and Markdown heading anchors |
| [tests/test_guide_contract.py](tests/test_guide_contract.py) | Exact environment parity and offline presentation |
| [Browser suite](../frontend/guide-tests/guides.spec.js) | Offline reading, responsive layouts, keyboard, copy, print and interactions |
| [MCP registry](../ocr-mcp-server/ocr_mcp/resources/docs.py) | Resource URIs and canonical-to-packaged copy map |

## Interface reference

Header/footer links reach every guide: the primary nav carries the four core
guides plus Help and the page's own current-page marker; the footer lists all
six. The brand returns to Getting started. Each page has a skip link, a main
landmark, stable section/disclosure IDs and exactly one
`aria-current="page"`. A hash opens all enclosing disclosures. Parameter IDs
are their exact environment variable names.

Copy buttons appear only when JavaScript runs. If the clipboard is unavailable,
the command is selected and the status explains manual copying; the copied item
is named and the status clears after a few seconds. All authored lessons and
native disclosures remain readable with JavaScript disabled.

Stable interfaces per page:

- **Six-scene player** (`architecture`, `document-pipeline`, `rag-chunking`):
  `#play`, `#stage-previous`, `#stage-next`, `#restart`, `#timeline` with
  `aria-valuetext`, `#speed`, `article#scene-0`…`#scene-5` with
  `h3#scene-title-N`, `.chapter-nav` stage tabs with `aria-pressed`, and
  `details#read-story` as the no-motion prose equivalent. `#scene-N` and
  `#scene-title-N` fragments open that stage paused.
- **Console walkthrough** (`getting-started`): `nav#tour-nav`, six
  `article.tour-step#step-*[data-step]`, `#tour-previous`, `#tour-next`,
  `#tour-position` ("Step n of 6"), `details#read-walkthrough`.
- **Command builder** (`cli`): `#command-choice`, `#mcp-choice`, `#wait-choice`,
  `#frontend-choice` (enabled only where `scripts/README.md` documents the
  flag), `#builder-windows`, `#builder-linux`.
- **Parameter explorer** (`environment`): search box, `#parameter-count`,
  `#no-results`, chips, `#env-section-*` groups, one `.parameter` per
  `.env.example` assignment, `#shell-choice` toggling `data-shell-view` on
  `#apply`.
- **Deployment explorer** (`architecture`): `#deployment`, `#agent-access`,
  `#archive`, `#setup-title`, `#setup-path`, `#mcp-node`, `#storage-node`,
  `#boundary-badge`, `details#private-network-boundary`.
- **Reading-order lab** (`document-pipeline`): `select#sample` (invoice, paper,
  form), `#show-order`, `#show-relations`, `#hide-furniture`, the SVG
  `#page-figure` (`role="img"`, `data-block-type` regions), `ol#block-list`,
  `pre#markdown-preview`, `h3#page-title`. Authored data lives in
  `<template id="sample-*">` elements, never in JSON script tags.
- **Budget lab** (`rag-chunking`): `select#document`,
  `input#budget[type=range]` (128–1024, step 64) with `aria-valuetext` and
  `output#budget-value`, `#overlap-toggle`, `ol#chunk-list`,
  `output#chunk-count` (`aria-live="polite"`). Authored documents live in
  `<template id="doc-*">` elements; the protected unit carries
  `data-protected="true"`.

## Configuration

There is no guide configuration and no credential input. Environment entries
carry `data-env-name`, `data-env-value` and `data-env-state=active|optional`, with
one entry per assignment in the template. Section 14's commented native examples
are distinct from Compose settings. Tests compare exact names, values and state.

## Behaviour contracts

- Keep both shell columns visible, including narrow screens and print; the
  environment shell picker dims rather than hides the other shell.
- Preserve IDs; changing one can break another page, README or MCP reference.
- Align examples with the [launcher contract](../scripts/README.md),
  [environment template](../.env.example) and [application](../README.md).
- Distinguish configuration verification, service readiness and MCP discovery.
- Players never auto-advance under reduced motion; manual stage controls still
  work. The walkthrough sizes only its active panel below 960 px.
- Print opens disclosures, reveals all scenes/steps and clears visual filtering
  temporarily, then restores the exact open/closed and hidden states. Repeated
  prints are supported.
- Labs label themselves illustrative: the reading-order lab is an authored
  page, not an OCR run; the budget lab uses a simple greedy fill while the
  service packs deterministically with dynamic programming and exact tokenizer
  counts. The protected unit must stay in exactly one chunk at every budget.
- Preserve embedded font blocks as whole verified units. Do not edit base64 text.
  [Font notices](FONT-LICENSES.txt) are also embedded as inert text in each HTML
  file so they accompany an individually distributed page.
- Re-copy edited HTML into `ocr_mcp/resources/docs/guide/` in the same change.
  The MCP parity test compares every registered document byte for byte.

## Capabilities and limits

**You can:** open any guide directly over `file://` without a server or network,
search all 108 `.env.example` settings offline, preview PowerShell and Bash
launcher commands side by side, replay the pipeline and chunking players, try
the reading-order and budget labs, and print complete unfolded guides.

**You cannot:** execute commands, read or write a local `.env`, store
credentials, run OCR or chunking, or measure live API/vLLM/storage readiness
from these static HTML files.

## Recipes

Edit the canonical HTML, update its MCP copy, then run checks from the root using
the supported Python environment:

```text
python -m pytest guide/tests tests/test_mcp_server.py tests/test_ci_workflow.py -q
python -m ruff check guide/tests
python -m ruff format --check guide/tests
```

Run the isolated browser suite from `frontend/`, using its pinned dependencies
and installed Chromium:

```text
npx --no-install playwright test --config playwright.guides.config.js
```

The config has no web server and selects only guide specs. It never starts the
OCR stack. The `guides` CI job runs both verification layers and is required
by aggregate `CI`. Release evidence requires `guides.log` and `guides.xml`.

## Verification

| Area | Inspect over `file://` |
|---|---|
| Navigation | Header/footer, current page, brand return, hash reveal and back/forward |
| Keyboard | Skip link, visible focus, native disclosures and labeled controls |
| Layout | All content at 320, 390, 768 and 1440 pixels; browser zoom at 200% |
| Players | Stage tabs, transport buttons, timeline `aria-valuetext`, reduced-motion pause, `#scene-N` fragments, `read-story` without JavaScript |
| Walkthrough | Step nav, "Step n of 6", print reveals all six and restores the active one |
| Labs | Sample/document switch, furniture filter, relation markers, budget slider, protected unit invariant, overlap markers |
| Shell choice | `data-shell-view` dims the other shell; both stay visible in print |
| Print | Mixed disclosures, filtered Environment, repeated print and state restoration |
| Offline | No HTTP requests, external assets or console errors; reading without JavaScript |
| Fonts | Embedded faces and actual Barlow Condensed / JetBrains Mono rendered glyphs |

Keep screenshots, PDFs and local evidence under gitignored `.scratch/`. Record
visual inspection separately from automated results. A local pass does not prove
that hosted GitHub Actions ran.

## Failure modes

| Symptom | Check |
|---|---|
| Missing file or fragment | Run link checks; preserve or restore the target ID |
| Two elements share an ID | Keep runtime IDs out of the markup; scene/section IDs must stay unique |
| Missing or stale setting | Compare exact assignment/state with `.env.example` |
| Search hides a hash target | Keep clear-search and hash-reveal behavior aligned |
| Player advances under reduced motion | `setPlaying` must no-op when the media query matches |
| Lab forgets its data | Authored data belongs in `<template>`/`data-*`, not hidden JSON scripts |
| Print changes reading state | Preserve beforeprint/afterprint state capture |
| MCP page differs | Refresh the packaged copy and run document parity tests |

## Invariants

The guide set is offline, single-file and illustrative. It contains template
defaults and synthetic examples only. Controls produce text or adjust
explanations, never operate the application. Both shells stay visible together.

## Extending it

Add a domain page only for a distinct lesson. Start from an existing page as the
shell, reuse the player component where a journey helps, add navigation, a
stable ID contract, offline tests and a `COPIED_DOCUMENTS`/resource entry.
Update this inventory and CI evidence together.

## Related documents

- [Launcher reference](../scripts/README.md)
- [Environment template](../.env.example)
- [Application reference](../README.md)
- [MCP package](../ocr-mcp-server/README.md)
- [Automation boundaries](../.github/AGENTS.md)
