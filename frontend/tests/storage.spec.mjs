import { mkdir } from "node:fs/promises";
import { expect, test, openHealthyConsole, PIXEL_PNG, withApi } from "./fixtures.mjs";

const JOB = "a".repeat(32);
const PREFIX = `parse/2026/09/26/${JOB}/`;
const ROW = { prefix: PREFIX, job_id: JOB, kind: "parse", created_at: "2026-09-26T10:00:00Z", object_count: 3, total_bytes: 300, partial_ocr: false };
const CURRENT_ROW = { prefix: "report.pdf__20260926T120000000000Z__Abcd123_/", job_id: "Abcd123_", filename: "report.pdf", kind: "parse", created_at: "2026-09-26T12:00:00Z", object_count: 2, total_bytes: 200, partial_ocr: false };
const FILES = [
  { key: PREFIX + "document.md", name: "document.md", size: 64, content_type: "text/markdown" },
  { key: PREFIX + "manifest.json", name: "manifest.json", size: 180, content_type: "application/json" },
  { key: PREFIX + "images/p0001-0001.png", name: "images/p0001-0001.png", size: 56, content_type: "image/png" },
];
const MARKDOWN = "# Stored document\n<img src=https://external.invalid/pixel onerror=alert(1)>";

for (const failClick of [false, true]) test(`storage Blob download cleans up after ${failClick ? "failure" : "success"}`, async ({ page }) => {
  await page.goto("/");
  const result = await page.evaluate(async fail => {
    const { downloadBlob } = await import("/assets/storage.js");
    const original = {
      create: URL.createObjectURL, revoke: URL.revokeObjectURL,
      click: HTMLAnchorElement.prototype.click, timer: window.setTimeout,
    };
    const events = [], timers = [];
    URL.createObjectURL = blob => { events.push(["create", blob.size]); return "blob:storage-test"; };
    URL.revokeObjectURL = url => events.push(["revoke", url]);
    window.setTimeout = (callback, delay) => { timers.push(callback); events.push(["delay", delay]); };
    HTMLAnchorElement.prototype.click = function () {
      events.push(["click", this.download, this.href, document.body.contains(this)]);
      if (fail) throw new Error("synthetic click failure");
    };
    let filename, error;
    try {
      try { filename = downloadBlob(new Blob(["data"]), "report.md"); }
      catch (caught) { error = caught.message; }
      events.push(["remaining", document.querySelectorAll('a[download="report.md"]').length]);
      timers.forEach(callback => callback());
      return { filename, error, events };
    } finally {
      URL.createObjectURL = original.create; URL.revokeObjectURL = original.revoke;
      HTMLAnchorElement.prototype.click = original.click; window.setTimeout = original.timer;
    }
  }, failClick);
  expect(result.filename).toBe(failClick ? undefined : "report.md");
  expect(result.error).toBe(failClick ? "synthetic click failure" : undefined);
  expect(result.events).toEqual([
    ["create", 4], ["click", "report.md", "blob:storage-test", true],
    ["delay", 10000], ["remaining", 0], ["revoke", "blob:storage-test"],
  ]);
});

async function storageFixture(page, { items = [ROW], files = FILES, deleteStatus = 200 } = {}) {
  const calls = [];
  let deleted = false;
  await page.route("**/storage/**", async route => {
    const request = route.request(), url = new URL(request.url());
    calls.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), method: request.method(), body: request.postDataJSON(), headers: request.headers() });
    if (request.method() === "DELETE") {
      deleted = deleteStatus === 200;
      await route.fulfill({ status: deleteStatus, json: deleted ? { deleted_objects: 3 } : { detail: { code: "storage_delete_partial", message: "Some files remain. Retry deletion." } } });
    } else if (url.pathname.endsWith("/archives")) {
      await route.fulfill({ json: { items: deleted ? [] : items, next_cursor: null } });
    } else if (url.pathname.endsWith("/archive")) {
      await route.fulfill({ json: { prefix: PREFIX, manifest: { complete: true }, files } });
    } else {
      const key = url.searchParams.get("key");
      await route.fulfill({ contentType: key.endsWith(".png") ? "image/png" : "text/plain", body: key.endsWith(".png") ? Buffer.from(PIXEL_PNG, "base64") : key.endsWith(".json") ? '{"complete":true}' : MARKDOWN });
    }
  });
  return calls;
}

test("Storage browses files, previews safely, downloads and sends API authentication", async ({ page }) => {
  const calls = await storageFixture(page);
  await openHealthyConsole(page);
  await withApi(page, api => api.saveToken("browser-test-key"));
  await page.getByRole("tab", { name: "Storage", exact: true }).click();
  await expect(page.locator("#storage-page-info")).toContainText("1 of 1 on this page");
  await page.locator(".storage-archive").click();
  await expect(page.locator(".storage-file")).toHaveCount(3);
  await page.locator(".storage-file-choice").filter({ hasText: "document.md" }).click();
  await expect(page.locator(".storage-preview pre")).toContainText(MARKDOWN);
  await expect(page.locator(".storage-preview img")).toHaveCount(0);
  await page.locator(".storage-file-choice").filter({ hasText: "manifest.json" }).click();
  await expect(page.locator(".storage-preview pre")).toContainText('"complete": true');
  await page.locator(".storage-file-choice").filter({ hasText: "images/" }).click();
  await expect(page.locator(".storage-preview img")).toBeVisible();
  expect(await page.locator(".storage-preview img").evaluate(image => image.naturalWidth)).toBe(1);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download document.md", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("document.md");
  expect(calls.every(call => call.headers.authorization === "Bearer browser-test-key")).toBe(true);
  expect(calls.some(call => call.query.preview === "true")).toBe(true);
  expect(calls.some(call => call.query.preview === "false")).toBe(true);
});

test("Storage labels current-layout archives with their filename and short code", async ({ page }) => {
  await storageFixture(page, { items: [CURRENT_ROW] });
  await openHealthyConsole(page, "#storage");
  const row = page.locator(".storage-archive");
  await expect(row).toContainText("report.pdf");
  await expect(row.locator("xpath=..").locator(".storage-job-code")).toHaveText("id: Abcd123_");
});

test("Storage rows download the archived source under the uploaded name only when saved", async ({ page }) => {
  const report = { ...CURRENT_ROW, input_key: CURRENT_ROW.prefix + "input.pdf" };
  const scan = { ...CURRENT_ROW, prefix: "scan__20260926T110000000000Z__Efgh456_/", job_id: "Efgh456_", filename: "scan", input_key: "scan__20260926T110000000000Z__Efgh456_/input.png" };
  const calls = await storageFixture(page, { items: [report, scan, ROW] });
  await openHealthyConsole(page, "#storage");
  const archives = page.getByRole("table", { name: "Saved archives", exact: true });
  await expect(archives.getByRole("button", { name: /^Download source of / })).toHaveCount(2);
  for (const [row, filename] of [[report, "report.pdf"], [scan, "scan.png"]]) {
    const download = page.waitForEvent("download");
    await archives.getByRole("button", { name: `Download source of ${row.filename} (${row.job_id})`, exact: true }).click();
    expect((await download).suggestedFilename()).toBe(filename);
    expect(calls.some(call => call.path.endsWith("/file") && call.query.key === row.input_key)).toBe(true);
    await expect(page.locator("#storage-status")).toContainText(`Archive ${row.filename} (${row.job_id}): Downloaded ${filename}.`);
  }
});

test("Storage filters, searches this page, and preserves explicit deletion confirmation", async ({ page }) => {
  const calls = await storageFixture(page);
  await openHealthyConsole(page, "#storage");
  await page.locator("#storage-kind").selectOption("parse");
  await page.locator("#storage-date-from").fill("2026-09-25");
  await page.locator("#storage-date-to").fill("2026-09-26");
  await page.locator("#storage-refresh").click();
  await expect(page.locator(".storage-archive")).toHaveCount(1);
  expect(calls.some(call => call.query.kind === "parse" && call.query.date_from === "2026-09-25" && call.query.date_to === "2026-09-26" && !("date" in call.query))).toBe(true);
  const archiveCalls = () => calls.filter(call => call.path.endsWith("/archives")).length;
  const beforeInvalid = archiveCalls();
  await page.locator("#storage-date-from").fill("2026-09-27");
  await page.locator("#storage-date-to").fill("2026-09-26");
  await expect(page.locator("#storage-status")).toContainText("From date must not be after To date");
  expect(archiveCalls()).toBe(beforeInvalid);
  await page.locator("#storage-date-from").fill("2026-09-25");
  await expect(page.locator(".storage-archive")).toHaveCount(1);
  await page.locator("#storage-search").fill("missing");
  await expect(page.locator("#storage-archives")).toContainText("No matches on this page");
  await page.locator("#storage-search").fill("");
  await page.locator(".storage-archive").click();
  await page.locator("#storage-archives").getByRole("button", { name: /^Delete archive / }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(calls.filter(call => call.method === "DELETE")).toHaveLength(0);
  await page.locator("#storage-archives").getByRole("button", { name: /^Delete archive / }).click();
  await page.locator("#storage-delete-confirm").click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
  const mutation = calls.find(call => call.method === "DELETE");
  expect(mutation.query.prefix).toBe(PREFIX);
  expect(mutation.body).toEqual({ confirm: JOB });
  await expect(page.locator(".storage-archive")).toHaveCount(0);
});

test("Storage keeps bounded-scan empty pages navigable", async ({ page }) => {
  await page.route("**/storage/archives?**", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    return route.fulfill({ json: cursor ? { items: [ROW], next_cursor: null } : { items: [], next_cursor: "page-two" } });
  });
  await openHealthyConsole(page, "#storage");
  await expect(page.locator("#storage-archives")).toContainText("Continue with Next");
  await page.locator("#storage-next").click();
  await expect(page.locator(".storage-archive")).toHaveCount(1);
  await expect(page.locator("#storage-next")).toBeDisabled();
  await page.locator("#storage-prev").click();
  await expect(page.locator("#storage-prev")).toBeDisabled();
  await expect(page.locator("#storage-next")).toBeEnabled();
});

test("Storage reports skipped corrupt archives without hiding readable ones", async ({ page }) => {
  await page.route("**/storage/archives?**", route => route.fulfill({
    json: { items: [ROW], next_cursor: null, skipped_archives: 2 },
  }));
  await openHealthyConsole(page, "#storage");
  await expect(page.locator("#storage-status")).toContainText("2 archives still being written are not shown yet");
  await expect(page.locator(".storage-archive")).toHaveCount(1);
});

test.describe("Storage errors", () => {
  test.use({ expectedConsoleErrors: [/503/] });
  test("reports incomplete deletion and leaves retry available", async ({ page }) => {
    await storageFixture(page, { deleteStatus: 503 });
    await openHealthyConsole(page, "#storage");
    await page.locator(".storage-archive").click();
    await page.locator("#storage-archives").getByRole("button", { name: /^Delete archive / }).click();
    await page.locator("#storage-delete-confirm").click();
    await expect(page.locator("#storage-confirm-error")).toContainText("Some files remain");
    await expect(page.locator("#storage-delete-confirm")).toBeEnabled();
    await expect(page.getByRole("dialog")).toBeVisible();
  });
  test("remains usable when OCR is unavailable and reports storage outage", async ({ page }) => {
    await page.route("**/health", route => route.fulfill({ json: { status: "degraded", vllm: "unreachable", model: "test" } }));
    await page.route("**/storage/archives?**", route => route.fulfill({ status: 503, json: { detail: { message: "Storage unavailable. Try again." } } }));
    await page.goto("/#storage");
    await expect(page.locator("#storage-status")).toContainText("Storage unavailable");
    await expect(page.locator("#storage-refresh")).toBeEnabled();
  });
  test("preserves a refresh error after successful deletion", async ({ page }) => {
    await storageFixture(page);
    let loads = 0;
    await page.route("**/storage/archives?**", route => {
      loads++;
      return route.fulfill(loads === 1
        ? { json: { items: [ROW], next_cursor: null } }
        : { status: 503, json: { detail: { code: "storage_unavailable" } } });
    });
    await openHealthyConsole(page, "#storage");
    await page.locator(".storage-archive").click();
    await page.locator("#storage-archives").getByRole("button", { name: /^Delete archive / }).click();
    await page.locator("#storage-delete-confirm").click();
    await expect(page.locator("#storage-status")).toContainText("Archive deleted. Storage is unavailable");
    await expect(page.locator("#storage-refresh")).toBeEnabled();
  });
});

test("Storage fits mobile and retains keyboard-accessible controls", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  // The widest row: a saved source adds a fourth action.
  await storageFixture(page, { items: [{ ...ROW, input_key: PREFIX + "input.pdf" }] });
  await openHealthyConsole(page, "#storage");
  await page.locator(".storage-archive").click();
  await page.locator(".storage-file-choice").filter({ hasText: "document.md" }).click();
  await expect(page.locator(".storage-preview pre")).toContainText("Stored document");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.locator("#storage-archives").getByRole("button", { name: /^Delete archive / }).click();
  await expect(page.locator("#storage-cancel")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
});

test("Storage tables expose icon actions and paginate files without losing selection", async ({ page }) => {
  const files = Array.from({ length: 23 }, (_, index) => ({
    key: PREFIX + `document-${index + 1}.md`, name: `document-${index + 1}.md`,
    size: 64 + index, content_type: "text/markdown",
  }));
  const calls = await storageFixture(page, { files });
  await openHealthyConsole(page, "#storage");
  const archives = page.getByRole("table", { name: "Saved archives", exact: true });
  await expect(archives.getByRole("columnheader").last()).toHaveText("Action");
  const open = archives.getByRole("button", { name: `Open archive ${JOB}`, exact: true });
  await open.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "PARSE ARCHIVE" })).toBeFocused();
  const table = page.getByRole("table", { name: "Archive files", exact: true });
  await expect(table.getByRole("columnheader").last()).toHaveText("Action");
  await expect(table.locator("tbody tr")).toHaveCount(10);
  const pagination = page.getByRole("navigation", { name: "File pagination" });
  await expect(pagination).toContainText("1–10 of 23 files · Page 1 of 3");
  await expect(pagination.getByRole("button", { name: "Previous" })).toBeDisabled();
  await pagination.getByRole("button", { name: "Next" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(10);
  await table.getByRole("button", { name: "Preview document-11.md", exact: true }).click();
  await expect(page.locator(".storage-preview pre")).toContainText(MARKDOWN);
  const download = page.waitForEvent("download");
  await table.getByRole("button", { name: "Download document-11.md", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("document-11.md");
  expect(calls.some(call => call.query.key === files[10].key)).toBe(true);
  await pagination.getByRole("button", { name: "Next" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(3);
  await expect(pagination.getByRole("button", { name: "Next" })).toBeDisabled();
  await pagination.getByRole("button", { name: "Previous" }).click();
  await expect(table.locator(".is-selected")).toContainText("document-11.md");
  await pagination.getByRole("combobox", { name: "Rows per page" }).selectOption("20");
  await expect(table.locator("tbody tr")).toHaveCount(20);
  await expect(pagination).toContainText("1–20 of 23 files · Page 1 of 2");
  await pagination.getByRole("combobox").selectOption("50");
  await expect(table.locator("tbody tr")).toHaveCount(23);
  await expect(pagination.getByRole("button", { name: "Next" })).toBeDisabled();
  await pagination.getByRole("combobox").selectOption("10");
  await mkdir("../docs/verification/storage-table", { recursive: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "../docs/verification/storage-table/desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  for (const target of [archives, table]) {
    const wrapper = target.locator("..");
    await wrapper.evaluate(element => { element.scrollLeft = element.scrollWidth; });
    const bounds = await target.locator("tbody tr").first().locator("td").last().boundingBox();
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
    await wrapper.evaluate(element => { element.scrollLeft = 0; });
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "../docs/verification/storage-table/mobile.png", fullPage: true });
  const remove = archives.getByRole("button", { name: `Delete archive ${JOB}`, exact: true });
  await remove.click();
  await expect(page.locator("#storage-cancel")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(remove).toBeFocused();
  expect(calls.filter(call => call.method === "DELETE")).toHaveLength(0);
  await remove.click();
  await page.locator("#storage-delete-confirm").click();
  await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
});

test("Storage archive page size and filters reset the cursor", async ({ page }) => {
  const calls = [];
  await page.route("**/storage/archives?**", route => {
    const query = Object.fromEntries(new URL(route.request().url()).searchParams);
    calls.push(query);
    return route.fulfill({ json: { items: [ROW], next_cursor: query.cursor ? null : "page-two" } });
  });
  await openHealthyConsole(page, "#storage");
  await page.locator("#storage-next").click();
  await expect(page.locator("#storage-page-info")).toContainText("Page 2");
  await expect(page.locator("#storage-next")).toBeDisabled();
  await page.locator("#storage-page-size").selectOption("50");
  await expect(page.locator("#storage-page-info")).toContainText("Page 1");
  await expect(page.locator("#storage-prev")).toBeDisabled();
  expect(calls.at(-1).limit).toBe("50");
  expect(calls.at(-1).cursor).toBeUndefined();
  await page.locator("#storage-next").click();
  await expect(page.locator("#storage-page-info")).toContainText("Page 2");
  await page.locator("#storage-kind").selectOption("ocr");
  await expect(page.locator("#storage-page-info")).toContainText("Page 1");
  await expect(page.locator("#storage-prev")).toBeDisabled();
  expect(calls.at(-1)).toMatchObject({ kind: "ocr", limit: "50" });
  expect(calls.at(-1).cursor).toBeUndefined();
});

for (const interaction of ["typing in the filter", "scrolling the page"]) {
  test(`Storage delayed details preserve ${interaction}`, async ({ page }) => {
    const items = Array.from({ length: 12 }, (_, index) => ({
      ...ROW, job_id: String(index).padStart(32, "a"),
      prefix: `parse/2026/09/26/${String(index).padStart(32, "a")}/`,
    }));
    await storageFixture(page, { items });
    let holdRoute;
    const pending = new Promise(resolve => { holdRoute = resolve; });
    await page.route("**/storage/archive?**", route => holdRoute(route));
    await openHealthyConsole(page, "#storage");
    await page.getByRole("button", { name: `Open archive ${items[0].job_id}`, exact: true }).click();
    const route = await pending;
    const search = page.locator("#storage-search");
    if (interaction === "typing in the filter") {
      await search.fill("a");
    } else {
      await page.mouse.wheel(0, 300);
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(200);
    }
    const scrollBefore = await page.evaluate(() => window.scrollY);
    await route.fulfill({ json: { prefix: items[0].prefix, files: FILES } });
    await expect(page.getByRole("table", { name: "Archive files", exact: true })).toBeVisible();
    if (interaction === "typing in the filter") {
      await expect(search).toBeFocused();
      await page.keyboard.type("bc");
      await expect(search).toHaveValue("abc");
    } else {
      await expect(page.getByRole("heading", { name: "PARSE ARCHIVE" })).not.toBeFocused();
    }
    expect(await page.evaluate(() => window.scrollY)).toBeCloseTo(scrollBefore, 0);
  });
}

test("Storage has a parameter reference like Parse and OCR", async ({ page }) => {
  await storageFixture(page);
  await openHealthyConsole(page, "#storage");
  const reference = page.locator("#panel-storage details.reference");
  await expect(reference.locator("summary")).toHaveText("Parameter reference");
  await expect(page.locator("#storage-reference")).toBeHidden();
  await reference.locator("summary").click();
  await expect(page.locator("#storage-reference .param-name")).toContainText([
    "Output type", "From / To (UTC)", "Search this page", "Refresh", "Rows per page",
    "Archive list", "Actions", "Files and preview", "Delete archive", "Availability",
  ]);
  await expect(page.locator("#storage-reference")).toContainText("unknown type appear only under All outputs");
  await expect(page.locator("#storage-reference")).toContainText("From / To (UTC)");
  await page.screenshot({ path: "test-results/storage-reference.png" });
});


for (const action of ["file", "zip"]) {
  test(`Storage reports local ${action} download errors and stays usable`, async ({ page }) => {
    await storageFixture(page);
    await openHealthyConsole(page, "#storage");
    await page.locator(".storage-archive").click();
    await page.evaluate(() => { URL.createObjectURL = () => { throw new Error("download unavailable"); }; });
    const control = action === "file"
      ? page.getByRole("button", { name: "Download document.md", exact: true })
      : page.getByRole("button", { name: `Download ${JOB} as ZIP`, exact: true });
    // ZIP needs its own response; it has no file key.
    if (action === "zip") await page.route("**/storage/archive.zip?**", r => r.fulfill({ contentType: "application/zip", body: "zip" }));
    await control.click();
    await expect(page.locator(action === "file" ? "#storage-detail .storage-status" : "#storage-status")).toContainText("download unavailable");
    await expect(control).toBeEnabled();
    await expect(page.locator(".storage-file")).toHaveCount(3);
  });
}

test("Storage download completion names its original archive after selection changes", async ({ page }) => {
  const other = { ...ROW, prefix: `parse/2026/09/26/${"b".repeat(32)}/`, job_id: "b".repeat(32) };
  await storageFixture(page, { items: [ROW, other] });
  let started, release;
  const pending = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/storage/file?**", async route => { started(); await held; await route.fulfill({ contentType: "text/plain", body: "original archive" }); });
  await openHealthyConsole(page, "#storage");
  await page.locator(".storage-archive").first().click();
  await page.getByRole("button", { name: "Download document.md", exact: true }).click();
  await pending;
  await page.locator(".storage-archive").nth(1).click();
  await expect(page.locator("#storage-detail .storage-prefix")).toHaveText(other.prefix);
  const downloaded = page.waitForEvent("download");
  release(); await downloaded;
  await expect(page.locator("#storage-status")).toContainText(`Archive ${JOB}: Downloaded document.md.`);
  await expect(page.locator("#storage-detail .storage-status")).toBeEmpty();
});

test("Storage invalid dates cancel loading and restore controls", async ({ page }) => {
  await storageFixture(page);
  await openHealthyConsole(page, "#storage");
  let release, started;
  const held = new Promise(resolve => { release = resolve; });
  const pending = new Promise(resolve => { started = resolve; });
  await page.route("**/storage/archives?**", async route => { started(); await held; await route.fulfill({ json: { items: [ROW], next_cursor: null } }); });
  await page.locator("#storage-refresh").click(); await pending;
  await page.locator("#storage-date-from").fill("2026-10-12");
  await page.locator("#storage-date-to").fill("2026-10-11");
  await expect(page.locator("#storage-status")).toContainText("From date must not be after To date");
  await expect(page.locator("#storage-refresh")).toBeEnabled();
  await expect(page.locator("#storage-archives")).not.toHaveAttribute("aria-busy");
  await expect(page.locator("#storage-prev")).toBeDisabled();
  await expect(page.locator("#storage-next")).toBeDisabled();
  release();
});
