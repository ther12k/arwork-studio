"""Task 35 — No-AI full-stack authoring composition smoke.

The reviewer's closing direction for the accepted Task 33 slice: prove the
COMPLETE authoring-to-game workflow on the REAL backend and compiler (no AI
provider, no browser fixture staging) and check COMPOSITION — each operation
must preserve the valid changes made by the previous operation:

    import a fixed SVG
      → add an artwork Pen shape (paint + playable region, overlap carved)
      → recolor it
      → change layer order
      → edit a Bézier handle (shape action)
      → ordinary Build (reload-equivalent: recompiles from the master)
      → export the artwork pack
      → load it in the shipped Color Duel adapter (integration contract)

Checked at every step: QA green, stable shape identity + object ownership,
visible tap ownership at an overlap probe, and the exported answer key
(region↔palette consistency, count identity). The real in-game golden-pack
run is Task 36; loading through the shipped adapter here is the contract
layer of "load it in Color Duel".
"""
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.app import create_app
from studio.curves import point_in_rings_rule

H = {'X-Studio-Request': '1'}

ROOT = Path(__file__).resolve().parent.parent
ADAPTER_CHECK = ROOT / 'scripts' / 'adapter-contract-check.mjs'

# Deterministic import master: a curved closed blob (the Bézier-edit target,
# same geometry family as the Task 33 fixture), a plain rect at the bottom,
# and empty top-right canvas where the artwork Pen shape lands OVER the
# blob's right lobe — a real overlap whose tap ownership must follow the pen.
COMPOSITION_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<g data-cd-object="obj-blob" data-cd-name="Blob">
  <path d="M 50,100 C 50,60 120,40 160,70 C 200,100 200,160 160,190 C 120,220 50,190 50,100 Z" fill="#77AA55"/>
</g>
<g data-cd-object="obj-plain" data-cd-name="Plain">
  <rect x="40" y="230" width="220" height="60" fill="#CC8844"/>
</g>
</svg>'''

# The Bézier-handle edit: same closed ring family, first anchor moved,
# curve commands preserved (never flattened).
EDITED_BLOB_D = ('M 10,170 C 30,60 130,30 180,80 '
                 'C 230,130 270,180 200,230 C 130,280 30,240 10,170 Z')

BUILD_BODY = {'target_regions': 30, 'palette_colors': 4, 'paint_colors': 16,
              'max_edge': 300, 'min_region_pixels': 4, 'min_label_radius': 1.0,
              'auto_subdivide': False}

PEN_D = 'M 150,70 L 220,70 L 220,150 L 150,150 Z'
PEN_FILL = '#FF7348'
RECOLOR_FILL = '#4C7FE8'
# Probe points: (180,110) inside the pen rect AND inside the blob's right
# lobe (before AND after the Bézier edit) — a true overlap whose tap
# ownership must follow the pixel order. (60,120) inside the blob only.
PROBE_OVERLAP = (180, 110)
PROBE_BLOB = (60, 120)


def new(c):
    return c.post('/api/projects', json={'title': 'composition'}, headers=H).json()['id']


def wait(c, pid, timeout=60):
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = c.get(f'/api/projects/{pid}', headers=H).json()
        if last['job']['status'] in ('done', 'failed', 'canceled', 'interrupted'):
            return last
        time.sleep(0.05)
    return last


def step(c, pid, rev, body):
    """Run one edit/objects action to its terminal state and return the NEW
    revision (composition steps are sequential — each builds on the last)."""
    r = c.post(f'/api/projects/{pid}/edit', headers=H, json=body)
    assert r.status_code == 200, r.text
    p = wait(c, pid)
    assert p['job']['status'] == 'done', p['job']
    assert p['currentRevision'] != rev, 'edit published no new revision'
    return p['currentRevision']


def files(c, pid, rev):
    base = f'/api/projects/{pid}/revisions/{rev}/files'
    return {
        'manifest': c.get(base + '/artwork.json').json(),
        'regions': c.get(base + '/regions.json').json()['regions'],
        'palette': c.get(base + '/palette.json').json(),
        'paint': c.get(base + '/paint.json').json(),
        'objects': c.get(base + '/objects.json').json()['objects'],
        'qa': c.get(base + '/validation.json').json(),
        'master': c.get(base + '/source-master.svg').text,
    }


def owning_regions(regions, x, y):
    return [r for r in regions
            if point_in_rings_rule(r.get('flat', {}).get('rings') or r.get('rings'),
                                   x, y, r.get('fillRule', 'evenodd'))]


def assert_qa(data, where):
    assert data['qa']['passed'], f'{where}: QA failed: {data["qa"]["errors"]} {data["qa"]["warnings"]}'


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        yield c


def test_authoring_composition_full_chain(client):
    # ---- 1. Import a fixed SVG + Build (real compiler, no AI anywhere) ----
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('composition.svg', COMPOSITION_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    rev1 = p['currentRevision']
    d1 = files(client, pid, rev1)
    assert_qa(d1, 'import+build')
    blob_shape = next(o for o in d1['objects'] if o['id'] == 'obj-blob')['shapeIds'][0]
    plain_shape = next(o for o in d1['objects'] if o['id'] == 'obj-plain')['shapeIds'][0]
    z = {e['shapeId']: e['z'] for e in d1['paint']['paths']}
    assert z[blob_shape] < z[plain_shape], 'document order must place blob below plain'
    # tap ownership before the pen: the blob owns the future-overlap point
    assert [r0['objectId'] for r0 in owning_regions(d1['regions'], *PROBE_OVERLAP)] == ['obj-blob']

    # ---- 2. Add an artwork Pen shape over the blob's right lobe ----------
    rev2 = step(client, pid, rev1, {
        'base_revision': rev1, 'action': 'draw', 'region_ids': [], 'd': PEN_D,
        'palette_id': 1, 'paint': True, 'color': PEN_FILL, 'stroke_width': 1.2})
    d2 = files(client, pid, rev2)
    assert_qa(d2, 'pen')
    pen_region = next(r0 for r0 in d2['regions'] if r0['id'].startswith('r-p-'))
    pen_shape = pen_region['masterShapeId']
    assert pen_shape and pen_shape.startswith('sp-'), 'pen shape must carry a stable pipeline id'
    pen_entry = next(e for e in d2['paint']['paths'] if e['shapeId'] == pen_shape)
    assert pen_entry['fill'] == PEN_FILL
    pen_object = pen_region['objectId']
    # overlap ownership follows the pixels: the pen (drawn on top) now owns
    # the probe; the blob underneath was carved so exactly ONE region hits.
    hits = owning_regions(d2['regions'], *PROBE_OVERLAP)
    assert [h['objectId'] for h in hits] == [pen_object], \
        f'overlap probe owned by {[h["id"] for h in hits]} after pen draw'

    # ---- 3. Recolor the pen shape -----------------------------------------
    rev3 = step(client, pid, rev2, {
        'base_revision': rev2, 'action': 'recolor', 'color': RECOLOR_FILL,
        'region_ids': [pen_region['id']]})
    d3 = files(client, pid, rev3)
    assert_qa(d3, 'recolor')
    entry3 = next(e for e in d3['paint']['paths'] if e['shapeId'] == pen_shape)
    assert entry3['fill'] == RECOLOR_FILL, 'recolor must reach the visible paint'
    assert RECOLOR_FILL in d3['master'], 'recolor must be folded into the authoritative source at publication'
    # composition: the pen's region + ownership survived the recolor
    pen3 = next(r0 for r0 in d3['regions'] if r0['id'] == pen_region['id'])
    assert pen3['masterShapeId'] == pen_shape and pen3['objectId'] == pen_object

    # ---- 4. Change layer order (bring the blob to front) ------------------
    r = client.post(f'/api/projects/{pid}/objects', headers=H, json={
        'base_revision': rev3, 'object_id': 'obj-blob', 'order_action': 'bring_to_front'})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    rev4 = p['currentRevision']
    d4 = files(client, pid, rev4)
    assert_qa(d4, 'layer order')
    z4 = {e['shapeId']: e['z'] for e in d4['paint']['paths']}
    assert z4[blob_shape] > z4[plain_shape], 'bring_to_front must flip the visual z order'
    assert z4[blob_shape] > z4[pen_shape], 'bring_to_front lifts the blob above EVERYTHING, pen included'
    # The overlap's tap ownership follows the PIXELS through the reorder
    # (the Task 32 round-2 contract, now inside the composition chain): the
    # blob's visible surface re-claims the probe the pen used to own.
    hits4 = owning_regions(d4['regions'], *PROBE_OVERLAP)
    assert [h['objectId'] for h in hits4] == ['obj-blob'],         f'overlap ownership must flip with the layer order, got {[h["id"] for h in hits4]}'
    # composition through the reorder recompile: recolor + pen identity intact
    entry4 = next(e for e in d4['paint']['paths'] if e['shapeId'] == pen_shape)
    assert entry4['fill'] == RECOLOR_FILL
    assert next(o for o in d4['objects'] if o['id'] == 'obj-blob')['shapeIds'] == [blob_shape]

    # ---- 5. Edit a Bézier handle of the curved source shape ---------------
    rev5 = step(client, pid, rev4, {
        'base_revision': rev4, 'action': 'shape', 'region_ids': [],
        'shape_id': blob_shape, 'd': EDITED_BLOB_D})
    d5 = files(client, pid, rev5)
    assert_qa(d5, 'shape edit')
    entry5 = next(e for e in d5['paint']['paths'] if e['shapeId'] == blob_shape)
    assert entry5['d'] == EDITED_BLOB_D, 'the edited outline must be the paint geometry'
    assert ' C ' in entry5['d'], 'curve commands must survive the edit (never flattened)'
    assert next(o for o in d5['objects'] if o['id'] == 'obj-blob')['shapeIds'] == [blob_shape]
    # composition: recolor + z order + pen surface all survive the recompile.
    # The pen region is found by its stable masterShapeId — region ids are
    # re-derived by recompiles, shape identity is not.
    entry5_pen = next(e for e in d5['paint']['paths'] if e['shapeId'] == pen_shape)
    assert entry5_pen['fill'] == RECOLOR_FILL
    z5 = {e['shapeId']: e['z'] for e in d5['paint']['paths']}
    assert z5[blob_shape] > z5[plain_shape]
    pen5 = next(r0 for r0 in d5['regions'] if r0.get('masterShapeId') == pen_shape)
    assert pen5['objectId'] == pen_object
    # the enlarged blob keeps the overlap it won by layer order
    hits5 = owning_regions(d5['regions'], *PROBE_OVERLAP)
    assert [h['objectId'] for h in hits5] == ['obj-blob']
    assert [h['objectId'] for h in owning_regions(d5['regions'], *PROBE_BLOB)] == ['obj-blob']

    # ---- 6. Ordinary Build (the reload-equivalent recompile) --------------
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    rev6 = p['currentRevision']
    d6 = files(client, pid, rev6)
    assert_qa(d6, 'ordinary build')
    entry6 = next(e for e in d6['paint']['paths'] if e['shapeId'] == blob_shape)
    assert entry6['d'] == EDITED_BLOB_D, 'Bézier edit lost by an ordinary Build'
    entry6_pen = next(e for e in d6['paint']['paths'] if e['shapeId'] == pen_shape)
    assert entry6_pen['fill'] == RECOLOR_FILL, 'pen recolor lost by an ordinary Build'
    z6 = {e['shapeId']: e['z'] for e in d6['paint']['paths']}
    assert z6[blob_shape] > z6[plain_shape], 'layer order lost by an ordinary Build'
    pen6 = next(r0 for r0 in d6['regions'] if r0.get('masterShapeId') == pen_shape)
    assert pen6['objectId'] == pen_object, 'pen ownership lost by an ordinary Build'
    hits6 = owning_regions(d6['regions'], *PROBE_OVERLAP)
    assert [h['objectId'] for h in hits6] == ['obj-blob'], 'overlap ownership lost by Build'
    assert d6['manifest']['regionCount'] == len(d6['regions'])

    # ---- 7. Export the artwork pack + answer-key consistency --------------
    export = client.get(f'/api/projects/{pid}/revisions/{rev6}/export')
    assert export.status_code == 200 and export.content[:2] == b'PK'
    buf = io.BytesIO(export.content)
    with zipfile.ZipFile(buf) as zf:
        names = set(zf.namelist())
        for required in ('artworks', 'artwork.json', 'regions.json', 'palette.json'):
            assert any(required in n for n in names), f'export missing {required}'
        # answer key: every region maps to a real palette group, counts match
        runtime = json.loads(zf.read(next(n for n in names if n.endswith('/artwork.json'))))
        regions = json.loads(zf.read(next(n for n in names if n.endswith('/regions.json'))))['regions']
        palette = json.loads(zf.read(next(n for n in names if n.endswith('/palette.json'))))
        assert runtime['regionCount'] == len(regions)
        palette_ids = {e['id'] for e in palette}
        assert all(r0['paletteId'] in palette_ids for r0 in regions)
        # tap ownership inside the exported pack (the game's answer key):
        # the reordered blob owns the overlap, exactly like the studio board.
        # Lean runtime regions intentionally carry only `d` (no authoring
        # rings) — hit-test through the same path flattening the game uses.
        from studio.pipeline import flatten_d
        lean_hits = [r0 for r0 in regions
                     if point_in_rings_rule(flatten_d(r0['d']), *PROBE_OVERLAP, r0.get('fillRule', 'evenodd'))]
        assert [h['objectId'] for h in lean_hits] == ['obj-blob']

    # ---- 8. Load the exported pack in the shipped Color Duel adapter -------
    import shutil
    import subprocess
    import sys
    import tempfile
    if shutil.which('bun') is None:
        pytest.skip('bun runtime unavailable')
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
            zf.extractall(tmp)
        folder = Path(tmp) / 'artworks'
        folders = [p for p in folder.iterdir() if p.is_dir()]
        assert folders, 'export has no artworks/<id> bundle folder'
        proc = subprocess.run([sys.executable and 'bun', str(ADAPTER_CHECK),
                               *(str(f) for f in folders)],
                              capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        assert proc.returncode == 0, f'shipped adapter rejected the composed export:\n{proc.stdout}\n{proc.stderr}'
        assert 'FAIL' not in proc.stdout
