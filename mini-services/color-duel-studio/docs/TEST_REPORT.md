# Delivery test report

## Executed

**31 Python tests passed.** Scope includes clean image upload, invalid/SVG upload rejection, permission confirmation, API write guards, provider-key redaction, no-key behavior, paid-action confirmation, compilation, polygon holes, closed paths, path/ring equivalence, label interiors, full partition/no overlaps, metadata checksums, runtime ZIP path correctness, authoring ZIP, group edit, adjacency-only merge, invalid label move rejection, precolored-detail conversion and mocked AI transport contracts.

**23 offline browser component checks passed.** Chromium rendered the actual studio HTML/CSS/JS, using an in-process FastAPI test client as a fetch bridge. The test exercised source loading, real compilation, vector preview, pointer hit testing, correct/incorrect fills, hidden labels, undo, zoom/fit, region selection, group-edit revision creation, export ZIP integrity, responsive layout and project persistence. There were no JavaScript exceptions.

The browser environment blocks URL navigation. We did not modify its policies. `tests/offline_ui_check.py` uses DOM content and local in-process API responses without browser networking; image loading and storage are test adapters. These checks are **not a direct HTTP browser end-to-end certification**. The supplied `tests/browser_smoke.py` can run that flow on the user's local machine.

**Bundled sample compiled and validated:** 622 playable regions, 58 precolored detail regions, 32 palette groups, 80 combined paint paths with 15,433 color subshapes. Geometry union covers the 576 × 768 canvas; no overlap, no missing area and no empty raster-roundtrip pixels. Precolored area is approximately 3.288%.

The screenshots in `docs/screenshots` show the actual app rendered during the offline browser component test, not image-generated product mockups.

## Not executed / not established

- Live OpenAI requests, actual provider account/model access, billed prices or real-world AI latency.
- Docker image build or fresh dependency installation on Windows/macOS.
- Direct-HTTP browser navigation in this restricted environment.
- Real touch-device pinch performance, native Flutter integration or low-end phone memory/frame-rate tests.
- Production hosting, authentication, durable job recovery or concurrent multi-user authoring.
- Semantic/artistic correctness of every generated region, commercial rights clearance or ranked-duel balance.

The geometric validation result must not be relabeled as "publish-ready" or "copyright-safe". Run a visual and play review for every artwork.
