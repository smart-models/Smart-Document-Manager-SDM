import assert from "node:assert/strict";
import test from "node:test";

import {
  buildOcrHelp,
  buildParseHelp,
  STORAGE_HELP,
  RAG_HELP,
  MONITORING_HELP,
  chartMarkdown,
  chartSvg,
  formatBytes,
  statusTile,
  downloadBaseName,
  extractErrorMessage,
  isOfficeUpload,
  isPdfUpload,
  normalizeServerLimits,
  ocrResultMode,
  otslToHtml,
  pageLimitValidationError,
  pagesMarkdown,
  parseEndpointMetrics,
  safeFilename,
  slimForDisplay,
  spottingRegions,
  uploadValidationError,
  validateParseResponse,
  validateOcrResponse,
  withoutJsonRag,
  counterDelta,
  chartScales,
  metricsTable,
  autotuneTiles,
  metricTiles,
  parseStorageMetrics,
} from "../dist/assets/models.js";

test("normalizes only positive integer server limits", () => {
  assert.deepEqual(
    normalizeServerLimits({ maxFileBytes: "31457280", maxPages: 50 }),
    { maxFileBytes: 31457280, maxPages: 50 },
  );
  assert.deepEqual(
    normalizeServerLimits({ maxFileBytes: 0, maxPages: "invalid" }),
    { maxFileBytes: null, maxPages: null },
  );
});

test("all reference entries keep the house length with live and fallback limits", () => {
  for (const limits of [{}, { maxFileBytes: 10 * 1024 * 1024, maxPages: 12 },
    { maxFileBytes: 1024 * 1024 * 1024, maxPages: 1000 }]) {
    for (const help of [buildParseHelp(limits), buildOcrHelp(limits), STORAGE_HELP, RAG_HELP, MONITORING_HELP]) {
      for (const [name, text] of Object.entries(help)) {
        assert.ok(text.length >= 250 && text.length <= 400, `${name}: ${text.length} characters`);
      }
    }
  }
});

test("OTSL tables rebuild merged cells and refuse orphan merges", () => {
  const table = (rows) => `<table><tbody>${rows}</tbody></table>`;
  assert.deepEqual(otslToHtml("<fcel>A<fcel>B<nl><fcel>1<ecel><nl>"),
    { html: table("<tr><td>A</td><td>B</td></tr><tr><td>1</td><td></td></tr>"), complete: true });
  assert.equal(otslToHtml("<fcel>H<lcel><nl><fcel>a<fcel>b<nl>").html,
    table('<tr><td colspan="2">H</td></tr><tr><td>a</td><td>b</td></tr>'));
  assert.equal(otslToHtml("<fcel>V<fcel>x<nl><ucel><fcel>y<nl>").html,
    table('<tr><td rowspan="2">V</td><td>x</td></tr><tr><td>y</td></tr>'));
  assert.equal(otslToHtml("<FCEL>B<lcel><nl><ucel><xcel><nl>").html,
    table('<tr><td colspan="2" rowspan="2">B</td></tr><tr></tr>'));
  // Cell text is escaped; output that stops mid-row keeps what arrived.
  const partial = otslToHtml("<fcel>A<fcel><b>x</b><nl><fcel>tail");
  assert.equal(partial.complete, false);
  assert.match(partial.html, /<td>&lt;b&gt;x&lt;\/b&gt;<\/td>/);
  assert.match(partial.html, /<td>tail<\/td><\/tr>/);
  for (const bad of ["<lcel>x<nl>", "<ucel><nl>", "<fcel>a<nl><fcel>b<xcel><nl>",
    "<fcel>a<lcel><nl><fcel>b<ucel><nl>", "text<fcel>a<nl>", "plain text"]) {
    assert.equal(otslToHtml(bad), null, bad);
  }
});

test("spotting regions pair each 8-token quadrilateral with its text", () => {
  const loc = (...values) => values.map(v => `<|LOC_${v}|>`).join("");
  assert.deepEqual(spottingRegions(`A${loc(1, 2, 3, 2, 3, 4, 1, 4)}\nB${loc(5, 5, 9, 5, 9, 6, 5, 6)}${loc(7)}`), [
    { text: "A", box: [1, 2, 3, 4] },
    { text: "B", box: [5, 5, 9, 6] },
  ]);
  assert.deepEqual(spottingRegions("plain text"), []);
});

test("builds reference text from live server limits", () => {
  const limits = { maxFileBytes: 10 * 1024 * 1024, maxPages: 12 };
  assert.match(buildParseHelp(limits).file, /10\.0 MB/);
  assert.match(buildParseHelp(limits).max_pages, /1–12/);
  assert.match(buildOcrHelp(limits).file, /10\.0 MB/);
  assert.doesNotMatch(buildParseHelp(limits).Run, /at most 16/);
  assert.doesNotMatch(buildOcrHelp(limits).Run, /at most 32/);
});

test("recognizes PDFs and validates upload size and endpoint type", () => {
  assert.equal(isPdfUpload({ name: "scan.PDF", type: "" }), true);
  assert.equal(isPdfUpload({ name: "scan.bin", type: "application/pdf" }), true);
  assert.equal(isPdfUpload({ name: "scan.png", type: "image/png" }), false);
  assert.match(
    uploadValidationError(
      { name: "scan.pdf", type: "application/pdf", size: 100 },
      1000,
      { allowPdf: false },
    ),
    /accepts images only/,
  );
  assert.match(
    uploadValidationError({ name: "large.png", size: 1001 }, 1000),
    /exceeds the server limit/,
  );
  assert.equal(uploadValidationError({ name: "ok.png", size: 999 }, 1000), null);
  // Known non-image types are refused before upload; unknown (empty) types go to the server.
  const text = { name: "notes.txt", type: "text/plain", size: 10 };
  assert.match(uploadValidationError(text, 1000), /Unsupported file type text\/plain.*PDF, an image or an Office/);
  assert.match(uploadValidationError(text, 1000, { allowPdf: false }), /Unsupported file type.*choose an image/);
  assert.equal(uploadValidationError({ name: "scan.heic", type: "", size: 10 }, 1000, { allowPdf: false }), null);
  assert.equal(uploadValidationError({ name: "photo.jpg", type: "image/jpeg", size: 10 }, 1000, { allowPdf: false }), null);
});

test("recognizes Office documents for parsing but not recognition", () => {
  for (const name of [
    "a.docx", "a.DOC", "a.pptx", "a.ppt", "a.xlsx", "a.xls", "a.odt", "a.odp", "a.ods",
  ]) {
    assert.equal(isOfficeUpload({ name, type: "" }), true, name);
  }
  assert.equal(
    isOfficeUpload({
      name: "report",
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }),
    true,
  );
  for (const name of ["a.pdf", "a.png", "a.zip", "a.exe", "docx"]) {
    assert.equal(isOfficeUpload({ name, type: "" }), false, name);
  }
  const docx = { name: "report.docx", type: "", size: 100 };
  assert.equal(uploadValidationError(docx, 1000), null);
  assert.match(
    uploadValidationError(docx, 1000, { allowPdf: false }),
    /accepts images only.*Office/,
  );
});

test("selects rich rendering only for compatible OCR tasks", () => {
  for (const task of ["ocr", "table", "chart"]) {
    assert.equal(ocrResultMode(task), "rich");
  }
  for (const task of ["formula", "spotting", "seal", "unknown"]) {
    assert.equal(ocrResultMode(task), "literal");
  }
});

test("OTSL tables remain literal while HTML and Markdown stay rich", () => {
  for (const marker of ["fcel", "ecel", "lcel", "ucel", "xcel", "nl"]) {
    assert.equal(ocrResultMode("table", `<${marker}>value`), "literal");
  }
  for (const result of ["<fcel>Name<fcel>Value<nl>", "<fcel>Name", "<FCEL>Name"]) {
    assert.equal(ocrResultMode("table", result), "literal");
    assert.equal(ocrResultMode("table", result), "literal");
  }
  for (const result of [
    "<table><tr><td>42</td></tr></table>",
    "| A | B |\n|---|---|\n| 1 | 2 |",
    "<fcell>not an OTSL marker",
    "",
    null,
  ]) {
    assert.equal(ocrResultMode("table", result), "rich");
  }
  assert.equal(ocrResultMode("ocr", "<fcel>Name"), "rich");
  assert.equal(ocrResultMode("chart", "<fcel>Name"), "rich");
});

test("validates max_pages against the live server range", () => {
  assert.equal(pageLimitValidationError("", 50), null);
  assert.equal(pageLimitValidationError("12", 50), null);
  assert.match(pageLimitValidationError("0", 50), /1 to 50/);
  assert.match(pageLimitValidationError("51", 50), /1 to 50/);
  assert.match(pageLimitValidationError("1.5", 50), /integer/);
});

test("unwraps both middleware and FastAPI error bodies into readable messages", () => {
  assert.match(extractErrorMessage({ code: "rate_limited" }, "fallback"),
    /^Too many requests.*\[rate_limited\]$/);
  assert.match(extractErrorMessage({ detail: { code: "corrupt_image" } }, "fallback"),
    /could not be decoded.*\[corrupt_image\]$/);
  assert.match(extractErrorMessage({ detail: { code: "max_pages_out_of_range", max: 50, value: 90 } }, "x"),
    /\(maximum 50, received 90\) \[max_pages_out_of_range\]$/);
  // Unknown codes stay visible verbatim; explicit messages win.
  assert.equal(extractErrorMessage({ detail: { code: "new_code" } }, "fallback"), "new_code");
  assert.equal(extractErrorMessage({ detail: { code: "queue_full", message: "Busy." } }, "f"), "Busy.");
});

test("slims embedded images without mutating response metadata", () => {
  const response = {
    pages: [{
      page: 1,
      width: 100,
      height: 200,
      images: ["base64"],
      image_names: ["figure.png"],
    }],
  };
  const slim = slimForDisplay(response);
  assert.equal(slim.pages[0].images, "[1 base64 image(s) omitted from this view]");
  assert.deepEqual(slim.pages[0].image_names, ["figure.png"]);
  assert.deepEqual(response.pages[0].images, ["base64"]);
});

test("parses current backend Prometheus metrics", () => {
  const metrics = [
    'ocr_requests_total{endpoint="parse",status="200"} 3',
    'ocr_requests_total{endpoint="parse",status="503"} 1',
    'ocr_request_duration_seconds_bucket{endpoint="parse",le="1"} 1',
    'ocr_request_duration_seconds_bucket{endpoint="parse",le="5"} 3',
    'ocr_request_duration_seconds_bucket{endpoint="parse",le="+Inf"} 4',
  ].join("\n");
  assert.deepEqual(parseEndpointMetrics(metrics), [{
    endpoint: "parse",
    requests: 4,
    errors: 1,
    p50: 3,
    p95: 5,
    p50LowerBound: false,
    p95LowerBound: true,
  }]);
});

test("rejects malformed parse payloads while preserving optional nulls and page errors", () => {
  const valid = { pages: [], processing_time_ms: 0, model_name: "fake", pipeline_version: "test", warnings: [] };
  assert.equal(validateParseResponse(valid), null);
  assert.equal(validateParseResponse({ ...valid, pages: [{ page: 1, error: "page_failed", blocks: null, markdown: null }] }), null);
  for (const malformed of [null, [], { ...valid, pages: null }, { ...valid, warnings: "bad" },
    { ...valid, pages: [{ page: "1" }] }, { ...valid, pages: [{ page: 1, blocks: [{ type: "text", bbox: "bad", content: "hello", order: null }] }] }]) {
    assert.equal(typeof validateParseResponse(malformed), "string");
  }
});

test("OCR validation rejects malformed coordinates without rejecting empty OCR text", () => {
  const valid = { result: "", task: "ocr", processing_time_ms: 0, model_name: "fake", warnings: [], boxes: null };
  assert.equal(validateOcrResponse(valid), null);
  assert.equal(validateOcrResponse({ ...valid, boxes: [[1, 2, 3, 4]] }), null);
  for (const malformed of [null, [], { ...valid, result: null }, { ...valid, boxes: [["1", 2]] }, { ...valid, warnings: [null] }, { ...valid, processing_time_ms: Infinity }]) {
    assert.equal(typeof validateOcrResponse(malformed), "string");
  }
});

test("counter resets and short chart series stay finite", () => {
  assert.equal(counterDelta(3, 10), 3);
  assert.equal(counterDelta(15, 10), 5);
  const pad = { left: 10, right: 10, top: 10, bottom: 10 };
  for (const series of [[], [{ requests: 4 }]]) {
    const scale = chartScales(series, 100, 100, pad);
    assert.ok(Number.isFinite(scale.x(0)));
    assert.ok(Number.isFinite(scale.y(4)));
    assert.ok(Number.isFinite(scale.min));
  }
});

test("saturated quantiles are lower bounds and missing histograms stay absent", () => {
  const rows = parseEndpointMetrics('ocr_requests_total{endpoint="ocr",status="400"} 1');
  assert.equal(rows[0].p95, null);
  assert.equal(rows[0].errors, 0);
  const saturated = parseEndpointMetrics([
    'ocr_request_duration_seconds_bucket{endpoint="parse",le="1"} 1',
    'ocr_request_duration_seconds_bucket{endpoint="parse",le="5"} 3',
    'ocr_request_duration_seconds_bucket{endpoint="parse",le="+Inf"} 10',
  ].join('\n'));
  assert.equal(saturated[0].p95LowerBound, true);
  assert.match(metricsTable(saturated), /≥5\.0000/);
});

test("error unwrapping tolerates unexpected payloads and honors message precedence", () => {
  for (const value of [null, [], "plain text", { detail: [] }, { detail: { message: {} } }]) {
    assert.equal(extractErrorMessage(value, "fallback"), "fallback");
  }
  assert.equal(extractErrorMessage({ detail: { message: "useful", code: "code" } }, "fallback"), "useful");
});

test("download names drop the last extension after sanitizing", () => {
  assert.equal(safeFilename("my report.v2.docx"), "my_report.v2.docx");
  assert.equal(downloadBaseName("report.pdf"), "report");
  assert.equal(downloadBaseName("relazione è.pdf"), "relazione_è");
  assert.equal(downloadBaseName("my report.v2.docx"), "my_report.v2");
  assert.equal(downloadBaseName("README"), "README");
  // Every script survives, so CJK uploads no longer all become "_".
  assert.equal(downloadBaseName("報告書.pdf"), "報告書");
  assert.equal(downloadBaseName("отчет 2024.xlsx"), "отчет_2024");
  // A bare extension is not a name: never produce ".png.ocr.md".
  assert.equal(downloadBaseName(".png"), "png");
  assert.equal(downloadBaseName("__.pdf"), "pdf");
  assert.equal(downloadBaseName(""), "document");
});

test("byte sizes scale past megabytes", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(5 * 1024 ** 3), "5.0 GB");
  assert.equal(formatBytes(5 * 1024 ** 4), "5.0 TB");
  assert.equal(formatBytes(NaN), "—");
});

test("max_pages accepts plain digits only, and flags unparsable input", () => {
  assert.equal(pageLimitValidationError("", 50), null);
  assert.equal(pageLimitValidationError("12", 50), null);
  for (const bad of ["1e1", "10.0", "0", "51", "-3"]) {
    assert.match(pageLimitValidationError(bad, 50), /from 1 to 50/, bad);
  }
  assert.match(pageLimitValidationError(null, 50), /from 1 to 50/);
});

test("uploads refuse empty files and SVG before reaching the server", () => {
  assert.match(uploadValidationError({ name: "a.png", type: "image/png", size: 0 }, 100), /empty/);
  assert.match(uploadValidationError({ name: "logo.svg", type: "image/svg+xml", size: 10 }, 100), /SVG/);
  assert.equal(uploadValidationError({ name: "a.png", type: "image/png", size: 10 }, 100), null);
});

test("chart pipe tables gain their separator row", () => {
  assert.equal(chartMarkdown("Year | Sales\n2021 | 120\n2022 | 140"),
    "Year | Sales\n--- | ---\n2021 | 120\n2022 | 140");
  const ready = "| a | b |\n|---|---|\n| 1 | 2 |";
  assert.equal(chartMarkdown(ready), ready);
  assert.equal(chartMarkdown("plain text\nmore"), "plain text\nmore");
});

test("status tiles distinguish degraded and unknown from down", () => {
  assert.match(statusTile("health", true), /metric-value up">UP</);
  assert.match(statusTile("health", "degraded"), /metric-value warn">DEGRADED</);
  assert.match(statusTile("vllm", "unknown"), /metric-value warn">UNKNOWN</);
  assert.match(statusTile("vllm", false, "unreachable"), /metric-value down">DOWN unreachable</);
});

test("flat chart series sit mid-height with whole-number ticks", () => {
  const pad = { top: 20, right: 10, bottom: 20, left: 40 };
  const flat = [{ t: "a", requests: 3 }, { t: "b", requests: 3 }];
  const { y } = chartScales(flat, 300, 200, pad);
  assert.equal(y(3), 100);
  const svg = chartSvg([{ t: "a", requests: 0 }, { t: "b", requests: 1 }], 300, 200, pad);
  assert.doesNotMatch(svg, />0\.5</);
});

test("autotuneTiles shows the settings in effect when there is no profile", () => {
  const html = autotuneTiles({
    action: "fallback", reason: "no GPU",
    effective: { vl_lanes: 2, page_parallelism: 1, layout_threads: null },
  });
  assert.match(html, /metric-label">vl lanes \(in effect\)</);
  assert.match(html, /metric-label">layout threads \(in effect\)<\/div><div class="metric-value">default</);
  const nested = autotuneTiles({ action: "calibrated", decisions: { list: [{ a: 1 }] } });
  assert.doesNotMatch(nested, /\[object Object\]/);
});

test("autotuneTiles renders every field as metric cards, never JSON", () => {
  const html = autotuneTiles({
    action: "calibrated", mode: "quick", objective: "balanced",
    warmup_seconds: 3.24, measured_candidates: 3,
    reason: "fallback kept defaults",
    decisions: { vl_lanes: 4, page_parallelism: 2, nested: { a: 1, b: "x" }, tags: ["u", "v"] },
  });
  assert.doesNotMatch(html, /json-block/);
  for (const label of ["action", "mode", "objective", "warmup (s)",
    "measured candidates", "reason", "vl lanes", "page parallelism"]) {
    const pattern = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    assert.match(html, new RegExp(`metric-label">${pattern}</`), label);
  }
  for (const value of ["calibrated", "quick", "balanced", "3.2", "fallback kept defaults"]) {
    assert.match(html, new RegExp(`>${value.replace(/[()]/g, c => `\\${c}`)}<`), value);
  }
  assert.match(html, /metric metric-wide/);
  assert.match(html, /metric-value metric-text/);
  assert.match(html, /a: 1 · b: x/);
  assert.match(html, /u, v/);
  // Sparse payloads still produce the mandatory trio.
  const minimal = autotuneTiles({ action: "disabled" });
  for (const label of ["action", "mode", "objective"]) {
    assert.match(minimal, new RegExp(`metric-label">${label}</`), label);
  }
  assert.doesNotMatch(minimal, /reason|wide/);
});

test("metricTiles entries accept wide and text options", () => {
  const html = metricTiles([["a", 1], ["b", "long text", { wide: true, text: true }]]);
  assert.match(html, /<div class="metric"><div class="metric-label">a</);
  assert.match(html, /<div class="metric metric-wide"><div class="metric-label">b</);
  assert.match(html, /<div class="metric-value metric-text">long text</);
  assert.match(html, /^<div class="metric-row">/);
  assert.match(metricTiles([["a", 1]], { fit: true }), /^<div class="metric-row metric-row-fit">/);
});

test("parseStorageMetrics reads lifetime storage counters", () => {
  const parsed = parseStorageMetrics([
    '# HELP ocr_storage_attempts_total x',
    'ocr_storage_attempts_total{status="archived"} 3',
    'ocr_storage_attempts_total{status="failed"} 1',
    'ocr_storage_inflight 2',
    'ocr_storage_bytes_total 2048',
    'ocr_requests_total{endpoint="parse",status="200"} 9',
  ].join("\n"));
  assert.equal(parsed.attempts.get("archived"), 3);
  assert.equal(parsed.attempts.get("failed"), 1);
  assert.equal(parsed.inflight, 2);
  assert.equal(parsed.bytes, 2048);
  const empty = parseStorageMetrics("");
  assert.equal(empty.attempts.size, 0);
  assert.equal(empty.inflight, null);
  assert.equal(empty.bytes, null);
});

test("markdown export and json_ocr shaping", () => {
  assert.equal(
    pagesMarkdown({ pages: [{ page: 1, markdown: "A" }, { page: 2, markdown: null }] }),
    "<!-- page 1 -->\n\nA\n\n<!-- page 2 -->\n\n",
  );
  const data = { pages: [], json_rag: { chunks: [] } };
  assert.deepEqual(withoutJsonRag(data), { pages: [] });
  assert.ok("json_rag" in data);
});

test("markdown export distinguishes failed pages from blank pages", () => {
  const markdown = pagesMarkdown({ pages: [
    { page: 1, markdown: "Kept text" },
    { page: 2, error: "page_timeout", markdown: "Incomplete text" },
    { page: 3, markdown: "" },
  ] });
  assert.match(markdown, /Kept text/);
  assert.match(markdown, /<!-- page 2 -->\n\n> OCR failed for page 2; content is unavailable\./);
  assert.doesNotMatch(markdown, /Incomplete text/);
  assert.ok(markdown.endsWith("<!-- page 3 -->\n\n"));
});


test("spotting discards invalid LOC groups without shifting labels", () => {
  const loc = (...values) => values.map(v => `<|LOC_${v}|>`).join("");
  const bad = loc(0, 0, 1500, 0, 1500, 1000, 0, 1000);
  const infinite = loc("9".repeat(400), 0, 1, 0, 1, 1, 0, 1);
  assert.deepEqual(spottingRegions(`bad${bad} huge${infinite} good${loc(1, 2, 3, 2, 3, 4, 1, 4)}`), [
    { text: "good", box: [1, 2, 3, 4] },
  ]);
});
