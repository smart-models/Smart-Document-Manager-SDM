// ── API client for the OCR Server console ──
// ES module — every method maps one-to-one to a server endpoint and resolves
// to {ok, status, data, error, elapsed}; network errors are captured, not thrown.

import {
  extractErrorMessage,
  normalizeServerLimits,
} from "/assets/models.js?v=11";

// The console is served by the OCR server itself, so the API base URL is
// always the page origin; the listen port is configured server-side via .env,
// never in the UI.
const BASE_URL = window.location.origin.replace(/\/+$/, "");

// The bearer token is the API key the user registers from the topbar popover
// (see initApiKey in app.js); it must match the token half of an
// OCR_SERVER_API_TOKENS entry on the server or protected endpoints answer 401.
const APIKEY_STORAGE_KEY = "ocr_api_key";
const LEGACY_STORAGE_KEY = "ocr_token";

let memoryToken = "";

function storedToken(storageName, key) {
  try { return window[storageName].getItem(key) || ""; }
  catch { return ""; }
}

function clearLegacyToken() {
  try {
    localStorage.removeItem(APIKEY_STORAGE_KEY);
    localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch { /* Storage may be disabled by browser policy. */ }
}

// Migrate once at module startup; getToken never writes to storage.
memoryToken = storedToken("sessionStorage", APIKEY_STORAGE_KEY)
  || storedToken("localStorage", APIKEY_STORAGE_KEY)
  || storedToken("localStorage", LEGACY_STORAGE_KEY);
if (memoryToken) {
  try { sessionStorage.setItem(APIKEY_STORAGE_KEY, memoryToken); } catch { /* In-memory session fallback. */ }
}
clearLegacyToken();

export function getToken() { return memoryToken; }

export function saveToken(value) {
  memoryToken = value || "";
  try {
    if (memoryToken) sessionStorage.setItem(APIKEY_STORAGE_KEY, memoryToken);
    else sessionStorage.removeItem(APIKEY_STORAGE_KEY);
  } catch { /* The active tab can still use the in-memory token. */ }
  clearLegacyToken();
  window.dispatchEvent(new CustomEvent("ocr-auth-status", { detail: "unknown" }));
}

// Server-provided flag: true when the server enforces bearer auth
// (OCR_SERVER_API_TOKENS set). Emitted by /assets/config.js.
export function authRequired() {
  return Boolean(window.OCR_CONFIG && window.OCR_CONFIG.authRequired);
}

// Server-provided limits for client-side pre-validation; null when config.js
// is unavailable (older server or the console hosted elsewhere).
export function serverLimits() {
  return normalizeServerLimits(window.OCR_CONFIG || {});
}

function inferenceTimeoutMs() {
  const seconds = window.OCR_CONFIG?.requestTimeoutSeconds;
  return typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0
    ? (seconds + 60) * 1000 : 660_000;
}

async function request(method, path, { body, params, multipart = false, blob = false, signal, timeoutMs = 5000, inference = false } = {}) {
  const started = performance.now();
  const controller = new AbortController();
  let timedOut = false;
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const url = new URL(BASE_URL + path);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, v);
      }
    }
    const headers = {};
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    let payload;
    if (multipart) {
      // FormData — the browser sets the multipart boundary itself; a manual
      // Content-Type would break the body.
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const resp = await fetch(url.toString(), { method, headers, body: payload, signal: controller.signal });
    if (path !== "/health" && (resp.ok || resp.status === 401 || resp.status === 403)) {
      window.dispatchEvent(new CustomEvent("ocr-auth-status", {
        detail: resp.ok ? "accepted" : "rejected",
      }));
    }
    let data;
    let text = "";
    if (blob && resp.ok) data = await resp.blob();
    else {
      text = await resp.text();
      if (path === "/metrics") data = text;
      else {
        try { data = JSON.parse(text); } catch { data = text; }
      }
    }
    const elapsed = (performance.now() - started) / 1000;
    let error = null;
    if (!resp.ok) {
      error = `HTTP ${resp.status}`;
      if (resp.status === 401) {
        error = getToken()
          ? "API key rejected by the server — register the correct one."
          : "API key required — register it from the API KEY button.";
      } else {
        error = extractErrorMessage(data, error);
      }
    }
    return { ok: resp.ok, status: resp.status, data, text, error, elapsed,
      sha256: resp.headers.get("X-OCR-Content-SHA256") || "",
      storageStatus: resp.headers.get("X-OCR-Storage-Status") || "",
      jobId: resp.headers.get("X-OCR-Job-Id") || "" };
  } catch (err) {
    const elapsed = (performance.now() - started) / 1000;
    // Raw fetch errors ("TypeError: Failed to fetch") are noise to users;
    // the console keeps the original for debugging.
    if (!controller.signal.aborted) console.warn("API request failed:", err);
    return {
      ok: false,
      status: 0,
      data: null,
      error: timedOut
        ? inference
          ? "Request timed out in the browser. OCR processing may still continue on the server."
          : `Request timed out after ${Math.round(timeoutMs / 1000)} s.`
        : controller.signal.aborted ? "Request cancelled."
          : "Cannot reach the OCR server (network error)",
      elapsed,
    };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

function invalidFile() {
  return Promise.resolve({ ok: false, status: 0, data: null, error: "Select a valid file before submitting.", elapsed: 0 });
}

export const api = {
  getToken,
  saveToken,
  authRequired,
  serverLimits,

  createChunks(body) {
    return request("POST", "/chunk", { body, timeoutMs: 180_000 });
  },

  storageArchives(params, { signal } = {}) {
    return request("GET", "/storage/archives", { params, signal, timeoutMs: 15000 });
  },
  storageArchive(prefix, { signal } = {}) {
    return request("GET", "/storage/archive", { params: { prefix }, signal, timeoutMs: 15000 });
  },
  // Full downloads may be large (the server allows 120 s); previews are capped.
  storageFile(key, { preview = false, signal } = {}) {
    return request("GET", "/storage/file", {
      params: { key, preview }, blob: true, signal, timeoutMs: preview ? 20000 : 130000,
    });
  },
  refineRag(body, { signal } = {}) {
    return request("POST", "/storage/rag/refine", {
      body, signal, timeoutMs: 130000,
    });
  },
  splitRag(body, { signal } = {}) {
    return request("POST", "/storage/rag/split", {
      body, signal, timeoutMs: 30000,
    });
  },
  storageArchiveZip(prefix, { signal } = {}) {
    return request("GET", "/storage/archive.zip", { params: { prefix }, blob: true, signal, timeoutMs: 130000 });
  },
  deleteStorageArchive(prefix, jobId, { signal } = {}) {
    return request("DELETE", "/storage/archive", {
      params: { prefix }, body: { confirm: jobId }, signal, timeoutMs: 15000,
    });
  },
  deleteStorageFile(key, name, { signal } = {}) {
    return request("DELETE", "/storage/file", {
      params: { key }, body: { confirm: name }, signal, timeoutMs: 15000,
    });
  },

  // Parse — multipart file upload (PDF or image).
  parseFile(file, { output = "both", max_pages, dpi, table_format, include_images = false, json_rag = false, progress_id, signal } = {}) {
    if (!(file instanceof Blob)) return invalidFile();
    const fd = new FormData();
    fd.append("file", file);
    fd.append("output", output);
    if (max_pages != null && max_pages !== "") fd.append("max_pages", String(max_pages));
    if (dpi != null && dpi !== "") fd.append("dpi", String(dpi));
    if (table_format != null && table_format !== "") fd.append("table_format", String(table_format));
    fd.append("include_images", include_images ? "true" : "false");
    fd.append("json_rag", json_rag ? "true" : "false");
    if (progress_id) fd.append("progress_id", progress_id);
    return request("POST", "/parse", { body: fd, multipart: true, signal, timeoutMs: inferenceTimeoutMs(), inference: true });
  },

  // Live progress snapshot of a running /parse (404 once unknown/expired).
  parseProgress(id, { signal } = {}) {
    return request("GET", "/parse/progress/" + id, { signal, timeoutMs: 3000 });
  },

  // Office → PDF preview (the same LibreOffice conversion /parse runs);
  // data is the PDF Blob on success.
  officePdf(file, { signal } = {}) {
    if (!(file instanceof Blob)) return invalidFile();
    const fd = new FormData();
    fd.append("file", file);
    return request("POST", "/office/pdf", { body: fd, multipart: true, blob: true, signal, timeoutMs: inferenceTimeoutMs(), inference: true });
  },

  // OCR — multipart image upload with task selection.
  ocrFile(file, { task = "ocr", signal } = {}) {
    if (!(file instanceof Blob)) return invalidFile();
    const fd = new FormData();
    fd.append("file", file);
    fd.append("task", task);
    return request("POST", "/ocr", { body: fd, multipart: true, signal, timeoutMs: inferenceTimeoutMs(), inference: true });
  },

  // Monitoring — /metrics resolves with raw Prometheus text in data.
  health(options) { return request("GET", "/health", options); },
  stats(options) { return request("GET", "/stats", options); },
  metrics(options) { return request("GET", "/metrics", options); },
};
