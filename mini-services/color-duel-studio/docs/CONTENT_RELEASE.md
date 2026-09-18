# Task 41 — Artwork Content Release Pipeline

The game repo intentionally does NOT track the hundreds of MB of generated
artwork packages — only `public/artworks/catalog.json` is in git. This
document is the contract for delivering the actual content from the Studio
to a deployment (local dev, CI, or production storage) without ever letting
a player see a catalog entry whose bytes are absent.

## The one unbreakable ordering rule

```
1. stage/upload the pack ASSETS first
2. verify every staged byte against its SHA-256
3. publish the catalog entry LAST
```

A catalog entry pointing at absent bytes = a player taps a card and gets a
broken artwork. Assets-first means the worst case is an unseen pack — which
nobody ever observes. `scripts/release_artwork_content.py` implements the
rule and its tests pin it (`tests/test_content_release.py` forces a
verification failure and asserts the catalog stayed untouched).

## Immutable, content-addressed versions

Each release lands under

```
<out>/artworks/<artworkId>/<contentVersion>/…
```

and is never overwritten or deleted. The pipeline records per pack:

```json
{
  "id": "mosslight-cottage",
  "version": "1.4.0",
  "contentHash": "<sha256 over the sorted 'relpath sha256' lines>",
  "files": { "artwork.json": "<sha256>", "regions.json": "<sha256>", "…": "…" }
}
```

- **contentHash** changes when ANY byte of ANY file changes.
- The release **version bumps its patch** relative to the previous RELEASE
  (tracked in `release-state.json`) — never relative to the pack's
  self-declared `version`, because studio re-exports may repeat it.
- Catalog `manifest` paths are **versioned**
  (`artworks/<id>/<version>/artwork.json`), so browsers/CDNs can cache
  aggressively: old bytes are never served under a new manifest, and old
  versions keep working for players mid-session.

## Usage

```bash
python scripts/release_artwork_content.py \
  --packs  workspace/exported/mosslight-cottage workspace/exported/… \
  --out    /srv/content-release \
  --catalog /path/to/color-duel/public/artworks/catalog.json \
  --state  release-state.json
```

- `--out` is the content root the game serves (in production: sync it to
  object storage/CDN — the layout is directly uploadable).
- `--catalog` is upserted atomically (tmp file + rename) and ONLY after
  every pack verified.
- Re-running with unchanged content is a no-op for that pack (same version,
  same bytes, catalog entry refreshed).
- Any verification failure exits non-zero having written nothing visible.

## Who consumes what

- **The game** follows the catalog: each entry's `manifest` path resolves
  sibling assets (`regions.json`, `palette.json`, `paint.json`, previews)
  relative to itself, so versioned directories need zero game changes
  (`artworkRepository.loadArtworkPack` skips entries whose packages are
  absent — a stale catalog row degrades to a skipped card, never a broken
  gallery).
- **CI** uses a slice of the same contract: `tests/game-integration/
  prepare_game.py` stages the golden packs into a static serve and appends
  catalog entries — assets first, catalog last, exactly like production.
- **Progress identity** (`src/lib/progressStore.ts` in the game) keys on the
  pack's `version` — a re-release under a new version starts players fresh
  on that content, by design.
