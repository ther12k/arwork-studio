# Connect the studio to Color Duel

Preserve the existing application's stack, navigation and competitive features. Do not copy a competitor's branding or replace Arena with unrelated gallery tabs.

## Web / React

Copy an exported `artworks/<id>` directory into public assets, then copy the included adapter into your source tree.

```js
import { loadArtwork, VectorBoard } from './detailed-board.mjs';
const bundle = await loadArtwork('/artworks/my-art/artwork.json');
const board = new VectorBoard(document.querySelector('svg'), bundle, {
  mode: 'number',
  onChange: state => console.log(state.completed, state.total)
});
board.setPalette(bundle.geometry.regions[0].paletteId);
// Dispose on route change/unmount:
// board.destroy();
```

The adapter supports numbered, memory and free-color display mechanics; it is an asset player, not a ranked match server. Its localStorage progress is appropriate for local solo previews, not authoritative VS scoring. The Studio disables game persistence during play tests so authoring does not change player progress.

The React wrapper is source for integration, not a rewritten React project. Manage async loads and disposal through its lifecycle. Version/hash-check restored sessions. Prefer thumbnails in the gallery and lazy load full geometry only when a puzzle is opened.

## Format contract (detailed-vector schema 2)

One versioned contract is shared by the studio renderer (`src/lib/detailed-board.ts`),
this shipped adapter (`integration/detailed-board.mjs`) and the export gate
(`studio.pipeline.validate_runtime_contract`, which runs on every export).
A bundle that passes studio validation passes this adapter:

- `geometry.geometrySchema` 1 (legacy M/L/Z polygons) or 2 (curved M/L/C/Q/Z masters)
- `regions[*].fillRule` is `evenodd` (default) or `nonzero` — the source SVG's
  fill rule is preserved per region; honor it for rendering AND hit-testing
- `paint.paths[*].fill` is `#RRGGBB` or `url(#g-...)` referencing `paint.gradients`;
  paths may carry `fillRule`, `fillOpacity`, `opacity`, `stroke`, `strokeWidth`
  and `z` (document order — render paint and ink merged by `z`)
- `paint.inkPaths` may be OPEN stroke paths (`strokeWidth` set or `filled: false`)
- Regions are VISIBLE SURFACES: opaque coverage has been subtracted, masks do
  not overlap, so any fill order colors correctly and every number label hits
  its own region

The default export is the LEAN RUNTIME bundle: only artwork/regions/palette/paint
JSON (+ validation evidence). Authoring duplicates (master/rings/flat/legacy)
and preview SVGs stay in the authoring export. Regressions are enforced by
`tests/test_game_adapter.py`, which compiles fresh artwork and runs it through
this exact adapter file (see `scripts/adapter-contract-check.mjs`).

## Flutter/native

Port the contract, not the web DOM implementation. Compile M/L/C/Q/Z paths once into native paths, honor each region's fill rule, cache the static vector underpainting, invert the full fit/pan/zoom matrix for taps, then test bounding boxes and polygon interiors. Keep numbers as text metadata. No compiled Flutter adapter is claimed in this system package.

## Publishing workflow

This MVP exports downloadable ZIPs. It does NOT write to GitHub or your production storage. For production, put a review/publish endpoint between authoring drafts and the live catalog. Validate the bundle server-side, store immutable assets under ID/version/hash, upload a compressed package or individual files, and update the catalog only after approval. The game should never run AI generation or polygonization in the match loop.

For solo play, cache approved assets and save region-ID sessions. For a duel, the server chooses an identical asset hash and rule version for both players and validates region completion events against that version. Editor operations never change the geometry of an active match.
