"use client";

/**
 * useStudio — page-level state + data flow for the Color Duel Art Studio.
 * Faithful port of the vanilla-JS `web/studio.mjs` controller onto React state.
 *
 * Board specifics (imperative, non-React):
 *  - the <svg> element is rendered ONCE by React with no children; VectorBoard
 *    owns its DOM children and is re-created only when the revision changes;
 *  - `board.paint` is wrapped so "Edit regions" taps select instead of fill;
 *  - `board.clientToArt` is wrapped to capture the last tap point (Place number).
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { loadBundle, VectorBoard, type BoardMode, type BoardState, type Bundle } from "@/lib/detailed-board";
import {
  activateRevision,
  buildDraft,
  cancelProjectJob as apiCancelProjectJob,
  createProject,
  createGenerationSession,
  discardGenerationSession,
  generateMaster,
  generateSvgMaster,
  getConfig,
  getProject,
  listProjects,
  loadSample,
  loadSvgSample,
  listGenerationSessions,
  analyzeSessionReference as apiAnalyzeSessionReference,
  commitSessionArtwork as apiCommitSessionArtwork,
  compileSession as apiCompileSession,
  convertSession as apiConvertSession,
  generateSessionArtwork as apiGenerateSessionArtwork,
  mutateSessionPlan as apiMutateSessionPlan,
  planChatSession as apiPlanChatSession,
  planSession as apiPlanSession,
  regenerateSessionObject as apiRegenerateSessionObject,
  updateSessionSettings as apiUpdateSessionSettings,
  uploadSessionSource as apiUploadSessionSource,
  patchProject,
  promoteReference as promoteReferenceApi,
  recordPlaytest as apiRecordPlaytest,
  runEdit as apiRunEdit,
  optimizeDifficulty as apiOptimizeDifficulty,
  sendChat as apiSendChat,
  submitReview as apiSubmitReview,
  uploadImage as apiUploadImage,
  uploadSvgMaster as apiUploadSvgMaster,
  type BuildSettings,
  type EditAction,
  type EditPayload,
  type GenerateSource,
  type GenerationSessionFull,
  type ImageQuality,
  type PlaytestRecordBody,
  type Project,
  type Revision,
  type SessionTier,
  type StudioConfig,
} from "@/lib/studio-api";

export type StudioView = "master" | "colored" | "numbered" | "play" | "inspect" | "zoomlab";

export const VIEW_LABELS: Record<StudioView, string> = {
  master: "Master",
  colored: "Vector",
  numbered: "Numbered",
  play: "Play test",
  inspect: "Edit regions",
  zoomlab: "Zoom lab",
};

const PROJECT_STORAGE_KEY = "studio-project";
const RECENT_COLORS_KEY = "cd-studio-recent-colors";
const FREE_HEX_RE = /^#[0-9A-Fa-f]{6}$/;
export type StudioTool = "select" | "cut" | "pen" | "node";
const DEFAULT_BRIEF =
  "An original detailed woodland treehouse beside a waterfall, with warm lanterns, a winding staircase and flowering plants. Clear contours, coherent architecture, rich shading. No text, UI, palette or gameplay numbers.";

const isBusyProject = (p: Project | null): boolean =>
  p?.job?.status === "queued" || p?.job?.status === "running";

export interface StudioApi {
  // data
  config: StudioConfig | null;
  projects: Project[];
  project: Project | null;
  busy: boolean;
  revision: Revision | null;
  connectionError: boolean;
  // board
  view: StudioView;
  bundle: Bundle | null;
  boardState: BoardState | null;
  selected: Set<string>;
  placing: boolean;
  selectionInfo: string;
  /** Imperative VectorBoard instance (exposed for the Cut/Pen drawing overlay). */
  boardRef: React.RefObject<VectorBoard | null>;
  // board tools (active in the inspect view)
  tool: StudioTool;
  setTool: (t: StudioTool) => void;
  /** Cut a region along a drawn line: edit action "cut" with the path d. */
  cutRegion: (regionId: string, d: string) => Promise<void>;
  /** Create a region from a drawn closed shape: edit action "draw". With
   *  paint=true (artwork pen) the shape also becomes finished artwork — a
   *  paint.json path with a stable shapeId, fill, optional ink outline and
   *  z-order; the region references it via masterShapeId so recolor works.
   *  Drawn ABOVE the art it carves the covered regions (surfaces never
   *  overlap); a custom color joins/creates the palette group with that
   *  answer color (P0.2: shared swatches are never mutated).
   *  paint=false (region pen) stays a gameplay-only white tap target. */
  drawRegion: (
    d: string,
    paletteId: number,
    group?: string,
    paint?: boolean,
    color?: string,
    strokeWidth?: number,
    zBehind?: boolean
  ) => Promise<void>;
  /** Rebuild the shared boundary between two regions (dragged anchors):
   *  edit action "node" with the new open boundary path d. */
  nodeEdit: (regionIds: [string, string], d: string) => Promise<void>;
  // free color (true custom colors, contract §4)
  freeColor: string;
  setBoardFreeColor: (hex: string) => void;
  recentColors: string[];
  boardMode: BoardMode;
  setBoardMode: (mode: BoardMode) => void;
  /** Record a completed play-test run and refresh the difficulty profile
   *  in-context (contract B) — no job, no board remount. */
  recordPlaytest: (payload: PlaytestRecordBody) => Promise<void>;
  // board action wrappers (safe to call from event handlers)
  setBoardPalette: (id: number) => void;
  findRegion: () => void;
  undoFill: () => void;
  resetTest: () => void;
  // refs shared with imperative DOM
  svgRef: React.RefObject<SVGSVGElement | null>;
  canvasRef: React.RefObject<HTMLDivElement | null>;
  zoomRef: React.RefObject<HTMLSpanElement | null>;
  messagesRef: React.RefObject<HTMLDivElement | null>;
  // editable fields
  titleInput: string;
  briefInput: string;
  chatInput: string;
  paidConsent: boolean;
  rightsConfirmed: boolean;
  generationSource: GenerateSource;
  quality: ImageQuality;
  editPalette: string;
  objectGroup: string;
  revisionSelect: string;
  buildSettings: BuildSettings;
  // setters used by panels
  setTitleInput: (v: string) => void;
  setBriefInput: (v: string) => void;
  setChatInput: (v: string) => void;
  setPaidConsent: (v: boolean) => void;
  setRightsConfirmed: (v: boolean) => void;
  setGenerationSource: (v: GenerateSource) => void;
  setQuality: (v: ImageQuality) => void;
  setEditPalette: (v: string) => void;
  setObjectGroup: (v: string) => void;
  setRevisionSelect: (v: string) => void;
  setBuildSetting: (key: keyof BuildSettings, value: number | string | boolean) => void;
  // SVG master generation inputs
  svgPrompt: string;
  setSvgPrompt: (v: string) => void;
  svgAspect: "1024x1536" | "1536x1024" | "1024x1024";
  setSvgAspect: (v: "1024x1536" | "1536x1024" | "1024x1024") => void;
  svgPaidConsent: boolean;
  setSvgPaidConsent: (v: boolean) => void;
  svgGenMode: "single" | "multistage";
  setSvgGenMode: (v: "single" | "multistage") => void;
  svgTargetRegions: number;
  setSvgTargetRegions: (v: number) => void;
  // actions
  openProjectById: (pid: string) => Promise<void>;
  createNewProject: () => Promise<void>;
  saveBrief: () => Promise<Project>;
  uploadFile: (file: File, role: "reference" | "master") => Promise<void>;
  uploadSvgFile: (file: File) => Promise<void>;
  promoteReference: () => Promise<void>;
  loadSampleProject: () => Promise<void>;
  loadSvgSampleProject: () => Promise<void>;
  sendChat: () => Promise<void>;
  generate: () => Promise<void>;
  generateSvg: () => Promise<void>;
  build: () => Promise<void>;
  runEdit: (action: EditAction, extra?: Partial<EditPayload>, regionIds?: string[]) => Promise<void>;
  /** Task 27 — Optimize Difficulty: gameplay-only move toward a tier on the
   *  current revision (new immutable revision; artwork stays untouched). */
  optimizeDifficulty: (tier: "easy" | "medium" | "hard" | "master") => Promise<void>;
  // Task 28 — creation flow (landing + shells; Tasks 29/30 fill the workspaces)
  /** Which creation shell is open (null = landing). */
  creationMode: "ai" | "image" | null;
  /** Latest resumable generation session (null when none / unavailable). */
  activeSession: GenerationSessionFull | null;
  /** "create" while the project has no artwork or the flow is resumed over the editor. */
  centerView: "editor" | "create";
  setCreationFlow: (mode: "ai" | "image" | null) => void;
  startAiCreation: (
    prompt: string,
    tier: SessionTier,
    aspect: "1024x1536" | "1536x1024" | "1024x1024"
  ) => Promise<void>;
  startImageCreation: (
    path: "reference" | "convert",
    tier: SessionTier,
    fidelity: "stylized" | "balanced" | "faithful",
    file?: File
  ) => Promise<void>;
  discardActiveSession: () => Promise<void>;
  resumeCreationSession: () => void;
  closeCreateWorkspace: () => void;
  // Task 29 — Create-with-AI session steps
  planSceneWithAi: (confirmPaid: boolean) => Promise<void>;
  revisePlanWithAi: (instruction: string, confirmPaid: boolean) => Promise<void>;
  editScenePlan: (mutations: unknown[]) => Promise<void>;
  generateArtwork: (confirmPaid: boolean) => Promise<void>;
  regenerateObject: (objectId: string, instructions: string, confirmPaid: boolean) => Promise<void>;
  recompileSessionArtwork: () => Promise<void>;
  convertImage: (confirmPaid: boolean) => Promise<void>;
  analyzeReference: (confirmPaid: boolean) => Promise<void>;
  changeImageSettings: (settings: {
    fidelity?: "stylized" | "balanced" | "faithful";
    requested_difficulty?: SessionTier;
  }) => Promise<void>;
  commitArtworkToEditor: () => Promise<void>;
  cancelJob: () => Promise<void>;
  clearSelection: () => void;
  startPlacing: () => void;
  /** Select a single region by id, switch to the board view and zoom to it (QA drill-down). */
  inspectRegion: (id: string) => void;
  activateSelectedRevision: () => Promise<void>;
  submitReviewNote: (note: string) => Promise<void>;
  switchView: (next: StudioView) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  fit: () => void;
}

const StudioContext = createContext<StudioApi | null>(null);

export function useStudioContext(): StudioApi {
  const ctx = useContext(StudioContext);
  if (!ctx) throw new Error("useStudioContext must be used inside <StudioProvider>");
  return ctx;
}

export function StudioProvider({ children }: { children: React.ReactNode }) {
  const [config, setConfig] = useState<StudioConfig | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [view, setView] = useState<StudioView>("master");
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [boardState, setBoardState] = useState<BoardState | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [placing, setPlacing] = useState(false);
  const [connectionError, setConnectionError] = useState(false);

  // editable fields
  const [titleInput, setTitleInput] = useState("New illustrated world");
  const [briefInput, setBriefInput] = useState("");
  const [chatInput, setChatInput] = useState("");
  const [paidConsent, setPaidConsent] = useState(false);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [generationSource, setGenerationSource] = useState<GenerateSource>("brief");
  const [quality, setQuality] = useState<ImageQuality>("medium");
  const [editPalette, setEditPalette] = useState("1");
  const [objectGroup, setObjectGroup] = useState("roof");
  const [revisionSelect, setRevisionSelect] = useState("");
  const [buildSettings, setBuildSettings] = useState<BuildSettings>({
    target_regions: 650,
    palette_colors: 32,
    paint_colors: 80,
    max_edge: 1024,
    compactness: 10,
    min_region_pixels: 35,
    min_label_radius: 3,
    ink_threshold: 40,
    backend: "spline-local",
    curve_tolerance: 1.0,
    corner_angle_deg: 60,
  });
  const [svgPrompt, setSvgPrompt] = useState("");
  const [svgAspect, setSvgAspect] = useState<"1024x1536" | "1536x1024" | "1024x1024">("1024x1536");
  const [svgPaidConsent, setSvgPaidConsent] = useState(false);
  const [svgGenMode, setSvgGenMode] = useState<"single" | "multistage">("single");
  const [svgTargetRegions, setSvgTargetRegions] = useState(300);
  // Board tools (inspect view) + free color + board mode (play view)
  const [tool, setTool] = useState<StudioTool>("select");
  const [freeColor, setFreeColor] = useState("#66AA33");
  const [recentColors, setRecentColors] = useState<string[]>([]);
  const [boardMode, setBoardMode] = useState<BoardMode>("number");
  // Task 28 — creation flow: which shell is open and the project's latest
  // un-committed generation session (restored across refreshes so an active
  // session is never "lost" just because the page reloaded).
  const [creationMode, setCreationMode] = useState<"ai" | "image" | null>(null);
  const [activeSession, setActiveSession] = useState<GenerationSessionFull | null>(null);
  const [showCreateWorkspace, setShowCreateWorkspace] = useState(false);

  // refs mirroring state for closures created once
  const projectRef = useRef<Project | null>(null);
  const viewRef = useRef<StudioView>("master");
  const selectedRef = useRef<Set<string>>(new Set());
  const placingRef = useRef(false);
  const boardRef = useRef<VectorBoard | null>(null);
  const bundleRef = useRef<Bundle | null>(null);
  const loadedRevisionRef = useRef<string | null>(null);
  const loadingTokenRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const titleRef = useRef(titleInput);
  const briefRef = useRef(briefInput);
  const chatRef = useRef(chatInput);
  const paidRef = useRef(paidConsent);
  const rightsRef = useRef(rightsConfirmed);
  const generationSourceRef = useRef(generationSource);
  const qualityRef = useRef(quality);
  const buildSettingsRef = useRef(buildSettings);
  const revisionSelectRef = useRef(revisionSelect);
  const svgPromptRef = useRef(svgPrompt);
  const svgAspectRef = useRef(svgAspect);
  const svgPaidRef = useRef(svgPaidConsent);
  const svgGenModeRef = useRef(svgGenMode);
  const svgTargetRef = useRef(svgTargetRegions);
  const toolRef = useRef<StudioTool>("select");
  const boardModeRef = useRef<BoardMode>("number");
  const freeColorRef = useRef(freeColor);
  const appliedPendingPbsRef = useRef<string>("");
  const configRef = useRef<StudioConfig | null>(null);

  // DOM refs
  const svgRef = useRef<SVGSVGElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const zoomRef = useRef<HTMLSpanElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);

  // keep field refs in sync
  useEffect(() => void (titleRef.current = titleInput), [titleInput]);
  useEffect(() => void (briefRef.current = briefInput), [briefInput]);
  useEffect(() => void (chatRef.current = chatInput), [chatInput]);
  useEffect(() => void (paidRef.current = paidConsent), [paidConsent]);
  useEffect(() => void (rightsRef.current = rightsConfirmed), [rightsConfirmed]);
  useEffect(() => void (generationSourceRef.current = generationSource), [generationSource]);
  useEffect(() => void (qualityRef.current = quality), [quality]);
  useEffect(() => void (svgPromptRef.current = svgPrompt), [svgPrompt]);
  useEffect(() => void (svgAspectRef.current = svgAspect), [svgAspect]);
  useEffect(() => void (svgPaidRef.current = svgPaidConsent), [svgPaidConsent]);
  useEffect(() => void (svgGenModeRef.current = svgGenMode), [svgGenMode]);
  useEffect(() => void (svgTargetRef.current = svgTargetRegions), [svgTargetRegions]);
  useEffect(() => void (toolRef.current = tool), [tool]);
  useEffect(() => void (boardModeRef.current = boardMode), [boardMode]);
  useEffect(() => void (freeColorRef.current = freeColor), [freeColor]);
  useEffect(() => void (configRef.current = config), [config]);
  useEffect(() => void (buildSettingsRef.current = buildSettings), [buildSettings]);
  useEffect(() => void (revisionSelectRef.current = revisionSelect), [revisionSelect]);

  const busy = isBusyProject(project);
  const revision = project?.revisions.find((r) => r.id === project.currentRevision) ?? null;
  // Task 28 — workspace routing: artwork (a revision or an imported master)
  // opens the editor; an empty project opens the Create Artwork flow (the
  // landing while no session exists, the session shell once one does). The
  // override lets the editor's resume banner re-open a session workspace on
  // top of the editor without losing either side.
  const hasArtwork = !!(project?.currentRevision || project?.master);
  const centerView: "editor" | "create" = showCreateWorkspace || !hasArtwork ? "create" : "editor";
  const selectionInfo = selected.size
    ? `${selected.size} selected · ${[...selected].slice(0, 4).join(", ")}${selected.size > 4 ? "…" : ""}`
    : "Tap regions to inspect or select them. Drag to pan.";

  // ---------------------------------------------------------------- helpers

  /** Prefill build settings from multi-stage generation hints (applied once
   *  per distinct value — polls re-deliver the same object and must not fight
   *  user edits). Called from setProjectSync (an event flow), never render. */
  const applyPendingBuildSettings = useCallback((pbs: { auto_subdivide: boolean; target_regions: number }) => {
    const key = JSON.stringify(pbs);
    if (appliedPendingPbsRef.current === key) return;
    appliedPendingPbsRef.current = key;
    setBuildSettings((prev) => ({
      ...prev,
      auto_subdivide: !!pbs.auto_subdivide,
      target_regions: Math.min(1600, Math.max(100, Math.round(pbs.target_regions) || prev.target_regions)),
    }));
  }, []);

  const setProjectSync = useCallback(
    (next: Project | null) => {
      projectRef.current = next;
      setProject(next);
      if (next?.pendingBuildSettings) applyPendingBuildSettings(next.pendingBuildSettings);
      if (next) {
        const current = next.revisions.find((r) => r.id === next.currentRevision);
        const fallback = current?.id ?? next.revisions[0]?.id ?? "";
        setRevisionSelect((prev) => (prev && next.revisions.some((r) => r.id === prev) ? prev : fallback));
      } else {
        setRevisionSelect("");
      }
    },
    [applyPendingBuildSettings]
  );

  /** Overwrite editable fields from the server (title / brief). */
  const syncFields = useCallback((p: Project) => {
    setTitleInput(p.title);
    setBriefInput(p.brief);
  }, []);

  const refreshList = useCallback(async (): Promise<Project[]> => {
    const items = await listProjects();
    setProjects(items);
    return items;
  }, []);

  /** Task 28 — latest resumable generation session of the project (committed
   *  and canceled sessions are history, not active work). Degrades to null
   *  silently: the landing/shell must render even without session access. */
  const refreshSessions = useCallback(async (pid: string): Promise<GenerationSessionFull | null> => {
    try {
      const { sessions } = await listGenerationSessions(pid);
      const active =
        sessions.find((s) => !["committed", "canceled"].includes(s.status)) ?? null;
      setActiveSession(active);
      return active;
    } catch {
      return null;
    }
  }, []);

  /** Pure DOM update — selection text is derived during render from `selected`. */
  const highlightSelection = useCallback(() => {
    const board = boardRef.current;
    if (!board) return;
    for (const [id, el] of board.elements) el.classList.toggle("selected-region", selectedRef.current.has(id));
  }, []);

  const clearBoard = useCallback(() => {
    loadingTokenRef.current++;
    boardRef.current?.destroy();
    boardRef.current = null;
    bundleRef.current = null;
    setBundle(null);
    setBoardState(null);
    loadedRevisionRef.current = null;
    selectedRef.current = new Set();
    setSelected(new Set());
    placingRef.current = false;
    setPlacing(false);
  }, []);

  // ------------------------------------------------------------- board view

  const applyBoardView = useCallback(() => {
    const board = boardRef.current;
    if (!board) return;
    board.setPreview(viewRef.current === "colored" || viewRef.current === "inspect");
    highlightSelection();
  }, [highlightSelection]);

  const handleBoardChange = useCallback(
    (state: BoardState, reason: string) => {
      if (zoomRef.current) zoomRef.current.textContent = `${Math.round(state.zoom * 100)}%`;
      if (reason !== "viewport") setBoardState(state);
      if (reason === "wrong-color") toast("That region needs a different palette group.");
      if (viewRef.current === "inspect") highlightSelection();
    },
    [highlightSelection]
  );

  // runEdit is called from inside the board paint wrapper; declare it via ref
  // first so the wrapper (created once per board) always calls the latest one.
  const runEditRef = useRef<(action: EditAction, extra?: Partial<EditPayload>) => Promise<void>>(async () => {
    /* replaced below */
  });

  /** Wrap board interactions for authoring mode (selection instead of paint). */
  const wrapBoard = useCallback(
    (board: VectorBoard) => {
      const normalPaint = board.paint.bind(board);
      board.paint = (id: string | null) => {
        if (viewRef.current !== "inspect") return normalPaint(id);
        if (!id) return "ignored";
        if (placingRef.current) {
          const p = board.lastTapPoint;
          if (!p) return "ignored";
          placingRef.current = false;
          setPlacing(false);
          void runEditRef.current("label", { x: p.x, y: p.y }).catch((e: Error) => toast(e.message));
          return "inspected";
        }
        const next = new Set(selectedRef.current);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        selectedRef.current = next;
        setSelected(next);
        if (next.size === 1) {
          const reg = board.regions.get(id);
          if (reg) {
            setEditPalette(String(reg.paletteId));
            setObjectGroup(!reg.objectId || reg.objectId === "unassigned" ? "roof" : reg.objectId);
          }
        }
        highlightSelection();
        return "inspected";
      };
      const convert = board.clientToArt.bind(board);
      board.clientToArt = (x: number, y: number, matrix?: DOMMatrix | null) => {
        const pt = convert(x, y, matrix);
        board.lastTapPoint = pt;
        return pt;
      };
    },
    [highlightSelection]
  );

  /** Create the VectorBoard whenever a new bundle arrives (revision change).
   *  The board re-mounts per revision, so the underpainting cache rebuilds
   *  per bundle (its key is the artwork version). Mode + free color persist
   *  across re-mounts through the refs. */
  useEffect(() => {
    if (!bundle || !svgRef.current) return;
    const board = new VectorBoard(svgRef.current, bundle, {
      persist: false,
      mode: boardModeRef.current,
      onChange: handleBoardChange,
    });
    if (boardModeRef.current === "free" && FREE_HEX_RE.test(freeColorRef.current)) {
      try {
        board.setFreeColor(freeColorRef.current);
      } catch {
        /* validated above — cannot throw */
      }
    }
    boardRef.current = board;
    wrapBoard(board);
    applyBoardView();
    return () => {
      board.destroy();
      if (boardRef.current === board) boardRef.current = null;
    };
  }, [bundle]);

  // keep board preview/selection in sync with the active view
  useEffect(() => {
    applyBoardView();
  }, [view, bundle, applyBoardView]);

  // auto-scroll chat messages
  useEffect(() => {
    const el = messagesRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [project?.messages.length, project?.id]);

  // re-measure labels when the canvas resizes
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => boardRef.current?.updateLabelVisibility());
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Load recent free colors from localStorage (outside the board — the board
  // itself stays memory-only by design). Async body so no storage read happens
  // during render/hydration.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const raw = localStorage.getItem(RECENT_COLORS_KEY);
        const parsed: unknown = raw ? JSON.parse(raw) : [];
        if (!cancelled && Array.isArray(parsed))
          setRecentColors(parsed.filter((c): c is string => typeof c === "string" && FREE_HEX_RE.test(c)).slice(0, 10));
      } catch {
        /* storage unavailable */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ----------------------------------------------------------------- polling

  const mountBoard = useCallback(async () => {
    const p = projectRef.current;
    const r = p?.revisions.find((x) => x.id === p.currentRevision);
    if (!p || !r || loadedRevisionRef.current === r.id) return;
    clearBoard();
    const token = loadingTokenRef.current;
    try {
      const b = await loadBundle(p.id, r.id);
      if (token !== loadingTokenRef.current) return;
      bundleRef.current = b;
      loadedRevisionRef.current = r.id;
      setBundle(b);
    } catch (e) {
      toast((e as Error).message);
    }
  }, [clearBoard]);

  const pollRef = useRef<(pid: string) => Promise<void>>(async () => {
    /* replaced below */
  });
  const poll = useCallback(
    async (pid: string) => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      try {
        const next = await getProject(pid);
        if (projectRef.current?.id !== pid) return;
        const changed = projectRef.current.currentRevision !== next.currentRevision;
        setProjectSync(next);
        if (!isBusyProject(next)) syncFields(next);
        if (changed && next.currentRevision) {
          viewRef.current = "colored";
          setView("colored");
          await mountBoard();
          await refreshList();
        } else if (
          !isBusyProject(next) &&
          next.currentRevision &&
          loadedRevisionRef.current !== next.currentRevision
        ) {
          // job finished between the POST response and the first poll — mount the missed revision
          viewRef.current = "colored";
          setView("colored");
          await mountBoard();
          await refreshList();
        }
        if (isBusyProject(next)) {
          pollTimerRef.current = setTimeout(() => void pollRef.current(pid), 900);
        } else if (next.job?.status === "failed") {
          toast(next.job.message || "Job failed");
        }
      } catch (e) {
        toast(`Job polling stopped: ${(e as Error).message}`);
      }
    },
    [mountBoard, refreshList, setProjectSync, syncFields]
  );
  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  const job = useCallback(
    async (post: () => Promise<{ jobId: string }>) => {
      const pid = projectRef.current?.id;
      if (!pid) throw new Error("Create a project first.");
      await post();
      setProjectSync(await getProject(pid));
      void poll(pid);
    },
    [poll, setProjectSync]
  );

  // ---------------------------------------------------------------- projects

  const openProject = useCallback(
    async (p: Project) => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      clearBoard();
      appliedPendingPbsRef.current = "";
      setProjectSync(p);
      syncFields(p);
      setChatInput("");
      setPaidConsent(false);
      setCreationMode(null);
      setShowCreateWorkspace(false);
      setActiveSession(null);
      viewRef.current = p.currentRevision ? "colored" : "master";
      setView(viewRef.current);
      try {
        localStorage.setItem(PROJECT_STORAGE_KEY, p.id);
      } catch {
        /* storage unavailable */
      }
      await refreshList();
      void refreshSessions(p.id);
      if (p.currentRevision) await mountBoard();
      if (isBusyProject(p)) void poll(p.id);
    },
    [clearBoard, mountBoard, poll, refreshList, refreshSessions, setProjectSync, syncFields]
  );

  const ensureProject = useCallback(async () => {
    if (projectRef.current) return projectRef.current;
    const created = await createProject("New illustrated world");
    await openProject(created);
    return created;
  }, [openProject]);

  const openProjectById = useCallback(
    async (pid: string) => {
      await openProject(await getProject(pid));
    },
    [openProject]
  );

  const createNewProject = useCallback(async () => {
    const created = await createProject("New illustrated world");
    await openProject(created);
    toast("New project created.");
  }, [openProject]);

  // -------------------------------------------------------------------- init

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await getConfig();
        if (cancelled) return;
        setConfig(cfg);
        const items = await refreshList();
        let saved: Project | undefined;
        try {
          const stored = localStorage.getItem(PROJECT_STORAGE_KEY);
          saved = stored ? items.find((p) => p.id === stored) : undefined;
        } catch {
          /* storage unavailable */
        }
        const target = saved ?? items[0];
        if (target) {
          await openProject(target);
        } else {
          const created = await createProject("Cascade Treehouse", DEFAULT_BRIEF);
          await openProject(created);
        }
      } catch (e) {
        if (cancelled) return;
        setConnectionError(true);
        toast((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  // ----------------------------------------------------------------- actions

  const saveBrief = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    const next = await patchProject(p.id, {
      title: titleRef.current.trim() || "Untitled artwork",
      brief: briefRef.current,
    });
    setProjectSync(next);
    await refreshList();
    return next;
  }, [refreshList, setProjectSync]);

  const uploadFile = useCallback(
    async (file: File, role: "reference" | "master") => {
      const p = await ensureProject();
      const next = await apiUploadImage(p.id, file, role, rightsRef.current);
      setProjectSync(next);
      syncFields(next);
      if (role === "master") {
        viewRef.current = "master";
        setView("master");
      }
      toast(
        role === "master" ? "Master ready. Build a vector draft." : "Reference added. Use chat to develop an original brief."
      );
    },
    [ensureProject, setProjectSync, syncFields]
  );

  const promoteReference = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    const next = await promoteReferenceApi(p.id, rightsRef.current);
    setProjectSync(next);
    viewRef.current = "master";
    setView("master");
    toast("Reference promoted to master.");
  }, [setProjectSync]);

  const loadSampleProject = useCallback(async () => {
    const p = await ensureProject();
    const next = await loadSample(p.id);
    setProjectSync(next);
    viewRef.current = "master";
    setView("master");
    toast("Example loaded. Build vector draft to convert it locally.");
  }, [ensureProject, setProjectSync]);

  const loadSvgSampleProject = useCallback(async () => {
    const p = await ensureProject();
    const next = await loadSvgSample(p.id);
    setProjectSync(next);
    syncFields(next);
    viewRef.current = "master";
    setView("master");
    toast("Curved SVG master loaded — curves preserved, never rasterized. Build the draft.");
  }, [ensureProject, setProjectSync, syncFields]);

  const uploadSvgFile = useCallback(
    async (file: File) => {
      const p = await ensureProject();
      const next = await apiUploadSvgMaster(p.id, file, rightsRef.current);
      setProjectSync(next);
      syncFields(next);
      viewRef.current = "master";
      setView("master");
      toast("SVG master imported (sanitized, curves preserved). Build the vector draft.");
    },
    [ensureProject, setProjectSync, syncFields]
  );

  const sendChat = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    const message = chatRef.current.trim();
    if (!message) throw new Error("Write a direction for the AI first.");
    await saveBrief();
    await job(() => apiSendChat(p.id, { message, include_reference: true, confirm_paid: paidRef.current }));
    setChatInput("");
    setPaidConsent(false);
  }, [job, saveBrief]);

  const generate = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    await saveBrief();
    await job(() =>
      generateMaster(p.id, {
        source: generationSourceRef.current,
        prompt: briefRef.current,
        quality: qualityRef.current,
        size: "1024x1536",
        confirm_paid: paidRef.current,
      })
    );
    setPaidConsent(false);
    viewRef.current = "master";
    setView("master");
  }, [job, saveBrief]);

  const generateSvg = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    const prompt = svgPromptRef.current.trim();
    if (!prompt) throw new Error("Describe the SVG artwork first.");
    if (!svgPaidRef.current) throw new Error("Tick the paid-request consent first.");
    if (!configRef.current?.ai?.configured) throw new Error("AI provider is not configured on this server.");
    await saveBrief();
    await job(() =>
      generateSvgMaster(p.id, {
        prompt,
        aspect: svgAspectRef.current,
        include_reference: true,
        confirm_paid: true,
        mode: svgGenModeRef.current,
        target_regions: svgTargetRef.current,
      })
    );
    setSvgPrompt("");
    setSvgPaidConsent(false);
    viewRef.current = "master";
    setView("master");
  }, [job, saveBrief]);

  const build = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    await saveBrief();
    await job(() => buildDraft(p.id, buildSettingsRef.current));
  }, [job, saveBrief]);

  const runEdit = useCallback(
    async (action: EditAction, extra: Partial<EditPayload> = {}, regionIds?: string[]) => {
      const p = projectRef.current;
      if (!p) throw new Error("Create a project first.");
      if (isBusyProject(p)) throw new Error("Wait for the current job.");
      if (!p.currentRevision) throw new Error("Build the vector regions first.");
      // Cut/draw supply their own region ids (the target region / none);
      // everything else uses the current selection.
      const ids = regionIds ?? [...selectedRef.current];
      if (!ids.length && action !== "draw") throw new Error("Select at least one region in Edit regions.");
      const body: EditPayload = {
        base_revision: p.currentRevision,
        action,
        region_ids: ids,
        ...extra,
      };
      await job(() => apiRunEdit(p.id, body));
      selectedRef.current = new Set();
      setSelected(new Set());
      highlightSelection();
    },
    [highlightSelection, job]
  );
  useEffect(() => {
    runEditRef.current = runEdit;
  }, [runEdit]);

  const optimizeDifficulty = useCallback(
    async (tier: "easy" | "medium" | "hard" | "master") => {
      const p = projectRef.current;
      if (!p) throw new Error("Create a project first.");
      if (isBusyProject(p)) throw new Error("Wait for the current job.");
      if (!p.currentRevision) throw new Error("Build the vector regions first.");
      await job(() => apiOptimizeDifficulty(p.id, { base_revision: p.currentRevision!, tier }));
      selectedRef.current = new Set();
      setSelected(new Set());
    },
    [job]
  );

  // ------------------------------------------------- creation flow (Task 28)

  /** Open one of the creation shells (or null → back to the landing). */
  const setCreationFlow = useCallback((mode: "ai" | "image" | null) => {
    setCreationMode(mode);
    if (mode) setShowCreateWorkspace(true);
  }, []);

  /** Create with AI: creates a REAL generation session (free — no AI call
   *  happens until a paid step is explicitly confirmed inside the upcoming
   *  workspace) and stores the prompt as the project brief. */
  const startAiCreation = useCallback(
    async (
      prompt: string,
      tier: SessionTier,
      aspect: "1024x1536" | "1536x1024" | "1024x1024"
    ) => {
      const p = await ensureProject();
      briefRef.current = prompt;
      setBriefInput(prompt);
      await saveBrief();
      await createGenerationSession(p.id, { mode: "ai_chat", requested_difficulty: tier, prompt, aspect });
      await refreshSessions(p.id);
      setCreationMode("ai");
      setShowCreateWorkspace(true);
    },
    [ensureProject, refreshSessions, saveBrief]
  );

  /** Create from Image (Task 30A): create the matching session, then store
   *  the image as the SESSION source (free, validated, refresh-proof) —
   *  BEFORE any paid call. The visible source is always the server asset;
   *  the project master is never touched by the draft. */
  const startImageCreation = useCallback(
    async (
      path: "reference" | "convert",
      tier: SessionTier,
      fidelity: "stylized" | "balanced" | "faithful",
      file?: File
    ) => {
      if (!file) throw new Error("Choose an image first — it is stored with the session before anything runs.");
      const p = projectRef.current ?? (await ensureProject());
      const session = await createGenerationSession(p.id, {
        mode: path === "reference" ? "image_reference" : "image_convert",
        requested_difficulty: tier,
        fidelity,
      });
      await apiUploadSessionSource(p.id, session.id, file);
      await refreshSessions(p.id);
      setCreationMode("image");
      setShowCreateWorkspace(true);
    },
    [ensureProject, refreshSessions]
  );

  /** Start over: discard the draft session entirely (nothing was generated,
   *  no revision exists — the artwork is untouched by definition). */
  const discardActiveSession = useCallback(async () => {
    const p = projectRef.current;
    const s = activeSession;
    if (!p || !s) return;
    await discardGenerationSession(p.id, s.id);
    const next = await refreshSessions(p.id);
    if (!next) {
      setCreationMode(null);
      if (!projectRef.current?.currentRevision && !projectRef.current?.master) {
        setShowCreateWorkspace(false);
      }
    }
  }, [activeSession, refreshSessions]);

  /** Re-open the active session's shell (editor resume banner). */
  const resumeCreationSession = useCallback(() => {
    if (!activeSession) return;
    setCreationMode(activeSession.mode === "ai_chat" ? "ai" : "image");
    setShowCreateWorkspace(true);
  }, [activeSession]);

  /** Leave the creation workspace (back to the editor when artwork exists,
   *  back to the landing when the project is still empty and session-less). */
  const closeCreateWorkspace = useCallback(() => {
    setShowCreateWorkspace(false);
    if (!activeSession) setCreationMode(null);
  }, [activeSession]);

  // --------------------------------------------- session steps (Task 29/31)

  /** Task 31 review fix — TWO distinct pieces of state:
   *  - requestInFlight: transient guard, true only while the HTTP request
   *    itself is on the wire.
   *  - pendingOperation: the operation identity (operation, sessionId, key)
   *    kept while the OUTCOME is unknown. A lost response does NOT clear it:
   *    resending the same logical action reuses the SAME key, so the backend
   *    replays the existing attempt instead of buying new work. It clears
   *    only when a terminal outcome is observed (via polling) or the session
   *    is discarded. The record is mirrored to localStorage so a page reload
   *    can still replay with the same identity. */
  const requestInFlightRef = useRef(false);
  const PENDING_OP_KEY = "cd-pending-operation";
  type PendingOp = { operation: string; sessionId: string; key: string };
  const pendingOpRef = useRef<PendingOp | null>(null);

  const setPendingOp = useCallback((op: PendingOp | null) => {
    pendingOpRef.current = op;
    try {
      if (op) localStorage.setItem(PENDING_OP_KEY, JSON.stringify(op));
      else localStorage.removeItem(PENDING_OP_KEY);
    } catch {
      /* storage unavailable — in-memory identity still guards this tab */
    }
  }, []);

  // Restore a pending operation after a page reload: the outcome is unknown,
  // so the next explicit action for the same session reuses the same key.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(PENDING_OP_KEY);
      if (raw) {
        const op = JSON.parse(raw) as PendingOp;
        if (op?.key && op?.sessionId) pendingOpRef.current = op;
      }
    } catch {
      /* corrupted record — ignore */
    }
  }, []);

  /** Fire one session step (paid gates live server-side) as an async job. */
  const runSessionStep = useCallback(
    async (operation: string, step: (key: string) => Promise<{ jobId: string }>) => {
      const p = projectRef.current;
      if (!p) throw new Error("Create a project first.");
      if (requestInFlightRef.current) throw new Error("A request is already in flight.");
      if (isBusyProject(p)) throw new Error("Wait for the current job.");
      if (!activeSession) throw new Error("No active generation session.");
      // Reuse the pending identity when the previous send for this session
      // has an UNKNOWN outcome (lost response) — same key, backend replays.
      const pending = pendingOpRef.current;
      const key =
        pending && pending.sessionId === activeSession.id
          ? pending.key
          : `${operation}:${activeSession.id}:${crypto.randomUUID()}`;
      setPendingOp({ operation, sessionId: activeSession.id, key });
      requestInFlightRef.current = true;
      try {
        await job(() => step(key));
        // The request was ACCEPTED (queued). The terminal outcome arrives via
        // polling; the post-job effect clears the pending identity then.
      } catch (err) {
        // A DEFINITE server rejection (our own API 4xx) means the work was
        // never admitted — drop the identity so the next confirm is a fresh
        // attempt. Network-type failures keep it (outcome still unknown).
        if (err instanceof Error && !/failed to fetch|network|load failed/i.test(err.message)) {
          setPendingOp(null);
        }
        throw err;
      } finally {
        requestInFlightRef.current = false;
      }
    },
    [activeSession, job, setPendingOp]
  );

  const planSceneWithAi = useCallback(
    (confirmPaid: boolean) => runSessionStep("plan", (key) => apiPlanSession(projectRef.current!.id, activeSession!.id, confirmPaid, key)),
    [runSessionStep]
  );

  const revisePlanWithAi = useCallback(
    (instruction: string, confirmPaid: boolean) =>
      runSessionStep("plan-chat", (key) => apiPlanChatSession(projectRef.current!.id, activeSession!.id, instruction, confirmPaid, key)),
    [runSessionStep]
  );

  const editScenePlan = useCallback(
    async (mutations: unknown[]) => {
      const p = projectRef.current;
      if (!p || !activeSession) throw new Error("No active generation session.");
      await apiMutateSessionPlan(p.id, activeSession.id, mutations);
      await refreshSessions(p.id);
    },
    [activeSession, refreshSessions]
  );

  const generateArtwork = useCallback(
    (confirmPaid: boolean) => runSessionStep("generate", (key) => apiGenerateSessionArtwork(projectRef.current!.id, activeSession!.id, confirmPaid, key)),
    [runSessionStep]
  );

  const regenerateObject = useCallback(
    (objectId: string, instructions: string, confirmPaid: boolean) =>
      runSessionStep("regenerate-object", (key) => apiRegenerateSessionObject(projectRef.current!.id, activeSession!.id, objectId, instructions, confirmPaid, key)),
    [runSessionStep]
  );

  const recompileSessionArtwork = useCallback(
    () => runSessionStep("compile", () => apiCompileSession(projectRef.current!.id, activeSession!.id)),
    [runSessionStep]
  );

  // Task 30 — image session steps
  const convertImage = useCallback(
    (confirmPaid: boolean) =>
      runSessionStep("convert", (key) => apiConvertSession(projectRef.current!.id, activeSession!.id, confirmPaid, key)),
    [runSessionStep]
  );

  const analyzeReference = useCallback(
    (confirmPaid: boolean) =>
      runSessionStep("reference-plan", (key) => apiAnalyzeSessionReference(projectRef.current!.id, activeSession!.id, confirmPaid, "", key)),
    [runSessionStep]
  );

  const changeImageSettings = useCallback(
    async (settings: { fidelity?: "stylized" | "balanced" | "faithful"; requested_difficulty?: SessionTier }) => {
      const p = projectRef.current;
      if (!p || !activeSession) throw new Error("No active generation session.");
      await apiUpdateSessionSettings(p.id, activeSession.id, settings);
      await refreshSessions(p.id);
    },
    [activeSession, refreshSessions]
  );

  /** Commit the verified session bundle: session → immutable revision → the
   *  normal editor (committed sessions drop out of active work on refresh). */
  const commitArtworkToEditor = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    if (isBusyProject(p)) throw new Error("Wait for the current job.");
    if (!activeSession) throw new Error("No active generation session.");
    const commitKey = `commit:${activeSession.id}:${crypto.randomUUID()}`;
    const res = await apiCommitSessionArtwork(p.id, activeSession.id, undefined, commitKey);
    const next = await getProject(p.id);
    setProjectSync(next);
    syncFields(next);
    setActiveSession(null);
    setCreationMode(null);
    setShowCreateWorkspace(false);
    viewRef.current = "colored";
    setView(viewRef.current);
    try {
      localStorage.setItem(PROJECT_STORAGE_KEY, p.id);
    } catch {
      /* storage unavailable */
    }
    await refreshList();
    await mountBoard();
    toast(`Artwork committed — revision v${res.revision.version}.`);
  }, [activeSession, mountBoard, refreshList, setProjectSync, syncFields]);

  /** Task 31B — request cancellation of the active job. Cooperative: stored
   *  first, worker stops before the next provider step. */
  const cancelJob = useCallback(async () => {
    const p = projectRef.current;
    if (!p) return;
    // Send the DISPLAYED job's id: a late cancel carrying a stale jobId is a
    // server-side no-op, so it can never cancel a newer attempt.
    const next = await apiCancelProjectJob(p.id, p.job?.id);
    setProjectSync(next);
    toast("Cancellation requested — no further generation steps will start.");
  }, [setProjectSync]);

  // Session truth lives server-side: after any session-step job reaches a
  // TERMINAL state (done / failed / canceled / interrupted), re-read the
  // active session so the workspace reflects the new stage without a manual
  // reload, and clear the pending operation identity (a retry after a KNOWN
  // outcome is an explicit new purchase and gets a fresh key).
  const jobId = project?.job?.id ?? null;
  const jobStatus = project?.job?.status ?? null;
  const lastRefreshedJobRef = useRef<string | null>(null);
  useEffect(() => {
    if (!jobId || !jobStatus) return;
    const terminal = jobStatus === "done" || jobStatus === "failed" ||
      jobStatus === "canceled" || jobStatus === "interrupted";
    if (!terminal) return;
    if (lastRefreshedJobRef.current === jobId) return;
    lastRefreshedJobRef.current = jobId;
    if (pendingOpRef.current) setPendingOp(null);
    if (projectRef.current && activeSession) void refreshSessions(projectRef.current.id);
  }, [jobId, jobStatus, activeSession, refreshSessions, setPendingOp]);

  const clearSelection = useCallback(() => {
    selectedRef.current = new Set();
    setSelected(new Set());
    placingRef.current = false;
    setPlacing(false);
    highlightSelection();
  }, [highlightSelection]);

  // ------------------------------------------------------- tools & free color

  /** Switch the active inspect-view tool (Select / Cut / Pen). */
  const changeTool = useCallback((next: StudioTool) => {
    // Leaving Select cancels any pending label placement (the drawing
    // overlay swallows board taps while a tool is active).
    if (next !== "select") {
      placingRef.current = false;
      setPlacing(false);
    }
    setTool(next);
  }, []);

  /** Cut the region under a drawn stroke — edit action "cut" (contract §2). */
  const cutRegion = useCallback(
    (regionId: string, d: string) => runEdit("cut", { d }, [regionId]),
    [runEdit]
  );

  /** Create a region from a drawn closed shape — edit action "draw"
   *  (contract §3). paint=true (artwork pen) also emits the paint.json path
   *  (stable shapeId/masterShapeId, fill, outline, z-order) so the shape is
   *  real finished artwork; paint=false stays a gameplay-only surface. */
  const drawRegion = useCallback(
    (d: string, paletteId: number, group?: string, paint?: boolean, color?: string, strokeWidth?: number, zBehind?: boolean) =>
      runEdit("draw", {
        d,
        palette_id: paletteId,
        ...(group ? { group } : {}),
        ...(paint ? { paint: true } : {}),
        ...(paint && color ? { color } : {}),
        ...(paint && strokeWidth ? { stroke_width: strokeWidth } : {}),
        ...(paint && zBehind ? { z_behind: true } : {}),
      }, []),
    [runEdit]
  );

  /** Rebuild the shared boundary between two regions from dragged
   *  anchors — edit action "node" (stage 3, contract A): region_ids are
   *  EXACTLY the edge's leftRegion/rightRegion, d the NEW open polyline. */
  const nodeEdit = useCallback(
    (regionIds: [string, string], d: string) => runEdit("node", { d }, regionIds),
    [runEdit]
  );

  /** Apply a custom free-mode color (contract §4) and remember it in the
   *  recent list (capped at 10, persisted in localStorage OUTSIDE the board). */
  const setBoardFreeColor = useCallback((hex: string) => {
    if (!FREE_HEX_RE.test(hex)) {
      toast("Enter a 6-digit hex color like #66AA33.");
      return;
    }
    const norm = hex.toUpperCase();
    try {
      boardRef.current?.setFreeColor(norm);
    } catch (e) {
      toast((e as Error).message);
      return;
    }
    setFreeColor(norm);
    setRecentColors((prev) => {
      const next = [norm, ...prev.filter((c) => c !== norm)].slice(0, 10);
      try {
        localStorage.setItem(RECENT_COLORS_KEY, JSON.stringify(next));
      } catch {
        /* storage unavailable */
      }
      return next;
    });
  }, []);

  /** Switch the board coloring mode (number / memory / free). */
  const changeBoardMode = useCallback((mode: BoardMode) => {
    try {
      boardRef.current?.setMode(mode);
      if (mode === "free" && FREE_HEX_RE.test(freeColorRef.current)) {
        boardRef.current?.setFreeColor(freeColorRef.current);
      }
      setBoardMode(mode);
    } catch (e) {
      toast((e as Error).message);
    }
  }, []);

  /** Record a completed play-test run (contract B) and refresh difficulty
   *  in-context: the active revision entry is patched immutably
   *  (currentRevision untouched) and the mounted bundle's manifest is updated
   *  IN PLACE — a new bundle object would remount the board and destroy the
   *  play progress, so only its manifest fields are swapped before a plain
   *  state update re-renders the difficulty panels. */
  const recordPlaytest = useCallback(
    async (payload: PlaytestRecordBody) => {
      const p = projectRef.current;
      if (!p) throw new Error("Create a project first.");
      const r = p.revisions.find((x) => x.id === p.currentRevision);
      if (!r) throw new Error("Build a draft first.");
      const res = await apiRecordPlaytest(p.id, r.id, payload);
      const difficulty = res.manifest?.difficulty;
      const validated = !!res.manifest?.difficultyValidatedByPlaytest;
      setProjectSync({
        ...p,
        revisions: p.revisions.map((rev) =>
          rev.id === r.id
            ? { ...rev, manifest: { ...(rev.manifest ?? {}), difficulty, difficultyValidatedByPlaytest: validated } }
            : rev
        ),
      });
      const b = bundleRef.current;
      if (b) {
        if (difficulty !== undefined) b.manifest.difficulty = difficulty;
        b.manifest.difficultyValidatedByPlaytest = validated;
      }
      toast("Playtest recorded — difficulty updated.");
    },
    [setProjectSync]
  );

  const startPlacing = useCallback(() => {
    if (selectedRef.current.size !== 1) {
      toast("Select exactly one region first.");
      return;
    }
    placingRef.current = true;
    setPlacing(true);
    toast("Tap a roomy point inside the selected region.");
  }, []);

  /** QA drill-down: make `id` the ONLY selected region, show the interactive
   *  board and zoom/focus it. Follows the same selection pattern as the paint
   *  wrapper (selectedRef + setSelected + highlightSelection + inspector inputs). */
  const inspectRegion = useCallback(
    (id: string) => {
      const board = boardRef.current;
      if (!board || !board.regions.has(id)) {
        toast("That region is not on the mounted board — open a board view first.");
        return;
      }
      if (viewRef.current !== "inspect") {
        viewRef.current = "inspect";
        setView("inspect");
      }
      const next = new Set([id]);
      selectedRef.current = next;
      setSelected(next);
      placingRef.current = false;
      setPlacing(false);
      const reg = board.regions.get(id);
      if (reg) {
        setEditPalette(String(reg.paletteId));
        setObjectGroup(!reg.objectId || reg.objectId === "unassigned" ? "roof" : reg.objectId);
      }
      board.focusRegion(id);
      highlightSelection();
    },
    [highlightSelection]
  );

  const activateSelectedRevision = useCallback(async () => {
    const p = projectRef.current;
    if (!p) throw new Error("Create a project first.");
    const target = revisionSelectRef.current;
    if (!target) throw new Error("No revision selected.");
    const next = await activateRevision(p.id, target);
    clearBoard();
    viewRef.current = "colored";
    setView("colored");
    setProjectSync(next);
    syncFields(next);
    await mountBoard();
    toast("Revision restored.");
  }, [clearBoard, mountBoard, setProjectSync, syncFields]);

  const submitReviewNote = useCallback(
    async (note: string) => {
      const p = projectRef.current;
      if (!p) throw new Error("Create a project first.");
      const r = p.revisions.find((x) => x.id === p.currentRevision);
      if (!r) throw new Error("Build a draft first.");
      const next = await apiSubmitReview(p.id, r.id, note);
      setProjectSync(next);
      toast("Visual review recorded.");
    },
    [setProjectSync]
  );

  const switchView = useCallback(
    (next: StudioView) => {
      if (next !== "master" && !projectRef.current?.currentRevision) {
        toast("Build the vector regions first.");
        return;
      }
      // Cut/Pen only make sense on the inspect board — reset when leaving.
      if (next !== "inspect" && toolRef.current !== "select") {
        setTool("select");
        toolRef.current = "select";
      }
      viewRef.current = next;
      setView(next);
      void mountBoard();
    },
    [mountBoard]
  );

  const setBuildSetting = useCallback((key: keyof BuildSettings, value: number | string | boolean) => {
    setBuildSettings((prev) => ({ ...prev, [key]: value }));
  }, []);

  const api: StudioApi = {
    config,
    projects,
    project,
    busy,
    revision,
    connectionError,
    view,
    bundle,
    boardState,
    selected,
    placing,
    selectionInfo,
    setBoardPalette: (id: number) => {
      const board = boardRef.current;
      if (!board) return;
      board.setPalette(id);
      // Free mode: the swatch loaded that palette entry's hex as the board's
      // active custom color — mirror it into the React state so the hex field
      // and color picker stay in sync with what taps will actually paint.
      if (board.mode === "free" && board.customColor) setFreeColor(board.customColor);
    },
    findRegion: () => boardRef.current?.nextRegion(),
    undoFill: () => boardRef.current?.undo(),
    resetTest: () => boardRef.current?.reset(),
    svgRef,
    canvasRef,
    zoomRef,
    messagesRef,
    boardRef,
    tool,
    setTool: changeTool,
    cutRegion,
    drawRegion,
    nodeEdit,
    freeColor,
    setBoardFreeColor,
    recentColors,
    boardMode,
    setBoardMode: changeBoardMode,
    recordPlaytest,
    titleInput,
    briefInput,
    chatInput,
    paidConsent,
    rightsConfirmed,
    generationSource,
    quality,
    editPalette,
    objectGroup,
    revisionSelect,
    buildSettings,
    setTitleInput,
    setBriefInput,
    setChatInput,
    setPaidConsent,
    setRightsConfirmed,
    setGenerationSource,
    setQuality,
    setEditPalette,
    setObjectGroup,
    setRevisionSelect,
    setBuildSetting,
    svgPrompt,
    setSvgPrompt,
    svgAspect,
    setSvgAspect,
    svgPaidConsent,
    setSvgPaidConsent,
    svgGenMode,
    setSvgGenMode,
    svgTargetRegions,
    setSvgTargetRegions,
    openProjectById,
    createNewProject,
    saveBrief,
    uploadFile,
    uploadSvgFile,
    promoteReference,
    loadSampleProject,
    loadSvgSampleProject,
    sendChat,
    generate,
    generateSvg,
    build,
    runEdit,
    optimizeDifficulty,
    creationMode,
    activeSession,
    centerView,
    setCreationFlow,
    startAiCreation,
    startImageCreation,
    discardActiveSession,
    resumeCreationSession,
    closeCreateWorkspace,
    planSceneWithAi,
    revisePlanWithAi,
    editScenePlan,
    generateArtwork,
    regenerateObject,
    recompileSessionArtwork,
    convertImage,
    analyzeReference,
    changeImageSettings,
    commitArtworkToEditor,
    cancelJob,
    clearSelection,
    startPlacing,
    inspectRegion,
    activateSelectedRevision,
    submitReviewNote,
    switchView,
    zoomIn: () => boardRef.current?.zoom(1.4),
    zoomOut: () => boardRef.current?.zoom(1 / 1.4),
    fit: () => boardRef.current?.fit(),
  };

  return <StudioContext.Provider value={api}>{children}</StudioContext.Provider>;
}
