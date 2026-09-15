import { defineConfig } from "@playwright/test";

/** Task 36 — REAL Color Duel game gate.
 *  prepare_game.py builds the actual game repo (COLOR_DUEL_DIR or ../color-duel,
 *  pinned in tests/game-integration/.serve/game-pin.json) and stages the
 *  UNCHANGED golden pack exports into its static artworks area. The webServer
 *  runs prepare (idempotent) and serves the result — static files only, no
 *  Studio backend anywhere (deployment independence). */
export default defineConfig({
  testDir: "tests/game-integration",
  testMatch: /game\.spec\.ts$/,
  timeout: 120_000,
  forbidOnly: true,
  use: {
    baseURL: "http://127.0.0.1:4176",
    headless: true,
  },
  webServer: {
    command: "python3 tests/game-integration/prepare_game.py && python3 -m http.server 4176 --bind 127.0.0.1 -d tests/game-integration/.serve",
    url: "http://127.0.0.1:4176/index.html",
    reuseExistingServer: true,
    timeout: 600_000,
  },
});
