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
 *  - geometry.edges (stage 2, optional): boundary entries {id, d (M/L/C/Q/Z,
 *    open or closed), kind: 'artwork' | 'subdivision', leftRegion/rightRegion
 *    (null or an existing region id)} plus optional geometry.boundaryStyle
 *    overrides. When edges is a non-empty array, region paths render FILL-ONLY
 *    and an edges overlay group (above masks/ink, below labels) draws the
 *    boundaries: artwork = solid (geometry.stroke, width 1.6, round caps),
 *    subdivision = light dashed (#7A8C94, width 0.85, dash '3 2.2' userSpace).
 *    Absent/empty edges = legacy stroke rendering, unchanged.
 *  - Free color: session.freeColors maps regionId -> #RRGGBB hex (flat fill,
 *    no gradient); board.setFreeColor(hex) is the custom brush; palette swatch
 *    selection in free mode sets the free color to that palette's hex; no
 *    mistake counting in free mode. BoardState.customColor exposes the brush.
 *  - Gestures are rAF-batched: pointermove/wheel store the latest event and run
 *    ONE scheduled frame; label visibility updates pause during a gesture.
 *  - Cached underpainting (stage 3): the paint + ink appearance groups
 *    serialize ONCE per bundle into standalone SVG documents (BASE viewBox,
 *    cloned gradient defs) and each group swaps to a single blob-URL <image>
 *    node once an Image probe decodes it — 200k+ live path commands leave
 *    the live DOM while masks/labels/edges/hatch stay interactive SVG.
 *    Failure at any step silently keeps the live path layers.
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

// ---------------------------------------------------------------------------
// Cached underpainting serialization (stage-3 contract §C) — standalone SVG
// documents mirroring each appearance group's own live mount() logic, so a
// swapped <image> is rendering-equivalent to the live-path fallback.
// ---------------------------------------------------------------------------
const XML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' };
const escapeXml = (value) => String(value).replace(/[&<>"']/g, (c) => XML_ESCAPES[c]);
const attrString = (attrs) => Object.entries(attrs).map(([k, v]) => `${k}="${escapeXml(v)}"`).join(' ');

/** Clone the paint gradient defs into the standalone document (the hatch
 *  pattern and palette gradients stay live in the board DOM). */
function serializeGradientDefs(gradients) {
  const parts = [];
  for (const g of gradients) {
    const tag = g.type === 'linear' ? 'linearGradient' : 'radialGradient';
    const attrs = [`id="${escapeXml(g.id)}"`, 'gradientUnits="userSpaceOnUse"'];
    const params = g.type === 'linear'
      ? [['x1', g.x1], ['y1', g.y1], ['x2', g.x2], ['y2', g.y2]]
      : [['cx', g.cx], ['cy', g.cy], ['r', g.r], ['fx', g.fx], ['fy', g.fy]];
    for (const [key, value] of params) if (value != null) attrs.push(`${key}="${value}"`);
    const stops = g.stops.map(s => {
      const sa = [`offset="${s.offset}"`, `stop-color="${escapeXml(s.color)}"`];
      if (s.opacity != null && s.opacity !== 1) sa.push(`stop-opacity="${s.opacity}"`);
      return `<stop ${sa.join(' ')}/>`;
    }).join('');
    parts.push(`<${tag} ${attrs.join(' ')}>${stops}</${tag}>`);
  }
  return parts.join('');
}

/** Art-layer path (below the masks) — the EXACT attribute logic the live
 *  'vector-paint' group uses in mount(); stroke-only ink entries are skipped
 *  the same way (they render in the ink layer above the masks). */
function serializeArtPath(p) {
  if (p.filled === false || (p.strokeWidth != null && !SAFE_HEX.test(p.fill))) return null;
  const gradientFill = p.fill.startsWith('url(#');
  const attrs = { d: p.d, fill: p.fill, 'fill-rule': p.fillRule || 'evenodd' };
  if (!gradientFill) { attrs.stroke = p.stroke || p.fill; attrs['stroke-width'] = p.strokeWidth ?? 0.55; attrs['stroke-linejoin'] = 'round'; }
  if (p.fillOpacity != null && p.fillOpacity < 0.999) attrs['fill-opacity'] = p.fillOpacity;
  if (p.opacity != null && p.opacity < 0.999) attrs.opacity = p.opacity;
  if (p.stroke && p.strokeWidth > 0) { attrs.stroke = p.stroke; attrs['stroke-width'] = p.strokeWidth; attrs['stroke-linejoin'] = 'round'; }
  return `<path ${attrString(attrs)}/>`;
}

/** Ink-layer path (above the masks) — the EXACT attribute logic the live
 *  'ink' group uses in mount(): open stroke line art vs closed ink fills. */
function serializeInkPath(p) {
  if (p.strokeWidth != null || p.filled === false) {
    return `<path d="${escapeXml(p.d)}" fill="none" stroke="${escapeXml(p.fill)}" stroke-width="${p.strokeWidth ?? 1.5}" stroke-linecap="round" stroke-linejoin="round"/>`;
  }
  const attrs = { d: p.d, fill: p.fill, 'fill-rule': p.fillRule || 'evenodd' };
  if (p.opacity != null && p.opacity < 0.999) attrs.opacity = p.opacity;
  return `<path ${attrString(attrs)}/>`;
}

/** Standalone document wrapper — always the BASE (art-space) viewBox, never
 *  the zoomed one, so the image stays aligned with the live layers at any
 *  zoom (the browser re-rasterizes it crisply). */
function wrapStandaloneSvg(defs, body, bx, by, bw, bh) {
  return `<svg xmlns="${NS}" viewBox="${bx} ${by} ${bw} ${bh}" width="${bw}" height="${bh}">` +
    (defs ? `<defs>${defs}</defs>` : '') + body.join('') + '</svg>';
}

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
  // Stage-2 boundary contract: optional edges + boundaryStyle (absent = legacy).
  if (g.edges !== undefined) {
    if (!Array.isArray(g.edges)) throw new Error('Invalid edges');
    const edgeIds = new Set();
    for (const e of g.edges) {
      if (!e || typeof e !== 'object') throw new Error('Invalid edge entry');
      if (!/^[a-zA-Z0-9_-]+$/.test(e.id) || edgeIds.has(e.id)) throw new Error('Duplicate or unsafe edge ID');
      edgeIds.add(e.id);
      if (typeof e.d !== 'string' || !SAFE_PATH_OPEN.test(e.d)) throw new Error('Invalid edge path');
      if (!['artwork','subdivision'].includes(e.kind)) throw new Error('Invalid edge kind');
      for (const side of ['leftRegion','rightRegion']) {
        const v = e[side];
        if (v !== undefined && v !== null && !ids.has(v)) throw new Error('Edge references unknown region');
      }
    }
    if (g.boundaryStyle !== undefined) {
      const bs = g.boundaryStyle;
      if (!bs || typeof bs !== 'object' || Array.isArray(bs)) throw new Error('Invalid boundaryStyle');
      for (const kind of ['artwork','subdivision']) {
        const s = bs[kind];
        if (s === undefined || s === null) continue;
        if (typeof s !== 'object' || !SAFE_HEX.test(s.stroke) || !finite(s.strokeWidth) || s.strokeWidth <= 0) throw new Error(`Invalid boundaryStyle ${kind}`);
        if (s.dash !== undefined && s.dash !== null && (typeof s.dash !== 'string' || s.dash.length > 40)) throw new Error(`Invalid boundaryStyle ${kind} dash`);
      }
    }
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
    // Stage-2 free colors: values are #RRGGBB hexes (legacy numeric values drop).
    for (const id of clean.completedRegionIds) {
      const c = raw.freeColors[id];
      if (typeof c === 'string' && SAFE_HEX.test(c)) clean.freeColors[id] = c;
    }
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
    this.artLayer = null; this.inkLayer = null; this.blobUrls = []; this.underpaintAttempts = 0; this.destroyed = false;
    this.ctx = document.createElement('canvas').getContext('2d');
    if (!this.ctx) throw new Error('Canvas hit-testing is unavailable');
    this.regions = new Map(bundle.geometry.regions.map(r => [r.id, r]));
    this.paths = new Map([...this.regions].map(([id,r]) => [id,new Path2D(r.d)]));
    this.edges = Array.isArray(bundle.geometry.edges) && bundle.geometry.edges.length ? bundle.geometry.edges : null;
    this.edgeMode = !!this.edges;
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
    this.revokeUnderpaintBlobs();   // full re-mount: release prior underpaint images
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
      this.artLayer = art;
      this.svg.append(art);
    }
    const regions = svgNode('g',{'stroke': this.edgeMode ? 'none' : g.stroke,'stroke-width':g.strokeWidth,'stroke-linejoin':'round'});
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
      this.inkLayer = ink;
      this.svg.append(ink);
    }
    if (this.edges) {
      // Stage-2 edges overlay: ABOVE masks/ink, BELOW labels. Region paths
      // render fill-only; boundaries are drawn here per kind (boundaryStyle
      // overrides the documented defaults).
      const bs = g.boundaryStyle || {};
      const styleOf = (kind) => {
        const base = kind === 'artwork'
          ? {stroke: g.stroke || '#22333B', 'stroke-width': 1.6}
          : {stroke: '#7A8C94', 'stroke-width': 0.85, 'stroke-dasharray': '3 2.2'};
        const custom = bs[kind];
        if (custom) {
          if (custom.stroke !== undefined) base.stroke = custom.stroke;
          if (custom.strokeWidth !== undefined) base['stroke-width'] = custom.strokeWidth;
          if (custom.dash !== undefined) base['stroke-dasharray'] = custom.dash;
        }
        return base;
      };
      const styles = {artwork: styleOf('artwork'), subdivision: styleOf('subdivision')};
      const overlay = svgNode('g',{'data-layer':'edges','pointer-events':'none','fill':'none','stroke-linecap':'round','stroke-linejoin':'round'});
      for (const e of this.edges) {
        const s = styles[e.kind] || styles.artwork;
        overlay.append(svgNode('path',{id:this.prefix+e.id,'data-edge-kind':e.kind,d:e.d,...s}));
      }
      this.svg.append(overlay);
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
    this.buildUnderpainting();
  }
  /** Paint + ink entries merged into original drawing order (by z). */
  orderedPaint() {
    const paint = this.bundle.paint;
    if (!paint) return [];
    const entries = [...(paint.paths ?? []), ...(paint.inkPaths ?? [])];
    return entries.sort((a, b) => (a.z ?? Infinity) - (b.z ?? Infinity) || 0);
  }
  /** Introspection for the cached underpainting (tests/tooling): image
   *  mounts attempted for this board and blob URLs still live. */
  underpaintState() { return { attempted: this.underpaintAttempts, blobCount: this.blobUrls.length }; }
  /** Cached underpainting (ported from the studio's src/lib/detailed-board.ts):
   *  serialize the paint + ink appearance ONCE into standalone SVG documents
   *  (base viewBox, cloned gradient defs only) and swap each appearance group
   *  for a single <image> element once its blob decodes — one image node
   *  replaces 200k+ live path commands while staying crisp at any zoom, where
   *  a fixed bitmap would blur. TWO images cover the two z-slots the
   *  appearance occupies (art BELOW the region masks, ink ABOVE them) so the
   *  classic layering is preserved exactly. Masks, labels, the edges overlay
   *  and the hatch pattern stay live SVG (interactive/ARIA-relevant). Reset
   *  and undo only affect region fills; the underpainting is static
   *  appearance. If serialization or decoding fails, the live path layers
   *  simply remain mounted — the fallback is automatic and silent. */
  buildUnderpainting() {
    if (!this.detailed || !this.bundle.paint) return;
    const paint = this.bundle.paint;
    const defs = serializeGradientDefs(paint.gradients ?? []);
    const [bx, by, bw, bh] = this.base;
    // Art layer (below the masks): closed fills, z-sorted — mirrors mount().
    const artBody = [];
    for (const p of this.orderedPaint()) {
      const serialized = serializeArtPath(p);
      if (serialized) artBody.push(serialized);
    }
    // Ink layer (above the masks): open line art + closed ink fills.
    const inkBody = paint.inkPaths.map(p => serializeInkPath(p));
    if (this.artLayer && artBody.length)
      this.mountUnderpaintImage(this.artLayer, wrapStandaloneSvg(defs, artBody, bx, by, bw, bh), 'underpaint-art');
    if (this.inkLayer && inkBody.length)
      this.mountUnderpaintImage(this.inkLayer, wrapStandaloneSvg(defs, inkBody, bx, by, bw, bh), 'underpaint-ink');
  }
  /** Load the serialized SVG via a blob URL, verify it decodes with an Image
   *  probe, and only then swap the live group children for the image. */
  mountUnderpaintImage(group, svgString, id) {
    if (typeof Image !== 'function') return;   // headless env: keep live paths
    let url;
    try { url = URL.createObjectURL(new Blob([svgString], { type: 'image/svg+xml' })); }
    catch { return; }                          // silent fallback: live paths stay mounted
    this.blobUrls.push(url);
    this.underpaintAttempts++;
    const probe = new Image();
    probe.onload = () => {
      if (this.destroyed) return;
      const [bx, by, bw, bh] = this.base;
      const image = svgNode('image', {
        id: this.prefix + id, href: url, x: bx, y: by, width: bw, height: bh,
        preserveAspectRatio: 'xMidYMid meet', 'pointer-events': 'none'
      });
      // xlink:href for engines that predate SVG2 href on <image>.
      image.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', url);
      group.replaceChildren(image);
    };
    probe.onerror = () => {
      // Silent fallback: keep the live path layer, release the blob.
      this.blobUrls = this.blobUrls.filter(u => u !== url);
      try { URL.revokeObjectURL(url); } catch { /* headless env */ }
    };
    probe.src = url;
  }
  /** Release every underpaint blob URL (destroy + full re-mount). */
  revokeUnderpaintBlobs() {
    for (const url of this.blobUrls) { try { URL.revokeObjectURL(url); } catch { /* headless env */ } }
    this.blobUrls = [];
  }
  listen(el,type,fn,options) {el.addEventListener(type,fn,options);this.handlers.push(()=>el.removeEventListener(type,fn,options));}
  state() {
    const completedRegionIds = [...this.completed];
    const objects = Object.fromEntries(this.bundle.manifest.objectGroups.map(group => [group.id,
      {completed:group.regionIds.filter(id=>this.completed.has(id)).length,total:group.regionIds.length}]));
    return {...this.session, completedRegionIds, freeColors:{...this.session.freeColors},
      customColor: this.session.freeColor ?? null,
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
        if (this.mode==='free' && !this.preview) {
          fill = this.session.freeColors[id] || '#FFFFFF';   // flat hex, not a gradient
        } else {
          fill = `url(#${this.prefix}paint-${r.paletteId})`;
        }
      } else if (this.mode==='number' && r.paletteId===this.session.selectedPaletteId) fill=`url(#${this.prefix}selected)`;
      if (this.detailed && (this.preview || (done && this.mode!=='free'))) fill='none';
      node.setAttribute('fill',fill);
      node.setAttribute('stroke',this.edgeMode || (this.detailed && (done || this.preview)) ? 'none' : this.bundle.geometry.stroke);
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
    if (this.pointers && this.pointers.size) return;   // skip while a gesture is active
    const screen = this.svg.getBoundingClientRect();
    const scale = Math.min(screen.width/this.view[2],screen.height/this.view[3]);
    for (const [id,r] of this.regions) {
      const visible = !this.completed.has(id) && !this.preview && this.mode==='number' && r.label.fontSize*scale >= 9;
      this.labels.get(id).style.display = visible ? '' : 'none';
    }
  }
  setPalette(id) {
    const entry = this.bundle.palette.find(p=>p.id===id);
    if (!entry) throw new Error('Unknown palette ID');
    if (this.mode==='free') {
      // Palette swatches in free mode are quick access to that palette's hex.
      this.session.freeColor = entry.hex; this.save(); this.refresh(); return;
    }
    this.session.selectedPaletteId=id; this.save(); this.refresh();
  }
  /** Free-mode custom color brush: any #RRGGBB hex (flat fill, no gradient). */
  setFreeColor(hex) {
    if (typeof hex !== 'string' || !SAFE_HEX.test(hex)) throw new Error('Use a #RRGGBB hex color');
    this.session.freeColor = hex; this.save(); this.refresh();
  }
  paletteHex(id) { const p = this.bundle.palette.find(q=>q.id===id); return p ? p.hex : null; }
  paint(id) {
    const r=this.regions.get(id);
    if (!r || this.preview) return 'ignored';
    if (this.completed.has(id) && this.mode!=='free') return 'already-complete';
    if (this.mode!=='free' && r.paletteId!==this.session.selectedPaletteId) {
      this.session.mistakes++;this.save();this.notify('wrong-color');return 'wrong-color';
    }
    if (this.mode==='free') {
      // Free mode: store the active hex (custom brush or the selected palette's
      // hex); repainting with a different color re-colors; NO mistake counting.
      const hex = this.session.freeColor || this.paletteHex(this.session.selectedPaletteId) || '#9AA7AD';
      if (this.completed.has(id) && this.session.freeColors[id]===hex) return 'already-complete';
      this.history.push({id,wasCompleted:this.completed.has(id),oldColor:this.session.freeColors[id]});
      this.completed.add(id);
      this.session.freeColors[id]=hex;
      this.save();this.refresh();this.notify('paint');return 'painted';
    }
    this.history.push({id,wasCompleted:this.completed.has(id),oldColor:this.session.freeColors[id]});
    this.completed.add(id);
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
    // rAF-batched pointermove/wheel: store the latest event, run ONE scheduled
    // frame per batch (high-frequency gestures no longer thrash the DOM).
    this._rafPending=false; this._lastMove=null; this._lastWheel=null;
    const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (cb)=>setTimeout(cb,16);
    const schedule = (fn) => {
      if (this._rafPending) return;
      this._rafPending = true;
      raf(() => { this._rafPending = false; fn(); });
    };
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
      this._lastMove = e;
      schedule(() => { const ev = this._lastMove; if (ev) this.handleMove(ev); });
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
    this.listen(this.svg,'wheel',e=>{
      e.preventDefault();                       // must stay synchronous
      this._lastWheel = e;
      schedule(() => {
        const ev = this._lastWheel; if (!ev) return;
        this.zoom(Math.exp(-ev.deltaY*.0015), this.clientToArt(ev.clientX,ev.clientY));
      });
    },{passive:false});
  }
  /** Deferred per-frame gesture math (pan + pinch); runs inside one rAF. */
  handleMove(e) {
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
  }
  destroy() {
    this.destroyed = true;
    for (const remove of this.handlers) remove();
    this.handlers = [];
    this.svg.replaceChildren();
    this.revokeUnderpaintBlobs();   // release the underpainting blob URLs
    this.pointers.clear();
  }
}
