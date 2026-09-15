import { defineConfig } from "@playwright/test";

/** Task 33 review closing gate — real-app Studio journey.
 *  Serves the PRODUCTION build (`dist/`, produced by `bun run build`) on a
 *  static file server and intercepts the studio-api HTTP boundary with fixture
 *  routes from the spec itself. Companion to playwright.board.config.ts
 *  (VectorBoard layer gate); testMatch keeps the two gates disjoint. */
export default defineConfig({
  testDir: "tests/browser",
  // Anchored to the segment before the file name: an unanchored "studio"
  // would match every spec path in a checkout whose directory is …-studio.
  testMatch: /(^|\/)studio\.spec\.ts$/,
  timeout: 120_000,
  forbidOnly: true,
  use: {
    baseURL: "http://127.0.0.1:4174",
    headless: true,
  },
  webServer: {
    command: "node .toolchain/static-server.mjs 4174 dist",
    url: "http://127.0.0.1:4174/index.html",
    reuseExistingServer: true,
    timeout: 15_000,
  },
});
