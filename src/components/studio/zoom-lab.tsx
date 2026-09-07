"use client";

/**
 * Zoom lab — the same crop of the current revision at 100% / 400% / 1000%,
 * rendered twice: from the authoritative curved masters and from the legacy
 * pixel-edge polygons (pre-upgrade staircase output).
 *
 * Pure comparison view: no blur, no rasterized images, no stroke-join tricks.
 * Fetches its own geometry payloads and persists nothing.
 */

import { Fragment, useEffect, useRef, useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { fetchGeometryMode, type GeometryModePayload, type ModeRegion } from "@/lib/studio-api";
import { useStudioContext } from "./use-studio";

interface CropRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const OVERVIEW_WIDTH = 220;
const MIN_CROP_UNITS = 40;
const INK = "#29383E";

/** Row heights grow with the zoom level so deep zoom gets more room. */
const ZOOM_ROWS: ReadonlyArray<{ zoom: number; height: number }> = [
  { zoom: 1, height: 140 },
  { zoom: 4, height: 170 },
  { zoom: 10, height: 200 },
];

const clamp = (v: number, lo: number, hi: number): number => (hi < lo ? lo : Math.max(lo, Math.min(hi, v)));

/** Centered square of ~25% of the artwork's smaller dimension (>= 40 units). */
function defaultCrop(viewBox: number[]): CropRect {
  const [vx, vy, vw, vh] = viewBox;
  const small = Math.min(vw, vh);
  const side = Math.min(Math.max(small * 0.25, MIN_CROP_UNITS), small);
  return { x: vx + (vw - side) / 2, y: vy + (vh - side) / 2, w: side, h: side };
}

/** bbox arrives as [minX, minY, maxX, maxY]. */
function intersects(crop: CropRect, bbox: number[]): boolean {
  const [minX, minY, maxX, maxY] = bbox;
  return minX < crop.x + crop.w && maxX > crop.x && minY < crop.y + crop.h && maxY > crop.y;
}

function regionsInCrop(regions: ModeRegion[], crop: CropRect): ModeRegion[] {
  return regions.filter((r) => intersects(crop, r.bbox));
}

function viewBoxString(vb: number[]): string {
  return `${vb[0]} ${vb[1]} ${vb[2]} ${vb[3]}`;
}

/** One comparison cell: the crop's center window at the row's zoom level. */
function ZoomCell({
  payload,
  crop,
  zoom,
  height,
}: {
  payload: GeometryModePayload;
  crop: CropRect;
  zoom: number;
  height: number;
}) {
  const side = crop.w / zoom;
  const cx = crop.x + crop.w / 2;
  const cy = crop.y + crop.h / 2;
  const regions = regionsInCrop(payload.regions, crop);
  const width = height * (crop.w / crop.h);
  return (
    <div
      className="studio-scroll overflow-x-auto rounded-[10px] border border-[#dce2d9] bg-white"
      role="img"
      aria-label={`${zoom * 100}% zoom, ${payload.mode === "curved" ? "curved masters" : "legacy polygons"}`}
    >
      <svg
        viewBox={`${cx - side / 2} ${cy - side / 2} ${side} ${side}`}
        style={{ width: `${width}px`, height: `${height}px` }}
        className="block"
      >
        {regions.map((r) => (
          <g key={r.id}>
            <path d={r.d} fill="#fff" stroke={INK} strokeWidth={payload.strokeWidth ?? 0.65} fillRule="evenodd" />
            <text x={r.label.x} y={r.label.y} fontSize={r.label.fontSize} fill={INK}>
              {r.paletteId}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function SkeletonRows() {
  return (
    <div className="grid grid-cols-2 gap-x-3 gap-y-2.5">
      <Skeleton className="h-8 rounded-md" />
      <Skeleton className="h-8 rounded-md" />
      {ZOOM_ROWS.map((row) => (
        <Fragment key={row.zoom}>
          <Skeleton className="h-8 w-24 rounded-full" />
          <Skeleton className="h-8 w-24 rounded-full" />
          <Skeleton className="rounded-[10px]" style={{ height: row.height }} />
          <Skeleton className="rounded-[10px]" style={{ height: row.height }} />
        </Fragment>
      ))}
    </div>
  );
}

export default function ZoomLab() {
  const { project } = useStudioContext();
  const projectId = project?.id ?? null;
  const revisionId = project?.currentRevision ?? null;

  const [curved, setCurved] = useState<GeometryModePayload | null>(null);
  const [legacy, setLegacy] = useState<GeometryModePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [crop, setCrop] = useState<CropRect | null>(null);

  const overviewRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startClientX: number;
    startClientY: number;
    cropX: number;
    cropY: number;
  } | null>(null);

  // Fetch both geometries for the current revision; persist nothing.
  useEffect(() => {
    if (!projectId || !revisionId) {
      setCurved(null);
      setLegacy(null);
      setCrop(null);
      setError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const [curvedPayload, legacyPayload] = await Promise.all([
          fetchGeometryMode(projectId, revisionId, "curved"),
          fetchGeometryMode(projectId, revisionId, "legacy"),
        ]);
        if (cancelled) return;
        setCurved(curvedPayload);
        setLegacy(legacyPayload);
        setCrop(defaultCrop(curvedPayload.viewBox));
      } catch (e) {
        if (cancelled) return;
        setCurved(null);
        setLegacy(null);
        setCrop(null);
        setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, revisionId]);

  // ----- crop rectangle dragging (pointer events, art-unit mapping) -----

  const moveCrop = (nextX: number, nextY: number) => {
    setCrop((prev) => {
      if (!prev || !curved) return prev;
      const [vx, vy, vw, vh] = curved.viewBox;
      return {
        ...prev,
        x: clamp(nextX, vx, vx + vw - prev.w),
        y: clamp(nextY, vy, vy + vh - prev.h),
      };
    });
  };

  const onCropPointerDown = (e: React.PointerEvent<SVGRectElement>) => {
    if (!crop) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      pointerId: e.pointerId,
      startClientX: e.clientX,
      startClientY: e.clientY,
      cropX: crop.x,
      cropY: crop.y,
    };
  };

  const onCropPointerMove = (e: React.PointerEvent<SVGRectElement>) => {
    const drag = dragRef.current;
    const rect = overviewRef.current?.getBoundingClientRect();
    if (!drag || !rect || !curved || rect.width <= 0 || rect.height <= 0) return;
    const [, , vw, vh] = curved.viewBox;
    const dx = ((e.clientX - drag.startClientX) / rect.width) * vw;
    const dy = ((e.clientY - drag.startClientY) / rect.height) * vh;
    moveCrop(drag.cropX + dx, drag.cropY + dy);
  };

  const onCropPointerEnd = (e: React.PointerEvent<SVGRectElement>) => {
    const drag = dragRef.current;
    if (drag && e.currentTarget.hasPointerCapture?.(drag.pointerId)) {
      e.currentTarget.releasePointerCapture(drag.pointerId);
    }
    dragRef.current = null;
  };

  const onCropKeyDown = (e: React.KeyboardEvent<SVGRectElement>) => {
    if (!crop || !curved) return;
    const step = Math.max(2, Math.min(curved.viewBox[2], curved.viewBox[3]) * 0.02);
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const delta = moves[e.key];
    if (!delta) return;
    e.preventDefault();
    moveCrop(crop.x + delta[0], crop.y + delta[1]);
  };

  // ------------------------------------------------------------------ render

  if (!revisionId) {
    return (
      <div className="flex min-h-64 flex-col items-center justify-center px-6 py-8 text-center">
        <h2 className="text-lg font-bold text-[#527360]">Zoom lab</h2>
        <p className="mt-2 text-xs leading-6 text-[#778481]">Build a vector draft first.</p>
      </div>
    );
  }

  const vb = curved?.viewBox;
  const overviewHeight = vb ? Math.round(OVERVIEW_WIDTH * (vb[3] / vb[2])) : 0;

  return (
    <div className="w-full min-w-0">
      {/* Crop picker + intro */}
      <div className="flex flex-wrap items-start gap-4">
        {loading && !curved ? (
          <Skeleton className="rounded-lg" style={{ width: OVERVIEW_WIDTH, height: overviewHeight || 165 }} />
        ) : vb && crop ? (
          <svg
            ref={overviewRef}
            viewBox={viewBoxString(vb)}
            width={OVERVIEW_WIDTH}
            style={{ height: overviewHeight }}
            className="block max-w-full shrink-0 rounded-lg border border-[#dce2d9] bg-white"
            role="img"
            aria-label="Crop picker: overview of all curved regions"
          >
            {curved?.regions.map((r) => (
              <path
                key={r.id}
                d={r.d}
                fill="#E7EDF0"
                stroke={curved?.stroke ?? INK}
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
                fillRule="evenodd"
              />
            ))}
            <rect
              x={crop.x}
              y={crop.y}
              width={crop.w}
              height={crop.h}
              fill="#087f7426"
              stroke="#087f74"
              strokeWidth={1.5}
              vectorEffect="non-scaling-stroke"
              tabIndex={0}
              role="button"
              aria-label="Crop rectangle — drag it, or focus and use arrow keys to move"
              style={{ touchAction: "none", cursor: "move" }}
              onPointerDown={onCropPointerDown}
              onPointerMove={onCropPointerMove}
              onPointerUp={onCropPointerEnd}
              onPointerCancel={onCropPointerEnd}
              onKeyDown={onCropKeyDown}
            />
          </svg>
        ) : null}
        <div className="min-w-0 flex-1">
          <h2 className="text-[13px] font-semibold text-[#183837]">Zoom lab — one crop, two geometries</h2>
          <p className="mt-1 text-[10px] leading-relaxed text-[#778481]">
            Drag the crop box on the overview. Every row below shows the same crop at a higher zoom: left column
            reconstructed from the authoritative curved masters, right column the same regions as pre-upgrade
            pixel-edge polygons.
          </p>
          {error && (
            <p className="mt-2 rounded-md border border-[#ecd5d2] bg-[#faf0ee] px-2 py-1.5 text-[10px] text-[#ba463f]">
              Could not load the zoom-lab geometry: {error}
            </p>
          )}
        </div>
      </div>

      {/* Comparison grid */}
      {loading && !curved ? (
        <div className="mt-4">
          <SkeletonRows />
        </div>
      ) : curved && legacy && crop ? (
        <div className="mt-4">
          <div className="grid grid-cols-2 gap-x-3 gap-y-2">
            {/* Column headers */}
            <div>
              <p className="text-[11px] font-semibold text-[#183837]">
                Curved masters <span className="font-normal text-[#778481]">(authoritative)</span>
              </p>
            </div>
            <div>
              <p className="text-[11px] font-semibold text-[#183837]">Legacy pixel-edge polygons</p>
              <p className="mt-0.5 text-[9px] text-[#778481]">
                Pre-upgrade tracer output — staircase edges at zoom
              </p>
            </div>

            {ZOOM_ROWS.map(({ zoom, height }) => (
              <Fragment key={zoom}>
                <div className="col-span-2 flex items-center gap-2 pt-2">
                  <span className="rounded-full bg-[#e5f3ed] px-2 py-0.5 text-[9px] font-semibold text-[#087f74]">
                    {zoom * 100}% zoom
                  </span>
                  <span className="h-px flex-1 bg-[#e1e5df]" aria-hidden />
                </div>
                <ZoomCell payload={curved} crop={crop} zoom={zoom} height={height} />
                <ZoomCell payload={legacy} crop={crop} zoom={zoom} height={height} />
              </Fragment>
            ))}
          </div>
          <p className="mt-3 text-[9px] leading-relaxed text-[#778481]">
            Same crop, same regions, same numbers. No blur, no rasterized images, no stroke-join tricks —
            reconstructed vector geometry only.
          </p>
        </div>
      ) : null}
    </div>
  );
}
