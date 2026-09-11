"use client";

/** Task 28 — Create Artwork entry UX.
 *
 * Replaces the compiler-console empty state for artwork-less projects with
 * two artist decisions: CREATE WITH AI or FROM IMAGE. Each opens a shell that
 * creates a REAL (free) generation session — no paid AI call happens until an
 * explicit confirmed step inside the workspace (Tasks 29/30 fill those in).
 * The low-level tools (SVG master import, raster upload, samples) stay
 * available under "Advanced" so the existing manual workflows never regress.
 * The active session is restored across refreshes: an empty project with a
 * session shows the session shell, the editor shows a resume banner. */

import { useRef, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
  FileUp,
  Image as ImageIcon,
  Layers,
  RotateCcw,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { SessionTier } from "@/lib/studio-api";
import { useStudioContext } from "./use-studio";
import { DIFFICULTY_TIERS } from "./difficulty";

const TIERS_ONLY: Array<{ key: SessionTier; label: string; color: string }> =
  DIFFICULTY_TIERS.map((t) => ({ key: t.key as SessionTier, label: t.label, color: t.color }));

const ASPECTS: Array<{ value: "1024x1536" | "1536x1024" | "1024x1024"; label: string }> = [
  { value: "1024x1536", label: "Portrait 3:4" },
  { value: "1536x1024", label: "Landscape 4:3" },
  { value: "1024x1024", label: "Square 1:1" },
];

const FIDELITIES: Array<{ value: "stylized" | "balanced" | "faithful"; label: string; hint: string }> = [
  { value: "stylized", label: "Stylized", hint: "Clean shapes, fewer fragments" },
  { value: "balanced", label: "Balanced", hint: "Faithful yet playful" },
  { value: "faithful", label: "Faithful", hint: "Closest to the original" },
];

const MODE_LABEL: Record<string, string> = {
  ai_chat: "Create with AI",
  image_reference: "Use as Reference",
  image_convert: "Convert Artwork",
};

const STATUS_TONE: Record<string, string> = {
  draft_plan: "border-[#e1e5df] bg-white text-[#657671]",
  generating: "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]",
  compiling: "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]",
  ready_to_commit: "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]",
  failed: "border-[#e8cfc7] bg-[#fdf3f1] text-[#ba463f]",
};

/** Small tier selector — Easy / Medium / Hard / Master only (raw region
 *  numbers are deliberately not part of the primary UX). */
function TierPicker({
  value,
  onChange,
  disabled,
}: {
  value: SessionTier;
  onChange: (t: SessionTier) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid grid-cols-4 gap-1" role="group" aria-label="Target difficulty">
      {TIERS_ONLY.map((t) => (
        <button
          key={t.key}
          type="button"
          aria-pressed={value === t.key}
          disabled={disabled}
          onClick={() => onChange(t.key)}
          className={`rounded-md border px-1 py-1.5 text-[10px] font-semibold transition-colors disabled:opacity-50 ${
            value === t.key
              ? "text-white"
              : "border-[#dfe6d8] bg-white text-[#4c5b56] hover:border-[#b9c7bd]"
          }`}
          style={value === t.key ? { background: t.color, borderColor: t.color } : undefined}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

/** The session card every shell converges on: real state, honestly labelled,
 *  with the upcoming-workspace note (Tasks 29/30 replace it with the actual
 *  scene-plan / image workspaces). */
function SessionCard({ mode }: { mode: "ai" | "image" }) {
  const { activeSession, discardActiveSession, closeCreateWorkspace, project } = useStudioContext();
  if (!activeSession) return null;
  const s = activeSession;
  const hasArtwork = !!(project?.currentRevision || project?.master);
  return (
    <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CheckCircle2 className="size-4 text-[#087f74]" aria-hidden />
          <h3 className="text-sm font-bold text-[#183837]">{MODE_LABEL[s.mode] ?? s.mode} session ready</h3>
        </div>
        <span
          className={`rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase tracking-[0.06em] ${
            STATUS_TONE[s.status] ?? STATUS_TONE.draft_plan
          }`}
        >
          {s.status.replace("_", " ")}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px] sm:grid-cols-3">
        <div>
          <dt className="text-[#778481]">Difficulty</dt>
          <dd className="font-semibold capitalize text-[#183837]">{s.requestedDifficulty}</dd>
        </div>
        {s.mode !== "ai_chat" && (
          <div>
            <dt className="text-[#778481]">Fidelity</dt>
            <dd className="font-semibold capitalize text-[#183837]">{s.fidelity ?? "balanced"}</dd>
          </div>
        )}
        <div>
          <dt className="text-[#778481]">Session</dt>
          <dd className="font-mono text-[10px] text-[#4c5b56]">{s.id}</dd>
        </div>
      </dl>
      {s.prompt ? (
        <p className="mt-3 line-clamp-3 rounded-lg bg-[#f4f6f0] px-3 py-2 text-[11px] leading-relaxed text-[#4c5b56]">
          {s.prompt}
        </p>
      ) : null}
      {s.error ? (
        <p className="mt-2 rounded-lg bg-[#fdf3f1] px-3 py-2 text-[10px] leading-relaxed text-[#ba463f]" role="alert">
          {s.error}
        </p>
      ) : null}
      <p className="mt-3 text-[10px] leading-relaxed text-[#778481]">
        Your session is saved on this studio and reopens automatically — nothing is lost on a page
        reload. The interactive {mode === "ai" ? "scene-plan" : "image"} workspace is the next
        update; no paid AI request runs until you explicitly confirm one there.
      </p>
      <div className="mt-3 flex flex-wrap justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          className="h-8 rounded-md border-[#e1e5df] bg-white text-[10px] hover:bg-[#f0f7f3]"
          onClick={() => void discardActiveSession().catch((e: Error) => toast(e.message))}
        >
          <RotateCcw className="size-3.5" aria-hidden />
          Start over
        </Button>
        {hasArtwork && (
          <Button
            variant="outline"
            size="sm"
            className="h-8 rounded-md border-[#e1e5df] bg-white text-[10px] hover:bg-[#f0f7f3]"
            onClick={closeCreateWorkspace}
          >
            Back to editor
          </Button>
        )}
      </div>
    </div>
  );
}

function AiShell() {
  const { startAiCreation, activeSession, setCreationFlow, briefInput, busy } = useStudioContext();
  const [prompt, setPrompt] = useState(briefInput);
  const [tier, setTier] = useState<SessionTier>("hard");
  const [aspect, setAspect] = useState<"1024x1536" | "1536x1024" | "1024x1024">("1024x1536");
  const [starting, setStarting] = useState(false);
  if (activeSession?.mode === "ai_chat") return <SessionCard mode="ai" />;
  const start = () => {
    const text = prompt.trim();
    if (!text) {
      toast("Describe the scene you want first.");
      return;
    }
    setStarting(true);
    void startAiCreation(text, tier, aspect)
      .catch((e: Error) => toast(e.message))
      .finally(() => setStarting(false));
  };
  return (
    <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-[#087f74]" aria-hidden />
          <h3 className="text-sm font-bold text-[#183837]">Create with AI</h3>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 rounded-md px-2 text-[10px] text-[#657671] hover:bg-[#f0f7f3]"
          onClick={() => setCreationFlow(null)}
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          Back
        </Button>
      </div>
      <div className="mt-3">
        <Label htmlFor="create-ai-prompt" className="text-[10px] leading-snug text-[#657671]">
          Describe the artwork
        </Label>
        <Textarea
          id="create-ai-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          maxLength={12000}
          rows={4}
          placeholder="An original richly illustrated woodland treehouse with a glowing window, winding stairs and a waterfall. No lettering or UI."
          className="mt-1 min-h-24 resize-y rounded-md border-[#e1e5df] bg-white text-xs leading-relaxed"
        />
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <Label className="text-[10px] leading-snug text-[#657671]">Difficulty</Label>
          <div className="mt-1">
            <TierPicker value={tier} onChange={setTier} disabled={starting} />
          </div>
        </div>
        <div>
          <Label htmlFor="create-ai-aspect" className="text-[10px] leading-snug text-[#657671]">
            Format
          </Label>
          <Select
            value={aspect}
            onValueChange={(v) => setAspect(v as typeof aspect)}
            disabled={starting}
          >
            <SelectTrigger id="create-ai-aspect" className="mt-1 h-9 rounded-md border-[#e1e5df] bg-white text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ASPECTS.map((a) => (
                <SelectItem key={a.value} value={a.value}>
                  {a.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <p className="mt-3 text-[10px] leading-relaxed text-[#778481]">
        Starting is free — the session only records your direction. Paid AI steps ask for explicit
        confirmation inside the workspace before anything runs.
      </p>
      <Button
        size="sm"
        disabled={starting || busy}
        onClick={start}
        className="mt-3 h-9 w-full rounded-md bg-[#0e554e] text-[11px] font-semibold text-white hover:bg-[#0a423d] sm:w-auto sm:px-6"
      >
        {starting ? "Creating session…" : "Start with AI"}
      </Button>
    </div>
  );
}

function ImageShell() {
  const { startImageCreation, activeSession, setCreationFlow, busy } = useStudioContext();
  const [path, setPath] = useState<"reference" | "convert">("convert");
  const [tier, setTier] = useState<SessionTier>("hard");
  const [fidelity, setFidelity] = useState<"stylized" | "balanced" | "faithful">("balanced");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  if (
    activeSession &&
    (activeSession.mode === "image_reference" || activeSession.mode === "image_convert")
  ) {
    return <SessionCard mode="image" />;
  }
  const pick = (f: File | null) => {
    setFile(f);
    if (preview) URL.revokeObjectURL(preview);
    setPreview(f ? URL.createObjectURL(f) : null);
  };
  const start = () => {
    if (path === "reference" && !file) {
      toast("Choose an image to use as reference first.");
      return;
    }
    setStarting(true);
    void startImageCreation(path, tier, fidelity, path === "reference" ? file ?? undefined : undefined)
      .catch((e: Error) => toast(e.message))
      .finally(() => setStarting(false));
  };
  return (
    <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ImageIcon className="size-4 text-[#087f74]" aria-hidden />
          <h3 className="text-sm font-bold text-[#183837]">Create from Image</h3>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 rounded-md px-2 text-[10px] text-[#657671] hover:bg-[#f0f7f3]"
          onClick={() => {
            pick(null);
            setCreationFlow(null);
          }}
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          Back
        </Button>
      </div>
      {/* Image picker + preview */}
      <div className="mt-3 flex flex-col gap-3 sm:flex-row">
        <div className="flex-1">
          <Label htmlFor="create-image-file" className="text-[10px] leading-snug text-[#657671]">
            Your image
          </Label>
          <input
            ref={fileInputRef}
            id="create-image-file"
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="sr-only"
            onChange={(e) => pick(e.target.files?.[0] ?? null)}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="mt-1 flex min-h-11 w-full items-center justify-center gap-2 rounded-md border border-dashed border-[#c4d2c6] bg-[#f7f9f4] px-3 py-2.5 text-[11px] font-medium text-[#4c5b56] hover:border-[#65a89b] hover:bg-[#f0f7f3]"
          >
            <FileUp className="size-4" aria-hidden />
            {file ? "Choose a different image" : "Choose an image (PNG · JPG · WebP)"}
          </button>
          {preview && (
            <img
              src={preview}
              alt=""
              className="mt-2 max-h-44 w-full rounded-lg border border-[#e1e5df] object-contain"
            />
          )}
        </div>
        {/* Path choice */}
        <div className="flex-1">
          <span className="block text-[10px] leading-snug text-[#657671]">How should it be used?</span>
          <div className="mt-1 space-y-1.5">
            {(
              [
                {
                  key: "reference" as const,
                  title: "Use as Reference",
                  hint: "The AI understands its mood & subject, then draws a NEW original scene — your image is never traced.",
                },
                {
                  key: "convert" as const,
                  title: "Convert Artwork",
                  hint: "Turn this exact artwork into a playable board — same look, real tap regions.",
                },
              ]
            ).map((p) => (
              <button
                key={p.key}
                type="button"
                aria-pressed={path === p.key}
                disabled={starting}
                onClick={() => setPath(p.key)}
                className={`w-full rounded-lg border px-3 py-2 text-left transition-colors disabled:opacity-50 ${
                  path === p.key
                    ? "border-[#087f74] bg-[#edf6f2]"
                    : "border-[#e1e5df] bg-white hover:border-[#b9c7bd]"
                }`}
              >
                <span className="block text-[11px] font-semibold text-[#183837]">{p.title}</span>
                <span className="mt-0.5 block text-[10px] leading-snug text-[#778481]">{p.hint}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <Label className="text-[10px] leading-snug text-[#657671]">Difficulty</Label>
          <div className="mt-1">
            <TierPicker value={tier} onChange={setTier} disabled={starting} />
          </div>
        </div>
        <div>
          <Label htmlFor="create-image-fidelity" className="text-[10px] leading-snug text-[#657671]">
            Fidelity
          </Label>
          <Select
            value={fidelity}
            onValueChange={(v) => setFidelity(v as typeof fidelity)}
            disabled={starting}
          >
            <SelectTrigger id="create-image-fidelity" className="mt-1 h-9 rounded-md border-[#e1e5df] bg-white text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {FIDELITIES.map((f) => (
                <SelectItem key={f.value} value={f.value}>
                  {f.label} — {f.hint}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <p className="mt-3 text-[10px] leading-relaxed text-[#778481]">
        Starting is free. For “Use as Reference” the image is stored with the project; every paid AI
        step asks for explicit confirmation inside the workspace before it runs.
      </p>
      <Button
        size="sm"
        disabled={starting || busy}
        onClick={start}
        className="mt-3 h-9 w-full rounded-md bg-[#0e554e] text-[11px] font-semibold text-white hover:bg-[#0a423d] sm:w-auto sm:px-6"
      >
        {starting ? "Creating session…" : `Start — ${path === "reference" ? "Use as Reference" : "Convert Artwork"}`}
      </Button>
    </div>
  );
}

function Landing() {
  const {
    setCreationFlow,
    activeSession,
    resumeCreationSession,
    uploadFile,
    uploadSvgFile,
    loadSampleProject,
    loadSvgSampleProject,
    busy,
  } = useStudioContext();
  const [advanced, setAdvanced] = useState(false);
  const svgInputRef = useRef<HTMLInputElement | null>(null);
  const rasterInputRef = useRef<HTMLInputElement | null>(null);
  return (
    <div className="mx-auto w-full max-w-3xl">
      <header className="mb-5 text-center">
        <span className="mb-2 block text-[9px] font-semibold uppercase tracking-[0.18em] text-[#80908a]">
          CREATE ARTWORK
        </span>
        <h2 className="text-2xl font-bold tracking-tight text-[#183837]">
          {activeSession ? "Pick up where you left off" : "How do you want to start?"}
        </h2>
        <p className="mx-auto mt-2 max-w-md text-xs leading-relaxed text-[#778481]">
          {activeSession
            ? "A generation session is already running for this project."
            : "Describe a scene and refine it with AI, or bring an image you own."}
        </p>
      </header>
      {activeSession ? (
        <div className="rounded-xl border border-[#cce7dc] bg-[#edf6f2] p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <Layers className="size-5 text-[#087f74]" aria-hidden />
              <div>
                <p className="text-sm font-bold text-[#183837]">
                  {MODE_LABEL[activeSession.mode] ?? "Generation"} in progress
                </p>
                <p className="mt-0.5 text-[10px] text-[#657671]">
                  {activeSession.requestedDifficulty} · session {activeSession.id}
                </p>
              </div>
            </div>
            <Button
              size="sm"
              className="h-9 rounded-md bg-[#0e554e] text-[11px] font-semibold text-white hover:bg-[#0a423d]"
              onClick={resumeCreationSession}
            >
              Resume session
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          <button
            type="button"
            onClick={() => setCreationFlow("ai")}
            className="group rounded-xl border border-[#dfe6d8] bg-white p-5 text-left transition-all hover:-translate-y-0.5 hover:border-[#65a89b] hover:shadow-[0_8px_24px_rgba(18,47,34,0.09)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#087f74]"
          >
            <span className="flex size-11 items-center justify-center rounded-[12px] bg-[#e5f3ed] text-[#087f74]">
              <Sparkles className="size-5" aria-hidden />
            </span>
            <span className="mt-3 block text-sm font-bold text-[#183837]">Create with AI</span>
            <span className="mt-1 block text-[11px] leading-relaxed text-[#778481]">
              Describe a scene and refine it with art direction until it feels right.
            </span>
            <span className="mt-3 inline-flex items-center gap-1 text-[11px] font-semibold text-[#087f74]">
              Start <span aria-hidden>→</span>
            </span>
          </button>
          <button
            type="button"
            onClick={() => setCreationFlow("image")}
            className="group rounded-xl border border-[#dfe6d8] bg-white p-5 text-left transition-all hover:-translate-y-0.5 hover:border-[#65a89b] hover:shadow-[0_8px_24px_rgba(18,47,34,0.09)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#087f74]"
          >
            <span className="flex size-11 items-center justify-center rounded-[12px] bg-[#e9f0ec] text-[#3a6b57]">
              <ImageIcon className="size-5" aria-hidden />
            </span>
            <span className="mt-3 block text-sm font-bold text-[#183837]">From Image</span>
            <span className="mt-1 block text-[11px] leading-relaxed text-[#778481]">
              Reference an image you own, or convert existing artwork into a playable board.
            </span>
            <span className="mt-3 inline-flex items-center gap-1 text-[11px] font-semibold text-[#087f74]">
              Upload <span aria-hidden>→</span>
            </span>
          </button>
        </div>
      )}
      {/* Advanced — the manual workflows stay one disclosure away */}
      <div className="mt-4 rounded-xl border border-[#e1e5df] bg-[#f7f8f3]">
        <button
          type="button"
          aria-expanded={advanced}
          onClick={() => setAdvanced((v) => !v)}
          className="flex min-h-11 w-full items-center justify-between px-4 py-3 text-[11px] font-semibold text-[#4c5b56]"
        >
          Advanced — import & samples
          <ChevronDown
            className={`size-4 transition-transform ${advanced ? "rotate-180" : ""}`}
            aria-hidden
          />
        </button>
        {advanced && (
          <div className="grid gap-2 border-t border-[#e1e5df] p-3 sm:grid-cols-2">
            <input
              ref={svgInputRef}
              type="file"
              accept=".svg,image/svg+xml"
              className="sr-only"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void uploadSvgFile(f).catch((err: Error) => toast(err.message));
                e.target.value = "";
              }}
            />
            <input
              ref={rasterInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="sr-only"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void uploadFile(f, "master").catch((err: Error) => toast(err.message));
                e.target.value = "";
              }}
            />
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => svgInputRef.current?.click()}
              className="h-9 justify-start rounded-md border-[#e1e5df] bg-white text-[11px]"
            >
              <FileUp className="size-3.5" aria-hidden />
              Import SVG master
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => rasterInputRef.current?.click()}
              className="h-9 justify-start rounded-md border-[#e1e5df] bg-white text-[11px]"
            >
              <ImageIcon className="size-3.5" aria-hidden />
              Upload raster artwork
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void loadSampleProject().catch((e: Error) => toast(e.message))}
              className="h-9 justify-start rounded-md border-[#e1e5df] bg-white text-[11px]"
            >
              Open image example
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void loadSvgSampleProject().catch((e: Error) => toast(e.message))}
              className="h-9 justify-start rounded-md border-[#e1e5df] bg-white text-[11px]"
            >
              Open SVG example
            </Button>
            <p className="text-[9px] leading-relaxed text-[#8a968f] sm:col-span-2">
              Importing a master opens the editor immediately; compiler settings (region density
              and curve fitting) live in the build panel there.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/** Center-workspace component for artwork-less projects (and for resuming a
 *  session over the editor): landing → shell → session card. */
export function CreateArtwork() {
  const { creationMode, activeSession } = useStudioContext();
  const shell =
    creationMode === "ai" ? (
      <AiShell />
    ) : creationMode === "image" ? (
      <ImageShell />
    ) : activeSession ? null : null;
  // A resumed/opened shell wins; otherwise the landing (which itself shows
  // the resume card when a session exists).
  return (
    <section className="studio-main-panel min-w-0" aria-label="Create artwork">
      {shell ?? <Landing />}
    </section>
  );
}
