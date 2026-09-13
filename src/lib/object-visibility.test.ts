/**
 * Task 32 review R2 — authoritative object visibility state.
 *
 * Behavior tests for the REAL computeObjectVisibility / ObjectVisibilityController
 * bodies (not mocks): subtree hide/isolate semantics, isolate-keeps-subtree,
 * unowned-art handling on isolate, and shape-hidden lookups.
 */

import { describe, expect, test } from "vitest";
import type { SemanticObject } from "@/lib/studio-api";
import { computeObjectVisibility, ObjectVisibilityController } from "./detailed-board";

const objects: SemanticObject[] = [
  { id: "sky", name: "Sky", shapeIds: ["s1"] },
  { id: "house", name: "House", shapeIds: ["s2"], parentId: "scene" },
  { id: "roof", name: "Roof", shapeIds: ["s3", "s4"], parentId: "house" },
  { id: "tree", name: "Tree", shapeIds: ["s5"] },
];

const regions: Array<[string, { objectId?: string }]> = [
  ["r-sky", { objectId: "sky" }],
  ["r-house", { objectId: "house" }],
  ["r-roof", { objectId: "roof" }],
  ["r-tree", { objectId: "tree" }],
  ["r-none", {}], // belongs to no object
];

function makeController() {
  const c = new ObjectVisibilityController();
  c.regionsSource = () => regions;
  c.setCatalog(objects);
  return c;
}

describe("computeObjectVisibility — subtree contract", () => {
  test("hiding a parent hides its whole subtree", () => {
    const hidden = computeObjectVisibility(objects, new Set(["house"]), null);
    expect(hidden.has("house")).toBe(true);
    expect(hidden.has("roof")).toBe(true);
    expect(hidden.has("sky")).toBe(false);
    expect(hidden.has("tree")).toBe(false);
  });

  test("hiding an individual child hides only that child", () => {
    const hidden = computeObjectVisibility(objects, new Set(["roof"]), null);
    expect(hidden.has("roof")).toBe(true);
    expect(hidden.has("house")).toBe(false);
  });

  test("isolating a parent keeps the parent SUBTREE visible and hides the rest", () => {
    const hidden = computeObjectVisibility(objects, new Set(), "house");
    expect(hidden.has("house")).toBe(false);
    expect(hidden.has("roof")).toBe(false); // descendant stays included
    expect(hidden.has("sky")).toBe(true);
    expect(hidden.has("tree")).toBe(true);
  });

  test("isolating a deep child hides its own parent (not the other way)", () => {
    const hidden = computeObjectVisibility(objects, new Set(), "roof");
    expect(hidden.has("roof")).toBe(false);
    expect(hidden.has("house")).toBe(true); // parent is NOT in the child's subtree
    expect(hidden.has("sky")).toBe(true);
  });
});

describe("ObjectVisibilityController — derived state the board consumes", () => {
  test("hide parent: regions, shapes and hit-lookups follow the subtree", () => {
    const c = makeController();
    c.setHiddenObjects(new Set(["house"]), null);
    expect(c.isRegionHidden("r-house")).toBe(true);
    expect(c.isRegionHidden("r-roof")).toBe(true);
    expect(c.isRegionHidden("r-sky")).toBe(false);
    expect(c.isRegionHidden("r-none")).toBe(false); // plain hide never touches unowned
    expect(c.isShapeHidden("s2")).toBe(true);
    expect(c.isShapeHidden("s3")).toBe(true);
    expect(c.isShapeHidden("s1")).toBe(false);
    expect(c.unownedArtHidden()).toBe(false);
  });

  test("isolate: everything outside the subtree hides, including unowned art and regions", () => {
    const c = makeController();
    c.setHiddenObjects(new Set(), "sky");
    expect(c.isRegionHidden("r-sky")).toBe(false);
    expect(c.isRegionHidden("r-house")).toBe(true);
    expect(c.isRegionHidden("r-none")).toBe(true); // unowned region hidden while isolating
    expect(c.unownedArtHidden()).toBe(true);
    expect(c.isShapeHidden(undefined)).toBe(true); // shape without owner/shapeId
    expect(c.isShapeHidden("s1")).toBe(false);
    expect(c.isShapeHidden("s5")).toBe(true);
  });

  test("hidden state is recomputed after the catalog changes (new bundle)", () => {
    const c = makeController();
    c.setHiddenObjects(new Set(["tree"]), null);
    expect(c.isRegionHidden("r-tree")).toBe(true);
    // next revision dropped the tree object entirely
    c.setCatalog(objects.filter((o) => o.id !== "tree"));
    expect(c.isRegionHidden("r-tree")).toBe(false);
    expect(c.isShapeHidden("s5")).toBe(false); // owner gone from catalog
  });

  test("highlight is tracked and ownership lookups stay available", () => {
    const c = makeController();
    c.setHighlightedObject("roof");
    expect(c.highlight()).toBe("roof");
    expect(c.ownedShapeIds("roof")).toEqual(new Set(["s3", "s4"]));
  });
});
