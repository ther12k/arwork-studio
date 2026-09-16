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
    # (Task 40C review: the pen drew an r-p-* gameplay region, so the reorder
    # now hits the manual-topology gate — the chain confirms the rebuild
    # explicitly, exactly like the UI dialog would.)
    r = client.post(f'/api/projects/{pid}/objects', headers=H, json={
        'base_revision': rev3, 'object_id': 'obj-blob', 'order_action': 'bring_to_front',
        'confirm_topology_rebuild': True})
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


# ---------------------------------------------------------- Task 39: ink strokes

INK_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<g data-cd-object="obj-blob" data-cd-name="Blob">
  <path d="M 50,100 C 50,60 120,40 160,70 C 200,100 200,160 160,190 C 120,220 50,190 50,100 Z" fill="#77AA55"/>
</g>
<g data-cd-object="obj-plain" data-cd-name="Plain">
  <rect x="40" y="230" width="220" height="60" fill="#CC8844"/>
</g>
<path d="M 40,40 C 90,10 150,90 200,50 C 230,30 260,60 280,40"
      fill="none" stroke="#1B4F8A" stroke-width="3"/>
<path d="M 20,140 L 60,140 L 60,170 L 20,170 Z"
      fill="none" stroke="#1B4F8A" stroke-width="2"/>
</svg>'''

INK_EDIT_D = 'M 30,60 C 80,20 150,90 200,50 C 230,30 260,60 285,70'


def test_ink_stroke_edit(client):
    """Open linework is editable in Art node mode: the stroke's d changes,
    identity/ownership survive, and the FILLED artwork is untouched — the
    recompile's determinism is the guarantee (paint bytes + region set are
    identical). Closed shapes still refuse open d."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('ink.svg', INK_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev = wait(client, pid)['currentRevision']

    before = files(client, pid, rev)
    ink = [p for p in before['paint']['inkPaths']]
    assert len(ink) == 2  # open squiggle + closed outline stroke
    ink_id = next(p_['shapeId'] for p_ in ink if p_['d'] == 'M 40,40 C 90,10 150,90 200,50 C 230,30 260,60 280,40')
    closed_ink_id = next(p_['shapeId'] for p_ in ink if p_['d'].rstrip().upper().endswith('Z'))
    filled_before = json.dumps(before['paint']['paths'], sort_keys=True)
    regions_before = sorted(r_['id'] for r_ in before['regions'])

    rev2 = step(client, pid, rev, {
        'action': 'shape', 'shape_id': ink_id, 'region_ids': [], 'd': INK_EDIT_D,
        'base_revision': rev})
    after = files(client, pid, rev2)
    ink2 = after['paint']['inkPaths']
    assert len(ink2) == 2
    edited_ink = next(p_ for p_ in ink2 if p_['shapeId'] == ink_id)
    assert edited_ink['d'] == INK_EDIT_D
    # The other ink stroke is untouched (per-shape edit, whole recompile).
    assert next(p_ for p_ in ink2 if p_['shapeId'] == closed_ink_id)['d'].endswith('Z')
    assert not INK_EDIT_D.rstrip().upper().endswith('Z'), 'the edited stroke is open'
    # Determinism guarantee: the filled paint bytes and the gameplay surface
    # set are IDENTICAL — an ink edit never re-derives gameplay.
    assert json.dumps(after['paint']['paths'], sort_keys=True) == filled_before
    assert sorted(r_['id'] for r_ in after['regions']) == regions_before
    assert after['objects'] == before['objects']
    # P2 contract: a CLOSED ink outline keeps the closed contract — an open d
    # for it fails the job ("must stay closed"), a closed d publishes.
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'action': 'shape', 'shape_id': closed_ink_id, 'region_ids': [],
        'd': 'M 20,140 L 60,140 L 60,170', 'base_revision': rev2})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'must stay closed' in p['job'].get('message', '')
    rev_c = p['currentRevision']
    rev_d = step(client, pid, rev_c, {
        'action': 'shape', 'shape_id': closed_ink_id, 'region_ids': [],
        'd': 'M 15,135 L 65,140 L 60,170 L 20,170 Z', 'base_revision': rev_c})
    # The master carries the edit at publication (authoritative source).
    assert INK_EDIT_D in after['master']
    assert not after['qa'].get('errors')

    # Closed paint shapes still refuse OPEN geometry (existing contract).
    blob_shape = next(o for o in after['objects'] if o['id'] == 'obj-blob')['shapeIds'][0]
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'action': 'shape', 'shape_id': blob_shape, 'region_ids': [],
        'd': 'M 10,10 L 90,10 L 90,90', 'base_revision': rev_d})
    assert r.status_code == 200, r.text  # accepted as a job; failure below
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'must stay closed' in p['job'].get('message', '')

    # A well-formed but DEGENERATE ink stroke (zero length) is a job failure
    # with an actionable message; non-M garbage never reaches the job — the
    # request model rejects it at the HTTP boundary (422) by contract.
    rev3 = p['currentRevision']
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'action': 'shape', 'shape_id': ink_id, 'region_ids': [],
        'd': 'M 80,45 L 80,45', 'base_revision': rev3})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'collapses to nothing' in p['job'].get('message', '')

    # P1 regression — compound subpaths are validated PER SUBPATH, never
    # chained: two zero-length subpaths must NOT inherit phantom connector
    # length from being flattened into one polyline.
    rev4 = p['currentRevision']
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'action': 'shape', 'shape_id': ink_id, 'region_ids': [],
        'd': 'M 0,0 L 0,0 M 100,100 L 100,100', 'base_revision': rev4})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'collapses to nothing' in p['job'].get('message', '')
    # ...while one drawable subpath is enough even next to a collapsed one.
    rev5 = p['currentRevision']
    step(client, pid, rev5, {
        'action': 'shape', 'shape_id': ink_id, 'region_ids': [],
        'd': 'M 0,0 L 0,0 M 100,100 L 120,100', 'base_revision': rev5})


# ---------------------------------------------------------- Task 40A: ink style

STYLE_TEAL = '#29383E'


def test_ink_style_edit(client):
    """Task 40A — ink appearance is shape-addressed and topology-neutral:
    color/width/opacity land in paint.inkPaths AND the authoritative master
    in the SAME revision; regions.json and paint.paths are byte-identical
    across the edit; an ordinary Build preserves the style; opacity renders
    end-to-end (master → paint → exported colored.svg)."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('ink.svg', INK_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev = wait(client, pid)['currentRevision']
    before = files(client, pid, rev)
    ink_id = next(p_['shapeId'] for p_ in before['paint']['inkPaths']
                  if p_['d'].startswith('M 40,40'))
    regions_sig = json.dumps(sorted(before['regions'], key=lambda r_: r_['id']), sort_keys=True)
    paths_sig = json.dumps(before['paint']['paths'], sort_keys=True)

    # Restyle the open squiggle: teal, width 3, 50% opacity.
    rev2 = step(client, pid, rev, {
        'base_revision': rev, 'action': 'shape_style', 'shape_id': ink_id,
        'region_ids': [], 'stroke_color': STYLE_TEAL, 'stroke_width': 3.0,
        'opacity': 0.5})
    d2 = files(client, pid, rev2)
    # Topology-neutral invariants (the hard gate).
    assert json.dumps(sorted(d2['regions'], key=lambda r_: r_['id']), sort_keys=True) == regions_sig
    assert json.dumps(d2['paint']['paths'], sort_keys=True) == paths_sig
    assert d2['objects'] == before['objects']
    # paint.inkPaths carries the new appearance.
    ink2 = next(p_ for p_ in d2['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert ink2['fill'] == STYLE_TEAL and ink2['strokeWidth'] == 3.0
    assert ink2.get('opacity') == 0.5
    # The authoritative master carries the same style in the same revision
    # (deletion semantics check comes later via opacity=1 / width changes).
    assert f'stroke="{STYLE_TEAL}"' in d2['master'] or f'stroke="{STYLE_TEAL.lower()}"' in d2['master']
    assert not d2['qa'].get('errors')
    # Opacity renders into the exported colored.svg (serializer chain).
    colored = client.get(f'/api/projects/{pid}/revisions/{rev2}/files/colored.svg', headers=H).text
    assert 'opacity="0.5"' in colored, 'ink opacity missing from the exported colored.svg'

    # Ordinary Build preserves the restyled ink (source is authoritative).
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    d3 = files(client, pid, p['currentRevision'])
    ink3 = next(p_ for p_ in d3['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert ink3['fill'] == STYLE_TEAL and ink3['strokeWidth'] == 3.0
    assert ink3.get('opacity') == 0.5, 'opacity resurrected/cleared by an ordinary Build'

    # Deletion semantics: opacity >= 1 REMOVES the attribute everywhere.
    rev4 = step(client, pid, p['currentRevision'], {
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': ink_id, 'region_ids': [], 'opacity': 1.0})
    d4 = files(client, pid, rev4)
    ink4 = next(p_ for p_ in d4['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert 'opacity' not in ink4
    assert f'stroke-width="3"' in d4['master'] or 'stroke-width="3.0"' in d4['master']
    colored4 = client.get(f'/api/projects/{pid}/revisions/{rev4}/files/colored.svg', headers=H).text
    ink_frag4 = [seg for seg in colored4.split('<path ') if f'stroke="{STYLE_TEAL}"' in seg]
    assert ink_frag4 and all('opacity=' not in seg.split('>')[0] for seg in ink_frag4), \
        'opacity attribute survived the opacity=1 edit'

    # Width change on a stroke master persists too (renderer-visible).
    rev5 = step(client, pid, rev4, {
        'base_revision': rev4, 'action': 'shape_style', 'shape_id': ink_id,
        'region_ids': [], 'stroke_width': 2.0})
    d5 = files(client, pid, rev5)
    ink5 = next(p_ for p_ in d5['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert ink5['strokeWidth'] == 2.0

    # Guardrails: an unknown shape id fails cleanly; an empty style fails
    # cleanly. (A FILLED target + stroke_color is no longer a guardrail —
    # Task 40B makes that the outline edit; covered in test_filled_style_edit.)
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': rev5, 'action': 'shape_style', 'shape_id': 's9999',
        'region_ids': [], 'stroke_color': STYLE_TEAL})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'not a paintable shape' in p['job'].get('message', '')
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': ink_id, 'region_ids': []})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'Nothing to restyle' in p['job'].get('message', '')


def test_ink_width_floor(client):
    """P2 contract: ink stroke width floor is 0.4 (the compiler normalizes
    smaller widths up, so sub-0.4 requests would be a silent lie); 0 is NOT
    'remove stroke' for ink (opacity 0 is the non-destructive hide); 0.4
    survives an ordinary Build."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('ink.svg', INK_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev = wait(client, pid)['currentRevision']
    d0 = files(client, pid, rev)
    ink_id = next(p_['shapeId'] for p_ in d0['paint']['inkPaths']
                  if p_['d'].startswith('M 40,40'))

    # 0.1: below the compiler floor — rejected with guidance.
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': rev, 'action': 'shape_style', 'shape_id': ink_id,
        'region_ids': [], 'stroke_width': 0.1})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert '0.4' in p['job'].get('message', '')

    # 0.4: the floor — accepted and stable across an ordinary Build.
    rev2 = step(client, pid, rev, {
        'base_revision': rev, 'action': 'shape_style', 'shape_id': ink_id,
        'region_ids': [], 'stroke_width': 0.4})
    d2 = files(client, pid, rev2)
    ink2 = next(p_ for p_ in d2['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert ink2['strokeWidth'] == 0.4
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    d3 = files(client, pid, p['currentRevision'])
    ink3 = next(p_ for p_ in d3['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert ink3['strokeWidth'] == 0.4, 'floor width not preserved by Build'

    # Opacity 0: the sanctioned hide — still a stroke, still pickable.
    rev4 = step(client, pid, p['currentRevision'], {
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': ink_id, 'region_ids': [], 'opacity': 0.0})
    d4 = files(client, pid, rev4)
    ink4 = next(p_ for p_ in d4['paint']['inkPaths'] if p_['shapeId'] == ink_id)
    assert ink4.get('opacity') == 0.0 and ink4['strokeWidth'] == 0.4
    assert d4['master'].count(f'id="{ink_id}"') == 1  # still present in source


# ---------------------------------------------------------- Task 40B: filled style

FILLED_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<defs>
  <linearGradient id="sunset" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#FFD27D"/>
    <stop offset="1" stop-color="#C4503A"/>
  </linearGradient>
</defs>
<g data-cd-object="obj-blob" data-cd-name="Blob">
  <path d="M 50,100 C 50,60 120,40 160,70 C 200,100 200,160 160,190 C 120,220 50,190 50,100 Z" fill="url(#sunset)"/>
</g>
<g data-cd-object="obj-plain" data-cd-name="Plain">
  <rect x="40" y="230" width="220" height="60" fill="#CC8844"/>
</g>
</svg>'''

ORANGE = '#E8760C'
OUTLINE_BLUE = '#1B4F8A'
SOLID_GREEN = '#3FA34D'


def _rgb(hx):
    hx = hx.lstrip('#')
    return tuple(int(hx[k:k + 2], 16) for k in (0, 2, 4))


def test_filled_style_edit(client):
    """Task 40B — filled-shape appearance on the mature recolor semantics,
    shape-addressed. Fill (+ preserve-shading gradient tint) moves EVERY
    region of the shape to the palette group whose answer color IS the new
    fill (reuse-or-create, never a shared-swatch mutation) while region
    GEOMETRY stays identical. Outlines are pure appearance: width 0 REMOVES
    one, and outline-only edits keep regions.json, palette.json and the
    answer key byte-identical. An ordinary Build (×2) preserves the tint,
    the outline and the removal."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('filled.svg', FILLED_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev = wait(client, pid)['currentRevision']
    d0 = files(client, pid, rev)
    assert_qa(d0, 'import+build')
    blob = next(o for o in d0['objects'] if o['id'] == 'obj-blob')['shapeIds'][0]
    plain = next(o for o in d0['objects'] if o['id'] == 'obj-plain')['shapeIds'][0]
    blob_regions = {r_['id'] for r_ in d0['regions'] if r_.get('masterShapeId') == blob}
    plain_regions = {r_['id'] for r_ in d0['regions'] if r_.get('masterShapeId') == plain}
    geom_before = {r_['id']: r_['d'] for r_ in d0['regions']}
    pid_before = {r_['id']: r_['paletteId'] for r_ in d0['regions']}
    stops_before = [s['color'] for s in d0['paint']['gradients'][0]['stops']]

    # ---- 1. Fill with preserve_shading: the gradient TINTS toward orange,
    # keeps its stop structure, and every blob region moves to the orange
    # answer group. Region geometry (d bytes) is untouched.
    rev1 = step(client, pid, rev, {
        'base_revision': rev, 'action': 'shape_style', 'shape_id': blob,
        'region_ids': [], 'color': ORANGE, 'preserve_shading': True})
    d1 = files(client, pid, rev1)
    assert_qa(d1, 'fill+preserve')
    entry = next(e for e in d1['paint']['paths'] if e['shapeId'] == blob)
    assert entry['fill'].startswith('url(#'), 'preserve_shading must keep the gradient'
    grad = next(g for g in d1['paint']['gradients'] if g['id'] == entry['fill'][5:-1])
    stops1 = [s['color'] for s in grad['stops']]
    assert len(stops1) == len(stops_before), 'stop structure must survive the tint'
    assert stops1 != stops_before, 'the tint must actually shift the stops'
    avg = [sum(c[k] for c in map(_rgb, stops1)) / len(stops1) for k in range(3)]
    # tolerance 4: the per-channel ratio tint can CLAMP at 0/255 on individual
    # stops, which pulls the realized average slightly off the exact target.
    assert all(abs(a - b) <= 4 for a, b in zip(avg, _rgb(ORANGE))), \
        f'tint must pull the stop average onto the target, got {stops1}'
    orange_group = next(e for e in d1['palette'] if e['hex'].upper() == ORANGE)
    pid_after = {r_['id']: r_['paletteId'] for r_ in d1['regions']}
    assert all(pid_after[rid] == orange_group['id'] for rid in blob_regions), \
        'every region of the shape must follow the new answer color'
    assert all(pid_after[rid] == pid_before[rid] for rid in plain_regions), \
        'unrelated regions keep their answer color (palette identity)'
    assert {r_['id']: r_['d'] for r_ in d1['regions']} == geom_before, \
        'fill edits must not touch region geometry'
    # the tint is folded into the authoritative master's stops (by ref)
    assert all(c in d1['master'] for c in stops1)
    # existing palette groups were not mutated (reuse-or-create only)
    for e in d1['palette']:
        old = next((o for o in d0['palette'] if o['id'] == e['id']), None)
        if old is not None:
            assert old['hex'] == e['hex'] and old['name'] == e['name']

    # ---- 2. Outline add: pure appearance — regions.json, palette.json and
    # the answer key are byte-identical.
    regions1 = sorted(d1['regions'], key=lambda r_: r_['id'])
    rev2 = step(client, pid, rev1, {
        'base_revision': rev1, 'action': 'shape_style', 'shape_id': blob,
        'region_ids': [], 'stroke_color': OUTLINE_BLUE, 'stroke_width': 2.0})
    d2 = files(client, pid, rev2)
    assert_qa(d2, 'outline add')
    entry2 = next(e for e in d2['paint']['paths'] if e['shapeId'] == blob)
    assert entry2['stroke'] == OUTLINE_BLUE and entry2['strokeWidth'] == 2.0
    assert sorted(d2['regions'], key=lambda r_: r_['id']) == regions1, \
        'outline edits must not touch regions.json'
    assert d2['palette'] == d1['palette'], 'outline edits must not touch palette.json'
    assert f'stroke="{OUTLINE_BLUE}"' in d2['master']
    assert 'stroke-width="2.0"' in d2['master'] or 'stroke-width="2"' in d2['master']
    colored2 = client.get(f'/api/projects/{pid}/revisions/{rev2}/files/colored.svg', headers=H).text
    assert f'stroke="{OUTLINE_BLUE}"' in colored2, 'outline missing from the exported colored.svg'

    # ---- 3. Ordinary Build ×2 preserves the tint AND the outline; palette
    # re-derives from the (tinted) master fills — the documented contract.
    for _ in range(2):
        r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
        assert r.status_code == 200, r.text
        p = wait(client, pid)
        assert p['job']['status'] == 'done', p['job']
    d3 = files(client, pid, p['currentRevision'])
    assert_qa(d3, 'build x2')
    entry3 = next(e for e in d3['paint']['paths'] if e['shapeId'] == blob)
    assert entry3['fill'].startswith('url(#'), 'gradient flattening lost by Build'
    grad3 = next(g for g in d3['paint']['gradients'] if g['id'] == entry3['fill'][5:-1])
    stops3 = [s['color'] for s in grad3['stops']]
    assert stops3 == stops1, 'gradient tint drifted across Build ×2'
    assert entry3.get('stroke') == OUTLINE_BLUE and entry3.get('strokeWidth') == 2.0, \
        'outline lost by an ordinary Build'
    pid3 = {r_['id']: r_['paletteId'] for r_ in d3['regions']}
    mid = stops3[len(stops3) // 2]  # the importer's representative fill
    assert len({pid3[rid] for rid in blob_regions}) == 1, 'the shape\'s regions stay one group'
    by_hex = {e['hex'].upper(): e['id'] for e in d3['palette']}
    assert pid3[next(iter(blob_regions))] == by_hex[mid.upper()], \
        'Build must re-derive the blob group from the tinted representative fill'

    # ---- 4. Outline removal: width 0 — real deletion semantics in paint AND
    # master; an ordinary Build must not resurrect it.
    rev4 = step(client, pid, p['currentRevision'], {
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': blob, 'region_ids': [], 'stroke_width': 0.0})
    d4 = files(client, pid, rev4)
    entry4 = next(e for e in d4['paint']['paths'] if e['shapeId'] == blob)
    assert 'stroke' not in entry4 and 'strokeWidth' not in entry4
    assert f'stroke="{OUTLINE_BLUE}"' not in d4['master']
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    d5 = files(client, pid, p['currentRevision'])
    entry5 = next(e for e in d5['paint']['paths'] if e['shapeId'] == blob)
    assert 'stroke' not in entry5, 'removed outline resurrected by Build'

    # ---- 5. Solid fill replace (preserve_shading=False on a gradient is a
    # deliberate flatten; here the plain shape is already solid) and the
    # idempotence guard: requesting the CURRENT fill must not move palette
    # groups even on manually reassigned regions.
    rev6 = step(client, pid, p['currentRevision'], {
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': plain, 'region_ids': [], 'color': SOLID_GREEN})
    d6 = files(client, pid, rev6)
    entry6 = next(e for e in d6['paint']['paths'] if e['shapeId'] == plain)
    assert entry6['fill'] == SOLID_GREEN
    green_group = next(e for e in d6['palette'] if e['hex'].upper() == SOLID_GREEN)
    pid6 = {r_['id']: r_['paletteId'] for r_ in d6['regions']}
    assert all(pid6[rid] == green_group['id'] for rid in plain_regions)
    # manual palette reassignment (number group ≠ appearance), then an
    # outline-only edit carrying the unchanged fill color: the manual group
    # assignment must survive.
    manual = next(e for e in d6['palette'] if e['id'] != green_group['id'])
    rev7 = step(client, pid, rev6, {
        'base_revision': rev6, 'action': 'palette',
        'region_ids': [sorted(plain_regions)[0]], 'palette_id': manual['id']})
    d7 = files(client, pid, rev7)
    first_plain = sorted(plain_regions)[0]
    assert next(r_ for r_ in d7['regions'] if r_['id'] == first_plain)['paletteId'] == manual['id']
    rev8 = step(client, pid, rev7, {
        'base_revision': rev7, 'action': 'shape_style', 'shape_id': plain,
        'region_ids': [], 'color': SOLID_GREEN, 'stroke_width': 1.0})
    d8 = files(client, pid, rev8)
    assert next(r_ for r_ in d8['regions'] if r_['id'] == first_plain)['paletteId'] == manual['id'], \
        'an unchanged fill must be a no-op for palette groups (outline-only invariant)'

    # ---- 6. Guardrails: opacity is not available for filled shapes; nonzero
    # outline widths start at 0.4; a region selection is rejected.
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': rev8, 'action': 'shape_style', 'shape_id': plain,
        'region_ids': [], 'opacity': 0.5})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'not available for filled shapes' in p['job'].get('message', '')
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': plain, 'region_ids': [], 'stroke_width': 0.2})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert '0.4' in p['job'].get('message', '')
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': p['currentRevision'], 'action': 'shape_style',
        'shape_id': plain, 'region_ids': [first_plain], 'stroke_width': 1.0})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'no region selection' in p['job'].get('message', '')


# ---------------------------------------------------------- Task 40C: shape order

ORDER_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<g data-cd-object="obj-back" data-cd-name="Back">
  <path d="M 40,40 L 200,40 L 200,200 L 40,200 Z" fill="#AA3377"/>
</g>
<g data-cd-object="obj-front" data-cd-name="Front">
  <path d="M 120,120 L 260,120 L 260,260 L 120,260 Z" fill="#33AA77"/>
  <path d="M 150,60 C 200,20 290,60 290,140 C 290,220 240,260 170,250 C 120,240 110,180 130,150 C 140,130 145,80 150,60 Z" fill="#3333AA"/>
</g>
</svg>'''

PROBE_STACK = (230, 200)  # inside the green rect AND the blue blob (not the purple rect)


def test_shape_order_edit(client):
    """Task 40C — shape-level layer order, one document-order position per
    move, FULL RECOMPILE (never a paint.z bump): the visible partition
    re-derives honestly (overlapping pixels change ownership), shapeIds and
    object ownership are preserved, group-edge moves are refused with
    guidance toward the whole-object controls, and an ordinary Build
    preserves the new order. Ink strokes reorder the same way."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('order.svg', ORDER_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev = wait(client, pid)['currentRevision']
    d0 = files(client, pid, rev)
    assert_qa(d0, 'import+build')
    objs = {o['id']: o for o in d0['objects']}
    back = next(s for s in objs['obj-back']['shapeIds'])
    green = next(s for s in objs['obj-front']['shapeIds']
                 if any(e.get('fill') == '#33AA77' and e.get('shapeId') == s
                        for e in d0['paint']['paths']))
    blue = next(s for s in objs['obj-front']['shapeIds'] if s != green)
    z0 = {e['shapeId']: e['z'] for e in d0['paint']['paths']}
    assert z0[back] < z0[green] < z0[blue], 'document order must be back < green < blue'
    # the blob (blue, last in its group) owns the overlapping probe pixel
    hits0 = owning_regions(d0['regions'], *PROBE_STACK)
    assert [h['masterShapeId'] for h in hits0] == [blue], \
        f'probe must be owned by the top shape, got {[h["masterShapeId"] for h in hits0]}'

    # blue backward: now BEHIND green in the same group — the recompile
    # re-derives the partition so green owns the overlap.
    rev2 = step(client, pid, rev, {
        'base_revision': rev, 'action': 'shape_order', 'region_ids': [],
        'shape_id': blue, 'order': 'backward'})
    d2 = files(client, pid, rev2)
    assert_qa(d2, 'shape order backward')
    z2 = {e['shapeId']: e['z'] for e in d2['paint']['paths']}
    assert z2[blue] < z2[green], 'backward must swap the sibling order'
    assert z2[back] == z0[back]
    hits2 = owning_regions(d2['regions'], *PROBE_STACK)
    assert [h['masterShapeId'] for h in hits2] == [green], \
        'the overlap ownership must follow the new paint order'
    # ownership gate: shapeIds per object are IDENTICAL (a sibling swap can
    # never re-parent a shape into another object).
    assert d2['objects'] == d0['objects']
    region_owner = {r_['masterShapeId']: r_['objectId'] for r_ in d2['regions'] if r_.get('masterShapeId')}
    assert region_owner.get(green) == 'obj-front' and region_owner.get(back) == 'obj-back'
    # the blue blob keeps a visible crescent (partial overlap) — still playable
    assert any(r_.get('masterShapeId') == blue for r_ in d2['regions']), \
        'a partially covered shape must keep its visible surface'

    # edge refusal: blue is now FIRST in its group — another backward is
    # refused with guidance toward the whole-object controls.
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': rev2, 'action': 'shape_order', 'region_ids': [],
        'shape_id': blue, 'order': 'backward'})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'already at the back of its object group' in p['job'].get('message', '')

    # an ordinary Build from the reordered revision preserves the new order.
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    d3 = files(client, pid, p['currentRevision'])
    assert_qa(d3, 'build after reorder')
    z3 = {e['shapeId']: e['z'] for e in d3['paint']['paths']}
    assert z3[blue] < z3[green], 'the reordered master is authoritative — Build must not revert'
    hits3 = owning_regions(d3['regions'], *PROBE_STACK)
    assert [h['masterShapeId'] for h in hits3] == [green]

    # blue forward: back to the original stack.
    rev4 = step(client, pid, p['currentRevision'], {
        'base_revision': p['currentRevision'], 'action': 'shape_order', 'region_ids': [],
        'shape_id': blue, 'order': 'forward'})
    d4 = files(client, pid, rev4)
    z4 = {e['shapeId']: e['z'] for e in d4['paint']['paths']}
    assert z4[blue] > z4[green]
    hits4 = owning_regions(d4['regions'], *PROBE_STACK)
    assert [h['masterShapeId'] for h in hits4] == [blue]

    # ---- reviewer's regression chain 1: manual gameplay topology gate -----
    # Cut a region by hand, then give it a custom label: the revision now
    # carries manual gameplay topology (provenance flag).
    back_regions = [r_ for r_ in d4['regions'] if r_.get('masterShapeId') == back]
    assert back_regions
    rev5 = step(client, pid, rev4, {
        'base_revision': rev4, 'action': 'cut', 'region_ids': [back_regions[0]['id']],
        'd': 'M 40,80 L 200,80'})
    d5 = files(client, pid, rev5)
    cut_region_ids = [r_['id'] for r_ in d5['regions'] if r_['id'].startswith('r-c-')]
    assert cut_region_ids
    assert d5['manifest']['provenance'].get('manualTopology') is True
    lower = next(r_ for r_ in d5['regions'] if owning_regions([r_], 100, 140))
    rev6 = step(client, pid, rev5, {
        'base_revision': rev5, 'action': 'label', 'region_ids': [lower['id']],
        'x': 100.0, 'y': 140.0})
    d6 = files(client, pid, rev6)
    assert d6['manifest']['provenance'].get('manualTopology') is True, \
        'a custom label keeps the manual topology flag'

    # A filled move REBUILDS the gameplay surfaces → rejected until the
    # explicit confirmation flag is sent (the UI dialog is never the only
    # guard), and the healthy revision is untouched.
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': rev6, 'action': 'shape_order', 'region_ids': [],
        'shape_id': blue, 'order': 'backward'})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'rebuilds the gameplay surfaces' in p['job'].get('message', '')
    assert p['currentRevision'] == rev6

    # The confirmed move publishes: the rebuild resets the manual topology
    # (the cut halves and the custom label are re-derived away), and the
    # PREVIOUS revision remains fully available in history.
    rev7 = step(client, pid, rev6, {
        'base_revision': rev6, 'action': 'shape_order', 'region_ids': [],
        'shape_id': blue, 'order': 'backward', 'confirm_topology_rebuild': True})
    d7 = files(client, pid, rev7)
    assert_qa(d7, 'confirmed topology rebuild')
    assert not [r_ for r_ in d7['regions'] if r_['id'].startswith('r-c-')], \
        'the confirmed rebuild re-derives the partition (manual cut halves gone)'
    assert not d7['manifest']['provenance'].get('manualTopology')
    assert any('previous revision remains available' in w for w in d7['qa'].get('warnings', []))
    d6_again = files(client, pid, rev6)
    assert [r_['id'] for r_ in d6_again['regions'] if r_['id'].startswith('r-c-')] == cut_region_ids, \
        'the pre-move revision must remain available with its manual topology'
    assert d7['manifest']['provenance']['shapeOrder']['topologyRebuilt'] is True

    # ---- ink fast path: NO recompile, gameplay bytes identical -----------
    # (Task 40C review) The squiggle's backward neighbor is the obj-plain
    # GROUP — a filled-shape move would be refused there, but an ink stroke
    # takes the topology-neutral fast path: root-level element move, z
    # re-ranked, gameplay untouched, no confirmation gate.
    pid2 = new(client)
    r = client.post(f'/api/projects/{pid2}/upload-svg', headers=H,
                    files={'file': ('ink.svg', INK_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid2}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev_i = wait(client, pid2)['currentRevision']
    di0 = files(client, pid2, rev_i)
    squiggle = next(p_['shapeId'] for p_ in di0['paint']['inkPaths'] if p_['d'].startswith('M 40,40'))
    closed = next(p_['shapeId'] for p_ in di0['paint']['inkPaths'] if p_['d'].rstrip().upper().endswith('Z'))
    zi0 = {p_['shapeId']: p_['z'] for p_ in di0['paint']['inkPaths']}
    assert zi0[closed] > zi0[squiggle]

    # reviewer's regression chain 2: Build → CUT region → move INK stroke →
    # regions.json identical, the manual cut still exists.
    objs_i = {o['id']: o for o in di0['objects']}
    plain_shape = objs_i['obj-plain']['shapeIds'][0]
    blob_shape_i = objs_i['obj-blob']['shapeIds'][0]
    plain_regions = sorted(r_['id'] for r_ in di0['regions'] if r_.get('masterShapeId') == plain_shape)
    rev_i1 = step(client, pid2, rev_i, {
        'base_revision': rev_i, 'action': 'cut', 'region_ids': [plain_regions[0]],
        'd': 'M 150,230 L 150,290'})
    di1 = files(client, pid2, rev_i1)
    cut_ids = [r_['id'] for r_ in di1['regions'] if r_['id'].startswith('r-c-')]
    assert cut_ids, 'the cut must create r-c-* regions'
    assert di1['manifest']['provenance'].get('manualTopology') is True, \
        'manual gameplay topology must be tracked in provenance'

    rev_i2 = step(client, pid2, rev_i1, {
        'base_revision': rev_i1, 'action': 'shape_order', 'region_ids': [],
        'shape_id': squiggle, 'order': 'backward'})
    di2 = files(client, pid2, rev_i2)
    # the fast-path invariants: gameplay carried over byte-identical.
    assert json.dumps(sorted(di2['regions'], key=lambda r_: r_['id']), sort_keys=True) == \
        json.dumps(sorted(di1['regions'], key=lambda r_: r_['id']), sort_keys=True), \
        'ink moves must not touch regions.json'
    assert di2['palette'] == di1['palette']
    assert di2['objects'] == di1['objects']
    assert [r_['id'] for r_ in di2['regions'] if r_['id'].startswith('r-c-')] == cut_ids, \
        'the manual cut must survive the ink move'
    assert di2['manifest']['provenance'].get('manualTopology') is True, \
        'the ink move keeps the manual topology (regions are untouched)'
    # the squiggle moved behind the obj-plain group's fill (root position
    # before the group): above the blob fill, below the plain fill, and the
    # closed outline stays topmost among ink strokes.
    zi2 = {p_['shapeId']: p_['z'] for p_ in di2['paint']['inkPaths']}
    zfills2 = {p_['shapeId']: p_['z'] for p_ in di2['paint']['paths']}
    assert zfills2[blob_shape_i] < zi2[squiggle] < zfills2[plain_shape], \
        'the stroke must sit between the two fills after the group-crossing move'
    assert zi2[closed] > zi2[squiggle]
    # the master carries the new root order (squiggle before the obj-plain group)
    assert di2['master'].index(f'id="{squiggle}"') < di2['master'].index('data-cd-object="obj-plain"')

    # closed backward moves one ROOT position: in front of the g-plain group
    # (its backward neighbor), i.e. behind the plain fill but still above the
    # squiggle. Forward restores the original stack.
    rev_i3 = step(client, pid2, rev_i2, {
        'base_revision': rev_i2, 'action': 'shape_order', 'region_ids': [],
        'shape_id': closed, 'order': 'backward'})
    di3 = files(client, pid2, rev_i3)
    zi3 = {p_['shapeId']: p_['z'] for p_ in di3['paint']['inkPaths']}
    zfills3 = {p_['shapeId']: p_['z'] for p_ in di3['paint']['paths']}
    assert zi3[squiggle] < zi3[closed] < zfills3[plain_shape], \
        'one root position = behind the neighbour group, still above the squiggle'
    rev_i4 = step(client, pid2, rev_i3, {
        'base_revision': rev_i3, 'action': 'shape_order', 'region_ids': [],
        'shape_id': closed, 'order': 'forward'})
    di4 = files(client, pid2, rev_i4)
    zi4 = {p_['shapeId']: p_['z'] for p_ in di4['paint']['inkPaths']}
    zfills4 = {p_['shapeId']: p_['z'] for p_ in di4['paint']['paths']}
    assert zi4[closed] > zi4[squiggle] and zi4[closed] > zfills4[plain_shape], \
        'forward must restore the original stack'
    assert json.dumps(sorted(di4['regions'], key=lambda r_: r_['id']), sort_keys=True) == \
        json.dumps(sorted(di1['regions'], key=lambda r_: r_['id']), sort_keys=True)

    # guardrails: missing shape and missing direction fail cleanly.
    r = client.post(f'/api/projects/{pid2}/edit', headers=H, json={
        'base_revision': rev_i4, 'action': 'shape_order', 'region_ids': [],
        'shape_id': 's9999', 'order': 'forward'})
    assert r.status_code == 200, r.text
    p = wait(client, pid2)
    assert p['job']['status'] == 'failed', p['job']
    assert 'not found in the source master' in p['job'].get('message', '')
    r = client.post(f'/api/projects/{pid2}/edit', headers=H, json={
        'base_revision': p['currentRevision'], 'action': 'shape_order',
        'region_ids': [], 'shape_id': closed})
    assert r.status_code == 200, r.text  # optional field: the JOB fails
    p = wait(client, pid2)
    assert p['job']['status'] == 'failed', p['job']
    assert 'Choose a move direction' in p['job'].get('message', '')


def test_topology_gate_generalization(client):
    """Task 40C review round 2 — the manual-topology confirmation gate is ONE
    policy across EVERY topology-rebuilding operation, backend-owned:

        manual work      operation                  expected
        cut + label      filled shape reorder       reject until confirm (covered elsewhere)
        cut + label      object layer reorder       reject until confirm
        cut + label      artwork shape geometry edit reject until confirm
        cut + label      ordinary Build             reject until confirm
        cut              ink reorder                no gate, regions byte-identical
        cut              ink/fill appearance edit   no gate, manual work remains

    A confirmed rebuild clears the flag honestly (the new revision carries no
    manual work) and the previous revision stays fully available."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('order.svg', ORDER_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev = wait(client, pid)['currentRevision']
    d0 = files(client, pid, rev)
    # helper over the CURRENT files each time (region ids re-derive)
    def regions_for(base, shape):
        return [r_ for r_ in files(client, pid, base)['regions']
                if r_.get('masterShapeId') == shape]

    objs = {o['id']: o for o in d0['objects']}
    back = objs['obj-back']['shapeIds'][0]
    green = next(s for s in objs['obj-front']['shapeIds']
                 if any(e.get('fill') == '#33AA77' and e.get('shapeId') == s
                        for e in d0['paint']['paths']))

    def add_manual_work(base):
        rev_cut = step(client, pid, base, {
            'base_revision': base, 'action': 'cut',
            'region_ids': [regions_for(base, back)[0]['id']],
            'd': 'M 40,80 L 200,80'})
        lower = next(r_ for r_ in files(client, pid, rev_cut)['regions']
                     if owning_regions([r_], 100, 140))
        rev_lab = step(client, pid, rev_cut, {
            'base_revision': rev_cut, 'action': 'label', 'region_ids': [lower['id']],
            'x': 100.0, 'y': 140.0})
        d = files(client, pid, rev_lab)
        assert d['manifest']['provenance'].get('manualTopology') is True
        assert any(r_['id'].startswith('r-c-') for r_ in d['regions'])
        return rev_lab

    # ---- appearance-only row: manual topology REMAINS ---------------------
    rev_manual = add_manual_work(rev)
    rev_ap = step(client, pid, rev_manual, {
        'base_revision': rev_manual, 'action': 'shape_style', 'shape_id': green,
        'region_ids': [], 'color': '#4C7FE8'})
    d_ap = files(client, pid, rev_ap)
    assert d_ap['manifest']['provenance'].get('manualTopology') is True, \
        'appearance-only edits must keep the manual topology flag'
    assert any(r_['id'].startswith('r-c-') for r_ in d_ap['regions'])

    # ---- object layer reorder: reject until confirmed ---------------------
    r = client.post(f'/api/projects/{pid}/objects', headers=H, json={
        'base_revision': rev_ap, 'object_id': 'obj-back', 'order_action': 'bring_to_front'})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'rebuilds the gameplay surfaces' in p['job'].get('message', '')
    assert p['currentRevision'] == rev_ap, 'the refusal must not publish'
    r = client.post(f'/api/projects/{pid}/objects', headers=H, json={
        'base_revision': rev_ap, 'object_id': 'obj-back', 'order_action': 'bring_to_front',
        'confirm_topology_rebuild': True})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    rev_ord = p['currentRevision']
    d_ord = files(client, pid, rev_ord)
    assert_qa(d_ord, 'confirmed object reorder')
    assert not d_ord['manifest']['provenance'].get('manualTopology'), \
        'a confirmed rebuild publishes without the manual-work flag'
    assert not any(r_['id'].startswith('r-c-') for r_ in d_ord['regions']), \
        'the confirmed rebuild honestly re-derives the partition (cut halves gone)'
    d_ap_again = files(client, pid, rev_ap)
    assert any(r_['id'].startswith('r-c-') for r_ in d_ap_again['regions']), \
        'the pre-reorder revision remains available with its manual work'

    # ---- artwork shape geometry edit: reject until confirmed --------------
    rev_manual2 = add_manual_work(rev_ord)
    edited_back_d = 'M 30,30 L 210,30 L 210,210 L 30,210 Z'
    r = client.post(f'/api/projects/{pid}/edit', headers=H, json={
        'base_revision': rev_manual2, 'action': 'shape', 'region_ids': [],
        'shape_id': back, 'd': edited_back_d})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'rebuilds the gameplay surfaces' in p['job'].get('message', '')
    rev_shape = step(client, pid, rev_manual2, {
        'base_revision': rev_manual2, 'action': 'shape', 'region_ids': [],
        'shape_id': back, 'd': edited_back_d, 'confirm_topology_rebuild': True})
    d_shape = files(client, pid, rev_shape)
    assert_qa(d_shape, 'confirmed shape edit')
    assert not d_shape['manifest']['provenance'].get('manualTopology')
    assert next(e for e in d_shape['paint']['paths'] if e['shapeId'] == back)['d'] == edited_back_d

    # ---- ordinary Build: reject until confirmed ---------------------------
    rev_manual3 = add_manual_work(rev_shape)
    r = client.post(f'/api/projects/{pid}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'failed', p['job']
    assert 'rebuilds the gameplay surfaces' in p['job'].get('message', '')
    assert p['currentRevision'] == rev_manual3
    r = client.post(f'/api/projects/{pid}/build',
                    headers=H, json={**BUILD_BODY, 'confirm_topology_rebuild': True})
    assert r.status_code == 200, r.text
    p = wait(client, pid)
    assert p['job']['status'] == 'done', p['job']
    d_build = files(client, pid, p['currentRevision'])
    assert_qa(d_build, 'confirmed rebuild')
    assert not d_build['manifest']['provenance'].get('manualTopology'), \
        'a fresh compilation carries no manual-work flag'
    # the persisted build-settings.json stays pure compiler configuration —
    # the confirmation flag must never leak into it (recompiles re-validate
    # that file as BuildSettings, extra=forbid).
    d_prev = files(client, pid, rev_manual3)
    assert any(r_['id'].startswith('r-c-') for r_ in d_prev['regions'])

    # ink reorder stays gate-free even on a manual-topology revision.
    d_build2 = files(client, pid, p['currentRevision'])
    ink_present = d_build2['paint']['inkPaths']
    assert ink_present == []  # ORDER_SVG has no ink; switch to INK_SVG project
    pid2 = new(client)
    r = client.post(f'/api/projects/{pid2}/upload-svg', headers=H,
                    files={'file': ('ink.svg', INK_SVG, 'image/svg+xml')},
                    data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid2}/build', headers=H, json=BUILD_BODY)
    assert r.status_code == 200, r.text
    rev_i = wait(client, pid2)['currentRevision']
    di0 = files(client, pid2, rev_i)
    plain_shape = next(o for o in di0['objects'] if o['id'] == 'obj-plain')['shapeIds'][0]
    squiggle = next(p_['shapeId'] for p_ in di0['paint']['inkPaths'] if p_['d'].startswith('M 40,40'))
    rev_ic = step(client, pid2, rev_i, {
        'base_revision': rev_i, 'action': 'cut',
        'region_ids': [next(r_['id'] for r_ in di0['regions'] if r_.get('masterShapeId') == plain_shape)],
        'd': 'M 150,230 L 150,290'})
    assert files(client, pid2, rev_ic)['manifest']['provenance'].get('manualTopology') is True
    rev_ii = step(client, pid2, rev_ic, {
        'base_revision': rev_ic, 'action': 'shape_order', 'region_ids': [],
        'shape_id': squiggle, 'order': 'backward'})
    di2 = files(client, pid2, rev_ii)
    assert di2['manifest']['provenance'].get('manualTopology') is True, \
        'the ink fast path keeps the manual topology (nothing was rebuilt)'
    assert any(r_['id'].startswith('r-c-') for r_ in di2['regions']), \
        'the manual cut survives the ink move'
