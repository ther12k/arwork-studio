import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./globals.css";

import { createRoot } from "react-dom/client";

import { StudioPage } from "@/components/studio/studio-page";
import { Toaster } from "@/components/ui/sonner";

// No StrictMode on purpose: the previous Next config ran with
// reactStrictMode: false and the canvas/board code was verified that way.
createRoot(document.getElementById("root")!).render(
  <>
    <StudioPage />
    <Toaster position="bottom-center" />
  </>,
);
