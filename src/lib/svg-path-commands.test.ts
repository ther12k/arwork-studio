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
