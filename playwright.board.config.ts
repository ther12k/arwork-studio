import { defineConfig } from "@playwright/test";

/** Task 32 review round-2 — real-browser VectorBoard gate.
 *  Serves the prebuilt harness (bun run browser:build) from a static file
 *  server; no Studio/AI stack involved. */
export default defineConfig({
  testDir: "tests/browser",
  testMatch: /board\.spec\.ts$/,
  timeout: 30_000,
  forbidOnly: true,
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
  },
  webServer: {
    command: "node tests/static-server.mjs 4173 tests/browser/dist",
    url: "http://127.0.0.1:4173/harness.html",
    reuseExistingServer: true,
    timeout: 15_000,
  },
});
