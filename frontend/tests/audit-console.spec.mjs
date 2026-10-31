// Regressions from the Parse/OCR/Storage/Monitor audit.
import { expect, test, openHealthyConsole, HEALTH, IMAGE, PARSE, OCR } from "./fixtures.mjs";

const PAGE = { page: 1, width: 1, height: 1, markdown: "# Kept result", blocks: [], error: null };

for (const selection of ["file", "options"]) {
  test(`parse marks results stale when ${selection} changes during a run`, async ({ page }) => {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route("**/parse", async route => {
      await held;
      await route.fulfill({ json: { ...PARSE, pages: [PAGE] } });
    });
    await openHealthyConsole(page);
    await page.locator("#parse-file").setInputFiles(IMAGE);
    await page.locator("#parse-run").click();
    if (selection === "file") {
      await page.locator("#parse-file").setInputFiles({ ...IMAGE, name: "other.png" });
    } else await page.locator("#parse-images").uncheck();
    release();
    await expect(page.locator("#parse-results .stale-note")).toContainText("previous run (sample.png)");
    await expect(page.locator("#parse-results")).toContainText("Kept result");
  });
}

test("parse detects a replacement file even when the name is unchanged", async ({ page }) => {
  await page.route("**/parse", route => route.fulfill({ json: { ...PARSE, pages: [PAGE] } }));
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .pv")).toBeVisible();
  await page.locator("#parse-file").setInputFiles({ ...IMAGE, buffer: Buffer.concat([IMAGE.buffer, Buffer.from("changed")]) });
  await expect(page.locator("#parse-results .stale-note")).toContainText("previous run (sample.png)");
});

for (const truncated of [false, true]) {
  test(`multi-frame images show results without an incorrect preview (truncated=${truncated})`, async ({ page }) => {
    const pages = truncated ? [PAGE] : [PAGE, { ...PAGE, page: 2, markdown: "# Second frame" }];
    const warnings = truncated ? [{ code: "pages_truncated", message: "Only one frame parsed",
      details: { max_pages: 1, total_pages: 2 } }] : [];
    await page.route("**/parse", route => route.fulfill({ json: { ...PARSE, pages, warnings } }));
    await openHealthyConsole(page);
    // Two 1x1 GIF frames with different palette indices.
    const gif = Buffer.from("47494638396101000100800000ffffff0000002c00000000010001000002024401002c00000000010001000002024c01003b", "hex");
    await page.locator("#parse-file").setInputFiles({ name: "frames.gif", mimeType: "image/gif", buffer: gif });
    await page.locator("#parse-run").click();
    const viewer = page.locator("#parse-results .pv");
    await expect(viewer.locator(".pv-host")).toContainText("Multi-frame image previews are unavailable");
    await expect(viewer.locator(".pv-host img, .pv-overlay rect")).toHaveCount(0);
    await expect(viewer.locator('[data-panel="rendered"]')).toContainText("Kept result");
    await viewer.locator('[data-act="retry-preview"]').click();
    await expect(viewer.locator(".pv-host")).toContainText("Multi-frame image previews are unavailable");
    if (!truncated) {
      await viewer.locator('[data-act="next"]').click();
      await expect(viewer.locator('[data-panel="rendered"]')).toContainText("Second frame");
      await expect(viewer.locator(".pv-host img, .pv-overlay rect")).toHaveCount(0);
    }
  });
}

test("a running parse can be cancelled and focus stays on a control", async ({ page }) => {
  let release;
  await page.route("**/parse", route => new Promise(resolve => { release = () => resolve(route.abort()); }));
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#parse-cancel")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#parse-errors")).toContainText("Parse cancelled");
  await expect(page.locator("#parse-cancel")).toBeHidden();
  await expect(page.locator("#parse-run")).toBeFocused();
  release?.();
});

test("results stay usable while the backend is degraded", async ({ page }) => {
  let healthy = true;
  await page.route("**/health", route => route.fulfill({ json: healthy ? HEALTH : { ...HEALTH, status: "degraded", vllm: "unreachable" } }));
  await page.route("**/parse", route => route.fulfill({ json: { ...PARSE, pages: [PAGE] } }));
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results")).toContainText("Kept result");
  healthy = false;
  await page.locator("#conn-retest").click();
  await expect(page.locator("#parse-controls")).toHaveAttribute("inert", "");
  await page.locator('#parse-results [data-tab="markdown"]').click();
  await expect(page.locator('#parse-results [data-panel="markdown"] code')).toHaveText("# Kept result");
});

test("changing options after a run marks its results as previous", async ({ page }) => {
  await page.route("**/ocr", route => route.fulfill({ json: OCR }));
  await openHealthyConsole(page, "#ocr");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-run").click();
  await expect(page.locator("#ocr-results")).toContainText("sample text");
  await page.locator("#ocr-task").selectOption("table");
  await expect(page.locator("#ocr-results .stale-note")).toContainText("previous run (sample.png, ocr)");
});

test("a dropped file becomes the upload instead of navigating away", async ({ page }) => {
  await openHealthyConsole(page, "#ocr");
  const transfer = await page.evaluateHandle(png => {
    const data = new DataTransfer();
    const bytes = Uint8Array.from(atob(png), c => c.charCodeAt(0));
    data.items.add(new File([bytes], "dropped.png", { type: "image/png" }));
    return data;
  }, IMAGE.buffer.toString("base64"));
  await page.dispatchEvent("#ocr-controls", "drop", { dataTransfer: transfer });
  await expect(page.locator("#ocr-file-name")).toContainText("dropped.png");
  await expect(page).toHaveURL(/#ocr$/);
});

test.describe("server errors", () => {
  test.use({ expectedConsoleErrors: [/503/] });
  test("error codes read as sentences and survive option changes", async ({ page }) => {
  await page.route("**/parse", route => route.fulfill({ status: 503, json: { detail: { code: "queue_full" } } }));
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-errors")).toContainText("busy with other documents");
  await page.locator("#parse-images").uncheck();
  await expect(page.locator("#parse-errors")).toContainText("queue_full");
  });
});

test("monitor reset keeps keyboard focus across refreshes", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page, "#monitor");
  const reset = page.locator("#mon-reset");
  await expect(reset).toBeEnabled();
  await reset.focus();
  const updated = page.locator("#mon-updated");
  for (let tick = 0; tick < 2; tick++) {
    const before = await updated.textContent();
    await page.clock.runFor(5000);
    await expect(updated).not.toHaveText(before);
    await expect(reset).toBeFocused();
  }
});


for (const selection of ["file", "task", "same-name"]) {
  test(`OCR marks changed ${selection} stale after completion`, async ({ page }) => {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route("**/ocr", async route => { await held; await route.fulfill({ json: OCR }); });
    await openHealthyConsole(page, "#ocr");
    await page.locator("#ocr-file").setInputFiles(IMAGE);
    await page.locator("#ocr-run").click();
    if (selection === "task") await page.locator("#ocr-task").selectOption("table");
    else await page.locator("#ocr-file").setInputFiles({ ...IMAGE,
      name: selection === "file" ? "other.png" : IMAGE.name,
      buffer: Buffer.concat([IMAGE.buffer, Buffer.from("changed")]),
    });
    release();
    await expect(page.locator("#ocr-results .stale-note")).toContainText("previous run (sample.png, ocr)");
    await expect(page.locator("#ocr-results")).toContainText("sample text");
  });
}

test("OCR retains results when export fails", async ({ page }) => {
  await page.route("**/ocr", route => route.fulfill({ json: OCR }));
  await openHealthyConsole(page, "#ocr");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-download").check();
  await page.evaluate(() => {
    const original = URL.createObjectURL;
    URL.createObjectURL = blob => {
      if (blob.type === "text/markdown") throw new Error("download unavailable");
      return original.call(URL, blob);
    };
  });
  await page.locator("#ocr-run").click();
  await expect(page.locator("#ocr-errors")).toContainText("Download failed");
  await expect(page.locator("#ocr-results .pv")).toBeVisible();
  await expect(page.locator("#ocr-results")).toContainText("sample text");
});

for (const outcome of ["cancel", "error"]) {
  test(`OCR ${outcome} does not wait for bitmap decoding`, async ({ page, expectedConsoleErrors }) => {
    if (outcome === "error") expectedConsoleErrors.push(/503/);
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route("**/ocr", async route => {
      await held;
      await route.fulfill({ status: 503, json: { detail: { code: "backend_unavailable" } } });
    });
    await openHealthyConsole(page, "#ocr");
    await page.evaluate(() => {
      window.createImageBitmap = () => new Promise(resolve => {
        window.releaseBitmap = () => resolve({ width: 1, height: 1, close: () => { window.bitmapClosed = true; } });
      });
    });
    await page.locator("#ocr-file").setInputFiles(IMAGE);
    await page.locator("#ocr-run").click();
    if (outcome === "cancel") await page.locator("#ocr-cancel").click();
    else release();
    await expect(page.locator("#ocr-errors")).toContainText(outcome === "cancel" ? "cancelled" : "backend is unavailable");
    await expect(page.locator("#ocr-cancel")).toBeHidden();
    await page.evaluate(() => window.releaseBitmap());
    await expect.poll(() => page.evaluate(() => window.bitmapClosed)).toBe(true);
    release();
  });
}

test("discarded LOC geometry keeps the following region label and scale", async ({ page }) => {
  const loc = (...values) => values.map(v => `<|LOC_${v}|>`).join("");
  const result = `Bad${loc(0, 0, 1500, 0, 1500, 1000, 0, 1000)} Good${loc(100, 200, 300, 200, 300, 400, 100, 400)}`;
  await page.route("**/ocr", route => route.fulfill({ json: { ...OCR, task: "spotting", result, boxes: [[100, 200, 300, 400]] } }));
  await openHealthyConsole(page, "#ocr");
  await page.locator("#ocr-file").setInputFiles(IMAGE);
  await page.locator("#ocr-task").selectOption("spotting");
  await page.locator("#ocr-run").click();
  await expect(page.locator("#ocr-results .pv-rect")).toHaveCount(1);
  expect(Number(await page.locator("#ocr-results .pv-rect").getAttribute("x"))).toBeCloseTo(0.1);
  await page.locator('#ocr-results [data-tab="blocks"]').click();
  await expect(page.locator('#ocr-results [data-panel="blocks"]')).toContainText("Good");
  await expect(page.locator('#ocr-results [data-panel="blocks"]')).not.toContainText("Bad");
});
