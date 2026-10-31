import { defineConfig } from '@playwright/test';

// Separate from console E2E: local HTML only, no harness or live service setup.
export default defineConfig({
  testDir: './guide-tests',
  testMatch: '*.spec.js',
  timeout: 30_000,
  fullyParallel: true,
  workers: 2,
  reporter: 'list',
  outputDir: './test-results/guides',
  use: { browserName: 'chromium', headless: true, reducedMotion: 'reduce' },
});
