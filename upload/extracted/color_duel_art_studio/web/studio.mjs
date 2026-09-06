import {loadArtwork,VectorBoard} from './detailed-board.mjs';
const $=id=>document.getElementById(id);
let project=null,config=null,board=null,bundle=null,view='master',uploadRole='reference',loadedRevision=null;
let selected=new Set(),placing=false,pollTimer=null,toastTimer=null,loadingToken=0;
const busy=()=>['queued','running'].includes(project?.job?.status);
async function api(path,options={}){
  const res=await fetch('/api'+path,{...options,headers:{'X-Studio-Request':'1',...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...options.headers}});
  if(!res.ok){let detail;try{detail=(await res.json()).detail}catch{detail=res.statusText}throw new Error(typeof detail==='string'?detail:JSON.stringify(detail));}
  return res.json();
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
function handle(id,fn){$(id).onclick=async()=>{try{await fn()}catch(e){toast(e.message)}};}
function projectPath(s=''){if(!project)throw new Error('Create a project first.');return '/projects/'+project.id+s;}
function revision(){return project?.revisions.find(r=>r.id===project.currentRevision);}
function fileUrl(name){return '/api'+projectPath('/revisions/'+project.currentRevision+'/files/'+name);}
function clearBoard(){loadingToken++;board?.destroy();board=null;bundle=null;loadedRevision=null;selected.clear();}
function setLink(id,url){const el=$(id);el.href=url||'#';el.classList.toggle('disabled',!url);el.setAttribute('aria-disabled',String(!url));}
function renderChat(){const box=$('messages');box.replaceChildren();if(!project.messages.length){const p=document.createElement('p');p.className='chat-empty';p.textContent='Describe an original scene. Chat refines the brief; generating an image is a separate action.';box.append(p);}for(const m of project.messages){const p=document.createElement('p');p.className='bubble '+m.role;p.textContent=m.content;box.append(p)}box.scrollTop=box.scrollHeight;}
function paintPalette(){if(!bundle)return;$('palette').replaceChildren();for(const p of bundle.palette){const b=document.createElement('button');b.className='swatch';b.dataset.palette=p.id;b.textContent=p.number;b.title=`${p.name} · group ${p.id}`;b.style.background=p.hex;const rgb=p.hex.match(/\w\w/g).map(v=>parseInt(v,16));b.style.color=(rgb[0]*.299+rgb[1]*.587+rgb[2]*.114)>150?'#152a2a':'white';b.onclick=()=>{board.setPalette(p.id);$('edit-palette').value=p.id;};$('palette').append(b)}}
function boardChange(state,reason){$('zoom-value').textContent=Math.round(state.zoom*100)+'%';$('progress-text').textContent=`${state.completed} / ${state.total} regions filled · ${state.mistakes} incorrect attempts`;
 document.querySelectorAll('.swatch').forEach(b=>b.classList.toggle('active',Number(b.dataset.palette)===state.selectedPaletteId));
 if(reason==='wrong-color')toast('That region needs a different palette group.');
 if(view==='inspect')highlightSelection();}
function highlightSelection(){if(!board)return;for(const [id,el] of board.elements)el.classList.toggle('selected-region',selected.has(id));$('selection-info').textContent=selected.size?`${selected.size} selected · ${[...selected].slice(0,4).join(', ')}${selected.size>4?'…':''}`:'Tap regions to inspect or select them. Drag to pan.';}
async function mountBoard(){const r=revision();if(!r||loadedRevision===r.id)return;clearBoard();const token=++loadingToken;const b=await loadArtwork(r.manifestUrl);if(token!==loadingToken)return;bundle=b;loadedRevision=r.id;
 board=new VectorBoard($('board'),bundle,{persist:false,onChange:boardChange});
 const normalPaint=board.paint.bind(board);
 board.paint=id=>{if(view!=='inspect')return normalPaint(id);if(!id)return 'ignored';if(placing){const p=board.lastTapPoint;if(!p)return 'ignored';placing=false;$('place-label').textContent='Place number';runEdit('label',{x:p.x,y:p.y}).catch(e=>toast(e.message));return 'inspected';}if(selected.has(id))selected.delete(id);else selected.add(id);if(selected.size===1){const reg=board.regions.get(id);$('edit-palette').value=reg.paletteId;$('object-group').value=reg.objectId==='unassigned'?'roof':reg.objectId;}highlightSelection();return 'inspected';};
 const convert=board.clientToArt.bind(board);board.clientToArt=(...args)=>{const pt=convert(...args);board.lastTapPoint=pt;return pt};
 paintPalette();applyView();window.studioBoard=board; // Exposes local tester state for integration tests/debugging only.
}
function applyView(){const hasMaster=!!project?.master,hasBundle=!!bundle;$('empty-state').hidden=hasMaster||hasBundle;$('master-image').hidden=!(view==='master'&&hasMaster);$('board').toggleAttribute('hidden',view==='master'||!hasBundle);
 $('palette-wrap').hidden=!hasBundle||view==='master'||view==='colored';$('inspector').hidden=view!=='inspect';$('canvas-tag').hidden=!hasBundle||view==='master';$('canvas-tag').textContent=view==='inspect'?'Select · merge · group · fix labels':view==='play'?'Play test · separate from game progress':'Draft · visual review required';
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
 if(board){board.setPreview(view==='colored'||view==='inspect');highlightSelection()}
}
function renderProject(syncFields=false){if(!project)return;$('workspace-title').textContent=project.title;document.body.classList.toggle('busy',busy());if(syncFields){$('title').value=project.title;$('brief').value=project.brief;renderChat()}
 $('reference-image').hidden=!project.reference;$('reference-placeholder').hidden=!!project.reference;if(project.reference)$('reference-image').src='/api'+projectPath('/image/reference')+'?v='+project.reference.sha256;
 if(project.master)$('master-image').src='/api'+projectPath('/image/master')+'?v='+project.master.sha256;
 const r=revision();$('revision-badge').textContent=r?`v${r.version} · ${r.kind}`:'No vector revision';$('stat-regions').textContent=r?.regionCount??'—';$('stat-palette').textContent=r?.qa.paletteGroups??'—';$('stat-paint').textContent=r?.qa.paintPaths??'—';
 const qa=$('qa');qa.replaceChildren();if(r){const pass=document.createElement('p');pass.className='pass';pass.textContent=r.qa.passed?'✓ Geometry checks passed':'Geometry needs attention';qa.append(pass);if(r.qa.humanReviewed){const p=document.createElement('p');p.className='reviewed';p.textContent='✓ Self-attested visual review recorded';qa.append(p)}for(const warning of r.qa.warnings){const p=document.createElement('p');p.className='warn';p.textContent=warning;qa.append(p)}}else{qa.textContent='Build a draft to check geometry, labels and tap-target sizes.'}
 $('revision-list').replaceChildren();for(const rev of [...project.revisions].reverse()){const o=document.createElement('option');o.value=rev.id;o.textContent=`v${rev.version} · ${rev.kind} · ${rev.regionCount} regions`;o.selected=rev.id===project.currentRevision;$('revision-list').append(o)}
 const root=r?'/api'+projectPath('/revisions/'+r.id):null;setLink('export',root&&root+'/export');setLink('export-authoring',root&&root+'/export?authoring=true');setLink('export-png',root&&root+'/render?width=2048');
 const j=project.job||{};$('job-message').textContent=j.message||'Local tools ready. No cloud request is made until you confirm.';$('job-message').className=j.status==='failed'?'job-failed':busy()?'job-busy':'';$('job-percent').textContent=busy()?Math.round((j.progress||0)*100)+'%':'';$('job-progress').value=j.progress||0;
 $('send-chat').disabled=!config?.ai.configured||busy();$('generate').disabled=!config?.ai.configured||busy();$('build').disabled=!project.master||busy();
 if(!r&&loadedRevision)clearBoard();applyView();}
async function refreshList(){const items=await api('/projects');$('project-list').replaceChildren();for(const p of items){const o=document.createElement('option');o.value=p.id;o.textContent=p.title;o.selected=p.id===project?.id;$('project-list').append(o)}return items;}
async function openProject(p){clearTimeout(pollTimer);clearBoard();project=p;view=p.currentRevision?'colored':'master';localStorage.setItem('studio-project',p.id);renderProject(true);await refreshList();if(p.currentRevision)await mountBoard();if(busy())poll();}
async function ensureProject(){if(project)return;await openProject(await post('/projects',{title:'New illustrated world'}));}
async function poll(){clearTimeout(pollTimer);const pid=project.id;const previousJob=project.job?.id;try{const next=await api('/projects/'+pid);if(project.id!==pid)return;const changed=project.currentRevision!==next.currentRevision;project=next;renderProject(!busy());if(changed&&project.currentRevision){view='colored';await mountBoard();await refreshList()}if(busy()){pollTimer=setTimeout(poll,900)}else if(project.job?.status==='failed'){toast(project.job.message)}}catch(e){toast('Job polling stopped: '+e.message)}}
async function job(path,body){await post(projectPath(path),body);project=await api(projectPath());renderProject();poll();}
async function saveBrief(){project=await api(projectPath(),{method:'PATCH',body:JSON.stringify({title:$('title').value.trim()||'Untitled artwork',brief:$('brief').value})});renderProject();await refreshList();}
async function runEdit(action,extra={}){if(!selected.size)throw new Error('Select at least one region in Edit regions.');if(busy())throw new Error('Wait for the current job.');const body={base_revision:project.currentRevision,action,region_ids:[...selected],...extra};await job('/edit',body);selected.clear();}
handle('new-project',async()=>openProject(await post('/projects',{title:'New illustrated world'})));
$('project-list').onchange=async()=>{try{await openProject(await api('/projects/'+$('project-list').value))}catch(e){toast(e.message)}};
handle('save-brief',async()=>{await saveBrief();toast('Brief saved locally.');});
handle('upload-reference',()=>{uploadRole='reference';$('file-input').click()});handle('upload-master',()=>{if(!$('rights').checked)throw new Error('Confirm ownership or permission before importing a master.');uploadRole='master';$('file-input').click()});
$('file-input').onchange=async()=>{try{await ensureProject();const file=$('file-input').files[0];if(!file)return;const form=new FormData();form.append('file',file);form.append('role',uploadRole);form.append('rights_confirmed',String($('rights').checked));project=await api(projectPath('/upload'),{method:'POST',body:form});if(uploadRole==='master')view='master';renderProject(true);toast(uploadRole==='master'?'Master ready. Build a vector draft.':'Reference added. Use chat to develop an original brief.');}catch(e){toast(e.message)}finally{$('file-input').value=''}};
handle('sample',async()=>{await ensureProject();project=await post(projectPath('/sample'));view='master';renderProject(true);toast('Example loaded. Build vector draft to convert it locally.');});
handle('promote-reference',async()=>{project=await post(projectPath('/reference-as-master'),{rights_confirmed:$('rights').checked});view='master';renderProject(true)});
handle('send-chat',async()=>{const message=$('message').value.trim();if(!message)throw new Error('Write a direction for the AI first.');await saveBrief();await job('/chat',{message,include_reference:true,confirm_paid:$('paid').checked});$('message').value='';$('paid').checked=false;});
handle('generate',async()=>{await saveBrief();await job('/generate',{prompt:$('brief').value,source:$('generation-source').value,quality:$('quality').value,size:'1024x1536',confirm_paid:$('paid').checked});$('paid').checked=false;view='master';});
handle('build',async()=>{await saveBrief();await job('/build',{target_regions:+$('target-regions').value,palette_colors:+$('palette-colors').value,paint_colors:+$('paint-colors').value,max_edge:+$('max-edge').value,compactness:+$('compactness').value,min_region_pixels:+$('min-area').value,min_label_radius:+$('min-radius').value,ink_threshold:+$('ink').value})});
$('target-regions').oninput=()=>$('target-value').textContent=$('target-regions').value;
for(const el of document.querySelectorAll('[data-view]'))el.onclick=async()=>{try{const next=el.dataset.view;if(next!=='master'&&!revision())throw new Error('Build the vector regions first.');view=next;await mountBoard();applyView()}catch(e){toast(e.message)}};
handle('zoom-in',()=>board?.zoom(1.4));handle('zoom-out',()=>board?.zoom(1/1.4));handle('fit',()=>board?.fit());handle('next-region',()=>board?.nextRegion());handle('undo',()=>board?.undo());handle('reset',()=>board?.reset());
handle('merge',()=>runEdit('merge',{palette_id:+$('edit-palette').value}));handle('assign-group',()=>runEdit('group',{group:$('object-group').value}));handle('assign-palette',()=>runEdit('palette',{palette_id:+$('edit-palette').value}));handle('decorate',()=>runEdit('decorate'));
handle('clear-selection',()=>{selected.clear();placing=false;highlightSelection()});handle('place-label',()=>{if(selected.size!==1)throw new Error('Select exactly one region first.');placing=true;$('place-label').textContent='Tap new position';toast('Tap a roomy point inside the selected region.');});
handle('activate-revision',async()=>{project=await post(projectPath('/activate'),{revision:$('revision-list').value});clearBoard();view='colored';renderProject(true);await mountBoard()});
handle('review',async()=>{if(!revision())throw new Error('Build a draft first.');const note=window.prompt('Record what you checked (boundaries, labels, small targets and completed appearance). This does not certify rights or ranked balance.');if(!note)return;project=await post(projectPath('/review'),{revision:project.currentRevision,note,confirmed:true});renderProject()});
handle('guide',()=>$('guide-dialog').showModal());handle('close-guide',()=>$('guide-dialog').close());
new ResizeObserver(()=>board?.updateLabelVisibility()).observe($('canvas-area'));
async function init(){try{config=await api('/config');$('ai-status').textContent=config.ai.configured?'Local compiler + AI connected':'Local compiler ready · AI not configured';$('ai-status').classList.add('ready');$('chat-status').textContent=config.ai.configured?config.ai.chatModel:'API key required';const items=await refreshList();const saved=localStorage.getItem('studio-project');await openProject(items.find(p=>p.id===saved)||items[0]||await post('/projects',{title:'Cascade Treehouse',brief:'An original detailed woodland treehouse beside a waterfall, with warm lanterns, a winding staircase and flowering plants. Clear contours, coherent architecture, rich shading. No text, UI, palette or gameplay numbers.'}));}catch(e){toast(e.message);$('job-message').textContent='Could not connect. Start the local server with python run.py.'}}
init();
