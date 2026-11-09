// ── OCR Server console ──
// One-page UI: Parse, OCR, Monitor. All API traffic goes through
// assets/api.js; all pure logic lives in assets/models.js.

// Static assets are revalidated with Cache-Control: no-cache; legacy version
// queries remain compatible with existing clients.
import { initTabs, initPopover, keepFocus, setHtml, setRegionLocked } from "/assets/ui.js?v=4";
import { api } from "/assets/api.js?v=10";
import {
  alertHtml,
  adjustCounterMetrics,
  autotuneTiles,
  buildOcrHelp,
  buildParseHelp,
  chartMarkdown,
  chartScales,
  chartSvg,
  downloadBaseName,
  esc,
  formatBytes,
  isOfficeUpload,
  isPdfUpload,
  metricTiles,
  metricsTable,
  MONITORING_HELP,
  STORAGE_HELP,
  ocrResultMode,
  otslToHtml,
  pageLimitValidationError,
  pagesMarkdown,
  parseEndpointMetrics,
  parseRejectedTotal,
  parseStorageMetrics,
  RAG_HELP,
  processStartTime,
  spottingRegions,
  statusTile,
  uploadValidationError,
  validateParseResponse,
  validateOcrResponse,
  validateHealthResponse,
  validateStatsResponse,
  validateMetricsResponse,
  withoutJsonRag,
} from "/assets/models.js?v=11";
import { renderMarkdown, renderFormula } from "/assets/math.js";
import { POLL_MS, progressTiles, progressStatus, processedPages, nextSnapshot } from "/assets/parse-progress.js";
import { mountViewer } from "/assets/viewer.js?v=1";
import { mountStorage } from "/assets/storage.js";
import { mountRagStorage } from "/assets/rag-storage.js";
import { mountRagSelection } from "/assets/rag-selection.js";

// Monitor state persists across tab changes. In-flight work belongs to the
// active monitor session and is cancelled when leaving the tab.
const SERIES_MAX = 120;
let monitorTimer = null;
const monitorSeries = [];
// View-only reset baselines: displayed counts = raw counter − baseline.
// Raw snapshots are kept so "Reset counters" can baseline the latest values.
let counterBaseline = new Map();
let lastRawCounters = new Map();
let lastProcessStart = null;
let monitorSnapshotFresh = false;
let monitorController = null;
// Time of the last tick where health, stats and metrics all answered; shown
// under the Status badges so stale panels stay recognisable after a failure.
let monitorLastOk = null;

function renderMonitorUpdated(ok) {
  const el = document.getElementById("mon-updated");
  const now = new Date().toLocaleTimeString();
  if (ok) {
    monitorLastOk = now;
    el.className = "caption";
    el.textContent = `last refresh ${now} · every 5 s`;
  } else {
    el.className = "caption down";
    el.textContent = `refresh failed at ${now}` +
      (monitorLastOk ? ` — showing data from ${monitorLastOk}` : "");
  }
}

// Chart geometry shared by the renderer and the hover layer. Width follows
// the container at render time (1 SVG unit = 1 CSS pixel).
let CHART_W = 600;
const CHART_H = 340;
const CHART_PAD = { top: 22, right: 16, bottom: 26, left: 46 };

// ── Init ──────────────────────────────────────────────────────
const uiLimits = api.serverLimits();
applyServerLimits(uiLimits);
const tablist = document.querySelector(".tabs");
initTabs(tablist);
// Page-specific URL persistence and polling; shared UI owns ARIA and keyboard navigation.
tablist.addEventListener("click", (event) => {
  const tab = event.target.closest('[role="tab"]');
  if (!tab || !tablist.contains(tab)) return;
  const name = tab.dataset.tab;
  history.replaceState({ tab: name }, "", `#${name}`);
  if (name === "monitor" && !document.hidden) startMonitor();
  else stopMonitor();
  if (name === "storage") storagePanel.activate();
  else storagePanel.deactivate();
  if (name === "rag") ragStoragePanel.activate();
  else ragStoragePanel.deactivate();
});
// Archive-only has no OCR to offer: the Parse and OCR tabs disappear and a
// visitor lands on Storage, which is what that mode exists for.
const INFERENCE_TABS = ["parse", "ocr"];
const defaultTab = () => document.getElementById(archiveOnly ? "tab-storage" : "tab-parse");
function activateHashTab() {
  const name = window.location.hash.slice(1);
  const tab = [...tablist.querySelectorAll('[role="tab"]')]
    .find((item) => item.dataset.tab === name && !item.hidden);
  (tab || defaultTab()).click();
}
function applyArchiveOnlyTabs() {
  for (const name of INFERENCE_TABS) document.getElementById(`tab-${name}`).hidden = archiveOnly;
  const active = tablist.querySelector('[aria-selected="true"]')?.dataset.tab;
  if (archiveOnly && INFERENCE_TABS.includes(active)) defaultTab().click();
}
window.addEventListener("hashchange", activateHashTab);
fillReference("parse-reference", buildParseHelp(uiLimits));
fillReference("ocr-reference", buildOcrHelp(uiLimits));
fillReference("rag-reference", RAG_HELP);
fillReference("storage-reference", STORAGE_HELP);
fillReference("monitor-reference", MONITORING_HELP);
const clearMonitorChartSelection = initMonitorChart();
initMonitorReset();
const ragPanel = mountRagSelection(
  document.getElementById("rag-root"), api,
  document.getElementById("rag-execution-root"),
  { onSaved: saved => ragStoragePanel.added(saved) },
);
const storagePanel = mountStorage(document.getElementById("storage-root"), api);
const ragStoragePanel = mountRagStorage(document.getElementById("rag-storage-root"), api, {
  onSelect: (value, file, parent) => ragPanel.select(value, file, parent),
  onClear: () => ragPanel.clear(),
});

// ── Health polling ────────────────────────────────────────────
// False until the first healthy probe: the action panels stay locked while
// the backend is degraded or unreachable (Monitor stays open on purpose —
// it is where the outage is diagnosed).
let servicesReady = false;
// True while /health reports mode "archive_only": OCR is off by design, so
// the Parse and OCR tabs are hidden instead of raising an alarm. apiHealthy keeps the
// server's own status apart from it: an archive-only server whose bucket is
// unreachable is degraded, and must not be described as serving archives.
let archiveOnly = false;
let apiHealthy = false;

// "ok", "missing" or "rejected" (see initApiKey). Anything but "ok" locks every
// tab that calls a protected endpoint; Monitor stays open for diagnosis.
let authState = "ok";
const AUTH_MESSAGES = {
  missing: "API KEY MISSING — OPEN API KEY AND REGISTER A KEY",
  rejected: "API KEY REJECTED — OPEN API KEY AND REGISTER THE CORRECT KEY",
};

const canRun = () => servicesReady && authState === "ok";

function renderServiceGate() {
  const alerts = [];
  // A healthy archive-only server needs no banner: its Parse and OCR tabs
  // are hidden, so nothing on screen is waiting for OCR.
  if (!servicesReady && !(archiveOnly && apiHealthy)) alerts.push(archiveOnly
    ? alertHtml("error",
      "ARCHIVE-ONLY MODE, STORAGE UNREACHABLE — this server runs without vLLM, and /health reports its archive bucket as unavailable, so stored archives cannot be read until storage recovers. OCR stays disabled by the operator. See the Monitor tab for details.")
    : alertHtml("error",
      "OCR backend unavailable — new parses and recognitions are paused until /health reports healthy. Results already shown stay available. See the Monitor tab for details."));
  if (authState !== "ok") alerts.push(alertHtml("error", AUTH_MESSAGES[authState]));
  // setHtml: the assertive region must not re-announce an unchanged banner.
  setHtml(document.getElementById("service-gate"), alerts.join(""));
}

function applyLockdowns() {
  const authLocked = authState !== "ok";
  // Parse and OCR need the model and a key; RAG and Storage only a key. Only
  // controls lock: results stay readable and copyable.
  const regions = {
    "parse-controls": !canRun(),
    "ocr-controls": !canRun(),
    "rag-storage-root": authLocked,
    "rag-root": authLocked,
    "rag-execution-root": authLocked,
    "storage-root": authLocked,
  };
  const fallback = document.getElementById(authLocked ? "apikey-btn" : "conn-retest");
  for (const [id, locked] of Object.entries(regions)) {
    const region = document.getElementById(id);
    if (locked) keepFocus(region.contains(document.activeElement) ? document.activeElement : null, fallback);
    setRegionLocked(region, locked);
  }
  applyArchiveOnlyTabs();
  renderServiceGate();
  refreshRunButtons();
}

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

let healthRequest = null;

async function pollHealth() {
  if (healthRequest) return healthRequest;
  healthRequest = pollHealthInner();
  try { await healthRequest; } finally { healthRequest = null; }
}

async function pollHealthInner() {
  const el = document.getElementById("conn-health");
  const status = document.getElementById("conn-health-status");
  const detail = document.getElementById("conn-health-detail");

  try {
    const result = await api.health();
    if (!result.ok) throw new Error(result.error || "Health check failed.");
    const data = result.data;
    const validationError = validateHealthResponse(data);
    if (validationError) throw new Error(validationError);
    archiveOnly = data.mode === "archive_only" || data.ocr_enabled === false;
    apiHealthy = data.status === "healthy";
    servicesReady = apiHealthy && !archiveOnly;
    el.className = `conn-health ${apiHealthy ? "ok" : "bad"}`;
    setText(status, data.status.toUpperCase());
    detail.textContent = `${archiveOnly ? "archive-only · " : ""}vllm ${data.vllm} · ${data.model} · ${result.elapsed.toFixed(3)}s`;
  } catch (error) {
    archiveOnly = false;
    apiHealthy = false;
    servicesReady = false;
    el.className = "conn-health bad";
    setText(status, "UNREACHABLE");
    detail.textContent = typeof error?.message === "string"
      ? error.message : "Health check failed.";
  } finally {
    applyLockdowns();
  }
}

// Background tabs do not poll: probes resume, immediately, once visible.
let healthTimer = null;
function scheduleHealth() {
  clearInterval(healthTimer);
  healthTimer = document.hidden ? null : setInterval(pollHealth, 10000);
}
// After the lock state above exists: initApiKey applies the first auth lock.
initApiKey();
pollHealth();
scheduleHealth();
document.addEventListener("visibilitychange", () => {
  scheduleHealth();
  if (!document.hidden) pollHealth();
  const monitorOpen = document.getElementById("tab-monitor").getAttribute("aria-selected") === "true";
  if (document.hidden) stopMonitor();
  else if (monitorOpen) startMonitor();
});
document.getElementById('conn-retest').addEventListener('click', pollHealth);

// ── API key ───────────────────────────────────────────────────

// The key is kept in this browser session and attached by api.js as the bearer
// token on every request: it must match the token half of an
// OCR_SERVER_API_TOKENS entry, otherwise protected data endpoints answer 401.
function initApiKey() {
  const btn = document.getElementById("apikey-btn");
  const popover = document.getElementById("apikey-popover");
  const input = document.getElementById("apikey-input");
  const clearBtn = document.getElementById("apikey-clear");
  const status = document.getElementById("apikey-status");
  let closeTimer;

  // Every protected response reports the key's verdict (api.js); saveToken
  // reports "unknown", which changes the state only once the key is gone.
  // Dot: red while the key is missing or rejected; blue for 3 s when a newly
  // saved or previously failing key is accepted, then off.
  let keyVerified = false;
  let dotTimer;
  const setAuthState = (detail) => {
    const hasKey = Boolean(api.getToken());
    if (!api.authRequired()) authState = "ok";
    else if (!hasKey) authState = "missing";
    else if (detail === "accepted") authState = "ok";
    else if (detail === "rejected") authState = "rejected";
    if (detail === "unknown" || authState !== "ok") keyVerified = false;
    if (authState !== "ok" || !hasKey) {
      clearTimeout(dotTimer);
      btn.classList.remove("is-set");
    } else if (detail === "accepted" && !keyVerified) {
      keyVerified = true;
      clearTimeout(dotTimer);
      btn.classList.add("is-set");
      dotTimer = setTimeout(() => btn.classList.remove("is-set"), 3000);
    }
    btn.classList.toggle("is-bad", authState !== "ok");
    applyLockdowns();
  };
  window.addEventListener("ocr-auth-status", (event) => setAuthState(event.detail));

  const setStatus = (text, kind = "") => {
    status.textContent = text;
    status.className = `caption apikey-status${kind ? ` ${kind}` : ""}`;
  };

  // Reflects storage into the UI: clear button, placeholder, topbar dot.
  // The stored key is never echoed back into the field. When the server
  // doesn't enforce auth (config.js says so), the popover says the key
  // is not required instead of implying a silent failure.
  const syncState = () => {
    const hasKey = Boolean(api.getToken());
    clearBtn.hidden = !hasKey;
    input.placeholder = hasKey ? "••••••••" : "type or paste the key";
    setStatus(api.authRequired() ? "" : "AUTH DISABLED SERVER-SIDE — KEY NOT REQUIRED");
  };

  const { close: closePopover } = initPopover({
    button: btn, popover,
    onOpen: () => {
      clearTimeout(closeTimer);
      input.value = "";
      syncState();
      input.focus();
    },
  });

  // A real <form> gives Enter-to-submit for free, on every keyboard.
  document.getElementById("apikey-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) {
      setStatus("ENTER A KEY FIRST", "error");
      input.focus();
      return;
    }
    api.saveToken(value);
    input.value = "";
    syncState();
    const verification = await api.stats();
    if (!verification.ok) {
      setStatus(verification.error || "KEY SAVED — VERIFICATION UNAVAILABLE", "error");
      return;
    }
    setStatus("API KEY REGISTERED", "ok");
    closeTimer = setTimeout(() => closePopover(), 800);
  });

  clearBtn.addEventListener("click", () => {
    api.saveToken("");
    syncState();
    setStatus("API KEY REMOVED", "ok");
    input.focus();
  });

  syncState();
  setAuthState("unknown");
  // A stored key may have been revoked since it was registered: verify it
  // once so the lock appears without waiting for the first protected call.
  if (api.authRequired() && api.getToken()) void api.stats();
}

// ── Parse tab ─────────────────────────────────────────────────
function applyServerLimits(limits) {
  const input = document.getElementById("parse-max-pages");
  if (limits.maxPages) {
    input.max = String(limits.maxPages);
    input.value = String(limits.maxPages);
  }
}

// Errors shown below the form are either validation (recomputed on every
// change) or the last server answer, which only a new file or run replaces.
function showError(errors, message, kind) {
  errors.dataset.kind = kind;
  errors.innerHTML = alertHtml("error", message);
}

function clearError(errors, kind = null) {
  if (kind && errors.dataset.kind !== kind) return;
  delete errors.dataset.kind;
  errors.innerHTML = "";
}

function maxPagesValue() {
  const input = document.getElementById("parse-max-pages");
  return input.validity.badInput ? null : input.value;
}

function updateUploadSelection({
  input,
  name,
  run,
  errors,
  allowPdf,
  extraError = null,
  newFile = false,
}) {
  const file = input.files[0];
  if (file) {
    const literal = el("span", "literal");
    literal.textContent = file.name;
    name.replaceChildren(literal, ` (${formatBytes(file.size)})`);
  } else name.textContent = name.dataset.empty || "No file selected";
  const error = uploadValidationError(file, uiLimits.maxFileBytes, { allowPdf })
    || extraError;
  if (error) showError(errors, error, "validation");
  else clearError(errors, newFile ? null : "validation");
  run.disabled = !file || Boolean(error) || !canRun() || run.dataset.busy === "true";
  return !error;
}

function refreshRunButtons() {
  for (const name of ["parse", "ocr"]) {
    const run = document.getElementById(`${name}-run`);
    const file = document.getElementById(`${name}-file`).files[0];
    const error = uploadValidationError(file, uiLimits.maxFileBytes, { allowPdf: name === "parse" })
      || (name === "parse" && pageLimitValidationError(maxPagesValue(), uiLimits.maxPages));
    run.disabled = !canRun() || !file || Boolean(error) || run.dataset.busy === "true";
  }
}

// Results describe the run that produced them. Changing the file or an option
// afterwards marks them as such instead of silently mixing old and new.
function markResultsStale(containerId, summary) {
  const container = document.getElementById(containerId);
  if (!container.querySelector(".pv") || container.querySelector(".stale-note")) return;
  const note = document.createElement("p");
  note.className = "alert stale-note";
  const literal = el("span", "literal");
  literal.textContent = summary;
  note.replaceChildren("These results are from the previous run (", literal, "). Run again to apply the new selection.");
  container.prepend(note);
}

const parseFileInput = document.getElementById('parse-file');
const parseFileBtn = document.getElementById('parse-file-btn');
const parseFileName = document.getElementById('parse-file-name');
const parseRun = document.getElementById('parse-run');
const parseErrors = document.getElementById("parse-errors");

parseFileName.dataset.empty = parseFileName.textContent;

function updateParseSelection(newFile = false) {
  return updateUploadSelection({
    input: parseFileInput,
    name: parseFileName,
    run: parseRun,
    errors: parseErrors,
    allowPdf: true,
    extraError: pageLimitValidationError(maxPagesValue(), uiLimits.maxPages),
    newFile,
  });
}

parseFileBtn.addEventListener('click', () => parseFileInput.click());
parseFileInput.addEventListener('change', () => updateParseSelection(true));

// Output selection: markdown and json are independent checkboxes — both
// checked sends "both", one checked sends that value. At least one must
// stay on: unchecking the last remaining checkbox is intercepted and
// re-checked before the change event fires.
function parseOutputValue() {
  const md = document.getElementById("parse-out-markdown").checked;
  const js = document.getElementById("parse-out-json").checked;
  return md && js ? "both" : md ? "markdown" : "json";
}

// Live request recap: exactly the multipart fields the next run will send.
// Empty values are omitted; dpi is present only for PDF and Office uploads.
function parseOptions(file) {
  const options = {
    file: file ? file.name : null,
    output: parseOutputValue(),
    table_format: document.getElementById("parse-tables").value,
    include_images: document.getElementById("parse-images").checked,
    json_rag: document.getElementById("parse-out-rag").checked,
  };
  const maxPages = (maxPagesValue() ?? "").trim();
  const dpi = document.getElementById("parse-dpi").value.trim();
  if (maxPages) options.max_pages = maxPages;
  if (file && (isPdfUpload(file) || isOfficeUpload(file)) && dpi) options.dpi = dpi;
  return options;
}

function renderParseRecap() {
  document.getElementById("parse-request-json").textContent =
    JSON.stringify(parseOptions(parseFileInput.files[0]), null, 2);
}

// Guard: if the user unchecks the last selected output checkbox, snap it
// back on so the payload always carries at least one representation.
for (const cb of ["parse-out-markdown", "parse-out-json"]) {
  document.getElementById(cb).addEventListener("click", function () {
    if (!this.checked && !document.getElementById(
      cb === "parse-out-markdown" ? "parse-out-json" : "parse-out-markdown"
    ).checked) {
      this.checked = true;
    }
  });
}

let lastParseRun = null;

function refreshParseForm() {
  renderParseRecap();
  updateParseSelection();
  if (lastParseRun && (parseFileInput.files[0] !== lastParseRun.file
    || JSON.stringify(parseOptions(parseFileInput.files[0])) !== lastParseRun.options)) {
    markResultsStale("parse-results", lastParseRun.label);
  }
}

document.getElementById("panel-parse").addEventListener("change", refreshParseForm);
document.getElementById("parse-max-pages").addEventListener("input", refreshParseForm);
renderParseRecap();

let parseViewer = null;

// Shared run lifecycle for Parse and OCR: busy state, a Cancel button that
// takes focus (so it is never dropped on <body>), an elapsed-time ticker, a
// screen-reader status line, and a guard against leaving mid-request. With a
// progressId (Parse), the ticker renders live badges from /parse/progress,
// polled without overlap; `send` receives one callback that stops both.
const activeRuns = new Set();
window.addEventListener("beforeunload", (event) => {
  if (activeRuns.size) event.preventDefault();
});

async function runRequest({ name, busyLabel, idleLabel, send, progressId = null }) {
  const run = document.getElementById(`${name}-run`);
  const cancel = document.getElementById(`${name}-cancel`);
  const results = document.getElementById(`${name}-results`);
  const status = document.getElementById(`${name}-status`);
  const controller = new AbortController();
  const onCancel = () => controller.abort();
  // Read focus before disabling: a disabled button loses it to <body>.
  const hadFocus = document.activeElement === run;
  run.disabled = true;
  run.dataset.busy = "true";
  run.textContent = busyLabel;
  cancel.hidden = false;
  cancel.addEventListener("click", onCancel);
  if (hadFocus) cancel.focus();
  activeRuns.add(controller);
  status.textContent = `${busyLabel}`;
  const started = Date.now();
  // Parse only: poll the server snapshot; a late reply after stop is ignored.
  let last = null;
  let stopped = false;
  let failures = 0;
  let polling = false;
  let announced = 0; // processed-page count already announced
  const progress = () => {
    const elapsed = Math.floor((Date.now() - started) / 1000);
    results.innerHTML = progressId
      ? metricTiles(progressTiles(last, elapsed), { fit: true })
      : `<div class="alert">PROCESSING… ${elapsed}s</div>`;
  };
  progress();
  const poller = progressId ? setInterval(async () => {
    if (polling) return; // never overlap requests
    polling = true;
    let reply = null;
    try {
      reply = await api.parseProgress(progressId, { signal: controller.signal });
    } catch { /* counted as a failed poll */ }
    polling = false;
    if (stopped) return;
    ({ snapshot: last, failures } = nextSnapshot(last, reply, failures));
    const text = progressStatus(last);
    if (text && processedPages(last) !== announced) { announced = processedPages(last); status.textContent = text; }
  }, POLL_MS) : null;
  const stop = () => { stopped = true; clearInterval(ticker); clearInterval(poller); };
  const ticker = setInterval(progress, 1000);
  try {
    return await send(controller.signal, stop);
  } finally {
    stop();
    activeRuns.delete(controller);
    cancel.removeEventListener("click", onCancel);
    const cancelHadFocus = document.activeElement === cancel;
    cancel.hidden = true;
    delete run.dataset.busy;
    run.textContent = idleLabel;
    refreshRunButtons();
    if (cancelHadFocus || (hadFocus && document.activeElement === document.body)) {
      (run.disabled ? document.getElementById(`${name}-file-btn`) : run).focus({ preventScroll: true });
    }
  }
}

parseRun.addEventListener('click', async () => {
  const file = parseFileInput.files[0];
  if (!file || !canRun() || parseRun.dataset.busy === "true") return;
  if (!updateParseSelection()) return;
  // Freeze what this run asked for: later clicks must not change its downloads.
  const options = parseOptions(file);
  const wanted = {
    download: document.getElementById("parse-download").checked,
    markdown: document.getElementById("parse-out-markdown").checked,
    json: document.getElementById("parse-out-json").checked,
    rag: document.getElementById("parse-out-rag").checked,
  };
  clearError(parseErrors);
  parseViewer?.destroy();
  parseViewer = null;
  lastParseRun = null;
  const status = document.getElementById("parse-status");
  const progressId = crypto.randomUUID();

  await runRequest({
    progressId,
    name: "parse", busyLabel: "Parsing…", idleLabel: "Parse document",
    send: async (signal, stopProgress) => {
      try {
        const result = await api.parseFile(file, { ...options, progress_id: progressId, signal });
        stopProgress();
        if (result.ok) {
          const validationError = validateParseResponse(result.data);
          if (validationError) throw new Error(validationError);
          renderParseResults(result.data, file, result);
          lastParseRun = { file, options: JSON.stringify(options), label: file.name };
          refreshParseForm();
          status.textContent = `Parse complete: ${result.data.pages.length} pages.`;
          if (wanted.download) {
            try {
              const base = downloadBaseName(file.name);
              if (wanted.markdown) downloadText(`${base}.md`, pagesMarkdown(result.data), "text/markdown");
              if (wanted.json) downloadJson(`${base}.json_ocr.json`, withoutJsonRag(result.data));
              if (wanted.rag && result.data.json_rag) downloadJson(`${base}.json_rag.json`, result.data.json_rag);
            } catch (error) {
              const message = `Download failed: ${error.message || "export unavailable"}. OCR results remain available.`;
              showError(parseErrors, message, "server");
              status.textContent = message;
            }
          }
        } else {
          const message = signal.aborted
            ? "Parse cancelled. The server may still finish the current page."
            : result.error;
          showError(parseErrors, message, "server");
          status.textContent = message;
          document.getElementById("parse-results").innerHTML = "";
        }
      } catch (error) {
        const message = error.message || "Cannot display or download the Parse response.";
        showError(parseErrors, message, "server");
        status.textContent = message;
        document.getElementById("parse-results").innerHTML = "";
      }
    },
  });
});

function truncationSummary(warnings) {
  const pages = warnings.find(w => w.code === "pages_truncated");
  const images = warnings.filter(w => w.code === "images_truncated").length;
  const parts = [];
  if (pages) {
    const { max_pages: kept, total_pages: total } = pages.details || {};
    parts.push(Number.isInteger(kept) && Number.isInteger(total) ? `${kept}/${total} pages` : "pages");
  }
  if (images) parts.push(`${images} images`);
  return parts.join(" · ") || "no";
}

function pageSizeSummary(pages) {
  const sizes = new Set(pages.filter(p => Number.isFinite(p.width) && Number.isFinite(p.height))
    .map(p => `${p.width} × ${p.height} px`));
  return sizes.size === 1 ? [...sizes][0] : sizes.size ? `mixed (${sizes.size} sizes)` : "—";
}

function renderParseResults(data, file, { storageStatus = "", jobId = "" } = {}) {
  // Archive outcome from X-OCR-Storage-Status / X-OCR-Job-Id; "—" for older servers.
  const storage = {
    stored: { text: jobId ? `stored · ${jobId}` : "stored", cls: "" },
    failed: { text: "failed", cls: " down" },
    disabled: { text: "off", cls: "" },
  }[storageStatus] ?? { text: "—", cls: "" };
  const c = document.getElementById('parse-results');
  c.innerHTML = '';

  const pageErrors = data.pages.filter(p => p.error).length;
  const truncated = truncationSummary(data.warnings || []);
  const metrics = el('div', 'metric-row metric-row-fit');
  metrics.innerHTML = `
    <div class="metric"><div class="metric-label">file</div><div class="metric-value" title="${esc(file.name)}">${esc(file.name)}</div></div>
    <div class="metric"><div class="metric-label">processing time</div><div class="metric-value">${(data.processing_time_ms / 1000).toFixed(2)}s</div></div>
    <div class="metric"><div class="metric-label">pages</div><div class="metric-value">${data.pages.length}</div></div>
    <div class="metric"><div class="metric-label">page size</div><div class="metric-value">${esc(pageSizeSummary(data.pages))}</div></div>
    <div class="metric"><div class="metric-label">page errors</div><div class="metric-value${pageErrors ? " down" : ""}">${pageErrors}</div></div>
    <div class="metric"><div class="metric-label">content tokens</div><div class="metric-value">${data.usage ? data.usage.content_tokens.toLocaleString("en-US") : "—"}</div></div>
    <div class="metric"><div class="metric-label">truncated</div><div class="metric-value${truncated === "no" ? "" : " down"}">${esc(truncated)}</div></div>
    <div class="metric"><div class="metric-label">storage</div><div class="metric-value${storage.cls}">${esc(storage.text)}</div></div>`;
  c.appendChild(metrics);

  if (!data.pages.length) {
    c.appendChild(alertEl('NO PAGES RETURNED'));
    return;
  }

  parseViewer = mountViewer(c, file, data);
}

// ── OCR tab ───────────────────────────────────────────────────
const ocrFileInput = document.getElementById('ocr-file');
const ocrFileBtn = document.getElementById('ocr-file-btn');
const ocrFileName = document.getElementById('ocr-file-name');
const ocrRun = document.getElementById('ocr-run');
const ocrErrors = document.getElementById("ocr-errors");
let ocrViewer = null;

ocrFileName.dataset.empty = ocrFileName.textContent;
let lastOcrRun = null;

ocrFileBtn.addEventListener('click', () => ocrFileInput.click());
ocrFileInput.addEventListener('change', () => {
  updateUploadSelection({
    input: ocrFileInput,
    name: ocrFileName,
    run: ocrRun,
    errors: ocrErrors,
    allowPdf: false,
    newFile: true,
  });
});

// Live request recap, same pattern as the Parse tab.
function ocrOptions(file) {
  return {
    file: file ? file.name : null,
    task: document.getElementById("ocr-task").value,
  };
}

function renderOcrRecap() {
  document.getElementById("ocr-request-json").textContent =
    JSON.stringify(ocrOptions(ocrFileInput.files[0]), null, 2);
}

function refreshOcrForm() {
  renderOcrRecap();
  if (lastOcrRun && (ocrFileInput.files[0] !== lastOcrRun.file
    || JSON.stringify(ocrOptions(ocrFileInput.files[0])) !== lastOcrRun.options)) {
    markResultsStale("ocr-results", lastOcrRun.label);
  }
}
document.getElementById("panel-ocr").addEventListener("change", refreshOcrForm);
renderOcrRecap();

ocrRun.addEventListener('click', async () => {
  const file = ocrFileInput.files[0];
  if (!file || !canRun() || ocrRun.dataset.busy === "true") return;
  if (!updateUploadSelection({
    input: ocrFileInput,
    name: ocrFileName,
    run: ocrRun,
    errors: ocrErrors,
    allowPdf: false,
  })) return;

  const options = ocrOptions(file);
  const download = document.getElementById("ocr-download").checked;
  clearError(ocrErrors);
  ocrViewer?.destroy();
  ocrViewer = null;
  lastOcrRun = null;
  const status = document.getElementById("ocr-status");

  await runRequest({
    name: "ocr", busyLabel: "Processing…", idleLabel: "Run OCR",
    send: async (signal, stopProgress) => {
      try {
        const pendingSize = imageSize(file, signal);
        const result = await api.ocrFile(file, { ...options, signal });
        stopProgress();
        if (result.ok && !signal.aborted) {
          const validationError = validateOcrResponse(result.data);
          if (validationError) throw new Error(validationError);
          const size = await pendingSize;
          if (signal.aborted) throw new Error("Recognition cancelled.");
          renderOcrResults(result.data, file, size);
          lastOcrRun = { file, options: JSON.stringify(options), label: `${file.name}, ${options.task}` };
          refreshOcrForm();
          status.textContent = `Recognition complete: ${visibleText(result.data).length} characters.`;
          if (download) {
            try {
              const base = downloadBaseName(file.name);
              downloadText(`${base}.ocr.md`, visibleText(result.data), "text/markdown");
              downloadJson(`${base}.ocr.json`, result.data);
            } catch (error) {
              const message = `Download failed: ${error.message || "export unavailable"}. OCR results remain available.`;
              showError(ocrErrors, message, "server");
              status.textContent = message;
            }
          }
        } else {
          const message = signal.aborted ? "Recognition cancelled." : result.error;
          showError(ocrErrors, message, "server");
          status.textContent = message;
          document.getElementById("ocr-results").innerHTML = "";
        }
      } catch (error) {
        const message = error.message || "Cannot display or download the OCR response.";
        showError(ocrErrors, message, "server");
        status.textContent = message;
        document.getElementById("ocr-results").innerHTML = "";
      }
    },
  });
});

// Natural pixel size of the upload, or null when the browser cannot decode it.
function imageSize(file, signal) {
  return new Promise((resolve) => {
    const finish = (size) => {
      signal.removeEventListener("abort", abort);
      resolve(size);
    };
    const abort = () => finish(null);
    if (signal.aborted) { resolve(null); return; }
    signal.addEventListener("abort", abort, { once: true });
    Promise.resolve().then(() => createImageBitmap(file)).then((bitmap) => {
      try { finish({ width: bitmap.width, height: bitmap.height }); }
      finally { bitmap.close(); }
    }, () => finish(null));
  });
}

// Spotting boxes arrive in the server's order: legacy [[x1,y1,x2,y2]]
// rectangles first, then one box per LOC region. LOC boxes are normalized
// 0–1000 units; legacy ones are pixels of the image the model saw, which the
// server upscales 2× when both sides are under 1500 px (preprocess_for_spotting).
// Both are scaled onto the original image for the overlay and region list.
const SPOTTING_UPSCALE_THRESHOLD = 1500;

function spottingBlocks(data, size) {
  if (data.task !== "spotting") return null;
  const boxes = data.boxes || [];
  const regions = spottingRegions(data.result);
  const legacy = Math.max(0, boxes.length - regions.length);
  const upscaled = size && size.width < SPOTTING_UPSCALE_THRESHOLD && size.height < SPOTTING_UPSCALE_THRESHOLD;
  const pixelScale = upscaled ? 0.5 : 1;
  return boxes.map((box, i) => {
    const isLegacy = i < legacy;
    const scale = isLegacy ? [pixelScale, pixelScale]
      : size ? [size.width / 1000, size.height / 1000] : [1, 1];
    return {
      type: "text",
      content: isLegacy ? "" : regions[i - legacy]?.text || "",
      order: i + 1,
      bbox: box.map((v, k) => v * scale[k % 2]),
    };
  });
}

// Formulas via KaTeX, rich Markdown/HTML through the sanitizer, OTSL rebuilt
// as a table, seal output literal. Spotting reads as plain text here: its
// location tokens live in the overlay and the regions tab. The raw tab always
// keeps the exact model output.
function ocrResultHtml(data) {
  if (!data.result) return `<p class="caption">No text recognized.</p>`;
  if (data.task === "formula") return `<div class="result-text formula-preview">${renderFormula(data.result)}</div>`;
  if (ocrResultMode(data.task, data.result) === "rich") {
    const source = data.task === "chart" ? chartMarkdown(data.result) : data.result;
    return `<div class="result-text">${renderMarkdown(source, { recognized: true })}</div>`;
  }
  if (data.task === "table") {
    const table = otslToHtml(data.result);
    if (table) {
      return `<div class="result-text"><div class="table-wrap">${table.html}</div></div>` + (table.complete ? ""
        : `<p class="caption">The output stops mid-table, so the last row is incomplete — see raw for the exact text.</p>`);
    }
    return `<p class="caption">The table could not be rebuilt from its cell markers — see raw for the exact text.</p>` +
      `<pre class="codeblock pv-literal"><code>${esc(data.result)}</code></pre>`;
  }
  return `<pre class="codeblock pv-literal"><code>${esc(visibleText(data))}</code></pre>`;
}

// Recognized text without spotting location tokens, which are coordinates.
function visibleText(data) {
  return data.task === "spotting" ? data.result.replace(/<\|LOC_\d+\|>/g, "") : data.result;
}

function renderOcrResults(data, file, size) {
  const c = document.getElementById("ocr-results");
  c.innerHTML = "";

  const truncated = (data.warnings || []).some(w => w.code === "generation_truncated");
  const blocks = spottingBlocks(data, size);
  const metrics = el("div", "metric-row metric-row-fit");
  metrics.innerHTML = `
    <div class="metric"><div class="metric-label">file</div><div class="metric-value" title="${esc(file.name)}">${esc(file.name)}</div></div>
    <div class="metric"><div class="metric-label">processing time</div><div class="metric-value">${(data.processing_time_ms / 1000).toFixed(2)}s</div></div>
    <div class="metric"><div class="metric-label">task</div><div class="metric-value">${esc(data.task)}</div></div>
    <div class="metric"><div class="metric-label">image size</div><div class="metric-value">${size ? `${size.width} × ${size.height} px` : "—"}</div></div>
    <div class="metric"><div class="metric-label">output</div><div class="metric-value">${visibleText(data).length.toLocaleString("en-US")} chars</div></div>
    ${blocks ? `<div class="metric"><div class="metric-label">regions</div><div class="metric-value">${blocks.length}</div></div>` : ""}
    <div class="metric"><div class="metric-label">truncated</div><div class="metric-value${truncated ? " down" : ""}">${truncated ? "yes" : "no"}</div></div>`;
  c.appendChild(metrics);

  for (const w of data.warnings || []) {
    c.appendChild(warningEl(w));
  }

  const page = { page: 1, width: size?.width, height: size?.height, markdown: data.result || null, blocks };
  ocrViewer = mountViewer(c, file, { pages: [page] }, {
    labels: { markdown: "raw", blocks: "regions" },
    renderContent: () => ocrResultHtml(data),
  });
}

// ── Monitor tab ───────────────────────────────────────────────
// Polled only while the tab is open (startMonitor/stopMonitor from the tab
// hook); the connbar keeps its own slower /health poll.

function renderMonitorChart() {
  clearMonitorChartSelection();
  const plot = document.getElementById("mon-chart-plot");
  CHART_W = Math.max(320, plot.clientWidth || 600);
  plot.innerHTML = chartSvg(monitorSeries, CHART_W, CHART_H, CHART_PAD);
}

// Crosshair + tooltip on the requests chart. Listeners live on the
// persistent wrapper; the SVG inside is replaced on every refresh tick.
function initMonitorChart() {
  const wrap = document.getElementById("mon-chart");
  const cross = document.getElementById("mon-chart-cross");
  const tip = document.getElementById("mon-chart-tip");
  const read = document.getElementById("mon-chart-read");
  let keyIndex = null;
  const hide = () => {
    cross.hidden = true;
    tip.hidden = true;
    tip.textContent = "";
    tip.removeAttribute("title");
    read.textContent = "";
    keyIndex = null;
  };
  wrap.addEventListener("pointerleave", hide);
  // Redraw at the new width on resize; between resizes the SVG stretches,
  // so the redraw keeps 1 unit = 1 pixel and the text crisp.
  window.addEventListener("resize", () => {
    if (monitorSeries.length >= 2 && wrap.clientWidth) {
      hide();
      renderMonitorChart();
    }
  });
  const sampleText = (sample) => `${sample.t} · ${sample.requests.toLocaleString()} requests`;
  // Keyboard reading: arrows move a sample at a time, Home/End jump.
  wrap.addEventListener("keydown", (event) => {
    if (monitorSeries.length < 2) return;
    const last = monitorSeries.length - 1;
    const moves = { ArrowLeft: -1, ArrowRight: 1 };
    let next;
    if (event.key in moves) next = Math.max(0, Math.min(last, (keyIndex ?? last) + moves[event.key]));
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = last;
    else return;
    event.preventDefault();
    keyIndex = next;
    showSample(next);
    read.textContent = `Sample ${next + 1} of ${last + 1}: ${sampleText(monitorSeries[next])}`;
  });
  wrap.addEventListener("blur", hide);
  wrap.addEventListener("pointermove", (event) => {
    const svg = wrap.querySelector("svg");
    if (!svg || monitorSeries.length < 2) { hide(); return; }
    const rect = svg.getBoundingClientRect();
    const innerW = CHART_W - CHART_PAD.left - CHART_PAD.right;
    const vx = (event.clientX - rect.left) / (rect.width / CHART_W);
    showSample(Math.max(0, Math.min(monitorSeries.length - 1,
      Math.round(((vx - CHART_PAD.left) / innerW) * (monitorSeries.length - 1)))));
  });
  function showSample(idx) {
    const svg = wrap.querySelector("svg");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    const scaleX = rect.width / CHART_W;
    const scaleY = rect.height / CHART_H;
    const { x } = chartScales(monitorSeries, CHART_W, CHART_H, CHART_PAD);
    const px = rect.left - wrapRect.left + x(idx) * scaleX;
    cross.style.left = `${px.toFixed(1)}px`;
    cross.style.top = `${(rect.top - wrapRect.top + CHART_PAD.top * scaleY).toFixed(1)}px`;
    cross.style.height =
      `${((CHART_H - CHART_PAD.top - CHART_PAD.bottom) * scaleY).toFixed(1)}px`;
    cross.hidden = false;
    tip.textContent = sampleText(monitorSeries[idx]);
    tip.title = monitorSeries[idx].at;
    tip.hidden = false;
    // Clamp the tooltip inside the wrapper, flipping side near the right edge.
    const tipW = tip.offsetWidth;
    const flip = px + 12 + tipW > wrapRect.width;
    tip.style.left = `${(flip ? px - tipW - 12 : px + 12).toFixed(1)}px`;
    tip.style.top = `${(rect.top - wrapRect.top + 14 * scaleY).toFixed(1)}px`;
  }
  return hide;
}

function initMonitorReset() {
  document.getElementById("mon-reset").disabled = true;
  document.getElementById("mon-reset").addEventListener("click", () => {
    if (!monitorSnapshotFresh || !monitorController) return;
    counterBaseline = new Map(lastRawCounters);
    monitorSeries.length = 0;
    renderMonitorChart();
    monitorTick();
  });
}

let monitorTickOwner = null;

async function monitorTick() {
  // A slow backend must not pile up overlapping refreshes: each tick would
  // race the previous one and render out of order.
  const controller = monitorController;
  if (!controller || monitorTickOwner === controller) return;
  monitorTickOwner = controller;
  try {
    await monitorTickInner(controller.signal);
  } catch {
    if (!controller.signal.aborted) {
      setResetAvailable(false);
      setHtml(document.getElementById("mon-table"), alertHtml("error", "Monitor refresh failed. Retrying on the next refresh."));
      renderMonitorUpdated(false);
    }
  } finally {
    if (monitorTickOwner === controller) monitorTickOwner = null;
  }
}

function setResetAvailable(fresh) {
  monitorSnapshotFresh = fresh;
  const reset = document.getElementById("mon-reset");
  if (!fresh) keepFocus(reset, document.getElementById("mon-chart"));
  reset.disabled = !fresh;
}

async function monitorTickInner(signal) {
  const [health, stats, metrics] = await Promise.all([
    api.health({ signal }), api.stats({ signal }), api.metrics({ signal }),
  ]);
  if (signal.aborted) return;

  const healthError = health.error || validateHealthResponse(health.data);
  const statsError = stats.error || validateStatsResponse(stats.data);
  const metricsError = metrics.error || validateMetricsResponse(metrics.data);
  const healthOk = health.ok && !healthError;
  const statsOk = stats.ok && !statsError;
  const metricsOk = metrics.ok && !metricsError;
  const hd = healthOk ? health.data : {};
  const healthState = !healthOk ? "unknown"
    : String(hd.status || "").toLowerCase() === "healthy" ? "up"
      : String(hd.status || "").toLowerCase() === "degraded" ? "degraded" : "down";
  const vllmRaw = String(hd.vllm || "").toLowerCase();
  // "disabled" is the archive-only API's deliberate absence of a model
  // server: shown neutral, never as DOWN.
  const vllmState = !healthOk ? "unknown"
    : vllmRaw === "reachable" ? "up"
      : vllmRaw === "disabled" ? "disabled" : "down";
  setHtml(document.getElementById("mon-badges"), `<div class="metric-row">` +
    statusTile("health", healthState, healthState === "down" ? String(hd.status || "") : "") +
    statusTile("vllm", vllmState, vllmState === "down" ? vllmRaw : "") +
    `</div>`);

  // Unreachable and "answered with an error" are different diagnoses.
  const healthFailure = health.ok ? healthError : health.status ? `/health answered ${health.error || `HTTP ${health.status}`}` : "server unreachable";
  setHtml(document.getElementById("mon-model"), healthOk
    ? metricTiles([
        ["mode", hd.mode || "ocr"],
        ["model", hd.model || "—"],
        ["version", hd.version || "—"],
        ["pipeline", hd.pipeline_version || "—"],
      ])
    : alertHtml("info", `Model info unavailable — ${healthFailure}.`));

  setHtml(document.getElementById("mon-autotune"),
    hd.autotune && typeof hd.autotune === "object"
      ? autotuneTiles(hd.autotune)
      : alertHtml("info", healthOk ? "No autotune data." : `Autotune state unavailable — ${healthFailure}.`));

  const stEl = document.getElementById("mon-storage");
  if (!healthOk) {
    setHtml(stEl, alertHtml("info", `Storage info unavailable — ${healthFailure}.`));
  } else if (hd.storage?.status === "disabled") {
    setHtml(stEl, alertHtml("info", "Output storage is disabled (OCR_STORAGE_ENABLED unset)."));
  } else {
    const storage = hd.storage && typeof hd.storage === "object" ? hd.storage : {};
    const stamp = Date.parse(storage.timestamp || "");
    const storageMetrics = metricsOk
      ? parseStorageMetrics(metrics.data) : null;
    const tiles = [
      ["last operation", storage.last_operation ?? "—"],
      ["timestamp", Number.isNaN(stamp) ? (storage.timestamp ?? "—") : new Date(stamp).toLocaleString()],
      ["in flight", storageMetrics?.inflight ?? "—"],
      ["bytes archived", storageMetrics?.bytes == null ? "—" : formatBytes(storageMetrics.bytes)],
    ];
    for (const [status, count] of storageMetrics?.attempts || []) {
      tiles.push([`attempts ${status}`, count]);
    }
    const storageState = { ok: "up", error: "down" }[storage.status] || "unknown";
    const statusRow = `<div class="metric-row">${statusTile("status", storageState,
      storageState === "unknown" && storage.status ? String(storage.status) : "")}</div>`;
    setHtml(stEl, statusRow + metricTiles(tiles));
  }

  // Per-endpoint Prometheus rows feed both the totals tiles and the table.
  let totals = null;
  if (metricsOk) {
    const started = processStartTime(metrics.data);
    if (started !== null && lastProcessStart !== null && started !== lastProcessStart) {
      counterBaseline.clear();
      lastRawCounters.clear();
      monitorSeries.length = 0;
    }
    if (started !== null) lastProcessStart = started;
    const adjusted = adjustCounterMetrics(metrics.data, counterBaseline, lastRawCounters);
    lastRawCounters = adjusted.counters;
    const rows = parseEndpointMetrics(adjusted.text);
    totals = {
      requests: rows.reduce((sum, row) => sum + row.requests, 0),
      rejected: parseRejectedTotal(adjusted.text),
    };
    setHtml(document.getElementById("mon-table"), metricsTable(rows));
    setResetAvailable(true);
  } else {
    setResetAvailable(false);
    setHtml(document.getElementById("mon-table"), alertHtml("error",
      `Metrics unavailable${metricsError ? ` — ${metricsError}` : ""}`));
  }
  document.getElementById("mon-chart-stale").hidden = Boolean(totals) || monitorSeries.length < 2;

  if (statsOk) {
    setHtml(document.getElementById("mon-stats"), metricTiles([
      ["parse in flight", stats.data.queue_size ?? "—"],
      ["active requests", stats.data.active ?? "—"],
      ["total_requests", totals ? totals.requests : "—"],
      ["rejected_requests", totals ? totals.rejected : "—"],
    ]));
  } else {
    setHtml(document.getElementById("mon-stats"), alertHtml("error",
      `Stats unavailable${statsError ? ` — ${statsError}` : ""}`));
  }
  // The chart samples the metrics counters, not /stats: a failed stats poll
  // must not punch a hole in the series.
  if (totals) {
    const now = new Date();
    monitorSeries.push({
      t: now.toLocaleTimeString(), at: now.toLocaleString(), requests: totals.requests,
    });
    if (monitorSeries.length > SERIES_MAX) {
      monitorSeries.splice(0, monitorSeries.length - SERIES_MAX);
    }
  }
  // Painted every tick, not only on new samples: with the series empty this
  // is what shows the "Collecting samples" caption instead of a blank box.
  renderMonitorChart();
  renderMonitorUpdated(healthOk && statsOk && metricsOk);
}

function startMonitor() {
  if (monitorTimer !== null) return;
  monitorController = new AbortController();
  monitorTick();
  monitorTimer = setInterval(monitorTick, 5000);
}

function stopMonitor() {
  monitorController?.abort();
  monitorController = null;
  setResetAvailable(false);
  if (monitorTimer !== null) {
    clearInterval(monitorTimer);
    monitorTimer = null;
  }
}

// ── Helpers ───────────────────────────────────────────────────
function el(tag, cls) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function alertEl(msg, isError) {
  const a = el('div', isError ? 'alert alert-error' : 'alert');
  if (isError) a.setAttribute('role', 'alert');
  a.textContent = msg;
  return a;
}

function warningEl(warning) {
  const node = alertEl(
    `[${warning.code || "warning"}] ${warning.message || ""}`,
    warning.severity === "error",
  );
  const context = {};
  if (warning.target && typeof warning.target === "object") {
    context.target = warning.target;
  }
  if (warning.details && typeof warning.details === "object") {
    context.details = warning.details;
  }
  if (Object.keys(context).length) {
    const details = el("details", "warning-details");
    const summary = el("summary");
    summary.textContent = "Context";
    const pre = el("pre", "json-block");
    const code = el("code");
    code.textContent = JSON.stringify(context, null, 2);
    pre.appendChild(code);
    details.append(summary, pre);
    node.appendChild(details);
  }
  return node;
}

function fillReference(containerId, items) {
  document.getElementById(containerId).innerHTML = Object.entries(items).map(([name, desc]) =>
    `<div class="param-card"><div class="param-name">${esc(name)}</div>` +
    `<div class="param-desc">${esc(desc)}</div></div>`
  ).join("");
}

function downloadText(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  // Firefox and Safari read the blob after click() returns: revoke later.
  try { a.click(); }
  finally { setTimeout(() => URL.revokeObjectURL(url), 10000); }
}

function downloadJson(filename, data) {
  downloadText(filename, JSON.stringify(data, null, 2), "application/json");
}

// ── Drop and paste ────────────────────────────────────────────
// A file dropped anywhere must never make the browser navigate away from the
// console (and its results); dropped on Parse or OCR it becomes the upload.
function acceptFile(input, file) {
  if (!file || input.closest("[inert]")) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function activeUploadInput() {
  const tab = tablist.querySelector('[aria-selected="true"]')?.dataset.tab;
  return tab === "parse" ? parseFileInput : tab === "ocr" ? ocrFileInput : null;
}

for (const type of ["dragover", "drop"]) {
  window.addEventListener(type, (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) return;
    event.preventDefault();
    const zone = event.target.closest?.(".drop-zone");
    if (type === "dragover") {
      event.dataTransfer.dropEffect = zone && !zone.inert ? "copy" : "none";
      return;
    }
    document.querySelectorAll(".drop-zone.drag-over").forEach(z => z.classList.remove("drag-over"));
    if (zone) acceptFile(zone.querySelector('input[type="file"]'), event.dataTransfer.files[0]);
  });
}
for (const zone of document.querySelectorAll(".drop-zone")) {
  zone.addEventListener("dragenter", () => zone.classList.add("drag-over"));
  zone.addEventListener("dragleave", (event) => {
    if (!zone.contains(event.relatedTarget)) zone.classList.remove("drag-over");
  });
}
// Pasting a screenshot (not text into a field) selects it as the upload.
window.addEventListener("paste", (event) => {
  const input = activeUploadInput();
  const target = event.target;
  if (!input || target.closest?.("input, textarea, [contenteditable]")) return;
  const file = [...(event.clipboardData?.files || [])][0];
  if (!file) return;
  event.preventDefault();
  acceptFile(input, file);
});

// Restore the selected tab after controls and chart state have initialized.
activateHashTab();
