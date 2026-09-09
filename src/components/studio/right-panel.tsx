"use client";

/** Right panel — 01 / REGION COMPILER + 02 / REVIEW & EXPORT.
 *  Stage 2 adds the auto-subdivide switch (+ multistage prefill note) and the
 *  difficulty profile panel (contract §5). */

import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Download,
  FileArchive,
  Gauge,
  Image as ImageIcon,
  Loader2,
  Merge,
  Paintbrush,
  SplitSquareHorizontal,
  Tag,
  Layers,
  Crosshair,
  Palette as PaletteIcon,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { exportUrl, renderUrl, type DifficultyProfile, type ImageQuality } from "@/lib/studio-api";
import { useStudioContext } from "./use-studio";
import { ReviewDialog } from "./review-dialog";
import {
  DIFFICULTY_TIERS as TIERS,
  PlaytestValidatedBadge,
  formatPlaytestClock,
  normalizeDifficulty,
} from "./difficulty";

function MicroCaption({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <p className={`text-[10px] leading-relaxed text-[#778481] ${className}`}>{children}</p>;
}

/** Pull the ±N px tolerance out of the flattened-rings derivation note. */
function flattenToleranceLine(derivation: string): string | null {
  const match = /±\s*([\d.]+)\s*px/.exec(derivation);
  return match ? match[1] : null;
}

/** Region ids referenced by a QA warning/error line ("r-00001", "r-m-abc123…").
 *  Empty when the line is not actionable (no region drill-down possible). */
function regionIdsIn(text: string): string[] {
  const ids = text.match(/\br-[A-Za-z0-9_-]{4,}\b/g);
  return ids ? [...new Set(ids)] : [];
}

/** "#RRGGBB" only — the backend recolor route rejects anything else. */
const HEX_COLOR_RE = /^#[0-9A-Fa-f]{6}$/;

// ---------------------------------------------------- difficulty profile panel

const num = (v: unknown): number | null => {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

/** Full difficulty profile (contract §5): 4-tier segmented bar + score +
 *  metric rows with units + a palette-ambiguity chip. Stage 3 (contract B)
 *  adds the play-test rows (count, median, pace, mistakes/region) and the
 *  "Playtest validated" pill when a completed run was recorded. Legacy
 *  manifests carry the string "unrated" — normalized to an Unrated note. */
function DifficultyPanel({ raw, validated }: { raw: string | DifficultyProfile | undefined; validated?: boolean }) {
  const normalized = normalizeDifficulty(raw);
  if (!normalized) {
    return (
      <div className="rp-wide mt-4 rounded-[10px] border border-[#e1e5df] bg-[#f4f6f0] px-3 py-2.5">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold text-[#778481]">
          <Gauge className="size-3.5" aria-hidden />
          Difficulty profile
        </div>
        <p className="mt-1 text-[10px] leading-relaxed text-[#8a968f]">
          Unrated — not computed for this revision. Rebuild with the upgraded compiler to analyze difficulty.
        </p>
      </div>
    );
  }
  const { tier: activeTier, score } = normalized;
  const tierIndex = TIERS.indexOf(activeTier);
  const profile = normalized.profile;
  const metrics = profile.metrics ?? {};
  const ambiguity = metrics.paletteAmbiguity;
  const ambiguityTone =
    ambiguity === "high"
      ? "border-[#e8cfc7] bg-[#fdf3f1] text-[#ba463f]"
      : ambiguity === "medium"
        ? "border-[#e8d9b8] bg-[#fdf6e3] text-[#957242]"
        : "border-[#cce7dc] bg-[#e5f3ed] text-[#087f74]";
  const rows: Array<[string, string]> = [
    ["Regions", num(metrics.regionCount)?.toLocaleString("en-US") ?? "—"],
    ["Median region area", num(metrics.medianRegionArea) != null ? `${num(metrics.medianRegionArea)!.toLocaleString("en-US", { maximumFractionDigits: 1 })} px²` : "—"],
    ["Tiny targets", num(metrics.tinyRegionPct) != null ? `${num(metrics.tinyRegionPct)!.toFixed(1)} %` : "—"],
    ["Required zoom", num(metrics.requiredZoom) != null ? `${num(metrics.requiredZoom)!.toFixed(1)} ×` : "—"],
    ["Palette groups", num(metrics.paletteGroups)?.toLocaleString("en-US") ?? "—"],
    ["Avg neighbours", num(metrics.avgNeighbors) != null ? num(metrics.avgNeighbors)!.toFixed(2) : "—"],
    ["Subdivision edges", num(metrics.subdivisionEdges)?.toLocaleString("en-US") ?? "—"],
    ["Object density", num(metrics.objectDensity) != null ? num(metrics.objectDensity)!.toFixed(2) : "—"],
    ["Label clearance", String(metrics.labelClearance ?? "—")],
  ];
  // Play-test factor (contract B) — only when a run has been recorded.
  if (num(metrics.playtestCount) != null)
    rows.push(["Playtests", num(metrics.playtestCount)!.toLocaleString("en-US")]);
  if (num(metrics.playtestMedianSeconds) != null)
    rows.push(["Median completion", formatPlaytestClock(num(metrics.playtestMedianSeconds)!)]);
  if (num(metrics.playtestSecondsPerRegion) != null)
    rows.push(["Pace", `${num(metrics.playtestSecondsPerRegion)!.toFixed(1)} s/region`]);
  if (num(metrics.playtestMistakesPerRegion) != null)
    rows.push(["Mistakes/region", num(metrics.playtestMistakesPerRegion)!.toFixed(2)]);
  return (
    <div className="rp-wide mt-4 rounded-[10px] border border-[#dfe6d8] bg-[#eff3ec] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold text-[#6c7c6b]">
          <Gauge className="size-3.5" aria-hidden />
          Difficulty profile
        </div>
        <span className="flex flex-wrap items-center gap-1.5">
          <span
            className="rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase tracking-[0.08em]"
            style={{ color: TIERS[tierIndex].color, borderColor: TIERS[tierIndex].color, background: "white" }}
          >
            {TIERS[tierIndex].label}
          </span>
          {validated && <PlaytestValidatedBadge />}
        </span>
      </div>
      {/* 4-tier segmented bar: each block = 25 points; filled up to score */}
      <div
        className="mt-2 flex h-2.5 gap-0.5 overflow-hidden rounded-full"
        role="img"
        aria-label={`Difficulty score ${score} of 100 — ${TIERS[tierIndex].label}`}
      >
        {TIERS.map((t, i) => {
          const fill = Math.max(0, Math.min(1, (score - i * 25) / 25));
          return (
            <div key={t.key} className="relative h-full flex-1 rounded-[2px] bg-[#e1e5df]" title={`${t.label}: ${i * 25}–${t.max}`}>
              <div className="h-full rounded-[2px]" style={{ width: `${fill * 100}%`, background: t.color }} />
            </div>
          );
        })}
      </div>
      <div className="mt-1 flex justify-between text-[9px] text-[#778481]">
        <span className={tierIndex === 0 ? "font-bold text-[#087f74]" : ""}>Easy</span>
        <span className={tierIndex === 1 ? "font-bold text-[#087f74]" : ""}>Medium</span>
        <span className={tierIndex === 2 ? "font-bold text-[#087f74]" : ""}>Hard</span>
        <span className={tierIndex === 3 ? "font-bold text-[#087f74]" : ""}>Master</span>
      </div>
      <p className="mt-1.5 text-[11px] font-bold text-[#183837]">
        Score {score} / 100
      </p>
      {ambiguity != null && (
        <span className={`mt-1.5 inline-block rounded-full border px-2 py-0.5 text-[9px] font-semibold capitalize ${ambiguityTone}`}>
          Palette ambiguity · {String(ambiguity)}
        </span>
      )}
      <dl className="mt-2 grid grid-cols-2 gap-x-2.5 gap-y-1 text-[10px] leading-snug">
        {rows.map(([label, value]) => (
          <div key={label} className="flex items-baseline justify-between gap-1.5">
            <dt className="text-[#778481]">{label}</dt>
            <dd className="font-semibold text-[#183837]">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function NumberField({
  id,
  label,
  value,
  min,
  max,
  step = 1,
  onCommit,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onCommit: (v: number) => void;
}) {
  return (
    <div>
      <Label htmlFor={id} className="text-[10px] leading-snug text-[#657671]">
        {label}
      </Label>
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        className="mt-1 h-8 rounded-md bg-white text-xs"
        onChange={(e) => {
          const v = Number(e.target.value);
          if (e.target.value !== "" && Number.isFinite(v)) onCommit(v);
        }}
        onBlur={(e) => {
          const v = Number(e.target.value);
          if (!Number.isFinite(v) || v < min || v > max) onCommit(Math.min(max, Math.max(min, Number.isFinite(v) ? v : min)));
        }}
      />
    </div>
  );
}

export function RightPanel() {
  const studio = useStudioContext();
  const {
    config,
    project,
    revision,
    bundle,
    view,
    busy,
    buildSettings,
    setBuildSetting,
    quality,
    setQuality,
    editPalette,
    setEditPalette,
    objectGroup,
    setObjectGroup,
    revisionSelect,
    setRevisionSelect,
    selected,
    placing,
    selectionInfo,
    startPlacing,
    clearSelection,
    inspectRegion,
    runEdit,
    build,
    activateSelectedRevision,
  } = studio;
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Recolor appearance (distinct from the palette number-group action)
  const [recolorHex, setRecolorHex] = useState("#66AA33");
  const [preserveShading, setPreserveShading] = useState(false);
  const hexValid = HEX_COLOR_RE.test(recolorHex);

  const canBuild = !!project?.master && !busy;
  const qa = revision?.qa;

  const doEdit = (action: Parameters<typeof runEdit>[0], extra?: Parameters<typeof runEdit>[1]) =>
    void runEdit(action, extra).catch((e: Error) => toast(e.message));

  const stats: Array<{ value: number | string; label: string }> = [
    { value: revision?.regionCount ?? "—", label: "tap regions" },
    { value: qa?.paletteGroups ?? "—", label: "color groups" },
    { value: qa?.paintPaths ?? "—", label: "paint paths" },
  ];

  return (
    <aside className="studio-right-panel" aria-label="Region compiler and export">
      {/* ---------------------------------------------------- 01 / COMPILER */}
      <div className="rp-wide">
        <span className="mb-2 block text-[9px] font-semibold uppercase tracking-[0.16em] text-[#80908a]">
          01 / REGION COMPILER
        </span>
        <h2 className="mb-2 text-[17px] font-bold tracking-tight text-[#183837]">Detail, without busywork.</h2>
        <p className="mb-4 text-[11px] leading-relaxed text-[#778481]">
          Visual shading and player tap regions are two different layers.
        </p>
      </div>

      <div className="flex items-baseline justify-between">
        <Label htmlFor="studio-target-regions" className="studio-label mb-1.5 mt-0">
          Target regions
        </Label>
        <output htmlFor="studio-target-regions" className="text-sm font-bold text-[#087f74]">
          {buildSettings.target_regions}
        </output>
      </div>
      <Slider
        id="studio-target-regions"
        min={100}
        max={1600}
        step={50}
        value={[buildSettings.target_regions]}
        onValueChange={(v) => setBuildSetting("target_regions", v[0] ?? 650)}
        className="py-1 [&_.bg-primary]:bg-[#087f74] [&_[data-slot=slider-range]]:bg-[#087f74] [&_[data-slot=slider-thumb]]:border-[#087f74]"
        aria-label="Target regions"
      />
      <MicroCaption>An approximate target, not a guaranteed count.</MicroCaption>

      {/* Auto-subdivide (contract §6): split the largest regions with organic
          cuts until ~target. Prefilled from multi-stage generation hints. */}
      <div className="mt-3 flex items-start justify-between gap-2.5 rounded-[10px] border border-[#dfe6d8] bg-[#eff3ec] px-3 py-2.5">
        <div className="min-w-0">
          <Label htmlFor="studio-auto-subdivide" className="text-[11px] font-semibold leading-snug text-[#183837]">
            Auto-subdivide to target
          </Label>
          <p className="mt-0.5 text-[10px] leading-relaxed text-[#778481]">
            Split the largest regions with organic cuts until the target count (SVG-master builds stay true
            vector).{" "}
            {project?.pendingBuildSettings && (
              <span className="font-semibold text-[#087f74]">Suggested by multi-stage generation.</span>
            )}
          </p>
        </div>
        <Switch
          id="studio-auto-subdivide"
          checked={!!buildSettings.auto_subdivide}
          onCheckedChange={(v) => setBuildSetting("auto_subdivide", v === true)}
          disabled={busy}
          className="mt-0.5 data-[state=checked]:bg-[#087f74]"
          aria-label="Auto-subdivide regions to the target count"
        />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <NumberField
          id="studio-palette-colors"
          label="Palette groups"
          value={buildSettings.palette_colors}
          min={4}
          max={80}
          onCommit={(v) => setBuildSetting("palette_colors", v)}
        />
        <NumberField
          id="studio-paint-colors"
          label="Paint tones"
          value={buildSettings.paint_colors}
          min={16}
          max={160}
          onCommit={(v) => setBuildSetting("paint_colors", v)}
        />
      </div>

      <Label htmlFor="studio-max-edge" className="studio-label mb-1.5">
        Vector sampling resolution
      </Label>
      <Select
        value={String(buildSettings.max_edge)}
        onValueChange={(v) => setBuildSetting("max_edge", Number(v))}
        disabled={busy}
      >
        <SelectTrigger id="studio-max-edge" className="h-8 w-full rounded-md bg-white text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="768">768 px · quick draft</SelectItem>
          <SelectItem value="1024">1024 px · balanced</SelectItem>
          <SelectItem value="1536">1536 px · detailed / heavier</SelectItem>
        </SelectContent>
      </Select>

      {/* Geometry backend + curve fit tolerance */}
      <Label htmlFor="studio-geometry-backend" className="studio-label mb-1.5">
        Geometry backend
      </Label>
      <Select
        value={buildSettings.backend ?? "spline-local"}
        onValueChange={(v) => setBuildSetting("backend", v)}
        disabled={busy}
      >
        <SelectTrigger id="studio-geometry-backend" className="h-8 w-full rounded-md bg-white text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="spline-local">Curved masters (spline fit)</SelectItem>
          <SelectItem value="polygon-legacy">Legacy polygons (comparison)</SelectItem>
        </SelectContent>
      </Select>

      <div className="mt-3 flex items-baseline justify-between">
        <Label htmlFor="studio-curve-tolerance" className="studio-label mb-1.5 mt-0">
          Curve fit tolerance
        </Label>
        <output htmlFor="studio-curve-tolerance" className="text-sm font-bold text-[#087f74]">
          ±{(buildSettings.curve_tolerance ?? 1).toFixed(1)} px
        </output>
      </div>
      <Slider
        id="studio-curve-tolerance"
        min={0.2}
        max={2}
        step={0.1}
        value={[buildSettings.curve_tolerance ?? 1]}
        onValueChange={(v) => setBuildSetting("curve_tolerance", v[0] ?? 1)}
        className="py-1 [&_.bg-primary]:bg-[#087f74] [&_[data-slot=slider-range]]:bg-[#087f74] [&_[data-slot=slider-thumb]]:border-[#087f74]"
        aria-label="Curve fit tolerance"
      />
      <MicroCaption>How far the spline fit may deviate from traced pixel edges.</MicroCaption>

      {/* Advanced settings */}
      <button
        type="button"
        onClick={() => setAdvancedOpen((o) => !o)}
        aria-expanded={advancedOpen}
        className="mt-3.5 flex w-full items-center gap-1.5 rounded-md py-1.5 text-left text-[11px] font-medium text-[#778481] hover:text-[#183837]"
      >
        <ChevronDown className={`size-3.5 transition-transform ${advancedOpen ? "" : "-rotate-90"}`} aria-hidden />
        Advanced settings
      </button>
      {advancedOpen && (
        <div className="mt-1 grid grid-cols-2 gap-2">
          <NumberField
            id="studio-compactness"
            label="Shape compactness (lower = less regular)"
            value={buildSettings.compactness}
            min={1}
            max={25}
            onCommit={(v) => setBuildSetting("compactness", v)}
          />
          <NumberField
            id="studio-min-area"
            label="Merge fragments below (pixels)"
            value={buildSettings.min_region_pixels}
            min={4}
            max={600}
            onCommit={(v) => setBuildSetting("min_region_pixels", v)}
          />
          <NumberField
            id="studio-min-radius"
            label="Minimum number clearance"
            value={buildSettings.min_label_radius}
            min={1}
            max={12}
            step={0.5}
            onCommit={(v) => setBuildSetting("min_label_radius", v)}
          />
          <NumberField
            id="studio-ink"
            label="Dark-ink threshold (0 disables)"
            value={buildSettings.ink_threshold}
            min={0}
            max={80}
            onCommit={(v) => setBuildSetting("ink_threshold", v)}
          />
          <div className="col-span-2">
            <div className="flex items-baseline justify-between">
              <Label htmlFor="studio-corner-angle" className="text-[10px] leading-snug text-[#657671]">
                Corner threshold (spline fit)
              </Label>
              <output htmlFor="studio-corner-angle" className="text-xs font-bold text-[#087f74]">
                {Math.round(buildSettings.corner_angle_deg ?? 60)}°
              </output>
            </div>
            <Slider
              id="studio-corner-angle"
              min={25}
              max={110}
              step={5}
              value={[buildSettings.corner_angle_deg ?? 60]}
              onValueChange={(v) => setBuildSetting("corner_angle_deg", v[0] ?? 60)}
              className="mt-1 py-1 [&_.bg-primary]:bg-[#087f74] [&_[data-slot=slider-range]]:bg-[#087f74] [&_[data-slot=slider-thumb]]:border-[#087f74]"
              aria-label="Corner threshold"
            />
          </div>
          <div className="col-span-2">
            <Label htmlFor="studio-quality" className="studio-label mb-1.5">
              AI image quality
            </Label>
            <Select
              value={quality}
              onValueChange={(v) => setQuality(v as ImageQuality)}
              disabled={!(config?.ai.configured ?? false)}
            >
              <SelectTrigger id="studio-quality" className="h-8 w-full rounded-md bg-white text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="low">low</SelectItem>
                <SelectItem value="medium">medium</SelectItem>
                <SelectItem value="high">high</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      )}

      <Button
        className="rp-wide mt-3.5 w-full justify-between rounded-xl bg-[#087f74] py-3 text-[11px] font-semibold text-white hover:bg-[#056c62]"
        disabled={!canBuild}
        onClick={() => void build().catch((e: Error) => toast(e.message))}
      >
        {busy && project?.job?.kind === "vector compilation" ? (
          <>
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Building…
          </>
        ) : (
          <>
            Build vector draft
            <span aria-hidden>→</span>
          </>
        )}
      </Button>
      {!project?.master && (
        <MicroCaption className="rp-wide mt-1.5">
          Load a master image first — upload one or try the bundled treehouse.
        </MicroCaption>
      )}

      <div className="rp-wide my-5 h-px bg-[#e1e5df]" />

      {/* ------------------------------------------------ 02 / REVIEW & EXPORT */}
      <div className="rp-wide">
        <span className="mb-2 block text-[9px] font-semibold uppercase tracking-[0.16em] text-[#80908a]">
          02 / REVIEW &amp; EXPORT
        </span>

        <div className="mb-4 mt-3 flex justify-between gap-2.5">
          {stats.map((s) => (
            <div key={s.label} className="flex-1 text-center">
              <strong className="block text-[23px] font-bold tracking-tight text-[#183837]">{s.value}</strong>
              <span className="text-[9px] text-[#778481]">{s.label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* QA box */}
      <div
        className={`rp-wide rounded-[10px] border px-3 py-2.5 text-[10px] leading-relaxed ${
          qa ? "border-[#dfe6d8] bg-[#eff3ec] text-[#6c7c6b]" : "border-[#e1e5df] bg-[#f4f6f0] text-[#8a968f]"
        }`}
        aria-live="polite"
      >
        {qa ? (
          <>
            <p className={`mb-1 font-bold ${qa.passed ? "text-[#30704f]" : "text-[#ba463f]"}`}>
              {qa.passed ? "✓ Geometry checks passed" : "Geometry needs attention"}
            </p>
            {qa.humanReviewed && (
              <p className="font-bold text-[#167a5d]">✓ Self-attested visual review recorded</p>
            )}
            {qa.geometry && (
              <div className="mt-1.5 border-t border-[#d7e0cf] pt-1.5">
                <p className="font-bold">Geometry (schema {qa.geometry.schema})</p>
                <p className="mt-0.5">
                  {qa.geometry.curvedCommands.toLocaleString("en-US")} /{" "}
                  {qa.geometry.totalCommands.toLocaleString("en-US")} curved commands (
                  {Math.round((qa.geometry.curvedCommandRatio ?? 0) * 100)}%)
                </p>
                <p>
                  hit-test: {qa.geometry.hitTestProbes.toLocaleString("en-US")} probes ·{" "}
                  {qa.geometry.hitTestConflicts} conflicts
                </p>
                {flattenToleranceLine(qa.geometry.flattenedDerivation) && (
                  <p>flattened rings derived at ±{flattenToleranceLine(qa.geometry.flattenedDerivation)} px</p>
                )}
                <p>partition band {qa.geometry.partitionToleranceAllowance.toFixed(2)} px²</p>
                {(qa.missingArea !== undefined || qa.overlapArea !== undefined) && (
                  <p>
                    missing {(qa.missingArea ?? 0).toFixed(2)} px² · overlap {(qa.overlapArea ?? 0).toFixed(2)} px²
                  </p>
                )}
              </div>
            )}
            {qa.visualReview && (
              <div className="mt-2 rounded-[8px] border border-[#e8d9b8] bg-[#fdf6e3] px-2.5 py-2">
                <p className="font-bold text-[#957242]">Visual review (separate from geometry tests)</p>
                <p className="mt-1 font-semibold text-[#957242]">Required — geometry tests cannot detect:</p>
                <ul className="mt-1 space-y-1">
                  {qa.visualReview.limitations.map((limitation, i) => (
                    <li key={i} className="flex gap-1.5 text-[#957242]">
                      <span aria-hidden>·</span>
                      <span>{limitation}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {qa.warnings.length > 0 && (
              <ul className="studio-scroll mt-1.5 max-h-40 space-y-1 overflow-y-auto pr-1">
                {qa.warnings.map((w, i) => {
                  const ids = regionIdsIn(w);
                  if (!ids.length) {
                    // Not actionable (no region drill-down) — plain warning line.
                    return (
                      <li key={i} className="flex gap-1.5 text-[#957242]">
                        <AlertTriangle className="mt-0.5 size-3 shrink-0" aria-hidden />
                        <span>{w}</span>
                      </li>
                    );
                  }
                  const first = ids[0];
                  return (
                    <li key={i}>
                      <Button
                        variant="ghost"
                        className="h-auto w-full justify-start gap-1.5 whitespace-normal rounded-md px-1.5 py-1 text-left text-[10px] font-normal leading-relaxed text-[#957242] hover:bg-[#ece6d5] hover:text-[#957242]"
                        onClick={() => inspectRegion(first)}
                        aria-label={`Select region ${first} and zoom to it`}
                        title={`Select region ${first} and zoom to it`}
                      >
                        <AlertTriangle className="mt-0.5 size-3 shrink-0" aria-hidden />
                        <span className="min-w-0 break-words">{w}</span>
                      </Button>
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        ) : (
          <p>Compile an image to inspect geometry checks and small-target warnings.</p>
        )}
      </div>

      {/* Difficulty profile (contract §5) — one place, no duplication:
          the review panel, fed by the mounted bundle's manifest. */}
      {bundle && <DifficultyPanel raw={bundle.manifest.difficulty} validated={bundle.manifest.difficultyValidatedByPlaytest} />}

      {/* Region inspector */}
      {view === "inspect" && bundle && (
        <div className="rp-wide mt-4 rounded-[10px] bg-[#edf6f2] p-3">
          <h3 className="text-[13px] font-semibold text-[#183837]">Region inspector</h3>
          <MicroCaption className="mt-0.5">{selectionInfo}</MicroCaption>
          <div className="mt-2.5 grid grid-cols-2 gap-2">
            <div>
              <Label htmlFor="studio-edit-palette" className="text-[10px] text-[#657671]">
                Palette ID
              </Label>
              <Input
                id="studio-edit-palette"
                type="number"
                min={1}
                value={editPalette}
                onChange={(e) => setEditPalette(e.target.value)}
                className="mt-1 h-8 rounded-md bg-white text-xs"
              />
            </div>
            <div>
              <Label htmlFor="studio-object-group" className="text-[10px] text-[#657671]">
                Object group
              </Label>
              <Input
                id="studio-object-group"
                value={objectGroup}
                placeholder="roof"
                pattern="[a-z0-9]+(-[a-z0-9]+)*"
                onChange={(e) => setObjectGroup(e.target.value)}
                className="mt-1 h-8 rounded-md bg-white text-xs"
              />
            </div>
          </div>
          <div className="mt-2.5 grid grid-cols-2 gap-1.5">
            <Button
              variant="outline"
              size="sm"
              className="h-auto min-h-8 rounded-md border-[#e1e5df] bg-white py-1 text-[9px] leading-tight"
              disabled={busy || !selected.size}
              title="Relabel the selected regions to a palette (number) group — gameplay association, no visual change"
              onClick={() => doEdit("palette", { palette_id: Number(editPalette) || 1 })}
            >
              <PaletteIcon className="size-3 shrink-0" aria-hidden />
              Assign number group
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 rounded-md border-[#e1e5df] bg-white text-[9px]"
              disabled={busy || !selected.size}
              onClick={() => doEdit("group", { group: objectGroup.trim() || "roof" })}
            >
              <Layers className="size-3" aria-hidden />
              Set group
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 rounded-md border-[#e1e5df] bg-white text-[9px]"
              disabled={busy || !selected.size}
              onClick={() => doEdit("merge", { palette_id: Number(editPalette) || 1 })}
            >
              <Merge className="size-3" aria-hidden />
              Merge selected
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 rounded-md border-[#e1e5df] bg-white text-[9px]"
              disabled={busy || !selected.size}
              onClick={() => doEdit("decorate")}
            >
              <Tag className="size-3" aria-hidden />
              Make detail
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 rounded-md border-[#e1e5df] bg-white text-[9px]"
              disabled={busy || !selected.size}
              title="Split disconnected tap targets into separate regions"
              onClick={() => {
                if (selected.size !== 1) {
                  toast("Select exactly one region to split.");
                  return;
                }
                doEdit("split");
              }}
            >
              <SplitSquareHorizontal className="size-3" aria-hidden />
              Split
            </Button>
            <Button
              variant="outline"
              size="sm"
              className={`h-8 rounded-md text-[9px] ${
                placing
                  ? "border-[#087f74] bg-[#087f74] text-white"
                  : "border-[#e1e5df] bg-white text-[#183837]"
              }`}
              disabled={selected.size !== 1 || busy}
              onClick={startPlacing}
            >
              <Crosshair className="size-3" aria-hidden />
              {placing ? "Tap new position" : "Place number"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 rounded-md border-[#e1e5df] bg-white text-[9px]"
              disabled={!selected.size}
              onClick={clearSelection}
            >
              <Trash2 className="size-3" aria-hidden />
              Clear selection
            </Button>
          </div>

          {/* Recolor appearance — changes what the artwork looks like (paint layer),
              distinct from the number-group association above. */}
          <div className="mt-2.5 rounded-md border border-[#d9e4dc] bg-white p-2.5">
            <div className="flex items-center gap-1.5">
              <Paintbrush className="size-3 text-[#657671]" aria-hidden />
              <span className="text-[11px] font-semibold text-[#183837]">Recolor appearance</span>
            </div>
            <div className="mt-2 flex items-center gap-2">
              <input
                type="color"
                value={hexValid ? recolorHex : "#66AA33"}
                onChange={(e) => setRecolorHex(e.target.value.toUpperCase())}
                className="h-8 w-10 shrink-0 cursor-pointer rounded-md border border-[#e1e5df] bg-white p-0.5"
                aria-label="Recolor target color"
                disabled={busy}
              />
              <Input
                id="studio-recolor-hex"
                value={recolorHex}
                onChange={(e) => setRecolorHex(e.target.value.trim().toUpperCase())}
                onBlur={() => {
                  const t = recolorHex.trim().toUpperCase();
                  setRecolorHex(/^[0-9A-F]{6}$/.test(t) ? `#${t}` : t);
                }}
                placeholder="#RRGGBB"
                spellCheck={false}
                autoComplete="off"
                className="h-8 rounded-md bg-white font-mono text-xs"
                aria-invalid={!hexValid}
                aria-describedby={hexValid ? undefined : "studio-recolor-hex-hint"}
              />
            </div>
            {!hexValid && (
              <p id="studio-recolor-hex-hint" className="mt-1 text-[9px] font-medium text-[#ba463f]">
                Enter a 6-digit hex color like #66AA33.
              </p>
            )}
            <div className="mt-2 flex items-start gap-2">
              <Checkbox
                id="studio-preserve-shading"
                checked={preserveShading}
                onCheckedChange={(v) => setPreserveShading(v === true)}
                disabled={busy}
                className="mt-0.5"
              />
              <Label
                htmlFor="studio-preserve-shading"
                className="text-[10px] leading-snug font-normal text-[#657671]"
              >
                Preserve shading
              </Label>
            </div>
            <MicroCaption className="mt-1">
              Tint gradients toward the target instead of replacing the fill.
            </MicroCaption>
            <Button
              variant="outline"
              size="sm"
              className="mt-2 h-8 w-full rounded-md border-[#e1e5df] bg-white text-[9px]"
              disabled={busy || !selected.size || !hexValid}
              title={
                hexValid
                  ? "Recolor the selected regions' artwork (SVG-master builds only)"
                  : "Enter a valid #RRGGBB hex first"
              }
              onClick={() =>
                doEdit("recolor", { color: recolorHex.toUpperCase(), preserve_shading: preserveShading })
              }
            >
              <Paintbrush className="size-3" aria-hidden />
              Recolor appearance
            </Button>
          </div>

          <MicroCaption className="mt-2">
            Number group = gameplay association (what number the region requires). Recolor = what
            the artwork looks like.
          </MicroCaption>
          <MicroCaption className="mt-1.5">
            Each edit creates a new revision. Merge only neighboring regions. &ldquo;Make detail&rdquo; precolors a
            region and removes it from the progress count.
          </MicroCaption>
        </div>
      )}

      {/* Revision history */}
      <div className="rp-wide">
        <Label htmlFor="studio-revision-list" className="studio-label mb-1.5">
          Revision history
        </Label>
        <Select
          value={revisionSelect}
          onValueChange={(v) => setRevisionSelect(v)}
          disabled={!project?.revisions.length}
        >
          <SelectTrigger id="studio-revision-list" className="h-8 w-full rounded-md bg-white text-xs">
            <SelectValue placeholder={project?.revisions.length ? "Select a revision" : "No revisions"} />
          </SelectTrigger>
          <SelectContent>
            {[...(project?.revisions ?? [])].reverse().map((rev) => (
              <SelectItem key={rev.id} value={rev.id}>
                v{rev.version} · {rev.kind} · {rev.regionCount} regions
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <button
          type="button"
          className="block py-2 text-left text-[11px] font-medium text-[#087f74] hover:underline disabled:opacity-50"
          disabled={!revisionSelect || busy}
          onClick={() =>
            void activateSelectedRevision().catch((e: Error) => toast(e.message))
          }
        >
          Restore selected revision
        </button>
      </div>

      <div className="rp-wide">
        <ReviewDialog>
          <Button
            variant="outline"
            className="mt-2 w-full rounded-xl border-[#cfe6db] bg-[#e5f3ed] py-2.5 text-[11px] font-semibold text-[#126e5e] hover:bg-[#d9ede2]"
            disabled={!revision || busy}
          >
            <CheckCircle2 className="size-4" aria-hidden />
            Mark visually reviewed
          </Button>
        </ReviewDialog>
      </div>

      {/* Export links */}
      <div className="rp-wide mt-3">
        {project && revision ? (
          <div className="flex flex-col gap-1">
          <a
            href={exportUrl(project.id, revision.id)}
            download
            className="flex w-full items-center justify-between rounded-xl bg-[#087f74] px-4 py-3 text-[11px] font-semibold text-white transition-colors hover:bg-[#056c62]"
          >
            <span className="flex items-center gap-2">
              <FileArchive className="size-4" aria-hidden />
              Export game bundle .zip
            </span>
            <span aria-hidden>↧</span>
          </a>
          <a
            href={exportUrl(project.id, revision.id, true)}
            download
            className="block py-1.5 text-left text-[11px] font-medium text-[#087f74] hover:underline"
          >
            Include source &amp; authoring files
          </a>
          <a
            href={renderUrl(project.id, revision.id, 2048)}
            download
            className="block py-1.5 text-left text-[11px] font-medium text-[#087f74] hover:underline"
          >
            <ImageIcon className="mr-1 inline size-3.5" aria-hidden />
            Render 2048px PNG from vector
          </a>
        </div>
        ) : (
          <div className="flex flex-col gap-1 opacity-40" aria-disabled="true">
          <span className="flex w-full cursor-not-allowed items-center justify-between rounded-xl bg-[#087f74] px-4 py-3 text-[11px] font-semibold text-white">
            <span className="flex items-center gap-2">
              <Download className="size-4" aria-hidden />
              Export game bundle .zip
            </span>
            <span aria-hidden>↧</span>
          </span>
          <span className="block cursor-not-allowed py-1.5 text-left text-[11px] font-medium text-[#087f74]">
            Include source &amp; authoring files
          </span>
          <span className="block cursor-not-allowed py-1.5 text-left text-[11px] font-medium text-[#087f74]">
            Render 2048px PNG from vector
          </span>
        </div>
        )}
      </div>
      <MicroCaption className="rp-wide mt-1">
        Export contains SVG + region, paint and palette JSON. Automatic output is a reviewable draft — not a
        guaranteed publish-ready asset.
      </MicroCaption>
    </aside>
  );
}
