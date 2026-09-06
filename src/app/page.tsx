import type { Metadata } from "next";

import { StudioPage } from "@/components/studio/studio-page";

export const metadata: Metadata = {
  title: "Color Duel · Art Studio",
  description:
    "Author playable coloring artwork: compile an approved master image into vector paint regions, review, play-test and export game-ready files.",
};

export default function Page() {
  return <StudioPage />;
}
