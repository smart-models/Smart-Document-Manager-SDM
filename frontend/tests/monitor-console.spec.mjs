import { test, expect, HEALTH, openHealthyConsole } from "./fixtures.mjs";

test("monitor separates profile choices from effective settings", async ({ page }) => {
  await page.route("**/health", route => route.fulfill({ json: { ...HEALTH, autotune: {
    decisions: { vl_lanes: 8 }, effective: { vl_lanes: 2, page_parallelism: 1, layout_threads: null },
  } } }));
  await openHealthyConsole(page, "#monitor");
  const panel = page.locator("#mon-autotune");
  await expect(panel.locator(".metric").filter({ hasText: "vl lanes (profile)" }).locator(".metric-value")).toHaveText("8");
  await expect(panel.locator(".metric").filter({ hasText: "vl lanes (in effect)" }).locator(".metric-value")).toHaveText("2");
  await expect(panel.locator(".metric").filter({ hasText: "layout threads (in effect)" })).toContainText("default");
  await panel.screenshot({ path: "../docs/verification/monitor-audit/autotune-fixed-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.screenshot({ path: "../docs/verification/monitor-audit/autotune-fixed-mobile.png" });
});

test("monitor rejects malformed metrics without zero samples and recovers", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page, "#monitor");
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await page.clock.fastForward(5000);
  const plot = page.locator("#mon-chart-plot");
  await expect(plot.locator("svg")).toHaveCount(1);
  const oldChart = await plot.innerHTML();
  await page.route("**/metrics", route => route.fulfill({ body: "<html>Not metrics</html>" }));
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-table")).toContainText("Invalid metrics response");
  await expect(page.locator("#mon-updated")).toContainText("refresh failed");
  await expect(page.locator("#mon-reset")).toBeDisabled();
  await expect(page.locator("#mon-chart-stale")).toBeVisible();
  expect(await plot.innerHTML()).toBe(oldChart);
  await page.unroute("**/metrics");
  await page.route("**/metrics", route => route.fulfill({ body: 'process_start_time_seconds 100\nocr_requests_total{endpoint="parse",status="200"} 11\n' }));
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await expect(page.locator("#mon-updated")).not.toContainText("refresh failed");
  await expect(page.locator("#mon-chart-stale")).toBeHidden();
});

test("monitor rejects invalid stats and health even with HTTP 200", async ({ page }) => {
  await page.clock.install();
  await openHealthyConsole(page, "#monitor");
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await page.route("**/stats", route => route.fulfill({ json: { queue_size: -1, active: {} } }));
  await page.route("**/health", route => route.fulfill({ json: { ...HEALTH, vllm: 42 } }));
  await page.clock.fastForward(10000);
  await expect(page.locator("#mon-stats")).toContainText("Invalid stats response");
  await expect(page.locator("#mon-stats .metric")).toHaveCount(0);
  await expect(page.locator("#mon-badges")).toContainText("UNKNOWN");
  await expect(page.locator("#mon-model")).toContainText("invalid status or model metadata");
  await expect(page.locator("#mon-updated")).toContainText("refresh failed");
  await expect(page.locator("#conn-health-status")).toHaveText("UNREACHABLE");
});

test("monitor clears tooltip and keyboard selection after refresh and restart", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install();
  let started = 100, requests = 10;
  await page.route("**/metrics", route => route.fulfill({ body: `process_start_time_seconds ${started}\nocr_requests_total{endpoint="parse",status="200"} ${requests}\n` }));
  await openHealthyConsole(page, "#monitor");
  await expect(page.locator("#mon-reset")).toBeEnabled();
  await page.clock.fastForward(5000);
  const chart = page.locator("#mon-chart");
  await expect(chart.locator("svg")).toHaveCount(1);
  await chart.focus();
  await chart.press("End");
  await expect(page.locator("#mon-chart-tip")).toContainText("10 requests");
  await expect(page.locator("#mon-chart-read")).toContainText("Sample 2 of 2");
  started = 200; requests = 1;
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-chart-plot")).toContainText("Collecting");
  await expect(page.locator("#mon-chart-tip")).toBeHidden();
  await expect(page.locator("#mon-chart-cross")).toBeHidden();
  await expect(page.locator("#mon-chart-read")).toBeEmpty();
  requests = 2;
  await page.clock.fastForward(5000);
  await expect(chart.locator("svg")).toHaveCount(1);
  await chart.press("End");
  await expect(page.locator("#mon-chart-tip")).toContainText("2 requests");
  requests = 3;
  await page.clock.fastForward(5000);
  await expect(page.locator("#mon-chart-tip")).toBeHidden();
  await expect(page.locator("#mon-chart-read")).toBeEmpty();
  await chart.press("ArrowLeft");
  await expect(page.locator("#mon-chart-read")).toContainText("Sample 2 of 3");
  await page.locator("#mon-reset").click();
  await expect(page.locator("#mon-chart-tip")).toBeHidden();
});
