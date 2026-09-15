"""Task 36 — Golden pack asset integrity + shipped-adapter contract.

The committed packs under tests/golden-packs/ are FIXED regression assets
(generated once by scripts/generate_golden_packs.py, pinned by sha256 in
manifest.json). This guard fails when:
  - a zip's bytes no longer match its pinned hash (asset drift),
  - the shipped game adapter (integration/detailed-board.mjs) rejects any
    pack — the bundle contract the real game loads,
  - the requested/measured difficulty split is missing from the manifest.
"""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / 'tests' / 'golden-packs'
ADAPTER_CHECK = ROOT / 'scripts' / 'adapter-contract-check.mjs'

EXPECTED_IDS = {
    'qa-composition',
    'qa-easy',
    'qa-hard',
    'qa-master',
    'qa-converted-balanced',
}


def test_golden_packs_are_pinned_and_complete():
    manifest = json.loads((GOLDEN / 'manifest.json').read_text())
    assert manifest['schema'] == 'golden-packs/1'
    ids = {p['id'] for p in manifest['packs']}
    assert ids == EXPECTED_IDS, f'golden pack set drifted: {ids ^ EXPECTED_IDS}'
    for pack in manifest['packs']:
        assert pack.get('requested'), f'{pack["id"]}: requested difficulty/source missing'
        assert pack.get('difficulty'), f'{pack["id"]}: measured difficulty missing'
        data = (GOLDEN / pack['zip']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == pack['sha256'], \
            f'{pack["id"]}: zip bytes no longer match the pinned hash — ' \
            'regenerate deliberately and update the pin'
        with zipfile.ZipFile(GOLDEN / pack['zip']) as zf:
            names = zf.namelist()
            assert any(n.endswith('/artwork.json') for n in names)
            assert any(n.endswith('/regions.json') for n in names)
            assert any(n.endswith('/paint.json') for n in names)


def test_golden_packs_pass_the_shipped_game_adapter(tmp_path):
    """ALL five packs — an adapter regression that only breaks e.g. the
    converted raster geometry or the 600-region master must not slip through
    on the composition pack's green result."""
    if shutil.which('bun') is None:
        pytest.skip('bun runtime unavailable')
    manifest = json.loads((GOLDEN / 'manifest.json').read_text())
    folders = []
    for pack in manifest['packs']:
        with zipfile.ZipFile(GOLDEN / pack['zip']) as zf:
            # zip entries already carry the artworks/<id>/ prefix
            zf.extractall(tmp_path)
        folder = tmp_path / 'artworks' / pack['artworkId']
        assert folder.is_dir(), f'{pack["id"]}: unexpected pack layout'
        folders.append(folder)
    assert len(folders) == len(EXPECTED_IDS)
    proc = subprocess.run(['bun', str(ADAPTER_CHECK), *(str(f) for f in folders)],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    assert proc.returncode == 0, f'adapter rejected a golden pack:\n{proc.stdout}\n{proc.stderr}'
    assert 'FAIL' not in proc.stdout
