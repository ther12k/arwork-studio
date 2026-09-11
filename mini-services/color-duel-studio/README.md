# Color Duel Art Studio

A runnable **single-user local artwork authoring tool**, not another mockup or an image-only sample pack.

**Reference image + art-direction chat → approved master → real vector paint + playable region geometry → review → versioned game ZIP.**

The compiler, editor, validation and export work **without any API key**. AI chat, image generation and image edits require your own server-side OpenAI key and incur provider charges. These calls never happen automatically during a build.

## Stage-2 feature set (0.3.0)

- **Cut & pen region tools**: cut one region along a drawn line (`r-c-*` pieces, subdivision edges classified by proximity) and draw new pen regions (`r-p-*`). **Artwork mode** turns the shape into real finished artwork (a paint.json path with a stable `masterShapeId`) and carves the covered regions away — it works over fully covered artwork; **region-only mode** draws gameplay-only surfaces on uncovered canvas. Masks never overlap; edges are reclassified as artwork vs subdivision.
- **Edges + boundary style**: `geometry.edges` distinguishes true artwork boundaries (solid) from artificial subdivision boundaries (light dashed); optional `boundaryStyle` overrides. Legacy bundles render unchanged.
- **Deterministic auto-subdivide**: `auto_subdivide: true` splits oversized regions with seeded organic cuts until `target_regions` — true-vector, no rasterization (SVG-master and multi-stage builds).
- **Difficulty analyzer**: every revision's manifest carries a deterministic `difficulty` profile (rating/score/metrics: region count, required zoom, tiny regions, label clearance, palette ambiguity, adjacency, subdivision edges).
- **AI multi-stage SVG generation** (paid, explicit confirm): strict-JSON scene plan → per-object vector fragments → one composed sanitized master; the next build auto-applies the target region count.
- **Free color**: any `#RRGGBB` in free mode (flat fills, no mistakes); rAF-batched board gestures.

## Start locally

Use Python 3.12 or 3.13. On Linux, install Cairo if it is not already present (`sudo apt-get install libcairo2`). On macOS, `brew install cairo` provides the native renderer. Docker is the simpler option for Windows or native-library issues.

```bash
# from the repository root — this backend lives in mini-services/color-duel-studio
cd mini-services/color-duel-studio
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Open **http://127.0.0.1:8765**. Keep this local authoring MVP private.

1. Click **Try the bundled treehouse** (or upload your own finished image as a master).
2. Select 768px for a quick first run. Set the target regions and palette count.
3. Click **Build vector draft**.
4. Compare **Master**, **Vector**, **Numbered**, and **Play test**.
5. In **Edit regions**, select adjacent shapes to merge; adjust palette, number placement or object groups.
6. Review the warnings, then **Export game bundle .zip**.

The included compiled example has **622 playable regions, 58 fixed-detail regions, 32 palette groups, and 80 combined vector paint paths containing 15,433 color subshapes**. The requested region target was 750, not an exact output guarantee. It was compiled at 576 × 768 pixels. Exporting a larger SVG/PNG does not invent missing source detail.

## AI setup (two deployment modes)

The repository root README is the authoritative overview. This backend supports two ways to wire AI; without configuration the AI controls render disabled — never simulated.

**1. Bundled AI bridge (repository Docker setup — the default).** The root `compose.yaml` starts this backend together with the repository's AI bridge (`../ai-bridge`, an OpenAI-compatible shim over `z-ai-web-dev-sdk`) and injects:

```dotenv
OPENAI_API_KEY=local-z-ai-bridge   # placeholder; only satisfies the OpenAI client contract
AI_BASE_URL=http://aibridge:8787/v1/
CHAT_MODEL=glm-4.6
IMAGE_MODEL=cogview-4
```

No external OpenAI key is needed; actual AI traffic goes to the local bridge.

**2. External OpenAI-compatible provider (standalone backend).** Running this backend on its own, point it at any OpenAI-compatible endpoint — including `https://api.openai.com/v1/` (the default `AI_BASE_URL`), which **incurs provider charges**:

```dotenv
OPENAI_API_KEY=your-key
AI_BASE_URL=https://api.openai.com/v1/
CHAT_MODEL=your-chat-model
IMAGE_MODEL=your-image-model
```

Restart the server after editing `.env`. Keys stay server-side; `/api/config` exposes only a configured/not-configured flag and model names. Provider/model availability depends on your account.

**Inspiration workflow:** upload a reference, refine the brief through chat, then use **Generate from brief**. The image generator receives the brief, not the raw reference, on this path. Chat may receive the reference so it can discuss broad visual traits. This does not certify originality or copyright clearance.

**Owned-image editing:** upload with the ownership/permission checkbox, choose **Edit owned reference**, or choose **Revise current master** to edit an image already generated/imported in the project. Chat itself does not modify image pixels. Clicking Generate is the separate image action.

A confirmation checkbox is required for every paid action. Exactly one image is requested. Network errors are not automatically retried; inspect provider usage before repeating an ambiguous failed call. Without a key, AI controls are visibly disabled, not simulated.

## Docker option

```bash
cp .env.example .env
docker compose up --build
```

The Compose port binds to **127.0.0.1**, not a public network interface. The image recipe is included; the Docker build was not executed in this delivery environment.

## Files emitted for the game

```text
artworks/<id>/
  artwork.json
  regions.json
  palette.json
  paint.json
  colored.svg
  numbered.svg
  linework.svg
  ink.svg
  selected-preview.svg
  thumbnail.webp
  validation.json
```

Authoring export additionally includes the sanitized master, build settings and raster previews. The runtime export removes the sourceMaster reference when it omits that file.

**Every exported SVG contains real paths, not an embedded PNG.** The finished painting and numbered states derive from the same geometry. `regions.json` is mandatory for gameplay; the numbered preview is not its replacement. The current format is `color-duel-detailed-vector-1`, aligned with the previous detailed asset pack in this conversation.

## Integration

The game does not need this Python backend at runtime. Import the emitted asset folder and use `integration/detailed-board.mjs` (or the React wrapper). The same data can be rendered by Flutter/native painters. See `docs/FORMAT.md` and `docs/INTEGRATION.md`.

This is **not a patch verified against the current GitHub repository**. GitHub access did not succeed in this environment. It uses the schema in the existing asset ZIP, rather than guessing your current source structure or stack.

## Tests and useful commands

```bash
python -m pytest -q
python scripts/validate_artwork.py examples/compiled-treehouse
python scripts/compile_artwork.py examples/treehouse-source.png output/my-art --id my-art --regions 750 --palette 32 --tones 80 --edge 768
```

`tests/browser_smoke.py` is a direct-browser smoke script for a local Chromium environment. See `docs/TEST_REPORT.md` for what was actually executed here, including limitations.

## Important limits

This is a working local **MVP**, not a hosted production service. Automatic segmentation is image-aware, not semantic: it may split a flower or combine an unwanted part of a roof. Review, merge, fix labels and assign objects before shipping. Semantic segmentation and node-level boundary editing remain next-stage work; the cut/pen tools, auto-subdivide and edges layers cover the region-topology part of that gap.

The generated paint layer approximates the master through a finite color trace with cleanup. Fine texture is intentionally simplified; high-detail tracing increases file size. Exact pixel-edge boundaries avoid gaps, but may look stair-stepped at extreme zoom. Device performance and ranked-duel balance need separate validation.

The workspace persists project JSON, sanitized images and immutable geometry revisions on disk. Back it up. Jobs use one in-process worker and are marked interrupted after a restart; there is no durable distributed queue, authentication or multi-user collaboration. Do not expose the server publicly without adding those controls.
