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
 *   - Free Color: the studio mode accepts a custom HEX outside the palette,
 *     paints the region with it, and records the progress.
 *   - Progress identity: records carry the pack's contentVersion; the same
 *     version restores (Profile "In Progress"), a re-shipped version never
 *     silently reuses the old completion, and the raw record survives.
 *   - Duel: the rival's fills stay OUT of the player's progress record while
 *     both play the identical content.
 *   - Deployment independence: every request stays on the static origin.
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
  // The Arena TAB is static chrome; the arena card grid renders only once
  // the catalog has hydrated — so navigate first and wait for the TARGET
  // CARD there. (A Home text anchor cannot work across environments: the
  // featured rail shows a local subset in a dev sandbox and only the QA
  // packs in a fresh CI clone.)
  await page.getByRole("button", { name: "Arena", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Duel Arena" })).toBeVisible({ timeout: 15_000 });
  // Click the card BUTTON (stable accessible name) — text-node clicks raced
  // the grid reflow while card thumbnails loaded and landed on neighbours.
  const cardBtn = page.getByRole("button", { name: new RegExp(`^${card}\\b`) }).first();
  await expect(cardBtn).toBeVisible({ timeout: 30_000 });
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

/** The RAW stored record including its contentVersion and free-color paints —
 *  the identity datum the version-awareness rows read directly (the game's
 *  own reads filter by it, so a mismatched record is invisible in the UI by
 *  design). */
function rawProgressOf(
  page: Page,
  artworkId: string
): Promise<
  { contentVersion?: string; completed: string[]; customRegionColors?: Record<string, string> } | undefined
> {
  return page.evaluate((id) => {
    const data = JSON.parse(localStorage.getItem("color-duel:progress:v1") ?? "{}");
    const p = data.artworks?.[id];
    return p
      ? { contentVersion: p.contentVersion, completed: p.completedRegionIds, customRegionColors: p.customRegionColors }
      : undefined;
  }, artworkId);
}

/** Set the palette's custom paint color. `<input type=color>` has no typable
 *  field, so the value goes through the native setter + a bubbling input
 *  event — the game's real React onChange → onSelectCustomColor runs. */
async function setCustomPaint(page: Page, hex: string) {
  await page.evaluate((value) => {
    const input = document.querySelector('input[aria-label="Custom paint color"]') as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, hex);
}

/** The Profile tab's "In Progress" shelf — the user-visible identity read:
 *  an artwork appears here only when a progress record matches its OWN
 *  content version (progressMapFor). */
const inProgressShelf = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: "In Progress" }) });

/** Enter studio mode through the real UI: the Studio card opens an artwork
 *  only once the catalog has hydrated (featured[0] is undefined before
 *  that), so the click retries until the briefing actually appears. The
 *  briefing STAYS OPEN (it names the artwork — rows may read it); start the
 *  match explicitly with startStudioMatch. */
async function openStudioBriefing(page: Page) {
  await expect(async () => {
    await page.locator("#home-studio-card").click();
    await expect(page.getByText("Studio Relax")).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 45_000 });
}

async function startStudioMatch(page: Page) {
  await page.locator("#pre-match-start-btn").click();
  await expect(page.locator("path[id^='region-']").first()).toBeVisible({ timeout: 15_000 });
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

  // VISUAL persistence (not just storage): the reopened canvas hydrates from
  // the record at mount — every completed region's opaque white mask
  // (fill="#FFFFFF" while unfilled) is GONE, replaced by fill="transparent"
  // so the finished underpainting shows through, exactly as the player left
  // it. If the canvas only pretended to resume, all three would still mask.
  for (const r of regions) {
    await expect
      .poll(async () =>
        page.locator(`path[id='region-${r.id}']`).evaluate((el) => el.getAttribute("fill"))
      )
      .toBe("transparent");
  }
});

// ------------------------------------------------------- Free Color (studio)

test("studio free color: custom HEX outside the palette paints, records, and restores", async ({ page }) => {
  test.setTimeout(180_000);
  // Studio mode is entered from Home's Studio card: with a fresh profile it
  // opens the FIRST catalog family's representative — which is
  // environment-dependent (the full local artwork set in a dev sandbox, the
  // staged QA packs in a fresh CI clone). The briefing NAMES the artwork it
  // is about to open; the catalog maps that title to the id and everything
  // below reads the SERVED pack — nothing is assumed about which pack runs.
  await page.goto("/");
  const catalog = await page.request.get(`${BASE}/artworks/catalog.json`).then((r) => r.json());
  await openStudioBriefing(page);
  const title = (await page.getByRole("heading", { level: 4 }).first().textContent()) ?? "";
  const artworkId = catalog.artworks.find((e: { title?: string }) => e.title === title)?.id as string;
  expect(artworkId, `briefing title "${title}" must map to a catalog entry`).toBeTruthy();

  const { regions, palette } = await loadPackFiles(page, artworkId);
  const manifest = await page.request
    .get(`${BASE}/artworks/${artworkId}/artwork.json`)
    .then((r) => r.json());
  expect(manifest.version, "served pack must declare a content version").toBeTruthy();

  await startStudioMatch(page);
  // A custom HEX the palette does NOT ship.
  const CUSTOM_HEX = "#B4D4AA";
  expect(palette.map((p) => p.hex.toUpperCase())).not.toContain(CUSTOM_HEX);
  await setCustomPaint(page, CUSTOM_HEX);

  // Studio free color: ANY unfilled region accepts the active paint — the
  // number-match rejection does not apply (no palette number needed). One
  // real pointer click fills it.
  const target = regions[0];
  // The PRISTINE fill (unfilled mask — white under the underpainting model,
  // paper tone in standard vector mode) is captured, not assumed: the
  // version-mismatch act below asserts the region returns to exactly it.
  const pristineFill = await page
    .locator(`path[id='region-${target.id}']`)
    .evaluate((el) => el.getAttribute("fill"));
  const tap = await clickPointFor(page, target.id, target.label.x, target.label.y);
  await page.mouse.click(tap.x, tap.y);

  // The region actually renders the custom paint…
  await expect
    .poll(async () =>
      page
        .locator(`path[id='region-${target.id}']`)
        .evaluate((el) => el.getAttribute("fill")?.toUpperCase())
    )
    .toBe(CUSTOM_HEX);
  // …and the completion is recorded under the pack's content version.
  await expect.poll(async () => (await progressOf(page, artworkId)).completed).toContain(target.id);
  const raw = await rawProgressOf(page, artworkId);
  expect(raw?.contentVersion).toBe(manifest.version);
  expect(raw?.customRegionColors?.[target.id]?.toUpperCase()).toBe(CUSTOM_HEX);

  // Restore, VISUALLY (the reviewer's bar: not localStorage reads). Same
  // content version → reopening the artwork in studio mode brings the
  // canvas back with the region STILL painted in the artist's custom HEX —
  // hydrated from the record at mount, not repainted by this act.
  await page.reload();
  // After hydration the hero CTA flips from "Start coloring" to
  // "Resume <title>" — the flip itself is the hydration signal and the
  // Home-level identity read (the real progressMapFor).
  await expect(page.locator("#home-hero-solo-btn")).toContainText(`Resume ${title}`, { timeout: 30_000 });
  await openStudioBriefing(page);
  await startStudioMatch(page);
  await expect
    .poll(async () =>
      page
        .locator(`path[id='region-${target.id}']`)
        .evaluate((el) => el.getAttribute("fill")?.toUpperCase())
    )
    .toBe(CUSTOM_HEX);

  // Version mismatch: the SAME artwork re-ships as a new content version
  // (the route bumps the manifest — the staged content change IS the thing
  // under test). The custom paint and the completion must NOT reappear: the
  // region renders its pristine unfilled mask again.
  await page.route(`**/artworks/${artworkId}/artwork.json`, async (route) => {
    const bumped = (await route.fetch().then((r) => r.json())) as { version: string };
    bumped.version = "9.9.9";
    await route.fulfill({ json: bumped });
  });
  await page.reload();
  await openStudioBriefing(page);
  await startStudioMatch(page);
  // The contract is the NEGATION: the artist's custom paint must not come
  // back. The engine may render the unfilled region as its pristine mask OR
  // as a target hint (the selected color still has unfilled regions) — both
  // are honest "not restored" states, so assert against the paint itself.
  await expect
    .poll(async () =>
      page
        .locator(`path[id='region-${target.id}']`)
        .evaluate((el) => el.getAttribute("fill")?.toUpperCase())
    )
    .not.toBe(CUSTOM_HEX);
  // The shipped-version record survives in storage, identity-filtered —
  // filtered, not destroyed: the paints stay with it.
  const stale = await rawProgressOf(page, artworkId);
  expect(stale?.contentVersion).toBe(manifest.version);
  expect(stale?.customRegionColors?.[target.id]?.toUpperCase()).toBe(CUSTOM_HEX);
});

// ------------------------------------------- version-aware progress identity

test("progress identity: same version restores, a re-shipped version never reuses old completions", async ({ page }) => {
  test.setTimeout(180_000);
  const { regions, palette } = await loadPackFiles(page, "qa-composition");
  // The shipped version is read, never assumed: the row's contract is
  // "same version restores / different version does not", whatever it is.
  const shippedVersion = await page.request
    .get(`${BASE}/artworks/qa-composition/artwork.json`)
    .then((r) => r.json())
    .then((m) => m.version as string);
  expect(shippedVersion).toBeTruthy();

  // Act 1 — play ONE region at the shipped content version.
  await openPack(page, "QA Composition");
  await expect(page.locator("path[id^='region-']")).toHaveCount(3);
  const first = regions[0];
  const num = palette.find((e) => e.id === first.paletteId)!.number;
  await paletteButton(page, num).click();
  const tap = await clickPointFor(page, first.id, first.label.x, first.label.y);
  await page.mouse.click(tap.x, tap.y);
  await expect.poll(async () => (await rawProgressOf(page, "qa-composition"))?.completed).toContain(first.id);
  expect((await rawProgressOf(page, "qa-composition"))?.contentVersion).toBe(shippedVersion);

  // Same version → Profile's "In Progress" shelf lists the artwork (the
  // game's real identity-checked read).
  const gotoProfile = async () => {
    // A running match covers the tab bar — leave it first if present.
    const exit = page.locator('button[title="Exit match"]');
    if (await exit.isVisible()) await exit.click();
    await page.getByRole("button", { name: "Profile", exact: false }).first().click();
    await expect(page.getByRole("heading", { name: "In Progress" })).toBeVisible({ timeout: 10_000 });
  };
  await gotoProfile();
  await expect(inProgressShelf(page).getByText("QA Composition", { exact: false })).toBeVisible();

  // Act 2 — the artwork RE-SHIPS as a new content version (same id). The
  // route serves the bumped manifest: this staged content change IS the
  // thing under test (the row is about identity, not pack bytes).
  await page.route("**/artworks/qa-composition/artwork.json", async (route) => {
    const manifest = (await route.fetch().then((r) => r.json())) as { version: string };
    manifest.version = "9.9.9";
    await route.fulfill({ json: manifest });
  });

  await page.reload();
  await gotoProfile();
  // The old completion must NOT silently reuse onto the new version: the
  // shelf is empty once the catalog hydrates (Profile chrome is static; the
  // shelf content is not), and the raw shipped-version record is still
  // intact in storage — identity-filtered, not destroyed.
  await expect(inProgressShelf(page).getByText("Nothing on the easel")).toBeVisible({ timeout: 30_000 });
  const stale = await rawProgressOf(page, "qa-composition");
  expect(stale?.contentVersion).toBe(shippedVersion);
  expect(stale?.completed).toContain(first.id);
});

// ----------------------------------------------------- duel (identical content)

test("duel: rival fills stay out of the player's progress record on identical content", async ({ page }) => {
  test.setTimeout(180_000);
  const { regions, palette } = await loadPackFiles(page, "qa-composition");
  const shippedVersion = await page.request
    .get(`${BASE}/artworks/qa-composition/artwork.json`)
    .then((r) => r.json())
    .then((m) => m.version as string);
  await openPack(page, "QA Composition");
  await expect(page.locator("path[id^='region-']")).toHaveCount(3);

  // The player answers ONE region correctly, immediately.
  const mine = regions[0];
  const num = palette.find((e) => e.id === mine.paletteId)!.number;
  await paletteButton(page, num).click();
  const tap = await clickPointFor(page, mine.id, mine.label.x, mine.label.y);
  await page.mouse.click(tap.x, tap.y);
  await expect.poll(async () => (await progressOf(page, "qa-composition")).completed).toContain(mine.id);

  // The rival AI ticks from the same pack (identical content identity): its
  // 50% toast (floor(3/2) = 1 fill) proves it actually painted.
  await expect(page.getByText("Rival filled 50%", { exact: false })).toBeVisible({ timeout: 20_000 });

  // The boundary under test: the player's record holds ONLY the player's own
  // fill — the rival's concurrent fills on the SAME artwork never leak in.
  const player = await progressOf(page, "qa-composition");
  expect(player.completed).toEqual([mine.id]);
  const raw = await rawProgressOf(page, "qa-composition");
  expect(raw?.contentVersion).toBe(shippedVersion);
});
