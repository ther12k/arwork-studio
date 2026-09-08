#!/usr/bin/env bun
/** Headless adapter checks (run by tests/test_game_adapter.py):
 *  - validateBundle accepts/ rejects the stage-2 edges + boundaryStyle contract
 *  - VectorBoard free-color API (setFreeColor, freeColors hex, no mistakes,
 *    palette swatch -> free color, BoardState.customColor, session restore)
 *  - edges overlay rendering: regions fill-only, per-kind strokes + overrides
 *
 * The DOM is stubbed minimally; only the shipped adapter's own logic runs.
 */
import { validateBundle, normalizeSession, VectorBoard } from '../integration/detailed-board.mjs';

class FakeEl {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.style = {};
    this.dataset = {}; this.textContent = '';
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...els) { for (const e of els) this.children.push(e); }
  replaceChildren() { this.children = []; }
  addEventListener() {}
  removeEventListener() {}
  focus() {}
  getBoundingClientRect() { return { width: 380, height: 507, x: 0, y: 0 }; }
  getScreenCTM() { return null; }
  hasPointerCapture() { return false; }
  setPointerCapture() {}
  releasePointerCapture() {}
}
class FakeSvg extends FakeEl {}

globalThis.SVGSVGElement = FakeSvg;
globalThis.document = {
  createElementNS: (ns, tag) => new FakeEl(tag),
  createElement: (tag) => tag === 'canvas'
    ? { getContext: () => ({ isPointInPath: () => false }) }
    : new FakeEl(tag),
};
globalThis.Path2D = class { constructor(d) { this.d = d; } };
globalThis.DOMPoint = class { constructor(x, y) { this.x = x; this.y = y; } };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);

const failures = [];
let passes = 0;
const check = (ok, label) => { if (!ok) failures.push(label); else { passes++; console.log(`PASS ${label}`); } };

function bundle(edges, boundaryStyle) {
  return {
    manifest: {
      format: 'color-duel-detailed-vector-1', id: 'edge-test', version: '0.1.0',
      regionCount: 2, contentHash: 'h1', objectGroups: [],
      assets: { regions: 'regions.json', palette: 'palette.json', paint: 'paint.json' },
    },
    geometry: {
      geometrySchema: 2, artworkId: 'edge-test', artworkVersion: '0.1.0',
      viewBox: [0, 0, 100, 100], fillRule: 'evenodd', stroke: '#29383E', strokeWidth: 0.65,
      regions: [
        { id: 'r-00001', paletteId: 1, objectId: 'unassigned',
          d: 'M 10,10 L 50,10 L 50,50 L 10,50 Z', fillRule: 'evenodd',
          bbox: [10, 10, 50, 50], area: 1600,
          label: { x: 30, y: 30, fontSize: 10, minScreenPx: 9, clearance: 15 } },
        { id: 'r-00002', paletteId: 2, objectId: 'unassigned',
          d: 'M 55,55 L 90,55 L 90,90 L 55,90 Z', fillRule: 'evenodd',
          bbox: [55, 55, 90, 90], area: 1225,
          label: { x: 72, y: 72, fontSize: 9, minScreenPx: 9, clearance: 12 } },
      ],
      decorations: [], detailPaths: [], ...(edges ? { edges } : {}), ...(boundaryStyle ? { boundaryStyle } : {}),
    },
    palette: [
      { id: 1, number: 1, name: 'Tone 01', hex: '#3366AA', paint: { type: 'linearGradient', stops: [{ offset: 0, color: '#3366AA' }, { offset: 1, color: '#3366AA' }] } },
      { id: 2, number: 2, name: 'Tone 02', hex: '#AA3355', paint: { type: 'linearGradient', stops: [{ offset: 0, color: '#AA3355' }, { offset: 1, color: '#AA3355' }] } },
    ],
    paint: { artworkId: 'edge-test', viewBox: [0, 0, 100, 100], paths: [], inkPaths: [], gradients: [] },
  };
}

const EDGES = [
  { id: 'e-0001', d: 'M 10,10 L 50,10 L 50,50 L 10,50 Z', kind: 'artwork', leftRegion: 'r-00001', rightRegion: null },
  { id: 'e-0002', d: 'M 50,10 L 50,50', kind: 'subdivision', leftRegion: 'r-00001', rightRegion: null },
];
const STYLE = { artwork: { stroke: '#123456', strokeWidth: 2.5 }, subdivision: { stroke: '#654321', strokeWidth: 1.1, dash: '4 3' } };

// 1. validateBundle: edges + boundaryStyle accepted; legacy (no edges) accepted.
try { validateBundle(bundle(EDGES, STYLE)); check(true, 'validateBundle accepts edges + boundaryStyle'); }
catch (e) { check(false, `validateBundle accepts edges + boundaryStyle: ${e.message}`); }
try { validateBundle(bundle()); check(true, 'validateBundle accepts legacy bundle without edges'); }
catch (e) { check(false, `validateBundle accepts legacy bundle: ${e.message}`); }

// 2. Negative cases.
const bad = (mutate, label) => {
  const b = bundle(JSON.parse(JSON.stringify(EDGES)), JSON.parse(JSON.stringify(STYLE)));
  mutate(b);
  try { validateBundle(b); check(false, `${label} (accepted!)`); }
  catch { check(true, `${label} rejected`); }
};
bad(b => { b.geometry.edges[0].kind = 'scribble'; }, 'invalid edge kind');
bad(b => { b.geometry.edges[1].rightRegion = 'r-99999'; }, 'edge unknown region ref');
bad(b => { b.geometry.edges[0].d = 'M 10,10 X 50,50'; }, 'unsafe edge path data');
bad(b => { b.geometry.edges[1].id = 'bad id!'; }, 'unsafe edge id');
bad(b => { b.geometry.boundaryStyle.artwork = { stroke: 'red', strokeWidth: 1 }; }, 'boundaryStyle non-hex stroke');
bad(b => { b.geometry.boundaryStyle.subdivision = { stroke: '#654321', strokeWidth: 0 }; }, 'boundaryStyle zero strokeWidth');
bad(b => { b.geometry.edges = 'nope'; }, 'edges not an array');

// 3. Free-color session restore: hexes kept, legacy numeric values dropped.
const restored = normalizeSession(bundle(), {
  schemaVersion: 1, artworkId: 'edge-test', artworkVersion: '0.1.0', contentHash: 'h1', mode: 'free',
  completedRegionIds: ['r-00001', 'r-00002'], freeColors: { 'r-00001': '#12AB9F', 'r-00002': 3 }, mistakes: 5,
}, 'free');
check(restored.freeColors['r-00001'] === '#12AB9F', 'free session keeps hex colors');
check(!('r-00002' in restored.freeColors) && restored.completedRegionIds.length === 1, 'legacy numeric freeColors drop');
check(restored.mistakes === 5, 'mistake counter survives (never incremented in free mode)');

// 4. VectorBoard free-mode API + edges rendering.
const svg = new FakeSvg('svg');
let board;
try {
  board = new VectorBoard(svg, bundle(EDGES, STYLE), { persist: false, mode: 'free' });
  check(true, 'VectorBoard mounts an edges bundle');
} catch (e) { check(false, `VectorBoard mounts: ${e.message}`); process.exit(1); }

// edges overlay: group above masks, below labels; regions fill-only.
const overlay = svg.children.find(c => c.attrs['data-layer'] === 'edges');
check(!!overlay, 'edges overlay group rendered');
const labelGroup = svg.children[svg.children.length - 1];
check(!!overlay && svg.children.indexOf(overlay) < svg.children.indexOf(labelGroup), 'edges overlay below labels');
const regionGroup = svg.children.find(c => c.children.some(n => n.attrs['data-region-id']));
check(regionGroup && regionGroup.attrs['stroke'] === 'none', 'region paths render fill-only in edge mode');
const art = overlay.children.find(p => p.attrs['data-edge-kind'] === 'artwork');
const sub = overlay.children.find(p => p.attrs['data-edge-kind'] === 'subdivision');
check(art && art.attrs['stroke'] === '#123456' && art.attrs['stroke-width'] === '2.5' && !art.attrs['stroke-dasharray'], 'artwork edge style (solid, override)');
check(sub && sub.attrs['stroke'] === '#654321' && sub.attrs['stroke-width'] === '1.1' && sub.attrs['stroke-dasharray'] === '4 3', 'subdivision edge style (dashed, override)');

// default styles without boundaryStyle
const svg2 = new FakeSvg('svg');
new VectorBoard(svg2, bundle(EDGES), { persist: false, mode: 'free' });
const overlay2 = svg2.children.find(c => c.attrs['data-layer'] === 'edges');
const sub2 = overlay2.children.find(p => p.attrs['data-edge-kind'] === 'subdivision');
const art2 = overlay2.children.find(p => p.attrs['data-edge-kind'] === 'artwork');
check(art2 && art2.attrs['stroke'] === '#29383E' && art2.attrs['stroke-width'] === '1.6', 'artwork default style (geometry.stroke, 1.6)');
check(sub2 && sub2.attrs['stroke'] === '#7A8C94' && sub2.attrs['stroke-width'] === '0.85' && sub2.attrs['stroke-dasharray'] === '3 2.2', 'subdivision default style (#7A8C94, 0.85, dash 3 2.2)');

// free color API
try { board.setFreeColor('red'); check(false, 'setFreeColor rejects non-hex'); }
catch { check(true, 'setFreeColor rejects non-hex'); }
board.setFreeColor('#12AB9F');
check(board.state().customColor === '#12AB9F', 'state().customColor exposes the brush');
check(board.paint('r-00001') === 'painted', 'free-mode paint works');
check(board.session.freeColors['r-00001'] === '#12AB9F', 'paint stores the hex (not a palette id)');
check(board.state().mistakes === 0, 'no mistake counting in free mode');
const node = board.elements.get('r-00001');
check(node.attrs['fill'] === '#12AB9F', 'completed free region fills with the flat hex directly');
check(board.paint('r-00001') === 'already-complete', 'same color repaint is already-complete');
board.setFreeColor('#FFEE00');
check(board.paint('r-00001') === 'painted' && board.session.freeColors['r-00001'] === '#FFEE00', 'repainting with a new hex recolors');
board.setPalette(2);
check(board.session.freeColor === '#AA3355', 'palette swatch in free mode sets the free color to its hex');
check(board.paint('r-00002') === 'painted' && board.session.freeColors['r-00002'] === '#AA3355', 'paint after swatch uses the palette hex');
check(board.undo() === true && !board.completed.has('r-00002'), 'undo restores free-mode completion');

if (failures.length) { console.error(`FAIL ${failures.join('; ')}`); process.exit(1); }
console.log(`adapter-board-check: all checks passed (${passes} assertions, 0 failures)`);
