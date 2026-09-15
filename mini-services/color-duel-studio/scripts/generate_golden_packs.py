"""Task 36 — Golden pack generator (fixed regression assets).

Generates the five golden game packs from deterministic inputs — NO AI, no
network, no per-run regeneration. The zips + manifest are committed under
tests/golden-packs/ and consumed by:

  - tests/test_golden_packs.py  (asset integrity + shipped-adapter contract)
  - tests/game-integration/     (the REAL Color Duel app loads them; Task 36)

Packs:
  qa-composition        the exact authoring-composition chain (import → pen →
                        recolor → reorder → Bézier edit → ordinary build) with
                        its known overlap probe (blob owns it after reorder)
  qa-easy / qa-hard /   one rich native-vector master (gradients, holes,
  qa-master             nonzero fill, ink strokes) compiled at escalating
                        auto-subdivide targets
  qa-converted-balanced reconstructed raster artwork through the Convert
                        pipeline (offline deterministic provider mock; balanced
                        fidelity, medium difficulty) — exercises rc-* shapes
                        and converted semantic ownership

Usage:  .venv/bin/python scripts/generate_golden_packs.py [--out tests/golden-packs]
"""

import argparse
import hashlib
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import STUDIO_VERSION  # noqa: E402
from studio.models import BuildSettings, EditRequest, ObjectUpdateRequest  # noqa: E402
from studio.pipeline import (  # noqa: E402
    compile_svg_master, edit_bundle, edit_objects_bundle, make_export,
)

OUT_DEFAULT = ROOT / 'tests' / 'golden-packs'

# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------

# The composition fixture (mirrors tests/test_authoring_composition.py — the
# same overlap probe and edit live inside the pack).
COMPOSITION_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<g data-cd-object="obj-blob" data-cd-name="Blob">
  <path d="M 50,100 C 50,60 120,40 160,70 C 200,100 200,160 160,190 C 120,220 50,190 50,100 Z" fill="#77AA55"/>
</g>
<g data-cd-object="obj-plain" data-cd-name="Plain">
  <rect x="40" y="230" width="220" height="60" fill="#CC8844"/>
</g>
</svg>'''

COMPOSITION_BUILD = dict(target_regions=30, palette_colors=4, paint_colors=16,
                         max_edge=300, min_region_pixels=4, min_label_radius=1.0,
                         auto_subdivide=False)
PEN_D = 'M 150,70 L 220,70 L 220,150 L 150,150 Z'
EDITED_BLOB_D = ('M 10,170 C 30,60 130,30 180,80 '
                 'C 230,130 270,180 200,230 C 130,280 30,240 10,170 Z')

# One rich native-vector master for the three difficulty tiers: gradient sky,
# overlapping rects, a nonzero-rule shape with a hole, translucent ellipse and
# open ink linework (mirrors the adapter-contract fixture family).
TIER_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 576 576">
<defs>
<linearGradient id="sky" gradientUnits="objectBoundingBox" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0" stop-color="#A9DBEF"/><stop offset="1" stop-color="#FFDCA6"/></linearGradient>
<radialGradient id="glow" gradientUnits="objectBoundingBox" cx="0.5" cy="0.5" r="0.5">
<stop offset="0" stop-color="#FFF3C4"/><stop offset="1" stop-color="#F2B34C"/></radialGradient>
</defs>
<rect x="0" y="0" width="576" height="576" fill="url(#sky)"/>
<circle cx="470" cy="110" r="70" fill="url(#glow)"/>
<rect x="60" y="240" width="230" height="230" fill="#3366AA"/>
<rect x="150" y="180" width="160" height="160" fill="#AA3355"/>
<path d="M 120,120 L 320,120 L 320,320 L 120,320 Z M 170,170 L 270,170 L 270,270 L 170,270 Z" fill="#884400" fill-rule="nonzero"/>
<ellipse cx="420" cy="330" rx="90" ry="55" fill="#FFFFFF" fill-opacity="0.45"/>
<path d="M 40,530 Q 160,480 280,530 T 520,530" fill="none" stroke="#29383E" stroke-width="3"/>
<path d="M 90,470 C 140,430 200,430 250,470" fill="none" stroke="#224466" stroke-width="2"/>
</svg>'''

TIERS = [
    ('qa-easy',   dict(target_regions=60,  auto_subdivide=True)),
    ('qa-hard',   dict(target_regions=300, auto_subdivide=True)),
    ('qa-master', dict(target_regions=600, auto_subdivide=True)),
]
TIER_BASE = dict(palette_colors=8, paint_colors=16, max_edge=256,
                 min_region_pixels=4, min_label_radius=1.0)


def _converted_png() -> bytes:
    """Deterministic 3-band illustration (sky / house / grass) for Convert."""
    from PIL import Image
    buf = io.BytesIO()
    im = Image.new('RGB', (288, 288))
    px = im.load()
    for y in range(288):
        for x in range(288):
            if y < 144:
                px[x, y] = (0x91, 0xCC, 0xDD)
            elif x < 144:
                px[x, y] = (0xEB, 0xC6, 0x81)
            else:
                px[x, y] = (0x41, 0xA5, 0x82)
    im.save(buf, format='PNG')
    return buf.getvalue()


def _mock_transport():
    """Offline deterministic provider mock (the smoke-suite contract)."""
    import httpx
    objects = [
        {'name': 'sky', 'description': 'blue sky', 'z': 0,
         'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
        {'name': 'house', 'description': 'yellow house', 'z': 1,
         'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
        {'name': 'grass', 'description': 'green field', 'z': 2,
         'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
    ]
    plan = json.dumps({'objects': objects})

    def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith('/json'):
            return httpx.Response(200, json={'output': [{'content': [
                {'type': 'output_text', 'text': plan}]}],
                'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            import re
            body = json.loads(req.content)
            m = re.search(r'\bbbox\s*\[?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', body.get('input', ''))
            if m:
                x0, y0, w0, h0 = (float(v) for v in m.groups())
            else:
                x0, y0, w0, h0 = 0, 0, 576, 576
            cx, cy = x0 + w0 / 2, y0 + h0 / 2
            w, h = max(w0 * 0.55, 40), max(h0 * 0.55, 40)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {w0} {h0}">'
                   f'<path d="M {cx - w / 2:.0f},{cy - h / 2:.0f} L {cx + w / 2:.0f},{cy - h / 2:.0f} '
                   f'L {cx + w / 2:.0f},{cy + h / 2:.0f} L {cx - w / 2:.0f},{cy + h / 2:.0f} Z" fill="#77AA55"/>'
                   f'</svg>')
            return httpx.Response(200, json={'output': [{'content': [
                {'type': 'output_text', 'text': svg}]}], 'usage': {'input_tokens': 200}})
        return httpx.Response(500, text='mock: unexpected path')
    return httpx.MockTransport(respond)


# ---------------------------------------------------------------------------
# Pack builders (each returns the exported lean zip bytes)
# ---------------------------------------------------------------------------

def build_composition(work: Path) -> bytes:
    src = work / 'master.svg'
    src.write_text(COMPOSITION_SVG, encoding='utf-8')
    settings = BuildSettings(**COMPOSITION_BUILD)
    compile_svg_master(src, work / 'c1', artwork_id='qa-composition', version='0.1.0',
                       title='QA Composition Golden', settings=settings)
    edit_bundle(work / 'c1', work / 'c2', EditRequest(
        base_revision='0.1.0', action='draw', region_ids=[], d=PEN_D,
        palette_id=1, paint=True, color='#FF7348', stroke_width=1.2), '0.1.1')
    pen_region = next(r for r in json.loads((work / 'c2' / 'regions.json').read_text())['regions']
                      if r['id'].startswith('r-p-'))
    edit_bundle(work / 'c2', work / 'c3', EditRequest(
        base_revision='0.1.1', action='recolor', color='#4C7FE8',
        region_ids=[pen_region['id']]), '0.1.2')
    edit_objects_bundle(work / 'c3', work / 'c4', ObjectUpdateRequest(
        base_revision='0.1.2', object_id='obj-blob', order_action='bring_to_front'), '0.1.3')
    objs = json.loads((work / 'c4' / 'objects.json').read_text())['objects']
    blob_shape = next(o for o in objs if o['id'] == 'obj-blob')['shapeIds'][0]
    edit_bundle(work / 'c4', work / 'c5', EditRequest(
        base_revision='0.1.3', action='shape', region_ids=[],
        shape_id=blob_shape, d=EDITED_BLOB_D), '0.1.4')
    # Ordinary Build: recompile from the (synced) authoritative master.
    compile_svg_master(work / 'c5' / 'source-master.svg', work / 'c6',
                       artwork_id='qa-composition', version='0.1.5',
                       title='QA Composition Golden', settings=settings)
    return make_export(work / 'c6', include_authoring=False)


def build_tier(work: Path, pack_id: str, overrides: dict) -> bytes:
    src = work / 'master.svg'
    src.write_text(TIER_SVG, encoding='utf-8')
    settings = BuildSettings(**{**TIER_BASE, **overrides})
    compile_svg_master(src, work / 'out', artwork_id=pack_id, version='0.1.0',
                       title=f'QA {pack_id.split("-", 1)[1].title()} Golden', settings=settings)
    return make_export(work / 'out', include_authoring=False)


def build_converted(work: Path) -> bytes:
    """Convert journey through the real app + offline provider mock.

    uuid4 is seeded with a fixed counter for the duration of the journey
    (same harness-side determinism as the offline provider mock): the real
    export code runs unmodified, but minted ids (project/artwork/session)
    — and therefore the pack bytes — reproduce across runs."""
    import itertools
    import uuid as uuid_mod
    from fastapi.testclient import TestClient
    import studio.generation as studio_generation
    from studio.app import create_app
    headers = {'X-Studio-Request': '1'}
    real_uuid4 = uuid_mod.uuid4
    seq = itertools.count(1)
    uuid_mod.uuid4 = lambda: uuid_mod.UUID(int=next(seq))
    # committedAt (manifest generation provenance) is stamped by
    # studio.generation._now — freeze it or every run hashes differently.
    real_now = studio_generation._now
    studio_generation._now = lambda: '2026-01-01T00:00:00+00:00'
    try:
        return _convert_journey(work, TestClient, create_app, headers)
    finally:
        uuid_mod.uuid4 = real_uuid4
        studio_generation._now = real_now


def _convert_journey(work: Path, TestClient, create_app, headers) -> bytes:
    with TestClient(create_app(work, transport=_mock_transport())) as c:
        pid = c.post('/api/projects', json={'title': 'QA Converted'},
                     headers=headers).json()['id']
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=headers,
                     json={'mode': 'image_convert', 'requested_difficulty': 'medium',
                           'fidelity': 'balanced'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r = c.post(f'{base}/source', headers=headers,
                   files={'file': ('src.png', _converted_png(), 'image/png')})
        assert r.status_code == 200, r.text
        r = c.post(f'{base}/convert', headers=headers,
                   files={'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200, r.text
        p = c.get(f'/api/projects/{pid}', headers=headers).json()
        deadline_import = __import__('time').time() + 120
        while __import__('time').time() < deadline_import and p['job']['status'] in ('queued', 'running'):
            __import__('time').sleep(0.1)
            p = c.get(f'/api/projects/{pid}', headers=headers).json()
        assert p['job']['status'] == 'done', p['job']
        r = c.post(f'{base}/commit', headers=headers, json={})
        assert r.status_code == 200, r.text
        rev = r.json()['revision']['id']
    folder = work / pid / 'revisions' / rev
    return make_export(folder, include_authoring=False)


# ---------------------------------------------------------------------------

def _normalize_zip(data: bytes) -> bytes:
    """Repack a zip with fixed entry metadata so the pinned sha256 is a
    function of CONTENT only — zipfile stamps wall-clock timestamps (and
    host-specific attrs) into entry headers, which made byte-identical
    content hash differently across runs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as dst:
        for name in sorted(n for n in src.namelist() if not n.endswith('/')):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            dst.writestr(info, src.read(name))
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    packs = []
    requested = {
        'qa-composition': {'source': 'authoring-composition chain (pen → recolor → reorder → Bézier edit → build)'},
        **{pid: {'source': 'native-vector master', 'targetRegions': ov['target_regions']}
           for pid, ov in TIERS},
        'qa-converted-balanced': {'source': 'Convert pipeline (offline provider mock)',
                                  'fidelity': 'balanced', 'difficulty': 'medium'},
    }
    builders = [('qa-composition', build_composition)]
    builders += [(pid, (lambda w, p=pid, o=ov: build_tier(w, p, o))) for pid, ov in TIERS]
    builders.append(('qa-converted-balanced', build_converted))

    for pack_id, build in builders:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            data = _normalize_zip(build(work))
            (out / f'{pack_id}.zip').write_bytes(data)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                art_name = next(n for n in zf.namelist() if n.endswith('/artwork.json'))
                manifest = json.loads(zf.read(art_name))
            packs.append({
                'id': pack_id,
                'requested': requested[pack_id],
                'zip': f'{pack_id}.zip',
                'sha256': hashlib.sha256(data).hexdigest(),
                'artworkId': manifest['id'],
                'format': manifest['format'],
                'regionCount': manifest['regionCount'],
                'difficulty': manifest.get('difficulty'),
                'artworkVersion': manifest['version'],
            })
        print(f'generated {pack_id}: {packs[-1]["regionCount"]} regions, '
              f'difficulty {packs[-1]["difficulty"]}')

    doc = {
        'schema': 'golden-packs/1',
        'studioVersion': STUDIO_VERSION,
        'generatedBy': 'scripts/generate_golden_packs.py',
        'note': 'Fixed regression assets — regenerate ONLY deliberately (this '
                'changes every content hash and requires a fresh pin).',
        'packs': packs,
    }
    (out / 'manifest.json').write_text(json.dumps(doc, indent=2) + '\n')
    print(f'wrote {out / "manifest.json"}')


if __name__ == '__main__':
    main()
