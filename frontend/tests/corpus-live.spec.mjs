import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const docs = path.join(root, "frontend/tests/testbook/documents");
const corpus = JSON.parse(await readFile(path.join(docs, "manifest.json"), "utf8"));
const expected = name => corpus.files.find(file => file.path === name).expect;
const FINAL = ["done", "partial", "cancelled", "failed"];

test.use({ trace: "off" }); // Authenticated live traces can contain local credentials.

// Archives the cases create through real /parse and /ocr calls, deleted after each case.
let owned = [];
let live = {};

async function openConsole(page, allowedErrors = []) {
  const base = process.env.OCR_UI_BASE_URL;
  if (!base) throw new Error("Live tests require OCR_UI_BASE_URL explicitly.");
  const env = await readFile(path.join(root, ".env"), "utf8").catch(() => "");
  const localToken = env.match(/^UPSTREAM_CLIENT_TOKEN=(.*)$/m)?.[1].trim().replace(/^(['"])(.*)\1$/, "$2") || "";
  const token = process.env.UPSTREAM_CLIENT_TOKEN ?? localToken;
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && !allowedErrors.some(pattern => pattern.test(message.text()))) errors.push(message.text());
  });
  await page.addInitScript(key => { if (key) sessionStorage.setItem("ocr_api_key", key); }, token);
  await page.goto(base);
  const config = await page.evaluate(() => window.OCR_CONFIG);
  if (config?.authRequired && !token) throw new Error("Live tests require UPSTREAM_CLIENT_TOKEN when server authentication is enabled.");
  const timeout = ((config?.requestTimeoutSeconds || 600) + 65) * 1000;
  test.setTimeout(timeout * 4);
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
  live = { base, headers: token ? { Authorization: `Bearer ${token}` } : {} };
  return { config, timeout, errors };
}

// Submits the current form and returns the parsed body; stored archives become case-owned.
async function submit(page, endpoint, timeout) {
  const pending = page.waitForResponse(
    res => new URL(res.url()).pathname === `/${endpoint}` && res.request().method() === "POST",
    { timeout },
  );
  await page.locator(`#${endpoint}-run`).click();
  const response = await pending;
  const headers = response.headers();
  if (headers["x-ocr-storage-status"] === "stored")
    owned.push({ prefix: headers["x-ocr-archive-key"].slice(0, -"manifest.json".length), jobId: headers["x-ocr-job-id"] });
  return { status: response.status(), body: await response.json() };
}

async function parse(page, file, timeout) {
  await page.locator("#tab-parse").click();
  await page.locator("#parse-file").setInputFiles(file);
  const result = await submit(page, "parse", timeout);
  expect(result.status).toBe(200);
  for (const pageResult of result.body.pages) expect(pageResult.error ?? null).toBeNull();
  await expect(page.locator("#parse-status")).toHaveText(`Parse complete: ${result.body.pages.length} pages.`, { timeout });
  return { ...result.body, markdown: result.body.pages.map(item => item.markdown ?? "").join("\n") };
}

test.afterEach(async ({ request }) => {
  for (const { prefix, jobId } of owned)
    await request.delete(`${live.base}/storage/archive?${new URLSearchParams({ prefix })}`, { headers: live.headers, data: { confirm: jobId } });
  owned = [];
});

test("real six-task matrix recognises every corpus element with its oracle", async ({ page }) => {
  const { timeout, errors } = await openConsole(page);
  await page.locator("#tab-ocr").click();
  for (const [task, name] of [
    ["ocr", "textline.png"],
    ["table", "medal_table.png"],
    ["table", "table.jpg"],
    ["formula", "formula.png"],
    ["chart", "chart.png"],
    ["spotting", "book.jpg"],
    ["seal", "seal.png"],
  ]) {
    await page.locator("#ocr-task").selectOption(task);
    await page.locator("#ocr-file").setInputFiles(path.join(docs, name));
    const { status, body } = await submit(page, "ocr", timeout);
    expect(status, `${task} ${name}`).toBe(200);
    expect(body.task).toBe(task);
    for (const text of expected(name)) expect(body.result, `${task} ${name}`).toContain(text);
    await expect(page.locator("#ocr-status")).toContainText("Recognition complete", { timeout });
    const metrics = page.locator("#ocr-results .metric-row");
    await expect(metrics).toContainText(name);
    await expect(metrics).toContainText(`task${task}`);
    const rendered = page.locator('#ocr-results [data-panel="rendered"]');
    if (task === "formula") await expect(page.locator("#ocr-results .formula-preview .katex")).toHaveCount(1);
    else if (task === "table") await expect(rendered.locator("table")).toHaveCount(1);
    else if (task === "spotting") await expect.poll(() => page.locator("#ocr-results .pv-rect").count()).toBeGreaterThan(0);
  }
  expect(errors).toEqual([]);
});

test("real multi-page form943.pdf parses every page at the default DPI", async ({ page }) => {
  const { timeout, errors } = await openConsole(page);
  const result = await parse(page, path.join(root, "calibration/form943.pdf"), timeout);
  expect(result.pages).toHaveLength(3);
  expect(result.markdown).toContain("Form 943");
  await expect(page.locator("#parse-results .pv-page")).toHaveText("Page 1 / 3");
  expect(errors).toEqual([]);
});

test("real paper page parse renders layout blocks and display formulas", async ({ page }) => {
  const { timeout, errors } = await openConsole(page);
  const result = await parse(page, path.join(docs, "doc_with_formula.png"), timeout);
  for (const text of expected("doc_with_formula.png")) expect(result.markdown).toContain(text);
  expect(result.pages[0].blocks.length).toBeGreaterThan(10);
  await expect.poll(() => page.locator("#parse-results .pv-rect").count()).toBeGreaterThan(10);
  await expect.poll(() => page.locator("#parse-results .katex").count()).toBeGreaterThan(0);
  expect(errors).toEqual([]);
});

test("real Office document is converted, previewed and parsed", async ({ page }) => {
  const { timeout, errors } = await openConsole(page);
  const preview = page.waitForResponse(res => new URL(res.url()).pathname === "/office/pdf", { timeout });
  const result = await parse(page, path.join(docs, "sample.docx"), timeout);
  expect((await preview).status()).toBe(200);
  for (const text of expected("sample.docx")) expect(result.markdown).toContain(text);
  await expect(page.locator("#parse-results .pv-host canvas")).toHaveCount(1);
  await expect(page.locator("#parse-results .metric-row")).toContainText("sample.docx");
  expect(errors).toEqual([]);
});

test("real multi-frame TIFF parses each frame as a page and OCR rejects it", async ({ page }) => {
  // The browser logs the expected 422 of the OCR attempt as a failed resource.
  const { timeout, errors } = await openConsole(page, [/status of 422/]);
  const file = path.join(docs, "multiframe.tif");
  const result = await parse(page, file, timeout);
  expect(result.pages).toHaveLength(3);
  for (const text of expected("multiframe.tif")) expect(result.markdown).toContain(text);
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("ocr");
  await page.locator("#ocr-file").setInputFiles(file);
  const { status, body } = await submit(page, "ocr", timeout);
  expect(status).toBe(422);
  expect(JSON.stringify(body)).toContain("multiframe_image_not_supported");
  await expect(page.locator("#ocr-errors")).toContainText("multiple frames");
  await expect(page.locator("#ocr-run")).toBeEnabled();
  expect(errors).toEqual([]);
});

test("real rotated inputs complete without an error while orientation correction is disabled", async ({ page }) => {
  const { timeout, errors } = await openConsole(page);
  const result = await parse(page, path.join(docs, "book_rot180.jpg"), timeout);
  expect(result.pages).toHaveLength(1);
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-task").selectOption("ocr");
  await page.locator("#ocr-file").setInputFiles(path.join(docs, "textline_rot180.jpg"));
  const { status, body } = await submit(page, "ocr", timeout);
  expect(status).toBe(200);
  expect(typeof body.result).toBe("string");
  await expect(page.locator("#ocr-status")).toContainText("Recognition complete", { timeout });
  expect(errors).toEqual([]);
});

test("real long PDF reports block progress and its cancellation reaches the server", async ({ page, request }) => {
  // Aborting the upload is the behaviour under test; the browser logs it.
  const { timeout, errors } = await openConsole(page, [/ERR_ABORTED|ERR_FAILED/]);
  let progressId;
  page.on("request", req => {
    progressId ??= /^\/parse\/progress\/([0-9a-f-]{36})$/.exec(new URL(req.url()).pathname)?.[1];
  });
  await page.locator("#tab-parse").click();
  await page.locator("#parse-file").setInputFiles(path.join(docs, "long.pdf"));
  await page.locator("#parse-run").click();
  // The console announces a block only once it is complete.
  await expect(page.locator("#parse-status")).toContainText(/block 1 of \d+ done/, { timeout });
  await page.locator("#parse-cancel").click();
  await expect(page.locator("#parse-errors")).toContainText("Parse cancelled");
  await expect(page.locator("#parse-run")).toBeEnabled();
  expect(progressId).toBeTruthy();
  const progressURL = `${live.base}/parse/progress/${progressId}`;
  let snapshot;
  await expect.poll(async () => {
    snapshot = await (await request.get(progressURL, { headers: live.headers })).json();
    return FINAL.includes(snapshot.state);
  }, { timeout }).toBe(true);
  expect(snapshot.state).toBe("cancelled");
  expect(snapshot.blocks_total).toBeGreaterThanOrEqual(2);
  expect(snapshot.pages_done).toBeGreaterThan(0);
  expect(snapshot.pages_done).toBeLessThan(snapshot.pages_total);
  expect(errors).toEqual([]);
});
