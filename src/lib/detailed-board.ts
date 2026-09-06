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
 */

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
  fillRule: "evenodd";
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

export interface Geometry {
  schemaVersion: number;
  artworkId: string;
  artworkVersion: string;
  viewBox: number[];
  fillRule: "evenodd";
  stroke: string;
  strokeWidth: number;
  regions: RegionEntry[];
  decorations: RegionEntry[];
  detailPaths: DetailPath[];
}

export interface PaintPath {
  fill: string; // #RRGGBB or url(#gradient-id)
  d: string;
  strokeWidth?: number; // present on stroked ink paths (open line art)
  filled?: boolean; // false = stroke-rendered ink, not a closed fill
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
}

export interface Bundle {
  manifest: Manifest;
  geometry: Geometry;
  palette: PaletteEntry[];
  paint: PaintLayer | null;
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
    if (!paletteIds.has(r.paletteId) || r.fillRule !== "evenodd" || !SAFE_PATH.test(r.d))
      throw new Error("Invalid region geometry or palette");
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

/** Fetch + validate the four bundle files for a revision, through the gateway. */
export async function loadBundle(pid: string, rev: string): Promise<Bundle> {
  const base = `/api/projects/${pid}/revisions/${rev}/files/`;
  const q = "?XTransformPort=8765";
  const [manifest, geometry, palette, paint] = await Promise.all([
    fetchJson<Manifest>(base + "artwork.json" + q),
    fetchJson<Geometry>(base + "regions.json" + q),
    fetchJson<PaletteEntry[]>(base + "palette.json" + q),
    fetchJson<PaintLayer | null>(base + "paint.json" + q),
  ]);
  return validateBundle({ manifest, geometry, palette, paint });
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
  freeColors: Record<string, number | undefined>;
  mistakes: number;
  updatedAt: string;
}

export interface BoardOptions {
  mode?: BoardMode;
  persist?: boolean;
  onChange?: (state: BoardState, reason: ChangeReason) => void;
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
  private handlers: Array<() => void> = [];
  private pointers = new Map<number, { x: number; y: number }>();
  private history: Array<{ id: string; wasCompleted: boolean; oldColor?: number }> = [];
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
  private drag: { x: number; y: number; view: number[]; inverse: DOMMatrix; anchor: DOMPoint; moved: boolean } | null = null;
  private pinch: { distance: number; view: number[]; inverse: DOMMatrix; anchor: DOMPoint | null } | null = null;
  private suppressTap = false;
  /** Set by the wrapper (clientToArt override) — last art-space point of a tap. */
  lastTapPoint: DOMPoint | null = null;

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
    this.mount();
    this.bindGestures();
    this.refresh();
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
    if (this.completed.has(id!) && this.session.freeColors[id!] === this.session.selectedPaletteId)
      return "already-complete";
    this.history.push({ id: id!, wasCompleted: this.completed.has(id!), oldColor: this.session.freeColors[id!] });
    this.completed.add(id!);
    if (this.mode === "free") this.session.freeColors[id!] = this.session.selectedPaletteId;
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
      const art = svgNode("g", { "data-layer": "vector-paint", "pointer-events": "none" });
      for (const p of this.bundle.paint.paths) {
        const gradientFill = p.fill.startsWith("url(#");
        art.append(
          svgNode("path", {
            d: p.d,
            fill: p.fill,
            ...(gradientFill ? {} : { stroke: p.fill, "stroke-width": 0.55, "stroke-linejoin": "round" }),
            "fill-rule": "evenodd",
          })
        );
      }
      this.svg.append(art);
    }
    const regions = svgNode("g", {
      stroke: g.stroke,
      "stroke-width": g.strokeWidth,
      "stroke-linejoin": "round",
      "fill-rule": "evenodd",
    });
    this.elements.clear();
    this.labels.clear();
    for (const r of g.regions) {
      const node = svgNode("path", {
        id: this.prefix + r.id,
        "data-region-id": r.id,
        "data-palette-id": r.paletteId,
        d: r.d,
        tabindex: 0,
        role: "button",
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
      const ink = svgNode("g", { "data-layer": "ink", "pointer-events": "none" });
      for (const p of this.bundle.paint.inkPaths) {
        if (p.strokeWidth != null || p.filled === false) {
          ink.append(svgNode("path", {
            d: p.d, fill: "none", stroke: p.fill,
            "stroke-width": p.strokeWidth ?? 1.5,
            "stroke-linecap": "round", "stroke-linejoin": "round",
          }));
        } else {
          ink.append(svgNode("path", { d: p.d, fill: p.fill, "fill-rule": "evenodd" }));
        }
      }
      this.svg.append(ink);
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
    this.listen(this.svg, "keydown", (e: KeyboardEvent) => {
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
        const palette = !this.preview && this.mode === "free" ? this.session.freeColors[id] : r.paletteId;
        fill = palette == null ? "#FFFFFF" : `url(#${this.prefix}paint-${palette})`;
      } else if (this.mode === "number" && r.paletteId === this.session.selectedPaletteId) {
        fill = `url(#${this.prefix}selected)`;
      }
      if (this.detailed && (this.preview || (done && this.mode !== "free"))) fill = "none";
      node.setAttribute("fill", fill);
      node.setAttribute("stroke", this.detailed && (done || this.preview) ? "none" : this.bundle.geometry.stroke);
      node.setAttribute("data-completed", String(done));
      node.setAttribute("tabindex", done && this.mode !== "free" ? "-1" : "0");
      node.setAttribute("aria-pressed", String(done));
      if (label) label.style.display = done || this.preview || this.mode !== "number" ? "none" : "";
    }
    this.updateLabelVisibility();
    this.notify("render");
  }

  updateLabelVisibility() {
    if (!this.detailed || this.labels.size === 0) return;
    const screen = this.svg.getBoundingClientRect();
    if (!screen.width || !screen.height) return;
    const scale = Math.min(screen.width / this.view[2], screen.height / this.view[3]);
    for (const [id, r] of this.regions) {
      const label = this.labels.get(id);
      if (!label) continue;
      const visible = !this.completed.has(id) && !this.preview && this.mode === "number" && r.label.fontSize * scale >= 9;
      label.style.display = visible ? "" : "none";
    }
  }

  setPalette(id: number) {
    if (!this.bundle.palette.some((p) => p.id === id)) throw new Error("Unknown palette ID");
    this.session.selectedPaletteId = id;
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
    // Iterate in reverse document order: the topmost region wins, which
    // matches how stacked SVG-master masks resolve overlaps (z-order).
    for (const [id, r] of [...this.regions].reverse()) {
      const b = r.bbox;
      if (x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3] && this.ctx!.isPointInPath(this.paths.get(id)!, x, y, "evenodd"))
        return id;
    }
    return null;
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

  private bindGestures() {
    const startPinch = () => {
      const pts = [...this.pointers.values()];
      if (pts.length < 2) return;
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      const inverse = this.svg.getScreenCTM()!.inverse();
      this.pinch = {
        distance: Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)),
        view: [...this.view],
        inverse,
        anchor: this.clientToArt(mid.x, mid.y, inverse),
      };
      this.suppressTap = true;
    };
    this.listen(this.svg, "pointerdown", (event) => {
      const e = event as PointerEvent;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      this.svg.setPointerCapture(e.pointerId);
      if (this.pointers.size === 1) {
        this.suppressTap = false;
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
      if (this.pointers.size >= 2 && this.pinch) {
        const pts = [...this.pointers.values()];
        const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
        const distance = Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y));
        const p = this.pinch;
        const now = this.clientToArt(mid.x, mid.y, p.inverse);
        const w = clamp((p.view[2] * p.distance) / distance, this.base[2] / 10, this.base[2]);
        const h = (w * this.base[3]) / this.base[2];
        if (now && p.anchor)
          this.applyView([
            p.anchor.x - ((now.x - p.view[0]) * w) / p.view[2],
            p.anchor.y - ((now.y - p.view[1]) * h) / p.view[3],
            w,
            h,
          ]);
      } else if (!this.suppressTap && this.drag) {
        const d = this.drag;
        if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 6) d.moved = true;
        if (d.moved) {
          const now = this.clientToArt(e.clientX, e.clientY, d.inverse);
          if (now && d.anchor)
            this.applyView([d.view[0] - (now.x - d.anchor.x), d.view[1] - (now.y - d.anchor.y), d.view[2], d.view[3]]);
        }
      }
    });
    const finish = (event: Event, cancel = false) => {
      const e = event as PointerEvent;
      if (!this.pointers.has(e.pointerId)) return;
      const tap = !cancel && this.pointers.size === 1 && !this.suppressTap && !this.drag?.moved;
      this.pointers.delete(e.pointerId);
      if (this.svg.hasPointerCapture(e.pointerId)) this.svg.releasePointerCapture(e.pointerId);
      if (tap) {
        const p = this.clientToArt(e.clientX, e.clientY);
        if (p) this.paint(this.hitTest(p.x, p.y));
      }
      if (!this.pointers.size) {
        this.drag = null;
        this.pinch = null;
        this.suppressTap = false;
      } else this.suppressTap = true;
    };
    this.listen(this.svg, "pointerup", (e) => finish(e));
    this.listen(this.svg, "pointercancel", (e) => finish(e, true));
    this.listen(
      this.svg,
      "wheel",
      (event) => {
        const e = event as WheelEvent;
        e.preventDefault();
        this.zoom(Math.exp(-e.deltaY * 0.0015), this.clientToArt(e.clientX, e.clientY));
      },
      { passive: false }
    );
  }

  destroy() {
    for (const remove of this.handlers) remove();
    this.handlers = [];
    this.svg.replaceChildren();
    this.pointers.clear();
  }
}
