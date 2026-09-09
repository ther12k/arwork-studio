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
