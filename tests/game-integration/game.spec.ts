/**
 * Task 36 — golden packs inside the REAL Color Duel game.
 *
 * The game's own loader (artworkRepository → vectorArtwork), renderer
 * (ColoringCanvas), interaction handlers (region path onClick), palette bar
 * and save path (progressStore) all participate. The Studio appears nowhere:
 * the packs are static files under /artworks/, byte-identical to the exports
 * (prepare_game.py only appends the game's own catalog index entries).
 *
 * Proven rows of the reviewer's matrix (this slice):
 *   - Rendering/mount: every golden pack loads via the real catalog +
 *     loadArtworkPlayData (through the Arena family/level picker where the
 *     game groups tiers); region paths render with correct counts and the
 *     detailed underpainting draws beneath them.
 *   - Hit-testing: the composition pack's known overlap probe resolves, via
 *     the browser's own top-most-region hit test at the mapped point, to the
 *     region of the VISIBLE (reordered) blob — and a REAL pointer click
 *     there fills it.
 *   - Number mode: correct palette fills a region and records progress;
 *     a WRONG number neither fills nor records (penalty indicator).
 *   - Completion: filling every region reaches the completed progress state.
 *   - Persistence: reload → the same artwork restores its filled regions.
 *   - Deployment independence: every request stays on the static origin.
 *
 * Not yet covered (documented, next slices): Free Color custom paint, Duel
 * (two players), and content-version-aware progress identity — the game's
 * progressStore keys by artworkId only; gating that requires a game-repo
 * patch.
 */

import { expect, test, type Locator, type Page } from "@playwright/test";

/** One row per golden pack. `card` is the Arena family card (the game groups
 *  artworks by id minus the -easy/-normal/-hard/-master suffix, so the three
 *  tier packs share the "QA Easy" card and are chosen in its level picker);
 *  `variant` is the difficulty label to pick when that dialog opens. */
const PACKS: Array<{ id: string; card: string; variant?: string; regionCount: number }> = [
  { id: "qa-composition", card: "QA Composition", regionCount: 3 },
  { id: "qa-easy", card: "QA Easy", variant: "Easy", regionCount: 60 },
  { id: "qa-hard", card: "QA Easy", variant: "Medium", regionCount: 300 },
  { id: "qa-master", card: "QA Easy", variant: "Hard", regionCount: 600 },
  { id: "qa-converted-balanced", card: "QA Converted Balanced", regionCount: 250 },
];

/** Composition-pack overlap probe (artwork coords): inside the reordered
 *  blob AND inside the pen rect — the game must resolve the VISIBLE owner
 *  (the blob, which was brought to front in the authoring chain). */
const OVERLAP_PROBE = { x: 180, y: 110 };

/** The composition fixture's blob fill (#77AA55 in generate_golden_packs.py)
 *  — the overlap probe must resolve to the blob's color group. */
const BLOB_HEX = "#77AA55";

type Region = { id: string; paletteId: number; label: { x: number; y: number } };
type PaletteEntry = { id: number; number: number; hex: string };

const BASE = "http://127.0.0.1:4176";

async function loadPackFiles(page: Page, id: string) {
  const [regions, palette] = await Promise.all([
    page.request.get(`${BASE}/artworks/${id}/regions.json`).then((r) => r.json()),
    page.request.get(`${BASE}/artworks/${id}/palette.json`).then((r) => r.json()),
  ]);
  return { regions: regions.regions as Region[], palette: palette as PaletteEntry[] };
}

/** Open a pack through the game's real navigation: Home → Arena → family
 *  card → (level picker for multi-tier families, incl. the one-time "How
 *  coloring works" intro on a fresh profile) → Duel → Match Briefing →
 *  the live coloring canvas. */
async function openPack(page: Page, card: string, variant?: string) {
  await page.goto("/");
  // Wait for hydration + catalog load: the cards render from the same
  // artworks state the Arena picker uses (clicking earlier hits a
  // not-yet-attached button and silently no-ops).
  await expect(page.getByText("Mosslight Cottage", { exact: false }).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Arena", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Duel Arena" })).toBeVisible({ timeout: 15_000 });
  // Click the card BUTTON (stable accessible name) — text-node clicks raced
  // the grid reflow while card thumbnails loaded and landed on neighbours.
  const cardBtn = page.getByRole("button", { name: new RegExp(`^${card}\\b`) }).first();
  await cardBtn.scrollIntoViewIfNeeded();
  await cardBtn.click();
  // Multi-tier families open the level picker; single-tier cards select
  // directly. The picker shows the onboarding intro once per profile.
  const dialog = page.getByRole("dialog");
  if (await dialog.waitFor({ state: "visible", timeout: 2_500 }).then(() => true, () => false)) {
    const intro = dialog.getByRole("button", { name: "Got it — choose a level" });
    if (await intro.isVisible()) await intro.click();
    if (variant) {
      await dialog.getByRole("button", { name: new RegExp(`^${variant}\\b`) }).click();
    }
    await dialog.getByRole("button", { name: /^Start \w+ artwork$/ }).click();
  }
  await page.locator("#arena-start-duel-btn").click();
  await page.getByRole("button", { name: "Start Match Now!" }).click();
  // The canvas is live once the region paths mount.
  await expect(page.locator("path[id^='region-']").first()).toBeVisible({ timeout: 15_000 });
}

/** Map artwork coordinates to viewport pixels. Region paths carry artwork
 *  coordinates inside the fit-transform group, and getScreenCTM() already
 *  composes EVERY ancestor transform (fit transform, zoom/pan viewBox,
 *  css scaling) — composing it with getCTM() would double-apply the
 *  viewBox and land the point off-canvas. */
async function artToScreen(page: Page, x: number, y: number): Promise<{ x: number; y: number }> {
  return page.evaluate(([ax, ay]) => {
    const path = document.querySelector("path[id^='region-']") as SVGPathElement;
    const p = new DOMPoint(ax, ay).matrixTransform(path.getScreenCTM()!);
    return { x: p.x, y: p.y };
  }, [x, y]);
}

/** Palette buttons are named "<number> <remaining>" (e.g. "2 3"); match the
 *  leading number only so remaining-count changes never break selection. */
const paletteButton = (page: Page, num: number | string) =>
  page.getByRole("button", { name: new RegExp(`^\\s*${num} \\d+$`) }).first();

/** The browser's own hit test at artwork coords: the top-most REGION (real
 *  z-order, the same geometry the player's pointer meets). The number
 *  labels sit above the paths and intercept clicks — elementsFromPoint
 *  skips pointer-events:none layers (underpainting, ink) and we take the
 *  first tappable region path in paint order, i.e. the VISIBLE owner. */
async function visibleRegionAt(page: Page, x: number, y: number): Promise<string | null> {
  const s = await artToScreen(page, x, y);
  return page.evaluate(([sx, sy]) => {
    for (const el of document.elementsFromPoint(sx, sy)) {
      if (el instanceof Element && el.id?.startsWith("region-")) return el.id.slice("region-".length);
    }
    return null;
  }, [s.x, s.y]);
}

/** Find a REAL clickable point inside the target region, starting from
 *  (x, y) in artwork coords: the region path itself must be the top-most
 *  element there — its number label (pointer-events:auto) selects the
 *  color instead of filling, so label-covered spots are nudged off. */
async function clickPointFor(
  page: Page,
  regionId: string,
  x: number,
  y: number
): Promise<{ x: number; y: number }> {
  const offsets = [
    [0, 0], [9, 0], [-9, 0], [0, 9], [0, -9],
    [16, 0], [-16, 0], [0, 16], [0, -16],
  ] as const;
  for (const [dx, dy] of offsets) {
    const s = await artToScreen(page, x + dx, y + dy);
    const top = await page.evaluate(([sx, sy]) => {
      const hit = document.elementFromPoint(sx, sy);
      return hit instanceof Element && hit.id.startsWith("region-") ? hit.id.slice("region-".length) : null;
    }, [s.x, s.y]);
    if (top === regionId) return s;
  }
  throw new Error(`no clickable point on top of region ${regionId}`);
}

function progressOf(page: Page, artworkId: string): Promise<{ completed: string[]; isComplete: boolean }> {
  return page.evaluate((id) => {
    const data = JSON.parse(localStorage.getItem("color-duel:progress:v1") ?? "{}");
    const p = data.artworks?.[id];
    return { completed: p?.completedRegionIds ?? [], isComplete: !!p?.isComplete };
  }, artworkId);
}

test.beforeEach(async ({ page }) => {
  // Deployment independence: gameplay must run purely from the game's static
  // pack assets. The gate fails on any STUDIO endpoint (backend ports or the
  // gateway API path); the game's own Google-Fonts CSS is not a Studio
  // dependency (noted as a game-side observation, not a gate).
  page.on("request", (r) => {
    expect(r.url(), `game called a Studio endpoint: ${r.url()}`).not.toMatch(
      /:8765|:8787|:81\/|XTransformPort/
    );
  });
  // Fresh profile ONCE — an addInitScript clear would run on every navigation
  // and wipe the very progress the persistence row must observe across reload.
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
});

test("all five golden packs mount in the real game with correct region counts", async ({ page }) => {
  test.setTimeout(300_000); // five full navigations incl. the 600-region pack
  for (const pack of PACKS) {
    await openPack(page, pack.card, pack.variant);
    await expect
      .poll(async () => page.locator("path[id^='region-']").count(), { timeout: 30_000 })
      .toBe(pack.regionCount);
    // detailed pack: the finished underpainting actually rendered — it is
    // drawn as paint paths BENEATH the region masks, so the svg must hold
    // more paths than the tappable regions alone.
    const total = await page.locator("svg path").count();
    expect(total, `${pack.id}: underpainting paths missing`).toBeGreaterThan(pack.regionCount);
  }
});

test("composition pack: overlap probe resolves the visible owner, fills, completes, and persists", async ({ page }) => {
  const { regions, palette } = await loadPackFiles(page, "qa-composition");
  await openPack(page, "QA Composition");
  await expect(page.locator("path[id^='region-']")).toHaveCount(3);

  // Hit-testing: the browser's top-most REGION at the overlap probe is the
  // VISIBLE owner — the blob's color group (#77AA55), because the authoring
  // chain brought the blob to front over the pen rect.
  const owner = await visibleRegionAt(page, OVERLAP_PROBE.x, OVERLAP_PROBE.y);
  expect(owner, "overlap probe hit no region").toBeTruthy();
  const ownerRegion = regions.find((r) => r.id === owner)!;
  const blobPaletteId = palette.find((e) => e.hex.toUpperCase() === BLOB_HEX)!.id;
  expect(ownerRegion.paletteId, "overlap probe resolved a region that is not the visible blob owner")
    .toBe(blobPaletteId);

  // Select the CORRECT palette number, then a REAL pointer click at the
  // probe (nudged off the number label if the label covers it).
  const ownerNumber = palette.find((e) => e.id === ownerRegion.paletteId)!.number;
  await paletteButton(page, ownerNumber).click();
  const tap = await clickPointFor(page, ownerRegion.id, OVERLAP_PROBE.x, OVERLAP_PROBE.y);
  await page.mouse.click(tap.x, tap.y);
  await expect
    .poll(async () => (await progressOf(page, "qa-composition")).completed)
    .toContain(ownerRegion.id);

  // Wrong-answer row: a REAL click with the WRONG number neither fills the
  // region nor records progress — the game flashes its penalty instead.
  const victim = regions.find((r) => r.id !== ownerRegion.id)!;
  const wrongNumber = palette.find((e) => e.id !== victim.paletteId)!.number;
  await paletteButton(page, wrongNumber).click();
  const wrongTap = await clickPointFor(page, victim.id, victim.label.x, victim.label.y);
  await page.mouse.click(wrongTap.x, wrongTap.y);
  await expect(page.getByText("-5 Wrong Number!")).toBeVisible({ timeout: 5_000 });
  expect((await progressOf(page, "qa-composition")).completed).not.toContain(victim.id);

  // Complete every remaining region with its correct palette number.
  for (const r of regions) {
    if ((await progressOf(page, "qa-composition")).completed.includes(r.id)) continue;
    const num = palette.find((e) => e.id === r.paletteId)!.number;
    await paletteButton(page, num).click();
    const t = await clickPointFor(page, r.id, r.label.x, r.label.y);
    await page.mouse.click(t.x, t.y);
    await expect
      .poll(async () => (await progressOf(page, "qa-composition")).completed)
      .toContain(r.id);
  }
  await expect
    .poll(async () => (await progressOf(page, "qa-composition")).isComplete)
    .toBe(true);

  // Persistence: reload → same artwork → the filled regions are restored.
  await page.reload();
  await openPack(page, "QA Composition");
  await expect(page.locator("path[id^='region-']")).toHaveCount(3);
  const after = await progressOf(page, "qa-composition");
  expect(after.completed).toHaveLength(3);
  expect(after.isComplete).toBe(true);
});
