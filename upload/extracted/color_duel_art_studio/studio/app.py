from __future__ import annotations
import io,json,os,re,shutil,threading,uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from fastapi import FastAPI,UploadFile,File,Form,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from .models import *
from .pipeline import clean_image,compile_image,edit_bundle,read_json,write_json,make_export,load_bundle,validate_bundle
from .ai import Provider

BASE=Path(__file__).resolve().parents[1]
load_dotenv(BASE/'.env')
SAFE=re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}$')
FILES={'artwork.json','regions.json','palette.json','paint.json','colored.svg','numbered.svg','linework.svg',
       'ink.svg','selected-preview.svg','thumbnail.webp','source-master.png','colored-preview.png','numbered-preview.png','validation.json','build-settings.json'}

def now(): return datetime.now(timezone.utc).isoformat()
def ident(): return uuid.uuid4().hex[:16]

def create_app(workspace: Path|None=None, transport=None):
    root=workspace or Path(os.getenv('STUDIO_WORKSPACE',str(BASE/'workspace')))
    root.mkdir(parents=True,exist_ok=True)
    lock=threading.RLock();pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='art-studio')
    provider=Provider(transport)
    # A stopped local process never silently replays paid jobs on restart.
    for path in root.glob('*/project.json'):
        p=read_json(path)
        if p.get('job',{}).get('status') in ['queued','running']:
            p['job']['status']='failed';p['job']['message']='Studio restarted. Job was not retried; check provider usage before retrying paid work.'
            write_json(path,p)
    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True,cancel_futures=False)
    app=FastAPI(title='Color Duel Art Studio',version='0.1.0',lifespan=lifespan)
    app.state.root=root
    def folder(pid):
        if not SAFE.fullmatch(pid): raise HTTPException(400,'Invalid project ID.')
        directory=root/pid
        if not (directory/'project.json').is_file(): raise HTTPException(404,'Project not found.')
        return directory
    def project(pid): return read_json(folder(pid)/'project.json')
    def save(p):
        p['updatedAt']=now(); path=root/p['id']/'project.json';tmp=path.with_suffix('.tmp')
        write_json(tmp,p);tmp.replace(path)
    def editable(p):
        if p.get('job',{}).get('status') in ['queued','running']: raise HTTPException(409,'This project has a running job. Wait for it to finish.')
    def revision_dir(pid,revision):
        if not SAFE.fullmatch(revision): raise HTTPException(400,'Invalid revision.')
        p=project(pid)
        if revision not in [r['id'] for r in p['revisions']]: raise HTTPException(404,'Revision not found.')
        return folder(pid)/'revisions'/revision
    def start(pid,kind,fn):
        with lock:
            p=project(pid);editable(p)
            active=sum(read_json(f).get('job',{}).get('status') in ['queued','running'] for f in root.glob('*/project.json'))
            if active>=4: raise HTTPException(429,'Local queue is full. Wait for another project to finish.')
            jid=ident(); p['job']={'id':jid,'kind':kind,'status':'queued','progress':0,'message':'Queued','startedAt':now()};save(p)
        def tick(fraction,message):
            with lock:
                p=project(pid);p['job'].update(status='running',progress=fraction,message=message);save(p)
        def work():
            try:
                tick(.01,'Starting '+kind)
                result=fn(tick)
                with lock:
                    p=project(pid)
                    if result.get('revision'):
                        p['revisions'].append(result['revision']);p['currentRevision']=result['revision']['id']
                    if result.get('master'):
                        p['master']=result['master'];p['currentRevision']=None
                    if result.get('chat'):
                        c=result['chat'];p['messages']+=c['messages'];p['brief']=c['brief'];p['aiUsage'].append(c['usage'])
                    if result.get('usage'): p['aiUsage'].append(result['usage'])
                    p['job'].update(status='done',progress=1,message='Ready',finishedAt=now());save(p)
            except Exception as exc:
                with lock:
                    p=project(pid);p['job'].update(status='failed',message=str(exc)[:700],finishedAt=now());save(p)
        pool.submit(work)
        return {'jobId':jid,'projectId':pid}
    @app.middleware('http')
    async def local_safety(request:Request,call_next):
        # Same-origin custom header blocks drive-by browser POSTs to a local service.
        if request.url.path.startswith('/api/') and request.method not in ['GET','HEAD','OPTIONS']:
            if request.headers.get('x-studio-request')!='1': return JSONResponse({'detail':'Missing X-Studio-Request header.'},403)
            origin=request.headers.get('origin')
            if origin:
                parsed=urlparse(origin)
                if parsed.netloc!=request.headers.get('host') or parsed.scheme not in ['http','https']:
                    return JSONResponse({'detail':'Cross-origin write blocked.'},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        if request.url.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(ValueError)
    async def bad_value(_,exc): return JSONResponse({'detail':str(exc)},400)
    @app.get('/api/config')
    def config(): return {'ai':provider.config(),'localOnly':True,'version':'0.1.0','supportedFormat':'color-duel-detailed-vector-1','maxUploadMB':12}
    @app.get('/api/projects')
    def list_projects():
        with lock: return sorted([read_json(f) for f in root.glob('*/project.json')],key=lambda p:p['updatedAt'],reverse=True)
    @app.post('/api/projects')
    def create(body:CreateProject):
        with lock:
            pid='art-'+ident();d=root/pid;d.mkdir()
            p={'id':pid,'title':body.title,'brief':body.brief,'createdAt':now(),'updatedAt':now(),
               'messages':[],'aiUsage':[],'master':None,'reference':None,'revisions':[],'currentRevision':None,'job':{}}
            save(p);return p
    @app.get('/api/projects/{pid}')
    def get_project(pid:str):
        with lock:return project(pid)
    @app.patch('/api/projects/{pid}')
    def update(pid:str,body:UpdateProject):
        with lock:
            p=project(pid);editable(p);p.update(title=body.title,brief=body.brief);save(p);return p
    @app.post('/api/projects/{pid}/upload')
    async def upload(pid:str,file:UploadFile=File(...),role:str=Form('reference'),rights_confirmed:bool=Form(False)):
        if role not in ['reference','master']: raise HTTPException(400,'Invalid upload role.')
        if role=='master' and not rights_confirmed: raise HTTPException(400,'Confirm ownership or permission before using an image as the master.')
        raw=await file.read(12*1024*1024+1)
        with lock:
            p=project(pid);editable(p);name=f'{role}-{ident()}.png'
            meta=clean_image(raw,folder(pid)/name)
            p[role]={'file':name,**meta,'source':'uploaded','rightsConfirmed':rights_confirmed,'createdAt':now()}
            if role=='master':p['currentRevision']=None
            save(p);return p
    @app.post('/api/projects/{pid}/reference-as-master')
    def promote(pid:str,body:PromoteRequest):
        with lock:
            p=project(pid);editable(p)
            if not body.rights_confirmed:raise HTTPException(400,'Confirm rights before tracing a reference.')
            if not p['reference']: raise HTTPException(400,'Upload a reference first.')
            p['reference']['rightsConfirmed']=True
            p['master']={**p['reference'],'rightsConfirmed':True};p['currentRevision']=None;save(p);return p
    @app.post('/api/projects/{pid}/sample')
    def sample(pid:str):
        with lock:
            p=project(pid);editable(p)
            src=BASE/'examples/treehouse-source.png'
            if not src.is_file():raise HTTPException(404,'Bundled example is missing.')
            name='master-'+ident()+'.png';meta=clean_image(src.read_bytes(),folder(pid)/name)
            p['master']={'file':name,**meta,'source':'Earlier AI-generated sample from this conversation; not a competitor screenshot. No legal clearance claim.','rightsConfirmed':False,'createdAt':now()}
            p['currentRevision']=None;save(p);return p
    @app.get('/api/projects/{pid}/image/{role}')
    def image(pid:str,role:str):
        if role not in ['master','reference']:raise HTTPException(404)
        p=project(pid)
        if not p[role]:raise HTTPException(404)
        return FileResponse(folder(pid)/p[role]['file'],media_type='image/png')
    @app.post('/api/projects/{pid}/chat')
    def chat(pid:str,body:ChatRequest):
        if not body.confirm_paid:raise HTTPException(400,'Explicit confirmation is required: this sends text and optionally the reference to the AI provider and may incur charges.')
        if not provider.config()['configured']:raise HTTPException(503,'AI not configured. Add OPENAI_API_KEY to .env. Offline conversion still works.')
        with lock:p=project(pid)
        ref=folder(pid)/p['reference']['file'] if body.include_reference and p['reference'] else None
        def run(tick):
            tick(.15,'Refining the brief with AI')
            answer=provider.chat(p,body.message,ref)
            return {'chat':{'brief':answer['brief'],'messages':[{'role':'user','content':body.message},{'role':'assistant','content':answer['reply']}],
                            'usage':{'kind':'chat','at':now(),'model':answer['model'],'usage':answer['usage']}}}
        return start(pid,'chat',run)
    @app.post('/api/projects/{pid}/generate')
    def generate(pid:str,body:GenerateRequest):
        if not body.confirm_paid:raise HTTPException(400,'Confirm the paid provider request first.')
        if not provider.config()['configured']:raise HTTPException(503,'AI not configured. Add OPENAI_API_KEY to .env.')
        with lock:p=project(pid)
        source=None
        if body.source in ['reference','current']:
            item=p['reference'] if body.source=='reference' else p['master']
            if not item:raise HTTPException(400,'The chosen source image is missing.')
            if body.source=='reference' and not item.get('rightsConfirmed'):raise HTTPException(400,'For inspiration-only references, use Chat to describe broad traits, then Generate from brief. Direct edits require a rights-confirmed upload.')
            source=folder(pid)/item['file']
        def run(tick):
            tick(.12,'Requesting one image from the AI provider')
            raw,meta=provider.image(body.prompt,body.quality,body.size,source)
            name='master-'+ident()+'.png';im=clean_image(raw,folder(pid)/name)
            write_json(folder(pid)/(name+'.provenance.json'),{'prompt':body.prompt,'request':body.model_dump(exclude={'confirm_paid'}),**meta})
            return {'master':{'file':name,**im,'source':'AI-generated via '+meta['model'],'rightsConfirmed':False,'createdAt':now()},
                'usage':{'kind':'image','at':now(),**meta}}
        return start(pid,'generation',run)
    @app.post('/api/projects/{pid}/build')
    def build(pid:str,body:BuildSettings):
        with lock:p=project(pid)
        if not p['master']:raise HTTPException(400,'Upload your own master, load the example, or generate an image first.')
        rev='rev-'+ident();version=f'0.{len(p["revisions"])+1}.0'
        def run(tick):
            result=compile_image(folder(pid)/p['master']['file'],folder(pid)/'revisions'/rev,
                artwork_id=pid,version=version,title=p['title'],settings=body,
                provenance={'source':p['master']['source'],'sourceHash':p['master']['sha256'],
                    'rightsConfirmedByUser':p['master'].get('rightsConfirmed',False),'legalClearanceVerified':False},progress=tick)
            return {'revision':{'id':rev,'version':version,'createdAt':now(),'kind':'build','sourceHash':p['master']['sha256'],
                'regionCount':result['manifest']['regionCount'],'qa':result['validation'],
                'manifestUrl':f'/api/projects/{pid}/revisions/{rev}/files/artwork.json'}}
        return start(pid,'vector compilation',run)
    @app.post('/api/projects/{pid}/edit')
    def edit(pid:str,body:EditRequest):
        with lock:p=project(pid)
        if p['currentRevision']!=body.base_revision:raise HTTPException(409,'Revision changed. Reload before editing.')
        src=revision_dir(pid,body.base_revision);rev='rev-'+ident();version=f'0.{len(p["revisions"])+1}.0'
        def run(tick):
            tick(.2,'Applying edit into a new immutable revision')
            result=edit_bundle(src,folder(pid)/'revisions'/rev,body,version)
            return {'revision':{'id':rev,'version':version,'createdAt':now(),'kind':body.action,
                'sourceHash':p['master']['sha256'],'regionCount':result['manifest']['regionCount'],'qa':result['validation'],
                'manifestUrl':f'/api/projects/{pid}/revisions/{rev}/files/artwork.json'}}
        return start(pid,'region edit',run)
    @app.post('/api/projects/{pid}/activate')
    def activate(pid:str,body:ActivateRequest):
        with lock:
            p=project(pid);editable(p);revision_dir(pid,body.revision)
            r=next(r for r in p['revisions'] if r['id']==body.revision)
            if not p['master'] or r.get('sourceHash')!=p['master']['sha256']:
                raise HTTPException(409,'This revision belongs to an older master. Start a separate project rather than mix sources.')
            p['currentRevision']=body.revision;save(p);return p
    @app.post('/api/projects/{pid}/review')
    def review(pid:str,body:ReviewRequest):
        if not body.confirmed:raise HTTPException(400,'Human review confirmation is required.')
        with lock:
            p=project(pid);editable(p)
            if p['currentRevision']!=body.revision:raise HTTPException(409,'Review the current revision.')
            d=revision_dir(pid,body.revision);b=load_bundle(d);qa=validate_bundle(b)
            if not qa['passed']:raise HTTPException(400,'Geometry validation failed.')
            m=b['manifest'];m['review']={'at':now(),'note':body.note,'type':'self-attested-human-review','legalClearanceVerified':False}
            m['qa']['status']='human-reviewed-draft';m['qa']['humanReviewed']=True
            qa['humanReviewed']=True;write_json(d/'artwork.json',m);write_json(d/'validation.json',qa)
            for r in p['revisions']:
                if r['id']==body.revision:r['qa']=qa
            save(p);return p
    @app.get('/api/projects/{pid}/revisions/{revision}/files/{name}')
    def artifact(pid:str,revision:str,name:str):
        if name not in FILES:raise HTTPException(404)
        f=revision_dir(pid,revision)/name
        if not f.is_file():raise HTTPException(404)
        return FileResponse(f)
    @app.get('/api/projects/{pid}/revisions/{revision}/export')
    def export(pid:str,revision:str,authoring:bool=False):
        content=make_export(revision_dir(pid,revision),include_authoring=authoring)
        return Response(content,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{pid}-{revision}.zip"'})
    @app.get('/api/projects/{pid}/revisions/{revision}/render')
    def render(pid:str,revision:str,width:int=2048):
        if width<256 or width>4096:raise HTTPException(400,'Width must be 256–4096 pixels.')
        import cairosvg
        d=revision_dir(pid,revision);m=read_json(d/'artwork.json');w,h=m['viewBox'][2:]
        if width*round(width*h/w)>20_000_000:raise HTTPException(400,'Export exceeds 20 megapixels.')
        content=cairosvg.svg2png(url=str(d/'colored.svg'),output_width=width,output_height=round(width*h/w))
        return Response(content,media_type='image/png',headers={'Content-Disposition':'attachment; filename="vector-export.png"'})
    app.mount('/static',StaticFiles(directory=BASE/'web'),name='static')
    @app.get('/')
    def index():return FileResponse(BASE/'web/index.html')
    return app

app=create_app()
