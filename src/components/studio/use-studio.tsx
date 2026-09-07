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

import { loadBundle, VectorBoard, type BoardState, type Bundle } from "@/lib/detailed-board";
import {
  activateRevision,
  buildDraft,
  createProject,
  generateMaster,
  generateSvgMaster,
  getConfig,
  getProject,
  listProjects,
  loadSample,
  loadSvgSample,
  patchProject,
  promoteReference as promoteReferenceApi,
  runEdit as apiRunEdit,
  sendChat as apiSendChat,
  submitReview as apiSubmitReview,
  uploadImage as apiUploadImage,
  uploadSvgMaster as apiUploadSvgMaster,
  type BuildSettings,
  type EditAction,
  type EditPayload,
  type GenerateSource,
  type ImageQuality,
  type Project,
  type Revision,
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
  setBuildSetting: (key: keyof BuildSettings, value: number | string) => void;
  // SVG master generation inputs
  svgPrompt: string;
  setSvgPrompt: (v: string) => void;
  svgAspect: "1024x1536" | "1536x1024" | "1024x1024";
  setSvgAspect: (v: "1024x1536" | "1536x1024" | "1024x1024") => void;
  svgPaidConsent: boolean;
  setSvgPaidConsent: (v: boolean) => void;
  // actions
  openProjectById: (pid: string) => Promise<void>;
  createNewProject: () => Promise<void>;
  saveBrief: () => Promise<void>;
  uploadFile: (file: File, role: "reference" | "master") => Promise<void>;
  uploadSvgFile: (file: File) => Promise<void>;
  promoteReference: () => Promise<void>;
  loadSampleProject: () => Promise<void>;
  loadSvgSampleProject: () => Promise<void>;
  sendChat: () => Promise<void>;
  generate: () => Promise<void>;
  generateSvg: () => Promise<void>;
  build: () => Promise<void>;
  runEdit: (action: EditAction, extra?: Partial<EditPayload>) => Promise<void>;
  clearSelection: () => void;
  startPlacing: () => void;
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
  useEffect(() => void (configRef.current = config), [config]);
  useEffect(() => void (buildSettingsRef.current = buildSettings), [buildSettings]);
  useEffect(() => void (revisionSelectRef.current = revisionSelect), [revisionSelect]);

  const busy = isBusyProject(project);
  const revision = project?.revisions.find((r) => r.id === project.currentRevision) ?? null;
  const selectionInfo = selected.size
    ? `${selected.size} selected · ${[...selected].slice(0, 4).join(", ")}${selected.size > 4 ? "…" : ""}`
    : "Tap regions to inspect or select them. Drag to pan.";

  // ---------------------------------------------------------------- helpers

  const setProjectSync = useCallback((next: Project | null) => {
    projectRef.current = next;
    setProject(next);
    if (next) {
      const current = next.revisions.find((r) => r.id === next.currentRevision);
      const fallback = current?.id ?? next.revisions[0]?.id ?? "";
      setRevisionSelect((prev) => (prev && next.revisions.some((r) => r.id === prev) ? prev : fallback));
    } else {
      setRevisionSelect("");
    }
  }, []);

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

  /** Create the VectorBoard whenever a new bundle arrives (revision change). */
  useEffect(() => {
    if (!bundle || !svgRef.current) return;
    const board = new VectorBoard(svgRef.current, bundle, {
      persist: false,
      mode: "number",
      onChange: handleBoardChange,
    });
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
      setProjectSync(p);
      syncFields(p);
      setChatInput("");
      setPaidConsent(false);
      viewRef.current = p.currentRevision ? "colored" : "master";
      setView(viewRef.current);
      try {
        localStorage.setItem(PROJECT_STORAGE_KEY, p.id);
      } catch {
        /* storage unavailable */
      }
      await refreshList();
      if (p.currentRevision) await mountBoard();
      if (isBusyProject(p)) void poll(p.id);
    },
    [clearBoard, mountBoard, poll, refreshList, setProjectSync, syncFields]
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
    async (action: EditAction, extra: Partial<EditPayload> = {}) => {
      const p = projectRef.current;
      if (!p) throw new Error("Create a project first.");
      if (!selectedRef.current.size) throw new Error("Select at least one region in Edit regions.");
      if (isBusyProject(p)) throw new Error("Wait for the current job.");
      if (!p.currentRevision) throw new Error("Build the vector regions first.");
      const body: EditPayload = {
        base_revision: p.currentRevision,
        action,
        region_ids: [...selectedRef.current],
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

  const clearSelection = useCallback(() => {
    selectedRef.current = new Set();
    setSelected(new Set());
    placingRef.current = false;
    setPlacing(false);
    highlightSelection();
  }, [highlightSelection]);

  const startPlacing = useCallback(() => {
    if (selectedRef.current.size !== 1) {
      toast("Select exactly one region first.");
      return;
    }
    placingRef.current = true;
    setPlacing(true);
    toast("Tap a roomy point inside the selected region.");
  }, []);

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
      viewRef.current = next;
      setView(next);
      void mountBoard();
    },
    [mountBoard]
  );

  const setBuildSetting = useCallback((key: keyof BuildSettings, value: number | string) => {
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
      boardRef.current?.setPalette(id);
    },
    findRegion: () => boardRef.current?.nextRegion(),
    undoFill: () => boardRef.current?.undo(),
    resetTest: () => boardRef.current?.reset(),
    svgRef,
    canvasRef,
    zoomRef,
    messagesRef,
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
    clearSelection,
    startPlacing,
    activateSelectedRevision,
    submitReviewNote,
    switchView,
    zoomIn: () => boardRef.current?.zoom(1.4),
    zoomOut: () => boardRef.current?.zoom(1 / 1.4),
    fit: () => boardRef.current?.fit(),
  };

  return <StudioContext.Provider value={api}>{children}</StudioContext.Provider>;
}
