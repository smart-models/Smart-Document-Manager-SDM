import path from "node:path";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const reportDir = path.join(root, "docs/verification/seaweedfs");

test.use({ trace: "off" }); // Authenticated live traces can contain local credentials.
test("real storage archive can be browsed, downloaded and deleted", async ({ page, request }) => {
  test.setTimeout(240_000);
  const base = process.env.OCR_UI_BASE_URL;
  if (!base) throw new Error("Set OCR_UI_BASE_URL to opt into the live storage test.");
  const env = await readFile(path.join(root, ".env"), "utf8");
  const localToken = env.match(/^UPSTREAM_CLIENT_TOKEN=(.*)$/m)?.[1].trim().replace(/^(['"])(.*)\1$/, "$2") || "";
  const token = process.env.UPSTREAM_CLIENT_TOKEN ?? localToken;
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  await page.addInitScript(value => { if (value) sessionStorage.setItem("ocr_api_key", value); }, token);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const input = await readFile(path.join(root, "calibration/plain_text.png"));
  const response = await request.post(`${base}/ocr`, {
    headers, data: { image_base64: input.toString("base64") }, timeout: 180_000,
  });
  expect(response.status()).toBe(200);
  expect(response.headers()["x-ocr-storage-status"]).toBe("stored");
  const manifestKey = response.headers()["x-ocr-archive-key"];
  const jobId = response.headers()["x-ocr-job-id"];
  const prefix = manifestKey.slice(0, -"manifest.json".length);
  const archiveURL = `${base}/storage/archive?${new URLSearchParams({ prefix })}`;
  let deleted = false;
  try {
    const infoResponse = await request.get(archiveURL, { headers });
    expect(infoResponse.status()).toBe(200);
    const info = await infoResponse.json();
    const markdown = info.files.find(file => file.name === "document.md");
    expect(markdown).toBeTruthy();
    const fileResponse = await request.get(`${base}/storage/file?${new URLSearchParams({ key: markdown.key })}`, { headers });
    expect(fileResponse.status()).toBe(200);
    const expectedBytes = await fileResponse.body();
    expect(expectedBytes.toString("utf8").toLowerCase()).toContain("quick brown fox");
    await page.goto(`${base}/#storage`);
    await expect(page.locator("#storage-refresh")).toBeEnabled();
    await page.locator("#storage-kind").selectOption("ocr");
    await expect(page.locator("#storage-refresh")).toBeEnabled();
    // Current prefixes are <file>__<YYYYMMDDTHHMMSSffffffZ>__<code>/ (UTC day).
    const stamp = prefix.split("__")[1];
    const date = `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}`;
    await page.locator("#storage-date-from").fill(date);
    await page.locator("#storage-date-to").fill(date);
    await page.locator("#storage-refresh").click();
    await page.locator("#storage-search").fill(jobId);
    const row = page.locator("#storage-archives tbody tr").filter({ hasText: jobId });
    for (let pages = 0; pages < 100; pages++) {
      await expect(page.locator("#storage-refresh")).toBeEnabled();
      if (await row.count()) break;
      expect(await page.locator("#storage-next").isEnabled()).toBe(true);
      await page.locator("#storage-next").click();
    }
    await expect(row).toHaveCount(1);
    await row.locator(".storage-archive").click();
    await page.locator(".storage-file-choice").filter({ hasText: "document.md" }).click();
    await expect(page.locator(".storage-preview pre")).toContainText("quick brown fox");
    const pendingDownload = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download document.md", exact: true }).click();
    const download = await pendingDownload;
    expect(download.suggestedFilename()).toBe("document.md");
    expect(await readFile(await download.path())).toEqual(expectedBytes);
    // The archived original comes back byte-for-byte with its detected extension.
    const pendingSource = page.waitForEvent("download");
    await row.getByRole("button", { name: /^Download source of / }).click();
    const source = await pendingSource;
    expect(source.suggestedFilename()).toMatch(/\.png$/);
    expect(await readFile(await source.path())).toEqual(input);
    await mkdir(reportDir, { recursive: true });
    await page.screenshot({ path: path.join(reportDir, "storage-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: path.join(reportDir, "storage-mobile.png"), fullPage: true });
    const remove = row.getByRole("button", { name: /^Delete archive / });
    await remove.click();
    await expect(page.locator("#storage-cancel")).toBeFocused();
    await expect(page.locator("#storage-confirm-description")).toContainText(jobId);
    await page.locator("#storage-cancel").click();
    expect((await request.get(archiveURL, { headers })).status()).toBe(200);
    await remove.click();
    await page.locator("#storage-delete-confirm").click();
    await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
    expect((await request.get(archiveURL, { headers })).status()).toBe(404);
    expect((await request.get(`${base}/storage/file?${new URLSearchParams({ key: markdown.key })}`, { headers })).status()).toBe(404);
    deleted = true;
    expect(errors).toEqual([]);
    await writeFile(path.join(reportDir, "browser.json"), JSON.stringify({
      timestamp: new Date().toISOString(), passed: true, job_id: jobId,
      real_ocr: true, filters: true, preview: true, download_bytes_equal: true,
      source_download_bytes_equal: true,
      mobile_width: 390, deletion_cancel_preserved_archive: true,
      confirmed_deletion: true, deleted_files_return_404: true, page_errors: errors,
    }, null, 2) + "\n");
  } finally {
    if (!deleted) await request.delete(archiveURL, { headers, data: { confirm: jobId } });
  }
});
