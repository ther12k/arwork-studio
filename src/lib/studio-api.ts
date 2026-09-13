/**
 * Typed client for the Color Duel studio backend (Python FastAPI mini-service on :8765).
 * All calls go through the Caddy gateway using RELATIVE paths + ?XTransformPort=8765.
 * Every request carries the `X-Studio-Request: 1` header required by the backend.
 */

const PORT = 8765;

export interface AiConfig {
  configured: boolean;
  chatModel: string;
  imageModel: string;
}

export interface BackendInfo {
  id: string;
  name: string;
  kind: string;
  paid: boolean;
  available: boolean;
  notes: string;
}

export interface StudioConfig {
  ai: AiConfig;
  localOnly: boolean;
  version: string;
  supportedFormat: string;
  geometrySchema?: number;
  maxUploadMB: number;
  backends?: BackendInfo[];
}

export interface SvgMasterSummary {
  viewBox: number[];
  shapes: number;
  inkShapes: number;
  gradients: number;
  curvedShapes: number;
  commands: number;
  curvedCommands: number;
  hiddenShapes: number;
  removed: string[];
  warnings: string[];
  rasterized: boolean;
}

export interface ImageAsset {
  file: string;
  width: number;
  height: number;
  sha256: string;
  source: string;
  rightsConfirmed: boolean;
  createdAt: string;
  kind?: "image" | "svg";
  summary?: SvgMasterSummary;
}

export interface QaReport {
  passed: boolean;
  errors: string[];
  warnings: string[];
  playableRegions: number;
  fixedRegions: number;
  paletteGroups: number;
  paintPaths: number;
  paintSubshapes?: number;
  smallTargetCount?: number;
  precoloredAreaPercent?: number;
  humanReviewed: boolean;
  checkScope?: string;
  missingArea?: number;
  overlapArea?: number;
  roundtripEmptyPixels?: number | null;
  geometry?: {
    schema: number;
    masterAuthority: string;
    flattenedDerivation: string;
    backend: string;
    curveFitTolerance: number | null;
    curvedCommands: number;
    totalCommands: number;
    curvedCommandRatio: number;
    hitTestProbes: number;
    hitTestConflicts: number;
    hitTestZOverlaps?: number;
    labelOwnershipConflicts?: number;
    partitionToleranceAllowance: number;
    visibleRegionGeometry?: boolean;
    acceptanceRule?: string;
  };
  visualReview?: {
    required: boolean;
    separateFromGeometryTests: boolean;
    limitations: string[];
  };
  [key: string]: unknown;
}

/** Manifest block shapes the frontend patches in-context. The playtest
 *  route returns the full (loose) manifest with the difficulty section
 *  recomputed; the project listing itself does not embed revision manifests. */
export interface RevisionManifest {
  difficulty?: string | DifficultyProfile;
  difficultyValidatedByPlaytest?: boolean;
  [key: string]: unknown;
}

export interface Revision {
  id: string;
  version: string;
  createdAt: string;
  kind: string;
  sourceHash: string;
  regionCount: number;
  qa: QaReport;
  manifestUrl: string;
  /** In-context manifest patch (difficulty + playtest validation); present
   *  after a playtest recording, not part of the server listing. */
  manifest?: RevisionManifest;
}

export interface Job {
  id?: string;
  kind?: string;
  status?: "queued" | "running" | "done" | "failed" | "canceled" | "interrupted";
  progress?: number;
  message?: string;
  startedAt?: string;
  finishedAt?: string | null;
  cancelRequested?: boolean;
  sequence?: number;
  /** Task 31C — structured progress from the worker (never parsed from message). */
  stage?: string;
  completedObjects?: number;
  totalObjects?: number;
  currentObjectId?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface Project {
  id: string;
  title: string;
  brief: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
  aiUsage: unknown[];
  master: ImageAsset | null;
  reference: ImageAsset | null;
  revisions: Revision[];
  currentRevision: string | null;
  job: Job;
  /** Set by multi-stage SVG generation: the next Build should apply these
   *  (auto-subdivide to the requested region count). */
  pendingBuildSettings?: { auto_subdivide: boolean; target_regions: number } | null;
  /** Task 27 — report of the last Optimize Difficulty run (also persisted in
   *  the created revision's manifest under difficultyOptimization). */
  lastOptimization?: OptimizationReport;
}

/** Task 27 — structured before/after of one Optimize Difficulty run. The
 *  gameplay layer (regions/labels/object budgets) moved toward the tier;
 *  `artworkUnchanged` is always true (the engine hard-verifies it). */
export interface OptimizationReport {
  requestedTier: string;
  targetRegions: number;
  initial: { rating: string; score: number; regionCount: number };
  achieved: { rating: string; score: number; regionCount: number };
  merges: number;
  splits: number;
  budgetsAdjusted?: number;
  budgets?: Record<string, number>;
  outcome: "target-reached" | "best-safe-result" | "safe-ceiling" | "already-at-target";
  reasons: string[];
  changed: boolean;
  noop?: boolean;
  reverted?: string;
  revisionId?: string;
  baseRevision?: string;
  artworkUnchanged?: boolean;
  regionCountBefore: number;
  regionCountAfter: number;
  [key: string]: unknown;
}

export type GenerateSource = "brief" | "reference" | "current";
export type ImageQuality = "low" | "medium" | "high";

export interface BuildSettings {
  target_regions: number;
  palette_colors: number;
  paint_colors: number;
  max_edge: number;
  compactness: number;
  min_region_pixels: number;
  min_label_radius: number;
  ink_threshold: number;
  backend?: "spline-local" | "polygon-legacy";
  curve_tolerance?: number;
  corner_angle_deg?: number;
  /** Stage 2: deterministically split large regions until ~target_regions. */
  auto_subdivide?: boolean;
}

export type EditAction =
  | "merge"
  | "group"
  | "palette"
  | "recolor"
  | "label"
  | "decorate"
  | "split"
  | "cut"
  | "draw"
  | "node";

export type GeometryMode = "curved" | "legacy";

export interface RegionLabel {
  x: number;
  y: number;
  fontSize: number;
  minScreenPx: number;
  clearance: number;
}

export interface ModeRegion {
  id: string;
  paletteId: number;
  fillRule: "evenodd" | "nonzero";
  d: string;
  label: RegionLabel;
  bbox: number[];
  masterShapeId?: string;
}

export interface GeometryModePayload {
  mode: GeometryMode;
  viewBox: number[];
  stroke?: string;
  strokeWidth?: number;
  regions: ModeRegion[];
  backend?: string;
  flattenTolerance?: number;
  curveFitTolerance?: number | null;
  [key: string]: unknown;
}

export interface EditPayload {
  base_revision: string;
  action: EditAction;
  region_ids: string[];
  group?: string;
  palette_id?: number;
  x?: number;
  y?: number;
  /** 'recolor': the visible appearance color (#RRGGBB). Also 'draw' with
   * paint=true: the fill of the new artwork path (defaults to the number
   * group's swatch). Distinct from 'palette', which assigns the gameplay
   * number group. */
  color?: string;
  /** 'recolor': keep gradient shading (tinted toward the target color)
   * instead of replacing the fill. */
  preserve_shading?: boolean;
  /** 'cut': the cut polyline (open path, M/L only). 'draw': the closed
   *  region outline (M…Z). 'node': the NEW open boundary polyline between
   *  the two selected region ids. Pattern upstream accepts M/L/C/Q/Z. */
  d?: string;
  /** 'draw' artwork pen: also emit a paint.json path (stable shapeId +
   *  masterShapeId on the region, fill, z-order) so the drawn shape becomes
   *  finished artwork — recolor works like any imported shape. */
  paint?: boolean;
  /** 'draw' with paint: ink outline width on the paint path (0 = none). */
  stroke_width?: number;
  /** 'draw' with paint: place the new paint path behind the existing art
   *  (min z − 1) instead of on top (max z + 1). */
  z_behind?: boolean;
}

export interface SemanticObjectSubdivision {
  detailWeight?: number;
  minRegions?: number;
  preferredRegions?: number;
  maxRegions?: number;
  preserveSilhouette?: boolean;
}

export interface SemanticObjectGeneration {
  locked?: boolean;
  prompt?: string;
  provider?: string;
  [key: string]: unknown;
}

export interface SemanticObject {
  id: string;
  name: string;
  type?: string;
  role?: string;
  parentId?: string;
  shapeIds?: string[];
  subdivision?: SemanticObjectSubdivision;
  generation?: SemanticObjectGeneration;
}

export interface ObjectsFile {
  schemaVersion: number;
  objects: SemanticObject[];
}

export interface ObjectUpdateRequest {
  base_revision: string;
  object_id: string;
  name?: string;
  parent_id?: string;
  locked?: boolean;
  detail_weight?: number;
  min_regions?: number;
  preferred_regions?: number;
  max_regions?: number;
  order_action?: "bring_to_front" | "send_to_back" | "above" | "below";
  target_object_id?: string;
}

export const updateProjectObject = (
  pid: string,
  update: ObjectUpdateRequest
): Promise<{ jobId: string }> => post(`/projects/${pid}/objects`, update);

/** Difficulty metrics from the compiler analyzer (contract §5).
 *  Numbers unless noted; labelClearance and paletteAmbiguity are enums
 *  expressed as strings. Extra keys are tolerated for forward compat. */
export interface DifficultyMetrics {
  regionCount?: number;
  medianRegionArea?: number;
  tinyRegionPct?: number;
  requiredZoom?: number;
  labelClearance?: "ok" | "tight" | "conflict" | string;
  paletteAmbiguity?: "low" | "medium" | "high" | string;
  paletteGroups?: number;
  avgNeighbors?: number;
  subdivisionEdges?: number;
  objectDensity?: number;
  /** Play-test factor (stage 3, contract B) — present once a completed
   *  PUZZLE-mode run (number/memory/duel) has been recorded for the
   *  revision. Free-color runs never feed these. */
  playtestCount?: number;
  playtestMedianSeconds?: number;
  playtestSecondsPerRegion?: number;
  playtestMistakesPerRegion?: number;
  /** Engagement / interaction metrics from free-color runs — a different
   *  task than puzzle difficulty (no target, no mistakes). */
  freePlayCount?: number;
  freeMedianSeconds?: number;
  [key: string]: unknown;
}

export interface DifficultyProfile {
  rating: "easy" | "medium" | "hard" | "master";
  score: number;
  metrics: DifficultyMetrics;
}

export class ApiError extends Error {}

export function studioUrl(path: string, extraQuery?: Record<string, string | number | boolean | undefined>): string {
  const url = `/api${path}?XTransformPort=${PORT}`;
  if (!extraQuery) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(extraQuery)) {
    if (value !== undefined) params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${url}&${qs}` : url;
}

async function api<T>(
  path: string,
  options: RequestInit = {},
  query?: Record<string, string | number | boolean | undefined>
): Promise<T> {
  const res = await fetch(studioUrl(path, query), {
    ...options,
    headers: {
      "X-Studio-Request": "1",
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = (await res.json())?.detail;
    } catch {
      detail = null;
    }
    const message =
      typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : `${res.status} ${res.statusText}`;
    throw new ApiError(message);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown): Promise<T> =>
  api<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

// --- Endpoints ---------------------------------------------------------------

export const getConfig = (): Promise<StudioConfig> => api<StudioConfig>("/config");

export const listProjects = (): Promise<Project[]> => api<Project[]>("/projects");

export const getProject = (pid: string): Promise<Project> => api<Project>(`/projects/${pid}`);

/** Task 31B — request cooperative cancellation. Sending the jobId makes a
 *  LATE cancel (stale target) a no-op so it can never kill a newer attempt. */
export const cancelProjectJob = (pid: string, jobId?: string): Promise<Project> =>
  post<Project>(`/projects/${pid}/job/cancel`, jobId ? { jobId } : {});

// --- Generation sessions (Task 28 shells; Tasks 29/30 fill the workspaces) ---

export type SessionMode = "ai_chat" | "image_reference" | "image_convert";
export type SessionTier = "easy" | "medium" | "hard" | "master";

/** Transactional generation session (draft until committed; the artwork is
 *  only touched at commit). Sessions are FREE to create — no AI call happens
 *  until an explicit paid step is confirmed inside the workspace. */
export interface GenerationSession {
  id: string;
  mode: SessionMode;
  status: "draft_plan" | "generating" | "compiling" | "ready_to_commit" | "committed" | "failed" | "canceled";
  requestedDifficulty: SessionTier;
  targetRegions?: number;
  fidelity?: string;
  prompt?: string;
  aspect?: string;
  error?: string | null;
  meta?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
  [key: string]: unknown;
}

export const listGenerationSessions = (pid: string): Promise<{ sessions: GenerationSession[] }> =>
  api<{ sessions: GenerationSession[] }>(`/projects/${pid}/generation/sessions`);

export const createGenerationSession = (
  pid: string,
  body: {
    mode: SessionMode;
    requested_difficulty: SessionTier;
    prompt?: string;
    aspect?: "1024x1536" | "1536x1024" | "1024x1024";
    fidelity?: "stylized" | "balanced" | "faithful";
  }
): Promise<GenerationSession> => post(`/projects/${pid}/generation/sessions`, body);

export const discardGenerationSession = (pid: string, sid: string): Promise<unknown> =>
  api(`/projects/${pid}/generation/sessions/${sid}`, { method: "DELETE" });

// --- Task 29 — Create-with-AI session steps (paid steps require confirm_paid
// and run as async jobs; the session itself is re-read after each job) ---

export interface ScenePlanObject {
  id: string;
  name: string;
  description: string;
  role: "background" | "midground" | "foreground" | "subject" | "accent" | string;
  z: number;
  bbox: number[];
  fills: string[];
  detailWeight: number;
  parentId?: string;
  subdivision?: Record<string, unknown>;
  generation?: { locked?: boolean; prompt?: string; provider?: string } & Record<string, unknown>;
}

export interface ScenePlan {
  schemaVersion: number;
  title: string;
  description?: string;
  requestedDifficulty: SessionTier;
  targetRegionRange: [number, number];
  targetRegions: number;
  objects: ScenePlanObject[];
  viewBox: number[];
  aspect?: string;
}

export interface GenerationSessionFull extends GenerationSession {
  scenePlan?: ScenePlan;
  meta?: {
    planUsage?: Record<string, unknown>;
    lastPlanChat?: { instruction: string; summary: string; applied: number; mutations?: unknown[] };
    generationStages?: Record<string, unknown>;
    keptLockedObjects?: string[];
    artworkStale?: boolean;
    pendingArtworkChanges?: Record<string, string[]>;
    source?: { file: string; sha256: string; width: number; height: number; name: string };
    activeBuildInputs?: Record<string, unknown>;
    buildInputsStale?: boolean;
    convertPolicy?: Record<string, unknown>;
    conversionScores?: {
      visualFidelity: number;
      gameReadiness: number;
      passed: boolean;
      gates: { visualGate: number; gameReadinessGate: number };
      metrics?: Record<string, unknown>;
      notes?: string[];
    };
    difficultyOptimization?: {
      requestedTier?: string;
      outcome?: string;
      achieved?: { rating: string; score: number; regionCount: number };
      reasons?: string[];
      [key: string]: unknown;
    };
    qa?: QaReport;
    measuredDifficulty?: { rating: string; score: number; metrics: Record<string, unknown> };
    regionCount?: number;
    [key: string]: unknown;
  };
}

export const planSession = (pid: string, sid: string, confirm_paid: boolean, idempotency_key?: string): Promise<{ jobId: string }> =>
  post(`/projects/${pid}/generation/sessions/${sid}/plan`, { confirm_paid, idempotency_key });

export const planChatSession = (
  pid: string,
  sid: string,
  instruction: string,
  confirm_paid: boolean,
  idempotency_key?: string
): Promise<{ jobId: string }> => post(`/projects/${pid}/generation/sessions/${sid}/plan-chat`, { instruction, confirm_paid, idempotency_key });

export const mutateSessionPlan = (pid: string, sid: string, mutations: unknown[]): Promise<GenerationSessionFull> =>
  post(`/projects/${pid}/generation/sessions/${sid}/mutate`, { mutations });

export const generateSessionArtwork = (pid: string, sid: string, confirm_paid: boolean, idempotency_key?: string): Promise<{ jobId: string }> =>
  post(`/projects/${pid}/generation/sessions/${sid}/generate`, { confirm_paid, idempotency_key });

export const regenerateSessionObject = (
  pid: string,
  sid: string,
  objectId: string,
  instructions: string,
  confirm_paid: boolean,
  idempotency_key?: string
): Promise<{ jobId: string }> =>
  post(`/projects/${pid}/generation/sessions/${sid}/regenerate-object`, { objectId, instructions, confirm_paid, idempotency_key });

export const compileSession = (pid: string, sid: string): Promise<{ jobId: string }> =>
  post(`/projects/${pid}/generation/sessions/${sid}/compile`, {});

export const commitSessionArtwork = (pid: string, sid: string, title?: string, idempotency_key?: string): Promise<{ revision: Revision }> =>
  post(`/projects/${pid}/generation/sessions/${sid}/commit`, { title, idempotency_key });

/** Read-only preview of the session's compiled artwork (review stage). */
export const sessionPreviewUrl = (pid: string, sid: string, name: "colored.svg" | "numbered.svg" | "colored-preview.png" | "numbered-preview.png" | "source.png"): string =>
  studioUrl(`/projects/${pid}/generation/sessions/${sid}/preview/${name}`);

/** Task 30A — FREE: validate + store the session's source image (no AI). */
export const uploadSessionSource = (pid: string, sid: string, file: File): Promise<GenerationSessionFull> => {
  const form = new FormData();
  form.append("file", file);
  return api<GenerationSessionFull>(`/projects/${pid}/generation/sessions/${sid}/source`, {
    method: "POST",
    body: form,
  });
};

/** Task 30C — FREE: fidelity / difficulty change (invalidates the previous
 *  result's commit eligibility via the input-identity comparison). */
export const updateSessionSettings = (
  pid: string,
  sid: string,
  body: { fidelity?: "stylized" | "balanced" | "faithful"; requested_difficulty?: SessionTier }
): Promise<GenerationSessionFull> => post(`/projects/${pid}/generation/sessions/${sid}/settings`, body);

/** Task 30C — paid Convert run against the STORED session source. */
export const convertSession = (pid: string, sid: string, confirm_paid: boolean, idempotency_key?: string): Promise<{ jobId: string }> => {
  const form = new FormData();
  form.append("body", JSON.stringify({ confirm_paid, idempotency_key }));
  return api<{ jobId: string }>(`/projects/${pid}/generation/sessions/${sid}/convert`, {
    method: "POST",
    body: form,
  });
};

/** Task 30B — paid vision analysis of the STORED session reference. */
export const analyzeSessionReference = (
  pid: string,
  sid: string,
  confirm_paid: boolean,
  instructions = "",
  idempotency_key?: string
): Promise<{ jobId: string }> => {
  const form = new FormData();
  form.append("body", JSON.stringify({ confirm_paid, instructions, idempotency_key }));
  return api<{ jobId: string }>(`/projects/${pid}/generation/sessions/${sid}/reference-plan`, {
    method: "POST",
    body: form,
  });
};

/** Read-only preview of the session's STORED SOURCE image. */
export const sessionSourceUrl = (pid: string, sid: string): string =>
  studioUrl(`/projects/${pid}/generation/sessions/${sid}/preview/source.png`);

export const createProject = (title: string, brief?: string): Promise<Project> =>
  post<Project>("/projects", { title, ...(brief ? { brief } : {}) });

export const patchProject = (pid: string, body: { title?: string; brief?: string }): Promise<Project> =>
  api<Project>(`/projects/${pid}`, { method: "PATCH", body: JSON.stringify(body) });

export const uploadImage = (
  pid: string,
  file: File,
  role: "reference" | "master",
  rightsConfirmed: boolean
): Promise<Project> => {
  const form = new FormData();
  form.append("file", file);
  form.append("role", role);
  form.append("rights_confirmed", String(rightsConfirmed));
  return api<Project>(`/projects/${pid}/upload`, { method: "POST", body: form });
};

export const promoteReference = (pid: string, rightsConfirmed: boolean): Promise<Project> =>
  post<Project>(`/projects/${pid}/reference-as-master`, { rights_confirmed: rightsConfirmed });

export const loadSample = (pid: string): Promise<Project> => post<Project>(`/projects/${pid}/sample`, {});

export const loadSvgSample = (pid: string): Promise<Project> =>
  post<Project>(`/projects/${pid}/sample-svg`, {});

export const uploadSvgMaster = (pid: string, file: File, rightsConfirmed: boolean): Promise<Project> => {
  const form = new FormData();
  form.append("file", file);
  form.append("rights_confirmed", String(rightsConfirmed));
  return api<Project>(`/projects/${pid}/upload-svg`, { method: "POST", body: form });
};

export const generateSvgMaster = (
  pid: string,
  body: {
    prompt: string;
    aspect: "1024x1536" | "1536x1024" | "1024x1024";
    include_reference: boolean;
    confirm_paid: boolean;
    /** "single" = one-shot SVG; "multistage" = scene plan → objects → compose. */
    mode: "single" | "multistage";
    /** Multistage only: desired region count after auto-subdivide (60..1200). */
    target_regions: number;
  }
): Promise<{ jobId: string; projectId: string }> => post(`/projects/${pid}/generate-svg`, body);

export const fetchGeometryMode = (pid: string, rev: string, mode: GeometryMode): Promise<GeometryModePayload> =>
  api<GeometryModePayload>(`/projects/${pid}/revisions/${rev}/geometry`, {}, { mode });

export const sendChat = (
  pid: string,
  body: { message: string; include_reference: boolean; confirm_paid: boolean }
): Promise<{ jobId: string; projectId: string }> => post(`/projects/${pid}/chat`, body);

export const generateMaster = (
  pid: string,
  body: {
    source: GenerateSource;
    prompt: string;
    quality: ImageQuality;
    size: "1024x1536" | "1536x1024" | "1024x1024";
    confirm_paid: boolean;
  }
): Promise<{ jobId: string; projectId: string }> => post(`/projects/${pid}/generate`, body);

export const buildDraft = (pid: string, settings: BuildSettings): Promise<{ jobId: string; projectId: string }> =>
  post(`/projects/${pid}/build`, settings);

export const runEdit = (pid: string, payload: EditPayload): Promise<{ jobId: string; projectId: string }> =>
  post(`/projects/${pid}/edit`, payload);

/** Task 27 — Optimize Difficulty: reshape the gameplay layer of the current
 *  revision toward a tier (artwork stays byte-identical; a moved geometry
 *  lands in a new immutable revision, an unchanged one is a no-op report). */
export const optimizeDifficulty = (
  pid: string,
  body: { base_revision: string; tier: "easy" | "medium" | "hard" | "master" }
): Promise<{ jobId: string; projectId: string }> => post(`/projects/${pid}/optimize`, body);

export const activateRevision = (pid: string, revision: string): Promise<Project> =>
  post<Project>(`/projects/${pid}/activate`, { revision });

export interface PlaytestResponse {
  /** Full manifest with the difficulty block recomputed (playtest metrics +
   *  the validated flag). */
  manifest: RevisionManifest;
  playtestCount: number;
  medianSeconds: number;
}

/** Record a completed play-test run against a revision (contract B) — a
 *  DIRECT call (no job polling); the backend recomputes difficulty with the
 *  playtests and rewrites the manifest difficulty block in place. */
export interface PlaytestRecordBody {
  seconds: number;
  filled: number;
  total: number;
  mistakes: number;
  mode: "number" | "memory" | "duel" | "free";
}

export const recordPlaytest = (
  pid: string,
  revision: string,
  body: PlaytestRecordBody
): Promise<PlaytestResponse> => post(`/projects/${pid}/revisions/${revision}/playtest`, body);

export const submitReview = (pid: string, revision: string, note: string): Promise<Project> =>
  post<Project>(`/projects/${pid}/review`, { revision, note, confirmed: true });

// --- URLs for binary downloads / <img> sources (relative + gateway port) ------

export const imageUrl = (pid: string, role: "master" | "reference", sha256: string): string =>
  studioUrl(`/projects/${pid}/image/${role}`, { v: sha256 });

export const masterSvgUrl = (pid: string, sha256: string): string =>
  studioUrl(`/projects/${pid}/master/svg`, { v: sha256 });

export const revisionFileUrl = (pid: string, rev: string, name: string): string =>
  studioUrl(`/projects/${pid}/revisions/${rev}/files/${name}`);

export const exportUrl = (pid: string, rev: string, authoring = false): string =>
  studioUrl(`/projects/${pid}/revisions/${rev}/export`, { authoring: authoring || undefined });

export const renderUrl = (pid: string, rev: string, width = 2048): string =>
  studioUrl(`/projects/${pid}/revisions/${rev}/render`, { width });
