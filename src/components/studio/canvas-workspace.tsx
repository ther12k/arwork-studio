"use client";

/** Center panel — artwork workspace: header, brief, canvas, palette, job status.
 *  Stage 2 adds: the inspect-view tool row (Select / Cut / Pen) with a drawing
 *  overlay for cut lines and pen shapes (AlertDialog / Popover confirmations),
 *  the play-test coloring-mode switch, the free-color cluster (custom hex +
 *  recents) and the compact difficulty mini-panel. Stage 3 adds the Node tool
 *  (drag boundary anchors, contract A) and play-test recording with the run
 *  clock + playtest difficulty rows (contract B). */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Gauge,
  Maximize2,
  MousePointer2,
  PenTool,
  Redo2,
  RotateCcw,
  Scissors,
  Search,
  Sparkles,
  Timer,
  Undo2,
  Waypoints,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  paintFillHex,
  type BoardMode,
  type EdgeEntry,
  type PaintLayer,
  type PaintPath,
  type VectorBoard,
} from "@/lib/detailed-board";
import { imageUrl, masterSvgUrl, type DifficultyProfile } from "@/lib/studio-api";
import {
  flattenPath,
  nearestOnPathCommands,
  parsePathCommands,
  polylineNearestDistance,
  removePathAnchor,
  serializePathCommands,
  splitPathCommand,
  type PathCommand,
} from "@/lib/svg-path";
import { VIEW_LABELS, type StudioTool, type StudioView, useStudioContext } from "./use-studio";
import { sessionResumeCopy } from "./create-artwork";
import {
  DIFFICULTY_TIERS,
  PlaytestValidatedBadge,
  difficultyMetricNumber,
  formatPlaytestClock,
  normalizeDifficulty,
} from "./difficulty";
import ZoomLab from "./zoom-lab";

const VIEW_ORDER: StudioView[] = ["master", "colored", "numbered", "play", "inspect", "zoomlab"];

const TOOL_HINTS: Record<StudioTool, string> = {
  select: "Tap regions to select",
  cut: "Drag a line across a region — outside one edge to outside the opposite edge",
  pen: "Draw a closed shape — the artwork pen paints it, the region pen is gameplay-only",
  node: "Tap a boundary between two regions, then drag its anchor points.",
  artnode:
    "Tap a shape to load it, drag anchors or handles — double-click an edge to add a node, double-click an anchor to remove it.",
};

const MODE_HINTS: Record<BoardMode, string> = {
  number: "Match palette numbers — wrong taps count as mistakes.",
  memory: "Numbers hidden — remember which group each region needs.",
  free: "Any color, any region — no mistakes, full freedom.",
};

const FREE_HEX_RE = /^#[0-9A-Fa-f]{6}$/;

function canvasTag(view: StudioView): string {
  if (view === "inspect") return "Select · merge · group · fix labels";
  if (view === "play") return "Play test · separate from game progress";
  if (view === "zoomlab") return "Same crop · curved vs legacy";
  return "Draft · review required";
}

/** Contrast-aware text color for a hex swatch. */
function swatchTextColor(hex: string): string {
  const rgb = hex.match(/\w\w/g)?.map((v) => parseInt(v, 16)) ?? [0, 0, 0];
  return rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114 > 150 ? "#152a2a" : "white";
}

// ---------------------------------------------------------------- stroke math

type ArtPt = { x: number; y: number };

/** Drop points closer than `minDist` art units to the previous kept point. */
function dropDensePoints(pts: ArtPt[], minDist: number): ArtPt[] {
  const out: ArtPt[] = [];
  for (const p of pts) {
    const last = out[out.length - 1];
    if (!last || Math.hypot(p.x - last.x, p.y - last.y) >= minDist) out.push(p);
  }
  return out;
}

function perpendicularDistance(p: ArtPt, a: ArtPt, b: ArtPt): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy);
  if (len < 1e-9) return Math.hypot(p.x - a.x, p.y - a.y);
  return Math.abs(dy * p.x - dx * p.y + b.x * a.y - b.y * a.x) / len;
}

/** Ramer–Douglas–Peucker simplification (art units). */
function rdp(pts: ArtPt[], epsilon: number): ArtPt[] {
  if (pts.length < 3) return pts;
  const a = pts[0];
  const b = pts[pts.length - 1];
  let maxDist = 0;
  let index = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const d = perpendicularDistance(pts[i], a, b);
    if (d > maxDist) {
      maxDist = d;
      index = i;
    }
  }
  if (maxDist > epsilon) {
    const left = rdp(pts.slice(0, index + 1), epsilon);
    const right = rdp(pts.slice(index), epsilon);
    return [...left.slice(0, -1), ...right];
  }
  return [a, b];
}

/** Shoelace area of a closed polygon (art units²). */
function polygonArea(pts: ArtPt[]): number {
  let sum = 0;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % pts.length];
    sum += a.x * b.y - b.x * a.y;
  }
  return Math.abs(sum) / 2;
}

/** Point at half the total arc length of the polyline (stroke midpoint). */
function polylineMidpoint(pts: ArtPt[]): ArtPt | null {
  if (!pts.length) return null;
  const segs: number[] = [];
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const d = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    segs.push(d);
    total += d;
  }
  if (!segs.length) return pts[0];
  let half = total / 2;
  for (let i = 0; i < segs.length; i++) {
    if (half <= segs[i]) {
      const t = segs[i] > 0 ? half / segs[i] : 0;
      return { x: pts[i].x + (pts[i + 1].x - pts[i].x) * t, y: pts[i].y + (pts[i + 1].y - pts[i].y) * t };
    }
    half -= segs[i];
  }
  return pts[pts.length - 1];
}

const fmt = (n: number) => (Math.round(n * 100) / 100).toString();

/** Build the edit-action path string: `M x,y L …` (+ ` Z` when closed). */
function pathFromPoints(pts: ArtPt[], close: boolean): string {
  return `M ${pts.map((p) => `${fmt(p.x)},${fmt(p.y)}`).join(" L ")}${close ? " Z" : ""}`;
}

/** Node tool (stage 3, contract A): the selected shared boundary. Anchors
 *  are stored in ART units; rendering converts them to client px so they
 *  stay a constant on-screen size at any zoom. */
interface NodeEdgeState {
  edgeId: string;
  left: string;
  right: string;
  kind: string;
  /** True for closed (Z) boundary rings — a full outline whose last vertex
   *  equals the first. The anchor list stores UNIQUE vertices; the render
   *  closes the ring visually and the submission re-appends Z. */
  closed: boolean;
  /** Original flattened boundary polyline (art units) — the ghost line. */
  base: ArtPt[];
  /** Draggable anchor positions (art units). */
  anchors: ArtPt[];
  /** Index of the anchor currently being dragged, null when idle. */
  dragging: number | null;
}

/** Art-space point → canvas-container-relative CSS position (px, clamped
 *  inside the canvas; "50%" center fallback). Anchors the Pen confirm
 *  popover at the drawn shape's centroid. */
function artPointToCanvas(board: VectorBoard, x: number, y: number): { left: number | string; top: number | string } {
  try {
    const ctm = board.svg.getScreenCTM();
    const host = board.svg.parentElement;
    if (!ctm || !host) return { left: "50%", top: "50%" };
    const screen = new DOMPoint(x, y).matrixTransform(ctm);
    const rect = host.getBoundingClientRect();
    if (!rect.width || !rect.height) return { left: "50%", top: "50%" };
    const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
    return { left: clamp(screen.x - rect.left, 48, rect.width - 48), top: clamp(screen.y - rect.top, 48, rect.height - 48) };
  } catch {
    return { left: "50%", top: "50%" };
  }
}

/** Compact difficulty summary (contract §5): 4-tier bar + score in one row.
 *  Stage 3 (contract B) adds the play-test metrics line + validated pill.
 *  Legacy "unrated" strings normalize to a muted note; the full metric table
 *  lives in the right panel (DifficultyPanel). */
function DifficultyMini({
  raw,
  validated,
}: {
  raw: string | DifficultyProfile | undefined;
  validated?: boolean;
}) {
  const d = normalizeDifficulty(raw);
  if (!d) {
    return (
      <p className="mt-2.5 flex items-center gap-1.5 text-[10px] text-[#778481]" role="status">
        <Gauge className="size-3.5 shrink-0" aria-hidden />
        Difficulty · <span className="italic">unrated</span>
        <span aria-hidden>·</span>
        rebuild to analyze
      </p>
    );
  }
  const metrics = d.profile.metrics ?? {};
  const ptCount = difficultyMetricNumber(metrics, "playtestCount");
  const ptMedian = difficultyMetricNumber(metrics, "playtestMedianSeconds");
  const ptPace = difficultyMetricNumber(metrics, "playtestSecondsPerRegion");
  const ptMistakes = difficultyMetricNumber(metrics, "playtestMistakesPerRegion");
  const freeCount = difficultyMetricNumber(metrics, "freePlayCount");
  const freeMedian = difficultyMetricNumber(metrics, "freeMedianSeconds");
  return (
    <div
      className="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-lg border border-[#e1e5df] bg-white/70 px-3 py-2"
      role="status"
      aria-label={`Difficulty ${d.tier.label}, score ${d.score} of 100`}
    >
      <span className="flex items-center gap-1.5 text-[10px] font-semibold text-[#657671]">
        <Gauge className="size-3.5" aria-hidden />
        Difficulty
      </span>
      <span className="flex h-2 w-24 gap-0.5 overflow-hidden rounded-full sm:w-28" aria-hidden>
        {DIFFICULTY_TIERS.map((t, i) => {
          const fill = Math.max(0, Math.min(1, (d.score - i * 25) / 25));
          return (
            <span key={t.key} className="relative h-full flex-1 rounded-[2px] bg-[#e1e5df]" title={`${t.label}: ${i * 25}–${t.max}`}>
              <span className="absolute inset-y-0 left-0 rounded-[2px]" style={{ width: `${fill * 100}%`, background: t.color }} />
            </span>
          );
        })}
      </span>
      <span className="text-[10px] font-bold" style={{ color: d.tier.color }}>
        {d.tier.label} · {d.score}/100
      </span>
      {ptCount != null && (
        <span className="text-[10px] text-[#657671]">
          {`Playtests ${ptCount.toLocaleString("en-US")}`}
          {ptMedian != null ? ` · median ${formatPlaytestClock(ptMedian)}` : ""}
          {ptPace != null ? ` · ${ptPace.toFixed(1)} s/region` : ""}
          {ptMistakes != null ? ` · ${ptMistakes.toFixed(2)} mistakes/region` : ""}
        </span>
      )}
      {freeCount != null && (
        <span className="text-[10px] text-[#778481]">
          {`Free-color plays ${freeCount.toLocaleString("en-US")} (engagement)`}
          {freeMedian != null ? ` · ${formatPlaytestClock(freeMedian)}` : ""}
        </span>
      )}
      {validated && <PlaytestValidatedBadge />}
    </div>
  );
}

// ------------------------------------------------------------------ component

export function CanvasWorkspace() {
  const studio = useStudioContext();
  const {
    project,
    view,
    bundle,
    boardState,
    busy,
    revision,
    connectionError,
    svgRef,
    canvasRef,
    zoomRef,
    boardRef,
    tool,
    setTool,
    cutRegion,
    drawRegion,
    nodeEdit,
    editShape,
    editShapeStyle,
    editShapeOrder,
    gatedOperation,
    confirmGatedOperation,
    dismissGatedOperation,
    syncProject,
    recordPlaytest,
    freeColor,
    setBoardFreeColor,
    recentColors,
    boardMode,
    setBoardMode,
    objectGroup,
    briefInput,
    setBriefInput,
    generationSource,
    setGenerationSource,
    setBoardPalette,
    setEditPalette,
    findRegion,
    undoFill,
    resetTest,
    saveBrief,
    generate,
    switchView,
    zoomIn,
    zoomOut,
    fit,
    activeSession,
    resumeCreationSession,
  } = studio;
  const aiConfigured = studio.config?.ai.configured ?? false;
  const hasMaster = !!project?.master;
  const hasBundle = !!bundle;

  const paletteEntries = bundle?.palette ?? [];
  const selectedPaletteId = boardState?.selectedPaletteId ?? null;
  const completed = boardState?.completed ?? 0;
  const total = boardState?.total ?? 0;
  const mistakes = boardState?.mistakes ?? 0;

  const job = project?.job ?? {};
  const jobMessage =
    job.message ?? "Local tools ready. No cloud request is made until you confirm.";
  const jobFailed = job.status === "failed";

  const setSwatch = (id: number) => {
    try {
      setBoardPalette(id);
      setEditPalette(String(id));
    } catch (e) {
      toast((e as Error).message);
    }
  };

  // ------------------------------------------------ drawing overlay (Cut / Pen)

  const strokeRef = useRef<ArtPt[]>([]);
  const [strokePts, setStrokePts] = useState<ArtPt[]>([]);
  const [strokeViewBox, setStrokeViewBox] = useState<string | null>(null);
  const [pendingCut, setPendingCut] = useState<{ regionId: string; d: string } | null>(null);
  const [pendingDraw, setPendingDraw] = useState<{
    d: string;
    area: number;
    /** Popover anchor (CSS left/top within the canvas container). */
    anchor: { left: number | string; top: number | string };
  } | null>(null);
  const [penPalette, setPenPalette] = useState("1");
  const [penGroup, setPenGroup] = useState("");
  /** Artwork pen (paint=true): the shape becomes finished artwork (paint.json
   *  path + masterShapeId). Region pen (paint=false): gameplay-only tap
   *  target. Default = artwork — Pen is primarily an artwork tool. */
  const [penMode, setPenMode] = useState<"artwork" | "region">("artwork");
  /** Fill of the new artwork path — defaults to the number group's swatch;
   *  a custom fill makes the shape join (or create) the palette group whose
   *  answer color IS that fill — the shared swatch is never mutated (P0.2). */
  const [penFill, setPenFill] = useState("");
  const [penOutline, setPenOutline] = useState(false);
  const [penStrokeWidth, setPenStrokeWidth] = useState("1.5");
  const [penBehind, setPenBehind] = useState(false);
  /** Hex of the currently chosen number group (fallback #808080). */
  const penGroupHex =
    paletteEntries.find((p) => String(p.id) === penPalette)?.hex ?? "#808080";
  const penFillHex = FREE_HEX_RE.test(penFill) ? penFill.toUpperCase() : penGroupHex;

  // ---------------------------------------------------- node tool (contract A)

  const [nodeEdge, setNodeEdge] = useState<NodeEdgeState | null>(null);
  const [nodeConfirmOpen, setNodeConfirmOpen] = useState(false);
  /** Tap bookmark for the node overlay (pointerdown position). */
  const nodeTapRef = useRef<{ x: number; y: number } | null>(null);
  /** The drawing overlay svg itself — node anchors render in its client-px
   *  coordinate space (viewBox undefined), so its rect defines the origin. */
  const overlaySvgRef = useRef<SVGSVGElement | null>(null);

  // --------------------------------------------- artwork node tool (Task 33)

  /** Draft identity (review follow-up: "request rejection recovery" is
   *  separate from "revision conflict recovery"). The base revision and the
   *  ORIGINAL path data anchor the draft: a draft whose base no longer
   *  matches the project is never silently rebased or dismissed — the save
   *  bar surfaces it and Save demands an explicit choice. */
  type ArtworkDraftContext = {
    projectId: string;
    baseRevision: string;
    shapeId: string;
    originalD: string;
  };

  /** An in-flight shape save. The job id lets the settle-watch attribute the
   *  published revision to THIS save; `submittedD` is what it must show. */
  type PendingArtworkSave = {
    jobId: string;
    submittedD: string;
    /** Base the job was submitted against (for conflict reporting). */
    baseRevision: string;
  };

  /** Snapshot of a draft geometry step for local undo/redo. */
  type ArtDraftSnapshot = {
    commands: PathCommand[];
    dirty: boolean;
  };

  type ArtDraftHistory = {
    projectId: string;
    baseRevision: string;
    shapeId: string;
    draftGeneration: number;
    past: ArtDraftSnapshot[];
    future: ArtDraftSnapshot[];
  };

  function cloneCommands(cmds: PathCommand[]): PathCommand[] {
    return cmds.map((c) => ({
      op: c.op,
      pts: c.pts.map((p) => ({ x: p.x, y: p.y })),
    }));
  }

  /** The artwork path being node-edited: its identity context, the parsed
   *  commands (M/L/C/Q/Z, absolute), which control point (command index +
   *  point index) is being dragged, and whether the path was authored OPEN
   *  (no trailing Z — stroke rules; closed paths, ink included, must keep
   *  their Z). Anchor = command endpoint, handle = a Bézier control point. */
  const [artPath, setArtPath] = useState<{
    context: ArtworkDraftContext;
    commands: PathCommand[];
    dragging: { cmd: number; pt: number } | null;
    dirty: boolean;
    open: boolean;
  } | null>(null);
  const [artSaveOpen, setArtSaveOpen] = useState(false);
  const [artConflictOpen, setArtConflictOpen] = useState(false);
  const artPendingSaveRef = useRef<PendingArtworkSave | null>(null);

  // ----- Task 40A/40B: shape appearance (shape-addressed) -----
  /** Identity of an appearance draft: which project/revision the style was
   *  loaded from (the P1 contract — the payload's base must be the revision
   *  the artist SAW, never whatever happens to be current at save time),
   *  which shape it styles, and the appearance baseline it was seeded with
   *  (a concurrent publish that changed the appearance demands a warning,
   *  an unchanged one makes "save against current" safe). Shared by the ink
   *  stroke editor (40A) and the filled Fill/Outline editor (40B). */
  type AppearanceValues = {
    fill: string; // filled: the fill (or gradient-stop average) — ink: the stroke color
    stroke: string; // filled: outline color ("" = no outline) — ink: unused
    strokeWidth: number; // filled: outline width (0 = none) — ink: stroke width
    opacity: number; // ink only this slice
  };
  type AppearanceDraftContext = {
    projectId: string;
    baseRevision: string;
    shapeId: string;
    original: AppearanceValues;
  };
  /** The CURRENT revision's appearance for the picked shape (from the live
   *  bundle) — what a fresh seed would capture, plus which editor applies. */
  const liveAppearance: { kind: "ink" | "filled"; ctx: AppearanceDraftContext } | null = (() => {
    if (!artPath || !bundle?.paint || !project?.currentRevision) return null;
    const sid = artPath.context.shapeId;
    const ink = bundle.paint.inkPaths.find((p) => p.shapeId === sid);
    if (ink) {
      return {
        kind: "ink",
        ctx: {
          projectId: project.id,
          baseRevision: project.currentRevision,
          shapeId: ink.shapeId!,
          original: { fill: ink.fill, stroke: "", strokeWidth: ink.strokeWidth ?? 1.5, opacity: ink.opacity ?? 1 },
        },
      };
    }
    const fp = bundle.paint.paths.find((p) => p.shapeId === sid);
    if (!fp) return null;
    return {
      kind: "filled",
      ctx: {
        projectId: project.id,
        baseRevision: project.currentRevision,
        shapeId: fp.shapeId!,
        original: {
          fill: paintFillHex(bundle.paint, fp),
          stroke: fp.stroke ?? "",
          strokeWidth: fp.strokeWidth ?? 0,
          opacity: 1,
        },
      },
    };
  })();
  /** The SEEDED draft context — frozen at pick time (per shape), so a
   *  concurrent publish does NOT move the draft's base out from under the
   *  artist. Null until the effect below seeds it. */
  const [styleContext, setStyleContext] = useState<AppearanceDraftContext | null>(null);
  const [styleKind, setStyleKind] = useState<"ink" | "filled" | null>(null);
  const [styleDraft, setStyleDraft] = useState<(AppearanceValues & { preserveShading: boolean }) | null>(null);
  const savedStyleJobRef = useRef<string | null>(null);
  useEffect(() => {
    // Seed ONCE per edited shape (keyed on the DRAFT's shape id — a stable
    // artPath property, NOT a bundle-derived value that blips null during a
    // revision remount): the context freezes the revision the artist saw.
    // Concurrent publishes must surface as a conflict, not as a silent
    // re-seed.
    if (!artPath || !liveAppearance) {
      setStyleContext(null);
      setStyleKind(null);
      setStyleDraft(null);
      return;
    }
    setStyleContext(liveAppearance.ctx);
    setStyleKind(liveAppearance.kind);
    setStyleDraft({ ...liveAppearance.ctx.original, preserveShading: true });
  }, [artPath?.context.shapeId]);
  // The artist's OWN style save rebases the draft: once ITS job publishes,
  // the artist's draft values ARE the published appearance — adopt them as
  // the new seed (base = the revision the artist created). Other jobs'
  // terminal outcomes never touch the draft (attribution by job id).
  useEffect(() => {
    const job = project?.job;
    if (
      !savedStyleJobRef.current ||
      !job ||
      job.id !== savedStyleJobRef.current ||
      job.status !== "done" ||
      busy ||
      !styleContext ||
      !styleDraft
    ) {
      return;
    }
    savedStyleJobRef.current = null;
    // Width 0 MEANS "no outline" (backend deletion semantics) — adopt that
    // normalized form as the new baseline so the saved draft and the live
    // revision agree.
    const saved = styleKind === "filled" && styleDraft.strokeWidth === 0
      ? { ...styleDraft, stroke: "" }
      : styleDraft;
    setStyleContext({
      projectId: styleContext.projectId,
      baseRevision: project.currentRevision ?? styleContext.baseRevision,
      shapeId: styleContext.shapeId,
      original: {
        fill: saved.fill,
        stroke: saved.stroke,
        strokeWidth: saved.strokeWidth,
        opacity: saved.opacity,
      },
    });
    setStyleDraft((d) => (d ? { ...d, stroke: saved.stroke } : d));
  }, [project?.job?.id, project?.job?.status, busy]);

  /** True when the revision the style draft was seeded from is no longer
   *  current (another operation published in between) — saving then needs
   *  the artist's explicit acknowledgment, exactly like the geometry draft. */
  const staleStyleBase =
    !!styleDraft &&
    !!styleContext &&
    styleContext.baseRevision !== project?.currentRevision &&
    styleContext.projectId === project?.id;
  /** True when the shape's appearance CHANGED in between (its current paint
   *  no longer matches the draft's seed) — saving would silently overwrite
   *  those unseen changes. */
  const styleChangedInBetween = (() => {
    if (!styleContext || !styleKind || !bundle?.paint) return false;
    const o = styleContext.original;
    if (styleKind === "ink") {
      const ink = bundle.paint.inkPaths.find((p) => p.shapeId === styleContext.shapeId);
      if (!ink) return false;
      return (
        ink.fill.toUpperCase() !== o.fill.toUpperCase() ||
        Math.abs((ink.strokeWidth ?? 1.5) - o.strokeWidth) > 1e-6 ||
        Math.abs((ink.opacity ?? 1) - o.opacity) > 1e-6
      );
    }
    const fp = bundle.paint.paths.find((p) => p.shapeId === styleContext.shapeId);
    if (!fp) return false;
    return (
      paintFillHex(bundle.paint, fp) !== o.fill.toUpperCase() ||
      (fp.stroke ?? "") !== o.stroke ||
      Math.abs((fp.strokeWidth ?? 0) - o.strokeWidth) > 1e-6
    );
  })();

  const styleDirty = !!styleDraft && !!styleContext &&
    (styleDraft.fill.toUpperCase() !== styleContext.original.fill.toUpperCase() ||
      styleDraft.stroke.toUpperCase() !== styleContext.original.stroke.toUpperCase() ||
      Math.abs(styleDraft.strokeWidth - styleContext.original.strokeWidth) > 1e-6 ||
      (styleKind === "ink" && Math.abs(styleDraft.opacity - styleContext.original.opacity) > 1e-6));

  const resetStyle = () => {
    if (!styleContext) return;
    setStyleDraft({ ...styleContext.original, preserveShading: styleDraft?.preserveShading ?? true });
  };

  const saveStyle = () => {
    const ctx = styleContext;
    if (!ctx || !styleDraft || !styleDirty || !styleKind) return;
    // Identity guard, same class as the geometry draft's: the draft must go
    // to ITS project. The payload's base is what the artist SAW — unless a
    // concurrent publish moved the project, in which case the visible chip
    // plus the explicit "Save against <current>" label IS the artist's
    // consented rebase (never an implicit one).
    if (ctx.projectId !== project?.id) {
      toast("This style draft belongs to another project — switch back to it to save.");
      return;
    }
    const baseRevision = staleStyleBase ? project?.currentRevision : ctx.baseRevision;
    if (!baseRevision) return;
    const payload: {
      stroke_color?: string;
      stroke_width?: number;
      opacity?: number;
      color?: string;
      preserve_shading?: boolean;
    } = {};
    if (styleKind === "ink") {
      if (styleDraft.fill.toUpperCase() !== ctx.original.fill.toUpperCase()) {
        payload.stroke_color = styleDraft.fill.toUpperCase();
      }
      if (Math.abs(styleDraft.strokeWidth - ctx.original.strokeWidth) > 1e-6) payload.stroke_width = styleDraft.strokeWidth;
      if (Math.abs(styleDraft.opacity - ctx.original.opacity) > 1e-6) payload.opacity = styleDraft.opacity;
    } else {
      if (styleDraft.fill.toUpperCase() !== ctx.original.fill.toUpperCase()) {
        payload.color = styleDraft.fill.toUpperCase();
        payload.preserve_shading = styleDraft.preserveShading;
      }
      if (styleDraft.stroke.toUpperCase() !== ctx.original.stroke.toUpperCase()) {
        payload.stroke_color = styleDraft.stroke.toUpperCase();
      }
      if (Math.abs(styleDraft.strokeWidth - ctx.original.strokeWidth) > 1e-6) payload.stroke_width = styleDraft.strokeWidth;
    }
    void editShapeStyle(ctx.shapeId, { ...payload, base_revision: baseRevision })
      .then((res) => {
        savedStyleJobRef.current = res.jobId;
      })
      .catch((e: Error) => toast(e.message));
  };

  const [artHistory, setArtHistory] = useState<ArtDraftHistory | null>(null);
  const artDragBeforeSnapshotRef = useRef<ArtDraftSnapshot | null>(null);
  const draftGenerationRef = useRef<number>(0);

  // ----- Task 40C review: topology-rebuild confirmation flow -----
  /** A gated operation (shape order, shape geometry save, object layer
   *  reorder, Build) the backend REJECTED because the revision carries
   *  manual gameplay topology. The hook's poll loop routes the refusal here
   *  with the operation's dialog copy; confirm re-dispatches WITH the flag —
   *  the backend owns the gate, the UI never bypasses it. */
  const moveShapeOrder = (order: "forward" | "backward") => {
    if (!artPath) return;
    void editShapeOrder(artPath.context.shapeId, order).catch((e: Error) => toast(e.message));
  };

  /** True when the project moved past the revision the draft was loaded
   *  from — another operation published while the artist was editing. */
  const staleDraftBase =
    !!artPath &&
    !!project?.currentRevision &&
    artPath.context.baseRevision !== project.currentRevision &&
    artPath.context.projectId === project.id;
  /** True when the MOUNTED project is not the project the draft belongs to
   *  (the workspace stays mounted across openProject). A foreign draft is
   *  never writable: saving would submit it to the other project's shape of
   *  the same id. It stays suspended — recoverable by switching back. */
  const foreignDraft = !!artPath && !!project && artPath.context.projectId !== project.id;
  /** True when the edited SHAPE ITSELF changed in between (its current paint
   *  path no longer matches the draft's origin) — saving then overwrites
   *  geometry changes the artist has not seen. */
  const draftShapeChangedInBetween =
    !!artPath &&
    (() => {
      const cur = bundle?.paint?.paths.find((q) => q.shapeId === artPath.context.shapeId)?.d;
      return cur !== undefined && cur !== artPath.context.originalD;
    })();

  const canUndo = !!artPath && !foreignDraft && !!artHistory && artHistory.past.length > 0;
  const canRedo = !!artPath && !foreignDraft && !!artHistory && artHistory.future.length > 0;

  const pickArtPath = (clientX: number, clientY: number) => {
    const board = boardRef.current;
    const p = project;
    if (!board || !bundle?.paint || !p?.currentRevision) return;
    const pt = board.clientToArt(clientX, clientY);
    if (!pt) return;
    // nearest pickable path within ~20 art units of the tap (reverse z =
    // topmost). Closed paint shapes and ink strokes are both editable; ink
    // z sits above the fills it decorates (document order). OPENNESS is a
    // property of the PATH's own geometry (trailing Z), not of its
    // classification: a closed ink outline edits under the closed rules
    // (must keep its Z), an open one under the stroke rules.
    const isOpenD = (d: string) => !d.trim().toUpperCase().endsWith("Z");
    const entries = [
      ...(bundle.paint.paths ?? [])
        .filter((q) => q.shapeId)
        .map((q) => ({ shapeId: q.shapeId!, d: q.d, z: q.z ?? -Infinity, open: isOpenD(q.d) })),
      ...(bundle.paint.inkPaths ?? [])
        .filter((q) => q.shapeId)
        .map((q) => ({ shapeId: q.shapeId!, d: q.d, z: q.z ?? -Infinity, open: isOpenD(q.d) })),
    ].sort((a, b) => b.z - a.z);
    let best: { shapeId: string; d: string; dist: number; open: boolean } | null = null;
    for (const q of entries) {
      const pts = flattenPath(q.d);
      if (pts.length < 2) continue;
      const dist = polylineNearestDistance(pts, { x: pt.x, y: pt.y });
      if (!best || dist < best.dist) best = { shapeId: q.shapeId, d: q.d, dist, open: q.open };
    }
    if (!best || best.dist > 20) {
      setArtPath(null);
      setArtHistory(null);
      toast("Tap directly on an artwork shape outline.");
      return;
    }
    const commands = parsePathCommands(best.d);
    if (!commands.length) {
      toast("This shape's path could not be parsed for node editing.");
      return;
    }
    if (!best.open && commands[commands.length - 1].op !== "Z") {
      toast("This shape is not a closed path — only closed artwork is editable in this slice.");
      return;
    }
    captureNodeTransform();
    draftGenerationRef.current += 1;
    const nextGen = draftGenerationRef.current;
    artDragBeforeSnapshotRef.current = null;
    setArtHistory({
      projectId: p.id,
      baseRevision: p.currentRevision,
      shapeId: best.shapeId,
      draftGeneration: nextGen,
      past: [],
      future: [],
    });
    setArtPath({
      context: {
        projectId: p.id,
        baseRevision: p.currentRevision,
        shapeId: best.shapeId,
        originalD: best.d,
      },
      commands,
      dragging: null,
      dirty: false,
      open: best.open,
    });
  };

  const beginArtDrag = (e: React.PointerEvent<SVGCircleElement>, cmd: number, pt: number) => {
    e.stopPropagation();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events / stale ids — continue without capture */
    }
    if (artPath) {
      artDragBeforeSnapshotRef.current = {
        commands: cloneCommands(artPath.commands),
        dirty: artPath.dirty,
      };
    }
    setArtPath((prev) => (prev ? { ...prev, dragging: { cmd, pt } } : prev));
  };

  const extendArtDrag = (e: React.PointerEvent<SVGCircleElement>) => {
    const board = boardRef.current;
    if (!board) return;
    const p = board.clientToArt(e.clientX, e.clientY);
    if (!p) return;
    setArtPath((prev) => {
      if (!prev || !prev.dragging) return prev;
      const { cmd, pt } = prev.dragging;
      return {
        ...prev,
        dirty: true,
        commands: prev.commands.map((c, i) =>
          i === cmd ? { ...c, pts: c.pts.map((q, j) => (j === pt ? { x: p.x, y: p.y } : q)) } : c
        ),
      };
    });
  };

  const endArtDrag = (e: React.PointerEvent<SVGCircleElement>) => {
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* pointer already released */
      }
    }
    const beforeSnap = artDragBeforeSnapshotRef.current;
    artDragBeforeSnapshotRef.current = null;
    setArtPath((prev) => {
      if (!prev || !prev.dragging) return prev;
      if (beforeSnap) {
        const beforeD = serializePathCommands(beforeSnap.commands);
        const afterD = serializePathCommands(prev.commands);
        if (beforeD !== afterD) {
          const gen = draftGenerationRef.current;
          setArtHistory((h) => {
            if (!h || h.draftGeneration !== gen) return h;
            return {
              ...h,
              past: [...h.past, beforeSnap].slice(-50),
              future: [],
            };
          });
        }
      }
      return { ...prev, dragging: null };
    });
  };

  const undoArtDraft = useCallback(() => {
    if (!artPath || foreignDraft) return;
    const cur = artPath;
    const gen = draftGenerationRef.current;
    setArtHistory((h) => {
      if (!h || h.draftGeneration !== gen || h.past.length === 0) return h;
      const prevSnap = h.past[h.past.length - 1];
      const newPast = h.past.slice(0, -1);
      const currentSnap = { commands: cloneCommands(cur.commands), dirty: cur.dirty };
      const newFuture = [...h.future, currentSnap].slice(-50);
      setArtPath({
        ...cur,
        commands: cloneCommands(prevSnap.commands),
        dirty: prevSnap.dirty,
        dragging: null,
      });
      return {
        ...h,
        past: newPast,
        future: newFuture,
      };
    });
  }, [artPath, foreignDraft]);

  const redoArtDraft = useCallback(() => {
    if (!artPath || foreignDraft) return;
    const cur = artPath;
    const gen = draftGenerationRef.current;
    setArtHistory((h) => {
      if (!h || h.draftGeneration !== gen || h.future.length === 0) return h;
      const nextSnap = h.future[h.future.length - 1];
      const newFuture = h.future.slice(0, -1);
      const currentSnap = { commands: cloneCommands(cur.commands), dirty: cur.dirty };
      const newPast = [...h.past, currentSnap].slice(-50);
      setArtPath({
        ...cur,
        commands: cloneCommands(nextSnap.commands),
        dirty: nextSnap.dirty,
        dragging: null,
      });
      return {
        ...h,
        past: newPast,
        future: newFuture,
      };
    });
  }, [artPath, foreignDraft]);

  /** Push the CURRENT draft state onto the undo stack — one meaningful step
   *  for DISCRETE operations (node add/remove), mirroring how one drag =
   *  one step. Truncates the redo branch. */
  const pushArtHistoryBefore = () => {
    if (!artPath) return;
    const gen = draftGenerationRef.current;
    const snap: ArtDraftSnapshot = { commands: cloneCommands(artPath.commands), dirty: artPath.dirty };
    setArtHistory((h) =>
      h && h.draftGeneration === gen ? { ...h, past: [...h.past, snap].slice(-50), future: [] } : h
    );
  };

  /** Double-click on an edge: insert a node at the nearest point of the
   *  drawn outline (de Casteljau split — the geometry is preserved exactly;
   *  only the editable node count changes). */
  const addArtNode = (clientX: number, clientY: number) => {
    const board = boardRef.current;
    if (!artPath || !board) return;
    const pt = board.clientToArt(clientX, clientY);
    if (!pt) return;
    const hit = nearestOnPathCommands(artPath.commands, pt);
    if (!hit || hit.dist > 12) {
      toast("Double-click directly on a drawn edge to add a node.");
      return;
    }
    const next = splitPathCommand(artPath.commands, hit.cmd, hit.t);
    if (!next) return;
    pushArtHistoryBefore();
    setArtPath((prev) => (prev ? { ...prev, commands: next, dirty: true } : prev));
  };

  /** Double-click on an anchor: remove that node by merging its two segments
   *  (refused with an explanation when it would break the ring). */
  const removeArtNode = (cmd: number) => {
    if (!artPath) return;
    const next = removePathAnchor(artPath.commands, cmd);
    if (!next) {
      toast("Only interior anchors can be removed — keep at least three edges.");
      return;
    }
    pushArtHistoryBefore();
    setArtPath((prev) => (prev ? { ...prev, commands: next, dirty: true } : prev));
  };

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (view !== "inspect" || tool !== "artnode" || !artPath) return;
      if (artSaveOpen || artConflictOpen) return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable ||
          target.closest?.("[contenteditable='true']"))
      ) {
        return; // Native text editing wins
      }
      const isCmdOrCtrl = e.metaKey || e.ctrlKey;
      if (isCmdOrCtrl && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) {
          redoArtDraft();
        } else {
          undoArtDraft();
        }
      } else if (isCmdOrCtrl && e.key.toLowerCase() === "y") {
        e.preventDefault();
        redoArtDraft();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [view, tool, artPath, artSaveOpen, artConflictOpen, undoArtDraft, redoArtDraft]);

  /** Save intent: foreign drafts are suspended (no request can target the
   *  wrong project); a stale base routes to the explicit-conflict dialog;
   *  otherwise the normal consequence confirmation. */
  const requestArtSave = () => {
    if (!artPath || !artPath.dirty) return;
    if (foreignDraft) {
      toast("This draft belongs to another project — switch back to it to save, or Cancel to discard.");
      return;
    }
    if (staleDraftBase) setArtConflictOpen(true);
    else setArtSaveOpen(true);
  };

  /** Save: submit the edited path (edit action "shape", keyed by the stable
   *  shapeId — the 409 stale-base guard is the shared edit-route check).
   *  PROJECT IDENTITY is re-verified here, not just at button level: a save
   *  dialog can stay open across an openProject switch, and runEdit would
   *  otherwise target whatever project is current. The draft is NOT dropped
   *  here: it stays mounted and editable until the job settles. Attribution
   *  is by job id (the settle-watch below) so only THIS save's success can
   *  clear it; rejections, failures, and unrelated revision changes keep the
   *  artist's changes available. */
  const submitArtSave = () => {
    const path = artPath;
    const p = project;
    if (!path || !path.dirty) return;
    if (!p || path.context.projectId !== p.id) {
      // Foreign project (dialog left open across a project switch): never
      // send — the request would edit the OTHER project's shape.
      toast("This draft belongs to another project — switch back to it to save.");
      return;
    }
    const d = serializePathCommands(path.commands);
    setArtSaveOpen(false);
    setArtConflictOpen(false);
    // jobId arrives with the POST response; "" marks "accepted, id pending".
    artPendingSaveRef.current = {
      jobId: "",
      submittedD: d,
      baseRevision: p.currentRevision ?? path.context.baseRevision,
    };
    void editShape(path.context.shapeId, d)
      .then((res) => {
        const pending = artPendingSaveRef.current;
        if (pending) artPendingSaveRef.current = { ...pending, jobId: res.jobId };
      })
      .catch((e: Error) => {
        artPendingSaveRef.current = null;
        toast(e.message);
        // A 409 means the client's snapshot is behind the server — resync so
        // the stale-base conflict becomes visible for an explicit choice.
        void syncProject();
      });
  };

  // Shape-save settle-watch: runEdit resolves when the job is ACCEPTED, not
  // when it finishes. Only a settled job whose ID IS OURS proves "my save
  // published its result" (draft discarded). A settled job that is NOT ours
  // means the project changed for another reason — the draft survives and the
  // save bar reports the stale base for an explicit save/discard choice.
  useEffect(() => {
    const pending = artPendingSaveRef.current;
    if (!pending || !pending.jobId || busy) return;
    const job = project?.job;
    if (!job?.id) return;
    artPendingSaveRef.current = null;
    if (job.id === pending.jobId && job.status === "done") {
      setArtPath(null);
      setArtHistory(null);
    }
    // Every other terminal outcome (our job failed/canceled/interrupted, or
    // an unrelated job settled) keeps the draft; the poller already toasted
    // failures.
  }, [busy, project?.job?.id, project?.job?.status]);

  const toolActive = view === "inspect" && hasBundle && tool !== "select" && !busy;

  const strokeWidthArt = (() => {
    const vb = strokeViewBox?.split(/\s+/).map(Number);
    const w = vb && vb.length === 4 && Number.isFinite(vb[2]) ? vb[2] : 576;
    return Math.max(1.2, w / 240);
  })();

  const beginStroke = (e: React.PointerEvent<SVGSVGElement>) => {
    const board = boardRef.current;
    if (!board) return;
    // Node tools: the pointerdown only bookmarks the tap — the target
    // (gameplay boundary / artwork shape) is picked on pointerup IF the
    // pointer did not drag (anchor handles stop propagation before this,
    // so drags never bookmark).
    if (tool === "node" || tool === "artnode") {
      nodeTapRef.current = { x: e.clientX, y: e.clientY };
      return;
    }
    const p = board.clientToArt(e.clientX, e.clientY);
    if (!p) return;
    // Freeze the overlay to the board's CURRENT viewBox: the overlay eats the
    // pointer events, so the board view cannot change mid-stroke.
    setStrokeViewBox(board.svg.getAttribute("viewBox") ?? null);
    strokeRef.current = [{ x: p.x, y: p.y }];
    setStrokePts([{ x: p.x, y: p.y }]);
    setPendingCut(null);
    setPendingDraw(null);
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events / stale ids — continue without capture */
    }
  };

  const extendStroke = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!strokeRef.current.length) return;
    const board = boardRef.current;
    if (!board) return;
    const p = board.clientToArt(e.clientX, e.clientY);
    if (!p) return;
    const last = strokeRef.current[strokeRef.current.length - 1];
    if (Math.hypot(p.x - last.x, p.y - last.y) < 1.5) return;
    strokeRef.current.push({ x: p.x, y: p.y });
    setStrokePts([...strokeRef.current]);
  };

  const endStroke = (e: React.PointerEvent<SVGSVGElement>) => {
    // Node tools: resolve the bookmarked tap (down+up without a significant
    // drag). Tapping empty canvas clears the current selection.
    if (tool === "node" || tool === "artnode") {
      const tap = nodeTapRef.current;
      nodeTapRef.current = null;
      if (tap && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) < 8) {
        if (tool === "node") pickNodeEdge(e.clientX, e.clientY);
        else pickArtPath(e.clientX, e.clientY);
      }
      return;
    }
    const raw = strokeRef.current;
    strokeRef.current = [];
    setStrokePts([]);
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* pointer already released */
      }
    }
    const board = boardRef.current;
    // Node mode returned above before any stroke could start; this guard
    // keeps the stroke path cut/pen-only.
    if (!board || !raw.length || (tool !== "cut" && tool !== "pen")) return;
    const pts = rdp(dropDensePoints(raw, 2), 1.2);
    if (tool === "cut") {
      if (pts.length < 2) {
        toast("Draw a longer cut line.");
        return;
      }
      const mid = polylineMidpoint(pts);
      const regionId = mid ? board.hitRegion(mid.x, mid.y) : null;
      if (!regionId) {
        // Backend contract: the cut crosses the whole region (the engine even
        // extends it past the bbox), so the LINE only has to cross it — the
        // midpoint must land inside the target region.
        toast("Drag the line across the region so its midpoint lands inside it — outside one edge to outside the other.");
        return;
      }
      setPendingCut({ regionId, d: pathFromPoints(pts, false) });
    } else {
      if (pts.length < 3) {
        toast("Draw a closed shape with at least 3 points.");
        return;
      }
      const area = polygonArea(pts);
      if (area < 120) {
        toast("Draw a larger shape — the region needs ~120 px² or more.");
        return;
      }
      setPenPalette(String(boardState?.selectedPaletteId ?? paletteEntries[0]?.id ?? 1));
      // A fresh stroke resets the fill to the chosen number group's swatch
      // (the default answer-key color) — the artist can override in the
      // confirm popover.
      setPenFill("");
      setPenGroup(objectGroup || "");
      const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
      const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
      setPendingDraw({ d: pathFromPoints(pts, true), area, anchor: artPointToCanvas(board, cx, cy) });
    }
  };

  const confirmCut = () => {
    const cut = pendingCut;
    if (!cut) return;
    setPendingCut(null);
    void cutRegion(cut.regionId, cut.d).catch((e: Error) => toast(e.message));
  };

  const confirmDraw = () => {
    const draw = pendingDraw;
    if (!draw) return;
    const paletteId = Number(penPalette) || 1;
    const artwork = penMode === "artwork";
    // Custom fill only when the artist actually changed it from the group
    // swatch (empty = default); the outline/layer options are artwork-only.
    const customFill = artwork && penFill && FREE_HEX_RE.test(penFill) ? penFillHex : undefined;
    const outlineW = artwork && penOutline ? Math.max(0, Math.min(8, Number(penStrokeWidth) || 0)) : undefined;
    setPendingDraw(null);
    void drawRegion(
      draw.d,
      paletteId,
      penGroup.trim() || undefined,
      artwork,
      customFill,
      outlineW,
      artwork ? penBehind : undefined
    ).catch((e: Error) => toast(e.message));
  };

  // ------------------------------------------- node tool handlers (contract A)

  /** Pick the nearest shared boundary (both sides non-null) within ~24 art
   *  units of the tap — the tap point is converted through board.clientToArt
   *  exactly like the cut overlay. No hit clears the current selection. */
  const pickNodeEdge = (clientX: number, clientY: number) => {
    const board = boardRef.current;
    if (!board || !bundle) return;
    const p = board.clientToArt(clientX, clientY);
    if (!p) return;
    const edges = bundle.geometry.edges ?? [];
    let best: { edge: EdgeEntry; dist: number } | null = null;
    for (const e of edges) {
      if (!e.leftRegion || !e.rightRegion) continue;
      const pts = flattenPath(e.d);
      if (pts.length < 2) continue;
      const dist = polylineNearestDistance(pts, { x: p.x, y: p.y });
      if (!best || dist < best.dist) best = { edge: e, dist };
    }
    if (!best || best.dist > 24) {
      setNodeEdge(null);
      setNodeConfirmOpen(false);
      toast("Tap directly on a boundary between two regions.");
      return;
    }
    const left = best.edge.leftRegion;
    const right = best.edge.rightRegion;
    if (!left || !right) return; // filtered above; keeps narrowing honest
    const raw = flattenPath(best.edge.d);
    // Closed rings (Z outlines are the norm for full boundary loops) end with
    // a repeat of the first vertex: flattenPath pushes it for hit-testing, but
    // the anchor list keeps UNIQUE vertices (a duplicate handle would let the
    // start and end drift apart when dragged). The ring closes visually and
    // the submission re-appends Z.
    const closed =
      raw.length >= 4 &&
      Math.hypot(raw[raw.length - 1].x - raw[0].x, raw[raw.length - 1].y - raw[0].y) < 0.05;
    const base = closed ? raw.slice(0, -1) : raw;
    // Capture the art→client transform NOW (event context — refs readable)
    // so the anchors render on the very first frame after selection.
    captureNodeTransform();
    setNodeEdge({
      edgeId: best.edge.id,
      left,
      right,
      kind: best.edge.kind,
      closed,
      base,
      anchors: base.map((q) => ({ x: q.x, y: q.y })),
      dragging: null,
    });
  };

  /** Anchor pointerdown: the handle owns its pointer (capture) and stops the
   *  overlay from treating this as a boundary-selection tap. */
  const beginNodeDrag = (e: React.PointerEvent<SVGCircleElement>, i: number) => {
    e.stopPropagation();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events / stale ids — continue without capture */
    }
    setNodeEdge((prev) => (prev ? { ...prev, dragging: i } : prev));
  };

  /** Anchor pointermove: the art-space position under the pointer replaces
   *  the dragged anchor (client→art via board.clientToArt; no distance
   *  limit — the server validates the resulting boundary). */
  const extendNodeDrag = (e: React.PointerEvent<SVGCircleElement>) => {
    const board = boardRef.current;
    if (!board) return;
    const p = board.clientToArt(e.clientX, e.clientY);
    if (!p) return;
    setNodeEdge((prev) => {
      if (!prev || prev.dragging == null) return prev;
      const dragging = prev.dragging;
      return { ...prev, anchors: prev.anchors.map((a, i) => (i === dragging ? { x: p.x, y: p.y } : a)) };
    });
  };

  const endNodeDrag = (e: React.PointerEvent<SVGCircleElement>) => {
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* pointer already released */
      }
    }
    setNodeEdge((prev) => (prev && prev.dragging != null ? { ...prev, dragging: null } : prev));
  };

  /** Confirm: submit the dragged anchors as the new boundary polyline
   *  between the edge's two regions (edit action "node"). Closed rings
   *  re-append Z so the server receives the full outline. */
  const confirmNode = () => {
    const edge = nodeEdge;
    if (!edge) return;
    setNodeConfirmOpen(false);
    setNodeEdge(null);
    void nodeEdit([edge.left, edge.right], pathFromPoints(edge.anchors, edge.closed)).catch((e: Error) => toast(e.message));
  };

  /** Art → client-px transform for the node overlay (captured OUTSIDE render
   *  — refs may only be read in handlers/effects). Stored as plain matrix
   *  components so render stays a pure map over `nodeEdge` state. */
  const [nodeTransform, setNodeTransform] = useState<{
    a: number;
    b: number;
    c: number;
    d: number;
    e: number;
    f: number;
    ox: number;
    oy: number;
  } | null>(null);

  const captureNodeTransform = useCallback(() => {
    const board = boardRef.current;
    const overlay = overlaySvgRef.current;
    if (!board || !overlay) return;
    try {
      const ctm = board.svg.getScreenCTM();
      const rect = overlay.getBoundingClientRect();
      if (!ctm || !rect.width || !rect.height) return;
      setNodeTransform({ a: ctm.a, b: ctm.b, c: ctm.c, d: ctm.d, e: ctm.e, f: ctm.f, ox: rect.left, oy: rect.top });
    } catch {
      /* layout unavailable — the observer pass retries */
    }
  }, [boardRef]);

  // Render-time adjustments (the free-color sync pattern below): leaving the
  // Node tool or the inspect view — or a bundle change replacing the board —
  // discards the selected boundary + its confirm state; entering node mode
  // resets the overlay to client-space coordinates (viewBox undefined →
  // 1 user unit = 1 client px).
  const nodeSessionKey =
    (tool === "node" || tool === "artnode") && view === "inspect" && bundle
      ? `${bundle.manifest.id}:${bundle.manifest.version}`
      : null;
  const [syncedNodeSession, setSyncedNodeSession] = useState<string | null>(null);
  if (nodeSessionKey !== syncedNodeSession) {
    setSyncedNodeSession(nodeSessionKey);
    setNodeEdge(null);
    setNodeConfirmOpen(false);
    // A bundle/view change does NOT discard an Art node DRAFT (review
    // follow-up): another operation may have published a revision while the
    // artist was editing — the draft is keyed by the stable shape id and its
    // base revision now differs from the project's, which the save bar
    // reports as an explicit conflict. The draft only goes away by the
    // artist's own action (Cancel, switching tools) or by its save
    // publishing. The gameplay BOUNDARY selection above is still discarded —
    // it references two region ids that the new revision may have re-derived.
    if (nodeSessionKey != null) setStrokeViewBox(null);
  }

  // Keep the anchors glued to the artwork when the board view changes
  // underneath (toolbar zoom / fit / focus — the overlay itself blocks
  // direct board gestures) and when the window resizes the canvas.
  const nodeEdgeId = nodeEdge?.edgeId ?? null;
  const artPathActive = tool === "artnode" && !!artPath;
  useEffect(() => {
    if (!nodeEdgeId && !artPathActive) return;
    const svg = boardRef.current?.svg;
    if (!svg) return;
    captureNodeTransform();
    const refresh = () => captureNodeTransform();
    let observer: MutationObserver | null = null;
    if (typeof MutationObserver !== "undefined") {
      observer = new MutationObserver(refresh);
      observer.observe(svg, { attributes: true, attributeFilter: ["viewBox"] });
    }
    window.addEventListener("resize", refresh);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", refresh);
    };
  }, [nodeEdgeId, artPathActive, boardRef, captureNodeTransform]);

  // Node overlay geometry: pure art → client px application of the captured
  // transform — the anchor handles keep a constant on-screen size at any
  // zoom. Null until a boundary is selected AND the transform is captured.
  // Closed rings draw the closing segment (anchorLine re-appends the first
  // vertex) while the draggable circles stay on the UNIQUE vertices.
  const closeRing = (pts: Array<{ x: number; y: number }>) =>
    nodeEdge?.closed && pts.length > 2 ? [...pts, pts[0]] : pts;
  const nodeGeometry =
    nodeEdge && nodeTransform
      ? (() => {
          const toClient = (p: { x: number; y: number }) => ({
            x: nodeTransform.a * p.x + nodeTransform.c * p.y + nodeTransform.e - nodeTransform.ox,
            y: nodeTransform.b * p.x + nodeTransform.d * p.y + nodeTransform.f - nodeTransform.oy,
          });
          const base = nodeEdge.base.map(toClient);
          const anchors = nodeEdge.anchors.map(toClient);
          return {
            baseLine: closeRing(base),
            anchorLine: closeRing(anchors),
            anchors,
          };
        })()
      : null;

  // Artwork-node overlay geometry (Task 33): the path's control points in
  // client px. Anchors (command endpoints) render solid; Bézier handles
  // (control points) render hollow with thin connector lines. The preview
  // polyline flattens the WHOLE serialized path in one pass so commands chain
  // from the real current point (a per-command flatten would evaluate every
  // segment from (0,0)) and the Z closure is drawn as a real segment.
  const artGeometry =
    artPath && nodeTransform
      ? (() => {
          const toClient = (p: { x: number; y: number }) => ({
            x: nodeTransform.a * p.x + nodeTransform.c * p.y + nodeTransform.e - nodeTransform.ox,
            y: nodeTransform.b * p.x + nodeTransform.d * p.y + nodeTransform.f - nodeTransform.oy,
          });
          const pts = flattenPath(serializePathCommands(artPath.commands));
          const controls: Array<{ x: number; y: number; cmd: number; pt: number }> = [];
          const tethers: Array<{ x: number; y: number }> = [];
          artPath.commands.forEach((c, ci) => {
            if ((c.op === "C" || c.op === "Q") && ci > 0) {
              const prevEnd = artPath.commands[ci - 1].pts;
              const startAnchor = prevEnd[prevEnd.length - 1];
              const endAnchor = c.pts[c.pts.length - 1];
              c.pts.forEach((q, pi) => {
                const isEnd = pi === c.pts.length - 1;
                if (isEnd) return;
                // The cubic's second control belongs to the segment's END
                // anchor; every other control (cubic c1, the quadratic
                // control) hangs off the segment's start anchor.
                const anchor = c.op === "C" && pi === c.pts.length - 2 ? endAnchor : startAnchor;
                controls.push({ ...toClient(q), cmd: ci, pt: pi });
                tethers.push(toClient(anchor));
              });
            }
          });
          return {
            previewLine: pts.map(toClient),
            anchors: artPath.commands
              .map((c, ci) => ({ c, ci }))
              .filter(({ c }) => c.op !== "Z" && c.pts.length > 0)
              .map(({ c, ci }) => ({ ...toClient(c.pts[c.pts.length - 1]), cmd: ci, pt: c.pts.length - 1 })),
            controls,
            tethers,
          };
        })()
      : null;

  // ----------------------------------------------------- free color (play view)

  const [freeHex, setFreeHex] = useState(freeColor);
  // Keep the hex field in sync with context-level changes (recent swatch or
  // palette quick-access) — the React "adjust state when a prop changes"
  // pattern, no effect needed.
  const [syncedFreeColor, setSyncedFreeColor] = useState(freeColor);
  if (freeColor !== syncedFreeColor) {
    setSyncedFreeColor(freeColor);
    setFreeHex(freeColor);
  }
  const freeHexValid = FREE_HEX_RE.test(freeHex);

  const applyFreeColor = (hex: string) => {
    if (!FREE_HEX_RE.test(hex)) {
      toast("Enter a 6-digit hex color like #66AA33.");
      return;
    }
    setFreeHex(hex.toUpperCase());
    setBoardFreeColor(hex);
  };

  // ---------------------------------------- play-test run clock (contract B)

  const [playStartMs, setPlayStartMs] = useState<number | null>(null);
  const [playElapsed, setPlayElapsed] = useState(0);
  const [playtestRecorded, setPlaytestRecorded] = useState(false);
  const [playtestSaving, setPlaytestSaving] = useState(false);

  // Fresh-run tracking (render-time adjustment, same pattern): the run clock
  // (re)starts when the play view is at progress 0 — board mount, "Reset
  // test", or a revision change — which also lifts the already-recorded
  // guard so a fresh run can be recorded again.
  const playRunKey =
    view === "play" && bundle && (boardState?.completed ?? 0) === 0
      ? `${bundle.manifest.id}:${bundle.manifest.version}`
      : null;
  const [syncedPlayRun, setSyncedPlayRun] = useState<string | null>(null);
  if (playRunKey !== syncedPlayRun) {
    setSyncedPlayRun(playRunKey);
    if (playRunKey != null) {
      setPlayStartMs(Date.now());
      setPlayElapsed(0);
      setPlaytestRecorded(false);
    }
  }

  // One-second tick while the play view is mounted — the record button shows
  // the live run clock. Cleared on view change / unmount (no leaks); recreated
  // when the clock itself restarts so the closure stays fresh.
  useEffect(() => {
    if (view !== "play") return;
    const id = window.setInterval(() => {
      setPlayElapsed(playStartMs != null ? Math.floor((Date.now() - playStartMs) / 1000) : 0);
    }, 1000);
    return () => window.clearInterval(id);
  }, [view, playStartMs]);

  const playtestComplete = total > 0 && completed === total;
  const playtestReady = playtestComplete && !playtestRecorded && !playtestSaving;
  const playClockLabel = playStartMs != null ? formatPlaytestClock(playElapsed) : null;

  /** Submit the completed run (contract B) — the context action patches the
   *  difficulty profile in-context; the board is NOT remounted. */
  const recordPlaytestRun = () => {
    const seconds = playStartMs != null ? Math.round((Date.now() - playStartMs) / 1000) : 0;
    if (seconds < 10) {
      toast("Playtest must run at least 10 seconds.");
      return;
    }
    setPlaytestSaving(true);
    void recordPlaytest({ seconds, filled: completed, total, mistakes, mode: boardMode })
      .then(() => {
        setPlaytestSaving(false);
        setPlaytestRecorded(true);
      })
      .catch((e: Error) => {
        setPlaytestSaving(false);
        toast(e.message);
      });
  };

  return (
    <section className="studio-main-panel min-w-0" aria-label="Artwork workspace">
      {/* Workspace header */}
      <div className="mb-5 flex flex-wrap items-center justify-between gap-2.5">
        <div>
          <span className="mb-1.5 block text-[9px] font-semibold uppercase tracking-[0.16em] text-[#80908a]">
            ARTWORK WORKSPACE
          </span>
          <h1 className="text-2xl font-bold leading-tight tracking-tight text-[#183837]">
            {project?.title ?? "Make something worth coloring."}
          </h1>
        </div>
        <div
          className={`rounded-full border px-2.5 py-1.5 text-[10px] ${
            revision
              ? "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]"
              : "border-[#e1e5df] text-[#778481]"
          }`}
          role="status"
        >
          {revision ? `v${revision.version} · ${revision.kind}` : "No revision yet"}
        </div>
      </div>

      {/* Task 28/29 — an active generation session never disappears behind
          the editor: resume it any time (the session workspace opens on top). */}
      {activeSession && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[#cce7dc] bg-[#edf6f2] px-3.5 py-2.5">
          <span className="flex items-center gap-2 text-[11px] font-semibold text-[#126e5e]">
            <Waypoints className="size-4 shrink-0" aria-hidden />
            {activeSession.mode === "ai_chat"
              ? "Create-with-AI session"
              : activeSession.mode === "image_reference"
                ? "Reference session"
                : "Convert session"}
            {" · "}
            {sessionResumeCopy(activeSession.status).title.toLowerCase()} — resume any time.
          </span>
          <Button
            size="sm"
            className="h-8 rounded-md bg-[#0e554e] text-[10px] font-semibold text-white hover:bg-[#0a423d]"
            onClick={resumeCreationSession}
          >
            Resume
          </Button>
        </div>
      )}

      {/* Brief box */}
      <div className="rounded-xl border border-[#e1e5df] bg-white/70 p-3.5">
        <label htmlFor="studio-brief" className="studio-label mb-1.5 mt-0">
          Approved image brief
          <span className="ml-1.5 font-normal text-[#778481]">Edit directly, or let chat refine it.</span>
        </label>
        <Textarea
          id="studio-brief"
          value={briefInput}
          onChange={(e) => setBriefInput(e.target.value)}
          maxLength={8000}
          rows={3}
          placeholder="An original, richly illustrated woodland treehouse with a glowing window, winding stairs and a waterfall. No lettering, numbers, palettes or UI."
          className="min-h-14 resize-y border-0 bg-transparent p-1 text-[11px] leading-relaxed shadow-none focus-visible:ring-0"
        />
        <div className="mt-2 flex flex-wrap items-center justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-8 rounded-md border-[#e1e5df] bg-white text-[10px] hover:border-[#65a89b] hover:bg-[#f0f7f3]"
            onClick={() =>
              void saveBrief()
                .then(() => toast("Brief saved locally."))
                .catch((e: Error) => toast(e.message))
            }
          >
            Save brief
          </Button>
          <Select
            value={generationSource}
            onValueChange={(v) => setGenerationSource(v as typeof generationSource)}
            disabled={!aiConfigured || busy}
          >
            <SelectTrigger
              className="h-8 w-44 rounded-md border-[#e1e5df] bg-white text-[10px]"
              aria-label="AI generation source"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="brief">Generate from brief</SelectItem>
              <SelectItem value="reference">Edit owned reference</SelectItem>
              <SelectItem value="current">Revise current master</SelectItem>
            </SelectContent>
          </Select>
          <Button
            size="sm"
            className="h-8 rounded-md border border-[#cfe6db] bg-[#e5f3ed] text-[10px] font-semibold text-[#126e5e] hover:bg-[#d9ede2] disabled:opacity-50"
            disabled={!aiConfigured || busy}
            title={aiConfigured ? "Generate a new master image with AI" : "API key required"}
            onClick={() =>
              void generate().catch((e: Error) => toast(e.message))
            }
          >
            <Sparkles className="size-3.5" aria-hidden />
            Generate master
          </Button>
        </div>
      </div>

      {/* Canvas toolbar */}
      <div className="mx-0 my-4 flex flex-wrap items-center justify-between gap-2">
        <div
          className="flex flex-wrap gap-0.5 rounded-[9px] bg-[#e8ece5] p-1"
          role="group"
          aria-label="Artwork preview state"
        >
          {VIEW_ORDER.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => switchView(v)}
              className={`rounded-md px-2.5 py-1.5 text-[10px] font-medium whitespace-nowrap transition-colors ${
                view === v
                  ? "bg-white text-[#087f74] shadow-[0_1px_4px_rgba(18,47,34,0.13)]"
                  : "text-[#657671] hover:text-[#183837]"
              }`}
              aria-pressed={view === v}
            >
              {VIEW_LABELS[v]}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            className="size-7 rounded-md text-[10px] text-[#657671] hover:bg-[#f0f7f3]"
            aria-label="Zoom out"
            onClick={zoomOut}
          >
            <ZoomOut className="size-3.5" aria-hidden />
          </Button>
          <span ref={zoomRef} className="w-9 text-center text-[10px] text-[#778481]" aria-live="off">
            100%
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="size-7 rounded-md text-[10px] text-[#657671] hover:bg-[#f0f7f3]"
            aria-label="Zoom in"
            onClick={zoomIn}
          >
            <ZoomIn className="size-3.5" aria-hidden />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-md px-2 text-[10px] text-[#657671] hover:bg-[#f0f7f3]"
            onClick={fit}
          >
            <Maximize2 className="size-3.5" aria-hidden />
            Fit
          </Button>
        </div>
      </div>

      {/* Canvas area */}
      <div
        ref={canvasRef}
        className={`studio-canvas relative flex min-w-0 rounded-xl border border-[#dce2d9] ${
          view === "zoomlab"
            ? "min-h-64 w-full flex-col items-stretch justify-start px-3.5 pb-14 pt-3.5"
            : "h-[clamp(440px,60vh,880px)] min-h-64 items-center justify-center overflow-hidden p-3.5"
        }`}
        role={view === "zoomlab" ? undefined : "img"}
        aria-label={view === "zoomlab" ? undefined : "Artwork canvas"}
      >
        {!hasMaster && !hasBundle && view !== "zoomlab" && (
          <div className="px-6 py-6 text-center">
            <span className="mb-5 block text-6xl text-[#acbfb1]" aria-hidden>
              ◈
            </span>
            <h2 className="text-2xl font-bold text-[#527360]">From illustration to playable art.</h2>
            <p className="mt-3 text-xs leading-7 text-[#778481]">
              Upload an approved image, or generate a new master.
              <br />
              Then build the vector regions and test the coloring.
            </p>
            <span className="mt-7 block text-[9px] font-semibold tracking-[0.14em] text-[#95a599]">
              ONE MASTER · MATCHED STATES · REAL SVG PATHS
            </span>
          </div>
        )}
        {view === "master" && hasMaster && project && (
          <figure className="flex h-full w-full min-h-0 flex-col items-center gap-1.5">
            <img
              src={
                project.master?.kind === "svg"
                  ? masterSvgUrl(project.id, project.master.sha256)
                  : imageUrl(project.id, "master", project.master!.sha256)
              }
              alt="Current approved artwork master"
              className="min-h-0 w-full flex-1 object-contain drop-shadow-[0_4px_15px_rgba(33,56,47,0.13)]"
            />
            {project.master?.kind === "svg" && project.master.summary && (
              <figcaption className="shrink-0 text-center text-[9px] leading-relaxed text-[#607767]">
                {project.master.summary.shapes} shapes · {project.master.summary.curvedShapes} curved ·{" "}
                {project.master.summary.gradients} gradients · {project.master.summary.hiddenShapes} hidden
                excluded · {project.master.summary.rasterized ? "rasterized" : "never rasterized"}
              </figcaption>
            )}
          </figure>
        )}
        {view === "zoomlab" && <ZoomLab />}
        {/* VectorBoard owns this element's children — React renders it empty, once. */}
        <svg
          ref={svgRef}
          xmlns="http://www.w3.org/2000/svg"
          className={`h-full w-full select-none drop-shadow-[0_4px_15px_rgba(33,56,47,0.06)] ${
            view === "master" || view === "zoomlab" || !hasBundle ? "hidden" : "block"
          }`}
          aria-label="Interactive vector artwork"
        />
        {/* Cut / Pen drawing overlay — same box as the board svg (inset-3.5
            matches the canvas p-3.5), pointer events active only while a tool
            is selected. The board keeps rendering below. NOTE: the svg itself
            is a REPLACED element — absolute insets alone do not stretch it
            (it would fall back to the intrinsic 300×150), so it fills a
            stretched wrapper div that mirrors the board svg's exact box
            (identical viewBox + identical rendered box ⇒ identical
            letterboxing ⇒ art-unit strokes land pixel-exact). */}
        {toolActive && (
          <div className="absolute inset-3.5 z-[3]">
            <svg
              ref={overlaySvgRef}
              xmlns="http://www.w3.org/2000/svg"
              viewBox={strokeViewBox ?? undefined}
              className="h-full w-full touch-none select-none"
              style={{ pointerEvents: "auto", cursor: "crosshair" }}
              role="img"
              aria-label={`Drawing layer — ${
                tool === "cut" ? "cut line" : tool === "pen" ? "pen shape" : "boundary anchors"
              } in progress`}
              onPointerDown={beginStroke}
              onPointerMove={extendStroke}
              onPointerUp={endStroke}
              onPointerCancel={endStroke}
            >
              {strokePts.length > 1 && (
                <polyline
                  points={strokePts.map((p) => `${p.x},${p.y}`).join(" ")}
                  fill="none"
                  stroke={tool === "cut" ? "#ba463f" : "#087f74"}
                  strokeWidth={strokeWidthArt}
                  strokeDasharray={tool === "cut" ? "8 5" : undefined}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}
              {/* Node tool (contract A): ghost of the ORIGINAL boundary + the
                  live polyline through the dragged anchors, both converted to
                  client px (viewBox undefined above) so the anchor handles
                  keep a constant on-screen size at any zoom. Only the circles
                  take pointer events — the lines never block board taps. */}
              {tool === "node" && nodeEdge && nodeGeometry && (
                <g
                  role="group"
                  aria-label={`Boundary anchors between ${nodeEdge.left} and ${nodeEdge.right}`}
                >
                  <g style={{ pointerEvents: "none" }}>
                    <polyline
                      points={nodeGeometry.baseLine.map((p) => `${p.x},${p.y}`).join(" ")}
                      fill="none"
                      stroke="#9aa7a1"
                      strokeWidth={1}
                      strokeDasharray="4 4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                    <polyline
                      points={nodeGeometry.anchorLine.map((p) => `${p.x},${p.y}`).join(" ")}
                      fill="none"
                      stroke="#087f74"
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </g>
                  {nodeGeometry.anchors.map((p, i) => (
                    <circle
                      key={i}
                      cx={p.x}
                      cy={p.y}
                      r={nodeEdge.dragging === i ? 7 : 5}
                      fill={nodeEdge.dragging === i ? "#087f74" : "white"}
                      stroke="#087f74"
                      strokeWidth={1.5}
                      style={{ cursor: nodeEdge.dragging === i ? "grabbing" : "grab" }}
                      aria-label={`Boundary anchor ${i + 1} of ${nodeGeometry.anchors.length}`}
                      onPointerDown={(e) => beginNodeDrag(e, i)}
                      onPointerMove={extendNodeDrag}
                      onPointerUp={endNodeDrag}
                      onPointerCancel={endNodeDrag}
                    />
                  ))}
                </g>
              )}
              {/* Artwork Path node overlay (Task 33): live Bézier preview +
                  draggable anchors (solid) and handles (hollow, tethered).
                  Same constant-screen-size handles as the boundary tool. */}
              {tool === "artnode" && artPath && artGeometry && (
                <g role="group" aria-label={`Artwork path anchors for shape ${artPath.context.shapeId}`}>
                  <g style={{ pointerEvents: "none" }}>
                    <polyline
                      points={artGeometry.previewLine.map((p) => `${p.x},${p.y}`).join(" ")}
                      fill="none"
                      stroke={artPath.dirty ? "#ba463f" : "#087f74"}
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      style={{ pointerEvents: "stroke", cursor: "copy" }}
                      onPointerDown={(e) => e.stopPropagation()}
                      onPointerUp={(e) => e.stopPropagation()}
                      onDoubleClick={(e) => {
                        // Add-node target: the parent group opted out of
                        // pointer events for drawing; the stroke re-enables
                        // them so double-click lands HERE (not on the canvas
                        // tap-to-pick below).
                        e.stopPropagation();
                        addArtNode(e.clientX, e.clientY);
                      }}
                    />
                    {artGeometry.controls.map((_, i) => (
                      <line
                        key={`h-${i}`}
                        x1={artGeometry.tethers[i]?.x}
                        y1={artGeometry.tethers[i]?.y}
                        x2={artGeometry.controls[i]?.x}
                        y2={artGeometry.controls[i]?.y}
                        stroke="#9aa7a1"
                        strokeWidth={1}
                      />
                    ))}
                  </g>
                  {artGeometry.controls.map((p, i) => (
                    <circle
                      key={`c-${i}`}
                      cx={p.x}
                      cy={p.y}
                      r={artPath.dragging?.cmd === p.cmd && artPath.dragging?.pt === p.pt ? 6 : 4}
                      fill={artPath.dragging?.cmd === p.cmd && artPath.dragging?.pt === p.pt ? "#ba463f" : "white"}
                      stroke="#ba463f"
                      strokeWidth={1.5}
                      style={{ cursor: "grab" }}
                      aria-label={`Bézier handle ${i + 1}`}
                      onPointerDown={(e) => beginArtDrag(e, p.cmd, p.pt)}
                      onPointerMove={extendArtDrag}
                      onPointerUp={endArtDrag}
                      onPointerCancel={endArtDrag}
                    />
                  ))}
                  {artGeometry.anchors.map((p) => (
                    <circle
                      key={`a-${p.cmd}`}
                      cx={p.x}
                      cy={p.y}
                      r={artPath.dragging?.cmd === p.cmd && artPath.dragging?.pt === p.pt ? 7 : 5}
                      fill={artPath.dragging?.cmd === p.cmd && artPath.dragging?.pt === p.pt ? "#087f74" : "white"}
                      stroke="#087f74"
                      strokeWidth={1.5}
                      style={{ cursor: artPath.dragging?.cmd === p.cmd ? "grabbing" : "grab" }}
                      aria-label={`Anchor ${p.cmd + 1}`}
                      onPointerDown={(e) => beginArtDrag(e, p.cmd, p.pt)}
                      onPointerMove={extendArtDrag}
                      onPointerUp={endArtDrag}
                      onPointerCancel={endArtDrag}
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        removeArtNode(p.cmd);
                      }}
                    />
                  ))}
                </g>
              )}
            </svg>
          </div>
        )}
        {/* Pen confirm popover (contract §3): anchored at the drawn shape's
            centroid inside the canvas; click-away / Escape cancels the shape.
            The anchor is inert — PopoverContent portals above the overlay. */}
        <Popover
          open={toolActive && !!pendingDraw}
          onOpenChange={(open) => {
            if (!open) setPendingDraw(null);
          }}
        >
          <PopoverAnchor
            className="pointer-events-none absolute size-0"
            style={
              pendingDraw
                ? { left: pendingDraw.anchor.left, top: pendingDraw.anchor.top }
                : { left: "50%", top: "50%" }
            }
            aria-hidden
          />
          <PopoverContent
            className="w-84 max-w-[calc(100vw-2.5rem)] rounded-xl border-[#cfe6db] bg-[#edf6f2] p-3.5"
            align="center"
            sideOffset={10}
            aria-label="Confirm new pen-drawn shape"
          >
            <p className="text-[11px] font-semibold text-[#183837]">
              {penMode === "artwork" ? "Create artwork shape + region?" : "Create a gameplay region?"}
            </p>
            <p className="mt-0.5 text-[10px] leading-relaxed text-[#657671]">
              Closes a {pendingDraw ? Math.round(pendingDraw.area).toLocaleString("en-US") : "…"} px² surface.{" "}
              {penMode === "artwork"
                ? "The shape becomes finished artwork (a paint layer path with fill, outline and z-order) plus the playable tap region — drawn over existing art, the regions underneath are carved so surfaces never overlap; recolor works like any imported shape."
                : "It becomes a white tap target (gameplay-only — the artist paints it later)."}
            </p>
            {/* Pen mode: artwork (paints + region) vs region-only */}
            <div
              className="mt-2.5 flex flex-wrap gap-0.5 rounded-[9px] bg-[#e0e8e2] p-1"
              role="group"
              aria-label="Pen mode"
            >
              {(
                [
                  { key: "artwork", label: "Artwork + region" },
                  { key: "region", label: "Region only" },
                ] as Array<{ key: "artwork" | "region"; label: string }>
              ).map((m) => (
                <button
                  key={m.key}
                  type="button"
                  aria-pressed={penMode === m.key}
                  disabled={busy}
                  onClick={() => setPenMode(m.key)}
                  className={`flex-1 rounded-md px-2 py-1.5 text-[10px] font-medium transition-colors disabled:opacity-50 ${
                    penMode === m.key
                      ? "bg-white text-[#087f74] shadow-[0_1px_4px_rgba(18,47,34,0.13)]"
                      : "text-[#657671] hover:text-[#183837]"
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
            <div className="mt-2.5 grid grid-cols-2 gap-2">
              <div>
                <label htmlFor="studio-pen-palette" className="text-[10px] leading-snug text-[#657671]">
                  Number group
                </label>
                <Select
                  value={penPalette}
                  onValueChange={(v) => {
                    setPenPalette(v);
                    // A group switch resets the fill to that group's swatch.
                    setPenFill("");
                  }}
                  disabled={busy}
                >
                  <SelectTrigger
                    id="studio-pen-palette"
                    className="mt-1 h-9 w-full rounded-md border-[#e1e5df] bg-white text-xs"
                    aria-label="Palette group for the new region"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {paletteEntries.map((p) => (
                      <SelectItem key={p.id} value={String(p.id)}>
                        {p.id} · {p.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label htmlFor="studio-pen-group" className="text-[10px] leading-snug text-[#657671]">
                  Object group (optional)
                </label>
                <Input
                  id="studio-pen-group"
                  value={penGroup}
                  placeholder="roof"
                  pattern="[a-z0-9]+(-[a-z0-9]+)*"
                  onChange={(e) => setPenGroup(e.target.value)}
                  className="mt-1 h-9 rounded-md bg-white text-xs"
                />
              </div>
            </div>
            {penMode === "artwork" && (
              <div className="mt-2.5 rounded-lg border border-[#dce4dd] bg-white/70 p-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <label
                    htmlFor="studio-pen-fill"
                    className="text-[10px] leading-snug text-[#657671]"
                  >
                    Fill
                  </label>
                  <input
                    id="studio-pen-fill"
                    type="color"
                    value={penFillHex}
                    onChange={(e) => setPenFill(e.target.value.toUpperCase())}
                    disabled={busy}
                    className="size-8 shrink-0 cursor-pointer rounded-md border border-[#e1e5df] bg-white p-0.5"
                    aria-label="Artwork fill color"
                  />
                  <Input
                    value={penFill || penGroupHex}
                    onChange={(e) => setPenFill(e.target.value.toUpperCase())}
                    placeholder={penGroupHex}
                    inputMode="text"
                    spellCheck={false}
                    maxLength={7}
                    disabled={busy}
                    aria-label="Artwork fill hex value"
                    className="h-8 w-24 shrink-0 rounded-md bg-white font-mono text-[10px] uppercase"
                  />
                  {!penFill && (
                    <span className="text-[9px] leading-snug text-[#778481]">
                      matches the number-group color
                    </span>
                  )}
                  {penFill && FREE_HEX_RE.test(penFill) && penFillHex !== penGroupHex && (
                    <span className="text-[9px] leading-snug text-[#778481]">
                      joins the palette group with this color (created if missing — the selected group keeps its swatch)
                    </span>
                  )}
                </div>
                {penFill && !FREE_HEX_RE.test(penFill) && (
                  <p className="mt-1 text-[9px] text-[#b3541e]" role="alert">
                    Use a #RRGGBB hex value — falling back to the group color.
                  </p>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <label
                    htmlFor="studio-pen-outline"
                    className="flex min-h-11 items-center gap-1.5 text-[10px] leading-snug text-[#657671]"
                  >
                    <input
                      id="studio-pen-outline"
                      type="checkbox"
                      checked={penOutline}
                      onChange={(e) => setPenOutline(e.target.checked)}
                      disabled={busy}
                      className="size-3.5 accent-[#087f74]"
                    />
                    Ink outline
                  </label>
                  {penOutline && (
                    <Input
                      value={penStrokeWidth}
                      onChange={(e) => setPenStrokeWidth(e.target.value)}
                      inputMode="decimal"
                      disabled={busy}
                      aria-label="Ink outline width"
                      className="h-8 w-16 rounded-md bg-white text-[10px]"
                    />
                  )}
                  {penOutline && <span className="text-[9px] text-[#778481]">px stroke</span>}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <span className="text-[10px] leading-snug text-[#657671]">Layer</span>
                  <div
                    className="flex flex-wrap gap-0.5 rounded-[9px] bg-[#e0e8e2] p-0.5"
                    role="group"
                    aria-label="Paint layer placement"
                  >
                    {(
                      [
                        { key: "above", label: "Above art" },
                        { key: "behind", label: "Behind art" },
                      ] as Array<{ key: "above" | "behind"; label: string }>
                    ).map((l) => (
                      <button
                        key={l.key}
                        type="button"
                        aria-pressed={penBehind === (l.key === "behind")}
                        disabled={busy}
                        onClick={() => setPenBehind(l.key === "behind")}
                        className={`rounded-md px-2 py-1 text-[9px] font-medium transition-colors disabled:opacity-50 ${
                          penBehind === (l.key === "behind")
                            ? "bg-white text-[#087f74] shadow-[0_1px_4px_rgba(18,47,34,0.13)]"
                            : "text-[#657671] hover:text-[#183837]"
                        }`}
                      >
                        {l.label}
                      </button>
                    ))}
                  </div>
                  {penBehind && (
                    <span className="text-[9px] leading-snug text-[#778481]">
                      may be hidden by opaque layers above
                    </span>
                  )}
                </div>
              </div>
            )}
            <div className="mt-2.5 flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]"
                onClick={() => setPendingDraw(null)}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
                disabled={busy || !paletteEntries.length}
                onClick={confirmDraw}
              >
                <PenTool className="size-3.5" aria-hidden />
                {busy
                  ? "Creating…"
                  : penMode === "artwork"
                    ? "Create artwork"
                    : "Create region"}
              </Button>
            </div>
          </PopoverContent>
        </Popover>
        {hasBundle && view !== "master" && (
          <div className="pointer-events-none absolute bottom-5 left-5 z-[2] rounded-md bg-white/90 px-2.5 py-1.5 text-[9px] text-[#607767]">
            {canvasTag(view)}
          </div>
        )}
      </div>

      {/* Artwork path Save/Cancel bar (Task 33): explicit confirmation — Save
          submits the edited path (server validates + recompiles), Cancel
          discards the in-editor changes. Rendered as a SIBLING BELOW the
          canvas: inside the canvas box the tool overlay (z-3, full canvas
          area) intercepts every pointer event, so an in-canvas bar would be
          visible but unclickable whenever a tool is active. */}
      {tool === "artnode" && artPath && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <span className="text-[10px] text-[#657671]">
            Shape <code className="font-mono text-[10px] text-[#183837]">{artPath.context.shapeId}</code>
            {artPath.dirty ? " · modified" : " · unchanged"}
          </span>
          {foreignDraft && (
            <span
              className="rounded-md border border-[#e5d8a8] bg-[#faf5e3] px-2 py-1 text-[10px] font-medium text-[#8a6d1a]"
              role="status"
            >
              Draft from another project — switch back to {artPath.context.projectId} to save it, or
              Cancel to discard
            </span>
          )}
          {staleDraftBase && (
            <span
              className="rounded-md border border-[#f0c8c3] bg-[#fbeeeb] px-2 py-1 text-[10px] font-medium text-[#ba463f]"
              role="status"
            >
              Base changed: loaded from {artPath.context.baseRevision}, project now at{" "}
              {project?.currentRevision}
              {draftShapeChangedInBetween ? " — this shape was edited in between" : ""}
            </span>
          )}
          <Button
            size="sm"
            variant="outline"
            className="h-7 rounded-md bg-white px-2.5 text-[10px]"
            disabled={busy || !canUndo || foreignDraft}
            onClick={undoArtDraft}
            title="Undo (Ctrl+Z)"
          >
            <Undo2 className="mr-1 size-3" />
            Undo
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 rounded-md bg-white px-2.5 text-[10px]"
            disabled={busy || !canRedo || foreignDraft}
            onClick={redoArtDraft}
            title="Redo (Ctrl+Shift+Z)"
          >
            <Redo2 className="mr-1 size-3" />
            Redo
          </Button>
          <Button
            size="sm"
            className="h-7 rounded-md bg-[#087f74] px-3 text-[10px] font-semibold text-white hover:bg-[#056a60]"
            disabled={busy || !artPath.dirty || foreignDraft}
            onClick={requestArtSave}
          >
            Save path
          </Button>
          {/* Task 40C — instant shape-level layer moves on the CURRENT
              revision (full recompile for filled shapes, ink fast path
              without one; never a paint.z bump). Independent of the geometry
              draft: the draft survives with the usual conflict chips if the
              reorder publishes first. A filled move over manual gameplay
              topology fails server-side until confirmed — the dialog below. */}
          <Button
            size="sm"
            variant="outline"
            className="h-7 rounded-md bg-white px-2.5 text-[10px]"
            disabled={busy || foreignDraft}
            title="Move the shape one layer backward (behind its neighbour)"
            onClick={() => moveShapeOrder("backward")}
          >
            <ArrowDown className="mr-1 size-3" />
            Move backward
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 rounded-md bg-white px-2.5 text-[10px]"
            disabled={busy || foreignDraft}
            title="Move the shape one layer forward (in front of its neighbour)"
            onClick={() => moveShapeOrder("forward")}
          >
            <ArrowUp className="mr-1 size-3" />
            Move forward
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-7 rounded-md bg-white px-3 text-[10px]"
            disabled={busy}
            onClick={() => {
              setArtPath(null);
              setArtHistory(null);
            }}
          >
            Cancel
          </Button>
        </div>
      )}

      {/* Task 40A/40B — shape-addressed appearance editor for the picked
          shape. Ink: stroke color/width/opacity. Filled: Fill (color +
          preserve shading) and Outline (color, width — 0 removes). Gameplay
          geometry is untouched; a fill edit may move the shape's regions to
          the number group whose answer color IS the new fill. */}
      {tool === "artnode" && artPath && styleContext && styleDraft && !busy && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-[#e1e5df] bg-white px-3 py-2">
          {styleKind === "ink" ? (
            <>
              <span className="text-[10px] font-semibold text-[#183837]">Stroke</span>
              <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
                Color
                <input
                  type="color"
                  aria-label="Stroke color"
                  value={styleDraft.fill}
                  onChange={(e) => setStyleDraft({ ...styleDraft, fill: e.target.value.toUpperCase() })}
                  className="h-6 w-8 cursor-pointer rounded border border-[#e1e5df] bg-white"
                />
                <code className="font-mono text-[10px] text-[#183837]">{styleDraft.fill}</code>
              </label>
              <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
                Width
                <input
                  type="number"
                  aria-label="Stroke width"
                  min={0.4}
                  max={8}
                  step={0.1}
                  value={styleDraft.strokeWidth}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    if (Number.isFinite(v)) setStyleDraft({ ...styleDraft, strokeWidth: Math.min(8, Math.max(0.4, v)) });
                  }}
                  className="h-6 w-14 rounded border border-[#e1e5df] px-1.5 text-[10px]"
                />
                px
              </label>
              <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
                Opacity
                <input
                  type="number"
                  aria-label="Stroke opacity"
                  min={0}
                  max={100}
                  step={5}
                  value={Math.round(styleDraft.opacity * 100)}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    if (Number.isFinite(v)) setStyleDraft({ ...styleDraft, opacity: Math.min(100, Math.max(0, v)) / 100 });
                  }}
                  className="h-6 w-14 rounded border border-[#e1e5df] px-1.5 text-[10px]"
                />
                %
              </label>
              <span className="text-[9px] text-[#778481]">appearance only — tap targets stay the same</span>
            </>
          ) : (
            <>
              <span className="text-[10px] font-semibold text-[#183837]">Fill</span>
              <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
                Color
                <input
                  type="color"
                  aria-label="Fill color"
                  value={styleDraft.fill}
                  onChange={(e) => setStyleDraft({ ...styleDraft, fill: e.target.value.toUpperCase() })}
                  className="h-6 w-8 cursor-pointer rounded border border-[#e1e5df] bg-white"
                />
                <code className="font-mono text-[10px] text-[#183837]">{styleDraft.fill}</code>
              </label>
              <label className="flex items-center gap-1 text-[10px] text-[#657671]">
                <input
                  type="checkbox"
                  aria-label="Preserve shading"
                  checked={styleDraft.preserveShading}
                  onChange={(e) => setStyleDraft({ ...styleDraft, preserveShading: e.target.checked })}
                  className="h-3.5 w-3.5 accent-[#087f74]"
                />
                Preserve shading
              </label>
              <span className="text-[10px] font-semibold text-[#183837]">Outline</span>
              <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
                Color
                <input
                  type="color"
                  aria-label="Outline color"
                  value={styleDraft.stroke || "#29383E"}
                  onChange={(e) => setStyleDraft({ ...styleDraft, stroke: e.target.value.toUpperCase() })}
                  className="h-6 w-8 cursor-pointer rounded border border-[#e1e5df] bg-white"
                />
                <code className="font-mono text-[10px] text-[#183837]">{styleDraft.stroke || "—"}</code>
              </label>
              <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
                Width
                <input
                  type="number"
                  aria-label="Outline width"
                  min={0}
                  max={8}
                  step={0.1}
                  value={styleDraft.strokeWidth}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    // 0 = remove the outline; nonzero values follow the 0.4
                    // floor (the same compiler minimum as ink strokes).
                    if (Number.isFinite(v)) setStyleDraft({ ...styleDraft, strokeWidth: v === 0 ? 0 : Math.min(8, Math.max(0.4, v)) });
                  }}
                  className="h-6 w-14 rounded border border-[#e1e5df] px-1.5 text-[10px]"
                />
                px
              </label>
              <span className="text-[9px] text-[#778481]">
                {styleDraft.strokeWidth === 0 ? "width 0 removes the outline" : "fill may move the number group; outlines never touch gameplay"}
              </span>
            </>
          )}
          {staleStyleBase && (
            <span
              className="rounded-md border border-[#e5d8a8] bg-[#faf5e3] px-2 py-1 text-[10px] font-medium text-[#8a6d1a]"
              role="status"
            >
              Base changed: style loaded from {styleContext?.baseRevision}, project now at{" "}
              {project?.currentRevision}
              {styleChangedInBetween ? " — this shape's appearance was edited in between" : ""}
            </span>
          )}
          <div className="ml-auto flex gap-2">
            <Button
              size="sm"
              variant="outline"
              className="h-7 rounded-md bg-white px-2.5 text-[10px]"
              disabled={!styleDirty}
              onClick={resetStyle}
            >
              Reset
            </Button>
            <Button
              size="sm"
              className="h-7 rounded-md bg-[#087f74] px-3 text-[10px] font-semibold text-white hover:bg-[#056a60]"
              disabled={busy || foreignDraft || !styleDirty}
              onClick={saveStyle}
              title={staleStyleBase ? "Explicitly apply this style on top of the current revision" : undefined}
            >
              {/* The rebase target is only advertised while a save is
                  actually possible — a clean draft cannot save, and the
                  label would collide with the geometry bar's own rebase
                  button on the same shape. */}
              {staleStyleBase && styleDirty ? `Save against ${project?.currentRevision}` : "Save appearance"}
            </Button>
          </div>
        </div>
      )}

      {/* Compact difficulty mini-panel (contract §5) — full metrics live in
          the right panel (DifficultyPanel). */}
      {hasBundle && view !== "master" && view !== "zoomlab" && (
        <DifficultyMini
          raw={bundle?.manifest.difficulty}
          validated={bundle?.manifest.difficultyValidatedByPlaytest}
        />
      )}

      {/* Tools row (inspect view only) */}
      {view === "inspect" && hasBundle && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
          <div
            className="flex flex-wrap gap-0.5 rounded-[9px] bg-[#e8ece5] p-1"
            role="group"
            aria-label="Region editing tool"
          >
            {(
              [
                { key: "select", label: "Select", icon: MousePointer2 },
                { key: "cut", label: "Cut", icon: Scissors },
                { key: "pen", label: "Pen", icon: PenTool },
                { key: "node", label: "Node", icon: Waypoints },
                { key: "artnode", label: "Art node", icon: PenTool },
              ] as Array<{ key: StudioTool; label: string; icon: typeof MousePointer2 }>
            ).map((t) => (
              <button
                key={t.key}
                type="button"
                aria-pressed={tool === t.key}
                disabled={busy}
                title={
                  t.key === "node"
                    ? "Gameplay Boundary — drag the shared boundary between two regions"
                    : t.key === "artnode"
                      ? "Artwork Path — edit the Bézier anchors of the underlying source shape"
                      : undefined
                }
                onClick={() => {
                  // Switching tools discards any pending cut/pen/node confirm.
                  setPendingCut(null);
                  setPendingDraw(null);
                  setNodeEdge(null);
                  setNodeConfirmOpen(false);
                  setArtPath(null);
                  setArtHistory(null);
                  setTool(t.key);
                }}
                className={`flex min-h-11 items-center gap-1.5 rounded-md px-3 text-[10px] font-medium transition-colors disabled:opacity-50 ${
                  tool === t.key
                    ? "bg-white text-[#087f74] shadow-[0_1px_4px_rgba(18,47,34,0.13)]"
                    : "text-[#657671] hover:text-[#183837]"
                }`}
              >
                <t.icon className="size-3.5" aria-hidden />
                {t.label}
              </button>
            ))}
          </div>
          <span className="text-[10px] text-[#778481]" role="status">
            {busy ? "Waiting for the current job…" : TOOL_HINTS[tool]}
          </span>
          {/* Boundary confirm trigger (contract A): visible once the Node tool
              has a selected boundary. */}
          {tool === "node" && nodeEdge && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 rounded-md border-[#cfe6db] bg-[#e5f3ed] text-[10px] font-semibold text-[#126e5e] hover:bg-[#d9ede2] disabled:opacity-50"
              aria-label="Apply boundary edit"
              disabled={busy}
              onClick={() => setNodeConfirmOpen(true)}
            >
              <Waypoints className="size-3.5" aria-hidden />
              Apply
            </Button>
          )}
        </div>
      )}

      {/* Cut confirmation (contract §2) — modal: the board behind stays
          visible, Cancel / Escape / overlay-click all cancel the stroke. */}
      <AlertDialog
        open={toolActive && !!pendingCut}
        onOpenChange={(open) => {
          if (!open) setPendingCut(null);
        }}
      >
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              Cut region {pendingCut?.regionId} along the drawn line?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              The line crosses the region from outside one edge to outside the opposite edge. Creates a new revision; both
              pieces stay playable tap targets and the new boundary is drawn as a dashed subdivision edge.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={confirmCut}
            >
              <Scissors className="size-3.5" aria-hidden />
              {busy ? "Cutting…" : "Cut region"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Node boundary confirmation (contract A) — the dragged anchors become
          the new shared boundary. Cancel / Escape discards the selection. */}
      <AlertDialog
        open={nodeConfirmOpen}
        onOpenChange={(open) => {
          if (!open) {
            setNodeConfirmOpen(false);
            setNodeEdge(null);
          }
        }}
      >
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              Rebuild the shared boundary between {nodeEdge?.left} and {nodeEdge?.right}?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              Moving the anchors redraws the boundary between the two regions; both stay playable tap targets and the
              boundary keeps its line style.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={confirmNode}
            >
              <Waypoints className="size-3.5" aria-hidden />
              {busy ? "Applying…" : "Apply boundary"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Artwork path save confirmation (Task 33) — explicit consequence
          statement: the recompile re-derives paint and gameplay surfaces. */}
      <AlertDialog
        open={artSaveOpen}
        onOpenChange={(open) => {
          if (!open) setArtSaveOpen(false);
        }}
      >
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              Save the edited artwork path?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              Shape {artPath?.context.shapeId} is recompiled from the edited outline: the finished illustration
              updates and its playable surfaces are re-derived — manual subdivisions inside the changed
              outline are superseded. The shape id and object ownership are preserved, and undo is
              revision history.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy || foreignDraft}
              onClick={submitArtSave}
            >
              <PenTool className="size-3.5" aria-hidden />
              {busy ? "Saving…" : "Save path"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Stale-base conflict (review follow-up): the project advanced past the
          revision this draft was loaded from. The artist gets an explicit
          choice — save against the CURRENT revision (the edit is keyed by the
          stable shape id), keep editing, or discard — never an implicit
          rebase or a silent dismissal. */}
      <AlertDialog
        open={artConflictOpen}
        onOpenChange={(open) => {
          if (!open) setArtConflictOpen(false);
        }}
      >
        <AlertDialogContent className="rounded-xl border-[#f0c8c3] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              The artwork changed since this shape was loaded
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              Shape {artPath?.context.shapeId} was loaded from revision {artPath?.context.baseRevision}, but
              the project is now at {project?.currentRevision}.{" "}
              {draftShapeChangedInBetween
                ? "This shape's outline was also edited in between — saving replaces those changes with your draft."
                : "This shape's outline is unchanged in between, so saving applies your edit on top of the latest revision."}{" "}
              Your draft stays available if you keep editing.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel
              className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]"
              onClick={() => {
                setArtPath(null);
                setArtHistory(null);
              }}
            >
              Discard draft
            </AlertDialogCancel>
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">
              Keep editing
            </AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy || foreignDraft}
              onClick={submitArtSave}
            >
              <PenTool className="size-3.5" aria-hidden />
              Save against {project?.currentRevision}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Task 40C review — topology-rebuild confirmation (backend-gated):
          ANY full-recompile operation over manually authored gameplay asks
          first. Copy comes from the hook (per operation). */}
      <AlertDialog open={!!gatedOperation} onOpenChange={(o) => !o && dismissGatedOperation()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              {gatedOperation?.title}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              {gatedOperation?.body}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={confirmGatedOperation}
            >
              {gatedOperation?.confirmLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Palette bar */}
      {hasBundle && view !== "master" && view !== "colored" && view !== "zoomlab" && (
        <div className="mt-2.5">
          {/* Coloring mode switch (play test) */}
          {view === "play" && (
            <div className="mb-1.5 flex flex-wrap items-center justify-between gap-1.5">
              <div
                className="flex flex-wrap gap-0.5 rounded-[9px] bg-[#e8ece5] p-0.5"
                role="group"
                aria-label="Coloring mode"
              >
                {(["number", "memory", "free"] as BoardMode[]).map((m) => (
                  <button
                    key={m}
                    type="button"
                    aria-pressed={boardMode === m}
                    onClick={() => setBoardMode(m)}
                    className={`rounded-md px-2.5 py-1.5 text-[10px] font-medium capitalize transition-colors ${
                      boardMode === m
                        ? "bg-white text-[#087f74] shadow-[0_1px_4px_rgba(18,47,34,0.13)]"
                        : "text-[#657671] hover:text-[#183837]"
                    }`}
                  >
                    {m === "number" ? "Numbered" : m === "memory" ? "Memory" : "Free"}
                  </button>
                ))}
              </div>
              <span className="text-[9px] text-[#778481]" role="status">
                {MODE_HINTS[boardMode]}
              </span>
            </div>
          )}
          <div className="mb-1 flex flex-wrap items-center justify-between gap-1.5 text-[10px] text-[#778481]">
            <span id="studio-progress-text">
              {boardState
                ? `${completed} / ${total} regions filled · ${mistakes} incorrect attempts`
                : "Choose a color to test"}
            </span>
            <div className="flex flex-wrap gap-0.5">
              {view === "play" && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 rounded-md border-[#cfe6db] bg-white px-2 text-[9px] font-semibold text-[#126e5e] hover:bg-[#f0f7f3] disabled:opacity-50"
                  disabled={!playtestReady}
                  onClick={recordPlaytestRun}
                  aria-label="Record playtest result"
                  title={
                    playtestComplete
                      ? "Record this completed run as a play-test difficulty sample"
                      : "Fill every region to enable play-test recording"
                  }
                >
                  <Timer className="size-3" aria-hidden />
                  {playtestRecorded
                    ? "Playtest recorded"
                    : `Record playtest${playClockLabel != null ? ` (${playClockLabel})` : ""}`}
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                className="h-6 rounded-md px-2 text-[9px] text-[#657671] hover:bg-[#f0f7f3]"
                onClick={findRegion}
              >
                <Search className="size-3" aria-hidden />
                Find region
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 rounded-md px-2 text-[9px] text-[#657671] hover:bg-[#f0f7f3]"
                onClick={undoFill}
              >
                <Undo2 className="size-3" aria-hidden />
                Undo fill
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 rounded-md px-2 text-[9px] text-[#657671] hover:bg-[#f0f7f3]"
                onClick={resetTest}
              >
                <RotateCcw className="size-3" aria-hidden />
                Reset test
              </Button>
            </div>
          </div>
          <div
            className="studio-scroll flex items-center gap-2 overflow-x-auto py-2 pb-3"
            role="group"
            aria-label="Color palette"
            aria-describedby="studio-progress-text"
          >
            {paletteEntries.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => setSwatch(p.id)}
                title={`${p.name} · group ${p.id}${boardMode === "free" ? " · quick color" : ""}`}
                aria-label={`${p.name}, group ${p.id}${boardMode === "free" ? ", use as free color" : ""}`}
                aria-pressed={selectedPaletteId === p.id}
                className={`flex size-9 shrink-0 items-center justify-center rounded-full border-2 border-white text-[11px] font-semibold shadow-[0_0_0_1px_rgba(189,199,189,0.5)] transition-transform ${
                  selectedPaletteId === p.id ? "scale-105 shadow-[0_0_0_3px_#087f74]" : "hover:scale-105"
                }`}
                style={{ background: p.hex, color: swatchTextColor(p.hex) }}
              >
                {p.number}
              </button>
            ))}
          </div>

          {/* Free color cluster — true custom colors (contract §4). Palette
              swatches in free mode double as quick access (they load their hex
              as the active color inside VectorBoard.setPalette). */}
          {view === "play" && boardMode === "free" && (
            <div className="mt-1.5 rounded-lg border border-[#dce4dd] bg-white/70 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[10px] font-semibold text-[#657671]">Custom color</span>
                <input
                  type="color"
                  value={freeHexValid ? freeHex : "#66AA33"}
                  onChange={(e) => applyFreeColor(e.target.value.toUpperCase())}
                  className="h-8 w-10 shrink-0 cursor-pointer rounded-md border border-[#e1e5df] bg-white p-0.5"
                  aria-label="Pick a custom free color"
                />
                <Input
                  value={freeHex}
                  onChange={(e) => setFreeHex(e.target.value.trim().toUpperCase())}
                  onBlur={() => {
                    const t = freeHex.trim().toUpperCase();
                    setFreeHex(/^[0-9A-F]{6}$/.test(t) ? `#${t}` : t);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") applyFreeColor(freeHex);
                  }}
                  placeholder="#RRGGBB"
                  spellCheck={false}
                  autoComplete="off"
                  className="h-8 w-24 rounded-md bg-white font-mono text-xs"
                  aria-label="Custom color hex value"
                  aria-invalid={!freeHexValid}
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8 rounded-md border-[#e1e5df] bg-white text-[10px] hover:bg-[#f0f7f3]"
                  disabled={!freeHexValid}
                  onClick={() => applyFreeColor(freeHex)}
                >
                  Apply
                </Button>
                {boardState?.customColor && (
                  <span className="ml-auto flex items-center gap-1.5 text-[9px] text-[#657671]">
                    Active
                    <span
                      className="inline-block size-3.5 rounded-full border border-[#c3ced4]"
                      style={{ background: boardState.customColor }}
                      aria-hidden
                    />
                    <span className="font-mono">{boardState.customColor}</span>
                  </span>
                )}
              </div>
              {recentColors.length > 0 && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  <span className="text-[9px] text-[#778481]">Recent</span>
                  {recentColors.map((c) => (
                    <button
                      key={c}
                      type="button"
                      onClick={() => applyFreeColor(c)}
                      title={`Reuse ${c}`}
                      aria-label={`Reuse recent color ${c}`}
                      className={`size-6 shrink-0 rounded-full border shadow-[0_0_0_1px_rgba(189,199,189,0.5)] transition-transform hover:scale-110 ${
                        freeColor === c ? "border-[#087f74] shadow-[0_0_0_2px_#087f74]" : "border-white"
                      }`}
                      style={{ background: c }}
                    />
                  ))}
                </div>
              )}
              <p className="mt-1.5 text-[9px] leading-relaxed text-[#778481]">
                In free mode every region accepts this color — no palette checks, no mistakes. Palette swatches
                above stay clickable as quick access.
              </p>
            </div>
          )}
        </div>
      )}

      {/* Job area */}
      <div className="mt-3" aria-live="polite">
        <div className="flex items-center justify-between gap-2 text-[10px]">
          <span
            className={
              jobFailed
                ? "text-[#ba463f]"
                : busy
                  ? "font-medium text-[#087f74]"
                  : "text-[#778481]"
            }
          >
            {connectionError ? "Could not connect to the local studio service." : jobMessage}
          </span>
          <strong className={jobFailed ? "text-[#ba463f]" : "text-[#087f74]"}>
            {busy ? `${Math.round((job.progress ?? 0) * 100)}%` : ""}
          </strong>
        </div>
        <Progress
          value={(job.progress ?? 0) * 100}
          className="mt-2 h-1 bg-[#e1e5df] [&>div]:bg-[#087f74]"
          aria-hidden
        />
      </div>
    </section>
  );
}
