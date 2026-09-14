/**
 * Task 33 — Artwork Path node editing helpers (svg-path command layer).
 *
 * parsePathCommands / serializePathCommands are the round-trip the Art Node
 * tool relies on: the board's paint `d` is parsed into editable absolute
 * commands, dragged in the overlay, serialized back, and submitted as the
 * new master path. Identity depends on the geometry surviving verbatim
 * (2-decimal rounding is the only normalization).
 */

import { describe, expect, test } from "vitest";
import {
  flattenPath,
  parsePathCommands,
  polylineNearestDistance,
  serializePathCommands,
} from "./svg-path";

describe("parsePathCommands", () => {
  test("parses M/L/C/Q/Z into absolute commands", () => {
    const cmds = parsePathCommands("M 10,20 L 30,40 C 50,60 70,80 90,100 Q 110,120 130,140 Z");
    expect(cmds.map((c) => c.op)).toEqual(["M", "L", "C", "Q", "Z"]);
    expect(cmds[0].pts).toEqual([{ x: 10, y: 20 }]);
    expect(cmds[2].pts).toEqual([
      { x: 50, y: 60 },
      { x: 70, y: 80 },
      { x: 90, y: 100 },
    ]);
    expect(cmds[3].pts).toEqual([
      { x: 110, y: 120 },
      { x: 130, y: 140 },
    ]);
  });

  test("normalizes relative commands to absolute", () => {
    const cmds = parsePathCommands("M 10,10 l 20,0 c 5,5 10,10 15,15 z");
    expect(cmds[1]).toEqual({ op: "L", pts: [{ x: 30, y: 10 }] });
    expect(cmds[2].pts[2]).toEqual({ x: 45, y: 25 });
    expect(cmds[3].op).toBe("Z");
  });

  test("repeated M pairs become M then L (SVG semantics)", () => {
    const cmds = parsePathCommands("M 0,0 50,50");
    expect(cmds.map((c) => c.op)).toEqual(["M", "L"]);
  });

  test("malformed input keeps the commands parsed so far", () => {
    expect(parsePathCommands("M 10,10 C 20,20")).toEqual([{ op: "M", pts: [{ x: 10, y: 10 }] }]);
    expect(parsePathCommands("junk")).toEqual([]);
    expect(parsePathCommands("")).toEqual([]);
  });
});

describe("serializePathCommands round trip", () => {
  test("parse → serialize → parse is identity (2-decimal normalization)", () => {
    const d = "M 10.13,20.5 L 30,40 C 50.5,60 70.25,80 90,100 Q 110,120 130,140.75 Z";
    const once = parsePathCommands(d);
    const round = parsePathCommands(serializePathCommands(once));
    expect(round).toEqual(once);
  });

  test("curve commands survive round trip without flattening to lines", () => {
    const d = "M 10,170 C 30,60 130,30 180,80 C 230,130 270,180 200,230 Z";
    const out = serializePathCommands(parsePathCommands(d));
    expect(out).toContain("C 30,60 130,30 180,80");
    expect(out.endsWith("Z")).toBe(true);
  });

  test("serialized output flattens to the same polyline as the input", () => {
    const d = "M 50,100 C 50,60 120,40 160,70 C 200,100 200,160 160,190 Z";
    const a = flattenPath(d);
    const b = flattenPath(serializePathCommands(parsePathCommands(d)));
    expect(a.length).toBe(b.length);
    for (let i = 0; i < a.length; i++) {
      expect(Math.hypot(a[i].x - b[i].x, a[i].y - b[i].y)).toBeLessThan(0.02);
    }
  });

  test("dragging a handle updates only that control point", () => {
    const cmds = parsePathCommands("M 10,10 C 20,20 40,40 60,60 Z");
    const dragged = cmds.map((c, i) =>
      i === 1 ? { ...c, pts: c.pts.map((q, j) => (j === 0 ? { x: 99, y: 5 } : q)) } : c
    );
    expect(dragged[1].pts[0]).toEqual({ x: 99, y: 5 });
    expect(dragged[1].pts[1]).toEqual({ x: 40, y: 40 }); // untouched
    expect(dragged[1].pts[2]).toEqual({ x: 60, y: 60 }); // endpoint untouched
  });
});

// --- Review round: SVG semantics the first fixtures did not cover -----------

describe("parsePathCommands — repeated parameter groups and closepath state", () => {
  test("one C command token with two parameter groups yields two cubics", () => {
    const cmds = parsePathCommands("M 10,10 C 20,20 30,20 40,10 50,0 60,0 70,10 Z");
    expect(cmds.map((c) => c.op)).toEqual(["M", "C", "C", "Z"]);
    expect(cmds[1].pts).toEqual([
      { x: 20, y: 20 },
      { x: 30, y: 20 },
      { x: 40, y: 10 },
    ]);
    // The second group chains from the first group's endpoint, not from (0,0).
    expect(cmds[2].pts).toEqual([
      { x: 50, y: 0 },
      { x: 60, y: 0 },
      { x: 70, y: 10 },
    ]);
    // …and round-trips without dropping the second curve.
    const out = parsePathCommands(serializePathCommands(cmds));
    expect(out).toEqual(cmds);
  });

  test("repeated Q parameter groups yield two quadratics", () => {
    const cmds = parsePathCommands("M 0,0 Q 10,10 20,0 30,-10 40,0 Z");
    expect(cmds.map((c) => c.op)).toEqual(["M", "Q", "Q", "Z"]);
    expect(cmds[2].pts[1]).toEqual({ x: 40, y: 0 });
  });

  test("Z returns the current point to the subpath start (relative move after close)", () => {
    const cmds = parsePathCommands("M 10,10 L 30,10 L 30,30 Z m 5,5 l 10,0 l 0,10 z");
    expect(cmds.filter((c) => c.op === "M").map((c) => c.pts[0])).toEqual([
      { x: 10, y: 10 },
      { x: 15, y: 15 }, // relative to the closed subpath's START, not (30,30)
    ]);
  });

  test("an incomplete trailing group is still dropped without throwing", () => {
    const cmds = parsePathCommands("M 0,0 C 1,1 2,2 3,3 4,4");
    expect(cmds.map((c) => c.op)).toEqual(["M", "C"]);
    expect(cmds[1].pts[2]).toEqual({ x: 3, y: 3 });
  });
});

describe("polylineNearestDistance — query point comes first", () => {
  test("segment midpoint, endpoint, perpendicular offset, degenerate segment", () => {
    const seg = [
      { x: 0, y: 0 },
      { x: 100, y: 0 },
    ];
    // Middle of a long edge — the exact tap the outline pickers must accept.
    expect(polylineNearestDistance(seg, { x: 50, y: 0 })).toBe(0);
    expect(polylineNearestDistance(seg, { x: 100, y: 0 })).toBe(0);
    expect(polylineNearestDistance(seg, { x: 50, y: 7 })).toBe(7);
    expect(polylineNearestDistance(seg, { x: 130, y: 0 })).toBe(30); // clamped past the end
    // Degenerate segment (start === end) measures plain point distance.
    expect(polylineNearestDistance([{ x: 5, y: 5 }, { x: 5, y: 5 }], { x: 5, y: 9 })).toBe(4);
  });

  test("outline picker regression (Node + Art node): tap mid-edge of a flattened rectangle hits", () => {
    // Reviewer probe: tap exactly halfway along the top edge measured 89.44
    // units when the arguments were swapped — beyond the 20-unit threshold.
    const rect = flattenPath("M 100,100 L 300,100 L 300,300 L 100,300 Z");
    expect(polylineNearestDistance(rect, { x: 200, y: 100 })).toBe(0);
    expect(polylineNearestDistance(rect, { x: 100, y: 200 })).toBe(0);
    expect(polylineNearestDistance(rect, { x: 110, y: 105 })).toBeLessThan(20);
  });
});

describe("whole-path preview flatten — continuity and closure", () => {
  test("a closed cubic path flattens back to the subpath start (Z draws a real segment)", () => {
    const pts = flattenPath("M 100,100 C 100,200 200,200 200,100 Z");
    const first = pts[0];
    const last = pts[pts.length - 1];
    expect(first).toEqual({ x: 100, y: 100 });
    expect(last).toEqual({ x: 100, y: 100 });
    // The closing segment exists: the run contains interior curve points.
    expect(pts.length).toBeGreaterThan(3);
  });

  test("serialize→flatten of parsed commands matches flatten of the source path", () => {
    // The preview contract: the artist edits against the geometry that Save
    // submits. serializePathCommands(parsePathCommands(d)) flattened in ONE
    // pass must equal the source polyline (the artGeometry preview does
    // exactly this).
    const d = "M 100,100 C 100,200 200,200 200,100 L 100,100 Z";
    const a = flattenPath(d);
    const b = flattenPath(serializePathCommands(parsePathCommands(d)));
    expect(a.length).toBe(b.length);
    for (let i = 0; i < a.length; i++) {
      expect(Math.hypot(a[i].x - b[i].x, a[i].y - b[i].y)).toBeLessThan(0.02);
    }
    // Cubic midpoint continuity: a per-command flatten would evaluate this
    // segment from (0,0); the whole-path flatten passes through (150,175) —
    // the analytic midpoint of the cubic at t=0.5.
    const expected = { x: 150, y: 175 };
    const best = Math.min(...a.map((p) => Math.hypot(p.x - expected.x, p.y - expected.y)));
    expect(best).toBeLessThan(1);
  });
});
