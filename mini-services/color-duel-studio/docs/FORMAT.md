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

QA (`validation.json → objects`) reports orphan shapes/regions, missing shapeIds, invalid parents, zero-geometry objects and impossible budgets as **warnings** — they never fail geometry validation.

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
