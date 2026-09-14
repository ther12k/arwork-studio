/**
 * Fixture backend for the Task 33 journey gate (tests/browser/studio.spec.ts).
 *
 * A "controlled transport": every studio-api endpoint the real app calls is
 * answered from these tables via Playwright route interception, so the REAL
 * hook/page logic (runEdit guard, saveArtPath settle-watch, job polling,
 * VectorBoard mounting) decides every outcome — only the HTTP boundary is
 * staged. The staged backend models the bits the journey depends on: async
 * jobs (queued → done), semantic job failure with the revision untouched, a
 * 409 stale-base rejection, and revision-addressed bundle files (rev-2 serves
 * the edited paint).
 */

import type { Page } from "@playwright/test";

export const PID = "proj-journey";
export const REV1 = "rev-1";
export const REV2 = "rev-2";
export const SHAPE = "s0002";
/** Closed two-cubic blob; handle 1 = c1 (70,60) of the first cubic. */
export const ORIGINAL_D = "M 70,180 C 70,60 230,60 230,180 C 230,300 70,300 70,180 Z";
/** Serialized result of dragging handle 1 to art (170,60). */
export const EDITED_D = "M 70,180 C 170,60 230,60 230,180 C 230,300 70,300 70,180 Z";

export type EditMode = "reject409" | "jobfail" | "success";

export type FixtureState = {
  mode: EditMode;
  revision: string;
  paintD: string;
  job: Record<string, unknown>;
  editCalls: Array<Record<string, unknown>>;
  runningGets: number;
  pendingD: string;
};

export const mkState = (): FixtureState => ({
  mode: "reject409",
  revision: REV1,
  paintD: ORIGINAL_D,
  job: { status: "idle" },
  editCalls: [],
  runningGets: 0,
  pendingD: "",
});

const ART = "journey-artwork";

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

const manifest = {
  schemaVersion: 1,
  format: "color-duel-detailed-vector-1",
  id: ART,
  version: "0.1.0",
  title: "Journey fixture",
  regionCount: 2,
  paletteCount: 2,
  objectGroups: [],
  assets: { regions: "regions.json", palette: "palette.json", paint: "paint.json" },
  contentHash: "journey-fixture",
  difficulty: { rating: "easy", score: 10, metrics: {} },
};

const geometry = {
  schemaVersion: 1,
  artworkId: ART,
  artworkVersion: "0.1.0",
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
};

const palette = [
  { id: 1, number: 1, name: "Red", hex: "#CC3333", paint: { type: "solid", stops: [] } },
  { id: 2, number: 2, name: "Blue", hex: "#3366CC", paint: { type: "solid", stops: [] } },
];

const paintFor = (d: string) => ({
  schemaVersion: 2,
  artworkId: ART,
  viewBox: [0, 0, 300, 300],
  paths: [
    { z: 0, shapeId: "s0001", d: "M 20,20 L 280,20 L 280,280 L 20,280 Z", fill: "#CC3333", fillRule: "evenodd" },
    { z: 1, shapeId: SHAPE, d, fill: "#3366CC", fillRule: "evenodd" },
  ],
  inkPaths: [],
  gradients: [],
  sourceColorShapeCount: 2,
});

const objects = {
  schemaVersion: 1,
  objects: [
    { id: "obj-red", name: "Red", shapeIds: ["s0001"] },
    { id: "obj-blue", name: "Blue", shapeIds: [SHAPE] },
  ],
};

const project = (state: FixtureState) => ({
  id: PID,
  title: "Journey fixture",
  brief: "fixture brief",
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  messages: [],
  aiUsage: [],
  master: null,
  reference: null,
  revisions: [revision(REV1), ...(state.revision === REV2 ? [revision(REV2)] : [])],
  currentRevision: state.revision,
  job: state.job,
});

/** Success jobs flip to done + publish rev-2 on the SECOND project GET (the
 *  first is the POST handler's own refresh, the second the poll loop). */
const advanceJob = (state: FixtureState) => {
  if (state.job.status === "running") {
    state.runningGets += 1;
    if (state.runningGets >= 2) {
      state.job = { id: "job-ok", kind: "shape edit", status: "done", progress: 100 };
      state.revision = REV2;
      state.paintD = state.pendingD;
    }
  }
};

const fileFor = (state: FixtureState, rev: string, name: string): unknown => {
  const d = rev === REV2 ? state.paintD : ORIGINAL_D;
  switch (name) {
    case "artwork.json":
      return manifest;
    case "regions.json":
      return geometry;
    case "palette.json":
      return palette;
    case "paint.json":
      return paintFor(d);
    case "objects.json":
      return objects;
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
    if (path === "/api/projects" && req.method() === "GET") return json([project(state)]);
    if (path === `/api/projects/${PID}` && req.method() === "GET") {
      advanceJob(state);
      return json(project(state));
    }
    if (path === `/api/projects/${PID}` && req.method() === "PATCH") return json(project(state));
    if (path === `/api/projects/${PID}/generation/sessions`) return json({ sessions: [] });
    const file = path.match(new RegExp(`^/api/projects/${PID}/revisions/([^/]+)/files/(.+)$`));
    if (file && req.method() === "GET") return json(fileFor(state, file[1], file[2]));
    if (path === `/api/projects/${PID}/edit` && req.method() === "POST") {
      const body = req.postDataJSON() as Record<string, unknown>;
      state.editCalls.push(body);
      if (state.mode === "reject409") {
        return json({ detail: "Stale base revision: the project has moved on." }, 409);
      }
      if (state.mode === "jobfail") {
        state.job = {
          id: "job-fail",
          kind: "shape edit",
          status: "failed",
          progress: 100,
          message: "Edited path is not a simple ring.",
        };
        return json({ jobId: "job-fail", projectId: PID });
      }
      state.job = { id: "job-ok", kind: "shape edit", status: "running", progress: 10 };
      state.runningGets = 0;
      state.pendingD = String(body.d);
      return json({ jobId: "job-ok", projectId: PID });
    }
    return json({ detail: "fixture route not implemented" }, 404);
  });
}
