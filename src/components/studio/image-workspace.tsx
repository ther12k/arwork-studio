"use client";

/** Task 30 — Create-from-Image workspace.
 *
 * 30B Reference: the STORED session source feeds the vision planning call;
 * everything after the plan reuses the Task-29 AI workspace stages (plan →
 * generate → review → commit). The result is an ORIGINAL interpretation —
 * never presented as a conversion promising the identical layout.
 *
 * 30C Convert: fidelity + difficulty are FREE settings; Convert is one paid
 * run against the stored source; the review shows source and result side by
 * side (stacked on mobile) with the two independent quality scores, gameplay
 * difficulty and the optimization outcome. Changing the source or fidelity
 * makes the previous result non-committable (input-identity comparison) —
 * paid work is never auto-started. */

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  Grid2x2,
  Image as ImageIcon,
  Loader2,
  Palette,
  RefreshCw,
  Sparkles,
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
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  sessionSourceUrl,
  sessionPreviewUrl,
  type GenerationSessionFull,
  type SessionTier,
} from "@/lib/studio-api";
import { useStudioContext } from "./use-studio";
import { AiWorkspace } from "./ai-workspace";
import { DIFFICULTY_TIERS } from "./difficulty";

const FIDELITIES: Array<{ value: "stylized" | "balanced" | "faithful"; label: string; hint: string }> = [
  { value: "stylized", label: "Stylized", hint: "Clean shapes, fewer fragments" },
  { value: "balanced", label: "Balanced", hint: "Faithful yet playable" },
  { value: "faithful", label: "Faithful", hint: "Closest to the original" },
];

/** Shared card: the session's stored source (server asset, refresh-proof). */
function SourceCard({ session, caption }: { session: GenerationSessionFull; caption: string }) {
  const { project } = useStudioContext();
  const src = session.meta?.source;
  if (!src) return null;
  return (
    <div className="rounded-xl border border-[#dfe6d8] bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="flex items-center gap-1.5 text-[11px] font-bold text-[#183837]">
          <ImageIcon className="size-3.5 text-[#3a6b57]" aria-hidden />
          Your source
        </h4>
        <span className="text-[9px] text-[#9aa7a1]">
          {src.name} · {src.width}×{src.height} px · saved with the session
        </span>
      </div>
      <img
        src={`${sessionSourceUrl(project!.id, session.id)}&v=${encodeURIComponent(src.sha256)}`}
        alt="Stored session source"
        className="mx-auto mt-2 max-h-64 rounded-lg border border-[#e1e5df] object-contain"
      />
      <p className="mt-1.5 text-[10px] leading-relaxed text-[#778481]">{caption}</p>
    </div>
  );
}

/** One quality score chip (automatic assessment — never an artistic verdict). */
function ScoreChip({ label, value, passed }: { label: string; value: number; passed: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold ${
        passed ? "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]" : "border-[#e8cfc7] bg-[#fdf3f1] text-[#ba463f]"
      }`}
    >
      {label} · {value}
      {passed ? " ✓" : " ✕"}
    </span>
  );
}

/** 30C — Convert review: source vs result, two independent scores, gameplay
 *  difficulty, optimization outcome; commit blocked when inputs changed. */
function ConvertWorkspace({ session }: { session: GenerationSessionFull }) {
  const studio = useStudioContext();
  const {
    busy,
    project,
    convertImage,
    changeImageSettings,
    commitArtworkToEditor,
  } = studio;
  const [dialog, setDialog] = useState<"convert" | null>(null);
  const [view, setView] = useState<"finished" | "regions">("finished");
  const scores = session.meta?.conversionScores;
  const opt = session.meta?.difficultyOptimization;
  const measured = session.meta?.measuredDifficulty;
  const policy = (session.meta?.convertPolicy ?? {}) as Record<string, unknown>;
  // Backend sets buildInputsStale when source/settings change after a build;
  // commit also re-checks the identity server-side (defense in depth).
  const stale = session.meta?.buildInputsStale === true;
  const previewName = view === "finished" ? "colored.svg" : "numbered.svg";
  const running = session.status === "generating" || session.status === "compiling";
  const job = project?.job ?? { message: undefined };
  const m = /object (\d+)\/(\d+): (.+)$/.exec(job.message ?? "");
  const progress = useMemo(() => (m ? Math.max(0, Number(m[1]) - 1) : null), [m]);
  const objectsCount = (session.scenePlan?.objects ?? []).length;

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4">
      {stale && (
        <div className="rounded-xl border border-[#e8cfc7] bg-[#fdf3f1] px-4 py-3" role="alert">
          <p className="flex items-center gap-2 text-[11px] font-semibold text-[#ba463f]">
            <AlertTriangle className="size-4" aria-hidden /> Result out of date
          </p>
          <p className="mt-1 text-[10px] leading-relaxed text-[#8a4a44]">
            The source or the settings changed after this result was built. Run Convert again to
            commit — nothing was spent automatically.
          </p>
        </div>
      )}

      <SourceCard
        session={session}
        caption="Convert turns THIS artwork into a playable board — same look, real tap regions."
      />

      {/* Free settings: fidelity + difficulty */}
      <div className="rounded-xl border border-[#dfe6d8] bg-white p-4">
        <h4 className="flex items-center gap-1.5 text-[11px] font-bold text-[#183837]">
          <Palette className="size-3.5 text-[#3a6b57]" aria-hidden />
          Conversion settings <span className="font-normal text-[#9aa7a1]">— free to change</span>
        </h4>
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="cv-fidelity" className="text-[10px] text-[#657671]">Fidelity</Label>
            <Select
              value={session.fidelity ?? "balanced"}
              onValueChange={(v) =>
                void changeImageSettings({ fidelity: v as "stylized" | "balanced" | "faithful" }).catch((e: Error) => toast(e.message))
              }
              disabled={busy || running}
            >
              <SelectTrigger id="cv-fidelity" className="mt-0.5 h-9 rounded-md border-[#e1e5df] bg-white text-xs">
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
            <p className="mt-1 text-[9px] leading-snug text-[#9aa7a1]">
              Changing fidelity marks the current result out of date (no automatic spend).
            </p>
          </div>
          <div>
            <Label htmlFor="cv-tier" className="text-[10px] text-[#657671]">Difficulty</Label>
            <div className="mt-1 grid grid-cols-4 gap-1" role="group" aria-label="Target difficulty">
              {DIFFICULTY_TIERS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  aria-pressed={session.requestedDifficulty === t.key}
                  disabled={busy || running}
                  onClick={() =>
                    void changeImageSettings({ requested_difficulty: t.key as SessionTier }).catch((e: Error) => toast(e.message))
                  }
                  className={`rounded-md border px-1 py-1.5 text-[10px] font-semibold transition-colors disabled:opacity-50 ${
                    session.requestedDifficulty === t.key
                      ? "text-white"
                      : "border-[#dfe6d8] bg-white text-[#4c5b56] hover:border-[#b9c7bd]"
                  }`}
                  style={
                    session.requestedDifficulty === t.key
                      ? { background: t.color, borderColor: t.color }
                      : undefined
                  }
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {running ? (
          <div className="mt-3 rounded-lg border border-[#cce7dc] bg-[#edf6f2] px-3 py-2.5" aria-live="polite">
            <p className="flex items-center gap-2 text-[11px] font-semibold text-[#126e5e]">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              {session.status === "compiling" ? "Compiling and scoring…" : job?.message || "Converting…"}
            </p>
            {progress != null && objectsCount > 0 && (
              <p className="mt-1 text-[10px] text-[#657671]">
                {Math.min(progress, objectsCount)} / {objectsCount} segments associated
              </p>
            )}
          </div>
        ) : (
          <Button
            size="sm"
            disabled={busy}
            onClick={() => setDialog("convert")}
            className="mt-3 h-9 w-full rounded-md bg-[#0e554e] text-[11px] font-semibold text-white hover:bg-[#0a423d] sm:w-auto sm:px-6"
          >
            <Sparkles className="size-3.5" aria-hidden />
            {session.status === "ready_to_commit" ? "Run Convert again" : "Convert artwork"}
          </Button>
        )}
      </div>

      {/* Result review */}
      {session.status === "ready_to_commit" && (
        <div className="rounded-xl border border-[#dfe6d8] bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="flex items-center gap-1.5 text-[11px] font-bold text-[#183837]">
              <Eye className="size-3.5 text-[#3a6b57]" aria-hidden />
              Source vs result
            </h4>
            {/* Finished artwork / playable regions toggle */}
            <div className="flex gap-0.5 rounded-[9px] bg-[#e8ece5] p-0.5" role="group" aria-label="Result view">
              {(
                [
                  { key: "finished", label: "Finished artwork", icon: Eye },
                  { key: "regions", label: "Playable regions", icon: Grid2x2 },
                ] as const
              ).map((v) => (
                <button
                  key={v.key}
                  type="button"
                  aria-pressed={view === v.key}
                  onClick={() => setView(v.key)}
                  className={`rounded-md px-2 py-1 text-[9px] font-medium transition-colors ${
                    view === v.key
                      ? "bg-white text-[#087f74] shadow-[0_1px_4px_rgba(18,47,34,0.13)]"
                      : "text-[#657671] hover:text-[#183837]"
                  }`}
                >
                  <v.icon className="mr-1 inline size-3" aria-hidden />
                  {v.label}
                </button>
              ))}
            </div>
          </div>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <figure>
              <figcaption className="mb-1 text-[9px] font-semibold uppercase tracking-[0.1em] text-[#80908a]">
                Source
              </figcaption>
                      <img
                src={`${sessionSourceUrl(project!.id, session.id)}&v=${encodeURIComponent(session.meta?.source?.sha256 ?? "")}`}
                alt="Stored source"
                className="w-full rounded-lg border border-[#e1e5df] bg-[#f4f6f0] object-contain"
              />
            </figure>
            <figure>
              <figcaption className="mb-1 text-[9px] font-semibold uppercase tracking-[0.1em] text-[#80908a]">
                Result ({view === "finished" ? "artwork" : "numbered regions"})
              </figcaption>
                      <img
                src={`${sessionPreviewUrl(project!.id, session.id, previewName)}&v=${encodeURIComponent(session.updatedAt ?? "")}`}
                alt="Conversion result"
                className="w-full rounded-lg border border-[#e1e5df] bg-[#f4f6f0] object-contain"
              />
            </figure>
          </div>

          {scores && (
            <div className="mt-3">
              <p className="text-[9px] font-semibold uppercase tracking-[0.08em] text-[#80908a]">
                Automatic quality assessment — not a guarantee of artistic quality
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <ScoreChip label="Visual fidelity" value={scores.visualFidelity} passed={scores.visualFidelity >= scores.gates.visualGate} />
                <ScoreChip label="Game readiness" value={scores.gameReadiness} passed={scores.gameReadiness >= scores.gates.gameReadinessGate} />
                {typeof policy.visualGate === "number" && (
                  <span className="rounded-full border border-[#e1e5df] bg-white px-2 py-0.5 text-[9px] text-[#657671]">
                    Gates: fidelity ≥ {scores.gates.visualGate} · readiness ≥ {scores.gates.gameReadinessGate}
                  </span>
                )}
              </div>
              {scores.notes?.length ? (
                <ul className="mt-1.5 space-y-0.5">
                  {scores.notes.map((n) => (
                    <li key={n} className="flex gap-1 text-[10px] leading-snug text-[#957242]">
                      <span aria-hidden>•</span>
                      {n}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          )}

          <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px] sm:grid-cols-4">
            <div>
              <dt className="text-[#778481]">Requested</dt>
              <dd className="font-semibold capitalize text-[#183837]">{session.requestedDifficulty}</dd>
            </div>
            <div>
              <dt className="text-[#778481]">Measured</dt>
              <dd className="font-semibold capitalize text-[#183837]">
                {measured ? `${measured.rating} · ${measured.score}` : "—"}
              </dd>
            </div>
            <div>
              <dt className="text-[#778481]">Regions</dt>
              <dd className="font-semibold text-[#183837]">{session.meta?.regionCount ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-[#778481]">Objects</dt>
              <dd className="font-semibold text-[#183837]">{objectsCount || "—"}</dd>
            </div>
          </dl>

          {opt?.outcome && opt.outcome !== "target-reached" && opt.reasons?.length ? (
            <p className="mt-2 rounded-lg border border-[#e8d9b8] bg-[#fdf6e3] px-3 py-2 text-[10px] leading-relaxed text-[#957242]">
              {opt.outcome === "safe-ceiling" ? "Safe ceiling" : "Best safe result"}: {opt.reasons.slice(0, 3).join(" · ")}
            </p>
          ) : null}

          <div className="mt-3 flex justify-end">
            <Button
              size="sm"
              disabled={busy || stale}
              title={stale ? "Run Convert again — the inputs changed after this result" : "Promote this result into an immutable revision"}
              onClick={() => void commitArtworkToEditor().catch((e: Error) => toast(e.message))}
              className="h-9 rounded-md bg-[#087f74] text-[11px] font-semibold text-white hover:bg-[#056c62]"
            >
              <CheckCircle2 className="size-3.5" aria-hidden />
              Commit artwork
            </Button>
          </div>
        </div>
      )}

      <AlertDialog open={dialog === "convert"} onOpenChange={(open) => !open && setDialog(null)}>
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">Convert this artwork?</AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              This sends your stored source to the AI provider for semantic decomposition and runs
              the deterministic reconstruction — it may incur provider usage.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={() => {
                setDialog(null);
                void convertImage(true).catch((e: Error) => toast(e.message));
              }}
            >
              <RefreshCw className="size-3.5" aria-hidden />
              Convert
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/** Entry: route an image session to its workspace. */
export function ImageWorkspace({ session }: { session: GenerationSessionFull }) {
  if (!session.meta?.source) {
    // No stored source yet (older session): the ImageShell handles re-upload.
    return null;
  }
  if (session.mode === "image_reference") {
    // 30B — the plan step sends the STORED source to vision planning; the
    // rest of the journey is the Task-29 AI workspace verbatim.
    return (
      <div className="mx-auto w-full max-w-3xl space-y-4">
        <SourceCard
          session={session}
          caption="Use as Reference: the AI reads its mood, palette and subject, then draws a NEW original scene — your image is never traced."
        />
        <AiWorkspace session={session} planMode="reference" />
      </div>
    );
  }
  return <ConvertWorkspace session={session} />;
}
