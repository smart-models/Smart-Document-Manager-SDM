import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import test from "node:test";

test("the default project covers every offline spec and excludes live specs", async () => {
  const { default: config } = await import("../playwright.config.mjs");
  const mock = config.projects.find(project => project.name === "mock");
  assert.ok(mock);
  for (const name of readdirSync(new URL("./", import.meta.url)).filter(name => name.endsWith(".spec.mjs"))) {
    const selected = mock.testMatch.test(name) && !mock.testIgnore.test(name);
    assert.equal(selected, !name.endsWith("live.spec.mjs"), name);
  }
});

test("Playwright keeps default offline and propagates explicit live selection to workers", () => {
  const configURL = new URL("../playwright.config.mjs", import.meta.url).href;
  const loadConfig = `
    const { default: config } = await import(${JSON.stringify(configURL)});
    console.log(JSON.stringify({ projects: config.projects.map(p => p.name), webServer: Boolean(config.webServer) }));
  `;
  const env = { ...process.env };
  delete env._OCR_PLAYWRIGHT_LIVE_RUN;
  const run = source => JSON.parse(execFileSync(process.execPath, ["--input-type=module", "-e", source], { env, encoding: "utf8" }));
  assert.deepEqual(run(loadConfig), { projects: ["mock"], webServer: true });
  for (const args of [["--project=live"], ["--project", "live"]]) {
    const workerConfig = run(`
      import { execFileSync } from "node:child_process";
      process.argv.push(...${JSON.stringify(args)});
      await import(${JSON.stringify(configURL)});
      // A fresh worker has no --project argument and inherits the marker.
      process.stdout.write(execFileSync(process.execPath, ["--input-type=module", "-e", ${JSON.stringify(loadConfig)}], { env: process.env, encoding: "utf8" }));
    `);
    assert.deepEqual(workerConfig, { projects: ["live"], webServer: false });
  }
});
