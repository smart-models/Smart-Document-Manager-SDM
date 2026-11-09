import { CONFIG, HEALTH, expect, test, openHealthyConsole, withApi } from "./fixtures.mjs";

async function openConsole(page, hash = "") {
  const external = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:8129" && url.protocol !== "data:") external.push(url.href);
  });
  await openHealthyConsole(page, hash);
  return { errors: [], external };
}

test("canonical UI keeps keyboard navigation, URL state and responsive layout", async ({ page }, testInfo) => {
  const { errors, external } = await openConsole(page);
  await page.locator("#tab-parse").focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#tab-ocr")).toBeFocused();
  await expect(page.locator("#panel-ocr")).toBeVisible();
  await page.keyboard.press("End");
  await expect(page.locator("#tab-monitor")).toBeFocused();
  await expect(page.locator("#mon-stats")).toContainText("active requests");
  await expect(page).toHaveURL(/#monitor$/);
  await page.reload();
  await expect(page.locator("#panel-monitor")).toBeVisible();
  await page.locator("#tab-monitor").focus();
  await page.keyboard.press("Home");
  await expect(page.locator("#tab-parse")).toBeFocused();
  expect(await page.locator("#tab-parse").evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe("none");
  for (const width of [1440, 760, 640]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const tab of ["parse", "ocr", "rag", "monitor"]) {
      await page.locator(`#tab-${tab}`).click();
      await expect(page.locator("h1:visible")).toHaveCount(1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`console-${tab}-${width}.png`), fullPage: true });
    }
  }
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});

test("degraded backend explains accessible locks and leaves diagnostics available", async ({ page }) => {
  await page.route("**/health", route => route.fulfill({
    json: { status: "degraded", vllm: "unreachable", model: "test", autotune: {} },
  }));
  await page.goto("/");
  // Only upload and run controls lock; results stay readable.
  await expect(page.locator("#parse-controls")).toHaveAttribute("inert", "");
  await expect(page.locator("#parse-controls")).toHaveAttribute("aria-disabled", "true");
  await expect(page.locator("#ocr-controls")).toHaveAttribute("aria-disabled", "true");
  await expect(page.locator("#panel-parse")).not.toHaveAttribute("inert", "");
  await expect(page.locator("#parse-results")).not.toHaveAttribute("inert", "");
  await expect(page.locator("#service-gate [role=alert]")).toContainText("OCR backend unavailable");
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#panel-monitor")).not.toHaveAttribute("inert", "");
});

test("archive-only health keeps the API healthy, hides the OCR tabs and lands on Storage", async ({ page }) => {
  await page.route("**/health", route => route.fulfill({
    json: { ...HEALTH, mode: "archive_only", vllm: "disabled", ocr_enabled: false },
  }));
  await page.goto("/");
  // The connection bar reports a healthy server, with the mode in its detail.
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
  await expect(page.locator("#conn-health")).toHaveClass(/\bok\b/);
  await expect(page.locator("#conn-health-detail")).toContainText("archive-only · vllm disabled");
  // Parse and OCR disappear; Storage is the landing tab and no banner is shown.
  await expect(page.locator("#tab-parse")).toBeHidden();
  await expect(page.locator("#tab-ocr")).toBeHidden();
  await expect(page.locator("#tab-storage")).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#panel-storage")).toBeVisible();
  await expect(page.locator("#panel-parse")).toBeHidden();
  await expect(page).toHaveURL(/#storage$/);
  await expect(page.locator("#service-gate .alert")).toHaveCount(0);
  for (const id of ["storage-root", "rag-root", "rag-storage-root", "rag-execution-root"]) {
    await expect(page.locator(`#${id}`), id).not.toHaveAttribute("inert", "");
  }
  // A hash pointing at a hidden tab falls back to Storage; arrows skip hidden tabs.
  await page.goto("/#parse");
  await expect(page.locator("#tab-storage")).toHaveAttribute("aria-selected", "true");
  await page.locator("#tab-rag").click();
  await page.locator("#tab-rag").press("ArrowLeft");
  await expect(page.locator("#tab-monitor")).toHaveAttribute("aria-selected", "true");
  await page.locator("#tab-monitor").press("Home");
  await expect(page.locator("#tab-rag")).toHaveAttribute("aria-selected", "true");
  await page.locator("#tab-storage").click();
  // Monitor shows vLLM as DISABLED (neutral) and names the mode, never DOWN.
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-badges")).toContainText("DISABLED");
  await expect(page.locator("#mon-badges")).not.toContainText("DOWN");
  await expect(page.locator("#mon-badges .metric-value.muted")).toHaveText("DISABLED");
  await expect(page.locator("#mon-model")).toContainText("archive_only");
});

test("a degraded archive-only server reports its storage outage instead of promising archives", async ({ page }) => {
  await page.route("**/health", route => route.fulfill({
    json: {
      ...HEALTH, status: "degraded", mode: "archive_only", vllm: "disabled", ocr_enabled: false,
      storage: { status: "error", last_operation: "probe", timestamp: "2026-11-09T10:00:00+00:00" },
    },
  }));
  await page.goto("/");
  await expect(page.locator("#conn-health-status")).toHaveText("DEGRADED");
  await expect(page.locator("#conn-health")).toHaveClass(/\bbad\b/);
  await expect(page.locator("#conn-health-detail")).toContainText("archive-only · vllm disabled");
  const banner = page.locator("#service-gate .alert");
  await expect(banner).toHaveAttribute("role", "alert");
  await expect(banner).toContainText("STORAGE UNREACHABLE");
  await expect(banner).not.toContainText("stay available");
  await expect(page.locator("#tab-parse")).toBeHidden();
  await expect(page.locator("#tab-ocr")).toBeHidden();
  // The outage is a storage problem, not a model problem: vLLM stays DISABLED.
  await page.locator("#tab-monitor").click();
  await expect(page.locator("#mon-badges")).toContainText("DISABLED");
  await expect(page.locator("#mon-badges")).not.toContainText("DOWN");
  await expect(page.locator("#mon-storage")).toContainText("DOWN");
});

test.describe("without a registered key", () => {
  test.use({ apiKey: "" });

  const PROTECTED = ["parse-controls", "ocr-controls", "rag-storage-root", "rag-root", "rag-execution-root", "storage-root"];
  const MISSING = "API KEY MISSING — OPEN API KEY AND REGISTER A KEY";

  async function expectLocked(page, locked) {
    for (const id of PROTECTED) {
      if (locked) await expect(page.locator(`#${id}`), id).toHaveAttribute("inert", "");
      else await expect(page.locator(`#${id}`), id).not.toHaveAttribute("inert", "");
    }
  }

  test("legacy keys move to session storage and shared popover preserves focus", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("ocr_token", "migration-test-only"));
    await page.goto("/");
    expect(await page.evaluate(() => sessionStorage.getItem("ocr_api_key"))).toBe("migration-test-only");
    expect(await page.evaluate(() => localStorage.getItem("ocr_token"))).toBeNull();
    expect(await page.evaluate(() => localStorage.getItem("ocr_api_key"))).toBeNull();
    await page.locator("#apikey-btn").click();
    await expect(page.locator("#apikey-input")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.locator("#apikey-btn")).toBeFocused();
    await expect(page.locator("#apikey-popover")).toBeHidden();
    await page.locator("#apikey-btn").click();
    await page.locator("#apikey-clear").click();
    expect(await page.evaluate(() => sessionStorage.getItem("ocr_api_key"))).toBeNull();
  });

  test("missing key locks protected tabs until a key is registered", async ({ page }) => {
    await openHealthyConsole(page);
    await expect(page.locator("#service-gate [role=alert]")).toHaveText(MISSING);
    await expectLocked(page, true);
    await expect(page.locator("#apikey-btn")).toHaveClass(/is-bad/);
    await expect(page.locator("#parse-run")).toBeDisabled();
    await page.locator("#tab-monitor").click();
    await expect(page.locator("#panel-monitor [inert]")).toHaveCount(0);

    await page.locator("#apikey-btn").click();
    await page.locator("#apikey-input").fill("example-accepted-key");
    await page.locator("#apikey-save").click();
    await expect(page.locator("#service-gate")).toBeEmpty();
    await expectLocked(page, false);
    // 3S dot language: red turns into a blue flash that clears after 3 s.
    await expect(page.locator("#apikey-btn")).not.toHaveClass(/is-bad/);
    await expect(page.locator("#apikey-btn")).toHaveClass(/is-set/);
    await expect(page.locator("#apikey-popover")).toBeHidden();
    await expect(page.locator("#apikey-btn")).not.toHaveClass(/is-set/, { timeout: 5000 });

    await page.locator("#apikey-btn").click();
    await page.locator("#apikey-clear").click();
    await expect(page.locator("#service-gate [role=alert]")).toHaveText(MISSING);
    await expectLocked(page, true);
  });

  test("rejected stored key keeps the lock with the rejection message", async ({ page, expectedConsoleErrors }) => {
    expectedConsoleErrors.push(/Failed to load resource: the server responded with a status of 401/);
    await page.route("**/stats", route => route.fulfill({ status: 401, json: { code: "unauthorized" } }));
    await page.addInitScript(() => sessionStorage.setItem("ocr_api_key", "example-rejected-key"));
    await openHealthyConsole(page);
    await expect(page.locator("#service-gate [role=alert]"))
      .toHaveText("API KEY REJECTED — OPEN API KEY AND REGISTER THE CORRECT KEY");
    await expectLocked(page, true);
    await expect(page.locator("#apikey-btn")).toHaveClass(/is-bad/);
  });

  test("auth disabled server-side never locks for a missing key", async ({ page }) => {
    await page.route("**/assets/config.js", route => route.fulfill({
      contentType: "text/javascript",
      body: `window.OCR_CONFIG = ${JSON.stringify({ ...CONFIG, authRequired: false })};`,
    }));
    await openHealthyConsole(page);
    await expect(page.locator("#service-gate")).toBeEmpty();
    await expectLocked(page, false);
    await expect(page.locator("#apikey-btn")).not.toHaveClass(/is-bad/);
  });
});

test("API key popover stays inside a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.locator("#apikey-btn").click();
  const bounds = await page.locator("#apikey-popover").evaluate(element => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right, width: rect.width };
  });
  expect(bounds.left).toBeGreaterThanOrEqual(0);
  expect(bounds.right).toBeLessThanOrEqual(390);
});

test("API key indicator follows protected responses rather than stored values", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/Failed to load resource: the server responded with a status of 401/);
  await page.route("**/stats", route => {
    const accepted = route.request().headers().authorization === "Bearer example-accepted-key";
    return route.fulfill({
      status: accepted ? 200 : 401,
      json: accepted ? { active: 0, queue_size: 0 } : { code: "unauthorized" },
    });
  });
  await page.goto("/");
  await page.locator("#apikey-btn").click();
  await page.locator("#apikey-input").fill("example-rejected-key");
  await page.locator("#apikey-save").click();
  await expect(page.locator("#apikey-btn")).toHaveClass(/is-bad/);
  await page.locator("#apikey-input").fill("example-accepted-key");
  await page.locator("#apikey-save").click();
  await expect(page.locator("#apikey-btn")).toHaveClass(/is-set/);
  await expect(page.locator("#apikey-popover")).toBeHidden();
  await page.locator("#apikey-btn").click();
  await expect(page.locator("#apikey-btn")).toHaveClass(/is-set/);
  await page.locator("#apikey-clear").click();
  await expect(page.locator("#apikey-btn")).not.toHaveClass(/is-set/);
});

test("network failures resolve to the documented API envelope", async ({ page, expectedConsoleErrors }) => {
  expectedConsoleErrors.push(/Failed to load resource: net::ERR_FAILED/);
  await page.route("**/stats", route => route.abort());
  await page.goto("/");
  const response = await withApi(page, api => api.stats());
  expect(response).toMatchObject({ ok: false, status: 0, data: null });
  expect(typeof response.error).toBe("string");
  expect(typeof response.elapsed).toBe("number");
});
