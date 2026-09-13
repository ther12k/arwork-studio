/**
 * VectorBoard — dependency-free region renderer for Color Duel detailed-vector bundles.
 * Ported from the vanilla-JS `web/detailed-board.mjs` (see docs/PROVENANCE.md upstream).
 *
 * Consume trusted, validated JSON only; never inject arbitrary uploaded SVG markup.
 * Changes vs. the original:
 *  - `loadArtwork` (browser-relative URL resolution) is replaced by `loadBundle(pid, rev)`
 *    which fetches through the gateway with an explicit `XTransformPort` query.
 *  - No localStorage session persistence: authoring play-tests must not write player
 *    progress. Sessions live in memory only.
 *
 * Stage 2 (contract §8 performance):
 *  - the paint + ink appearance is cached ONCE per bundle as standalone SVG
 *    <image> elements (see buildUnderpainting) — one image node replaces the
 *    200k+ live path commands a big bundle carries;
 *  - pointermove/wheel are rAF-batched, and during drag/pinch the viewBox is
 *    untouched: a GPU CSS transform moves the whole board, and the final view
 *    is applied once at gesture end (see bindGestures).
 */

import type { DifficultyProfile, ObjectsFile, SemanticObject } from "./studio-api";

const NS = "http://www.w3.org/2000/svg";
let sequence = 0;

const clamp = (x: number, min: number, max: number) => Math.max(min, Math.min(max, x));

type Attrs = Record<string, string | number | undefined>;

function svgNode(tag: string, attrs: Attrs = {}): SVGElement {
  const el = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}

// ---------------------------------------------------------------------------
// Underpainting serialization (contract §8) — standalone SVG documents
// ---------------------------------------------------------------------------

const XML_ESCAPES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" };
const escapeXml = (value: string): string => value.replace(/[&<>"']/g, (c) => XML_ESCAPES[c]);

/** Clone the paint gradient defs into the standalone document (the hatch
 *  pattern and palette gradients stay live in the board DOM). */
function serializeGradientDefs(gradients: PaintGradient[]): string {
  const parts: string[] = [];
  for (const g of gradients) {
    const tag = g.type === "linear" ? "linearGradient" : "radialGradient";
    const attrs = [`id="${escapeXml(g.id)}"`, 'gradientUnits="userSpaceOnUse"'];
    const params: Array<[string, number | undefined]> =
      g.type === "linear"
        ? [
            ["x1", g.x1],
            ["y1", g.y1],
            ["x2", g.x2],
            ["y2", g.y2],
          ]
        : [
            ["cx", g.cx],
            ["cy", g.cy],
            ["r", g.r],
            ["fx", g.fx],
            ["fy", g.fy],
          ];
    for (const [key, value] of params) if (value != null) attrs.push(`${key}="${value}"`);
    const stops = g.stops
      .map((s) => {
        const sa = [`offset="${s.offset}"`, `stop-color="${escapeXml(s.color)}"`];
        if (s.opacity != null && s.opacity !== 1) sa.push(`stop-opacity="${s.opacity}"`);
        return `<stop ${sa.join(" ")}/>`;
      })
      .join("");
    parts.push(`<${tag} ${attrs.join(" ")}>${stops}</${tag}>`);
  }
  return parts.join("");
}

/** Art-layer path (below the masks): closed fills, z-sorted — the exact
 *  attribute logic the live `vector-paint` group uses in mount(). */
function serializeArtPath(p: PaintPath): string | null {
  if (p.filled === false || (p.strokeWidth != null && !p.fill.startsWith("#"))) return null;
  const gradientFill = p.fill.startsWith("url(#");
  const attrs = [`d="${escapeXml(p.d)}"`, `fill="${escapeXml(p.fill)}"`, `fill-rule="${p.fillRule ?? "evenodd"}"`];
  if (!gradientFill) {
    attrs.push(`stroke="${escapeXml(p.stroke ?? p.fill)}"`, `stroke-width="${p.strokeWidth ?? 0.55}"`, 'stroke-linejoin="round"');
  }
  if (p.fillOpacity != null && p.fillOpacity < 0.999) attrs.push(`fill-opacity="${p.fillOpacity}"`);
  if (p.opacity != null && p.opacity < 0.999) attrs.push(`opacity="${p.opacity}"`);
  return `<path ${attrs.join(" ")}/>`;
}

/** Ink-layer path (above the masks): open stroke line art + closed ink
 *  shapes — the exact attribute logic the live `ink` group uses in mount(). */
function serializeInkPath(p: PaintPath): string {
  if (p.strokeWidth != null || p.filled === false) {
    return `<path d="${escapeXml(p.d)}" fill="none" stroke="${escapeXml(p.fill)}" stroke-width="${p.strokeWidth ?? 1.5}" stroke-linecap="round" stroke-linejoin="round"/>`;
  }
  const attrs = [`d="${escapeXml(p.d)}"`, `fill="${escapeXml(p.fill)}"`, `fill-rule="${p.fillRule ?? "evenodd"}"`];
  if (p.opacity != null && p.opacity < 0.999) attrs.push(`opacity="${p.opacity}"`);
  if (p.fillOpacity != null && p.fillOpacity < 0.999) attrs.push(`fill-opacity="${p.fillOpacity}"`);
  return `<path ${attrs.join(" ")}/>`;
}

function wrapStandaloneSvg(defs: string, body: string[], bx: number, by: number, bw: number, bh: number): string {
  return (
    `<svg xmlns="${NS}" viewBox="${bx} ${by} ${bw} ${bh}" width="${bw}" height="${bh}">` +
    (defs ? `<defs>${defs}</defs>` : "") +
    body.join("") +
    "</svg>"
  );
}

// ---------------------------------------------------------------------------
// Bundle types (modelled on examples/compiled-treehouse/*.json + schemas)
// ---------------------------------------------------------------------------

export interface PaletteStop {
  offset: number;
  color: string;
}

export interface PaletteEntry {
  id: number;
  number: number;
  name: string;
  hex: string;
  paint: { type: string; stops: PaletteStop[] };
}

export interface RegionLabel {
  x: number;
  y: number;
  fontSize: number;
  minScreenPx: number;
  clearance: number;
}

export interface RegionEntry {
  id: string;
  paletteId: number;
  objectId?: string;
  d: string;
  fillRule: "evenodd" | "nonzero";
  masterShapeId?: string;
  rings: number[][][];
  bbox: number[];
  area: number;
  label: RegionLabel;
}

export interface DetailPath {
  d: string;
  stroke: string;
  strokeWidth: number;
  opacity?: number;
}

/** Stage-2 boundary kinds: true artwork outlines vs artificial subdivisions. */
export interface EdgeEntry {
  id: string;
  /** M/L/C/Q path, open or closed (matches the safe-path regexes). */
  d: string;
  kind: "artwork" | "subdivision";
  leftRegion?: string | null;
  rightRegion?: string | null;
}

export interface BoundaryStyleEntry {
  stroke: string;
  strokeWidth: number;
  dash?: string;
}

export interface BoundaryStyle {
  artwork: BoundaryStyleEntry;
  subdivision: BoundaryStyleEntry;
}

export interface Geometry {
  schemaVersion: number;
  artworkId: string;
  artworkVersion: string;
  viewBox: number[];
  fillRule: "evenodd" | "nonzero";
  stroke: string;
  strokeWidth: number;
  regions: RegionEntry[];
  decorations: RegionEntry[];
  detailPaths: DetailPath[];
  /** Non-empty ⇒ regions render fill-only and boundaries come from the edges
   *  overlay (artwork solid, subdivision dashed). Absent/empty ⇒ legacy look. */
  edges?: EdgeEntry[];
  boundaryStyle?: BoundaryStyle;
}

export interface PaintPath {
  fill: string; // #RRGGBB or url(#gradient-id)
  d: string;
  strokeWidth?: number; // present on stroked ink paths (open line art)
  filled?: boolean; // false = stroke-rendered ink, not a closed fill
  fillRule?: "evenodd" | "nonzero"; // source fill rule (schema 2)
  fillOpacity?: number; // 0..1, preserved from the source SVG
  opacity?: number; // 0..1, preserved from the source SVG
  stroke?: string; // outline color on FILLED shapes (preserved)
  z?: number; // document order: paint + ink render merged by z
  shapeId?: string; // link to the sanitized master shape (authoring)
}

export interface PaintGradientStop {
  offset: number;
  color: string;
  opacity?: number;
}

export interface PaintGradient {
  id: string;
  type: "linear" | "radial";
  stops: PaintGradientStop[];
  // linear params
  x1?: number;
  y1?: number;
  x2?: number;
  y2?: number;
  // radial params
  cx?: number;
  cy?: number;
  r?: number;
  fx?: number;
  fy?: number;
}

export interface PaintLayer {
  schemaVersion: number;
  artworkId: string;
  viewBox?: number[];
  paths: PaintPath[];
  inkPaths: PaintPath[];
  gradients?: PaintGradient[];
}

export interface ObjectGroup {
  id: string;
  regionIds: string[];
}

export interface Manifest {
  schemaVersion: number;
  format: "color-duel-vector-1" | "color-duel-detailed-vector-1";
  id: string;
  version: string;
  title: string;
  description?: string;
  regionCount: number;
  paletteCount?: number;
  objectGroups?: ObjectGroup[];
  assets: { regions: string; palette: string; paint?: string; [key: string]: string | undefined };
  contentHash: string;
  /** Legacy bundles carry the string "unrated"; new ones carry the full
   *  profile (contract §5). The UI normalizes both. */
  difficulty?: string | DifficultyProfile;
  /** Stage 3 (contract B): true once at least one completed play-test run
   *  has been recorded for this revision. */
  difficultyValidatedByPlaytest?: boolean;
  /** Generation provenance on AI-created revisions — `sessionId` is the
   *  linkage the Object Inspector requires before offering targeted
   *  regeneration (an unrelated draft must never be reused). */
  generation?: { sessionId?: string } & Record<string, unknown>;
}

export interface Bundle {
  manifest: Manifest;
  geometry: Geometry;
  palette: PaletteEntry[];
  paint: PaintLayer | null;
  objects?: ObjectsFile | null;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const SAFE_PATH = /^M[\s\d.,eE+\-MLQCZ]+Z$/;
const SAFE_PATH_OPEN = /^M[\s\d.,eE+\-MLQCZ]+$/;
const SAFE_HEX = /^#[0-9A-Fa-f]{6}$/;
const SAFE_GRADIENT_REF = /^url\(#g-[a-zA-Z0-9_-]+\)$/;

export function validateBundle(bundle: Bundle): Bundle {
  const { manifest: m, geometry: g } = bundle || ({} as Bundle);
  const p = bundle?.palette;
  if (!m || !g || !Array.isArray(p) || !["color-duel-vector-1", "color-duel-detailed-vector-1"].includes(m.format))
    throw new Error("Unsupported artwork bundle");
  if (m.id !== g.artworkId || m.version !== g.artworkVersion || m.regionCount !== g.regions?.length)
    throw new Error("Artwork identity/count mismatch");
  if (
    !Array.isArray(g.viewBox) ||
    g.viewBox.length !== 4 ||
    g.viewBox.some((x) => !Number.isFinite(x)) ||
    g.viewBox[2] <= 0 ||
    g.viewBox[3] <= 0
  )
    throw new Error("Invalid viewBox");
  const paletteIds = new Set(p.map((x) => x.id));
  if (paletteIds.size !== p.length) throw new Error("Duplicate palette ID");
  const ids = new Set<string>();
  for (const r of [...g.regions, ...(g.decorations ?? [])]) {
    if (ids.has(r.id) || !/^[a-zA-Z0-9_-]+$/.test(r.id)) throw new Error("Duplicate or unsafe region ID");
    ids.add(r.id);
    const rule = r.fillRule ?? "evenodd";
    if (!paletteIds.has(r.paletteId) || !["evenodd", "nonzero"].includes(rule) || !SAFE_PATH.test(r.d))
      throw new Error("Invalid region geometry or palette");
  }
  // Edges overlay (contract §1): optional, additive, validated like the rest.
  if (g.edges !== undefined) {
    if (!Array.isArray(g.edges)) throw new Error("Invalid edges");
    const edgeIds = new Set<string>();
    for (const e of g.edges) {
      if (!e || typeof e.id !== "string" || !/^[a-zA-Z0-9_-]+$/.test(e.id)) throw new Error("Invalid edge ID");
      if (edgeIds.has(e.id)) throw new Error("Duplicate edge ID");
      edgeIds.add(e.id);
      if (typeof e.d !== "string" || !(SAFE_PATH.test(e.d) || SAFE_PATH_OPEN.test(e.d)))
        throw new Error("Invalid edge path");
      if (e.kind !== "artwork" && e.kind !== "subdivision") throw new Error("Invalid edge kind");
      if (e.leftRegion != null && !ids.has(e.leftRegion)) throw new Error("Edge references unknown region");
      if (e.rightRegion != null && !ids.has(e.rightRegion)) throw new Error("Edge references unknown region");
    }
    if (g.boundaryStyle !== undefined) {
      const bs = g.boundaryStyle;
      if (!bs || typeof bs !== "object") throw new Error("Invalid boundary style");
      for (const key of ["artwork", "subdivision"] as const) {
        const entry = bs[key];
        if (entry === undefined || entry === null) continue; // per-kind optional
        if (
          typeof entry !== "object" ||
          typeof entry.stroke !== "string" ||
          entry.stroke.length === 0 ||
          !Number.isFinite(entry.strokeWidth)
        )
          throw new Error("Invalid boundary style");
        if (entry.dash !== undefined && typeof entry.dash !== "string") throw new Error("Invalid boundary dash");
      }
    }
  }
  if (m.format === "color-duel-detailed-vector-1") {
    const paint = bundle.paint;
    if (!paint || paint.artworkId !== m.id || !Array.isArray(paint.paths) || !Array.isArray(paint.inkPaths))
      throw new Error("Missing detailed vector paint");
    const gradientIds = new Set((paint.gradients ?? []).map((x) => x.id));
    for (const path of paint.paths) {
      const okFill = SAFE_HEX.test(path.fill) ||
        (SAFE_GRADIENT_REF.test(path.fill) && gradientIds.has(path.fill.slice(5, -1)));
      if (!okFill || !SAFE_PATH.test(path.d)) throw new Error("Invalid paint path");
      if (path.fillRule !== undefined && !["evenodd", "nonzero"].includes(path.fillRule))
        throw new Error("Invalid paint fill rule");
      for (const key of ["opacity", "fillOpacity", "strokeWidth", "z"] as const) {
        const v = path[key];
        if (v !== undefined && !Number.isFinite(v)) throw new Error(`Invalid paint ${key}`);
      }
    }
    // Ink layer holds closed filled shapes OR open stroke line art.
    for (const path of paint.inkPaths) {
      const okFill = SAFE_HEX.test(path.fill) ||
        (SAFE_GRADIENT_REF.test(path.fill) && gradientIds.has(path.fill.slice(5, -1)));
      const okPath = path.strokeWidth != null || path.filled === false ? SAFE_PATH_OPEN.test(path.d) : SAFE_PATH.test(path.d);
      if (!okFill || !okPath) throw new Error("Invalid ink path");
    }
  }
  return bundle;
}

// ---------------------------------------------------------------------------
// Loader — gateway-aware (XTransformPort must survive in the URL)
// ---------------------------------------------------------------------------

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { "X-Studio-Request": "1" } });
  if (!res.ok) throw new Error(`Artwork request failed: ${res.status}`);
  return res.json() as Promise<T>;
}

/** Fetch + validate the bundle files for a revision, through the gateway. */
export async function loadBundle(pid: string, rev: string): Promise<Bundle> {
  const base = `/api/projects/${pid}/revisions/${rev}/files/`;
  const q = "?XTransformPort=8765";
  const [manifest, geometry, palette, paint, objects] = await Promise.all([
    fetchJson<Manifest>(base + "artwork.json" + q),
    fetchJson<Geometry>(base + "regions.json" + q),
    fetchJson<PaletteEntry[]>(base + "palette.json" + q),
    fetchJson<PaintLayer | null>(base + "paint.json" + q),
    fetchJson<ObjectsFile | null>(base + "objects.json" + q).catch(() => null),
  ]);
  const bundle = validateBundle({ manifest, geometry, palette, paint });
  bundle.objects = objects;
  return bundle;
}

// ---------------------------------------------------------------------------
// Board state
// ---------------------------------------------------------------------------

export type PaintResult = "ignored" | "already-complete" | "wrong-color" | "painted";
export type BoardMode = "number" | "memory" | "free";
export type ChangeReason = "render" | "paint" | "wrong-color" | "viewport" | string;

export interface BoardState {
  mode: BoardMode;
  artworkId: string;
  artworkVersion: string;
  completedRegionIds: string[];
  selectedPaletteId: number;
  /** Active free-mode color (#RRGGBB); null until a custom color is chosen. */
  customColor: string | null;
  mistakes: number;
  total: number;
  completed: number;
  progress: number;
  objects: Record<string, { completed: number; total: number }>;
  preview: boolean;
  zoom: number;
}

interface Session {
  artworkId: string;
  artworkVersion: string;
  mode: BoardMode;
  completedRegionIds: string[];
  selectedPaletteId: number;
  /** Region id → applied #RRGGBB (free mode stores true hex, not palette ids). */
  freeColors: Record<string, string>;
  mistakes: number;
  updatedAt: string;
}

export interface BoardOptions {
  mode?: BoardMode;
  persist?: boolean;
  onChange?: (state: BoardState, reason: ChangeReason) => void;
}

// ---------------------------------------------------------------------------
// Object visibility (Task 32 review R2) — one authoritative, transient state
// ---------------------------------------------------------------------------

/** Contract: hiding or isolating a PARENT operates on its subtree; selecting
 *  or hiding an individual child still operates on that child alone. Ids the
 *  catalog no longer knows (dropped in a newer revision) are ignored — the
 *  catalog stays authoritative over stale hidden state. */
export function computeObjectVisibility(
  objects: SemanticObject[],
  hiddenIds: Set<string>,
  isolatedId: string | null
): Set<string> {
  const known = new Set(objects.map((o) => o.id));
  const isolated = isolatedId && known.has(isolatedId) ? isolatedId : null;
  const childrenOf = new Map<string, string[]>();
  for (const o of objects) {
    if (o.parentId && o.parentId !== o.id) {
      const arr = childrenOf.get(o.parentId) ?? [];
      arr.push(o.id);
      childrenOf.set(o.parentId, arr);
    }
  }
  const subtree = (root: string): Set<string> => {
    const out = new Set<string>();
    const stack = [root];
    while (stack.length) {
      const id = stack.pop()!;
      if (out.has(id)) continue;
      out.add(id);
      for (const child of childrenOf.get(id) ?? []) stack.push(child);
    }
    return out;
  };
  const effective = new Set<string>();
  if (isolated) {
    const keep = subtree(isolated);
    for (const o of objects) if (!keep.has(o.id)) effective.add(o.id);
    return effective;
  }
  for (const id of hiddenIds) {
    if (!known.has(id)) continue;
    for (const id2 of subtree(id)) effective.add(id2);
  }
  return effective;
}

/**
 * Authoritative editor-side visibility state for semantic objects. The board
 * consults it for artwork (cached underpainting or live paths), region
 * overlays, labels, hit-testing and keyboard focus; it survives zoom, resize,
 * palette changes and completion refreshes because every one of those paths
 * re-reads this state instead of one-off DOM classes.
 */
export class ObjectVisibilityController {
  private objects: SemanticObject[] = [];
  private hidden = new Set<string>();
  private isolated: string | null = null;
  private highlightId: string | null = null;
  private hiddenRegions = new Set<string>();
  private hiddenShapes = new Set<string>();
  private unownedHidden = false;
  private owners = new Map<string, string>();

  /** Refresh the object catalog after a bundle load; recomputes derived sets. */
  setCatalog(objects: SemanticObject[]): void {
    this.objects = objects ?? [];
    this.recompute();
  }

  setHiddenObjects(hidden: Set<string>, isolated: string | null): void {
    this.hidden = new Set(hidden);
    this.isolated = isolated;
    this.recompute();
  }

  setHighlightedObject(objectId: string | null): void {
    this.highlightId = objectId;
  }

  isRegionHidden(id: string): boolean {
    return this.hiddenRegions.has(id);
  }

  hiddenShapeIds(): Set<string> {
    return this.hiddenShapes;
  }

  unownedArtHidden(): boolean {
    return this.unownedHidden;
  }

  shapeOwner(shapeId: string): string | undefined {
    return this.owners.get(shapeId);
  }

  highlight(): string | null {
    return this.highlightId;
  }

  ownedShapeIds(objectId: string): Set<string> {
    return new Set(this.objects.find((o) => o.id === objectId)?.shapeIds ?? []);
  }

  /** True when a shape belongs to no object (drives isolate on unowned art). */
  isShapeHidden(shapeId: string | undefined): boolean {
    if (shapeId && this.hiddenShapes.has(shapeId)) return true;
    if (this.unownedHidden) {
      if (!shapeId) return true;
      if (!this.owners.has(shapeId)) return true;
    }
    return false;
  }

  private recompute(): void {
    this.owners = new Map();
    for (const o of this.objects) {
      for (const sid of o.shapeIds ?? []) this.owners.set(sid, o.id);
    }
    const effective = computeObjectVisibility(this.objects, this.hidden, this.isolated);
    this.hiddenRegions = new Set();
    for (const [id, region] of this.regionsLookup()) {
      const oid = region.objectId;
      if (!oid || oid === "unassigned") {
        // Isolate hides art outside the isolated subtree — including
        // regions that belong to no object at all.
        if (this.isolated) this.hiddenRegions.add(id);
        continue;
      }
      if (effective.has(oid)) this.hiddenRegions.add(id);
    }
    this.hiddenShapes = new Set();
    for (const o of this.objects) {
      if (!effective.has(o.id)) continue;
      for (const sid of o.shapeIds ?? []) this.hiddenShapes.add(sid);
    }
    this.unownedHidden = this.isolated != null;
  }

  private regionsLookup(): Iterable<[string, { objectId?: string }]> {
    // The controller stays DOM-free: the board injects its region map here.
    return this.regionsSource ? this.regionsSource() : [];
  }

  regionsSource: (() => Iterable<[string, { objectId?: string }]> ) | null = null;
}

// ---------------------------------------------------------------------------
// VectorBoard
// ---------------------------------------------------------------------------

export class VectorBoard {
  readonly svg: SVGSVGElement;
  readonly bundle: Bundle;
  readonly options: BoardOptions;
  mode: BoardMode;
  readonly prefix: string;
  /** Active free-mode color (#RRGGBB) — null until a custom color is chosen.
   *  In free mode this is what paint() applies; palette swatches load their
   *  entry's hex here as a quick access. */
  customColor: string | null = null;
  private handlers: Array<() => void> = [];
  private pointers = new Map<number, { x: number; y: number }>();
  private history: Array<{ id: string; wasCompleted: boolean; oldColor?: string }> = [];
  preview = false;
  private ctx: CanvasRenderingContext2D | null;
  regions = new Map<string, RegionEntry>();
  private paths = new Map<string, Path2D>();
  readonly elements = new Map<string, SVGPathElement>();
  private labels = new Map<string, SVGTextElement>();
  private session: Session;
  private completed = new Set<string>();
  private base: number[];
  private view: number[];
  private detailed: boolean;
  /** Non-empty edges overlay ⇒ fill-only region paths (boundaries drawn by the overlay). */
  private edgesMode = false;
  /** Live appearance groups replaced by cached underpainting <image> elements. */
  private artLayer: SVGGElement | null = null;
  private inkLayer: SVGGElement | null = null;
  private blobUrls: string[] = [];
  private destroyed = false;
  private rafId = 0;
  private flushGestureFrame: (() => void) | null = null;
  /** View the in-flight gesture targets; applied once at gesture end. */
  private pendingView: number[] | null = null;
  /** Gesture-start bounding rect (the CSS transform would corrupt fresh reads). */
  private gestureRect: DOMRect | null = null;
  private drag: { x: number; y: number; view: number[]; inverse: DOMMatrix; anchor: DOMPoint | null; moved: boolean } | null = null;
  private pinch: { distance: number; view: number[]; inverse: DOMMatrix; anchor: DOMPoint | null; mid0: { x: number; y: number } } | null = null;
  private suppressTap = false;
  /** Set by the wrapper (clientToArt override) — last art-space point of a tap. */
  lastTapPoint: DOMPoint | null = null;
  /** Task 32 R2 — authoritative transient object visibility/hover state. */
  private visibility = new ObjectVisibilityController();
  /** Active underpainting blob URL per layer id — revoked when the cached
   *  appearance is regenerated (visibility change) or on destroy. */
  private underpaintUrls = new Map<string, string>();

  constructor(svg: SVGSVGElement, bundle: Bundle, options: BoardOptions = {}) {
    if (!(svg instanceof SVGSVGElement)) throw new Error("Pass an <svg> element to VectorBoard");
    validateBundle(bundle);
    this.svg = svg;
    this.bundle = bundle;
    this.options = options;
    this.mode = options.mode ?? "number";
    if (!["number", "memory", "free"].includes(this.mode)) throw new Error("Invalid coloring mode");
    this.prefix = `cdv-${++sequence}-`;
    this.ctx = document.createElement("canvas").getContext("2d");
    if (!this.ctx) throw new Error("Canvas hit-testing is unavailable");
    this.regions = new Map(bundle.geometry.regions.map((r) => [r.id, r]));
    this.paths = new Map([...this.regions].map(([id, r]) => [id, new Path2D(r.d)]));
    const paletteIds = [...new Set(bundle.palette.map((p) => p.id))].sort((a, b) => a - b);
    this.session = {
      artworkId: bundle.manifest.id,
      artworkVersion: bundle.manifest.version,
      mode: this.mode,
      completedRegionIds: [],
      selectedPaletteId: paletteIds[0] ?? 1,
      freeColors: {},
      mistakes: 0,
      updatedAt: new Date().toISOString(),
    };
    this.completed = new Set(this.session.completedRegionIds);
    this.base = [...bundle.geometry.viewBox];
    this.view = [...this.base];
    this.detailed = bundle.manifest.format === "color-duel-detailed-vector-1";
    this.edgesMode = Array.isArray(bundle.geometry.edges) && bundle.geometry.edges.length > 0;
    this.visibility.regionsSource = () => this.regions;
    this.visibility.setCatalog(bundle.objects?.objects ?? []);
    this.mount();
    this.bindGestures();
    this.refresh();
    this.buildUnderpainting();
  }

  /** Tap-to-fill. Kept as an overridable field so authoring modes can intercept. */
  paint: (id: string | null) => PaintResult | string = (id) => {
    const r = id ? this.regions.get(id) : undefined;
    if (!r || this.preview) return "ignored";
    if (this.completed.has(id!) && this.mode !== "free") return "already-complete";
    if (this.mode !== "free" && r.paletteId !== this.session.selectedPaletteId) {
      this.session.mistakes++;
      this.save();
      this.notify("wrong-color");
      return "wrong-color";
    }
    if (this.mode === "free") {
      // Free mode: any #RRGGBB is legal — no palette check, no mistake
      // counting. Falls back to the selected palette entry's hex until a
      // custom color is chosen (palette swatches double as quick access).
      const hex = this.customColor ?? this.paletteHex(this.session.selectedPaletteId) ?? "#FFFFFF";
      if (this.completed.has(id!) && this.session.freeColors[id!] === hex) return "already-complete";
      this.history.push({ id: id!, wasCompleted: this.completed.has(id!), oldColor: this.session.freeColors[id!] });
      this.completed.add(id!);
      this.session.freeColors[id!] = hex;
      this.save();
      this.refresh();
      this.notify("paint");
      return "painted";
    }
    this.history.push({ id: id!, wasCompleted: this.completed.has(id!), oldColor: this.session.freeColors[id!] });
    this.completed.add(id!);
    this.save();
    this.refresh();
    this.notify("paint");
    return "painted";
  };

  /** Client-space → artwork-space transform. Overridable (used to capture tap points). */
  clientToArt: (x: number, y: number, matrix?: DOMMatrix | null) => DOMPoint | null = (x, y, matrix) => {
    const ctm = matrix ?? this.svg.getScreenCTM()?.inverse() ?? null;
    if (!ctm) return null;
    return new DOMPoint(x, y).matrixTransform(ctm);
  };

  private mount() {
    this.svg.replaceChildren();
    this.svg.setAttribute("viewBox", this.view.join(" "));
    this.svg.setAttribute("aria-label", `${this.bundle.manifest.title}, interactive coloring artwork`);
    this.svg.setAttribute("role", "group");
    this.svg.style.touchAction = "none";
    const defs = svgNode("defs");
    for (const p of this.bundle.palette) {
      const grad = svgNode("linearGradient", {
        id: `${this.prefix}paint-${p.id}`,
        x1: "0%",
        y1: "0%",
        x2: "100%",
        y2: "100%",
      });
      for (const stop of p.paint.stops) grad.append(svgNode("stop", { offset: stop.offset, "stop-color": stop.color }));
      defs.append(grad);
    }
    if (this.detailed && this.bundle.paint?.gradients) {
      for (const g of this.bundle.paint.gradients) {
        const attrs: Attrs =
          g.type === "linear"
            ? { id: g.id, gradientUnits: "userSpaceOnUse", x1: g.x1, y1: g.y1, x2: g.x2, y2: g.y2 }
            : { id: g.id, gradientUnits: "userSpaceOnUse", cx: g.cx, cy: g.cy, r: g.r, fx: g.fx, fy: g.fy };
        const node = svgNode(g.type === "linear" ? "linearGradient" : "radialGradient", attrs);
        for (const stop of g.stops) {
          const stopAttrs: Attrs = { offset: stop.offset, "stop-color": stop.color };
          if (stop.opacity != null && stop.opacity !== 1) stopAttrs["stop-opacity"] = stop.opacity;
          node.append(svgNode("stop", stopAttrs));
        }
        defs.append(node);
      }
    }
    const hatch = svgNode("pattern", {
      id: this.prefix + "selected",
      width: 12,
      height: 12,
      patternUnits: "userSpaceOnUse",
    });
    hatch.append(
      svgNode("rect", { width: 12, height: 12, fill: "#EDF1F4" }),
      svgNode("path", { d: "M0 0H6V6H0Z M6 6H12V12H6Z", fill: "#C3CED4" })
    );
    defs.append(hatch);
    this.svg.append(defs);
    const g = this.bundle.geometry;
    if (this.detailed && this.bundle.paint) {
      // Appearance layer below the masks: filled paths in ORIGINAL drawing
      // order (z), honouring per-path fill rule, fill-opacity/opacity and
      // strokes on filled shapes. Stroke-only ink renders in the ink layer
      // above the masks so the linework stays visible during play.
      const art = svgNode("g", { "data-layer": "vector-paint", "pointer-events": "none" }) as SVGGElement;
      const entries = [...this.bundle.paint.paths, ...this.bundle.paint.inkPaths].sort(
        (a, b) => (a.z ?? Infinity) - (b.z ?? Infinity)
      );
      for (const p of entries) {
        if (p.filled === false || (p.strokeWidth != null && !p.fill.startsWith("#"))) continue;
        const gradientFill = p.fill.startsWith("url(#");
        const attrs: Attrs = { d: p.d, fill: p.fill, "fill-rule": p.fillRule ?? "evenodd" };
        if (p.shapeId) attrs["data-shape-id"] = p.shapeId;
        if (!gradientFill) {
          attrs.stroke = p.stroke ?? p.fill;
          attrs["stroke-width"] = p.strokeWidth ?? 0.55;
          attrs["stroke-linejoin"] = "round";
        }
        if (p.fillOpacity != null && p.fillOpacity < 0.999) attrs["fill-opacity"] = p.fillOpacity;
        if (p.opacity != null && p.opacity < 0.999) attrs.opacity = p.opacity;
        if (p.stroke && p.strokeWidth && p.strokeWidth > 0 && !gradientFill) {
          attrs.stroke = p.stroke;
          attrs["stroke-width"] = p.strokeWidth;
        }
        art.append(svgNode("path", attrs));
      }
      this.artLayer = art;
      this.svg.append(art);
    }
    // With an edges overlay, region paths render FILL-ONLY: boundaries are
    // drawn by the overlay (always visible, even for completed regions).
    const regionGroupAttrs: Attrs = { "stroke-linejoin": "round" };
    if (!this.edgesMode) {
      regionGroupAttrs.stroke = g.stroke;
      regionGroupAttrs["stroke-width"] = g.strokeWidth;
    }
    const regions = svgNode("g", regionGroupAttrs);
    this.elements.clear();
    this.labels.clear();
    for (const r of g.regions) {
      const node = svgNode("path", {
        id: this.prefix + r.id,
        "data-region-id": r.id,
        "data-palette-id": r.paletteId,
        "data-object-id": r.objectId || "",
        d: r.d,
        tabindex: 0,
        role: "button",
        "fill-rule": r.fillRule ?? "evenodd",
        "aria-label": `Region ${r.id}, palette ${this.mode === "memory" ? "hidden" : r.paletteId}`,
      }) as SVGPathElement;
      this.elements.set(r.id, node);
      regions.append(node);
    }
    this.svg.append(regions);
    const fixed = svgNode("g", { "pointer-events": "none", stroke: g.stroke, "stroke-width": 1.3, "stroke-linejoin": "round", "fill-rule": "evenodd" });
    for (const r of this.detailed ? [] : g.decorations)
      fixed.append(svgNode("path", { d: r.d, fill: `url(#${this.prefix}paint-${r.paletteId})` }));
    this.svg.append(fixed);
    const details = svgNode("g", { "pointer-events": "none", fill: "none", "stroke-linejoin": "round", "stroke-linecap": "round" });
    for (const d of g.detailPaths)
      details.append(svgNode("path", { d: d.d, stroke: d.stroke, "stroke-width": d.strokeWidth, opacity: d.opacity }));
    this.svg.append(details);
    if (this.detailed && this.bundle.paint) {
      const ink = svgNode("g", { "data-layer": "ink", "pointer-events": "none" }) as SVGGElement;
      for (const p of this.bundle.paint.inkPaths) {
        if (p.strokeWidth != null || p.filled === false) {
          const inkAttrs: Attrs = {
            d: p.d, fill: "none", stroke: p.fill,
            "stroke-width": p.strokeWidth ?? 1.5,
            "stroke-linecap": "round", "stroke-linejoin": "round",
          };
          if (p.shapeId) inkAttrs["data-shape-id"] = p.shapeId;
          ink.append(svgNode("path", inkAttrs));
        } else {
          const attrs: Attrs = { d: p.d, fill: p.fill, "fill-rule": p.fillRule ?? "evenodd" };
          if (p.shapeId) attrs["data-shape-id"] = p.shapeId;
          if (p.opacity != null && p.opacity < 0.999) attrs.opacity = p.opacity;
          if (p.fillOpacity != null && p.fillOpacity < 0.999) attrs["fill-opacity"] = p.fillOpacity;
          ink.append(svgNode("path", attrs));
        }
      }
      this.inkLayer = ink;
      this.svg.append(ink);
    }
    // Edges overlay (contract §1): ABOVE the ink layer, BELOW the labels.
    // Artwork boundaries render solid; subdivision boundaries render light
    // and dashed. boundaryStyle (if present) overrides the defaults.
    if (this.edgesMode && g.edges) {
      const style = g.boundaryStyle;
      const defaults: Record<EdgeEntry["kind"], { stroke: string; strokeWidth: number; dash?: string }> = {
        artwork: { stroke: g.stroke || "#22333B", strokeWidth: 1.6 },
        subdivision: { stroke: "#7A8C94", strokeWidth: 0.85, dash: "3 2.2" },
      };
      const edges = svgNode("g", {
        "data-layer": "edges",
        "pointer-events": "none",
        fill: "none",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      });
      for (const e of g.edges) {
        const base = defaults[e.kind];
        const custom = style?.[e.kind];
        const attrs: Attrs = {
          id: this.prefix + e.id,
          "data-edge-kind": e.kind,
          d: e.d,
          stroke: custom?.stroke ?? base.stroke,
          "stroke-width": custom?.strokeWidth ?? base.strokeWidth,
        };
        const dash = custom?.dash ?? base.dash;
        if (dash) attrs["stroke-dasharray"] = dash;
        edges.append(svgNode("path", attrs));
      }
      this.svg.append(edges);
    }
    const labels = svgNode("g", {
      "pointer-events": "none",
      "font-family": "Arial,sans-serif",
      fill: "#33444C",
      "text-anchor": "middle",
      "dominant-baseline": "central",
    });
    for (const r of g.regions) {
      const label = svgNode("text", {
        "data-label-for": r.id,
        x: r.label.x,
        y: r.label.y,
        "font-size": r.label.fontSize,
      }) as SVGTextElement;
      label.textContent = String(r.paletteId);
      labels.append(label);
      this.labels.set(r.id, label);
    }
    this.svg.append(labels);
    this.listen(this.svg, "keydown", (event: Event) => {
      const e = event as KeyboardEvent;
      const id = (e.target as Element | null)?.getAttribute?.("data-region-id");
      if (id && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        this.paint(id);
      }
    });
  }

  private listen(el: EventTarget, type: string, fn: EventListener, options?: AddEventListenerOptions) {
    el.addEventListener(type, fn, options);
    this.handlers.push(() => el.removeEventListener(type, fn, options));
  }

  state(): BoardState {
    const completedRegionIds = [...this.completed];
    const groups = this.bundle.manifest.objectGroups ?? [];
    const objects: Record<string, { completed: number; total: number }> = {};
    for (const group of groups)
      objects[group.id] = {
        completed: group.regionIds.filter((id) => this.completed.has(id)).length,
        total: group.regionIds.length,
      };
    return {
      artworkId: this.session.artworkId,
      artworkVersion: this.session.artworkVersion,
      mode: this.session.mode,
      completedRegionIds,
      selectedPaletteId: this.session.selectedPaletteId,
      customColor: this.customColor,
      mistakes: this.session.mistakes,
      total: this.regions.size,
      completed: completedRegionIds.length,
      progress: this.regions.size ? completedRegionIds.length / this.regions.size : 0,
      objects,
      preview: this.preview,
      zoom: this.base[2] / this.view[2],
    };
  }

  /** In-memory only — authoring play-tests never write player progress. */
  private save() {
    this.session.completedRegionIds = [...this.completed];
    this.session.updatedAt = new Date().toISOString();
  }

  private notify(reason: ChangeReason) {
    this.options.onChange?.(this.state(), reason);
  }

  refresh() {
    for (const [id, r] of this.regions) {
      const done = this.completed.has(id);
      const node = this.elements.get(id);
      const label = this.labels.get(id);
      if (!node) continue;
      let fill = "#FFFFFF";
      if (this.preview || done) {
        if (!this.preview && this.mode === "free") {
          // Free mode: flat custom hex — never a gradient url.
          fill = this.session.freeColors[id] ?? "#FFFFFF";
        } else {
          fill = `url(#${this.prefix}paint-${r.paletteId})`;
        }
      } else if (this.mode === "number" && r.paletteId === this.session.selectedPaletteId) {
        fill = `url(#${this.prefix}selected)`;
      }
      if (this.detailed && (this.preview || (done && this.mode !== "free"))) fill = "none";
      node.setAttribute("fill", fill);
      // Edges mode: region paths stay fill-only in EVERY state — the overlay
      // always draws the boundaries (that is the point of true vs artificial
      // boundaries: completed regions keep visible outlines too).
      node.setAttribute(
        "stroke",
        this.edgesMode || (this.detailed && (done || this.preview)) ? "none" : this.bundle.geometry.stroke
      );
      node.setAttribute("data-completed", String(done));
      node.setAttribute("tabindex", done && this.mode !== "free" ? "-1" : "0");
      node.setAttribute("aria-pressed", String(done));
      // Task 32 R2: hidden objects keep their regions/labels hidden across
      // completion refreshes, palette changes and preview switches.
      if (this.visibility.isRegionHidden(id)) {
        if (label) label.style.display = "none";
        node.setAttribute("tabindex", "-1");
        continue;
      }
      if (label) label.style.display = done || this.preview || this.mode !== "number" ? "none" : "";
    }
    this.updateLabelVisibility();
    this.notify("render");
  }

  updateLabelVisibility(rect?: DOMRect) {
    if (!this.detailed || this.labels.size === 0) return;
    // An explicit rect override lets gesture end re-measure against the
    // UNTRANSFORMED box (the GPU transform is cleared right after applyView,
    // but getBoundingClientRect inside applyView would already see it).
    const screen = rect ?? this.svg.getBoundingClientRect();
    if (!screen.width || !screen.height) return;
    const scale = Math.min(screen.width / this.view[2], screen.height / this.view[3]);
    for (const [id, r] of this.regions) {
      const label = this.labels.get(id);
      if (!label) continue;
      const visible =
        !this.completed.has(id) &&
        !this.preview &&
        this.mode === "number" &&
        !this.visibility.isRegionHidden(id) &&
        r.label.fontSize * scale >= 9;
      label.style.display = visible ? "" : "none";
    }
  }

  setPalette(id: number) {
    if (!this.bundle.palette.some((p) => p.id === id)) throw new Error("Unknown palette ID");
    this.session.selectedPaletteId = id;
    if (this.mode === "free") {
      // Quick access: in free mode a palette swatch loads that entry's hex
      // as the active custom color (the palette itself is unchanged).
      const hex = this.paletteHex(id);
      if (hex) this.customColor = hex.toUpperCase();
    }
    this.save();
    this.refresh();
  }

  /** Set the active free-mode color. Validated #RRGGBB — throws otherwise. */
  setFreeColor(hex: string) {
    if (typeof hex !== "string" || !SAFE_HEX.test(hex)) throw new Error("Free color must be a #RRGGBB hex value");
    this.customColor = hex.toUpperCase();
    this.refresh();
  }

  setMode(mode: BoardMode) {
    if (!["number", "memory", "free"].includes(mode)) throw new Error("Invalid coloring mode");
    this.mode = mode;
    this.session.mode = mode;
    this.save();
    this.refresh();
  }

  setPreview(value: boolean) {
    this.preview = !!value;
    this.refresh();
  }

  undo(): boolean {
    if (this.preview) return false;
    const previous = this.history.pop();
    if (!previous) return false;
    if (!previous.wasCompleted) this.completed.delete(previous.id);
    if (previous.oldColor === undefined) delete this.session.freeColors[previous.id];
    else this.session.freeColors[previous.id] = previous.oldColor;
    this.save();
    this.refresh();
    return true;
  }

  reset() {
    this.completed.clear();
    this.session.freeColors = {};
    this.session.mistakes = 0;
    this.history = [];
    this.preview = false;
    this.save();
    this.refresh();
    this.fit();
  }

  private hitTest(x: number, y: number): string | null {
    // Iterate in reverse document order: the topmost region wins. Regions
    // are visible surfaces (non-overlapping masks), so this only matters at
    // shared boundaries; per-region fill rules are honoured. Task 32 R2:
    // regions of hidden/isolated-away objects are not reachable by pointer.
    for (const [id, r] of [...this.regions].reverse()) {
      if (this.visibility.isRegionHidden(id)) continue;
      const b = r.bbox;
      const rule = r.fillRule ?? "evenodd";
      if (x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3] && this.ctx!.isPointInPath(this.paths.get(id)!, x, y, rule))
        return id;
    }
    return null;
  }

  /** Public region lookup in artwork coordinates — the Cut tool uses this
   *  to find the region under the stroke midpoint. */
  hitRegion(x: number, y: number): string | null {
    return this.hitTest(x, y);
  }

  private paletteHex(id: number): string | null {
    return this.bundle.palette.find((p) => p.id === id)?.hex ?? null;
  }

  private applyView(view: number[]) {
    const [bx, by, bw, bh] = this.base;
    const w = clamp(view[2], bw / 10, bw);
    const h = (w * bh) / bw;
    this.view = [clamp(view[0], bx, bx + bw - w), clamp(view[1], by, by + bh - h), w, h];
    this.svg.setAttribute("viewBox", this.view.join(" "));
    this.updateLabelVisibility();
    this.notify("viewport");
  }

  fit() {
    this.applyView([...this.base]);
  }

  zoom(factor: number, anchor?: { x: number; y: number } | null) {
    const v = this.view;
    const a = anchor ?? { x: v[0] + v[2] / 2, y: v[1] + v[3] / 2 };
    const w = clamp(v[2] / factor, this.base[2] / 10, this.base[2]);
    const h = (w * this.base[3]) / this.base[2];
    this.applyView([a.x - ((a.x - v[0]) * w) / v[2], a.y - ((a.y - v[1]) * h) / v[3], w, h]);
  }

  nextRegion(): string | null {
    if (this.mode !== "number" || this.preview) return null;
    const r = [...this.regions.values()].find(
      (region) => !this.completed.has(region.id) && region.paletteId === this.session.selectedPaletteId
    );
    if (!r) return null;
    const w = Math.max(
      this.base[2] / 10,
      Math.min(this.base[2], Math.max(r.bbox[2] - r.bbox[0], (r.bbox[3] - r.bbox[1]) * (this.base[2] / this.base[3])) * 1.6)
    );
    this.applyView([r.label.x - w / 2, r.label.y - (w * this.base[3]) / this.base[2] / 2, w, (w * this.base[3]) / this.base[2]]);
    this.elements.get(r.id)?.focus({ preventScroll: true });
    return r.id;
  }

  /**
   * Center the viewport on a specific region and focus its path element —
   * used by the QA panel drill-down ("select region & zoom to it").
   * Unlike nextRegion() this ignores mode/preview and completion state.
   */
  focusRegion(id: string): string | null {
    const r = this.regions.get(id);
    if (!r) return null;
    const w = Math.max(
      this.base[2] / 10,
      Math.min(
        this.base[2],
        Math.max(r.bbox[2] - r.bbox[0], (r.bbox[3] - r.bbox[1]) * (this.base[2] / this.base[3])) * 1.8
      )
    );
    this.applyView([
      r.label.x - w / 2,
      r.label.y - (w * this.base[3]) / this.base[2] / 2,
      w,
      (w * this.base[3]) / this.base[2],
    ]);
    this.elements.get(id)?.focus({ preventScroll: true });
    return id;
  }

  /** Task 32 R2 — highlight all regions and visual shapes of one object. */
  setHighlightedObject(objectId: string | null) {
    this.visibility.setHighlightedObject(objectId);
    this.applyObjectHighlight();
  }

  /** Task 32 R2 — transient hide/isolate backed by ONE authoritative state:
   *  artwork (cached underpainting is REGENERATED with the hidden shapes
   *  removed), region overlays, labels, hit-testing and keyboard focus all
   *  follow it, and it survives zoom/resize/palette/completion refreshes. */
  setHiddenObjects(hiddenIds: Set<string>, isolatedId: string | null) {
    this.visibility.setHiddenObjects(hiddenIds, isolatedId);
    this.applyObjectVisibility();
    this.applyObjectHighlight();
  }

  /** Push the controller's visibility state into the DOM + cached artwork. */
  private applyObjectVisibility() {
    for (const [id, el] of this.elements) {
      const hidden = this.visibility.isRegionHidden(id);
      el.classList.toggle("object-hidden", hidden);
      if (hidden) el.setAttribute("tabindex", "-1");
    }
    // Live-path fallback (before the underpainting image loads or when it
    // failed): classes hide the individual shape nodes.
    const shapeNodes = this.svg.querySelectorAll<SVGPathElement>("[data-shape-id]");
    shapeNodes.forEach((node) => {
      const hidden = this.visibility.isShapeHidden(node.getAttribute("data-shape-id") ?? undefined);
      node.classList.toggle("object-hidden", hidden);
    });
    // Labels: hidden regions lose theirs; visible ones get ONE authoritative
    // pass (never a per-region reset inside the loop — that was the bug where
    // a later visible region switched earlier hidden labels back on).
    for (const [id, label] of this.labels) {
      if (this.visibility.isRegionHidden(id)) label.style.display = "none";
    }
    this.updateLabelVisibility();
    // Cached appearance: regenerate so hidden shapes disappear from the
    // underpainting <image> too (replacing paths with an image makes
    // per-shape classes moot in Colored/Inspect view).
    this.buildUnderpainting();
  }

  private applyObjectHighlight() {
    const objectId = this.visibility.highlight();
    const owned = objectId ? this.visibility.ownedShapeIds(objectId) : new Set<string>();
    for (const [id, el] of this.elements) {
      const reg = this.regions.get(id);
      el.classList.toggle("object-highlight-region", !!objectId && reg?.objectId === objectId);
    }
    const shapeNodes = this.svg.querySelectorAll<SVGPathElement>("[data-shape-id]");
    shapeNodes.forEach((node) => {
      const sid = node.getAttribute("data-shape-id");
      node.classList.toggle("object-highlight-shape", !!sid && owned.has(sid));
    });
  }

  private bindGestures() {
    // High-performance gestures (contract §8):
    //  - pointermove/wheel store the LATEST event and ONE requestAnimationFrame
    //    per frame runs the handler (rAF batching);
    //  - during an active drag/pinch the SVG viewBox is NOT touched — instead a
    //    GPU CSS transform (translate+scale, transform-origin at the gesture
    //    anchor in client px) moves the whole board. The transform is computed
    //    from the same view-math the classic per-move path used (relative to
    //    the gesture-start view), so the visuals are identical while the
    //    browser only composites one layer;
    //  - on gesture END the final view is applied with applyView() FIRST and
    //    the CSS transform is cleared AFTER (order matters: applyView clamps
    //    exactly like the classic path and leaves the CTM correct for tap
    //    detection / drawing tools);
    //  - updateLabelVisibility is skipped while a gesture is active (no
    //    applyView calls) and runs once at gesture end.
    let queuedMove: PointerEvent | null = null;
    let queuedWheel: WheelEvent | null = null;
    const runFrame = () => {
      this.rafId = 0;
      const move = queuedMove;
      const wheel = queuedWheel;
      queuedMove = null;
      queuedWheel = null;
      if (move) this.processMove(move);
      if (wheel) this.processWheel(wheel);
    };
    const scheduleFrame = () => {
      if (!this.rafId) this.rafId = requestAnimationFrame(runFrame);
    };
    this.flushGestureFrame = () => {
      if (this.rafId) {
        cancelAnimationFrame(this.rafId);
        runFrame();
      }
    };
    const startPinch = () => {
      const pts = [...this.pointers.values()];
      if (pts.length < 2) return;
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      const inverse = this.svg.getScreenCTM()!.inverse();
      this.gestureRect = this.svg.getBoundingClientRect();
      this.pinch = {
        distance: Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)),
        view: [...this.view],
        inverse,
        anchor: this.clientToArt(mid.x, mid.y, inverse),
        mid0: mid,
      };
      this.suppressTap = true;
    };
    this.listen(this.svg, "pointerdown", (event) => {
      const e = event as PointerEvent;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      // Synthetic/test events and stale pointers can make capture fail; a throw
      // here would skip drag setup and break the whole tap flow, so guard it.
      try {
        this.svg.setPointerCapture(e.pointerId);
      } catch {
        /* no active pointer with this id — safe to continue without capture */
      }
      if (this.pointers.size === 1) {
        this.suppressTap = false;
        this.gestureRect = this.svg.getBoundingClientRect();
        const inverse = this.svg.getScreenCTM()!.inverse();
        this.drag = {
          x: e.clientX,
          y: e.clientY,
          view: [...this.view],
          inverse,
          anchor: this.clientToArt(e.clientX, e.clientY, inverse),
          moved: false,
        };
      } else if (this.pointers.size === 2) startPinch();
    });
    this.listen(this.svg, "pointermove", (event) => {
      const e = event as PointerEvent;
      if (!this.pointers.has(e.pointerId)) return;
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      queuedMove = e;
      scheduleFrame();
    });
    const finish = (event: Event, cancel = false) => {
      const e = event as PointerEvent;
      if (!this.pointers.has(e.pointerId)) return;
      // Flush any pending gesture frame so tap detection sees the latest
      // movement (a move that arrived just before pointerup must count).
      this.flushGestureFrame?.();
      const tap = !cancel && this.pointers.size === 1 && !this.suppressTap && !this.drag?.moved;
      this.pointers.delete(e.pointerId);
      if (this.svg.hasPointerCapture(e.pointerId)) this.svg.releasePointerCapture(e.pointerId);
      if (tap) {
        const p = this.clientToArt(e.clientX, e.clientY);
        if (p) this.paint(this.hitTest(p.x, p.y));
      }
      if (!this.pointers.size) this.endGesture();
      else this.suppressTap = true;
    };
    this.listen(this.svg, "pointerup", (e) => finish(e));
    this.listen(this.svg, "pointercancel", (e) => finish(e, true));
    this.listen(
      this.svg,
      "wheel",
      (event) => {
        const e = event as WheelEvent;
        // preventDefault must stay synchronous (rAF is too late to cancel the
        // scroll); the zoom computation itself is rAF-batched.
        e.preventDefault();
        queuedWheel = e;
        scheduleFrame();
      },
      { passive: false }
    );
  }

  /** Frame handler for a batched pointermove (runs inside one rAF). */
  private processMove(e: PointerEvent) {
    if (this.pointers.size >= 2 && this.pinch) {
      const pts = [...this.pointers.values()];
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      const distance = Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y));
      const p = this.pinch;
      const now = this.clientToArt(mid.x, mid.y, p.inverse);
      const w = clamp((p.view[2] * p.distance) / distance, this.base[2] / 10, this.base[2]);
      const h = (w * this.base[3]) / this.base[2];
      if (now && p.anchor) {
        // Target view exactly as the classic math computed it; the viewBox is
        // only updated at gesture end (see endGesture).
        this.pendingView = [
          p.anchor.x - ((now.x - p.view[0]) * w) / p.view[2],
          p.anchor.y - ((now.y - p.view[1]) * h) / p.view[3],
          w,
          h,
        ];
        this.setGestureTransform(mid, p.view[2] / w, p.mid0);
      }
    } else if (!this.suppressTap && this.drag) {
      const d = this.drag;
      if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 6) d.moved = true;
      if (d.moved) {
        const now = this.clientToArt(e.clientX, e.clientY, d.inverse);
        if (now && d.anchor) {
          this.pendingView = [d.view[0] - (now.x - d.anchor.x), d.view[1] - (now.y - d.anchor.y), d.view[2], d.view[3]];
          // k=1: a pan is a pure translation of (current - start) client px.
          this.setGestureTransform({ x: e.clientX, y: e.clientY }, 1, { x: d.x, y: d.y });
        }
      }
    }
  }

  /** Frame handler for a batched wheel event (zoom applies the view directly). */
  private processWheel(e: WheelEvent) {
    this.zoom(Math.exp(-e.deltaY * 0.0015), this.clientToArt(e.clientX, e.clientY));
  }

  /** GPU transform equivalent of the pending view: the art anchor at the
   *  gesture start follows the current pointer, everything else scales by k
   *  about the start point (derived from the same math as applyView, so the
   *  composition at gesture end is pixel-identical to the classic path). */
  private setGestureTransform(current: { x: number; y: number }, scale: number, origin: { x: number; y: number }) {
    // Cache the rect: once a transform is active, getBoundingClientRect()
    // returns the TRANSFORMED box and would corrupt the origin math.
    const rect = this.gestureRect ?? this.svg.getBoundingClientRect();
    const style = this.svg.style;
    style.transformBox = "border-box";
    style.transformOrigin = `${origin.x - rect.left}px ${origin.y - rect.top}px`;
    style.transform = `translate(${current.x - origin.x}px, ${current.y - origin.y}px) scale(${scale})`;
  }

  /** Gesture end: apply the final view (clamped), then clear the transform —
   *  in THAT order, so the CTM is correct before anything else reads it. */
  private endGesture() {
    const pending = this.pendingView;
    const rect = this.gestureRect;
    this.drag = null;
    this.pinch = null;
    this.suppressTap = false;
    this.pendingView = null;
    this.gestureRect = null;
    if (pending) this.applyView(pending);
    this.svg.style.transform = "";
    this.svg.style.transformOrigin = "";
    // Re-run the label pass against the untransformed box (updateLabelVisibility
    // was skipped for the whole gesture; applyView measured a transformed rect).
    if (rect) this.updateLabelVisibility(rect);
  }

  // -------------------------------------------------------------------------
  // Cached underpainting (contract §8)
  // -------------------------------------------------------------------------

  /**
   * Serialize the paint + ink appearance ONCE into standalone SVG documents
   * (base viewBox, cloned gradient defs only) and swap each appearance group
   * for a single <image> element once the blob decodes. Rationale: one image
   * node replaces 200k+ live path commands (the 552-region treehouse carries
   * ~250k); the browser re-rasterizes a single image cheaply and stays crisp
   * at any zoom, where a fixed bitmap would blur.
   *
   * TWO images are cached — one per z-slot the appearance occupies (the art
   * layer BELOW the region masks, the ink layer ABOVE them) — so the classic
   * layering (in particular the coloring-book linework above white masks) is
   * preserved exactly. Masks, labels, edges overlay and the hatch pattern
   * stay live SVG because they are interactive/ARIA-relevant.
   *
   * If serialization or decoding fails, the live path layers simply remain
   * mounted — the fallback is automatic and silent.
   */
  private buildUnderpainting() {
    if (!this.detailed || !this.bundle.paint) return;
    const paint = this.bundle.paint;
    const defs = serializeGradientDefs(paint.gradients ?? []);
    const [bx, by, bw, bh] = this.base;
    // Task 32 R2: hidden/isolated-away shapes are EXCLUDED from the cached
    // appearance — replacing live paths with an <image> makes per-shape
    // classes moot, so visibility must be baked into the cached SVG.
    const shapeVisible = (p: PaintPath) => !this.visibility.isShapeHidden(p.shapeId);
    // Art layer (below the masks): closed fills, z-sorted — mirrors mount().
    const artEntries = [...paint.paths, ...paint.inkPaths]
      .filter(shapeVisible)
      .sort((a, b) => (a.z ?? Infinity) - (b.z ?? Infinity));
    const artBody: string[] = [];
    for (const p of artEntries) {
      const serialized = serializeArtPath(p);
      if (serialized) artBody.push(serialized);
    }
    // Ink layer (above the masks): open line art + closed ink shapes.
    const inkBody = paint.inkPaths.filter(shapeVisible).map((p) => serializeInkPath(p));
    if (this.artLayer && artBody.length)
      this.mountUnderpaintImage(this.artLayer, wrapStandaloneSvg(defs, artBody, bx, by, bw, bh), "underpaint-art");
    if (this.inkLayer && inkBody.length)
      this.mountUnderpaintImage(this.inkLayer, wrapStandaloneSvg(defs, inkBody, bx, by, bw, bh), "underpaint-ink");
  }

  /** Load the serialized SVG via a blob URL, verify it decodes with an
   *  Image probe, and only then swap the live group children for the image. */
  private mountUnderpaintImage(group: SVGGElement, svgString: string, id: string) {
    let url: string;
    try {
      url = URL.createObjectURL(new Blob([svgString], { type: "image/svg+xml" }));
    } catch {
      return; // silent fallback: live paths stay mounted
    }
    // A visibility change regenerates the cached appearance — revoke the
    // previous layer URL so repeated toggles do not leak blobs.
    const previous = this.underpaintUrls.get(id);
    if (previous) {
      this.blobUrls = this.blobUrls.filter((u) => u !== previous);
      URL.revokeObjectURL(previous);
    }
    this.underpaintUrls.set(id, url);
    this.blobUrls.push(url);
    const probe = new Image();
    probe.onload = () => {
      if (this.destroyed) return;
      const [bx, by, bw, bh] = this.base;
      const image = svgNode("image", {
        id: this.prefix + id,
        href: url,
        x: bx,
        y: by,
        width: bw,
        height: bh,
        preserveAspectRatio: "xMidYMid meet",
        "pointer-events": "none",
      });
      // xlink:href for engines that predate SVG2 href on <image>.
      image.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", url);
      group.replaceChildren(image);
    };
    probe.onerror = () => {
      // Silent fallback: keep the live path layer, release the blob.
      this.blobUrls = this.blobUrls.filter((u) => u !== url);
      URL.revokeObjectURL(url);
    };
    probe.src = url;
  }

  destroy() {
    this.destroyed = true;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
    this.flushGestureFrame = null;
    for (const remove of this.handlers) remove();
    this.handlers = [];
    this.svg.replaceChildren();
    this.svg.style.transform = "";
    this.svg.style.transformOrigin = "";
    this.svg.style.transformBox = "";
    // Release the underpainting blob URLs (also covers re-measure/re-mount).
    for (const url of this.blobUrls) URL.revokeObjectURL(url);
    this.blobUrls = [];
    this.underpaintUrls.clear();
    this.pointers.clear();
  }
}
