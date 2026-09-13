/**
 * Task 32 review round-2 — browser gates for the board object-visibility
 * cache (real Chromium, real SVG/blob decode; no jsdom substitute).
 */

import { expect, test } from "@playwright/test";

interface BoardHooks {
  setHidden(ids: string[]): void;
  isolate(id: string | null): void;
  cachedState(): { artHref: string | null; inkHref: string | null };
  pixelArt(x: number, y: number): Promise<readonly [number, number, number, number] | null>;
  pixelInk(x: number, y: number): Promise<readonly [number, number, number, number] | null>;
  settle(ms?: number): Promise<void>;
  regionClass(id: string): string;
}

const rgb = (p: readonly [number, number, number, number]) => `#${[p[0], p[1], p[2]].map((v) => v.toString(16).padStart(2, "0")).join("")}`;

test.beforeEach(async ({ page }) => {
  await page.goto("/harness.html");
  await page.waitForFunction(() => (window as unknown as { __board?: unknown }).__board !== undefined);
  await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    await b.settle(400); // first underpainting image decode
  });
});

test("initial cache: overlap pixel answers the TOP object (blue), background answers red", async ({ page }) => {
  const state = await page.evaluate(() => (window as unknown as { __board: BoardHooks }).__board.cachedState());
  expect(state.artHref).toBeTruthy();
  const overlap = await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(140, 140);
  });
  expect(rgb(overlap!)).toBe("#3366cc");
  const corner = await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(50, 50);
  });
  expect(rgb(corner!)).toBe("#cc3333");
});

test("hide the LAST visible shape: cached art image is CLEARED, pixels gone (round-2 R2)", async ({ page }) => {
  await page.evaluate(() => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    b.setHidden(["obj-red"]);
  });
  await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    await b.settle();
  });
  let state = await page.evaluate(() => (window as unknown as { __board: BoardHooks }).__board.cachedState());
  expect(state.artHref).toBeTruthy(); // red hidden, blue art remains
  expect(rgb((await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(50, 50);
  }))!)).toBe("#ffffff"); // red pixels really gone

  // now hide the LAST visible object -> EMPTY body must CLEAR the layer
  await page.evaluate(() => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    b.setHidden(["obj-red", "obj-blue"]);
  });
  await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    await b.settle();
  });
  state = await page.evaluate(() => (window as unknown as { __board: BoardHooks }).__board.cachedState());
  expect(state.artHref).toBeNull(); // the old code kept the stale image here
  expect(state.inkHref).toBeNull(); // ink (owned by blue) cleared too
  const clearedPixel = await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(140, 140);
  });
  expect(clearedPixel).toBeNull(); // probe treats cleared layer as nothing painted
});

test("rapid visibility changes: the FINAL state wins, no stale image republish", async ({ page }) => {
  for (let i = 0; i < 25; i++) {
    await page.evaluate((hidden) => {
      const b = (window as unknown as { __board: BoardHooks }).__board;
      b.setHidden(hidden ? ["obj-red", "obj-blue"] : []);
    }, i % 2 === 0);
  }
  // ends on i=24 -> hidden=true (last call hid everything)
  await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    await b.settle();
  });
  let state = await page.evaluate(() => (window as unknown as { __board: BoardHooks }).__board.cachedState());
  expect(state.artHref).toBeNull();

  // one more toggle cycle ending visible — the generation token must let the
  // newest result through and never resurrect an older hidden/visible mix
  for (let i = 0; i < 10; i++) {
    await page.evaluate((visible) => {
      const b = (window as unknown as { __board: BoardHooks }).__board;
      b.setHidden(visible ? [] : ["obj-red", "obj-blue"]);
    }, i % 2 === 1);
  }
  await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    await b.settle();
  });
  state = await page.evaluate(() => (window as unknown as { __board: BoardHooks }).__board.cachedState());
  expect(state.artHref).toBeTruthy();
  const overlap = await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(140, 140);
  });
  expect(rgb(overlap!)).toBe("#3366cc");
});

test("isolate renders only the isolated subtree's artwork", async ({ page }) => {
  await page.evaluate(() => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    b.isolate("obj-red");
  });
  await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    await b.settle();
  });
  // blue-only corner must now be EMPTY (white) — blue is isolated away
  const blueOnly = await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(250, 250);
  });
  expect(rgb(blueOnly!)).toBe("#ffffff");
  // red-only area still red
  const redOnly = await page.evaluate(async () => {
    const b = (window as unknown as { __board: BoardHooks }).__board;
    return await b.pixelArt(50, 50);
  });
  expect(rgb(redOnly!)).toBe("#cc3333");
});
