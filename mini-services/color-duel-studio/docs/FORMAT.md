# Artwork contract

## One master, two independent vector representations

1. **Paint layer (`paint.json`):** many small colored polygons grouped by fill into combined SVG paths. These preserve a richer illustration than a flat region swatch. They are not required taps.
2. **Gameplay layer (`regions.json`):** independently identified closed polygons with palette group IDs, number positions, object groups and bounding boxes.

`paletteId` answers "which palette button accepts this region?" `id` answers "which exact region has been filled?" Regions sharing a palette group do not fill simultaneously. Swatches represent a paint family; a revealed region may contain several shading colors.

All files use the same `[0, 0, width, height]` viewBox. `rings[0]` is the exterior boundary; subsequent rings are holes. Regions use curved M/L/C/Q/Z masters (schema 2); flattened rings are derived approximations. Both rendering and hit testing honor the per-region `fillRule` (`evenodd` default, source rule preserved).

## Layer order

- Cached detailed vector paint once across the full canvas.
- Opaque white masks for unfilled regions, or checkerboard masks for the selected matching group.
- Source dark-ink shapes (optional; some dark foliage can be included by the threshold).
- Number labels for unfilled regions only, with minimum screen-size visibility.

On completion: remove that region's mask and number, revealing the aligned underlying painting. Do not stretch a full image independently into each region. In memory play, hide both numbers and matching-region highlights.

## Geometry and QA

The compiler uses SLIC draft segmentation, adjacency-only tiny-fragment merging, polygonization of a label grid, and exact shared pixel-edge coordinates. No independent per-region simplification is applied because that would create gaps/overlaps between neighbors.

The validator checks polygon validity, positive areas, viewBox bounds, unique IDs, palette membership, paths/rings equivalence, label anchors, complete canvas union, zero overlap and a raster coverage roundtrip. It reports areas that cannot fit conservative labels as precolored decorations excluded from progress. A geometric pass does not prove good artistic boundaries, legal clearance or balanced gameplay.

## Edges and boundary style (schema 2, optional)

`regions.json` may carry `geometry.edges`: boundary entries `{id, d, kind, leftRegion, rightRegion}` where `kind` is `"artwork"` (a true master boundary) or `"subdivision"` (an artificial gameplay boundary created by cuts, pen subtraction or auto-subdivide). `leftRegion`/`rightRegion` name the bordering regions or are null. When `edges` is a non-empty array, region paths render fill-only and boundaries are drawn by an edges overlay above masks/ink, below labels: artwork = solid `geometry.stroke` width 1.6; subdivision = `#7A8C94` width 0.85 dashed `3 2.2` (userSpace). `geometry.boundaryStyle` (`{artwork: {stroke, strokeWidth, dash?}, subdivision: {...}}`) overrides the defaults per kind. Absent/empty `edges` keeps the legacy region-stroke rendering. Raster compiles emit no edges in v1; the runtime export keeps both fields.

## Semantic object model (authoring layer, optional)

Revisions may carry `objects.json`: `{"schemaVersion": 1, "objects": [...]}`. It is **authoring metadata** — the runtime export never includes it, the authoring export does, and it sits outside `contentHash`. Ownership is strictly one-directional:

- `objects.json` owns `shapeIds` (the paint/ink shape ids);
- every region carries `objectId`; `objects.json` NEVER stores regionIds (regions are derived state that changes on every edit).

Record fields: `id` (required, `obj-*` convention), `name`, `type`, `role`, `parentId` (nested objects, e.g. roof inside house), `shapeIds`, `subdivision {detailWeight, minRegions, preferredRegions, maxRegions, preserveSilhouette}` and `generation {prompt, provider, locked}`. All but `id` are optional; `preserveSilhouette` is reserved (accepted, not yet honoured by subdivision).

Identity is born **in the master SVG**: `<g data-cd-object="obj-tree" data-cd-name="Tree">` groups survive sanitization, so every rebuild reconstructs the same objects.json. The AI multistage planner stamps each planned object's fragment with a `data-cd-object` group at compose time. On build, regions inherit their shape's `objectId`; the authoring records supplied to the build (scene plan) merge by id with the group-derived records and may carry subdivision budgets.

Auto-subdivide consumes the budgets: `raw = areaShare × detailWeight × shapeCountShare`, clamped to `[minRegions, maxRegions]` (a contradictory `minRegions > maxRegions` honours maxRegions for allocation and leaves minRegions as the QA threshold), then largest-remainder normalized to the target with leftover redistributed to objects below their caps. Bundles without object groups keep the legacy global largest-first behavior.

Edit synchronization (`_sync_objects_from_regions`) reconciles object records after cuts, merges, pens, or node adjustments: shape ownership is the union of existing live shapes (`paint.paths` + `paint.inkPaths`) and active region `masterShapeId`s, so decorative ink and shading shapes are never orphaned when gameplay regions are edited. Objects without playable regions are kept as long as they retain live paint/ink shapes or serve as parents to other active objects.

QA (`validation.json → objects`) reports orphan shapes/regions, missing shapeIds, invalid parents, zero-geometry objects and impossible budgets as **warnings** — they never fail geometry validation.

## Generation sessions and provenance (authoring layer)

AI generation and image workflows are orchestrated transactionally via `GenerationSessionManager`. A session isolates all drafts in `workspace/projects/{pid}/sessions/{session_id}/` (ScenePlan, vector fragments, candidate master SVG, compiled test bundle) and only promotes to an immutable project revision (`revisions/rev-*`) upon explicit commit after passing QA. If generation fails, cancels, or fails validation, current project revisions remain completely untouched.

Create-with-AI runs as separate paid steps so vector cost is only paid once the plan is stable: `plan` (one strict-JSON scene-planning call → fills the draft's objects) → user revisions via structured mutations → `generate` (one fragment call per planned object, composed into the session master, compiled + QA'd) → `commit`. Targeted object regeneration (`regenerate-object`) replaces a single object's shapes in the session master — the `objectId` is preserved while internal shapeIds change — and recompiles, re-deriving neighbours' visible surfaces without touching untouched objects' shapes.

When committed, `artwork.json` records generation provenance:

```json
{
  "generation": {
    "mode": "ai_chat",
    "requestedDifficulty": "hard",
    "targetRegionRange": [320, 550],
    "measuredDifficulty": {
      "rating": "hard",
      "score": 68.4
    },
    "fidelity": "balanced",
    "sessionId": "sess-a1b2c3d4e5f6",
    "scenePrompt": "Cozy forest café beside a waterfall",
    "scenePlan": {
      "schemaVersion": 1,
      "title": "Cozy forest café",
      "requestedDifficulty": "hard",
      "targetRegions": 430,
      "objects": [...]
    },
    "committedAt": "2026-09-11T12:00:00Z"
  }
}
```

## Convert Artwork (image → vectors)

`image_convert` sessions follow the same first-class semantic contract as native AI artwork. The division of labour: **the AI decides *what* the objects are** (semantic ScenePlan with approximate bboxes in plan viewBox space, scaled into image pixel space before association); **deterministic CV decides *where* the pixel boundaries are** (SLIC superpixels → tiny-component merge → Lab ΔE adjacent merge, then connected-component split).

**Candidate segmentation is independent of gameplay subdivision.** Reconstruction always segments the source at `CONVERT_CANDIDATE_BASE` (220) candidate labels regardless of the requested difficulty; difficulty only steers the gameplay subdivision of already-reconstructed paint. Consequence (asserted by tests): converting the same source at the same fidelity produces byte-identical paint shapeIds and path `d` strings for Easy and Master — only `regions.json`, labels and difficulty metrics differ.

Fidelity presets change real algorithm parameters (`studio/generation.py → CONVERT_POLICIES`):

| Preset | segmentDensity | colorMergeDeltaE | curveTolerance | minComponentArea | paletteTarget | visualGate |
|---|---|---|---|---|---|---|
| stylized | 0.6 | 14 | 2.0 | 120 | 16 | 55 |
| balanced | 1.0 | 9 | 1.1 | 42 | 24 | 70 |
| faithful | 1.8 | 5 | 0.6 | 18 | 40 | 82 |

- `segmentDensity` scales the SLIC candidate count (`target_regions × max(0.2, density)`, floor 30) — it is *not* the final gameplay region count.
- `colorMergeDeltaE` drives a real adjacent-segment merge pass in mean CIELAB space (conflict-free one-to-one merges per pass; chains deferred to later passes).
- `curveTolerance`, `minComponentArea`, `paletteTarget` flow into `BuildSettings`.

Raster paint reconstruction is object-aware: every paint path carries a stable `shapeId` (`rc-<object>-<n>`, e.g. `rc-tree-0042`) and an `objectId`, grouped by `(objectId, fill)`. The **initial converted revision** therefore ships `objects.json` whose records own the live `rc-*` shapes — same contract as native SVG artwork, not something that only appears after the first edit. Session quality scores `visualFidelity` (vs the policy's visual gate) and `gameReadiness` (gate 80 for all presets) independently; over-vectorization (gameplay regions ≫ candidate segments) fails the session with an actionable message. Geometry QA is enforced upstream — `compile_image → emit_bundle` raises on `validate_bundle` failures before the quality scorer runs.

Session intermediates: `source.png` (normalized via `clean_image`), `decomposition.json`, `reconstructed-master.svg`, `bundle/`. On commit the bundle is promoted atomically to `revisions/rev-*` and the project master becomes the normalized raster source (`master-*.png`), so later edit actions rebuild from the same pixels.

## Difficulty Optimization (gameplay-only engine)

`optimize_gameplay_difficulty(bundle, tier)` (`studio/difficulty.py`) drives a compiled bundle's gameplay layer toward a requested tier (easy 100–180 → medium 180–320 → hard 320–550 → master 550–800; initial targets 140/250/430/650). It is the same engine for the Convert pipeline's requested tier and the future "Optimize to Master" button on existing revisions.

Contract: it moves **only** regions, region labels, `objects.subdivision.preferredRegions` and the derived difficulty metrics. Every accepted iteration must keep the paint paths and `objects.shapeIds` byte-identical and pass geometry QA; any violation rolls back to the last healthy state. Two move directions:

- **current > target → conservative semantic merge**: same `objectId` only (never across objects), edge-adjacent (corner-touching MultiPolygon unions are dropped, not locked), same palette group first then closest CIELAB ΔE, merged label must stay readable; merged masters are exact pixel-edge (`fit=False`) so the raster partition stays watertight.
- **current < target → semantic split** through the existing object-budget auto-subdivider (area × detailWeight × complexity, `[minRegions, maxRegions]` clamps, per-split minimum = `max(2× build minimum, tiny floor)` so pieces never become microscopic targets; a cut is skipped when a piece would carry an unreadable label).

Bounded deterministic loop (≤3 iterations: measure → move → QA → measure; same bundle + target ⇒ identical regions). Quality outranks the requested label: label-conflict regressions, below-minimum tap areas, >9× required zoom, or a tiny-region explosion reject a candidate; a `safe-ceiling`/`best-safe-result` outcome reports human-readable reasons instead of forcing the number. Split/merge re-exposed sub-tolerance seams are absorbed by re-measuring `geometry.partitionTolerance` (the same documented-band mechanism the merge/cut edit actions use), and the raster-roundtrip pixel allowance scales with board density (`max(8, regions/50)`) because sub-pixel boundary seams grow with boundary length, not with defects. The report lands in session meta (`difficultyOptimization`): requested tier, target, per-iteration measurements, merges/splits, per-object budgets, achieved rating/score and reasons.

The engine is also exposed as a revision action: `POST /api/projects/{pid}/optimize` `{base_revision, tier}` (async job like the edit actions). A moved geometry lands in a NEW immutable revision (`manifest.difficultyOptimization` carries the full before/after report; `project.lastOptimization` mirrors it for the UI); an unchanged one returns a no-op report without creating a duplicate revision. The route re-verifies the artwork invariant at byte level (paint.json hash before == after) — a violation fails the job and creates nothing. Undo is natural: activate the previous revision.

## Difficulty profile

`artwork.json` carries `manifest.difficulty = {rating, score, metrics}` computed deterministically for every revision: `rating` is easy (<25) / medium (<50) / hard (<75) / master; `score` is a 0–100 weighted sum over `regionCount, medianRegionArea, tinyRegionPct, requiredZoom` (worst-case zoom for a 44px touch target from a fit viewport), `labelClearance` (ok/tight/conflict), `paletteAmbiguity` (low/medium/high), `paletteGroups, avgNeighbors, subdivisionEdges, objectDensity`. It replaces the old `"unrated"` placeholder; `difficultyValidatedByPlaytest` stays false until a real playtest.

## Versioning and state

`contentHash = SHA256(exact regions.json bytes + exact palette.json bytes + exact paint.json bytes)`.

Save player progress separately:

```json
{
  "schemaVersion": 1,
  "artworkId": "art-example",
  "artworkVersion": "0.3.0",
  "contentHash": "...",
  "mode": "number",
  "completedRegionIds": ["r-00102"],
  "selectedPaletteId": 13,
  "mistakes": 0
}
```

Every merge/group/palette/label/detail edit creates a new geometry revision/version/hash. Never blindly carry progress to a new segmentation. A title match is insufficient. Restoration in this studio only activates revisions whose source hash matches the current master.

## Detailed example

```json
{
  "id": "r-00102",
  "paletteId": 13,
  "objectId": "roof",
  "d": "M 10,10 L 50,10 L 50,40 L 10,40 Z",
  "fillRule": "evenodd",
  "rings": [[[10,10],[50,10],[50,40],[10,40],[10,10]]],
  "bbox": [10,10,50,40],
  "area": 1200,
  "label": {"x":30,"y":25,"fontSize":12,"minScreenPx":9,"clearance":15}
}
```

The illustrative fragment above is not a separate bundled puzzle. Use the generated files and validator for complete assets. Object groups begin unassigned; the studio does not pretend to recognize roof/flower/animal semantics automatically.

## Source resolution vs export resolution

`generation.sourcePixels` describes the approved input. `generation.workingPixels` describes the actual grid from which paths were derived. An arbitrary-size SVG is resolution-independent geometry, but its sampled detail still comes from that working grid. A 4096px raster export is an enlarged vector render, not a newly generated 4K original.
