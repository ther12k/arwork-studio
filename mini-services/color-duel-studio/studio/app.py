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
from . import STUDIO_VERSION
from .pipeline import (BACKENDS, clean_image, compile_image, compile_svg_master,
                        difficulty_profile, edit_bundle, read_json, write_json, make_export,
                        load_bundle, validate_bundle, legacy_geometry)
from .svg_master import clean_svg
from .ai import Provider
from .generation import (
    GenerationSessionManager,
    create_scene_plan,
    apply_scene_mutations,
    DIFFICULTY_RANGES,
    DIFFICULTY_TIERS,
)

BASE = Path(__file__).resolve().parents[1]
load_dotenv(BASE / '.env')
SAFE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,80}$')
FILES = {'artwork.json', 'regions.json', 'palette.json', 'paint.json', 'objects.json',
         'colored.svg', 'numbered.svg', 'linework.svg',
         'ink.svg', 'selected-preview.svg', 'thumbnail.webp', 'source-master.png', 'source-master.svg',
         'colored-preview.png', 'numbered-preview.png', 'validation.json', 'build-settings.json'}

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
    app=FastAPI(title='Color Duel Art Studio',version=STUDIO_VERSION,lifespan=lifespan)
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
                    if result.get('pendingBuildSettings'):
                        p['pendingBuildSettings']=result['pendingBuildSettings']
                    if result.get('consumePending'):
                        p.pop('pendingBuildSettings',None)
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
                host=request.headers.get('host','')
                # Gateway (Caddy) forwards Host without the port, so compare hostnames.
                same_site=parsed.netloc==host or parsed.hostname==host.split(':')[0]
                if not same_site or parsed.scheme not in ['http','https']:
                    return JSONResponse({'detail':'Cross-origin write blocked.'},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        if request.url.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(ValueError)
    async def bad_value(_,exc): return JSONResponse({'detail':str(exc)},400)
    @app.get('/api/config')
    def config():
        ai = provider.config()
        for backend in BACKENDS:
            if backend['id'] in ('provider-svg-generation', 'provider-svg-multistage'):
                backend['available'] = bool(ai['configured'])
            if backend['id'] == 'provider-vectorizer':
                backend['available'] = bool(os.getenv('VECTORIZER_API_KEY'))
        return {'ai': ai, 'localOnly': True, 'version': STUDIO_VERSION, 'supportedFormat': 'color-duel-detailed-vector-1',
                'geometrySchema': 2, 'maxUploadMB': 12, 'backends': BACKENDS}
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
    async def upload(pid: str, file: UploadFile = File(...), role: str = Form('reference'), rights_confirmed: bool = Form(False)):
        if role not in ['reference', 'master']: raise HTTPException(400, 'Invalid upload role.')
        if role == 'master' and not rights_confirmed: raise HTTPException(400, 'Confirm ownership or permission before using an image as the master.')
        raw = await file.read(12 * 1024 * 1024 + 1)
        with lock:
            p = project(pid); editable(p); name = f'{role}-{ident()}.png'
            meta = clean_image(raw, folder(pid) / name)
            p[role] = {'file': name, **meta, 'source': 'uploaded', 'rightsConfirmed': rights_confirmed, 'createdAt': now()}
            if role == 'master': p['currentRevision'] = None
            save(p); return p

    @app.post('/api/projects/{pid}/upload-svg')
    async def upload_svg(pid: str, file: UploadFile = File(...), rights_confirmed: bool = Form(False)):
        """Sanitized SVG-master import: curves, holes, gradients, transforms and
        drawing order are preserved; the master is never rasterized or retraced."""
        if not rights_confirmed:
            raise HTTPException(400, 'Confirm ownership or permission before using an SVG as the master.')
        raw = await file.read(12 * 1024 * 1024 + 1)
        with lock:
            p = project(pid); editable(p)
            name = f'master-{ident()}.svg'
            meta = clean_svg(raw, folder(pid) / name)
            p['master'] = {'file': name, **meta, 'source': 'uploaded SVG master (sanitized; curves preserved, never rasterized)',
                           'rightsConfirmed': True, 'createdAt': now()}
            p['currentRevision'] = None
            save(p); return p

    @app.get('/api/projects/{pid}/master/svg')
    def master_svg(pid: str):
        p = project(pid)
        m = p.get('master') or {}
        if m.get('kind') != 'svg': raise HTTPException(404, 'The current master is not an SVG master.')
        f = folder(pid) / m['file']
        if not f.is_file(): raise HTTPException(404)
        return FileResponse(f, media_type='image/svg+xml', headers={'Cache-Control': 'no-store'})

    @app.post('/api/projects/{pid}/sample-svg')
    def sample_svg(pid: str):
        """Load the bundled curved SVG example (treehouse-master.svg)."""
        with lock:
            p = project(pid); editable(p)
            src = BASE / 'examples/treehouse-master.svg'
            if not src.is_file(): raise HTTPException(404, 'Bundled SVG example is missing.')
            name = f'master-{ident()}.svg'
            meta = clean_svg(src.read_bytes(), folder(pid) / name)
            p['master'] = {'file': name, **meta, 'source': 'Bundled studio example (original hand-authored artwork for this tool).',
                           'rightsConfirmed': True, 'createdAt': now()}
            p['currentRevision'] = None
            save(p); return p
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

    @app.post('/api/projects/{pid}/generate-svg')
    def generate_svg(pid: str, body: GenerateSvgRequest):
        """Separate SVG-generation route for providers that author SVG masters.
        The paid call is explicit; credentials stay server-side. The result is
        sanitized and imported with curves preserved — never rasterized."""
        if not body.confirm_paid:
            raise HTTPException(400, 'Explicit confirmation required: this sends the brief (and optionally the reference) to the paid AI provider and may incur charges.')
        if not provider.config()['configured']:
            raise HTTPException(503, 'AI not configured. Add OPENAI_API_KEY to .env. SVG-master upload and offline conversion work without it.')
        with lock:
            p = project(pid)
            ref = None
            if body.include_reference and p.get('reference'):
                ref = folder(pid) / p['reference']['file']
        def run(tick):
            if body.mode == 'multistage':
                # Multi-stage native-vector generation (contract 7): scene plan
                # (strict JSON) -> one SVG fragment per object -> composed,
                # sanitized master. The next build auto-subdivides to target.
                tick(.08, 'Planning the scene (multi-stage vector generation)')
                svg_text, meta = provider.svg_multistage(body.prompt, body.aspect, ref,
                                                         body.target_regions, tick)
                tick(.9, 'Sanitizing the composed SVG master')
                name = f'master-{ident()}.svg'
                im = clean_svg(svg_text.encode('utf-8'), folder(pid) / name)
                write_json(folder(pid) / (name + '.provenance.json'),
                           {'prompt': body.prompt, 'request': body.model_dump(exclude={'confirm_paid'}), **meta,
                            'sanitizerReport': im.get('summary', {}).get('warnings', [])})
                return {'master': {'file': name, **im, 'source': 'AI multi-stage SVG via ' + meta.get('model', 'chat') + ' (scene plan + per-object fragments; sanitized, curves preserved)',
                                   'rightsConfirmed': False, 'createdAt': now()},
                        'usage': {'at': now(), **meta, 'kind': 'svg-multistage'},
                        'pendingBuildSettings': {'auto_subdivide': True, 'target_regions': int(body.target_regions)}}
            tick(.15, 'Requesting an SVG master from the AI provider')
            svg_text, meta = provider.svg(body.prompt, body.aspect, ref)
            tick(.55, 'Sanitizing the generated SVG master')
            name = f'master-{ident()}.svg'
            im = clean_svg(svg_text.encode('utf-8'), folder(pid) / name)
            write_json(folder(pid) / (name + '.provenance.json'),
                       {'prompt': body.prompt, 'request': body.model_dump(exclude={'confirm_paid'}), **meta,
                        'sanitizerReport': im.get('summary', {}).get('warnings', [])})
            return {'master': {'file': name, **im, 'source': 'AI-generated SVG via ' + meta.get('model', 'chat') + ' (sanitized; curves preserved)',
                               'rightsConfirmed': False, 'createdAt': now()},
                    'usage': {'kind': 'svg-generation', 'at': now(), **meta}}
        return start(pid, 'svg generation', run)
    @app.post('/api/projects/{pid}/build')
    def build(pid:str,body:BuildSettings):
        with lock:p=project(pid)
        if not p['master']:raise HTTPException(400,'Upload your own master, load an example, or generate one first.')
        rev='rev-'+ident();version=f'0.{len(p["revisions"])+1}.0'
        is_svg=p['master'].get('kind')=='svg'
        # Multistage generation stores pendingBuildSettings; the next build
        # applies them unless the request explicitly overrides the fields.
        pending=p.get('pendingBuildSettings') or {}
        explicit=body.model_fields_set
        effective=body
        if is_svg and pending:
            changed={}
            if pending.get('auto_subdivide') is not None and 'auto_subdivide' not in explicit:
                changed['auto_subdivide']=bool(pending['auto_subdivide'])
            if pending.get('target_regions') and 'target_regions' not in explicit:
                changed['target_regions']=int(pending['target_regions'])
            if changed:
                effective=body.model_copy(update=changed)
        def run(tick):
            if is_svg:
                result=compile_svg_master(folder(pid)/p['master']['file'],folder(pid)/'revisions'/rev,
                    artwork_id=pid,version=version,title=p['title'],settings=effective,
                    provenance={'source':p['master']['source'],'sourceHash':p['master']['sha256'],
                        'rightsConfirmedByUser':p['master'].get('rightsConfirmed',False),'legalClearanceVerified':False},progress=tick)
            else:
                result=compile_image(folder(pid)/p['master']['file'],folder(pid)/'revisions'/rev,
                    artwork_id=pid,version=version,title=p['title'],settings=effective,
                    provenance={'source':p['master']['source'],'sourceHash':p['master']['sha256'],
                        'rightsConfirmedByUser':p['master'].get('rightsConfirmed',False),'legalClearanceVerified':False},progress=tick)
            reply={'revision':{'id':rev,'version':version,'createdAt':now(),'kind':'build','sourceHash':p['master']['sha256'],
                'regionCount':result['manifest']['regionCount'],'qa':result['validation'],
                'manifestUrl':f'/api/projects/{pid}/revisions/{rev}/files/artwork.json'}}
            if pending and effective is not body:
                reply['consumePending']=True   # one-shot: applied by this build
            return reply
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
    @app.post('/api/projects/{pid}/revisions/{revision}/playtest')
    def playtest(pid: str, revision: str, body: PlaytestRecord):
        """Record a play-test run against a revision (stage-3 contract B).

        Appends {recordedAt, seconds, filled, total, mistakes, mode} to the
        revision's playtests.json (newest 50 kept), recomputes the difficulty
        profile WITH the play-tests and patches the manifest difficulty block
        in place; the rest of the manifest stays untouched.
        """
        with lock:
            p = project(pid); editable(p)
            d = revision_dir(pid, revision)
            # Payload hardening: a malformed playtest must not distort the
            # difficulty calibration (filled can never exceed total, and total
            # must be this revision's actual playable region count).
            region_count = read_json(d / 'artwork.json').get('regionCount')
            if body.filled > body.total:
                raise HTTPException(400, 'filled cannot exceed total.')
            if region_count is not None and body.total != int(region_count):
                raise HTTPException(400, f"total must equal this revision's region count ({region_count}).")
            entries = []
            pt = d / 'playtests.json'
            if pt.is_file():
                try:
                    loaded = read_json(pt)
                    if isinstance(loaded, list):
                        entries = loaded
                except Exception:
                    entries = []
            entries.append({'recordedAt': now(), **body.model_dump()})
            entries = entries[-50:]          # keep the newest 50 runs
            write_json(pt, entries)
            profile = difficulty_profile(load_bundle(d), entries)
            manifest = read_json(d / 'artwork.json')
            manifest['difficulty'] = profile
            # Only completed PUZZLE runs (number/memory/duel) validate the
            # rating; free-color completions are engagement data only.
            completed = [e for e in entries if e.get('filled', 0) >= e.get('total', 0)
                         and e.get('mode') in (None, 'number', 'memory', 'duel')]
            manifest['difficultyValidatedByPlaytest'] = bool(completed)
            write_json(d / 'artwork.json', manifest)
        return {'manifest': {'id': manifest['id'], 'version': manifest['version'],
                             'regionCount': manifest['regionCount'],
                             'difficulty': manifest['difficulty'],
                             'difficultyValidatedByPlaytest': manifest['difficultyValidatedByPlaytest']},
                'playtestCount': len(entries),
                'medianSeconds': profile['metrics'].get('playtestMedianSeconds'),
                'freePlayCount': profile['metrics'].get('freePlayCount')}
    @app.get('/api/projects/{pid}/revisions/{revision}/files/{name}')
    def artifact(pid:str,revision:str,name:str):
        if name not in FILES:raise HTTPException(404)
        f=revision_dir(pid,revision)/name
        if not f.is_file():raise HTTPException(404)
        return FileResponse(f)

    @app.get('/api/projects/{pid}/revisions/{revision}/geometry')
    def geometry_mode(pid:str,revision:str,mode:str='curved'):
        """Zoom-lab payload. mode=curved returns the authoritative masters;
        mode=legacy returns the same regions as pre-upgrade pixel-edge
        polygons (exact for raster builds, simulated snap for SVG masters)."""
        if mode not in ('curved','legacy'):raise HTTPException(400,'mode must be curved or legacy.')
        b=load_bundle(revision_dir(pid,revision));g=b['geometry']
        if mode=='legacy':return legacy_geometry(g)
        regions=[{'id':r['id'],'paletteId':r['paletteId'],'fillRule':'evenodd',
                  'd':r['d'],'label':r['label'],'bbox':r['bbox']} for r in g['regions']]
        out={k:v for k,v in g.items() if k not in ('regions','decorations','detailPaths','importReport')}
        out['regions']=regions;out['mode']='curved'
        return out
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
    # -----------------------------------------------------------------------
    # Generation Orchestrator routes (Phase 2A)
    # -----------------------------------------------------------------------
    @app.post('/api/projects/{pid}/generation/sessions')
    def create_generation_session_route(pid: str, body: CreateSessionRequest):
        with lock:
            p = project(pid)
            editable(p)
            sm = GenerationSessionManager(folder(pid), pid)
            session = sm.create_session(
                mode=body.mode,
                requested_difficulty=body.requested_difficulty,
                prompt=body.prompt,
                aspect=body.aspect,
                fidelity=body.fidelity or 'balanced',
            )
            return session

    @app.get('/api/projects/{pid}/generation/sessions')
    def list_generation_sessions_route(pid: str):
        sm = GenerationSessionManager(folder(pid), pid)
        return {'sessions': sm.list_sessions()}

    @app.get('/api/projects/{pid}/generation/sessions/{sid}')
    def get_generation_session_route(pid: str, sid: str):
        sm = GenerationSessionManager(folder(pid), pid)
        try:
            return sm.get_session(sid)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, f'Session {sid} not found.')

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/mutate')
    def mutate_session_plan_route(pid: str, sid: str, body: MutateScenePlanRequest):
        with lock:
            p = project(pid)
            editable(p)
            sm = GenerationSessionManager(folder(pid), pid)
            try:
                return sm.mutate_plan(sid, body.mutations)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc))

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/cancel')
    def cancel_generation_session_route(pid: str, sid: str):
        with lock:
            sm = GenerationSessionManager(folder(pid), pid)
            try:
                return sm.cancel_session(sid)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc))

    @app.delete('/api/projects/{pid}/generation/sessions/{sid}')
    def delete_generation_session_route(pid: str, sid: str):
        with lock:
            sm = GenerationSessionManager(folder(pid), pid)
            try:
                sm.discard_session(sid)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {'ok': True}

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/compile')
    def compile_session_route(pid: str, sid: str, master_file: UploadFile | None = None):
        with lock:
            p = project(pid)
            editable(p)
            sm = GenerationSessionManager(folder(pid), pid)
            try:
                sdir = sm.session_path(sid)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            target_svg = sdir / 'source-master.svg'
            if master_file:
                content = master_file.file.read()
                try:
                    clean_svg(content, target_svg)
                except ValueError as exc:
                    sm.update_status(sid, 'failed', error=str(exc))
                    raise HTTPException(400, f'Invalid SVG master: {exc}')
            if not target_svg.is_file():
                raise HTTPException(400, 'Session has no master SVG to compile.')

        def run(tick):
            tick(0.2, 'Compiling session bundle')
            try:
                res = sm.compile_session(sid, target_svg, progress=tick)
                return {'session': sm.get_session(sid), 'validation': res['validation']}
            except Exception as exc:
                sm.update_status(sid, 'failed', error=str(exc))
                raise

        return start(pid, 'session vector compilation', run)

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/commit')
    def commit_session_route(pid: str, sid: str, body: CommitSessionRequest = CommitSessionRequest()):
        with lock:
            p = project(pid)
            editable(p)
            sm = GenerationSessionManager(folder(pid), pid)
            version = f'0.{len(p["revisions"]) + 1}.0'
            title = body.title or p['title']
            try:
                res = sm.commit_session(sid, version=version, title=title)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc))

            p['revisions'].append(res['revision'])
            p['currentRevision'] = res['revision']['id']

            # If session produced a master SVG, promote it to project master
            sdir = sm.session_path(sid)
            draft_svg = sdir / 'source-master.svg'
            if draft_svg.is_file():
                dest_name = f'master-{ident()}.svg'
                shutil.copy2(draft_svg, folder(pid) / dest_name)
                im = clean_svg(draft_svg.read_bytes(), folder(pid) / dest_name)
                p['master'] = {
                    'file': dest_name,
                    **im,
                    'source': f"AI-generated ({res['manifest']['generation']['mode']})",
                    'rightsConfirmed': False,
                    'createdAt': now(),
                }
            save(p)
            return res

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/plan')
    def plan_session_route(pid: str, sid: str, body: dict = {}):
        # Paid step 1 of Create-with-AI: strict-JSON scene planning. Drafts a
        # revisable ScenePlan; no vector fragments are purchased yet.
        with lock:
            p = project(pid)
            editable(p)
            if not (body or {}).get('confirm_paid'):
                raise HTTPException(400, 'Confirm the paid provider request first: this sends the prompt to the AI provider and may incur charges.')
            if not provider.config()['configured']:
                raise HTTPException(503, 'AI not configured. Add OPENAI_API_KEY to .env. Upload-to-vector works without it.')
            sm = GenerationSessionManager(folder(pid), pid)

        def run(tick):
            session, usage = sm.plan_session_with_ai(sid, provider,
                                                     instructions=str((body or {}).get('instructions') or ''),
                                                     progress=tick)
            return {'session': session, 'usage': {'kind': 'scene-plan', 'at': now(), **usage}}

        return start(pid, 'AI scene planning', run)

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/generate')
    def generate_session_route(pid: str, sid: str, body: dict = {}):
        # Paid step 2 of Create-with-AI: one vector fragment per planned
        # object, composed + compiled + QA'd inside the session workspace.
        with lock:
            p = project(pid)
            editable(p)
            if not (body or {}).get('confirm_paid'):
                raise HTTPException(400, 'Confirm the paid provider request first: this runs one AI call per planned object and may incur charges.')
            if not provider.config()['configured']:
                raise HTTPException(503, 'AI not configured. Add OPENAI_API_KEY to .env. Upload-to-vector works without it.')
            sm = GenerationSessionManager(folder(pid), pid)

        def run(tick):
            session, usage = sm.generate_session_master(sid, provider, progress=tick)
            return {'session': session, 'usage': {'at': now(), **usage}}

        return start(pid, 'AI artwork synthesis', run)

    @app.post('/api/projects/{pid}/generation/sessions/{sid}/regenerate-object')
    def regenerate_session_object_route(pid: str, sid: str, body: dict = {}):
        # Paid targeted regeneration: replace one object's shapes (objectId is
        # preserved), recompile, QA — everything else keeps its geometry.
        with lock:
            p = project(pid)
            editable(p)
            body = body or {}
            if not body.get('confirm_paid'):
                raise HTTPException(400, 'Confirm the paid provider request first: this sends the object brief to the AI provider and may incur charges.')
            if not provider.config()['configured']:
                raise HTTPException(503, 'AI not configured. Add OPENAI_API_KEY to .env. Upload-to-vector works without it.')
            object_id = str(body.get('objectId') or '')
            if not object_id:
                raise HTTPException(400, 'objectId is required.')
            sm = GenerationSessionManager(folder(pid), pid)

        def run(tick):
            session, usage = sm.regenerate_session_object(sid, provider, object_id,
                                                          instructions=str(body.get('instructions') or ''),
                                                          progress=tick)
            return {'session': session, 'usage': {'at': now(), **usage}}

        return start(pid, 'targeted object regeneration', run)

    app.mount('/static',StaticFiles(directory=BASE/'web'),name='static')
    @app.get('/')
    def index():return FileResponse(BASE/'web/index.html')
    return app

app=create_app()
