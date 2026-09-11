"use client";

/** Color Duel Art Studio — single-page authoring tool (client shell). */

import { Sparkles } from "lucide-react";

import { GuideDialog } from "./guide-dialog";
import { CanvasWorkspace } from "./canvas-workspace";
import { CreateArtwork } from "./create-artwork";
import { LeftPanel } from "./left-panel";
import { RightPanel } from "./right-panel";
import { useStudioContext, StudioProvider } from "./use-studio";

function Header() {
  const studio = useStudioContext();
  const aiConfigured = studio.config?.ai.configured ?? false;
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[#e1e5df] bg-[#fbfcf8] px-5 py-4 md:px-7">
      <div className="flex items-center gap-3">
        <span
          className="flex size-11 items-center justify-center rounded-[13px] bg-[#e5f3ed] text-[26px] leading-none text-[#087f74]"
          aria-hidden
        >
          ◈
        </span>
        <div>
          <strong className="block text-[17px] font-bold tracking-tight text-[#183837]">Color Duel</strong>
          <span className="mt-0.5 block text-[9px] font-semibold tracking-[0.2em] text-[#778481]">
            ART STUDIO
          </span>
        </div>
        <span className="ml-1 rounded-[5px] bg-[#edf0e9] px-1.5 py-1 text-[9px] font-semibold tracking-[0.15em] text-[#657671]">
          LOCAL MVP
        </span>
      </div>
      <div className="flex items-center gap-3">
        <span
          className={`whitespace-nowrap rounded-full border px-2.5 py-1.5 text-[10px] ${
            aiConfigured
              ? "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]"
              : "border-[#e1e5df] text-[#778481]"
          }`}
          role="status"
        >
          {aiConfigured ? (
            <>
              <Sparkles className="mr-1 inline size-3" aria-hidden />
              Local compiler + AI connected
            </>
          ) : (
            "Local compiler ready · AI not configured"
          )}
        </span>
        <GuideDialog />
      </div>
    </header>
  );
}

function Footer() {
  return (
    <footer className="mt-auto border-t border-[#e1e5df] bg-[#fbfcf8] px-5 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] text-center text-[10px] text-[#778481] md:px-7">
      Color Duel Art Studio · local authoring MVP · automatic output is a reviewable draft — not a guaranteed
      publish-ready asset
    </footer>
  );
}

/** Task 28 — the center workspace switches between Create mode (artwork-less
 *  project or a resumed generation session) and Editor mode. The shell —
 *  header, project selector, side panels — stays stable so the transition
 *  Create → Generate → Commit → Editor feels like one app. */
function Workspace() {
  const { centerView } = useStudioContext();
  return centerView === "create" ? <CreateArtwork /> : <CanvasWorkspace />;
}

export function StudioPage() {
  return (
    <StudioProvider>
      <div className="flex min-h-screen flex-col bg-[#f5f4ef] text-[13px] font-[450] text-[#183837]">
        <Header />
        <main className="studio-main w-full flex-1">
          <LeftPanel />
          <Workspace />
          <RightPanel />
        </main>
        <Footer />
      </div>
    </StudioProvider>
  );
}
