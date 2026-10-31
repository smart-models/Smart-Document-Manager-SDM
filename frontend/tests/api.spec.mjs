import { expect, test, openHealthyConsole, withApi } from "./fixtures.mjs";

test("missing or invalid uploads return an envelope without a POST", async ({ page }) => {
  await openHealthyConsole(page);
  let posts = 0;
  page.on("request", request => { if (request.method() === "POST") posts++; });
  const responses = await withApi(page, api => Promise.all([api.parseFile(null), api.ocrFile("bad")]));
  for (const response of responses) expect(response).toMatchObject({ ok: false, status: 0, data: null, elapsed: 0 });
  expect(posts).toBe(0);
});

test("diagnostic timeout covers body reads and differs from voluntary cancellation", async ({ page }) => {
  await openHealthyConsole(page);
  const results = await withApi(page, async api => {
    const originalFetch = window.fetch;
    window.fetch = async (_url, options) => ({
      ok: true, status: 200,
      text: () => new Promise((resolve, reject) => {
        if (options.signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
        options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      }),
    });
    try {
      const timedOut = await api.stats({ timeoutMs: 15 });
      const controller = new AbortController();
      const pending = api.stats({ signal: controller.signal });
      controller.abort();
      return { timedOut, cancelled: await pending };
    } finally { window.fetch = originalFetch; }
  });
  expect(results.timedOut.error).toContain("timed out");
  expect(results.cancelled.error).toBe("Request cancelled.");
  expect(results.timedOut.elapsed).toBeGreaterThan(0);
});

test("request deadlines follow configuration and successful bodies clear timers", async ({ page }) => {
  await openHealthyConsole(page);
  const result = await withApi(page, async api => {
    const originalFetch = window.fetch;
    const originalSet = window.setTimeout;
    const originalClear = window.clearTimeout;
    const timers = [];
    const cleared = [];
    window.setTimeout = (_fn, delay) => { timers.push(delay); return timers.length; };
    window.clearTimeout = id => cleared.push(id);
    window.fetch = async () => ({ ok: true, status: 200, text: async () => "42", headers: { get: () => null } });
    try {
      await api.health();
      const metrics = await api.metrics();
      await api.parseFile(new Blob(["x"]));
      window.OCR_CONFIG.requestTimeoutSeconds = NaN;
      await api.ocrFile(new Blob(["x"]));
      return { timers, cleared, metrics };
    } finally { window.fetch = originalFetch; window.setTimeout = originalSet; window.clearTimeout = originalClear; }
  });
  expect(result.timers).toEqual([5000, 5000, 90000, 660000]);
  expect(result.cleared).toEqual([1, 2, 3, 4]);
  expect(result.metrics.data).toBe("42");
});

test("storage disabled by browser policy leaves the console and active token usable", async ({ page }) => {
  await page.addInitScript(() => {
    for (const name of ["localStorage", "sessionStorage"]) Object.defineProperty(window, name, { get() { throw new DOMException("denied", "SecurityError"); } });
  });
  await openHealthyConsole(page);
  const token = await withApi(page, api => { api.saveToken("test-only"); return api.getToken(); });
  expect(token).toBe("test-only");
});
