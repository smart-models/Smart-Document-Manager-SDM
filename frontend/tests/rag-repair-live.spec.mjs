import path from "node:path";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { test, expect } from "@playwright/test";

const root = path.resolve(import.meta.dirname, "../..");
const resultDir = path.join(root, "benchmarks/results/rag-tests-2026-10-03");
test.use({ trace: "off" });

for (const name of ["irs-1040-2025", "bert", "istat-rapporto-2025-cap1-pages001-020"]) {
  test(`real ${name}: split, View, undo, recut, save and reload`, async ({ page, request }) => {
    test.setTimeout(180_000);
    page.setDefaultTimeout(15_000);
    const corpus = JSON.parse(await readFile(path.join(resultDir, "corpus.json"), "utf8"));
    const source = corpus.documents.find(d => d.response_file === `${name}.response.json`);
    expect(source).toBeTruthy();
    const env = await readFile(path.join(root, ".env"), "utf8");
    const token = process.env.UPSTREAM_CLIENT_TOKEN ?? env.match(/^UPSTREAM_CLIENT_TOKEN=(.*)$/m)?.[1].trim().replace(/^(['"])(.*)\1$/, "$2") ?? "";
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.addInitScript(value => { if (value) sessionStorage.setItem("ocr_api_key", value); }, token);
    await page.goto("/#rag");
    await page.locator("#rag-storage-search").fill(name);
    const original = page.locator("#rag-storage-files tbody tr").filter({ hasText: "json_rag.json" });
    await expect(original).toHaveCount(1, { timeout: 30_000 });
    await original.getByRole("button", { name: /^Preview and select/ }).click();
    await expect(page.locator("#rag-selected-json-summary")).toHaveText("json_rag.json");
    await page.locator("#rag-preflight-min").fill("128");
    await page.locator("#rag-preflight-max").fill(name.startsWith("istat") ? "256" : "512");
    await page.locator("#rag-preflight-run").click();
    const rows = page.locator("#rag-preflight-groups tbody tr");
    const pageInfo = page.locator("#rag-preflight-page-info");
    await expect(pageInfo).toContainText("groups");
    await page.locator("#rag-preflight-page-size").selectOption("50");
    const readPageInfo = async () => {
      const text = await pageInfo.textContent() || "";
      const match = text.match(/(\d+)\D+(\d+)\s+of\s+(\d+)\s+groups/);
      if (!match) throw new Error(`Unexpected preflight page info: ${JSON.stringify(text)}`);
      return { first: Number(match[1]), last: Number(match[2]), total: Number(match[3]) };
    };
    const totalGroups = async () => (await readPageInfo()).total;
    // The table is paginated: navigate until the page showing group n.
    const showGroup = async n => {
      for (let i = 0; i < 20; i++) {
        const { first, last } = await readPageInfo();
        if (n >= first && n <= last) return;
        await page.locator(n > last ? "#rag-preflight-next" : "#rag-preflight-prev").click();
      }
      throw new Error(`Group ${n} is not reachable in the paginated preflight table.`);
    };
    const target = rows.filter({ has: page.locator("td.rag-preflight-above") }).first();
    for (let i = 0; i < 20 && !(await target.count()); i++) {
      const next = page.locator("#rag-preflight-next");
      if (await next.isDisabled()) break;
      await next.click();
    }
    await expect(target).toBeVisible();
    const groupNumber = Number(await target.locator("td").first().innerText());
    const oldCount = await totalGroups();
    let previews = [];
    async function cut() {
      previews = [];
      const pending = page.waitForResponse(r => r.url().endsWith("/storage/rag/split") && r.request().method() === "POST");
      await showGroup(groupNumber);
      await page.getByRole("button", { name: `Cut group ${groupNumber}`, exact: true }).click();
      const response = await pending;
      expect(response.status()).toBe(200);
      const split = await response.json();
      await expect(page.locator("#rag-preflight-status")).toBeEmpty();
      for (let index = 0; index < split.fragments.length; index++) {
        await showGroup(groupNumber + index);
        await page.getByRole("button", { name: `Preview group ${groupNumber + index}`, exact: true }).click();
        const preview = page.locator("#rag-preflight-detail > pre");
        await expect(preview).not.toBeEmpty();
        const rendered = await preview.innerText();
        previews.push(rendered);
        // Independently render source code-point slices in the response order.
        const originalRag = JSON.parse(await readFile(path.join(resultDir, source.response_file), "utf8")).json_rag;
        const fragments = split.fragments[index].entries;
        const parts = fragments.map(entry => typeof entry === "number" ? originalRag.chunks[entry].text : Array.from(originalRag.chunks[entry[0]].text).slice(entry[1], entry[2]).join(""));
        const normalizeTagSpacing = text => text.replace(/>\s+</g, "><");
        expect(normalizeTagSpacing(rendered)).toBe(normalizeTagSpacing(parts.join("")));
        if (rendered.includes("<table")) {
          const firstEntry = fragments[0];
          const sourceIndex = typeof firstEntry === "number" ? firstEntry : firstEntry[0];
          const header = originalRag.chunks[sourceIndex].text.match(/<tr\b[^>]*>.*?<\/tr\s*>/is)?.[0];
          if (header) expect(rendered).toContain(header);
        }
      }
      expect(await page.locator("#rag-preflight-result").innerText()).not.toContain("[object Object]");
      await page.screenshot({ path: path.join(resultDir, `${name}-split.png`), fullPage: true });
    }
    await cut();
    await showGroup(groupNumber);
    await rows.getByRole("button", { name: "Undo gesture 1", exact: true }).first().click();
    expect(await totalGroups()).toBe(oldCount);
    if (name === "irs-1040-2025") {
      const merge = rows.getByRole("button", { name: /Merge group/ }).first();
      await merge.click();
      expect(await totalGroups()).toBe(oldCount - 1);
      const merged = rows.filter({ has: page.getByRole("button", { name: /Undo gesture/ }) });
      await merged.getByRole("button", { name: /^Preview group/ }).click();
      await expect(page.locator("#rag-preflight-detail > pre")).not.toBeEmpty();
      await merged.getByRole("button", { name: /Undo gesture/ }).click();
      expect(await totalGroups()).toBe(oldCount);
    }
    await cut();
    const pendingSave = page.waitForResponse(r => r.url().endsWith("/storage/rag/refine") && r.request().method() === "POST");
    await page.locator("#rag-execute").click();
    const saved = await pendingSave;
    expect(saved.status()).toBe(200);
    const value = await saved.json();
    await expect(page.locator("#rag-execution-state")).toContainText("Saved");
    await expect(rows.getByRole("button", { name: /Undo/ })).toHaveCount(0);
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const fetched = await request.get(`/storage/file?${new URLSearchParams({ key: value.file.key })}`, { headers });
    expect(fetched.status()).toBe(200);
    const rag = await fetched.json();
    expect(rag.chunks.slice(groupNumber - 1, groupNumber - 1 + previews.length).map(c => c.text)).toEqual(previews);
    expect(rag.chunks.length).toBe(value.chunk_count);
    expect(JSON.stringify(rag)).not.toContain("[object Object]");
    await page.locator("#rag-storage-refresh").click();
    const refined = page.locator("#rag-storage-files tbody tr").filter({ hasText: value.file.name });
    await expect(refined).toHaveCount(1, { timeout: 30_000 });
    await refined.getByRole("button", { name: /^Preview and select/ }).click();
    await expect(page.locator("#rag-selected-json-summary")).toHaveText(value.file.name);
    expect(errors).toEqual([]);
    await mkdir(resultDir, { recursive: true });
    await writeFile(path.join(resultDir, `${name}-browser.json`), JSON.stringify({ passed: true, saved_key: value.file.key, chunks: rag.chunks.length, page_errors: errors }, null, 2));
  });
}
