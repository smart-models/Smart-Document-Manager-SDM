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
  await expect(page.locator("#storage-page-info")).toHaveText("Page 1 of 1");
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

test("Storage filters, searches, and preserves explicit deletion confirmation", async ({ page }) => {
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
  await expect(page.locator("#storage-archives")).toContainText("No matching archives");
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

test("Storage follows cursors past bounded-scan empty pages", async ({ page }) => {
  const cursors = [];
  await page.route("**/storage/archives?**", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    cursors.push(cursor);
    return route.fulfill({ json: cursor ? { items: [ROW], next_cursor: null } : { items: [], next_cursor: "page-two" } });
  });
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-archive")).toHaveCount(1);
  await expect(page.locator("#storage-page-info")).toHaveText("Page 1 of 1");
  await expect(page.locator("#storage-next")).toBeDisabled();
  expect(cursors).toEqual([null, "page-two"]);
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
  // Serve 25 rows ten at a time; `failing` names the requests that answer 503.
  async function routeFailingArchives(page, failing) {
    const rows = Array.from({ length: 25 }, (_, i) => archiveRow(25 - i));
    await page.route("**/storage/archives?**", route => {
      const start = Number(new URL(route.request().url()).searchParams.get("cursor") || 0), end = start + 10;
      if (failing(start)) return route.fulfill({ status: 503, json: { detail: { code: "storage_unavailable" } } });
      return route.fulfill({ json: { items: rows.slice(start, end), next_cursor: end < rows.length ? String(end) : null } });
    });
  }
  test("keeps the rows of a load that failed part-way", async ({ page }) => {
    await routeFailingArchives(page, start => start === 10);
    await openHealthyConsole(page, "#storage");
    await expect(page.locator("#storage-status")).toContainText("Listing incomplete. Storage is unavailable");
    await expect(page.locator("#storage-status")).toHaveClass(/alert-error/);
    await expect(page.locator("#storage-page-info")).toHaveText("Page 1 of 1");
    expect(await archiveNames(page)).toHaveLength(10);
  });
  test("returns to the viewed page after a failed refresh", async ({ page }) => {
    let failing = false;
    await routeFailingArchives(page, () => failing);
    await openHealthyConsole(page, "#storage");
    await page.locator("#storage-next").click();
    await page.locator("#storage-next").click();
    await expect(page.locator("#storage-page-info")).toHaveText("Page 3 of 3");
    failing = true;
    await page.locator("#storage-refresh").click();
    await expect(page.locator("#storage-archives tbody")).toHaveText("Saved files could not be loaded. Refresh to try again.");
    failing = false;
    await page.locator("#storage-refresh").click();
    await expect(page.locator("#storage-page-info")).toHaveText("Page 3 of 3");
  });
  test("stops when the listing repeats a cursor", async ({ page }) => {
    await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [ROW], next_cursor: "again" } }));
    await openHealthyConsole(page, "#storage");
    await expect(page.locator("#storage-status")).toContainText("Archive pagination did not advance");
    await expect(page.locator("#storage-refresh")).toBeEnabled();
  });
  test("reports a malformed listing instead of throwing", async ({ page }) => {
    await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: null, next_cursor: null } }));
    await openHealthyConsole(page, "#storage");
    await expect(page.locator("#storage-status")).toContainText("Storage returned an unexpected response.");
  });
  test("gives focus back to Refresh after a failed load", async ({ page }) => {
    await openHealthyConsole(page, "#storage");
    const refresh = page.locator("#storage-refresh");
    await expect(refresh).toBeEnabled();
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route("**/storage/archives?**", async route => {
      await held;
      await route.fulfill({ status: 503, json: { detail: { code: "storage_unavailable" } } });
    });
    await refresh.click();
    await expect(refresh).toBeDisabled();
    await nextFrame(page);
    release();
    await expect(page.locator("#storage-status")).toContainText("Storage is unavailable");
    await expect(refresh).toBeFocused();
  });
  test("keeps a failed load's message while searching and sorting", async ({ page }) => {
    await page.route("**/storage/archives?**", route => route.fulfill({ status: 503, json: { detail: { code: "storage_unavailable" } } }));
    await openHealthyConsole(page, "#storage");
    const table = page.locator("#storage-archives tbody");
    const failed = "Saved files could not be loaded. Refresh to try again.";
    await expect(table).toHaveText(failed);
    await page.locator("#storage-search").fill("missing");
    await expect(table).toHaveText(failed);
    await page.getByRole("button", { name: "Size", exact: true }).click();
    await expect(table).toHaveText(failed);
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

test("Storage tables expose icon actions and paginate files without losing selection", async ({ page }, testInfo) => {
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
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("desktop.png"), fullPage: true });
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
  await page.screenshot({ path: testInfo.outputPath("mobile.png"), fullPage: true });
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

// Archive `index` was created index minutes after noon: higher is newer.
function archiveRow(index, extra = {}) {
  const code = `Code${String(index).padStart(4, "0")}`, minute = String(index).padStart(2, "0");
  return {
    ...CURRENT_ROW, prefix: `n${index}.pdf__20260926T12${minute}00000000Z__${code}/`, job_id: code,
    filename: `n${index}.pdf`, created_at: `2026-09-26T12:${minute}:00Z`, ...extra,
  };
}

// Serve rows newest first, at most `chunk` per request whatever the limit asks.
async function routeArchives(page, rows, chunk = 10) {
  const calls = [];
  await page.route("**/storage/archives?**", route => {
    const query = Object.fromEntries(new URL(route.request().url()).searchParams);
    calls.push(query);
    const start = Number(query.cursor || 0), end = start + Math.min(Number(query.limit), chunk);
    return route.fulfill({ json: {
      items: rows.slice(start, end), next_cursor: end < rows.length ? String(end) : null,
    } });
  });
  return calls;
}

const archiveNames = page => page.locator("#storage-archives tbody .storage-name").allTextContents();
// `n` rows newest first, named n<n> down to n1.
const rowsOf = n => Array.from({ length: n }, (_, i) => archiveRow(n - i));
const clickColumn = (page, name) => page.getByRole("button", { name, exact: true }).click();
const openedPrefix = page => page.locator("#storage-detail .storage-prefix");
// Let the browser paint, so anything a released request changes has happened.
const nextFrame = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));

// Serve `rows` and accept deletions; opening any archive returns `opened`.
async function routeDeletable(page, rows, opened = rows[0]) {
  await routeArchives(page, rows);
  await page.route("**/storage/archive?**", route => route.request().method() === "DELETE"
    ? route.fulfill({ json: { deleted_objects: 3 } })
    : route.fulfill({ json: { prefix: opened.prefix, files: [] } }));
}

test("Storage loads every archive page once, then pages locally", async ({ page }) => {
  const calls = await routeArchives(page, Array.from({ length: 25 }, (_, i) => archiveRow(25 - i)));
  await openHealthyConsole(page, "#storage");
  const info = page.locator("#storage-page-info");
  await expect(info).toHaveText("Page 1 of 3");
  expect(calls.map(call => [call.cursor, call.limit])).toEqual([[undefined, "50"], ["10", "50"], ["20", "50"]]);
  expect((await archiveNames(page))[0]).toBe("n25.pdf");
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 2 of 3");
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 3 of 3");
  await expect(page.locator("#storage-archives tbody tr")).toHaveCount(5);
  await expect(page.locator("#storage-prev")).toBeFocused();
  await page.locator("#storage-page-size").selectOption("20");
  await expect(info).toHaveText("Page 1 of 2");
  expect(calls).toHaveLength(3);
  await page.locator("#storage-kind").selectOption("ocr");
  await expect(info).toHaveText("Page 1 of 2");
  expect(calls).toHaveLength(6);
  expect(calls.at(-3)).toMatchObject({ kind: "ocr", limit: "50" });
  expect(calls.at(-3).cursor).toBeUndefined();
});

test("Storage sorts every loaded archive by column, ascending then descending", async ({ page }) => {
  // Twelve rows over two requests: the oldest is the largest, n7 is the only OCR,
  // N3 is capitalised and n5 is an abandoned write without counts.
  const rows = Array.from({ length: 12 }, (_, i) => archiveRow(12 - i, {
    total_bytes: (i + 1) * 100, object_count: 12 - i, kind: i === 5 ? "ocr" : "parse",
  }));
  rows[9] = { ...rows[9], filename: "N3.pdf" };
  rows[7] = { ...rows[7], incomplete: true, kind: "unknown", object_count: 0, total_bytes: 0 };
  await routeArchives(page, rows, 7);
  await openHealthyConsole(page, "#storage");
  const header = name => page.locator("#storage-archives th").filter({ hasText: new RegExp(`^${name}$`) });
  const sortBy = name => page.getByRole("button", { name, exact: true });
  await expect(page.locator("#storage-page-info")).toHaveText("Page 1 of 2");
  await expect(header("Created")).toHaveAttribute("aria-sort", "descending");
  await expect(page.locator("#storage-archives th[aria-sort]")).toHaveCount(1);
  // Unsorted columns stay recognisable without hover, which touch never has.
  await expect(header("File").locator("svg")).toHaveCSS("opacity", "0.8");
  await expect(header("Created").locator("svg")).toHaveCSS("opacity", "1");
  expect((await archiveNames(page)).slice(0, 2)).toEqual(["n12.pdf", "n11.pdf"]);

  await sortBy("File").click();
  await expect(header("File")).toHaveAttribute("aria-sort", "ascending");
  await expect(header("Created")).not.toHaveAttribute("aria-sort");
  await expect(sortBy("File")).toBeFocused();
  expect((await archiveNames(page)).slice(0, 4)).toEqual(["n1.pdf", "n2.pdf", "N3.pdf", "n4.pdf"]);
  await sortBy("File").click();
  await expect(header("File")).toHaveAttribute("aria-sort", "descending");
  expect((await archiveNames(page)).slice(0, 2)).toEqual(["n12.pdf", "n11.pdf"]);

  await sortBy("Size").click();
  await sortBy("Size").click();
  await expect(header("Size")).toHaveAttribute("aria-sort", "descending");
  expect((await archiveNames(page)).slice(0, 2)).toEqual(["n1.pdf", "n2.pdf"]);
  await page.locator("#storage-next").click();
  await expect(page.locator("#storage-page-info")).toHaveText("Page 2 of 2");
  expect(await archiveNames(page)).toEqual(["n12.pdf", "n5.pdf"]);

  await sortBy("Output").click();
  await expect(page.locator("#storage-page-info")).toHaveText("Page 1 of 2");
  expect((await archiveNames(page))[0]).toBe("n7.pdf");
  await sortBy("Files").click();
  await sortBy("Files").click();
  expect((await archiveNames(page))[0]).toBe("n12.pdf");
  await sortBy("Created").click();
  await expect(header("Created")).toHaveAttribute("aria-sort", "ascending");
  expect((await archiveNames(page))[0]).toBe("n1.pdf");
  await expect(page.locator("#storage-archives th").last()).not.toHaveAttribute("aria-sort");
  await expect(page.locator("#storage-archives th").last().getByRole("button")).toHaveCount(0);
});

test("Storage search covers every visible column of every page without asking the server", async ({ page }) => {
  const rows = Array.from({ length: 25 }, (_, i) => archiveRow(25 - i, { object_count: 900 + i }));
  rows[5] = { ...rows[5], total_bytes: 3 * 1024 * 1024 };
  rows[16] = { ...rows[16], kind: "ocr" };
  rows[21] = { ...rows[21], partial_ocr: true };
  rows[23] = { ...rows[23], incomplete: true };
  const calls = await routeArchives(page, rows);
  await openHealthyConsole(page, "#storage");
  const info = page.locator("#storage-page-info"), search = page.locator("#storage-search");
  await expect(info).toHaveText("Page 1 of 3");
  const loads = calls.length;
  await page.locator("#storage-next").click();
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 3 of 3");

  await search.fill("N7.PDF");
  await expect(info).toHaveText("Page 1 of 1");
  expect(await archiveNames(page)).toEqual(["n7.pdf"]);
  await expect(page.locator("#storage-prev")).toBeDisabled();
  await expect(page.locator("#storage-next")).toBeDisabled();
  await expect(search).toBeFocused();
  await expect(page.locator("#storage-search-status")).toHaveText("1 match");

  for (const [query, names] of [
    ["2026-09-26 12:03", ["n3.pdf"]], // Created, as displayed
    ["917", ["n8.pdf"]], // Files
    ["3.0 mb", ["n20.pdf"]], // Size
    ["partial", ["n4.pdf"]], // the Partial OCR badge
    ["ocr", ["n9.pdf", "n4.pdf"]], // Output, and that badge again
    ["incomplete", ["n2.pdf"]],
  ]) {
    await search.fill(query);
    expect(await archiveNames(page), query).toEqual(names);
  }
  // An incomplete archive shows "—": its stored count is not a match.
  await search.fill("923");
  await expect(page.locator("#storage-archives tbody")).toHaveText("No matching archives.");
  await expect(page.locator("#storage-search-status")).toHaveText("0 matches");

  // Eleven matches page and sort like the whole list; a new query starts over.
  await search.fill("n1");
  await expect(info).toHaveText("Page 1 of 2");
  await expect(page.locator("#storage-search-status")).toHaveText("11 matches");
  expect((await archiveNames(page))[0]).toBe("n19.pdf");
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 2 of 2");
  expect(await archiveNames(page)).toEqual(["n1.pdf"]);
  await search.fill("n19");
  await expect(info).toHaveText("Page 1 of 1");
  await search.fill("n1");
  await page.getByRole("button", { name: "File", exact: true }).click();
  expect((await archiveNames(page)).slice(0, 2)).toEqual(["n1.pdf", "n10.pdf"]);
  await search.fill("");
  await expect(info).toHaveText("Page 1 of 3");
  await expect(page.locator("#storage-search-status")).toBeEmpty();
  expect(calls).toHaveLength(loads);
});

test("Storage search matches the shown name and the prefix, ignoring surrounding spaces", async ({ page }) => {
  await routeArchives(page, [
    archiveRow(2, { filename: "Mario Rossi.pdf", prefix: "Mario_Rossi.pdf__20260926T120200000000Z__Code0002/" }),
    archiveRow(1, { job_id: "JobOnly1", prefix: "parse/2026/09/26/00000000000000000000000000000001/", filename: undefined }),
  ]);
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-name")).toHaveCount(2);
  const search = page.locator("#storage-search");
  for (const [query, expected] of [
    ["mario rossi", ["Mario Rossi.pdf"]], ["mario_rossi", ["Mario Rossi.pdf"]],
    ["  mario rossi  ", ["Mario Rossi.pdf"]], ["joboNLY1", ["JobOnly1"]], ["parse/2026", ["JobOnly1"]],
  ]) {
    await search.fill(query);
    expect(await archiveNames(page), query).toEqual(expected);
  }
});

test("Storage search resets a page its matches could still reach", async ({ page }) => {
  await routeArchives(page, rowsOf(25));
  await openHealthyConsole(page, "#storage");
  const info = page.locator("#storage-page-info");
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 2 of 3");
  await page.locator("#storage-search").fill("n1");
  await expect(info).toHaveText("Page 1 of 2");
});

test("Storage Previous hands focus to Next at the first page", async ({ page }) => {
  await routeArchives(page, rowsOf(25));
  await openHealthyConsole(page, "#storage");
  await page.locator("#storage-next").click();
  await page.locator("#storage-prev").click();
  await expect(page.locator("#storage-next")).toBeFocused();
});

test("Storage Refresh reopens the archive and a filter starts over on every cursor", async ({ page }) => {
  const rows = rowsOf(25);
  const calls = await routeArchives(page, rows);
  await page.route("**/storage/archive?**", route => route.fulfill({ json: { prefix: rows[12].prefix, files: [] } }));
  await openHealthyConsole(page, "#storage");
  const info = page.locator("#storage-page-info");
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 2 of 3");
  await page.getByRole("button", { name: "n13.pdf", exact: true }).click();
  await expect(openedPrefix(page)).toHaveText(rows[12].prefix);
  await page.locator("#storage-refresh").click();
  await expect(openedPrefix(page)).toHaveText(rows[12].prefix);
  await expect(info).toHaveText("Page 2 of 3");
  await expect(page.locator("#storage-refresh")).toBeFocused();
  await page.locator("#storage-kind").selectOption("ocr");
  await expect(info).toHaveText("Page 1 of 3");
  await expect(openedPrefix(page)).toHaveCount(0);
  // The server refuses a cursor whose filters differ from the first request's.
  expect(calls.slice(-3).map(call => call.kind)).toEqual(["ocr", "ocr", "ocr"]);
});

test("Storage deletion of another archive stays on the page and keeps its files open", async ({ page }) => {
  const rows = rowsOf(25);
  await routeDeletable(page, rows, rows[12]);
  await openHealthyConsole(page, "#storage");
  await page.locator("#storage-next").click();
  await page.getByRole("button", { name: "n13.pdf", exact: true }).click();
  await expect(openedPrefix(page)).toHaveText(rows[12].prefix);
  await page.getByRole("button", { name: "Delete archive n14.pdf (Code0014)", exact: true }).click();
  await page.locator("#storage-delete-confirm").click();
  await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
  await expect(page.locator("#storage-page-info")).toHaveText("Page 2 of 3");
  await expect(openedPrefix(page)).toHaveText(rows[12].prefix);
});

test("Storage deletion of the open archive closes its files", async ({ page }) => {
  const rows = rowsOf(2);
  await routeDeletable(page, rows);
  await openHealthyConsole(page, "#storage");
  await page.getByRole("button", { name: "n2.pdf", exact: true }).click();
  await expect(openedPrefix(page)).toHaveText(rows[0].prefix);
  await page.getByRole("button", { name: "Delete archive n2.pdf (Code0002)", exact: true }).click();
  await page.locator("#storage-delete-confirm").click();
  await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
  await expect(page.locator("#storage-detail")).toHaveText("Select an archive to view its files.");
});

test("Storage deletion of the last page's only row moves to the new last page", async ({ page }) => {
  await routeDeletable(page, rowsOf(21));
  await openHealthyConsole(page, "#storage");
  await page.locator("#storage-next").click();
  await page.locator("#storage-next").click();
  await expect(page.locator("#storage-page-info")).toHaveText("Page 3 of 3");
  await page.getByRole("button", { name: "Delete archive n1.pdf (Code0001)", exact: true }).click();
  await page.locator("#storage-delete-confirm").click();
  await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
  await expect(page.locator("#storage-page-info")).toHaveText("Page 2 of 2");
  await expect(page.locator(".storage-archive")).toHaveCount(10);
});

test("Storage sorts missing values last in both directions and keeps server order for ties", async ({ page }) => {
  await routeArchives(page, [
    archiveRow(5, { created_at: null, object_count: 1 }),
    archiveRow(4, { total_bytes: 200, object_count: 9 }),
    archiveRow(3, { total_bytes: 100, object_count: 5 }),
    archiveRow(2, { total_bytes: 0, object_count: 0, incomplete: true }),
    archiveRow(1, { total_bytes: 100, object_count: 5 }),
  ]);
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-name")).toHaveCount(5);
  expect(await archiveNames(page)).toEqual(["n4.pdf", "n3.pdf", "n2.pdf", "n1.pdf", "n5.pdf"]);
  await clickColumn(page, "Created");
  expect(await archiveNames(page)).toEqual(["n1.pdf", "n2.pdf", "n3.pdf", "n4.pdf", "n5.pdf"]);
  await clickColumn(page, "Size");
  expect(await archiveNames(page)).toEqual(["n3.pdf", "n1.pdf", "n5.pdf", "n4.pdf", "n2.pdf"]);
  await clickColumn(page, "Size");
  expect(await archiveNames(page)).toEqual(["n5.pdf", "n4.pdf", "n3.pdf", "n1.pdf", "n2.pdf"]);
  await clickColumn(page, "Files");
  expect(await archiveNames(page)).toEqual(["n5.pdf", "n3.pdf", "n1.pdf", "n4.pdf", "n2.pdf"]);
  await clickColumn(page, "Files");
  expect(await archiveNames(page)).toEqual(["n4.pdf", "n3.pdf", "n1.pdf", "n5.pdf", "n2.pdf"]);
});

test("Storage File sort falls back to the job code and ignores case", async ({ page }) => {
  await routeArchives(page, [
    archiveRow(9, { filename: undefined, job_id: "Bbbb1234" }),
    archiveRow(8, { filename: "REPORT.pdf" }),
    archiveRow(7, { filename: "report.pdf" }),
    archiveRow(6, { filename: undefined, job_id: "Aaaa1234" }),
  ]);
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-name")).toHaveCount(4);
  await clickColumn(page, "File");
  expect(await archiveNames(page)).toEqual(["Aaaa1234", "Bbbb1234", "REPORT.pdf", "report.pdf"]);
});

test("Storage Created sorts by instant, not by timestamp text", async ({ page }) => {
  await routeArchives(page, [archiveRow(7), archiveRow(6, { created_at: "2026-09-26T14:06:00+02:00" })]);
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-name")).toHaveCount(2);
  expect(await archiveNames(page)).toEqual(["n7.pdf", "n6.pdf"]);
});

test("Storage sort survives a reload and its header shows the direction", async ({ page }) => {
  await routeArchives(page, rowsOf(3));
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-archive")).toHaveCount(3);
  await clickColumn(page, "File");
  const icon = () => page.locator('[data-sort="file"] svg path').getAttribute("d");
  const ascending = await icon();
  await clickColumn(page, "File");
  expect(await icon()).not.toBe(ascending);
  await page.locator("#storage-refresh").click();
  await expect(page.locator(".storage-archive")).toHaveCount(3);
  expect(await archiveNames(page)).toEqual(["n3.pdf", "n2.pdf", "n1.pdf"]);
  await expect(page.locator('#storage-archives th[aria-sort="descending"]')).toContainText("File");
});

test("Storage invalid dates clear loaded rows and the open archive", async ({ page }) => {
  const rows = rowsOf(3);
  await routeArchives(page, rows);
  await page.route("**/storage/archive?**", route => route.fulfill({ json: { prefix: rows[0].prefix, files: [] } }));
  await openHealthyConsole(page, "#storage");
  await page.locator("#storage-date-to").fill("2026-10-11");
  await expect(page.locator(".storage-archive")).toHaveCount(3);
  await page.getByRole("button", { name: "n3.pdf", exact: true }).click();
  await expect(openedPrefix(page)).toHaveText(rows[0].prefix);
  await page.locator("#storage-date-from").fill("2026-10-12");
  await expect(page.locator("#storage-archives tbody")).toHaveText("From date must not be after To date.");
  await expect(page.locator("#storage-detail")).toHaveText("Select an archive to view its files.");
});

test("Storage invalid dates abort the running load", async ({ page }) => {
  const rows = rowsOf(3);
  await routeArchives(page, rows);
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-archive")).toHaveCount(3);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const replies = [];
  await page.route("**/storage/archives?**", route => {
    replies.push(held.then(() => route.fulfill({ json: { items: rows, next_cursor: null } })).catch(() => {}));
  });
  const table = page.locator("#storage-archives tbody"), invalid = "From date must not be after To date.";
  await page.locator("#storage-date-from").fill("2026-10-12");
  await expect(table).toHaveText("Loading saved archives…");
  await page.locator("#storage-date-to").fill("2026-10-11");
  await expect(table).toHaveText(invalid);
  release();
  await Promise.all(replies);
  await nextFrame(page);
  await expect(page.locator("#storage-status")).toHaveText(invalid);
  await expect(page.locator(".storage-archive")).toHaveCount(0);
});

test("Storage Refresh stays disabled during a load", async ({ page }) => {
  let release, reached;
  const held = new Promise(resolve => { release = resolve; });
  const second = new Promise(resolve => { reached = resolve; });
  await page.route("**/storage/archives?**", async route => {
    if (!new URL(route.request().url()).searchParams.get("cursor")) {
      return route.fulfill({ json: { items: [archiveRow(2)], next_cursor: "two" } });
    }
    reached(); await held;
    return route.fulfill({ json: { items: [archiveRow(1)], next_cursor: null } });
  });
  await openHealthyConsole(page, "#storage");
  await second;
  await expect(page.locator("#storage-refresh")).toBeDisabled();
  release();
  await expect(page.locator("#storage-refresh")).toBeEnabled();
});

test("Storage says so when it is empty and does not stay busy", async ({ page }) => {
  await openHealthyConsole(page, "#storage");
  await expect(page.locator("#storage-archives tbody")).toHaveText("No saved archives found. Run Parse or OCR, then refresh.");
  await expect(page.locator("#storage-archives")).not.toHaveAttribute("aria-busy");
});

test("Storage Enter in the search does not reload the list", async ({ page }) => {
  const calls = await routeArchives(page, [ROW]);
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-archive")).toHaveCount(1);
  const loads = calls.length;
  await page.locator("#storage-search").press("Enter");
  await expect(page.locator("#storage-archives")).not.toHaveAttribute("aria-busy");
  expect(calls).toHaveLength(loads);
});

test("Storage deletion drops the row without reloading the list", async ({ page }) => {
  const calls = await storageFixture(page, { items: [archiveRow(2), archiveRow(1)] });
  await openHealthyConsole(page, "#storage");
  await expect(page.locator(".storage-archive")).toHaveCount(2);
  const loads = () => calls.filter(call => call.path.endsWith("/archives")).length;
  const before = loads();
  await page.getByRole("button", { name: "Delete archive n2.pdf (Code0002)", exact: true }).click();
  await page.locator("#storage-delete-confirm").click();
  await expect(page.locator("#storage-status")).toHaveText("Archive deleted.");
  expect(await archiveNames(page)).toEqual(["n1.pdf"]);
  expect(loads()).toBe(before);
  await expect(page.locator("#storage-refresh")).toBeFocused();
});

test("Storage Refresh stays on the page unless an action changes it meanwhile", async ({ page }) => {
  await routeArchives(page, Array.from({ length: 35 }, (_, i) => archiveRow(35 - i)), 50);
  await openHealthyConsole(page, "#storage");
  const info = page.locator("#storage-page-info");
  await page.locator("#storage-next").click();
  await page.locator("#storage-next").click();
  await expect(info).toHaveText("Page 3 of 4");
  await page.locator("#storage-refresh").click();
  await expect(page.locator(".storage-archive")).toHaveCount(10);
  await expect(info).toHaveText("Page 3 of 4");

  let release, started;
  const held = new Promise(resolve => { release = resolve; });
  const pending = new Promise(resolve => { started = resolve; });
  await page.route("**/storage/archives?**", async route => {
    started(); await held;
    await route.fallback();
  });
  await page.locator("#storage-refresh").click();
  await pending;
  await page.getByRole("button", { name: "Size", exact: true }).click();
  release();
  await expect(page.locator(".storage-archive")).toHaveCount(10);
  await expect(info).toHaveText("Page 1 of 4");
});

test("Storage reports load progress and sums archives still being written", async ({ page }) => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/storage/archives?**", async route => {
    if (!new URL(route.request().url()).searchParams.get("cursor")) {
      return route.fulfill({ json: { items: [archiveRow(2)], next_cursor: "more", skipped_archives: 2 } });
    }
    await held;
    return route.fulfill({ json: { items: [archiveRow(1)], next_cursor: null, skipped_archives: 1 } });
  });
  await openHealthyConsole(page, "#storage");
  await expect(page.locator("#storage-status")).toHaveText("Loading saved archives… 1");
  release();
  await expect(page.locator("#storage-status")).toHaveText("3 archives still being written are not shown yet.");
  expect(await archiveNames(page)).toEqual(["n2.pdf", "n1.pdf"]);
});

test("Storage keeps the scroll and an unobscured header when rows are rebuilt at phone width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await routeArchives(page, Array.from({ length: 12 }, (_, i) => archiveRow(12 - i, { input_key: `n${12 - i}.pdf/input.pdf` })));
  await openHealthyConsole(page, "#storage");
  const wrap = page.locator("#storage-archives .storage-table-wrap"), size = page.getByRole("button", { name: "Size", exact: true });
  await expect(page.locator(".storage-archive")).toHaveCount(10);
  await wrap.evaluate(element => { element.scrollLeft = element.scrollWidth; });
  const before = await wrap.evaluate(element => element.scrollLeft);
  expect(before).toBeGreaterThan(0);
  await size.click();
  await expect(size).toBeFocused();
  expect(await wrap.evaluate(element => element.scrollLeft)).toBe(before);
  expect((await size.boundingBox()).height).toBeGreaterThanOrEqual(44);
  const unobscured = header => header.evaluate(button => {
    const box = button.getBoundingClientRect();
    return document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)?.closest("button") === button;
  });
  expect(await unobscured(size)).toBe(true);
  await page.locator("#storage-next").click();
  expect(await wrap.evaluate(element => element.scrollLeft)).toBe(before);
  // A pointer sort leaves the scroll alone even where a keyboard one would move it.
  await wrap.evaluate(element => { element.scrollLeft = 0; });
  await clickColumn(page, "File");
  expect(await wrap.evaluate(element => element.scrollLeft)).toBe(0);
  // Tabbing through the headers must not stop under the sticky Action column.
  await wrap.focus();
  for (const name of ["File", "Output", "Created", "Files", "Size"]) {
    await page.keyboard.press("Tab");
    const header = page.getByRole("button", { name, exact: true });
    await expect(header).toBeFocused();
    expect(await unobscured(header), name).toBe(true);
  }
});

test("Storage keeps showing a load in progress while searching, sorting or re-paging", async ({ page }) => {
  let release, started;
  const held = new Promise(resolve => { release = resolve; });
  const pending = new Promise(resolve => { started = resolve; });
  await page.route("**/storage/archives?**", async route => {
    started(); await held;
    await route.fulfill({ json: { items: [ROW], next_cursor: null } });
  });
  await openHealthyConsole(page, "#storage");
  await pending;
  const table = page.locator("#storage-archives tbody"), loading = "Loading saved archives…";
  await expect(table).toHaveText(loading);
  await page.locator("#storage-search").fill("missing");
  await expect(table).toHaveText(loading);
  await expect(page.locator("#storage-search-status")).toBeEmpty();
  await page.locator("#storage-search").fill("");
  await page.locator("#storage-page-size").selectOption("20");
  await expect(table).toHaveText(loading);
  await page.getByRole("button", { name: "Size", exact: true }).click();
  await expect(table).toHaveText(loading);
  release();
  await expect(page.locator(".storage-archive")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Size", exact: true })).toBeFocused();
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

test("Storage has a parameter reference like Parse and OCR", async ({ page }, testInfo) => {
  await storageFixture(page);
  await openHealthyConsole(page, "#storage");
  const reference = page.locator("#panel-storage details.reference");
  await expect(reference.locator("summary")).toHaveText("Parameter reference");
  await expect(page.locator("#storage-reference")).toBeHidden();
  await reference.locator("summary").click();
  await expect(page.locator("#storage-reference .param-name")).toContainText([
    "Output type", "From / To (UTC)", "Search archives", "Refresh", "Rows per page",
    "Archive list", "Actions", "Files and preview", "Delete archive", "Availability",
  ]);
  await expect(page.locator("#storage-reference")).toContainText("unknown type appear only under All outputs");
  await expect(page.locator("#storage-reference")).toContainText("From / To (UTC)");
  await expect(page.locator("#storage-reference")).toContainText("across every page");
  await expect(page.locator("#storage-reference")).toContainText("Select a column header");
  await expect(page.locator("#storage-reference")).not.toContainText("Changing the size reloads");
  await expect(page.locator("#storage-reference")).toContainText("A long list takes longer");
  await expect(page.locator("#storage-reference")).not.toContainText("three icon buttons");
  await page.screenshot({ path: testInfo.outputPath("storage-reference.png") });
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
  await expect(page.locator("#storage-archives tbody")).toHaveText("From date must not be after To date.");
  await expect(page.locator("#storage-refresh")).toBeEnabled();
  await expect(page.locator("#storage-archives")).not.toHaveAttribute("aria-busy");
  await expect(page.locator("#storage-prev")).toBeDisabled();
  await expect(page.locator("#storage-next")).toBeDisabled();
  release();
});
