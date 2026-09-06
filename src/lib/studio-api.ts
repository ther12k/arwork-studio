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

export interface StudioConfig {
  ai: AiConfig;
  localOnly: boolean;
  version: string;
  supportedFormat: string;
  maxUploadMB: number;
}

export interface ImageAsset {
  file: string;
  width: number;
  height: number;
  sha256: string;
  source: string;
  rightsConfirmed: boolean;
  createdAt: string;
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
}

export type EditAction = "merge" | "group" | "palette" | "label" | "decorate";

export interface EditPayload {
  base_revision: string;
  action: EditAction;
  region_ids: string[];
  group?: string;
  palette_id?: number;
  x?: number;
  y?: number;
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

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(studioUrl(path), {
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

export const revisionFileUrl = (pid: string, rev: string, name: string): string =>
  studioUrl(`/projects/${pid}/revisions/${rev}/files/${name}`);

export const exportUrl = (pid: string, rev: string, authoring = false): string =>
  studioUrl(`/projects/${pid}/revisions/${rev}/export`, { authoring: authoring || undefined });

export const renderUrl = (pid: string, rev: string, width = 2048): string =>
  studioUrl(`/projects/${pid}/revisions/${rev}/render`, { width });
