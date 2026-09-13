/**
 * Task 32 — Semantic Object & Layer Inspector component tests.
 *
 * Tests:
 *  - Hierarchy rendering from objects.json (parent-child tree).
 *  - Selecting an object reveals details and enables actions.
 *  - Lock/unlock and detail priority actions trigger updateObject.
 *  - Hide/isolate toggles update UI state without network calls.
 *  - Select all regions selects matching gameplay regions.
 *  - Empty state renders gracefully when no objects exist.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Bundle } from "@/lib/detailed-board";
import type { ObjectsFile, SemanticObject } from "@/lib/studio-api";
import { ObjectInspector } from "./object-inspector";

const sampleObjects: SemanticObject[] = [
  {
    id: "obj-sky",
    name: "Sky",
    role: "background",
    shapeIds: ["s0001", "s0002"],
    subdivision: { detailWeight: 0.5 },
    generation: { locked: false },
  },
  {
    id: "obj-cottage",
    name: "Cottage",
    role: "subject",
    shapeIds: ["s0003"],
    subdivision: { detailWeight: 2.0 },
    generation: { locked: true, prompt: "fairy cottage" },
  },
  {
    id: "obj-roof",
    name: "Roof",
    role: "subject",
    parentId: "obj-cottage",
    shapeIds: ["s0004", "s0005"],
    subdivision: { detailWeight: 1.0 },
  },
];

const mockUpdateObject = vi.fn();
const mockSelectAllObjectRegions = vi.fn();
const mockToggleHideObject = vi.fn();
const mockToggleIsolateObject = vi.fn();
const mockSetSelectedObjectId = vi.fn();
const mockRegenerateObject = vi.fn();

let mockSelectedObjectId: string | null = "obj-roof";
let mockHiddenObjectIds = new Set<string>();
let mockIsolatedObjectId: string | null = null;
let mockBundle: Bundle | null = {
  manifest: {
    schemaVersion: 1,
    format: "color-duel-detailed-vector-1",
    id: "test-art",
    version: "0.1.0",
    title: "Test Art",
    regionCount: 3,
    assets: { regions: "regions.json", palette: "palette.json", paint: "paint.json" },
    contentHash: "hash-123",
  },
  geometry: {
    schemaVersion: 1,
    artworkId: "test-art",
    artworkVersion: "0.1.0",
    viewBox: [0, 0, 300, 300],
    fillRule: "evenodd",
    stroke: "#000",
    strokeWidth: 1,
    regions: [
      { id: "r-001", paletteId: 1, objectId: "obj-sky", d: "M 0 0 Z", fillRule: "evenodd", rings: [], bbox: [0,0,10,10], area: 100, label: { x: 5, y: 5, fontSize: 10, minScreenPx: 9, clearance: 5 } },
      { id: "r-002", paletteId: 2, objectId: "obj-roof", d: "M 0 0 Z", fillRule: "evenodd", rings: [], bbox: [0,0,10,10], area: 100, label: { x: 5, y: 5, fontSize: 10, minScreenPx: 9, clearance: 5 } },
      { id: "r-003", paletteId: 2, objectId: "obj-roof", d: "M 0 0 Z", fillRule: "evenodd", rings: [], bbox: [0,0,10,10], area: 100, label: { x: 5, y: 5, fontSize: 10, minScreenPx: 9, clearance: 5 } },
    ],
    decorations: [],
    detailPaths: [],
  },
  palette: [{ id: 1, number: 1, name: "Blue", hex: "#3366AA", paint: { type: "solid", stops: [] } }],
  paint: { schemaVersion: 2, artworkId: "test-art", paths: [], inkPaths: [] },
  objects: {
    schemaVersion: 1,
    objects: sampleObjects,
  } as ObjectsFile,
};

vi.mock("./use-studio", () => ({
  useStudioContext: () => ({
    bundle: mockBundle,
    busy: false,
    selectedObjectId: mockSelectedObjectId,
    setSelectedObjectId: mockSetSelectedObjectId,
    hiddenObjectIds: mockHiddenObjectIds,
    isolatedObjectId: mockIsolatedObjectId,
    toggleHideObject: mockToggleHideObject,
    toggleIsolateObject: mockToggleIsolateObject,
    selectAllObjectRegions: mockSelectAllObjectRegions,
    updateObject: mockUpdateObject,
    activeSession: { id: "sess-1", mode: "ai_chat", status: "ready_to_commit" },
    regenerateObject: mockRegenerateObject,
  }),
}));

describe("Task 32 — ObjectInspector Component", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders the object tree with counts and hierarchy", () => {
    render(<ObjectInspector />);

    expect(screen.getByText("Objects & Layers")).toBeDefined();
    expect(screen.getByText("3 objects")).toBeDefined();
    expect(screen.getByText("Sky")).toBeDefined();
    expect(screen.getByText("Cottage")).toBeDefined();
    expect(screen.getByText("Roof")).toBeDefined();
  });

  it("shows selected object details (name, region/shape counts, role)", () => {
    mockSelectedObjectId = "obj-roof";
    render(<ObjectInspector />);

    // Roof details
    expect(screen.getByText("obj-roof")).toBeDefined();
    expect(screen.getByText("2 gameplay regions")).toBeDefined();
    expect(screen.getByText("2 visual shapes")).toBeDefined();
    expect(screen.getByText("Role: subject")).toBeDefined();
  });

  it("triggers selectAllObjectRegions on CTA click", () => {
    mockSelectedObjectId = "obj-roof";
    render(<ObjectInspector />);

    const selectBtn = screen.getByText(/Select All Regions \(2\)/i);
    fireEvent.click(selectBtn);
    expect(mockSelectAllObjectRegions).toHaveBeenCalledWith("obj-roof");
  });

  it("triggers detail priority update when preset is clicked", () => {
    mockSelectedObjectId = "obj-roof";
    render(<ObjectInspector />);

    const highBtn = screen.getByRole("button", { name: /High/i });
    fireEvent.click(highBtn);
    expect(mockUpdateObject).toHaveBeenCalledWith({
      object_id: "obj-roof",
      detail_weight: 2.0,
    });
  });

  it("toggles lock state on lock button click", () => {
    mockSelectedObjectId = "obj-roof";
    render(<ObjectInspector />);

    const lockBtn = screen.getByRole("button", { name: /Unlocked/i });
    fireEvent.click(lockBtn);
    expect(mockUpdateObject).toHaveBeenCalledWith({
      object_id: "obj-roof",
      locked: true,
    });
  });

  it("disables regenerate button when object is locked", () => {
    mockSelectedObjectId = "obj-cottage"; // locked: true in fixture
    render(<ObjectInspector />);

    const regenBtn = screen.getByRole("button", { name: /Regenerate "Cottage"/i });
    expect(regenBtn.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/Unlock to regenerate/i)).toBeDefined();
  });

  it("invokes hide and isolate callbacks without network calls", () => {
    render(<ObjectInspector />);

    const hideButtons = screen.getAllByTitle(/Hide in view/i);
    fireEvent.click(hideButtons[0]);
    expect(mockToggleHideObject).toHaveBeenCalled();

    const isolateButtons = screen.getAllByTitle(/Isolate object/i);
    fireEvent.click(isolateButtons[0]);
    expect(mockToggleIsolateObject).toHaveBeenCalled();
  });

  it("renders empty state when revision has no objects", () => {
    const prevBundle = mockBundle;
    mockBundle = {
      ...prevBundle!,
      objects: { schemaVersion: 1, objects: [] },
    };
    render(<ObjectInspector />);
    expect(screen.getByText(/No semantic objects/i)).toBeDefined();
    mockBundle = prevBundle;
  });
});
