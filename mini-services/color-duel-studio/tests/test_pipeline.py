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
