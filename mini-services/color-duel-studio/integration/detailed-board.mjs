/** Dependency-free region renderer. Copyright/provenance: see docs/PROVENANCE.md.
 * Consume our trusted, validated JSON; do NOT inject arbitrary uploaded SVG markup.
 * The demo is an offline solo asset tester, not an authoritative multiplayer server.
 *
 * SHARED FORMAT CONTRACT (detailed-vector schema 2) — this validator and the
 * studio's src/lib/detailed-board.ts accept the same field set, and the studio's
 * export pipeline (pipeline.validate_runtime_contract) mirrors these rules:
 *  - geometry.geometrySchema 1 (legacy polygons) or 2 (curved masters)
 *  - regions[*].d uses M/L/C/Q/Z path commands (curves preserved)
 *  - regions[*].fillRule is 'evenodd' (default) or 'nonzero' (source rule kept)
 *  - paint.paths[*] fill is #RRGGBB or url(#g-...) referencing paint.gradients
 *  - paint paths carry optional fillRule/fillOpacity/opacity/stroke/strokeWidth/z
 *  - paint.inkPaths may be OPEN paths (stroke line art) when strokeWidth/filled=false
 *  - Regions are VISIBLE SURFACES: masks do not overlap, so any fill order
 *    colors correctly (fill-order independence).
 */
const NS = 'http://www.w3.org/2000/svg';
let sequence = 0;
const clamp = (x, min, max) => Math.max(min, Math.min(max, x));
function svgNode(tag, attrs = {}) {
  const el = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}

const SAFE_PATH = /^M[\s\d.,eE+\-MLQCZ]+Z$/;
const SAFE_PATH_OPEN = /^M[\s\d.,eE+\-MLQCZ]+$/;
const SAFE_HEX = /^#[0-9A-Fa-f]{6}$/;
const SAFE_GRADIENT_REF = /^url\(#g-[a-zA-Z0-9_-]+\)$/;
const SAFE_GRADIENT_ID = /^g-[a-zA-Z0-9_-]+$/;
const finite = (v) => Number.isFinite(v);

export function validateBundle(bundle) {
  const {manifest: m, geometry: g, palette: p} = bundle || {};
  if (!m || !g || !Array.isArray(p) || !['color-duel-vector-1','color-duel-detailed-vector-1'].includes(m.format)) throw new Error('Unsupported artwork bundle');
  if (m.id !== g.artworkId || m.version !== g.artworkVersion || m.regionCount !== g.regions?.length) throw new Error('Artwork identity/count mismatch');
  if (!Array.isArray(g.viewBox) || g.viewBox.length !== 4 || g.viewBox.some(x => !Number.isFinite(x)) || g.viewBox[2] <= 0 || g.viewBox[3] <= 0) throw new Error('Invalid viewBox');
  const paletteIds = new Set(p.map(x => x.id));
  if (paletteIds.size !== p.length) throw new Error('Duplicate palette ID');
  const ids = new Set();
  for (const r of [...g.regions, ...g.decorations]) {
    if (ids.has(r.id) || !/^[a-zA-Z0-9_-]+$/.test(r.id)) throw new Error('Duplicate or unsafe region ID');
    ids.add(r.id);
    const rule = r.fillRule === undefined ? 'evenodd' : r.fillRule;
    if (!paletteIds.has(r.paletteId) || !['evenodd','nonzero'].includes(rule) || !SAFE_PATH.test(r.d)) throw new Error('Invalid region geometry or palette');
  }
  if (m.format === 'color-duel-detailed-vector-1') {
    const paint = bundle.paint;
    if (!paint || paint.artworkId !== m.id || !Array.isArray(paint.paths) || !Array.isArray(paint.inkPaths)) throw new Error('Missing detailed vector paint');
    const gradientIds = new Set((paint.gradients ?? []).map(x => x.id));
    for (const gr of paint.gradients ?? []) {
      if (!SAFE_GRADIENT_ID.test(gr.id) || !['linear','radial'].includes(gr.type) || !Array.isArray(gr.stops) || !gr.stops.length) throw new Error('Invalid gradient definition');
      for (const s of gr.stops) if (!SAFE_HEX.test(s.color) || !finite(s.offset)) throw new Error('Invalid gradient stop');
    }
    for (const path of paint.paths) {
      const fill = path.fill;
      const okFill = SAFE_HEX.test(fill) || (SAFE_GRADIENT_REF.test(fill) && gradientIds.has(fill.slice(5, -1)));
      if (!okFill || !SAFE_PATH.test(path.d)) throw new Error('Invalid paint path');
      if (path.fillRule !== undefined && !['evenodd','nonzero'].includes(path.fillRule)) throw new Error('Invalid paint fill rule');
      for (const key of ['opacity','fillOpacity','strokeWidth','z']) {
        const v = path[key];
        if (v !== undefined && !finite(v)) throw new Error(`Invalid paint ${key}`);
      }
      if (path.opacity !== undefined && (path.opacity < 0 || path.opacity > 1)) throw new Error('Invalid paint opacity');
      if (path.fillOpacity !== undefined && (path.fillOpacity < 0 || path.fillOpacity > 1)) throw new Error('Invalid paint fill-opacity');
    }
    // Ink layer holds closed filled shapes OR open stroke line art.
    for (const path of paint.inkPaths) {
      const fill = path.fill;
      const okFill = SAFE_HEX.test(fill) || (SAFE_GRADIENT_REF.test(fill) && gradientIds.has(fill.slice(5, -1)));
      const okPath = path.strokeWidth != null || path.filled === false ? SAFE_PATH_OPEN.test(path.d) : SAFE_PATH.test(path.d);
      if (!okFill || !okPath) throw new Error('Invalid ink path');
    }
  }
  return bundle;
}

/** URL must point at artwork.json. Relative paths are resolved beside that manifest. */
export async function loadArtwork(manifestUrl, {signal} = {}) {
  async function get(url) {
    const res = await fetch(url, {signal});
    if (!res.ok) throw new Error(`Artwork request failed: ${res.status} (${url})`);
    return res.json();
  }
  const absolute = new URL(manifestUrl, document.baseURI).href;
  const manifest = await get(absolute);
  const [geometry, palette, paint] = await Promise.all([
    get(new URL(manifest.assets.regions, absolute)),
    get(new URL(manifest.assets.palette, absolute)),
    manifest.assets.paint ? get(new URL(manifest.assets.paint, absolute)) : Promise.resolve(null)
  ]);
  return validateBundle({manifest, geometry, palette, paint});
}

/** Drop stale/unknown IDs; never restore progress across different geometry hashes. */
export function normalizeSession(bundle, raw, mode = 'number') {
  validateBundle(bundle);
  const m = bundle.manifest, valid = new Set(bundle.geometry.regions.map(r => r.id));
  const ids = new Set(mode === 'free' ? bundle.palette.map(p => p.id) : bundle.geometry.regions.map(r => r.paletteId));
  const clean = {schemaVersion:1, artworkId:m.id, artworkVersion:m.version,
    contentHash:m.contentHash, mode, completedRegionIds:[], selectedPaletteId:[...ids].sort((a,b)=>a-b)[0],
    freeColors:{}, mistakes:0, updatedAt:new Date().toISOString()};
  if (!raw || raw.schemaVersion !== 1 || raw.artworkId !== m.id || raw.artworkVersion !== m.version || raw.contentHash !== m.contentHash || raw.mode !== mode) return clean;
  clean.completedRegionIds = [...new Set(Array.isArray(raw.completedRegionIds) ? raw.completedRegionIds.filter(id => valid.has(id)) : [])];
  if (ids.has(raw.selectedPaletteId)) clean.selectedPaletteId = raw.selectedPaletteId;
  if (Number.isSafeInteger(raw.mistakes) && raw.mistakes >= 0) clean.mistakes = raw.mistakes;
  if (mode === 'free' && raw.freeColors && typeof raw.freeColors === 'object') {
    const allIds = new Set(bundle.palette.map(p => p.id));
    for (const id of clean.completedRegionIds) if (allIds.has(raw.freeColors[id])) clean.freeColors[id] = raw.freeColors[id];
    clean.completedRegionIds = clean.completedRegionIds.filter(id => id in clean.freeColors);
  }
  return clean;
}

export class VectorBoard {
  constructor(svg, bundle, options = {}) {
    if (!(svg instanceof SVGSVGElement)) throw new Error('Pass an <svg> element to VectorBoard');
    validateBundle(bundle);
    this.svg = svg; this.bundle = bundle; this.options = options;
    this.mode = options.mode || 'number';
    if (!['number','memory','free'].includes(this.mode)) throw new Error('Invalid coloring mode');
    this.prefix = `cdv-${++sequence}-`;
    this.handlers = []; this.pointers = new Map(); this.history = []; this.preview = false;
    this.ctx = document.createElement('canvas').getContext('2d');
    if (!this.ctx) throw new Error('Canvas hit-testing is unavailable');
    this.regions = new Map(bundle.geometry.regions.map(r => [r.id, r]));
    this.paths = new Map([...this.regions].map(([id,r]) => [id,new Path2D(r.d)]));
    this.storageKey = `color-duel:vector:${bundle.manifest.id}:${bundle.manifest.version}:${bundle.manifest.contentHash}:${this.mode}`;
    let stored = options.session || null;
    if (!stored && options.persist !== false) {
      try { stored = JSON.parse(localStorage.getItem(this.storageKey) || 'null'); } catch { this.storageWarning = true; }
    }
    this.session = normalizeSession(bundle,stored,this.mode);
    this.completed = new Set(this.session.completedRegionIds);
    this.base = [...bundle.geometry.viewBox]; this.view = [...this.base];
    this.mount(); this.bindGestures(); this.refresh();
  }
  mount() {
    this.svg.replaceChildren();
    this.svg.setAttribute('viewBox', this.view.join(' '));
    this.svg.setAttribute('aria-label', `${this.bundle.manifest.title}, interactive coloring artwork`);
    this.svg.setAttribute('role','group');
    this.svg.style.touchAction = 'none';
    const defs = svgNode('defs');
    for (const p of this.bundle.palette) {
      const grad = svgNode('linearGradient',{id:`${this.prefix}paint-${p.id}`, x1:'0%',y1:'0%',x2:'100%',y2:'100%'});
      for (const stop of p.paint.stops) grad.append(svgNode('stop',{offset:stop.offset,'stop-color':stop.color}));
      defs.append(grad);
    }
    if (this.bundle.paint?.gradients) {
      for (const g of this.bundle.paint.gradients) {
        const attrs = g.type === 'linear'
          ? {id: g.id, gradientUnits: 'userSpaceOnUse', x1: g.x1, y1: g.y1, x2: g.x2, y2: g.y2}
          : {id: g.id, gradientUnits: 'userSpaceOnUse', cx: g.cx, cy: g.cy, r: g.r, fx: g.fx, fy: g.fy};
        const node = svgNode(g.type === 'linear' ? 'linearGradient' : 'radialGradient', attrs);
        for (const stop of g.stops) {
          const stopAttrs = {offset: stop.offset, 'stop-color': stop.color};
          if (stop.opacity != null && stop.opacity !== 1) stopAttrs['stop-opacity'] = stop.opacity;
          node.append(svgNode('stop', stopAttrs));
        }
        defs.append(node);
      }
    }
    const hatch = svgNode('pattern',{id:this.prefix+'selected',width:12,height:12,patternUnits:'userSpaceOnUse'});
    hatch.append(svgNode('rect',{width:12,height:12,fill:'#EDF1F4'}),svgNode('path',{d:'M0 0H6V6H0Z M6 6H12V12H6Z',fill:'#C3CED4'}));
    defs.append(hatch); this.svg.append(defs);
    const g = this.bundle.geometry;
    this.detailed = this.bundle.manifest.format === 'color-duel-detailed-vector-1';
    if (this.detailed) {
      // Appearance layer below the masks: filled paths in ORIGINAL drawing
      // order (z), honouring per-path fill rule, opacity and strokes.
      // (Stroke ink is rendered above the masks in the ink layer so the
      // linework stays visible during play; see the numbered.svg preview.)
      const art = svgNode('g',{'data-layer':'vector-paint','pointer-events':'none'});
      for (const p of this.orderedPaint()) {
        if (p.filled === false || (p.strokeWidth != null && !SAFE_HEX.test(p.fill))) {
          continue; // stroke-only ink: rendered in the ink layer above masks
        }
        const gradientFill = p.fill.startsWith('url(#');
        const attrs = {d: p.d, fill: p.fill, 'fill-rule': p.fillRule || 'evenodd'};
        if (!gradientFill) { attrs.stroke = p.stroke || p.fill; attrs['stroke-width'] = p.strokeWidth ?? 0.55; attrs['stroke-linejoin'] = 'round'; }
        if (p.fillOpacity != null && p.fillOpacity < 0.999) attrs['fill-opacity'] = p.fillOpacity;
        if (p.opacity != null && p.opacity < 0.999) attrs.opacity = p.opacity;
        if (p.stroke && p.strokeWidth > 0) { attrs.stroke = p.stroke; attrs['stroke-width'] = p.strokeWidth; attrs['stroke-linejoin'] = 'round'; }
        art.append(svgNode('path', attrs));
      }
      this.svg.append(art);
    }
    const regions = svgNode('g',{'stroke':g.stroke,'stroke-width':g.strokeWidth,'stroke-linejoin':'round'});
    this.elements = new Map(); this.labels = new Map();
    for (const r of g.regions) {
      const node = svgNode('path',{id:this.prefix+r.id,'data-region-id':r.id,'data-palette-id':r.paletteId,d:r.d,tabindex:0,role:'button',
        'fill-rule': r.fillRule || 'evenodd',
        'aria-label':`Region ${r.id}, palette ${this.mode==='memory'?'hidden':r.paletteId}`});
      this.elements.set(r.id,node); regions.append(node);
    }
    this.svg.append(regions);
    const fixed = svgNode('g',{'pointer-events':'none','stroke':g.stroke,'stroke-width':1.3,'stroke-linejoin':'round','fill-rule':'evenodd'});
    for (const r of (this.detailed ? [] : g.decorations)) fixed.append(svgNode('path',{d:r.d,fill:`url(#${this.prefix}paint-${r.paletteId})`}));
    this.svg.append(fixed);
    const details = svgNode('g',{'pointer-events':'none',fill:'none','stroke-linejoin':'round','stroke-linecap':'round'});
    for (const d of g.detailPaths) details.append(svgNode('path',{d:d.d,stroke:d.stroke,'stroke-width':d.strokeWidth,opacity:d.opacity}));
    this.svg.append(details);
    if (this.detailed) {
      const ink = svgNode('g',{'data-layer':'ink','pointer-events':'none'});
      for (const p of this.bundle.paint.inkPaths) {
        if (p.strokeWidth != null || p.filled === false) {
          ink.append(svgNode('path',{d:p.d, fill:'none', stroke:p.fill,
            'stroke-width':p.strokeWidth ?? 1.5, 'stroke-linecap':'round', 'stroke-linejoin':'round'}));
        } else {
          ink.append(svgNode('path',{d:p.d, fill:p.fill, 'fill-rule':p.fillRule || 'evenodd',
            ...(p.opacity != null && p.opacity < 0.999 ? {opacity: p.opacity} : {})}));
        }
      }
      this.svg.append(ink);
    }
    const labels = svgNode('g',{'pointer-events':'none','font-family':'Arial,sans-serif',fill:'#33444C','text-anchor':'middle','dominant-baseline':'central'});
    for (const r of g.regions) {
      const label = svgNode('text',{'data-label-for':r.id,x:r.label.x,y:r.label.y,'font-size':r.label.fontSize});
      label.textContent = String(r.paletteId); labels.append(label); this.labels.set(r.id,label);
    }
    this.svg.append(labels);
    this.listen(this.svg,'keydown',e => {
      const id = e.target.getAttribute?.('data-region-id');
      if (id && (e.key==='Enter' || e.key===' ')) {e.preventDefault();this.paint(id);}
    });
  }
  /** Paint + ink entries merged into original drawing order (by z). */
  orderedPaint() {
    const paint = this.bundle.paint;
    if (!paint) return [];
    const entries = [...(paint.paths ?? []), ...(paint.inkPaths ?? [])];
    return entries.sort((a, b) => (a.z ?? Infinity) - (b.z ?? Infinity) || 0);
  }
  listen(el,type,fn,options) {el.addEventListener(type,fn,options);this.handlers.push(()=>el.removeEventListener(type,fn,options));}
  state() {
    const completedRegionIds = [...this.completed];
    const objects = Object.fromEntries(this.bundle.manifest.objectGroups.map(group => [group.id,
      {completed:group.regionIds.filter(id=>this.completed.has(id)).length,total:group.regionIds.length}]));
    return {...this.session, completedRegionIds, freeColors:{...this.session.freeColors},
      total:this.regions.size, completed:completedRegionIds.length, progress:completedRegionIds.length/this.regions.size,
      objects,preview:this.preview,zoom:this.base[2]/this.view[2],storageWarning:!!this.storageWarning};
  }
  save() {
    this.session.completedRegionIds = [...this.completed]; this.session.updatedAt = new Date().toISOString();
    if (this.options.persist !== false) try { localStorage.setItem(this.storageKey,JSON.stringify(this.session)); }
      catch { this.storageWarning = true; }
  }
  notify(reason) {this.options.onChange?.(this.state(),reason);}
  refresh() {
    for (const [id,r] of this.regions) {
      const done = this.completed.has(id), node = this.elements.get(id), label = this.labels.get(id);
      let fill = '#FFFFFF';
      if (this.preview || done) {
        const palette = (!this.preview && this.mode==='free') ? this.session.freeColors[id] : r.paletteId;
        fill = `url(#${this.prefix}paint-${palette})`;
      } else if (this.mode==='number' && r.paletteId===this.session.selectedPaletteId) fill=`url(#${this.prefix}selected)`;
      if (this.detailed && (this.preview || (done && this.mode!=='free'))) fill='none';
      node.setAttribute('fill',fill);
      node.setAttribute('stroke',this.detailed && (done || this.preview) ? 'none' : this.bundle.geometry.stroke);
      node.dataset.completed=String(done);
      node.setAttribute('tabindex',done && this.mode!=='free' ? '-1':'0');
      node.setAttribute('aria-pressed',String(done));
      label.style.display = done || this.preview || this.mode!=='number' ? 'none':'';
    }
    this.updateLabelVisibility();
    this.notify('render');
  }
  updateLabelVisibility() {
    if (!this.detailed || !this.labels) return;
    const screen = this.svg.getBoundingClientRect();
    const scale = Math.min(screen.width/this.view[2],screen.height/this.view[3]);
    for (const [id,r] of this.regions) {
      const visible = !this.completed.has(id) && !this.preview && this.mode==='number' && r.label.fontSize*scale >= 9;
      this.labels.get(id).style.display = visible ? '' : 'none';
    }
  }
  setPalette(id) {
    if (!this.bundle.palette.some(p=>p.id===id)) throw new Error('Unknown palette ID');
    this.session.selectedPaletteId=id; this.save(); this.refresh();
  }
  paint(id) {
    const r=this.regions.get(id);
    if (!r || this.preview) return 'ignored';
    if (this.completed.has(id) && this.mode!=='free') return 'already-complete';
    if (this.mode!=='free' && r.paletteId!==this.session.selectedPaletteId) {
      this.session.mistakes++;this.save();this.notify('wrong-color');return 'wrong-color';
    }
    if (this.completed.has(id) && this.session.freeColors[id]===this.session.selectedPaletteId) return 'already-complete';
    this.history.push({id,wasCompleted:this.completed.has(id),oldColor:this.session.freeColors[id]});
    this.completed.add(id);
    if (this.mode==='free') this.session.freeColors[id]=this.session.selectedPaletteId;
    this.save();this.refresh();this.notify('paint');return 'painted';
  }
  undo() {
    if (this.preview) return false;
    const previous=this.history.pop(); if (!previous) return false;
    if (!previous.wasCompleted) this.completed.delete(previous.id);
    if (previous.oldColor===undefined) delete this.session.freeColors[previous.id]; else this.session.freeColors[previous.id]=previous.oldColor;
    this.save();this.refresh();return true;
  }
  reset() {
    this.completed.clear();this.session.freeColors={};this.session.mistakes=0;this.history=[];this.preview=false;
    this.save();this.refresh();this.fit();
  }
  setPreview(value) {this.preview=!!value;this.refresh();}
  hitTest(x,y) {
    // Iterate in reverse document order (topmost wins). Regions are visible
    // surfaces and must not overlap; per-region fill rules are honoured.
    for (const [id,r] of [...this.regions].reverse()) {
      const b=r.bbox;
      const rule = r.fillRule || 'evenodd';
      if (x>=b[0] && x<=b[2] && y>=b[1] && y<=b[3] && this.ctx.isPointInPath(this.paths.get(id),x,y,rule)) return id;
    }
    return null;
  }
  clientToArt(x,y,matrix=null) {
    const ctm=matrix || this.svg.getScreenCTM()?.inverse();
    if (!ctm) return null;
    return new DOMPoint(x,y).matrixTransform(ctm);
  }
  applyView(view) {
    const [bx,by,bw,bh]=this.base;
    const w=clamp(view[2],bw/10,bw),h=w*bh/bw;
    this.view=[clamp(view[0],bx,bx+bw-w),clamp(view[1],by,by+bh-h),w,h];
    this.svg.setAttribute('viewBox',this.view.join(' '));this.updateLabelVisibility();this.notify('viewport');
  }
  fit() {this.applyView([...this.base]);}
  zoom(factor,anchor=null) {
    const v=this.view, a=anchor || {x:v[0]+v[2]/2,y:v[1]+v[3]/2};
    const w=clamp(v[2]/factor,this.base[2]/10,this.base[2]),h=w*this.base[3]/this.base[2];
    this.applyView([a.x-(a.x-v[0])*w/v[2],a.y-(a.y-v[1])*h/v[3],w,h]);
  }
  nextRegion() {
    if (this.mode!=='number' || this.preview) return null;
    const r=[...this.regions.values()].find(r=>!this.completed.has(r.id) && r.paletteId===this.session.selectedPaletteId);
    if (!r) return null;
    const w=Math.max(this.base[2]/10,Math.min(this.base[2],Math.max(r.bbox[2]-r.bbox[0],(r.bbox[3]-r.bbox[1])*this.base[2]/this.base[3])*1.6));
    this.applyView([r.label.x-w/2,r.label.y-w*this.base[3]/this.base[2]/2,w,w*this.base[3]/this.base[2]]);
    this.elements.get(r.id).focus({preventScroll:true});return r.id;
  }
  bindGestures() {
    const startPinch=()=>{
      const pts=[...this.pointers.values()];
      const mid={x:(pts[0].x+pts[1].x)/2,y:(pts[0].y+pts[1].y)/2};
      const inverse=this.svg.getScreenCTM().inverse();
      this.pinch={distance:Math.max(1,Math.hypot(pts[0].x-pts[1].x,pts[0].y-pts[1].y)),view:[...this.view],inverse,anchor:this.clientToArt(mid.x,mid.y,inverse)};
      this.suppressTap=true;
    };
    this.listen(this.svg,'pointerdown',e=>{
      if (e.pointerType==='mouse' && e.button!==0)return;
      this.pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
      try { this.svg.setPointerCapture(e.pointerId); } catch { /* synthetic/stale pointer */ }
      if (this.pointers.size===1) {
        this.suppressTap=false;
        const inverse=this.svg.getScreenCTM().inverse();
        this.drag={x:e.clientX,y:e.clientY,view:[...this.view],inverse,anchor:this.clientToArt(e.clientX,e.clientY,inverse),moved:false};
      } else if (this.pointers.size===2) startPinch();
    });
    this.listen(this.svg,'pointermove',e=>{
      if (!this.pointers.has(e.pointerId))return;
      this.pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
      if (this.pointers.size>=2 && this.pinch) {
        const pts=[...this.pointers.values()], mid={x:(pts[0].x+pts[1].x)/2,y:(pts[0].y+pts[1].y)/2};
        const distance=Math.max(1,Math.hypot(pts[0].x-pts[1].x,pts[0].y-pts[1].y));
        const p=this.pinch, now=this.clientToArt(mid.x,mid.y,p.inverse),w=clamp(p.view[2]*p.distance/distance,this.base[2]/10,this.base[2]),h=w*this.base[3]/this.base[2];
        this.applyView([p.anchor.x-(now.x-p.view[0])*w/p.view[2],p.anchor.y-(now.y-p.view[1])*h/p.view[3],w,h]);
      } else if (!this.suppressTap && this.drag) {
        const d=this.drag;
        if (Math.hypot(e.clientX-d.x,e.clientY-d.y)>6) d.moved=true;
        if (d.moved) {const now=this.clientToArt(e.clientX,e.clientY,d.inverse);this.applyView([d.view[0]-(now.x-d.anchor.x),d.view[1]-(now.y-d.anchor.y),d.view[2],d.view[3]]);}
      }
    });
    const finish=(e,cancel=false)=>{
      if (!this.pointers.has(e.pointerId))return;
      const tap=!cancel && this.pointers.size===1 && !this.suppressTap && !this.drag?.moved;
      this.pointers.delete(e.pointerId);
      if (this.svg.hasPointerCapture(e.pointerId))this.svg.releasePointerCapture(e.pointerId);
      if (tap) {const p=this.clientToArt(e.clientX,e.clientY);if(p)this.paint(this.hitTest(p.x,p.y));}
      if (!this.pointers.size) {this.drag=null;this.pinch=null;this.suppressTap=false;} else this.suppressTap=true;
    };
    this.listen(this.svg,'pointerup',e=>finish(e));
    this.listen(this.svg,'pointercancel',e=>finish(e,true));
    this.listen(this.svg,'wheel',e=>{e.preventDefault();this.zoom(Math.exp(-e.deltaY*.0015),this.clientToArt(e.clientX,e.clientY));},{passive:false});
  }
  destroy() {for(const remove of this.handlers)remove();this.handlers=[];this.svg.replaceChildren();this.pointers.clear();}
}
