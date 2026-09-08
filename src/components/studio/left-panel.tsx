"use client";

/** Left panel — 01 / ART DIRECTION: workspace, reference, sample, chat. */

import { useRef, useState } from "react";
import { ArrowUp, ImagePlus, Sparkles, Upload, Wand2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { imageUrl } from "@/lib/studio-api";
import { useStudioContext } from "./use-studio";

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <span className="block text-[9px] font-semibold uppercase tracking-[0.16em] text-[#80908a]">{children}</span>
  );
}

function MicroCaption({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <p className={`text-[10px] leading-relaxed text-[#778481] ${className}`}>{children}</p>;
}

async function withToast(fn: () => Promise<void>) {
  try {
    await fn();
  } catch (e) {
    toast((e as Error).message);
  }
}

export function LeftPanel() {
  const studio = useStudioContext();
  const {
    config,
    projects,
    project,
    busy,
    titleInput,
    setTitleInput,
    chatInput,
    setChatInput,
    paidConsent,
    setPaidConsent,
    rightsConfirmed,
    setRightsConfirmed,
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
    messagesRef,
    createNewProject,
    openProjectById,
    uploadFile,
    promoteReference,
    loadSampleProject,
    loadSvgSampleProject,
    uploadSvgFile,
    generateSvg,
    sendChat,
  } = studio;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadRole = useRef<"reference" | "master">("reference");
  const svgFileInputRef = useRef<HTMLInputElement | null>(null);
  const [referenceError, setReferenceError] = useState<string | null>(null);

  const aiConfigured = config?.ai.configured ?? false;

  const pickFile = (role: "reference" | "master") => {
    if (role === "master" && !rightsConfirmed) {
      setReferenceError("Confirm ownership or permission before importing a master.");
      toast("Confirm ownership or permission before importing a master.");
      return;
    }
    setReferenceError(null);
    uploadRole.current = role;
    fileInputRef.current?.click();
  };

  const onFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    await withToast(() => uploadFile(file, uploadRole.current));
  };

  const pickSvgFile = () => {
    if (!rightsConfirmed) {
      toast("Confirm ownership or permission before importing a master.");
      return;
    }
    svgFileInputRef.current?.click();
  };

  const onSvgFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    await withToast(() => uploadSvgFile(file));
  };


  return (
    <aside className="studio-left-panel" aria-label="Art direction">
      <div className="flex items-center justify-between">
        <Eyebrow>01 / ART DIRECTION</Eyebrow>
        <Button
          variant="outline"
          size="icon"
          className="size-7 rounded-md border-[#e1e5df] text-lg font-normal"
          title="New project"
          aria-label="New project"
          disabled={busy}
          onClick={() => void withToast(() => createNewProject())}
        >
          ＋
        </Button>
      </div>

      <label htmlFor="studio-project-list" className="studio-label">
        Workspace
      </label>
      <Select
        value={project?.id ?? ""}
        onValueChange={(value) => void withToast(() => openProjectById(value))}
        disabled={busy}
      >
        <SelectTrigger id="studio-project-list" className="w-full bg-white">
          <SelectValue placeholder="Select a project" />
        </SelectTrigger>
        <SelectContent>
          {projects.map((p) => (
            <SelectItem key={p.id} value={p.id}>
              {p.title}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <label htmlFor="studio-title" className="studio-label">
        Artwork title
      </label>
      <Input
        id="studio-title"
        value={titleInput}
        maxLength={100}
        className="bg-white"
        onChange={(e) => setTitleInput(e.target.value)}
        placeholder="New illustrated world"
      />

      {/* Reference card */}
      <div className="mt-4 rounded-xl border border-dashed border-[#c5d1c8] bg-white p-3">
        {project?.reference ? (
          <img
            src={imageUrl(project.id, "reference", project.reference.sha256)}
            alt="Uploaded inspiration reference"
            className="max-h-36 w-full rounded-lg object-contain"
          />
        ) : (
          <div className="flex flex-col items-center gap-1 px-2 py-2 text-center">
            <Upload className="mb-1 size-6 text-[#087f74]" aria-hidden />
            <strong className="text-xs font-semibold text-[#183837]">Start with an image</strong>
            <span className="text-[10px] text-[#778481]">PNG, JPEG or WebP · up to 12 MB</span>
          </div>
        )}
        <div className="mt-2 flex gap-1.5">
          <Button
            variant="outline"
            size="sm"
            className="h-8 flex-1 rounded-md border-[#e1e5df] bg-white px-2 text-[10px] hover:border-[#65a89b] hover:bg-[#f0f7f3]"
            disabled={busy}
            onClick={() => pickFile("reference")}
          >
            <ImagePlus className="size-3.5" aria-hidden />
            Add reference
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-8 flex-1 rounded-md border-[#e1e5df] bg-white px-2 text-[10px] hover:border-[#65a89b] hover:bg-[#f0f7f3]"
            disabled={busy}
            onClick={() => pickFile("master")}
          >
            <Upload className="size-3.5" aria-hidden />
            Upload master
          </Button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          className="hidden"
          onChange={(e) => void onFileChange(e)}
          aria-hidden
          tabIndex={-1}
        />
        <label className="mt-2 flex items-start gap-1.5 text-[10px] font-normal text-[#657671]">
          <Checkbox
            checked={rightsConfirmed}
            onCheckedChange={(v) => setRightsConfirmed(v === true)}
            className="mt-0.5 size-3.5"
            aria-label="I own or have permission to use this image"
          />
          I own or have permission to use this image.
        </label>
        {referenceError && <p className="mt-1 text-[10px] text-red-700">{referenceError}</p>}
        <MicroCaption>
          A reference guides the brief. Only use an owned/licensed image as a direct tracing or editing source.
        </MicroCaption>
        <button
          type="button"
          className="mt-1 block px-0 py-1.5 text-left text-[11px] font-medium text-[#087f74] hover:underline disabled:opacity-50"
          disabled={busy || !project?.reference}
          onClick={() => void withToast(() => promoteReference())}
        >
          Use reference as master
        </button>
      </div>

      <Button
        variant="outline"
        className="mt-3.5 h-auto w-full justify-start whitespace-normal rounded-xl border-[#d4e6d8] bg-[#edf6f0] py-2.5 text-left text-[11px] font-semibold hover:border-[#65a89b] hover:bg-[#e2f0e8]"
        disabled={busy}
        onClick={() => void withToast(() => loadSampleProject())}
      >
        <Wand2 className="size-4 shrink-0 text-[#087f74]" aria-hidden />
        <span className="leading-tight">
          Try the bundled treehouse
          <span className="mt-0.5 block text-[9px] font-normal text-[#778481]">No AI key needed</span>
        </span>
      </Button>

      {/* SVG master card — curve-preserving import route */}
      <div className="mt-2.5 rounded-xl border border-[#d4e6d8] bg-white p-3">
        <strong className="block text-xs font-semibold text-[#183837]">SVG master (curves preserved)</strong>
        <Button
          variant="outline"
          className="mt-2 h-auto w-full justify-start whitespace-normal rounded-lg border-[#d4e6d8] bg-[#edf6f0] px-2.5 py-2 text-left text-[11px] font-semibold hover:border-[#65a89b] hover:bg-[#e2f0e8]"
          disabled={busy}
          onClick={() => void withToast(() => loadSvgSampleProject())}
        >
          <Wand2 className="size-3.5 shrink-0 text-[#087f74]" aria-hidden />
          <span className="leading-tight">
            Load curved SVG example
            <span className="mt-0.5 block text-[9px] font-normal text-[#778481]">
              Curves, holes, gradients · no AI key
            </span>
          </span>
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="mt-1.5 h-8 w-full rounded-lg border-[#e1e5df] bg-white px-2 text-[10px] hover:border-[#65a89b] hover:bg-[#f0f7f3]"
          disabled={busy}
          onClick={pickSvgFile}
        >
          <Upload className="size-3.5" aria-hidden />
          Import SVG master…
        </Button>
        <input
          ref={svgFileInputRef}
          type="file"
          accept=".svg,image/svg+xml"
          className="hidden"
          onChange={(e) => void onSvgFileChange(e)}
          aria-hidden
          tabIndex={-1}
        />
        <MicroCaption className="mt-1.5">
          Imported SVG masters keep curves, holes, gradients, transforms and drawing order — nothing is rasterized
          or retraced. Uses the rights confirmation above.
        </MicroCaption>

        {/* AI SVG generation (paid) */}
        <div className="mt-3 border-t border-[#eef1eb] pt-2.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold text-[#657671]">AI SVG generation (paid)</span>
            <span className="text-[9px] text-[#778481]" title={aiConfigured ? config?.ai.imageModel : undefined}>
              {aiConfigured ? config?.ai.imageModel : "API key required"}
            </span>
          </div>
          <Textarea
            value={svgPrompt}
            onChange={(e) => setSvgPrompt(e.target.value)}
            placeholder="Describe a clean vector-style illustration to author as SVG…"
            maxLength={3000}
            rows={2}
            className="mt-1.5 resize-none rounded-lg bg-white text-[11px]"
            aria-label="SVG generation prompt"
          />
          <div className="mt-1.5 flex items-center gap-1.5">
            <Select
              value={svgAspect}
              onValueChange={(v) => setSvgAspect(v as typeof svgAspect)}
              disabled={busy}
            >
              <SelectTrigger
                className="h-8 flex-1 rounded-md border-[#e1e5df] bg-white text-[10px]"
                aria-label="SVG master aspect ratio"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1024x1536">Portrait 576×768</SelectItem>
                <SelectItem value="1536x1024">Landscape 768×576</SelectItem>
                <SelectItem value="1024x1024">Square 640×640</SelectItem>
              </SelectContent>
            </Select>
            <Select
              value={svgGenMode}
              onValueChange={(v) => setSvgGenMode(v as typeof svgGenMode)}
              disabled={busy}
            >
              <SelectTrigger
                className="h-8 flex-1 rounded-md border-[#e1e5df] bg-white text-[10px]"
                aria-label="SVG generation mode"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="single">One-shot SVG</SelectItem>
                <SelectItem value="multistage">Multi-stage vector</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {svgGenMode === "multistage" && (
            <div className="mt-1.5">
              <label htmlFor="studio-svg-target-regions" className="text-[10px] font-normal text-[#657671]">
                Target regions (60–1200)
              </label>
              <Input
                id="studio-svg-target-regions"
                type="number"
                min={60}
                max={1200}
                step={10}
                value={svgTargetRegions}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  if (e.target.value !== "" && Number.isFinite(v)) setSvgTargetRegions(v);
                }}
                onBlur={() => setSvgTargetRegions(Math.min(1200, Math.max(60, Math.round(svgTargetRegions) || 300)))}
                className="mt-1 h-8 rounded-md bg-white text-xs"
              />
              <MicroCaption className="mt-1">
                Scene plan → per-object vector fragments → compose. The next Build auto-enables
                auto-subdivide toward this target.
              </MicroCaption>
            </div>
          )}
          <label className="mt-1.5 flex items-start gap-1.5 text-[10px] font-normal text-[#657671]">
            <Checkbox
              checked={svgPaidConsent}
              onCheckedChange={(v) => setSvgPaidConsent(v === true)}
              className="mt-0.5 size-3.5"
              aria-label="Allow this paid AI request, including provider charges"
            />
            Allow this paid request.
          </label>
          <Button
            size="sm"
            className="mt-1.5 h-9 w-full rounded-lg bg-[#087f74] text-[10px] font-semibold text-white hover:bg-[#056c62]"
            disabled={!aiConfigured || busy}
            title={aiConfigured ? "Generate an SVG master with AI (paid)" : "API key required"}
            onClick={() => void withToast(() => generateSvg())}
          >
            <Sparkles className="size-3.5" aria-hidden />
            Generate SVG master
          </Button>
          {!aiConfigured && (
            <MicroCaption className="mt-1.5 flex items-center gap-1 text-[#a77627]">
              <Sparkles className="size-3" aria-hidden />
              AI SVG generation needs an API key. Import a hand-made SVG instead.
            </MicroCaption>
          )}
        </div>
      </div>

      {/* Chat */}
      <div className="mt-6 mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold tracking-tight text-[#183837]">Art direction chat</h2>
        <span className="text-[9px] text-[#778481]" title={aiConfigured ? config?.ai.chatModel : undefined}>
          {aiConfigured ? config?.ai.chatModel : "API key required"}
        </span>
      </div>

      <div
        ref={messagesRef}
        className="studio-scroll max-h-[166px] min-h-[130px] overflow-y-auto rounded-lg"
        aria-label="Chat messages"
      >
        {project && project.messages.length > 0 ? (
          project.messages.map((m, i) => (
            <p
              key={i}
              className={`mb-2 whitespace-pre-wrap rounded-[10px] px-3 py-2.5 text-[11px] leading-relaxed ${
                m.role === "user" ? "ml-4 bg-[#e5f3ed] text-[#183837]" : "bg-[#eef1eb] text-[#183837]"
              }`}
            >
              {m.content}
            </p>
          ))
        ) : (
          <p className="px-1 py-2.5 text-[11px] italic leading-relaxed text-[#96a09a]">
            &ldquo;A lantern-lit treehouse beside a waterfall. Keep the detailed scene, but use larger colorable
            shapes.&rdquo;
          </p>
        )}
      </div>

      <div className="relative mt-2">
        <Textarea
          value={chatInput}
          onChange={(e) => setChatInput(e.target.value)}
          placeholder="Describe an idea or refine the current brief…"
          maxLength={3000}
          rows={2}
          className="resize-none rounded-lg bg-white pr-11 text-[11px]"
          aria-label="Chat message"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !busy && aiConfigured) {
              e.preventDefault();
              void withToast(() => sendChat());
            }
          }}
        />
        <Button
          size="icon"
          aria-label="Send to AI"
          title={aiConfigured ? "Send to AI" : "API key required"}
          disabled={!aiConfigured || busy}
          className="absolute bottom-2.5 right-2 size-8 rounded-lg bg-[#087f74] text-white hover:bg-[#056c62]"
          onClick={() => void withToast(() => sendChat())}
        >
          <ArrowUp className="size-4" aria-hidden />
        </Button>
      </div>

      <label className="mt-2 flex items-start gap-1.5 text-[10px] font-normal text-[#657671]">
        <Checkbox
          checked={paidConsent}
          onCheckedChange={(v) => setPaidConsent(v === true)}
          className="mt-0.5 size-3.5"
          aria-label="Allow this AI request, including provider charges"
        />
        Allow this AI request, including provider charges.
      </label>
      <MicroCaption className="mt-1.5">
        Images stay local during vector conversion. Chat/generation sends the selected inputs to the AI provider.
      </MicroCaption>

      {!aiConfigured && (
        <MicroCaption className="mt-2 flex items-center gap-1 text-[#a77627]">
          <Sparkles className="size-3" aria-hidden />
          AI chat and image generation need an API key. Local tools still work.
        </MicroCaption>
      )}
    </aside>
  );
}
