# Artwork contract

## One master, two independent vector representations

1. **Paint layer (`paint.json`):** many small colored polygons grouped by fill into combined SVG paths. These preserve a richer illustration than a flat region swatch. They are not required taps.
2. **Gameplay layer (`regions.json`):** independently identified closed polygons with palette group IDs, number positions, object groups and bounding boxes.

`paletteId` answers "which palette button accepts this region?" `id` answers "which exact region has been filled?" Regions sharing a palette group do not fill simultaneously. Swatches represent a paint family; a revealed region may contain several shading colors.

All files use the same `[0, 0, width, height]` viewBox. `rings[0]` is the exterior boundary; subsequent rings are holes. Both rendering and hit testing MUST use `evenodd`. Paths contain only absolute M/L/Z commands in artwork coordinates. Region rings and path strings are emitted from the same coordinates.

## Layer order

- Cached detailed vector paint once across the full canvas.
- Opaque white masks for unfilled regions, or checkerboard masks for the selected matching group.
- Source dark-ink shapes (optional; some dark foliage can be included by the threshold).
- Number labels for unfilled regions only, with minimum screen-size visibility.

On completion: remove that region's mask and number, revealing the aligned underlying painting. Do not stretch a full image independently into each region. In memory play, hide both numbers and matching-region highlights.

## Geometry and QA

The compiler uses SLIC draft segmentation, adjacency-only tiny-fragment merging, polygonization of a label grid, and exact shared pixel-edge coordinates. No independent per-region simplification is applied because that would create gaps/overlaps between neighbors.

The validator checks polygon validity, positive areas, viewBox bounds, unique IDs, palette membership, paths/rings equivalence, label anchors, complete canvas union, zero overlap and a raster coverage roundtrip. It reports areas that cannot fit conservative labels as precolored decorations excluded from progress. A geometric pass does not prove good artistic boundaries, legal clearance or balanced gameplay.

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
