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
    # gameplay-only surface: no new paint path was added
    assert not any(str(p.get('shapeId','')).startswith('r-p-') for p in after['paint']['paths'])
    # pen edges reference the new region (left/right = adjacent region ids)
    edges=after['geometry'].get('edges') or []
    assert any(pen[0]['id'] in (e.get('leftRegion'),e.get('rightRegion')) for e in edges)
    assert validate_bundle(after)['passed']

def test_pen_full_overlap_rejected(pen_asset,tmp_path):
    with pytest.raises(ValueError,match='overlaps fully'):
        edit_bundle(pen_asset/'bundle',tmp_path/'bad',EditRequest(base_revision='x',action='draw',
            region_ids=[],d='M 60,60 L 90,60 L 90,90 L 60,90 Z',palette_id=1),'0.2.0')

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
