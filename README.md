# arwork-studio — Color Duel Art Studio

A single-user local artwork authoring tool: reference image + art-direction chat → approved
master → real vector paint + playable region geometry → review / play-test → versioned
game-ready ZIP bundle.

The compiler, editor, validation and export work **without any API key**. AI chat, image
generation and image edits are optional and run through a local OpenAI-compatible bridge
(`z-ai-web-dev-sdk`) — no external OpenAI key required. Paid actions always require an explicit
confirm checkbox.

## Architecture

| Piece | Stack | Where |
|---|---|---|
| Frontend (SPA) | Vite · React 19 · Tailwind 4 · shadcn/ui | `src/`, entry `index.html` + `src/main.tsx` |
| Studio backend | Python · FastAPI · numpy/scipy/scikit-image/shapely/CairoSVG | `mini-services/color-duel-studio/` |
| AI bridge | bun · OpenAI-compatible shim over `z-ai-web-dev-sdk` | `mini-services/ai-bridge/` |
| Gateway | Caddy on :81 — serves the SPA, routes `?XTransformPort=` to the services | `Caddyfile` (dev) · `docker/Caddyfile` (Docker) |

There is **no database**: the Python backend persists projects, immutable geometry revisions and
exports as plain files under `mini-services/color-duel-studio/workspace/`. The exported game
bundle (format `color-duel-detailed-vector-1`) is self-contained — the game runtime does not need
this backend at all (see `mini-services/color-duel-studio/docs/INTEGRATION.md`).

## Run

Docker (recommended):

```bash
docker compose up --build
# open http://localhost:81
```

Or natively: frontend with `bun install && bun run dev` (port 3000), backend per
`mini-services/color-duel-studio/README.md` (Python 3.12+, port 8765), then Caddy :81 as the
gateway. In dev the frontend talks to the backend only through the gateway
(`?XTransformPort=8765` + `X-Studio-Request: 1`).

## Development

```bash
bun run lint            # eslint
bunx tsc --noEmit       # typecheck (strict)
bun run build           # typecheck + static production build → dist/
cd mini-services/color-duel-studio && python -m pytest tests/ -q
```

CI (GitHub Actions) runs the backend pytest suites + a release-archive leak assertion, and the
frontend typecheck + lint + static build.

> Single-user local MVP by design — no authentication, no multi-user collaboration. Do not
> expose it publicly without adding those controls. See `worklog.md` for the full development
> history and `AGENTS.md` for agent-oriented repo guidance.
