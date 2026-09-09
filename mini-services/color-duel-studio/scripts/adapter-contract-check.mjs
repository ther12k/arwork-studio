#!/usr/bin/env bun
/** Adapter contract check: run a freshly compiled bundle through the ACTUAL
 * shipped game adapter (integration/detailed-board.mjs) — the same validator
 * a game integration imports. "Passed validation" in the studio must mean
 * "the shipped adapter can load it".
 *
 * Stage 3: every bundle is ALSO mounted headlessly on the shipped VectorBoard
 * (stub DOM, same technique as tests/adapter-board-check.mjs) to gate the
 * cached-underpainting swap: the paint/ink appearance groups must serialize
 * into standalone SVG documents (BASE viewBox + cloned gradient defs), mount
 * as blob URLs, swap each group for a single <image> node once the Image
 * probe loads, and revoke every blob URL on destroy(). bun has no image
 * decode pipeline, so Image/Blob-URL are stubbed deterministically — the gate
 * asserts the underpaint machinery and the serialized documents, not raster
 * output. Bundles without paint appearance must attempt no underpaint.
 *
 * Usage: bun scripts/adapter-contract-check.mjs <bundle-folder> [<more folders>...]
 * Exits non-zero with details when any bundle fails the contract.
 */
import { validateBundle, VectorBoard } from '../integration/detailed-board.mjs';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SAFE_HEX = /^#[0-9A-Fa-f]{6}$/;

class CheckEl {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.style = {};
    this.dataset = {}; this.textContent = '';
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  setAttributeNS(_ns, k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...els) { for (const e of els) this.children.push(e); }
  replaceChildren(...els) { this.children = [...els]; }
  addEventListener() {}
  removeEventListener() {}
  focus() {}
  getBoundingClientRect() { return { width: 380, height: 507, x: 0, y: 0 }; }
  getScreenCTM() { return null; }
  hasPointerCapture() { return false; }
  setPointerCapture() {}
  releasePointerCapture() {}
}
class CheckSvg extends CheckEl {}

globalThis.SVGSVGElement = CheckSvg;
globalThis.document = {
  createElementNS: (_ns, tag) => new CheckEl(tag),
  createElement: (tag) => tag === 'canvas'
    ? { getContext: () => ({ isPointInPath: () => false }) }
    : new CheckEl(tag),
};
globalThis.Path2D = class { constructor(d) { this.d = d; } };
globalThis.DOMPoint = class { constructor(x, y) { this.x = x; this.y = y; } };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);

// Deterministic Image / blob-URL stubs: createObjectURL captures the Blob (its
// serialized SVG text is asserted below) and every Image "decodes" on the next
// microtask, so the probe onload lands before the settle await resolves.
const blobObjects = [];
const blobUrls = [];
const revokedUrls = [];
URL.createObjectURL = (blob) => {
  blobObjects.push(blob);
  const url = `blob:underpaint-${blobUrls.length + 1}`;
  blobUrls.push(url);
  return url;
};
URL.revokeObjectURL = (url) => { revokedUrls.push(url); };
globalThis.Image = class {
  set src(url) { this._src = url; queueMicrotask(() => this.onload && this.onload()); }
  get src() { return this._src; }
};

/** XML must stay well-formed for the <image> decode: a duplicated attribute
 *  (easy to produce when porting the mount() override logic to strings) is a
 *  hard parse error in an XML document, so every tag is checked. */
function assertNoDuplicateAttributes(doc, label) {
  const tags = doc.match(/<[a-zA-Z][^>]*>/g) ?? [];
  for (const tag of tags) {
    const keys = [...tag.matchAll(/ ([a-zA-Z][\w:.-]*)="/g)].map(m => m[1]);
    const dup = keys.find((k, i) => keys.indexOf(k) !== i);
    if (dup) throw new Error(`${label}: duplicate attribute "${dup}" in ${tag.slice(0, 90)}`);
  }
}

/** Every url(#…) fill in a standalone document must resolve to a gradient def
 *  cloned into THAT document (a missing def would render black). */
function assertGradientRefsResolve(doc, gradients, label) {
  const ids = new Set((gradients ?? []).map(g => g.id));
  for (const ref of doc.matchAll(/fill="url\(#([^)]+)\)"/g)) {
    const id = ref[1];
    if (!ids.has(id)) throw new Error(`${label}: serialized fill references unknown gradient ${id}`);
    if (!doc.includes(`id="${id}"`)) throw new Error(`${label}: gradient ${id} was not cloned into <defs>`);
  }
}

/** Mount the bundle on the shipped VectorBoard and gate the cached
 *  underpainting: live-path counts before the swap, blob-URL <image> swap
 *  after the probe loads, serialized standalone documents, and
 *  revoke-on-destroy. */
async function checkUnderpaint(bundle, name) {
  const paint = bundle.paint;
  const svg = new CheckSvg('svg');
  const board = new VectorBoard(svg, bundle, { persist: false });
  const artGroup = svg.children.find(c => c.attrs['data-layer'] === 'vector-paint');
  const inkGroup = svg.children.find(c => c.attrs['data-layer'] === 'ink');
  // Independent re-encoding of each live layer's documented mount rules.
  const artCount = paint
    ? [...(paint.paths ?? []), ...(paint.inkPaths ?? [])]
      .filter(p => !(p.filled === false || (p.strokeWidth != null && !SAFE_HEX.test(p.fill)))).length
    : 0;
  const inkCount = paint ? (paint.inkPaths ?? []).length : 0;
  const inkStrokeCount = paint
    ? (paint.inkPaths ?? []).filter(p => p.strokeWidth != null || p.filled === false).length
    : 0;
  const layers = [
    { group: artGroup, id: 'underpaint-art', live: artCount, noneFill: 0 },
    { group: inkGroup, id: 'underpaint-ink', live: inkCount, noneFill: inkStrokeCount },
  ].filter(l => l.live > 0);
  const expectedAttempts = layers.length;

  if (artCount && !artGroup) throw new Error('vector-paint layer missing');
  if (inkCount && !inkGroup) throw new Error('ink layer missing');
  if (artGroup && artGroup.children.length !== artCount)
    throw new Error(`live vector-paint layer holds ${artGroup.children.length} path(s), expected ${artCount}`);
  if (inkGroup && inkGroup.children.length !== inkCount)
    throw new Error(`live ink layer holds ${inkGroup.children.length} path(s), expected ${inkCount}`);

  const state = board.underpaintState();
  if (state.attempted !== expectedAttempts || state.blobCount !== expectedAttempts)
    throw new Error(`underpaint state {attempted: ${state.attempted}, blobCount: ${state.blobCount}}, expected ${expectedAttempts} mount(s)`);

  // The (stubbed) probe decodes on a microtask; let it land before asserting.
  await new Promise(resolve => setTimeout(resolve, 0));

  const [bx, by, bw, bh] = bundle.geometry.viewBox;
  const created = blobUrls.slice(blobUrls.length - expectedAttempts);
  const docs = [];
  for (let i = blobUrls.length - expectedAttempts; i < blobUrls.length; i++) docs.push(await blobObjects[i].text());

  layers.forEach((layer, index) => {
    const { group, id, live, noneFill } = layer;
    const doc = docs[index];
    const label = `${name} ${id}`;
    if (group.children.length !== 1 || group.children[0].tag !== 'image')
      throw new Error(`${label}: expected a single <image> after the swap, got [${group.children.map(c => c.tag).join(', ')}]`);
    const image = group.children[0];
    if (!image.attrs.id?.endsWith(id)) throw new Error(`${label}: image id is ${image.attrs.id}`);
    if (!image.attrs.href?.startsWith('blob:')) throw new Error(`${label}: image href is ${image.attrs.href}`);
    if (image.attrs['xlink:href'] !== image.attrs.href) throw new Error(`${label}: xlink:href fallback missing`);
    if (image.attrs.x !== String(bx) || image.attrs.y !== String(by) ||
        image.attrs.width !== String(bw) || image.attrs.height !== String(bh))
      throw new Error(`${label}: image box ${[image.attrs.x, image.attrs.y, image.attrs.width, image.attrs.height]} != base viewBox`);
    if (image.attrs.preserveAspectRatio !== 'xMidYMid meet' || image.attrs['pointer-events'] !== 'none')
      throw new Error(`${label}: image interactivity/preserveAspectRatio attrs wrong`);
    if (!doc.startsWith(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${bx} ${by} ${bw} ${bh}" width="${bw}" height="${bh}">`))
      throw new Error(`${label}: standalone document header mismatch: ${doc.slice(0, 110)}`);
    if ((doc.match(/<path /g) ?? []).length !== live)
      throw new Error(`${label}: serialized ${doc.match(/<path /g)?.length ?? 0} path(s), expected ${live}`);
    if ((doc.match(/fill="none"/g) ?? []).length !== noneFill)
      throw new Error(`${label}: serialized ${doc.match(/fill="none"/g)?.length ?? 0} stroke-only path(s), expected ${noneFill}`);
    assertGradientRefsResolve(doc, paint?.gradients, label);
    assertNoDuplicateAttributes(doc, label);
  });

  board.destroy();
  if (svg.children.length !== 0) throw new Error(`${name}: destroy() left nodes mounted`);
  if (board.underpaintState().blobCount !== 0) throw new Error(`${name}: blob URLs survived destroy()`);
  for (const url of created) if (!revokedUrls.includes(url)) throw new Error(`${name}: blob URL ${url} not revoked on destroy()`);

  console.log(`PASS ${name}: underpaint — ` + (expectedAttempts
    ? `${layers.map(l => `${l.live} live path(s) -> 1 <image>`).join(', ')}; ${expectedAttempts} blob URL(s) revoked on destroy`
    : 'no paint appearance, 0 image mounts (correct)'));
}

const folders = process.argv.slice(2);
if (!folders.length) {
  console.error('Usage: bun scripts/adapter-contract-check.mjs <bundle-folder> [...]');
  process.exit(2);
}

let failed = 0;
for (const folder of folders) {
  const name = folder.replace(/\/+$/, '').split('/').pop();
  try {
    const manifest = JSON.parse(readFileSync(join(folder, 'artwork.json'), 'utf-8'));
    const geometry = JSON.parse(readFileSync(join(folder, 'regions.json'), 'utf-8'));
    const palette = JSON.parse(readFileSync(join(folder, 'palette.json'), 'utf-8'));
    const paint = manifest.assets?.paint
      ? JSON.parse(readFileSync(join(folder, manifest.assets.paint), 'utf-8'))
      : null;
    const bundle = validateBundle({ manifest, geometry, palette, paint });
    const curved = geometry.regions?.filter(r => /[CQ]/.test(r.d)).length ?? 0;
    console.log(`PASS ${name}: ${geometry.regions?.length ?? 0} regions (${curved} curved), ` +
      `${palette.length} palette groups, ${paint?.paths?.length ?? 0} paint paths, ` +
      `${paint?.inkPaths?.length ?? 0} ink paths, ${paint?.gradients?.length ?? 0} gradients`);
    await checkUnderpaint(bundle, name);
  } catch (error) {
    failed++;
    console.error(`FAIL ${name}: ${error.message}`);
  }
}
process.exit(failed ? 1 : 0);
