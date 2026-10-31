import { expect, test as base } from "@playwright/test";

export const CONFIG = { authRequired: true, maxFileBytes: 10485760, maxPages: 12, requestTimeoutSeconds: 30 };
export const HEALTH = { status: "healthy", vllm: "reachable", model: "fake-model", version: "test", pipeline_version: "test", autotune: {}, storage: { status: "unknown", last_operation: null, timestamp: null } };
export const PIXEL_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
export const IMAGE = { name: "sample.png", mimeType: "image/png", buffer: Buffer.from(PIXEL_PNG, "base64") };
export const PARSE = { pages: [], processing_time_ms: 10, model_name: "fake-model", pipeline_version: "test", warnings: [] };
export const OCR = { result: "sample text", task: "ocr", boxes: null, processing_time_ms: 10, model_name: "fake-model", warnings: [] };
export const OTSL_TABLE_RESULT =
  "<fcel>Name<fcel>Value<fcel>Unit<nl>" +
  "<fcel>Alpha<fcel>100<fcel>kg<nl>" +
  "<fcel>Beta<fcel>250<fcel>m<nl>" +
  "<fcel>Gamma<fcel>3.14<fcel>rad<nl>";

export const test = base.extend({
  expectedConsoleErrors: async ({}, use) => { await use([]); },
  // CONFIG requires auth, so the console locks without a key: register one
  // once per tab by default. `test.use({ apiKey: "" })` starts keyless.
  apiKey: ["mock-session-key", { option: true }],
  page: async ({ page, apiKey, expectedConsoleErrors }, use) => {
    if (apiKey) await page.addInitScript(key => {
      if (sessionStorage.getItem("ocr_test_key_seeded")) return;
      sessionStorage.setItem("ocr_test_key_seeded", "1");
      sessionStorage.setItem("ocr_api_key", key);
    }, apiKey);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => {
      if (message.type() === "error" && !expectedConsoleErrors.some(pattern => pattern.test(message.text()))) errors.push(message.text());
    });
    await page.route("**/assets/config.js", route => route.fulfill({ contentType: "text/javascript", body: `window.OCR_CONFIG = ${JSON.stringify(CONFIG)};` }));
    await page.route("**/health", route => route.fulfill({ json: HEALTH }));
    await page.route("**/stats", route => route.fulfill({ json: { active: 0, queue_size: 0 } }));
    await page.route("**/storage/archives?**", route => route.fulfill({ json: { items: [], next_cursor: null } }));
    await page.route("**/metrics", route => route.fulfill({ contentType: "text/plain", body: 'process_start_time_seconds 100\nocr_requests_total{endpoint="parse",status="200"} 10\n' }));
    await use(page);
    expect(errors, "unexpected browser errors").toEqual([]);
  },
});

export { expect };

export async function openHealthyConsole(page, hash = "") {
  await page.goto(`/${hash}`);
  await expect(page.locator("#conn-health-status")).toHaveText("HEALTHY");
}

// Import the same module URL as app.js; version queries are not duplicated here.
export async function withApi(page, operation) {
  return page.evaluate(async source => {
    const appURL = document.querySelector('script[type="module"]').src;
    const app = await (await fetch(appURL)).text();
    const url = app.match(/import \{ api \} from "([^"]+)"/)[1];
    const { api } = await import(url);
    return new Function("api", `return (${source})(api)`)(api);
  }, operation.toString());
}
