import { test, expect, openHealthyConsole, IMAGE } from "./fixtures.mjs";

test("console messages share uppercase typography and semantic colors", async ({ page }) => {
  await openHealthyConsole(page);
  const messages = await page.locator(
    ".caption, .alert, .storage-status, .storage-meta, .storage-empty, .storage-pagination [role=status], [role=alert]",
  ).evaluateAll(elements => elements.map(el => {
    const style = getComputedStyle(el);
    return { name: el.id || el.className, transform: style.textTransform,
      font: style.fontFamily, size: style.fontSize, color: style.color,
      error: el.matches("[role=alert], .alert-error, .error, .down, .warn") };
  }));
  expect(messages.length).toBeGreaterThan(15);
  for (const message of messages) {
    expect(message.transform, message.name).toBe("uppercase");
    expect(message.font, message.name).toContain("JetBrains Mono");
    expect(message.size, message.name).toBe("11.2px");
    expect(message.color, message.name).toBe(message.error ? "rgb(255, 80, 80)" : "rgb(112, 137, 173)");
  }
});

test("message updates preserve filenames and style validation and deletion errors", async ({ page, expectedConsoleErrors }, testInfo) => {
  expectedConsoleErrors.push(/Failed to load resource: the server responded with a status of 409/);
  const archive = { prefix: "Report.pdf__audit/", filename: "Report.pdf", job_id: "audit", kind: "parse", created_at: "2026-10-11T10:00:00Z" };
  const name = "json_rag_MixedCase_001.json";
  const source = { chunks: [{ id: 1, text: "Original MixedCase text", page: 1, token_count: 10 }], metadata: {} };
  await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [archive], next_cursor: null } }));
  await page.route("**/storage/archive?**", route => route.request().method() === "DELETE"
    ? route.fulfill({ status: 409, json: { detail: "Deletion failed." } })
    : route.fulfill({ json: { manifest: {}, files: [
      { key: archive.prefix + "json_rag.json", name: "json_rag.json", size: 200, content_type: "application/json" },
      { key: archive.prefix + name, name, size: 200, content_type: "application/json" },
    ] } }));
  await page.route("**/storage/file?**", route => route.request().method() === "DELETE"
    ? route.fulfill({ status: 409, json: { detail: "Deletion failed." } })
    : route.fulfill({ json: source }));
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles({ ...IMAGE, name: "MixedCase.png" });
  await expect(page.locator("#parse-file-name .literal")).toHaveText("MixedCase.png");
  await expect(page.locator("#parse-file-name .literal")).toHaveCSS("text-transform", "none");
  await page.locator("#tab-storage").click();
  await page.locator(".storage-list .storage-name").click();
  await page.locator("#storage-detail .storage-file-choice").nth(1).click();
  const status = page.locator("#storage-detail .storage-status");
  await expect(status).toContainText("Previewing");
  await expect(status).toHaveCSS("text-transform", "uppercase");
  await expect(status.locator(".literal")).toHaveText(name);
  await expect(status.locator(".literal")).toHaveCSS("text-transform", "none");
  await expect(page.locator("#storage-detail pre")).toContainText("Original MixedCase text");
  await expect(page.locator("#storage-detail pre")).toHaveCSS("text-transform", "none");
  await page.locator("#storage-archives button.storage-danger").click();
  await page.locator("#storage-delete-confirm").click();
  const error = page.locator("#storage-confirm-error");
  await expect(error).toContainText("Deletion failed");
  await expect(error).toHaveCSS("text-transform", "uppercase");
  await expect(error).toHaveCSS("font-size", "11.2px");
  await expect(error).toHaveCSS("color", "rgb(255, 80, 80)");
  await page.locator("#storage-cancel").click();
  await page.locator("#tab-rag").click();
  await page.locator("#rag-storage-files .storage-file-choice").first().click();
  await page.locator("#rag-preflight-min").fill("0");
  await expect(page.locator("#rag-preflight-error")).toContainText("Min token");
  await expect(page.locator("#rag-preflight-error")).toHaveCSS("text-transform", "uppercase");
  await expect(page.locator("#rag-preflight-error")).toHaveCSS("font-size", "11.2px");
  await expect(page.locator("#rag-preflight-error")).toHaveCSS("color", "rgb(255, 80, 80)");
  await page.locator("#rag-storage-files button.storage-danger").click();
  await page.locator("#rag-storage-confirm-delete").click();
  const ragError = page.locator("#rag-storage-confirm-error");
  await expect(ragError).toContainText("Deletion failed");
  await expect(ragError).toHaveCSS("text-transform", "uppercase");
  await expect(ragError).toHaveCSS("font-size", "11.2px");
  await expect(ragError).toHaveCSS("color", "rgb(255, 80, 80)");
  await page.screenshot({ path: testInfo.outputPath("consistent-message-styles.png") });
});
