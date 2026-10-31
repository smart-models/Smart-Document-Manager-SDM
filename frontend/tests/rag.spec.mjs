import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { test, expect, openHealthyConsole, withApi } from "./fixtures.mjs";
import { openRagWithFixture } from "./rag-workflows.mjs";

test("Execute waits for a cut and saves the completed partition", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const fixture = JSON.parse(await readFile(new URL("./fixtures/rag-split.json", import.meta.url), "utf8"))["table-over-maximum"];
  await page.route("**/storage/rag/split", async route => {
    await gate;
    await route.fulfill({ json: { fragments: fixture.fragments, warnings: [] } });
  });
  let received;
  await page.route("**/storage/rag/refine", route => {
    received = route.request().postDataJSON();
    return route.fulfill({ json: { prefix: "fixture-table-over-maximum/", chunk_count: received.groups.length,
      file: { name: "json_rag_fixture_001.json", key: "fixture-table-over-maximum/json_rag_fixture_001.json", size: 10 } } });
  });
  await page.getByRole("button", { name: "Cut group 1", exact: true }).click();
  await expect(page.locator("#rag-execute")).toBeDisabled();
  expect(received).toBeUndefined();
  release();
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(5);
  await expect(page.locator("#rag-execute")).toBeEnabled();
  await page.locator("#rag-execute").click();
  await expect(page.locator("#rag-execution-state")).toContainText("Saved");
  expect(received.groups).toHaveLength(5);
});

for (const uncertain of [false, true]) test(`modified requests get a new identity after ${uncertain ? "an uncertain" : "a successful"} save`, async ({ page, expectedConsoleErrors }) => {
  if (uncertain) expectedConsoleErrors.push(/Failed to load resource/);
  await openRagWithFixture(page, "table-over-maximum");
  const requests = [];
  await page.route("**/storage/rag/refine", route => {
    requests.push(route.request().postDataJSON());
    if (uncertain && requests.length === 1) return route.abort();
    return route.fulfill({ json: { prefix: "fixture-table-over-maximum/", chunk_count: requests.at(-1).groups.length,
      file: { name: "json_rag_fixture_001.json", key: "fixture-table-over-maximum/json_rag_fixture_001.json", size: 10 } } });
  });
  const execute = page.locator("#rag-execute");
  await execute.click();
  await expect(page.locator("#rag-execution-state")).toContainText(uncertain ? "Save failed" : "Saved");
  await page.getByRole("button", { name: "Merge group 2 with next", exact: true }).click();
  await execute.click();
  await expect.poll(() => requests.length).toBe(2);
  await expect(execute).toBeEnabled();
  expect(requests[1].execution_id).not.toBe(requests[0].execution_id);
  expect(requests[1].groups).not.toEqual(requests[0].groups);
  await execute.click();
  await expect.poll(() => requests.length).toBe(3);
  expect(requests[2]).toEqual(requests[1]);
});

test("an obsolete split cannot invalidate the new preflight", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route("**/storage/rag/split", async route => {
    await gate;
    await route.fulfill({ status: 503, json: { detail: { code: "rag_tokenizer_unavailable" } } });
  });
  await page.getByRole("button", { name: "Cut group 1", exact: true }).click();
  await page.locator("#rag-preflight-min").fill("500");
  await page.locator("#rag-preflight-run").click();
  const response = page.waitForResponse("**/storage/rag/split");
  release(); await response;
  await expect(page.locator("#rag-preflight-error")).toBeEmpty();
  await expect(page.getByRole("button", { name: "Cut group 1", exact: true })).toBeVisible();
  await expect(page.locator("#rag-execute")).toBeEnabled();
});

test("a late saved-file preview cannot replace a new plan preview", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  const key = "fixture-table-over-maximum/json_rag_fixture_001.json";
  await page.route("**/storage/rag/refine", route => route.fulfill({ json: {
    prefix: "fixture-table-over-maximum/", chunk_count: 3,
    file: { name: "json_rag_fixture_001.json", key, size: 10 } } }));
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const pending = new Promise(resolve => { entered = resolve; });
  await page.route("**/storage/file?**", async route => {
    if (new URL(route.request().url()).searchParams.get("key") !== key) return route.fallback();
    entered(); await gate;
    await route.fulfill({ json: { chunks: [] } });
  });
  await page.locator("#rag-execute").click(); await pending;
  await page.locator("#rag-preflight-run").click();
  const response = page.waitForResponse(r => r.url().includes("/storage/file?") && new URL(r.url()).searchParams.get("key") === key);
  release(); await response;
  await expect(page.locator("#rag-execution-json-summary")).toHaveText("aggregated JSON");
});

for (const stage of ["url", "click"]) test(`RAG reports a local download failure at ${stage} and cleans up`, async ({ page }) => {
  await openRagWithFixture(page, "balanced");
  await page.evaluate(stage => {
    window.revoked = [];
    window.originalTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) => delay === 10000 ? (callback(...args), 0) : window.originalTimeout(callback, delay, ...args);
    if (stage === "url") URL.createObjectURL = () => { throw new Error("synthetic URL failure"); };
    else {
      URL.createObjectURL = () => "blob:synthetic";
      URL.revokeObjectURL = url => window.revoked.push(url);
      HTMLAnchorElement.prototype.click = () => { throw new Error("synthetic click failure"); };
    }
  }, stage);
  await page.getByRole("button", { name: /^Download json_rag.json/ }).click();
  await expect(page.locator("#rag-storage-file-status")).toContainText("Download of json_rag.json failed");
  await expect(page.getByRole("button", { name: /^Download json_rag.json/ })).toBeEnabled();
  await expect(page.locator('a[download="json_rag.json"]')).toHaveCount(0);
  if (stage === "click") expect(await page.evaluate(() => window.revoked)).toEqual(["blob:synthetic"]);
});

test.beforeEach(async ({ expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/Failed to load resource: the server responded with a status of 503/);
});

function savedArchive(index) {
  return { prefix: `report-${index}.pdf__20260927T120000000000Z__job${index}/`,
    filename: `report-${index}.pdf`, job_id: `job${index}`, kind: "parse",
    created_at: "2026-09-27T12:00:00Z", partial_ocr: index === 0 };
}

function savedFiles(archive) {
  return { manifest: { source: { original_name: `Original report ${archive.job_id}.pdf` } }, files: [
    { key: archive.prefix + "json_rag.json", name: "json_rag.json", size: 320, content_type: "application/json" },
    { key: archive.prefix + "json_ocr.json", name: "json_ocr.json", size: 800 },
    { key: archive.prefix + "json_chunks.json", name: "json_chunks.json", size: 400 },
  ] };
}

test("RAG lists JSON RAG across bucket pages with parent files, search and existing table actions", async ({ page }, testInfo) => {
  const archives = Array.from({ length: 23 }, (_, index) => savedArchive(index));
  const calls = [], content = JSON.stringify({ chunks: [{ id: 1, page: 1,
    text: "<img src=x onerror=alert(1)>" }],
  metadata: { document: "report.pdf", pages: 7, avg_tokens: 128.456 } });
  await page.route("**/storage/**", route => {
    const request = route.request(), url = new URL(request.url());
    calls.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), headers: request.headers() });
    if (url.pathname.endsWith("/archives")) {
      // An empty server page with a cursor must not end the inventory scan.
      const cursor = url.searchParams.get("cursor");
      return route.fulfill({ json: cursor === "last"
        ? { items: archives.slice(12), next_cursor: null }
        : cursor === "middle" ? { items: [], next_cursor: "last" }
          : { items: [...archives.slice(0, 12), { prefix: "incomplete/", incomplete: true }], next_cursor: "middle" } });
    }
    if (url.pathname.endsWith("/archive")) {
      const archive = archives.find(item => item.prefix === url.searchParams.get("prefix"));
      expect(archive).toBeTruthy();
      return route.fulfill({ json: savedFiles(archive) });
    }
    return route.fulfill({ contentType: "application/json", body: content });
  });
  await page.route("**/parse", () => { throw new Error("Listing RAG must not rerun parsing"); });
  await page.route("**/chunk", () => { throw new Error("Listing RAG must not create chunks"); });
  await openHealthyConsole(page);
  await withApi(page, api => api.saveToken("rag-browser-test-key"));
  expect(calls).toHaveLength(0);
  await page.locator("#tab-rag").click();
  const table = page.locator("#rag-storage-files");
  await expect(page.locator("#rag-storage-status")).toBeEmpty();
  await expect(table.locator("tbody tr")).toHaveCount(10);
  await expect(page.locator("#rag-storage-page-info")).toHaveText("Page 1 · 10 of 23 files");
  await expect(table.getByRole("columnheader")).toHaveText(["File JSON RAG", "Id", "Parent file", "Created", "Size", "Action"]);
  await expect(table.locator("tbody tr").first()).toContainText("Original report job0.pdf");
  await expect(table.locator("tbody tr").first().locator(".storage-id-cell")).toHaveText("job0");
  await expect(table.locator("tbody tr").first()).toContainText("Partial OCR");
  await expect(table).not.toContainText("json_ocr.json");
  await expect(table).not.toContainText("json_chunks.json");
  expect(calls.some(call => call.path === "/storage/file")).toBe(false);
  await page.screenshot({ path: testInfo.outputPath("rag-inventory-desktop.png"), fullPage: true });
  await page.locator("#rag-storage-next").click();
  await expect(table.locator("tbody tr")).toHaveCount(10);
  await expect(page.locator("#rag-storage-page-info")).toHaveText("Page 2 · 10 of 23 files");
  await page.locator("#rag-storage-next").click();
  await expect(table.locator("tbody tr")).toHaveCount(3);
  await expect(page.locator("#rag-storage-page-info")).toHaveText("Page 3 · 3 of 23 files");
  await expect(page.locator("#rag-storage-next")).toBeDisabled();
  await page.locator("#rag-storage-search").fill("Original report job22");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await table.getByRole("button", { name: /^Preview and select / }).click();
  await expect(page.locator("#rag-storage-file-status")).toBeEmpty();
  await expect(table.getByRole("button", { name: /^Preview and select / })).toBeFocused();
  await expect(page.locator("#rag-selected-json-details")).toBeVisible();
  await expect(page.locator("#rag-selected-json-summary")).toHaveText("json_rag.json");
  // toHaveText ignores text-transform: check the rendered case too.
  await expect(page.locator("#rag-selected-json-summary .literal")).toHaveCSS("text-transform", "none");
  await expect(page.locator("#rag-source-badges .metric-label")).toHaveText([
    "parent file", "raw chunks", "avg tokens", "pages", "tables", "code blocks",
  ]);
  await expect(page.locator("#rag-source-badges .metric-value")).toHaveText([
    "Original report job22.pdf", "1", "128.46", "7", "no", "no",
  ]);
  await expect(page.locator("#rag-selected-json-details pre")).toBeHidden();
  await page.locator("#rag-selected-json-details summary").click();
  await expect(page.locator("#rag-selected-json-details pre")).toContainText('"chunks": [');
  await expect(page.locator("#rag-selected-json")).toContainText("<img src=x onerror=alert(1)>");
  await expect(page.locator("#rag-selected-json img")).toHaveCount(0);
  const pending = page.waitForEvent("download");
  await table.getByRole("button", { name: /^Download / }).click();
  const download = await pending;
  expect(download.suggestedFilename()).toBe("json_rag.json");
  expect(await readFile(await download.path(), "utf8")).toBe(content);
  expect(calls.every(call => call.headers.authorization === "Bearer rag-browser-test-key")).toBe(true);
  expect(calls.filter(call => call.path === "/storage/file").map(call => call.query.preview)).toEqual(["false", "false"]);
  await page.setViewportSize({ width: 390, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(table.getByRole("button", { name: /^Download / })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("rag-inventory-mobile.png"), fullPage: true });
  const tableBounds = await table.boundingBox(), optionsBounds = await page.locator("#rag-root").boundingBox();
  expect(tableBounds.y + tableBounds.height).toBeLessThan(optionsBounds.y);
});

test("RAG lists refined JSON RAG files beside their source and selects them", async ({ page }) => {
  const archive = savedArchive(1);
  const refinedName = "json_rag_job1_001.json";
  const refined = { key: archive.prefix + refinedName, name: refinedName, size: 420, content_type: "application/json" };
  const aggregated = { chunks: [{ id: 1, text: "Aggregated text", token_count: 220, page: 1 }],
    metadata: { document: "report-1.pdf", n_chunks: 1, pages: 3, avg_tokens: 220 } };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: {
    manifest: { source: { original_name: "Original report job1.pdf" } },
    files: [...savedFiles(archive).files, refined],
  } }));
  await page.route("**/storage/file?**", route => route.fulfill({ json: aggregated }));
  await openHealthyConsole(page, "#rag");
  const table = page.locator("#rag-storage-files");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(table.locator("tbody tr").first()).toContainText("json_rag.json");
  await expect(table.locator("tbody tr").nth(1)).toContainText(refinedName);
  await expect(table).not.toContainText("json_ocr.json");
  // The JSON type filter splits originals from refined artifacts.
  await page.locator("#rag-storage-kind").selectOption("refined");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.locator("tbody tr").first()).toContainText(refinedName);
  await page.locator("#rag-storage-kind").selectOption("original");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.locator("tbody tr").first()).toContainText("json_rag.json");
  await expect(table).not.toContainText(refinedName);
  await page.locator("#rag-storage-kind").selectOption("");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await table.locator("tbody tr").nth(1).getByRole("button", { name: /^Preview and select / }).click();
  await expect(page.locator("#rag-selected-json-summary")).toHaveText(refinedName);
  await expect(page.locator("#rag-source-badges .metric-label")).toHaveText([
    "parent file", "raw chunks", "avg tokens", "pages", "tables", "code blocks",
  ]);
  await expect(page.locator("#rag-source-badges .metric-value")).toHaveText([
    "Original report job1.pdf", "1", "220", "3", "no", "no",
  ]);
});

test("refined JSON RAG files can be deleted from the table after confirmation", async ({ page }) => {
  const archive = savedArchive(1);
  const refinedName = "json_rag_job1_001.json";
  const refined = { key: archive.prefix + refinedName, name: refinedName, size: 420, content_type: "application/json" };
  const aggregated = { chunks: [{ id: 1, text: "Aggregated text", token_count: 220, page: 1 }],
    metadata: { document: "report-1.pdf", n_chunks: 1 } };
  let deleted = null;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: {
    manifest: { source: { original_name: "Original report job1.pdf" } },
    files: [...savedFiles(archive).files, refined],
  } }));
  await page.route("**/storage/file?**", route => {
    const request = route.request();
    if (request.method() === "DELETE") {
      deleted = { url: request.url(), body: request.postDataJSON() };
      return route.fulfill({ json: { deleted_objects: 1 } });
    }
    return route.fulfill({ json: aggregated });
  });
  await openHealthyConsole(page, "#rag");
  const table = page.locator("#rag-storage-files");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  // Only the refined artifact is removable: the source row has no trash.
  await expect(table.locator("tbody tr").first().getByRole("button", { name: /^Delete / })).toHaveCount(0);
  await table.locator("tbody tr").nth(1).getByRole("button", { name: /^Preview and select / }).click();
  await expect(page.locator("#rag-selected-json-summary")).toHaveText(refinedName);
  await table.locator("tbody tr").nth(1).getByRole("button", { name: /^Delete / }).click();
  const dialog = page.locator("#rag-storage-confirm");
  await expect(dialog).toBeVisible();
  await expect(page.locator("#rag-storage-confirm-description")).toContainText(refinedName);
  await page.locator("#rag-storage-confirm-cancel").click();
  await expect(dialog).toBeHidden();
  expect(deleted).toBeNull();
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await table.locator("tbody tr").nth(1).getByRole("button", { name: /^Delete / }).click();
  await page.locator("#rag-storage-confirm-delete").click();
  await expect(page.locator("#rag-storage-file-status")).toBeEmpty();
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(page.locator("#rag-selected")).toBeHidden();
  expect(deleted.body).toEqual({ confirm: refinedName });
  expect(new URL(deleted.url).searchParams.get("key")).toBe(refined.key);
});

test("RAG reports unreadable archives as a partial listing and refresh recovers", async ({ page }) => {
  const archive = savedArchive(1);
  let failing = true;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive, savedArchive(2)], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => {
    const prefix = new URL(route.request().url()).searchParams.get("prefix");
    if (prefix === archive.prefix && failing) return route.fulfill({ status: 503, json: { detail: { code: "storage_unavailable" } } });
    return route.fulfill({ json: savedFiles(prefix === archive.prefix ? archive : savedArchive(2)) });
  });
  await openHealthyConsole(page, "#rag");
  await expect(page.locator("#rag-storage-status")).toContainText("Listing incomplete: 1 archives could not be read.");
  await expect(page.locator("#rag-storage-files tbody tr")).toHaveCount(1);
  failing = false;
  await page.locator("#rag-storage-refresh").click();
  await expect(page.locator("#rag-storage-status")).toBeEmpty();
  await expect(page.locator("#rag-storage-files tbody tr")).toHaveCount(2);
});

test("RAG filters saved files by inclusive UTC dates and rejects a reversed range", async ({ page }) => {
  const archives = ["2026-09-25", "2026-09-27", "2026-09-29"].map((date, index) => ({
    ...savedArchive(index), created_at: `${date}T12:00:00Z`,
  }));
  const listings = [];
  await page.route("**/storage/archives?**", route => {
    const params = new URL(route.request().url()).searchParams;
    listings.push(Object.fromEntries(params));
    const from = params.get("date_from"), to = params.get("date_to");
    return route.fulfill({ json: { items: archives.filter(archive => {
      const date = archive.created_at.slice(0, 10);
      return (!from || date >= from) && (!to || date <= to);
    }), next_cursor: null } });
  });
  await page.route("**/storage/archive?**", route => {
    const prefix = new URL(route.request().url()).searchParams.get("prefix");
    return route.fulfill({ json: savedFiles(archives.find(archive => archive.prefix === prefix)) });
  });
  await openHealthyConsole(page, "#rag");
  const rows = page.locator("#rag-storage-files tbody tr");
  await expect(rows).toHaveCount(3);
  await page.locator("#rag-storage-date-from").fill("2026-09-27");
  await expect(rows).toHaveCount(2);
  await page.locator("#rag-storage-date-to").fill("2026-09-27");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("job1");
  expect(listings.at(-1)).toMatchObject({ date_from: "2026-09-27", date_to: "2026-09-27" });
  await page.locator("#rag-storage-search").fill("job0");
  await expect(page.locator("#rag-storage-files")).toContainText("No matching JSON RAG files.");
  await page.locator("#rag-storage-search").fill("");
  const beforeInvalid = listings.length;
  await page.locator("#rag-storage-date-from").fill("2026-09-28");
  await expect(page.locator("#rag-storage-status")).toHaveText("From date must not be after To date.");
  await expect(page.locator("#rag-storage-files")).toContainText("From date must not be after To date.");
  expect(listings).toHaveLength(beforeInvalid);
  await page.locator("#rag-storage-date-to").fill("2026-09-30");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("job2");
  expect(listings.at(-1)).toMatchObject({ date_from: "2026-09-28", date_to: "2026-09-30" });
});

test("RAG distinguishes disabled storage from an empty inventory", async ({ page }) => {
  await page.route("**/storage/archives?**", route => route.fulfill({ status: 503, json: { detail: { code: "storage_browser_disabled" } } }));
  await openHealthyConsole(page, "#rag");
  await expect(page.locator("#rag-storage-status")).toContainText("Archive browsing is not enabled");
  await expect(page.locator("#rag-storage-files")).not.toContainText("No JSON RAG files found");
  await expect(page.locator("#rag-source-badges .metric")).toHaveCount(0);
  await expect(page.locator("#panel-rag input[type=file]")).toHaveCount(0);
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.locator("#rag-storage-refresh").click();
  await expect(page.locator("#rag-storage-files")).toContainText("No JSON RAG files found");
});

test("RAG discards an old listing after tab navigation and resolves legacy parents without inventing a filename", async ({ page }) => {
  const archive = savedArchive(0);
  let release, started = false;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route("**/storage/archives?**", async route => {
    started = true; await pending;
    await route.fulfill({ json: { items: [archive], next_cursor: null } });
  });
  await openHealthyConsole(page, "#rag");
  await expect.poll(() => started).toBe(true);
  await page.locator("#tab-parse").click();
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [{ ...archive, filename: null }], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: { ...savedFiles(archive), manifest: {} } }));
  await page.locator("#tab-rag").click();
  await expect(page.locator("#rag-storage-status")).toBeEmpty();
  release();
  await expect(page.locator("#rag-storage-files")).toContainText("Original filename unavailable");
  await expect(page.locator("#rag-storage-files tbody tr")).toHaveCount(1);
});

test("section 02 receives the selected stored JSON RAG without a file picker or OCR request", async ({ page }) => {
  const archive = savedArchive(1);
  const rag = { chunks: [
    { id: 1, text: "Stored first chunk\n\nA | B\n--- | ---\n1 | 2", page: 2, page_header: "Header" },
    { id: 2, text: "```py\nStored second chunk\n```", page: 3 }],
  metadata: { document: "report.pdf", pages: 3, avg_tokens: 41.567 } };
  let reads = 0;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => {
    reads++;
    expect(new URL(route.request().url()).searchParams.get("preview")).toBe("false");
    return route.fulfill({ json: rag });
  });
  await page.route("**/parse", () => { throw new Error("Selecting RAG must not rerun parsing"); });
  await page.route("**/chunk", () => { throw new Error("Selecting RAG must not rechunk raw data"); });
  await openHealthyConsole(page, "#rag");
  await expect(page.locator("#rag-source-badges")).toBeEmpty();
  await expect(page.locator("#panel-rag input[type=file]")).toHaveCount(0);
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await expect(page.locator("#rag-source-badges .metric-label")).toHaveText([
    "parent file", "raw chunks", "avg tokens", "pages", "tables", "code blocks",
  ]);
  await expect(page.locator("#rag-source-badges .metric-value")).toHaveText([
    "Original report job1.pdf", "2", "41.57", "3", "yes", "yes",
  ]);
  const jsonPreview = page.locator("#rag-selected-json-details");
  await expect(jsonPreview).toBeVisible();
  await expect(jsonPreview.locator("summary")).toHaveText("json_rag.json");
  await expect(jsonPreview.locator("pre")).toBeHidden();
  await expect(jsonPreview).not.toHaveAttribute("open");
  await jsonPreview.locator("summary").click();
  await expect(page.locator("#rag-selected-json")).toHaveJSProperty("textContent", JSON.stringify(rag, null, 2));
  await expect(jsonPreview.locator("pre")).toBeVisible();
  await expect(jsonPreview.locator("pre")).toContainText("Stored second chunk");
  // The stored-file preview lives inside section 02, before the preflight form.
  expect(await page.locator("#rag-selected-json-details").evaluate(element =>
    element.closest("#rag-selected") !== null)).toBe(true);
  expect(reads).toBe(1);
  await page.locator("#tab-parse").click();
  await page.locator("#tab-rag").click();
  await expect(page.locator("#rag-source-badges .metric-value").first()).toHaveText("Original report job1.pdf");
  await expect(page.locator("#rag-storage-files .storage-file-choice")).toHaveAttribute("aria-pressed", "true");
  // A refresh that still lists the selected file keeps it; clearing only
  // happens when its row disappears from the listing.
  await page.locator("#rag-storage-date-to").fill("2026-09-26");
  await expect(page.locator("#rag-source-badges .metric-value").first()).toHaveText("Original report job1.pdf");
  await expect(page.locator("#rag-selected")).toBeVisible();
  await expect(page.locator("#rag-storage-files .storage-file-choice")).toHaveAttribute("aria-pressed", "true");
});

test("an invalid stored JSON RAG cannot replace a valid selection", async ({ page }) => {
  const archives = [savedArchive(1), savedArchive(2)];
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: archives, next_cursor: null } }));
  await page.route("**/storage/archive?**", route => {
    const prefix = new URL(route.request().url()).searchParams.get("prefix");
    return route.fulfill({ json: savedFiles(archives.find(archive => archive.prefix === prefix)) });
  });
  await page.route("**/storage/file?**", route => {
    const key = new URL(route.request().url()).searchParams.get("key");
    return route.fulfill({ json: key.startsWith(archives[0].prefix)
      ? { chunks: [{ id: 1, text: "Keep me", page: 1 }] }
      : { chunks: [{ id: 2, text: null }] } });
  });
  await openHealthyConsole(page, "#rag");
  const choices = page.locator("#rag-storage-files .storage-file-choice");
  await choices.nth(0).click();
  await page.locator("#rag-selected-json-details summary").click();
  await expect(page.locator("#rag-selected-json")).toContainText("Keep me");
  await choices.nth(1).click();
  await expect(page.locator("#rag-storage-file-status")).toContainText("not a valid JSON RAG document");
  await expect(page.locator("#rag-selected-json")).toContainText("Keep me");
  await expect(choices.nth(0)).toHaveAttribute("aria-pressed", "true");
});

test("preflight predicts range and page outcomes, previews payloads and invalidates changed settings", async ({ page }, testInfo) => {
  const archive = savedArchive(1);
  const value = { chunks: [200, 200, 200, 100].map((token_count, index) => ({
    id: index + 1, text: `Text ${index + 1}`, page: index < 3 ? 1 : 2, token_count,
    document: "report.pdf", page_header: "Shared header", footnote: `Note ${index + 1}`,
  })) };
  const original = JSON.stringify(value);
  let reads = 0;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => { reads++; return route.fulfill({ contentType: "application/json", body: original }); });
  await page.route("**/chunk", () => { throw new Error("Preflight uses saved token estimates"); });
  await page.route("**/parse", () => { throw new Error("Preflight must not rerun OCR"); });
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await expect(page.locator("#rag-preflight-min")).toHaveValue("512");
  await expect(page.locator("#rag-preflight-max")).toHaveValue("1024");
  await page.locator("#rag-preflight-min").fill("250");
  await page.locator("#rag-preflight-max").fill("400");
  await expect(page.locator("#rag-preflight-form select")).toHaveCount(0);
  // RAG inherits the actual PARSE checkbox appearance, including red checked state.
  const checkboxStyles = await page.evaluate(() => {
    const read = id => {
      const element = document.querySelector(id), css = getComputedStyle(element);
      // Hidden PARSE reports specified pixels, visible RAG reports layout pixels
      // quantized to 1/64px. Compare dimensions at a tenth of a CSS pixel.
      return [parseFloat(css.width).toFixed(1), parseFloat(css.height).toFixed(1), css.appearance, css.backgroundColor, css.borderColor,
        getComputedStyle(element, "::after").clipPath];
    };
    return { parse: read("#parse-out-rag"), table: read("#rag-keep-tables"), code: read("#rag-keep-code") };
  });
  expect(checkboxStyles.table).toEqual(checkboxStyles.parse);
  expect(checkboxStyles.code).toEqual(checkboxStyles.parse);
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-page-info")).toHaveText("1–3 of 3 groups");
  // Section 04 stays silent until an outcome exists, with the action last.
  await expect(page.locator("#rag-execution-state")).not.toContainText("Run preflight to prepare");
  await expect(page.locator("#rag-execution-root > *").last()).toHaveId("rag-execute");
  await expect(page.locator("#rag-preflight-counts .metric-label")).toHaveText([
    "Within range", "Below range", "Above range", "Unknown size",
  ]);
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["1", "2", "0", "0"]);
  await expect(page.locator("#rag-preflight-distribution .metric-label")).toHaveText([
    "total chunks", "min chunk tokens", "avg chunk tokens", "max chunk tokens",
  ]);
  await expect(page.locator("#rag-preflight-distribution .metric-value")).toHaveText(["3", "100", "233", "400"]);
  // Section 04 previews the aggregation as soon as the plan exists.
  await expect(page.locator("#rag-execution-json-details")).toBeVisible();
  await expect(page.locator("#rag-execution-json-summary")).toHaveText("aggregated JSON");
  // Labels stay uppercase; only file names are literal.
  await expect(page.locator("#rag-execution-json-summary")).toHaveCSS("text-transform", "uppercase");
  await expect(page.locator("#rag-execution-json-summary .literal")).toHaveCount(0);
  await page.locator("#rag-execution-json-details summary").click();
  await expect(page.locator("#rag-execution-json")).toContainText('"n_chunks": 3');
  await expect(page.locator("#rag-execution-json")).toContainText('"text": "Text 1"');
  await expect(page.locator("#rag-execution-json-note")).toContainText("The saved file adds execution metadata");
  await page.locator("#rag-preflight-groups").getByRole("button", { name: "Preview group 1", exact: true }).click();
  await expect(page.locator("#rag-preflight-detail > pre")).toHaveText("Text 1");
  await expect(page.locator("#rag-preflight-detail > h3")).toHaveText("Chunk id: 1");
  await expect(page.locator("#rag-preflight-detail > p")).toHaveCount(0);
  await page.locator("#rag-page-boundary").uncheck();
  await expect(page.locator("#rag-preflight-result")).toBeHidden();
  // A plan preview describes a plan that no longer exists.
  await expect(page.locator("#rag-execution-json-details")).toBeHidden();
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-page-info")).toHaveText("1–2 of 2 groups");
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["2", "0", "0", "0"]);
  await expect(page.locator("#rag-preflight-distribution .metric-value").first()).toHaveText("2");
  await expect(page.locator("#rag-execution-json-summary")).toHaveText("aggregated JSON");
  await page.locator("#rag-execution-json-details summary").click();
  await expect(page.locator("#rag-execution-json")).toContainText('"text": "Text 1\\n\\nText 2"');
  await page.locator("#rag-preflight-groups").getByRole("button", { name: "Preview group 1", exact: true }).click();
  await expect(page.locator("#rag-preflight-detail > pre")).toHaveText("Text 1\n\nText 2");
  await page.locator("#rag-preflight-detail summary").click();
  await expect(page.locator("#rag-preflight-detail details pre")).toContainText("Note 1");
  await expect(page.locator("#rag-preflight-detail details pre")).toContainText("Note 2");
  expect(reads).toBe(1);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("rag-preflight-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("rag-preflight-mobile.png"), fullPage: true });
  // The range is validated while typing, before preflight can be launched.
  await page.locator("#rag-preflight-min").fill("500");
  await expect(page.locator("#rag-preflight-error")).toContainText("greater than or equal to min token");
  await expect(page.locator("#rag-preflight-max")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#rag-preflight-run")).toBeDisabled();
  await expect(page.locator("#rag-preflight-result")).toBeHidden();
  await page.locator("#rag-preflight-min").fill("0");
  await expect(page.locator("#rag-preflight-error")).toContainText("at least 1");
  await expect(page.locator("#rag-preflight-min")).toHaveAttribute("aria-invalid", "true");
  await page.locator("#rag-preflight-min").fill("250");
  await expect(page.locator("#rag-preflight-error")).toBeEmpty();
  await expect(page.locator("#rag-preflight-run")).toBeEnabled();
  const pending = page.waitForEvent("download");
  await page.locator("#rag-storage-files").getByRole("button", { name: /^Download / }).click();
  expect(await readFile(await (await pending).path(), "utf8")).toBe(original);
});

test("preflight exposes unknown sizes, paginates all groups and keeps the selection across refreshes", async ({ page }) => {
  const archive = savedArchive(1);
  const value = { chunks: Array.from({ length: 23 }, (_, index) => ({
    id: index + 1, text: `Block ${index + 1}`, page: 1, ...(index ? { token_count: 900 } : {}),
  })), metadata: { failed_pages: [2] } };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({ json: value }));
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-min").fill("256");
  await page.locator("#rag-preflight-max").fill("512");
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["0", "0", "22", "1"]);
  await expect(page.locator("#rag-preflight-groups tbody tr").first()).toContainText("Unknown size");
  await expect(page.locator("#rag-preflight-groups").getByRole("columnheader")).toHaveText([
    "Chunk id", "Aggregated chunks", "Page", "Tokens", "Token range", "Action",
  ]);
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(10);
  await page.locator("#rag-preflight-next").click();
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(10);
  await expect(page.locator("#rag-preflight-page-info")).toHaveText("11–20 of 23 groups");
  await page.locator("#rag-preflight-next").click();
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(3);
  await expect(page.locator("#rag-preflight-page-info")).toHaveText("21–23 of 23 groups");
  await page.locator("#rag-preflight-page-size").selectOption("50");
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(23);
  await expect(page.locator("#rag-preflight-page-info")).toHaveText("1–23 of 23 groups");
  await expect(page.locator("#rag-preflight-next")).toBeDisabled();
  await page.locator("#rag-storage-refresh").click();
  // Refresh reloads the listing without dropping the selection or its plan.
  await expect(page.locator("#rag-preflight-result")).toBeVisible();
  await expect(page.locator("#rag-source-badges")).toContainText("Original report job1.pdf");
  await expect(page.locator("#rag-storage-files .storage-file-choice")).toHaveAttribute("aria-pressed", "true");
});

test("protection controls and forced pages expose conflicting settings in preflight", async ({ page }) => {
  const archive = savedArchive(1);
  const value = { chunks: ["```py", "x = 1", "y = 2", "```"].map((text, index) => ({
    id: index + 1, text, page: index < 2 ? 1 : 2, token_count: 100,
  })) };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({ json: value }));
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-min").fill("150");
  await page.locator("#rag-preflight-max").fill("250");
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["2", "0", "0", "0"]);
  await page.locator("#rag-page-boundary").uncheck();
  await expect(page.locator("#rag-preflight-result")).toBeHidden();
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["0", "0", "1", "0"]);
  await page.locator("#rag-keep-code").uncheck();
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["2", "0", "0", "0"]);
});

test("section 03 executes the reviewed plan and saves a named file beside its parent", async ({ page }) => {
  const archive = savedArchive(1);
  const original = JSON.stringify({ chunks: [
    { id: 4, text: "First", page: 1, token_count: 100, page_header: "Header" },
    { id: 5, text: "Second", page: 1, token_count: 120, footnote: "Note" },
  ], metadata: { document: "report-1.pdf", n_chunks: 2 } });
  const sourceHash = createHash("sha256").update(original).digest("hex");
  const refined = JSON.stringify({ chunks: [{ id: 1, text: "First\n\nSecond", token_count: 220 }],
    metadata: { document: "report-1.pdf", n_chunks: 1, aggregated: true } });
  let received;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({
    status: 200, contentType: "application/json", headers: { "X-OCR-Content-SHA256": sourceHash },
    body: new URL(route.request().url()).searchParams.get("key").endsWith("json_rag_job1_001.json") ? refined : original,
  }));
  await page.route("**/storage/rag/refine", async route => {
    received = route.request().postDataJSON();
    return route.fulfill({ json: {
      prefix: archive.prefix,
      file: { name: "json_rag_job1_001.json",
        key: `${archive.prefix}json_rag_job1_001.json`, size: 420, content_type: "application/json" },
      chunk_count: 1,
    } });
  });
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-min").fill("150");
  await page.locator("#rag-preflight-max").fill("250");
  await page.locator("#rag-preflight-run").click();
  const execute = page.locator("#rag-execute");
  await expect(execute).toBeEnabled();
  await execute.click();
  await expect(page.locator("#rag-execution-state")).toContainText("Saved json_rag_job1_001.json");
  await expect(page.locator("#rag-execution-json-details")).toBeVisible();
  await expect(page.locator("#rag-execution-json-summary")).toHaveText(
    "json_rag_job1_001.json");
  await expect(page.locator("#rag-execution-json-summary .literal")).toHaveCSS("text-transform", "none");
  await expect(page.locator("#rag-execution-json-details pre")).toBeHidden();
  await page.locator("#rag-execution-json-details summary").click();
  await expect(page.locator("#rag-execution-json")).toContainText('"aggregated": true');
  expect(received.source_key).toBe(`${archive.prefix}json_rag.json`);
  expect(received.source_sha256).toBe(sourceHash);
  expect(received.options).toEqual({ min_tokens: 150, max_tokens: 250, page_boundary: true,
    keep_tables_together: true, keep_code_blocks_together: true });
  expect(received.groups).toEqual([[0, 1]]);
  expect(received.created_at).toMatch(/^2026-/);
  await page.locator("#rag-preflight-min").fill("200");
  await expect(execute).toBeDisabled();
});

test("execution retries reuse the plan's execution id", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/Failed to load resource/);
  const archive = savedArchive(1);
  const original = JSON.stringify({ chunks: [
    { id: 1, text: "First", page: 1, token_count: 300 },
    { id: 2, text: "Second", page: 1, token_count: 300 },
  ], metadata: { document: "report-1.pdf" } });
  const sourceHash = createHash("sha256").update(original).digest("hex");
  const requests = [];
  let failures = 1;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({
    contentType: "application/json", headers: { "X-OCR-Content-SHA256": sourceHash }, body: original }));
  await page.route("**/storage/rag/refine", route => {
    requests.push(route.request().postDataJSON());
    if (failures-- > 0) return route.abort();
    return route.fulfill({ json: { prefix: archive.prefix,
      file: { name: "json_rag_job1_001.json", key: `${archive.prefix}json_rag_job1_001.json`,
        size: 10, content_type: "application/json" },
      chunk_count: 1 } });
  });
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-run").click();
  const execute = page.locator("#rag-execute");
  await expect(execute).toBeEnabled();
  await execute.click();
  await expect(page.locator("#rag-execution-state")).toContainText("Save failed");
  await expect(page.locator("#rag-execution-state")).toContainText("may still have saved");
  await expect(execute).toBeEnabled();
  await execute.click();
  await expect(page.locator("#rag-execution-state")).toContainText("Saved json_rag_job1_001.json");
  expect(requests).toHaveLength(2);
  expect(requests[1].execution_id).toBe(requests[0].execution_id);
  expect(requests[1].created_at).toBe(requests[0].created_at);
  await page.locator("#rag-preflight-min").fill("100");
  await page.locator("#rag-preflight-run").click();
  await expect(execute).toBeEnabled();
  await execute.click();
  await expect(page.locator("#rag-execution-state")).toContainText("Saved");
  expect(requests).toHaveLength(3);
  expect(requests[2].execution_id).not.toBe(requests[0].execution_id);
});

test("a refined file activates only Selected JSON RAG and the original restores refinement", async ({ page }, testInfo) => {
  const archive = savedArchive(1);
  const refinedName = "json_rag_job1_001.json";
  const refined = { key: archive.prefix + refinedName, name: refinedName, size: 420, content_type: "application/json" };
  const aggregated = { chunks: [{ id: 1, text: "Aggregated text", token_count: 300, page: 1 }],
    metadata: { document: "report-1.pdf", n_chunks: 1 } };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: {
    manifest: { source: { original_name: "Original report job1.pdf" } },
    files: [...savedFiles(archive).files, refined],
  } }));
  const contents = JSON.stringify(aggregated);
  const sourceHash = createHash("sha256").update(contents).digest("hex");
  await page.route("**/storage/file?**", route => route.fulfill({
    contentType: "application/json", headers: { "X-OCR-Content-SHA256": sourceHash }, body: contents,
  }));
  await openHealthyConsole(page, "#rag");
  const table = page.locator("#rag-storage-files");
  const original = table.locator("tbody tr").first().getByRole("button", { name: /^Preview and select / });
  await original.click();
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-result")).toBeVisible();
  await expect(page.locator("#rag-execute")).toBeEnabled();
  await table.locator("tbody tr").nth(1).getByRole("button", { name: /^Preview and select / }).click();
  await expect(page.locator("#rag-selected-json-summary")).toHaveText(refinedName);
  await expect(page.locator("#rag-selected-note")).toContainText("refined file");
  await expect(page.locator("#rag-selected-note")).toHaveCSS("text-transform", "uppercase");
  await expect(page.locator("#rag-selected-note")).toHaveCSS("font-size", "11.2px");
  await expect(page.locator("#rag-selected-note")).toHaveCSS("font-family",
    await page.locator("#rag-execution-state").evaluate(el => getComputedStyle(el).fontFamily));
  await expect(page.locator("#rag-selected-note .literal")).toHaveText("json_rag.json");
  await expect(page.locator("#rag-selected-note .literal")).toHaveCSS("text-transform", "none");
  await expect(page.getByRole("heading", { name: "03 Aggregation preflight", exact: true })).toBeHidden();
  await expect(page.locator("#rag-preflight-form")).toBeHidden();
  await expect(page.locator("#rag-preflight-run")).toBeDisabled();
  await expect(page.locator("#rag-execution-root")).toBeHidden();
  await page.locator("#rag-selected-json-summary").click();
  await expect(page.locator("#rag-selected-json")).toContainText("Aggregated text");
  await page.locator("#rag-preflight-form").evaluate(form =>
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await expect(page.locator("#rag-preflight-result")).toBeHidden();
  await expect(page.locator("#rag-execute")).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("rag-refined-selection.png"), fullPage: true });
  await original.click();
  await expect(page.locator("#rag-selected-note")).toBeHidden();
  await expect(page.locator("#rag-preflight-form")).toBeVisible();
  await expect(page.locator("#rag-execution-root")).toBeVisible();
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-result")).toBeVisible();
  await expect(page.locator("#rag-execute")).toBeEnabled();
});

test("a saved refinement appears in the list and Refresh keeps the selection", async ({ page }) => {
  const archive = savedArchive(1);
  const original = JSON.stringify({ chunks: [
    { id: 1, text: "First", page: 1, token_count: 300 },
  ], metadata: { document: "report-1.pdf" } });
  const sourceHash = createHash("sha256").update(original).digest("hex");
  const refinedName = "json_rag_job1_001.json";
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({
    contentType: "application/json", headers: { "X-OCR-Content-SHA256": sourceHash }, body: original }));
  await page.route("**/storage/rag/refine", route => route.fulfill({ json: {
    prefix: archive.prefix,
    file: { name: refinedName, key: `${archive.prefix}${refinedName}`, size: 420, content_type: "application/json" },
    chunk_count: 1,
  } }));
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-run").click();
  await page.locator("#rag-execute").click();
  await expect(page.locator("#rag-execution-state")).toContainText(`Saved ${refinedName}`);
  const table = page.locator("#rag-storage-files");
  await expect(table).toContainText(refinedName);
  await page.locator("#rag-storage-refresh").click();
  await expect(page.locator("#rag-storage-file-status")).toBeEmpty();
  await expect(page.locator("#rag-selected")).toBeVisible();
  await expect(table.locator("tr.is-selected")).toHaveCount(1);
});

test("the RAG tab stays silent until a file is selected", async ({ page }) => {
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [savedArchive(1)], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(savedArchive(1)) }));
  await openHealthyConsole(page, "#rag");
  await expect(page.locator("#rag-storage-files tbody tr")).toHaveCount(1);
  await expect(page.locator("#rag-selected")).toBeHidden();
  await expect(page.locator("#rag-execution-state")).toBeEmpty();
});

test("RAG parameter reference explains the desktop saved-file refinement controls", async ({ page }) => {
  await openHealthyConsole(page, "#rag");
  const reference = page.locator("#rag-reference-details");
  await expect(reference.locator("summary")).toHaveText("Parameter reference");
  await expect(page.locator("#rag-reference")).toBeHidden();
  await reference.locator("summary").click();
  const titles = page.locator("#rag-reference .param-name");
  for (const title of ["Saved JSON RAG", "JSON type", "From / To (UTC)", "Search files",
    "Refresh and pagination", "Select and preview", "Download and delete", "min token / max token",
    "keep tables", "keep code blocks", "page boundary", "Run preflight", "Token range and counts",
    "Cut", "Merge and undo", "JSON preview", "Execute and save JSON RAG"]) {
    await expect(titles).toContainText([title]);
  }
  for (const title of ["Input", "Maximum tokens", "Overlap tokens", "Strict token limit",
    "Keep tables and code whole", "Page margins", "Table headers", "Source ID and language",
    "Output metadata", "Accept truncated OCR"]) {
    await expect(titles.filter({ hasText: new RegExp(`^${title}$`) })).toHaveCount(0);
  }
});

test("a source without chunks cannot be refined", async ({ page }) => {
  const archive = savedArchive(1);
  const original = JSON.stringify({ chunks: [], metadata: { document: "report-1.pdf" } });
  const sourceHash = createHash("sha256").update(original).digest("hex");
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({
    contentType: "application/json", headers: { "X-OCR-Content-SHA256": sourceHash }, body: original }));
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await expect(page.locator("#rag-execution-state")).toContainText("no chunks");
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-error")).toContainText("no chunks");
  await expect(page.locator("#rag-preflight-result")).toBeHidden();
  await expect(page.locator("#rag-execute")).toBeDisabled();
});

test("preflight counts out-of-range groups in its tiles", async ({ page }) => {
  const archive = savedArchive(1);
  const value = { chunks: [
    { id: 1, text: "Within", page: 1, token_count: 300 },
    { id: 2, text: "Above", page: 1, token_count: 900 },
  ] };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", route => route.fulfill({ json: value }));
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-min").fill("100");
  await page.locator("#rag-preflight-max").fill("400");
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["1", "0", "1", "0"]);
  await page.locator("#rag-preflight-max").fill("1000");
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-counts .metric-value")).toHaveText(["2", "0", "0", "0"]);
});

test("manual RAG group changes refresh preflight counts and token distribution", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  await expect(page.locator("#rag-preflight-distribution .metric-value").first()).toHaveText("3");
  await page.getByRole("button", { name: "Merge group 2 with next", exact: true }).click();
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(2);
  await expect(page.locator("#rag-preflight-distribution .metric-value")).toHaveText(["2", "600", "1300", "2000"]);
  await expect.poll(() => page.locator("#rag-preflight-counts .metric-value")
    .evaluateAll(nodes => nodes.map(node => Number(node.textContent)))).toEqual([1, 0, 1, 0]);
});

test("selecting a file marks the table busy and re-selecting is a no-op", async ({ page }) => {
  const archive = savedArchive(1);
  const rag = { chunks: [{ id: 1, text: "Stored", page: 1, token_count: 42 }] };
  const sourceHash = createHash("sha256").update(JSON.stringify(rag)).digest("hex");
  let reads = 0;
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: savedFiles(archive) }));
  await page.route("**/storage/file?**", async route => {
    reads++;
    await new Promise(resolve => setTimeout(resolve, 300));
    return route.fulfill({ contentType: "application/json",
      headers: { "X-OCR-Content-SHA256": sourceHash }, body: JSON.stringify(rag) });
  });
  await openHealthyConsole(page, "#rag");
  const choice = page.locator("#rag-storage-files .storage-file-choice");
  await choice.click();
  await expect(page.locator("#rag-storage-files")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#rag-selected-json-summary")).toHaveText("json_rag.json");
  await expect(page.locator("#rag-storage-files")).not.toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#rag-storage-file-status")).toBeEmpty();
  await choice.click();
  await expect(page.locator("#rag-storage-file-status")).toBeEmpty();
  expect(reads).toBe(1);
});

test("RAG skips OCR archives in the listing but still reads parse-chunks", async ({ page }) => {
  const parse = savedArchive(1);
  const chunks = { ...savedArchive(2), kind: "parse-chunks" };
  const ocr = { ...savedArchive(3), kind: "ocr" };
  const archives = [parse, chunks, ocr];
  const fetched = [];
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: archives, next_cursor: null } }));
  await page.route("**/storage/archive?**", route => {
    const prefix = new URL(route.request().url()).searchParams.get("prefix");
    fetched.push(prefix);
    return route.fulfill({ json: savedFiles(archives.find(archive => archive.prefix === prefix)) });
  });
  await openHealthyConsole(page, "#rag");
  await expect(page.locator("#rag-storage-files tbody tr")).toHaveCount(2);
  expect(fetched.sort()).toEqual([parse.prefix, chunks.prefix].sort());
  expect(fetched).not.toContain(ocr.prefix);
});

test("split, glue and undo are reachable with their reasons", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  const row = page.locator("#rag-preflight-groups tbody tr").first();
  await expect(row.getByRole("button", { name: /Cut group 1/ })).toBeEnabled();
  await row.getByRole("button", { name: /Cut group 1/ }).click();
  await expect(page.locator("#rag-preflight-status")).toContainText("Cutting");
  await expect(page.getByRole("button", { name: /Undo gesture 1/ })).toHaveCount(3);
  await row.getByRole("button", { name: /Undo/ }).click();
  await expect(page.locator("#rag-preflight-groups tbody tr")).toHaveCount(3);
});

test("a below-minimum split note is readable and its code reaches the save", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  await page.route("**/storage/rag/split", route => route.fulfill({ json: {
    fragments: [
      { entries: [[0, 0, 100]], token_count: 900, status: "within", notes: [] },
      { entries: [[0, 100, 180]], token_count: 200, status: "below", notes: [
        { code: "below_minimum_fragment", severity: "info", targets: [], parameters: [
          { name: "detail", value: "group below the minimum" }, { name: "tokens", value: 200 },
        ] },
      ] },
    ], warnings: [],
  } }));
  let savedRequest;
  await page.route("**/storage/rag/refine", route => {
    savedRequest = route.request().postDataJSON();
    return route.fulfill({ json: { prefix: "fixture/", chunk_count: 4,
      file: { name: "json_rag_result.json", key: "fixture/json_rag_result.json" } } });
  });
  const rows = page.locator("#rag-preflight-groups tbody tr");
  await rows.first().getByRole("button", { name: /Cut group 1/ }).click();
  await expect(rows).toHaveCount(4);
  const status = rows.nth(1).locator("td").nth(4);
  await expect(status).toHaveText("Below range");
  await expect(status).toHaveAttribute("title", /target minimum is 512/);
  await expect(page.locator("#rag-preflight-groups")).not.toContainText("[object Object]");
  await page.locator("#rag-execute").click();
  await expect(page.locator("#rag-execution-state")).toContainText("Saved");
  expect(savedRequest.overrides).toEqual([[], ["below_minimum_fragment"], [], []]);
});

test("a row within range hides the unavailable scissors", async ({ page }) => {
  await openRagWithFixture(page, "balanced");
  const button = page.locator("#rag-preflight-groups tbody tr").first().getByRole("button", { name: /Cut group/ });
  await expect(button).toHaveCount(0);
});

test("changing a setting clears the table, warns, and removes the undo", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  await page.locator("#rag-preflight-groups tbody tr").first()
    .getByRole("button", { name: /Cut group 1/ }).click();
  await page.locator("#rag-preflight-max").fill("2048");
  await expect(page.locator("#rag-preflight-result")).toBeHidden();
  // R23: the warning lives in the status line, outside the hidden section, so
  // the user can actually read it.
  await expect(page.locator("#rag-preflight-status"))
    .toContainText(/manual gestures were discarded/);
  expect(await page.evaluate(() => document.body.innerText))
    .toContain("MANUAL GESTURES WERE DISCARDED");
  await expect(page.getByRole("button", { name: /Undo/ })).toHaveCount(0);
});

test("a consumed gesture hides its undo until the dependent cut is undone", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  const rows = page.locator("#rag-preflight-groups tbody tr");
  // Gesture 1: merge the below row 2 with the above row 1; gesture 2 cuts it.
  await rows.nth(1).getByRole("button", { name: /Merge group 2 with previous/ }).click();
  await expect(rows).toHaveCount(2);
  await expect(rows.first().getByRole("button", { name: /Undo gesture 1/ })).toBeEnabled();
  await rows.first().getByRole("button", { name: /Cut group 1/ }).click();
  await expect(rows).toHaveCount(4);
  // Unavailable actions are hidden; undoing the cut restores the glue undo.
  const blocked = rows.first().getByRole("button", { name: /Undo gesture 1/ });
  await expect(blocked).toHaveCount(0);
  await expect(rows.first().getByRole("button", { name: /Undo gesture 2/ })).toBeEnabled();
  await rows.first().getByRole("button", { name: /Undo gesture 2/ }).click();
  await expect(rows.first().getByRole("button", { name: /Undo gesture 1/ })).toBeEnabled();
});

test("a successful save invalidates the gestures and removes the undo", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  await page.route("**/storage/rag/refine", route => route.fulfill({ json: {
    prefix: "fixture-table-over-maximum/", chunk_count: 5,
    file: { name: "json_rag_fixture_1.json", content_type: "application/json", size: 900,
      key: "fixture-table-over-maximum/json_rag_fixture_1.json" } } }));
  const rows = page.locator("#rag-preflight-groups tbody tr");
  await rows.first().getByRole("button", { name: /Cut group 1/ }).click();
  await expect(page.getByRole("button", { name: /Undo gesture 1/ })).toHaveCount(3);
  await page.locator("#rag-execute").click();
  await expect(page.locator("#rag-execution-state")).toContainText(/Saved/);
  // FR-014: the saved plan is no longer an unsaved gesture plan.
  await expect(page.getByRole("button", { name: /Undo/ })).toHaveCount(0);
});

test("while Cutting only the target row is locked and the others keep their undo", async ({ page }) => {
  test.setTimeout(60_000);
  await openRagWithFixture(page, "table-over-maximum", { splitDelayMs: 31_000 });
  const rows = page.locator("#rag-preflight-groups tbody tr");
  // A glue puts an undo on a different row before the slow cut starts.
  await rows.nth(1).getByRole("button", { name: /Merge group 2 with next/ }).click();
  await expect(rows).toHaveCount(2);
  const undo = rows.nth(1).getByRole("button", { name: /Undo/ });
  await expect(undo).toBeEnabled();
  const target = rows.first();
  await target.getByRole("button", { name: /Cut group 1/ }).click();
  await expect(page.locator("#rag-preflight-status")).toContainText("Cutting");
  await expect(target.getByRole("button", { name: /Cut group 1/ })).toHaveCount(0);
  await expect(undo).toBeEnabled();
  await undo.click();
  await expect(rows).toHaveCount(3);
});

test("a tokenizer 503 from saving hides the scissors and preserves glue", async ({ page }) => {
  await openRagWithFixture(page, "table-over-maximum");
  await page.route("**/storage/rag/refine", route => route.fulfill({ status: 503, json: {
    detail: { code: "rag_tokenizer_unavailable", message: "The tokenizer is unavailable", details: {} } } }));
  const rows = page.locator("#rag-preflight-groups tbody tr");
  const cut = rows.first().getByRole("button", { name: /Cut group 1/ });
  const glue = rows.nth(1).getByRole("button", { name: /Merge group 2 with next/ });
  await expect(cut).toBeEnabled();
  await page.locator("#rag-execute").click();
  await expect(page.locator("#rag-execution-state")).toContainText(/Save failed/);
  await expect(cut).toHaveCount(0);
  await expect(page.locator("#rag-execution-state")).toContainText(/tokenizer/i);
  await expect(glue).toBeEnabled();
});

test("a slow split shows Cutting and leaves the row unchanged on timeout", async ({ page }) => {
  test.setTimeout(60_000);
  await openRagWithFixture(page, "table-over-maximum", { splitDelayMs: 31_000 });
  const row = page.locator("#rag-preflight-groups tbody tr").first();
  await row.getByRole("button", { name: /Cut group 1/ }).click();
  await expect(page.locator("#rag-preflight-status")).toContainText("Cutting");
  await expect(page.locator("#rag-preflight-error")).toContainText(/timed out/, { timeout: 40_000 });
  await expect(row.getByRole("button", { name: /Cut group 1/ })).toBeEnabled();
});

test("selected json_rag with unprocessed pages shows partial tile", async ({ page }) => {
  const archive = { prefix: "p/", filename: "r.pdf", job_id: "p", kind: "parse", created_at: "2026-10-01T00:00:00Z", partial_ocr: true };
  const file = { key: "p/json_rag.json", name: "json_rag.json", size: 100, content_type: "application/json" };
  const value = { chunks: [{ id: 0, text: "a" }], metadata: { pages: 5, unprocessed_pages: [4, 5] } };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: { manifest: { source: { original_name: "r.pdf" } }, files: [file] } }));
  await page.route("**/storage/file?**", route => route.fulfill({ contentType: "application/json",
    headers: { "X-OCR-Content-SHA256": "a".repeat(64) }, body: JSON.stringify(value) }));
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await expect(page.locator("#rag-selected")).toContainText("2 unprocessed pages");
});
