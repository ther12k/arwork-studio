import io,json,zipfile,hashlib,shutil
from pathlib import Path
import numpy as np
import pytest
from PIL import Image,ImageDraw
from shapely.geometry import Polygon,Point
from studio.pipeline import *
from studio.models import BuildSettings,EditRequest

@pytest.fixture(scope='module')
def asset(tmp_path_factory):
    root=tmp_path_factory.mktemp('asset')
    im=Image.new('RGB',(128,160),'#91ccdd');d=ImageDraw.Draw(im)
    d.rectangle((0,95,128,160),fill='#41a582');d.rectangle((24,50,98,130),fill='#ebc681')
    d.polygon([(14,50),(63,20),(110,50)],fill='#d87155');d.rectangle((49,80,72,130),fill='#665146')
    d.ellipse((90,10,115,35),fill='#edda8a');d.rectangle((32,65,44,84),fill='#365367')
    im.save(root/'source.png')
    compile_image(root/'source.png',root/'bundle',artwork_id='test-art',version='0.1.0',title='Test fixture',
                  settings=BuildSettings(target_regions=40,palette_colors=8,paint_colors=16,max_edge=256))
    return root

def test_real_vectors(asset):
    for name in ['colored.svg','numbered.svg','linework.svg']:
        text=(asset/'bundle'/name).read_text()
        assert '<path' in text
        assert '<image' not in text and 'data:image' not in text and '<script' not in text

def test_manifest_references(asset):
    b=load_bundle(asset/'bundle');m=b['manifest']
    assert all((asset/'bundle'/f).is_file() for f in m['assets'].values())
    assert m['format']=='color-duel-detailed-vector-1'
    assert m['regionCount']>0

def test_partition_and_holes(asset):
    q=validate_bundle(load_bundle(asset/'bundle'))
    assert q['passed'] and q['missingArea']==0 and q['overlapArea']==0 and q['roundtripEmptyPixels']==0
    poly=Polygon([(0,0),(100,0),(100,100),(0,100)],holes=[[(20,20),(80,20),(80,80),(20,80)]])
    r=pack_region(poly,'hole',1)
    assert len(r['rings'])==2
    assert r['d'].count('M ')==2 and r['d'].count(' Z')==2
    assert poly.contains(Point(r['label']['x'],r['label']['y']))

def test_numbered_states_match(asset):
    b=load_bundle(asset/'bundle');text=(asset/'bundle'/'numbered.svg').read_text()
    for r in b['geometry']['regions']:assert r['d'] in text

def test_content_hash(asset):
    folder=asset/'bundle';m=read_json(folder/'artwork.json')
    raw=b''.join((folder/f).read_bytes() for f in ['regions.json','palette.json','paint.json'])
    assert m['contentHash']==hashlib.sha256(raw).hexdigest()
    for f,h in m['checksums'].items():assert checksum(folder/f)==h

def test_export_without_authoring(asset):
    z=zipfile.ZipFile(io.BytesIO(make_export(asset/'bundle')))
    assert not any('source-master.png' in n for n in z.namelist())
    m=json.loads(z.read('artworks/test-art/artwork.json'))
    assert 'sourceMaster' not in m['assets']
    assert all('artworks/test-art/'+n in z.namelist() for n in m['assets'].values())

def test_export_with_authoring(asset):
    z=zipfile.ZipFile(io.BytesIO(make_export(asset/'bundle',True)))
    assert 'artworks/test-art/source-master.png' in z.namelist()

def test_unknown_palette_rejected(asset):
    b=load_bundle(asset/'bundle');b['geometry']['regions'][0]['paletteId']=999
    assert not validate_bundle(b)['passed']

def test_duplicate_region_rejected(asset):
    b=load_bundle(asset/'bundle');b['geometry']['regions'][1]['id']=b['geometry']['regions'][0]['id']
    assert not validate_bundle(b)['passed']

def test_outside_label_rejected(asset):
    b=load_bundle(asset/'bundle');b['geometry']['regions'][0]['label']['x']=-500
    assert not validate_bundle(b)['passed']

def test_group_edit_new_revision(asset,tmp_path):
    source=asset/'bundle';b=load_bundle(source);rid=b['geometry']['regions'][0]['id']
    before=checksum(source/'regions.json')
    edit_bundle(source,tmp_path/'group',EditRequest(base_revision='x',action='group',region_ids=[rid],group='roof'),'0.2.0')
    after=load_bundle(tmp_path/'group')
    assert after['manifest']['objectGroups'][0]['id']=='roof'
    assert after['manifest']['contentHash']!=b['manifest']['contentHash']
    assert checksum(source/'regions.json')==before
    assert after['geometry']['artworkVersion']=='0.2.0'

def test_adjacent_merge(asset,tmp_path):
    b=load_bundle(asset/'bundle');regs=b['geometry']['regions'];pair=None
    for i,a in enumerate(regs):
        for c in regs[i+1:]:
            pa,pb=region_polygon(a),region_polygon(c)
            if pa.boundary.intersection(pb.boundary).length>1 and pa.union(pb).geom_type=='Polygon':
                pair=[a['id'],c['id']];break
        if pair:break
    assert pair
    result=edit_bundle(asset/'bundle',tmp_path/'merged',EditRequest(base_revision='x',action='merge',region_ids=pair),'0.2.0')
    assert result['manifest']['regionCount']==len(regs)-1
    assert result['validation']['passed']

def test_separated_merge_rejected(asset,tmp_path):
    regs=load_bundle(asset/'bundle')['geometry']['regions']
    pair=next([a['id'],b['id']] for a in regs for b in regs if region_polygon(a).distance(region_polygon(b))>10)
    with pytest.raises(ValueError,match='edge-adjacent'):
        edit_bundle(asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='merge',region_ids=pair),'0.2.0')

def test_bad_label_edit_rejected(asset,tmp_path):
    rid=load_bundle(asset/'bundle')['geometry']['regions'][0]['id']
    with pytest.raises(ValueError,match='inside'):
        edit_bundle(asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='label',region_ids=[rid],x=-10,y=-10),'0.2.0')

def test_mark_detail(asset,tmp_path):
    b=load_bundle(asset/'bundle');rid=b['geometry']['regions'][0]['id']
    result=edit_bundle(asset/'bundle',tmp_path/'detail',EditRequest(base_revision='x',action='decorate',region_ids=[rid]),'0.2.0')
    assert result['manifest']['regionCount']==len(b['geometry']['regions'])-1
    assert result['validation']['passed']

@pytest.mark.parametrize('blob',[b'',b'<svg><script>alert(1)</script></svg>',b'not an image'])
def test_bad_upload(blob,tmp_path):
    with pytest.raises(ValueError):clean_image(blob,tmp_path/'x.png')

def test_valid_upload_strips_metadata(asset,tmp_path):
    m=clean_image((asset/'source.png').read_bytes(),tmp_path/'clean.png')
    assert m['width']==128 and len(m['sha256'])==64


# ---------------------------------------------------------------------------
# Stage-2: cut / pen tools, edges, auto-subdivide, difficulty (contract 1-6)
# ---------------------------------------------------------------------------

SMALL_SVG='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="0" y="0" width="200" height="200" fill="#A9DBEF"/>
<rect x="60" y="60" width="80" height="80" fill="#3366AA"/>
</svg>'''
PEN_SVG='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="50" y="50" width="100" height="100" fill="#3366AA"/>
</svg>'''
INK_SVG='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="0" y="0" width="200" height="200" fill="#A9DBEF"/>
<rect x="60" y="60" width="80" height="80" fill="#3366AA"/>
<path d="M 20 20 L 180 20 L 180 40" fill="none" stroke="#22333B" stroke-width="2"/>
</svg>'''

@pytest.fixture(scope='module')
def svg_asset(tmp_path_factory):
    root=tmp_path_factory.mktemp('svg2')
    (root/'master.svg').write_text(SMALL_SVG)
    compile_svg_master(root/'master.svg',root/'bundle',artwork_id='svg-test',version='0.1.0',title='SVG fixture',
        settings=BuildSettings(target_regions=30,palette_colors=8,paint_colors=16,max_edge=256,
                               min_region_pixels=4,min_label_radius=1.0))
    return root

@pytest.fixture(scope='module')
def pen_asset(tmp_path_factory):
    root=tmp_path_factory.mktemp('pen')
    (root/'master.svg').write_text(PEN_SVG)
    compile_svg_master(root/'master.svg',root/'bundle',artwork_id='pen-test',version='0.1.0',title='Pen fixture',
        settings=BuildSettings(target_regions=30,palette_colors=8,paint_colors=16,max_edge=256,
                               min_region_pixels=4,min_label_radius=1.0))
    return root

def test_cut_splits_region_with_subdivision_edges(svg_asset,tmp_path):
    b=load_bundle(svg_asset/'bundle')
    target=next(r for r in b['geometry']['regions'] if region_polygon(r).covers(Point(100,100)))
    result=edit_bundle(svg_asset/'bundle',tmp_path/'cut',
        EditRequest(base_revision='x',action='cut',region_ids=[target['id']],d='M 100 40 L 100 160'),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'cut');regs=after['geometry']['regions']
    pieces=[r for r in regs if r['id'].startswith('r-c-')]
    assert len(pieces)==2 and target['id'] not in {r['id'] for r in regs}
    assert all(r['area']>=4 for r in pieces)
    assert validate_bundle(after)['passed']
    edges=after['geometry']['edges']
    sub=[e for e in edges if e['kind']=='subdivision']
    assert sub and all(e['leftRegion'] in {r['id'] for r in regs} or e['leftRegion'] is None for e in sub)
    # prior edges referencing the target were dropped, new subdivision edges exist
    assert all(target['id'] not in (e.get('leftRegion'),e.get('rightRegion')) for e in edges)
    assert any(e['leftRegion'] in {p['id'] for p in pieces} or e['rightRegion'] in {p['id'] for p in pieces} for e in sub)

def test_cut_non_crossing_line_rejected(svg_asset,tmp_path):
    b=load_bundle(svg_asset/'bundle')
    target=next(r for r in b['geometry']['regions'] if region_polygon(r).covers(Point(100,100)))
    with pytest.raises(ValueError,match='cross the whole region'):
        edit_bundle(svg_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='cut',
            region_ids=[target['id']],d='M 0 10 L 30 10'),'0.2.0')

def test_pen_draw_creates_non_overlapping_region(pen_asset,tmp_path):
    result=edit_bundle(pen_asset/'bundle',tmp_path/'pen',
        EditRequest(base_revision='x',action='draw',region_ids=[],d='M 10,10 L 40,10 L 40,40 L 10,40 Z',
                    palette_id=1,group='doodle'),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'pen');regs=after['geometry']['regions']
    pen=[r for r in regs if r['id'].startswith('r-p-')]
    assert len(pen)==1 and pen[0]['area']>=4 and pen[0]['objectId']=='doodle'
    assert pen[0]['master']['source']=='pen-drawn'
    # masks never overlap: the pen region is disjoint from the existing rect
    rect=next(r for r in regs if not r['id'].startswith('r-p-'))
    assert region_polygon(pen[0]).intersection(region_polygon(rect)).area < 1.0
    # region pen (paint=False default): gameplay-only surface, no paint path
    # is added and the region carries no masterShapeId
    before=load_bundle(pen_asset/'bundle')
    assert len(after['paint']['paths'])==len(before['paint']['paths'])
    assert 'masterShapeId' not in pen[0]
    # pen edges reference the new region (left/right = adjacent region ids)
    edges=after['geometry'].get('edges') or []
    assert any(pen[0]['id'] in (e.get('leftRegion'),e.get('rightRegion')) for e in edges)
    assert validate_bundle(after)['passed']

def test_pen_artwork_paints_and_recolors(pen_asset,tmp_path):
    """Artwork pen (P0): the drawn shape becomes finished artwork - a stable
    paint.json path (sp-* shapeId, fill, z-order) the region references via
    masterShapeId, so the normal recolor flow works on pen-drawn art."""
    before=load_bundle(pen_asset/'bundle')
    n_paths=len(before['paint']['paths'])
    max_z=max(p.get('z',0) for p in before['paint']['paths'])
    group_hex=next(e['hex'] for e in before['palette'] if e['id']==1)
    result=edit_bundle(pen_asset/'bundle',tmp_path/'penart',
        EditRequest(base_revision='x',action='draw',region_ids=[],d='M 10,10 L 40,10 L 40,40 L 10,40 Z',
                    palette_id=1,group='doodle',paint=True),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'penart')
    pen=next(r for r in after['geometry']['regions'] if r['id'].startswith('r-p-'))
    sid=pen.get('masterShapeId')
    assert sid and sid.startswith('sp-')
    entries=[p for p in after['paint']['paths'] if p.get('shapeId')==sid]
    assert len(entries)==1
    entry=entries[0]
    # fill defaults to the number group's swatch, z lands above the art
    assert entry['fill']==group_hex
    assert entry['z']>max_z
    # the painted surface and the tap surface coincide exactly
    assert entry['d']==pen['d'] and entry['fillRule']==pen['fillRule']
    assert after['paint']['sourceColorShapeCount']==before['paint']['sourceColorShapeCount']+1
    # the colored reveal actually shows the drawn shape
    assert entry['d'][:8] in (tmp_path/'penart'/'colored.svg').read_text()
    # normal recolor flow now works on the pen-drawn shape
    recolor=edit_bundle(tmp_path/'penart',tmp_path/'recolor',
        EditRequest(base_revision='x',action='recolor',region_ids=[pen['id']],color='#FF7348'),'0.3.0')
    assert recolor['validation']['passed']
    after2=load_bundle(tmp_path/'recolor')
    entry2=next(p for p in after2['paint']['paths'] if p.get('shapeId')==sid)
    assert entry2['fill']=='#FF7348'
    # P0.2 palette identity: the pen region MOVES to the palette group whose
    # answer color IS #FF7348; the original group's swatch is never mutated.
    orange=next(e for e in after2['palette'] if e['hex'].upper()=='#FF7348')
    pen2=next(r for r in after2['geometry']['regions'] if r['id']==pen['id'])
    assert pen2['paletteId']==orange['id'] and orange['id']!=1
    assert next(e['hex'] for e in after2['palette'] if e['id']==1)==group_hex
    assert validate_bundle(after2)['colorAnswerConsistency']['conflictCount']==0

def test_pen_artwork_custom_fill_stroke_and_layer(pen_asset,tmp_path):
    before=load_bundle(pen_asset/'bundle')
    min_z=min(p.get('z',0) for p in before['paint']['paths'])
    result=edit_bundle(pen_asset/'bundle',tmp_path/'penfill',
        EditRequest(base_revision='x',action='draw',region_ids=[],d='M 10,10 L 40,10 L 40,40 L 10,40 Z',
                    palette_id=1,paint=True,color='#21b6c7',stroke_width=1.5,z_behind=True),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'penfill')
    pen=next(r for r in after['geometry']['regions'] if r['id'].startswith('r-p-'))
    entry=next(p for p in after['paint']['paths'] if p.get('shapeId')==pen['masterShapeId'])
    assert entry['fill']=='#21B6C7' and entry['stroke']=='#29383E' and entry['strokeWidth']==1.5
    assert entry['z']<min_z                    # behind the existing art
    # P0.2 palette identity: a custom fill creates/reuses the matching group;
    # group 1's shared swatch is NEVER mutated (answer key stays truthful).
    assert next(e['hex'] for e in after['palette'] if e['id']==1)=='#3366AA'
    cyan=next(e for e in after['palette'] if e['hex'].upper()=='#21B6C7')
    assert cyan['id']!=1 and pen['paletteId']==cyan['id']
    assert validate_bundle(after)['passed']
    assert validate_bundle(after)['colorAnswerConsistency']['conflictCount']==0
    # the exported runtime contract still accepts the pen paint path
    assert make_export(tmp_path/'penfill')  # does not raise

def test_pen_full_overlap_rejected(pen_asset,tmp_path):
    with pytest.raises(ValueError,match='overlaps fully'):
        edit_bundle(pen_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='draw',
            region_ids=[],d='M 60,60 L 90,60 L 90,90 L 60,90 Z',palette_id=1),'0.2.0')

def test_pen_artwork_carves_full_coverage(svg_asset,tmp_path):
    """P0.1: on normal finished artwork (regions cover ~100% of the canvas)
    the artwork pen draws OVER the art: the covered gameplay surfaces are
    carved and rebuilt (A := A - P) so masks never overlap - instead of the
    old 'overlaps fully' dead end."""
    before=load_bundle(svg_asset/'bundle')
    before_area=sum(r['area'] for r in before['geometry']['regions'])
    before_paths=len(before['paint']['paths'])
    assert before_area>=40000-1.0            # full coverage: the pre-fix failing case
    # full coverage: the pre-fix failing case. (Palette is lightness-ordered:
    # the darker rect fill ranks as group 1, the light background as group 2.)
    rect_before=next(r for r in before['geometry']['regions'] if r['area']<10000)
    result=edit_bundle(svg_asset/'bundle',tmp_path/'carve',
        EditRequest(base_revision='x',action='draw',region_ids=[],
                    d='M 80,80 L 120,80 L 120,120 L 80,120 Z',
                    palette_id=rect_before['paletteId'],paint=True,color='#D8E4A0'),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'carve');g=after['geometry']
    pen=[r for r in g['regions'] if r['id'].startswith('r-p-')]
    assert len(pen)==1
    assert abs(pen[0]['area']-1600.0)<2.0     # full drawn geometry is the tap surface
    assert pen[0]['paletteId']==next(e['id'] for e in after['palette'] if e['hex']=='#D8E4A0')
    assert pen[0]['masterShapeId'].startswith('sp-')
    # the covered rect region was carved around the pen shape and keeps its links
    carved=[r for r in g['regions'] if r['id'].startswith('r-v-')]
    assert len(carved)==1
    assert abs(carved[0]['area']-4800.0)<3.0
    assert carved[0]['paletteId']==rect_before['paletteId'] and carved[0].get('masterShapeId')==rect_before.get('masterShapeId')
    # gameplay masks still never overlap and the playable surface is conserved
    assert result['validation']['overlapArea']<1.0
    assert abs(sum(r['area'] for r in g['regions'])-before_area)<4.0
    # the paint layer grew by exactly the pen artwork path (above the art)
    assert len(after['paint']['paths'])==before_paths+1
    pen_z=next(p['z'] for p in after['paint']['paths'] if p.get('shapeId')==pen[0]['masterShapeId'])
    assert pen_z>max(p['z'] for p in before['paint']['paths'])
    # carved boundary segments exist for both the pen region and the carved piece
    edges=g.get('edges') or []
    live={r['id'] for r in g['regions']}
    assert all((e.get('leftRegion') is None or e['leftRegion'] in live) and
               (e.get('rightRegion') is None or e['rightRegion'] in live) for e in edges)
    assert any(e.get('leftRegion')==carved[0]['id'] or e.get('rightRegion')==carved[0]['id'] for e in edges)
    # answer-key integrity holds on the fresh edit
    assert validate_bundle(after)['colorAnswerConsistency']['conflictCount']==0

def test_recolor_palette_identity(svg_asset,tmp_path):
    """P0.2: recolor moves the chosen regions to the palette group whose
    answer color IS the new appearance. The old group keeps its truthful
    swatch (its other regions stay correct), and recoloring back REUSES the
    existing group instead of growing the palette."""
    b=load_bundle(svg_asset/'bundle')
    by_fill={}
    for r in b['geometry']['regions']:
        path=next((p for p in b['paint']['paths'] if p.get('shapeId')==r.get('masterShapeId')),None)
        if path: by_fill[str(path['fill']).upper()]=r
    rect=by_fill['#3366AA']
    result=edit_bundle(svg_asset/'bundle',tmp_path/'re1',
        EditRequest(base_revision='x',action='recolor',region_ids=[rect['id']],color='#FF7348'),'0.2.0')
    assert result['validation']['passed']
    a=load_bundle(tmp_path/'re1')
    orange=next(e for e in a['palette'] if e['hex'].upper()=='#FF7348')
    blue=next(e for e in a['palette'] if e['hex'].upper()=='#3366AA')
    rect2=next(r for r in a['geometry']['regions'] if r['id']==rect['id'])
    assert rect2['paletteId']==orange['id'] and orange['id']!=blue['id']
    path=next(p for p in a['paint']['paths'] if p.get('shapeId')==rect['masterShapeId'])
    assert path['fill']=='#FF7348'
    assert validate_bundle(a)['colorAnswerConsistency']['conflictCount']==0
    # recoloring BACK reuses the blue group (palette does not grow)
    result2=edit_bundle(tmp_path/'re1',tmp_path/'re2',
        EditRequest(base_revision='x',action='recolor',region_ids=[rect['id']],color='#3366AA'),'0.3.0')
    assert result2['validation']['passed']
    a2=load_bundle(tmp_path/'re2')
    rect3=next(r for r in a2['geometry']['regions'] if r['id']==rect['id'])
    assert rect3['paletteId']==blue['id']
    assert len(a2['palette'])==len(a['palette'])

def test_color_answer_consistency_qa(svg_asset):
    """QA surfaces answer-key drift (region swatch vs painted appearance)
    instead of silently passing corrupted bundles."""
    b=load_bundle(svg_asset/'bundle')
    clean=validate_bundle(b)
    assert clean['passed'] and clean['colorAnswerConsistency']['conflictCount']==0
    assert clean['colorAnswerConsistency']['checked']>=2
    # corrupt: assign the blue rect region to the light-blue group
    rect=next(r for r in b['geometry']['regions'] if r['paletteId']==2)
    rect['paletteId']=1
    drifted=validate_bundle(b)
    assert drifted['colorAnswerConsistency']['conflictCount']==1
    assert rect['id'] in drifted['colorAnswerConsistency']['conflictRegionIds']
    assert any('Color-answer consistency' in w for w in drifted['warnings'])

@pytest.fixture(scope='module')
def ink_asset(tmp_path_factory):
    root=tmp_path_factory.mktemp('ink')
    (root/'master.svg').write_text(INK_SVG)
    compile_svg_master(root/'master.svg',root/'bundle',artwork_id='ink-test',version='0.1.0',title='Ink fixture',
        settings=BuildSettings(target_regions=30,palette_colors=8,paint_colors=16,max_edge=256,
                               min_region_pixels=4,min_label_radius=1.0))
    return root

def test_export_layer_order_matches_runtime(ink_asset):
    """numbered.svg / linework.svg stack ink BELOW the semantic edges overlay
    with labels last - the same paint -> masks -> ink -> edges -> labels DOM
    order the runtime board renders (parity, not just dash attributes)."""
    b=load_bundle(ink_asset/'bundle')
    assert b['paint'].get('inkPaths'), 'fixture needs a stroke-only ink path'
    numbered=(ink_asset/'bundle'/'numbered.svg').read_text()
    linework=(ink_asset/'bundle'/'linework.svg').read_text()
    for name,text in (('numbered.svg',numbered),('linework.svg',linework)):
        ink=text.index('data-layer="ink"')
        edges=text.index('data-layer="edges"')
        assert ink<edges,f'{name}: ink must render below the edges overlay'
        labels=text.find('<text')
        if labels!=-1:
            assert edges<labels,f'{name}: labels must render last'
    paint=numbered.index('data-layer="paint"')
    masks=numbered.index('fill="white"')
    assert paint<masks<numbered.index('data-layer="ink"')<numbered.index('data-layer="edges"')

def test_pen_requires_palette(pen_asset,tmp_path):
    with pytest.raises(ValueError,match='number group'):
        edit_bundle(pen_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='draw',
            region_ids=[],d='M 10,10 L 40,10 L 40,40 L 10,40 Z'),'0.2.0')

def test_draw_rejects_region_selection(pen_asset,tmp_path):
    b=load_bundle(pen_asset/'bundle');rid=b['geometry']['regions'][0]['id']
    with pytest.raises(ValueError,match='no region selection'):
        edit_bundle(pen_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='draw',
            region_ids=[rid],d='M 10,10 L 40,10 L 40,40 L 10,40 Z',palette_id=1),'0.2.0')

def test_auto_subdivide_treehouse(tmp_path_factory):
    root=tmp_path_factory.mktemp('sub')
    settings=BuildSettings(target_regions=90,palette_colors=32,paint_colors=80,max_edge=1024,
                           min_region_pixels=35,min_label_radius=3.0,auto_subdivide=True)
    compile_svg_master(Path(__file__).resolve().parents[1]/'examples/treehouse-master.svg',root/'bundle',
        artwork_id='sub-test',version='0.1.0',title='Treehouse sub',settings=settings)
    b=load_bundle(root/'bundle');g=b['geometry']
    assert 80<=len(g['regions'])<=95,'auto-subdivide should reach ~target'
    assert all(r['area']>=35 for r in g['regions'])
    assert any(r['master']['source']=='subdivision-split' for r in g['regions'])
    kinds={e['kind'] for e in g['edges']}
    assert kinds=={'artwork','subdivision'}
    assert any(e['kind']=='subdivision' and e['leftRegion'] and e['rightRegion'] for e in g['edges'])
    assert validate_bundle(b)['passed']

def test_difficulty_profile_in_manifest(svg_asset):
    m=read_json(svg_asset/'bundle'/'artwork.json')
    d=m['difficulty']
    assert isinstance(d,dict) and d['rating'] in ('easy','medium','hard','master')
    assert 0<=d['score']<=100
    for key in ('regionCount','medianRegionArea','tinyRegionPct','requiredZoom','labelClearance',
                'paletteAmbiguity','paletteGroups','avgNeighbors','subdivisionEdges','objectDensity'):
        assert key in d['metrics']
    assert m['difficultyValidatedByPlaytest'] is False
    qa=read_json(svg_asset/'bundle'/'validation.json')
    assert qa['difficulty']['rating']==d['rating']

def test_edges_survive_lean_runtime_export(svg_asset):
    z=zipfile.ZipFile(io.BytesIO(make_export(svg_asset/'bundle')))
    runtime=json.loads(z.read('artworks/svg-test/regions.json'))
    assert isinstance(runtime.get('edges'),list) and runtime['edges']
    assert 'boundaryStyle' in runtime
    assert all('master' not in r and 'flat' not in r for r in runtime['regions'])

def test_merge_prunes_stale_edges(svg_asset,tmp_path):
    b=load_bundle(svg_asset/'bundle');regs=b['geometry']['regions']
    pair=None
    for i,a in enumerate(regs):
        for c in regs[i+1:]:
            pa,pb=region_polygon(a),region_polygon(c)
            if pa.boundary.intersection(pb.boundary).length>1 and pa.union(pb).geom_type=='Polygon':
                pair=[a['id'],c['id']];break
        if pair:break
    assert pair
    result=edit_bundle(svg_asset/'bundle',tmp_path/'merged',
        EditRequest(base_revision='x',action='merge',region_ids=pair),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'merged')
    live={r['id'] for r in after['geometry']['regions']}|{r['id'] for r in after['geometry']['decorations']}
    for e in after['geometry'].get('edges') or []:
        assert (e.get('leftRegion') is None or e.get('leftRegion') in live) \
            and (e.get('rightRegion') is None or e.get('rightRegion') in live)


# ---------------------------------------------------------------------------
# Stage-3: node boundary drag + play-test difficulty factor (contract A/B)
# ---------------------------------------------------------------------------

NODE_SVG='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="0" y="0" width="100" height="200" fill="#3366AA"/>
<rect x="100" y="0" width="100" height="200" fill="#A9DBEF"/>
</svg>'''
STRIPS_SVG='''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="0" y="0" width="60" height="200" fill="#3366AA"/>
<rect x="60" y="0" width="60" height="200" fill="#A9DBEF"/>
<rect x="120" y="0" width="80" height="200" fill="#79B258"/>
</svg>'''

@pytest.fixture(scope='module')
def node_asset(tmp_path_factory):
    root=tmp_path_factory.mktemp('node')
    (root/'master.svg').write_text(NODE_SVG)
    compile_svg_master(root/'master.svg',root/'bundle',artwork_id='node-test',version='0.1.0',title='Node fixture',
        settings=BuildSettings(target_regions=30,palette_colors=8,paint_colors=16,max_edge=256,
                               min_region_pixels=4,min_label_radius=1.0))
    return root

@pytest.fixture(scope='module')
def strips_asset(tmp_path_factory):
    root=tmp_path_factory.mktemp('strips')
    (root/'master.svg').write_text(STRIPS_SVG)
    compile_svg_master(root/'master.svg',root/'bundle',artwork_id='strips-test',version='0.1.0',title='Strips fixture',
        settings=BuildSettings(target_regions=30,palette_colors=8,paint_colors=16,max_edge=256,
                               min_region_pixels=4,min_label_radius=1.0))
    return root

def test_node_edit_moves_shared_boundary(node_asset,tmp_path):
    b=load_bundle(node_asset/'bundle');g=b['geometry']
    left,right=[r['id'] for r in g['regions']]
    # The pair-matching edge is the left region's artwork outline; dragging its
    # shared side from x=100 to x=120 moves a 20x200 strip to the left region.
    result=edit_bundle(node_asset/'bundle',tmp_path/'node',
        EditRequest(base_revision='x',action='node',region_ids=[left,right],
                    d='M 0,0 L 120,0 L 120,200 L 0,200'),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'node');regs=after['geometry']['regions']
    moved=[r for r in regs if r['id'].startswith('r-n-')]
    assert len(moved)==2 and {r['id'] for r in regs}=={r['id'] for r in moved}
    assert all(r['master']['source']=='node-edit' for r in moved)
    assert {round(r['area']) for r in moved}=={24000,16000}
    assert {r['masterShapeId'] for r in moved}=={'s0000','s0001'}   # inherited
    edges=after['geometry']['edges']
    assert all(left not in (e.get('leftRegion'),e.get('rightRegion'))
               and right not in (e.get('leftRegion'),e.get('rightRegion')) for e in edges)
    new_ids={r['id'] for r in moved}
    shared=[e for e in edges if e['leftRegion'] in new_ids and e['rightRegion'] in new_ids]
    assert shared and any(e['kind']=='artwork' for e in shared)   # artwork boundaries stay artwork
    assert validate_bundle(after)['passed']
    assert after['manifest']['provenance']['lastEdit']=='node'
    assert after['geometry']['partitionTolerance']>g['partitionTolerance']

def test_node_edit_keeps_subdivision_kind(svg_asset,tmp_path):
    # Cut first, then drag the fresh cut boundary: a subdivision stays subdivision.
    b=load_bundle(svg_asset/'bundle')
    target=next(r for r in b['geometry']['regions'] if region_polygon(r).covers(Point(100,100)))
    edit_bundle(svg_asset/'bundle',tmp_path/'cut',
        EditRequest(base_revision='x',action='cut',region_ids=[target['id']],d='M 100 40 L 100 160'),'0.2.0')
    cut=load_bundle(tmp_path/'cut')
    pieces=[r['id'] for r in cut['geometry']['regions'] if r['id'].startswith('r-c-')]
    shared=next(e for e in cut['geometry']['edges']
                if {e.get('leftRegion'),e.get('rightRegion')}==set(pieces))
    assert shared['kind']=='subdivision'
    result=edit_bundle(tmp_path/'cut',tmp_path/'node',
        EditRequest(base_revision='x',action='node',region_ids=pieces,d='M 110,140 L 110,60'),'0.3.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'node')
    nodes=[r for r in after['geometry']['regions'] if r['id'].startswith('r-n-')]
    assert len(nodes)==2 and {round(r['area']) for r in nodes}=={4000,2400}
    new_ids={r['id'] for r in nodes}
    new_shared=[e for e in after['geometry']['edges']
                if e['leftRegion'] in new_ids and e['rightRegion'] in new_ids]
    assert new_shared and all(e['kind']=='subdivision' for e in new_shared)
    assert all(p not in (e.get('leftRegion'),e.get('rightRegion'))
               for p in pieces for e in after['geometry']['edges'])
    assert validate_bundle(after)['passed']

def test_node_requires_shared_edge_pair(strips_asset,tmp_path):
    b=load_bundle(strips_asset/'bundle');ids=[r['id'] for r in b['geometry']['regions']]
    with pytest.raises(ValueError,match='shared boundary between exactly two'):
        edit_bundle(strips_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='node',
            region_ids=[ids[0],ids[2]],d='M 30,0 L 30,200'),'0.2.0')

def test_node_noop_drag_rejected(node_asset,tmp_path):
    b=load_bundle(node_asset/'bundle');left,right=[r['id'] for r in b['geometry']['regions']]
    with pytest.raises(ValueError,match='new position'):
        edit_bundle(node_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='node',
            region_ids=[left,right],d='M 0,0 L 100,0 L 100,200 L 0,200'),'0.2.0')

def test_node_missing_d_rejected(node_asset,tmp_path):
    b=load_bundle(node_asset/'bundle');left,right=[r['id'] for r in b['geometry']['regions']]
    with pytest.raises(ValueError,match='at least one anchor'):
        edit_bundle(node_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='node',
            region_ids=[left,right]),'0.2.0')

def test_node_closed_ring_submission(node_asset,tmp_path):
    # Closed (Z) edges are the norm for rect outlines; the frontend submits the
    # dragged ring closed (Z) — the lens math treats it exactly like the open
    # variant (flatten_d drops the closure command).
    b=load_bundle(node_asset/'bundle');left,right=[r['id'] for r in b['geometry']['regions']]
    result=edit_bundle(node_asset/'bundle',tmp_path/'node',
        EditRequest(base_revision='x',action='node',region_ids=[left,right],
                    d='M 0,0 L 120,0 L 120,200 L 0,200 Z'),'0.2.0')
    assert result['validation']['passed']
    after=load_bundle(tmp_path/'node')
    moved=[r for r in after['geometry']['regions'] if r['id'].startswith('r-n-')]
    assert len(moved)==2 and {round(r['area']) for r in moved}=={24000,16000}

def test_node_spill_into_third_region_rejected(strips_asset,tmp_path):
    b=load_bundle(strips_asset/'bundle');ids=[r['id'] for r in b['geometry']['regions']]
    with pytest.raises(ValueError,match='crosses other regions'):
        edit_bundle(strips_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='node',
            region_ids=[ids[0],ids[1]],d='M 0,0 L 150,0 L 150,200 L 0,200'),'0.2.0')

def test_node_too_small_region_rejected(node_asset,tmp_path):
    b=load_bundle(node_asset/'bundle');left,right=[r['id'] for r in b['geometry']['regions']]
    with pytest.raises(ValueError,match='too small to tap'):
        edit_bundle(node_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='node',
            region_ids=[left,right],d='M 0,0 L 199.99,0 L 199.99,200 L 0,200'),'0.2.0')

def test_node_disconnected_result_rejected(svg_asset,tmp_path):
    b=load_bundle(svg_asset/'bundle')
    target=next(r for r in b['geometry']['regions'] if region_polygon(r).covers(Point(100,100)))
    edit_bundle(svg_asset/'bundle',tmp_path/'cut',
        EditRequest(base_revision='x',action='cut',region_ids=[target['id']],d='M 100 40 L 100 160'),'0.2.0')
    cut=load_bundle(tmp_path/'cut')
    pieces=[r['id'] for r in cut['geometry']['regions'] if r['id'].startswith('r-c-')]
    # A new boundary that crosses the old one pinches a piece at a single point.
    with pytest.raises(ValueError,match='disconnected pieces'):
        edit_bundle(tmp_path/'cut',tmp_path/'bad',EditRequest(base_revision='x',action='node',
            region_ids=pieces,d='M 60,100 L 140,100'),'0.3.0')

def test_difficulty_profile_playtest_blend(svg_asset):
    b=load_bundle(svg_asset/'bundle')
    base=difficulty_profile(b)
    assert 'playtestCount' not in base['metrics']
    assert difficulty_profile(b,[])['score']==base['score']    # no playtests -> unchanged
    count=base['metrics']['regionCount']
    runs=[{'seconds':100.0,'filled':count,'total':count,'mistakes':6,'mode':'number'},
          {'seconds':200.0,'filled':count,'total':count,'mistakes':2,'mode':'memory'}]
    blended=difficulty_profile(b,runs)
    metrics=blended['metrics']
    assert metrics['playtestCount']==2
    assert metrics['playtestMedianSeconds']==150.0
    assert metrics['playtestSecondsPerRegion']==round(150.0/count,2)
    assert metrics['playtestMistakesPerRegion']==round(8.0/count,3)
    pace=min(1.0,(150.0/count)/20.0)
    assert blended['score']==round(min(100.0,0.9*base['score']+10.0*pace),1)
    assert blended['rating'] in ('easy','medium','hard','master')
    # Mode separation (contract: free is engagement, not puzzle difficulty):
    # uncompleted free runs carry engagement data but never feed the puzzle
    # metrics, validate the rating or blend the score.
    partial=[{'seconds':60.0,'filled':1,'total':count,'mistakes':0,'mode':'free'}]
    unvalidated=difficulty_profile(b,partial)
    assert unvalidated['score']==base['score']
    assert 'playtestCount' not in unvalidated['metrics']
    assert unvalidated['metrics']['freePlayCount']==1
    assert unvalidated['metrics']['freeMedianSeconds']==60.0
    # even a COMPLETED free run never validates or blends puzzle difficulty
    free_done=[{'seconds':60.0,'filled':count,'total':count,'mistakes':0,'mode':'free'}]
    free_profile=difficulty_profile(b,free_done)
    assert free_profile['score']==base['score']
    assert 'playtestCount' not in free_profile['metrics']
    # duel is a puzzle mode and blends like number/memory
    duel=[{'seconds':100.0,'filled':count,'total':count,'mistakes':0,'mode':'duel'}]
    assert difficulty_profile(b,duel)['metrics']['playtestCount']==1

def test_emit_bundle_reads_playtest_records(svg_asset,tmp_path):
    b=load_bundle(svg_asset/'bundle')
    out=tmp_path/'rev';out.mkdir()
    write_json(out/'playtests.json',[{'seconds':42.0,'filled':2,'total':2,'mistakes':1,'mode':'number'}])
    emit_bundle(out,b,previews=False)
    m=read_json(out/'artwork.json')
    assert m['difficultyValidatedByPlaytest'] is True
    assert m['difficulty']['metrics']['playtestCount']==1
    qa=read_json(out/'validation.json')
    assert 'blended' in qa['difficulty']['note']
    # free-only playtests never validate the rating
    out2=tmp_path/'rev2';out2.mkdir()
    write_json(out2/'playtests.json',[{'seconds':42.0,'filled':2,'total':2,'mistakes':0,'mode':'free'}])
    emit_bundle(out2,b,previews=False)
    m2=read_json(out2/'artwork.json')
    assert m2['difficultyValidatedByPlaytest'] is False
    assert m2['difficulty']['metrics']['freePlayCount']==1

def test_exports_render_semantic_edges(svg_asset,tmp_path):
    """numbered/linework exports use the semantic edges[] overlay (artwork
    solid, subdivision light+dashed) - visually identical to the runtime
    board, not the outline-per-region style."""
    b=load_bundle(svg_asset/'bundle')
    target=next(r for r in b['geometry']['regions'] if region_polygon(r).covers(Point(100,100)))
    edit_bundle(svg_asset/'bundle',tmp_path/'cut',
        EditRequest(base_revision='x',action='cut',region_ids=[target['id']],d='M 100 40 L 100 160'),'0.2.0')
    line=(tmp_path/'cut'/'linework.svg').read_text()
    numbered=(tmp_path/'cut'/'numbered.svg').read_text()
    for text in (line,numbered):
        assert 'data-layer="edges"' in text
        assert 'data-edge-kind="artwork"' in text
        assert 'data-edge-kind="subdivision"' in text
        assert 'stroke-dasharray="3 2.2"' in text
        assert 'stroke="#29383E"' in text
    # masks render fill-only (the overlay draws the boundaries)
    assert '<g fill-rule="evenodd">' in numbered
    # legacy bundles without edges keep the outline-per-region fallback
    line_svg_asset=(svg_asset/'bundle'/'linework.svg').read_text()
    assert 'data-layer="edges"' in line_svg_asset  # svg masters always carry edges
