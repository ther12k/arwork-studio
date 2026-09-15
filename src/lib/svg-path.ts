/**
 * SVG path flattening + polyline distance helpers (stage 3, contract A).
 *
 * `flattenPath` parses the studio's safe path subset — M/L/C/Q/Z, absolute AND
 * relative, numbers separated by commas and/or whitespace, scientific notation
 * — into a polyline of artwork-space points. Curves are evaluated
 * parametrically (de Casteljau / Bernstein form) with `curveSamples` points
 * per segment; the segment start is never pushed (it is the previous command's
 * endpoint) so consecutive commands chain without duplicates.
 *
 * Both the Node tool (edge hit-testing + boundary ghost rendering) and any
 * future polyline math share this one source. Malformed input never throws —
 * the points parsed so far are returned.
 */

export interface FlatPoint {
  x: number;
  y: number;
}

type Token = { kind: "cmd"; ch: string } | { kind: "num"; value: number };

/** Command letters of the safe subset; everything else is separators/junk. */
const CMD_RE = /[MLQCZmlqcz]/;
/** SVG number: digits with optional fraction + exponent, optional sign. */
const NUM_RE = /[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/g;

function tokenize(d: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < d.length) {
    const ch = d[i];
    if (CMD_RE.test(ch)) {
      tokens.push({ kind: "cmd", ch });
      i++;
      continue;
    }
    NUM_RE.lastIndex = i;
    const m = NUM_RE.exec(d);
    if (m && m.index === i && m[0].length > 0) {
      const value = Number(m[0]);
      if (Number.isFinite(value)) tokens.push({ kind: "num", value });
      i += m[0].length;
      continue;
    }
    i++; // separators (space, comma) and anything unparsable
  }
  return tokens;
}

function cubicAt(p0: FlatPoint, p1: FlatPoint, p2: FlatPoint, p3: FlatPoint, t: number): FlatPoint {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const e = t * t * t;
  return {
    x: a * p0.x + b * p1.x + c * p2.x + e * p3.x,
    y: a * p0.y + b * p1.y + c * p2.y + e * p3.y,
  };
}

function quadAt(p0: FlatPoint, p1: FlatPoint, p2: FlatPoint, t: number): FlatPoint {
  const u = 1 - t;
  const a = u * u;
  const b = 2 * u * t;
  const c = t * t;
  return {
    x: a * p0.x + b * p1.x + c * p2.x,
    y: a * p0.y + b * p1.y + c * p2.y,
  };
}

/**
 * Flatten path data into a polyline. `M`/`L` push their endpoint (repeated
 * implicit pairs follow SVG semantics: extra `M` pairs become linetos),
 * `C`/`Q` push `curveSamples` points including the curve endpoint, and `Z`
 * closes back to the subpath start when it is not already the last point.
 */
export function flattenPath(d: string, curveSamples = 8): FlatPoint[] {
  const pts: FlatPoint[] = [];
  if (typeof d !== "string" || d.length === 0) return pts;
  const tokens = tokenize(d);
  const samples = Math.max(1, Math.floor(curveSamples));
  let i = 0;
  let cur: FlatPoint = { x: 0, y: 0 };
  let start: FlatPoint = { x: 0, y: 0 };

  const push = (x: number, y: number) => {
    const last = pts[pts.length - 1];
    if (!last || last.x !== x || last.y !== y) pts.push({ x, y });
  };
  const readPair = (): FlatPoint | null => {
    const a = tokens[i];
    const b = tokens[i + 1];
    if (!a || !b || a.kind !== "num" || b.kind !== "num") return null;
    i += 2;
    return { x: a.value, y: b.value };
  };

  while (i < tokens.length) {
    const tok = tokens[i];
    if (tok.kind !== "cmd") {
      i++; // stray numbers before any command — skip defensively
      continue;
    }
    const rel = tok.ch === tok.ch.toLowerCase();
    const op = tok.ch.toUpperCase();
    i++;

    if (op === "Z") {
      push(start.x, start.y);
      cur = { x: start.x, y: start.y };
      continue;
    }

    if (op === "M" || op === "L") {
      let first = true;
      for (;;) {
        const pair = readPair();
        if (!pair) break;
        const x = rel ? cur.x + pair.x : pair.x;
        const y = rel ? cur.y + pair.y : pair.y;
        if (op === "M" && first) {
          start = { x, y };
          first = false;
        }
        push(x, y);
        cur = { x, y };
      }
      continue;
    }

    // C (cubic, 3 pairs) / Q (quadratic, 2 pairs) — repeated argument sets
    // per SVG; incomplete sets stop parsing (points so far are kept).
    const argPairs = op === "C" ? 3 : op === "Q" ? 2 : 0;
    if (!argPairs) continue; // unreachable given the tokenizer, kept defensive
    for (;;) {
      const coords: FlatPoint[] = [];
      let complete = true;
      for (let c = 0; c < argPairs; c++) {
        const pair = readPair();
        if (!pair) {
          complete = false;
          break;
        }
        coords.push({ x: rel ? cur.x + pair.x : pair.x, y: rel ? cur.y + pair.y : pair.y });
      }
      if (!complete) break;
      const last = coords[coords.length - 1];
      for (let k = 1; k <= samples; k++) {
        const t = k / samples;
        const p =
          op === "C"
            ? cubicAt(cur, coords[0], coords[1], coords[2], t)
            : quadAt(cur, coords[0], coords[1], t);
        push(p.x, p.y);
      }
      cur = { x: last.x, y: last.y };
    }
  }
  return pts;
}

/** Point-to-segment distance (projection clamped onto the segment). */
function pointSegmentDistance(p: FlatPoint, a: FlatPoint, b: FlatPoint): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lenSq = dx * dx + dy * dy;
  let t = 0;
  if (lenSq > 0) t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

/** Minimum distance from `p` to the polyline (Infinity when empty). */
export function polylineNearestDistance(pts: FlatPoint[], p: FlatPoint): number {
  if (!pts.length) return Infinity;
  if (pts.length === 1) return Math.hypot(p.x - pts[0].x, p.y - pts[0].y);
  let best = Infinity;
  for (let i = 1; i < pts.length; i++) {
    const d = pointSegmentDistance(p, pts[i - 1], pts[i]);
    if (d < best) best = d;
  }
  return best;
}

// ---------------------------------------------------------------------------
// Task 33 — Artwork Path node editing: parse into editable commands and back.
// ---------------------------------------------------------------------------

/** One editable path command. Points are ABSOLUTE artwork coordinates:
 *  M/L carry their endpoint, C its 3 points (c1, c2, end), Q its 2 (ctrl, end),
 *  Z none. Serialization always emits absolute commands. */
export interface PathCommand {
  op: "M" | "L" | "C" | "Q" | "Z";
  pts: FlatPoint[];
}

const CMD_ARG_PAIRS: Record<string, number> = { M: 1, L: 1, C: 3, Q: 2, Z: 0 };

/** Parse the studio's safe subset into commands (relative input normalized to
 *  absolute). Malformed input never throws — the commands parsed so far are
 *  returned, mirroring flattenPath's tolerance. */
export function parsePathCommands(d: string): PathCommand[] {
  const out: PathCommand[] = [];
  if (typeof d !== "string" || d.length === 0) return out;
  const tokens = tokenize(d);
  let i = 0;
  let cur: FlatPoint = { x: 0, y: 0 };
  let start: FlatPoint = { x: 0, y: 0 };
  while (i < tokens.length) {
    const tok = tokens[i];
    if (tok.kind !== "cmd") {
      i++;
      continue;
    }
    const rel = tok.ch === tok.ch.toLowerCase();
    const op = tok.ch.toUpperCase();
    i++;
    const argPairs = CMD_ARG_PAIRS[op];
    if (argPairs === undefined) continue;
    if (op === "Z") {
      out.push({ op: "Z", pts: [] });
      // Closepath returns the current point to the subpath start (a following
      // relative move is relative to it), mirroring flattenPath.
      cur = { x: start.x, y: start.y };
      continue;
    }
    // M follows SVG semantics: only the first pair is a moveto, subsequent
    // pairs are linetos. C/Q accept repeated parameter groups.
    let first = true;
    for (;;) {
      const coords: FlatPoint[] = [];
      let complete = true;
      for (let c = 0; c < argPairs; c++) {
        const a = tokens[i];
        const b = tokens[i + 1];
        if (!a || !b || a.kind !== "num" || b.kind !== "num") {
          complete = false;
          break;
        }
        i += 2;
        coords.push({ x: rel ? cur.x + a.value : a.value, y: rel ? cur.y + b.value : b.value });
      }
      if (!complete) break;
      if (op === "M" && first) start = { x: coords[0].x, y: coords[0].y };
      const outOp: PathCommand["op"] = op === "M" && !first ? "L" : (op as PathCommand["op"]);
      out.push({ op: outOp, pts: coords });
      const last = coords[coords.length - 1];
      cur = { x: last.x, y: last.y };
      first = false;
    }
  }
  return out;
}

/** Serialize commands back to path data (absolute, 2-decimal coordinates,
 *  leading M guaranteed by the caller). Round-trips parsePathCommands. */
export function serializePathCommands(cmds: PathCommand[]): string {
  const n = (v: number) => String(Math.round(v * 100) / 100);
  const parts: string[] = [];
  for (const c of cmds) {
    if (c.op === "Z") {
      parts.push("Z");
      continue;
    }
    parts.push(c.op + " " + c.pts.map((p) => `${n(p.x)},${n(p.y)}`).join(" "));
  }
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// Task 37 — add/remove nodes: segment hit-testing, de Casteljau split, anchor
// removal by neighbor merge. Pure functions over PathCommand[]; the caller
// (CanvasWorkspace) owns history/dirty bookkeeping.
// ---------------------------------------------------------------------------

const lerpPt = (a: FlatPoint, b: FlatPoint, t: number): FlatPoint => ({
  x: a.x + (b.x - a.x) * t,
  y: a.y + (b.y - a.y) * t,
});

/** Drawable segment commands (M is a point, Z closes implicitly). */
function isSegment(c: PathCommand): boolean {
  return c.op === "L" || c.op === "C" || c.op === "Q";
}

/** Point ON a segment command at parameter t. The segment's start anchor is
 *  not stored on the command — the caller supplies the previous command's
 *  endpoint, exactly how the renderer chains commands. */
function segmentPointAt(start: FlatPoint, c: PathCommand, t: number): FlatPoint {
  if (c.op === "L") return lerpPt(start, c.pts[0], t);
  if (c.op === "C") return cubicAt(start, c.pts[0], c.pts[1], c.pts[2], t);
  return quadAt(start, c.pts[0], c.pts[1], t);
}

export interface PathHit {
  cmd: number;
  t: number;
  x: number;
  y: number;
  dist: number;
}

/** Nearest point on the path's DRAWN segments (M and the closing Z chord are
 *  never hit targets — a node cannot be added on the closure). Curves are
 *  sampled and the query is projected onto the sampled chords, giving
 *  sub-sample parameter accuracy — precise enough for click hit-testing. */
export function nearestOnPathCommands(
  cmds: PathCommand[],
  p: FlatPoint,
  curveSamples = 24
): PathHit | null {
  let best: PathHit | null = null;
  for (let ci = 1; ci < cmds.length; ci++) {
    const c = cmds[ci];
    if (!isSegment(c)) continue;
    const prevPts = cmds[ci - 1].pts;
    const start = prevPts[prevPts.length - 1];
    const samples = c.op === "L" ? 1 : Math.max(2, curveSamples);
    let a = segmentPointAt(start, c, 0);
    for (let k = 1; k <= samples; k++) {
      const t1 = k / samples;
      const b = segmentPointAt(start, c, t1);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const lenSq = dx * dx + dy * dy;
      let s = 0;
      if (lenSq > 0) s = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq));
      const hx = a.x + s * dx;
      const hy = a.y + s * dy;
      const dist = Math.hypot(p.x - hx, p.y - hy);
      if (!best || dist < best.dist) {
        best = { cmd: ci, t: (k - 1 + s) / samples, x: hx, y: hy, dist };
      }
      a = b;
    }
  }
  return best;
}

/** Split one drawn segment at parameter t. de Casteljau subdivision — the
 *  two halves reproduce the original curve EXACTLY (no visual change); lines
 *  split by linear interpolation. Returns the new commands with the segment
 *  replaced by its two halves, or null when `cmdIndex` is not a drawn
 *  segment. t is clamped away from the ends so the split never creates a
 *  zero-length half. */
export function splitPathCommand(
  cmds: PathCommand[],
  cmdIndex: number,
  tRaw: number
): PathCommand[] | null {
  const c = cmds[cmdIndex];
  if (cmdIndex < 1 || !c || !isSegment(c)) return null;
  const t = Math.min(0.98, Math.max(0.02, tRaw));
  const prevPts = cmds[cmdIndex - 1].pts;
  const p0 = prevPts[prevPts.length - 1];
  const next = cmds.slice();
  if (c.op === "L") {
    const s = lerpPt(p0, c.pts[0], t);
    next.splice(cmdIndex, 1, { op: "L", pts: [s] }, { op: "L", pts: [{ ...c.pts[0] }] });
    return next;
  }
  if (c.op === "C") {
    const [p1, p2, p3] = c.pts;
    const q0 = lerpPt(p0, p1, t);
    const q1 = lerpPt(p1, p2, t);
    const q2 = lerpPt(p2, p3, t);
    const r0 = lerpPt(q0, q1, t);
    const r1 = lerpPt(q1, q2, t);
    const s = lerpPt(r0, r1, t);
    next.splice(
      cmdIndex,
      1,
      { op: "C", pts: [q0, r0, s] },
      { op: "C", pts: [r1, q2, { ...p3 }] }
    );
    return next;
  }
  const [p1, p2] = c.pts;
  const q0 = lerpPt(p0, p1, t);
  const q1 = lerpPt(p1, p2, t);
  const s = lerpPt(q0, q1, t);
  next.splice(cmdIndex, 1, { op: "Q", pts: [q0, s] }, { op: "Q", pts: [q1, { ...p2 }] });
  return next;
}

/** Remove the anchor that ENDS segment `cmdIndex` by merging that segment
 *  with the FOLLOWING one. C+C and Q+Q merges keep the outer control points
 *  so the endpoint tangents survive; any line involved degrades the merge to
 *  a straight L (a single segment cannot represent two arbitrary curves).
 *  Guards: the M anchor and the last segment's anchor are not removable, and
 *  a closed ring keeps at least three edges. Returns the new commands or
 *  null. */
export function removePathAnchor(cmds: PathCommand[], cmdIndex: number): PathCommand[] | null {
  const cur = cmds[cmdIndex];
  if (cmdIndex < 1 || !cur || !isSegment(cur)) return null;
  const nxt = cmds[cmdIndex + 1];
  if (!nxt || !isSegment(nxt)) return null;
  if (cmds.filter(isSegment).length < 3) return null;
  const end = nxt.pts[nxt.pts.length - 1];
  let merged: PathCommand;
  if (cur.op === "C" && nxt.op === "C") {
    merged = { op: "C", pts: [cur.pts[0], nxt.pts[1], end] };
  } else if (cur.op === "Q" && nxt.op === "Q") {
    merged = { op: "Q", pts: [cur.pts[0], end] };
  } else {
    merged = { op: "L", pts: [end] };
  }
  const next = cmds.slice();
  next.splice(cmdIndex, 2, merged);
  return next;
}
