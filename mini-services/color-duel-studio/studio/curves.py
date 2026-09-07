"""Curve-preserving geometry kernel for Color Duel Art Studio.

The AUTHORITATIVE geometry of every region/paint/ink shape is a list of SVG path
commands (M / L / C / Q / Z, absolute, arcs pre-converted to cubics).  Flattened
polygon rings are DERIVED approximations produced by :func:`flatten_path` with an
explicit tolerance; they are never a replacement for the master geometry.

Design contract enforced by this module:
- ``parse_path`` normalizes any SVG ``d`` string into absolute M/L/C/Q/Z
  (relative coordinates resolved, H/V folded to L, S/T reflected to C/Q,
  elliptical arcs converted to cubic runs).
- ``format_path`` emits only characters matched by the runtime safety pattern
  ``^[M][0-9MLQCZ ,.eE+-]*[Z]$`` (no exponents are actually produced).
- Shared boundary chains are fitted ONCE and reused by both neighbouring
  regions (possibly reversed via :func:`reverse_commands`), so the curved
  partition stays watertight by construction.
- Corners and straight architectural edges survive fitting: turning-angle
  corner detection splits spans, and spans that are already straight emit
  plain ``L`` commands.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Point = Tuple[float, float]
Command = tuple  # ('M',x,y) ('L',x,y) ('C',x1,y1,x2,y2,x,y) ('Q',qx,qy,x,y) ('Z',)

__all__ = [
    'parse_path', 'format_path', 'subpaths_of', 'flatten_path', 'flatten_subpath',
    'commands_bbox', 'rings_bbox', 'point_in_rings', 'point_in_rings_rule',
    'point_in_commands', 'solid_polygons',
    'evenodd_area', 'rdp', 'fit_polyline', 'fit_ring', 'reverse_commands',
    'parse_transform', 'mat_identity', 'mat_mul', 'mat_apply', 'transform_commands',
    'arc_to_cubics', 'snap_ring', 'max_deviation', 'fmt_num',
]

_WS = ' \t\n\r\f\v,'


# ---------------------------------------------------------------------------
# Number formatting / parsing
# ---------------------------------------------------------------------------

def fmt_num(n: float, precision: int = 3) -> str:
    """Format a coordinate without ever emitting unsafe characters."""
    if not math.isfinite(n):
        raise ValueError('Path coordinates must be finite.')
    text = f'{n:.{precision}f}'
    if float(text) == 0.0:  # normalizes -0.000 … to a stable "0"
        return '0'
    if n == int(n) and abs(n) < 1e15:
        return str(int(n))
    return text.rstrip('0').rstrip('.')


def _read_number(text: str, i: int) -> Tuple[float, int]:
    while i < len(text) and text[i] in _WS:
        i += 1
    if i >= len(text):
        raise ValueError('Unexpected end of path data.')
    start = i
    if text[i] in '+-':
        i += 1
    seen_digit = False
    seen_dot = False
    seen_exp = False
    while i < len(text):
        c = text[i]
        if c.isdigit():
            seen_digit = True
            i += 1
        elif c == '.' and not seen_dot and not seen_exp:
            seen_dot = True
            i += 1
        elif c in 'eE' and seen_digit and not seen_exp:
            j = i + 1
            if j < len(text) and text[j] in '+-':
                j += 1
            if j < len(text) and text[j].isdigit():
                seen_exp = True
                i = j + 1
            else:
                break
        else:
            break
    if not seen_digit:
        raise ValueError(f'Invalid number in path data at offset {start}.')
    return float(text[start:i]), i


def _read_flag(text: str, i: int) -> Tuple[int, int]:
    while i < len(text) and text[i] in _WS:
        i += 1
    if i >= len(text) or text[i] not in '01':
        raise ValueError('Arc flags must be a single 0 or 1 digit.')
    return (1 if text[i] == '1' else 0), i + 1


_ARG_COUNT = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2, 'A': 7, 'Z': 0}


def arc_to_cubics(x0: float, y0: float, rx: float, ry: float, phi_deg: float,
                  large: int, sweep: int, x1: float, y1: float) -> List[Command]:
    """Convert one elliptical-arc command into cubic runs (exact to ~1e-6)."""
    if rx == 0 or ry == 0 or (x0 == x1 and y0 == y1):
        return [('L', x1, y1)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg % 360.0)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    if den == 0:
        return [('L', x1, y1)]
    co = math.sqrt(max(0.0, num / den))
    if large == sweep:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x0 + x1) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y0 + y1) / 2.0

    def angle_between(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        if norm == 0:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, dot / norm)))
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    theta = angle_between(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle_between((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    segments = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2.0))))
    delta = dtheta / segments
    out: List[Command] = []
    th = theta

    def ellipse_point(t: float) -> Point:
        ex = rx * math.cos(t)
        ey = ry * math.sin(t)
        return (cos_p * ex - sin_p * ey + cx, sin_p * ex + cos_p * ey + cy)

    def ellipse_derivative(t: float) -> Point:
        dex = -rx * math.sin(t)
        dey = ry * math.cos(t)
        return (cos_p * dex - sin_p * dey, sin_p * dex + cos_p * dey)

    for _ in range(segments):
        t_next = th + delta
        p0 = ellipse_point(th)
        p3 = ellipse_point(t_next)
        d0 = ellipse_derivative(th)
        d3 = ellipse_derivative(t_next)
        k = delta / 3.0
        c1 = (p0[0] + k * d0[0], p0[1] + k * d0[1])
        c2 = (p3[0] - k * d3[0], p3[1] - k * d3[1])
        out.append(('C', c1[0], c1[1], c2[0], c2[1], p3[0], p3[1]))
        th = t_next
    return out


def parse_path(d: str) -> List[Command]:
    """Parse an SVG path ``d`` string into normalized absolute commands.

    Handles implicit argument repetition (pairs after M become L), relative
    coordinates, H/V folding, S/T control reflection, and A→C conversion.
    """
    if not isinstance(d, str):
        raise ValueError('Path data must be a string.')
    cmds: List[Command] = []
    i, n = 0, len(d)
    cx = cy = 0.0
    sx = sy = 0.0
    last_c2: Point | None = None   # second control point of the previous C/S
    last_q1: Point | None = None   # control point of the previous Q/T

    def skip_ws(index: int) -> int:
        while index < n and d[index] in _WS:
            index += 1
        return index

    def peek_number(index: int) -> bool:
        j = skip_ws(index)
        return j < n and (d[j].isdigit() or d[j] in '+-.')

    while True:
        i = skip_ws(i)
        if i >= n:
            break
        letter = d[i]
        if letter.upper() not in _ARG_COUNT:
            raise ValueError(f'Unsupported path command {letter!r}.')
        relative = letter.islower()
        base = letter.upper()
        i += 1
        effective = base  # M repeats degrade to L

        while True:  # implicit repetition loop
            if effective == 'Z':
                cmds.append(('Z',))
                cx, cy = sx, sy
                last_c2 = last_q1 = None
                break
            if effective == 'A':
                rx, i = _read_number(d, i)
                ry, i = _read_number(d, i)
                rot, i = _read_number(d, i)
                large, i = _read_flag(d, i)
                sweep, i = _read_flag(d, i)
                ax, i = _read_number(d, i)
                ay, i = _read_number(d, i)
                if relative:
                    ax += cx
                    ay += cy
                cubics = arc_to_cubics(cx, cy, rx, ry, rot, large, sweep, ax, ay)
                cmds.extend(cubics)
                cx, cy = cubics[-1][5], cubics[-1][6]
                last_c2 = (cubics[-1][3], cubics[-1][4])
                last_q1 = None
            elif effective == 'M' or effective == 'L':
                mx, i = _read_number(d, i)
                my, i = _read_number(d, i)
                if relative:
                    mx += cx
                    my += cy
                if effective == 'M':
                    cmds.append(('M', mx, my))
                    sx, sy = mx, my
                else:
                    cmds.append(('L', mx, my))
                cx, cy = mx, my
                last_c2 = last_q1 = None
            elif effective == 'H':
                hx, i = _read_number(d, i)
                x = hx + (cx if relative else 0.0)
                cmds.append(('L', x, cy))
                cx = x
                last_c2 = last_q1 = None
            elif effective == 'V':
                vy, i = _read_number(d, i)
                y = vy + (cy if relative else 0.0)
                cmds.append(('L', cx, y))
                cy = y
                last_c2 = last_q1 = None
            elif effective == 'C':
                c1x, i = _read_number(d, i)
                c1y, i = _read_number(d, i)
                c2x, i = _read_number(d, i)
                c2y, i = _read_number(d, i)
                ex, i = _read_number(d, i)
                ey, i = _read_number(d, i)
                if relative:
                    c1x += cx; c1y += cy; c2x += cx; c2y += cy; ex += cx; ey += cy
                cmds.append(('C', c1x, c1y, c2x, c2y, ex, ey))
                last_c2 = (c2x, c2y)
                last_q1 = None
                cx, cy = ex, ey
            elif effective == 'S':
                c2x, i = _read_number(d, i)
                c2y, i = _read_number(d, i)
                ex, i = _read_number(d, i)
                ey, i = _read_number(d, i)
                if relative:
                    c2x += cx; c2y += cy; ex += cx; ey += cy
                if last_c2 is None:
                    c1x, c1y = cx, cy
                else:
                    c1x, c1y = 2 * cx - last_c2[0], 2 * cy - last_c2[1]
                cmds.append(('C', c1x, c1y, c2x, c2y, ex, ey))
                last_c2 = (c2x, c2y)
                last_q1 = None
                cx, cy = ex, ey
            elif effective == 'Q':
                qx, i = _read_number(d, i)
                qy, i = _read_number(d, i)
                ex, i = _read_number(d, i)
                ey, i = _read_number(d, i)
                if relative:
                    qx += cx; qy += cy; ex += cx; ey += cy
                cmds.append(('Q', qx, qy, ex, ey))
                last_q1 = (qx, qy)
                last_c2 = None
                cx, cy = ex, ey
            elif effective == 'T':
                tx, i = _read_number(d, i)
                ty, i = _read_number(d, i)
                if relative:
                    tx += cx
                    ty += cy
                if last_q1 is None:
                    qx, qy = cx, cy
                else:
                    qx, qy = 2 * cx - last_q1[0], 2 * cy - last_q1[1]
                cmds.append(('Q', qx, qy, tx, ty))
                last_q1 = (qx, qy)
                last_c2 = None
                cx, cy = tx, ty
            else:  # pragma: no cover - guarded by _ARG_COUNT
                raise ValueError(f'Unsupported path command {effective!r}.')
            if peek_number(i):
                if effective == 'M':
                    effective = 'L'
                continue
            break

    if not cmds or cmds[0][0] != 'M':
        raise ValueError('Path data must begin with a moveto command.')
    return cmds


def subpaths_of(cmds: Sequence[Command]) -> List[List[Command]]:
    """Split a command list into subpaths, each starting with M."""
    out: List[List[Command]] = []
    current: List[Command] = []
    for cmd in cmds:
        if cmd[0] == 'M':
            if current:
                out.append(current)
            current = [cmd]
        else:
            if not current:
                raise ValueError('Path data must begin with a moveto command.')
            current.append(cmd)
    if current:
        out.append(current)
    return out


def format_path(cmds: Sequence[Command], precision: int = 3) -> str:
    """Serialize commands to a compact, runtime-safe d string."""
    parts: List[str] = []
    for cmd in cmds:
        tag = cmd[0]
        if tag == 'Z':
            parts.append('Z')
        elif tag == 'M':
            parts.append(f'M {fmt_num(cmd[1], precision)},{fmt_num(cmd[2], precision)}')
        elif tag == 'L':
            parts.append(f'L {fmt_num(cmd[1], precision)},{fmt_num(cmd[2], precision)}')
        elif tag == 'C':
            parts.append(
                f'C {fmt_num(cmd[1], precision)},{fmt_num(cmd[2], precision)} '
                f'{fmt_num(cmd[3], precision)},{fmt_num(cmd[4], precision)} '
                f'{fmt_num(cmd[5], precision)},{fmt_num(cmd[6], precision)}')
        elif tag == 'Q':
            parts.append(
                f'Q {fmt_num(cmd[1], precision)},{fmt_num(cmd[2], precision)} '
                f'{fmt_num(cmd[3], precision)},{fmt_num(cmd[4], precision)}')
        else:
            raise ValueError(f'Cannot serialize command {tag!r}.')
    return ' '.join(parts)


def commands_bbox(cmds: Sequence[Command]) -> Tuple[float, float, float, float]:
    """Conservative bbox including Bézier control points."""
    xs: List[float] = []
    ys: List[float] = []
    for cmd in cmds:
        for k in range(1, len(cmd), 2):
            xs.append(cmd[k])
            ys.append(cmd[k + 1])
    if not xs:
        raise ValueError('Empty path.')
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Flattening (derived approximation — tolerance is explicit and documented)
# ---------------------------------------------------------------------------

def _perp_dist(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy)
    if norm == 0:
        return math.dist(p, a)
    return abs(dx * (a[1] - p[1]) - dy * (a[0] - p[0])) / norm


def _flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point, tol: float,
                   out: List[Point], depth: int = 0) -> None:
    if depth > 24 or math.dist(p0, p3) < 1e-9:
        out.append(p3)
        return
    if max(_perp_dist(p1, p0, p3), _perp_dist(p2, p0, p3)) <= tol:
        out.append(p3)
        return
    # de Casteljau split at t = 0.5
    ax, ay = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    bx, by = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    cxx, cyy = (p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2
    dx, dy = (ax + bx) / 2, (ay + by) / 2
    ex, ey = (bx + cxx) / 2, (by + cyy) / 2
    fx, fy = (dx + ex) / 2, (dy + ey) / 2
    _flatten_cubic(p0, (ax, ay), (dx, dy), (fx, fy), tol, out, depth + 1)
    _flatten_cubic((fx, fy), (ex, ey), (cxx, cyy), p3, tol, out, depth + 1)


def _flatten_quad(p0: Point, p1: Point, p2: Point, tol: float,
                  out: List[Point], depth: int = 0) -> None:
    if depth > 24 or math.dist(p0, p2) < 1e-9:
        out.append(p2)
        return
    if _perp_dist(p1, p0, p2) <= tol:
        out.append(p2)
        return
    ax, ay = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    bx, by = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    mx, my = (ax + bx) / 2, (ay + by) / 2
    _flatten_quad(p0, (ax, ay), (mx, my), tol, out, depth + 1)
    _flatten_quad((mx, my), (bx, by), p2, tol, out, depth + 1)


def flatten_subpath(cmds: Sequence[Command], tol: float) -> List[Point]:
    """Flatten one subpath (starts with M, no inner M) to a polyline."""
    pts: List[Point] = []
    cur: Point = (cmds[0][1], cmds[0][2])
    pts.append(cur)
    for cmd in cmds[1:]:
        tag = cmd[0]
        if tag == 'Z':
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            cur = pts[0]
        elif tag == 'L':
            cur = (cmd[1], cmd[2])
            pts.append(cur)
        elif tag == 'C':
            _flatten_cubic(cur, (cmd[1], cmd[2]), (cmd[3], cmd[4]), (cmd[5], cmd[6]), tol, pts)
            cur = (cmd[5], cmd[6])
        elif tag == 'Q':
            _flatten_quad(cur, (cmd[1], cmd[2]), (cmd[3], cmd[4]), tol, pts)
            cur = (cmd[3], cmd[4])
        else:
            raise ValueError(f'Unexpected command in subpath: {tag!r}')
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()  # rings are stored without a duplicated closing vertex
    return pts


def flatten_path(cmds: Sequence[Command], tol: float) -> List[List[Point]]:
    """Flatten a full path into rings (one per subpath), rounded to 0.01px.

    Rounding to a fixed grid keeps shared boundaries bit-identical between
    neighbouring regions, which makes polygonal validation exact.
    """
    rings: List[List[Point]] = []
    for sub in subpaths_of(cmds):
        ring = flatten_subpath(sub, tol)
        if len(ring) >= 3:
            rings.append([(round(x, 2), round(y, 2)) for x, y in ring])
    return rings


def rings_bbox(rings: Sequence[Sequence[Point]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    if not xs:
        raise ValueError('Empty rings.')
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Even-odd point/area queries
# ---------------------------------------------------------------------------

def point_in_ring(ring: Sequence[Point], x: float, y: float) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for k in range(n):
        xi, yi = ring[k]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < cross:
                inside = not inside
        j = k
    return inside


def point_in_rings(rings: Sequence[Sequence[Point]], x: float, y: float) -> bool:
    count = 0
    for ring in rings:
        if point_in_ring(ring, x, y):
            count += 1
    return count % 2 == 1


def point_in_rings_rule(rings: Sequence[Sequence[Point]], x: float, y: float,
                        rule: str = 'evenodd') -> bool:
    """Point membership honouring the SVG fill rule (evenodd or nonzero)."""
    winding = 0
    hits = 0
    for ring in rings:
        if point_in_ring(ring, x, y):
            hits += 1
            winding += 1 if _ring_signed_area(ring) > 0 else -1
    if rule == 'nonzero':
        return winding != 0
    return hits % 2 == 1


def solid_polygons(rings, rule: str = 'evenodd'):
    """Convert flattened rings into solid polygon(s) honouring the fill rule.

    evenodd: ring nesting depth decides solid vs hole (even depth = solid).
    nonzero: cumulative ring orientation decides — a same-winding nested
    subpath is a UNION (not a hole), an opposite-winding one subtracts,
    exactly like SVG's nonzero rule.  Ring order is not trusted; self-
    touching rings are split via make_valid.
    """
    from shapely.geometry import Polygon
    from shapely import make_valid

    def parts_of(g):
        if g is None or g.is_empty:
            return []
        if g.geom_type == 'Polygon':
            return [g] if g.area > 0 else []
        if g.geom_type in ('MultiPolygon', 'GeometryCollection'):
            out = []
            for child in g.geoms:
                out.extend(parts_of(child))
            return out
        return []

    parts = []
    for ring in rings:
        if ring is None or len(ring) < 3:
            continue
        p = Polygon(ring)
        if not p.is_valid:
            p = make_valid(p)
        for poly in parts_of(p):
            if poly.area > 1e-12:
                parts.append(poly)
    if not parts:
        return []
    # Nesting uses each part's own boundary vertex: an interior
    # representative point of an outer ring can fall inside its hole and
    # invert the parity.
    reps = [Point_(p.exterior.coords[0]) for p in parts]
    depth = [sum(1 for j, q in enumerate(parts) if j != i and q.contains(reps[i]))
             for i in range(len(parts))]
    signs = [1 if _ring_signed_area(p.exterior.coords) > 0 else -1 for p in parts]
    parent = []
    for j in range(len(parts)):
        candidates = [i for i in range(len(parts)) if i != j and parts[i].contains(reps[j])]
        parent.append(min(candidates, key=lambda i: parts[i].area) if candidates else None)
    solids = []
    for i, p in enumerate(parts):
        # cumulative winding along the ancestor chain INCLUDING this ring
        wind = signs[i]
        k = parent[i]
        guard = 0
        while k is not None and guard < len(parts):
            wind += signs[k]
            k = parent[k]
            guard += 1
        if rule == 'nonzero':
            solid_here = wind != 0
        else:
            solid_here = depth[i] % 2 == 0
        if not solid_here:
            continue
        holes = []
        for j in range(len(parts)):
            if parent[j] != i:
                continue
            if rule == 'nonzero':
                child_wind = signs[j]
                k2 = parent[j]
                guard = 0
                while k2 is not None and guard < len(parts):
                    child_wind += signs[k2]
                    k2 = parent[k2]
                    guard += 1
                if child_wind == 0:
                    holes.append(parts[j].exterior)
            elif depth[j] % 2 == 1:
                holes.append(parts[j].exterior)
        solid = Polygon(p.exterior, holes) if holes else Polygon(p.exterior)
        if not solid.is_valid:
            solid = make_valid(solid)
        for piece in parts_of(solid):
            if piece.area > 1e-9:
                solids.append(piece)
    return solids


def Point_(coord):
    from shapely.geometry import Point as _ShapelyPoint
    return _ShapelyPoint(coord[0], coord[1])


def point_in_commands(cmds: Sequence[Command], x: float, y: float, tol: float = 0.25) -> bool:
    return point_in_rings(flatten_path(cmds, tol), x, y)


def _ring_signed_area(ring: Sequence[Point]) -> float:
    s = 0.0
    n = len(ring)
    for k in range(n):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def evenodd_area(rings: Sequence[Sequence[Point]]) -> float:
    """Area under the even-odd fill rule (holes subtract)."""
    total = 0.0
    for idx, ring in enumerate(rings):
        depth = sum(1 for jdx, other in enumerate(rings)
                    if jdx != idx and point_in_ring(other, ring[0][0], ring[0][1]))
        a = abs(_ring_signed_area(ring))
        total += a if depth % 2 == 0 else -a
    return total


def snap_ring(ring: Sequence[Point]) -> List[Point]:
    """Snap a ring to the integer pixel-edge grid (legacy simulation)."""
    out: List[Point] = []
    for x, y in ring:
        p = (float(round(x)), float(round(y)))
        if not out or out[-1] != p:
            out.append(p)
    while len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def max_deviation(source_pts: Sequence[Point], rings: Sequence[Sequence[Point]]) -> float:
    """Max distance from source polyline vertices to a flattened ring set."""
    segs: List[Tuple[Point, Point]] = []
    for ring in rings:
        for k in range(len(ring)):
            segs.append((ring[k], ring[(k + 1) % len(ring)]))
    if not segs:
        return float('inf')
    best = 0.0
    for p in source_pts:
        d = min(_perp_dist(p, a, b) if a != b else math.dist(p, a) for a, b in segs)
        best = max(best, d)
    return best


# ---------------------------------------------------------------------------
# Simplification, corner detection, Schneider fitting
# ---------------------------------------------------------------------------

def rdp(points: Sequence[Point], tol: float) -> List[Point]:
    """Ramer–Douglas–Peucker simplification (iterative)."""
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for k in range(a + 1, b):
            px, py = points[k]
            if norm:
                d = abs(dx * (ay - py) - dy * (ax - px)) / norm
            else:
                d = math.hypot(px - ax, py - ay)
            if d > best:
                best, bi = d, k
        if best > tol:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(points, keep) if k]


def corner_indices(pts: Sequence[Point], window: int = 3, cos_limit: float = 0.5) -> List[int]:
    """Indices of deliberate corners using a windowed turning angle.

    Staircase quantization averages out over the window; genuine architectural
    corners persist, so straight edges and sharp junctions survive fitting.
    The window shrinks on short polylines so it never spans more than a third
    of the chain (which would average away real corners).
    """
    n = len(pts)
    if n <= 2:
        return list(range(n))
    window = max(1, min(window, (n - 1) // 3))
    raw: List[Tuple[int, float]] = []  # (index, turn magnitude)
    for i in range(n):
        a = pts[max(0, i - window)]
        b = pts[min(n - 1, i + window)]
        v1 = (pts[i][0] - a[0], pts[i][1] - a[1])
        v2 = (b[0] - pts[i][0], b[1] - pts[i][1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-9 or l2 < 1e-9:
            raw.append((i, math.pi))
            continue
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        if cosang < cos_limit:
            raw.append((i, math.acos(max(-1.0, min(1.0, cosang)))))
    # merge only immediately adjacent corner indices (staircase remnants)
    merged: List[int] = []
    cluster: List[Tuple[int, float]] = []
    for idx, turn in raw:
        if cluster and idx - cluster[-1][0] > 1:
            merged.append(max(cluster, key=lambda t: t[1])[0])
            cluster = []
        cluster.append((idx, turn))
    if cluster:
        merged.append(max(cluster, key=lambda t: t[1])[0])
    if not merged:
        merged = [0, n - 1]
    if 0 not in merged:
        merged.insert(0, 0)
    if n - 1 not in merged:
        merged.append(n - 1)
    return sorted(set(merged))


def _chord_params(pts: Sequence[Point]) -> List[float]:
    total = 0.0
    ts = [0.0]
    for k in range(1, len(pts)):
        total += math.dist(pts[k - 1], pts[k])
        ts.append(total)
    if total <= 0:
        return [k / (len(pts) - 1) for k in range(len(pts))]
    return [t / total for t in ts]


def _normalize(v: Point) -> Point:
    n = math.hypot(*v)
    if n < 1e-12:
        return (0.0, 0.0)
    return (v[0] / n, v[1] / n)


def _end_tangent(pts: Sequence[Point], at_end: bool, min_dist: float = 2.0) -> Point:
    """Robust end tangent: walk along the polyline until min_dist is covered.

    Distance-weighted 3-point tangents let a 1px staircase jog dominate the
    direction; walking a fixed geometric distance suppresses that.
    """
    n = len(pts)
    if n < 2:
        return (1.0, 0.0)
    if at_end:
        i = n - 1
        j = i - 1
        acc = 0.0
        while j > 0:
            acc += math.dist(pts[j + 1], pts[j])
            if acc >= min_dist:
                break
            j -= 1
        v = (pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
    else:
        j = 1
        acc = 0.0
        while j < n - 1:
            acc += math.dist(pts[j - 1], pts[j])
            if acc >= min_dist:
                break
            j += 1
        v = (pts[j][0] - pts[0][0], pts[j][1] - pts[0][1])
    nt = _normalize(v)
    if nt == (0.0, 0.0):
        nt = _normalize((pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]))
    if nt == (0.0, 0.0):
        nt = (1.0, 0.0)
    return nt


def _bezier_point(p0: Point, c1: Point, c2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    b0 = u * u * u
    b1 = 3 * t * u * u
    b2 = 3 * t * t * u
    b3 = t * t * t
    return (b0 * p0[0] + b1 * c1[0] + b2 * c2[0] + b3 * p3[0],
            b0 * p0[1] + b1 * c1[1] + b2 * c2[1] + b3 * p3[1])


def _bezier_derivative(p0: Point, c1: Point, c2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    return (3 * u * u * (c1[0] - p0[0]) + 6 * t * u * (c2[0] - c1[0]) + 3 * t * t * (p3[0] - c2[0]),
            3 * u * u * (c1[1] - p0[1]) + 6 * t * u * (c2[1] - c1[1]) + 3 * t * t * (p3[1] - c2[1]))


def _fit_cubic_ls(pts: Sequence[Point], ts: Sequence[float],
                  t1: Point, t2: Point) -> Tuple[Point, Point, Point, Point]:
    """Least-squares cubic through endpoints with given end tangents (Schneider)."""
    p0, p3 = pts[0], pts[-1]
    # normal equations for (alpha1, alpha2): c1 = p0 + a1*T1, c2 = p3 - a2*T2
    a11 = a12 = a22 = 0.0
    b1 = b2 = 0.0
    for p, t in zip(pts, ts):
        u = 1.0 - t
        g1 = 3 * t * u * u      # coefficient of alpha1 (times T1)
        g2 = -3 * t * t * u     # coefficient of alpha2 (times T2)
        f0 = u * u * u + 3 * t * u * u
        f3 = 3 * t * t * u + t * t * t
        rhs_x = p[0] - p0[0] * f0 - p3[0] * f3
        rhs_y = p[1] - p0[1] * f0 - p3[1] * f3
        # rows: [g1*T1x, g2*T2x] and [g1*T1y, g2*T2y]
        a11 += g1 * g1 * (t1[0] ** 2 + t1[1] ** 2)
        a12 += g1 * g2 * (t1[0] * t2[0] + t1[1] * t2[1])
        a22 += g2 * g2 * (t2[0] ** 2 + t2[1] ** 2)
        b1 += g1 * (rhs_x * t1[0] + rhs_y * t1[1])
        b2 += g2 * (rhs_x * t2[0] + rhs_y * t2[1])
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-12:
        alpha = math.dist(p0, p3) / 3.0
        a1 = a2 = max(alpha, 1e-6)
    else:
        a1 = (a22 * b1 - a12 * b2) / det
        a2 = (a11 * b2 - a12 * b1) / det
        if a1 < 0 or a2 < 0 or not math.isfinite(a1) or not math.isfinite(a2):
            alpha = math.dist(p0, p3) / 3.0
            a1 = a2 = max(alpha, 1e-6)
    c1 = (p0[0] + a1 * t1[0], p0[1] + a1 * t1[1])
    c2 = (p3[0] - a2 * t2[0], p3[1] - a2 * t2[1])
    return p0, c1, c2, p3


def _max_error(pts: Sequence[Point], ts: Sequence[float], bez) -> Tuple[float, int]:
    p0, c1, c2, p3 = bez
    worst, wi = 0.0, 0
    for idx, (p, t) in enumerate(zip(pts, ts)):
        bp = _bezier_point(p0, c1, c2, p3, t)
        d = (p[0] - bp[0]) ** 2 + (p[1] - bp[1]) ** 2
        if d > worst:
            worst, wi = d, idx
    return math.sqrt(worst), wi


def _reparameterize(pts: Sequence[Point], ts: List[float], bez) -> List[float]:
    p0, c1, c2, p3 = bez
    out: List[float] = []
    for p, t in zip(pts, ts):
        for _ in range(4):
            d = _bezier_derivative(p0, c1, c2, p3, t)
            bp = _bezier_point(p0, c1, c2, p3, t)
            den = d[0] * d[0] + d[1] * d[1]
            if den < 1e-12:
                break
            num = d[0] * (bp[0] - p[0]) + d[1] * (bp[1] - p[1])
            t -= (num / den) * 1.0
        out.append(min(1.0, max(0.0, t)))
    return out


def _hull_within(pts: Sequence[Point], bez, tol: float) -> bool:
    """Reject degenerate parameterizations: control points must stay near data."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    margin = 2.0 * tol
    for cx, cy in (bez[1], bez[2]):
        if not (x0 - margin <= cx <= x1 + margin and y0 - margin <= cy <= y1 + margin):
            return False
    return True


def _fit_cubic_span(pts: List[Point], tol: float, t1: Point, t2: Point,
                    depth: int = 0) -> List[Command]:
    """Fit one open polyline span with cubic(s); interpolates both endpoints."""
    if len(pts) == 2:
        return [('L', pts[-1][0], pts[-1][1])]
    # already straight? emit a single line (architectural edges)
    chord = (pts[0], pts[-1])
    if max(_perp_dist(p, *chord) for p in pts) < 0.08:
        return [('L', pts[-1][0], pts[-1][1])]
    ts = _chord_params(pts)
    bez = _fit_cubic_ls(pts, ts, t1, t2)
    err, wi = _max_error(pts, ts, bez)
    if err > tol or not _hull_within(pts, bez, tol):
        ts = _reparameterize(pts, ts, bez)
        bez = _fit_cubic_ls(pts, ts, t1, t2)
        err, wi = _max_error(pts, ts, bez)
    if err <= tol and _hull_within(pts, bez, tol):
        return [('C', bez[1][0], bez[1][1], bez[2][0], bez[2][1], bez[3][0], bez[3][1])]
    if depth >= 14 or len(pts) <= 3:
        # cannot curve-fit: fall back to exact polyline segments
        return [('L', p[0], p[1]) for p in pts[1:]]
    if not (0 < wi < len(pts) - 1):
        wi = len(pts) // 2
    left = pts[:wi + 1]
    right = pts[wi:]
    mid_tangent = _normalize((pts[min(len(pts) - 1, wi + 1)][0] - pts[max(0, wi - 1)][0],
                              pts[min(len(pts) - 1, wi + 1)][1] - pts[max(0, wi - 1)][1]))
    if mid_tangent == (0.0, 0.0):
        mid_tangent = t2
    return (_fit_cubic_span(left, tol, t1, mid_tangent, depth + 1)
            + _fit_cubic_span(right, tol, mid_tangent, t2, depth + 1))


def _fit_span(pts: List[Point], tol: float) -> List[Command]:
    """Fit one span (corners at both ends, endpoints interpolated exactly)."""
    if len(pts) == 2:
        return [('L', pts[-1][0], pts[-1][1])]
    t1 = _end_tangent(pts, at_end=False)
    t2 = _end_tangent(pts, at_end=True)
    return _fit_cubic_span(pts, tol, t1, t2)


def fit_polyline(pts: Sequence[Point], fit_tol: float, corner_cos: float = 0.5,
                 window: int = 3) -> List[Command]:
    """Fit an OPEN polyline (a shared boundary chain) with corner preservation.

    Returns commands starting with M. The endpoints are interpolated exactly,
    which keeps the assembled region partition watertight.
    """
    if len(pts) < 2:
        raise ValueError('A chain needs at least two points.')
    simp = rdp(pts, min(0.5, fit_tol))
    if len(simp) < 2:
        simp = [pts[0], pts[-1]]
    cmds: List[Command] = [('M', simp[0][0], simp[0][1])]
    corners = corner_indices(simp, window=window, cos_limit=corner_cos)
    for a, b in zip(corners, corners[1:]):
        span = simp[a:b + 1]
        cmds.extend(_fit_span(span, fit_tol))
    return cmds


def fit_ring(pts: Sequence[Point], fit_tol: float, corner_cos: float = 0.5,
             window: int = 3) -> List[Command]:
    """Fit a CLOSED ring (merge refits) — returns M ... Z commands."""
    if len(pts) < 3:
        raise ValueError('A ring needs at least three points.')
    ring = list(pts)
    if ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        raise ValueError('Degenerate ring.')
    # start at the vertex farthest from the centroid (stable, away from seams)
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    start = max(range(len(ring)), key=lambda k: (ring[k][0] - cx) ** 2 + (ring[k][1] - cy) ** 2)
    rot = ring[start:] + ring[:start]
    simp = rdp(rot, min(0.5, fit_tol))
    if len(simp) < 3:
        return [('M', ring[0][0], ring[0][1]), ('L', ring[1][0], ring[1][1]), ('Z',)]
    # cyclic corner detection
    n = len(simp)
    corners: List[int] = []
    for i in range(n):
        a = simp[(i - window) % n]
        b = simp[(i + window) % n]
        v1 = (simp[i][0] - a[0], simp[i][1] - a[1])
        v2 = (b[0] - simp[i][0], b[1] - simp[i][1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-9 or l2 < 1e-9:
            corners.append(i)
            continue
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        if cosang < corner_cos:
            corners.append(i)
    if not corners:
        corners = [0]
    # spans between consecutive corners, wrapping around
    cmds: List[Command] = [('M', simp[corners[0]][0], simp[corners[0]][1])]
    ordered = sorted(set(corners + [0]))
    for a, b in zip(ordered, ordered[1:]):
        span = simp[a:b + 1]
        if len(span) >= 2:
            cmds.extend(_fit_span(span, fit_tol))
    wrap = simp[ordered[-1]:] + [simp[ordered[0]]]
    if len(wrap) >= 2:
        cmds.extend(_fit_span(wrap, fit_tol))
    cmds.append(('Z',))
    return cmds


# ---------------------------------------------------------------------------
# Reversal (shared chains are reused in both traversal directions)
# ---------------------------------------------------------------------------

def reverse_commands(cmds: Sequence[Command]) -> List[Command]:
    """Reverse an open command chain (M + L/C/Q...). Endpoints preserved."""
    if not cmds or cmds[0][0] != 'M':
        raise ValueError('Chain must start with M.')
    if any(cmd[0] == 'M' for cmd in cmds[1:]):
        raise ValueError('Chain reversal expects a single subpath.')
    if cmds[-1][0] == 'Z':
        raise ValueError('Chain reversal expects an open path.')
    cur = (cmds[0][1], cmds[0][2])
    segments: List[Tuple[Point, Command, Point]] = []
    for cmd in cmds[1:]:
        if cmd[0] == 'L':
            end = (cmd[1], cmd[2])
        elif cmd[0] == 'C':
            end = (cmd[5], cmd[6])
        elif cmd[0] == 'Q':
            end = (cmd[3], cmd[4])
        else:
            raise ValueError(f'Cannot reverse command {cmd[0]!r}.')
        segments.append((cur, cmd, end))
        cur = end
    if not segments:
        return list(cmds)
    out: List[Command] = [('M', segments[-1][2][0], segments[-1][2][1])]
    for start, cmd, end in reversed(segments):
        if cmd[0] == 'L':
            out.append(('L', start[0], start[1]))
        elif cmd[0] == 'C':
            out.append(('C', cmd[3], cmd[4], cmd[1], cmd[2], start[0], start[1]))
        elif cmd[0] == 'Q':
            out.append(('Q', cmd[1], cmd[2], start[0], start[1]))
    return out


# ---------------------------------------------------------------------------
# Affine transforms
# ---------------------------------------------------------------------------

Mat = Tuple[float, float, float, float, float, float]  # a b c d e f


def mat_identity() -> Mat:
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m1: Mat, m2: Mat) -> Mat:
    """Return m1 ∘ m2 (apply m2 first, then m1)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2,
            b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2,
            b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1,
            b1 * e2 + d1 * f2 + f1)


def mat_apply(m: Mat, p: Point) -> Point:
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def parse_transform(text: str | None) -> Mat:
    """Parse an SVG transform attribute into a single matrix."""
    import re as _re

    m = mat_identity()
    if not text:
        return m
    for match in _re.finditer(r'([a-zA-Z]+)\s*\(([^)]*)\)', text):
        name = match.group(1).lower()
        raw = match.group(2)
        args = [float(v) for v in _re.findall(r'[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?', raw)]
        if not args and name not in ('translate', 'scale'):
            raise ValueError(f'Unsupported transform: {name}.')
        if name == 'translate':
            tx = args[0] if len(args) > 0 else 0.0
            ty = args[1] if len(args) > 1 else 0.0
            step = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif name == 'scale':
            sx = args[0] if len(args) > 0 else 1.0
            sy = args[1] if len(args) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == 'rotate':
            angle = math.radians(args[0]) if args else 0.0
            ca, sa = math.cos(angle), math.sin(angle)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                step = mat_mul(mat_mul((1.0, 0.0, 0.0, 1.0, cx, cy),
                                       (ca, sa, -sa, ca, 0.0, 0.0)),
                               (1.0, 0.0, 0.0, 1.0, -cx, -cy))
            else:
                step = (ca, sa, -sa, ca, 0.0, 0.0)
        elif name == 'skewx':
            t = math.tan(math.radians(args[0] if args else 0.0))
            step = (1.0, 0.0, t, 1.0, 0.0, 0.0)
        elif name == 'skewy':
            t = math.tan(math.radians(args[0] if args else 0.0))
            step = (1.0, t, 0.0, 1.0, 0.0, 0.0)
        elif name == 'matrix' and len(args) == 6:
            step = tuple(args)  # type: ignore[assignment]
        else:
            raise ValueError(f'Unsupported transform: {name}.')
        m = mat_mul(m, step)
    return m


def transform_commands(cmds: Sequence[Command], m: Mat) -> List[Command]:
    out: List[Command] = []
    for cmd in cmds:
        tag = cmd[0]
        if tag == 'Z':
            out.append(('Z',))
        elif tag in ('M', 'L'):
            p = mat_apply(m, (cmd[1], cmd[2]))
            out.append((tag, p[0], p[1]))
        elif tag == 'C':
            c1 = mat_apply(m, (cmd[1], cmd[2]))
            c2 = mat_apply(m, (cmd[3], cmd[4]))
            p = mat_apply(m, (cmd[5], cmd[6]))
            out.append(('C', c1[0], c1[1], c2[0], c2[1], p[0], p[1]))
        elif tag == 'Q':
            q = mat_apply(m, (cmd[1], cmd[2]))
            p = mat_apply(m, (cmd[3], cmd[4]))
            out.append(('Q', q[0], q[1], p[0], p[1]))
        else:
            raise ValueError(f'Cannot transform command {tag!r}.')
    return out
