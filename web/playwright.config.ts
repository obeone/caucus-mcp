/**
 * Playwright configuration for Caucus dashboard E2E tests.
 *
 * The global setup (e2e/global-setup.ts) boots the real Python hub as a
 * subprocess, waits for it to serve /, then tears it down in global teardown.
 * Tests drive the already-built bundle served by the hub.
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  /* Maximum time one test can run. */
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  /* Run tests serially in one worker to avoid port conflicts. */
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],

  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",

  use: {
    /* Base URL is set by global-setup and written to process.env.E2E_BASE_URL */
    baseURL: process.env["E2E_BASE_URL"] ?? "http://127.0.0.1:9765",
    trace: "on-first-retry",
    headless: true,
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
