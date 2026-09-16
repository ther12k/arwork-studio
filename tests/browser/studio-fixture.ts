/**
 * Fixture backend for the Task 33 journey gate (tests/browser/studio.spec.ts).
 *
 * A "controlled transport": every studio-api endpoint the real app calls is
 * answered from these tables via Playwright route interception, so the REAL
 * hook/page logic (runEdit guard, saveArtPath settle-watch, job polling,
 * VectorBoard mounting) decides every outcome — only the HTTP boundary is
 * staged. The staged backend models the bits the journey depends on: async
 * jobs (running → done), semantic job failure with the revision untouched, a
 * 409 stale-base rejection, BASE-REVISION verification (a payload whose
 * base_revision is not the current revision is rejected, like the real
 * service), an UNRELATED operation route (/objects rename) that advances the
 * revision WITHOUT touching the artwork (drives the draft-conflict row), and
 * revision-addressed bundle files (each revision serves its own paint).
 */

import type { Page } from "@playwright/test";

export const PID = "proj-journey";
export const PID_B = "proj-b";
export const REV_B1 = "b-rev-1";
export const REV1 = "rev-1";
export const REV2 = "rev-2";
export const REV3 = "rev-3";
export const SHAPE = "s0002";
/** An OPEN ink stroke (filled:false, above the fills) — the Task 39
 *  open-path editing target. Midpoint of its first cubic ≈ (120,49). */
export const INK = "s0003";
export const INK_D = "M 40,40 C 90,10 150,90 200,50 C 230,30 260,60 280,40";
export const INK_EDITED_D = "M 40,40 C 90,10 150,90 200,50 C 230,30 260,60 285,70";
/** The squiggle's authored appearance (rev-1 baseline). */
export const INK_STYLE0 = { color: "#1B4F8A", width: 3 };
/** Closed two-cubic blob; handle 1 = c1 (70,60) of the first cubic. */
export const ORIGINAL_D = "M 70,180 C 70,60 230,60 230,180 C 230,300 70,300 70,180 Z";
/** Serialized result of dragging handle 1 to art (170,60). */
export const EDITED_D = "M 70,180 C 170,60 230,60 230,180 C 230,300 70,300 70,180 Z";

export type EditMode = "reject409" | "jobfail" | "topologyfail" | "success";

/** Task 40B: per-revision appearance of the FILLED blob (fill + outline).
 *  No stroke ⇒ no outline (the backend removes both keys on width 0). */
export type ShapeStyle = { fill: string; stroke?: string; strokeWidth?: number };

export type FixtureState = {
  mode: EditMode;
  revision: string;
  /** Paint `d` served per revision (revisions carry their own artwork). */
  revPaint: Record<string, string>;
  revInk: Record<string, string>;
  /** Task 40A: per-revision ink stroke style (color/width/opacity). */
  revInkStyle: Record<string, { color: string; width: number; opacity?: number }>;
  /** Task 40B: per-revision filled-shape appearance. */
  revShape: Record<string, ShapeStyle>;
  /** Task 40C: per-revision paint order — true = the blob sits BEHIND the
   *  red base rect (z swapped). */
  revBlobBehind: Record<string, boolean>;
  /** Which shape the accepted edit job targeted (paint vs ink routing). */
  pendingShape: string;
  /** Accepted shape_style job's next appearance. */
  pendingStyle: { color: string; width: number; opacity?: number } | null;
  pendingShapeStyle: ShapeStyle | null;
  /** Accepted shape_order job's next paint order. */
  pendingOrder: boolean | null;
  job: Record<string, unknown>;
  editCalls: Array<Record<string, unknown>>;
  objectCalls: Array<Record<string, unknown>>;
  buildCalls: Array<Record<string, unknown>>;
  runningGets: number;
  pendingD: string;
  pendingKind: "edit" | "objects" | "style" | "build" | "order" | null;
  /** jobId returned by the POST — the settled project.job must keep THIS id
   *  (the app's settle-watch attributes outcomes by job identity). */
  activeJobId: string;
};

const SHAPE0: ShapeStyle = { fill: "#3366CC" };

export const mkState = (): FixtureState => ({
  mode: "reject409",
  revision: REV1,
  revPaint: { [REV1]: ORIGINAL_D },
  revInk: { [REV1]: INK_D },
  revInkStyle: {},
  revShape: { [REV1]: SHAPE0 },
  revBlobBehind: { [REV1]: false },
  pendingShape: SHAPE,
  pendingStyle: null,
  pendingShapeStyle: null,
  pendingOrder: null,
  job: { status: "idle" },
  editCalls: [],
  objectCalls: [],
  buildCalls: [],
  runningGets: 0,
  pendingD: "",
  pendingKind: null,
  activeJobId: "",
});

const ART = "journey-artwork";
/** Immutable revision chain: each operation publishes the next one. The tail
 *  beyond REV3 serves the fallback (unchanged) artwork — the 40B journeys
 *  Build twice on top of the style saves. */
const CHAIN = [REV1, REV2, REV3, "rev-4", "rev-5"];
const NEXT_REV: Record<string, string> = Object.fromEntries(
  CHAIN.slice(0, -1).map((r, i) => [r, CHAIN[i + 1]])
);

const revision = (id: string) => ({
  id,
  version: "0.3.1",
  createdAt: "2026-01-01T00:00:00Z",
  kind: "build",
  sourceHash: `sha-${id}`,
  regionCount: 2,
  qa: {
    passed: true,
    errors: [],
    warnings: [],
    playableRegions: 2,
    fixedRegions: 0,
    paletteGroups: 2,
    paintPaths: 2,
    humanReviewed: false,
  },
  manifestUrl: `/api/projects/${PID}/revisions/${id}/files/artwork.json`,
});

/** Per-revision manifest — artworkVersion advances with the revision (the
 *  real compiler bumps it; the node-session key in the app depends on it). */
const manifestFor = (rev: string) => ({
  schemaVersion: 1,
  format: "color-duel-detailed-vector-1",
  id: ART,
  version: { [REV1]: "0.1.0", [REV2]: "0.1.1", [REV3]: "0.1.2" }[rev] ?? "0.1.0",
  title: "Journey fixture",
  regionCount: 2,
  paletteCount: 2,
  objectGroups: [],
  assets: { regions: "regions.json", palette: "palette.json", paint: "paint.json" },
  contentHash: "journey-fixture",
  difficulty: { rating: "easy", score: 10, metrics: {} },
});

/** Per-revision geometry — artworkVersion advances WITH the manifest version
 *  (validateBundle asserts they match, exactly like the real compiler). */
const geometryFor = (rev: string) => ({
  schemaVersion: 1,
  artworkId: ART,
  artworkVersion: { [REV1]: "0.1.0", [REV2]: "0.1.1", [REV3]: "0.1.2" }[rev] ?? "0.1.0",
  viewBox: [0, 0, 300, 300],
  fillRule: "evenodd",
  stroke: "#22333B",
  strokeWidth: 0.8,
  edges: [],
  regions: [
    {
      id: "r-red",
      paletteId: 1,
      objectId: "obj-red",
      d: "M 20,20 L 180,20 L 180,100 L 100,100 L 100,260 L 20,260 Z",
      fillRule: "evenodd",
      masterShapeId: "s0001",
      rings: [[[20, 20], [180, 20], [180, 100], [100, 100], [100, 260], [20, 260]]],
      bbox: [20, 20, 180, 260],
      area: 19200,
      label: { x: 60, y: 60, fontSize: 14, minScreenPx: 9, clearance: 20 },
    },
    {
      id: "r-blue",
      paletteId: 2,
      objectId: "obj-blue",
      d: "M 100,100 L 260,100 L 260,260 L 100,260 Z",
      fillRule: "evenodd",
      masterShapeId: SHAPE,
      rings: [[[100, 100], [260, 100], [260, 260], [100, 260]]],
      bbox: [100, 100, 260, 260],
      area: 25600,
      label: { x: 180, y: 180, fontSize: 14, minScreenPx: 9, clearance: 20 },
    },
  ],
  decorations: [],
  detailPaths: [],
});

const palette = [
  { id: 1, number: 1, name: "Red", hex: "#CC3333", paint: { type: "solid", stops: [] } },
  { id: 2, number: 2, name: "Blue", hex: "#3366CC", paint: { type: "solid", stops: [] } },
];

const paintFor = (
  d: string,
  inkD: string,
  style: { color: string; width: number; opacity?: number },
  shape: ShapeStyle,
  blobBehind: boolean
) => ({
  schemaVersion: 2,
  artworkId: ART,
  viewBox: [0, 0, 300, 300],
  paths: [
    // Task 40C: the order flip swaps the z of the base rect and the blob.
    { z: blobBehind ? 1 : 0, shapeId: "s0001", d: "M 20,20 L 280,20 L 280,280 L 20,280 Z", fill: "#CC3333", fillRule: "evenodd" },
    {
      z: blobBehind ? 0 : 1, shapeId: SHAPE, d, fill: shape.fill, fillRule: "evenodd",
      ...(shape.stroke && shape.strokeWidth
        ? { stroke: shape.stroke, strokeWidth: shape.strokeWidth }
        : {}),
    },
  ],
  inkPaths: [{
    z: 2, shapeId: INK, d: inkD, fill: style.color, strokeWidth: style.width, filled: false,
    ...(style.opacity != null && style.opacity < 0.999 ? { opacity: style.opacity } : {}),
  }],
  gradients: [],
  sourceColorShapeCount: 2,
});

const objectsFor = (rev: string) => ({
  schemaVersion: 1,
  objects: [
    { id: "obj-red", name: "Red", shapeIds: ["s0001"] },
    {
      id: "obj-blue",
      name: rev === REV1 ? "Blue" : rev === REV2 ? "Blue Renamed" : "Blue Twice",
      shapeIds: [SHAPE],
    },
  ],
});

// Project B — a SECOND project whose artwork also has a shape "s0002"
// (different geometry/fill). The cross-project draft guard depends on this
// collision being possible: A's draft must never be submittable against B's
// s0002.
const ART_B = "journey-artwork-b";
const B_D = "M 60,60 C 60,20 180,20 180,60 C 180,100 60,100 60,60 Z";
const manifestB = {
  schemaVersion: 1,
  format: "color-duel-detailed-vector-1",
  id: ART_B,
  version: "0.2.0",
  title: "Journey fixture B",
  regionCount: 1,
  paletteCount: 2,
  objectGroups: [],
  assets: { regions: "regions.json", palette: "palette.json", paint: "paint.json" },
  contentHash: "journey-fixture-b",
  difficulty: { rating: "easy", score: 10, metrics: {} },
};
const geometryB = {
  schemaVersion: 1,
  artworkId: ART_B,
  artworkVersion: "0.2.0",
  viewBox: [0, 0, 300, 300],
  fillRule: "evenodd",
  stroke: "#22333B",
  strokeWidth: 0.8,
  edges: [],
  regions: [
    {
      id: "r-b1", paletteId: 1, objectId: "obj-b1",
      d: B_D, fillRule: "evenodd", masterShapeId: SHAPE,
      rings: [[[60, 60], [180, 60], [180, 100], [60, 100]]],
      bbox: [60, 20, 180, 100], area: 4800,
      label: { x: 120, y: 60, fontSize: 14, minScreenPx: 9, clearance: 20 },
    },
  ],
  decorations: [],
  detailPaths: [],
};
const paintB = {
  schemaVersion: 2,
  artworkId: ART_B,
  viewBox: [0, 0, 300, 300],
  paths: [{ z: 0, shapeId: SHAPE, d: B_D, fill: "#CC33AA", fillRule: "evenodd" }],
  inkPaths: [],
  gradients: [],
  sourceColorShapeCount: 1,
};
const projectB = () => ({
  id: PID_B,
  title: "Project B",
  brief: "fixture brief b",
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  messages: [],
  aiUsage: [],
  master: null,
  reference: null,
  revisions: [
    {
      id: REV_B1, version: "0.3.1", createdAt: "2026-01-01T00:00:00Z", kind: "build",
      sourceHash: "sha-b1", regionCount: 1,
      qa: { passed: true, errors: [], warnings: [], playableRegions: 1, fixedRegions: 0,
            paletteGroups: 2, paintPaths: 1, humanReviewed: false },
      manifestUrl: `/api/projects/${PID_B}/revisions/${REV_B1}/files/artwork.json`,
    },
  ],
  currentRevision: REV_B1,
  job: { status: "idle" },
});

const project = (state: FixtureState) => ({
  id: PID,
  title: "Journey fixture",
  brief: "fixture brief",
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  messages: [],
  aiUsage: [],
  // A vector master so the right panel's "Build vector draft" is enabled —
  // the 40B journeys Build after style saves (recompile ⇒ artwork unchanged).
  master: {
    file: "master.svg", width: 300, height: 300, sha256: "sha-master",
    source: "upload", rightsConfirmed: true, createdAt: "2026-01-01T00:00:00Z", kind: "svg",
  },
  reference: null,
  revisions: CHAIN.slice(0, CHAIN.indexOf(state.revision) + 1).map(revision),
  currentRevision: state.revision,
  job: state.job,
});

/** Publish the next revision. An edit job carries its submitted paint; an
 *  objects job carries the previous artwork unchanged; a BUILD job is the
 *  recompile-from-master equivalent: the artwork carries over unchanged; an
 *  ORDER job swaps the base rect and the blob in paint order. */
const publish = (state: FixtureState) => {
  const next = NEXT_REV[state.revision];
  if (!next) throw new Error("fixture revision chain exhausted");
  const carriesEdit = state.pendingKind === "edit" && !!state.pendingD;
  state.revPaint[next] = carriesEdit && state.pendingShape === SHAPE
    ? state.pendingD!
    : state.revPaint[state.revision];
  state.revInk[next] = carriesEdit && state.pendingShape === INK
    ? state.pendingD!
    : state.revInk[state.revision];
  state.revInkStyle[next] = state.pendingKind === "style" && state.pendingStyle
    ? state.pendingStyle
    : state.revInkStyle[state.revision] ?? INK_STYLE0;
  state.revShape[next] = state.pendingKind === "style" && state.pendingShapeStyle
    ? state.pendingShapeStyle
    : state.revShape[state.revision] ?? SHAPE0;
  state.revBlobBehind[next] = state.pendingKind === "order" && state.pendingOrder != null
    ? state.pendingOrder
    : state.revBlobBehind[state.revision] ?? false;
  state.revision = next;
  state.job = {
    id: state.activeJobId,
    kind: state.pendingKind === "edit" ? "shape edit"
      : state.pendingKind === "build" ? "vector compilation"
      : state.pendingKind === "order" ? "shape reorder"
      : state.pendingKind === "style" ? "ink style" : "object update",
    status: "done",
    progress: 100,
  };
};

/** Running jobs flip to done + publish on the SECOND project GET (the first
 *  is the POST handler's own refresh, the second the poll loop). */
const advanceJob = (state: FixtureState) => {
  if (state.job.status !== "running") return;
  state.runningGets += 1;
  if (state.runningGets >= 2) publish(state);
};

const fileFor = (state: FixtureState, rev: string, name: string): unknown => {
  if (rev === REV_B1) {
    switch (name) {
      case "artwork.json":
        return manifestB;
      case "regions.json":
        return geometryB;
      case "palette.json":
        return palette;
      case "paint.json":
        return paintB;
      case "objects.json":
        return { schemaVersion: 1, objects: [{ id: "obj-b1", name: "B1", shapeIds: [SHAPE] }] };
      default:
        return { detail: "no such file" };
    }
  }
  const d = state.revPaint[rev] ?? ORIGINAL_D;
  const inkD = state.revInk[rev] ?? INK_D;
  const inkStyle = state.revInkStyle[rev] ?? INK_STYLE0;
  switch (name) {
    case "artwork.json":
      return manifestFor(rev);
    case "regions.json":
      return geometryFor(rev);
    case "palette.json":
      return palette;
    case "paint.json":
      return paintFor(d, inkD, inkStyle, state.revShape[rev] ?? SHAPE0, state.revBlobBehind[rev] ?? false);
    case "objects.json":
      return objectsFor(rev);
    default:
      return { detail: "no such file" };
  }
};

export async function routeBackend(page: Page, state: FixtureState) {
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/config") && req.method() === "GET") {
      return json({
        ai: { configured: false, chatModel: "-", imageModel: "-" },
        localOnly: true,
        version: "0.0.0",
        supportedFormat: "color-duel-detailed-vector-1",
        maxUploadMB: 10,
      });
    }
    if (path === "/api/projects" && req.method() === "GET") return json([project(state), projectB()]);
    if (path === `/api/projects/${PID_B}` && req.method() === "GET") return json(projectB());
    if (path === `/api/projects/${PID_B}` && req.method() === "PATCH") return json(projectB());
    if (path === `/api/projects/${PID_B}/generation/sessions`) return json({ sessions: [] });
    if (path === `/api/projects/${PID}` && req.method() === "GET") {
      advanceJob(state);
      return json(project(state));
    }
    if (path === `/api/projects/${PID}` && req.method() === "PATCH") return json(project(state));
    if (path === `/api/projects/${PID}/generation/sessions`) return json({ sessions: [] });
    const file = path.match(
      new RegExp(`^/api/projects/(?:${PID}|${PID_B})/revisions/([^/]+)/files/(.+)$`)
    );
    if (file && req.method() === "GET") return json(fileFor(state, file[1], file[2]));
    if (path === `/api/projects/${PID}/objects` && req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, unknown>;
      if (body.base_revision !== state.revision) {
        return json({ detail: "Stale base revision: the project has moved on." }, 409);
      }
      state.objectCalls.push(body);
      state.activeJobId = "job-objects";
      state.job = { id: "job-objects", kind: "object update", status: "running", progress: 10 };
      state.runningGets = 0;
      state.pendingKind = "objects";
      return json({ jobId: "job-objects" });
    }
    if (path === `/api/projects/${PID}/build` && req.method() === "POST") {
      // Recompile from the master: the artwork carries over UNCHANGED (the
      // fixture's master always represents the latest published appearance —
      // the real backend guarantees that via _sync_source_master).
      state.buildCalls.push(req.postDataJSON() as Record<string, unknown>);
      state.activeJobId = "job-build";
      state.runningGets = 0;
      state.pendingKind = "build";
      state.pendingD = null;
      state.job = { id: "job-build", kind: "vector compilation", status: "running", progress: 10 };
      return json({ jobId: "job-build", projectId: PID });
    }
    if (path === `/api/projects/${PID}/edit` && req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, unknown>;
      if (state.mode !== "reject409" && body.base_revision !== state.revision) {
        return json({ detail: "Stale base revision: the project has moved on." }, 409);
      }
      state.editCalls.push(body);
      if (state.mode === "reject409") {
        return json({ detail: "Stale base revision: the project has moved on." }, 409);
      }
      if (state.mode === "jobfail") {
        state.activeJobId = "job-fail";
        state.job = {
          id: "job-fail",
          kind: body.action === "shape_style" ? "ink style" : "shape edit",
          status: "failed",
          progress: 100,
          message: "Edited path is not a simple ring.",
        };
        return json({ jobId: "job-fail", projectId: PID });
      }
      if (state.mode === "topologyfail" && body.action === "shape_order") {
        // Task 40C review: the manual-topology gate — the move is refused
        // until confirm_topology_rebuild is sent.
        state.activeJobId = "job-gate";
        state.job = {
          id: "job-gate",
          kind: "shape reorder",
          status: "failed",
          progress: 0.2,
          message:
            "This move rebuilds the gameplay surfaces; manual cuts, boundaries and label positions may be reset. Confirm the topology rebuild to continue.",
        };
        return json({ jobId: "job-gate", projectId: PID });
      }
      state.activeJobId = "job-ok";
      state.runningGets = 0;
      if (body.action === "shape_order") {
        // Task 40C: one-layer paint-order move — swap the blob and the base
        // rect in the published paint.
        state.pendingOrder = body.order === "backward";
        state.pendingKind = "order";
        state.pendingD = null;
        state.pendingShapeStyle = null;
        state.pendingStyle = null;
        state.job = { id: "job-ok", kind: "shape reorder", status: "running", progress: 10 };
        return json({ jobId: "job-ok", projectId: PID });
      }
      if (body.action === "shape_style" && String(body.shape_id) === SHAPE) {
        // Task 40B: filled target — fill + outline with the backend's
        // deletion semantics (width 0 removes the outline entirely).
        const cur = state.revShape[state.revision] ?? SHAPE0;
        const nextShape: ShapeStyle = { ...cur };
        if (typeof body.color === "string") nextShape.fill = body.color;
        if (body.stroke_width === 0) {
          delete nextShape.stroke;
          delete nextShape.strokeWidth;
        } else {
          if (typeof body.stroke_color === "string") nextShape.stroke = body.stroke_color;
          if (typeof body.stroke_width === "number") nextShape.strokeWidth = body.stroke_width;
          else if (nextShape.stroke && !nextShape.strokeWidth) nextShape.strokeWidth = 1.5;
        }
        state.pendingShapeStyle = nextShape;
        state.pendingStyle = null;
        state.pendingKind = "style";
        state.pendingD = null;
        state.job = { id: "job-ok", kind: "ink style", status: "running", progress: 10 };
        return json({ jobId: "job-ok", projectId: PID });
      }
      if (body.action === "shape_style") {
        // Task 40A: appearance-only — merge the new style over the current
        // revision's ink style (echoes the real backend's paint behavior).
        const cur = state.revInkStyle[state.revision] ?? INK_STYLE0;
        state.pendingStyle = {
          color: typeof body.stroke_color === "string" ? body.stroke_color : cur.color,
          width: typeof body.stroke_width === "number" ? body.stroke_width : cur.width,
          ...(typeof body.opacity === "number"
            ? body.opacity < 0.999
              ? { opacity: body.opacity }
              : {}
            : cur.opacity != null
              ? { opacity: cur.opacity }
              : {}),
        };
        state.pendingShapeStyle = null;
        state.pendingKind = "style";
        state.pendingD = null;
        state.job = { id: "job-ok", kind: "ink style", status: "running", progress: 10 };
        return json({ jobId: "job-ok", projectId: PID });
      }
      state.job = { id: "job-ok", kind: "shape edit", status: "running", progress: 10 };
      state.pendingD = String(body.d);
      state.pendingKind = "edit";
      state.pendingShape = String(body.shape_id ?? SHAPE);
      return json({ jobId: "job-ok", projectId: PID });
    }
    return json({ detail: "fixture route not implemented" }, 404);
  });
}
