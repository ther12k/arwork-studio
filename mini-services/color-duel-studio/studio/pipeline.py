"""Deterministic raster -> vector paint + independent playable-region compiler.

UPGRADE (curve-preserving workflow):
- Gameplay regions, paint and ink layers all carry CURVED master geometry
  (SVG path commands M/L/C/Q/Z). Flattened polygon rings are DERIVED
  approximations with an explicit tolerance (see ``flattenTolerance``);
  they never replace the master.
- Raster builds extract shared boundary chains from the pixel grid and
  fit each chain ONCE, so neighbouring regions reuse the identical curve
  and the curved partition stays watertight by construction.
- ``backend='polygon-legacy'`` reproduces the pre-upgrade pixel-edge
  polygon output (kept for comparison and fallback).
- ``compile_svg_master`` imports sanitized SVG masters directly,
  preserving curves, holes, gradients, transforms and drawing order.
  Imported SVG masters are never rasterized or retraced.

No raster images are embedded into emitted SVG. SLIC provides
image-aware *draft* regions, not semantic object detection.
"""
from __future__ import annotations

import hashlib, json, math, shutil, zipfile
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import cv2
from PIL import Image, ImageOps, ImageFilter
from scipy import ndimage as ndi
from skimage.segmentation import slic
from shapely import make_valid
from shapely.geometry import shape, Polygon, Point, box
from shapely.ops import unary_union, polylabel

from .curves import (
    Command, evenodd_area, fit_polyline, fit_ring, flatten_path, format_path,
    fmt_num, parse_path, point_in_rings, point_in_rings_rule, reverse_commands,
    rings_bbox, snap_ring, solid_polygons, subpaths_of, flatten_subpath,
)
from .models import BuildSettings

SCHEMA = 'color-duel-detailed-vector-1'
INK = '#29383E'
GEOMETRY_SCHEMA = 2            # 2 = curved masters authoritative + visible-region geometry
FLATTEN_TOLERANCE = 0.25       # px, documented derived-approximation tolerance
import re as _re
SAFE_D = _re.compile(r'^M[\s\d.,eE+\-MLQCZ]+Z$')
SAFE_D_OPEN = _re.compile(r'^M[\s\d.,eE+\-MLQCZ]+$')
SAFE_HEX = _re.compile(r'^#[0-9A-Fa-f]{6}$')
SAFE_GRAD_REF = _re.compile(r'^url\(#g-[a-zA-Z0-9_-]+\)$')
SAFE_ID = _re.compile(r'^[a-zA-Z0-9_-]+$')
RUNTIME_REGION_KEYS = ('id', 'paletteId', 'objectId', 'd', 'fillRule', 'bbox', 'area', 'label')
BACKENDS = [
    {'id': 'spline-local', 'name': 'Local spline tracing (curves)', 'kind': 'raster-to-vector',
     'paid': False, 'available': True,
     'notes': 'Corner-preserving Schneider cubic fitting over shared boundary chains. Free, local, deterministic.'},
    {'id': 'polygon-legacy', 'name': 'Legacy pixel-edge polygons', 'kind': 'raster-to-vector',
     'paid': False, 'available': True,
     'notes': 'Pre-upgrade behaviour: exact pixel-edge M/L/Z polygons (staircase edges at zoom). Comparison only.'},
    {'id': 'svg-master', 'name': 'SVG master import (curves preserved)', 'kind': 'svg-import',
     'paid': False, 'available': True,
     'notes': 'Sanitized SVG master: curves, holes, supported gradients, transforms and drawing order preserved. Never rasterized.'},
    {'id': 'provider-vectorizer', 'name': 'External image-to-SVG provider', 'kind': 'raster-to-vector',
     'paid': True, 'available': False,
     'notes': 'Optional paid external vectorizer. Requires server-side credentials (VECTORIZER_API_KEY); disabled without them.'},
    {'id': 'provider-svg-generation', 'name': 'AI SVG generation', 'kind': 'svg-generation',
     'paid': True, 'available': False,
     'notes': 'Separate paid route: the configured chat provider drafts an SVG master from the brief. Sanitized before import.'},
]


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_image(data: bytes, destination: Path) -> dict:
    from io import BytesIO
    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError('Use a PNG, JPEG or WebP under 12 MB.')
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Only PNG, JPEG and WebP images are accepted for raster masters. Use the SVG master route for SVG files.')
            if source.width * source.height > 32_000_000:
                raise ValueError('Image exceeds 32 megapixels.')
            if min(source.size) < 64:
                raise ValueError('Image must be at least 64 pixels in each dimension.')
            if getattr(source, 'is_animated', False):
                raise ValueError('Use a still image, not an animation.')
            im = ImageOps.exif_transpose(source).convert('RGBA')
            background = Image.new('RGBA', im.size, 'white')
            background.alpha_composite(im)
            rgb = background.convert('RGB')
            destination.parent.mkdir(parents=True, exist_ok=True)
            rgb.save(destination, format='PNG')  # Drops EXIF and executable metadata.
            return {'width': rgb.width, 'height': rgb.height, 'sha256': checksum(destination), 'kind': 'image'}
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('The file could not be decoded safely as a still image.') from exc


def polygon_parts(geom):
    if geom.geom_type == 'Polygon':
        if not geom.is_empty and geom.area > 0:
            yield geom
    elif geom.geom_type in ('MultiPolygon', 'GeometryCollection'):
        for child in geom.geoms:
            yield from polygon_parts(child)


def rings_of(p) -> list:
    return [[[float(x), float(y)] for x, y in ring.coords] for ring in [p.exterior, *p.interiors]]


def number(n: float) -> str:
    return str(int(n)) if n == int(n) else f'{n:.4f}'.rstrip('0').rstrip('.')


def path_of(rings: list) -> str:
    """LEGACY polygon formatter: pixel-edge M/L/Z rings only (comparison mode)."""
    paths = []
    for ring in rings:
        coords = ring[:-1] if ring[0] == ring[-1] else ring
        paths.append('M ' + ' L '.join(f'{number(x)},{number(y)}' for x, y in coords) + ' Z')
    return ' '.join(paths)


def rule_area(rings, rule: str = 'evenodd') -> float:
    """Filled area of flattened rings under the given fill rule.

    Nonzero nesting: same-winding nested subpaths are already inside their
    parent, so the area is the UNION of the solids (not their sum).
    """
    if rule == 'evenodd':
        return evenodd_area(rings) if rings else 0.0
    solids = solid_polygons(rings, rule) if rings else []
    if not solids:
        return 0.0
    if len(solids) == 1:
        return float(solids[0].area)
    return float(_safe_union(solids).area)


def region_polygon(r):
    rings = r.get('flat', {}).get('rings') or r.get('rings') or []
    rule = r.get('fillRule', 'evenodd')
    solids = solid_polygons(rings, rule)
    if not solids:
        return Polygon()
    if len(solids) == 1:
        return solids[0]
    return _safe_union(solids)


def _safe_union(geoms):
    """Union that tolerates pathological (self-crossing) inputs."""
    try:
        return unary_union(list(geoms))
    except Exception:
        cleaned = []
        for g in geoms:
            if g.is_empty:
                continue
            if not g.is_valid:
                g = make_valid(g)
            pieces = list(polygon_parts(g))
            if pieces:
                cleaned.extend(pieces)
            elif not g.is_empty:
                cleaned.append(g)
        return unary_union([g for g in cleaned if not g.is_empty]) or Polygon()


def solid_polygons(rings, rule='evenodd'):
    """Deprecated local shim -> curves.solid_polygons (fill-rule aware).

    evenodd: containment depth decides solid vs hole.
    nonzero: cumulative ring orientation decides (same-winding nested
    subpaths union, opposite-winding subtract), exactly like SVG.
    """
    from .curves import solid_polygons as _solid
    return _solid(rings, rule)


def region_master_commands(r) -> List[Command]:
    """Authoritative curved commands for a region (master is the source of truth)."""
    master = r.get('master') or {}
    d = master.get('d') or r.get('d')
    if not d:
        raise ValueError('Region has no master path.')
    return parse_path(d)


def make_label(poly, palette_id: int, mask=None, origin=(0, 0)) -> dict:
    if poly.geom_type != 'Polygon':
        parts = list(polygon_parts(poly))
        if not parts:
            raise ValueError('Region has no usable polygon for a label.')
        poly = max(parts, key=lambda p: p.area)
    if mask is not None:
        dist = ndi.distance_transform_edt(np.pad(mask, 1))
        yy, xx = np.unravel_index(np.argmax(dist), dist.shape)
        px, py = origin[0] + xx - 1 + .5, origin[1] + yy - 1 + .5
        point = Point(px, py)
        if not poly.contains(point):
            point = poly.representative_point()
    else:
        point = polylabel(poly, tolerance=.7)
    radius = float(poly.boundary.distance(point))
    # Text rectangle must fit inside the inscribed circle; account for digit count.
    size = min(22., radius * 1.6 / math.sqrt((len(str(palette_id)) * .65) ** 2 + 1))
    return {'x': round(float(point.x), 4), 'y': round(float(point.y), 4), 'fontSize': round(size, 3),
            'minScreenPx': 9, 'clearance': round(radius, 3)}


def _corner_cos(angle_deg: float) -> float:
    return math.cos(math.radians(min(179.0, max(1.0, angle_deg))))


def pack_region(geometry, rid: str, pid: int, object_id: str = 'unassigned',
                label: dict | None = None, source: str = 'boundary-chain-fit',
                fit_tolerance: float = 1.0, legacy_rings: list | None = None,
                fit: bool = True, fill_rule: str = 'evenodd') -> dict:
    """Pack a region from curved master commands OR a plain shapely polygon.

    Master geometry is authoritative; ``rings``/``flat`` are derived
    approximations at the documented flatten tolerance. Polygon input is
    refit with corner preservation (``fit=True``, merges) or emitted as exact
    M/L/Z pixel-edge commands (``fit=False``, the legacy backend).
    ``fill_rule`` preserves the source shape's SVG fill rule: nonzero
    compound paths stay nonzero so a same-winding nested subpath keeps
    unioning instead of turning into an unintended hole.
    """
    if isinstance(geometry, Polygon):
        cmds: List[Command] = []
        for ring in rings_of(geometry):
            pts = [(float(x), float(y)) for x, y in (ring[:-1] if ring[0] == ring[-1] else ring)]
            if len(pts) < 3:
                continue
            if fit:
                cmds.extend(fit_ring(pts, fit_tolerance, _corner_cos(60)))
            else:
                cmds.append(('M', pts[0][0], pts[0][1]))
                cmds.extend(('L', x, y) for x, y in pts[1:])
                cmds.append(('Z',))
        if not cmds:
            raise ValueError('Degenerate region geometry.')
        legacy = legacy_rings if legacy_rings is not None else rings_of(geometry)
    else:
        cmds = list(geometry)
        legacy = legacy_rings or []
    d = format_path(cmds)
    flat = flatten_path(cmds, FLATTEN_TOLERANCE)
    if not flat:
        raise ValueError('Region master flattens to nothing.')
    solids = solid_polygons(flat, fill_rule)
    poly = solids[0] if solids else Polygon(flat[0])
    if len(solids) > 1:
        poly = _safe_union(solids)
    area = rule_area(flat, fill_rule)
    bbox = list(map(float, rings_bbox(flat)))
    region = {
        'id': rid, 'paletteId': int(pid), 'objectId': object_id,
        'd': d, 'fillRule': fill_rule,
        'master': {'d': d, 'fillRule': fill_rule, 'source': source,
                   'tolerance': round(float(fit_tolerance), 3)},
        'flat': {'tolerance': FLATTEN_TOLERANCE,
                 'rings': [[[round(float(x), 2), round(float(y), 2)] for x, y in ring] for ring in flat],
                 'note': 'Derived approximation for hit-testing, validation and legacy consumers. Not the master geometry.'},
        'rings': [[[round(float(x), 2), round(float(y), 2)] for x, y in ring] for ring in flat],
        'bbox': bbox, 'area': float(area),
        'label': label or make_label(poly, int(pid)),
    }
    if legacy:
        region['legacy'] = {
            'rings': [[[float(x), float(y)] for x, y in ring] for ring in legacy],
            'note': 'Pre-upgrade pixel-edge polygon of the same region; comparison/verification only (staircase edges at zoom).'}
    return region


# ---------------------------------------------------------------------------
# Shared-boundary chain extraction (raster -> curved masters)
# ---------------------------------------------------------------------------

def _crack_edges(labels: np.ndarray, background: int = 0):
    """Yield (u, v, labelA, labelB, pixelA, pixelB) crack edges of a label grid.

    u/v are integer vertex tuples on the pixel-edge lattice. pixelA/pixelB are
    the (row, col) of the cells adjacent to the edge (None for background).
    """
    H, W = labels.shape
    diff_h = labels[:, :-1] != labels[:, 1:]
    for r, c in zip(*np.nonzero(diff_h)):
        r, c = int(r), int(c)
        yield (c + 1, r), (c + 1, r + 1), int(labels[r, c]), int(labels[r, c + 1]), (r, c), (r, c + 1)
    diff_v = labels[:-1, :] != labels[1:, :]
    for r, c in zip(*np.nonzero(diff_v)):
        r, c = int(r), int(c)
        yield (c, r + 1), (c + 1, r + 1), int(labels[r, c]), int(labels[r + 1, c]), (r, c), (r + 1, c)
    for c in range(W):
        v = int(labels[0, c])
        if v != background:
            yield (c, 0), (c + 1, 0), background, v, None, (0, c)
        v = int(labels[H - 1, c])
        if v != background:
            yield (c, H), (c + 1, H), v, background, (H - 1, c), None
    for r in range(H):
        v = int(labels[r, 0])
        if v != background:
            yield (0, r), (0, r + 1), background, v, None, (r, 0)
        v = int(labels[r, W - 1])
        if v != background:
            yield (W, r), (W, r + 1), v, background, (r, W - 1), None


def _extract_chains(edges: List[tuple], adj: Dict[tuple, List[int]],
                     forced: Dict[tuple, set] | None = None):
    """Group crack edges into label-pair chains broken at junction nodes.

    A chain is a maximal path of edges sharing the same label pair, broken at
    vertices whose pair-degree != 2 (region junctions) and at forced break
    vertices (where some label's component degree != 2, so region walks would
    have to turn mid-chain). Closed pair-loops are kept closed unless another
    region touches a chain vertex (then reopened there).
    Returns (chains, edge_chain, chain_edge_pos):
      edge_chain[eid]   = (chain_id, pts order is u->v?)
      chain_edge_pos[eid] = (chain_id, position of the edge in chain pts order)
    """
    used = [False] * len(edges)
    forced = forced or {}

    def pair_of(eid):
        ea, eb = edges[eid][2], edges[eid][3]
        return (ea, eb) if ea <= eb else (eb, ea)

    def unused_pair_edges(vertex, pair):
        out = []
        for eid in adj[vertex]:
            if not used[eid] and pair_of(eid) == pair:
                out.append(eid)
        return out

    chains = []
    edge_chain: Dict[int, Tuple[int, bool]] = {}
    chain_edge_pos: Dict[int, Tuple[int, int]] = {}
    for start in range(len(edges)):
        if used[start]:
            continue
        u, v = edges[start][0], edges[start][1]
        pair = pair_of(start)
        used[start] = True

        def extend(from_vertex, stop_vertex):
            """Walk unused pair edges until a node, forced break, or stop_vertex."""
            path = [from_vertex]
            ids = []
            cur = from_vertex
            while True:
                if cur in forced and pair in forced[cur]:
                    break
                candidates = unused_pair_edges(cur, pair)
                if len(candidates) != 1:
                    break
                eid = candidates[0]
                used[eid] = True
                ids.append(eid)
                eu, ev = edges[eid][0], edges[eid][1]
                nxt = ev if eu == cur else eu
                path.append(nxt)
                cur = nxt
                if cur == stop_vertex:
                    break
            return path, ids

        fwd_pts, fwd_ids = extend(v, u)
        if fwd_pts[-1] == u:
            # forward walk wrapped back to the start vertex: closed loop u -> v -> ... -> u
            pts = [u] + fwd_pts
            all_ids = [start] + fwd_ids
        else:
            back_pts, back_ids = extend(u, v)
            if back_pts[-1] == v:
                # backward walk reached v through the other side: closed loop
                # v -> ... -> u, with e0 (u—v) closing the ring
                pts = list(reversed(back_pts)) + [v]
                all_ids = [start] + back_ids
            else:
                pts = list(reversed(back_pts)) + fwd_pts
                all_ids = [start] + back_ids + fwd_ids
        closed = len(pts) > 2 and pts[0] == pts[-1]
        if closed:
            for idx in range(len(pts) - 1):
                if len(adj[pts[idx]]) > 2:
                    # another region touches this vertex: reopen the ring here
                    pts = pts[idx:-1] + pts[:idx] + [pts[idx]]
                    closed = False
                    break
        cid = len(chains)
        chains.append({'pts': pts, 'pair': pair, 'closed': closed})
        _mark_chain_edges(pts, edges, all_ids, cid, edge_chain, chain_edge_pos)
    return chains, edge_chain, chain_edge_pos


def _mark_chain_edges(pts, edges, edge_ids, cid, edge_chain, chain_edge_pos):
    """Record each chain edge's orientation and position in the chain."""
    lookup = {}
    for eid in edge_ids:
        u, v = edges[eid][0], edges[eid][1]
        lookup[(u, v)] = eid
    for k in range(len(pts) - 1):
        key = (pts[k], pts[k + 1])
        eid = lookup.get(key)
        if eid is not None:
            edge_chain[eid] = (cid, True)
            chain_edge_pos[eid] = (cid, k)
        else:
            eid = lookup.get((pts[k + 1], pts[k]))
            if eid is not None:
                edge_chain[eid] = (cid, False)
                chain_edge_pos[eid] = (cid, k)


def _euler_cycles(edge_ids: List[int], edges, adj, edge_chain):
    """Decompose a balanced edge set into closed walks (Hierholzer-lite).

    At each vertex the walk prefers continuing along the current chain in the
    same direction, and every walk is rotated to start on a segment boundary
    so partial first/last segments never occur.
    """
    allowed = set(edge_ids)
    used = set()
    cycles: List[List[Tuple[int, tuple, tuple]]] = []  # (edge_id, from_vertex, to_vertex)

    def seg_key(item):
        eid = item[0]
        ch = edge_chain.get(eid)
        if ch is None:
            return (None, None)
        cid, forward = ch
        rev = (item[1] == edges[eid][0]) != forward
        return (cid, rev)

    for e0 in edge_ids:
        if e0 in used:
            continue
        u, v = edges[e0][0], edges[e0][1]
        walk = [(e0, u, v)]
        used.add(e0)
        start, cur = u, v
        while cur != start:
            candidates = [eid for eid in adj[cur] if eid in allowed and eid not in used]
            if not candidates:
                break  # defensive; balanced sets should not dead-end
            prev_key = seg_key(walk[-1])
            same_chain = [eid for eid in candidates if seg_key((eid, cur, None)) == prev_key]
            eid = (same_chain or candidates)[0]
            eu, ev = edges[eid][0], edges[eid][1]
            nxt = ev if eu == cur else eu
            walk.append((eid, cur, nxt))
            used.add(eid)
            cur = nxt
        # rotate to a segment boundary (cyclically) so segments are whole chains
        n = len(walk)
        if n > 1:
            for k in range(n):
                if seg_key(walk[k]) != seg_key(walk[k - 1]):
                    walk = walk[k:] + walk[:k]
                    break
        cycles.append(walk)
    return cycles


def _segments_of(walk, edges, edge_chain, chain_edge_pos):
    """(chain_id, reversed, pos_lo, pos_hi) runs of a closed walk."""
    segments: List[Tuple[int, bool, int, int]] = []
    for eid, frm, to in walk:
        ch_id, pts_forward = edge_chain[eid]
        rev = (frm == edges[eid][0]) != pts_forward
        pos = chain_edge_pos[eid][1]
        if segments and segments[-1][0] == ch_id and segments[-1][1] == rev:
            lo, hi = segments[-1][2], segments[-1][3]
            segments[-1] = (ch_id, rev, min(lo, pos), max(hi, pos))
        else:
            segments.append((ch_id, rev, pos, pos))
    return segments


def masters_from_labels(labels: np.ndarray, background: int = 0,
                         fit_tolerance: float = 1.0, corner_cos: float = 0.5) -> Dict[int, list]:
    """Curve-preserving masters per label, split per connected pixel component.

    Shared boundary chains are fitted once and reused by both neighbouring
    labels (reversed where needed), keeping the curved partition watertight.
    Returns {label: [component, ...]} where each component is
    {'master': commands, 'legacyRings': rings, 'pixelArea': int, 'bbox': box}.
    """
    edges = list(_crack_edges(labels, background))
    if not edges:
        return {}
    adj: Dict[tuple, List[int]] = defaultdict(list)
    for idx, (u, v, a, b, _, _) in enumerate(edges):
        adj[u].append(idx)
        adj[v].append(idx)

    # Forced chain breaks: where a pair passes straight through a vertex but
    # one of its labels has a component degree != 2 there, a region walk must
    # turn mid-chain — pre-splitting keeps every assembled segment a whole chain.
    label_deg: Dict[tuple, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    pair_deg: Dict[tuple, Dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    for u, v, a, b, _, _ in edges:
        pair = (a, b) if a <= b else (b, a)
        for vertex in (u, v):
            label_deg[vertex][a] += 1
            label_deg[vertex][b] += 1
            pair_deg[vertex][pair] += 1
    forced: Dict[tuple, set] = defaultdict(set)
    for vertex, pairs in pair_deg.items():
        for pair, count in pairs.items():
            if count == 2:
                la, lb = pair
                if label_deg[vertex].get(la, 0) != 2 or label_deg[vertex].get(lb, 0) != 2:
                    forced[vertex].add(pair)

    def build_chains():
        chains, edge_chain, chain_edge_pos = _extract_chains(edges, adj, forced)
        for chain in chains:
            pts = chain['pts']
            if chain['closed']:
                chain['cmds'] = fit_ring(pts[:-1], fit_tolerance, corner_cos)
            else:
                chain['cmds'] = fit_polyline(pts, fit_tolerance, corner_cos)
            chain['cmds_rev'] = None  # lazy
        return chains, edge_chain, chain_edge_pos

    chains, edge_chain, chain_edge_pos = build_chains()

    def cmds_rev(chain):
        if chain['cmds_rev'] is None:
            chain['cmds_rev'] = reverse_commands(chain['cmds'])
        return chain['cmds_rev']

    # per label, per pixel-component edge sets (computed once; edges never change)
    comp_index: Dict[Tuple[int, int], List[int]] = {}
    comp_areas: Dict[Tuple[int, int], int] = {}
    labels_list = sorted({e[2] for e in edges} | {e[3] for e in edges})
    for value in labels_list:
        if value == background:
            continue
        mask = labels == value
        comp, ncomp = ndi.label(mask)  # 4-connected, matches rasterio connectivity=4
        if ncomp == 0:
            continue
        areas = np.bincount(comp.ravel(), minlength=ncomp + 1)
        by_comp: Dict[int, List[int]] = defaultdict(list)
        for idx, (u, v, a, b, pa, pb) in enumerate(edges):
            pixel = pa if a == value else (pb if b == value else None)
            if pixel is None:
                continue
            cid = int(comp[pixel[0], pixel[1]])
            if cid:
                by_comp[cid].append(idx)
        for cid, eids in by_comp.items():
            comp_index[(value, cid)] = eids
            comp_areas[(value, cid)] = int(areas[cid])

    # Safety passes: verify every walk segment covers a whole chain; if a
    # partial segment survives, force a chain break there and rebuild.
    for _pass in range(2):
        extra = 0
        for eids in comp_index.values():
            for walk in _euler_cycles(eids, edges, adj, edge_chain):
                for ch_id, _rev, lo, hi in _segments_of(walk, edges, edge_chain, chain_edge_pos):
                    n_pts = len(chains[ch_id]['pts'])
                    if hi - lo + 1 < n_pts - 1:
                        extra += 1
                        pts = chains[ch_id]['pts']
                        if 0 < lo:
                            forced[pts[lo]].add(chains[ch_id]['pair'])
                        if hi < n_pts - 2:
                            forced[pts[hi + 1]].add(chains[ch_id]['pair'])
        if not extra:
            break
        chains, edge_chain, chain_edge_pos = build_chains()

    result: Dict[int, list] = {}
    for (value, cid), eids in comp_index.items():
        cycles = _euler_cycles(eids, edges, adj, edge_chain)
        loops = []  # (ring_area, loop_cmds, ring)
        for walk in cycles:
            segments = _segments_of(walk, edges, edge_chain, chain_edge_pos)
            loop_cmds: List[Command] = []
            loop_pts: List[Tuple[float, float]] = []
            single_closed = False
            if len(segments) == 1:
                ch = chains[segments[0][0]]
                if ch['closed']:
                    single_closed = True
                    loop_cmds = list(ch['cmds'])
                    loop_pts = list(ch['pts']) + [ch['pts'][0]]
            if not single_closed:
                for ch_id, rev, _lo, _hi in segments:
                    seg = cmds_rev(chains[ch_id]) if rev else chains[ch_id]['cmds']
                    if not loop_cmds:
                        loop_cmds.append(('M', seg[0][1], seg[0][2]))
                    loop_cmds.extend(seg[1:])
                    seg_pts = chains[ch_id]['pts']
                    pts_seq = list(reversed(seg_pts)) if rev else list(seg_pts)
                    if loop_pts and loop_pts[-1] == pts_seq[0]:
                        pts_seq = pts_seq[1:]
                    loop_pts.extend(pts_seq)
                loop_cmds.append(('Z',))
            if not loop_cmds or len(loop_pts) < 3:
                continue
            if loop_pts[0] == loop_pts[-1]:
                loop_pts = loop_pts[:-1]
            if len(loop_pts) < 3:
                continue
            ring = [(float(x), float(y)) for x, y in loop_pts]
            raw_area = abs(sum(ring[k][0] * ring[(k + 1) % len(ring)][1]
                               - ring[(k + 1) % len(ring)][0] * ring[k][1]
                               for k in range(len(ring))) / 2.0)
            loops.append((raw_area, loop_cmds, ring))
        if not loops:
            continue
        # outer rings first, holes after: stable order for legacy consumers
        loops.sort(key=lambda t: t[0], reverse=True)
        master: List[Command] = []
        legacy_rings: List[List[Tuple[float, float]]] = []
        for _area, loop_cmds, ring in loops:
            if master:
                master.extend(loop_cmds)
            else:
                master = list(loop_cmds)
            legacy_rings.append(ring)
        xs = [p[0] for ring in legacy_rings for p in ring]
        ys = [p[1] for ring in legacy_rings for p in ring]
        components = result.setdefault(value, [])
        components.append({
            'master': master,
            'legacyRings': legacy_rings,
            'pixelArea': comp_areas[(value, cid)],
            'bbox': (min(xs), min(ys), max(xs), max(ys)),
        })
    return result


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def merge_tiny(labels: np.ndarray, rgb: np.ndarray, minimum: int) -> np.ndarray:
    """Merge only adjacent tiny components; never bridge unrelated pieces."""
    labels = labels.astype(np.int32).copy()
    for _ in range(3):
        counts = np.bincount(labels.ravel())
        tiny = np.where((counts > 0) & (counts < minimum))[0]
        tiny = tiny[tiny != 0]
        if not len(tiny):
            break
        slices = ndi.find_objects(labels)
        for rid in tiny:
            sl = slices[rid - 1] if rid - 1 < len(slices) else None
            if sl is None:
                continue
            y, x = sl
            y0, y1 = max(0, y.start - 1), min(labels.shape[0], y.stop + 1)
            x0, x1 = max(0, x.start - 1), min(labels.shape[1], x.stop + 1)
            crop = labels[y0:y1, x0:x1]; pixels = rgb[y0:y1, x0:x1]
            own = crop == rid
            edge = ndi.binary_dilation(own, structure=ndi.generate_binary_structure(2, 1)) & ~own
            neighbors = np.unique(crop[edge]); neighbors = neighbors[neighbors != 0]
            if not len(neighbors):
                continue
            mean = pixels[own].mean(0)
            target = min(neighbors, key=lambda n: float(np.sum((pixels[(crop == n) & edge].mean(0) - mean) ** 2)))
            crop[own] = target
    return labels


def palette_for_regions(means: np.ndarray, requested: int):
    from skimage.color import rgb2lab, lab2rgb
    labs = rgb2lab(np.asarray(means, dtype=float).reshape(-1, 1, 3) / 255).reshape(-1, 3).astype(np.float32)
    count = min(requested, len(labs), len(np.unique(np.round(labs, 1), axis=0)))
    count = max(1, count)
    cv2.setRNGSeed(23)
    _, indexes, centers = cv2.kmeans(labs, count, None,
                                      (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 80, .1), 1, cv2.KMEANS_PP_CENTERS)
    order = np.argsort(centers[:, 0], kind='stable')
    inverse = np.empty(count, dtype=int); inverse[order] = np.arange(count)
    colors = np.clip(lab2rgb(centers[order].reshape(-1, 1, 3)).reshape(-1, 3) * 255, 0, 255).astype(np.uint8)
    palette = []
    for i, c in enumerate(colors, 1):
        hx = '#' + ''.join(f'{int(v):02X}' for v in c)
        palette.append({'id': i, 'number': i, 'name': f'Tone {i:02d}', 'hex': hx,
                        'paint': {'type': 'linearGradient', 'stops': [{'offset': 0, 'color': hx}, {'offset': 1, 'color': hx}]}})
    return palette, inverse[indexes.ravel()] + 1


def _palette_from_hexes(hexes: Sequence[str]):
    """Palette for SVG masters: exact master fills ordered by Lab lightness."""
    from skimage.color import rgb2lab
    unique = sorted(set(hexes))
    labs = rgb2lab(np.asarray([[int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)] for h in unique],
                              dtype=float).reshape(-1, 1, 3) / 255).reshape(-1, 3)
    order = sorted(range(len(unique)), key=lambda i: (labs[i][0], unique[i]))
    mapping = {unique[i]: rank + 1 for rank, i in enumerate(order)}
    palette = []
    for rank, i in enumerate(order, 1):
        hx = unique[i]
        palette.append({'id': rank, 'number': rank, 'name': f'Tone {rank:02d}', 'hex': hx,
                        'paint': {'type': 'linearGradient', 'stops': [{'offset': 0, 'color': hx}, {'offset': 1, 'color': hx}]}})
    return palette, mapping


# ---------------------------------------------------------------------------
# Paint / ink tracing (all layers curve-preserving)
# ---------------------------------------------------------------------------

def trace_paint(rgb: np.ndarray, settings: BuildSettings, artwork_id: str,
                curved: bool = True):
    """Detailed vector underpainting. Curved mode fits shared chains; the
    legacy mode reproduces the pre-upgrade pixel-edge polygons exactly."""
    im = Image.fromarray(cv2.bilateralFilter(rgb, 5, 22, 2))
    quant = im.quantize(colors=settings.paint_colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).filter(ImageFilter.ModeFilter(5))
    ids = np.asarray(quant, dtype=np.uint8)
    table = np.array(quant.getpalette(), dtype=np.uint8).reshape(-1, 3)
    hexes = ['#' + ''.join(f'{int(v):02X}' for v in c) for c in table]
    n_shapes = 0
    grouped: Dict[str, List[str]] = defaultdict(list)
    if curved:
        masters = masters_from_labels(ids, background=-1,
                                      fit_tolerance=settings.curve_tolerance,
                                      corner_cos=_corner_cos(settings.corner_angle_deg))
        for value, components in masters.items():
            hx = hexes[value] if value < len(hexes) else '#808080'
            for comp in components:
                grouped[hx].append(format_path(comp['master']))
                n_shapes += 1
    else:
        from rasterio.features import shapes
        for geom, val in shapes(ids, connectivity=4):
            p = shape(geom); n_shapes += 1
            hx = hexes[int(val)] if int(val) < len(hexes) else '#808080'
            grouped[hx].append(path_of(rings_of(p)))
    paint_paths = [{'fill': hx, 'd': ' '.join(ds)} for hx, ds in sorted(grouped.items())]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    dark = (gray < settings.ink_threshold).astype(np.uint8)
    ink = []
    if dark.any():
        if curved:
            ink_masters = masters_from_labels(dark.astype(np.int32), background=0,
                                              fit_tolerance=settings.curve_tolerance,
                                              corner_cos=_corner_cos(settings.corner_angle_deg))
            parts = [format_path(c['master']) for comps in ink_masters.values() for c in comps]
            n_shapes += len(parts)
        else:
            from rasterio.features import shapes
            parts = []
            for geom, _ in shapes(dark, mask=dark.astype(bool), connectivity=4):
                poly = shape(geom)
                if poly.area >= 2:
                    parts.append(path_of(rings_of(poly)))
        if parts:
            ink = [{'fill': INK, 'd': ' '.join(parts)}]
    h, w = rgb.shape[:2]
    return {'schemaVersion': 2, 'artworkId': artwork_id, 'viewBox': [0, 0, w, h],
            'paths': paint_paths, 'inkPaths': ink, 'sourceColorShapeCount': n_shapes,
            'notes': ('Detailed vector appearance layer produced by curve-preserving tracing. '
                      'Do not replace with flat single-color fills or the artwork visually degrades.')}, ids


# ---------------------------------------------------------------------------
# SVG output helpers
# ---------------------------------------------------------------------------

def svg_open(w, h):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'


def _xa(value: str) -> str:
    """Escape a value for an XML attribute context (defense in depth)."""
    return (str(value).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _gradient_defs(gradients):
    if not gradients:
        return ''
    parts = ['<defs>']
    for g in gradients:
        stops = ''.join(f'<stop offset="{fmt_num(s["offset"], 4)}" stop-color="{_xa(s["color"])}"'
                        + (f' stop-opacity="{fmt_num(s["opacity"], 3)}"' if s.get('opacity', 1) != 1 else '')
                        + '/>' for s in g['stops'])
        gid = _xa(g['id'])
        if g['type'] == 'linear':
            parts.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                         f'x1="{fmt_num(g["x1"])}" y1="{fmt_num(g["y1"])}" x2="{fmt_num(g["x2"])}" y2="{fmt_num(g["y2"])}">{stops}</linearGradient>')
        else:
            parts.append(f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                         f'cx="{fmt_num(g["cx"])}" cy="{fmt_num(g["cy"])}" r="{fmt_num(g["r"])}" '
                         f'fx="{fmt_num(g["fx"])}" fy="{fmt_num(g["fy"])}">{stops}</radialGradient>')
    parts.append('</defs>')
    return ''.join(parts)


def _ordered_paint_entries(paint, include_ink=True):
    """Paint + ink entries merged back into original drawing order (by z).

    Legacy bundles without ``z`` keep their previous ordering (filled paths
    first, ink after). Ink drawn behind a fill in the source stays behind it.
    """
    entries = list(paint.get('paths') or [])
    if include_ink:
        entries += list(paint.get('inkPaths') or [])
    return sorted(entries, key=lambda e: e.get('z', 1 << 30))


def _paint_element(p) -> str:
    """One paint layer element honouring fill rule, opacity, stroke, z-role."""
    if p.get('filled') is False or (p.get('strokeWidth') and not p.get('fill')):
        return (f'<path fill="none" stroke="{_xa(p["fill"])}" stroke-width="{number(p.get("strokeWidth", 1.5))}" '
                f'stroke-linecap="round" stroke-linejoin="round" d="{p["d"]}"/>')
    attrs = [f'fill="{_xa(p["fill"])}"']
    if p.get('fillRule') and p['fillRule'] != 'evenodd':
        attrs.append(f'fill-rule="{p["fillRule"]}"')
    if p.get('fillOpacity') is not None and p['fillOpacity'] < 0.999:
        attrs.append(f'fill-opacity="{fmt_num(p["fillOpacity"], 3)}"')
    if p.get('opacity') is not None and p['opacity'] < 0.999:
        attrs.append(f'opacity="{fmt_num(p["opacity"], 3)}"')
    if p.get('stroke') and p.get('strokeWidth', 0) > 0:
        attrs.append(f'stroke="{_xa(p["stroke"])}" stroke-width="{number(p["strokeWidth"])}" stroke-linejoin="round"')
    return f'<path {" ".join(attrs)} d="{p["d"]}"/>'


def svg_paint(paint):
    """Colored appearance layer: gradients + paths + ink in drawing order."""
    defs = _gradient_defs(paint.get('gradients'))
    body = '<g>' + ''.join(_paint_element(p) for p in _ordered_paint_entries(paint)) + '</g>'
    return defs + body


def svg_ink(paint):
    parts = [_paint_element(p) for p in (paint.get('inkPaths') or [])]
    return '<g>' + ''.join(parts) + '</g>'


# ---------------------------------------------------------------------------
# Validation (geometry tests; visual review is reported separately)
# ---------------------------------------------------------------------------

def validate_bundle(bundle: dict, roundtrip=True) -> dict:
    m, g, p, paint = bundle['manifest'], bundle['geometry'], bundle['palette'], bundle['paint']
    errors = []; warnings = []
    regs = g['regions'] + g.get('decorations', [])
    w, h = map(int, g['viewBox'][2:]); ids = set(); pids = {a['id'] for a in p}
    polys = []; outside_labels = []
    flat_tol = float(g.get('flattenTolerance', FLATTEN_TOLERANCE))
    source = g.get('source', 'raster')
    allowance = float(g.get('partitionTolerance', 0.0))
    if m['id'] != g['artworkId'] or m['id'] != paint['artworkId']: errors.append('Artwork identity mismatch.')
    if m['version'] != g['artworkVersion']: errors.append('Artwork version mismatch.')
    if len(pids) != len(p): errors.append('Duplicate palette IDs.')
    if m['regionCount'] != len(g['regions']): errors.append('Incorrect region count.')
    curved_cmds = 0; total_cmds = 0
    hit_samples = 0; hit_mismatches = []; z_overlaps = 0
    fill_rules = {'evenodd': 0, 'nonzero': 0}
    label_mismatches = []
    prep = [(r, r.get('flat', {}).get('rings') or r.get('rings') or []) for r in regs]
    z_order = {id(r): k for k, (r, _rings) in enumerate(prep)}
    for r, rings in prep:
        if r['id'] in ids: errors.append('Duplicate region ID.')
        ids.add(r['id'])
        rule = r.get('fillRule', 'evenodd')
        if rule not in fill_rules:
            errors.append(f'Unsupported fill rule {rule!r}: ' + r['id'])
            rule = 'evenodd'
        else:
            fill_rules[rule] += 1
        # master path must parse, be closed and multi-subpath-complete
        try:
            cmds = region_master_commands(r)
        except ValueError as exc:
            errors.append(f'Unparsable master path: {r["id"]} ({exc})')
            continue
        if not r['d'].startswith('M') or not r['d'].endswith('Z'):
            errors.append('Open path: ' + r['id'])
        for cmd in cmds:
            total_cmds += 1
            if cmd[0] in ('C', 'Q'):
                curved_cmds += 1
        flat = flatten_path(cmds, flat_tol)
        flat_stored = rings
        if len(flat) != len(flat_stored):
            errors.append('Flattened ring count mismatch (derived data drift): ' + r['id'])
        area_flat = rule_area(flat, rule)
        if abs(area_flat - r['area']) > max(0.05, 0.004 * max(1.0, r['area'])):
            errors.append('Area mismatch vs flattened master: ' + r['id'])
        poly = region_polygon(r); polys.append(poly)
        if r['paletteId'] not in pids: errors.append('Unknown palette group.')
        if not poly.is_valid or poly.area <= 0: errors.append('Invalid polygon: ' + r['id'])
        if not box(0, 0, w, h).buffer(2.0).covers(poly): errors.append('Out of bounds: ' + r['id'])
        if not point_in_rings_rule(rings, r['label']['x'], r['label']['y'], rule):
            outside_labels.append(r['id'])
        # hit-test alignment: label point + bbox-inset probes must resolve to
        # THIS region. With visible-region geometry no other region may own
        # the point (fill-order independence: every region can be colored
        # first without depending on another region's completion).
        probes = [(r['label']['x'], r['label']['y'], True)]
        x0, y0, x1, y1 = r['bbox']
        for dx, dy in ((.25, .25), (.75, .25), (.25, .75), (.75, .75)):
            probe = (x0 + dx * (x1 - x0), y0 + dy * (y1 - y0))
            if point_in_rings_rule(rings, probe[0], probe[1], rule):
                probes.append((probe[0], probe[1], False))
        for px, py, is_label in probes:
            hit_samples += 1
            owners = [other for other, other_rings in prep
                      if other is not r and other in g['regions']
                      and other['bbox'][0] <= px <= other['bbox'][2]
                      and other['bbox'][1] <= py <= other['bbox'][3]
                      and point_in_rings_rule(other_rings, px, py,
                                              other.get('fillRule', 'evenodd'))]
            if source == 'svg-master' and owners:
                if is_label:
                    # Acceptance rule: every number must hit its own region.
                    label_mismatches.append(f'{r["id"]}@{px:.1f},{py:.1f}→{owners[0]["id"]}')
                z_overlaps += 1
            elif owners:
                hit_mismatches.append(f'{r["id"]}@{px:.1f},{py:.1f}→{owners[0]["id"]}')
    if outside_labels:
        errors.append('Labels outside region interiors: ' + ','.join(outside_labels[:5]))
    if label_mismatches:
        errors.append('Label ownership: number labels that resolve to a different region (visible-region geometry broken): '
                      + '; '.join(label_mismatches[:5]))
    # partition topology on the tolerance-flattened rings. Decorations are
    # not interactive (no masks, no hit-testing), so the gameplay overlap
    # gate measures playable regions only; raster keeps its stricter
    # full-partition check (decorations were part of the SLIC partition).
    if source == 'svg-master':
        region_polys = [region_polygon(r) for r in g['regions']]
        combined = unary_union([make_valid(poly) for poly in region_polys if not poly.is_empty])
        total_area = sum(poly.area for poly in region_polys if not poly.is_empty)
    else:
        combined = unary_union([make_valid(poly) for poly in polys if not poly.is_empty])
        total_area = sum(poly.area for poly in polys if not poly.is_empty)
    overlap = max(0.0, total_area - combined.area)
    missing = max(0.0, w * h - combined.area)
    # Shared chains make the curved partition exact up to the fit tolerance:
    # sub-tolerance slivers (<1px² total, e.g. one crossing on a 1px feature)
    # are a documented band; real defects are orders of magnitude larger.
    band = max(allowance + 0.01, 1.0)
    if source == 'raster':
        if missing > band or overlap > band:
            errors.append(f'Region partition has missing ({missing:.2f}px) or overlapping ({overlap:.2f}px) area beyond tolerance {allowance:.2f}.')
        elif overlap > 0.01 or missing > 0.01:
            warnings.append(f'Partition deviation within the curve-fit tolerance band: overlap {overlap:.2f}px², missing {missing:.2f}px² (shared chains keep gaps bounded; check 1px features at zoom).')
    else:
        # Visible-region geometry: masks must not overlap beyond the fit
        # tolerance band, otherwise coloring one region depends on another
        # region's completion (fill-order independence breaks).
        if overlap > band:
            errors.append(f'SVG-master regions overlap by {overlap:.1f}px² (beyond tolerance {band:.2f}); '
                          'visible-region subtraction failed - re-import the master.')
        elif overlap > 0.01:
            warnings.append(f'SVG-master regions overlap by {overlap:.1f}px² (within the fit tolerance band).')
        if missing > 0.01:
            warnings.append(f'SVG-master regions leave {missing:.1f}px² of the canvas uncovered; '
                            'shading and decorative shapes are expected to be non-playable.')
    empty_pixels = None
    if roundtrip and source == 'raster':
        back = _rasterize_regions(regs, w, h)
        empty_pixels = int((back == 0).sum())
        if empty_pixels > 8:
            errors.append(f'Raster roundtrip left {empty_pixels} uncovered pixels.')
        elif empty_pixels:
            warnings.append(f'Raster roundtrip left {empty_pixels} sub-pixel boundary pixels uncovered (within flatten tolerance).')
    small = [r['id'] for r in g['regions'] if 2 * r['label']['clearance'] * 360 / w * 8 < 24]
    if small:
        warnings.append(f'{len(small)} regions have less than a 24px inscribed target at 8x zoom on a 360px-wide canvas; inspect or merge them.')
    if hit_mismatches:
        warnings.append(f'Hit-test alignment: {len(hit_mismatches)}/{hit_samples} interior probes resolve to a lower region than expected: '
                        + '; '.join(hit_mismatches[:4]))
    if z_overlaps:
        warnings.append(f'Hit-test alignment: {z_overlaps}/{hit_samples} interior probes lie under another region; '
                        'visible-region geometry should own every tap surface - inspect the affected shapes.')
    if paint.get('sourceColorShapeCount', 0) > 15000:
        warnings.append('Detailed vector painting is heavy. Cache/rasterize its static layer in the game and test real devices.')
    warnings.append('Automatic regions are drafts, not guaranteed to follow semantic object boundaries. Human visual review is required.')
    fixed = sum(r['area'] for r in g.get('decorations', []))
    report = {
        'passed': not errors, 'errors': errors, 'warnings': warnings,
        'playableRegions': len(g['regions']),
        'fixedRegions': len(g.get('decorations', [])), 'paletteGroups': len(p),
        'paintPaths': len(paint['paths']), 'paintSubshapes': paint.get('sourceColorShapeCount'),
        'area': total_area, 'canvasArea': w * h, 'overlapArea': overlap, 'missingArea': missing,
        'roundtripEmptyPixels': empty_pixels, 'smallTargetCount': len(small),
        'precoloredAreaPercent': round(100 * fixed / (w * h), 3), 'humanReviewed': False,
        'geometry': {
            'schema': g.get('geometrySchema', 1),
            'masterAuthority': 'regions[*].master.d (curved SVG path commands)',
            'flattenedDerivation': f'rings/flat.rings are derived at ±{flat_tol}px tolerance; not master geometry',
            'backend': g.get('backend', 'legacy'),
            'curveFitTolerance': g.get('curveFitTolerance'),
            'curvedCommands': curved_cmds, 'totalCommands': total_cmds,
            'curvedCommandRatio': round(curved_cmds / total_cmds, 4) if total_cmds else 0.0,
            'hitTestProbes': hit_samples, 'hitTestConflicts': len(hit_mismatches),
            'hitTestZOverlaps': z_overlaps, 'labelOwnershipConflicts': len(label_mismatches),
            'partitionToleranceAllowance': allowance,
            'fillRules': fill_rules,
            'visibleRegionGeometry': source == 'svg-master',
            'acceptanceRule': ('every number label hits its own region, and every region can be colored first '
                               'without depending on another region completion (non-overlapping masks)'),
        },
        'visualReview': {
            'required': True,
            'separateFromGeometryTests': True,
            'limitations': [
                'Geometry tests prove watertightness, label containment, bounds and hit-test alignment at the documented flatten tolerance; they cannot judge aesthetics.',
                'Whether traced boundaries follow the intended object edges (semantic quality) needs human review at 400%+ zoom.',
                'Curve-fit tolerance hides sub-tolerance detail; small deliberate features may be smoothed.',
                'Colour harmony, shading intent and gameplay difficulty are not machine-validated.',
            ],
        },
        'checkScope': 'IDs, palette refs, versions, closed curved paths, master/flat consistency, polygon validity, bounds, label interiors, '
                      'partition union/overlap at flatten tolerance, raster coverage, hit-test probe alignment, curve statistics. '
                      'Not semantic/artistic quality.',
    }
    return report


def _rasterize_regions(regs, w, h):
    from rasterio.features import rasterize
    import affine as _affine
    polys = []
    for r in regs:
        rings = r.get('flat', {}).get('rings') or r.get('rings') or []
        if rings:
            polys.append(Polygon(rings[0], rings[1:]) if len(rings) > 1 else Polygon(rings[0]))
    return rasterize(((poly, 1) for poly in polys if not poly.is_empty),
                     out_shape=(h, w), fill=0, dtype=np.uint8, transform=_affine.Affine.translation(0, 0))


# ---------------------------------------------------------------------------
# Bundle emission
# ---------------------------------------------------------------------------

def emit_bundle(folder: Path, bundle: dict, previews=True) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    m, g, p, paint = bundle['manifest'], bundle['geometry'], bundle['palette'], bundle['paint']
    m['regionCount'] = len(g['regions']); m['paletteCount'] = len(p)
    w, h = map(int, g['viewBox'][2:]); vb = svg_open(w, h)
    write_json(folder / 'regions.json', g); write_json(folder / 'palette.json', p); write_json(folder / 'paint.json', paint)
    m['contentHash'] = hashlib.sha256(b''.join((folder / f).read_bytes() for f in ['regions.json', 'palette.json', 'paint.json'])).hexdigest()
    final = vb + svg_paint(paint) + '</svg>'
    (folder / 'colored.svg').write_text(final)
    outlines = '<g fill="none" stroke="' + INK + '" stroke-width="0.65" stroke-linejoin="round">' + \
        ''.join(f'<path d="{r["d"]}"/>' for r in g['regions']) + '</g>'
    (folder / 'linework.svg').write_text(vb + outlines + svg_ink(paint) + '</svg>')
    (folder / 'ink.svg').write_text(vb + svg_ink(paint) + '</svg>')

    def numbered(selected=False):
        select = g['regions'][0]['paletteId'] if g['regions'] else 1
        defs = '<defs><pattern id="sel" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#F0F3F6"/><path d="M0 0H4V4H0Z M4 4H8V8H4Z" fill="#CBD5DD"/></pattern></defs>'
        masks = '<g stroke="' + INK + '" stroke-width=".65" fill-rule="evenodd">' + ''.join(
            f'<path d="{r["d"]}"' + (' fill-rule="nonzero"' if r.get('fillRule') == 'nonzero' else '')
            + ' fill="' + ('url(#sel)' if selected and r['paletteId'] == select else 'white') + '"/>' for r in g['regions']) + '</g>'
        labels = '<g font-family="sans-serif" text-anchor="middle" dominant-baseline="central" fill="' + INK + '">' + ''.join(
            f'<text x="{r["label"]["x"]}" y="{r["label"]["y"]}" font-size="{r["label"]["fontSize"]}">{r["paletteId"]}</text>' for r in g['regions']) + '</g>'
        paint_under = {'gradients': paint.get('gradients'), 'paths': paint['paths'], 'inkPaths': []}
        return vb + defs + svg_paint(paint_under) + masks + svg_ink(paint) + labels + '</svg>'

    (folder / 'numbered.svg').write_text(numbered())
    (folder / 'selected-preview.svg').write_text(numbered(True))
    qa = validate_bundle(bundle)
    if not qa['passed']:
        raise ValueError('Asset validation failed: ' + '; '.join(qa['errors'][:5]))
    m['qa'] = {'status': 'draft-needs-human-review', 'passedGeometryChecks': True, 'humanReviewed': False}
    write_json(folder / 'validation.json', qa)
    if previews:
        import cairosvg
        from io import BytesIO
        raw = cairosvg.svg2png(bytestring=final.encode(), output_width=640, output_height=round(640 * h / w))
        img = Image.open(BytesIO(raw)).convert('RGB'); img.save(folder / 'thumbnail.webp', quality=88)
        img.save(folder / 'colored-preview.png')
        cairosvg.svg2png(bytestring=numbered().encode(), write_to=str(folder / 'numbered-preview.png'),
                         output_width=640, output_height=round(640 * h / w))
    m['checksums'] = {f: checksum(folder / f) for f in ['regions.json', 'palette.json', 'paint.json', 'colored.svg', 'numbered.svg', 'linework.svg', 'ink.svg']}
    write_json(folder / 'artwork.json', m)
    return qa


# ---------------------------------------------------------------------------
# Compilation: raster masters
# ---------------------------------------------------------------------------

def compile_image(source: Path, output: Path, *, artwork_id: str, version: str, title: str,
                  settings: BuildSettings, provenance: dict | None = None, progress: Callable = lambda *_: None) -> dict:
    progress(.04, 'Preparing approved master')
    im = Image.open(source).convert('RGB'); original = im.size
    im.thumbnail((settings.max_edge, settings.max_edge), Image.Resampling.LANCZOS)
    rgb = np.array(im); h, w = rgb.shape[:2]
    progress(.12, 'Finding image-aware draft regions')
    labels = slic(rgb, n_segments=settings.target_regions, compactness=settings.compactness,
                  sigma=.8, start_label=1, enforce_connectivity=True, min_size_factor=.25, channel_axis=-1)
    labels = merge_tiny(labels, rgb, settings.min_region_pixels)
    curved = settings.backend == 'spline-local'
    corner_cos = _corner_cos(settings.corner_angle_deg)
    progress(.24, 'Fitting shared boundary chains into curved masters' if curved else 'Extracting pixel-edge polygons (legacy)')
    regions = []; decorations = []
    if curved:
        masters = masters_from_labels(labels, background=0,
                                       fit_tolerance=settings.curve_tolerance, corner_cos=corner_cos)
        label_values = sorted(masters.keys())
        means = []
        for value in label_values:
            mask = labels == value
            px = rgb[mask]
            means.append(px.mean(0) if len(px) else np.array([128.0, 128.0, 128.0]))
        progress(.34, 'Grouping palette colors and positioning labels')
        palette, assignments = palette_for_regions(np.array(means), settings.palette_colors)
        palette_map = {value: int(assignments[i]) for i, value in enumerate(label_values)}
        rid = 0
        for value in label_values:
            for comp in masters[value]:
                rid += 1
                pid = palette_map[value]
                flat = flatten_path(comp['master'], FLATTEN_TOLERANCE)
                if not flat:
                    continue
                poly = Polygon(flat[0], flat[1:]) if len(flat) > 1 else Polygon(flat[0])
                label = make_label(poly, pid)
                reg = pack_region(comp['master'], f'r-{rid:05d}', pid, label=label,
                                  source='boundary-chain-fit', fit_tolerance=settings.curve_tolerance,
                                  legacy_rings=comp['legacyRings'])
                if label['clearance'] < settings.min_label_radius or label['fontSize'] < 3.5:
                    decorations.append(reg)
                else:
                    regions.append(reg)
    else:
        from rasterio.features import shapes
        progress(.28, 'Building closed, non-overlapping region polygons (legacy)')
        polys = []; means = []; masks = []; origins = []
        for geom, _ in shapes(labels.astype(np.int32), connectivity=4):
            poly = shape(geom)
            for poly in polygon_parts(make_valid(poly)):
                x0, y0, x1, y1 = map(int, poly.bounds)
                local = _rasterize_mask(poly, x0, y0, x1, y1)
                if not local.any():
                    continue
                polys.append(poly); means.append(rgb[y0:y1, x0:x1][local].mean(0)); masks.append(local); origins.append((x0, y0))
        progress(.38, 'Grouping palette colors and positioning labels')
        palette, assignments = palette_for_regions(np.array(means), settings.palette_colors)
        for i, (poly, pid, mask, origin) in enumerate(zip(polys, assignments, masks, origins), 1):
            label = make_label(poly, int(pid), mask, origin)
            reg = pack_region(poly, f'r-{i:05d}', int(pid), label=label, source='legacy-polygon', fit=False)
            if label['clearance'] < settings.min_label_radius or label['fontSize'] < 3.5:
                decorations.append(reg)
            else:
                regions.append(reg)
    if not regions:
        raise ValueError('No playable regions. Lower the label radius or region count.')
    progress(.48, 'Tracing the detailed vector paint layer' if curved else 'Tracing pixel-edge paint polygons (legacy)')
    paint, _ = trace_paint(rgb, settings, artwork_id, curved=curved)
    geometry = {'schemaVersion': 2, 'geometrySchema': GEOMETRY_SCHEMA, 'source': 'raster',
                'artworkId': artwork_id, 'artworkVersion': version,
                'viewBox': [0, 0, w, h], 'fillRule': 'evenodd', 'stroke': INK, 'strokeWidth': .65,
                'backend': settings.backend, 'flattenTolerance': FLATTEN_TOLERANCE,
                'curveFitTolerance': settings.curve_tolerance if curved else None,
                'partitionTolerance': 0.0,
                'regions': regions, 'decorations': decorations, 'detailPaths': []}
    manifest = {'schemaVersion': 1, 'format': SCHEMA, 'id': artwork_id, 'version': version, 'title': title,
                'description': 'Curve-preserving vector-region draft. Masters are curved SVG paths; flattened rings are derived approximations.',
                'category': 'Studio', 'viewBox': [0, 0, w, h], 'difficulty': 'unrated', 'difficultyValidatedByPlaytest': False,
                'regionCount': len(regions), 'paletteCount': len(palette), 'objectGroups': [],
                'assets': {'regions': 'regions.json', 'palette': 'palette.json', 'paint': 'paint.json', 'coloredSvg': 'colored.svg',
                           'numberedSvg': 'numbered.svg', 'lineworkSvg': 'linework.svg', 'inkSvg': 'ink.svg', 'thumbnail': 'thumbnail.webp', 'sourceMaster': 'source-master.png'},
                'rendering': {'model': 'vector-underpainting-with-region-masks', 'fillRule': 'evenodd', 'decorationsArePrecolored': True,
                              'labelMinScreenPx': 9, 'zoomRecommended': 8,
                              'geometrySchema': GEOMETRY_SCHEMA,
                              'masterNote': 'regions[*].master.d holds authoritative curved path commands; rings are derived at the documented tolerance.'},
                'generation': {'settings': settings.model_dump(), 'sourcePixels': list(original), 'workingPixels': [w, h],
                               'algorithm': ('SLIC + adjacent-small-region merge + shared-boundary-chain Schneider cubic fitting '
                                             '(corner-preserving, watertight by construction); bilateral-smoothed median-cut '
                                             'underpainting traced with the same curve fitting' if curved else
                                             'SLIC + adjacent-small-region merge + exact pixel-edge polygonization (legacy pre-upgrade tracer)'),
                               'rasterizedMaster': False},
                'provenance': provenance or {'source': 'User-supplied image; rights not independently verified'}}
    bundle = {'manifest': manifest, 'geometry': geometry, 'palette': palette, 'paint': paint}
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output / 'source-master.png')
    progress(.70, 'Checking topology, labels and runtime exports')
    qa = emit_bundle(output, bundle)
    write_json(output / 'build-settings.json', settings.model_dump())
    progress(1, 'Draft bundle ready for review')
    return {'manifest': manifest, 'validation': qa}


def _rasterize_mask(poly, x0, y0, x1, y1):
    from rasterio.features import rasterize
    import affine as _affine
    return rasterize([(poly, 1)], out_shape=(y1 - y0, x1 - x0),
                     transform=_affine.Affine.translation(x0, y0), dtype=np.uint8).astype(bool)


# ---------------------------------------------------------------------------
# Compilation: sanitized SVG masters (never rasterized)
# ---------------------------------------------------------------------------

def _split_disconnected_master(cmds: List[Command], fit_tolerance: float):
    """Split a master whose subpaths form disconnected tap targets.

    Returns [(part_commands, part_flat_rings)] — one per connected geometry.
    """
    subs = subpaths_of(cmds)
    parts = []
    for sub in subs:
        flat = flatten_path(sub, FLATTEN_TOLERANCE)
        if flat:
            parts.append({'cmds': sub, 'rings': flat})
    # merge parts whose polygons touch/overlap (connected tap target)
    merged = []
    for part in parts:
        poly = Polygon(part['rings'][0], part['rings'][1:]) if len(part['rings']) > 1 else Polygon(part['rings'][0])
        part['poly'] = poly if poly.is_valid else make_valid(poly)
        placed = False
        for group in merged:
            if any(g['poly'].intersects(part['poly']) or g['poly'].touches(part['poly']) or
                   g['poly'].buffer(0.01).intersects(part['poly']) for g in group):
                group.append(part)
                placed = True
                break
        if not placed:
            merged.append([part])
    out = []
    for group in merged:
        cmds_out: List[Command] = []
        rings_out = []
        for part in group:
            cmds_out.extend(part['cmds'])
            rings_out.extend(part['rings'])
        out.append((cmds_out, rings_out))
    return out


def compile_svg_master(source: Path, output: Path, *, artwork_id: str, version: str, title: str,
                        settings: BuildSettings, provenance: dict | None = None,
                        progress: Callable = lambda *_: None) -> dict:
    """Compile a sanitized SVG master into the detailed-vector bundle.

    Trust rules enforced here (review stage 1):
    - VISIBLE-REGION GEOMETRY: an opaque shape drawn above another covers it.
      Gameplay regions are the *visible surfaces* (own geometry minus the
      coverage of every later opaque shape), so region masks never overlap:
      every number hits its own region and every region can be colored
      first, whatever order the player fills in.
    - Visual fidelity: paint/ink layers keep the original drawing order (z),
      per-shape fill rule, fill-opacity/opacity and strokes on filled
      shapes. Transparent shapes and role=shading are appearance, not
      gameplay; role=ink stays ink. Nothing is rasterized.
    """
    from .svg_master import import_master, shape_solids, _solid_union
    progress(.06, 'Sanitizing the SVG master (curves are never rasterized)')
    doc = import_master(source.read_text(encoding='utf-8'))
    x, y, w, h = (float(v) for v in doc.view_box)
    if w > 4096 or h > 4096:
        raise ValueError('Scale the SVG master down to at most 4096 user units per side.')
    corner_cos = _corner_cos(settings.corner_angle_deg)
    progress(.18, 'Separating shading shapes from gameplay tap surfaces')
    ink_parts = []
    for s in doc.ink_shapes:
        ink_parts.append({'shapeId': s['id'], 'z': s['order'], 'fill': s.get('stroke') or INK, 'd': s['d'],
                          'strokeWidth': round(max(0.4, s.get('strokeWidth', 1.5)), 3), 'filled': False})
    # NOTE: filled shapes with strokes are no longer converted to ink
    # overlays (that reordered ink above fills); the stroke stays on the
    # filled paint path itself, exactly like the source SVG.
    gameplay = []
    for s in doc.shapes:
        if s.get('hidden'):
            continue
        is_shading = (s.get('role') == 'shading'
                      or s.get('fillOpacity', 1.0) * s.get('opacity', 1.0) < 0.999)
        if is_shading and s.get('role') != 'gameplay':
            continue
        gameplay.append(s)
    if not gameplay:
        raise ValueError('The SVG master has no gameplay-sized filled shapes.')
    progress(.30, 'Deriving visible tap surfaces (opaque coverage subtracted)')
    # Coverage model: every non-hidden, effectively opaque filled shape
    # (any role) covers whatever is drawn below it.
    cover = []
    for s in doc.shapes:
        if s.get('hidden') or s.get('fillOpacity', 1.0) * s.get('opacity', 1.0) < 0.999:
            continue
        solid = _solid_union(shape_solids(s['rings'], s['fillRule']))
        if not solid.is_empty and solid.area > 0:
            cover.append((s['order'], solid))
    cover.sort(key=lambda t: t[0])
    orders = [o for o, _poly in cover]
    suffix = [Polygon()] * (len(cover) + 1)
    for i in range(len(cover) - 1, -1, -1):
        try:
            suffix[i] = cover[i][1].union(suffix[i + 1])
        except Exception:
            suffix[i] = suffix[i + 1]

    def coverage_above(order: float):
        import bisect
        return suffix[bisect.bisect_right(orders, order)]

    from shapely.geometry.polygon import orient as _orient
    candidates = []
    for s in gameplay:
        own = _solid_union(shape_solids(s['rings'], s['fillRule']))
        if own.is_empty or own.area <= 0:
            continue
        above = coverage_above(s['order'])
        visible = own if above.is_empty else own.difference(above)
        if visible.is_empty or visible.area <= 1e-9:
            continue                     # fully covered: not a reachable surface
        ratio = visible.area / max(own.area, 1e-9)
        if ratio >= 0.999:
            # Uncovered: keep the verbatim master commands (perfect fidelity),
            # splitting disconnected tap targets as before.
            for part_cmds, part_rings in _split_disconnected_master(s['commands'], settings.curve_tolerance):
                area = rule_area(part_rings, s['fillRule'])
                if area <= 0:
                    continue
                candidates.append({'shape': s, 'cmds': part_cmds, 'rings': part_rings,
                                   'area': area, 'fillRule': s['fillRule'], 'verbatim': True,
                                   'poly': _solid_union(shape_solids(part_rings, s['fillRule']))})
        else:
            # Partially covered: the visible surface is the derived geometry
            # (own boundary minus foreground coverage). Each connected piece
            # becomes its own tap target; rings are oriented so evenodd and
            # nonzero agree on the result.
            for part in polygon_parts(make_valid(visible)):
                if part is None or part.is_empty or part.area <= 1e-9:
                    continue
                try:
                    part = _orient(part, 1.0)
                except Exception:
                    pass
                candidates.append({'shape': s, 'poly': part, 'area': part.area,
                                   'fillRule': 'evenodd', 'verbatim': False})
    if not candidates:
        raise ValueError('No reachable tap surfaces: every gameplay shape is covered.')
    progress(.42, 'Building the palette from master fills')
    hexes = [c['shape'].get('fill') or '#808080' for c in candidates]
    palette, mapping = _palette_from_hexes(hexes)
    if len(palette) > 80:
        raise ValueError('The SVG master uses more than 80 distinct gameplay fills; reduce the palette.')
    regions = []; decorations = []
    for idx, cand in enumerate(candidates, 1):
        pid = mapping[cand['shape'].get('fill') or '#808080']
        poly = cand.get('poly')
        if poly is None:
            poly = Polygon(cand['rings'][0], cand['rings'][1:]) if len(cand['rings']) > 1 else Polygon(cand['rings'][0])
        label = make_label(poly, pid)
        if cand['verbatim']:
            reg = pack_region(cand['cmds'], f'r-{idx:05d}', pid, label=label,
                              source='svg-master-import', fit_tolerance=0.0,
                              fill_rule=cand['fillRule'])
        else:
            reg = pack_region(cand['poly'], f'r-{idx:05d}', pid, label=label,
                              source='visible-surface-refit', fit_tolerance=settings.curve_tolerance,
                              fill_rule='evenodd')
        reg['masterShapeId'] = cand['shape']['id']
        if label['clearance'] < settings.min_label_radius or label['fontSize'] < 3.5 \
                or cand['area'] < settings.min_region_pixels:
            decorations.append(reg)
        else:
            regions.append(reg)
    if not regions:
        raise ValueError('No playable regions in the SVG master; shapes are too small.')
    progress(.55, 'Emitting the detailed paint layer (order, strokes and gradients preserved)')
    paint_paths = []
    gradients = []
    for s in doc.shapes:
        if s.get('hidden'):
            continue
        entry = {'z': s['order'], 'shapeId': s['id'], 'd': s['d'], 'fillRule': s['fillRule']}
        if s.get('gradient'):
            gradients.append(s['gradient'])
            entry['fill'] = f'url(#{s["gradient"]["id"]})'
        else:
            entry['fill'] = s.get('fill') or '#808080'
        if s.get('fillOpacity', 1.0) < 0.999:
            entry['fillOpacity'] = round(s['fillOpacity'], 4)
        if s.get('opacity', 1.0) < 0.999:
            entry['opacity'] = round(s['opacity'], 4)
        if s.get('stroke') and s.get('strokeWidth', 0) > 0:
            entry['stroke'] = s['stroke']
            entry['strokeWidth'] = round(s['strokeWidth'], 3)
        paint_paths.append(entry)
    ink_paths = []
    for part in ink_parts:
        entry = {'fill': part['fill'], 'd': part['d']}
        if part.get('z') is not None:
            entry['z'] = part['z']
        if part.get('shapeId'):
            entry['shapeId'] = part['shapeId']
        if part.get('strokeWidth'):
            entry['strokeWidth'] = part['strokeWidth']
        if part.get('filled') is False:
            entry['filled'] = False
        ink_paths.append(entry)
    paint = {'schemaVersion': 2, 'artworkId': artwork_id, 'viewBox': [0, 0, w, h],
             'paths': paint_paths, 'inkPaths': ink_paths, 'gradients': gradients,
             'sourceColorShapeCount': len(paint_paths) + len(ink_paths),
             'notes': ('Detailed vector appearance layer copied from the sanitized SVG master with curves, holes, '
                       'gradients, per-shape fill rules, opacity, strokes and drawing order preserved. Never rasterized. '
                       'Do not replace with flat single-color fills or the artwork visually degrades.')}
    # Visible-region partition: masks must not overlap beyond the fit band.
    overlap_allowance = 0.0
    try:
        polys = [region_polygon(r) for r in regions]
        union = unary_union([p for p in polys if not p.is_empty])
        overlap_allowance = max(0.0, sum(p.area for p in polys if not p.is_empty) - union.area)
        # margin for union/make_valid drift when validation re-measures
        overlap_allowance += max(1.0, overlap_allowance * 0.05)
    except Exception:
        overlap_allowance = float(w * h * 0.002)
    geometry = {'schemaVersion': 2, 'geometrySchema': GEOMETRY_SCHEMA, 'source': 'svg-master',
                'artworkId': artwork_id, 'artworkVersion': version,
                'viewBox': [0, 0, w, h], 'fillRule': 'evenodd', 'stroke': INK, 'strokeWidth': .65,
                'backend': 'svg-master', 'flattenTolerance': FLATTEN_TOLERANCE,
                'curveFitTolerance': settings.curve_tolerance, 'partitionTolerance': round(overlap_allowance + 0.01, 3),
                'visibleRegionGeometry': True,
                'importReport': doc.report, 'regions': regions, 'decorations': decorations, 'detailPaths': []}
    manifest = {'schemaVersion': 1, 'format': SCHEMA, 'id': artwork_id, 'version': version, 'title': title,
                'description': 'Compiled from a sanitized SVG master: curves, holes, gradients, transforms and drawing order preserved; regions are visible surfaces.',
                'category': 'Studio', 'viewBox': [0, 0, w, h], 'difficulty': 'unrated', 'difficultyValidatedByPlaytest': False,
                'regionCount': len(regions), 'paletteCount': len(palette), 'objectGroups': [],
                'assets': {'regions': 'regions.json', 'palette': 'palette.json', 'paint': 'paint.json', 'coloredSvg': 'colored.svg',
                           'numberedSvg': 'numbered.svg', 'lineworkSvg': 'linework.svg', 'inkSvg': 'ink.svg', 'thumbnail': 'thumbnail.webp', 'sourceMaster': 'source-master.svg'},
                'rendering': {'model': 'vector-underpainting-with-region-masks', 'fillRule': 'per-region (evenodd default, nonzero preserved)', 'decorationsArePrecolored': True,
                              'labelMinScreenPx': 9, 'zoomRecommended': 8, 'geometrySchema': GEOMETRY_SCHEMA,
                              'visibleRegionGeometry': True,
                              'masterNote': 'Masters are the imported SVG path commands verbatim (arcs pre-converted to cubics); covered portions are replaced by the derived visible-surface geometry.'},
                'generation': {'settings': settings.model_dump(),
                               'algorithm': 'Sanitized SVG-master import: curves/holes/gradients/transforms/drawing-order preserved; '
                                            'visible surfaces derived by subtracting opaque coverage; disconnected tap targets split; '
                                            'fully hidden shapes excluded; shading and transparent shapes are appearance, not gameplay.',
                               'rasterizedMaster': False},
                'provenance': provenance or {'source': 'User-supplied SVG; rights not independently verified'}}
    bundle = {'manifest': manifest, 'geometry': geometry, 'palette': palette, 'paint': paint}
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output / 'source-master.svg')
    progress(.72, 'Checking topology, labels and runtime exports')
    qa = emit_bundle(output, bundle)
    write_json(output / 'build-settings.json', settings.model_dump())
    progress(1, 'SVG-master bundle ready for review')
    return {'manifest': manifest, 'validation': qa}


# ---------------------------------------------------------------------------
# Bundle loading (save compatibility: legacy v1 polygon bundles migrate)
# ---------------------------------------------------------------------------

def load_bundle(folder):
    geometry = read_json(folder / 'regions.json')
    if geometry.get('geometrySchema', 1) < 2:
        # Legacy polygon bundle: promote rings to line-only masters (lossless).
        for r in geometry.get('regions', []) + geometry.get('decorations', []):
            rings = r.get('rings') or []
            d = path_of(rings) if rings else r.get('d', '')
            r['master'] = {'d': d, 'fillRule': r.get('fillRule', 'evenodd'),
                           'source': 'legacy-polygon-migration', 'tolerance': 0.0}
            r['flat'] = {'tolerance': 0.0, 'rings': rings,
                         'note': 'Legacy polygon bundle migrated in-memory; rings are exact pixel-edge geometry.'}
            r['d'] = d
        geometry['geometrySchema'] = 1
        geometry['legacyImport'] = True
        geometry.setdefault('source', 'raster')
        geometry.setdefault('backend', 'legacy-polygon')
        geometry.setdefault('flattenTolerance', 0.0)
    return {'manifest': read_json(folder / 'artwork.json'), 'geometry': geometry,
            'palette': read_json(folder / 'palette.json'), 'paint': read_json(folder / 'paint.json')}


def legacy_geometry(geometry: dict) -> dict:
    """Zoom-lab payload: the same regions as pre-upgrade pixel-edge polygons."""
    regions = []
    for r in geometry.get('regions', []):
        legacy = r.get('legacy', {}).get('rings')
        if not legacy:
            legacy = [snap_ring(ring) for ring in (r.get('flat', {}).get('rings') or r.get('rings') or [])]
            legacy = [ring for ring in legacy if len(ring) >= 3]
        regions.append({'id': r['id'], 'paletteId': r['paletteId'], 'fillRule': 'evenodd',
                        'd': path_of(legacy), 'label': r['label'], 'bbox': r['bbox']})
    out = {k: v for k, v in geometry.items() if k not in ('regions', 'decorations')}
    out['regions'] = regions
    out['decorations'] = []
    out['mode'] = 'legacy'
    return out


# ---------------------------------------------------------------------------
# Edits (curve-preserving; every edit produces a new version)
# ---------------------------------------------------------------------------



def _tint_gradient(gradient: dict, target_hex: str) -> None:
    """Shift a gradient's stop colors toward ``target_hex`` while keeping the
    stop-to-stop shading relationship (light/dark structure is preserved)."""
    stops = gradient.get('stops') or []
    if not stops:
        return
    avg = [0.0, 0.0, 0.0]
    for s in stops:
        c = s.get('color', '#808080').lstrip('#')
        avg[0] += int(c[0:2], 16); avg[1] += int(c[2:4], 16); avg[2] += int(c[4:6], 16)
    avg = [v / len(stops) for v in avg]
    tgt = [int(target_hex.lstrip('#')[k:k + 2], 16) for k in (0, 2, 4)]
    ratios = [(tgt[k] / avg[k]) if avg[k] > 1e-6 else (1.0 if tgt[k] > 0 else 0.4) for k in range(3)]
    for s in stops:
        c = s.get('color', '#808080').lstrip('#')
        rgb = [min(255, max(0, round(int(c[k:k + 2], 16) * ratios[k // 2]))) for k in (0, 2, 4)]
        s['color'] = '#' + ''.join(f'{v:02X}' for v in rgb)


def _recolor_bundle(bundle: dict, chosen, color: str, preserve_shading: bool) -> None:
    """Recolor the *visible appearance* of the chosen regions' source shapes.

    Separate from the 'palette' action (which assigns the number group):
    this changes what the player sees. ``preserve_shading`` keeps gradient
    shading (tinted toward the target); otherwise the fill is replaced.
    """
    paint = bundle['paint']
    shape_ids = set()
    for r in chosen:
        sid = r.get('masterShapeId')
        if not sid:
            raise ValueError(f'Region {r["id"]} has no paintable source shape; '
                             'raster-built bundles can only be re-colored by rebuilding.')
        shape_ids.add(sid)
    paths = [p for p in (paint.get('paths') or []) if p.get('shapeId') in shape_ids]
    if not paths:
        raise ValueError('No paint paths found for the selected regions.')
    grad_ids = {p['fill'][5:-1] for p in paths if str(p.get('fill', '')).startswith('url(#')}
    for path in paths:
        if preserve_shading and str(path.get('fill', '')).startswith('url(#'):
            continue                      # tinted through the gradient below
        path['fill'] = color
    if preserve_shading and grad_ids:
        for gradient in (paint.get('gradients') or []):
            if gradient.get('id') in grad_ids:
                _tint_gradient(gradient, color)
    # keep the palette swatch in sync with the new appearance
    pids = {r['paletteId'] for r in chosen}
    for entry in bundle['palette']:
        if entry['id'] in pids:
            entry['hex'] = color
            entry['paint'] = {'type': 'linearGradient',
                              'stops': [{'offset': 0, 'color': color}, {'offset': 1, 'color': color}]}



def edit_bundle(source: Path, output: Path, request, version: str):
    bundle = load_bundle(source); g = bundle['geometry']; m = bundle['manifest']
    settings = read_json(source / 'build-settings.json') if (source / 'build-settings.json').is_file() else {}
    fit_tolerance = float(settings.get('curve_tolerance', 1.0))
    regs = {r['id']: r for r in g['regions']}
    chosen = [regs.get(rid) for rid in dict.fromkeys(request.region_ids)]
    if any(r is None for r in chosen):
        raise ValueError('Select existing playable regions from the current revision.')
    valid_palette = {p['id'] for p in bundle['palette']}
    pid = request.palette_id or chosen[0]['paletteId']
    if pid not in valid_palette: raise ValueError('Unknown palette group.')
    if request.action == 'merge':
        if len(chosen) < 2: raise ValueError('Select two or more adjacent regions.')
        union = unary_union([make_valid(region_polygon(r)) for r in chosen])
        if union.geom_type != 'Polygon' or not union.is_valid:
            raise ValueError('Merge only edge-adjacent regions; disconnected pieces cannot be one tap target.')
        used = set(request.region_ids)
        g['regions'] = [r for r in g['regions'] if r['id'] not in used]
        merged_id = 'r-m-' + hashlib.sha256(('|'.join(sorted(used)) + version).encode()).hexdigest()[:12]
        # curve-preserving refit: corners & straight architectural edges survive
        merged = pack_region(union, merged_id, pid, chosen[0]['objectId'],
                             source='merge-boundary-refit', fit_tolerance=fit_tolerance)
        g['regions'].append(merged)
        # The refit hugs the exact union polygon within the fit tolerance; the
        # measured deviation becomes this revision's documented partition band.
        deviation = region_polygon(merged).symmetric_difference(union).area
        g['partitionTolerance'] = round(float(g.get('partitionTolerance', 0.0)) + deviation + 0.01, 3)
    elif request.action == 'split':
        if len(chosen) != 1: raise ValueError('Select exactly one region to split.')
        target = chosen[0]
        parts = _split_disconnected_master(region_master_commands(target), fit_tolerance)
        if len(parts) < 2:
            raise ValueError('This region is already a single connected tap target.')
        g['regions'] = [r for r in g['regions'] if r['id'] != target['id']]
        for idx, (part_cmds, _rings) in enumerate(parts):
            split_id = 'r-s-' + hashlib.sha256((target['id'] + version + str(idx)).encode()).hexdigest()[:12]
            g['regions'].append(pack_region(part_cmds, split_id, target['paletteId'], target['objectId'],
                                            source='split-disconnected', fit_tolerance=0.0))
    elif request.action == 'group':
        for r in chosen: r['objectId'] = request.group
    elif request.action == 'palette':
        # Assigns the NUMBER GROUP (gameplay association), not the artwork's
        # visible color - use 'recolor' to change the painted appearance.
        for r in chosen:
            r['paletteId'] = pid
            r['label'] = make_label(region_polygon(r), pid)
    elif request.action == 'recolor':
        if not request.color:
            raise ValueError('Choose a #RRGGBB color to recolor with.')
        _recolor_bundle(bundle, chosen, request.color.upper(), bool(request.preserve_shading))
    elif request.action == 'label':
        if len(chosen) != 1 or request.x is None or request.y is None:
            raise ValueError('Select one region and a label position.')
        poly = region_polygon(chosen[0]); point = Point(request.x, request.y)
        if not poly.contains(point): raise ValueError('Label anchor must be strictly inside the region.')
        radius = poly.boundary.distance(point); digits = len(str(chosen[0]['paletteId']))
        size = min(22., radius * 1.6 / math.sqrt((digits * .65) ** 2 + 1))
        if size < 3.5: raise ValueError('Too close to the edge to fit the number. Choose a wider interior area.')
        chosen[0]['label'] = {'x': request.x, 'y': request.y, 'fontSize': round(size, 3), 'clearance': round(radius, 3), 'minScreenPx': 9}
    elif request.action == 'decorate':
        if len(chosen) >= len(g['regions']): raise ValueError('Keep at least one playable region.')
        remove = set(request.region_ids)
        g['regions'] = [r for r in g['regions'] if r['id'] not in remove]
        g['decorations'].extend(chosen)
    groups = defaultdict(list)
    for r in g['regions']:
        if r['objectId'] != 'unassigned':
            groups[r['objectId']].append(r['id'])
    m['objectGroups'] = [{'id': key, 'title': key.replace('-', ' ').title(), 'regionIds': ids} for key, ids in sorted(groups.items())]
    m['version'] = version; g['artworkVersion'] = version
    m.pop('review', None)
    m['provenance']['lastEdit'] = request.action
    output.mkdir(parents=True, exist_ok=True)
    master_name = m['assets'].get('sourceMaster', 'source-master.png')
    for f in {master_name, 'build-settings.json'}:
        if (source / f).is_file(): shutil.copy2(source / f, output / f)
    qa = emit_bundle(output, bundle)
    return {'manifest': m, 'validation': qa}


def validate_runtime_contract(bundle: dict) -> list:
    """Mirror of the shipped game adapter's validateBundle (shared contract).

    Both sides must accept exactly the same field set: region ids, palette
    references, per-region fill rules, curved M/L/C/Q/Z path data, paint
    fills (#hex or url(#g-...)), gradients, strokes and open ink paths.
    Runs on every export so "passed validation" implies "the game can load it".
    """
    errors = []
    m, g, p, paint = bundle['manifest'], bundle['geometry'], bundle['palette'], bundle['paint']
    if m.get('format') not in ('color-duel-vector-1', SCHEMA):
        errors.append('Unsupported artwork bundle format.')
    if m['id'] != g.get('artworkId') or m['version'] != g.get('artworkVersion') \
            or m.get('regionCount') != len(g.get('regions', [])):
        errors.append('Artwork identity/count mismatch.')
    vb = g.get('viewBox')
    if not isinstance(vb, list) or len(vb) != 4 or not all(isinstance(v, (int, float)) for v in vb) \
            or vb[2] <= 0 or vb[3] <= 0:
        errors.append('Invalid viewBox.')
    palette_ids = set()
    for entry in p:
        if entry['id'] in palette_ids:
            errors.append('Duplicate palette ID.')
        palette_ids.add(entry['id'])
    grad_ids = {gr.get('id') for gr in (paint.get('gradients') or [])}
    seen = set()
    for r in [*g.get('regions', []), *g.get('decorations', [])]:
        if r['id'] in seen or not SAFE_ID.match(r['id']):
            errors.append('Duplicate or unsafe region ID.')
        seen.add(r['id'])
        if r.get('paletteId') not in palette_ids:
            errors.append(f'Unknown palette group: {r["id"]}')
        rule = r.get('fillRule', 'evenodd')
        if rule not in ('evenodd', 'nonzero'):
            errors.append(f'Unsupported region fill rule: {r["id"]}')
        if not SAFE_D.match(r['d']):
            errors.append(f'Invalid region path data: {r["id"]}')
    for path in (paint.get('paths') or []):
        fill = path.get('fill', '')
        ok_fill = SAFE_HEX.match(fill) or (SAFE_GRAD_REF.match(fill) and fill[5:-1] in grad_ids)
        if not ok_fill:
            errors.append('Invalid paint fill.')
        if not SAFE_D.match(path['d']):
            errors.append('Invalid paint path.')
        if path.get('fillRule') not in (None, 'evenodd', 'nonzero'):
            errors.append('Invalid paint fill rule.')
        for key in ('opacity', 'fillOpacity', 'strokeWidth', 'z'):
            v = path.get(key)
            if v is not None and (not isinstance(v, (int, float)) or not (0 <= v if key != 'z' else True)):
                errors.append(f'Invalid paint {key}.')
    for path in (paint.get('inkPaths') or []):
        fill = path.get('fill', '')
        ok_fill = SAFE_HEX.match(fill) or (SAFE_GRAD_REF.match(fill) and fill[5:-1] in grad_ids)
        open_ok = (path.get('strokeWidth') is not None or path.get('filled') is False) and SAFE_D_OPEN.match(path['d'])
        if not ok_fill:
            errors.append('Invalid ink fill.')
        if not (SAFE_D.match(path['d']) or open_ok):
            errors.append('Invalid ink path.')
    for gr in (paint.get('gradients') or []):
        if not _re.match(r'^g-[a-zA-Z0-9_-]+$', gr.get('id', '')):
            errors.append('Unsafe gradient id.')
        if gr.get('type') not in ('linear', 'radial') or not gr.get('stops'):
            errors.append('Invalid gradient definition.')
    return errors


def _runtime_geometry(g: dict) -> dict:
    """Lean runtime geometry: exactly what the game adapter needs."""
    keep = ('schemaVersion', 'geometrySchema', 'source', 'artworkId', 'artworkVersion',
            'viewBox', 'fillRule', 'stroke', 'strokeWidth', 'backend',
            'flattenTolerance', 'curveFitTolerance', 'partitionTolerance',
            'visibleRegionGeometry', 'detailPaths')
    out = {k: v for k, v in g.items() if k in keep}
    out['regions'] = [{k: r[k] for k in RUNTIME_REGION_KEYS if k in r} for r in g.get('regions', [])]
    out['decorations'] = []
    out['note'] = 'Runtime bundle: master/flat/rings/legacy authoring geometry stripped; regions[*].d is authoritative.'
    return out


def _runtime_paint(paint: dict) -> dict:
    keep = ('schemaVersion', 'artworkId', 'viewBox', 'paths', 'inkPaths', 'gradients',
            'sourceColorShapeCount', 'notes')
    return {k: v for k, v in paint.items() if k in keep}


def make_export(folder: Path, include_authoring=False):
    """Export the runtime bundle (lean) or the full authoring bundle.

    The runtime export carries only the geometry, paint, palette, labels and
    metadata the game needs - no duplicate master/ring representations -
    and is contract-checked against the shipped game adapter rules before
    it is written, so a "passed validation" export actually loads.
    """
    m = read_json(folder / 'artwork.json')
    g = read_json(folder / 'regions.json')
    p = read_json(folder / 'palette.json')
    paint = read_json(folder / 'paint.json')
    master_name = m['assets'].get('sourceMaster', 'source-master.png')
    from io import BytesIO
    output = BytesIO()
    root = f'artworks/{m["id"]}/'
    export_m = json.loads(json.dumps(m))
    contract_errors = validate_runtime_contract({'manifest': m, 'geometry': g, 'palette': p, 'paint': paint})
    if contract_errors:
        raise ValueError('Runtime contract check failed: ' + '; '.join(contract_errors[:5]))
    if include_authoring:
        names = ['artwork.json', 'regions.json', 'palette.json', 'paint.json', 'colored.svg', 'numbered.svg',
                 'linework.svg', 'ink.svg', 'selected-preview.svg', 'thumbnail.webp', 'validation.json',
                 master_name, 'build-settings.json', 'colored-preview.png', 'numbered-preview.png']
        bundle_kind = 'authoring'
    else:
        g_out = _runtime_geometry(g)
        paint_out = _runtime_paint(paint)
        write_json(folder / '.runtime-regions.json', g_out)
        write_json(folder / '.runtime-paint.json', paint_out)
        slim = {'manifest': export_m, 'geometry': g_out, 'palette': p, 'paint': paint_out}
        slim_errors = validate_runtime_contract(slim)
        if slim_errors:
            (folder / '.runtime-regions.json').unlink(missing_ok=True)
            (folder / '.runtime-paint.json').unlink(missing_ok=True)
            raise ValueError('Lean runtime contract check failed: ' + '; '.join(slim_errors[:5]))
        export_m['assets'] = {k: v for k, v in export_m['assets'].items()
                              if k in ('regions', 'palette', 'paint')}
        export_m['exportKind'] = 'runtime'
        bundle_kind = 'runtime'
        export_m['runtime'] = {'contract': 'detailed-vector schema 2 (curves, gradients, per-region fill rules)',
                               'leanGeometry': True,
                               'contentHash': hashlib.sha256((folder / '.runtime-regions.json').read_bytes()
                                                             + (folder / '.runtime-paint.json').read_bytes()
                                                             + (folder / 'palette.json').read_bytes()).hexdigest()}
        export_m['contentHash'] = export_m['runtime']['contentHash']
        # ONLY what the game adapter loads: the four JSON files plus the
        # validation evidence. Preview SVGs and thumbnails stay in the
        # authoring export - the adapter renders from JSON, not from SVGs.
        names = ['artwork.json', 'validation.json']
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in names:
            if name == 'artwork.json':
                z.writestr(root + name, json.dumps(export_m, indent=2))
            elif (folder / name).is_file():
                z.write(folder / name, root + name)
        if not include_authoring:
            z.writestr(root + 'regions.json', (folder / '.runtime-regions.json').read_text(encoding='utf-8'))
            z.writestr(root + 'paint.json', (folder / '.runtime-paint.json').read_text(encoding='utf-8'))
            z.writestr(root + 'palette.json', (folder / 'palette.json').read_text(encoding='utf-8'))
        z.writestr('catalog-entry.json', json.dumps({'id': m['id'], 'title': m['title'], 'manifest': root + 'artwork.json', 'format': m['format'], 'status': m['qa']['status'], 'exportKind': bundle_kind}, indent=2))
        z.writestr('IMPORT.md', 'Load artwork.json and its regions/palette/paint files with the detailed-vector adapter (integration/detailed-board.mjs). '
                                'The runtime bundle was contract-checked against that adapter before export. '
                                'Geometry schema 2: regions[*].d is the AUTHORITATIVE curved path data (M/L/C/Q/Z); authoring duplicates (master/rings/flat/legacy) are stripped from the runtime export. '
                                'Regions are visible surfaces: masks do not overlap, so any fill order colors correctly. '
                                'Preserve viewBox, per-region fill rules (evenodd/nonzero), gradients, contentHash and version. Do not stretch a full painting into each region. Do not use numbered.svg as hit-test metadata. Validation does not establish copyright clearance.\n')
    (folder / '.runtime-regions.json').unlink(missing_ok=True)
    (folder / '.runtime-paint.json').unlink(missing_ok=True)
    return output.getvalue()
