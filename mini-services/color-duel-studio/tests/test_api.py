import io,time,json,base64
import httpx,pytest
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
# Stage-2: cut/draw edit routes, multistage SVG generation, config 0.3.0
# ---------------------------------------------------------------------------

def test_config_version_and_new_backends(client):
    r=client.get('/api/config');assert r.status_code==200
    body=r.json()
    assert body['version']=='0.3.0'
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
