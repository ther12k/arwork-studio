"use client";

/** Task 29 — Create-with-AI workspace: the artist journey from a free
 * session to a committed revision in three stages.
 *
 *   1. Scene Plan    — paid AI planning (explicit confirm), semantic object
 *                      list, structured edits, and artist chat that becomes
 *                      STRUCTURED plan mutations (one strict-JSON translation
 *                      call — the conversation is never resent).
 *   2. Generate      — one vector call per object with per-object progress.
 *   3. Review/Commit — compiled preview, QA/difficulty summary, per-object
 *                      targeted regeneration (objectId preserved), lock
 *                      semantics, and one explicit commit into the editor.
 *
 * Every paid step shows its own confirmation next to the action; buttons
 * disable while a job runs, so a re-render can never double-submit. */

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Check,
  ChevronDown,
  Clock,
  Hammer,
  Layers,
  Loader2,
  Lock,
  LockOpen,
  Mountain,
  Pencil,
  RefreshCw,
  Send,
  Sparkles,
  Star,
  Trees,
  X,
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
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { sessionPreviewUrl, type GenerationSessionFull, type ScenePlanObject } from "@/lib/studio-api";
import { useStudioContext } from "./use-studio";

const ROLE_ICON: Record<string, typeof Star> = {
  background: Mountain,
  midground: Layers,
  foreground: Trees,
  subject: Star,
  accent: Sparkles,
};

const ROLES = ["background", "midground", "foreground", "subject", "accent"] as const;

const STAGES = [
  { key: "plan", label: "Scene Plan" },
  { key: "generate", label: "Generate Artwork" },
  { key: "review", label: "Review & Commit" },
] as const;

function stageOf(s: GenerationSessionFull, editingPlan: boolean): "plan" | "generate" | "review" {
  if (s.status === "ready_to_commit" && !editingPlan) return "review";
  if (s.status === "generating" || s.status === "compiling") return "generate";
  return "plan";
}

function StageHeader({ stage }: { stage: "plan" | "generate" | "review" }) {
  const idx = STAGES.findIndex((s) => s.key === stage);
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-1" aria-label="Generation stages">
      {STAGES.map((s, i) => (
        <li key={s.key} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-[#b9c7bd]" aria-hidden>→</span>}
          <span
            className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase tracking-[0.06em] ${
              i === idx
                ? "border-[#087f74] bg-[#e5f3ed] text-[#087f74]"
                : i < idx
                  ? "border-[#cce7dc] bg-white text-[#3a6b57]"
                  : "border-[#e1e5df] bg-white text-[#9aa7a1]"
            }`}
          >
            {i < idx ? <Check className="size-3" aria-hidden /> : null}
            {s.label}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** One scene-plan object row: semantic summary + lock + edit + regenerate. */
function ObjectRow({
  obj,
  generated,
  regenBusy,
  pendingFields,
  onEdit,
  onToggleLock,
  onRegenerate,
}: {
  obj: ScenePlanObject;
  generated: boolean;
  regenBusy: boolean;
  pendingFields?: string[];
  onEdit: (obj: ScenePlanObject) => void;
  onToggleLock: (obj: ScenePlanObject) => void;
  onRegenerate: (obj: ScenePlanObject) => void;
}) {
  const [open, setOpen] = useState(false);
  const locked = !!obj.generation?.locked;
  const pending = pendingFields?.length ?? 0;
  const Icon = ROLE_ICON[obj.role] ?? Layers;
  return (
    <li className="rounded-lg border border-[#e1e5df] bg-white">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex min-h-11 w-full items-center gap-2.5 px-3 py-2 text-left"
      >
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-[#edf6f2] text-[#3a6b57]">
          <Icon className="size-3.5" aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[11px] font-semibold text-[#183837]">{obj.name}</span>
          <span className="block truncate text-[10px] capitalize text-[#778481]">
            {obj.role} · detail {Number(obj.detailWeight ?? 1).toFixed(1)}
            {generated ? " · generated ✓" : ""}
          </span>
        </span>
        {pending > 0 && (
          <span
            className="rounded-full border border-[#e8cfc7] bg-[#fdf3f1] px-1.5 py-0.5 text-[9px] font-semibold text-[#ba463f]"
            title={`Artwork not updated yet: ${pendingFields!.join(", ")}`}
          >
            Needs regen
          </span>
        )}
        {locked && (
          <span className="flex items-center gap-1 rounded-full border border-[#e8d9b8] bg-[#fdf6e3] px-1.5 py-0.5 text-[9px] font-semibold text-[#957242]">
            <Lock className="size-3" aria-hidden /> Locked
          </span>
        )}
        <ChevronDown className={`size-3.5 shrink-0 text-[#9aa7a1] transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open && (
        <div className="border-t border-[#eef2ea] px-3 py-2.5">
          {obj.description && (
            <p className="text-[10px] leading-relaxed text-[#657671]">{obj.description}</p>
          )}
          {obj.fills?.length > 0 && (
            <div className="mt-1.5 flex items-center gap-1.5">
              <span className="text-[9px] text-[#9aa7a1]">Palette</span>
              {obj.fills.slice(0, 6).map((hex) => (
                <span
                  key={hex}
                  className="inline-block size-3.5 rounded-full border border-[#c3ced4]"
                  style={{ background: hex }}
                  title={hex}
                />
              ))}
            </div>
          )}
          <div className="mt-2 flex flex-wrap gap-1.5">
            <Button
              variant="outline"
              size="sm"
              className="h-7 rounded-md border-[#e1e5df] bg-white px-2 text-[10px]"
              onClick={() => onEdit(obj)}
            >
              <Pencil className="size-3" aria-hidden />
              Edit
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 rounded-md border-[#e1e5df] bg-white px-2 text-[10px]"
              disabled={regenBusy}
              onClick={() => onToggleLock(obj)}
            >
              {locked ? <LockOpen className="size-3" aria-hidden /> : <Lock className="size-3" aria-hidden />}
              {locked ? "Unlock" : "Lock"}
            </Button>
            {generated && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 rounded-md border-[#cfe6db] bg-[#e5f3ed] px-2 text-[10px] font-semibold text-[#126e5e] hover:bg-[#d9ede2]"
                disabled={locked || regenBusy}
                title={locked ? "Unlock this object before regenerating it" : "Regenerate just this object (paid)"}
                onClick={() => onRegenerate(obj)}
              >
                <RefreshCw className="size-3" aria-hidden />
                Regenerate
              </Button>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

/** Inline structured editor for one planned object (update_object mutation). */
function ObjectEditor({
  obj,
  saving,
  onSave,
  onClose,
}: {
  obj: ScenePlanObject;
  saving: boolean;
  onSave: (objectId: string, changes: Record<string, unknown>) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState(obj.name);
  const [description, setDescription] = useState(obj.description ?? "");
  const [role, setRole] = useState<string>(obj.role);
  const [detail, setDetail] = useState(String(obj.detailWeight ?? 1));
  return (
    <div className="mb-2 rounded-lg border border-[#cfe6db] bg-[#edf6f2] p-3">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-bold text-[#183837]">Edit {obj.name}</h4>
        <Button variant="ghost" size="icon" className="size-6 rounded-md" aria-label="Close editor" onClick={onClose}>
          <X className="size-3.5" aria-hidden />
        </Button>
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <div>
          <Label htmlFor={`obj-name-${obj.id}`} className="text-[10px] text-[#657671]">Name</Label>
          <Input id={`obj-name-${obj.id}`} value={name} onChange={(e) => setName(e.target.value)} className="mt-0.5 h-8 rounded-md bg-white text-xs" maxLength={80} />
        </div>
        <div>
          <Label htmlFor={`obj-role-${obj.id}`} className="text-[10px] text-[#657671]">Role</Label>
          <Select value={role} onValueChange={setRole}>
            <SelectTrigger id={`obj-role-${obj.id}`} className="mt-0.5 h-8 rounded-md border-[#e1e5df] bg-white text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ROLES.map((r) => (
                <SelectItem key={r} value={r} className="capitalize">{r}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor={`obj-desc-${obj.id}`} className="text-[10px] text-[#657671]">Description for generation</Label>
          <Textarea id={`obj-desc-${obj.id}`} value={description} onChange={(e) => setDescription(e.target.value)} rows={2} maxLength={400} className="mt-0.5 min-h-10 resize-y rounded-md border-[#e1e5df] bg-white text-xs" />
        </div>
        <div>
          <Label htmlFor={`obj-detail-${obj.id}`} className="text-[10px] text-[#657671]">Detail weight (0.2–4)</Label>
          <Input id={`obj-detail-${obj.id}`} type="number" min={0.2} max={4} step={0.1} value={detail}
            onChange={(e) => setDetail(e.target.value)} className="mt-0.5 h-8 w-28 rounded-md bg-white text-xs" />
        </div>
      </div>
      <div className="mt-2.5 flex justify-end gap-2">
        <Button variant="outline" size="sm" className="h-8 rounded-md border-[#e1e5df] bg-white text-[10px]" onClick={onClose}>
          Cancel
        </Button>
        <Button
          size="sm"
          disabled={saving}
          className="h-8 rounded-md bg-[#0e554e] text-[10px] font-semibold text-white hover:bg-[#0a423d]"
          onClick={() =>
            onSave(obj.id, {
              name: name.trim() || obj.name,
              description: description.trim(),
              role,
              detailWeight: Math.max(0.2, Math.min(4, Number(detail) || 1)),
            })
          }
        >
          {saving ? "Saving…" : "Save change"}
        </Button>
      </div>
    </div>
  );
}

export function AiWorkspace({ session }: { session: GenerationSessionFull }) {
  const studio = useStudioContext();
  const {
    busy,
    project,
    planSceneWithAi,
    revisePlanWithAi,
    editScenePlan,
    generateArtwork,
    regenerateObject,
    recompileSessionArtwork,
    commitArtworkToEditor,
  } = studio;
  const plan = session.scenePlan;
  const objects = plan?.objects ?? [];
  const hasArtwork = !!session.meta?.generationStages || session.meta?.artworkStale === true || session.status === "ready_to_commit";
  const [editingPlan, setEditingPlan] = useState(false);
  const [editObj, setEditObj] = useState<ScenePlanObject | null>(null);
  const [chatInput, setChatInput] = useState("");
  const [dialog, setDialog] = useState<"plan" | "generate" | "regen" | "chat" | null>(null);
  const [regenTarget, setRegenTarget] = useState<ScenePlanObject | null>(null);
  const [regenHint, setRegenHint] = useState("");
  const [regenKeepPosition, setRegenKeepPosition] = useState(true);
  const job = project?.job ?? { status: undefined, message: undefined };
  const stage = stageOf(session, editingPlan);
  const lastChat = session.meta?.lastPlanChat;
  // Visual plan changes the artwork does not reflect yet (per object):
  // the board cannot be committed until each entry is regenerated.
  const pendingChanges: Record<string, string[]> =
    (session.meta?.pendingArtworkChanges as Record<string, string[]> | undefined) ?? {};
  const hasPending = Object.keys(pendingChanges).length > 0;
  const stale = hasPending;

  // per-object progress from the job message ("Vectorizing object i/N: name")
  const progress = useMemo(() => {
    if (session.status !== "generating" && session.status !== "compiling") return null;
    const m = /object (\d+)\/(\d+): (.+)$/.exec(job.message ?? "");
    if (!m) return { done: 0, total: objects.length, current: null as string | null };
    const done = Math.max(0, Number(m[1]) - 1);
    return { done, total: Number(m[2]), current: m[3] };
  }, [session.status, job.message, objects.length]);

  const saveObjectEdit = async (objectId: string, changes: Record<string, unknown>) => {
    try {
      await editScenePlan([{ op: "update_object", objectId, changes }]);
      setEditObj(null);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const toggleLock = async (obj: ScenePlanObject) => {
    try {
      await editScenePlan([{ op: "update_object", objectId: obj.id, changes: { generation: { locked: !obj.generation?.locked } } }]);
      toast(obj.generation?.locked ? `${obj.name} unlocked.` : `${obj.name} locked — bulk regeneration keeps its artwork.`);
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const applyChat = async () => {
    const instruction = chatInput.trim();
    if (!instruction) {
      toast("Write what should change about the scene.");
      return;
    }
    setDialog(null);
    try {
      await revisePlanWithAi(instruction, true);
      setChatInput("");
    } catch (e) {
      toast((e as Error).message);
    }
  };

  const sessionFailed = session.status === "failed";
  const canRegen = hasArtwork && !busy;

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4">
      <header>
        <span className="mb-2 block text-[9px] font-semibold uppercase tracking-[0.18em] text-[#80908a]">
          CREATE WITH AI
        </span>
        <StageHeader stage={stage} />
      </header>

      {sessionFailed && (
        <div className="rounded-xl border border-[#e8cfc7] bg-[#fdf3f1] px-4 py-3" role="alert">
          <p className="flex items-center gap-2 text-[11px] font-semibold text-[#ba463f]">
            <AlertTriangle className="size-4" aria-hidden /> Generation needs attention
          </p>
          <p className="mt-1 text-[10px] leading-relaxed text-[#8a4a44]">
            {session.error ?? "The last step failed."} Your project and any committed revision are
            untouched — retry when ready.
          </p>
        </div>
      )}

      {/* ---------------------------------------------------------- stage 1 */}
      {stage === "plan" && (
        <div className="space-y-3">
          {editingPlan && session.status === "ready_to_commit" && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 rounded-md px-2 text-[10px] text-[#657671] hover:bg-[#f0f7f3]"
              onClick={() => setEditingPlan(false)}
            >
              ← Back to review
            </Button>
          )}
          <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-bold text-[#183837]">{plan?.title || "Your idea"}</h3>
              <span className="flex gap-1.5 text-[9px] text-[#778481]">
                <span className="rounded-full border border-[#e1e5df] bg-white px-2 py-0.5 capitalize">
                  {session.requestedDifficulty}
                </span>
                <span className="rounded-full border border-[#e1e5df] bg-white px-2 py-0.5">
                  {session.aspect === "1536x1024" ? "Landscape" : session.aspect === "1024x1024" ? "Square" : "Portrait"}
                </span>
              </span>
            </div>
            {session.prompt && (
              <p className="mt-2 rounded-lg bg-[#f4f6f0] px-3 py-2 text-[11px] leading-relaxed text-[#4c5b56]">
                {session.prompt}
              </p>
            )}
            {objects.length === 0 ? (
              <>
                <p className="mt-3 flex items-center gap-1.5 text-[11px] italic text-[#778481]">
                  <Clock className="size-3.5" aria-hidden /> Scene hasn't been planned yet.
                </p>
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => setDialog("plan")}
                  className="mt-3 h-9 w-full rounded-md bg-[#0e554e] text-[11px] font-semibold text-white hover:bg-[#0a423d] sm:w-auto sm:px-6"
                >
                  <Sparkles className="size-3.5" aria-hidden />
                  Plan scene with AI
                </Button>
              </>
            ) : (
              <>
                <div className="mt-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="text-[13px] font-bold text-[#183837]">{plan!.title}</span>
                  <span className="text-[10px] text-[#778481]">
                    {objects.length} objects · target {plan!.targetRegionRange?.[0]}–{plan!.targetRegionRange?.[1]} regions
                  </span>
                </div>
                {stale && (
                  <p className="mt-2 rounded-lg border border-[#e8cfc7] bg-[#fdf3f1] px-3 py-2 text-[10px] leading-relaxed text-[#ba463f]">
                    The generated artwork does not reflect your latest plan yet:
                    {Object.entries(pendingChanges).map(([oid, fields]) => (
                      <span key={oid} className="block">
                        • {objects.find((o) => o.id === oid)?.name ?? oid} — {fields.join(", ")}
                      </span>
                    ))}
                    Regenerate those objects (or everything) before committing.
                  </p>
                )}
                <ul className="mt-2.5 space-y-1.5">
                  {editObj && (
                    <ObjectEditor obj={editObj} saving={busy} onSave={saveObjectEdit} onClose={() => setEditObj(null)} />
                  )}
                  {objects.map((o) => (
                    <ObjectRow
                      key={o.id}
                      obj={o}
                      generated={hasArtwork}
                      regenBusy={busy}
                      pendingFields={pendingChanges[o.id]}
                      onEdit={setEditObj}
                      onToggleLock={toggleLock}
                      onRegenerate={(target) => {
                        setRegenTarget(target);
                        setRegenHint("");
                        setRegenKeepPosition(true);
                        setDialog("regen");
                      }}
                    />
                  ))}
                </ul>
                {lastChat && (
                  <p className="mt-2 rounded-lg bg-[#f0f7f3] px-3 py-2 text-[10px] leading-relaxed text-[#126e5e]" role="status">
                    AI: {lastChat.summary} ({lastChat.applied} change{lastChat.applied === 1 ? "" : "s"} applied)
                  </p>
                )}
                {/* artist chat → structured mutations */}
                <div className="mt-3 rounded-lg border border-[#e1e5df] bg-[#f7f8f3] p-2.5">
                  <Label htmlFor="ai-plan-chat" className="text-[10px] text-[#657671]">
                    Tell the AI what to change — e.g. “make the waterfall larger and remove the tree”
                  </Label>
                  <div className="mt-1 flex gap-2">
                    <Input
                      id="ai-plan-chat"
                      value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      placeholder="Make the sky warmer at sunset…"
                      maxLength={600}
                      disabled={busy}
                      className="h-9 flex-1 rounded-md bg-white text-xs"
                    />
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy || !chatInput.trim()}
                      onClick={() => setDialog("chat")}
                      className="h-9 rounded-md border-[#cfe6db] bg-white px-3 text-[10px] font-semibold text-[#126e5e] hover:bg-[#f0f7f3]"
                    >
                      <Send className="size-3.5" aria-hidden />
                      Apply
                    </Button>
                  </div>
                  <p className="mt-1 text-[9px] leading-snug text-[#9aa7a1]">
                    The AI returns structured plan edits (one cheap JSON call) — the deterministic
                    engine applies them; the conversation is never resent.
                  </p>
                </div>
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => setDialog("generate")}
                  className="mt-3 h-9 w-full rounded-md bg-[#0e554e] text-[11px] font-semibold text-white hover:bg-[#0a423d] sm:w-auto sm:px-6"
                >
                  <Hammer className="size-3.5" aria-hidden />
                  Scene looks good — Generate artwork
                </Button>
                {hasArtwork && !stale && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => void recompileSessionArtwork().catch((e: Error) => toast(e.message))}
                    className="ml-2 mt-3 h-9 rounded-md border-[#e1e5df] bg-white text-[10px]"
                    title="Rebuild the board from the existing artwork (free) — applies difficulty/metadata changes without regenerating"
                  >
                    <RefreshCw className="size-3.5" aria-hidden />
                    Recompile without generating
                  </Button>
                )}
                {stale && hasArtwork && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled
                    className="ml-2 mt-3 h-9 rounded-md border-[#e1e5df] bg-white text-[10px] opacity-50"
                    title={`A plain recompile cannot apply visual changes: ${Object.keys(pendingChanges).join(", ")} still pending — regenerate them first.`}
                  >
                    <RefreshCw className="size-3.5" aria-hidden />
                    Recompile (visual changes pending)
                  </Button>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------- stage 2 */}
      {stage === "generate" && (
        <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5" aria-live="polite">
          <h3 className="flex items-center gap-2 text-sm font-bold text-[#183837]">
            <Loader2 className="size-4 animate-spin text-[#087f74]" aria-hidden />
            {session.status === "compiling" ? "Compiling the board…" : "Generating artwork"}
          </h3>
          {progress && objects.length > 0 && (
            <>
              <div className="mt-2 flex items-center gap-2 text-[11px] text-[#4c5b56]">
                <span className="font-semibold">
                  {Math.min(progress.done, objects.length)} / {objects.length} objects
                </span>
                {progress.current && <span className="truncate">· {progress.current}</span>}
              </div>
              <Progress value={(Math.min(progress.done, objects.length) / objects.length) * 100}
                className="mt-1.5 h-1.5 bg-[#e1e5df] [&>div]:bg-[#087f74]" aria-hidden />
              <ul className="mt-3 grid gap-x-4 gap-y-1 sm:grid-cols-2">
                {objects.map((o, i) => (
                  <li key={o.id} className="flex items-center gap-1.5 text-[10px]">
                    {i < progress.done ? (
                      <Check className="size-3.5 shrink-0 text-[#087f74]" aria-hidden />
                    ) : i === progress.done ? (
                      <Loader2 className="size-3.5 shrink-0 animate-spin text-[#087f74]" aria-hidden />
                    ) : (
                      <span className="size-3.5 shrink-0 rounded-full border border-[#dfe6d8]" aria-hidden />
                    )}
                    <span className={i <= progress.done ? "text-[#183837]" : "text-[#9aa7a1]"}>{o.name}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------- stage 3 */}
      {stage === "review" && session.status === "ready_to_commit" && (
        <div className="space-y-3">
          <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5">
            <h3 className="text-sm font-bold text-[#183837]">Review the draft</h3>
            <div className="mt-2.5 overflow-hidden rounded-lg border border-[#e1e5df] bg-[#f4f6f0]">
              <img
                src={`${sessionPreviewUrl(project!.id, session.id, "colored.svg")}&v=${encodeURIComponent(session.updatedAt ?? "")}`}
                alt="Generated artwork preview"
                className="mx-auto max-h-96 w-full object-contain"
              />
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px] sm:grid-cols-4">
              <div>
                <dt className="text-[#778481]">QA</dt>
                <dd className="font-semibold text-[#087f74]">{session.meta?.qa?.passed ? "Passed ✓" : "Failed"}</dd>
              </div>
              <div>
                <dt className="text-[#778481]">Difficulty</dt>
                <dd className="font-semibold capitalize text-[#183837]">
                  {session.meta?.measuredDifficulty
                    ? `${session.meta.measuredDifficulty.rating} · ${session.meta.measuredDifficulty.score}`
                    : "—"}
                </dd>
              </div>
              <div>
                <dt className="text-[#778481]">Regions</dt>
                <dd className="font-semibold text-[#183837]">{session.meta?.regionCount ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-[#778481]">Requested</dt>
                <dd className="font-semibold capitalize text-[#183837]">
                  {session.requestedDifficulty}
                  {session.meta?.measuredDifficulty?.rating === session.requestedDifficulty ? " ✓" : ""}
                </dd>
              </div>
            </dl>
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => setEditingPlan(true)}
                className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]"
              >
                <Pencil className="size-3.5" aria-hidden />
                Continue editing plan
              </Button>
              <Button
                size="sm"
                disabled={busy}
                onClick={() => void commitArtworkToEditor().catch((e: Error) => toast(e.message))}
                className="h-9 rounded-md bg-[#087f74] text-[11px] font-semibold text-white hover:bg-[#056c62]"
              >
                <CheckCircle2 className="size-3.5" aria-hidden />
                Commit artwork
              </Button>
            </div>
          </div>
          <div className="rounded-xl border border-[#dfe6d8] bg-white p-4 sm:p-5">
            <h4 className="text-[11px] font-bold text-[#183837]">
              Objects ({objects.length}) — regenerate one at a time, everything else stays untouched
            </h4>
            <ul className="mt-2 space-y-1.5">
              {objects.map((o) => (
                <ObjectRow
                  key={o.id}
                  obj={o}
                  generated
                  regenBusy={busy}
                  pendingFields={pendingChanges[o.id]}
                  onEdit={(target) => {
                    setEditingPlan(true);
                    setEditObj(target);
                  }}
                  onToggleLock={toggleLock}
                  onRegenerate={(target) => {
                    setRegenTarget(target);
                    setRegenHint("");
                    setRegenKeepPosition(true);
                    setDialog("regen");
                  }}
                />
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* -------------------------------------------------- confirm dialogs */}
      <AlertDialog open={dialog === "plan"} onOpenChange={(open) => !open && setDialog(null)}>
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">Plan this scene with AI?</AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              This sends your brief to the configured AI provider and may incur provider usage. It
              drafts a revisable object plan — no artwork is generated yet.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={() => {
                setDialog(null);
                void planSceneWithAi(true).catch((e: Error) => toast(e.message));
              }}
            >
              <Sparkles className="size-3.5" aria-hidden />
              Confirm & Plan
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={dialog === "generate"} onOpenChange={(open) => !open && setDialog(null)}>
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              Generate {objects.length || ""} artwork object{objects.length === 1 ? "" : "s"}?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              This runs approximately {objects.length || "several"} AI generation calls (one vector
              fragment per object) and may incur provider usage. The board is compiled and QA-checked
              automatically afterwards.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={() => {
                setDialog(null);
                void generateArtwork(true).catch((e: Error) => toast(e.message));
              }}
            >
              <Hammer className="size-3.5" aria-hidden />
              Generate artwork
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={dialog === "chat"} onOpenChange={(open) => !open && setDialog(null)}>
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">Revise the plan with AI?</AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              “{chatInput.trim()}” — this sends your instruction and the current object list to the AI
              provider (one cheap JSON call) and applies the structured edits it returns.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={applyChat}
            >
              <Send className="size-3.5" aria-hidden />
              Apply changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={dialog === "regen"} onOpenChange={(open) => !open && setDialog(null)}>
        <AlertDialogContent className="rounded-xl border-[#cfe6db] sm:max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-left text-sm text-[#183837]">
              Regenerate {regenTarget?.name}?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-left text-[11px] leading-relaxed text-[#657671]">
              One AI call replaces this object's artwork — its identity and the rest of the scene stay
              untouched; the board is recompiled and QA-checked. This may incur provider usage.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <Label htmlFor="regen-hint" className="text-[10px] text-[#657671]">What should change?</Label>
            <Textarea
              id="regen-hint"
              value={regenHint}
              onChange={(e) => setRegenHint(e.target.value)}
              rows={2}
              maxLength={400}
              placeholder="Make it wider with three cascading levels."
              className="min-h-14 resize-y rounded-md border-[#e1e5df] bg-white text-xs"
            />
            <label className="flex items-center gap-1.5 text-[10px] text-[#657671]">
              <input
                type="checkbox"
                checked={regenKeepPosition}
                onChange={(e) => setRegenKeepPosition(e.target.checked)}
                className="size-3.5 accent-[#087f74]"
              />
              Keep this object in the same position
            </label>
          </div>
          <AlertDialogFooter className="gap-2 sm:justify-end">
            <AlertDialogCancel className="h-9 rounded-md border-[#e1e5df] bg-white text-[10px]">Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="h-9 rounded-md bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
              disabled={busy}
              onClick={() => {
                const target = regenTarget;
                setDialog(null);
                if (!target) return;
                const hint = regenHint.trim()
                  ? regenKeepPosition
                    ? regenHint.trim()
                    : `${regenHint.trim()} (you may reinterpret its placement within its planned area.)`
                  : regenKeepPosition
                    ? ""
                    : "You may reinterpret this object's placement within its planned area.";
                void regenerateObject(target.id, hint, true).catch((e: Error) => toast(e.message));
              }}
            >
              <RefreshCw className="size-3.5" aria-hidden />
              Regenerate object
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
