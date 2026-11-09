// ── OCR Server console — pure logic ──
// No DOM, no vendor imports: everything here is importable in Node
// (node --test) and reused by app.js for rendering.

// ── HTML helpers ────────────────────────────────────────────────────────────

import { esc, alertHtml, jsonBlock, metricTiles } from "./ui.js?v=4";
export { esc, alertHtml, jsonBlock, metricTiles };

export function uppercaseMessage(message) {
  return String(message).replace(/\.+\s*$/, "");
}

// Status badge tile: same look as metricTiles, with the state color on the
// value. `state` is true/"up", "degraded", "unknown", "disabled" or
// false/"down": a server answering degraded, or one that cannot be asked, is
// not reported as down, and a component switched off on purpose (vLLM in
// archive-only mode) is neutral rather than a warning.
const STATUS_WORDS = { up: "UP", degraded: "DEGRADED", unknown: "UNKNOWN", disabled: "DISABLED", down: "DOWN" };
export function statusTile(label, state, detail = "") {
  const key = state === true ? "up" : state === false ? "down" : STATUS_WORDS[state] ? state : "unknown";
  const tone = key === "up" ? "up" : key === "down" ? "down" : key === "disabled" ? "muted" : "warn";
  return `<div class="metric"><div class="metric-label">${esc(label)}</div>` +
    `<div class="metric-value ${tone}">` +
    `${esc(`${STATUS_WORDS[key]}${detail ? ` ${detail}` : ""}`)}</div></div>`;
}

// ── Formatting ──────────────────────────────────────────────────────────────

export function formatBytes(n) {
  const value = Number(n);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let scaled = value;
  let unit = -1;
  do { scaled /= 1024; unit++; } while (scaled >= 1024 && unit < units.length - 1);
  return `${scaled.toFixed(1)} ${units[unit]}`;
}

export function normalizeServerLimits(config = {}) {
  const positiveInteger = (value) => {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  };
  return {
    maxFileBytes: positiveInteger(config.maxFileBytes),
    maxPages: positiveInteger(config.maxPages),
  };
}

export function isPdfUpload(file) {
  if (!file) return false;
  const type = String(file.type || "").trim().toLowerCase();
  const name = String(file.name || "").trim();
  return type === "application/pdf" || /\.pdf$/i.test(name);
}

// Office files are converted to PDF server-side (LibreOffice); the server
// sniffs content, so this only steers the UI (tab choice, dpi field).
export const OFFICE_EXTENSIONS = [
  "docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "odp", "ods",
];
const OFFICE_NAME = new RegExp(`\.(${OFFICE_EXTENSIONS.join("|")})$`, "i");
const OFFICE_TYPE =
  /^application\/(msword|vnd\.ms-(excel|powerpoint)|vnd\.openxmlformats-officedocument\.|vnd\.oasis\.opendocument\.(text|presentation|spreadsheet)$)/;

export function isOfficeUpload(file) {
  if (!file) return false;
  const type = String(file.type || "").trim().toLowerCase();
  const name = String(file.name || "").trim();
  return OFFICE_TYPE.test(type) || OFFICE_NAME.test(name);
}

export function uploadValidationError(
  file,
  maxFileBytes,
  { allowPdf = true } = {},
) {
  if (!file) return null;
  if (Number(file.size) === 0) return "The selected file is empty.";
  if (!allowPdf && (isPdfUpload(file) || isOfficeUpload(file))) {
    return "The OCR endpoint accepts images only — use Parse for PDF or Office documents.";
  }
  // An empty type (unknown extension) is left for the server to sniff.
  const type = String(file.type || "").trim().toLowerCase();
  if (type === "image/svg+xml" || /\.svgz?$/i.test(String(file.name || ""))) {
    return "SVG is a vector format the recognizer cannot read — export it as PNG first.";
  }
  if (type && !type.startsWith("image/") && !isPdfUpload(file) && !isOfficeUpload(file)) {
    return allowPdf
      ? `Unsupported file type ${type} — choose a PDF, an image or an Office document.`
      : `Unsupported file type ${type} — choose an image.`;
  }
  if (maxFileBytes && Number(file.size) > maxFileBytes) {
    return `File size ${formatBytes(file.size)} exceeds the server limit ` +
      `${formatBytes(maxFileBytes)}.`;
  }
  return null;
}

// `value` is the field text, or null when the browser holds unparsable input
// (a number field reports "" for "5-", which must not mean "server default").
export function pageLimitValidationError(value, maxPages) {
  const range = Number.isInteger(maxPages) && maxPages > 0 ? ` from 1 to ${maxPages}` : " of at least 1";
  if (value === null) return `max_pages must be an integer${range}.`;
  const text = String(value ?? "").trim();
  if (!text) return null;
  // Number("1e1") and Number("10.0") are integers, but the server wants digits.
  const parsed = /^\d+$/.test(text) ? Number(text) : NaN;
  const upper = Number.isInteger(maxPages) && maxPages > 0 ? maxPages : null;
  if (!Number.isInteger(parsed) || parsed < 1 || (upper && parsed > upper)) {
    return upper
      ? `max_pages must be an integer from 1 to ${upper}.`
      : "max_pages must be a positive integer.";
  }
  return null;
}

export function ocrResultMode(task, result = "") {
  if (task === "table" && typeof result === "string"
      && /<(?:fcel|ecel|lcel|ucel|xcel|nl)>/i.test(result)) {
    return "literal";
  }
  return ["ocr", "table", "chart"].includes(String(task)) ? "rich" : "literal";
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const finiteNumber = (value) => typeof value === "number" && Number.isFinite(value);

export function validateHealthResponse(data) {
  return isObject(data) && ["status", "vllm", "model"].every(key => typeof data[key] === "string")
    ? null : "Unexpected health response: invalid status or model metadata.";
}

export function validateStatsResponse(data) {
  return isObject(data) && ["active", "queue_size"].every(key => finiteNumber(data[key]) && data[key] >= 0)
    ? null : "Invalid stats response.";
}

export function validateMetricsResponse(text) {
  if (typeof text !== "string") return "Invalid metrics response.";
  let recognized = false;
  for (const line of text.split("\n")) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    const sample = parsePromLine(line.trim());
    if (!sample) return "Invalid metrics response.";
    if (sample.name === "process_start_time_seconds" || sample.name.startsWith("ocr_")) {
      if (!finiteNumber(sample.value) || sample.value < 0) return "Invalid metrics response.";
      recognized ||= ["process_start_time_seconds", "ocr_queue_size", "ocr_active_requests", "ocr_vllm_up",
        "ocr_requests_total", "ocr_requests_rejected_total", "ocr_request_duration_seconds_bucket",
        "ocr_storage_attempts_total", "ocr_storage_inflight", "ocr_storage_bytes_total"].includes(sample.name);
    }
  }
  return recognized ? null : "Invalid metrics response.";
}
const optional = (value, check) => value == null || check(value);
const arrayOf = (value, check) => Array.isArray(value) && value.every(check);
const isString = (value) => typeof value === "string";

function validateCommonResponse(data, endpoint) {
  if (!isObject(data)) return `Unexpected ${endpoint} response: expected an object.`;
  if (!finiteNumber(data.processing_time_ms) || data.processing_time_ms < 0 || !isString(data.model_name)) {
    return `Unexpected ${endpoint} response: invalid processing metadata.`;
  }
  if (!arrayOf(data.warnings, (warning) => isObject(warning)
    && isString(warning.code) && isString(warning.message)
    && optional(warning.severity, isString)
    && optional(warning.target, isObject) && optional(warning.details, isObject))) {
    return `Unexpected ${endpoint} response: invalid warnings.`;
  }
  return null;
}

export function validateParseResponse(data) {
  const error = validateCommonResponse(data, "Parse");
  if (error) return error;
  if (!isString(data.pipeline_version) || !Array.isArray(data.pages)) {
    return "Unexpected Parse response: invalid pages or pipeline version.";
  }
  if (!optional(data.usage, usage => isObject(usage) && Number.isInteger(usage.content_tokens))) {
    return "Unexpected Parse response: invalid token usage.";
  }
  for (const page of data.pages) {
    if (!isObject(page) || !Number.isInteger(page.page)
      || !optional(page.width, Number.isInteger) || !optional(page.height, Number.isInteger)
      || !optional(page.markdown, isString) || !optional(page.error, isString)
      || !optional(page.images, value => arrayOf(value, isString))
      || !optional(page.image_names, value => arrayOf(value, isString))
      || !optional(page.blocks, value => arrayOf(value, block => isObject(block)
        && isString(block.type) && isString(block.content)
        && arrayOf(block.bbox, finiteNumber) && optional(block.order, Number.isInteger)))) {
      return "Unexpected Parse response: invalid page data.";
    }
  }
  return null;
}

export function validateOcrResponse(data) {
  const error = validateCommonResponse(data, "OCR");
  if (error) return error;
  if (!isString(data.result) || !isString(data.task)
    || !optional(data.boxes, value => arrayOf(value, box => arrayOf(box, finiteNumber)))) {
    return "Unexpected OCR response: invalid result or box coordinates.";
  }
  return null;
}

// ── OTSL tables ─────────────────────────────────────────────────────────────

// The table task answers in OTSL: <fcel> text cell, <ecel> empty cell, <lcel>
// merged with the cell on its left, <ucel> with the cell above, <xcel> with
// both, <nl> end of row. Returns { html, complete } — complete is false when
// the output stops mid-row — or null when a merge token has no cell to join,
// so a guessed table is never shown as the real one. Cell text is escaped.
export function otslToHtml(result) {
  const source = String(result);
  const tokens = [...source.matchAll(/<(fcel|ecel|lcel|ucel|xcel|nl)>/gi)];
  if (!tokens.length || source.slice(0, tokens[0].index).trim()) return null;
  const rows = [[]];
  tokens.forEach((match, i) => {
    const tag = match[1].toLowerCase();
    if (tag === "nl") { rows.push([]); return; }
    const end = i + 1 < tokens.length ? tokens[i + 1].index : source.length;
    rows.at(-1).push({ tag, text: source.slice(match.index + match[0].length, end).trim() });
  });
  const complete = rows.at(-1).length === 0;
  if (complete) rows.pop();
  const at = (r, c) => rows[r]?.[c];
  const covered = rows.map((row) => row.map(() => false));
  const html = [];
  for (let r = 0; r < rows.length; r++) {
    const cells = [];
    for (let c = 0; c < rows[r].length; c++) {
      const { tag, text } = rows[r][c];
      if (tag !== "fcel" && tag !== "ecel") continue;
      let colspan = 1;
      while (at(r, c + colspan)?.tag === "lcel") colspan++;
      let rowspan = 1;
      while (at(r + rowspan, c)?.tag === "ucel") rowspan++;
      for (let y = r; y < r + rowspan; y++) {
        for (let x = c; x < c + colspan; x++) {
          if (y === r && x === c) continue;
          const cell = at(y, x);
          if (!cell) continue; // a truncated last row may stop inside the block
          if (["fcel", "ecel"].includes(cell.tag) || covered[y][x]) return null;
          covered[y][x] = true;
        }
      }
      const span = (colspan > 1 ? ` colspan="${colspan}"` : "") + (rowspan > 1 ? ` rowspan="${rowspan}"` : "");
      cells.push(`<td${span}>${esc(text)}</td>`);
    }
    html.push(`<tr>${cells.join("")}</tr>`);
  }
  const orphan = rows.some((row, r) => row.some((cell, c) =>
    !["fcel", "ecel"].includes(cell.tag) && !covered[r][c]));
  if (orphan) return null;
  return { html: `<table><tbody>${html.join("")}</tbody></table>`, complete };
}

// ── Spotting ────────────────────────────────────────────────────────────────

// Mirrors ocrserver.element_ocr.parse_spotting_output for LOC output: each
// group of 8 <|LOC_n|> tokens is one quadrilateral, labelled by the text that
// precedes its run. Boxes are axis-aligned envelopes in normalized 0–1000 units.
export function spottingRegions(result) {
  const regions = [];
  const loc = /<\|LOC_(\d+)\|>/g;
  let coords = [];
  let text = "";
  let last = 0;
  const flush = () => {
    for (let i = 0; i + 8 <= coords.length; i += 8) {
      if (!coords.slice(i, i + 8).every((v) => Number.isFinite(v) && v >= 0 && v <= 1000)) continue;
      const xs = [0, 2, 4, 6].map((k) => coords[i + k]);
      const ys = [1, 3, 5, 7].map((k) => coords[i + k]);
      regions.push({ text, box: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)] });
    }
    coords = [];
  };
  const source = String(result);
  for (const match of source.matchAll(loc)) {
    const gap = source.slice(last, match.index).trim();
    if (gap) { flush(); text = gap; }
    coords.push(Number(match[1]));
    last = match.index + match[0].length;
  }
  flush();
  return regions;
}

// ── Error unwrapping ────────────────────────────────────────────────────────

// OCR Server errors reach the console in two shapes: middleware-level errors
// are flat ({code, message}); endpoint HTTPExceptions are wrapped by FastAPI
// ({detail: {code, message}} or a plain string). Validation errors put an
// array in detail and fall back to the HTTP status.
// Most endpoint errors carry only a stable code: say what it means and what
// to do, keeping the code for bug reports. Limits are appended when sent.
const ERROR_MESSAGES = {
  queue_full: "The server is busy with other documents. Try again in a moment.",
  slots_full: "All recognition slots are busy. Try again in a moment.",
  image_memory_full: "Image processing memory is busy or this image is too large. Downscale it or retry shortly.",
  multiframe_image_not_supported: "This image contains multiple frames. Use the PARSE tab to process all frames.",
  backend_error: "The OCR backend returned an unexpected response. Check the Monitor tab.",
  backend_unavailable: "The OCR backend is unavailable. Check the Monitor tab.",
  shutting_down: "The server is restarting. Try again shortly.",
  rate_limited: "Too many requests from this address. Wait a few seconds and retry.",
  request_timeout: "The server stopped this request at its time limit. Try fewer pages or a lower dpi.",
  internal_error: "The server failed while processing this file.",
  corrupt_image: "The file could not be decoded as an image or PDF. It may be damaged or in an unsupported format.",
  image_too_large: "The image has too many pixels to process safely. Downscale it and retry.",
  file_too_large: "The file exceeds the server upload limit.",
  invalid_task: "Unknown recognition task.",
  invalid_integer: "A numeric field is not a whole number.",
  max_pages_out_of_range: "max_pages is outside the range the server accepts.",
  dpi_out_of_range: "dpi is outside the range the server accepts.",
  office_disabled: "Office conversion is disabled on this server.",
};

function errorDetails(inner) {
  const facts = [];
  if (Number.isFinite(inner.max)) facts.push(`maximum ${inner.max}`);
  if (Number.isFinite(inner.min)) facts.push(`minimum ${inner.min}`);
  if (Number.isFinite(inner.max_pixels)) facts.push(`limit ${inner.max_pixels.toLocaleString("en-US")} pixels`);
  if (inner.value !== undefined && inner.value !== null && typeof inner.value !== "object") {
    facts.push(`received ${JSON.stringify(inner.value)}`);
  }
  return facts.length ? ` (${facts.join(", ")})` : "";
}

export function extractErrorMessage(data, fallback) {
  const inner = data && typeof data === "object" && !Array.isArray(data)
    ? (data.detail ?? data)
    : null;
  if (inner && typeof inner === "object" && !Array.isArray(inner)) {
    if (typeof inner.message === "string" && inner.message) return inner.message;
    if (typeof inner.code === "string" && inner.code) {
      const known = ERROR_MESSAGES[inner.code];
      return known ? `${known}${errorDetails(inner)} [${inner.code}]` : inner.code;
    }
    return fallback;
  }
  if (typeof inner === "string" && inner) return inner;
  return fallback;
}

// ── Display shaping ─────────────────────────────────────────────────────────

// Raw-response rendering copy: include_images can return megabytes of base64
// per page — fine as <img> and in a downloaded file, not as DOM text.
// Pages without images are passed through untouched.
export function slimForDisplay(data) {
  if (!data || !Array.isArray(data.pages)) return data;
  return {
    ...data,
    pages: data.pages.map((p) =>
      p && Array.isArray(p.images) && p.images.length
        ? { ...p, images: `[${p.images.length} base64 image(s) omitted from this view]` }
        : p
    ),
  };
}

// ── Download shaping ────────────────────────────────────────────────────────

// Letters and digits of every script survive (報告書.pdf stays 報告書.pdf);
// separators, control and reserved characters become "_".
export function safeFilename(name) {
  return String(name || "document").normalize("NFC")
    .replace(/[^\p{L}\p{M}\p{N}._-]+/gu, "_");
}

// Downloads share one base: the sanitized upload name without its last
// extension, so report.pdf yields report.md, report.json_ocr.json, report.json_rag.json.
export function downloadBaseName(name) {
  const safe = safeFilename(name).replace(/^[._]+/, "");
  const dot = safe.lastIndexOf(".");
  const base = dot > 0 ? safe.slice(0, dot) : safe;
  return base.replace(/[._]+$/, "") || "document";
}

// Chart output is often a pipe table without its |---| separator row, which
// Markdown does not render as a table. Add the row when the first two lines
// have the same column count.
export function chartMarkdown(result) {
  const text = String(result);
  const lines = text.split("\n");
  const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").length;
  if (lines.length < 2 || !lines[0].includes("|") || /^\s*\|?\s*:?-{3,}/.test(lines[1])
      || cells(lines[0]) < 2 || cells(lines[0]) !== cells(lines[1])) {
    return text;
  }
  const separator = Array.from({ length: cells(lines[0]) }, () => "---").join(" | ");
  return [lines[0], separator, ...lines.slice(1)].join("\n");
}

export function pagesMarkdown(data) {
  return (data.pages || [])
    .map((page) => `<!-- page ${page.page} -->\n\n${page.error
      ? `> OCR failed for page ${page.page}; content is unavailable.`
      : page.markdown || ""}`)
    .join("\n\n");
}

// json_ocr is the Paddle response as before; json_rag is its own download.
export function withoutJsonRag(data) {
  const copy = { ...data };
  delete copy.json_rag;
  return copy;
}

// ── Reference panel content ─────────────────────────────────────────────────
// Rendered as param-cards inside each tab's <details class="reference">.

// House style for every entry below: two or three declarative sentences, 250-400
// characters. Open with what the control does, then the constraint or caveat
// that affects the user's choice. Keep titles aligned with visible controls.
export function buildParseHelp(config = {}) {
  const limits = normalizeServerLimits(config);
  const fileLimit = limits.maxFileBytes
    ? formatBytes(limits.maxFileBytes)
    : "the server-configured upload limit";
  const pageLimit = limits.maxPages
    ? `1–${limits.maxPages}`
    : "the server-configured maximum";
  return {
    file: `Accepts a PDF, an image (PNG, JPEG, WebP, TIFF…) or an Office file (.docx .pptx .xlsx .doc .ppt .xls .odt .odp .ods) up to ${fileLimit}, chosen or dropped on the tab. Images are parsed upright (EXIF) at native resolution; a multi-page TIFF gives one page per frame. Office files are rendered to PDF by LibreOffice first (office_converted); oversize files get 413 file_too_large.`,
    output: "Selects the outputs returned by Parse: markdown, structured json_ocr blocks and the optional json_rag export. At least markdown or json_ocr must remain selected. Block overlays need json_ocr; json_rag creates one chunk per visible OCR block, and export failure leaves the OCR result available with json_rag_failed.",
    max_pages: `Limits processing to the first pages of a PDF, Office document or multi-page image, within the live server range (${pageLimit}). This is a page cap, not a page range. The field starts at the server limit and uses the server default when cleared; skipped pages or image frames are reported by pages_truncated.`,
    dpi: "Sets the rasterization resolution for PDF and Office pages: the presets run from 100 (fast draft) to 300 (fine print), while the API accepts 50–400. Higher values help small print but cost time and memory on every page. Server default omits the field and applies OCR_PARSE_DPI; images keep their native pixels, so dpi is never sent for them.",
    table_format: "Sets how tables are serialized: markdown is the console default and turns every table into a pipe table; merged cells are expanded into repeated values and counted by the tables_spans_expanded info warning. html keeps row and column spans losslessly, while json_ocr blocks[].content always keeps the original HTML. The choice applies to markdown output and json_rag chunks.",
    include_images: "Returns each detected figure as a base64 PNG crop. The viewer shows the crops in the figures tab and inside the rendered markdown, where they replace the imgs/… references; without them those spots show a placeholder. Crops above the server size limit are skipped with images_truncated, and long documents can grow the response by megabytes.",
    "Request recap": "Previews the multipart fields the next run sends and updates on every change. Fields missing from the preview are not uploaded: an empty max_pages falls back to the server limit, and dpi appears only for a PDF or Office document with an explicit value. file shows the name only; the content travels as the upload itself.",
    "Download results": "Saves one file per checked output when the run completes, named after the document without its extension: name.md (pages separated by <!-- page N --> comments), name.json_ocr.json (the full response without json_rag, figures included) and name.json_rag.json when the export was built. The browser may ask once to allow multiple downloads.",
    Run: "Submits the document and counts the elapsed seconds; long documents take minutes, and Cancel stops waiting (the server may finish its current page). Without prompt queue capacity the server answers 503 queue_full, and the per-address rate limit 429 rate_limited. A run past the server timeout ends with 504 request_timeout; nothing is retried automatically.",
    Results: "The badge row summarizes the run: file, server processing time, pages returned, page size in pixels at the parse resolution (mixed when pages differ), pages with errors, content tokens of all blocks and truncation. page errors and truncated turn red when something is missing; each warning below keeps its code, and Context expands its target and details.",
    Viewer: "The page preview is drawn in the browser from your own file; Office documents are converted once more for display. Fit shows the whole page, − and + step from the current scale, the percentage button resets to 100%, and an overflowing page can be dragged. Thumbnails opens a page strip; Blocks toggles the detected regions, and clicking one finds it in the blocks tab.",
    "Content tabs": "rendered shows the page markdown as sanitized HTML with formulas, tables and figures; markdown shows the raw text with a Copy button; blocks lists the detected regions in reading order, unordered ones such as headers last; figures appears only when the page returned crops. The tabs follow the page chosen on the left, and arrow keys move between them.",
    "Block types": "Lists the detected block types with their overlay colors. Selecting a type hides or shows its regions and rows; Hide all and Show all apply to every type. These controls affect the viewer only: they do not remove blocks from downloaded OCR JSON or change the json_rag export generated by Parse.",
    "Saved JSON RAG": "Creates the original JSON RAG export when json_rag is selected. The RAG tab works with files saved in Storage: refresh its list and select the original json_rag.json to refine it. Downloading a local JSON file does not add it to that list, and the desktop RAG tab does not import structured OCR JSON.",
    "Saved archives": "Saves processing results in a server archive when output storage is enabled. This is separate from Download results, which saves files in your browser. Open Storage to browse saved runs and the original JSON RAG files from Parse; a successful OCR response does not guarantee a successful archive write.",
    Availability: "Pauses uploads and new runs until /health reports a healthy OCR backend. Existing results remain readable in the viewer. The banner and Monitor explain the current state; controls unlock after a successful health probe, and an API key must be registered if the server requires authentication.",
  };
}

export function buildOcrHelp(config = {}) {
  const limits = normalizeServerLimits(config);
  const fileLimit = limits.maxFileBytes
    ? formatBytes(limits.maxFileBytes)
    : "the server-configured upload limit";
  return {
    file: `Accepts one single-frame image, chosen, dropped or pasted, up to ${fileLimit}. EXIF orients images and transparency is flattened onto white. PDF, Office, SVG and empty files are rejected before upload; multi-frame images return multiframe_image_not_supported; use Parse for their frames; invalid images return corrupt_image or image_too_large.`,
    task: "Selects one recognition task: ocr extracts text, table returns cell markup (usually OTSL, sometimes HTML or Markdown), formula returns LaTeX, chart converts a chart into a data table, spotting finds text regions with their boxes, seal reads stamp impressions. Spotting upscales 2× when both image sides are below 1500 px.",
    "Request recap": "Previews the multipart body the next run sends — file name and task — and updates on every change so the displayed fields match the next upload. There is nothing else to configure: recognition options are per-task presets chosen by the server, not user parameters.",
    "Download results": "Saves two files when the run completes: <image name>.ocr.md with the recognized text (spotting location tokens removed) and <image name>.ocr.json with the complete response — exact result, task, model, processing time, warnings and, for spotting, every box in its original units. The browser may ask once to allow multiple downloads.",
    Run: "Submits the image and counts the elapsed seconds until the response arrives; Cancel stops waiting. A request that cannot acquire a recognition slot promptly is refused with 503 slots_full, and the per-address rate limit answers 429 rate_limited. A run exceeding the server timeout ends with 504 request_timeout; nothing is retried automatically.",
    Results: "The badge row summarizes the run: file, server processing time, task, image size in pixels, recognized characters (spotting location tokens excluded), the number of regions for spotting, and truncation, which turns red when generation stopped at the token limit. Each warning below keeps its code, and Context expands its details.",
    Viewer: "The uploaded image sits beside the result: Fit shows it whole, − and + step from the current scale, the percentage button resets to 100%, and an overflowing image can be dragged. rendered shows the formatted output, raw the exact text with a Copy button; spotting adds a regions tab and a box overlay whose boxes open their row.",
    Rendering: "Displays text, tables and charts as sanitized Markdown, formulas as formatted math, and seals as literal text. OTSL tables retain merged cells; HTML is literal except tables, and remote images are blocked. Spotting hides location tokens in rendered text and the Markdown download; the raw tab and JSON download preserve the exact result.",
    "Spotting coordinates": "LOC quadrilaterals become axis-aligned boxes [x1,y1,x2,y2] in normalized 0–1000 units, which the viewer scales onto the image for the overlay and the regions tab, each labelled with its text. Legacy numeric rectangles are pixels of the image the model saw, upscaled 2× when both sides are under 1500 px, and are scaled back. The JSON download preserves the original units.",
    Warnings: "generation_truncated means the result is incomplete even when the request succeeds with HTTP 200, and the truncated badge turns red. Partial text is preserved, and Context expands the warning target and details; incomplete spotting coordinates can be reported alongside truncation. The console does not retry recognition automatically.",
    "Saved archives": "Saves processing results in a server archive when output storage is enabled. This is separate from Download results, which saves files in your browser. Open Storage to browse saved runs and the original JSON RAG files from Parse; a successful OCR response does not guarantee a successful archive write.",
    Availability: "Pauses uploads and new runs until /health reports a healthy OCR backend. Existing results remain readable in the viewer. The banner and Monitor explain the current state; controls unlock after a successful health probe, and an API key must be registered if the server requires authentication.",
  };
}

export const STORAGE_HELP = {
  "Output type": "Filters archives by Parse or OCR, while All outputs includes every kind, including parse-chunks. Changing the filter reloads the list from the first page. Incomplete archives can retain a known type in older storage layouts; those with an unknown type appear only under All outputs because their manifest cannot identify the producer.",
  "From / To (UTC)": "Limits the archive list to two calendar dates, both included, using UTC like the Created column. The UTC date may differ from the date in your local timezone. Either date may remain empty for an open range; From must not be later than To, and changing either date reloads the list from the first page.",
  "Search archives": "Filters the archives across every page by what their rows show: file name, archive code, output type, Partial OCR or Incomplete marks, creation time, file count and size, plus the storage prefix. It works as you type, without asking the server, within the Output type and date filters, and returns to the first page. Clearing the field shows all archives again.",
  Refresh: "Reloads every archive of the current filters and stays on the page you are viewing, keeping the open archive when it still exists, so new runs and deletions from other tabs appear. A long list takes longer, with progress in the status line. Changing a filter starts over from the first page. Archives still being written are counted there until their manifest is complete.",
  "Rows per page": "Sets the number of archives per page to 10, 20 or 50. All archives of the current filters are already loaded, so changing the size returns to the first page at once, and Previous and Next move between pages without asking the server. Page X of N counts the archives matching the search; the file table of an open archive has its own rows-per-page control.",
  "Archive list": "Shows one row per saved run: file name and short archive code, output type, creation time in UTC (local time on hover), file count and total size. Select a column header to sort all archives ascending, and again for descending; newest first is the default. Partial OCR marks a parse with failed pages; Incomplete marks an abandoned write, which can only be deleted.",
  Actions: "Each row has icon buttons: open shows the archive's files below the list, ZIP downloads the whole archive as one file, download source appears when the upload was saved, and delete asks for confirmation first; an incomplete row offers only delete. ZIP and file downloads have up to two minutes on the server. Storage is busy means other downloads or listings are running: wait a moment and retry.",
  "Files and preview": "An open archive lists every stored file with its type and size. Preview shows Markdown, JSON and text files as plain text up to 1 MB, never as HTML, and PNG figures up to 10 MB; other types can only be downloaded. The result of each preview or download is reported under the file table, naming the file it refers to.",
  "Delete archive": "Deleting removes every file of the archive permanently; there is no recycle bin. The dialog shows the file name, archive code, creation time and file count, since several uploads can share a name. A partial failure keeps the archive listed so the deletion can be retried, and an archive still being written cannot be deleted yet.",
  Availability: "Storage works independently of the OCR backend, so archives stay browsable while Parse and OCR are locked. It needs OCR_STORAGE_BROWSER_ENABLED and the browser credentials on the server; without them every request reports that browsing is disabled. With OCR_SERVER_API_TOKENS set, register the API key first.",
};

export const RAG_HELP = {
  "Saved JSON RAG": "Lists JSON RAG files saved in server archives. Enable json_rag in Parse, then refresh this list and select the original json_rag.json to refine it. Refined files can be viewed and downloaded but cannot be refined again; this page does not import local OCR JSON or rerun document recognition.",
  "JSON type": "Filters the loaded JSON RAG files by type: All JSON RAG, Original only or Refined only. Original files provide the source for a new refinement; refined files show an existing result. Changing the filter returns to the first page without fetching file contents or changing the selected file.",
  "From / To (UTC)": "Limits the archive scan to two calendar dates, both included, using UTC rather than your local timezone. Either date may remain empty for an open range. From must not be later than To; changing a date reloads the file list; JSON type and Search files are then applied to the files found in that range.",
  "Search files": "Filters loaded files by JSON RAG name, parent document, archive ID or storage prefix. The search includes files across the local result pages. It does not start another server scan; change the UTC range or refresh the list to discover additional archives.",
  "Refresh and pagination": "Reloads the JSON RAG file list from archives within the selected UTC range. Rows per page shows 10, 20 or 50 matching files, with Previous and Next moving through the loaded results. Unavailable archives are reported in the status line; a partial listing should be refreshed before assuming a file is absent.",
  "Select and preview": "Loads a file when you select its name or preview action, then shows its source information and JSON. Original files with chunks can run preflight; refined files are view-only. The JSON preview displays at most 100,000 characters; download the file for its complete contents, including data omitted from the preview.",
  "Download and delete": "Downloads the selected JSON RAG file without changing its archive. Delete is available only for refined files and asks for confirmation before permanently removing that file. The original json_rag.json remains available for another refinement; to remove the entire archive, use the separate delete action in Storage.",
  "min token / max token": "Sets the target range for aggregation, initially 512 to 1024 tokens. Enter positive whole numbers with min token no greater than max token. These are best-effort targets based on stored counts, not a strict chunk limit; whole blocks or protected structures may produce groups outside the range; missing counts stay unknown.",
  "keep tables": "Keeps recognized table continuations together during automatic aggregation. It starts enabled and may produce a group above max token when the protected structure is large. Page boundary can take priority over this protection; changing the option discards the current plan and manual edits; run preflight again.",
  "keep code blocks": "Keeps recognized code-block continuations together during automatic aggregation. It starts enabled and may produce a group above max token when the protected structure is large. Page boundary can take priority over this protection; changing the option discards the current plan and manual edits; run preflight again.",
  "page boundary": "Stops automatic aggregation at page changes when enabled, which is the console default. Document changes and failed-page gaps remain boundaries even when it is off. Missing page information is reported, and this option takes priority over structure protection; a manual merge across pages is recorded as an override.",
  "Run preflight": "Builds an aggregation plan from the selected original file without saving it. Review the groups, warnings and token distribution before making cuts or merges. Changing the selected source, token range or protection options discards the plan and its manual edits; run preflight again to prepare the new choices.",
  "Token range and counts": "Classifies each group as Within range, Below range, Above range or Unknown size using the selected targets. Automatic groups use stored token estimates; cut fragments use counts returned by the server tokenizer. Unknown does not mean zero; the summary and distribution update as cuts, merges and undo change the plan.",
  "Cut": "Splits an oversized group using the server tokenizer and the selected structure settings. The returned fragments may include groups below min token, with notes explaining the result. Cutting does not save a file; if the tokenizer is unavailable, cuts are disabled for the page session; reload the page before retrying them.",
  "Merge and undo": "Combines a below-range or unknown group with an available neighbor in source order. Merging may exceed max token or cross a page boundary, which is reported in the plan. Fragments of the same source chunk must be recomposed with Undo; a gesture cannot be undone while a later gesture still depends on its results.",
  "JSON preview": "Shows the selected source JSON and, after preflight, the proposed aggregation with your manual edits. The plan preview is not the final saved file: execution adds its own metadata. Previews are limited to 100,000 characters and clearly mark truncation; download the saved JSON for the complete result.",
  "Execute and save JSON RAG": "Saves the reviewed plan as a new refined file beside the original. The server checks the source hash and validates the plan and declared fragment counts; whole chunks retain stored estimates. Saving needs output storage and write credentials; the original is preserved, and failures are shown without replacing it.",
};

export const PARSE_HELP = buildParseHelp();
export const OCR_HELP = buildOcrHelp();

export const MONITORING_HELP = {
  "01 Status": "Shows the API health and the reachability of its vLLM backend. UP means the component is available; DEGRADED health indicates an unreachable OCR backend, which pauses Parse and OCR submissions. In archive-only mode the API is UP while vLLM reads DISABLED: the Parse and OCR tabs are hidden, Storage and RAG keep working. UNKNOWN means /health could not be read; polling pauses in hidden tabs.",
  "02 Model": "Shows the mode (ocr or archive_only), model ID, server version and pipeline version reported by /health. Use these values to check the identity reported after a deployment and include them when describing an OCR issue. If health information is unavailable, the panel explains the failure rather than presenting old values as current.",
  "03 Stats": "Shows parse requests holding a queue slot, active Parse and OCR admissions, total requests and rejected requests. The first two values describe current work, not a waiting backlog. Totals come from Prometheus and include rejected responses, so total and rejected requests overlap; Reset counters changes the displayed request totals.",
  "Reset counters": "Resets the displayed request and rejection totals to a baseline taken from the latest metrics snapshot. Endpoint request and error counts follow that baseline; latency and storage counters retain their lifetime values. This changes the view only; reloading the page restores raw totals, and process restarts clear the baseline.",
  Chart: "Plots total requests over time: one sample per successful metrics tick, kept to the last 120 (about ten minutes of polling). The x-axis is by sample index, not wall-clock, so gaps from failed ticks or time spent on other tabs are not drawn. Hover the plot for the exact value at a sample; the series clears on Reset counters, a process restart and page reloads.",
  "04 Autotune": "Shows the reported outcome of stage-2 calibration, its mode, objective and warmup time. The action distinguishes calibration, profile reuse, dry-run, fallback, disabled and not_run outcomes. Reason cards explain disabled or fallback results; the section ends with promoted decisions or the effective settings when no profile is available.",
  "05 Storage": "Shows the archive backend state, last operation and timestamp from /health, plus lifetime storage counters from Prometheus. These include attempts by outcome, active workers and confirmed bytes. A bucket probe runs about once a minute, so status is the latest observation rather than a guarantee; disabled means output storage is off.",
  "06 Per-endpoint metrics": "Shows request counts, 5xx errors and p50/p95 latency for each endpoint, including Office preview conversion. Requests and errors follow Reset counters; latency covers the server lifetime. Errors include 503 admission rejections; the symbol ≈ marks estimated quantiles, while ≥ marks a lower bound beyond the histogram range.",
  "Last refresh": "Shows the time of the last complete Monitor refresh and its five-second polling interval. A failed tick reports the failure time and the last successful refresh when available, so you can recognize stale information. Polling runs while Monitor is active, pauses when the browser tab is hidden and resumes when visible.",
};

// ── Prometheus parsing ──────────────────────────────────────────────────────

// Estimate a quantile from cumulative histogram buckets:
// [[upperBound, cumulativeCount], ...] sorted ascending, +Inf included.
export function estimateQuantile(buckets, q) {
  if (!buckets.length) return 0;
  const total = buckets[buckets.length - 1][1];
  if (total <= 0) return 0;
  const target = q * total;
  let prevBound = 0;
  let prevCount = 0;
  for (const [bound, cum] of buckets) {
    if (cum >= target) {
      if (bound === Infinity) return prevBound;
      const span = cum - prevCount;
      if (span <= 0) return bound;
      const frac = (target - prevCount) / span;
      return prevBound + frac * (bound - prevBound);
    }
    prevBound = bound;
    prevCount = cum;
  }
  return buckets[buckets.length - 1][0];
}

function quantileLowerBound(buckets, q) {
  if (!buckets.length || buckets.at(-1)[1] <= 0) return false;
  const target = q * buckets.at(-1)[1];
  return buckets.find(([, count]) => count >= target)?.[0] === Infinity;
}

export function counterDelta(raw, baseline) {
  return raw < baseline ? raw : raw - baseline;
}

// Baselines belong to individual labelled counters: one endpoint/reason can
// reset while another keeps increasing.
export function adjustCounterMetrics(text, baseline, previous = new Map()) {
  const counters = new Map();
  const adjusted = String(text).split("\n").map(line => {
    const sample = parsePromLine(line.trim());
    if (!sample || !["ocr_requests_total", "ocr_requests_rejected_total"].includes(sample.name)
      || !Number.isFinite(sample.value) || sample.value < 0) return line;
    const key = JSON.stringify([sample.name, Object.entries(sample.labels).sort()]);
    const raw = sample.value;
    counters.set(key, raw);
    if (raw < (baseline.get(key) || 0) || raw < (previous.get(key) ?? raw)) baseline.delete(key);
    const labels = Object.entries(sample.labels).map(([name, value]) => `${name}=${JSON.stringify(value)}`).join(",");
    return `${sample.name}{${labels}} ${counterDelta(raw, baseline.get(key) || 0)}`;
  }).join("\n");
  return { text: adjusted, counters };
}

export function processStartTime(text) {
  for (const line of String(text).split("\n")) {
    const sample = parsePromLine(line.trim());
    if (sample?.name === "process_start_time_seconds" && Number.isFinite(sample.value)) return sample.value;
  }
  return null;
}

function parsePromLine(line) {
  const match = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[+-]?Inf|NaN)(?:\s+-?\d+)?\s*$/.exec(line);
  if (!match) return null;
  const labels = {};
  if (match[2]) {
    if (!/^\s*(?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"\s*,\s*)*(?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"\s*)?$/.test(match[2])) return null;
    const labelRe = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g;
    let lm;
    while ((lm = labelRe.exec(match[2])) !== null) {
      if (Object.hasOwn(labels, lm[1])) return null;
      labels[lm[1]] = lm[2].replace(/\\(["\\n])/g, (_, c) => (c === "n" ? "\n" : c));
    }
  }
  const value = match[3] === "+Inf" ? Infinity : Number(match[3]);
  return { name: match[1], labels, value };
}

// Parse Prometheus exposition text into per-endpoint rows:
// {endpoint, requests, errors, p50, p95}. p50/p95 are null without a histogram.
// Errors count 5xx statuses only — client errors (4xx) are input problems,
// not server failures.
export function parseEndpointMetrics(text) {
  const requests = new Map();
  const errors = new Map();
  const hist = new Map();

  for (const rawLine of String(text).split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const sample = parsePromLine(line);
    if (!sample || Number.isNaN(sample.value)) continue;

    if (sample.name === "ocr_requests_total") {
      const ep = sample.labels.endpoint || "?";
      requests.set(ep, (requests.get(ep) || 0) + sample.value);
      if (String(sample.labels.status || "").startsWith("5")) {
        errors.set(ep, (errors.get(ep) || 0) + sample.value);
      }
    } else if (sample.name === "ocr_request_duration_seconds_bucket") {
      const ep = sample.labels.endpoint || "?";
      const le = sample.labels.le || "+Inf";
      const bound = le === "+Inf" || le === "Inf" ? Infinity : parseFloat(le);
      if (!hist.has(ep)) hist.set(ep, []);
      hist.get(ep).push([bound, sample.value]);
    }
  }

  const endpoints = [...new Set([...requests.keys(), ...hist.keys()])].sort();
  return endpoints.map((ep) => {
    const buckets = (hist.get(ep) || []).slice().sort((a, b) => a[0] - b[0]);
    const hasHist = buckets.length > 0;
    return {
      endpoint: ep,
      requests: requests.get(ep) || 0,
      errors: errors.get(ep) || 0,
      p50: hasHist ? estimateQuantile(buckets, 0.5) : null,
      p95: hasHist ? estimateQuantile(buckets, 0.95) : null,
      p50LowerBound: quantileLowerBound(buckets, 0.5),
      p95LowerBound: quantileLowerBound(buckets, 0.95),
    };
  });
}

// Lifetime archive counters: attempts by outcome, workers in flight, bytes
// confirmed. Deliberately NOT baselined by Reset counters — storage history
// outlives the request-count display window.
export function parseStorageMetrics(text) {
  const attempts = new Map();
  let inflight = null;
  let bytes = null;
  for (const rawLine of String(text).split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const sample = parsePromLine(line);
    if (!sample || Number.isNaN(sample.value)) continue;
    if (sample.name === "ocr_storage_attempts_total") {
      const status = sample.labels.status || "unknown";
      attempts.set(status, (attempts.get(status) || 0) + sample.value);
    } else if (sample.name === "ocr_storage_inflight") {
      inflight = sample.value;
    } else if (sample.name === "ocr_storage_bytes_total") {
      bytes = (bytes || 0) + sample.value;
    }
  }
  return { attempts, inflight, bytes };
}

// Sum of ocr_requests_rejected_total across all rejection reasons.
export function parseRejectedTotal(text) {
  let total = 0;
  for (const rawLine of String(text).split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const sample = parsePromLine(line);
    if (sample && sample.name === "ocr_requests_rejected_total"
        && !Number.isNaN(sample.value)) {
      total += sample.value;
    }
  }
  return total;
}

// ── Monitoring rendering ────────────────────────────────────────────────────

// Every autotune fact as metric cards — never a JSON dump. Scalar decisions
// get one card each; nested decisions flatten to "key: value" text and span
// the row so they stay readable.
function decisionText(value) {
  if (value === null || typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map(decisionText).join(", ");
  return Object.entries(value).map(([k, x]) => `${k}: ${decisionText(x)}`).join(" · ");
}

export function autotuneTiles(at) {
  const row = [
    ["action", at.action || "—"],
    ["mode", at.mode || "—"],
    ["objective", at.objective || "—"],
  ];
  if (Number.isFinite(at.warmup_seconds)) {
    row.push(["warmup (s)", Number(at.warmup_seconds).toFixed(1)]);
  }
  if (typeof at.measured_candidates === "number") {
    row.push(["measured candidates", at.measured_candidates]);
  }
  let html = metricTiles(row);
  if (typeof at.reason === "string" && at.reason) {
    html += metricTiles([["reason", at.reason, { wide: true, text: true }]]);
  }
  // Show both sources when environment overrides change the profile settings.
  const filled = (value) => value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length;
  const profile = filled(at.decisions) ? at.decisions : null;
  const effective = filled(at.effective) ? at.effective : null;
  const overridden = profile && effective && Object.entries(effective).some(([key, value]) => profile[key] !== value);
  const groups = profile ? [[profile, overridden ? " (profile)" : ""]] : [];
  if (effective && (!profile || overridden)) groups.push([effective, " (in effect)"]);
  for (const [decisions, suffix] of groups) {
    html += metricTiles(Object.entries(decisions).map(([key, value]) => {
      const label = key.replaceAll("_", " ") + suffix;
      if (value === null) return [label, "default"];
      if (typeof value === "object") return [label, decisionText(value), { wide: true, text: true }];
      return [label, value];
    }));
  }
  return html;
}

export function metricsTable(rows) {
  if (!rows.length) return alertHtml("info", "No request metrics yet.");
  const quantile = (value, lowerBound) => value === null ? "—"
    : `<span title="${lowerBound ? "Lower bound: histogram range exceeded" : "Estimated from histogram buckets"}">${lowerBound ? "≥" : "≈"}${value.toFixed(4)}</span>`;
  const body = rows.map((r) =>
    `<tr><td>${esc(r.endpoint)}</td>` +
    `<td class="num">${Math.round(r.requests)}</td>` +
    `<td class="num">${Math.round(r.errors)}</td>` +
    `<td class="num">${quantile(r.p50, r.p50LowerBound)}</td>` +
    `<td class="num">${quantile(r.p95, r.p95LowerBound)}</td></tr>`
  ).join("");
  return `<div class="table-wrap"><table>
    <thead><tr><th scope="col">endpoint</th><th scope="col">requests</th>
    <th scope="col">errors</th><th scope="col">p50 (s)</th><th scope="col">p95 (s)</th></tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

// Chart geometry is passed in by the caller (app.js measures the container):
// 1 SVG unit = 1 CSS pixel, so the plot fills the panel instead of being
// letterboxed by a fixed viewBox.
const fmtCompact = new Intl.NumberFormat("en", {
  notation: "compact", maximumFractionDigits: 1,
});

export function chartScales(series, W, H, PAD) {
  const values = series.map((s) => s.requests);
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  return {
    min, max,
    x: (i) => PAD.left + (i / Math.max(1, series.length - 1)) * innerW,
    // A flat series sits mid-height instead of on the bottom edge.
    y: (v) => max === min
      ? H - PAD.bottom - innerH / 2
      : H - PAD.bottom - ((v - min) / (max - min)) * innerH,
  };
}

export function chartSvg(series, W, H, PAD) {
  if (series.length < 2) {
    return `<p class="caption">Collecting samples — the chart appears after the second refresh.</p>`;
  }
  const { min, max, x, y } = chartScales(series, W, H, PAD);
  const pts = series.map((s, i) => `${x(i).toFixed(1)},${y(s.requests).toFixed(1)}`);
  const first = series[0];
  const last = series[series.length - 1];
  const lastX = x(series.length - 1).toFixed(1);
  const lastY = y(last.requests).toFixed(1);
  const baseline = (H - PAD.bottom).toFixed(1);
  // Recessive gridlines on whole request counts; one line when the series is flat.
  const ticks = [...new Set(max === min ? [min] : [min, Math.round((min + max) / 2), max])];
  const grid = ticks.map((v) =>
    `<line x1="${PAD.left}" y1="${y(v).toFixed(1)}" x2="${W - PAD.right}" y2="${y(v).toFixed(1)}" class="chart-grid" />` +
    `<text x="${PAD.left - 8}" y="${(y(v) + 3).toFixed(1)}" class="chart-tick" text-anchor="end">${esc(fmtCompact.format(v))}</text>`
  ).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img"
      aria-label="Total requests over time, from ${esc(String(first.requests))} at ${esc(first.t)} to ${esc(String(last.requests))} at ${esc(last.t)}">
    ${grid}
    <path class="chart-area" d="M${pts.join(" L")} L${lastX},${baseline} L${x(0).toFixed(1)},${baseline} Z" />
    <polyline class="chart-line" points="${pts.join(" ")}" />
    <circle class="chart-dot" cx="${lastX}" cy="${lastY}" r="3.5" />
    <text x="${PAD.left}" y="12" class="chart-title">total requests</text>
    <text x="${W - PAD.right}" y="12" class="chart-value" text-anchor="end">${esc(fmtCompact.format(last.requests))}</text>
    <text x="${PAD.left}" y="${H - 6}" class="chart-tick">${esc(first.t)}</text>
    <text x="${W - PAD.right}" y="${H - 6}" class="chart-tick" text-anchor="end">${esc(last.t)}</text>
  </svg>`;
}
