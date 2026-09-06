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
