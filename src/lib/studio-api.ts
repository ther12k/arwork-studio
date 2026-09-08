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

export interface Revision {
  id: string;
  version: string;
  createdAt: string;
  kind: string;
  sourceHash: string;
  regionCount: number;
  qa: QaReport;
  manifestUrl: string;
}

export interface Job {
  id?: string;
  kind?: string;
  status?: "queued" | "running" | "done" | "failed";
  progress?: number;
  message?: string;
  startedAt?: string;
  finishedAt?: string | null;
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

export type EditAction = "merge" | "group" | "palette" | "recolor" | "label" | "decorate" | "split" | "cut" | "draw";

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
  /** 'recolor': the visible appearance color (#RRGGBB). Distinct from
   * 'palette', which assigns the gameplay number group. */
  color?: string;
  /** 'recolor': keep gradient shading (tinted toward the target color)
   * instead of replacing the fill. */
  preserve_shading?: boolean;
  /** 'cut': the cut polyline (open path, M/L only). 'draw': the closed
   * region outline (M…Z). Pattern ^M[\s\d.,eE+\-MLQCZz]+$ upstream. */
  d?: string;
}

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
    let detail: unknown = null;
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

export const activateRevision = (pid: string, revision: string): Promise<Project> =>
  post<Project>(`/projects/${pid}/activate`, { revision });

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
