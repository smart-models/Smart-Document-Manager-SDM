import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const evidenceDir = path.join(root, "docs/verification/frontend-math");

test("real OTSL renders as a table, LaTeX has a preview, and downloads preserve API output", async ({ page }) => {
  if (!process.env.OCR_UI_BASE_URL) throw new Error("Live tests require OCR_UI_BASE_URL explicitly.");
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const token = process.env.UPSTREAM_CLIENT_TOKEN || "";
  await page.addInitScript(key => { if (key) sessionStorage.setItem("ocr_api_key", key); }, token);
  await page.goto(process.env.OCR_UI_BASE_URL);
  const config = await page.evaluate(() => window.OCR_CONFIG);
  if (config?.authRequired && !token) throw new Error("Live tests require UPSTREAM_CLIENT_TOKEN when server authentication is enabled.");
  const operationTimeout = ((config?.requestTimeoutSeconds || 600) + 65) * 1000;
  test.setTimeout(operationTimeout * 2 + 30_000);
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
  await page.locator("#tab-ocr").click();
  await page.locator("#ocr-download").check();
  await mkdir(evidenceDir, { recursive: true });
  const cases = [];
  const downloads = [];
  page.on("download", item => downloads.push(item));
  for (const [task, fixture] of [
    ["table", "calibration/simple_table.png"],
    ["formula", "calibration/formula.png"],
  ]) {
    await page.locator("#ocr-task").selectOption(task);
    await page.locator("#ocr-file").setInputFiles(path.join(root, fixture));
    const responsePromise = page.waitForResponse(
      response => new URL(response.url()).pathname === "/ocr" && response.request().method() === "POST",
      { timeout: operationTimeout },
    );
    downloads.length = 0;
    await page.locator("#ocr-run").click();
    const response = await responsePromise;
    expect(response.ok()).toBe(true);
    // Two files per run: readable text and the complete JSON record.
    await expect.poll(() => downloads.length, { timeout: 30_000 }).toBe(2);
    const download = downloads.find(item => item.suggestedFilename().endsWith(".ocr.json"));
    const text = downloads.find(item => item.suggestedFilename().endsWith(".ocr.md"));
    const payload = await response.json();
    expect(payload.task).toBe(task);
    expect(payload.result.length).toBeGreaterThan(0);
    if (task === "table") expect(payload.result).toMatch(/<(?:fcel|ecel|lcel|ucel|xcel|nl)>/i);
    if (task === "formula") {
      await expect(page.locator("#ocr-results .formula-preview .katex")).toHaveCount(1);
    } else {
      // OTSL is rebuilt as a real table in the rendered tab.
      await expect(page.locator('#ocr-results [data-panel="rendered"] table')).toHaveCount(1);
    }
    // The raw tab keeps the exact model output.
    await page.locator('#ocr-results [data-tab="markdown"]').click();
    const output = page.locator('#ocr-results [data-panel="markdown"] code');
    await expect(output).toBeVisible();
    expect(await output.textContent()).toBe(payload.result);
    expect(await download.failure()).toBeNull();
    expect(JSON.parse(await readFile(await download.path(), "utf8"))).toEqual(payload);
    expect(await readFile(await text.path(), "utf8")).toBe(payload.result);
    await expect(page.locator("#ocr-run")).toBeEnabled();
    await page.screenshot({ path: path.join(evidenceDir, `${task}-live.png`), fullPage: true });
    cases.push({ task, fixture, output: payload, literal_preserved: true, download_preserved: true, math_preview: task === "formula" });
  }
  expect(errors).toEqual([]);
  await mkdir(evidenceDir, { recursive: true });
  await writeFile(path.join(evidenceDir, "browser-live.json"), JSON.stringify({ cases, browser_errors: errors }, null, 2) + "\n");
});
