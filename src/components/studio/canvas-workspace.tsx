"use client";

/** Center panel — artwork workspace: header, brief, canvas, palette, job status. */

import { Crosshair, Maximize2, RotateCcw, Search, Sparkles, Undo2, ZoomIn, ZoomOut } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { imageUrl } from "@/lib/studio-api";
import { VIEW_LABELS, type StudioView, useStudioContext } from "./use-studio";

const VIEW_ORDER: StudioView[] = ["master", "colored", "numbered", "play", "inspect"];

function canvasTag(view: StudioView): string {
  if (view === "inspect") return "Select · merge · group · fix labels";
  if (view === "play") return "Play test · separate from game progress";
  return "Draft · review required";
}

/** Contrast-aware text color for a hex swatch. */
function swatchTextColor(hex: string): string {
  const rgb = hex.match(/\w\w/g)?.map((v) => parseInt(v, 16)) ?? [0, 0, 0];
  return rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114 > 150 ? "#152a2a" : "white";
}

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
          className="flex gap-0.5 rounded-[9px] bg-[#e8ece5] p-1"
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
        className="studio-canvas relative flex h-[clamp(440px,60vh,880px)] min-h-64 items-center justify-center overflow-hidden rounded-xl border border-[#dce2d9] p-3.5"
        role="img"
        aria-label="Artwork canvas"
      >
        {!hasMaster && !hasBundle && (
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
          <img
            src={imageUrl(project.id, "master", project.master!.sha256)}
            alt="Current approved artwork master"
            className="h-full w-full object-contain drop-shadow-[0_4px_15px_rgba(33,56,47,0.13)]"
          />
        )}
        {/* VectorBoard owns this element's children — React renders it empty, once. */}
        <svg
          ref={svgRef}
          xmlns="http://www.w3.org/2000/svg"
          className={`h-full w-full select-none drop-shadow-[0_4px_15px_rgba(33,56,47,0.06)] ${
            view === "master" || !hasBundle ? "hidden" : "block"
          }`}
          aria-label="Interactive vector artwork"
        />
        {hasBundle && view !== "master" && (
          <div className="pointer-events-none absolute bottom-5 left-5 z-[2] rounded-md bg-white/90 px-2.5 py-1.5 text-[9px] text-[#607767]">
            {canvasTag(view)}
          </div>
        )}
      </div>

      {/* Palette bar */}
      {hasBundle && view !== "master" && view !== "colored" && (
        <div className="mt-2.5">
          <div className="mb-1 flex flex-wrap items-center justify-between gap-1.5 text-[10px] text-[#778481]">
            <span id="studio-progress-text">
              {boardState
                ? `${completed} / ${total} regions filled · ${mistakes} incorrect attempts`
                : "Choose a color to test"}
            </span>
            <div className="flex gap-0.5">
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
                title={`${p.name} · group ${p.id}`}
                aria-label={`${p.name}, group ${p.id}`}
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
