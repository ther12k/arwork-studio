/**
 * Task 33 review closing gate — REAL interaction journey over the REAL studio
 * app (production `dist/` build: StudioPage → use-studio hook → CanvasWorkspace
 * → VectorBoard), driven through the route-intercepted FIXTURE BACKEND in
 * studio-fixture.ts ("controlled transport"). Nothing about editShape /
 * runEdit / saveArtPath is mocked: the hook's real guard logic, the real
 * overlay geometry and the real job polling decide the outcomes — only the
 * HTTP boundary is staged.
 *
 * Proven rows (reviewer's matrix):
 *   1. tapping the MIDDLE of a long outline selects the correct source shape,
 *   2. Art node saves with NO gameplay region selection (region_ids: []),
 *   3. the dragged-handle preview matches the geometry actually submitted,
 *   4. a rejected edit (409) and a FAILED JOB keep the draft editable and the
 *      healthy revision untouched,
 *   5. save success → reload → next revision serves the edited geometry under
 *      the SAME stable shape id,
 *   6. explicit Cancel sends no request and discards the draft.
 *
 * Not proven here (documented limits): real compiler recompilation (the
 * fixture backend stages revision rev-2 instead of running pipeline.py), and
 * multi-shape / open-path editing (deferred slices).
 */

import { expect, test, type Page } from "@playwright/test";

import { flattenPath } from "../../src/lib/svg-path";
import {
  EDITED_D,
  ORIGINAL_D,
  REV1,
  SHAPE,
  mkState,
  routeBackend,
  type FixtureState,
} from "./studio-fixture";

type Pt = { x: number; y: number };

/** The board svg's label is set by VectorBoard itself at mount. */
const BOARD = 'svg[aria-label*="interactive coloring artwork"]';

// --------------------------------------------------------------- page helpers

/** Map artwork coordinates to page pixels through the BOARD svg's real CTM.
 *  Scrolls the board into the viewport first: mouse events use viewport
 *  coordinates, and a half-scrolled canvas would map to negative y. */
const artToPage = async (page: Page, x: number, y: number): Promise<Pt> => {
  await page.locator(BOARD).scrollIntoViewIfNeeded();
  return page.evaluate(
    ([ax, ay]) => {
      const board = document.querySelector('svg[aria-label*="interactive coloring artwork"]') as SVGSVGElement;
      const p = new DOMPoint(ax, ay).matrixTransform(board.getScreenCTM()!);
      return { x: p.x, y: p.y };
    },
    [x, y]
  );
};

/** Read one pixel of the cached underpainting ART layer (real blob decode →
 *  canvas → getImageData). The cached image spans the base viewBox 1:1, so
 *  pixel (x,y) is artwork point (x,y). Returns null while no image is
 *  published. This is the honest geometry probe: in preview modes the app
 *  replaces live paint paths with this cached <image>, so path attributes
 *  are NOT visible in the DOM. */
const artPixel = (page: Page, x: number, y: number): Promise<readonly [number, number, number] | null> =>
  page.evaluate(
    ([px, py]) =>
      new Promise<readonly [number, number, number] | null>((resolve) => {
        const board = document.querySelector('svg[aria-label*="interactive coloring artwork"]');
        const img = board?.querySelector('image[id$="underpaint-art"]') as SVGImageElement | null;
        const href = img?.getAttribute("href");
        if (!href) return resolve(null);
        const image = new Image();
        image.onload = () => {
          const canvas = document.createElement("canvas");
          canvas.width = 300;
          canvas.height = 300;
          const ctx = canvas.getContext("2d")!;
          ctx.drawImage(image, 0, 0, 300, 300);
          const d = ctx.getImageData(px, py, 1, 1).data;
          resolve([d[0], d[1], d[2]]);
        };
        image.onerror = () => resolve(null);
        image.src = href;
      }),
    [x, y]
  );

const isBlue = (rgb: readonly [number, number, number] | null) =>
  !!rgb && Math.abs(rgb[0] - 51) < 40 && Math.abs(rgb[1] - 102) < 40 && Math.abs(rgb[2] - 204) < 40;
const isRed = (rgb: readonly [number, number, number] | null) =>
  !!rgb && Math.abs(rgb[0] - 204) < 40 && Math.abs(rgb[1] - 51) < 40 && Math.abs(rgb[2] - 51) < 40;

/** Poll the underpainting pixel until the color predicate holds. */
const expectPixel = async (
  page: Page,
  x: number,
  y: number,
  ok: (rgb: readonly [number, number, number] | null) => boolean,
  timeout = 20_000
) => {
  await expect
    .poll(async () => {
      const rgb = await artPixel(page, x, y);
      return ok(rgb);
    }, { timeout })
    .toBe(true);
};

/** Read the Art-node preview polyline back in ARTWORK coordinates (overlay
 *  CTM → screen → board CTM inverse) so it can be compared with the source
 *  path and with the submitted geometry. */
const previewArtPoints = (page: Page): Promise<Pt[]> =>
  page.evaluate(() => {
    const overlay = document.querySelector('svg[aria-label^="Drawing layer"]') as SVGSVGElement;
    const board = document.querySelector('svg[aria-label*="interactive coloring artwork"]') as SVGSVGElement;
    const poly = overlay.querySelector('g[aria-label^="Artwork path anchors"] polyline') as SVGPolylineElement;
    const toScreen = overlay.getScreenCTM()!;
    const toArt = board.getScreenCTM()!.inverse();
    return poly
      .getAttribute("points")!
      .trim()
      .split(/\s+/)
      .map((pair) => {
        const [x, y] = pair.split(",").map(Number);
        return new DOMPoint(x, y).matrixTransform(toScreen).matrixTransform(toArt);
      });
  });

const maxVertexDistance = (a: Pt[], b: Pt[]): number => {
  let worst = 0;
  for (const p of a) {
    let best = Infinity;
    for (const q of b) best = Math.min(best, Math.hypot(p.x - q.x, p.y - q.y));
    worst = Math.max(worst, best);
  }
  return worst;
};

const minDistToPoint = (pts: Pt[], p: Pt): number =>
  Math.min(...pts.map((q) => Math.hypot(q.x - p.x, q.y - p.y)));

const overlayGroup = (page: Page) =>
  page.locator(`g[aria-label="Artwork path anchors for shape ${SHAPE}"]`);

// ---------------------------------------------------------------- the journey

test("Art node journey: mid-edge pick, no-selection save, preview==payload, reject keeps draft, save survives reload, cancel sends nothing", async ({ page }) => {
  test.setTimeout(120_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  // Board mounted on the fixture: the live region layer carries our region,
  // and the cached underpainting renders the ORIGINAL geometry. (187,95)
  // sits BELOW the edited top curve (inside rev-2's blob) but ABOVE the
  // original one (outside rev-1's blob) — so red proves rev-1 geometry and
  // blue proves the edited geometry reached the canvas.
  const region = page.locator(`${BOARD} path[data-region-id="r-blue"]`);
  await expect(region).toBeAttached();
  await expectPixel(page, 187, 95, isRed);

  // Row 1 — select the MIDDLE of a long outline (not a corner): the top
  // cubic's t=0.5 point (150,90) selects shape s0002.
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap = await artToPage(page, 150, 90);
  await page.mouse.click(tap.x, tap.y);
  await expect(overlayGroup(page)).toBeVisible();

  // The untouched preview represents the SOURCE path (whole-path flatten:
  // continuous curves + closure).
  const source = await previewArtPoints(page);
  expect(maxVertexDistance(source, flattenPath(ORIGINAL_D))).toBeLessThan(2);

  // Row 3 (drag half) — drag Bézier handle 1 (c1 at (70,60)) to (170,60).
  // Map the drop point FIRST (it settles scrolling), then read the handle box
  // so both share the same scroll state.
  const handle = page.locator(
    'g[aria-label^="Artwork path anchors"] circle[aria-label="Bézier handle 1"]'
  );
  const drop = await artToPage(page, 170, 60);
  const hb = await handle.boundingBox();
  await page.mouse.move(hb!.x + hb!.width / 2, hb!.y + hb!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop.x, drop.y, { steps: 8 });
  await page.mouse.up();
  // The preview follows the EDITED geometry: it passes through the new curve
  // midpoint (187.5,90) and no longer through the old one (150,90). The
  // readback is calibrated on the UNTOUCHED M anchor first: capture-time vs
  // read-time CTMs can differ by a couple of subpixel units, which shows up
  // as a constant offset on every vertex (the drag itself is exact — the
  // submitted payload below matches to 2 decimals).
  const calibrate = (pts: Pt[], start: Pt): Pt[] => {
    const dx = start.x - pts[0].x;
    const dy = start.y - pts[0].y;
    return pts.map((p) => ({ x: p.x + dx, y: p.y + dy }));
  };
  const editedPreview = calibrate(await previewArtPoints(page), { x: 70, y: 180 });
  expect(minDistToPoint(editedPreview, { x: 187.5, y: 90 })).toBeLessThan(2);
  expect(minDistToPoint(editedPreview, { x: 150, y: 90 })).toBeGreaterThan(8);

  // Row 4a — Save is REJECTED at the HTTP boundary (409 stale base): the
  // toast fires, the draft stays editable, no job exists, revision untouched.
  await page.getByRole("button", { name: "Save path" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expect(page.getByText("Stale base revision")).toBeVisible();
  await expect(overlayGroup(page)).toBeVisible();
  expect(state.editCalls).toHaveLength(1);
  expect(state.revision).toBe(REV1);

  // Row 4b — Save is ACCEPTED but the JOB FAILS validation: the poller toasts,
  // busy clears, and the draft STILL waits for correction on the same revision.
  state.mode = "jobfail";
  await page.getByRole("button", { name: "Save path" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  // The toast is the poller's failure signal; the same message also lands in
  // the workspace job-status line, so assert on the toast element itself.
  await expect(page.locator('[data-sonner-toast]', { hasText: "not a simple ring" })).toBeVisible();
  await expect(overlayGroup(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Save path" })).toBeEnabled();
  expect(state.editCalls).toHaveLength(2);
  expect(state.revision).toBe(REV1);

  // Rows 2 + 3 (submit half) — the corrected save SUCCEEDS. The journey never
  // selected a gameplay region, so the payload must carry region_ids: [] and
  // the exact dragged geometry. The published revision re-renders the board:
  // the probe pixel flips red → blue (the edited outline now covers it).
  state.mode = "success";
  await page.getByRole("button", { name: "Save path" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expectPixel(page, 187, 95, isBlue);
  const payload = state.editCalls[state.editCalls.length - 1];
  expect(payload).toMatchObject({
    base_revision: REV1,
    action: "shape",
    shape_id: SHAPE,
    region_ids: [],
    d: EDITED_D,
  });
  // Success clears the draft (the settle-watch saw the NEW revision land).
  await expect(overlayGroup(page)).toHaveCount(0);

  // Row 5 — reload → the fixture "Build" serves rev-2: the SAME stable shape
  // id carries the edited geometry (pixel still blue after a fresh mount).
  await page.reload();
  await expect(page.locator(`${BOARD} path[data-region-id="r-blue"]`)).toBeAttached();
  await expectPixel(page, 187, 95, isBlue);

  // Row 6 — explicit Cancel: no request, draft discarded.
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap2 = await artToPage(page, 187.5, 90); // on the EDITED top curve
  await page.mouse.click(tap2.x, tap2.y);
  await expect(overlayGroup(page)).toBeVisible();
  const callsBeforeCancel = state.editCalls.length;
  const drop2 = await artToPage(page, 100, 40);
  const h2 = await handle.boundingBox();
  await page.mouse.move(h2!.x + h2!.width / 2, h2!.y + h2!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop2.x, drop2.y, { steps: 5 });
  await page.mouse.up();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(overlayGroup(page)).toHaveCount(0);
  await page.waitForTimeout(400);
  expect(state.editCalls).toHaveLength(callsBeforeCancel);
});
