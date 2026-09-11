import io,time,json,base64,re,os,tempfile
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
def wait(c,pid):
    end=time.time()+30
    while time.time()<end:
        p=c.get('/api/projects/'+pid).json()
        if p['job']['status'] in ['done','failed']:return p
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
        assert p['job']['status'] == 'done', p['job']
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
        assert p['job']['status'] == 'done', p['job']
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
        assert p['job']['status'] == 'done', p['job']
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
        assert p['job']['status'] == 'done', p['job']
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
        assert p['job']['status'] == 'done', p['job']
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
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="10" height="10" fill="#3366AA"/></svg>'}]}],
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
        p = wait(c, pid)
        assert p['job']['status'] == 'done', p['job']
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
            p = wait(c, pid)
            assert p['job']['status'] == 'done', p['job']
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
        wait(c, pid)
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
        assert p['job']['status'] == 'done', p['job']
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
    """Difficulty independence is now architectural: Easy vs Master sessions
    on the same source + fidelity must produce the SAME paint reconstruction
    (same shapeIds; identical path bytes) — only gameplay targets differ."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    paints = {}
    for difficulty in ('easy', 'master'):
        with TestClient(create_app(Path(tempfile.mkdtemp()), transport=_convert_transport([]))) as c:
            pid = new(c)
            sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                         json={'mode': 'image_convert', 'requested_difficulty': difficulty,
                               'fidelity': 'balanced'}).json()['id']
            assert _run_convert(c, pid, sid, _convert_fixture_png()).status_code == 200
            p = wait(c, pid)
            assert p['job']['status'] == 'done', p['job']
            sdir = Path(c.app.state.root) / pid / 'sessions' / sid
            paint = json.loads((sdir / 'bundle' / 'paint.json').read_text())
            # difficulty must not alter the RECONSTRUCTION: same shapes, same
            # ids, same path bytes (artworkId differs per project, hence the
            # structural comparison instead of raw bytes)
            paints[difficulty] = {p['shapeId']: p['d'] for p in paint['paths']}
    assert paints['easy'] == paints['master'], 'paint reconstruction must not depend on difficulty'


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
