#!/usr/bin/env bun
/** Adapter contract check: run a freshly compiled bundle through the ACTUAL
 * shipped game adapter (integration/detailed-board.mjs) — the same validator
 * a game integration imports. "Passed validation" in the studio must mean
 * "the shipped adapter can load it".
 *
 * Usage: bun scripts/adapter-contract-check.mjs <bundle-folder> [<more folders>...]
 * Exits non-zero with details when any bundle fails the contract.
 */
import { validateBundle } from '../integration/detailed-board.mjs';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

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
    validateBundle({ manifest, geometry, palette, paint });
    const curved = geometry.regions?.filter(r => /[CQ]/.test(r.d)).length ?? 0;
    console.log(`PASS ${name}: ${geometry.regions?.length ?? 0} regions (${curved} curved), ` +
      `${palette.length} palette groups, ${paint?.paths?.length ?? 0} paint paths, ` +
      `${paint?.inkPaths?.length ?? 0} ink paths, ${paint?.gradients?.length ?? 0} gradients`);
  } catch (error) {
    failed++;
    console.error(`FAIL ${name}: ${error.message}`);
  }
}
process.exit(failed ? 1 : 0);
