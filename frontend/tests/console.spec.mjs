import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir, readFile } from "node:fs/promises";

import { expect, test, openHealthyConsole, CONFIG, HEALTH, IMAGE, PARSE, OCR, OTSL_TABLE_RESULT, withApi } from "./fixtures.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.resolve(HERE, "../../calibration");
const PIXEL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

for (const viewport of [{ width: 1280, height: 800 }, { width: 1920, height: 1080 }]) {
  test(`desktop parameter references remain readable at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openHealthyConsole(page);
    const screenshots = path.resolve(HERE, "../../docs/verification/parameter-reference-2026-10-12");
    await mkdir(screenshots, { recursive: true });
    for (const tab of ["parse", "ocr", "rag", "storage", "monitor"]) {
      await page.locator(`#tab-${tab}`).click();
      const reference = page.locator(`#${tab}-reference`);
      await reference.locator("..").locator("summary").click();
      await expect(reference).toBeVisible();
      const layout = await reference.locator(".param-desc").evaluateAll(elements => ({
        pageFits: document.documentElement.scrollWidth <= window.innerWidth,
        cardsFit: elements.every(element => {
          const rect = element.getBoundingClientRect();
          return rect.left >= 0 && rect.right <= window.innerWidth
            && element.scrollWidth <= element.clientWidth + 1
            && element.scrollHeight <= element.clientHeight + 1;
        }),
      }));
      expect(layout).toEqual({ pageFits: true, cardsFit: true });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(screenshots, `${tab}-${viewport.width}.png`), fullPage: true });
    }
  });
}

test("hydrates controls and help from server configuration with nondefault limits", async ({ page }) => {
  await openHealthyConsole(page);
  await expect(page.locator("#parse-max-pages")).toHaveValue(String(CONFIG.maxPages));
  await expect(page.locator("#parse-max-pages")).toHaveAttribute("max", String(CONFIG.maxPages));
  await expect(page.locator("#parse-dpi")).toHaveValue("");
  await page.locator("#panel-parse details.reference").click();
  await expect(page.locator("#parse-reference")).toContainText("10.0 MB");
  await expect(page.locator("#parse-reference")).toContainText("1–12");

});

test("validates uploads and omits dpi for images", async ({ page }) => {
  await openHealthyConsole(page);

  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-file").setInputFiles(path.join(FIXTURES, "multipage.pdf"));
  await expect(page.locator("#ocr-run")).toBeDisabled();
  await expect(page.locator("#ocr-errors")).toContainText("accepts images only");
  await page.locator("#ocr-file").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  await expect(page.locator("#ocr-run")).toBeDisabled();
  await expect(page.locator("#ocr-errors")).toContainText("Unsupported file type text/plain");

  let multipartBody = "";
  await page.route("**/parse", async (route) => {
    multipartBody = route.request().postDataBuffer()?.toString("latin1") || "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        pages: [{
          page: 1,
          width: 640,
          height: 480,
          markdown: "# Parsed",
          blocks: [],
          images: [PIXEL_PNG],
          image_names: ["figure-1.png"],
          error: null,
        }],
        model_name: "PaddlePaddle/PaddleOCR-VL-1.6",
        pipeline_version: "v1.6",
        processing_time_ms: 10,
        warnings: [{
          code: "sample_warning",
          severity: "warning",
          message: "Sample warning.",
          target: { field: "page", index: 0 },
          details: { page: 1 },
        }],
      }),
    });
  });

  await page.locator("#tab-parse").click();
  await page.locator("#parse-file").setInputFiles(path.join(FIXTURES, "plain_text.png"));
  await expect(page.locator("#parse-request-json")).not.toContainText('"dpi"');
  await page.locator("#parse-max-pages").fill("13");
  await expect(page.locator("#parse-run")).toBeDisabled();
  await expect(page.locator("#parse-errors")).toContainText("1 to 12");
  await page.locator("#parse-max-pages").fill("12");
  await expect(page.locator("#parse-run")).toBeEnabled();
  await page.locator("#parse-images").check();
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .metric-row")).toContainText("processing time");
  expect(multipartBody).toContain('name="file"');
  expect(multipartBody).not.toContain('name="dpi"');
  await expect(page.locator("#parse-results .metric-row")).toContainText("plain_text.png");
  await expect(page.locator("#parse-results .metric-row")).toContainText("640 × 480 px");
  await expect(page.locator("#parse-results .metric-row")).toContainText("truncatedno");
  await expect(page.locator("#parse-results figcaption")).toHaveText("figure-1.png");
  await expect(page.locator("#parse-results .warning-details")).toHaveCount(0);
  await expect(page.locator("#parse-results")).not.toContainText("sample_warning");

});

test("long filenames do not widen Parse or OCR on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openHealthyConsole(page);
  const file = { name: "a_very_long_document_name_with_many_words_for_testing_ui_overflow_and_filename_visibility.png",
    mimeType: "image/png", buffer: Buffer.from("fixture") };
  for (const tab of ["parse", "ocr"]) {
    await page.locator(`#tab-${tab}`).click();
    await page.locator(`#${tab}-file`).setInputFiles(file);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  }
});

test("accepts Office documents for parsing only", async ({ page }) => {
  await openHealthyConsole(page);
  const docx = {
    name: "report.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("PKfixture"),
  };
  await expect(page.locator("#parse-file")).toHaveAttribute("accept", /\.docx/);
  await expect(page.locator("#ocr-file")).toHaveAttribute("accept", "image/*");

  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-file").setInputFiles(docx);
  await expect(page.locator("#ocr-run")).toBeDisabled();
  await expect(page.locator("#ocr-errors")).toContainText("PDF or Office documents");

  let multipartBody = "";
  await page.route("**/parse", async (route) => {
    multipartBody = route.request().postDataBuffer()?.toString("latin1") || "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...PARSE,
        warnings: [{
          code: "office_converted",
          severity: "info",
          message: "Office document was rendered to PDF by LibreOffice before OCR.",
          target: { field: "file" },
          details: { source_format: "docx" },
        }],
      }),
    });
  });
  await page.locator("#tab-parse").click();
  await page.locator("#parse-file").setInputFiles(docx);
  await page.locator("#parse-dpi").selectOption({ index: 1 });
  await expect(page.locator("#parse-request-json")).toContainText('"dpi"');
  await expect(page.locator("#parse-run")).toBeEnabled();
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .metric-row")).toContainText("processing time");
  expect(multipartBody).toContain('filename="report.docx"');
  expect(multipartBody).toContain('name="dpi"');
  await expect(page.locator("#parse-results")).not.toContainText("office_converted");
});

// Smallest valid PDF with a correct xref, so pdf.js logs nothing.
function tinyPdf(pages = 1) {
  const kids = Array.from({ length: pages }, (_, i) => `${i + 3} 0 R`).join(" ");
  const objects = ["<</Type/Catalog/Pages 2 0 R>>", `<</Type/Pages/Kids[${kids}]/Count ${pages}>>`,
    ...Array.from({ length: pages }, () => "<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>")];
  let pdf = "%PDF-1.4\n";
  const offsets = objects.map((body, i) => {
    const offset = pdf.length;
    pdf += `${i + 1} 0 obj\n${body}\nendobj\n`;
    return offset;
  });
  const xref = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n` +
    offsets.map(o => `${String(o).padStart(10, "0")} 00000 n \n`).join("") +
    `trailer\n<</Size ${objects.length + 1}/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, "latin1");
}

test("result viewer previews Office pages with block overlay and tabs", async ({ page }) => {
  await openHealthyConsole(page);
  let previewBody = "";
  await page.route("**/office/pdf", async (route) => {
    previewBody = route.request().postDataBuffer()?.toString("latin1") || "";
    await route.fulfill({ status: 200, contentType: "application/pdf", body: tinyPdf() });
  });
  await page.route("**/parse", route => route.fulfill({ json: {
    ...PARSE,
    pages: [{
      page: 1, width: 400, height: 200, markdown: "# Title\n\nBody text",
      blocks: [
        { type: "doc_title", bbox: [10, 10, 390, 40], content: "Title", order: 0 },
        { type: "text", bbox: [10, 60, 390, 120], content: "Body text", order: 1 },
      ],
    }],
  } }));

  await page.locator("#parse-file").setInputFiles({
    name: "report.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("PKfixture"),
  });
  await page.locator("#parse-run").click();
  const viewer = page.locator("#parse-results .pv");
  await expect(page.locator("#parse-results .metric-row")).toContainText("report.docx");
  await expect(viewer.locator(".pv-host canvas")).toHaveCount(1);
  expect(previewBody).toContain('filename="report.docx"');
  // A single page has nothing to page through: pager and Thumbnails stay hidden.
  await expect(viewer.locator(".pv-page")).toBeHidden();
  await expect(viewer.locator('[data-act="film"]')).toBeHidden();
  await expect(viewer.locator(".pv-rect")).toHaveCount(2);
  await expect(viewer.locator(".pv-overlay")).toHaveAttribute("viewBox", "0 0 400 200");
  await expect(viewer.locator(".result-text h3")).toHaveText("Title");

  // Arrow keys move between visible content tabs; only the selected one is tabbable.
  await viewer.getByRole("tab", { name: "rendered" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(viewer.getByRole("tab", { name: "markdown" })).toBeFocused();
  await expect(viewer.getByRole("tab", { name: "markdown" })).toHaveAttribute("tabindex", "0");
  await expect(viewer.getByRole("tab", { name: "rendered" })).toHaveAttribute("tabindex", "-1");
  await page.keyboard.press("End");
  await expect(viewer.getByRole("tab", { name: "blocks" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(viewer.getByRole("tab", { name: "rendered" })).toHaveAttribute("aria-selected", "true");

  // The Copy label reports the outcome, then resets.
  await viewer.getByRole("tab", { name: "markdown" }).click();
  const copy = viewer.locator('[data-act="copy"]');
  await copy.click();
  await expect(copy).not.toHaveText("Copy");
  await expect(copy).toHaveText("Copy");

  // Clicking a block opens the Blocks tab; legend chips filter both views.
  await viewer.locator('.pv-rect[data-type="text"]').click({ force: true });
  await expect(viewer.getByRole("tab", { name: "blocks" })).toHaveAttribute("aria-selected", "true");
  await expect(viewer.locator(".pv-block")).toHaveCount(2);
  await viewer.locator('.pv-lchip[data-type="text"]').click();
  await expect(viewer.locator('.pv-block[data-type="text"]')).toBeHidden();
  await expect(viewer.locator('.pv-rect.filtered')).toHaveCount(1);

  // One button shows every type after a partial filter, or hides them all.
  const toggleAll = viewer.locator('[data-act="legend-all"]');
  await expect(viewer.getByRole("heading", { name: "Block types" })).toBeVisible();
  await expect(toggleAll).toHaveText("Show all");
  await toggleAll.click();
  await expect(viewer.locator(".pv-rect.filtered")).toHaveCount(0);
  await expect(viewer.locator('.pv-lchip[aria-pressed="true"]')).toHaveCount(2);
  await expect(toggleAll).toHaveText("Hide all");
  await toggleAll.click();
  await expect(viewer.locator(".pv-rect.filtered")).toHaveCount(2);
  await expect(viewer.locator('.pv-lchip[aria-pressed="false"]')).toHaveCount(2);
  await toggleAll.click();


  // Zoom steps start from the Fit scale and the label tracks the real scale.
  const zoomLabel = viewer.locator('[data-act="zoom-100"]');
  const stageWidth = () => viewer.locator(".pv-stage").evaluate(node => node.getBoundingClientRect().width);
  const fitLabel = await zoomLabel.textContent();
  const fitWidth = await stageWidth();
  await viewer.locator('[data-act="zoom-out"]').click();
  await expect(zoomLabel).not.toHaveText(fitLabel);
  expect(await stageWidth()).toBeLessThan(fitWidth);
  await expect(viewer.locator('[data-act="fit"]')).toHaveAttribute("aria-pressed", "false");
  await zoomLabel.click();
  await expect(zoomLabel).toHaveText("100%");

  // An overflowing page pans with a mouse drag; a fitted one does not.
  const frame = viewer.locator(".pv-stage-wrap");
  await expect(frame).toHaveClass(/pannable/);
  const box = await frame.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - 120, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();
  expect(await frame.evaluate(node => node.scrollLeft)).toBeGreaterThan(100);
  await viewer.locator('[data-act="fit"]').click();
  await expect(frame).not.toHaveClass(/pannable/);
});

test("viewer refits after a hidden-tab completion and resolves markdown figures", async ({ page }) => {
  await openHealthyConsole(page);
  const figureRequests = [];
  page.on("request", request => { if (request.url().includes("/imgs/")) figureRequests.push(request.url()); });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse", async route => { await held; await route.fulfill({ json: {
    ...PARSE,
    usage: { tokenizer: "t", content_tokens: 12345, blocks: 2 },
    warnings: [{ code: "pages_truncated", message: "PDF truncated.", severity: "warning" }],
    pages: [{
      page: 1, width: 400, height: 200,
      markdown: '# T\n\n<div><img src="imgs/fig1.jpg" alt="Image" /></div>\n\n<img src="imgs/missing.jpg" alt="Image" />',
      images: [PIXEL_PNG], image_names: ["imgs/fig1.jpg"],
      blocks: [
        { type: "image", bbox: [0, 0, 10, 10], content: "", order: null },
        { type: "text", bbox: [0, 20, 10, 30], content: "b", order: 1 },
      ],
    }],
  } }); });
  await page.locator("#parse-file").setInputFiles({ name: "doc.pdf", mimeType: "application/pdf", buffer: tinyPdf(2) });
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .metric-row")).toContainText("elapsed");
  await expect(page.locator("#parse-results")).not.toContainText("PROCESSING");
  await page.locator("#tab-monitor").click();
  release();
  const viewer = page.locator("#parse-results .pv");
  await expect(viewer.locator(".pv-host canvas")).toHaveCount(1);
  await page.locator("#tab-parse").click();
  await expect.poll(() => viewer.locator(".pv-stage").evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThan(300);
  await expect(viewer.locator(".pv-page")).toHaveText("Page 1 / 2");
  await viewer.locator('[data-act="film"]').click();
  await expect(viewer.locator(".pv-film canvas")).toHaveCount(2);

  const metrics = page.locator("#parse-results .metric-row");
  await expect(metrics).toContainText("12,345");
  await expect(metrics).toContainText("truncatedpages");
  await expect(viewer.locator('[data-panel="rendered"] img')).toHaveAttribute("src", /^data:image\/png;base64,/);
  await expect(viewer.locator('[data-panel="rendered"]')).toContainText("[figure imgs/missing.jpg");
  expect(figureRequests).toEqual([]);
  await viewer.getByRole("tab", { name: "blocks" }).click();
  await expect(viewer.locator(".pv-b-type")).toHaveText(["text", "image"]);
});

test("renders rich OCR output through the sanitizer", async ({ page }) => {
  await openHealthyConsole(page);
  await page.route("**/ocr", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        result: "<table><tr><td>42</td></tr></table><script>window.__xss=true</script>",
        task: "table",
        boxes: null,
        model_name: "PaddlePaddle/PaddleOCR-VL-1.6",
        processing_time_ms: 5,
        warnings: [],
      }),
    });
  });

  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("table");
  await page.locator("#ocr-file").setInputFiles(path.join(FIXTURES, "simple_table.png"));
  await page.locator("#ocr-run").click();
  await expect(page.locator("#ocr-results table td")).toHaveText("42");
  await expect(page.locator("#ocr-results script")).toHaveCount(0);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();

});

test("rebuilds complete OTSL tables and keeps the markers in raw", async ({ page }, testInfo) => {
  const payload = { ...OCR, task: "table", result: OTSL_TABLE_RESULT };
  await page.route("**/ocr", route => route.fulfill({ json: payload }));
  await openHealthyConsole(page);
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("table");
  await page.locator("#ocr-file").setInputFiles(path.join(FIXTURES, "simple_table.png"));
  await page.locator("#ocr-run").click();
  const rendered = page.locator('#ocr-results [data-panel="rendered"]');
  await expect(rendered.locator("tr")).toHaveCount(4);
  await expect(rendered.locator("tr").first().locator("td")).toHaveText(["Name", "Value", "Unit"]);
  await expect(rendered.locator("tr").last().locator("td")).toHaveText(["Gamma", "3.14", "rad"]);
  await expect(rendered).not.toContainText("<fcel>");
  await page.locator('#ocr-results [data-tab="markdown"]').click();
  await expect(page.locator('#ocr-results [data-panel="markdown"] code')).toHaveText(OTSL_TABLE_RESULT);
  await page.screenshot({
    path: testInfo.outputPath("otsl-table.png"),
    fullPage: true,
  });
});

for (const [name, result] of [
  ["partial", "<fcel>Name"],
  ["script-containing", "<fcel>Name<script>window.__xss=true</script>"],
]) {
  test(`renders ${name} OTSL as escaped cells without executing markup`, async ({ page }) => {
    await page.route("**/ocr", route => route.fulfill({ json: { ...OCR, task: "table", result } }));
    await openHealthyConsole(page);
    await page.locator("#tab-ocr").click();
    await page.locator("#ocr-task").selectOption("table");
    await page.locator("#ocr-file").setInputFiles(IMAGE);
    await page.locator("#ocr-run").click();
    const rendered = page.locator('#ocr-results [data-panel="rendered"]');
    await expect(rendered.locator("td").first()).toHaveText(result.slice("<fcel>".length));
    await expect(rendered).toContainText("last row is incomplete");
    await expect(page.locator("#ocr-results script")).toHaveCount(0);
    expect(await page.evaluate(() => window.__xss)).toBeUndefined();
    await expect(page.locator('#ocr-results [data-panel="markdown"] code')).toHaveText(result);
  });
}

test("renders Markdown table output as cells", async ({ page }) => {
  const result = "| Name | Value |\n|---|---|\n| Alpha | 100 |";
  await page.route("**/ocr", route => route.fulfill({ json: { ...OCR, task: "table", result } }));
  await openHealthyConsole(page);
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("table");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-run").click();
  await expect(page.locator("#ocr-results table th")).toHaveText(["Name", "Value"]);
  await expect(page.locator("#ocr-results table td")).toHaveText(["Alpha", "100"]);
  await expect(page.locator('#ocr-results [data-panel="markdown"] code')).toHaveText(result);
  // Without spotting boxes there are no regions to overlay, list or filter.
  await expect(page.locator('#ocr-results [data-tab="blocks"]')).toBeHidden();
  await expect(page.locator('#ocr-results [data-act="blocks"]')).toBeHidden();
  await expect(page.locator("#ocr-results .pv-legend-box")).toBeHidden();
});

test("OCR result mirrors Parse with badges, an image viewer and spotting regions", async ({ page }) => {
  const loc = (...values) => values.map(v => `<|LOC_${v}|>`).join("");
  const result = `Hello${loc(100, 200, 300, 200, 300, 400, 100, 400)}\nWorld${loc(500, 500, 900, 500, 900, 600, 500, 600)}`;
  await page.route("**/ocr", route => route.fulfill({ json: {
    ...OCR, task: "spotting", result, boxes: [[100, 200, 300, 400], [500, 500, 900, 600]],
  } }));
  await openHealthyConsole(page, "#ocr");
  await page.locator("#ocr-task").selectOption("spotting");
  await page.locator("#ocr-file").setInputFiles(path.join(FIXTURES, "plain_text.png"));
  await page.locator("#ocr-run").click();

  const metrics = page.locator("#ocr-results .metric-row");
  await expect(metrics).toContainText("plain_text.png");
  await expect(metrics).toContainText("taskspotting");
  await expect(metrics).toContainText(/image size\d+ × \d+ px/);
  await expect(metrics).toContainText("regions2");
  await expect(metrics).toContainText("truncatedno");
  await expect(page.locator("#ocr-results .raw-details")).toHaveCount(0);

  const viewer = page.locator("#ocr-results .pv");
  await expect(viewer.locator(".pv-host img")).toHaveCount(1);
  await expect(viewer.locator(".pv-rect")).toHaveCount(2);
  await expect(viewer.locator(".pv-page")).toBeHidden();
  await expect(viewer.locator('[data-act="blocks"]')).toHaveText("regions");
  // Normalized 0–1000 boxes land in image pixels: the first starts at 10% of the width.
  const width = await viewer.locator(".pv-host img").evaluate(img => img.naturalWidth);
  expect(Number(await viewer.locator(".pv-rect").first().getAttribute("x"))).toBeCloseTo(width * 0.1);
  await expect(metrics).toContainText("output11 chars");
  await viewer.getByRole("tab", { name: "regions" }).click();
  await expect(viewer.locator(".pv-b-content")).toHaveText(["Hello", "World"]);
  await expect(viewer.locator(".pv-b-head").first()).toContainText("#1");
  await viewer.getByRole("tab", { name: "raw" }).click();
  await expect(viewer.locator('[data-panel="markdown"] code')).toHaveText(result);
});

test("keeps OTSL literal when its merge markers cannot be rebuilt", async ({ page }) => {
  const result = "<lcel>orphan<nl>";
  await page.route("**/ocr", route => route.fulfill({ json: { ...OCR, task: "table", result } }));
  await openHealthyConsole(page, "#ocr");
  await page.locator("#ocr-task").selectOption("table");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-run").click();
  const rendered = page.locator('#ocr-results [data-panel="rendered"]');
  await expect(rendered).toContainText("could not be rebuilt");
  await expect(rendered.locator("pre code")).toHaveText(result);
  await expect(rendered.locator("table")).toHaveCount(0);
});

test("shows truncation and incomplete-coordinate warnings without retrying", async ({ page }) => {
  const payload = {
    ...OCR,
    task: "spotting",
    result: "word<|LOC_1|>",
    boxes: [],
    warnings: [{
      code: "generation_truncated",
      severity: "warning",
      message: "Generation stopped at a token limit; result may be incomplete.",
      target: { field: "result" },
      details: { finish_reason: "length" },
    }, {
      code: "spotting_incomplete_coordinates",
      severity: "warning",
      message: "Some location tokens could not form a complete quadrilateral.",
      details: { incomplete_tokens: 1 },
    }],
  };
  let requests = 0;
  await page.route("**/ocr", route => {
    requests++;
    return route.fulfill({ json: payload });
  });
  await openHealthyConsole(page);
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("spotting");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-run").click();
  const output = page.locator('#ocr-results [data-panel="rendered"] pre.codeblock code');
  await expect(output).toBeVisible();
  expect(await output.textContent()).toBe("word");
  await expect(page.locator('#ocr-results [data-panel="markdown"] code')).toHaveText(payload.result);
  const warning = page.locator("#ocr-results > .alert").filter({ hasText: "[generation_truncated]" });
  await expect(warning).toContainText("result may be incomplete");
  await warning.locator("summary").click();
  await expect(warning.locator("code")).toBeVisible();
  expect(JSON.parse(await warning.locator("code").textContent())).toEqual({
    target: { field: "result" }, details: { finish_reason: "length" },
  });
  await expect(page.locator("#ocr-results")).toContainText("[spotting_incomplete_coordinates]");
  await expect(page.locator("#ocr-results .metric-row")).toContainText("truncatedyes");
  await expect(page.locator("#ocr-run")).toBeEnabled();
  expect(requests).toBe(1);
});

for (const endpoint of ["parse", "ocr"]) {
  test(`${endpoint} rejects malformed responses and allows a later successful submit`, async ({ page }) => {
    await openHealthyConsole(page);
    await page.locator(`#tab-${endpoint}`).click();
    await page.locator(`#${endpoint}-file`).setInputFiles(IMAGE);
    let payload;
    await page.route(`**/${endpoint}`, route => route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) }));
    const malformed = endpoint === "parse"
      ? [null, [], { ...PARSE, pages: null }, { ...PARSE, pages: [{ page: '<img src=x onerror="window.__xss=true">' }] }, { ...PARSE, warnings: "invalid" }, { ...PARSE, usage: { content_tokens: '<img src=x onerror="window.__xss=true">' } }]
      : [null, [], { ...OCR, boxes: [["bad", 1]] }, { ...OCR, warnings: [null] }];
    for (payload of malformed) {
      await page.locator(`#${endpoint}-run`).click();
      await expect(page.locator(`#${endpoint}-errors`)).toContainText("Unexpected");
      await expect(page.locator(`#${endpoint}-run`)).toBeEnabled();
    }
    payload = endpoint === "parse" ? PARSE : OCR;
    await page.locator(`#${endpoint}-run`).click();
    await expect(page.locator(`#${endpoint}-results`)).toContainText("processing time");
    expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  });

  for (const failure of ["sanitizer", "download"]) {
    test(`${endpoint} restores its submit button after ${failure} throws`, async ({ page }) => {
      await openHealthyConsole(page);
      await page.route(`**/${endpoint}`, route => route.fulfill({ json: endpoint === "parse" ? { ...PARSE, pages: [{ page: 1, markdown: "text" }] } : OCR }));
      await page.locator(`#tab-${endpoint}`).click();
      await page.locator(`#${endpoint}-file`).setInputFiles(IMAGE);
      if (failure === "download") {
        await page.locator(`#${endpoint}-download`).check();
        await page.evaluate(() => { URL.createObjectURL = () => { throw new Error("Download unavailable"); }; });
      } else {
        await page.evaluate(async () => {
          const source = await (await fetch("/assets/math.js")).text();
          const url = source.match(/import DOMPurify from "([^"]+)"/)[1];
          const { default: sanitizer } = await import(url);
          sanitizer.sanitize = () => { throw new Error("Sanitizer unavailable"); };
        });
      }
      await page.locator(`#${endpoint}-run`).click();
      await expect(page.locator(`#${endpoint}-errors`)).toContainText("unavailable");
      await expect(page.locator(`#${endpoint}-run`)).toBeEnabled();
      if (endpoint === "parse" && failure === "download") {
        await expect(page.locator("#parse-results .pv")).toBeVisible();
        await expect(page.locator("#parse-results")).toContainText("text");
        await expect(page.locator("#parse-errors")).toContainText("OCR results remain available");
      }
    });
  }
}

test("completion respects a cleared file and degraded service state", async ({ page }) => {
  await openHealthyConsole(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse", async route => { await held; await route.fulfill({ json: PARSE }); });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await page.locator("#parse-file").setInputFiles([]);
  await page.route("**/health", route => route.fulfill({ json: { status: "degraded", vllm: "unreachable", model: "fake" } }));
  await page.locator("#conn-retest").click();
  await expect(page.locator("#conn-health-status")).toHaveText("DEGRADED");
  release();
  await expect(page.locator("#parse-results")).toContainText("processing time");
  await expect(page.locator("#parse-run")).toBeDisabled();
});

test("rich Markdown rejects executable URLs, event attributes, clobbering and deep templates", async ({ page }) => {
  await openHealthyConsole(page);
  const attack = [
    '<script>window.__xss=true</script>',
    '<img src="data:image/png;base64,' + PIXEL_PNG + '" onerror="window.__xss=true" alt="&quot; onload=&quot;window.__xss=true">',
    '[click](javascript:window.__xss=true)',
    '<a href="javascript:window.__xss=true">link</a>',
    '<form id="DOMPurify"><input name="sanitize"></form>',
    '<template>'.repeat(60) + '<img src=x onerror="window.__xss=true">' + '</template>'.repeat(60),
    '<table><tr><td>safe output</td></tr></table>',
  ].join('\n');
  await page.route("**/ocr", route => route.fulfill({ json: { ...OCR, result: attack } }));
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-run").click();
  await expect(page.locator("#ocr-results")).toContainText("safe output");
  await expect(page.locator('#ocr-results script, #ocr-results form, #ocr-results input, #ocr-results template, #ocr-results [onerror], #ocr-results [onload], #ocr-results [href^="javascript:"]')).toHaveCount(0);
  // Recognized HTML is content: it shows literally instead of vanishing.
  await expect(page.locator("#ocr-results .result-text")).toContainText("<script>window.__xss=true</script>");
  await expect(page.locator('#ocr-results a[href]:not([target="_blank"])')).toHaveCount(0);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
});

test("formula output with trailing text keeps recognized-output rules", async ({ page }) => {
  await openHealthyConsole(page);
  // A formula followed by more text falls back to Markdown rendering. It is
  // still model output of one image: raw HTML shows literally and a remote
  // image never loads (no outbound request driven by a crafted picture).
  const result = '$x^2$ and <img src="http://example.invalid/pixel.png" alt="pixel"> <script>window.__xss=true</script>';
  const remote = [];
  page.on("request", request => { if (request.url().includes("example.invalid")) remote.push(request.url()); });
  await page.route("**/ocr", route => route.fulfill({ json: { ...OCR, task: "formula", result } }));
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("formula");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-run").click();
  const preview = page.locator("#ocr-results .formula-preview");
  await expect(preview).toContainText("<script>window.__xss=true</script>");
  await expect(preview.locator(".katex, math").first()).toBeAttached();
  await expect(page.locator('#ocr-results img[src^="http"]')).toHaveCount(0);
  expect(remote).toEqual([]);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
});

test("monitor baselines survive independent counter resets and detect process restarts", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page);
  let parse = 10, ocr = 10, rejected = 6, started = 100;
  await page.route("**/metrics", route => route.fulfill({
    contentType: "text/plain",
    body: `process_start_time_seconds ${started}\nocr_requests_total{endpoint="parse",status="200"} ${parse}\nocr_requests_total{endpoint="ocr",status="200"} ${ocr}\nocr_requests_rejected_total{reason="queue"} ${rejected}\n`,
  }));
  await page.locator("#tab-monitor").click();
  const total = page.locator("#mon-stats .metric").filter({ hasText: "total_requests" }).locator(".metric-value");
  const rejectedTile = page.locator("#mon-stats .metric").filter({ hasText: "rejected_requests" }).locator(".metric-value");
  await expect(total).toHaveText("20");
  await page.locator("#mon-reset").click();
  await expect(total).toHaveText("0");
  parse = 3; ocr = 15; rejected = 2;
  await page.clock.fastForward(5000);
  await expect(total).toHaveText("8");
  await expect(rejectedTile).toHaveText("2");
  parse = 11; ocr = 17; rejected = 7;
  await page.clock.fastForward(5000);
  await expect(total).toHaveText("18");
  await expect(rejectedTile).toHaveText("7");
  started = 200; parse = 20; ocr = 25;
  await page.clock.fastForward(5000);
  await expect(total).toHaveText("45");
});

test("monitor reset requires a fresh snapshot and leaving the tab cancels stale work", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/Failed to load resource: the server responded with a status of 503/);
  await page.clock.install();
  await openHealthyConsole(page);
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await page.route("**/metrics", route => route.fulfill({ status: 503, body: "unavailable" }));
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-table")).toContainText("Metrics unavailable");
  await expect(page.locator("#mon-reset")).toBeDisabled();
  await page.unroute("**/metrics");
  let aborted = false;
  page.on("requestfailed", request => { if (request.url().endsWith("/metrics")) aborted = true; });
  await page.route("**/metrics", async () => {});
  await page.clock.fastForward(5000);
  await page.locator("#tab-parse").click();
  await expect.poll(() => aborted).toBe(true);
  await page.unroute("**/metrics");
  await page.route("**/metrics", route => route.fulfill({ body: 'ocr_requests_total{endpoint="parse",status="200"} 9' }));
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await expect(page.locator("#mon-table")).toContainText("parse");
});

test("monitor shows unexpected refresh errors and resumes polling", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page);
  await withApi(page, api => {
    const original = api.stats;
    api.stats = () => { api.stats = original; throw new Error("simulated unexpected failure"); };
  });
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-table")).toContainText("Monitoring refresh failed");
  await expect(page.locator("#mon-reset")).toBeDisabled();
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await expect(page.locator("#mon-stats")).toContainText("active requests");
});

test("monitor chart crosshair and tooltip follow the pointer", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page);
  await page.locator("#tab-monitor").click();
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-chart-plot svg")).toHaveCount(1);
  await page.locator("#mon-chart").scrollIntoViewIfNeeded();
  const box = await page.locator("#mon-chart-plot svg").boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  const cross = page.locator("#mon-chart-cross");
  await expect(cross).toBeVisible();
  expect(await cross.evaluate(el => getComputedStyle(el).position)).toBe("absolute");
  expect((await cross.boundingBox()).width).toBeGreaterThan(0);
  const tip = page.locator("#mon-chart-tip");
  await expect(tip).toBeVisible();
  expect(await tip.evaluate(el => getComputedStyle(el).position)).toBe("absolute");
  await expect(tip).toContainText("requests");
});

test("monitor renders autotune fields and decisions as cards without JSON", async ({ page }) => {
  await page.route("**/health", route => route.fulfill({ json: {
    ...HEALTH,
    autotune: {
      action: "calibrated", mode: "quick", objective: "balanced",
      decisions: { vl_lanes: 2, page_parallelism: 4, layout_threads: 3 },
    },
  } }));
  await openHealthyConsole(page);
  await page.locator("#tab-monitor").click();
  const panel = page.locator("#mon-autotune");
  await expect(panel.locator(".metric").filter({ hasText: "action" })).toContainText("calibrated");
  await expect(panel).toContainText("vl lanes");
  await expect(panel).toContainText("page parallelism");
  await expect(panel).toContainText("layout threads");
  await expect(panel.locator(".json-block")).toHaveCount(0);
});

test("monitor storage section shows health status and lifetime counters", async ({ page }) => {
  await page.route("**/metrics", route => route.fulfill({
    contentType: "text/plain",
    body: 'process_start_time_seconds 100\n' +
      'ocr_requests_total{endpoint="parse",status="200"} 10\n' +
      'ocr_storage_attempts_total{status="archived"} 3\n' +
      'ocr_storage_inflight 0\nocr_storage_bytes_total 2048\n',
  }));
  await openHealthyConsole(page);
  await page.locator("#tab-monitor").click();
  const panel = page.locator("#mon-storage");
  await expect(panel.locator(".metric").filter({ hasText: "status" })).toContainText("unknown");
  await expect(panel.locator(".metric").filter({ hasText: "attempts archived" })).toContainText("3");
  await expect(panel.locator(".metric").filter({ hasText: "in flight" })).toContainText("0");
  await expect(panel.locator(".metric").filter({ hasText: "bytes archived" })).toContainText("2.0 KB");
});

test("monitor refresh caption reports the last good refresh and failures", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/Failed to load resource: the server responded with a status of 503/);
  await page.clock.install();
  await openHealthyConsole(page);
  await page.locator("#tab-monitor").click();
  const caption = page.locator("#mon-updated");
  await expect(caption).toContainText("last refresh");
  await page.route("**/metrics", route => route.fulfill({ status: 503, body: "unavailable" }));
  await page.clock.fastForward(5000);
  await expect(caption).toContainText("refresh failed");
  await expect(caption).toContainText("showing data from");
  await expect(caption).toHaveClass(/down/);
});

test("monitor table keeps zero-delta endpoints after Reset", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page);
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-table")).toContainText("parse");
  await page.locator("#mon-reset").click();
  // The delta drops to 0 but the row (and its lifetime latencies) stays listed.
  await expect(page.locator("#mon-table tr").filter({ hasText: "parse" }).locator("td").nth(1)).toHaveText("0");
});

test("repeated health retests share the current probe and hidden file inputs leave the tab order", async ({ page }) => {
  await openHealthyConsole(page);
  for (const endpoint of ["parse", "ocr"]) {
    await expect(page.locator(`#${endpoint}-file`)).toHaveAttribute("tabindex", "-1");
  }
  await withApi(page, api => {
    window.__healthCalls = 0;
    api.health = () => {
      window.__healthCalls++;
      return new Promise(resolve => { window.__resolveHealth = resolve; });
    };
  });
  await page.locator("#conn-retest").click();
  await page.locator("#conn-retest").click();
  expect(await page.evaluate(() => window.__healthCalls)).toBe(1);
  await page.evaluate(() => window.__resolveHealth({ ok: true, data: { status: "healthy", vllm: "reachable", model: "fake" }, elapsed: 0.1 }));
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
});

test("invalid nested health metadata locks submissions and a valid probe recovers", async ({ page }) => {
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await expect(page.locator("#parse-run")).toBeEnabled();
  let payload;
  await page.route("**/health", route => route.fulfill({ json: payload }));
  for (payload of [
    { status: { toString: null }, vllm: "reachable", model: "fake" },
    { status: "healthy", vllm: { toString: null }, model: "fake" },
    { status: "healthy", vllm: "reachable", model: { toString: null } },
    { status: 1, vllm: "reachable", model: "fake" },
    null,
    [],
  ]) {
    await page.locator("#conn-retest").click();
    await expect(page.locator("#conn-health-status")).toHaveText("UNREACHABLE");
    await expect(page.locator("#conn-health-detail")).toContainText("Unexpected health response");
    await expect(page.locator("#parse-controls")).toHaveAttribute("inert", "");
    await expect(page.locator("#parse-run")).toBeDisabled();
  }
  payload = { status: "healthy", vllm: "reachable", model: "fake" };
  await page.locator("#conn-retest").click();
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
  await expect(page.locator("#parse-run")).toBeEnabled();
});

test("unexpected health rejection closes the service gate and polling recovers", async ({ page }) => {
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await withApi(page, api => {
    const original = api.health;
    api.health = async () => { api.health = original; throw new Error("Health probe failed"); };
  });
  await page.locator("#conn-retest").click();
  await expect(page.locator("#conn-health-detail")).toHaveText("Health probe failed");
  await expect(page.locator("#parse-controls")).toHaveAttribute("inert", "");
  await expect(page.locator("#parse-run")).toBeDisabled();
  await page.locator("#conn-retest").click();
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
  await expect(page.locator("#parse-run")).toBeEnabled();
});

async function parseWithDownloads(page, response) {
  let multipartBody = "";
  await page.route("**/parse", async (route) => {
    multipartBody = route.request().postDataBuffer()?.toString("latin1") || "";
    await route.fulfill({ json: response });
  });
  await page.locator("#tab-parse").click();
  await page.locator("#parse-file").setInputFiles({
    name: "report.png", mimeType: "image/png", buffer: Buffer.from(PIXEL_PNG, "base64"),
  });
  await page.locator("#parse-out-rag").check();
  await expect(page.locator("#parse-request-json")).toContainText('"json_rag": true');
  await page.locator("#parse-download").check();
  const downloads = [];
  page.on("download", (download) => downloads.push(download));
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .metric-row")).toContainText("processing time");
  return { downloads, body: () => multipartBody };
}

async function downloadedFiles(downloads) {
  return Object.fromEntries(await Promise.all(downloads.map(async (download) =>
    [download.suggestedFilename(), await readFile(await download.path(), "utf8")])));
}

const PARSED_PAGE = { page: 1, width: 10, height: 10, markdown: "# Parsed", blocks: [], error: null };

test("parse downloads markdown, json_ocr and json_rag named without the extension", async ({ page }) => {
  await openHealthyConsole(page);
  await expect(page.locator("label:has(#parse-out-json)")).toHaveText("json_ocr");
  const rag = { chunks: [{ id: 1, text: "Parsed", document: "report.png", page: 1 }], metadata: { n_chunks: 1 } };
  const { downloads, body } = await parseWithDownloads(page, { ...PARSE, pages: [PARSED_PAGE], json_rag: rag });
  await expect.poll(() => downloads.length).toBe(3);
  expect(body()).toMatch(/name="json_rag"\r\n\r\ntrue/);
  const files = await downloadedFiles(downloads);
  expect(Object.keys(files).sort()).toEqual(["report.json_ocr.json", "report.json_rag.json", "report.md"]);
  expect(files["report.md"]).toBe("<!-- page 1 -->\n\n# Parsed");
  expect(JSON.parse(files["report.json_ocr.json"])).not.toHaveProperty("json_rag");
  expect(JSON.parse(files["report.json_ocr.json"]).pages[0].markdown).toBe("# Parsed");
  expect(JSON.parse(files["report.json_rag.json"])).toEqual(rag);
});

test("a failed json_rag export downloads nothing for it", async ({ page }) => {
  await openHealthyConsole(page);
  const failed = {
    ...PARSE,
    pages: [PARSED_PAGE],
    json_rag: null,
    warnings: [{ code: "json_rag_failed", severity: "warning", message: "The json_rag export could not be built; OCR results are unaffected.", target: { field: "json_rag" }, details: null }],
  };
  const { downloads } = await parseWithDownloads(page, failed);
  await expect(page.locator("#parse-results")).not.toContainText("json_rag_failed");
  await expect.poll(() => downloads.length).toBe(2);
  expect(downloads.map((download) => download.suggestedFilename()).sort()).toEqual(["report.json_ocr.json", "report.md"]);
});

const SNAP = { state: "ocr", pages_total: 12, pages_done: 4, pages_failed: 0, pages_unprocessed: 0,
  block: 3, blocks_total: 3, block_pages: 4, dpi: 250, elapsed_s: 5, eta_s: 9 };

test("parse shows live progress badges", async ({ page }) => {
  await openHealthyConsole(page);
  let release, sentId;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse/progress/*", route => route.fulfill({ json: SNAP }));
  await page.route("**/parse", async route => {
    sentId = /name="progress_id"\r\n\r\n([0-9a-f-]{36})/.exec(route.request().postData() ?? "")?.[1];
    await held; await route.fulfill({ json: PARSE });
  });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results")).toContainText("3/3 (4 pages)");
  await expect(page.locator("#parse-status")).toContainText("block 2 of 3 done, 4 of 12 pages");
  release();
  await expect(page.locator("#parse-results")).toContainText("processing time");
  expect(sentId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
});

test("progress 404 falls back to dashes and elapsed", async ({ page }) => {
  await openHealthyConsole(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse/progress/*", route => route.fulfill({ status: 404, json: { code: "progress_not_found" } }));
  await page.route("**/parse", async route => { await held; await route.fulfill({ json: PARSE }); });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .metric-row")).toContainText("elapsed");
  await expect(page.locator("#parse-results")).toContainText("—");
  release();
  await expect(page.locator("#parse-results")).toContainText("processing time");
});

test("late progress poll does not replace results", async ({ page }) => {
  await openHealthyConsole(page);
  let releaseProgress, polled, lateSent;
  const gate = new Promise(resolve => { releaseProgress = resolve; });
  const seen = new Promise(resolve => { polled = resolve; });
  const sent = new Promise(resolve => { lateSent = resolve; });
  await page.route("**/parse/progress/*", async route => {
    polled(); await gate;
    await route.fulfill({ json: SNAP }).catch(() => {});
    lateSent();
  });
  await page.route("**/parse", async route => { await seen; await route.fulfill({ json: PARSE }); });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-status")).toContainText("Parse complete");
  releaseProgress();
  await sent;
  // Let the page process the late reply before asserting the guard held.
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 100)));
  await expect(page.locator("#parse-status")).toContainText("Parse complete");
  await expect(page.locator("#parse-results")).toContainText("processing time");
});

for (const [status, jobId, text, down] of [
  ["stored", "ab12cd34", "storagestored · ab12cd34", false],
  ["stored", "", "storagestored", false],
  ["failed", "", "storagefailed", true],
  ["disabled", "", "storageoff", false],
  [null, "", "storage—", false],
]) {
  test(`parse storage tile: ${status ?? "no header"}${jobId ? " with job id" : ""}`, async ({ page }) => {
    await openHealthyConsole(page);
    const headers = {};
    if (status) headers["X-OCR-Storage-Status"] = status;
    if (jobId) headers["X-OCR-Job-Id"] = jobId;
    await page.route("**/parse", route => route.fulfill({ json: PARSE, headers }));
    await page.locator("#parse-file").setInputFiles(IMAGE);
    await page.locator("#parse-run").click();
    const tile = page.locator("#parse-results .metric").filter({ hasText: "storage" });
    await expect(tile).toHaveText(text);
    await expect(tile.locator(".metric-value")).toHaveClass(down ? /down/ : /^(?!.*down).*$/);
  });
}

test("a single failed progress poll keeps the badges", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/ERR_FAILED/);
  await openHealthyConsole(page);
  let release, calls = 0;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse/progress/*", route => {
    calls += 1;
    return calls === 2 ? route.abort() : route.fulfill({ json: SNAP });
  });
  await page.route("**/parse", async route => { await held; await route.fulfill({ json: PARSE }); });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results")).toContainText("3/3 (4 pages)");
  // Watch every render through the failed tick (poll 2) until poll 3 succeeds.
  const seenDashes = await page.evaluate(() => new Promise(resolve => {
    let dashed = false; const start = Date.now();
    const timer = setInterval(() => {
      const text = document.getElementById("parse-results").textContent;
      if (!text.includes("3/3 (4 pages)")) dashed = true;
      if (Date.now() - start > 2600) { clearInterval(timer); resolve(dashed); }
    }, 50);
  }));
  expect(seenDashes).toBe(false);
  expect(calls).toBeGreaterThanOrEqual(3);
  release();
  await expect(page.locator("#parse-results")).toContainText("processing time");
});

test("three failed progress polls show dashes while elapsed keeps counting", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/ERR_FAILED/);
  await openHealthyConsole(page);
  let release, calls = 0;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse/progress/*", route => (++calls === 1 ? route.fulfill({ json: SNAP }) : route.abort()));
  await page.route("**/parse", async route => { await held; await route.fulfill({ json: PARSE }); });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results")).toContainText("3/3 (4 pages)");
  await expect(page.locator("#parse-results")).not.toContainText("3/3 (4 pages)", { timeout: 8000 });
  await expect(page.locator("#parse-results .metric-row")).toContainText("elapsed");
  release();
  await expect(page.locator("#parse-results")).toContainText("processing time");
});

test("progress polls never overlap", async ({ page }) => {
  await openHealthyConsole(page);
  let release, inflight = 0, maxInflight = 0;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/parse/progress/*", async route => {
    inflight += 1; maxInflight = Math.max(maxInflight, inflight);
    await new Promise(resolve => setTimeout(resolve, 1800));
    inflight -= 1;
    await route.fulfill({ json: SNAP }).catch(() => {});
  });
  await page.route("**/parse", async route => { await held; await route.fulfill({ json: PARSE }); });
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await page.waitForTimeout(4500);
  expect(maxInflight).toBe(1);
  release();
  await expect(page.locator("#parse-results")).toContainText("processing time");
});
