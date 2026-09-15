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
  nearestOnPathCommands,
  parsePathCommands,
  polylineNearestDistance,
  removePathAnchor,
  serializePathCommands,
  splitPathCommand,
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

// ------------------------------------------- Task 37: add/remove nodes

const LENS_D = "M 70,180 C 70,60 230,60 230,180 C 230,300 70,300 70,180 Z";

describe("nearestOnPathCommands — click hit-testing on drawn segments", () => {
  test("a tap on the cubic's analytic midpoint resolves to that segment near t=0.5", () => {
    const hit = nearestOnPathCommands(parsePathCommands(LENS_D), { x: 150, y: 90 })!;
    expect(hit).toBeTruthy();
    expect(hit.cmd).toBe(1);
    expect(Math.abs(hit.t - 0.5)).toBeLessThan(0.03);
    expect(hit.dist).toBeLessThan(0.5);
  });

  test("a point on the second segment resolves there; a far point reports its distance", () => {
    const cmds = parsePathCommands(LENS_D);
    const hit = nearestOnPathCommands(cmds, { x: 150, y: 270 })!;
    expect(hit.cmd).toBe(2);
    const far = nearestOnPathCommands(cmds, { x: 400, y: 400 })!;
    expect(far.dist).toBeGreaterThan(100);
  });

  test("M and Z are never hit targets (only the two cubics answer)", () => {
    const cmds = parsePathCommands(LENS_D);
    for (const p of [
      { x: 70, y: 180 },
      { x: 230, y: 180 },
      { x: 150, y: 90 },
    ]) {
      const hit = nearestOnPathCommands(cmds, p)!;
      expect(hit.cmd).toBeGreaterThanOrEqual(1);
      expect(hit.cmd).toBeLessThanOrEqual(2);
    }
  });
});

describe("splitPathCommand — de Casteljau split preserves geometry exactly", () => {
  const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

  test("cubic split controls follow the de Casteljau construction", () => {
    // Curve M(0,0) C(30,0)(60,30)(60,60) split at t=0.35.
    const cmds = parsePathCommands("M 0,0 C 30,0 60,30 60,60 L 0,60 Z");
    const next = splitPathCommand(cmds, 1, 0.35)!;
    expect(next).not.toBeNull();
    expect(next.length).toBe(cmds.length + 1);
    expect(next[next.length - 1].op).toBe("Z");
    const t = 0.35;
    const p0 = { x: 0, y: 0 };
    const p1 = { x: 30, y: 0 };
    const p2 = { x: 60, y: 30 };
    const p3 = { x: 60, y: 60 };
    const q0 = { x: lerp(p0.x, p1.x, t), y: lerp(p0.y, p1.y, t) };
    const q1 = { x: lerp(p1.x, p2.x, t), y: lerp(p1.y, p2.y, t) };
    const q2 = { x: lerp(p2.x, p3.x, t), y: lerp(p2.y, p3.y, t) };
    const r0 = { x: lerp(q0.x, q1.x, t), y: lerp(q0.y, q1.y, t) };
    const r1 = { x: lerp(q1.x, q2.x, t), y: lerp(q1.y, q2.y, t) };
    const s = { x: lerp(r0.x, r1.x, t), y: lerp(r0.y, r1.y, t) };
    const left = next[1];
    const right = next[2];
    expect(left.op).toBe("C");
    expect(right.op).toBe("C");
    for (const [got, want] of [
      [left.pts[0], q0],
      [left.pts[1], r0],
      [left.pts[2], s],
      [right.pts[0], r1],
      [right.pts[1], q2],
      [right.pts[2], p3],
    ]) {
      expect(Math.hypot(got.x - want.x, got.y - want.y)).toBeLessThan(1e-9);
    }
  });

  test("the two halves reproduce the original curve point-for-point (C, Q, L)", () => {
    // Independent Bernstein evaluation: for every u, the left half at u is
    // the original at t*u, and the right half at u is the original at
    // t+(1-t)*u — the defining property of an exact subdivision. (A finite-
    // sampling distance check cannot prove this: its error floor is the
    // sampling step, not the math.)
    const at = (
      start: { x: number; y: number },
      c: { op: string; pts: Array<{ x: number; y: number }> },
      t: number
    ) => {
      const u = 1 - t;
      if (c.op === "L") {
        return {
          x: start.x + (c.pts[0].x - start.x) * t,
          y: start.y + (c.pts[0].y - start.y) * t,
        };
      }
      if (c.op === "C") {
        const [p1, p2, p3] = c.pts;
        return {
          x: u * u * u * start.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
          y: u * u * u * start.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y,
        };
      }
      const [p1, p2] = c.pts;
      return {
        x: u * u * start.x + 2 * u * t * p1.x + t * t * p2.x,
        y: u * u * start.y + 2 * u * t * p1.y + t * t * p2.y,
      };
    };
    const cases: Array<[string, number]> = [
      ["M 70,180 C 70,60 230,60 230,180 C 230,300 70,300 70,180 Z", 0.4],
      ["M 10,90 Q 90,10 170,90 Q 90,170 10,90 Z", 0.65],
      ["M 0,0 L 60,0 L 60,60 L 0,60 Z", 0.3],
    ];
    for (const [d, t] of cases) {
      const cmds = parsePathCommands(d);
      const split = splitPathCommand(cmds, 1, t)!;
      const start = cmds[0].pts[0];
      const orig = cmds[1];
      const left = split[1];
      const right = split[2];
      for (const u of [0, 0.25, 0.5, 0.75, 1]) {
        const a = at(start, orig, t * u);
        const b = at(start, left, u);
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeLessThan(1e-9);
        const c = at(start, orig, t + (1 - t) * u);
        const e = at(left.pts[left.pts.length - 1], right, u);
        expect(Math.hypot(c.x - e.x, c.y - e.y)).toBeLessThan(1e-9);
      }
      // The edited commands survive the serialize→parse format round trip.
      const rt = parsePathCommands(serializePathCommands(split));
      expect(rt.length).toBe(split.length);
      expect(rt.map((x) => x.op)).toEqual(split.map((x) => x.op));
    }
  });

  test("t is clamped away from the ends; M and Z cannot be split", () => {
    const cmds = parsePathCommands(LENS_D);
    const nearStart = splitPathCommand(cmds, 1, 0)!;
    const nearEnd = splitPathCommand(cmds, 1, 1)!;
    // Degenerate halves are prevented: both split points sit inside the curve.
    const s1 = nearStart[1].pts[2];
    const s2 = nearEnd[1].pts[2];
    expect(Math.hypot(s1.x - 70, s1.y - 180)).toBeGreaterThan(0.5);
    expect(Math.hypot(s2.x - 230, s2.y - 180)).toBeGreaterThan(0.5);
    expect(splitPathCommand(cmds, 0, 0.5)).toBeNull();
    expect(splitPathCommand(cmds, cmds.length - 1, 0.5)).toBeNull();
  });
});

describe("removePathAnchor — neighbor merge with guards", () => {
  test("C+C merge keeps the outer control points (endpoint tangents survive)", () => {
    const cmds = parsePathCommands(
      "M 0,0 C 10,0 20,10 20,20 C 20,30 10,40 0,40 C -10,30 -20,10 0,0 Z"
    );
    const next = removePathAnchor(cmds, 1)!;
    expect(next).not.toBeNull();
    expect(next.length).toBe(cmds.length - 1);
    expect(next[1].op).toBe("C");
    expect(next[1].pts[0]).toEqual({ x: 10, y: 0 }); // c1 of the first segment
    expect(next[1].pts[1]).toEqual({ x: 10, y: 40 }); // c2 of the second
    expect(next[1].pts[2]).toEqual({ x: 0, y: 40 }); // end anchor
    expect(next[next.length - 1].op).toBe("Z");
    // Round-trips through the parser.
    expect(parsePathCommands(serializePathCommands(next)).length).toBe(next.length);
  });

  test("L+L merges to a line; any curve+line mix degrades to a line", () => {
    const lls = parsePathCommands("M 0,0 L 10,0 L 20,10 L 0,30 Z");
    const ll = removePathAnchor(lls, 1)!;
    expect(ll[1].op).toBe("L");
    expect(ll[1].pts[0]).toEqual({ x: 20, y: 10 });
    const mixed = parsePathCommands("M 0,0 C 10,0 20,10 20,20 L 0,40 C -10,30 -20,10 0,0 Z");
    const mx = removePathAnchor(mixed, 1)!;
    expect(mx[1].op).toBe("L");
    expect(mx[1].pts[0]).toEqual({ x: 0, y: 40 });
  });

  test("Q+Q merges keeping the first control; M, last segment, and thin rings are refused", () => {
    const qs = parsePathCommands("M 10,90 Q 90,10 170,90 Q 90,170 10,90 Z");
    expect(removePathAnchor(qs, 1)).toBeNull(); // only 2 edges — below the floor
    const q3 = parsePathCommands("M 10,90 Q 50,10 90,90 Q 130,170 170,90 Q 90,250 10,90 Z");
    const merged = removePathAnchor(q3, 1)!;
    expect(merged[1].op).toBe("Q");
    expect(merged[1].pts[0]).toEqual({ x: 50, y: 10 });
    expect(merged[1].pts[1]).toEqual({ x: 170, y: 90 });
    // Guards: M anchor (0), the last segment's anchor (no following segment).
    expect(removePathAnchor(q3, 0)).toBeNull();
    expect(removePathAnchor(q3, q3.length - 2)).toBeNull();
  });
});

describe("removePathAnchor — open strokes", () => {
  test("an open 2-segment stroke merges down to one segment (M anchor kept)", () => {
    const cmds = parsePathCommands("M 40,40 C 90,10 150,90 200,50 C 230,30 260,60 280,40");
    expect(cmds[cmds.length - 1].op).not.toBe("Z"); // open
    const next = removePathAnchor(cmds, 1)!;
    expect(next).not.toBeNull();
    expect(next.length).toBe(2); // M + merged C
    expect(next[1].op).toBe("C");
    // Outer controls survive: c1 of the first, c2 of the second.
    expect(next[1].pts[0]).toEqual({ x: 90, y: 10 });
    expect(next[1].pts[1]).toEqual({ x: 260, y: 60 });
    expect(next[1].pts[2]).toEqual({ x: 280, y: 40 });
    // A single-segment stroke (one interior anchor left) is refused.
    expect(removePathAnchor(next, 1)).toBeNull();
  });

  test("a closed 2-edge ring still refuses (closed floor stays 3)", () => {
    const cmds = parsePathCommands("M 0,0 C 10,0 20,10 20,20 C 20,30 10,40 0,40 Z");
    expect(cmds.filter((c) => c.op !== "M" && c.op !== "Z").length).toBe(2);
    expect(removePathAnchor(cmds, 1)).toBeNull();
  });
});
