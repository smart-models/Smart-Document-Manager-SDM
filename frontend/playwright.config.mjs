import { defineConfig } from "@playwright/test";

const mockURL = "http://127.0.0.1:8129";
// Workers reload this module without the coordinator's CLI arguments. Carry
// the explicit opt-in through their inherited environment so project names
// remain identical; the marker exists only within this Playwright process tree.
const liveRequested = process.env._OCR_PLAYWRIGHT_LIVE_RUN === "1"
  || process.argv.some((arg, index) => arg === "--project=live"
  || (arg === "--project" && process.argv[index + 1] === "live"));
if (liveRequested) process.env._OCR_PLAYWRIGHT_LIVE_RUN = "1";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  workers: 1,
  use: { baseURL: mockURL, trace: "retain-on-failure" },
  webServer: liveRequested ? undefined : {
    command: "node tests/static-server.mjs",
    url: mockURL,
    reuseExistingServer: false,
  },
  projects: liveRequested ? [{
      name: "live",
      testMatch: /live\.spec\.mjs$/,
      use: { baseURL: process.env.OCR_UI_BASE_URL },
  }] : [{ name: "mock", testMatch: /\.spec\.mjs$/, testIgnore: /live\.spec\.mjs$/ }],
});
