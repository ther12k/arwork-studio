"""Task 36 — prepare the REAL Color Duel app with the golden packs.

Builds the game (from COLOR_DUEL_DIR or ../color-duel), drops the UNCHANGED
golden pack exports into its static artworks/ area, appends catalog entries,
and stages everything under tests/game-integration/.serve/ for a plain static
file server. No Studio backend, no pack repair: the exported JSON files are
served byte-for-byte (only the game's own catalog index gains entries — the
studio export intentionally does not ship the game's catalog).

Records .serve/game-pin.json: game repo, commit, dirty flag, pack manifest
hash — the reviewer's "pin both sides" rule.

Idempotent: skips the build when the staged pin already matches.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

STUDIO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = STUDIO_ROOT / 'mini-services' / 'color-duel-studio' / 'tests' / 'golden-packs'
SERVE = Path(__file__).resolve().parent / '.serve'

DIFFICULTY_MAP = {'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'master': 'Hard'}


def game_dir() -> Path:
    env = os.environ.get('COLOR_DUEL_DIR')
    if env:
        p = Path(env)
    else:
        p = STUDIO_ROOT.parent / 'color-duel'
    if not (p / 'package.json').is_file():
        print(
            f'Color Duel game not found at {p}. Set COLOR_DUEL_DIR or clone the '
            'game next to this repo (see game-pin.json for the pinned repo).',
            file=sys.stderr,
        )
        sys.exit(3)
    return p


def git_state(g: Path) -> dict:
    def run(*args):
        return subprocess.run(['git', *args], cwd=g, capture_output=True, text=True).stdout.strip()
    return {
        'repo': run('remote', 'get-url', 'origin') or None,
        'sha': run('rev-parse', 'HEAD') or None,
        'dirty': bool(run('status', '--porcelain')),
    }


def main() -> None:
    g = game_dir()
    pin_in = git_state(g)
    packs_manifest = json.loads((GOLDEN / 'manifest.json').read_text())
    packs_hash = hashlib.sha256((GOLDEN / 'manifest.json').read_bytes()).hexdigest()
    pin = {**pin_in, 'studioPacksManifestSha256': packs_hash}

    force = '--force' in sys.argv
    if (SERVE / 'game-pin.json').is_file() and not force:
        try:
            if json.loads((SERVE / 'game-pin.json').read_text()) == pin and (SERVE / 'index.html').is_file():
                print(f'.serve already prepared (pin matches, game {pin["sha"][:8]})')
                return
        except Exception:
            pass

    print(f'building Color Duel at {g} ({pin["sha"][:8]}{", dirty" if pin["dirty"] else ""})...')
    # The game is a bun project (bun.lock).
    if not (g / 'node_modules').is_dir():
        subprocess.run(['bun', 'install'], cwd=g, check=True)
    subprocess.run(['bun', 'run', 'build'], cwd=g, check=True)

    if SERVE.exists():
        shutil.rmtree(SERVE)
    SERVE.mkdir(parents=True)
    shutil.copytree(g / 'dist', SERVE, dirs_exist_ok=True)

    # Golden packs: exported zips extracted UNCHANGED into the game's static
    # artworks area; catalog gains entries (the game's own index file).
    catalog_path = SERVE / 'artworks' / 'catalog.json'
    catalog = json.loads(catalog_path.read_text())
    existing = {a['id'] for a in catalog['artworks']}
    for pack in packs_manifest['packs']:
        with zipfile.ZipFile(GOLDEN / pack['zip']) as zf:
            # zip entries already carry the artworks/<id>/ prefix
            zf.extractall(SERVE)
        if pack['artworkId'] not in existing:
            rating = (pack.get('difficulty') or {}).get('rating', 'easy')
            catalog['artworks'].append({
                'id': pack['artworkId'],
                'title': f'QA {pack["id"].replace("qa-", "").replace("-", " ").title()}',
                'manifest': f'artworks/{pack["artworkId"]}/artwork.json',
                'regionCount': pack['regionCount'],
                'category': 'Studio QA',
                'difficulty': DIFFICULTY_MAP.get(rating, 'Easy'),
            })
    catalog_path.write_text(json.dumps(catalog, indent=2))
    (SERVE / 'game-pin.json').write_text(json.dumps(pin, indent=2))
    print(f'staged {len(packs_manifest["packs"])} golden packs into {SERVE}')


if __name__ == '__main__':
    main()
