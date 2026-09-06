"""Offline UI component test using an in-process FastAPI client.
No browser networking, no AI calls and no browser-policy modifications.
This does not replace a direct-HTTP end-to-end test on the user's machine.
"""
import base64,json,os,tempfile,zipfile
from pathlib import Path
from urllib.parse import urlparse
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from studio.app import create_app
ROOT=Path(__file__).resolve().parents[1]
OUT=Path(os.getenv('STUDIO_TEST_OUTPUT','tests/offline-output'));OUT.mkdir(parents=True,exist_ok=True)
os.environ['OPENAI_API_KEY']=''
checks=[]
def check(name,condition):
    assert condition,name
    checks.append(name)
with tempfile.TemporaryDirectory() as td, TestClient(create_app(Path(td))) as client, sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=os.getenv('CHROMIUM_BIN','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def request_bridge(args):
        url=urlparse(args['url']);path=url.path+('?' + url.query if url.query else '')
        response=client.request(args.get('method','GET'),path,headers=args.get('headers',{}),content=args.get('body'))
        return {'status':response.status_code,'headers':dict(response.headers),'data':base64.b64encode(response.content).decode()}
    page.expose_function('testApiBridge',request_bridge)
    html=(ROOT/'web/index.html').read_text().replace('<link rel="stylesheet" href="/static/studio.css">','').replace('<script type="module" src="/static/studio.mjs"></script>','')
    html=html.replace('<head>','<head><base href="http://testserver/">')
    page.set_content(html)
    page.add_style_tag(content=(ROOT/'web/studio.css').read_text())
    page.add_script_tag(content='''
    Object.defineProperty(window,'localStorage',{value:{getItem:k=>null,setItem:()=>{},removeItem:()=>{}}});
    window.fetch=async(url,options={})=>{const h=Object.fromEntries(new Headers(options.headers||{}));const v=await window.testApiBridge({url:new URL(url,document.baseURI).href,method:options.method||'GET',headers:h,body:options.body||null});return new Response(Uint8Array.from(atob(v.data),c=>c.charCodeAt(0)),{status:v.status,headers:v.headers});};
    const nativeSrc=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
    Object.defineProperty(HTMLImageElement.prototype,'src',{get(){return nativeSrc.get.call(this)},set(value){if(String(value).startsWith('data:'))return nativeSrc.set.call(this,value);window.testApiBridge({url:new URL(value,document.baseURI).href}).then(r=>nativeSrc.set.call(this,'data:'+(r.headers['content-type']||'image/png')+';base64,'+r.data));}});
    ''')
    renderer=(ROOT/'web/detailed-board.mjs').read_text().replace('export async function','async function').replace('export function','function').replace('export class','class')
    page.add_script_tag(content='(()=>{'+renderer+'\nwindow.StudioRenderer={loadArtwork,VectorBoard};})();')
    ui=(ROOT/'web/studio.mjs').read_text().replace("import {loadArtwork,VectorBoard} from './detailed-board.mjs';","const {loadArtwork,VectorBoard}=window.StudioRenderer;")
    page.add_script_tag(content='(()=>{'+ui+'})();')
    page.wait_for_selector('#project-list option',state='attached')
    check('Local compiler status shown','Local compiler ready' in page.locator('#ai-status').inner_text())
    check('AI disabled with no key',page.locator('#generate').is_disabled() and page.locator('#send-chat').is_disabled())
    page.locator('#sample').click();page.wait_for_function("!document.querySelector('#master-image').hidden && document.querySelector('#master-image').naturalWidth>0")
    check('Bundled source appears',page.locator('#master-image').is_visible())
    page.locator('#max-edge').select_option('768');page.locator('#target-regions').fill('750');page.locator('#target-regions').dispatch_event('input')
    page.locator('#build').click()
    page.wait_for_function("!!window.studioBoard && !document.querySelector('#board').hasAttribute('hidden')",timeout=120000)
    check('Compiler produces detailed regions',page.evaluate('studioBoard.regions.size')>300)
    check('Detailed vector painting present',page.locator('#board [data-layer="vector-paint"] path').count()>0)
    check('No embedded raster in SVG',page.locator('#board image').count()==0)
    check('QA pass displayed','Geometry checks passed' in page.locator('#qa').inner_text())
    page.screenshot(path=str(OUT/'studio-desktop.png'),full_page=True)
    page.locator('[data-view="play"]').click()
    r=page.evaluate('() => [...studioBoard.regions.values()].sort((a,b)=>b.area-a.area)[0]')
    page.locator(f'#palette [data-palette="{r["paletteId"]}"]').click()
    def position():return page.evaluate('id=>{let r=studioBoard.regions.get(id),p=new DOMPoint(r.label.x,r.label.y).matrixTransform(studioBoard.svg.getScreenCTM());return {x:p.x,y:p.y}}',r['id'])
    pos=position();page.mouse.click(pos['x'],pos['y'])
    check('Pointer fills correct region',page.evaluate('id=>studioBoard.completed.has(id)',r['id']))
    check('Number hidden after fill',page.evaluate('id=>studioBoard.labels.get(id).style.display',r['id'])=='none')
    page.locator('#undo').click();check('Undo restores region',not page.evaluate('id=>studioBoard.completed.has(id)',r['id']))
    wrong=1 if r['paletteId']!=1 else 2;page.locator(f'#palette [data-palette="{wrong}"]').click()
    pos=position();page.mouse.click(pos['x'],pos['y'])
    check('Wrong palette rejected',not page.evaluate('id=>studioBoard.completed.has(id)',r['id']))
    check('Mistake counted',page.evaluate('studioBoard.session.mistakes')==1)
    page.locator('#zoom-in').click();check('Zoom works',page.evaluate('studioBoard.state().zoom')>1)
    page.locator('#fit').click();check('Fit works',page.evaluate('studioBoard.state().zoom')==1)
    page.locator('[data-view="inspect"]').click();pos=position();page.mouse.click(pos['x'],pos['y'])
    check('Inspector selects region','1 selected' in page.locator('#selection-info').inner_text())
    before=page.evaluate('studioBoard.bundle.manifest.version');page.locator('#object-group').fill('roof');page.locator('#assign-group').click()
    page.wait_for_function('old=>window.studioBoard && studioBoard.bundle.manifest.version!==old',arg=before,timeout=120000)
    check('Group edit creates new revision',page.evaluate('studioBoard.bundle.manifest.objectGroups[0].id')=='roof')
    page.locator('[data-view="numbered"]').click();page.locator('#zoom-in').click();page.locator('#zoom-in').click()
    check('Numbers visible at useful zoom',page.locator('#board text:visible').count()>0)
    page.screenshot(path=str(OUT/'studio-numbered.png'),full_page=True)
    export_url=page.locator('#export').get_attribute('href');raw=client.get(export_url).content
    import io
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        check('Backend export ZIP intact',z.testzip() is None)
        check('Export contains region geometry',any(n.endswith('/regions.json') for n in z.namelist()))
        check('Export contains paint layer',any(n.endswith('/paint.json') for n in z.namelist()))
    page.set_viewport_size({'width':390,'height':844});page.locator('[data-view="colored"]').click();page.locator('#fit').click();page.wait_for_timeout(700)
    check('Mobile no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1'))
    page.screenshot(path=str(OUT/'studio-mobile.png'),full_page=True)
    check('No JS exceptions',not errors)
    check('Project revisions persisted',len(client.get('/api/projects').json()[0]['revisions'])==2)
    browser.close()
(OUT/'report.json').write_text(json.dumps({'scope':'Offline browser component tests with in-process FastAPI bridge. No browser HTTP networking or live AI.','passed':len(checks),'checks':checks,'errors':errors},indent=2))
print(json.dumps({'passed':len(checks),'checks':checks,'errors':errors},indent=2))
