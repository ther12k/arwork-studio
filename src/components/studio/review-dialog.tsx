"use client";

/** Dialog for recording a self-attested visual review note. */

import { useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { useStudioContext } from "./use-studio";

const MIN_CHARS = 10;

export function ReviewDialog({ children }: { children: React.ReactNode }) {
  const studio = useStudioContext();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const valid = note.trim().length >= MIN_CHARS;

  const submit = async () => {
    if (!valid) return;
    setSubmitting(true);
    try {
      await studio.submitReviewNote(note.trim());
      setNote("");
      setOpen(false);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="max-w-lg rounded-2xl border-[#e1e5df] bg-[#fbfcf8] sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg font-bold tracking-tight text-[#183837]">
            Mark visually reviewed
          </DialogTitle>
          <DialogDescription className="text-[11px] leading-relaxed text-[#778481]">
            Record what you checked — boundaries, labels, small targets and the completed appearance. This
            does not certify rights or ranked balance.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={5}
          maxLength={1500}
          placeholder="e.g. Checked all boundaries along the waterfall, labels are readable, smallest regions still feel tappable, colors match the master…"
          aria-label="Review note"
          className="min-h-28 resize-y rounded-lg bg-white text-xs leading-relaxed"
        />
        <p className={`text-[10px] ${valid ? "text-[#778481]" : "text-[#957242]"}`} aria-live="polite">
          {valid ? `${note.trim().length} / 1500 characters` : `At least ${MIN_CHARS} characters required.`}
        </p>
        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            size="sm"
            className="rounded-md border-[#e1e5df] bg-white"
            onClick={() => setOpen(false)}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            className="rounded-md bg-[#087f74] text-white hover:bg-[#056c62]"
            disabled={!valid || submitting}
            onClick={() => void submit()}
          >
            <CheckCircle2 className="size-4" aria-hidden />
            {submitting ? "Recording…" : "Record review"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
