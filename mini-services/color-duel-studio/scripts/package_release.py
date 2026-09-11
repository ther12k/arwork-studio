#!/usr/bin/env python3
"""Build a CLEAN release archive of the Color Duel studio (production hygiene).

Distributing a workspace/database/build-artifact tarball leaks project state
and, worse, secrets. This script packs only the shippable source:

  INCLUDED   studio backend (app/pipeline/models/ai/...), web/ (vanilla JS
             reference client), integration/, schemas/, scripts/, tests/,
             docs/, examples/ (fixtures), requirements*.txt, compose.yaml,
             Dockerfile, README, run.py.
  EXCLUDED   workspace/ (projects, revisions, playtests, uploads),
             __pycache__/, node_modules/, .pytest_cache/, .git/, .env and
             .env.local/.env.production (secrets; the .env.example template
             ships), *.log, .DS_Store, tool-results/, dist/.

Usage:  python scripts/package_release.py [output.tar.gz]
Output: dist/color-duel-studio-<version>.tar.gz (version from studio/app.py)
"""
from __future__ import annotations

import pathlib
import re
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'dist'

EXCLUDE_DIRS = {'__pycache__', 'node_modules', 'workspace', 'tool-results',
                '.pytest_cache', '.git', 'dist', '.zscripts'}
EXCLUDE_FILES = {'.env', '.env.local', '.env.production', '.DS_Store'}
EXCLUDE_SUFFIXES = ('.log', '.pyc', '.db')


def version() -> str:
    # Authoritative source: studio/__init__.py STUDIO_VERSION (one constant,
    # everything derives from it). app.py's FastAPI(version=...) stays a
    # fallback for older checkouts.
    text = (ROOT / 'studio' / '__init__.py').read_text(encoding='utf-8')
    match = re.search(r"STUDIO_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not match:
        text = (ROOT / 'studio' / 'app.py').read_text(encoding='utf-8')
        match = re.search(r"['\"]version['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
        if not match:
            match = re.search(r"version=['\"]([^'\"]+)['\"]", text)
    return match.group(1) if match else '0.0.0'


def include(path: pathlib.Path) -> bool:
    if path.name in EXCLUDE_FILES:
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return path.name not in EXCLUDE_DIRS


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else \
        OUT_DIR / f'color-duel-studio-{version()}.tar.gz'
    if target.exists():
        target.unlink()
    count = 0
    with tarfile.open(target, 'w:gz') as tar:
        for path in sorted(ROOT.rglob('*')):
            rel = path.relative_to(ROOT)
            parts = set(rel.parts)
            if parts & EXCLUDE_DIRS:
                continue
            if path.is_file() and include(path):
                tar.add(path, arcname=str(pathlib.Path('color-duel-studio') / rel))
                count += 1
    print(f'{target}  ({count} files, {target.stat().st_size / 1024:.1f} KiB)')
    # Safety net: refuse to ship anything that smells like a secret.
    # .env.example is the committed documentation template — allowed to ship.
    with tarfile.open(target) as tar:
        leaked = [n for n in tar.getnames()
                  if (n.endswith('.env') or '/.env' in n or n.endswith(('.db', '.log')))
                  and not n.endswith('.env.example')]
    if leaked:
        target.unlink(missing_ok=True)
        print('REFUSED: secret/artifact files leaked into the archive: ' + ', '.join(leaked), file=sys.stderr)
        return 1
    print('Clean: no .env, database, log or workspace files inside.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
