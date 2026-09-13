/** Task 35B — React/client-level recovery acceptance.
 *
 * Exercises the REAL StudioProvider hook (studio-api and detailed-board
 * mocked at the module boundary) — the layer the HTTP smoke suite cannot
 * reach: which idempotency key the hook actually sends, what survives a
 * LOST response, and which terminal outcome may clear a pending identity.
 */

import { cleanup, render, waitFor } from "@testing-library/react";
import { createElement, useEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const sentBodies: Array<Record<string, unknown>> = [];
let getProjectQueue: Array<Record<string, unknown>> = [];

function _project(over: Record<string, unknown> = {}): Project {
  return {
    id: "art-probe",
    title: "Probe",
    brief: "",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    messages: [],
    aiUsage: [],
    master: null,
    reference: null,
    revisions: [],
    currentRevision: null,
    job: {},
    ...over,
  } as Project;
}

function _session(status: string): GenerationSession {
  return {
    id: "sess-probe",
    mode: "ai_chat",
    status: status as GenerationSession["status"],
    requestedDifficulty: "medium",
    prompt: "probe",
  } as GenerationSession;
}

let generateResult: "lost" | "ok" = "ok";

vi.mock("@/lib/studio-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/studio-api")>();
  return {
    ...actual,
    getConfig: vi.fn(async () => ({ ai: { configured: false, chatModel: "m", imageModel: "m" }, localOnly: true })),
    listProjects: vi.fn(async () => []),
    createProject: vi.fn(async (title: string) => _project({ title })),
    getProject: vi.fn(async () => {
      // sequential responses: acceptance snapshot first, then poll results
      const next = getProjectQueue.shift();
      return _project(next ?? { job: {} });
    }),
    patchProject: vi.fn(async () => _project(getProjectQueue.shift() ?? {})),
    listGenerationSessions: vi.fn(async () => ({
      sessions: [_session("draft_plan")],
    })),
    createGenerationSession: vi.fn(async () => _session("draft_plan")),
    generateSessionArtwork: vi.fn(async (_pid: string, _sid: string, confirm_paid: boolean, idempotency_key?: string) => {
      sentBodies.push({ confirm_paid, idempotency_key });
      if (generateResult === "lost") throw new TypeError("Failed to fetch");
      return { jobId: "job-gen-1" };
    }),
    commitSessionArtwork: vi.fn(async () => ({ revision: {
      id: "rev-probe", version: "0.1.0", createdAt: "2026-01-01T00:00:00Z", kind: "generation",
      sourceHash: "x", regionCount: 10, qa: { passed: true }, manifestUrl: "",
    } })),
  };
});

vi.mock("@/lib/detailed-board", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    loadBundle: vi.fn(async () => ({
      manifest: { id: "art-probe", version: "0.1.0", difficulty: { rating: "medium", score: 50, metrics: {} } },
      palette: [],
      geometry: { regions: [] },
    })),
    VectorBoard: class {
      svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      elements = new Map();
      destroy() {}
      setPreview() {}
      clientToArt() { return null; }
    },
  };
});

import { StudioProvider, useStudioContext } from "./use-studio";
import { ApiError, type Project, type GenerationSession } from "@/lib/studio-api";

type Studio = ReturnType<typeof useStudioContext>;

let captured: Studio | null = null;

function Harness(): ReactNode {
  const studio = useStudioContext();
  // keep the latest context so probes observe polled updates (effect, not
  // render — render must stay pure)
  useEffect(() => {
    captured = studio;
  });
  return createElement("div", { "data-testid": "probe" });
}

function mountStudio(projectStates: Array<Record<string, unknown>> = []) {
  captured = null;
  localStorage.setItem("studio-project", "art-probe");
  // getProject call sequence: [openProject load, job() acceptance, poll...]
  getProjectQueue = projectStates.map((over) => ({ ..._project(over) }));
  render(createElement(StudioProvider, null, createElement(Harness)));
}

async function waitActiveSession(): Promise<Studio> {
  // Poll the LIVE captured context (updated by the harness effect on every
  // render), not a frozen early snapshot: context values are new objects per
  // render, so a snapshot taken before the session list resolves would never
  // gain `activeSession` — and its callbacks would be bound to null forever.
  await waitFor(() => expect(captured?.activeSession).not.toBeNull(), { timeout: 10_000 });
  return captured as unknown as Studio;
}

beforeEach(() => {
  sentBodies.length = 0;
  getProjectQueue = [];
  generateResult = "ok";
  localStorage.clear();
});

afterEach(() => {
  cleanup();                 // unmount providers so stale pollers stop
  vi.clearAllMocks();
});

function currentPolledJobId(): string | null {
  const job = (captured as unknown as { project: { job: { id?: string } } } | null)
    ?.project?.job;
  return job?.id ?? null;
}

function pendingOpKey(): string | null {
  const raw = localStorage.getItem("cd-pending-operation");
  return raw ? ((JSON.parse(raw).key as string) ?? null) : null;
}

describe("Task 35B — client-level recovery", () => {
  it("35B-1 lost generate response: the retry REUSES the same idempotency key", async () => {
    mountStudio([
      { job: { id: "job-gen-1", status: "running", progress: 0.2, message: "Vectorizing" } },
      { job: { id: "job-gen-1", status: "done", progress: 1 } },
      { job: { id: "job-gen-1", status: "done", progress: 1 } },
    ]);
    const studio = await waitActiveSession();

    generateResult = "lost";
    await expect(studio.generateArtwork(true)).rejects.toThrow();
    const firstKey = sentBodies.at(-1)?.idempotency_key;
    expect(firstKey).toBeTruthy();
    // outcome UNKNOWN: identity kept (localStorage + ref)
    expect(JSON.parse(localStorage.getItem("cd-pending-operation")!).key).toBe(firstKey);

    // resending the same logical action reuses the SAME key — the backend
    // replays the existing attempt instead of buying new work
    generateResult = "ok";
    await studio.generateArtwork(true);
    const keys = sentBodies.map((b) => b.idempotency_key);
    expect(keys.filter((k) => k === firstKey).length).toBe(2);
  }, 20_000);

  it("35B-2 known-failed generate: the explicit retry uses a NEW key", async () => {
    mountStudio([
      { job: { id: "job-gen-1", status: "failed", message: "provider blip" } },
      { job: { id: "job-gen-1", status: "failed", message: "provider blip" } },
      { job: { id: "job-gen-1", status: "failed", message: "provider blip" } },
    ]);
    const studio = await waitActiveSession();

    // definite server rejection (our API translated the provider failure to
    // a 4xx): the identity is dropped — the next confirm is a NEW purchase
    const { generateSessionArtwork } = await import("@/lib/studio-api");
    (generateSessionArtwork as ReturnType<typeof vi.fn>).mockImplementationOnce(
      async (_pid: string, _sid: string, confirm_paid: boolean, idempotency_key?: string) => {
        sentBodies.push({ confirm_paid, idempotency_key });
        throw new ApiError("The generation failed: provider blip.");
      },
    );
    generateResult = "lost";
    await expect(studio.generateArtwork(true)).rejects.toThrow(ApiError);
    const firstKey = sentBodies.at(-1)?.idempotency_key;
    expect(firstKey).toBeTruthy();
    // definite rejection → identity dropped
    expect(pendingOpKey()).toBeNull();

    generateResult = "ok";
    await studio.generateArtwork(true);
    const retryKey = sentBodies.at(-1)?.idempotency_key;
    expect(retryKey).toBeTruthy();
    expect(retryKey).not.toBe(firstKey);
  }, 20_000);

  it("35B-3 pending identity survives an UNRELATED job's terminal outcome, and is cleared by the MATCHING one", async () => {
    // getProject sequence: [mount load job-A running, generate accept job-A
    // running, UNRELATED job-B done, MATCHING job-A done, spare]
    mountStudio([
      { job: { id: "job-A", status: "running", progress: 0.2 } },
      { job: { id: "job-A", status: "running", progress: 0.3 } },
      { job: { id: "job-B", status: "done", progress: 1 } },
      { job: { id: "job-A", status: "done", progress: 1 } },
    ]);
    const studio = await waitActiveSession();
    generateResult = "ok";
    await studio.generateArtwork(true);
    const firstKey = sentBodies.at(-1)?.idempotency_key;
    expect(firstKey).toBeTruthy();
    // pending attached to job-A (the job that accepted the work)
    await waitFor(() => expect(pendingOpKey()).toBe(firstKey), { timeout: 5000 });

    // UNRELATED job-B settles (the poll observes it): the pending identity —
    // which belongs to job-A — must NOT be resolved by it
    await waitFor(() => {
      expect(currentPolledJobId()).toBe("job-B");
      expect(pendingOpKey()).toBe(firstKey);
    }, { timeout: 10_000 });

    // MATCHING job-A settles: pending identity resolves
    await waitFor(() => {
      expect(currentPolledJobId()).toBe("job-A");
      expect(pendingOpKey()).toBeNull();
    }, { timeout: 12_000 });
  }, 20_000);

});
