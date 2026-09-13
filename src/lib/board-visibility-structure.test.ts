/**
 * Task 32 review R2 — board integration structure for object visibility.
 *
 * jsdom cannot instantiate VectorBoard (no canvas 2D / Path2D), so these
 * assertions pin the REAL integration points in detailed-board.ts that the
 * isolated controller tests cannot reach: refresh survival, hit-test
 * filtering, label single-pass handling, and cached-underpainting
 * regeneration on visibility change.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";

const src = readFileSync(join(process.cwd(), "src", "lib", "detailed-board.ts"), "utf8");

describe("VectorBoard visibility integration (Task 32 review R2)", () => {
  test("hit-testing skips regions of hidden objects", () => {
    expect(src).toMatch(/hitTest[\s\S]*?isRegionHidden\(id\)[\s\S]*?continue/);
  });

  test("completion refreshes never resurrect hidden regions or labels", () => {
    const refresh = src.slice(src.indexOf("  refresh() {"), src.indexOf("  updateLabelVisibility("));
    expect(refresh).toContain("isRegionHidden(id)");
    expect(refresh).toContain('label.style.display = "none"');
    expect(refresh).toContain('setAttribute("tabindex", "-1")');
  });

  test("label sizing excludes hidden regions (zoom/resize survival)", () => {
    const labelFn = src.slice(src.indexOf("updateLabelVisibility(rect?: DOMRect)"), src.indexOf("setPalette("));
    expect(labelFn).toContain("!this.visibility.isRegionHidden(id)");
  });

  test("cached underpainting is regenerated from filtered shapes (visibility baked in)", () => {
    const build = src.slice(src.indexOf("private buildUnderpainting()"), src.indexOf("private mountUnderpaintImage("));
    expect(build).toContain("isShapeHidden(p.shapeId)");
    expect(build).toContain("filter(shapeVisible)");
  });

  test("applying visibility re-applies labels once and regenerates the cache", () => {
    const apply = src.slice(src.indexOf("private applyObjectVisibility()"), src.indexOf("private applyObjectHighlight()"));
    expect(apply).toContain("this.updateLabelVisibility();");
    expect(apply).toContain("this.buildUnderpainting();");
    // one label pass — the per-region reset inside the loop was the bug
    expect(apply.match(/updateLabelVisibility\(\)/g)?.length).toBe(1);
  });

  test("underpainting regeneration revokes the previous layer blob URL", () => {
    expect(src).toContain("this.underpaintUrls.get(id)");
    expect(src).toContain("URL.revokeObjectURL(previous)");
  });

  test("an EMPTY visible layer clears the cached image instead of keeping it (round-2 R2)", () => {
    const build = src.slice(src.indexOf("private buildUnderpainting()"), src.indexOf("private publishUnderpaintLayer("));
    // null body (empty layer) flows through the same publish path
    expect(build).toMatch(/publishUnderpaintLayer\([\s\S]*?artBody\.length \? [\s\S]*?: null\)/);
    expect(build).toMatch(/publishUnderpaintLayer\([\s\S]*?inkBody\.length \? [\s\S]*?: null\)/);
    const publish = src.slice(src.indexOf("private publishUnderpaintLayer("), src.indexOf("private mountUnderpaintImage("));
    expect(publish).toContain("group.replaceChildren()");
    expect(publish).toContain("URL.revokeObjectURL(previous)");
  });

  test("a per-layer generation token invalidates in-flight image loads (round-2 R2)", () => {
    expect(src).toContain("private underpaintGeneration = new Map<string, number>()");
    const mount = src.slice(src.indexOf("private mountUnderpaintImage("), src.indexOf("  destroy()"));
    expect(mount).toContain("this.underpaintGeneration.get(id) !== generation");
  });

  test("the board wires its region map into the controller catalog", () => {
    expect(src).toContain("this.visibility.regionsSource = () => this.regions;");
    expect(src).toContain("this.visibility.setCatalog(bundle.objects?.objects ?? [])");
  });
});
