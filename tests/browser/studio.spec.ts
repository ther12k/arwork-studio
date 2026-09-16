/**
 * Task 33 review closing gate — REAL interaction journey over the REAL studio
 * app (production `dist/` build: StudioPage → use-studio hook → CanvasWorkspace
 * → VectorBoard), driven through the route-intercepted FIXTURE BACKEND in
 * studio-fixture.ts ("controlled transport"). Nothing about editShape /
 * runEdit / submitArtSave is mocked: the hook's real guard logic, the real
 * overlay geometry, the real job polling and the real settle-watch decide the
 * outcomes — only the HTTP boundary is staged.
 *
 * Proven rows (reviewer's matrix + round-2 follow-ups):
 *   1. tapping the MIDDLE of a long outline selects the correct source shape,
 *   2. Art node saves with NO gameplay region selection (region_ids: []),
 *   3. the dragged-handle preview matches the geometry actually submitted
 *      (and the UNCORRECTED overlay readback is bounded — calibration can only
 *      absorb a small drift, never a real misalignment),
 *   4. a rejected edit (409) and a FAILED JOB keep the draft editable and the
 *      healthy revision untouched,
 *   4b. revision-conflict recovery is separate from request rejection: an
 *      UNRELATED operation (object rename) advances the project while the
 *      draft is dirty — the draft is retained, the stale base is reported,
 *      and Save demands an EXPLICIT choice; saving applies on top of the
 *      CURRENT revision (base rev-2), never a silent rebase or dismissal,
 *   5. save success → reload → the later revision serves the edited geometry
 *      under the SAME stable shape id,
 *   6. explicit Cancel sends no request and discards the draft.
 *
 * Not proven here (documented limits): real compiler recompilation (the
 * fixture backend stages revisions instead of running pipeline.py — the
 * composition chain with the real compiler lives in the backend test suite),
 * and multi-shape / open-path editing (deferred slices).
 */

import { expect, test, type Page } from "@playwright/test";

import { flattenPath } from "../../src/lib/svg-path";
import {
  EDITED_D,
  INK,
  ORIGINAL_D,
  REV1,
  REV2,
  REV3,
  SHAPE,
  mkState,
  routeBackend,
  type FixtureState,
} from "./studio-fixture";

void EDITED_D;
void ORIGINAL_D;

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

/** One-directional point-cloud → polyline distance: every point of `a` vs
 *  the SEGMENTS of `b`. Sampling-invariant geometry comparison — comparing
 *  two flattenings of the SAME curve as point sets fails on sample-offset
 *  (a split segment samples at shifted parameters), while this measures the
 *  curves themselves. */
const hausdorffToPolyline = (a: Pt[], b: Pt[]): number => {
  let worst = 0;
  for (const p of a) {
    let best = Infinity;
    for (let i = 1; i < b.length; i++) {
      const dx = b[i].x - b[i - 1].x;
      const dy = b[i].y - b[i - 1].y;
      const lenSq = dx * dx + dy * dy;
      let t = 0;
      if (lenSq > 0) {
        t = Math.max(0, Math.min(1, ((p.x - b[i - 1].x) * dx + (p.y - b[i - 1].y) * dy) / lenSq));
      }
      best = Math.min(
        best,
        Math.hypot(p.x - (b[i - 1].x + t * dx), p.y - (b[i - 1].y + t * dy))
      );
    }
    worst = Math.max(worst, best);
  }
  return worst;
};

const overlayGroup = (page: Page) =>
  page.locator(`g[aria-label="Artwork path anchors for shape ${SHAPE}"]`);

const saveBar = (page: Page) => page.getByRole("button", { name: "Save path" });

// ---------------------------------------------------------------- the journey

test("Art node journey: mid-edge pick, no-selection save, preview==payload, reject + conflict keep the draft, save survives reload, cancel sends nothing", async ({ page }) => {
  test.setTimeout(150_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  // Board mounted on the fixture: the live region layer carries our region,
  // and the cached underpainting renders the ORIGINAL geometry. (187,95)
  // sits BELOW the edited top curve (inside rev-3's blob) but ABOVE the
  // original one (outside rev-1's blob) — so red proves original geometry
  // and blue proves the edited geometry reached the canvas.
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
  // Follow-up 2 — TWO separate properties. (a) The UNCORRECTED readback is
  // bounded: the untouched M anchor must sit within ~3 art units (~6 screen
  // px at this canvas size) of its true position, so the calibration below
  // can only ever absorb small capture-time drift, never a real overlay
  // misalignment. (b) Calibrated on that anchor, the preview follows the
  // EDITED geometry: it passes through the new curve midpoint (187.5,90) and
  // no longer through the old one (150,90).
  const rawPreview = await previewArtPoints(page);
  expect(Math.hypot(rawPreview[0].x - 70, rawPreview[0].y - 180)).toBeLessThan(3);
  const calibrate = (pts: Pt[], start: Pt): Pt[] => {
    const dx = start.x - pts[0].x;
    const dy = start.y - pts[0].y;
    return pts.map((p) => ({ x: p.x + dx, y: p.y + dy }));
  };
  const editedPreview = calibrate(rawPreview, { x: 70, y: 180 });
  expect(minDistToPoint(editedPreview, { x: 187.5, y: 90 })).toBeLessThan(2);
  expect(minDistToPoint(editedPreview, { x: 150, y: 90 })).toBeGreaterThan(8);

  // Row 4a — Save is REJECTED at the HTTP boundary (409 stale base): the
  // toast fires, the draft stays editable, no job exists, revision untouched.
  await saveBar(page).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expect(page.locator('[data-sonner-toast]', { hasText: "Stale base revision" })).toBeVisible();
  await expect(overlayGroup(page)).toBeVisible();
  expect(state.editCalls).toHaveLength(1);
  expect(state.revision).toBe(REV1);

  // Row 4b — Save is ACCEPTED but the JOB FAILS validation: the poller toasts,
  // busy clears, and the draft STILL waits for correction on the same revision.
  state.mode = "jobfail";
  await saveBar(page).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expect(page.locator('[data-sonner-toast]', { hasText: "not a simple ring" })).toBeVisible();
  await expect(overlayGroup(page)).toBeVisible();
  await expect(saveBar(page)).toBeEnabled();
  expect(state.editCalls).toHaveLength(2);
  expect(state.revision).toBe(REV1);

  // Row 4c — REVISION CONFLICT, distinct from request rejection: an unrelated
  // operation (renaming the owning object in the inspector) advances the
  // project to rev-2 while the draft is dirty. The draft must SURVIVE, the
  // stale base must be REPORTED, and the artwork itself is untouched (the
  // probe pixel stays red).
  await page.getByTitle("Blue", { exact: true }).click();
  await page.getByTitle("Rename object").click();
  await page.locator('input[value="Blue"]').fill("Blue Renamed");
  await page.locator('input[value="Blue Renamed"]').press("Enter");
  // (Task 40B: picking the filled shape ALSO seeds an appearance draft, so
  // both drafts report the conflict — anchor the geometry chip's exact text.)
  await expect(page.getByText(/^Base changed: loaded from rev-1, project now at rev-2/)).toBeVisible({ timeout: 20_000 });
  await expect(saveBar(page)).toBeVisible();
  await expect(saveBar(page)).toBeEnabled();
  expect(state.revision).toBe(REV2);
  expect(state.editCalls).toHaveLength(2); // no accidental save
  await expectPixel(page, 187, 95, isRed); // rename never touched the artwork

  // Saving the conflicted draft demands the EXPLICIT choice (the conflict
  // dialog, not the normal confirmation) — and states honestly that the
  // shape's outline is unchanged in between.
  state.mode = "success";
  await saveBar(page).click();
  await expect(page.getByText("The artwork changed since this shape was loaded")).toBeVisible();
  await expect(page.getByText("outline is unchanged in between", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: `Save against ${REV2}` }).click();
  // The save applies on TOP of the current revision: payload base is rev-2
  // (never a silent rebase of the stale rev-1 draft), region_ids stay [],
  // and the published rev-3 renders the edited geometry.
  await expectPixel(page, 187, 95, isBlue, 20_000);
  const payload = state.editCalls[state.editCalls.length - 1];
  expect(payload).toMatchObject({
    base_revision: REV2,
    action: "shape",
    shape_id: SHAPE,
    region_ids: [],
    d: EDITED_D,
  });
  // Success clears the draft (the settle-watch attributed the published job).
  await expect(saveBar(page)).toHaveCount(0);

  // Row 5 — reload → the fixture serves rev-3: the SAME stable shape id
  // carries the edited geometry (pixel still blue after a fresh mount).
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

// ------------------------------------------------- cross-project draft safety

test("foreign-project draft: suspended, unsendable, recoverable — never written into another project", async ({ page }) => {
  test.setTimeout(120_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  // Dirty a draft on project A (shape s0002, handle dragged).
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap = await artToPage(page, 150, 90);
  await page.mouse.click(tap.x, tap.y);
  await expect(overlayGroup(page)).toBeVisible();
  const handle = page.locator('circle[aria-label="Bézier handle 1"]');
  const drop = await artToPage(page, 170, 60);
  const hb = await handle.boundingBox();
  await page.mouse.move(hb!.x + hb!.width / 2, hb!.y + hb!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop.x, drop.y, { steps: 6 });
  await page.mouse.up();

  // Open the save CONFIRMATION, then switch projects with it open. If the
  // modal layer blocks the switch, close the dialog and continue — the
  // functional guard (submitArtSave re-checks identity) is additionally
  // proven by the disabled action below.
  await saveBar(page).click();
  const dialog = page.getByRole("alertdialog");
  const dialogOpen = await dialog.isVisible().catch(() => false);
  if (dialogOpen) {
    const combobox = page.getByRole("combobox", { name: "Workspace" });
    const switched = await combobox
      .click({ timeout: 2_000 })
      .then(() => true)
      .catch(() => false);
    if (!switched) await dialog.getByRole("button", { name: "Cancel" }).click();
  }
  await page.getByRole("combobox", { name: "Workspace" }).click();
  await page.getByRole("option", { name: "Project B" }).click();

  // On B: the draft from A survives (workspace stays mounted) but is
  // SUSPENDED — chip explains, Save disabled, zero edit requests so far.
  await expect(page.getByText("Draft from another project", { exact: false })).toBeVisible({ timeout: 15_000 });
  await expect(saveBar(page)).toBeDisabled();
  expect(state.editCalls).toHaveLength(0);
  // B's own board mounted (its single region is live).
  await expect(page.locator(`${BOARD} path[data-region-id="r-b1"]`)).toBeAttached();

  // Even a disabled-action attempt sends nothing.
  await saveBar(page).click({ force: true }).catch(() => {});
  await page.waitForTimeout(300);
  expect(state.editCalls).toHaveLength(0);

  // Switch back to A: the draft is recoverable — chip gone, Save enabled,
  // and saving publishes to A (base rev-1), never B.
  await page.getByRole("combobox", { name: "Workspace" }).click();
  await page.getByRole("option", { name: "Journey fixture" }).click();
  await expect(page.getByText("Draft from another project", { exact: false })).toHaveCount(0, { timeout: 15_000 });
  await expect(saveBar(page)).toBeEnabled();
  state.mode = "success";
  await saveBar(page).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expectPixel(page, 187, 95, isBlue, 20_000);
  const payload = state.editCalls[state.editCalls.length - 1];
  expect(state.editCalls).toHaveLength(1);
  expect(payload).toMatchObject({ base_revision: REV1, action: "shape", shape_id: SHAPE });
  // B was never written: its revision never advanced.
  expect(state.revision).toBe(REV2); // A advanced rev-1 → rev-2 only
});

// ------------------------------------------------- local draft undo/redo

test("Art node local draft undo/redo: debounced drag step, keyboard shortcuts, input isolation, and visual save persistence", async ({ page }) => {
  test.setTimeout(120_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  // Mount board, switch to Art node tool, and pick shape s0002.
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap = await artToPage(page, 150, 90);
  await page.mouse.click(tap.x, tap.y);
  await expect(overlayGroup(page)).toBeVisible();

  const undoBtn = page.getByRole("button", { name: "Undo", exact: true });
  const redoBtn = page.getByRole("button", { name: "Redo", exact: true });

  // Initial state: clean draft, undo and redo both disabled, status shows unchanged.
  await expect(undoBtn).toBeDisabled();
  await expect(redoBtn).toBeDisabled();
  await expect(page.getByText("unchanged", { exact: false })).toBeVisible();

  // Regression 1: Drag handle with 30 pointermove steps -> exactly 1 undo step.
  const handle1 = page.locator('circle[aria-label="Bézier handle 1"]');
  const h1b = await handle1.boundingBox();
  const drop1 = await artToPage(page, 170, 60);
  await page.mouse.move(h1b!.x + h1b!.width / 2, h1b!.y + h1b!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop1.x, drop1.y, { steps: 30 });
  await page.mouse.up();

  // After 30 moves, undo is enabled, redo is disabled, preview shifted to (187.5, 90).
  await expect(undoBtn).toBeEnabled();
  await expect(redoBtn).toBeDisabled();
  await expect(page.getByText("modified", { exact: false })).toBeVisible();
  const p1 = await previewArtPoints(page);
  expect(minDistToPoint(p1, { x: 187.5, y: 90 })).toBeLessThan(3);

  // Regression 2: Ctrl+Z returns geometry to pre-drag position; redo restores dragged position.
  await page.keyboard.press("Control+z");
  await expect(undoBtn).toBeDisabled(); // Proves only 1 undo step was recorded despite 30 moves!
  await expect(redoBtn).toBeEnabled();
  await expect(page.getByText("unchanged", { exact: false })).toBeVisible();
  const p0 = await previewArtPoints(page);
  expect(minDistToPoint(p0, { x: 150, y: 90 })).toBeLessThan(3);

  // Redo via keyboard shortcut (Control+Shift+Z)
  await page.keyboard.press("Control+Shift+z");
  await expect(undoBtn).toBeEnabled();
  await expect(redoBtn).toBeDisabled();
  await expect(page.getByText("modified", { exact: false })).toBeVisible();
  const p1Again = await previewArtPoints(page);
  expect(minDistToPoint(p1Again, { x: 187.5, y: 90 })).toBeLessThan(3);

  // Regression 3: Three sequential drags -> three ordered history steps.
  // Drag 2: Drag handle 2 to (220, 60)
  const handle2 = page.locator('circle[aria-label="Bézier handle 2"]');
  const h2b = await handle2.boundingBox();
  const drop2 = await artToPage(page, 220, 60);
  await page.mouse.move(h2b!.x + h2b!.width / 2, h2b!.y + h2b!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop2.x, drop2.y, { steps: 10 });
  await page.mouse.up();

  // Drag 3: Drag anchor 1 to (80, 190)
  const anchor1 = page.locator('circle[aria-label="Anchor 1"]');
  const a1b = await anchor1.boundingBox();
  const drop3 = await artToPage(page, 80, 190);
  await page.mouse.move(a1b!.x + a1b!.width / 2, a1b!.y + a1b!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop3.x, drop3.y, { steps: 10 });
  await page.mouse.up();

  // We now have 3 steps in past:
  // Step 3 undo (reverts anchor 1 drag)
  await undoBtn.click();
  await expect(undoBtn).toBeEnabled();
  await expect(redoBtn).toBeEnabled();

  // Step 2 undo (reverts handle 2 drag)
  await undoBtn.click();
  await expect(undoBtn).toBeEnabled();
  await expect(redoBtn).toBeEnabled();

  // Step 1 undo (reverts handle 1 drag -> back to original clean draft!)
  await undoBtn.click();
  await expect(undoBtn).toBeDisabled();
  await expect(redoBtn).toBeEnabled();
  await expect(page.getByText("unchanged", { exact: false })).toBeVisible();

  // Redo back to state after Drag 1
  await redoBtn.click();
  await expect(undoBtn).toBeEnabled();
  const pAfterRedo1 = await previewArtPoints(page);
  expect(minDistToPoint(pAfterRedo1, { x: 187.5, y: 90 })).toBeLessThan(3);

  // Regression 4: Native text input focus takes precedence over shortcut.
  const titleInput = page.locator("#studio-title");
  await titleInput.click();
  await titleInput.fill("Test Title Typing");
  // Press Control+z while inside input: Art draft is NOT undone
  await page.keyboard.press("Control+z");
  // Undo button for art draft is STILL enabled, draft preview is untouched!
  await expect(undoBtn).toBeEnabled();
  const pUntouched = await previewArtPoints(page);
  expect(minDistToPoint(pUntouched, { x: 187.5, y: 90 })).toBeLessThan(3);

  // Regression 5 & Visual Save persistence:
  // Save path now saves the current state (state after Drag 1: top curve bulge).
  state.mode = "success";
  await saveBar(page).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();

  // Published revision renders the redone geometry: underpainting pixel flips red -> blue.
  await expectPixel(page, 187, 95, isBlue, 20_000);
  const payload = state.editCalls[state.editCalls.length - 1];
  expect(payload).toMatchObject({
    base_revision: REV1,
    action: "shape",
    shape_id: SHAPE,
    region_ids: [],
    d: EDITED_D,
  });

  // Successful revision cleared draft and history controls.
  await expect(saveBar(page)).toHaveCount(0);
  await expect(undoBtn).toHaveCount(0);
  await expect(redoBtn).toHaveCount(0);

  // Reload: the redone state persists.
  await page.reload();
  await expect(page.locator(`${BOARD} path[data-region-id="r-blue"]`)).toBeAttached();
  await expectPixel(page, 187, 95, isBlue);
});

// ------------------------------------------------- add/remove nodes (Task 38)

test("Art node add/remove nodes: exact split, guarded removal, composes with undo/redo and Save", async ({ page }) => {
  test.setTimeout(150_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  // Load shape s0002 (two cubic edges, three anchors).
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap = await artToPage(page, 150, 90);
  await page.mouse.click(tap.x, tap.y);
  await expect(overlayGroup(page)).toBeVisible();

  const anchors = page.locator('circle[aria-label^="Anchor "]');
  const undoBtn = page.getByRole("button", { name: "Undo", exact: true });
  await expect(anchors).toHaveCount(3);
  const originalPreview = await previewArtPoints(page);

  // Negative guard: with only two edges, removing the interior anchor is
  // REFUSED — toast explains, geometry unchanged, no history step created.
  await page.locator('circle[aria-label="Anchor 2"]').dblclick();
  await expect(page.locator('[data-sonner-toast]', { hasText: "at least three" })).toBeVisible();
  await expect(anchors).toHaveCount(3);
  await expect(undoBtn).toBeDisabled();

  // Add a node exactly on the top curve (de Casteljau split): one new
  // anchor, the preview geometry is PRESERVED, exactly one history step.
  await page.mouse.dblclick(tap.x, tap.y);
  await expect(anchors).toHaveCount(4);
  await expect(page.getByText("modified", { exact: false })).toBeVisible();
  await expect(undoBtn).toBeEnabled();
  const splitPreview = await previewArtPoints(page);
  expect(hausdorffToPolyline(splitPreview, originalPreview)).toBeLessThan(2);
  expect(hausdorffToPolyline(originalPreview, splitPreview)).toBeLessThan(2);

  // Undo returns the untouched ring (status back to unchanged, undo
  // exhausted); redo re-adds the node.
  await page.keyboard.press("Control+z");
  await expect(anchors).toHaveCount(3);
  await expect(page.getByText("unchanged", { exact: false })).toBeVisible();
  await expect(undoBtn).toBeDisabled();
  const undonePreview = await previewArtPoints(page);
  expect(hausdorffToPolyline(undonePreview, originalPreview)).toBeLessThan(0.5);
  expect(hausdorffToPolyline(originalPreview, undonePreview)).toBeLessThan(0.5);
  await page.keyboard.press("Control+Shift+z");
  await expect(anchors).toHaveCount(4);

  // Remove an interior anchor (neighbor merge): 4 → 3 anchors and the
  // geometry genuinely changes (merge controls differ) — one more history
  // step, and undo brings the split state back.
  await page.locator('circle[aria-label="Anchor 3"]').dblclick();
  await expect(anchors).toHaveCount(3);
  const afterRemove = await previewArtPoints(page);
  expect(hausdorffToPolyline(splitPreview, afterRemove)).toBeGreaterThan(5);
  await page.keyboard.press("Control+z");
  await expect(anchors).toHaveCount(4);

  // Drag the NEW node up so the published geometry is visually distinct from
  // the original lens (the split alone is invisible by design): the probe
  // pixel at (187,95) sits below the raised top curve.
  const newNode = page.locator('circle[aria-label="Anchor 2"]');
  const nb = await newNode.boundingBox();
  const raise = await artToPage(page, 150, 40);
  await page.mouse.move(nb!.x + nb!.width / 2, nb!.y + nb!.height / 2);
  await page.mouse.down();
  await page.mouse.move(raise.x, raise.y, { steps: 8 });
  await page.mouse.up();
  await expect(anchors).toHaveCount(4);

  // Save: the payload carries THREE cubic segments (2 + 1 from the split —
  // the added node survived into the submitted path), the published
  // revision renders the raised geometry (pixel flips red → blue), the
  // draft + history clear, and the edit survives reload.
  state.mode = "success";
  await saveBar(page).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expectPixel(page, 187, 95, isBlue, 20_000);
  const payload = state.editCalls[state.editCalls.length - 1];
  expect(payload).toMatchObject({ base_revision: REV1, action: "shape", shape_id: SHAPE });
  expect((payload.d.match(/ C /g) ?? []).length).toBe(3);
  expect(payload.d).not.toBe(ORIGINAL_D);
  await expect(saveBar(page)).toHaveCount(0);
  await page.reload();
  await expectPixel(page, 187, 95, isBlue);
});

// ------------------------------------------------- open ink strokes (Task 39)

test("Art node open ink stroke: pickable, editable, undoable — open payload saved and durable", async ({ page }) => {
  test.setTimeout(150_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();

  // Tap the middle of the ink squiggle's first cubic (≈120,49) — nearest
  // path there is the STROKE (the lens fills are 30+ units away), so the
  // draft loads the open path, not the closed fills.
  const inkOverlay = page.locator(`g[aria-label="Artwork path anchors for shape ${INK}"]`);
  const tap = await artToPage(page, 120, 49);
  await page.mouse.click(tap.x, tap.y);
  await expect(inkOverlay).toBeVisible();

  const anchors = page.locator('circle[aria-label^="Anchor "]');
  await expect(anchors).toHaveCount(3);
  // The preview is genuinely OPEN: it starts at (40,40), ends at (280,40),
  // and does not close back (no Z in the source stroke).
  const pristine = await previewArtPoints(page);
  expect(Math.hypot(pristine[0].x - 40, pristine[0].y - 40)).toBeLessThan(2);
  expect(minDistToPoint(pristine, { x: 280, y: 40 })).toBeLessThan(2);
  expect(Math.hypot(pristine[0].x - pristine[pristine.length - 1].x, pristine[0].y - pristine[pristine.length - 1].y)).toBeGreaterThan(100);

  // Drag the terminal anchor down to (285,70) — one undo step.
  const last = page.locator('circle[aria-label="Anchor 3"]');
  const lb = await last.boundingBox();
  const drop = await artToPage(page, 285, 70);
  await page.mouse.move(lb!.x + lb!.width / 2, lb!.y + lb!.height / 2);
  await page.mouse.down();
  await page.mouse.move(drop.x, drop.y, { steps: 8 });
  await page.mouse.up();
  await expect(page.getByText("modified", { exact: false })).toBeVisible();

  // Add a node on the first cubic (exact split) — a second undo step.
  await page.mouse.dblclick(tap.x, tap.y);
  await expect(anchors).toHaveCount(4);

  // Undo ×2 returns the PRISTINE stroke (unchanged, stack exhausted).
  await page.keyboard.press("Control+z");
  await page.keyboard.press("Control+z");
  await expect(anchors).toHaveCount(3);
  await expect(page.getByText("unchanged", { exact: false })).toBeVisible();
  const undone = await previewArtPoints(page);
  expect(minDistToPoint(undone, { x: 280, y: 40 })).toBeLessThan(2);
  // Redo ×2 re-applies drag + split.
  await page.keyboard.press("Control+Shift+z");
  await page.keyboard.press("Control+Shift+z");
  await expect(anchors).toHaveCount(4);
  const edited = await previewArtPoints(page);
  expect(minDistToPoint(edited, { x: 285, y: 70 })).toBeLessThan(2);

  // The closed fills are untouched throughout (ink edits are paint-only —
  // the fixture echoes the backend determinism guarantee).
  await expectPixel(page, 187, 95, isRed);

  // Save: payload targets the ink shape, carries an OPEN path (no Z) with
  // THREE cubics (split included), and publishes cleanly.
  state.mode = "success";
  await saveBar(page).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Save path" }).click();
  await expect(saveBar(page)).toHaveCount(0);
  const payload = state.editCalls[state.editCalls.length - 1];
  expect(payload).toMatchObject({ base_revision: REV1, action: "shape", shape_id: INK });
  expect(payload.d.startsWith("M ")).toBe(true);
  expect(payload.d).not.toContain("Z");
  expect((payload.d.match(/ C /g) ?? []).length).toBe(3);
  // s0002's paint was NOT part of the submission.
  expect(payload.d).not.toBe(ORIGINAL_D);

  // Reload: the published ink revision serves the edited stroke — re-pick
  // and read the preview back (endpoint (285,70), 4 anchors, still open).
  await page.reload();
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap2 = await artToPage(page, 120, 49);
  await page.mouse.click(tap2.x, tap2.y);
  await expect(inkOverlay).toBeVisible();
  await expect(anchors).toHaveCount(4);
  const reloaded = await previewArtPoints(page);
  expect(minDistToPoint(reloaded, { x: 285, y: 70 })).toBeLessThan(2);
  expect(Math.hypot(reloaded[0].x - 40, reloaded[0].y - 40)).toBeLessThan(2);
});

// ------------------------------------------------- ink appearance (Task 40A)

test("Ink appearance: color/width/opacity end-to-end, topology untouched, style draft survives a failed job", async ({ page }) => {
  test.setTimeout(150_000);
  const state: FixtureState = mkState();
  await routeBackend(page, state);
  await page.goto("/");

  // Pick the ink squiggle (the Art node draft loads its open geometry).
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const inkOverlay = page.locator(`g[aria-label="Artwork path anchors for shape ${INK}"]`);
  const tap = await artToPage(page, 120, 49);
  await page.mouse.click(tap.x, tap.y);
  await expect(inkOverlay).toBeVisible();

  // The stroke's appearance is only visible through the cached underpaint
  // image (preview mode replaces live paths). Probe the pixel at the
  // stroke's center (120,49): authored = navy #1B4F8A opaque (alpha 255).
  const isNavy = (px: readonly number[] | null) =>
    !!px && Math.abs(px[0] - 27) < 30 && Math.abs(px[1] - 79) < 30 && Math.abs(px[2] - 138) < 30 && px[3] > 240;
  const isTeal = (px: readonly number[] | null) =>
    !!px && Math.abs(px[0] - 41) < 26 && Math.abs(px[1] - 56) < 26 && Math.abs(px[2] - 62) < 26;
  const inkPixelRGBA = async (x: number, y: number) =>
    page.evaluate(
      ([px, py]) =>
        new Promise<readonly (readonly number[])[] | null>((resolve) => {
          const board = document.querySelector('svg[aria-label*="interactive coloring artwork"]');
          const img = board?.querySelector('image[id$="underpaint-ink"]') as SVGImageElement | null;
          const href = img?.getAttribute("href");
          if (!href) return resolve(null);
          const image = new Image();
          image.onload = () => {
            const canvas = document.createElement("canvas");
            canvas.width = 300;
            canvas.height = 300;
            const ctx = canvas.getContext("2d")!;
            ctx.drawImage(image, 0, 0, 300, 300);
            // Grid around the aimed point: the tap can land within a few
            // units of the thin (3px) stroke, so sample a 5x5 neighborhood.
            const out: Array<readonly number[]> = [];
            for (let dx = -4; dx <= 4; dx += 2) {
              for (let dy = -4; dy <= 4; dy += 2) {
                out.push([...ctx.getImageData(px + dx, py + dy, 1, 1).data]);
              }
            }
            resolve(out);
          };
          image.onerror = () => resolve(null);
          image.src = href;
        }),
      [x, y] as const
    );
  const anyPixel = async (pred: (px: readonly number[]) => boolean) =>
    ((await inkPixelRGBA(120, 49)) ?? []).some(pred);
  await expect.poll(() => anyPixel(isNavy), { timeout: 15_000 }).toBe(true);

  // Restyle: teal, width 3, 50% opacity → Save appearance.
  await page.getByRole("button", { name: "Reset" }).locator("visible=true").first(); // noop guard
  await page.locator('input[aria-label="Stroke color"]').fill("#29383E");
  await page.locator('input[aria-label="Stroke width"]').fill("3");
  await page.locator('input[aria-label="Stroke opacity"]').fill("50");
  state.mode = "success";
  await page.getByRole("button", { name: "Save appearance" }).click();

  // Published revision re-renders the ink with the new appearance — teal,
  // with the OPACITY present as reduced alpha (was dropped end-to-end
  // before Task 40A). Topology untouched: regions stay attached.
  await expect.poll(() => anyPixel(isTeal), { timeout: 20_000 }).toBe(true);
  const tealHit = ((await inkPixelRGBA(120, 49)) ?? []).find((px) => isTeal(px));
  expect(tealHit, "50% opacity must show as reduced alpha").toBeTruthy();
  expect(tealHit![3], "50% opacity must show as reduced alpha").toBeLessThan(200);
  await expect(page.locator(`${BOARD} path[data-region-id="r-blue"]`)).toBeAttached();

  // The style draft survives as editable; geometry draft untouched
  // (shape unchanged → not dirty, no save bar for geometry).
  await expect(page.locator('input[aria-label="Stroke color"]')).toHaveValue(/^#29383e$/i);

  // Reload: the appearance persists (served from the published revision).
  await page.reload();
  await expect.poll(() => anyPixel(isTeal), { timeout: 20_000 }).toBe(true);

  // Failed style edit: the job failure keeps the local controls and their
  // values (revision-safe — nothing was published).
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap3 = await artToPage(page, 120, 49);
  await page.mouse.click(tap3.x, tap3.y);
  await expect(inkOverlay).toBeVisible();
  state.mode = "jobfail";
  await page.locator('input[aria-label="Stroke opacity"]').fill("100");
  await page.getByRole("button", { name: "Save appearance" }).click();
  await expect(page.locator("[data-sonner-toast]", { hasText: "not a simple ring" })).toBeVisible();
  await expect(page.locator('input[aria-label="Stroke opacity"]')).toHaveValue("100");
  expect(state.editCalls.filter((c) => c.action === "shape_style")).toHaveLength(2);

  // P1 — stale style base: a concurrent operation (the objects rename route)
  // publishes rev-3 while the style draft still sits on rev-2. The draft is
  // retained, the conflict is REPORTED, and saving sends the DRAFT's base
  // (rev-2, what the artist saw) — never silently rebased onto rev-3.
  state.mode = "success";
  await page.getByTitle("Blue Renamed", { exact: true }).click();
  await page.getByTitle("Rename object").click();
  await page.locator('input[value="Blue Renamed"]').fill("Blue Twice");
  await page.locator('input[value="Blue Twice"]').press("Enter");
  await expect(page.getByText(/style loaded from rev-2, project now at rev-3/)).toBeVisible({ timeout: 20_000 });
  // The save button itself states the explicit rebase target.
  await page.locator('input[aria-label="Stroke opacity"]').fill("100");
  await page.getByRole("button", { name: "Save against rev-3" }).click();
  await expect(page.locator('input[aria-label="Stroke opacity"]')).toHaveValue("100", { timeout: 20_000 });
  const stylePayload = state.editCalls.filter((c) => c.action === "shape_style")[2];
  expect(stylePayload).toMatchObject({ shape_id: INK, base_revision: REV3 });
});


// ------------------------------------------------ Task 40B: filled appearance

const isOrange = (rgb: readonly [number, number, number] | null) =>
  !!rgb && Math.abs(rgb[0] - 232) < 30 && Math.abs(rgb[1] - 118) < 30 && Math.abs(rgb[2] - 12) < 30;
/** The outline stroke color #1B4F8A on the underpaint-art raster (alpha must
 *  show real coverage — blends with the underlying fill don't count). */
const isNavyStroke = (px: readonly number[] | null) =>
  !!px && Math.abs(px[0] - 27) < 45 && Math.abs(px[1] - 79) < 45 && Math.abs(px[2] - 138) < 45 && px[3] > 200;

/** 5x5 grid probe of the underpaint ART layer (the 2px outline can sit within
 *  a couple of units of the aimed point — same trick as the ink probe). */
const artGridRGBA = (page: Page, x: number, y: number) =>
  page.evaluate(
    ([px, py]) =>
      new Promise<readonly (readonly number[])[] | null>((resolve) => {
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
          const out: Array<readonly number[]> = [];
          for (let dx = -4; dx <= 4; dx += 2) {
            for (let dy = -4; dy <= 4; dy += 2) {
              out.push([...ctx.getImageData(px + dx, py + dy, 1, 1).data]);
            }
          }
          resolve(out);
        };
        image.onerror = () => resolve(null);
        image.src = href;
      }),
    [x, y] as const
  );
const gridHas = async (page: Page, x: number, y: number, ok: (px: readonly number[]) => boolean) =>
  ((await artGridRGBA(page, x, y)) ?? []).some(ok);

test("Filled shape appearance journey: fill + outline survive reload and Build, width 0 removes the outline for good", async ({ page }) => {
  test.setTimeout(180_000);
  const state: FixtureState = mkState();
  state.mode = "success";
  await routeBackend(page, state);
  await page.goto("/");

  const region = page.locator(`${BOARD} path[data-region-id="r-blue"]`);
  await expect(region).toBeAttached();
  // The blob's interior paints blue before the edit (s0002 over the red rect).
  await expectPixel(page, 150, 150, isBlue);

  // Pick the FILLED blob outline at (150,90) — the ink squiggle sits ~35
  // units away, the blob outline dead-on.
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap = await artToPage(page, 150, 90);
  await page.mouse.click(tap.x, tap.y);
  const fillInput = page.locator('input[aria-label="Fill color"]');
  await expect(fillInput).toHaveValue(/^#3366cc$/i);
  await expect(page.locator('input[aria-label="Preserve shading"]')).toBeChecked();
  // No outline in the source: width seeds at 0 ("none").
  await expect(page.locator('input[aria-label="Outline width"]')).toHaveValue("0");

  // Act 1 — orange fill + 2px navy outline, one save.
  await fillInput.fill("#E8760C");
  await page.locator('input[aria-label="Outline color"]').fill("#1B4F8A");
  await page.locator('input[aria-label="Outline width"]').fill("2");
  await page.getByRole("button", { name: "Save appearance" }).click();
  const stylePayloads = () => state.editCalls.filter((c) => c.action === "shape_style");
  await expect.poll(() => stylePayloads().length).toBe(1);
  expect(stylePayloads()[0]).toMatchObject({
    shape_id: SHAPE,
    base_revision: REV1,
    color: "#E8760C",
    preserve_shading: true,
    stroke_color: "#1B4F8A",
    stroke_width: 2,
  });

  // Published revision: interior orange, outline navy on the underpaint-art
  // raster (the cached image is the renderer — live paths are never in the
  // DOM once the underpaint decodes, so pixels ARE the contract here).
  await expectPixel(page, 150, 150, isOrange);
  await expect.poll(() => gridHas(page, 150, 90, isNavyStroke), { timeout: 20_000 }).toBe(true);

  // Ordinary Build (recompile from master): appearance must survive. The
  // style draft SURVIVES the build publish (rebased to rev-2 by its own
  // save), so the project now sitting at rev-3 makes the draft stale —
  // the conflict chip appears WITHOUT any reload.
  await page.getByRole("button", { name: "Build vector draft" }).click();
  await expect.poll(() => state.buildCalls.length).toBe(1);
  await expectPixel(page, 150, 150, isOrange);
  await expect.poll(() => gridHas(page, 150, 90, isNavyStroke), { timeout: 20_000 }).toBe(true);
  await expect(page.getByText(/style loaded from rev-2, project now at rev-3/)).toBeVisible({ timeout: 20_000 });

  // Act 2 — width 0 REMOVES the outline. Saving the stale draft demands the
  // explicit "Save against rev-3" rebase. Fill/stroke color stay untouched →
  // the payload carries ONLY the width-0 removal.
  await page.locator('input[aria-label="Outline width"]').fill("0");
  await page.getByRole("button", { name: "Save against rev-3" }).click();
  await expect.poll(() => stylePayloads().length).toBe(2);
  expect(stylePayloads()[1]).toMatchObject({ shape_id: SHAPE, base_revision: REV3, stroke_width: 0 });
  expect(stylePayloads()[1]).not.toHaveProperty("color");
  expect(stylePayloads()[1]).not.toHaveProperty("stroke_color");
  // Outline gone from the rendered artwork: the navy stroke probes come up
  // empty while the orange fill stays (only the invisible hairline fallback
  // would render — the authored 2px stroke was deleted).
  await expect.poll(async () => !(await gridHas(page, 150, 90, isNavyStroke)), { timeout: 20_000 }).toBe(true);

  // Reload → still removed; a final Build must NOT resurrect it.
  await page.reload();
  await expect(region).toBeAttached();
  await expectPixel(page, 150, 150, isOrange);
  await expect.poll(async () => !(await gridHas(page, 150, 90, isNavyStroke)), { timeout: 20_000 }).toBe(true);
  await page.getByRole("button", { name: "Build vector draft" }).click();
  await expect.poll(() => state.buildCalls.length).toBe(2);
  await expectPixel(page, 150, 150, isOrange);
  await expect.poll(async () => !(await gridHas(page, 150, 90, isNavyStroke)), { timeout: 20_000 }).toBe(true);
  // The appearance editor seeds the removed-outline state honestly.
  await page.getByRole("button", { name: "Edit regions" }).click();
  await page.getByRole("button", { name: "Art node" }).click();
  const tap3 = await artToPage(page, 150, 90);
  await page.mouse.click(tap3.x, tap3.y);
  await expect(page.locator('input[aria-label="Outline width"]')).toHaveValue("0");
  await expect(fillInput).toHaveValue(/^#e8760c$/i);
});
