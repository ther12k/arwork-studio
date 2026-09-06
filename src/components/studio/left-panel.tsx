"use client";

/** Left panel — 01 / ART DIRECTION: workspace, reference, sample, chat. */

import { useRef, useState } from "react";
import { ArrowUp, ImagePlus, Sparkles, Upload, Wand2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
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
    messagesRef,
    createNewProject,
    openProjectById,
    uploadFile,
    promoteReference,
    loadSampleProject,
    sendChat,
  } = studio;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadRole = useRef<"reference" | "master">("reference");
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
