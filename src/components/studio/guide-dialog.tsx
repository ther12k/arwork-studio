"use client";

/** "How it works" guide dialog — the 4-step artwork pipeline. */

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

export function GuideDialog() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 rounded-lg border-[#e1e5df] bg-white text-[10px] font-semibold hover:border-[#65a89b] hover:bg-[#f0f7f3]"
        >
          How it works
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl rounded-2xl border-[#e1e5df] bg-[#fbfcf8] sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold tracking-tight text-[#183837]">Your artwork pipeline</DialogTitle>
          <DialogDescription className="sr-only">
            The four-step pipeline from image to playable, exported artwork
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3.5 text-[12px] leading-relaxed text-[#183837]">
          <p>
            <b>1. Direct conversion:</b> upload an owned finished image as the master. Build a vector draft
            locally; no AI key required.
          </p>
          <p>
            <b>2. AI direction:</b> add an inspiration reference, describe the idea in chat, then generate an
            original master from the revised brief. Direct image edits are a separate, explicit action.
          </p>
          <p>
            <b>3. Review:</b> compare Master and Vector, inspect numbered regions, play test, merge adjacent
            regions, fix palette assignments and set object groups.
          </p>
          <p>
            <b>4. Export:</b> a versioned ZIP with real geometry and a detailed-vector renderer contract.
            Artwork IDs, versions and hashes protect saved progress.
          </p>
          <p className="text-[11px] text-[#778481]">
            This is a single-user local MVP. It has no public accounts, durable worker queue or multiplayer
            service. Live AI needs your server-side API key and provider charges.
          </p>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button className="rounded-md bg-[#087f74] text-white hover:bg-[#056c62]">Back to studio</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
