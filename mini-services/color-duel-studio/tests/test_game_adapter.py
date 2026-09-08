"""Release-gate regression tests (review stage 1).

These encode the acceptance rules the engineering review demanded:

1. SHARED FORMAT CONTRACT - a freshly compiled artwork must pass the ACTUAL
   shipped game adapter (integration/detailed-board.mjs), not just the
   studio renderer. Checked by scripts/adapter-contract-check.mjs (bun).
2. VISIBLE-REGION GEOMETRY - region masks do not overlap, every number label
   hits its own region, and every region can be colored first (fill-order
   independence does not depend on another region's completion).
3. SVG FIDELITY - strokes on filled shapes, fill-opacity, per-shape fill
   rules (nonzero), percentage gradients and drawing order survive
   import -> sanitize -> compile.
4. RECOLOR semantics - 'recolor' changes the visible appearance, 'palette'
   changes the number group.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from shapely.ops import unary_union

from studio.pipeline import (
    compile_image, compile_svg_master, edit_bundle, load_bundle,
    make_export, region_polygon, validate_bundle,
)
from studio.models import BuildSettings, EditRequest
from studio.curves import point_in_rings_rule

ROOT = Path(__file__).resolve().parent.parent
ADAPTER_CHECK = ROOT / 'scripts' / 'adapter-contract-check.mjs'

MASTER_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240">
<defs>
<linearGradient id="sky" gradientUnits="objectBoundingBox" x1="0%" y1="0%" x2="0%" y2="100%">
<stop offset="0" stop-color="#A9DBEF"/><stop offset="1" stop-color="#FFDCA6"/></linearGradient>
</defs>
<rect x="0" y="0" width="240" height="240" fill="url(#sky)"/>
<rect x="20" y="20" width="140" height="140" fill="#3366AA"/>
<rect x="60" y="60" width="120" height="120" fill="#AA3355"/>
<rect x="90" y="90" width="150" height="100" fill="#66AA33" stroke="#223311" stroke-width="2"/>
<path d="M10 200 L70 200 L40 230 Z M20 205 L60 205 L40 220 Z" fill="#884400" fill-rule="nonzero"/>
<ellipse cx="180" cy="40" rx="40" ry="22" fill="#FFFFFF" fill-opacity="0.45"/>
<path d="M5 235 Q 40 215 80 235 T 160 235" fill="none" stroke="#29383E" stroke-width="2"/>
</svg>'''


@pytest.fixture(scope='module')
def svg_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp('svgmaster')
    (root / 'master.svg').write_text(MASTER_SVG)
    settings = BuildSettings(target_regions=40, palette_colors=8, paint_colors=16,
                             max_edge=256, min_region_pixels=4, min_label_radius=1.0)
    compile_svg_master(root / 'master.svg', root / 'bundle', artwork_id='adapter-test',
                       version='0.1.0', title='Adapter contract', settings=settings)
    return root / 'bundle'


@pytest.fixture(scope='module')
def raster_bundle(tmp_path_factory):
    from PIL import Image, ImageDraw
    root = tmp_path_factory.mktemp('raster')
    im = Image.new('RGB', (128, 160), '#91ccdd')
    d = ImageDraw.Draw(im)
    d.rectangle((0, 95, 128, 160), fill='#41a582')
    d.rectangle((24, 50, 98, 130), fill='#ebc681')
    d.polygon([(14, 50), (63, 20), (110, 50)], fill='#d87155')
    im.save(root / 'source.png')
    compile_image(root / 'source.png', root / 'bundle', artwork_id='raster-test',
                  version='0.1.0', title='Raster fixture',
                  settings=BuildSettings(target_regions=40, palette_colors=8,
                                         paint_colors=16, max_edge=256))
    return root / 'bundle'


def _run_adapter(*folders):
    """Run the shipped game adapter (bun) against bundle folders."""
    if shutil.which('bun') is None:
        pytest.skip('bun runtime unavailable')
    proc = subprocess.run(
        [sys.executable and 'bun', str(ADAPTER_CHECK), *(str(f) for f in folders)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    return proc


def test_fresh_svg_master_passes_shipped_adapter(svg_bundle, raster_bundle):
    proc = _run_adapter(svg_bundle, raster_bundle)
    assert proc.returncode == 0, f'shipped adapter rejected a fresh compile:\n{proc.stdout}\n{proc.stderr}'
    assert 'FAIL' not in proc.stdout


def test_lean_runtime_export_passes_shipped_adapter(svg_bundle, tmp_path):
    data = make_export(svg_bundle, include_authoring=False)
    out = tmp_path / 'runtime.zip'
    out.write_bytes(data)
    import zipfile
    folder = tmp_path / 'unzipped' / 'artworks' / 'adapter-test'
    with zipfile.ZipFile(out) as z:
        z.extractall(tmp_path / 'unzipped')
    proc = _run_adapter(folder)
    assert proc.returncode == 0, f'shipped adapter rejected the lean runtime export:\n{proc.stdout}\n{proc.stderr}'
    # lean geometry really is lean: no duplicated authoring representations
    geometry = json.loads((folder / 'regions.json').read_text())
    for key in ('master', 'flat', 'rings', 'legacy'):
        assert all(key not in r for r in geometry['regions']), f'authoring field {key} leaked into the runtime export'


def test_masks_do_not_overlap_and_labels_own_their_region(svg_bundle):
    bundle = load_bundle(svg_bundle)
    regs = bundle['geometry']['regions']
    polys = [region_polygon(r) for r in regs]
    union = unary_union([p for p in polys if not p.is_empty])
    total = sum(p.area for p in polys if not p.is_empty)
    overlap = total - union.area
    assert overlap < 1.0, f'region masks overlap by {overlap:.2f}px² - fill-order independence broken'
    for r in regs:
        rings = r.get('flat', {}).get('rings') or r.get('rings')
        rule = r.get('fillRule', 'evenodd')
        assert point_in_rings_rule(rings, r['label']['x'], r['label']['y'], rule), \
            f'label of {r["id"]} is not inside its own region'
        owners = [o['id'] for o in regs if o is not r
                  and point_in_rings_rule(o.get('flat', {}).get('rings') or o.get('rings'),
                                          r['label']['x'], r['label']['y'],
                                          o.get('fillRule', 'evenodd'))]
        assert not owners, f'number of {r["id"]} hits region(s) {owners}'


def test_foreground_covers_background_surface(svg_bundle):
    """The review's reproduction: shapes stacked over a background. Each
    region must be its source shape MINUS the opaque coverage above it."""
    bundle = load_bundle(svg_bundle)
    regs = bundle['geometry']['regions']
    # A point inside the topmost rect (90,90,150x100) resolves to exactly one region.
    owners = [r['id'] for r in regs
              if point_in_rings_rule(r.get('flat', {}).get('rings') or r.get('rings'),
                                     140, 130, r.get('fillRule', 'evenodd'))]
    assert len(owners) == 1, f'point inside the foreground resolves to {owners} (need exactly 1)'
    by_shape = {r.get('masterShapeId'): r for r in regs}
    # sky background 240x240 = 57600, minus everything drawn above it
    assert by_shape['s0000']['area'] < 57600 - 1000, 'background kept covered area'
    # blue rect 140x140 = 19600, partially covered by the two rects above
    assert by_shape['s0001']['area'] < 19600 - 1000, 'first stacked rect kept covered area'
    # red rect 120x120 = 14400, partially covered by the green rect above
    assert by_shape['s0002']['area'] < 14400 - 1000, 'second stacked rect kept covered area'
    # green rect 150x100 = 15000: nothing drawn above it -> verbatim full area
    assert abs(by_shape['s0003']['area'] - 15000) < 20, 'topmost rect must keep its full surface'


def test_fidelity_survives_import_and_compile(svg_bundle):
    bundle = load_bundle(svg_bundle)
    paint = bundle['paint']
    fills = {p['fill'] for p in paint['paths']}
    assert any(f.startswith('url(#') for f in fills), 'gradient fill lost'
    assert any(p.get('stroke') for p in paint['paths']), 'stroke on a filled shape lost'
    assert any(p.get('fillOpacity', 1) < 0.999 for p in paint['paths']), 'fill-opacity lost'
    assert any(p.get('fillRule') == 'nonzero' for p in paint['paths']), 'nonzero fill rule lost'
    # drawing order preserved: the ink path (last in source) has the highest z
    zs = [p.get('z') for p in paint['paths'] + paint['inkPaths'] if p.get('z') is not None]
    assert zs and zs == sorted(zs), 'paint entries are not z-ordered'
    # percentage gradient resolved to user space against the 240px viewBox
    grad = paint['gradients'][0]
    assert grad['y2'] == 240.0, f'percentage gradient mis-resolved: {grad}'


def test_validation_report_has_release_gates(svg_bundle):
    bundle = load_bundle(svg_bundle)
    report = validate_bundle(bundle)
    assert report['passed']
    geo = report['geometry']
    assert geo['visibleRegionGeometry'] is True
    assert geo['labelOwnershipConflicts'] == 0
    assert 'acceptanceRule' in geo


def test_recolor_changes_appearance_and_palette_changes_group(svg_bundle, tmp_path):
    bundle = load_bundle(svg_bundle)
    regs = bundle['geometry']['regions']
    rid = regs[0]['id']
    shape_id = regs[0].get('masterShapeId')
    assert shape_id, 'region missing masterShapeId link'

    # recolor: visible appearance changes (paint fill of the source shape)
    edit_bundle(svg_bundle, tmp_path / 'recolor',
                EditRequest(base_revision='x', action='recolor', region_ids=[rid],
                            color='#1B9AAA', preserve_shading=False), '0.2.0')
    recolored = load_bundle(tmp_path / 'recolor')
    target = [p for p in recolored['paint']['paths'] if p.get('shapeId') == shape_id]
    assert target and all(p['fill'] == '#1B9AAA' for p in target), 'recolor did not change the paint layer'

    # recolor with preserve_shading: gradient stops are tinted, not replaced
    grad_regions = [r for r in regs if load_bundle(svg_bundle)['paint']['paths']
                    and any(p.get('shapeId') == r.get('masterShapeId')
                            and str(p.get('fill', '')).startswith('url(#')
                            for p in load_bundle(svg_bundle)['paint']['paths'])]
    if grad_regions:
        edit_bundle(svg_bundle, tmp_path / 'tint',
                    EditRequest(base_revision='x', action='recolor', region_ids=[grad_regions[0]['id']],
                                color='#1B9AAA', preserve_shading=True), '0.4.0')
        tinted = load_bundle(tmp_path / 'tint')
        grad = tinted['paint']['gradients'][0]
        assert all(s['color'] != '#1B9AAA' for s in grad['stops']) or len(grad['stops']) == 1, \
            'preserve_shading replaced the fill instead of tinting'
        assert grad['stops'][0]['color'].startswith('#'), 'tinted stop color malformed'

    # palette: number group changes, paint layer unchanged
    edit_bundle(svg_bundle, tmp_path / 'palette',
                EditRequest(base_revision='x', action='palette', region_ids=[rid],
                            palette_id=3), '0.3.0')
    regrouped = load_bundle(tmp_path / 'palette')
    changed = [r for r in regrouped['geometry']['regions'] if r['id'] == rid][0]
    assert changed['paletteId'] == 3, 'palette action did not reassign the number group'
    original_fills = {p['fill'] for p in bundle['paint']['paths']}
    after_fills = {p['fill'] for p in regrouped['paint']['paths']}
    assert original_fills == after_fills, 'palette action must NOT change the visible artwork'


# ---------------------------------------------------------------------------
# Stage-2 contract: shipped adapter accepts edges bundles + free-color API
# ---------------------------------------------------------------------------

BOARD_CHECK = ROOT / 'tests' / 'adapter-board-check.mjs'


def _write_bundle(folder: Path, edges=None, boundary_style=None):
    geometry = {
        'geometrySchema': 2, 'artworkId': 'edge-test', 'artworkVersion': '0.1.0',
        'viewBox': [0, 0, 100, 100], 'fillRule': 'evenodd', 'stroke': '#29383E', 'strokeWidth': 0.65,
        'regions': [{'id': 'r-00001', 'paletteId': 1, 'objectId': 'unassigned',
                     'd': 'M 10,10 L 50,10 L 50,50 L 10,50 Z', 'fillRule': 'evenodd',
                     'bbox': [10, 10, 50, 50], 'area': 1600,
                     'label': {'x': 30, 'y': 30, 'fontSize': 10, 'minScreenPx': 9, 'clearance': 15}}],
        'decorations': [], 'detailPaths': [],
    }
    if edges is not None:
        geometry['edges'] = edges
    if boundary_style is not None:
        geometry['boundaryStyle'] = boundary_style
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'artwork.json').write_text(json.dumps({
        'schemaVersion': 1, 'format': 'color-duel-detailed-vector-1', 'id': 'edge-test',
        'version': '0.1.0', 'title': 'Edge contract', 'regionCount': 1, 'contentHash': 'h1',
        'objectGroups': [], 'qa': {'status': 'draft'},
        'assets': {'regions': 'regions.json', 'palette': 'palette.json', 'paint': 'paint.json'}}))
    (folder / 'regions.json').write_text(json.dumps(geometry))
    (folder / 'palette.json').write_text(json.dumps([
        {'id': 1, 'number': 1, 'name': 'Tone 01', 'hex': '#3366AA',
         'paint': {'type': 'linearGradient', 'stops': [{'offset': 0, 'color': '#3366AA'}, {'offset': 1, 'color': '#3366AA'}]}}]))
    (folder / 'paint.json').write_text(json.dumps(
        {'artworkId': 'edge-test', 'viewBox': [0, 0, 100, 100], 'paths': [], 'inkPaths': [], 'gradients': []}))
    return folder


def test_shipped_adapter_accepts_edges_bundle(tmp_path):
    edges = [
        {'id': 'e-0001', 'd': 'M 10,10 L 50,10 L 50,50 L 10,50 Z', 'kind': 'artwork',
         'leftRegion': 'r-00001', 'rightRegion': None},
        {'id': 'e-0002', 'd': 'M 30,10 L 30,50', 'kind': 'subdivision',
         'leftRegion': 'r-00001', 'rightRegion': None},
    ]
    style = {'artwork': {'stroke': '#22333B', 'strokeWidth': 1.6},
             'subdivision': {'stroke': '#7A8C94', 'strokeWidth': 0.85, 'dash': '3 2.2'}}
    folder = _write_bundle(tmp_path / 'edges', edges, style)
    proc = _run_adapter(folder)
    assert proc.returncode == 0, f'shipped adapter rejected an edges bundle:\n{proc.stdout}\n{proc.stderr}'


def test_shipped_adapter_rejects_invalid_edges(tmp_path):
    edges = [{'id': 'e-0001', 'd': 'M 10,10 L 50,10', 'kind': 'scribble',
              'leftRegion': 'r-00001', 'rightRegion': None}]
    folder = _write_bundle(tmp_path / 'bad-edges', edges)
    proc = _run_adapter(folder)
    assert proc.returncode != 0, 'shipped adapter accepted an invalid edge kind'


def test_shipped_adapter_rejects_edge_unknown_region_ref(tmp_path):
    edges = [{'id': 'e-0001', 'd': 'M 10,10 L 50,10', 'kind': 'artwork',
              'leftRegion': 'r-404', 'rightRegion': None}]
    folder = _write_bundle(tmp_path / 'bad-ref', edges)
    proc = _run_adapter(folder)
    assert proc.returncode != 0, 'shipped adapter accepted an unknown edge region reference'


def test_adapter_free_color_and_board_api():
    """Headless board API check: setFreeColor/freeColors hex semantics,
    no mistake counting in free mode, edges overlay rendering."""
    if shutil.which('bun') is None:
        pytest.skip('bun runtime unavailable')
    proc = subprocess.run(['bun', str(BOARD_CHECK)], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=120)
    assert proc.returncode == 0, f'adapter free-color/edges board check failed:\n{proc.stdout}\n{proc.stderr}'
    assert '0 failures' in proc.stdout
