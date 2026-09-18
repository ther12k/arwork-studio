"""Task 41 — Artwork Content Release Pipeline.

The game repo deliberately does NOT track the hundreds of MB of generated
artwork packages (only catalog.json is in git). Production therefore needs a
reproducible delivery path from the Studio to the game, with ONE ordering
rule that must never be violated:

    1. stage/upload the pack ASSETS first,
    2. verify every staged byte against its SHA-256,
    3. publish the catalog entry LAST.

A catalog entry that points at absent bytes means a player taps a card and
gets a broken artwork; assets-first means the worst case is an unseen pack.

Every release is IMMUTABLE and content-addressed: a pack's files land under

    <out>/artworks/<artworkId>/<contentVersion>/...

(old versions are never overwritten or deleted), and the release manifest
records, per pack:

    {
      "id": "...", "version": "...", "contentHash": "<sha256>",
      "files": {"artwork.json": "<sha256>", ...}
    }

contentHash is the SHA-256 over the sorted "<relpath> <sha256>" lines, so any
byte change in any file yields a new hash and — via the state file — a bumped
patch version. The catalog's manifest paths are versioned, so browsers/CDNs
can cache aggressively without ever serving old bytes under a new manifest.

Usage:
    python scripts/release_artwork_content.py \
        --packs exported/pack-a exported/pack-b \
        --out  /srv/content-release \
        --catalog /path/to/color-duel/public/artworks/catalog.json \
        --state release-state.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

CATALOG_FIELDS = ("title", "category", "difficulty")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash_of(files: dict[str, str]) -> str:
    """Content hash over the per-file hashes (sorted for determinism)."""
    blob = "".join(f"{name} {files[name]}\n" for name in sorted(files))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_pack(pack_dir: Path) -> dict:
    """Read a validated pack: artwork.json is mandatory (id + version)."""
    manifest_path = pack_dir / "artwork.json"
    if not manifest_path.is_file():
        raise SystemExit(f"pack {pack_dir}: artwork.json missing (run the studio validator first)")
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("id") or not manifest.get("title"):
        raise SystemExit(f"pack {pack_dir}: artwork.json lacks id/title")
    files = {
        str(p.relative_to(pack_dir)).replace(os.sep, "/"): sha256_file(p)
        for p in sorted(pack_dir.rglob("*"))
        if p.is_file()
    }
    if "artwork.json" not in files:
        raise SystemExit(f"pack {pack_dir}: no files hashed")  # pragma: no cover
    return {
        "id": manifest["id"],
        "title": manifest["title"],
        "declared_version": str(manifest.get("version") or "0.0.0"),
        "region_count": int(manifest.get("regionCount") or 0),
        "category": manifest.get("category"),
        "difficulty": manifest.get("difficultyLabel") or manifest.get("difficulty"),
        "dir": pack_dir,
        "files": files,
        "content_hash": content_hash_of(files),
    }


def bump_patch(version: str) -> str:
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        parts[2] = str(int(parts[2]) + 1)
    except ValueError:
        parts[2] = "1"
    return ".".join(parts)


def plan_version(pack: dict, state: dict) -> tuple[str, str]:
    """Return (version, action). Unchanged content keeps its version; changed
    content bumps the PREVIOUS RELEASE version's patch (never the pack's
    self-declared version — re-exports may repeat it, releases may not)."""
    previous = state.get("packs", {}).get(pack["id"])
    if previous and previous["contentHash"] == pack["content_hash"]:
        return previous["version"], "unchanged"
    if previous:
        return bump_patch(previous["version"]), "updated"
    return pack["declared_version"], "new"


def stage_assets(pack: dict, out: Path, version: str) -> Path:
    """Copy the pack's files into the immutable versioned directory. A
    pre-existing directory must already hold the exact bytes (releases are
    immutable — same directory name, same content) or the run aborts."""
    dest = out / "artworks" / pack["id"] / version
    if dest.exists():
        verify_staged(pack, dest, version)
        return dest
    for name in pack["files"]:
        src = pack["dir"] / name
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    return dest


def verify_staged(pack: dict, dest: Path, version: str) -> None:
    """Re-read every staged byte and compare with the manifest hashes. Any
    mismatch (partial copy, bit rot, tampering) aborts BEFORE the catalog is
    touched."""
    for name, expected in pack["files"].items():
        staged = dest / name
        if not staged.is_file():
            raise SystemExit(f"verify {pack['id']}@{version}: {name} missing after staging")
        if sha256_file(staged) != expected:
            raise SystemExit(f"verify {pack['id']}@{version}: {name} SHA-256 mismatch")
    extra = {
        str(p.relative_to(dest)).replace(os.sep, "/")
        for p in dest.rglob("*")
        if p.is_file()
    } - set(pack["files"])
    if extra:
        raise SystemExit(f"verify {pack['id']}@{version}: unexpected files {sorted(extra)}")


def catalog_entry(pack: dict, version: str) -> dict:
    base = f"artworks/{pack['id']}/{version}"
    return {
        "id": pack["id"],
        "title": pack["title"],
        "manifest": f"{base}/artwork.json",
        "regionCount": pack["region_count"],
        "category": pack["category"] or "Nature",
        "difficulty": pack["difficulty"] or "Medium",
    }


def publish_catalog(catalog_path: Path, entries: list[dict]) -> None:
    """Upsert entries into the game's catalog — THE LAST STEP. Atomic write
    (tmp file + os.replace) so a crash mid-write can never leave a truncated
    catalog."""
    catalog = json.loads(catalog_path.read_text()) if catalog_path.is_file() else {"artworks": []}
    by_id = {e["id"]: e for e in catalog.get("artworks", [])}
    for entry in entries:
        by_id[entry["id"]] = entry
    catalog["artworks"] = [by_id[k] for k in sorted(by_id)]
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=catalog_path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(catalog, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_name, catalog_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--packs", nargs="+", required=True, type=Path,
                        help="validated pack directories (artwork.json + assets)")
    parser.add_argument("--out", required=True, type=Path,
                        help="content release root (the object-storage staging area)")
    parser.add_argument("--catalog", required=True, type=Path,
                        help="the game's catalog.json — published LAST, only after verify")
    parser.add_argument("--state", type=Path,
                        default=Path("release-state.json"),
                        help="release state (contentHash -> version per pack) for bumping")
    args = parser.parse_args(argv)

    packs = [load_pack(p) for p in args.packs]
    state = json.loads(args.state.read_text()) if args.state.is_file() else {"packs": {}}

    staged: list[dict] = []
    for pack in packs:
        version, action = plan_version(pack, state)
        print(f"[{action:>9}] {pack['id']} -> v{version} (contentHash {pack['content_hash'][:12]}…)")
        dest = stage_assets(pack, args.out, version)
        verify_staged(pack, dest, version)  # hard stop BEFORE any catalog write
        staged.append({"pack": pack, "version": version, "action": action})

    # Everything verified — now the writes that make the release visible.
    entries = [catalog_entry(s["pack"], s["version"]) for s in staged]
    publish_catalog(args.catalog, entries)
    for s in staged:
        state["packs"][s["pack"]["id"]] = {
            "contentHash": s["pack"]["content_hash"],
            "version": s["version"],
        }
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2))

    manifest_path = args.out / "release-manifest.json"
    release_manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    for s in staged:
        release_manifest[s["pack"]["id"]] = {
            "id": s["pack"]["id"],
            "version": s["version"],
            "contentHash": s["pack"]["content_hash"],
            "files": s["pack"]["files"],
        }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(release_manifest, indent=2, sort_keys=True))

    print(f"published {len(entries)} catalog entr{'y' if len(entries) == 1 else 'ies'} "
          f"-> {args.catalog} (assets verified at {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
