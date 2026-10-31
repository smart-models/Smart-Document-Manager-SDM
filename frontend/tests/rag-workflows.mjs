import { readFile } from "node:fs/promises";
import { openHealthyConsole, expect } from "./fixtures.mjs";

const ragSplitFixtures = JSON.parse(await readFile(new URL("./fixtures/rag-split.json", import.meta.url), "utf8"));

// Serve one named json_rag fixture, select it and run the aggregation preflight.
// `splitDelayMs` keeps the split route pending so the 30 s browser timeout is
// exercised. The split endpoint answers with the fixture's measured fragments.
export async function openRagWithFixture(page, name, { splitDelayMs } = {}) {
  const scenario = ragSplitFixtures[name];
  if (!scenario) throw new Error(`Unknown RAG fixture: ${name}`);
  const archive = { prefix: `fixture-${name}/`, filename: "report.pdf", job_id: name,
    kind: "parse", created_at: "2026-10-01T00:00:00Z", partial_ocr: false };
  const file = { key: `${archive.prefix}json_rag.json`, name: "json_rag.json", size: 1024,
    content_type: "application/json" };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.fulfill({ json: {
    manifest: { source: { original_name: "report.pdf" } }, files: [file] } }));
  await page.route("**/storage/file?**", route => route.fulfill({ contentType: "application/json",
    headers: { "X-OCR-Content-SHA256": "a".repeat(64) },
    body: JSON.stringify(scenario.value) }));
  await page.route("**/storage/rag/split", async route => {
    const delay = splitDelayMs ?? 150;
    if (delay) await new Promise(resolve => setTimeout(resolve, delay));
    try { await route.fulfill({ json: { fragments: scenario.fragments ?? [], warnings: [] } }); }
    catch { /* The browser aborted the request on timeout. */ }
  });
  await openHealthyConsole(page, "#rag");
  await page.locator("#rag-storage-files .storage-file-choice").click();
  await page.locator("#rag-preflight-run").click();
  await expect(page.locator("#rag-preflight-result")).toBeVisible();
}

export const canonical = (await readFile(new URL("../../tests/fixtures/ocr-rag-chunks.example.json", import.meta.url), "utf8")).trim();
export const parsed = { pages: [{ page: 1, blocks: [{ type: "text", content: "Testo del chunk", bbox: [0, 0, 100, 20] }] }] };
export const defaultMetadataFields = [
  "page", "pages", "section_id", "section", "section_path", "block_types", "source_spans",
  "context_spans", "page_context", "assets", "relations", "overflow", "overflow_tokens",
  "overflow_reason", "overlap_token_count", "source_fingerprint", "tokenizer", "chunker_version",
  "policy_hash", "source_id", "lang", "diagnostics", "document_context",
];

export async function importSource(page, document = parsed) {
  await page.locator("#rag-import").setInputFiles({ name: "report.json", mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify({ file: "report.pdf", options: { max_pages: 1 }, response: document })),
  });
}

export function registerRagWorkflows(test, expect, setup) {
  test("RAG options are explicit, include all policies, and never repeat OCR", async ({ page }) => {
    const calls = [];
    await setup(page, async route => {
      calls.push(route.request().postDataJSON());
      await route.fulfill({ contentType: "application/json", body: canonical });
    });
    await importSource(page);
    await page.locator("#rag-max").fill("1024");
    await page.locator("#rag-overlap").fill("32");
    await page.locator("#rag-strict").uncheck();
    await page.locator("#rag-tables").uncheck();
    await page.locator("#rag-code").uncheck();
    await page.locator("#rag-margins").selectOption("text");
    await page.locator("#rag-headers").selectOption("first_row");
    await page.locator("#rag-source-id").fill("source-1");
    await page.locator("#rag-lang").fill("it");
    expect(calls).toHaveLength(0);
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-summary")).toContainText("1 chunks");
    expect(calls[0]).toEqual({ parsed_document: parsed, accept_truncated: false, source_id: "source-1", lang: "it",
      options: { max_tokens: 1024, overlap_tokens: 32, strictly: false, preserve_tables: false,
        preserve_code_blocks: false, marginal_policy: "text", table_header_policy: "first_row",
        metadata_fields: defaultMetadataFields } });
    await page.locator("#rag-max").fill("2048");
    await expect(page.locator("#rag-result-state")).toContainText("Previous successful result");
    expect(calls).toHaveLength(1);
    await page.locator("#rag-run").click();
    await expect.poll(() => calls.length).toBe(2);
    expect(calls[1].parsed_document).toEqual(parsed);
  });

  test("strict conflict offers three deliberate fixes and retains the previous file", async ({ page }) => {
    let count = 0;
    await setup(page, async route => {
      count++;
      if (count === 1) return route.fulfill({ contentType: "application/json", body: canonical });
      return route.fulfill({ status: 422, json: { detail: { code: "protected_block_exceeds_budget",
        message: "A protected table exceeds the budget", details: { page: 2, source_key: "p2:b3", type: "table",
          max_tokens: 512, required_tokens: 1600, suggested_max_tokens: 1600,
          minimum_feasible_max_tokens: null, conflicting_option: "preserve_tables" } } } });
    });
    await importSource(page);
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-summary")).toContainText("1 chunks");
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-error")).toContainText("required with context 1600");
    await expect(page.locator("#rag-error")).not.toContainText("Verified minimum");
    await expect(page.locator("#rag-result-state")).toContainText("Previous successful result");
    await page.getByRole("button", { name: "Increase token budget", exact: true }).click();
    await expect(page.locator("#rag-max")).toHaveValue("1600");
    await page.getByRole("button", { name: "Allow splitting this type", exact: true }).click();
    await expect(page.locator("#rag-tables")).not.toBeChecked();
    await page.getByRole("button", { name: "Disable strict limit, keep protection", exact: true }).click();
    await expect(page.locator("#rag-strict")).not.toBeChecked();
    await expect(page.locator("#rag-tables")).toBeChecked();
    expect(count).toBe(2);
    const pending = page.waitForEvent("download");
    await page.locator("#rag-download").click();
    expect(await readFile(await (await pending).path(), "utf8")).toBe(canonical);
  });

  test("capacity retry is manual and a truncated import is never accepted implicitly", async ({ page }) => {
    const calls = [];
    await setup(page, async route => {
      calls.push(route.request().postDataJSON());
      if (calls.length === 1) return route.fulfill({ status: 503, json: {
        detail: { code: "chunk_capacity_exceeded", message: "Capacity is occupied", details: {} },
      } });
      return route.fulfill({ contentType: "application/json", body: canonical });
    });
    await importSource(page, { ...parsed, warnings: [{ code: "pages_truncated", message: "Limited prefix" }] });
    await expect(page.locator("#rag-truncated")).not.toBeChecked();
    await expect(page.locator("#rag-source")).toContainText("explicit choice");
    await page.locator("#rag-truncated").check();
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-error")).toContainText("chunk_capacity_exceeded");
    expect(calls).toHaveLength(1);
    expect(calls[0].accept_truncated).toBe(true);
    await page.locator("#rag-error").getByRole("button", { name: "Retry", exact: true }).click();
    await expect(page.locator("#rag-summary")).toContainText("1 chunks");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual(calls[0]);
  });

  test("preview reports overflow, page labels, diagnostic codes and safely renders content", async ({ page }) => {
    const payload = JSON.parse(canonical);
    const chunk = payload.chunks[0];
    chunk.text = '<img src="data:,bad" onerror="window.ragInjected=1"><script>window.ragInjected=1</script>\n\n$E=mc^2$';
    chunk.section = '<img onerror="window.ragInjected=1">';
    chunk.overflow = true;
    chunk.overflow_tokens = 7;
    chunk.token_count = 519;
    chunk.diagnostics = [{ code: "protected_block_overflow", target: {}, parameters: {} }];
    await setup(page, route => route.fulfill({ json: payload }));
    await importSource(page);
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-summary")).toContainText("1 overflows · 1 diagnostics");
    await expect(page.locator("#rag-chunks")).toContainText("Printed page labels — input page 1: 1");
    await expect(page.locator("#rag-chunks")).toContainText("Authorized overflow: +7");
    await expect(page.locator("#rag-distribution")).toContainText("p95 519");
    await expect(page.locator("#rag-chunks [onerror], #rag-chunks script")).toHaveCount(0);
    expect(await page.evaluate(() => window.ragInjected || 0)).toBe(0);
  });

  test("malformed import and success response leave the prior download available", async ({ page }) => {
    let count = 0;
    await setup(page, route => route.fulfill({ json: ++count === 1 ? JSON.parse(canonical) : { broken: true } }));
    await importSource(page);
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-summary")).toContainText("1 chunks");
    await page.locator("#rag-import").setInputFiles({ name: "broken.json", mimeType: "application/json", buffer: Buffer.from("{") });
    await expect(page.locator("#rag-error")).toContainText("Import a structured");
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-error")).not.toBeEmpty();
    await expect(page.locator("#rag-result-state")).toContainText("Previous successful result");
    await expect(page.locator("#rag-download")).toBeEnabled();
    expect(count).toBe(2);
  });

  test("invalid nested preview metadata cannot replace an existing RAG file", async ({ page }) => {
    const malformed = JSON.parse(canonical);
    malformed.chunks[0].page_context = { page_labels: "invalid" };
    let count = 0;
    await setup(page, route => route.fulfill({ contentType: "application/json",
      body: ++count === 1 ? canonical : JSON.stringify(malformed) }));
    await importSource(page);
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-summary")).toContainText("1 chunks");
    await page.locator("#rag-run").click();
    await expect(page.locator("#rag-error")).not.toBeEmpty();
    const pending = page.waitForEvent("download");
    await page.locator("#rag-download").click();
    expect(await readFile(await (await pending).path(), "utf8")).toBe(canonical);
  });
}
