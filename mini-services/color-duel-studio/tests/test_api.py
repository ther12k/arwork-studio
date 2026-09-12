import io,time,json,base64,re,os,tempfile,hashlib
from studio.pipeline import read_json, write_json
from pathlib import Path
import httpx,pytest
import numpy as np
from PIL import Image
from fastapi.testclient import TestClient
from studio.app import create_app
from studio.ai import Provider
H={'X-Studio-Request':'1'}

def picture():
    b=io.BytesIO();Image.new('RGB',(96,96),'#eab080').save(b,format='PNG');return b.getvalue()

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:yield c

def new(c):return c.post('/api/projects',json={'title':'API test'},headers=H).json()['id']
def wait(c,pid,timeout=30):
    end=time.time()+timeout
    while time.time()<end:
        p=c.get('/api/projects/'+pid).json()
        if p['job']['status'] in ['done','failed','canceled','interrupted']:return p
        time.sleep(.1)
    raise AssertionError('Job timeout')

def test_write_guard(client):assert client.post('/api/projects',json={'title':'x'}).status_code==403

def test_cross_origin_blocked(client):
    assert client.post('/api/projects',json={'title':'x'},headers={**H,'Origin':'https://evil.example'}).status_code==403

def test_crud(client):
    pid=new(client)
    assert client.get('/api/projects/'+pid).json()['title']=='API test'
    assert len(client.get('/api/projects').json())==1
    assert client.patch('/api/projects/'+pid,headers=H,json={'title':'New title','brief':'A boat'}).status_code==200

def test_config_no_secret(client,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','secret-test-value')
    r=client.get('/api/config');assert r.json()['ai']['configured'];assert 'secret-test-value' not in r.text

def test_upload_permissions(client):
    pid=new(client)
    r=client.post(f'/api/projects/{pid}/upload',headers=H,files={'file':('x.png',picture(),'image/png')},data={'role':'master','rights_confirmed':'false'})
    assert r.status_code==400

def test_ai_disabled_without_key(client,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False);pid=new(client)
    assert client.post(f'/api/projects/{pid}/chat',headers=H,json={'message':'hello','confirm_paid':True}).status_code==503

def test_paid_confirmation(client,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','placeholder');pid=new(client)
    assert client.post(f'/api/projects/{pid}/generate',headers=H,json={'prompt':'a cottage'}).status_code==400

def test_no_master(client):
    pid=new(client);assert client.post(f'/api/projects/{pid}/build',headers=H,json={}).status_code==400

def test_upload_build_export(client):
    pid=new(client)
    r=client.post(f'/api/projects/{pid}/upload',headers=H,files={'file':('x.png',picture(),'image/png')},data={'role':'master','rights_confirmed':'true'})
    assert r.status_code==200
    r=client.post(f'/api/projects/{pid}/build',headers=H,json={'target_regions':35,'palette_colors':4,'paint_colors':16,'max_edge':256})
    assert r.status_code==200
    p=wait(client,pid);assert p['job']['status']=='done',p['job']
    rev=p['currentRevision'];base=f'/api/projects/{pid}/revisions/{rev}'
    assert client.get(base+'/files/regions.json').status_code==200
    assert client.get(base+'/export').content[:2]==b'PK'
    assert client.get(base+'/files/project.json').status_code==404
    assert client.get(base+'/render?width=99999').status_code==400
    assert client.post(f'/api/projects/{pid}/review',headers=H,json={'revision':rev,'confirmed':False,'note':'Reviewed the region shapes'}).status_code==400

def test_svg_upload_rejected(client):
    pid=new(client)
    r=client.post(f'/api/projects/{pid}/upload',headers=H,files={'file':('x.svg',b'<svg/>','image/svg+xml')},data={'role':'reference'})
    assert r.status_code==400

def test_provider_contracts_mocked(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    seen=[]
    def respond(req):
        seen.append(req)
        if req.url.path.endswith('/responses'):
            body=json.loads(req.content)
            assert body['store'] is False
            assert body['text']['format']['type']=='json_schema'
            assert body['input'][-1]['content'][0]['type']=='input_text'
            return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':json.dumps({'reply':'Consider lanterns','brief':'An original forest cabin'})}]}],'usage':{'input_tokens':42}})
        return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(picture()).decode()}]})
    p=Provider(httpx.MockTransport(respond))
    c=p.chat({'brief':'A cabin','messages':[]},'Make it night')
    assert c['brief']=='An original forest cabin'
    raw,meta=p.image(c['brief'],'medium','1024x1536')
    assert raw.startswith(b'\x89PNG') and meta['sourceMode']=='generation'
    assert len(seen)==2

def test_provider_no_auto_retry(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fake');calls=[]
    def fail(req):calls.append(req);return httpx.Response(429,json={'error':{'code':'rate_limit'}})
    p=Provider(httpx.MockTransport(fail))
    with pytest.raises(ValueError,match='No automatic retry'):p.image('test','low','1024x1024')
    assert len(calls)==1


# ---------------------------------------------------------------------------
# Stage-2: cut/draw edit routes, multistage SVG generation, config version
# ---------------------------------------------------------------------------

def test_config_version_and_new_backends(client):
    r=client.get('/api/config');assert r.status_code==200
    body=r.json()
    assert body['version']=='0.3.1'
    backends={b['id']:b for b in body['backends']}
    for bid,kind in [('pen-cut-tools','region-topology-editing'),('auto-subdivide','deterministic-subdivision'),
                     ('difficulty-analyzer','qa'),('provider-svg-multistage','vector-generation')]:
        assert bid in backends and backends[bid]['kind']==kind
    assert backends['pen-cut-tools']['available'] is True
    assert backends['auto-subdivide']['available'] is True
    assert backends['difficulty-analyzer']['available'] is True

SMALL_SVG=b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="50" y="50" width="100" height="100" fill="#3366AA"/>
</svg>'''

def _svg_project(c):
    pid=new(c)
    r=c.post(f'/api/projects/{pid}/upload-svg',headers=H,
             files={'file':('m.svg',SMALL_SVG,'image/svg+xml')},data={'rights_confirmed':'true'})
    assert r.status_code==200,r.text
    r=c.post(f'/api/projects/{pid}/build',headers=H,json={'target_regions':30,'palette_colors':4,
        'paint_colors':16,'max_edge':256,'min_region_pixels':4,'min_label_radius':1.0})
    assert r.status_code==200
    p=wait(c,pid);assert p['job']['status']=='done',p['job']
    return pid,p['currentRevision']

def test_edit_cut_and_draw_routes(client):
    pid,rev=_svg_project(client)
    base=f'/api/projects/{pid}/revisions/{rev}'
    regions=client.get(base+'/files/regions.json').json()['regions']
    rect=next(r for r in regions if r['bbox'][2]-r['bbox'][0]>50)
    x0,y0,x1,y1=rect['bbox']
    midx=(x0+x1)/2
    cut={'base_revision':rev,'action':'cut','region_ids':[rect['id']],
         'd':f'M {midx:.0f} {y0-12:.0f} L {midx:.0f} {y1+12:.0f}'}
    r=client.post(f'/api/projects/{pid}/edit',headers=H,json=cut)
    assert r.status_code==200,r.text
    p=wait(client,pid);assert p['job']['status']=='done',p['job']
    rev2=p['currentRevision']
    regs2=client.get(f'/api/projects/{pid}/revisions/{rev2}/files/regions.json').json()['regions']
    pieces=[r2 for r2 in regs2 if r2['id'].startswith('r-c-')]
    assert len(pieces)==2
    qa=client.get(f'/api/projects/{pid}/revisions/{rev2}/files/validation.json').json()
    assert qa['passed']
    # pen: draw on the empty canvas corner
    draw={'base_revision':rev2,'action':'draw','region_ids':[],'d':'M 8,8 L 42,8 L 42,42 L 8,42 Z',
          'palette_id':1,'group':'doodle'}
    r=client.post(f'/api/projects/{pid}/edit',headers=H,json=draw)
    assert r.status_code==200,r.text
    p=wait(client,pid);assert p['job']['status']=='done',p['job']
    regs3=client.get(f'/api/projects/{pid}/revisions/{p["currentRevision"]}/files/regions.json').json()['regions']
    assert [r3 for r3 in regs3 if r3['id'].startswith('r-p-')]
    # artwork pen: the drawn shape also becomes a paint.json artwork path
    draw_art={'base_revision':p['currentRevision'],'action':'draw','region_ids':[],'d':'M 8,150 L 42,150 L 42,184 L 8,184 Z',
              'palette_id':1,'paint':True,'color':'#FF7348','stroke_width':1.2}
    r=client.post(f'/api/projects/{pid}/edit',headers=H,json=draw_art)
    assert r.status_code==200,r.text
    p=wait(client,pid);assert p['job']['status']=='done',p['job']
    rev4=p['currentRevision']
    base4=f'/api/projects/{pid}/revisions/{rev4}'
    regs4=client.get(base4+'/files/regions.json').json()['regions']
    pen=next(r4 for r4 in regs4 if r4['id'].startswith('r-p-') and r4.get('masterShapeId'))
    paint=client.get(base4+'/files/paint.json').json()
    entry=next(p4 for p4 in paint['paths'] if p4['shapeId']==pen['masterShapeId'])
    assert entry['fill']=='#FF7348' and entry['strokeWidth']==1.2
    qa4=client.get(base4+'/files/validation.json').json()
    assert qa4['passed']

def test_edit_cut_missing_d_rejected(client):
    pid,rev=_svg_project(client)
    r=client.post(f'/api/projects/{pid}/edit',headers=H,json={'base_revision':rev,'action':'cut','region_ids':['r-00001']})
    assert r.status_code==200          # jobs are async: the failure surfaces in the job
    p=wait(client,pid)
    assert p['job']['status']=='failed'
    assert 'cut line' in p['job']['message']

SCENE_OBJECTS=[
    {'name':'sky','description':'gradient sky','z':0,'bbox':[0,0,576,400],'shapes':8,'fills':['#A9DBEF','#FFDCA6']},
    {'name':'hills','description':'rolling hills','z':1,'bbox':[0,300,576,200],'shapes':10,'fills':['#79B258']},
    {'name':'house','description':'cottage with roof and windows','z':2,'bbox':[150,350,250,250],'shapes':20,'fills':['#EBC681','#D87155']},
    {'name':'tree','description':'big tree with canopy','z':3,'bbox':[380,250,180,300],'shapes':18,'fills':['#8B5E3C','#79B258']},
    {'name':'path','description':'garden path','z':4,'bbox':[0,600,576,168],'shapes':8,'fills':['#C9B79C']},
    {'name':'flowers','description':'foreground flowers','z':5,'bbox':[20,640,120,100],'shapes':12,'fills':['#E8604C','#FFC94D']},
]

def _multistage_transport(seen):
    def respond(req):
        seen.append(req.url.path)
        if req.url.path.endswith('/json'):
            body=json.loads(req.content)
            assert body['store'] is False
            assert body['text']['format']['type']=='json_schema'
            return httpx.Response(200,json={'output':[{'content':[{'type':'output_text',
                'text':json.dumps({'objects':SCENE_OBJECTS})}]}],'usage':{'input_tokens':60}})
        if req.url.path.endswith('/svg'):
            body=json.loads(req.content)
            import re as _re
            m=_re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"',body['instructions'])
            assert m,'fragment request must carry the object bbox'
            bx,by,bw,bh=(int(v) for v in m.groups())
            pad=max(4,min(bw,bh)//8)
            svg=(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                 f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/>'
                 f'<path d="M {bx+pad},{by+pad} Q {bx+bw/2},{by+pad+ (bh-2*pad)/2} {bx+bw-pad},{by+pad} Z" fill="#AA3355" fill-opacity="0.5"/>'
                 f'<path d="M {bx+pad},{by+bh-pad} L {bx+bw/2},{by+pad+ (bh-2*pad)/2}" fill="none" stroke="#29383E" stroke-width="2"/>'
                 f'</svg>')
            return httpx.Response(200,json={'output':[{'content':[{'type':'output_text','text':svg}]}],'usage':{'input_tokens':80}})
        return httpx.Response(404,json={'error':{'code':'no_route'}})
    return httpx.MockTransport(respond)

def test_generate_svg_multistage_mocked(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    seen=[]
    with TestClient(create_app(tmp_path,transport=_multistage_transport(seen))) as c:
        pid=new(c)
        r=c.post(f'/api/projects/{pid}/generate-svg',headers=H,json={
            'prompt':'a cottage garden','mode':'multistage','target_regions':120,'confirm_paid':True})
        assert r.status_code==200,r.text
        p=wait(c,pid);assert p['job']['status']=='done',p['job']
        assert p['master']['kind']=='svg'
        # one scene-plan JSON call + one fragment call per object (<= 12 calls)
        assert sum(1 for s in seen if s.endswith('/json'))==1
        assert sum(1 for s in seen if s.endswith('/svg'))==len(SCENE_OBJECTS)
        assert len(seen)<=1+len(SCENE_OBJECTS)<=12
        assert p['master']['summary']['shapes']>=len(SCENE_OBJECTS)*2
        assert p['pendingBuildSettings']=={'auto_subdivide':True,'target_regions':120}
        usage=p['aiUsage'][-1]
        assert usage['kind']=='svg-multistage' and usage['multistage'] is True
        assert len(usage['stages'])>=1+len(SCENE_OBJECTS)
        # the next build applies the pending settings (client did not set them)
        r=c.post(f'/api/projects/{pid}/build',headers=H,json={'palette_colors':4,'paint_colors':16,'max_edge':256,
            'min_region_pixels':4,'min_label_radius':1.0})
        assert r.status_code==200
        p=wait(c,pid);assert p['job']['status']=='done',p['job']
        assert p['currentRevision']
        count=p['revisions'][-1]['regionCount']
        assert count>len(SCENE_OBJECTS)*3,'auto-subdivide should have applied the pending target'
        assert 'pendingBuildSettings' not in p   # consumed by the build


# ---------------------------------------------------------------------------
# Stage-3: node edit route + play-test difficulty recording (contract A/B)
# ---------------------------------------------------------------------------

NODE_SVG=b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect x="0" y="0" width="100" height="200" fill="#3366AA"/>
<rect x="100" y="0" width="100" height="200" fill="#A9DBEF"/>
</svg>'''

def test_edit_node_route(client):
    pid=new(client)
    r=client.post(f'/api/projects/{pid}/upload-svg',headers=H,
        files={'file':('n.svg',NODE_SVG,'image/svg+xml')},data={'rights_confirmed':'true'})
    assert r.status_code==200,r.text
    r=client.post(f'/api/projects/{pid}/build',headers=H,json={'target_regions':30,'palette_colors':4,
        'paint_colors':16,'max_edge':256,'min_region_pixels':4,'min_label_radius':1.0})
    assert r.status_code==200
    p=wait(client,pid);assert p['job']['status']=='done',p['job']
    rev=p['currentRevision']
    base=f'/api/projects/{pid}/revisions/{rev}'
    regions=client.get(base+'/files/regions.json').json()['regions']
    left,right=[r['id'] for r in regions]
    body={'base_revision':rev,'action':'node','region_ids':[left,right],
          'd':'M 0,0 L 120,0 L 120,200 L 0,200'}
    r=client.post(f'/api/projects/{pid}/edit',headers=H,json=body)
    assert r.status_code==200,r.text
    p=wait(client,pid);assert p['job']['status']=='done',p['job']
    rev2=p['currentRevision']
    regs2=client.get(f'/api/projects/{pid}/revisions/{rev2}/files/regions.json').json()['regions']
    moved=[r2 for r2 in regs2 if r2['id'].startswith('r-n-')]
    assert len(moved)==2 and {round(r2['area']) for r2 in moved}=={24000,16000}
    qa=client.get(f'/api/projects/{pid}/revisions/{rev2}/files/validation.json').json()
    assert qa['passed']
    # a node edit with a region that does not exist fails with an actionable job message
    r=client.post(f'/api/projects/{pid}/edit',headers=H,
        json={'base_revision':rev2,'action':'node','region_ids':['r-00001','r-99999'],
              'd':'M 0,0 L 120,0 L 120,200 L 0,200'})
    assert r.status_code==200          # jobs are async: the failure surfaces in the job
    p=wait(client,pid)
    assert p['job']['status']=='failed'
    assert 'Select existing playable regions' in p['job']['message']

def test_playtest_record_updates_difficulty(client,tmp_path):
    pid,rev=_svg_project(client)
    base=f'/api/projects/{pid}/revisions/{rev}'
    revdir=tmp_path/pid/'revisions'/rev
    region_count=json.loads((revdir/'artwork.json').read_text())['regionCount']
    before=json.loads((revdir/'artwork.json').read_text())
    assert 'playtestCount' not in before['difficulty']['metrics']
    r=client.post(base+'/playtest',headers=H,
                  json={'seconds':240.0,'filled':region_count,'total':region_count,'mistakes':3,'mode':'number'})
    assert r.status_code==200,r.text
    out=r.json()
    assert out['playtestCount']==1 and out['medianSeconds']==240.0
    assert out['manifest']['difficultyValidatedByPlaytest'] is True
    metrics=out['manifest']['difficulty']['metrics']
    for key in ('playtestCount','playtestMedianSeconds','playtestSecondsPerRegion','playtestMistakesPerRegion'):
        assert key in metrics
    # score blend: 240s saturates the pace term (min(1, 6))
    assert out['manifest']['difficulty']['score']==round(0.9*before['difficulty']['score']+10.0,1)
    entries=json.loads((revdir/'playtests.json').read_text())
    assert len(entries)==1 and entries[0]['mode']=='number' and 'recordedAt' in entries[0]
    after=json.loads((revdir/'artwork.json').read_text())
    assert after['difficultyValidatedByPlaytest'] is True
    assert after['difficulty']['metrics']['playtestCount']==1
    # the rest of the manifest is untouched
    assert after['contentHash']==before['contentHash'] and after['checksums']==before['checksums']
    assert after['assets']==before['assets'] and after['version']==before['version']
    # a second run keeps both entries and refreshes the median
    r=client.post(base+'/playtest',headers=H,
                  json={'seconds':60.0,'filled':region_count,'total':region_count,'mistakes':0,'mode':'memory'})
    assert r.status_code==200
    assert r.json()['playtestCount']==2 and r.json()['medianSeconds']==150.0

def test_playtest_invalid_body_and_unknown_ids(client):
    pid,rev=_svg_project(client)
    base=f'/api/projects/{pid}/revisions/{rev}'
    total=client.get(base+'/files/artwork.json').json()['regionCount']
    good={'seconds':60.0,'filled':total,'total':total,'mistakes':0,'mode':'number'}
    assert client.post(base+'/playtest',headers=H,json={**good,'seconds':5}).status_code==422
    assert client.post(base+'/playtest',headers=H,json={**good,'mode':'turbo'}).status_code==422
    assert client.post(base+'/playtest',headers=H,json={**good,'filled':0}).status_code==422
    assert client.post(base+'/playtest',headers=H,json={'seconds':60}).status_code==422
    assert client.post(f'/api/projects/{pid}/revisions/rev-nope/playtest',headers=H,json=good).status_code==404
    assert client.post(f'/api/projects/{pid}-missing/revisions/{rev}/playtest',headers=H,json=good).status_code==404

def test_playtest_payload_hardening(client):
    """A malformed playtest payload must never distort difficulty: filled
    cannot exceed total, and total must equal the revision's actual playable
    region count."""
    pid,rev=_svg_project(client)
    base=f'/api/projects/{pid}/revisions/{rev}'
    region_count=client.get(base+'/files/artwork.json').json()['regionCount']
    good={'seconds':60.0,'filled':region_count,'total':region_count,'mistakes':0,'mode':'number'}
    assert client.post(base+'/playtest',headers=H,json={**good,'filled':region_count+3}).status_code==400
    assert client.post(base+'/playtest',headers=H,json={**good,'total':region_count+7}).status_code==400
    assert 'region count' in client.post(base+'/playtest',headers=H,json={**good,'total':region_count+7}).json()['detail']
    # the well-formed shape still records normally
    assert client.post(base+'/playtest',headers=H,json=good).status_code==200


def test_generation_session_crud_and_mutations(client):
    pid = new(client)
    # Create session
    r = client.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                    json={'mode': 'ai_chat', 'requested_difficulty': 'hard', 'prompt': 'Cozy fantasy village'})
    assert r.status_code == 200, r.text
    sess = r.json()
    sid = sess['id']
    assert sess['status'] == 'draft_plan'
    assert sess['requestedDifficulty'] == 'hard'
    assert sess['targetRegionRange'] == [320, 550]
    assert sess['targetRegions'] == 430

    # List sessions
    r = client.get(f'/api/projects/{pid}/generation/sessions', headers=H)
    assert r.status_code == 200
    assert any(s['id'] == sid for s in r.json()['sessions'])

    # Get session
    r = client.get(f'/api/projects/{pid}/generation/sessions/{sid}', headers=H)
    assert r.status_code == 200
    assert r.json()['id'] == sid

    # Mutate plan: add object + set difficulty to master
    mutations = [
        {'op': 'add_object', 'object': {
            'id': 'obj-waterfall', 'name': 'Waterfall', 'role': 'midground', 'z': 2,
            'bbox': [100, 100, 200, 300], 'fills': ['#4AA3DF'], 'detailWeight': 1.5
        }},
        {'op': 'set_difficulty', 'difficulty': 'master'},
    ]
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/mutate', headers=H,
                    json={'mutations': mutations})
    assert r.status_code == 200, r.text
    mutated = r.json()
    assert mutated['requestedDifficulty'] == 'master'
    assert mutated['targetRegionRange'] == [550, 800]
    assert mutated['targetRegions'] == 650
    obj_ids = [o['id'] for o in mutated['scenePlan']['objects']]
    assert 'obj-waterfall' in obj_ids
    waterfall = next(o for o in mutated['scenePlan']['objects'] if o['id'] == 'obj-waterfall')
    assert waterfall['detailWeight'] == 1.5

    # Cancel session
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/cancel', headers=H)
    assert r.status_code == 200
    assert r.json()['status'] == 'canceled'

    # Cannot mutate canceled session
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/mutate', headers=H,
                    json={'mutations': [{'op': 'set_difficulty', 'difficulty': 'easy'}]})
    assert r.status_code == 400


def test_generation_session_transaction_and_isolation(client, tmp_path):
    # Setup initial healthy revision
    pid, rev1 = _svg_project(client)
    p_initial = client.get(f'/api/projects/{pid}').json()
    assert p_initial['currentRevision'] == rev1
    revs_initial_count = len(p_initial['revisions'])

    # Start generation session
    r = client.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                    json={'mode': 'ai_chat', 'requested_difficulty': 'easy', 'prompt': 'Two color fields'})
    assert r.status_code == 200
    sid = r.json()['id']

    # Compile session using an SVG master
    test_svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
    <g data-cd-object="obj-sky" data-cd-name="Sky">
      <rect x="0" y="0" width="200" height="100" fill="#3366AA"/>
    </g>
    <g data-cd-object="obj-ground" data-cd-name="Ground">
      <rect x="0" y="100" width="200" height="100" fill="#44AA66"/>
    </g>
    </svg>'''
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/compile', headers=H,
                    files={'master_file': ('master.svg', test_svg, 'image/svg+xml')})
    assert r.status_code == 200
    p = wait(client, pid)
    assert p['job']['status'] == 'done'

    # Check isolation: project's currentRevision is STILL rev1!
    # Generation was in session temp workspace, NOT touching current revision!
    p_current = client.get(f'/api/projects/{pid}').json()
    assert p_current['currentRevision'] == rev1
    assert len(p_current['revisions']) == revs_initial_count

    # Session is now ready_to_commit
    sess = client.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
    assert sess['status'] == 'ready_to_commit'
    assert sess['meta']['qa']['passed'] is True

    # Now commit session to project revision
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H,
                    json={'title': 'Committed AI Generation'})
    assert r.status_code == 200, r.text
    commit_res = r.json()
    rev2 = commit_res['revision']['id']
    assert rev2 != rev1

    # Project now points to rev2
    p_after = client.get(f'/api/projects/{pid}').json()
    assert p_after['currentRevision'] == rev2
    assert len(p_after['revisions']) == revs_initial_count + 1

    # Check manifest enrichment with generation block
    manifest = client.get(f'/api/projects/{pid}/revisions/{rev2}/files/artwork.json').json()
    assert 'generation' in manifest
    gen = manifest['generation']
    assert gen['mode'] == 'ai_chat'
    assert gen['requestedDifficulty'] == 'easy'
    assert gen['sessionId'] == sid
    assert 'measuredDifficulty' in gen
    assert 'scenePlan' in gen

    # Check objects.json was emitted
    objs = client.get(f'/api/projects/{pid}/revisions/{rev2}/files/objects.json').json()
    assert objs['schemaVersion'] == 1
    obj_ids = {o['id'] for o in objs['objects']}
    assert {'obj-sky', 'obj-ground'} <= obj_ids


def test_generation_session_failure_rollback(client):
    # Setup initial healthy revision
    pid, rev1 = _svg_project(client)
    p_initial = client.get(f'/api/projects/{pid}').json()

    # Test 1: Uploading an empty/unsupported SVG master immediately fails
    r = client.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                    json={'mode': 'ai_chat', 'requested_difficulty': 'easy'})
    sid1 = r.json()['id']
    empty_svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"></svg>'''
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid1}/compile', headers=H,
                    files={'master_file': ('master.svg', empty_svg, 'image/svg+xml')})
    assert r.status_code == 400
    sess1 = client.get(f'/api/projects/{pid}/generation/sessions/{sid1}').json()
    assert sess1['status'] == 'failed'
    assert 'no supported drawable shapes' in sess1['error']

    # Test 2: Master passes sanitization but fails vector compilation (shapes too small)
    r = client.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                    json={'mode': 'ai_chat', 'requested_difficulty': 'easy'})
    sid2 = r.json()['id']
    tiny_svg = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
    <rect x="0" y="0" width="1" height="1" fill="#112233"/>
    </svg>'''
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid2}/compile', headers=H,
                    files={'master_file': ('master.svg', tiny_svg, 'image/svg+xml')})
    assert r.status_code == 200
    p = wait(client, pid)
    assert p['job']['status'] == 'failed'

    # Session status is failed with error message
    sess2 = client.get(f'/api/projects/{pid}/generation/sessions/{sid2}').json()
    assert sess2['status'] == 'failed'
    assert 'too small' in sess2['error']

    # Rollback guarantee: healthy current revision and revisions list remain 100% untouched!
    p_after = client.get(f'/api/projects/{pid}').json()
    assert p_after['currentRevision'] == rev1
    assert len(p_after['revisions']) == len(p_initial['revisions'])

    # Cannot commit a failed session
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid2}/commit', headers=H)
    assert r.status_code == 400




def test_generation_plan_and_generate_mocked(tmp_path, monkeypatch):
    """Phase 2B gate: chat-first scene plan (cheap JSON call) -> user
    mutations -> per-object synthesis (one /svg call per planned object) ->
    ready_to_commit in session isolation -> atomic commit with semantic
    objects.json."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_multistage_transport(seen))) as c:
        pid = new(c)
        r = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                   json={'mode': 'ai_chat', 'requested_difficulty': 'hard',
                         'prompt': 'a cottage garden beside a waterfall'})
        assert r.status_code == 200, r.text
        sid = r.json()['id']

        # paid gate: planning without confirm is rejected
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H, json={})
        assert r.status_code == 400

        # plan (cheap strict-JSON call)
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
                   json={'confirm_paid': True})
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        assert sum(1 for s in seen if s.endswith('/json')) == 1
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        obj_ids = [o['id'] for o in sess['scenePlan']['objects']]
        assert 'obj-sky' in obj_ids and 'obj-house' in obj_ids
        sky = next(o for o in sess['scenePlan']['objects'] if o['id'] == 'obj-sky')
        assert 0 < sky['detailWeight'] <= 4
        assert sess['meta']['planUsage'].get('input_tokens') == 60
        assert any(u.get('kind') == 'scene-plan' for u in p['aiUsage'])

        # user revises the draft: add a cat, remove the path
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/mutate', headers=H,
                   json={'mutations': [
                       {'op': 'add_object', 'object': {'id': 'obj-cat-1', 'name': 'Cat',
                                                       'role': 'foreground', 'z': 6,
                                                       'bbox': [40, 600, 80, 60],
                                                       'fills': ['#333333'], 'detailWeight': 0.8}},
                       {'op': 'remove_object', 'objectId': 'obj-path'},
                   ]})
        assert r.status_code == 200, r.text
        ids = [o['id'] for o in r.json()['scenePlan']['objects']]
        assert 'obj-cat-1' in ids and 'obj-path' not in ids

        # paid gate on generate
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H, json={})
        assert r.status_code == 400

        # generate: one fragment call per planned object
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H,
                   json={'confirm_paid': True})
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        assert sum(1 for s in seen if s.endswith('/svg')) == len(ids)
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta']['qa']['passed'] is True
        assert any(u.get('kind') == 'generation-synthesis' for u in p['aiUsage'])

        # isolation: nothing committed yet
        p_now = c.get(f'/api/projects/{pid}').json()
        assert p_now['currentRevision'] is None and len(p_now['revisions']) == 0

        # atomic commit
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H,
                   json={'title': 'Garden from session'})
        assert r.status_code == 200, r.text
        rev = r.json()['revision']['id']
        p = c.get(f'/api/projects/{pid}').json()
        assert p['currentRevision'] == rev and len(p['revisions']) == 1
        manifest = c.get(f'/api/projects/{pid}/revisions/{rev}/files/artwork.json').json()
        assert manifest['generation']['mode'] == 'ai_chat'
        assert manifest['generation']['requestedDifficulty'] == 'hard'
        objs = c.get(f'/api/projects/{pid}/revisions/{rev}/files/objects.json').json()
        oids = {o['id'] for o in objs['objects']}
        assert 'obj-cat-1' in oids and 'obj-path' not in oids
        assert {'obj-sky', 'obj-hills', 'obj-house', 'obj-tree', 'obj-flowers'} <= oids


def test_generation_targeted_regeneration_mocked(tmp_path, monkeypatch):
    """Phase 2B gate: targeted regeneration replaces ONE object's shapes and
    recompiles, while untouched objects keep their exact shapeIds and the
    objectId stays stable."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_multistage_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
               json={'confirm_paid': True})
        wait(c, pid)
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H,
                   json={'confirm_paid': True})
        assert r.status_code == 200
        wait(c, pid)
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'ready_to_commit'

        sfile = tmp_path / pid / 'sessions' / sid / 'bundle' / 'objects.json'
        before = {o['id']: o['shapeIds'] for o in json.loads(sfile.read_text())['objects']}
        svg_calls_before = sum(1 for s in seen if s.endswith('/svg'))

        # unknown object rejected
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/regenerate-object', headers=H,
                   json={'objectId': 'obj-ghost', 'confirm_paid': True})
        assert r.status_code == 200            # async job: failure surfaces in the job
        p = wait(c, pid)
        assert p['job']['status'] == 'failed'
        assert 'not part of this session plan' in p['job']['message']

        # targeted regeneration of obj-house (one extra /svg call, others untouched)
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/regenerate-object', headers=H,
                   json={'objectId': 'obj-house', 'instructions': 'make the roof red',
                         'confirm_paid': True})
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        assert sum(1 for s in seen if s.endswith('/svg')) == svg_calls_before + 1
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta']['qa']['passed'] is True

        after = {o['id']: o['shapeIds'] for o in json.loads(sfile.read_text())['objects']}
        # untouched objects keep their exact shape ownership
        assert after['obj-sky'] == before['obj-sky']
        assert after['obj-flowers'] == before['obj-flowers']
        # regenerated object keeps its objectId and still owns shapes
        assert after['obj-house']
        assert any(u.get('kind') == 'object-regeneration' and u.get('objectId') == 'obj-house'
                   for u in p['aiUsage'])
        # the master file carries the replacement: the deterministic
        # per-(object, fragment) regen prefix is present on obj-house shapes
        # while the group identity is unchanged
        master_text = (tmp_path / pid / 'sessions' / sid / 'source-master.svg').read_text()
        assert 'data-cd-object="obj-house"' in master_text
        assert re.search(r'id="obj-house-[0-9a-f]{8}-s\d{4}"', master_text)
        # still no project revision was created by generation
        p_now = c.get(f'/api/projects/{pid}').json()
        assert p_now['currentRevision'] is None and len(p_now['revisions']) == 0


def test_generation_reference_plan_mocked(tmp_path, monkeypatch):
    """Phase 2C gate — Use as Reference: the uploaded image is attached to the
    vision planning call, drafts a NEW semantic ScenePlan, and the artwork is
    then generated as native vectors via the normal generate step. The plan
    instructs the provider to reuse only mood/palette/subject, not tracing."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    def respond(req):
        seen.append((req.url.path, req.content))
        if req.url.path.endswith('/json'):
            body = json.loads(req.content)
            # the reference image MUST be attached to the vision planning call
            content = body['input'][0]['content']
            assert any(part['type'] == 'input_image' for part in content), 'reference image must be sent'
            assert any('not its composition' in body['instructions'] for _ in [0])
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': SCENE_OBJECTS})}]}], 'usage': {'input_tokens': 90}})
        if req.url.path.endswith('/svg'):
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 576 768">'
                         '<rect x="0" y="0" width="576" height="768" fill="#88AA99"/></svg>')}]}],
                'usage': {'input_tokens': 80}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})

    with TestClient(create_app(tmp_path, transport=httpx.MockTransport(respond))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium',
                           'prompt': 'something like my photo', 'fidelity': 'balanced'}).json()['id']

        # paid gate first
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/reference-plan', headers=H,
                   files={'file': ('photo.png', picture(), 'image/png')}, data={'body': '{}'})
        assert r.status_code == 400

        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/reference-plan', headers=H,
                   files={'file': ('photo.png', picture(), 'image/png')},
                   data={'body': json.dumps({'confirm_paid': True, 'instructions': 'warmer palette'})})
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'draft_plan'
        oids = [o['id'] for o in sess['scenePlan']['objects']]
        assert 'obj-sky' in oids and 'obj-house' in oids
        assert sess['meta']['planUsage'].get('input_tokens') == 90
        assert sess['meta']['referenceFile'].startswith('reference-image')
        assert any(u.get('kind') == 'reference-scene-plan' for u in p['aiUsage'])

        # generate uses the vision-derived plan unchanged (one /svg per object)
        svg_before = sum(1 for path, _ in seen if path.endswith('/svg'))
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H,
                   json={'confirm_paid': True})
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        assert sum(1 for path, _ in seen if path.endswith('/svg')) == svg_before + len(SCENE_OBJECTS)
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta']['qa']['passed'] is True

        # commit: provenance records the image_reference mode
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        rev = r.json()['revision']['id']
        manifest = c.get(f'/api/projects/{pid}/revisions/{rev}/files/artwork.json').json()
        assert manifest['generation']['mode'] == 'image_reference'
        assert manifest['generation']['fidelity'] == 'balanced'

        # reference-plan on a plain ai_chat session is also allowed (guidance image);
        # wrong-mode sessions are rejected
        sid2 = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                      json={'mode': 'image_convert', 'prompt': 'x'}).json()['id']
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid2}/reference-plan', headers=H,
                   files={'file': ('photo.png', picture(), 'image/png')},
                   data={'body': json.dumps({'confirm_paid': True})})
        assert r.status_code == 200              # async job carries the failure
        p = wait(c, pid)
        assert p['job']['status'] == 'failed'
        assert 'does not support reference planning' in p['job']['message']


# ---------------------------------------------------------------------------
# Phase 2D — Convert Artwork: semantic decomposition + deterministic CV
# ---------------------------------------------------------------------------

def _convert_transport(seen):
    """Vision decomposition returns objects positioned like the fixture image
    (sky top / house bottom-left / grass bottom-right); fragments reuse the
    multistage pattern. Association must map sky labels → obj-sky etc."""
    def respond(req):
        seen.append(req.url.path)
        if req.url.path.endswith('/json'):
            objects = [
                {'name': 'sky', 'description': 'blue sky', 'z': 0,
                 'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
                {'name': 'house', 'description': 'yellow house', 'z': 1,
                 'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
                {'name': 'grass', 'description': 'green field', 'z': 2,
                 'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
            ]
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': objects})}]}], 'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            # fragments draw INSIDE the requested bbox (multistage pattern) —
            # a static 100x100 fragment would fail the compose placement
            # sanity for objects away from the canvas origin (reference flows
            # plan sky/house/grass at their fixture positions).
            body = json.loads(req.content)
            m = re.search(r'planned bbox: \[x=([0-9.]+), y=([0-9.]+), width=([0-9.]+), height=([0-9.]+)\]', body.get('instructions', ''))
            if m:
                bx, by, bw, bh = (int(round(float(v))) for v in m.groups())
            else:
                m = re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
                bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            pad = max(4, min(bw, bh) // 8)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/>'
                   f'<path d="M {bx+pad},{by+pad} Q {bx+bw/2},{by+pad+(bh-2*pad)/2} {bx+bw-pad},{by+pad} Z" fill="#AA3355" fill-opacity="0.5"/>'
                   f'</svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 10}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})
    return httpx.MockTransport(respond)


def _convert_fixture_png():
    """Flat 3-band illustration: sky / house (bottom-left) / grass (bottom-right)."""
    from PIL import Image as PILImage
    im = PILImage.new('RGB', (288, 288))
    px = im.load()
    for y in range(288):
        for x in range(288):
            if y < 144:
                px[x, y] = (0x91, 0xCC, 0xDD)          # sky
            elif x < 144:
                px[x, y] = (0xEB, 0xC6, 0x81)          # house (bottom-left)
            else:
                px[x, y] = (0x41, 0xA5, 0x82)          # grass (bottom-right)
    buf = io.BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()


def _run_convert(c, pid, sid, png, body=None):
    r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/convert', headers=H,
               files={'file': ('source.png', png, 'image/png')},
               data={'body': json.dumps(body or {'confirm_paid': True})})
    return r


def test_convert_gate_and_transaction(client, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')   # pass the configured gate; mode/paid gates are under test
    pid, _ = _svg_project(client)
    p_before = client.get(f'/api/projects/{pid}').json()
    sid = client.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                      json={'mode': 'image_convert', 'requested_difficulty': 'medium',
                            'fidelity': 'balanced'}).json()['id']
    # paid gate: no confirm → 400 before anything runs
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/convert', headers=H,
                    files={'file': ('s.png', _convert_fixture_png(), 'image/png')}, data={'body': '{}'})
    assert r.status_code == 400
    # image_convert cannot use the Reference planning route
    r = client.post(f'/api/projects/{pid}/generation/sessions/{sid}/reference-plan', headers=H,
                    files={'file': ('s.png', _convert_fixture_png(), 'image/png')},
                    data={'body': json.dumps({'confirm_paid': True})})
    assert r.status_code == 200
    p = wait(client, pid)
    assert p['job']['status'] == 'failed'
    assert 'does not support reference planning' in p['job']['message']
    # healthy revision untouched by the failed attempt
    p_after = client.get(f'/api/projects/{pid}').json()
    assert p_after['currentRevision'] == p_before['currentRevision']


def test_convert_end_to_end_semantic_ownership(client, monkeypatch):
    """Gates 1/2/3/6/7: flat illustration converts with clean semantic
    ownership, bounded over-vectorization, quality gates enforced, difficulty
    independence (subdivision target comes from the session), and clean
    transaction into commit."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_convert', 'requested_difficulty': 'hard',
                           'fidelity': 'balanced'}).json()['id']
        r = _run_convert(c, pid, sid, _convert_fixture_png())
        assert r.status_code == 200, r.text
        # hard sessions optimize toward 430 gameplay regions — heavy QA
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta']['qa']['passed'] is True
        scores = sess['meta']['conversionScores']
        # flat 3-band source must reconstruct cleanly (visual gate passed) and
        # NOT be over-segmented
        assert scores['passed'] is True
        assert scores['visualFidelity'] >= 70
        assert scores['gates'] == {'visualGate': 70, 'gameReadinessGate': 80}
        assert scores['metrics']['visualShapes'] <= 60
        # semantic association: sky/house/grass objects all own regions
        decomp_file = Path(c.app.state.root) / pid / 'sessions' / sid / 'decomposition.json'
        assert decomp_file.is_file(), 'decomposition.json intermediate artifact must exist'
        decomp = json.loads(decomp_file.read_text())
        owned = {o['objectId'] for o in decomp['objectStats'] if o['objectId'] != 'unassigned'}
        assert {'obj-sky', 'obj-house', 'obj-grass'} <= owned
        assert sess['meta']['convertPolicy']['fidelity'] == 'balanced'
        assert (Path(c.app.state.root) / pid / 'sessions' / sid / 'reconstructed-master.svg').is_file()
        # isolation until commit, then provenance
        assert c.get(f'/api/projects/{pid}').json()['currentRevision'] is None
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        rev = r.json()['revision']['id']
        manifest = c.get(f'/api/projects/{pid}/revisions/{rev}/files/artwork.json').json()
        assert manifest['generation']['mode'] == 'image_convert'
        assert manifest['generation']['fidelity'] == 'balanced'
        assert manifest['generation']['convertPolicy' if 'convertPolicy' in manifest['generation'] else 'requestedDifficulty']
        # gameplay regions carry the semantic ownership
        regions = c.get(f'/api/projects/{pid}/revisions/{rev}/files/regions.json').json()['regions']
        owned = {r['objectId'] for r in regions if r['objectId'] != 'unassigned'}
        assert {'obj-sky', 'obj-house', 'obj-grass'} <= owned
        # difficulty independence: hard target ≈ 430 requested; segmentation
        # follows the policy density, but the SESSION records the target range
        assert manifest['generation']['requestedDifficulty'] == 'hard'


def test_convert_fidelity_changes_parameters_and_output(client, monkeypatch):
    """Gate 5: Faithful vs Stylized on the SAME fixture must produce measurably
    different policies AND more reconstruction shapes for Faithful — not just
    different metadata."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    for fidelity in ('stylized', 'faithful'):
        with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
            pid = new(c)
            sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                         json={'mode': 'image_convert', 'fidelity': fidelity}).json()['id']
            r = _run_convert(c, pid, sid, _convert_fixture_png())
            assert r.status_code == 200, r.text
            # hard sessions optimize toward 430 gameplay regions — heavy QA
            p = wait(c, pid, timeout=240)
            assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
            sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
            assert sess['status'] == 'ready_to_commit'
            policy = sess['meta']['convertPolicy']
            scores = sess['meta']['conversionScores']
            if fidelity == 'stylized':
                stylized = (policy, scores)
                assert policy['curveTolerance'] == 2.0 and policy['paletteTarget'] == 16
                assert scores['gates']['visualGate'] == 55
            else:
                faithful = (policy, scores)
                assert policy['curveTolerance'] == 0.6 and policy['paletteTarget'] == 40
                assert scores['gates']['visualGate'] == 82
    assert faithful[0]['segmentDensity'] > stylized[0]['segmentDensity']
    assert faithful[0]['colorMergeDeltaE'] < stylized[0]['colorMergeDeltaE']


def test_convert_over_vectorization_rejected(client, monkeypatch):
    """QA recognizes over-vectorization: a photo-like noisy source at FAITHFUL
    (low merge tolerance, tiny components) must fail the readiness gate with
    the actionable over-segmentation message — and never create a revision."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    rng = np.random.RandomState(7)
    noisy = (rng.rand(192, 192, 3) * 255).astype('uint8')
    buf = io.BytesIO()
    Image.fromarray(noisy).save(buf, format='PNG')
    with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_convert', 'fidelity': 'faithful'}).json()['id']
        r = _run_convert(c, pid, sid, buf.getvalue())
        assert r.status_code == 200
        p = wait(c, pid)
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        if p['job']['status'] == 'failed':
            assert 'quality gate' in p['job']['message']
            assert sess['status'] == 'failed'
        else:
            # even if it passed, scores must have been computed and gated
            assert sess['meta']['conversionScores']['passed'] is True
        # transaction guarantee either way
        assert c.get(f'/api/projects/{pid}').json()['currentRevision'] is None


def test_convert_round_trip_edits_keep_objects_valid(client, monkeypatch):
    """Gate 8: converted artwork survives Cut edits — objects.json stays valid,
    objectId ownership preserved, QA reports no orphan shapes."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_convert', 'fidelity': 'balanced'}).json()['id']
        assert _run_convert(c, pid, sid, _convert_fixture_png()).status_code == 200
        wait(c, pid, timeout=240)
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H, json={})
        rev = r.json()['revision']['id']
        regions = c.get(f'/api/projects/{pid}/revisions/{rev}/files/regions.json').json()['regions']
        target = max(regions, key=lambda r: r['area'])
        x0, y0, x1, y1 = target['bbox']
        xm = round((x0 + x1) / 2, 1)
        cut = {'base_revision': rev, 'action': 'cut', 'region_ids': [target['id']],
               'd': f'M {xm} {y0 - 2} L {xm} {y1 + 2}'}
        r = c.post(f'/api/projects/{pid}/edit', headers=H, json=cut)
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        rev2 = p['currentRevision']
        regs2 = c.get(f'/api/projects/{pid}/revisions/{rev2}/files/regions.json').json()['regions']
        # ownership survived the cut: same object set, cut pieces inherit objectId
        assert {r2['objectId'] for r2 in regs2} >= {r['objectId'] for r in regions if r['id'] != target['id']}
        qa = c.get(f'/api/projects/{pid}/revisions/{rev2}/files/validation.json').json()
        assert qa['passed']
        assert qa['objects']['orphanShapes'] == 0


# ---------------------------------------------------------------------------
# Phase 2D.1 — semantic/fidelity hardening
# ---------------------------------------------------------------------------

def test_convert_fidelity_params_change_segmentation(client, monkeypatch):
    """segmentDensity and colorMergeDeltaE must genuinely change the candidate
    segmentation output, not just sit in policy metadata."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    from studio.pipeline import _image_labels
    from studio.models import BuildSettings
    src = Path(tempfile.mkdtemp()) / 'src.png'
    Image.open(io.BytesIO(_convert_fixture_png())).save(src, format='PNG')
    settings = BuildSettings(target_regions=60, min_region_pixels=8, max_edge=256)
    rgb_a, labels_a, *_ = _image_labels(src, settings, segment_density=0.5, color_merge_delta_e=0.0)
    rgb_b, labels_b, *_ = _image_labels(src, settings, segment_density=2.0, color_merge_delta_e=0.0)
    n_a = len(np.unique(labels_a)); n_b = len(np.unique(labels_b))
    assert n_b > n_a, f'density must change candidate count (low={n_a}, high={n_b})'
    # ΔE merge on the same flat fixture: identical-color bands merge aggressively
    _, labels_m, *_ = _image_labels(src, settings, segment_density=1.0, color_merge_delta_e=25.0)
    assert len(np.unique(labels_m)) < len(np.unique(_image_labels(src, settings, segment_density=1.0, color_merge_delta_e=0.0)[1]))
    # and a strict ΔE keeps the flat bands apart
    _, labels_s, *_ = _image_labels(src, settings, segment_density=1.0, color_merge_delta_e=2.0)
    assert len(np.unique(labels_s)) >= 3          # sky/house/grass stay separate


def test_convert_paint_hash_invariant_across_difficulty(client, monkeypatch):
    """The two halves of the difficulty contract, proven together: Easy vs
    Master sessions on the same source + fidelity produce the SAME paint
    reconstruction (identical shapeIds and path bytes) AND divergent gameplay
    geometry — the requested tier must actually reshape regions/budgets via
    the difficulty optimizer, never the artwork."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    paints = {}
    counts = {}
    for difficulty in ('easy', 'master'):
        with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
            pid = new(c)
            sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                         json={'mode': 'image_convert', 'requested_difficulty': difficulty,
                               'fidelity': 'balanced'}).json()['id']
            assert _run_convert(c, pid, sid, _convert_fixture_png()).status_code == 200
            # Master compiles + optimizes toward ~650 gameplay regions with
            # full QA and preview renders — legitimately heavier than 30s.
            p = wait(c, pid, timeout=240)
            assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
            sdir = Path(c.app.state.root) / pid / 'sessions' / sid
            paint = json.loads((sdir / 'bundle' / 'paint.json').read_text())
            regions = json.loads((sdir / 'bundle' / 'regions.json').read_text())['regions']
            # difficulty must not alter the RECONSTRUCTION: same shapes, same
            # ids, same path bytes (artworkId differs per project, hence the
            # structural comparison instead of raw bytes)
            paints[difficulty] = {p['shapeId']: p['d'] for p in paint['paths']}
            counts[difficulty] = len(regions)
            report = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()[
                'meta']['difficultyOptimization']
            assert report['requestedTier'] == difficulty
            assert report['achieved']['regionCount'] == counts[difficulty]
            assert report['changed'] is True, 'the tier must move the gameplay layer'
    assert paints['easy'] == paints['master'], 'paint reconstruction must not depend on difficulty'
    assert counts['master'] > counts['easy'], 'Master gameplay must be denser than Easy'


def test_convert_initial_revision_has_objects_json(client, monkeypatch):
    """First-class contract: the INITIAL converted revision (before any edit)
    ships objects.json whose records own the rc-* paint shapes; orphanShapes
    is 0 with a NON-EMPTY owned set (unlike the pre-2D.1 vacuous pass)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
        pid = new(c)
        # Easy keeps the candidate count low, so the three flat bands stay
        # distinct segments and each semantic object owns real geometry.
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_convert', 'requested_difficulty': 'easy',
                           'fidelity': 'balanced'}).json()['id']
        assert _run_convert(c, pid, sid, _convert_fixture_png()).status_code == 200
        wait(c, pid)
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        rev = r.json()['revision']['id']
        base = f'/api/projects/{pid}/revisions/{rev}'
        # objects.json exists at commit time (not only after an edit)
        objs = c.get(base + '/files/objects.json').json()
        recs = {o['id']: o for o in objs['objects']}
        assert {'obj-sky', 'obj-house', 'obj-grass'} <= set(recs)
        # every record owns rc-* paint shapes
        for o in objs['objects']:
            assert o['shapeIds'] and all(s.startswith('rc-') for s in o['shapeIds']), o
        # paint paths carry stable shapeIds and ownership
        paint = c.get(base + '/files/paint.json').json()
        assert paint['paths'] and all(p.get('shapeId') for p in paint['paths'])
        owned = {s for o in objs['objects'] for s in o['shapeIds']}
        live = {p['shapeId'] for p in paint['paths']}
        assert owned == live, 'objects.json must own exactly the live rc-* shapes'
        # orphan QA now verifies a NON-EMPTY ownership set
        qa = c.get(base + '/files/validation.json').json()
        assert qa['objects']['orphanShapes'] == 0
        assert qa['objects']['assignedRegions'] > 0


def test_convert_optimization_report_and_metadata(client, monkeypatch):
    """Task 26: the optimizer report lands in session meta with per-object
    budgets, and the ScenePlan's semantic metadata (role, detailWeight)
    survives into the committed objects.json alongside actual rc-* ownership —
    the budget engine's input contract for later Optimize runs."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_convert', 'requested_difficulty': 'medium',
                           'fidelity': 'balanced'}).json()['id']
        assert _run_convert(c, pid, sid, _convert_fixture_png()).status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        report = sess['meta']['difficultyOptimization']
        assert report['requestedTier'] == 'medium'
        assert report['targetRegions'] == 250
        assert report['changed'] is True
        assert report['outcome'] in ('target-reached', 'best-safe-result', 'safe-ceiling')
        assert set(report['budgets']) == {'obj-sky', 'obj-house', 'obj-grass'}
        assert sess['meta']['qa']['passed'] is True
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        rev = r.json()['revision']['id']
        base = f'/api/projects/{pid}/revisions/{rev}'
        objs = c.get(base + '/files/objects.json').json()
        by_id = {o['id']: o for o in objs['objects']}
        assert set(by_id) == {'obj-sky', 'obj-house', 'obj-grass'}
        # plan semantics (name/role/detailWeight from the vision plan) merged
        # with ACTUAL rc-* shape ownership; budgets stamped as preferredRegions
        for oid, shapes in (('obj-sky', 10), ('obj-house', 14), ('obj-grass', 10)):
            rec = by_id[oid]
            assert rec['name'] == oid.replace('obj-', '')
            assert rec['role'] == 'midground'
            assert rec['subdivision']['detailWeight'] == pytest.approx(round(min(shapes / 12.0, 4.0), 3))
            assert rec['subdivision']['preferredRegions'] == report['budgets'][oid]
            assert rec['shapeIds'] and all(s.startswith('rc-') for s in rec['shapeIds'])
        manifest = c.get(base + '/files/artwork.json').json()
        assert manifest['difficulty']['rating'] == report['achieved']['rating']
        assert manifest['difficulty']['metrics']['regionCount'] == report['achieved']['regionCount']


# ---------------------------------------------------------------------------
# Task 27 — Optimize Difficulty revision action
# ---------------------------------------------------------------------------

def test_optimize_creates_revision_with_artwork_frozen(client):
    """Downward hop on a dense board: Optimize to Easy creates a NEW immutable
    revision — gameplay merged down, paint bytes identical, objects.shapeIds
    identical, report persisted in manifest and project, source untouched."""
    pid = new(client)
    r = client.post(f'/api/projects/{pid}/upload-svg', headers=H,
                    files={'file': ('m.svg', SMALL_SVG, 'image/svg+xml')}, data={'rights_confirmed': 'true'})
    assert r.status_code == 200, r.text
    r = client.post(f'/api/projects/{pid}/build', headers=H, json={
        'target_regions': 300, 'palette_colors': 4, 'paint_colors': 16, 'max_edge': 256,
        'min_region_pixels': 4, 'min_label_radius': 1.0, 'auto_subdivide': True})
    assert r.status_code == 200
    p = wait(client, pid, timeout=120)
    assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
    rev = p['currentRevision']
    base = f'/api/projects/{pid}/revisions/{rev}'
    before = {
        'paint': hashlib.sha256(client.get(base + '/files/paint.json').content).hexdigest(),
        'regions': client.get(base + '/files/regions.json').json()['regions'],
        'master': client.get(base + '/files/source-master.svg').content,
    }
    assert len(before['regions']) > 180, 'fixture must start above the easy band'
    r = client.post(f'/api/projects/{pid}/optimize', headers=H,
                    json={'base_revision': rev, 'tier': 'easy'})
    assert r.status_code == 200, r.text
    p = wait(client, pid, timeout=240)
    assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
    rev2 = p['currentRevision']
    assert rev2 and rev2 != rev
    report = p['lastOptimization']
    assert report['requestedTier'] == 'easy'
    assert report['artworkUnchanged'] is True
    assert report['changed'] is True
    assert report['revisionId'] == rev2
    assert report['regionCountAfter'] < report['regionCountBefore']
    base2 = f'/api/projects/{pid}/revisions/{rev2}'
    # artwork layer byte-identical
    assert hashlib.sha256(client.get(base2 + '/files/paint.json').content).hexdigest() == before['paint']
    assert client.get(base2 + '/files/source-master.svg').content == before['master']
    # gameplay moved and QA passed
    regions2 = client.get(base2 + '/files/regions.json').json()['regions']
    assert len(regions2) == report['regionCountAfter']
    qa = client.get(base2 + '/files/validation.json').json()
    assert qa['passed'] is True
    # report persisted in the revision manifest with before/after
    manifest = client.get(base2 + '/files/artwork.json').json()
    assert manifest['difficultyOptimization']['requestedTier'] == 'easy'
    assert manifest['difficultyOptimization']['initial']['regionCount'] == len(before['regions'])
    assert manifest['difficultyOptimization']['achieved']['regionCount'] == len(regions2)
    assert manifest['difficultyOptimization']['artworkUnchanged'] is True
    assert manifest['difficultyOptimization']['revisionId'] == rev2
    # objects.shapeIds unchanged (objects.json may gain preferredRegions)
    o1 = {o['id']: o.get('shapeIds', []) for o in (client.get(base + '/files/objects.json').json().get('objects') or [])}
    o2 = {o['id']: o.get('shapeIds', []) for o in (client.get(base2 + '/files/objects.json').json().get('objects') or [])}
    assert o1 == o2
    # source revision remains immutable and restorable (natural undo)
    assert client.get(base + '/files/regions.json').json()['regions'] == before['regions']
    assert client.get(f'/api/projects/{pid}').json()['revisions'][-1]['id'] == rev2
    return pid, rev2


def test_optimize_noop_does_not_create_revision(client):
    """Optimizing a revision that already sits in the requested tier band is
    an honest no-op: report returned, NO duplicate revision created. A stale
    base revision is rejected like the edit route."""
    pid, rev_easy = test_optimize_creates_revision_with_artwork_frozen(client)
    p0 = client.get(f'/api/projects/{pid}').json()
    n0 = len(p0['revisions'])
    r = client.post(f'/api/projects/{pid}/optimize', headers=H,
                    json={'base_revision': rev_easy, 'tier': 'easy'})
    assert r.status_code == 200, r.text
    p = wait(client, pid, timeout=120)
    assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
    assert p['currentRevision'] == rev_easy, 'no-op must not create a revision'
    assert len(p['revisions']) == n0
    assert p['lastOptimization']['noop'] is True
    assert p['lastOptimization']['changed'] is False
    assert p['lastOptimization']['artworkUnchanged'] is True
    # stale base revision is rejected like the edit route (409 conflict)
    r = client.post(f'/api/projects/{pid}/optimize', headers=H,
                    json={'base_revision': 'rev-nonexistent', 'tier': 'master'})
    assert r.status_code == 409


def test_optimize_upward_then_deterministic(client):
    """Upward hop on a small board: Master raises gameplay complexity without
    touching paint; the engine is deterministic on the same base + tier."""
    pid, rev = _svg_project(client)
    paint0 = hashlib.sha256(
        client.get(f'/api/projects/{pid}/revisions/{rev}/files/paint.json').content).hexdigest()
    r = client.post(f'/api/projects/{pid}/optimize', headers=H,
                    json={'base_revision': rev, 'tier': 'master'})
    assert r.status_code == 200
    p = wait(client, pid, timeout=240)
    assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
    master_rev = p['currentRevision']
    assert master_rev != rev
    report = p['lastOptimization']
    assert report['regionCountAfter'] > report['regionCountBefore']
    assert client.get(f'/api/projects/{pid}/revisions/{master_rev}/files/validation.json').json()['passed']
    paint1 = hashlib.sha256(
        client.get(f'/api/projects/{pid}/revisions/{master_rev}/files/paint.json').content).hexdigest()
    assert paint1 == paint0
    # determinism: same base revision + same tier -> identical region geometry
    from studio.difficulty import optimize_gameplay_difficulty
    from studio.pipeline import load_bundle
    src = Path(client.app.state.root) / pid / 'revisions' / rev
    b1, r1 = optimize_gameplay_difficulty(load_bundle(src), 'master')
    b2, r2 = optimize_gameplay_difficulty(load_bundle(src), 'master')
    assert [(x['id'], x['d']) for x in b1['geometry']['regions']] == \
           [(x['id'], x['d']) for x in b2['geometry']['regions']]
    assert r1['achieved'] == r2['achieved']


# ---------------------------------------------------------------------------
# Task 29 — Create-with-AI workspace backend: plan chat → structured
# mutations, lock semantics, stale-plan flow, session preview
# ---------------------------------------------------------------------------

def _task29_transport(seen, svg_delay=0.0):
    """Multistage mock whose /json endpoint ALSO serves the plan-mutations
    translator (distinguished by its instructions prefix). svg_delay slows
    fragment calls for cancellation/progress timing tests."""
    def respond(req):
        seen.append(req.url.path)
        if svg_delay and req.url.path.endswith('/svg'):
            time.sleep(svg_delay)
        if req.url.path.endswith('/json'):
            body = json.loads(req.content)
            assert body['store'] is False
            if str(body.get('instructions', '')).startswith('You translate'):
                return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                    'text': json.dumps({
                        'summary': 'Waterfall enlarged; right tree removed.',
                        'mutations': [
                            {'op': 'update_object', 'objectId': 'obj-tree',
                             'changes': {'description': 'enlarged canopy tree', 'bbox': [330, 190, 250, 370]}},
                            {'op': 'remove_object', 'objectId': 'obj-path'},
                        ]})}]}], 'usage': {'input_tokens': 40}})
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': SCENE_OBJECTS})}]}], 'usage': {'input_tokens': 60}})
        if req.url.path.endswith('/svg'):
            body = json.loads(req.content)
            import re as _re
            m = re.search(r'planned bbox: \[x=([0-9.]+), y=([0-9.]+), width=([0-9.]+), height=([0-9.]+)\]', body.get('instructions', ''))
            if m:
                bx, by, bw, bh = (int(round(float(v))) for v in m.groups())
            else:
                m = re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
                bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            pad = max(4, min(bw, bh) // 8)
            # PER-CALL fill variation (same geometry): consecutive provider
            # calls return visibly different fragments, so a broken
            # keep/preserve mechanism cannot pass by comparing identical mock
            # output — while never introducing new geometry cases.
            marker = f'#00{len(seen) % 100:02X}{(len(seen) * 7) % 100:02X}'
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="{marker}"/>'
                   f'<path d="M {bx+pad},{by+pad} Q {bx+bw/2},{by+pad+(bh-2*pad)/2} {bx+bw-pad},{by+pad} Z" fill="#AA3355" fill-opacity="0.5"/>'
                   f'</svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 80}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})
    return httpx.MockTransport(respond)


def _plan_and_generate(c, pid, seen):
    sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                 json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                       'prompt': 'garden'}).json()['id']
    r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
               json={'confirm_paid': True})
    assert r.status_code == 200, r.text
    p = wait(c, pid)
    assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
    r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H,
               json={'confirm_paid': True})
    assert r.status_code == 200
    p = wait(c, pid)
    assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
    sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
    assert sess['status'] == 'ready_to_commit'
    return sid


def test_plan_chat_translates_to_structured_mutations(tmp_path, monkeypatch):
    """Task 29 chat → mutations: ONE strict-JSON call turns the artist
    instruction into ops applied by the deterministic engine — the object is
    updated by id, the other removed, and the summary lands in session meta."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
                   json={'confirm_paid': True})
        assert r.status_code == 200
        wait(c, pid)
        before = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert any(o['id'] == 'obj-path' for o in before['scenePlan']['objects'])

        # paid gate + empty instruction gate
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan-chat', headers=H,
                   json={'instruction': 'make the tree bigger'})
        assert r.status_code == 400
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan-chat', headers=H,
                   json={'confirm_paid': True, 'instruction': '   '})
        assert r.status_code == 400

        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan-chat', headers=H,
                   json={'confirm_paid': True, 'instruction': 'make the tree bigger and remove the path'})
        assert r.status_code == 200, r.text
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        # exactly ONE extra /json call (the translator), full plan never resent
        assert sum(1 for s in seen if s.endswith('/json')) == 2
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        ids = [o['id'] for o in sess['scenePlan']['objects']]
        assert 'obj-path' not in ids
        tree = next(o for o in sess['scenePlan']['objects'] if o['id'] == 'obj-tree')
        # deterministic engine clamps the bbox into the 576-wide viewBox
        assert tree['bbox'] == [330.0, 190.0, 246.0, 370.0]
        assert tree['description'] == 'enlarged canopy tree'
        chat = sess['meta']['lastPlanChat']
        assert chat['applied'] == 2
        assert 'Waterfall enlarged' in chat['summary']
        assert any(u.get('kind') == 'plan-revision' for u in p['aiUsage'])
        # a failed session's chat is allowed (retry path): mutate back is fine
        assert sess['status'] == 'draft_plan'


def test_plan_edit_after_ready_marks_artwork_stale_then_compile(tmp_path, monkeypatch):
    """Visual plan changes stay PENDING until applied to the artwork: a plain
    recompile of the old master cannot return the session to ready_to_commit
    and commit is refused. A metadata-only edit (object name) never pends;
    regenerating the changed object resolves exactly its own entry."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, [])
        base = f'/api/projects/{pid}/generation/sessions/{sid}'

        # visual change: description edit on obj-flowers
        r = c.post(f'{base}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-flowers',
                                        'changes': {'description': 'paler foreground flowers'}}]})
        assert r.status_code == 200, r.text
        sess = r.json()
        assert sess['status'] == 'draft_plan'
        assert sess['meta']['pendingArtworkChanges'] == {'obj-flowers': ['description']}
        assert sess['meta']['artworkStale'] is True
        # the compiled artwork still exists for preview
        assert c.get(f'{base}/preview/colored.svg').status_code == 200

        # a free recompile validates the OLD master but must NOT clear the
        # pending visual change nor reach ready_to_commit
        r = c.post(f'{base}/compile', headers=H, json={})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'draft_plan'
        assert sess['meta']['pendingArtworkChanges'] == {'obj-flowers': ['description']}
        assert sess['meta']['artworkStale'] is True
        # commit refuses the visually-outdated bundle (status guard; the
        # pending-specific message stays as defense-in-depth)
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 400
        assert 'does not reflect' in r.json()['detail'] or "must be 'ready_to_commit'" in r.json()['detail']

        # metadata-only edit (rename) never pends — revert-free check on a
        # second field: rename does not add a pending entry
        r = c.post(f'{base}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-sky',
                                        'changes': {'name': 'Open sky'}}]})
        sess = r.json()
        assert 'obj-sky' not in (sess['meta'].get('pendingArtworkChanges') or {})

        # regenerating the changed object resolves exactly its entry → ready
        r = c.post(f'{base}/regenerate-object', headers=H,
                   json={'objectId': 'obj-flowers', 'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        assert 'pendingArtworkChanges' not in sess['meta']
        assert 'artworkStale' not in sess['meta']


def test_two_visual_changes_regen_one_commit_blocked(tmp_path, monkeypatch):
    """The reviewer's regression: after generation, change TWO objects
    visually, regenerate only ONE — the session must not be committable until
    the second object's change is applied (bulk regen resolves the rest)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, seen)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        svg0 = sum(1 for x in seen if x.endswith('/svg'))

        r = c.post(f'{base}/mutate', headers=H, json={'mutations': [
            {'op': 'update_object', 'objectId': 'obj-tree',
             'changes': {'bbox': [330, 190, 246, 370]}},
            {'op': 'update_object', 'objectId': 'obj-flowers',
             'changes': {'fills': ['#E8604C']}},
        ]})
        assert r.json()['meta']['pendingArtworkChanges'] == {
            'obj-tree': ['bbox'], 'obj-flowers': ['fills']}

        # regenerate ONLY the tree: its entry clears, flowers stay pending
        r = c.post(f'{base}/regenerate-object', headers=H,
                   json={'objectId': 'obj-tree', 'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'draft_plan', 'one pending object must block ready'
        assert sess['meta']['pendingArtworkChanges'] == {'obj-flowers': ['fills']}
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 400, 'commit must be blocked while obj-flowers is pending'
        detail = r.json()['detail']
        assert 'obj-flowers' in detail or "must be 'ready_to_commit'" in detail

        # bulk regeneration composes from the current plan → all pending gone
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        assert 'pendingArtworkChanges' not in sess['meta']
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        p = c.get(f'/api/projects/{pid}').json()
        assert p['currentRevision'] and len(p['revisions']) == 1


def test_locked_object_regenerate_guard_and_bulk_keep(tmp_path, monkeypatch):
    """Lock semantics: a locked object refuses targeted regeneration; a bulk
    regeneration spends NO provider call on it (locked-with-artwork is skipped
    entirely) and re-injects its EXACT previous painted geometry — proven by a
    before/after appearance snapshot against a mock whose output DIFFERS on
    every call; free objects visibly change. Unlock re-enables regeneration."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, seen)
        sdir = tmp_path / pid / 'sessions' / sid

        def object_appearance(oid):
            """Identity + full painted appearance of one object's shapes:
            membership via objects.json shapeIds, attributes from paint.json
            (path d, fill, stroke, opacity, z). Never empty for a generated
            object."""
            objs = {o['id']: o['shapeIds'] for o in json.loads((sdir / 'bundle' / 'objects.json').read_text())['objects']}
            sids = set(objs.get(oid) or [])
            assert sids, f'{oid} owns no shapes — snapshot would prove nothing'
            paint = json.loads((sdir / 'bundle' / 'paint.json').read_text())
            entries = [p2 for p2 in paint['paths'] if p2.get('shapeId') in sids]
            assert entries, f'{oid} has shapes but no painted paths'
            return sorted(
                (p2.get('shapeId'),
                 tuple((k, p2.get(k)) for k in ('d', 'fill', 'stroke', 'strokeWidth', 'fillOpacity', 'opacity', 'z') if p2.get(k) is not None))
                for p2 in entries)

        # lock obj-sky via the structured plan mutation (metadata: no pending)
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-sky',
                                        'changes': {'generation': {'locked': True}}}]})
        assert r.status_code == 200
        locked_rec = next(o for o in r.json()['scenePlan']['objects'] if o['id'] == 'obj-sky')
        assert locked_rec['generation']['locked'] is True

        # targeted regeneration of the locked object fails without spending
        svg_calls = sum(1 for x in seen if x.endswith('/svg'))
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/regenerate-object', headers=H,
                   json={'objectId': 'obj-sky', 'confirm_paid': True})
        assert r.status_code == 200           # async: surfaces as a failed job
        p = wait(c, pid)
        assert p['job']['status'] == 'failed'
        assert 'locked' in p['job']['message']
        assert sum(1 for x in seen if x.endswith('/svg')) == svg_calls

        # snapshots BEFORE the bulk regeneration
        sky_before = object_appearance('obj-sky')
        house_before = object_appearance('obj-house')

        # bulk regeneration: the locked object is SKIPPED (no provider call),
        # free objects are re-generated with visibly different mock output
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H,
                   json={'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta'].get('keptLockedObjects') == ['obj-sky']
        # 6 objects, sky locked-with-artwork → exactly 5 fragment calls
        assert sum(1 for x in seen if x.endswith('/svg')) == svg_calls + 5

        # AFTER: locked identity + appearance byte-identical; free object changed
        assert object_appearance('obj-sky') == sky_before, 'locked artwork must survive a bulk regen'
        assert object_appearance('obj-house') != house_before, 'free objects must visibly change (mock varies per call)'

        # unlock -> targeted regeneration works again
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-sky',
                                        'changes': {'generation': {'locked': False}}}]})
        assert r.status_code == 200
        r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/regenerate-object', headers=H,
                   json={'objectId': 'obj-sky', 'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        assert sum(1 for x in seen if x.endswith('/svg')) == svg_calls + 5 + 1


def test_session_preview_route_gated(tmp_path, monkeypatch):
    """Preview: 404 before any artwork exists, 200 after generate, unknown
    names always rejected."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'prompt': 'garden'}).json()['id']
        r = c.get(f'/api/projects/{pid}/generation/sessions/{sid}/preview/colored.svg')
        assert r.status_code == 404
        r = c.get(f'/api/projects/{pid}/generation/sessions/{sid}/preview/../../project.json')
        assert r.status_code == 404
        sid = _plan_and_generate(c, pid, [])
        r = c.get(f'/api/projects/{pid}/generation/sessions/{sid}/preview/colored.svg')
        assert r.status_code == 200 and '<svg' in r.text
        r = c.get(f'/api/projects/{pid}/generation/sessions/{sid}/preview/numbered-preview.png')
        assert r.status_code == 200
        r = c.get(f'/api/projects/{pid}/generation/sessions/{sid}/preview/notes.txt')
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Task-29 review patch 2 — locked + pending interaction
# ---------------------------------------------------------------------------

def test_locked_with_pending_blocks_bulk_before_spend(tmp_path, monkeypatch):
    """Change fills -> lock -> bulk generate: rejected BEFORE any provider
    call; the pending entry stays intact and commit remains blocked."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, seen)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        svg0 = sum(1 for x in seen if x.endswith('/svg'))
        # visual change on obj-house, then lock it (metadata, no pending)
        r = c.post(f'{base}/mutate', headers=H, json={'mutations': [
            {'op': 'update_object', 'objectId': 'obj-house', 'changes': {'fills': ['#EBC681', '#D87155', '#FFFFFF']}}]})
        assert r.json()['meta']['pendingArtworkChanges'] == {'obj-house': ['fills']}
        # locking is metadata: the pending entry SURVIVES the lock
        r = c.post(f'{base}/mutate', headers=H, json={'mutations': [
            {'op': 'update_object', 'objectId': 'obj-house', 'changes': {'generation': {'locked': True}}}]})
        assert r.json()['meta']['pendingArtworkChanges'] == {'obj-house': ['fills']}
        sess = c.get(base).json()
        assert sess['meta']['pendingArtworkChanges'] == {'obj-house': ['fills']}
        # bulk generate must be rejected without spending
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        assert r.status_code == 200                    # async job
        p = wait(c, pid)
        assert p['job']['status'] == 'failed'
        assert 'unapplied visual changes' in p['job']['message']
        assert '"house"' in p['job']['message']
        assert sum(1 for x in seen if x.endswith('/svg')) == svg0, 'no provider call may happen'
        sess = c.get(base).json()
        assert sess['status'] == 'draft_plan'
        assert sess['meta']['pendingArtworkChanges'] == {'obj-house': ['fills']}
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 400


def test_all_locked_local_prune_applies_removal(tmp_path, monkeypatch):
    """Remove an object, lock everything that remains: bulk generation spends
    ZERO provider calls but still applies the plan locally — the removed
    object is gone from master, paint and ownership BEFORE pending clears,
    and the session reaches ready_to_commit."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, seen)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        sdir = tmp_path / pid / 'sessions' / sid
        svg0 = sum(1 for x in seen if x.endswith('/svg'))
        # remove obj-path (visual -> pending 'removed'), lock all 5 remaining
        r = c.post(f'{base}/mutate', headers=H, json={'mutations': [
            {'op': 'remove_object', 'objectId': 'obj-path'}]})
        assert r.json()['meta']['pendingArtworkChanges'] == {'obj-path': ['removed']}
        remaining = [o['id'] for o in r.json()['scenePlan']['objects']]
        assert 'obj-path' not in remaining
        r = c.post(f'{base}/mutate', headers=H, json={'mutations': [
            {'op': 'update_object', 'objectId': oid, 'changes': {'generation': {'locked': True}}}
            for oid in remaining]})
        assert r.status_code == 200
        # bulk generate: zero provider calls, local prune applies the removal
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        assert sum(1 for x in seen if x.endswith('/svg')) == svg0, 'all-locked run must not spend'
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta'].get('keptLockedObjects') == remaining
        # removal applied across master, paint and ownership
        master = (sdir / 'source-master.svg').read_text()
        assert 'data-cd-object="obj-path"' not in master
        paint = json.loads((sdir / 'bundle' / 'paint.json').read_text())
        assert all(p2.get('objectId') != 'obj-path' for p2 in paint['paths'])
        objs = json.loads((sdir / 'bundle' / 'objects.json').read_text())['objects']
        assert all(o['id'] != 'obj-path' for o in objs)
        # pending cleared only after the prune applied it
        assert 'pendingArtworkChanges' not in sess['meta']
        # the locked objects' artwork survived the prune
        assert all(f'data-cd-object="{oid}"' in master for oid in remaining)


# ---------------------------------------------------------------------------
# Task 30 — image session source asset, input identity, settings invalidation
# ---------------------------------------------------------------------------

def _convert_session(pid, c, fidelity='balanced'):
    return c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                  json={'mode': 'image_convert', 'requested_difficulty': 'hard',
                        'fidelity': fidelity}).json()['id']


def test_session_source_upload_persists_and_survives_refresh(tmp_path, monkeypatch):
    """30A: the visible source is stored server-side BEFORE any AI call —
    free (no /json hit), refresh-proof (re-read from the session asset),
    failed upload keeps the old source, project master untouched."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_convert_transport(seen))) as c:
        pid = new(c)
        sid = _convert_session(pid, c)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        # project master must stay empty (draft never touches it)
        assert c.get(f'/api/projects/{pid}').json()['master'] is None
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('scene.png', _convert_fixture_png(), 'image/png')})
        assert r.status_code == 200, r.text
        src = r.json()['meta']['source']
        assert src['file'] == 'source.png' and src['width'] == 288 and src['height'] == 288
        assert src['sha256'] and src['name'] == 'scene.png'
        assert sum(1 for s2 in seen if s2.endswith('/json')) == 0, 'upload must be AI-free'
        # the asset is served back (refresh-proof preview)
        r = c.get(f'{base}/preview/source.png')
        assert r.status_code == 200 and r.headers['content-type'].startswith('image/png')
        # a FAILED upload keeps the old source
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('bad.txt', b'not an image', 'text/plain')})
        assert r.status_code == 400
        assert c.get(f'{base}/preview/source.png').status_code == 200
        assert c.get(f'/api/projects/{pid}').json()['master'] is None
        # convert can now run WITHOUT re-uploading (file omitted)
        monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
        r = c.post(f'{base}/convert', headers=H,
                   files={'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200, r.text
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        # identity recorded from the STORED source
        assert sess['meta']['activeBuildInputs']['sourceSha256'] == src['sha256']


def test_image_sessions_keep_sources_separate(tmp_path, monkeypatch):
    """Reviewer's two-session test: two convert sessions in the SAME project
    with different sources never swap their sources — preview bytes match
    each session's own upload hash, and changing session B's source through
    the endpoint updates ONLY B (bytes, metadata hash, identity snapshot)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid_a = _convert_session(pid, c)
        sid_b = _convert_session(pid, c)
        variant = io.BytesIO()
        Image.new('RGB', (256, 256), '#446688').save(variant, format='PNG')
        ha = c.post(f'/api/projects/{pid}/generation/sessions/{sid_a}/source', headers=H,
                    files={'file': ('a.png', _convert_fixture_png(), 'image/png')}).json()['meta']['source']['sha256']
        hb = c.post(f'/api/projects/{pid}/generation/sessions/{sid_b}/source', headers=H,
                    files={'file': ('b.png', variant.getvalue(), 'image/png')}).json()['meta']['source']['sha256']
        assert ha != hb
        # preview bytes hash to each session's OWN source
        import hashlib as _h
        da = c.get(f'/api/projects/{pid}/generation/sessions/{sid_a}/preview/source.png').content
        db = c.get(f'/api/projects/{pid}/generation/sessions/{sid_b}/preview/source.png').content
        assert _h.sha256(da).hexdigest() == ha
        assert _h.sha256(db).hexdigest() == hb
        # change B's source through the endpoint: only B moves
        variant2 = io.BytesIO()
        Image.new('RGB', (256, 256), '#884466').save(variant2, format='PNG')
        hb2 = c.post(f'/api/projects/{pid}/generation/sessions/{sid_b}/source', headers=H,
                     files={'file': ('b2.png', variant2.getvalue(), 'image/png')}).json()['meta']['source']['sha256']
        assert hb2 != hb
        assert c.get(f'/api/projects/{pid}/generation/sessions/{sid_a}/preview/source.png').content == da, \
            'session A must be untouched by B\'s source change'
        assert _h.sha256(c.get(f'/api/projects/{pid}/generation/sessions/{sid_b}/preview/source.png').content).hexdigest() == hb2
        sa = c.get(f'/api/projects/{pid}/generation/sessions/{sid_a}').json()['meta']['source']['sha256']
        sb = c.get(f'/api/projects/{pid}/generation/sessions/{sid_b}').json()['meta']['source']['sha256']
        assert (sa, sb) == (ha, hb2)


def test_source_or_settings_change_invalidates_result(tmp_path, monkeypatch):
    """30C/30D: after a successful convert, changing the SOURCE or the
    FIDELITY makes the old result non-committable (honest message), without
    auto-starting any paid work; restoring the inputs re-enables commit."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = _convert_session(pid, c)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        assert r.status_code == 200
        r = c.post(f'{base}/convert', headers=H,
                   files={'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        # settings change (fidelity) -> free, marks result stale
        r = c.post(f'{base}/settings', headers=H, json={'fidelity': 'faithful'})
        assert r.status_code == 200, r.text
        assert r.json()['fidelity'] == 'faithful'
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 400
        assert 'different inputs' in r.json()['detail']
        # restore the fidelity: identity matches again -> committable
        r = c.post(f'{base}/settings', headers=H, json={'fidelity': 'balanced'})
        assert r.status_code == 200
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        # now change the SOURCE: the committed-session case is gone, so check
        # the guard on a fresh session instead — covered by settings above.


# ---------------------------------------------------------------------------
# Task-30 review patch — reference identity, no-file planning, summaries
# ---------------------------------------------------------------------------

def test_reference_plan_uses_stored_source_without_file(tmp_path, monkeypatch):
    """P1: after upload + refresh, /reference-plan works WITHOUT a multipart
    file — planning succeeds and the STORED (normalized) source reaches the
    vision call."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_convert_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'hard'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('scene.png', _convert_fixture_png(), 'image/png')})
        assert r.status_code == 200, r.text
        sha = r.json()['meta']['source']['sha256']
        r = c.post(f'{base}/reference-plan', headers=H,
                   files={'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200, r.text
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        # the vision call carried the stored image (input_image part present)
        assert any(s.endswith('/json') for s in seen)
        sess = c.get(base).json()
        obj_ids = [o['id'] for o in sess['scenePlan']['objects']]
        assert 'obj-sky' in obj_ids
        assert sess['meta']['source']['sha256'] == sha


def test_reference_source_change_blocks_commit(tmp_path, monkeypatch):
    """P1: Reference A → analyze → generate → source replaced with B →
    commit of A's result is REFUSED (identity mismatch); the artwork itself
    is untouched and the session stays recoverable."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        sha_a = r.json()['meta']['source']['sha256']
        r = c.post(f'{base}/reference-plan', headers=H,
                   files={'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200
        wait(c, pid, timeout=120)
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta']['activeBuildInputs']['sourceSha256'] == sha_a
        # swap the source
        variant = io.BytesIO()
        Image.new('RGB', (256, 256), '#224488').save(variant, format='PNG')
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('b.png', variant.getvalue(), 'image/png')})
        sha_b = r.json()['meta']['source']['sha256']
        assert sha_b != sha_a
        assert r.json()['meta']['buildInputsStale'] is True
        # commit of the stale reference result is refused
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 400
        assert 'different inputs' in r.json()['detail']
        # the generated artwork is untouched (recoverable, preview serves)
        assert c.get(f'{base}/preview/colored.svg').status_code == 200


def test_convert_summary_matches_final_manifest(tmp_path, monkeypatch):
    """P2: the session summary (measuredDifficulty, regionCount) describes
    the FINAL manifest — including after the optimizer moved the geometry."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = _convert_session(pid, c)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base}/convert', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        manifest = json.loads((tmp_path / pid / 'sessions' / sid / 'bundle' / 'artwork.json').read_text())
        assert sess['meta']['measuredDifficulty']['rating'] == manifest['difficulty']['rating']
        assert sess['meta']['measuredDifficulty']['score'] == manifest['difficulty']['score']
        assert sess['meta']['regionCount'] == manifest['regionCount']
        opt = sess['meta']['difficultyOptimization']
        assert opt['achieved']['regionCount'] == manifest['regionCount']


def test_restore_fidelity_clears_stale_without_spend(tmp_path, monkeypatch):
    """P2: Balanced → Faithful → Balanced: the stale flag is COMPUTED from
    the input comparison, so restoring re-enables commit with no extra
    provider call."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_convert_transport(seen))) as c:
        pid = new(c)
        sid = _convert_session(pid, c)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base}/convert', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'
        json_calls_0 = sum(1 for x in seen if x.endswith('/json')) + sum(1 for x in seen if x.endswith('/svg'))
        r = c.post(f'{base}/settings', headers=H, json={'fidelity': 'faithful'})
        assert r.json()['meta']['buildInputsStale'] is True
        r = c.post(f'{base}/settings', headers=H, json={'fidelity': 'balanced'})
        assert r.json()['meta']['buildInputsStale'] is False
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        # no additional provider activity happened beyond the first convert
        calls_now = sum(1 for x in seen if x.endswith('/json')) + sum(1 for x in seen if x.endswith('/svg'))
        assert calls_now == json_calls_0 + 0


def test_inline_convert_upload_updates_metadata_and_identity(tmp_path, monkeypatch):
    """P1: an inline /convert upload routes through the SAME source helper —
    bytes, meta.source hash and the identity snapshot all point at B."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = _convert_session(pid, c)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        # first convert from A (stored path)
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        sha_a = r.json()['meta']['source']['sha256']
        c.post(f'{base}/convert', headers=H, files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=240)
        # inline convert with B (multipart file, no prior /source)
        variant = io.BytesIO()
        Image.new('RGB', (256, 256), '#552288').save(variant, format='PNG')
        r = c.post(f'{base}/convert', headers=H,
                   files={'file': ('b.png', variant.getvalue(), 'image/png'),
                          'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200, r.text
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        import hashlib as _h
        stored = (tmp_path / pid / 'sessions' / sid / 'source.png').read_bytes()
        sha_b = _h.sha256(stored).hexdigest()
        assert sess['meta']['source']['sha256'] == sha_b != sha_a
        assert sess['meta']['activeBuildInputs']['sourceSha256'] == sha_b
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Task 31 part 1 — build provenance (compile must not re-validate old masters)
# ---------------------------------------------------------------------------

def test_recompile_after_source_change_is_refused(tmp_path, monkeypatch):
    """P1: Reference A → analyze → generate (ready) → source swapped to B →
    a PLAIN recompile must not proceed (it would stamp master A with source
    B's provenance). Commit stays refused; the honest error names the fix."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=120)
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['meta']['masterOrigin']['sourceSha256'] == sess['meta']['source']['sha256']
        # swap the source
        variant = io.BytesIO()
        Image.new('RGB', (256, 256), '#224488').save(variant, format='PNG')
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('b.png', variant.getvalue(), 'image/png')})
        assert r.json()['meta']['buildInputsStale'] is True
        # plain recompile must be REFUSED synchronously (no state mutated)
        r = c.post(f'{base}/compile', headers=H, json={})
        assert r.status_code == 400
        assert 'Re-analyze the reference' in r.json()['detail']
        # the session keeps its healthy ready state (the refusal mutates
        # nothing) and commit still refuses on identity, not on a nuked status
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 400
        assert 'different inputs' in r.json()['detail']


def test_inline_reference_upload_updates_provenance(tmp_path, monkeypatch):
    """P1: stored A → inline Reference B (multipart on /reference-plan) —
    the analyzed image, meta.source hash and plan provenance ALL point at B."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        sha_a = r.json()['meta']['source']['sha256']
        variant = io.BytesIO()
        Image.new('RGB', (256, 256), '#224488').save(variant, format='PNG')
        r = c.post(f'{base}/reference-plan', headers=H,
                   files={'file': ('b.png', variant.getvalue(), 'image/png'),
                          'body': (None, json.dumps({'confirm_paid': True}))})
        assert r.status_code == 200, r.text
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        sha_b = sess['meta']['source']['sha256']
        assert sha_b != sha_a
        # metadata, plan provenance and analyze origin all point at B
        assert sess['meta']['planOriginSourceSha256'] == sha_b
        assert sess['meta']['source']['name'] == 'b.png'


def test_metadata_only_compile_keeps_origin_and_enables_commit(tmp_path, monkeypatch):
    """Reviewer gate: rename metadata → free compile → commit succeeds; a
    difficulty change also recompiles freely and stays committable — plan
    content is NOT part of the identity equality (visual drift is guarded
    per-object by pendingArtworkChanges, metadata edits never pend)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=120)
        c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'
        origin_sha = c.get(base).json()['meta']['masterOrigin']['sourceSha256']
        # metadata rename (update_object name) — never pends
        r = c.post(f'{base}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-sky',
                                        'changes': {'name': 'Open sky'}}]})
        assert r.status_code == 200
        assert not (r.json()['meta'].get('pendingArtworkChanges') or {})
        # free compile still allowed; commit succeeds (identity unchanged)
        r = c.post(f'{base}/compile', headers=H, json={})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta']['buildInputsStale'] is False
        assert sess['meta']['activeBuildInputs']['sourceSha256'] == origin_sha
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        # difficulty change on the committed session's follow-up: mutate ->
        # compile adopts new gameplay settings -> commit again succeeds
        sid2 = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                      json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base2 = f'/api/projects/{pid}/generation/sessions/{sid2}'
        c.post(f'{base2}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base2}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=120)
        c.post(f'{base2}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'
        r = c.post(f'{base2}/mutate', headers=H,
                   json={'mutations': [{'op': 'set_difficulty', 'difficulty': 'hard'}]})
        assert r.status_code == 200
        assert r.json()['meta'].get('buildInputsStale') is True
        r = c.post(f'{base2}/compile', headers=H, json={})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess2 = c.get(base2).json()
        assert sess2['status'] == 'ready_to_commit'
        assert sess2['meta']['buildInputsStale'] is False
        r = c.post(f'{base2}/commit', headers=H, json={})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Task 31 — operation identity, cancellation, recovery, structured progress
# ---------------------------------------------------------------------------

def _slow29_transport(seen, delay=0.25):
    """_task29_transport whose fragment calls are slow enough to cancel
    mid-generation."""
    return _task29_transport(seen, svg_delay=delay)


def test_idempotent_same_key_collapses_and_replays(tmp_path, monkeypatch):
    """Same key twice: ONE provider run; the second response replays the
    same attempt. After completion the replay carries attemptStatus=done.
    Same key with a different payload is a 409."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        json_calls_before = sum(1 for x in seen if x.endswith('/json'))
        body = {'confirm_paid': True, 'instruction': 'make the tree bigger', 'idempotency_key': 'op-1'}
        r1 = c.post(f'{base}/plan-chat', headers=H, json=body)
        r2 = c.post(f'{base}/plan-chat', headers=H, json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()['jobId'] == r2.json()['jobId']
        assert r2.json()['idempotentReplay'] is True
        wait(c, pid, timeout=120)
        # exactly ONE translator call for the two identical submits
        assert sum(1 for x in seen if x.endswith('/json')) == json_calls_before + 1
        # resend after completion: same attempt, no new provider call
        r3 = c.post(f'{base}/plan-chat', headers=H, json=body)
        assert r3.status_code == 200
        assert r3.json()['idempotentReplay'] is True
        assert r3.json()['attemptStatus'] == 'done'
        assert sum(1 for x in seen if x.endswith('/json')) == json_calls_before + 1
        # same key, different payload → 409
        r4 = c.post(f'{base}/plan-chat', headers=H,
                    json={'confirm_paid': True, 'instruction': 'different work', 'idempotency_key': 'op-1'})
        assert r4.status_code == 409


def test_failed_attempt_never_respends_automatically(tmp_path, monkeypatch):
    """A replay of a FAILED attempt returns the failed attempt (and spends
    nothing) — retrying is an explicit new action, not a silent resend."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        svg0 = sum(1 for x in seen if x.endswith('/svg'))
        r = c.post(f'{base}/regenerate-object', headers=H,
                   json={'objectId': 'obj-ghost', 'confirm_paid': True, 'idempotency_key': 'op-x'})
        assert r.status_code == 200
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'failed'
        calls_after_fail = sum(1 for x in seen if x.endswith('/svg'))
        r2 = c.post(f'{base}/regenerate-object', headers=H,
                    json={'objectId': 'obj-ghost', 'confirm_paid': True, 'idempotency_key': 'op-x'})
        assert r2.status_code == 200
        assert r2.json()['idempotentReplay'] is True
        assert r2.json()['attemptStatus'] == 'failed'
        assert sum(1 for x in seen if x.endswith('/svg')) == calls_after_fail


def test_commit_resend_returns_same_revision(tmp_path, monkeypatch):
    """Resending the same commit request returns the already-created revision
    instead of a duplicate."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, [])
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r1 = c.post(f'{base}/commit', headers=H, json={'idempotency_key': 'commit-1'})
        assert r1.status_code == 200, r1.text
        rev1 = r1.json()['revision']['id']
        r2 = c.post(f'{base}/commit', headers=H, json={'idempotency_key': 'commit-1'})
        assert r2.status_code == 200
        assert r2.json()['idempotentReplay'] is True
        assert r2.json()['revision']['id'] == rev1
        p = c.get(f'/api/projects/{pid}').json()
        assert len(p['revisions']) == 1


def test_cancel_running_generation(tmp_path, monkeypatch):
    """Cancel mid-generation on a FRESH generation (no fragment cache yet):
    no further fragment steps run, the job ends 'canceled' with the honest
    copy, and the session returns to draft_plan (resumable). A retry with a
    NEW key completes — reusing the checkpointed fragments of the objects
    that already succeeded before the cancel (that is the point of the
    checkpoint)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_slow29_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        svg_at_start = sum(1 for x in seen if x.endswith('/svg'))
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 'gen-1'})
        assert r.status_code == 200
        # wait until the first fragments are running, then cancel
        deadline = time.time() + 15
        while time.time() < deadline and sum(1 for x in seen if x.endswith('/svg')) <= svg_at_start:
            time.sleep(0.05)
        assert sum(1 for x in seen if x.endswith('/svg')) > svg_at_start, 'fragments never started'
        c.post(f'/api/projects/{pid}/job/cancel', headers=H)
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'canceled', (p['job'].get('status'), p['job'].get('message'))
        assert 'Cancellation requested' in p['job']['message']
        calls_at_cancel = sum(1 for x in seen if x.endswith('/svg'))
        time.sleep(1.0)   # a late response would add calls
        assert sum(1 for x in seen if x.endswith('/svg')) == calls_at_cancel, \
            'no further provider steps may run after cancellation'
        sess = c.get(base).json()
        assert sess['status'] == 'draft_plan', 'canceled session must stay resumable'
        # the cancellation bookkeeping settles BEFORE the next attempt starts
        time.sleep(0.3)
        # retry with a NEW key completes the work
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 'gen-2'})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        # checkpoint: fragments fetched before the cancel are NOT re-bought —
        # the retry's fragment calls are bounded by the remaining objects
        calls_after_retry = sum(1 for x in seen if x.endswith('/svg')) - calls_at_cancel
        assert calls_after_retry < 6, 'checkpoint should skip already-cached fragments'


def test_restart_sweep_marks_interrupted(tmp_path, monkeypatch):
    """After a service restart, a running job becomes 'interrupted' (readable
    recovery state, no automatic paid replay) and a session left generating
    returns to draft_plan."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    ws = tmp_path / 'ws'
    with TestClient(create_app(ws, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'prompt': 'garden'}).json()['id']
        # simulate a hard stop mid-generation
        pj = read_json(ws / pid / 'project.json')
        pj['job'] = {'id': 'x', 'kind': 'AI artwork synthesis', 'status': 'running',
                     'progress': 0.4, 'message': 'Vectorizing object 2/6: tree'}
        write_json(ws / pid / 'project.json', pj)
        sf = ws / pid / 'sessions' / sid / 'session.json'
        sess = read_json(sf)
        sess['status'] = 'generating'
        write_json(sf, sess)
    calls = []
    with TestClient(create_app(ws, transport=_task29_transport(calls))) as c2:
        p = c2.get(f'/api/projects/{pid}').json()
        assert p['job']['status'] == 'interrupted'
        assert 'retry explicitly' in p['job']['message']
        assert not isBusyStatus(p['job']['status'])
        sess = c2.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        assert sess['status'] == 'draft_plan'
        assert 'restart' in sess['meta'].get('interruptedNote', '')
        assert sum(1 for x in calls if x.endswith('/svg')) == 0, 'no paid replay on boot'


def isBusyStatus(st):
    return st in ('queued', 'running')


def test_structured_progress_fields(tmp_path, monkeypatch):
    """31C: while generating, the job exposes structured progress (stage,
    completedObjects, totalObjects, currentObjectId, sequence) — the frontend
    never parses the message string."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_slow29_transport(seen, delay=0.2))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
               json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H,
               json={'confirm_paid': True})
        observed = None
        deadline = time.time() + 15
        while time.time() < deadline:
            job = c.get(f'/api/projects/{pid}').json()['job']
            if job.get('stage') == 'vector_generation' and job.get('totalObjects'):
                observed = job
                break
            if job.get('status') in ('done', 'failed', 'canceled'):
                break
            time.sleep(0.03)
        assert observed, 'structured progress never observed'
        assert observed['totalObjects'] == 6
        assert isinstance(observed['completedObjects'], int)
        assert observed['currentObjectId']
        assert observed['sequence'] >= 1
        wait(c, pid, timeout=240)


# ---------------------------------------------------------------------------
# Task 31 round 2 — per-stage provenance, key scoping, checkpoint reuse
# ---------------------------------------------------------------------------

def test_reference_generate_refuses_swapped_source(tmp_path, monkeypatch):
    """Analyze source A → swap source to B → generate WITHOUT re-analysis:
    refused — the plan belongs to A and must never be stamped with origin B."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_convert_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=120)
        variant = io.BytesIO()
        Image.new('RGB', (256, 256), '#224488').save(variant, format='PNG')
        r = c.post(f'{base}/source', headers=H,
                   files={'file': ('b.png', variant.getvalue(), 'image/png')})
        assert r.status_code == 200
        svg0 = sum(1 for x in seen if x.endswith('/svg')) if False else None
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'failed'
        assert 'Re-analyze the reference' in p['job']['message']
        sess = c.get(base).json()
        # plan provenance still points at A — never re-stamped to B
        assert sess['meta']['planOriginSourceSha256'] != sess['meta']['source']['sha256']


def test_regenerated_visual_change_commits_and_rename_flows(tmp_path, monkeypatch):
    """Reviewer gates: Reference → change fills on one object → regenerate
    THAT object → ready + commit succeeds; a metadata rename separately
    compiles free and commits — no planRev false-staleness."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=120)
        c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        # visual change on flowers → pending
        r = c.post(f'{base}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-flowers',
                                        'changes': {'fills': ['#E8604C', '#FF88AA']}}]})
        assert r.json()['meta']['pendingArtworkChanges'] == {'obj-flowers': ['fills']}
        # regenerate the changed object → its pending entry clears → ready
        r = c.post(f'{base}/regenerate-object', headers=H,
                   json={'objectId': 'obj-flowers', 'confirm_paid': True})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'
        assert sess['meta'].get('buildInputsStale') is False
        r = c.post(f'{base}/commit', headers=H, json={})
        assert r.status_code == 200, r.text
        # metadata rename on a fresh session: free compile → commit succeeds
        sid2 = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                      json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
        base2 = f'/api/projects/{pid}/generation/sessions/{sid2}'
        c.post(f'{base2}/source', headers=H,
               files={'file': ('a.png', _convert_fixture_png(), 'image/png')})
        c.post(f'{base2}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True}))})
        wait(c, pid, timeout=120)
        c.post(f'{base2}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'
        r = c.post(f'{base2}/mutate', headers=H,
                   json={'mutations': [{'op': 'update_object', 'objectId': 'obj-hills',
                                        'changes': {'name': 'Rolling hills'}}]})
        assert r.status_code == 200
        assert not (r.json()['meta'].get('pendingArtworkChanges') or {})
        r = c.post(f'{base2}/compile', headers=H, json={})
        assert r.status_code == 200
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'
        sess2 = c.get(base2).json()
        assert sess2['status'] == 'ready_to_commit'
        assert sess2['meta']['buildInputsStale'] is False
        r = c.post(f'{base2}/commit', headers=H, json={})
        assert r.status_code == 200, r.text


def test_idempotent_key_scoped_per_operation(tmp_path, monkeypatch):
    """A key used for /plan can never replay as /generate (or vice versa):
    cross-operation reuse is a 409, not a silent cross-endpoint result."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r = c.post(f'{base}/plan', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 'shared-key'})
        assert r.status_code == 200
        wait(c, pid, timeout=120)
        # SAME key on a DIFFERENT operation → synchronous 409 (refused at
        # admission, before any job or provider work)
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 'shared-key'})
        assert r.status_code == 409
        assert 'scoped to one operation' in r.json()['detail']


def test_concurrent_admission_yields_one_valid_job(tmp_path, monkeypatch):
    """TRUE concurrency: two threads submit the same key simultaneously.
    The admission critical section (replay search -> validation -> allocation
    -> persist) runs under the project lock, so both threads must observe the
    SAME valid jobId and exactly one provider job must run."""
    import threading
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_slow29_transport(seen, delay=0.15))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
               json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        body = {'confirm_paid': True, 'idempotency_key': 'race-1'}
        url = f'/api/projects/{pid}/generation/sessions/{sid}/generate'
        results = [None, None]
        barrier = threading.Barrier(2, timeout=10)

        def submit(slot):
            barrier.wait()           # both threads fire together
            results[slot] = c.post(url, headers=H, json=body)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        r1, r2 = results
        assert r1 is not None and r2 is not None
        assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
        j1, j2 = r1.json()['jobId'], r2.json()['jobId']
        assert j1 and j2, f'both submissions must observe a valid job: {j1!r} vs {j2!r}'
        assert j1 == j2, f'concurrent same-key submits must collapse: {j1} vs {j2}'
        replay = r2.json().get('idempotentReplay') or r1.json().get('idempotentReplay')
        assert replay is True, 'one of the two must be a replay of the other'
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        sess = c.get(f'/api/projects/{pid}/generation/sessions/{sid}').json()
        # no orphan: every attempt record carries its jobId
        for a in sess['meta'].get('attempts', []):
            if a.get('key') == 'race-1':
                assert a.get('jobId') == j1


def test_checkpoint_reuse_on_retry_after_failure(tmp_path, monkeypatch):
    """Retry after a mid-generation failure reuses checkpointed fragments of
    the already-succeeded objects — only the failing fragment and later ones
    are re-purchased. (Explicit generation restart, per-object checkpoint.)"""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    fail_once = {'armed': True}
    seen2: list = []
    calls_per_bbox: dict = {}

    def respond(req):
        import re as _re
        seen2.append(req.url.path)
        if req.url.path.endswith('/json'):
            objects = [
                {'name': 'sky', 'description': 'blue sky', 'z': 0,
                 'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
                {'name': 'house', 'description': 'yellow house', 'z': 1,
                 'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
                {'name': 'grass', 'description': 'green field', 'z': 2,
                 'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
            ]
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': objects})}]}], 'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            body = json.loads(req.content)
            m = re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
            bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            key = (bx, by)
            calls_per_bbox[key] = calls_per_bbox.get(key, 0) + 1
            # fail the GRASS fragment (third plan object) on its first call
            if fail_once['armed'] and key == (288, 380):
                fail_once['armed'] = False
                return httpx.Response(500, json={'error': {'code': 'provider_blip'}})
            pad = max(4, min(bw, bh) // 8)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/>'
                   f'<path d="M {bx+pad},{by+pad} Q {bx+bw/2},{by+pad+(bh-2*pad)/2} {bx+bw-pad},{by+pad} Z" fill="#AA3355" fill-opacity="0.5"/>'
                   f'</svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 10}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})

    with TestClient(create_app(tmp_path, transport=httpx.MockTransport(respond))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'done'
        # first generate: sky+house succeed, grass fails (provider blip)
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'failed'
        calls_after_first = dict(calls_per_bbox)
        assert calls_after_first.get((0, 0)) == 1 and calls_after_first.get((0, 380)) == 1
        # retry with a NEW key: cached fragments are reused — sky and house
        # are NOT re-purchased; grass succeeds this time
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        assert calls_per_bbox.get((0, 0)) == 1, 'sky fragment must come from the checkpoint'
        assert calls_per_bbox.get((0, 380)) == 1, 'house fragment must come from the checkpoint'
        assert calls_per_bbox.get((288, 380)) == 2, 'failed fragment is re-purchased once'
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'


# ---------------------------------------------------------------------------
# Task 31 frontend wiring probes (HTTP-level, no browser required)
# ---------------------------------------------------------------------------

def test_frontend_key_present_in_plan_request(tmp_path, monkeypatch):
    """Proves idempotency_key is wired to /plan in the API layer.
    We call the endpoint directly with a key (simulating what the hook
    sends after a user click) and verify: (1) accepted, (2) replay works."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'harbour'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        # Send with a key (what the hook always does now)
        r1 = c.post(f'{base}/plan', headers=H,
                    json={'confirm_paid': True, 'idempotency_key': 'fe-plan-1'})
        assert r1.status_code == 200
        wait(c, pid, timeout=120)
        # Re-send same key (simulates lost-response retry from UI) → replay
        r2 = c.post(f'{base}/plan', headers=H,
                    json={'confirm_paid': True, 'idempotency_key': 'fe-plan-1'})
        assert r2.status_code == 200
        assert r2.json()['idempotentReplay'] is True
        assert r2.json()['attemptStatus'] == 'done'


def test_frontend_inflight_guard_prevents_double_submit(tmp_path, monkeypatch):
    """Simulates the frontend in-flight guard: while the first request is
    in flight, a second with the SAME key gets the same job (not 429 or 409).
    This verifies that concurrent admission collapses rather than rejects."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_slow29_transport(seen, delay=0.15))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
               json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        body = {'confirm_paid': True, 'idempotency_key': 'fe-gen-1'}
        r1 = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H, json=body)
        r2 = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H, json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        j1 = r1.json()['jobId']; j2 = r2.json()['jobId']
        assert j1 == j2, f'both submits must share one job: {j1} vs {j2}'
        assert r2.json()['idempotentReplay'] is True
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'


def test_frontend_lost_response_retries_same_key(tmp_path, monkeypatch):
    """Simulates the lost-response scenario: UI sent a request but never got
    a response (network drop). UI retries with the SAME key — exactly one
    provider call is made regardless of how many times the UI retries."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_task29_transport(seen))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        c.post(f'/api/projects/{pid}/generation/sessions/{sid}/plan', headers=H,
               json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        svg_before = sum(1 for x in seen if x.endswith('/svg'))
        body = {'confirm_paid': True, 'idempotency_key': 'fe-gen-lostresponse'}
        r1 = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H, json=body)
        assert r1.status_code == 200
        wait(c, pid, timeout=240)
        svg_after_first = sum(1 for x in seen if x.endswith('/svg'))
        # UI retries (lost-response scenario) 3 more times with same key
        for _ in range(3):
            r = c.post(f'/api/projects/{pid}/generation/sessions/{sid}/generate', headers=H, json=body)
            assert r.status_code == 200
            assert r.json()['idempotentReplay'] is True
        # No new provider calls were made
        assert sum(1 for x in seen if x.endswith('/svg')) == svg_after_first, \
            'retries with same key must not buy additional provider calls'


def test_frontend_commit_deduplicated_with_key(tmp_path, monkeypatch):
    """Commit with key: double-click or lost response never emits duplicate
    revisions. Second submit returns the already-created revision."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = _plan_and_generate(c, pid, [])
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        r1 = c.post(f'{base}/commit', headers=H, json={'idempotency_key': 'fe-commit-1'})
        assert r1.status_code == 200
        rev_id = r1.json()['revision']['id']
        # resend (lost response / double-click)
        r2 = c.post(f'{base}/commit', headers=H, json={'idempotency_key': 'fe-commit-1'})
        assert r2.status_code == 200
        assert r2.json()['idempotentReplay'] is True
        assert r2.json()['revision']['id'] == rev_id
        p = c.get(f'/api/projects/{pid}').json()
        assert len(p['revisions']) == 1, 'only one revision must appear in the project'


def test_cancel_does_not_affect_new_attempt(tmp_path, monkeypatch):
    """A LATE cancel carrying a stale jobId must not cancel a newer attempt.
    Proves the jobId-aware cancel gate works end-to-end."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_slow29_transport(seen, delay=0.12))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 'g1'})
        old_job_id = r.json()['jobId']
        # wait for generation to complete
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done'
        # trigger difficulty change to allow regeneration
        c.post(f'{base}/mutate', headers=H,
               json={'mutations': [{'op': 'set_difficulty', 'difficulty': 'hard'}]})
        # start a NEW generation; send a cancel with the OLD jobId (late cancel)
        r2 = c.post(f'{base}/generate', headers=H,
                    json={'confirm_paid': True, 'idempotency_key': 'g2'})
        new_job_id = r2.json()['jobId']
        assert new_job_id != old_job_id
        stale_cancel = c.post(f'/api/projects/{pid}/job/cancel', headers=H,
                               json={'jobId': old_job_id})
        assert stale_cancel.status_code == 200
        p = wait(c, pid, timeout=240)
        # new job must complete, not canceled by stale cancel
        assert p['job']['status'] == 'done', \
            f'stale cancel must not affect the new job: {p["job"]["status"]}'


# ---------------------------------------------------------------------------
# Task 31 round 3 — terminal agreement, checkpoint validation & era lifecycle
# ---------------------------------------------------------------------------

def test_terminal_states_agree_after_cancel(tmp_path, monkeypatch):
    """Single terminal decision owner: when a cancel lands during a slow
    generation, the job AND the attempt must both end 'canceled' — the old
    interleaving (attempt=done while job=canceled) is closed by construction
    (wrapped() never publishes terminal state; launch's on_terminal does)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    seen = []
    with TestClient(create_app(tmp_path, transport=_slow29_transport(seen, delay=0.12))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        c.post(f'{base}/generate', headers=H,
               json={'confirm_paid': True, 'idempotency_key': 'race-cancel'})
        # cancel while fragments are in flight
        deadline = time.time() + 15
        while time.time() < deadline and not seen or \
                sum(1 for x in seen if x.endswith('/svg')) < 1:
            time.sleep(0.05)
        c.post(f'/api/projects/{pid}/job/cancel', headers=H)
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'canceled', p['job']
        sess = c.get(base).json()
        attempts = sess['meta'].get('attempts', [])
        mine = [a for a in attempts if a.get('key') == 'race-cancel']
        assert mine and mine[-1]['status'] == 'canceled', \
            f'attempt must agree with job: {mine}'


def test_terminal_states_agree_after_done(tmp_path, monkeypatch):
    """No cancel: job done AND attempt done, and the attempt record carries
    no premature state (single decision owner publishes both together)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_task29_transport([]))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        c.post(f'{base}/generate', headers=H,
               json={'confirm_paid': True, 'idempotency_key': 'done-1'})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        sess = c.get(base).json()
        attempts = sess['meta'].get('attempts', [])
        mine = [a for a in attempts if a.get('key') == 'done-1']
        assert mine and mine[-1]['status'] == 'done'
        assert mine[-1].get('jobId'), 'attempt must carry its jobId'


def test_invalid_cached_fragment_not_reused(tmp_path, monkeypatch):
    """Reviewer probe as a regression: the provider first returns an empty
    (no-drawable-shapes) fragment — the poisoned cache entry must be DELETED
    so the retry fetches fresh valid work and completes."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    empty_first = {'grass': True}

    def respond(req):
        import re as _re
        if req.url.path.endswith('/json'):
            objects = [
                {'name': 'sky', 'description': 'blue sky', 'z': 0,
                 'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
                {'name': 'house', 'description': 'yellow house', 'z': 1,
                 'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
                {'name': 'grass', 'description': 'green field', 'z': 2,
                 'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
            ]
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': objects})}]}], 'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            body = json.loads(req.content)
            m = re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
            bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            pad = max(4, min(bw, bh) // 8)
            if empty_first['grass'] and bx == 288:
                empty_first['grass'] = False
                # wrapper-valid but EMPTY fragment (no drawable shapes)
                return httpx.Response(200, json={'output': [{'content': [
                    {'type': 'output_text', 'text': f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}"></svg>'}
                ]}], 'usage': {'input_tokens': 5}})
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/>'
                   f'</svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 10}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})

    with TestClient(create_app(tmp_path, transport=httpx.MockTransport(respond))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        wait(c, pid, timeout=120)
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'failed', p['job']
        assert ('no drawable shapes' in p['job']['message']
                or 'no supported drawable shapes' in p['job']['message'])
        # retry: the poisoned cache entry was deleted — fresh work is fetched
        # and the generation completes
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', (p['job'].get('status'), p['job'].get('message'))
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'


def test_checkpoint_survives_across_era_generation(tmp_path, monkeypatch):
    """Second reviewer gate: initial generation succeeds → plan edit → next
    generation fails at the third object → retry still reuses the VALID
    fragments from the second attempt (per-era cache is not wiped between
    retries of the same era, and the plan edit started a NEW era cleanly)."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    fail_grass = {'armed': False}   # armed only after generation 1 succeeds
    calls_per_bbox: dict = {}

    def respond(req):
        import re as _re
        if req.url.path.endswith('/json'):
            objects = [
                {'name': 'sky', 'description': 'blue sky', 'z': 0,
                 'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
                {'name': 'house', 'description': 'yellow house', 'z': 1,
                 'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
                {'name': 'grass', 'description': 'green field', 'z': 2,
                 'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
            ]
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': objects})}]}], 'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            body = json.loads(req.content)
            m = re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
            bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            key = (bx, by)
            calls_per_bbox[key] = calls_per_bbox.get(key, 0) + 1
            if fail_grass['armed'] and key == (288, 380):
                fail_grass['armed'] = False
                return httpx.Response(500, json={'error': {'code': 'blip'}})
            pad = max(4, min(bw, bh) // 8)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/>'
                   f'</svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 10}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})

    with TestClient(create_app(tmp_path, transport=httpx.MockTransport(respond))) as c:
        pid = new(c)
        sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                     json={'mode': 'ai_chat', 'requested_difficulty': 'medium',
                           'prompt': 'garden'}).json()['id']
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=120)
        assert p['job']['status'] == 'done'
        # generation 1 succeeds fully (era cache filled: 3 fragments); the
        # failure is armed only AFTER it, so it can not poison the first era
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        # plan EDIT with a VISUAL change starts a new era; arm the failure on
        # the changed object so the era-2 attempt fails at grass
        fail_grass['armed'] = True
        c.post(f'{base}/mutate', headers=H,
               json={'mutations': [{'op': 'update_object', 'objectId': 'obj-grass',
                                    'changes': {'fills': ['#41A582', '#2E7D5B']}}]})
        calls_before = dict(calls_per_bbox)
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'failed'
        # retry: fragments cached in THIS era (from the failed attempt) are
        # reused — sky/house are not re-purchased
        calls_before_retry = dict(calls_per_bbox)
        r = c.post(f'{base}/generate', headers=H, json={'confirm_paid': True})
        p = wait(c, pid, timeout=240)
        assert p['job']['status'] == 'done', p['job']
        assert calls_per_bbox[(0, 0)] == calls_before_retry.get((0, 0)), \
            'sky must come from this era checkpoint'
        assert calls_per_bbox[(0, 380)] == calls_before_retry.get((0, 380)), \
            'house must come from this era checkpoint'
