import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("real Parse, OCR and monitoring use the configured server", async ({ page }) => {
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
  await expect(page.locator("#parse-max-pages")).toHaveValue(String(config.maxPages));
  for (const endpoint of ["parse", "ocr"]) {
    await page.locator(`#tab-${endpoint}`).click();
    await page.locator(`#${endpoint}-file`).setInputFiles(path.join(root, "calibration/plain_text.png"));
    const response = page.waitForResponse(res => new URL(res.url()).pathname === `/${endpoint}` && res.request().method() === "POST", { timeout: operationTimeout });
    await page.locator(`#${endpoint}-run`).click();
    expect((await response).ok()).toBe(true);
    await expect(page.locator(`#${endpoint}-results`)).toContainText("quick brown fox", { timeout: operationTimeout });
  }
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-stats")).toContainText("active requests");
  await expect(page.locator("#mon-table")).toContainText("parse");
  await page.screenshot({ path: path.join(root, "docs/verification/audit-fixes-live-monitor.png"), fullPage: true });
  expect(errors).toEqual([]);
});
