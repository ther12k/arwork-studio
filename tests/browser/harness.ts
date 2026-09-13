/**
 * Task 32 review round-2 — REAL-BROWSER VectorBoard gate.
 *
 * A minimal harness page (no AI stack, no Studio UI) that mounts the REAL
 * VectorBoard on a deterministic overlap fixture and exposes probe hooks:
 *   - cached appearance state (the underpainting <image> per layer),
 *   - pixel reads rendered FROM the cached blob (real SVG decode, the same
 *     class of evidence as the reviewer's Chromium probe),
 *   - visibility controls driving the authoritative controller.
 *
 * Proven gates:
 *   1. empty cached layer: hiding the last visible shape REMOVES the cached
 *      image and its pixels (previously the stale image survived),
 *   2. cache lifecycle: rapid visibility changes cannot publish an outdated
 *      image (generation token),
 *   3. hide/isolate semantics follow the rendered output, not just classes.
 */

import { VectorBoard, type Bundle } from "../../src/lib/detailed-board";

// Overlap fixture (mirrors the backend test geometry, 300×300):
//   red rect (20,20)-(180,180), blue rect (100,100)-(260,260), blue on top.
// Regions are the visible partition: blue full rect; red L-shape minus blue.
const RED_VISIBLE = "M 20,20 L 180,20 L 180,100 L 100,100 L 100,260 L 20,260 Z";
const BLUE_FULL = "M 100,100 L 260,100 L 260,260 L 100,260 Z";

const bundle: Bundle = {
  manifest: {
    schemaVersion: 1,
    format: "color-duel-detailed-vector-1",
    id: "browser-fixture",
    version: "0.1.0",
    title: "Browser gate fixture",
    regionCount: 2,
    paletteCount: 2,
    objectGroups: [],
    assets: { regions: "regions.json", palette: "palette.json", paint: "paint.json" },
    contentHash: "browser-fixture",
    difficulty: { rating: "easy", score: 10, metrics: {} },
  },
  geometry: {
    schemaVersion: 1,
    artworkId: "browser-fixture",
    artworkVersion: "0.1.0",
    viewBox: [0, 0, 300, 300],
    fillRule: "evenodd",
    stroke: "#22333B",
    strokeWidth: 0.8,
    edges: [],
    regions: [
      {
        id: "r-red", paletteId: 1, objectId: "obj-red",
        d: RED_VISIBLE, fillRule: "evenodd", masterShapeId: "s0001",
        rings: [[[20,20],[180,20],[180,100],[100,100],[100,260],[20,260]]],
        bbox: [20, 20, 180, 260], area: 19200,
        label: { x: 60, y: 60, fontSize: 14, minScreenPx: 9, clearance: 20 },
      },
      {
        id: "r-blue", paletteId: 2, objectId: "obj-blue",
        d: BLUE_FULL, fillRule: "evenodd", masterShapeId: "s0002",
        rings: [[[100,100],[260,100],[260,260],[100,260]]],
        bbox: [100, 100, 260, 260], area: 25600,
        label: { x: 180, y: 180, fontSize: 14, minScreenPx: 9, clearance: 20 },
      },
    ],
    decorations: [],
    detailPaths: [],
  },
  palette: [
    { id: 1, number: 1, name: "Red", hex: "#CC3333", paint: { type: "solid", stops: [] } },
    { id: 2, number: 2, name: "Blue", hex: "#3366CC", paint: { type: "solid", stops: [] } },
  ],
  paint: {
    schemaVersion: 2,
    artworkId: "browser-fixture",
    viewBox: [0, 0, 300, 300],
    paths: [
      { z: 0, shapeId: "s0001", d: "M 20,20 L 180,20 L 180,180 L 20,180 Z", fill: "#CC3333", fillRule: "evenodd" },
      { z: 1, shapeId: "s0002", d: BLUE_FULL, fill: "#3366CC", fillRule: "evenodd" },
    ],
    inkPaths: [
      // open line art owned by obj-blue — exercises the ink layer cache
      { z: 2, shapeId: "s0003", d: "M 110,110 L 250,250", fill: "#1B4F8A", strokeWidth: 3, filled: false },
    ],
    gradients: [],
    sourceColorShapeCount: 3,
  },
  objects: {
    schemaVersion: 1,
    objects: [
      { id: "obj-red", name: "Red", shapeIds: ["s0001"] },
      { id: "obj-blue", name: "Blue", shapeIds: ["s0002", "s0003"] },
    ],
  },
};

const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
svg.setAttribute("width", "600");
svg.setAttribute("height", "600");
document.getElementById("app")!.appendChild(svg);

const board = new VectorBoard(svg, bundle, { persist: false, mode: "number" });
board.setPreview(true); // editor/inspect look: region masks fill-none, art visible

function imageHref(layerId: string): string | null {
  const node = svg.querySelector(`image[id$="${layerId}"]`) as SVGImageElement | null;
  return node ? (node.getAttribute("href") ?? node.getAttributeNS("http://www.w3.org/1999/xlink", "href")) : null;
}

/** Decode one cached layer blob and read a pixel — real SVG/blob decoding. */
async function pixelFromLayer(layerId: string, x: number, y: number): Promise<readonly [number, number, number, number] | null> {
  const href = imageHref(layerId);
  if (href === null) return null; // layer cleared = nothing painted there
  const img = new Image();
  img.src = href;
  await img.decode();
  const canvas = document.createElement("canvas");
  canvas.width = 300;
  canvas.height = 300;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#FFFFFF"; // white background like the reviewer's probe
  ctx.fillRect(0, 0, 300, 300);
  ctx.drawImage(img, 0, 0, 300, 300);
  const d = ctx.getImageData(x, y, 1, 1).data;
  return [d[0], d[1], d[2], d[3]] as const;
}

function settleFrames(ms = 250): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

(window as unknown as { __board: unknown }).__board = {
  setHidden: (ids: string[]) => board.setHiddenObjects(new Set(ids), null),
  isolate: (id: string | null) => board.setHiddenObjects(new Set(), id),
  cachedState: () => ({ artHref: imageHref("underpaint-art"), inkHref: imageHref("underpaint-ink") }),
  pixelArt: (x: number, y: number) => pixelFromLayer("underpaint-art", x, y),
  pixelInk: (x: number, y: number) => pixelFromLayer("underpaint-ink", x, y),
  settle: settleFrames,
  regionClass: (id: string) => board.elements.get(id)?.getAttribute("class") ?? "",
};
