# Color Duel Vector Art Studio — engineering review

**Reviewed:** 7 September 2026  
**Input:** `workspace-229f39c7-5e48-4295-a85a-59e0144ab323 (1).tar`  
**Decision:** Keep the compiler and workbench foundation, but do not use its current exports as the production source of truth for Color Duel. Repair the format contract, visible-region topology, SVG fidelity and SVG serialization before adding more artwork or redesigning the interface.

## Scope and verification

This review concerns the uploaded snapshot, not a live deployment or the current Color Duel game repository. Paths below are relative to the archive root; `studio/*.py`, `schemas/*` and `integration/*` are under `mini-services/color-duel-studio/`.

Executed checks:

- The existing Python suite: **31 passed in 2.39 seconds**, in this review environment. This is not a production performance benchmark.
- Nine small SVG compiler fixtures, covering nested shapes, filled strokes, fill opacity, object opacity, nonzero fill, percentage gradients, gradient transforms, ink order and attribute escaping; a separate palette-edit check.
- The actual `src/lib/detailed-board.ts` renderer, transpiled without application-logic changes and instantiated in Chromium 144.0.7559.96. Tests used an isolated DOM harness, not the full Next.js shell. Tested the current native SVG and curved-raster samples and a two-rectangle fixture.
- The shipped integration adapter against old and new bundles; the published regions schema against the new bundles.
- A strict standalone TypeScript check of `src/lib/detailed-board.ts`.
- A harmless SVG escaping proof: a local browser variable, and no network request or user-data access.

Not verified: full Next.js build or visual browser testing of its React panels, paid AI generation, the actual game importer, multiplayer synchronization, real mobile-device frame rates or production deployment. The frontend dependency installation could not reach npm because DNS requests failed (`EAI_AGAIN`). UI-shell comments therefore come from source review, not invented live screenshots. The browser evidence screenshots in this pack are actual isolated renderer outputs.

## What is worth keeping

The four-file bundle (`artwork.json`, `regions.json`, `palette.json`, `paint.json`), separate visual paint and playable geometry, server-side compiler, source hashes, versioned geometry revisions, explicit review state and play-test isolation are useful foundations. Raster tracing fits shared boundaries rather than blindly smoothing every neighboring region separately. Native SVG import and a separate SVG-generation route exist; they are not just embedded PNGs disguised as SVG. Play-test viewport updates already avoid a React state update for every viewport notification.

Evidence: `studio/pipeline.py:236–291, 501–663, 997–1037`; `studio/app.py:62–89, 149–163, 269–323`; `src/components/studio/use-studio.tsx:298–304, 354–369`; `src/lib/detailed-board.ts:295–325, 521–525`.

However, native-authored vector art and vectorized raster art are different authoring routes. A trace with cubic curves is genuinely vector data, but remains a reconstruction of sampled image boundaries. The interface should identify the source route instead of treating every vector result as equivalent.

## Findings

### CD-S01 — P0: The advertised game integration rejects new compiler output

**Reproduced.** `integration/detailed-board.mjs` accepts only M/L/Z path commands and flat hexadecimal fills. The current TypeScript renderer supports M/L/Q/C/Z, gradient references and stroked ink, but those updates were not carried into the integration adapter. The separate vanilla web adapter is also old.

Results:

| Bundle | Current studio validator | Shipped game adapter |
|---|---|---|
| Old polygon treehouse | Accepted | Accepted |
| Current native SVG treehouse | Accepted | Invalid region geometry or palette |
| Current curved raster treehouse | Accepted | Invalid region geometry or palette |

The published regions schema still requires `schemaVersion: 1` and M/L/Z-only paths. It reports **32 validation errors** on the current native sample and **554** on the curved-raster sample. These counts include repeated violations, not 32/554 distinct root causes.

**Evidence:** `integration/detailed-board.mjs:14–33`; `schemas/regions.schema.json:15–17, 63–66`; `src/lib/detailed-board.ts:152–197`; `reproductions/adapter-results.json`; `reproductions/export-metrics.json`.

**Fix:** Maintain one versioned runtime contract and one shared renderer/validator package, with studio and game consumers. Publish a matching schema and a supported-feature/version negotiation rule. Test a freshly built artifact through the actual exported integration path. Do not merely remove the regex checks; retain bounded, finite path parsing, safe paint references and complexity limits.

**Acceptance:** Legacy compatibility remains explicit; newly compiled curved, gradient and stroked-ink fixtures load in both studio and game adapters and satisfy the same declared schema.

### CD-S02 — P0: SVG shape stacking is not valid independent gameplay geometry

**Reproduced.** The importer turns filled shapes into tap targets without removing portions covered by later shapes. Labels are calculated inside complete source shapes, not necessarily inside their visible, reachable portions. The validator treats SVG overlaps as warnings. The renderer resolves taps topmost-first.

On the bundled current SVG treehouse, **7 of 40 playable label anchors resolve to another region** in the actual browser hit test. The numbered render visibly contains colliding numbers. This is a deterministic ownership defect, not an artistic preference.

The two-rectangle fixture reveals a second problem. Place a red foreground rectangle over a blue background. Fill the red rectangle first. The renderer reports `painted` and completion advances, but a sample strictly inside the red region remains **white [255,255,255]**, rather than **red [255,0,0]**. Removing the foreground white mask exposes an unfinished background white mask above the detailed painting. The fully colored preview looks correct, which can conceal the bug during approval.

**Evidence:** `studio/pipeline.py:1226–1249, 889–901`; `src/lib/detailed-board.ts:403–439, 531–550, 604–612`; `reproductions/browser-results.json`; `reproductions/native_svg-numbered.png`; `reproductions/nested-after-foreground-fill.png`.

**Fix:** Preserve a separate visual scene graph and compile disjoint visible gameplay regions from it. Treat visible ownership, opaque occlusion, translucent shading, decorative ink and background coverage deliberately. For ordinary opaque stacked shapes, a useful starting definition is `visible region = source shape minus higher occluding shapes`; this is not a complete recipe for transparent artwork. Split disconnected visible parts. Label and index the resulting playable surfaces. Preserve curved masters and shared boundaries through any boolean/flattening pipeline with explicit tolerances.

**Acceptance:** Every label hits its own target; every target is independently reachable; filling any target first reveals its intended visual area; completing all regions yields the same image regardless of fill order. Test permutations on small fixtures, not only completed previews.

### CD-S03 — P0: SVG serialization can turn an ID into an executable attribute

**Reproduced with a harmless local marker.** Input XML can contain a quote encoded inside an `id` value. The XML parser decodes it, and `emit_master_svg()` places the value directly inside an attribute using string interpolation without escaping. The resulting SVG can contain an injected event attribute that did not exist as an attribute in the original DOM.

The original fixture did not execute its marker. The sanitized result executed it when displayed as SVG DOM content and given a mouse event. No network access or data extraction was performed. This was **not** an exploit test against the uploaded app's deployed origin. Its current `<img>` preview is a different execution context; that distinction must not be lost. Serving the same SVG as a standalone same-origin document remains a concern.

**Evidence:** `studio/svg_master.py:62–66, 476, 594–611, 624–627`; `studio/app.py:165–172`; `reproductions/security-results.json`.

**Fix:** Use an XML serializer with escaping, generate internal safe IDs, rewrite internal references structurally, and apply explicit element/attribute allowlists. Add security tests after serialization. Review raw SVG serving and restrictive CSP/sandbox or download-only handling as defense in depth. Do not rely on the custom request header as authentication. Review public gateway exposure before hosting.

**Acceptance:** Encoded quotes, ampersands and angle brackets remain attribute text or are rejected. They cannot introduce event attributes, elements or external resource references after serialization.

### CD-S04 — P1: Native SVG appearance is not faithfully preserved

**Reproduced through the actual sanitize → compile path.** All of these fixtures still passed geometry validation:

| Feature | Observed defect |
|---|---|
| Filled shape with a black stroke | Sanitized master loses its stroke; compiled edge becomes red instead of black. |
| Red foreground at 50% fill opacity over blue | Original/sanitized pixel is approximately purple [128,0,127]; compiled pixel is opaque red [255,0,0]. |
| Object opacity | Opacity is also omitted from the compiled paint representation. |
| Nested subpaths using nonzero fill | Original center is filled; compiled center becomes a hole because the fill rule is forced to evenodd. |
| Gradient `x2="100%"` | Percentage is parsed as 100 rather than 1 in object-bounding-box coordinates; gradient is almost flat at the test point. |
| `gradientTransform` | Attribute keys are lowercased, but this one is looked up using mixed case; rotation is ignored. |
| Ink originally behind a filled shape | Sanitization groups ink after filled shapes, making the hidden line reappear on top. |

**Evidence:** `studio/svg_master.py:62–78, 274–346, 594–611`; `studio/pipeline.py:277–280, 1253–1277`; `reproductions/compiler-results.json`; fixture original/sanitized/compiled PNGs.

**Fix:** Carry fill rules, opacity, fill-opacity, stroke styling, transforms and ordering through a real intermediate scene representation. Distinguish intentionally unsupported SVG features from accepted features. Reject or explicitly report unsupported styles rather than silently claiming full fidelity. Add before/after visual fixtures, including repeated sanitize/import round trips.

### CD-S05 — P1: This is an artwork compiler/workbench, not yet a drawing studio

**Source-confirmed product gap.** The region inspector supports merge, group, palette reassignment, label movement, converting to detail, and splitting disconnected components. Its Split action does not draw a cut through a connected region; it raises an error on a single connected target. There is no native pen/Bézier-node editor, boundary drawing, shape resizing or connected-region knife tool in the reviewed UI.

**Evidence:** `studio/models.py:53–60`; `studio/pipeline.py:1373–1419`; `src/components/studio/right-panel.tsx:432–535`.

**Fix:** Either position the current MVP honestly as a compiler/review tool or add focused vector-authoring tools: node selection and handles, shape tools, shared-edge-aware region cuts, region merging, snapping, layer locking and keyboard undo/redo. Do not replace this with an unconstrained raster brush when the output must remain vector regions. A reference image can be a locked guide layer.

### CD-S06 — P1: Complexity controls do not match the native SVG route

**Source-confirmed.** The SVG-generation prompt asks for **20–60 shapes**. The included native sample has **40 playable regions**. `compile_svg_master()` enumerates source shapes and does not use `target_regions` to create hundreds of new regions. Raster controls such as paint-tone count and sampling resolution are still shown without route-specific treatment.

**Evidence:** `studio/ai.py:34–45`; `studio/pipeline.py:1188–1304`; `src/components/studio/right-panel.tsx:141–218`.

**Fix:** Separate “Import / Generate native SVG” from “Trace raster image.” Explain which settings apply. Native SVG should offer meaningful structural complexity and optional explicit subdivisions, not promise that changing the target slider will transform a 40-region picture into an expert-level artwork. Difficulty should be based on playability metrics and tests, not just count or line density.

### CD-S07 — P1: Palette assignment and actual recoloring are conflated

**Reproduced, but potentially intentional semantics.** The palette action changes `paletteId` and recalculates labels. Both `paint.json` and `colored.svg` remain byte-for-byte unchanged in the probe. For the detailed number mode, completing a region reveals the existing underpainting, not a newly selected flat color.

This can be valid for reassignment of number groups. It is not a visual recoloring tool. The label “Set palette” is too ambiguous for an authoring studio.

**Evidence:** `studio/pipeline.py:1402–1405`; `src/lib/detailed-board.ts:531–545`; `reproductions/compiler-results.json` (`palette_edit`).

**Fix:** Separate “Assign number group” from “Recolor appearance.” For true free-color artwork authoring, define solid-fill versus shade-preserving recoloring and add an actual custom color picker. The current studio always creates its board in `mode: "number"`; the existence of a free-mode enum alone does not expose a free-color authoring workflow (`use-studio.tsx:354–360`).

### CD-S08 — P1: Runtime exports carry substantial authoring/debug geometry

**Measured.** The current 552-region curved-raster example has:

- `regions.json`: **6,273,991 bytes**.
- `paint.json`: **4,286,185 bytes**.
- The four primary JSON files together: **10,567,872 bytes** uncompressed.
- Runtime ZIP: **8,775,891 bytes**.

The current native example is much smaller: **87,727 bytes** for the four JSON files and **54,337 bytes** for the runtime ZIP. These are different artworks/detail levels, so this is not a controlled comparison of algorithm efficiency.

Region data duplicates `d` in `master.d`, rings in `rings` and `flat.rings`, and for raster builds also includes legacy rings. Runtime export removes the source-master reference but retains much authoring/debug data. Multiple SVG preview variants add package weight. The React loader eagerly reads all four primary files.

**Evidence:** `studio/pipeline.py:275–290, 1436–1458`; `src/lib/detailed-board.ts:212–222`; `reproductions/export-metrics.json`.

**Fix:** Define full authoring and lean runtime formats. Include only the geometry needed by the chosen runtime, an explicit authority/tolerance contract and required visual paths. Keep trace-debug rings and comparison previews out of normal runtime delivery. Profile frame time on target devices; coalesce pointer/viewport work per frame, update labels only when needed and consider cached/tiled static appearance while retaining vector source truth. Do not claim that SVG alone guarantees smooth zoom.

### CD-S09 — P1: Validation status overstates gameplay readiness

**Reproduced.** The importer declares SVG overlaps nonfatal, and a label can pass because it is inside its own original shape even when another shape owns the tap. Geometry-only tests do not check source appearance preservation. Every visual fixture above passes geometry QA. Review/export remains draft-capable, which is useful, but “Export game bundle” does not distinguish a development export from an approved published asset.

**Evidence:** `studio/pipeline.py:879–901, 918–922, 944–976, 1022–1025`; `studio/app.py:310–323, 344–347`; `src/components/studio/right-panel.tsx:373–399, 601–615`.

**Fix:** Distinguish geometry validity, visible ownership, visual fidelity, runtime compatibility and gameplay usability. Keep draft export available, but create an explicit publication gate. A human checkbox must not override deterministic failures. Use structured issue records with region IDs so clicking a warning focuses the faulty region.

### CD-S10 — P1: Strict TypeScript failures are hidden by build configuration

**Reproduced on the renderer file.** A strict standalone check reports:

1. `detailed-board.ts:482`: a `(KeyboardEvent) => void` callback is not assignable to a general `EventListener`.
2. `detailed-board.ts:685`: `DOMPoint | null` is assigned to a non-null `DOMPoint` drag anchor.

`next.config.ts` sets `typescript.ignoreBuildErrors: true`. This review did not run a full Next build; the two errors were found without that build.

**Evidence:** `tsc-renderer.log`; `next.config.ts:6–8`; `src/lib/detailed-board.ts:482, 675–686`.

**Fix:** Narrow event types inside listener wrappers, guard missing transforms/anchors, remove ignored build errors after fixing them, and add independent `tsc --noEmit` and lint CI steps.

### CD-S11 — P2: Editing layout favors controls over the canvas

**UI source review, not full-shell visual measurement.** Two 300px sidebars surround the canvas on desktop, with art direction and brief controls occupying persistent space. At tablet widths the inspector moves below the workspace; on mobile the art-direction and inspector panels become long stacked sections. Frequent action text is often 9–11px. The current viewport itself is a useful starting point, but editing needs a more focused layout.

**Evidence:** `src/app/globals.css:129–151, 210–258`; `src/components/studio/canvas-workspace.tsx:108–166, 225–234`; `src/components/studio/right-panel.tsx:465–535`.

**Recommended UX:** A desktop-first studio with a compact workflow header, collapsible project/layer navigator, large persistent canvas, contextual inspector and compact bottom status/QA strip. Hide chat and the brief after master approval unless requested. Use drawers at tablet widths; mobile can prioritize preview/QA rather than squeezing a full editor into the same layout. Keep number-view boundaries visible during region editing instead of relying on full-color preview plus selection alone.

### CD-S12 — P2: Distinguish real ink from optional game subdivisions

All gameplay outlines currently share the same stroke language, while traced ink can add a very dense overlay. The raster sample is visually busy at fit-to-canvas. More line density does not automatically make a more satisfying hard puzzle.

**Evidence:** `studio/pipeline.py:1006–1017`; `src/lib/detailed-board.ts:418–447`; `reproductions/curved_raster-numbered.png`.

**Fix:** Keep independent artwork ink, structural boundaries, optional artificial subdivision boundaries and labels. Expose overlay toggles in the studio. Test thin/dashed artificial boundaries against stronger true contours and make export/runtime line styles explicit. Preview gallery line art and completed art separately; the studio itself should still allow viewing the finished reference for QA.

### CD-S13 — P2: Publication metadata is not yet a duel release contract

The manifest already has `contentHash`, version and an explicitly unrated difficulty, which are good. Studio doesn't need a multiplayer server, but it should export artifacts the game can identify and validate deterministically. Object grouping exists; the shipped assets are not automatically difficulty-validated.

**Evidence:** `studio/pipeline.py:1000–1003, 1292–1305, 1420–1425`; current manifests and `validation.json`.

**Fix:** Define a published-artifact identity: schema/runtime contract version, content hash, stable region IDs, visible area and label clearance metrics, adjacency/object-group metadata where needed, approved difficulty profile, publication state and runtime budgets. Use the same artifact hash for both sides of a duel. Keep scoring authority in the game/server rather than trusting editor preview state. Avoid assigning ranked suitability based only on region count.

### CD-S14 — P2: Packaged history has missing revisions

The native treehouse project lists `rev-284441c8a2294774` and `rev-08418b6db3824931`, but their artifact directories are absent in this archive. The current revision exists. This could be a packaging omission, not necessarily a live data-loss bug; the distinction is unresolved.

**Evidence:** `workspace/art-9761ab8df8004aa3/project.json`, compared with its extracted `revisions/` directory; `inventory.json`.

**Fix:** Validate project-history references during packaging/startup and disable missing revisions in the restore selector with an explicit explanation. Add workspace backup/export integrity tests.

## Suggested implementation order

**Phase A — Make outputs trustworthy:** CD-S01, S02, S03, S04, S09 and S10. Add the small adversarial fixtures first, then repair code until those tests pass. Do not accept “works on the fully colored preview” as proof.

**Phase B — Make this an authoring tool:** Route-aware native-vector/raster setup; real boundary/node tools; connected-region cuts and topology-preserving merges; clear palette semantics; focused canvas/inspector UX; separate ink and subdivision layers.

**Phase C — Make it a publishing pipeline:** Lean runtime packages, exact game-import tests, device budgets, structured region QA, artifact identity and difficulty/playtest metadata. Additional artwork production should follow these fixes, not substitute for them.

## Minimum release test matrix

A publishable artifact should satisfy all of the following:

1. Import → sanitize → compile preserves accepted SVG semantics and forbids executable content; unsupported features produce explicit diagnostics.
2. Curves, holes, transforms, gradients, transparency and ink order have visual golden tests.
3. Every reachable connected playable region has a safe label that hit-tests to itself at supported view transforms.
4. Any region can be completed first; fill order does not affect its intended result; completion and undo remain consistent.
5. Studio and game adapters accept the same supported fixtures and reject the same malformed inputs.
6. Shared boundaries remain consistent after merge, split and edits; widening tolerance must not conceal expanding topology defects.
7. Number reassignment and visual recoloring have distinct tests and UI wording.
8. A fresh runtime export passes schema/identity checks, published checksums and measured loading/interaction budgets on target devices.

## External technical references

The project-specific findings above come from source and executed probes, not from these references. The references explain the relevant platform semantics:

- W3C SVG 2, **Painting: Filling, Stroking and Marker Symbols** — fill rules, fill opacity and stroke semantics (`https://www.w3.org/TR/SVG2/painting.html`).
- W3C SVG 2, **Paint Servers** — gradient coordinate systems and transforms (`https://www.w3.org/TR/SVG2/pservers.html`).
- W3C SVG 2, **Rendering Model** — rendering-tree order and compositing (`https://www.w3.org/TR/SVG2/render.html`).
- scikit-image 0.25 documentation, **skimage.segmentation.slic** — approximate number of superpixel labels and compactness (`https://scikit-image.org/docs/0.25.x/api/skimage.segmentation.html`).
- OWASP, **Cross Site Scripting Prevention Cheat Sheet** — attribute-context encoding and safe output handling (`https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html`).

## Bottom line

Do not replace the whole project. Its compiler/workbench architecture has useful parts. But the next milestone should be **a small vector artwork that survives import, editing, validation and game playback correctly in every fill order**, not a larger artwork library or another layer of UI polish.
