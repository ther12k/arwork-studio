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

import hashlib, json, math, random, shutil, zipfile
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import cv2
from PIL import Image, ImageOps, ImageFilter
from scipy import ndimage as ndi
from skimage.segmentation import slic
from shapely import make_valid
from shapely.geometry import shape, Polygon, Point, LineString, box
from shapely.ops import unary_union, polylabel, split
from shapely.strtree import STRtree

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
EDGE_KINDS = ('artwork', 'subdivision')
BOUNDARY_STYLE_DEFAULT = {'artwork': {'stroke': INK, 'strokeWidth': 1.6},
                          'subdivision': {'stroke': '#7A8C94', 'strokeWidth': 0.85, 'dash': '3 2.2'}}
EDGE_CLASSIFY_TOL = 1.5        # px: segment midpoint distance that still counts as ON a boundary
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
    {'id': 'pen-cut-tools', 'name': 'Pen & cut region topology tools', 'kind': 'region-topology-editing',
     'paid': False, 'available': True,
     'notes': 'Cut a region along a drawn line and draw new pen regions. Artwork pen emits a paint '
              'path + masterShapeId and carves the regions underneath (works over fully covered '
              'artwork); region-only pen draws gameplay-only surfaces on uncovered canvas. Edges are '
              'reclassified as artwork vs subdivision.'},
    {'id': 'auto-subdivide', 'name': 'Deterministic organic auto-subdivide', 'kind': 'deterministic-subdivision',
     'paid': False, 'available': True,
     'notes': 'Splits oversized regions with seeded organic (sine-wiggled) cuts until the target region count; true-vector, no rasterization.'},
    {'id': 'difficulty-analyzer', 'name': 'Difficulty analyzer', 'kind': 'qa',
     'paid': False, 'available': True,
     'notes': 'Deterministic difficulty profile (region count, zoom, tiny regions, label clearance, palette ambiguity, adjacency) per revision.'},
    {'id': 'provider-vectorizer', 'name': 'External image-to-SVG provider', 'kind': 'raster-to-vector',
     'paid': True, 'available': False,
     'notes': 'Optional paid external vectorizer. Requires server-side credentials (VECTORIZER_API_KEY); disabled without them.'},
    {'id': 'provider-svg-generation', 'name': 'AI SVG generation', 'kind': 'vector-generation',
     'paid': True, 'available': False,
     'notes': 'Separate paid route: the configured chat provider drafts an SVG master from the brief. Sanitized before import.'},
    {'id': 'provider-svg-multistage', 'name': 'AI multi-stage SVG generation', 'kind': 'vector-generation',
     'paid': True, 'available': False,
     'notes': 'Paid route: strict-JSON scene plan (object bboxes, z order, fills) then one vector fragment per object, composed into a single sanitized master; next build auto-subdivides.'},
]


def write_json(path: Path, value) -> None:
    # Atomic publish (tmp + rename): concurrent readers — polling HTTP
    # requests during a running job — must never observe a half-written
    # session/project file (a truncated read surfaces as a spurious 400
    # 'Expecting value' JSON error).
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    tmp.replace(path)


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


def label_conflict(label: dict) -> bool:
    """True when a region label is unreadable (no clearance / font too small
    / text wider than the inscribed circle). Same thresholds the difficulty
    profile uses to flag 'conflict'; the auto-subdivider and the difficulty
    optimizer reject candidates that would create such labels."""
    clearance, font = float(label.get('clearance', 0.0)), float(label.get('fontSize', 0.0))
    return clearance <= 0 or font < 3.5 or clearance < font * 0.5


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
# Semantic object model (authoring layer, objects.json)
# ---------------------------------------------------------------------------
# One-way ownership contract: objects.json owns masterShapeIds; every region
# carries objectId. objects.json NEVER stores regionIds (regions are derived
# state that changes on every edit; shape ownership is the stable truth).

OBJECTS_SCHEMA_VERSION = 1
_OBJECT_ID_RE = _re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_OBJECT_STR_RE = _re.compile(r'^[^\x00-\x1f<>]{0,120}$')


def normalize_objects(raw) -> List[dict] | None:
    """Validate/normalize a parsed objects.json payload into record dicts.

    Lenient by design: unknown fields are dropped, records that are not
    dicts or lack a valid id are skipped, ids are de-duplicated. Returns
    None when there is nothing usable.
    """
    if not isinstance(raw, dict):
        return None
    items = raw.get('objects')
    if not isinstance(items, list):
        return None
    out: List[dict] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        oid = str(item.get('id') or '').strip()
        if not _OBJECT_ID_RE.match(oid) or oid in seen:
            continue
        seen.add(oid)
        rec: dict = {'id': oid}
        name = str(item.get('name') or '').strip()
        if name:
            rec['name'] = name[:80]
        for key in ('type', 'role'):
            val = str(item.get(key) or '').strip()
            if val:
                rec[key] = val[:40]
        parent = str(item.get('parentId') or '').strip()
        if parent:
            rec['parentId'] = parent[:64]
        shape_ids = [str(s) for s in (item.get('shapeIds') or [])
                     if isinstance(s, str) and s.strip()]
        if shape_ids:
            rec['shapeIds'] = list(dict.fromkeys(shape_ids))[:400]
        sub = item.get('subdivision')
        if isinstance(sub, dict):
            clean: dict = {}
            if sub.get('detailWeight') is not None:
                try:
                    w = float(sub['detailWeight'])
                    if 0 < w <= 20:
                        clean['detailWeight'] = round(w, 3)
                except (TypeError, ValueError):
                    pass
            for key in ('minRegions', 'preferredRegions', 'maxRegions'):
                if sub.get(key) is not None:
                    try:
                        clean[key] = max(0, int(sub[key]))
                    except (TypeError, ValueError):
                        pass
            if 'preserveSilhouette' in sub:
                clean['preserveSilhouette'] = bool(sub['preserveSilhouette'])
            if clean:
                rec['subdivision'] = clean
        gen = item.get('generation')
        if isinstance(gen, dict):
            g = {k: gen[k] for k in ('prompt', 'provider') if isinstance(gen.get(k), str)}
            g['locked'] = bool(gen.get('locked', False))
            rec['generation'] = g
        out.append(rec)
    return out or None


def _objects_from_shapes(shapes: list) -> List[dict] | None:
    """Build object records from sanitized master shapes carrying objectRef
    (data-cd-object groups). Nested groups record the enclosing object as
    parentId. Shapes hidden behind opaque art are excluded consistently with
    the paint layer, so edit-time ownership sync stays stable. Returns None
    when the master has no object groups."""
    grouped: dict = {}
    for s in shapes:
        if s.get('hidden'):
            continue
        ref = s.get('objectRef')
        if not ref:
            continue
        rec = grouped.setdefault(ref, {'id': ref, 'shapeIds': [], 'name': None, 'parent': None})
        rec['shapeIds'].append(s['id'])
        if not rec['name'] and s.get('objectName'):
            rec['name'] = s['objectName']
        if not rec['parent'] and s.get('objectParent'):
            rec['parent'] = s['objectParent']
    if not grouped:
        return None
    records = []
    for oid in sorted(grouped):
        rec = grouped[oid]
        out: dict = {'id': oid, 'name': rec['name'] or oid.replace('-', ' ').title(),
                     'shapeIds': sorted(rec['shapeIds'])}
        if rec['parent'] and rec['parent'] != oid and rec['parent'] in grouped:
            out['parentId'] = rec['parent']
        records.append(out)
    return normalize_objects({'objects': records})


def _sync_objects_from_regions(bundle: dict) -> List[dict] | None:
    """Reconcile object records with region truth and live paint/ink shapes after an edit.

    Ownership contract:
    - Object shapeIds = (existing object's live shapeIds) | (masterShapeIds from regions for this object).
      This ensures decorative/shading shapes and ink paths belonging to an object
      are NOT orphaned when a region is cut, merged, or modified.
    - If a region is explicitly reassigned to another object (e.g. 'group' action),
      its masterShapeId moves to the new object if no other region of the old
      object references it.
    - Objects are kept if they have active regions/decorations, OR still own live
      paint/ink shapes, OR are parent to another live object.
    - Preserves authoring metadata (name, type, role, parentId, subdivision, generation).
    - Returns None for fully unassigned art with no object records.
    """
    regions = bundle['geometry']['regions']
    decorations = bundle['geometry'].get('decorations', [])
    all_regs = regions + decorations
    paint = bundle.get('paint') or {}
    live_shapes = {p['shapeId'] for p in (paint.get('paths') or []) + (paint.get('inkPaths') or [])
                   if p.get('shapeId')}
    existing = {o['id']: o for o in (bundle.get('objects') or [])}
    pending_names = bundle.pop('_pending_object_names', None) or {}

    by_obj: dict = {}
    for r in all_regs:
        oid = r.get('objectId') or 'unassigned'
        if oid == 'unassigned':
            continue
        by_obj.setdefault(oid, []).append(r)

    candidate_ids = set(by_obj.keys())
    for oid, o in existing.items():
        if set(o.get('shapeIds') or []) & live_shapes:
            candidate_ids.add(oid)

    # Keep parent objects if any candidate references them as parentId
    changed = True
    while changed:
        changed = False
        for oid in list(candidate_ids):
            pid = existing.get(oid, {}).get('parentId')
            if pid and pid not in candidate_ids and pid in existing:
                candidate_ids.add(pid)
                changed = True

    if not candidate_ids:
        return None

    out = []
    for oid in sorted(candidate_ids):
        rec = dict(existing.get(oid) or {})
        rec['id'] = oid
        rec['name'] = pending_names.get(oid) or rec.get('name') or oid.replace('-', ' ').title()

        region_sids = {r['masterShapeId'] for r in by_obj.get(oid, []) if r.get('masterShapeId')}
        other_region_sids = {r['masterShapeId'] for other_id, regs in by_obj.items()
                             if other_id != oid for r in regs if r.get('masterShapeId')}
        transferred = other_region_sids - region_sids
        existing_sids = (set(existing.get(oid, {}).get('shapeIds') or []) & live_shapes) - transferred
        combined_sids = sorted((region_sids | existing_sids) & live_shapes)
        rec['shapeIds'] = combined_sids

        parent = rec.get('parentId')
        if parent and (parent not in candidate_ids or parent == oid):
            rec.pop('parentId', None)

        out.append(rec)

    parent_ids = {rec['parentId'] for rec in out if rec.get('parentId')}
    final_out = [
        rec for rec in out
        if rec['shapeIds'] or rec['id'] in by_obj or rec['id'] in parent_ids
    ]

    return final_out or None


def _object_qa(bundle: dict) -> tuple:
    """Semantic object checks (authoring layer): returns (issues, summary).

    Non-fatal by contract: object metadata problems never fail geometry
    validation, but every one of these conditions is REPORTED so orphaned
    semantics cannot rot silently.
    """
    objects = bundle.get('objects') or []
    g = bundle['geometry']
    paint = bundle['paint']
    shape_ids = {p['shapeId'] for p in (paint.get('paths') or []) + (paint.get('inkPaths') or [])
                 if p.get('shapeId')}
    regions_by_obj: dict = {}
    for r in g['regions']:
        regions_by_obj.setdefault(r.get('objectId') or 'unassigned', []).append(r)
    known = {o['id'] for o in objects}
    issues: list = []
    for o in objects:
        missing = [s for s in (o.get('shapeIds') or []) if s not in shape_ids]
        if missing:
            issues.append(f'Object {o["id"]} references missing shapes: ' + ', '.join(missing[:3])
                          + ('…' if len(missing) > 3 else ''))
        parent = o.get('parentId')
        if parent and parent not in known:
            issues.append(f'Object {o["id"]} references invalid parent {parent}.')
        if parent == o['id']:
            issues.append(f'Object {o["id"]} is its own parent.')
        count = len(regions_by_obj.get(o['id'], []))
        sub = o.get('subdivision') or {}
        wanted = int(sub.get('minRegions') or 0)
        if wanted and count < wanted:
            issues.append(f'Object {o["id"]} budget impossible: {count} regions < minRegions {wanted} '
                          '(the object is too small for its requested region budget).')
        if not count and not o.get('shapeIds'):
            issues.append(f'Object {o["id"]} has zero geometry (no regions, no shapes).')
    assigned = sum(len(v) for k, v in regions_by_obj.items() if k != 'unassigned')
    unassigned = len(regions_by_obj.get('unassigned', []))
    for oid in sorted(regions_by_obj):
        if oid != 'unassigned' and oid not in known:
            issues.append(f'{len(regions_by_obj[oid])} regions reference missing object record {oid}.')
    owned = {s for o in objects for s in (o.get('shapeIds') or [])}
    orphan = sorted(shape_ids - owned)
    if unassigned:
        issues.append(f'{unassigned} playable regions have no object (unassigned).')
    if orphan:
        issues.append(f'{len(orphan)} paint shapes belong to no object.')
    summary = {'schemaVersion': OBJECTS_SCHEMA_VERSION, 'count': len(objects),
               'assignedRegions': assigned, 'unassignedRegions': unassigned,
               'orphanShapes': len(orphan), 'issues': issues,
               'note': 'objects.json is authoring metadata: objects own shapeIds, regions carry objectId. '
                       'The runtime export ignores it; authoring exports include it.'}
    return issues, summary


# ---------------------------------------------------------------------------
# Cut / pen geometry engine (stage-2 region topology tools)
# ---------------------------------------------------------------------------

def flatten_d(d: str, tol: float = FLATTEN_TOLERANCE) -> List[List[Tuple[float, float]]]:
    """Flatten an M/L/C/Q/Z path string into polylines (one per subpath).

    Open paths stay open (cut lines); a Z closes the ring back to its start
    (rings are stored without the duplicated closing vertex — Polygon()
    closes implicitly, which auto-closes pen loops missing their Z).
    """
    cmds = parse_path(d)
    out: List[List[Tuple[float, float]]] = []
    for sub in subpaths_of(cmds):
        pts = flatten_subpath(sub, tol)
        if len(pts) >= 2:
            out.append([(float(x), float(y)) for x, y in pts])
    return out


def polyline_d(coords) -> str:
    """M/L path string for a coordinate run (open; no closing Z)."""
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) < 2:
        return ''
    return 'M ' + ' L '.join(f'{number(x)},{number(y)}' for x, y in pts)


def _polys(geom) -> List[Polygon]:
    """Valid, non-empty polygon parts of any geometry (split results)."""
    out: List[Polygon] = []
    for part in polygon_parts(geom):
        if part is None or part.is_empty or part.area <= 0:
            continue
        if not part.is_valid:
            part = make_valid(part)
        if part.geom_type == 'Polygon':
            out.append(part)
        else:
            out.extend(p for p in polygon_parts(part) if p.geom_type == 'Polygon' and p.area > 0)
    return [p for p in out if p.is_valid and p.area > 0]


def cut_polygon(poly: Polygon, polyline) -> List[Polygon]:
    """Split a polygon with an open polyline (the cut tool's engine).

    The polyline is extended beyond the polygon's bbox on both ends so a
    crossing line always severs the polygon completely. ``split`` handles
    the clean case; carving a hair-thin buffered corridor and taking the
    difference is the robust fallback.
    """
    pts = [(float(x), float(y)) for x, y in polyline]
    if len(pts) < 2:
        return []
    if poly.is_empty or poly.area <= 0:
        return []
    if not poly.is_valid:
        poly = make_valid(poly)
    if poly.geom_type != 'Polygon':
        base = [p for p in polygon_parts(poly) if p.area > 0]
        if not base:
            return []
        poly = base[0] if len(base) == 1 else _safe_union(base)
    x0, y0, x1, y1 = poly.bounds
    pad = math.hypot(x1 - x0, y1 - y0) + 8.0

    def _ext(a, b):
        dx, dy = a[0] - b[0], a[1] - b[1]
        n = math.hypot(dx, dy)
        return (a[0] + dx / n * pad, a[1] + dy / n * pad) if n > 1e-12 else a

    line = LineString([_ext(pts[0], pts[1]), *pts, _ext(pts[-1], pts[-2])])
    pieces: List[Polygon] = []
    try:
        pieces = _polys(split(poly, line))
    except Exception:
        pieces = []
    if len(pieces) < 2:
        try:
            carved = _polys(make_valid(poly.difference(line.buffer(0.01))))
        except Exception:
            carved = []
        if len(carved) >= 2:
            pieces = carved
    return pieces


def _ref_lines(refs) -> List[LineString]:
    lines: List[LineString] = []
    for ref in refs or []:
        if isinstance(ref, LineString):
            lines.append(ref)
        elif hasattr(ref, 'geom_type') and not isinstance(ref, (Polygon, Point)):
            for part in getattr(ref, 'geoms', [ref]):
                if isinstance(part, LineString) and len(part.coords) >= 2:
                    lines.append(part)
        elif ref and len(ref) >= 2 and not hasattr(ref, 'geom_type'):
            lines.append(LineString([(float(x), float(y)) for x, y in ref]))
    return [l for l in lines if not l.is_empty]


def classify_outline(outline_coords, ref_lines, tol: float = EDGE_CLASSIFY_TOL,
                     near_kind: str = 'subdivision', far_kind: str = 'artwork') -> List[Tuple[str, list]]:
    """Split an outline into consecutive runs by proximity to reference lines.

    Walks consecutive outline point pairs, classifies each segment by the
    minimum distance of its midpoint to any reference LineString, then
    merges consecutive same-kind runs. Returns [(kind, coords-run)].
    """
    coords = [(float(x), float(y)) for x, y in outline_coords]
    if len(coords) < 2:
        return []
    lines = _ref_lines(ref_lines)
    if not lines:
        return [(far_kind, coords)]
    tree = STRtree(lines)
    runs: List[list] = []
    for i in range(len(coords) - 1):
        p = Point((coords[i][0] + coords[i + 1][0]) / 2.0, (coords[i][1] + coords[i + 1][1]) / 2.0)
        j = tree.nearest(p)
        dist = lines[int(j)].distance(p) if j is not None else 1e9
        kind = near_kind if dist <= tol else far_kind
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(coords[i + 1])
        else:
            runs.append([kind, [coords[i], coords[i + 1]]])
    return [(kind, pts) for kind, pts in runs]


class _RegionIndex:
    """Region polygons + STRtree for geometric left/right neighbor probing."""

    def __init__(self, regions):
        self.ids = [r['id'] for r in regions]
        self.polys = []
        for r in regions:
            p = region_polygon(r)
            if p.is_empty or p.area <= 0:
                p = make_valid(p)
            self.polys.append(p)
        self.tree = STRtree(self.polys) if self.polys else None

    def owner(self, x: float, y: float, max_dist: float = 0.75):
        """Region id owning a probe point (covers first, nearest fallback)."""
        if self.tree is None:
            return None
        p = Point(x, y)
        idxs = [int(i) for i in np.atleast_1d(self.tree.query(p.buffer(max_dist + 0.25)))]
        if not idxs:
            return None
        covering = [i for i in idxs if self.polys[i].covers(p)]
        if len(covering) == 1:
            return self.ids[covering[0]]
        if covering:
            return None                     # probe sits on a shared boundary
        best, best_d = None, max_dist
        for i in idxs:
            d = self.polys[i].distance(p)
            if d <= best_d:
                best, best_d = self.ids[i], d
        return best


def _edge_sides(p0, p1, index: '_RegionIndex', offset: float = 1.2):
    """Probe both perpendicular sides of a segment for owning region ids."""
    dx, dy = float(p1[0]) - float(p0[0]), float(p1[1]) - float(p0[1])
    n = math.hypot(dx, dy)
    if n < 1e-9 or index is None:
        return None, None
    px, py = -dy / n, dx / n
    mx, my = (float(p0[0]) + float(p1[0])) / 2.0, (float(p0[1]) + float(p1[1])) / 2.0
    left = index.owner(mx + offset * px, my + offset * py)
    right = index.owner(mx - offset * px, my - offset * py)
    return left, right


def _next_edge_id(edges: list) -> str:
    n = 0
    for e in edges:
        m = _re.match(r'^e-(\d+)$', str(e.get('id', '')))
        if m:
            n = max(n, int(m.group(1)))
    return f'e-{n + 1:04d}'


def _emit_edge(edges: list, coords, kind: str, left, right, index: '_RegionIndex' | None,
               self_id: str | None = None) -> None:
    """Append one EdgeEntry for a coordinate run (neighbors probed if unset).

    The probe uses the run's LONGEST segment (a real boundary segment), never
    the start->end chord (a chord can cut across the region and return
    neighbors that do not border the boundary at all). ``self_id`` (the region
    whose outline produced the run) is guaranteed one side when given.
    """
    d = polyline_d(coords)
    if not d:
        return
    if left is None and right is None and index is not None and len(coords) >= 2:
        pts = [(float(x), float(y)) for x, y in coords]
        seg = max(range(len(pts) - 1),
                  key=lambda i: math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
        left, right = _edge_sides(pts[seg], pts[seg + 1], index)
    if self_id is not None and self_id not in (left, right):
        # The run lies on self's boundary: keep the probed neighbor on one side.
        other = left if left not in (None, self_id) else right
        left, right = self_id, (other if other not in (None, self_id) else None)
    edges.append({'id': _next_edge_id(edges), 'd': d, 'kind': kind,
                  'leftRegion': left, 'rightRegion': right})


def _emit_classified_edges(edges: list, poly: Polygon, ref_lines, near_kind: str,
                           far_kind: str, index: '_RegionIndex',
                           prior_art_lines: List[LineString] | None = None,
                           self_id: str | None = None) -> None:
    """Classify a polygon's rings against reference lines and emit edges.

    ``prior_art_lines`` (cut action): runs classified near the cut keep the
    ARTWORK kind when they hug a prior artwork boundary of the target.
    ``self_id``: the owning region id (cut piece / pen region); contract:
    left/right = adjacent new region ids (or null at the canvas boundary).
    """
    if poly is None or poly.is_empty:
        return
    rings = [list(poly.exterior.coords)] + [list(ring.coords) for ring in poly.interiors]
    for ring in rings:
        if len(ring) < 3:
            continue
        runs = classify_outline(ring, ref_lines, EDGE_CLASSIFY_TOL, near_kind, far_kind)
        if near_kind == 'subdivision' and prior_art_lines:
            merged: List[list] = []
            for kind, pts in runs:
                if kind != 'subdivision' or len(pts) < 3:
                    merged.append([kind, pts])
                    continue
                for k2, pts2 in classify_outline(pts, prior_art_lines, EDGE_CLASSIFY_TOL,
                                                 'artwork', 'subdivision'):
                    if merged and merged[-1][0] == k2 and merged[-1][1][-1] == pts2[0]:
                        merged[-1][1].extend(pts2[1:])
                    else:
                        merged.append([k2, pts2])
            runs = [(k, p) for k, p in merged]
        for kind, pts in runs:
            if len(pts) >= 2:
                _emit_edge(edges, pts, kind, None, None, index, self_id)


def _organic_cut_line(poly: Polygon, seed: int) -> List[Tuple[float, float]]:
    """Deterministic hand-cut-looking polyline through a polygon's centroid.

    A line roughly perpendicular to the major axis (minimum rotated rect),
    perturbed with a small sine wiggle (amplitude 2-4% of the extent, >= 8
    samples) so boundaries look hand-cut instead of mechanical.
    """
    rng = random.Random(seed)
    cx, cy = poly.centroid.x, poly.centroid.y
    x0, y0, x1, y1 = poly.bounds
    long_v, length = (x1 - x0, 0.0), x1 - x0
    try:
        rect = poly.minimum_rotated_rectangle
        coords = list(rect.exterior.coords) if rect.geom_type == 'Polygon' else []
    except Exception:
        coords = []
    if len(coords) >= 4:
        best = None
        for i in range(len(coords) - 1):
            ax, ay = coords[i]
            bx, by = coords[i + 1]
            L = math.hypot(bx - ax, by - ay)
            if best is None or L > best[0]:
                best = (L, (bx - ax, by - ay))
        if best and best[0] > 0:
            length, long_v = best
    n = math.hypot(*long_v) or 1.0
    ux, uy = long_v[0] / n, long_v[1] / n          # major axis (wiggle direction)
    vx, vy = -uy, ux                               # perpendicular (cut direction)
    extent = max(x1 - x0, y1 - y0, 1.0)
    reach = extent * 1.3 + 6.0
    amp = extent * (0.02 + 0.02 * rng.random())    # 2-4% of the region extent
    phase = rng.uniform(0, math.tau)
    waves = rng.uniform(1.5, 2.8)
    samples = max(8, min(24, int(extent / 8) + 8))
    pts = []
    for i in range(samples + 1):
        t = -1.0 + 2.0 * i / samples
        w = amp * math.sin(phase + waves * math.pi * t)
        pts.append((cx + t * reach * vx + w * ux, cy + t * reach * vy + w * uy))
    return pts


def _object_region_budgets(regions: list, objects: list | None, target: int) -> dict:
    """Per-object region budgets for semantic subdivision.

    raw budget = area share x detailWeight x shape-complexity share, then
    clamped to [minRegions, maxRegions] and largest-remainder normalized so
    budgets sum to the target exactly (or as close as the clamps allow).
    Regions without a record share the 'unassigned' pseudo-object with
    weight 1 and a [0, target] clamp band. Deterministic.
    """
    by_obj: dict = defaultdict(float)
    for r in regions:
        by_obj[r.get('objectId') or 'unassigned'] += max(0.0, float(r.get('area', 0.0)))
    ids = sorted(by_obj)
    if not objects and len(ids) == 1:
        return {ids[0]: target}
    recs = {o['id']: o for o in (objects or [])}
    total_area = sum(by_obj.values()) or 1.0
    total_shapes = 0
    shape_counts: dict = {}
    for oid in ids:
        rec = recs.get(oid) or {}
        count = len(rec.get('shapeIds') or [])
        shape_counts[oid] = count
        total_shapes += max(1, count)
    raw: dict = {}
    lo: dict = {}
    hi: dict = {}
    frac: dict = {}
    for oid in ids:
        rec = recs.get(oid) or {}
        sub = rec.get('subdivision') or {}
        weight = float(sub.get('detailWeight') or 1.0)
        # mean-preserving complexity share: an object with the average shape
        # count gets factor 1; more planned shapes => more detail regions.
        complexity = (max(1, shape_counts[oid]) / max(1, total_shapes)) * len(ids)
        area_share = by_obj[oid] / total_area
        raw[oid] = max(0.0, area_share * weight * complexity)
        lo[oid] = max(0, int(sub.get('minRegions') or 0))
        # A contradictory pair (minRegions > maxRegions) intentionally honours
        # maxRegions for allocation and leaves minRegions as the QA threshold:
        # the budget then reports 'impossible' instead of exploding the object
        # into microscopic regions.
        hi[oid] = max(1, min(int(sub.get('maxRegions') or target), target))
        frac[oid] = raw[oid]
    raw_sum = sum(raw.values()) or 1.0
    budgets = {}
    for oid in ids:
        want = frac[oid] / raw_sum * target
        budgets[oid] = int(min(max(math.floor(want), lo[oid]), hi[oid]))
    # largest-remainder redistribution toward the exact target, honouring clamps
    order = sorted(ids, key=lambda o: (-(frac[o] / raw_sum * target - math.floor(frac[o] / raw_sum * target)), o))
    delta = target - sum(budgets.values())
    while delta > 0:
        progressed = False
        for oid in order:
            if delta <= 0:
                break
            if budgets[oid] < hi[oid]:
                budgets[oid] += 1
                delta -= 1
                progressed = True
        if not progressed:
            break
    while delta < 0:
        progressed = False
        for oid in reversed(order):
            if delta >= 0:
                break
            if budgets[oid] > max(1, lo[oid]):
                budgets[oid] -= 1
                delta += 1
                progressed = True
        if not progressed:
            break
    return budgets


def _auto_subdivide(regions: list, settings: BuildSettings, edges: list,
                    objects: list | None = None,
                    progress: Callable = lambda *_: None,
                    fit: bool = True) -> int:
    """Deterministically split oversized regions toward the target count.

    Semantic phase: each object gets a region budget (area x detailWeight x
    complexity, clamped to [minRegions, maxRegions], normalized to the
    target). Splits are spent on objects still under their budget first
    (largest slack first, largest region within it), then — when every
    budget is met but the global target is not — on eligible objects below
    their maxRegions cap, largest region first. While under target and the
    candidate region is >= 2x the minimum playable size, split it with an
    organic cut; both pieces are refit and the new shared boundary is
    emitted as SUBDIVISION edges. A cut is skipped when either piece would
    carry an unreadable label or fall below the tap minimum (the candidate
    is parked and the next-largest region is tried). Deterministic (seeded
    by region id hash); loop bound 1200 splits.

    ``fit=False`` emits exact pixel-edge masters instead of curve-refitted
    ones (difficulty optimizer: gameplay-only edits on raster bundles must
    keep the partition exactly watertight for the raster roundtrip QA).
    """
    target = int(settings.target_regions)
    min_px = float(settings.min_region_pixels)
    budgets = _object_region_budgets(regions, objects, target)
    skip: set = set()
    splits = 0
    while len(regions) < target and splits < 1200:
        pool = [r for r in regions if r['id'] not in skip]
        if not pool:
            break
        counts: dict = defaultdict(int)
        for r in pool:
            counts[r.get('objectId') or 'unassigned'] += 1
        largest = None
        # 1) objects still under their semantic budget: largest slack first
        for oid in sorted(budgets, key=lambda o: (-(budgets[o] - counts.get(o, 0)), o)):
            if counts.get(oid, 0) >= budgets[oid]:
                continue
            members = [r for r in pool if (r.get('objectId') or 'unassigned') == oid]
            if not members:
                continue
            cand = max(members, key=lambda r: r['area'])
            if cand['area'] >= 2 * min_px:
                largest = cand
                break
        # 2) budgets met but target not reached: grow objects below their
        #    maxRegions cap (leftover redistribution), largest region first.
        if largest is None:
            recs = {o['id']: o for o in (objects or [])}
            eligible = []
            for oid in sorted(budgets):
                if counts.get(oid, 0) >= budgets[oid] and counts.get(oid, 0) > 0:
                    hi = max(1, min(int((recs.get(oid) or {}).get('subdivision', {}).get('maxRegions') or target), target))
                    if counts[oid] >= hi:
                        continue
                members = [r for r in pool if (r.get('objectId') or 'unassigned') == oid]
                if members:
                    eligible.append(max(members, key=lambda r: r['area']))
            if not eligible:
                break
            largest = max(eligible, key=lambda r: r['area'])
        if largest['area'] < 2 * min_px:
            break
        seed = int(hashlib.sha256(largest['id'].encode()).hexdigest()[:8], 16)
        poly = region_polygon(largest)
        if poly.is_empty or poly.area <= 0:
            skip.add(largest['id'])
            continue
        if not poly.is_valid:
            poly = make_valid(poly)
        if poly.geom_type != 'Polygon':
            parts = [p for p in polygon_parts(poly) if p.area > 0]
            if not parts:
                skip.add(largest['id'])
                continue
            poly = parts[0] if len(parts) == 1 else _safe_union(parts)
        line = _organic_cut_line(poly, seed)
        pieces = cut_polygon(poly, line)
        if len(pieces) < 2 or any(p.area < min_px for p in pieces):
            skip.add(largest['id'])       # this cut would create untappable pieces
            continue
        piece_labels = [make_label(p, largest['paletteId']) for p in pieces]
        if any(label_conflict(lab) for lab in piece_labels):
            skip.add(largest['id'])       # this cut would create unreadable labels
            continue
        regions.remove(largest)
        new_ids = []
        for idx, piece in enumerate(pieces):
            rid = 'r-d-' + hashlib.sha256((largest['id'] + str(seed) + str(idx)).encode()).hexdigest()[:12]
            reg = pack_region(piece, rid, largest['paletteId'], largest['objectId'],
                              label=piece_labels[idx], source='subdivision-split',
                              fit_tolerance=0.0 if not fit else float(settings.curve_tolerance),
                              fit=fit)
            if largest.get('masterShapeId'):
                reg['masterShapeId'] = largest['masterShapeId']
            regions.append(reg)
            new_ids.append(rid)
        # The new boundary is the cut line clipped to the region: subdivision.
        left, right = (new_ids[0], new_ids[1]) if len(new_ids) >= 2 else (None, None)
        try:
            shared = LineString(line).intersection(poly)
        except Exception:
            shared = None
        segs = [shared] if isinstance(shared, LineString) else list(getattr(shared, 'geoms', []) or [])
        for seg in segs:
            if isinstance(seg, LineString) and len(seg.coords) >= 2:
                _emit_edge(edges, list(seg.coords), 'subdivision', left, right, None)
        splits += 1
        if splits % 25 == 0:
            progress(.58, f'Subdividing large regions: {len(regions)}/{target} tap targets')
    # Re-splitting removes earlier piece ids: drop their now-stale edges.
    live = {r['id'] for r in regions}
    edges[:] = [e for e in edges
                if all(v is None or v in live for v in (e.get('leftRegion'), e.get('rightRegion')))]
    return splits


def _emit_master_edges(regions: list, doc, edges: list) -> None:
    """Emit EdgeEntries for compiled SVG-master regions (contract 1: edges).

    Verbatim regions keep their own master path as ONE artwork edge (their
    boundary IS the master path). Derived regions (visible-surface refits,
    subdivision pieces) get their outline classified against the union of
    master-shape boundaries: within ~1.5px = artwork, else subdivision.
    """
    ref_lines: List[LineString] = []
    for s in doc.shapes:
        for ring in s.get('rings') or []:
            if ring and len(ring) >= 2:
                ref_lines.append(LineString([(float(x), float(y)) for x, y in ring]))
    if not ref_lines:
        return
    index = _RegionIndex(regions)
    for r in regions:
        if r.get('master', {}).get('source') == 'svg-master-import':
            edges.append({'id': _next_edge_id(edges), 'd': r['d'], 'kind': 'artwork',
                          'leftRegion': r['id'], 'rightRegion': _outside_neighbor(r, index)})
            continue
        poly = region_polygon(r)
        if poly is None or poly.is_empty:
            continue
        rings = [list(poly.exterior.coords)] + [list(ring.coords) for ring in poly.interiors]
        for ring in rings:
            if len(ring) < 3:
                continue
            for kind, pts in classify_outline(ring, ref_lines, EDGE_CLASSIFY_TOL, 'artwork', 'subdivision'):
                if len(pts) >= 2:
                    _emit_edge(edges, pts, kind, None, None, index, r['id'])


def _outside_neighbor(r: dict, index: '_RegionIndex', samples: int = 10):
    """Consensus non-self neighbour of a region's own outline (or null)."""
    rings = r.get('flat', {}).get('rings') or r.get('rings') or []
    ring = rings[0] if rings else []
    if len(ring) < 3 or index.tree is None:
        return None
    counts: Dict[str, int] = {}
    n = len(ring)
    step = max(1, (n - 1) // samples)
    for k in range(0, n - 1, step):
        p0, p1 = ring[k], ring[(k + 1) % n]
        left, right = _edge_sides(p0, p1, index)
        for owner in (left, right):
            if owner and owner != r['id']:
                counts[owner] = counts.get(owner, 0) + 1
    if not counts:
        return None
    best = max(sorted(counts.items()), key=lambda kv: kv[1])
    return best[0] if best[1] >= 2 or len(counts) == 1 else None


def _prune_edges(g: dict, removed_ids: set) -> None:
    """Drop edge entries whose left/right reference deleted region ids."""
    edges = g.get('edges')
    if not edges or not removed_ids:
        return
    g['edges'] = [e for e in edges
                  if e.get('leftRegion') not in removed_ids and e.get('rightRegion') not in removed_ids]


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
    if len(labs) < 2:
        # Single-candidate input (e.g. a solid-color image): cv2.kmeans cannot
        # run on one sample — the palette IS that color, no clustering.
        centers = labs.astype(np.float32)
        indexes = np.zeros(len(labs), dtype=np.int32)
    else:
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
    body = '<g data-layer="paint">' + ''.join(_paint_element(p) for p in _ordered_paint_entries(paint)) + '</g>'
    return defs + body


def svg_ink(paint):
    parts = [_paint_element(p) for p in (paint.get('inkPaths') or [])]
    return '<g data-layer="ink">' + ''.join(parts) + '</g>'


def _edges_overlay(g: dict) -> str:
    """Semantic boundary overlay for the static exports (linework / numbered).

    Mirrors the runtime board's edges layer exactly: artwork edges render
    solid INK (1.6), subdivision edges render light and dashed (0.85,
    '3 2.2'), with optional ``boundaryStyle`` overrides per kind. Returns ''
    for legacy bundles without edges (callers fall back to the
    outline-per-region style).
    """
    edges = g.get('edges') or []
    if not edges:
        return ''
    style = g.get('boundaryStyle') or {}
    runs = []
    for kind in EDGE_KINDS:
        ds = [e['d'] for e in edges if e.get('kind') == kind]
        if not ds:
            continue
        base = dict(BOUNDARY_STYLE_DEFAULT[kind])
        custom = style.get(kind) or {}
        for key in ('stroke', 'strokeWidth', 'dash'):
            if custom.get(key) is not None:
                base[key] = custom[key]
        dash = f' stroke-dasharray="{_xa(str(base["dash"]))}"' if base.get('dash') else ''
        runs.append(f'<g fill="none" stroke="{_xa(str(base["stroke"]))}" '
                    f'stroke-width="{number(float(base["strokeWidth"]))}" '
                    f'stroke-linecap="round" stroke-linejoin="round"{dash} data-edge-kind="{kind}">'
                    + ''.join(f'<path d="{_xa(d)}"/>' for d in ds) + '</g>')
    return '<g data-layer="edges">' + ''.join(runs) + '</g>'


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
        # Sub-pixel boundary seams scale with board density: a densely
        # subdivided gameplay board legitimately shows more of the same
        # sub-tolerance boundary pixels compile output shows (missing REGIONS
        # would be thousands of pixels, not tens).
        pixel_allowance = max(8, len(regs) // 50)
        if empty_pixels > pixel_allowance:
            errors.append(f'Raster roundtrip left {empty_pixels} uncovered pixels '
                          f'(allowance {pixel_allowance}).')
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
    # P0.2 answer-key integrity: each playable region's number-group swatch
    # must match the painted appearance of its source shape. Pen / recolor /
    # AI edits can silently break this; QA surfaces it explicitly.
    color_checked, color_conflicts = _color_answer_conflicts(bundle)
    if color_conflicts:
        warnings.append(f'Color-answer consistency: {len(color_conflicts)} region(s) painted a color their '
                        f'number group does not show (pen/recolor edits): ' + ', '.join(color_conflicts[:5]))
    object_issues, object_summary = _object_qa(bundle)
    warnings.extend(object_issues)
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
        'colorAnswerConsistency': {'checked': color_checked, 'conflictCount': len(color_conflicts),
                                   'conflictRegionIds': color_conflicts[:20],
                                   'note': 'regions whose number-group swatch matches their painted appearance (answer-key integrity)'},
        'objects': object_summary,
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
                      'partition union/overlap at flatten tolerance, raster coverage, hit-test probe alignment, curve statistics, color-answer consistency, '
                      'semantic object integrity (orphan shapes/regions, missing shapeIds, invalid parents, zero-geometry objects, impossible budgets). '
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
# Difficulty profile (contract 5: deterministic analyzer)
# ---------------------------------------------------------------------------

def _hex_rgb(hx: str):
    return tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))


def difficulty_profile(bundle: dict, playtests: list | None = None) -> dict:
    """Deterministic difficulty profile of a compiled bundle (contract 5).

    Weighted score 0-100; rating easy <25 <= medium <50 <= hard <75 <= master.
    ``playtests`` (stage-3 contract B): recorded play-test runs. Only PUZZLE
    runs (mode number/memory/duel; legacy entries without a mode count as
    number) calibrate the score - completed puzzle runs blend it by completion
    pace (±10% band) and drive ``difficultyValidatedByPlaytest``. Free-color
    runs measure engagement/interaction, not puzzle difficulty: they surface
    as ``freePlayCount`` / ``freeMedianSeconds`` metrics and are NEVER blended
    into the score. Time/mistake metrics come from the completed puzzle runs
    when at least one exists (a completion time is only meaningful when the
    board was filled); otherwise from all recorded puzzle runs (partial-run
    data, no blend).
    """
    g, p = bundle['geometry'], bundle['palette']
    regs = g.get('regions') or []
    w, h = float(g['viewBox'][2]), float(g['viewBox'][3])
    count = len(regs)
    areas = [float(r['area']) for r in regs] or [1.0]
    median = float(np.median(areas))
    tiny = sum(1 for a in areas if a < 400.0 or a < 0.4 * median)
    tiny_pct = tiny / max(1, count)
    # requiredZoom: worst-case zoom for a 44px touch target from a fit viewport
    # (nominal 380px board width); inscribed-diameter proxy 2*sqrt(area/pi).
    scale = 380.0 / max(1.0, w)
    worst = min(2.0 * math.sqrt(max(a, 1e-6) / math.pi) for a in areas)
    required_zoom = 44.0 / max(1e-6, worst * scale)
    # label clearance from existing label data
    conflicts = tight = 0
    for r in regs:
        lab = r.get('label') or {}
        clearance, font = float(lab.get('clearance', 0.0)), float(lab.get('fontSize', 0.0))
        if clearance <= 0 or font < 3.5 or clearance < font * 0.5:
            conflicts += 1
        elif clearance < font * 1.2:
            tight += 1
    if conflicts:
        label_clearance = 'conflict'
    elif tight > 0.2 * count:
        label_clearance = 'tight'
    else:
        label_clearance = 'ok'
    # palette ambiguity: closest RGB pair distance + group count
    rgbs = [_hex_rgb(entry['hex']) for entry in p if SAFE_HEX.match(entry.get('hex', ''))]
    closest = 442.0
    for i in range(len(rgbs)):
        for j in range(i + 1, len(rgbs)):
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(rgbs[i], rgbs[j])))
            closest = min(closest, d)
    if len(rgbs) < 2:
        ambiguity = 'low'
    elif closest < 45:
        ambiguity = 'high'
    elif closest < 90 or len(rgbs) >= 28:
        ambiguity = 'medium'
    else:
        ambiguity = 'low'
    # adjacency (cap: sample the largest 300 regions when huge)
    sample = regs if count <= 300 else sorted(regs, key=lambda r: -r['area'])[:300]
    polys = [region_polygon(r) for r in sample]
    polys = [q for q in polys if not q.is_empty and q.area > 0]
    degrees = 0.0
    if len(polys) > 1:
        tree = STRtree(polys)
        for i, q in enumerate(polys):
            for j in np.atleast_1d(tree.query(q.buffer(0.5))):
                if int(j) != i and q.touches(polys[int(j)]):
                    degrees += 1
        degrees /= len(polys)
    edges = g.get('edges') or []
    sub_edges = sum(1 for e in edges if e.get('kind') == 'subdivision')
    density = count / max(1e-6, w * h / 10000.0)
    metrics = {
        'regionCount': count,
        'medianRegionArea': round(median, 2),
        'tinyRegionPct': round(tiny_pct, 4),
        'requiredZoom': round(min(required_zoom, 99.0), 2),
        'labelClearance': label_clearance,
        'paletteAmbiguity': ambiguity,
        'paletteGroups': len(p),
        'avgNeighbors': round(degrees, 3),
        'subdivisionEdges': sub_edges,
        'objectDensity': round(density, 3),
    }
    score = (min(1.0, count / 400.0) * 22
             + min(1.0, tiny_pct / 0.5) * 15
             + min(1.0, required_zoom / 8.0) * 21
             + {'ok': 0.0, 'tight': 0.5, 'conflict': 1.0}[label_clearance] * 10
             + {'low': 0.0, 'medium': 0.5, 'high': 1.0}[ambiguity] * 10
             + min(1.0, degrees / 8.0) * 12
             + min(1.0, density / 10.0) * 10)
    score = round(min(100.0, max(0.0, score)), 1)
    # Play-test factor (stage-3 contract B, mode-separated): puzzle runs
    # (number/memory/duel) calibrate the score - completed runs validate the
    # rating and blend it by completion pace, fast boards ease, slow climb.
    # Free-color runs are engagement metrics only (never blended).
    PUZZLE_MODES = (None, 'number', 'memory', 'duel')
    puzzle_runs = [e for e in (playtests or []) if e.get('mode') in PUZZLE_MODES]
    free_runs = [e for e in (playtests or []) if e.get('mode') == 'free']
    completed = [e for e in puzzle_runs if e.get('filled', 0) >= e.get('total', 0)]
    if puzzle_runs:
        source = completed or puzzle_runs
        seconds = [float(e.get('seconds', 0.0) or 0.0) for e in source]
        median_seconds = float(np.median(seconds)) if seconds else 0.0
        metrics['playtestCount'] = len(puzzle_runs)
        metrics['playtestMedianSeconds'] = round(median_seconds, 1)
        metrics['playtestSecondsPerRegion'] = round(median_seconds / max(1, count), 2)
        metrics['playtestMistakesPerRegion'] = round(
            sum(float(e.get('mistakes', 0) or 0.0) for e in source) / max(1, count), 3)
    if free_runs:
        # Engagement / interaction metrics: free coloring is a different task
        # (no puzzle target, no mistakes), so it never touches the difficulty
        # score or the validated flag.
        metrics['freePlayCount'] = len(free_runs)
        metrics['freeMedianSeconds'] = round(float(np.median(
            [float(e.get('seconds', 0.0) or 0.0) for e in free_runs])), 1)
    if completed:
        pace = min(1.0, (median_seconds / max(1, count)) / 20.0)
        score = round(min(100.0, 0.9 * score + 10.0 * pace), 1)
    rating = 'easy' if score < 25 else ('medium' if score < 50 else ('hard' if score < 75 else 'master'))
    return {'rating': rating, 'score': score, 'metrics': metrics}


# ---------------------------------------------------------------------------
# Bundle emission
# ---------------------------------------------------------------------------

def emit_bundle(folder: Path, bundle: dict, previews=True) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    m, g, p, paint = bundle['manifest'], bundle['geometry'], bundle['palette'], bundle['paint']
    m['regionCount'] = len(g['regions']); m['paletteCount'] = len(p)
    # Difficulty profile (contract 5): recomputed for every revision; replaces
    # the 'unrated' placeholder. Play-tests recorded next to the output folder
    # (contract B) feed the profile; fresh revision folders start unvalidated.
    playtests = None
    playtest_file = folder / 'playtests.json'
    if playtest_file.is_file():
        try:
            loaded = read_json(playtest_file)
            if isinstance(loaded, list):
                playtests = loaded
        except Exception:
            playtests = None
    m['difficulty'] = difficulty_profile(bundle, playtests)
    # Only completed PUZZLE runs (number/memory/duel) validate the rating;
    # free-color completions are engagement data, not difficulty evidence.
    completed = [e for e in (playtests or [])
                 if e.get('filled', 0) >= e.get('total', 0) and e.get('mode') in (None, 'number', 'memory', 'duel')]
    m['difficultyValidatedByPlaytest'] = bool(completed)
    w, h = map(int, g['viewBox'][2:]); vb = svg_open(w, h)
    write_json(folder / 'regions.json', g); write_json(folder / 'palette.json', p); write_json(folder / 'paint.json', paint)
    # Authoring layer: objects.json ships next to the runtime files (revision
    # folders + authoring exports). It is deliberately OUTSIDE the content
    # hash — the hash covers the runtime contract (regions/palette/paint).
    objects = bundle.get('objects')
    if objects:
        write_json(folder / 'objects.json', {'schemaVersion': OBJECTS_SCHEMA_VERSION, 'objects': objects})
    else:
        (folder / 'objects.json').unlink(missing_ok=True)
    m['objectsCount'] = len(objects or [])
    m['contentHash'] = hashlib.sha256(b''.join((folder / f).read_bytes() for f in ['regions.json', 'palette.json', 'paint.json'])).hexdigest()
    final = vb + svg_paint(paint) + '</svg>'
    (folder / 'colored.svg').write_text(final)
    # Static exports render the semantic edges overlay (contract: visually
    # identical to the runtime board) - artwork edges solid, subdivision
    # edges light+dashed. Legacy bundles without edges fall back to the
    # outline-per-region style.
    edges_svg = _edges_overlay(g)
    if edges_svg:
        # Runtime parity (the board stacks paint -> regions -> ink -> EDGES ->
        # labels): the exported linework draws the semantic overlay ABOVE the
        # ink layer, so exports and runtime agree on layer order too.
        (folder / 'linework.svg').write_text(vb + svg_ink(paint) + edges_svg + '</svg>')
    else:
        outlines = '<g fill="none" stroke="' + INK + '" stroke-width="0.65" stroke-linejoin="round">' + \
            ''.join(f'<path d="{r["d"]}"/>' for r in g['regions']) + '</g>'
        (folder / 'linework.svg').write_text(vb + outlines + svg_ink(paint) + '</svg>')
    (folder / 'ink.svg').write_text(vb + svg_ink(paint) + '</svg>')

    def numbered(selected=False):
        select = g['regions'][0]['paletteId'] if g['regions'] else 1
        defs = '<defs><pattern id="sel" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#F0F3F6"/><path d="M0 0H4V4H0Z M4 4H8V8H4Z" fill="#CBD5DD"/></pattern></defs>'
        # With a semantic edges overlay the masks render FILL-ONLY (the
        # overlay draws every boundary), matching the runtime board.
        mask_attrs = 'fill-rule="evenodd"' if edges_svg \
            else 'stroke="' + INK + '" stroke-width=".65" fill-rule="evenodd"'
        masks = '<g ' + mask_attrs + '>' + ''.join(
            f'<path d="{r["d"]}"' + (' fill-rule="nonzero"' if r.get('fillRule') == 'nonzero' else '')
            + ' fill="' + ('url(#sel)' if selected and r['paletteId'] == select else 'white') + '"/>' for r in g['regions']) + '</g>'
        labels = '<g font-family="sans-serif" text-anchor="middle" dominant-baseline="central" fill="' + INK + '">' + ''.join(
            f'<text x="{r["label"]["x"]}" y="{r["label"]["y"]}" font-size="{r["label"]["fontSize"]}">{r["paletteId"]}</text>' for r in g['regions']) + '</g>'
        paint_under = {'gradients': paint.get('gradients'), 'paths': paint['paths'], 'inkPaths': []}
        if edges_svg:
            # Runtime parity: ink renders BELOW the semantic edges overlay,
            # labels stay on top (paint -> masks -> ink -> edges -> labels).
            return vb + defs + svg_paint(paint_under) + masks + svg_ink(paint) + edges_svg + labels + '</svg>'
        return vb + defs + svg_paint(paint_under) + masks + svg_ink(paint) + labels + '</svg>'

    (folder / 'numbered.svg').write_text(numbered())
    (folder / 'selected-preview.svg').write_text(numbered(True))
    qa = validate_bundle(bundle)
    if not qa['passed']:
        raise ValueError('Asset validation failed: ' + '; '.join(qa['errors'][:5]))
    m['qa'] = {'status': 'draft-needs-human-review', 'passedGeometryChecks': True, 'humanReviewed': False}
    qa['difficulty'] = {'rating': m['difficulty']['rating'], 'score': m['difficulty']['score'],
                        'metrics': m['difficulty']['metrics'],
                        'note': ('Deterministic analyzer blended with completed puzzle-mode (number/memory/duel) play-test times; free-color runs are engagement metrics only.'
                                 if m['difficultyValidatedByPlaytest'] else
                                 'Deterministic analyzer; difficultyValidatedByPlaytest stays false until a completed number/memory/duel playtest. Free-color runs never count as difficulty evidence.')}
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

def _lab_of(rgb: np.ndarray) -> np.ndarray:
    """RGB → CIE Lab (D65) for perceptual ΔE merge decisions."""
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def _merge_similar_adjacent(labels: np.ndarray, rgb: np.ndarray, delta_e: float,
                            min_size: int) -> np.ndarray:
    """Fidelity pass (Phase 2D.1): iteratively merge adjacent candidate
    segments whose mean Lab ΔE is below the policy threshold. Runs BEFORE
    semantic association and region building, so colorMergeDeltaE genuinely
    changes the candidate geometry (photo noise joins its neighbours; flat
    illustration bands stay separate)."""
    if delta_e <= 0:
        return labels
    lab = _lab_of(rgb)
    changed = True
    guard = 0
    while changed and guard < 8:
        changed = False
        guard += 1
        values = [int(v) for v in np.unique(labels) if int(v) != 0]
        if len(values) < 2:
            break
        means: Dict[int, np.ndarray] = {}
        areas: Dict[int, int] = {}
        for v in values:
            mask = labels == v
            means[v] = lab[mask].mean(0)
            areas[v] = int(mask.sum())
        pairs: Dict[int, List[int]] = defaultdict(list)
        for a, b in ((labels[:, :-1], labels[:, 1:]), (labels[:-1, :], labels[1:, :])):
            edge = a != b
            for la, lb in zip(a[edge], b[edge]):
                la, lb = int(la), int(lb)
                if la and lb and la != lb and lb not in pairs[la]:
                    pairs[la].append(lb)
                    pairs[lb].append(la)
        # smallest-first merge plan keeps the plan deterministic
        plan: List[Tuple[int, int]] = []
        for v in values:
            for n in sorted(pairs.get(v, [])):
                if n <= v:
                    continue
                de = float(np.sqrt(((means[v] - means[n]) ** 2).sum()))
                if de <= delta_e:
                    small, large = (v, n) if areas[v] <= areas[n] else (n, v)
                    if areas[large] >= min_size or areas[small] < min_size:
                        plan.append((small, large))
        if not plan:
            break
        # Single-pass, conflict-free merge: a segment merges into at most one
        # survivor this round, and a survivor absorbs at most one partner.
        # Chain merges (A→B, B→C) are deferred to the next round after means
        # are recomputed — this keeps every merged label area consistent and
        # the region partition overlap-free.
        busy: set = set()
        relabel: Dict[int, int] = {}
        for small, large in sorted(plan):
            if small in busy or large in busy:
                continue
            relabel[small] = large
            busy.add(small)
            busy.add(large)
        if not relabel:
            break
        out = labels.copy()
        for src, dst in sorted(relabel.items()):
            out[labels == src] = dst
        labels = out
        changed = True
    # Merging whole labels can create non-connected label areas; region and
    # paint builders require connected geometry — split into fresh ids per
    # connected component (deterministic: row-major scan order).
    split = np.zeros_like(labels)
    nxt = 0
    for v in [int(v) for v in np.unique(labels) if int(v) != 0]:
        mask = (labels == v).astype(np.uint8)
        n_comp, comp = cv2.connectedComponents(mask, connectivity=4)
        for idx in range(1, n_comp):
            nxt += 1
            split[comp == idx] = nxt
    return split


def _image_labels(source: Path, settings: BuildSettings,
                  segment_density: float = 1.0, color_merge_delta_e: float = 0.0):
    """Shared SLIC + tiny-merge step for raster compilation (Phase 2D entry).

    Returns (rgb, labels, original_size, working_size) so semantic converters
    can analyze the EXACT same labels the region compiler will consume.

    Convert fidelity knobs (Phase 2D.1):
    - segment_density scales the CANDIDATE segment count independently of the
      gameplay target (candidate segmentation ≠ final gameplay regions);
    - color_merge_delta_e (CIE Lab) merges perceptually-equal adjacent
      candidates BEFORE association, so fidelity genuinely changes geometry."""
    im = Image.open(source).convert('RGB'); original = im.size
    im.thumbnail((settings.max_edge, settings.max_edge), Image.Resampling.LANCZOS)
    rgb = np.array(im); h, w = rgb.shape[:2]
    n_candidates = max(30, int(round(settings.target_regions * max(0.2, segment_density))))
    labels = slic(rgb, n_segments=n_candidates, compactness=settings.compactness,
                  sigma=.8, start_label=1, enforce_connectivity=True, min_size_factor=.25, channel_axis=-1)
    labels = merge_tiny(labels, rgb, settings.min_region_pixels)
    if color_merge_delta_e > 0:
        labels = _merge_similar_adjacent(labels, rgb, color_merge_delta_e, settings.min_region_pixels)
    return rgb, labels, original, (w, h)


def _semantic_raster_paint(labels: np.ndarray, rgb: np.ndarray, label_map: dict,
                           settings: BuildSettings, artwork_id: str, w: int, h: int) -> dict:
    """Object-aware raster paint for Convert (Phase 2D.1).

    Builds the visual reconstruction from the SAME candidate labels the
    gameplay regions use, but grouped by SEMANTIC object + fill hex — each
    group becomes one paint path with a stable rc-* shapeId. Visual layer and
    gameplay layer stay separate (no per-region paint), yet the reconstruction
    carries real ownership:

        obj-tree -> rc-tree-0001, rc-tree-0002, ...
        obj-house -> rc-house-0001, ...

    Same color areas of different objects never share a path, so later
    targeted edits/regeneration can address one object's artwork."""
    corner_cos = _corner_cos(settings.corner_angle_deg)
    masters = masters_from_labels(labels, background=0,
                                  fit_tolerance=settings.curve_tolerance, corner_cos=corner_cos)
    paths = []
    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for value, comps in masters.items():
        oid = label_map.get(int(value), 'unassigned')
        mask = labels == int(value)
        hx = '#' + ''.join(f'{int(v):02X}' for v in np.clip(rgb[mask].mean(0), 0, 255)) if mask.any() else '#808080'
        for comp in comps:
            groups[(oid, hx)].append(format_path(comp['master']))
    counters: Dict[str, int] = defaultdict(int)
    for (oid, hx), ds in sorted(groups.items()):
        slug = oid.replace('obj-', '').replace('unassigned', 'canvas')
        counters[slug] += 1
        shape_id = f'rc-{slug}-{counters[slug]:04d}'
        paths.append({'shapeId': shape_id, 'objectId': oid, 'fill': hx, 'd': ' '.join(ds)})
    return {'schemaVersion': 2, 'artworkId': artwork_id, 'viewBox': [0, 0, w, h],
            'paths': paths, 'inkPaths': [], 'sourceColorShapeCount': len(paths),
            'notes': ('Object-aware reconstruction from semantic candidate segments; each path '
                      'carries a stable shapeId and object ownership (rc-* shapes).')}


def _objects_from_segment_map(segment_object_map: dict, paint: dict,
                              plan_objects: list | None = None) -> list | None:
    """Initial object records for converted bundles (Phase 2D.1 + 26): scene
    plan semantics merged with ACTUAL rc-* shape ownership.

    Actual paint ownership is authoritative for shapeIds (only live rc-*
    shapes owned by segments enter the records); everything else — name,
    role, parentId, subdivision/detailWeight — comes from the ScenePlan
    records, so the gameplay budget engine (detailWeight, min/maxRegions)
    sees the same semantics the AI planned. Objects are ordered by plan
    z-order first, plan-less objects last (id order)."""
    if not segment_object_map:
        return None
    owned: Dict[str, List[str]] = defaultdict(list)
    for p in (paint.get('paths') or []):
        oid = p.get('objectId')
        sid = p.get('shapeId')
        if oid and oid != 'unassigned' and sid:
            owned[oid].append(sid)
    if not owned:
        return None
    plan = {o['id']: o for o in (plan_objects or []) if isinstance(o, dict) and o.get('id')}
    ordered = [o['id'] for o in (plan_objects or []) if isinstance(o, dict) and o.get('id') in owned]
    ordered += [oid for oid in sorted(owned) if oid not in plan]
    records = []
    for oid in ordered:
        meta = plan.get(oid) or {}
        rec: dict = {'id': oid}
        rec['name'] = str(meta.get('name') or oid.replace('obj-', '').replace('-', ' ').title())[:80]
        for key in ('type', 'role', 'parentId'):
            if meta.get(key):
                rec[key] = meta[key]
        sub = meta.get('subdivision') if isinstance(meta.get('subdivision'), dict) else {}
        if not sub and meta.get('detailWeight') is not None:
            sub = {'detailWeight': meta.get('detailWeight')}
        elif sub and sub.get('detailWeight') is None and meta.get('detailWeight') is not None:
            sub = {**sub, 'detailWeight': meta.get('detailWeight')}
        if sub:
            rec['subdivision'] = sub
        rec['shapeIds'] = sorted(owned[oid])
        records.append(rec)
    return normalize_objects({'objects': records})


def compile_image(source: Path, output: Path, *, artwork_id: str, version: str, title: str,
                  settings: BuildSettings, provenance: dict | None = None,
                  segment_object_map: dict | None = None,
                  objects: list | None = None,
                  segment_density: float = 1.0, color_merge_delta_e: float = 0.0,
                  progress: Callable = lambda *_: None) -> dict:
    progress(.04, 'Preparing approved master')
    rgb, labels, original, (w, h) = _image_labels(source, settings,
                                                  segment_density=segment_density,
                                                  color_merge_delta_e=color_merge_delta_e)
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
                object_id = (segment_object_map or {}).get(int(value), 'unassigned')
                reg = pack_region(comp['master'], f'r-{rid:05d}', pid, object_id, label=label,
                                  source='boundary-chain-fit', fit_tolerance=settings.curve_tolerance,
                                  legacy_rings=comp['legacyRings'])
                if label['clearance'] < settings.min_label_radius or label['fontSize'] < 3.5:
                    decorations.append(reg)
                else:
                    regions.append(reg)
    else:
        from rasterio.features import shapes
        progress(.28, 'Building closed, non-overlapping region polygons (legacy)')
        polys = []; means = []; masks = []; origins = []; label_values = []
        for geom, val in shapes(labels.astype(np.int32), connectivity=4):
            poly = shape(geom)
            for poly in polygon_parts(make_valid(poly)):
                x0, y0, x1, y1 = map(int, poly.bounds)
                local = _rasterize_mask(poly, x0, y0, x1, y1)
                if not local.any():
                    continue
                polys.append(poly); means.append(rgb[y0:y1, x0:x1][local].mean(0))
                masks.append(local); origins.append((x0, y0)); label_values.append(int(val))
        progress(.38, 'Grouping palette colors and positioning labels')
        palette, assignments = palette_for_regions(np.array(means), settings.palette_colors)
        for i, (poly, pid, mask, origin, value) in enumerate(zip(polys, assignments, masks, origins, label_values), 1):
            label = make_label(poly, int(pid), mask, origin)
            object_id = (segment_object_map or {}).get(int(value), 'unassigned')
            reg = pack_region(poly, f'r-{i:05d}', int(pid), object_id, label=label, source='legacy-polygon', fit=False)
            if label['clearance'] < settings.min_label_radius or label['fontSize'] < 3.5:
                decorations.append(reg)
            else:
                regions.append(reg)
    if not regions:
        raise ValueError('No playable regions. Lower the label radius or region count.')
    progress(.48, 'Tracing the detailed vector paint layer' if curved else 'Tracing pixel-edge paint polygons (legacy)')
    if segment_object_map:
        # Convert (Phase 2D.1): object-aware reconstruction — stable rc-* shapeIds
        # with semantic ownership, built from the same candidate labels.
        paint = _semantic_raster_paint(labels, rgb, segment_object_map, settings, artwork_id, w, h)
    else:
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
    if segment_object_map:
        # Converted bundles ship objects.json from the FIRST revision (not
        # only after an edit): same first-class contract as native SVG/AI art.
        # ScenePlan records carry the semantics (role/subdivision budgets);
        # actual rc-* ownership stays authoritative for shapeIds.
        objects = _objects_from_segment_map(segment_object_map, paint, objects)
        if objects:
            bundle['objects'] = objects
            manifest['objectsCount'] = len(objects)
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
                        objects: list | None = None,
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
        object_id = cand['shape'].get('objectRef') or 'unassigned'
        if cand['verbatim']:
            reg = pack_region(cand['cmds'], f'r-{idx:05d}', pid, object_id, label=label,
                              source='svg-master-import', fit_tolerance=0.0,
                              fill_rule=cand['fillRule'])
        else:
            reg = pack_region(cand['poly'], f'r-{idx:05d}', pid, object_id, label=label,
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
    # Stage-2 deterministic auto-subdivide (contract 6): close the gap between
    # authored shape count and a rich gameplay region count, true-vector only.
    # Semantic phase: object records (from data-cd-object master groups) drive
    # per-object region budgets instead of global largest-first. Authoring
    # metadata supplied by the caller (scene plan / UI) merges in by object id:
    # the master's groups own the shapeIds, the provided records may carry
    # subdivision budgets, type/role, parentId and generation info.
    derived = _objects_from_shapes(list(doc.shapes) + list(doc.ink_shapes))
    if provided_objects := normalize_objects({'objects': objects or []}):
        derived = derived or []
        derived_by_id = {o['id']: o for o in derived}
        merged = list(derived)
        for rec in provided_objects:
            target_rec = derived_by_id.get(rec['id'])
            if target_rec is None:
                merged.append(rec)
                continue
            for key, val in rec.items():
                if key != 'shapeIds' and (key != 'name' or not target_rec.get('name')):
                    target_rec[key] = val
        objects = normalize_objects({'objects': merged})
    else:
        objects = derived
    edges: List[dict] = []
    if settings.auto_subdivide:
        progress(.58, f'Subdividing large regions toward ~{settings.target_regions} tap targets')
        _auto_subdivide(regions, settings, edges, objects=objects, progress=progress)
    progress(.66, 'Classifying artwork boundaries vs subdivision edges')
    _emit_master_edges(regions, doc, edges)
    progress(.70, 'Emitting the detailed paint layer (order, strokes and gradients preserved)')
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
                'edges': edges,
                'boundaryStyle': BOUNDARY_STYLE_DEFAULT,
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
    bundle = {'manifest': manifest, 'geometry': geometry, 'palette': palette, 'paint': paint,
              'objects': objects}
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output / 'source-master.svg')
    progress(.86, 'Checking topology, labels and runtime exports')
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
    # Semantic object model (authoring layer): optional objects.json next to
    # the runtime files. Corrupt/unknown payloads degrade to None (unassigned),
    # never to a failed load.
    objects = None
    if (folder / 'objects.json').is_file():
        try:
            objects = normalize_objects(read_json(folder / 'objects.json'))
        except Exception:
            objects = None
    return {'manifest': read_json(folder / 'artwork.json'), 'geometry': geometry,
            'palette': read_json(folder / 'palette.json'), 'paint': read_json(folder / 'paint.json'),
            'objects': objects}


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


def _palette_entry_for_color(bundle: dict, color: str) -> dict:
    """Palette identity helper (P0.2): a number group's answer color is its
    identity. Return the existing group whose swatch IS ``color`` (hex,
    case-insensitive) or append a new one - never mutate a shared swatch, so
    regions outside this edit keep a truthful answer key."""
    for entry in bundle['palette']:
        if str(entry.get('hex', '')).upper() == str(color).upper():
            return entry
    new_id = max((int(e['id']) for e in bundle['palette']), default=0) + 1
    entry = {'id': new_id, 'number': new_id, 'name': f'Tone {new_id:02d}', 'hex': color,
             'paint': {'type': 'linearGradient',
                       'stops': [{'offset': 0, 'color': color}, {'offset': 1, 'color': color}]}}
    bundle['palette'].append(entry)
    return entry


def _appearance_colors(paint: dict, shape_id: str) -> List[str]:
    """Solid fill (or the gradient stop colors) of one paint path, upper-case."""
    path = next((p for p in (paint.get('paths') or []) if p.get('shapeId') == shape_id), None)
    if path is None:
        return []
    fill = str(path.get('fill', ''))
    if fill.startswith('url(#'):
        grad = next((gr for gr in (paint.get('gradients') or []) if gr.get('id') == fill[5:-1]), None)
        return [str(s.get('color', '')).upper() for s in (grad or {}).get('stops', []) if s.get('color')]
    return [fill.upper()] if SAFE_HEX.match(fill) else []


def _color_answer_conflicts(bundle: dict, threshold: float = 100.0) -> tuple:
    """Answer-key integrity (P0.2 QA check): every playable region's
    number-group swatch must match the painted appearance of its source
    shape. Pen / recolor / AI edits can silently break this.

    Returns ``(checked, [conflicting region ids])``. Regions without a
    paintable source shape (gameplay-only pen targets) have no appearance and
    cannot conflict; gradient-tinted appearances pass when any stop is near
    the swatch (shading spread is preserved by design).
    """
    paint = bundle['paint']
    swatch = {}
    for e in bundle['palette']:
        hexv = str(e.get('hex', ''))
        swatch[int(e['id'])] = _hex_rgb(hexv) if SAFE_HEX.match(hexv) else None
    checked, conflicts = 0, []
    for r in bundle['geometry'].get('regions', []):
        sid = r.get('masterShapeId')
        target = swatch.get(int(r.get('paletteId', -1)))
        if not sid or target is None:
            continue
        looks = [_hex_rgb(c) for c in _appearance_colors(paint, sid) if SAFE_HEX.match(c)]
        if not looks:
            continue
        checked += 1
        if min(math.sqrt(sum((x - y) ** 2 for x, y in zip(c, target))) for c in looks) > threshold:
            conflicts.append(r['id'])
    return checked, conflicts


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
    # P0.2 palette identity: NEVER mutate a shared swatch - every OTHER region
    # in that group would silently get a wrong answer color. The chosen
    # regions move to the palette group whose answer color IS the new
    # appearance (reused when it exists, created otherwise). Repainting a
    # whole group stays a separate, intentional operation.
    entry = _palette_entry_for_color(bundle, color)
    pid = int(entry['id'])
    for r in chosen:
        if r['paletteId'] != pid:
            r['paletteId'] = pid
            r['label'] = make_label(region_polygon(r), pid)



def edit_bundle(source: Path, output: Path, request, version: str):
    bundle = load_bundle(source); g = bundle['geometry']; m = bundle['manifest']
    settings = read_json(source / 'build-settings.json') if (source / 'build-settings.json').is_file() else {}
    fit_tolerance = float(settings.get('curve_tolerance', 1.0))
    min_px = float(settings.get('min_region_pixels', 35))
    pen_warning: str | None = None
    regs = {r['id']: r for r in g['regions']}
    chosen_ids = list(dict.fromkeys(request.region_ids))
    if any(rid not in regs for rid in chosen_ids) and request.action != 'draw':
        raise ValueError('Select existing playable regions from the current revision.')
    if request.action == 'draw' and chosen_ids:
        raise ValueError('The pen tool takes no region selection; just draw the shape where you need it.')
    chosen = [regs[rid] for rid in chosen_ids if rid in regs]
    # Per-action selection counts (contract addendum):
    # merge>=2, split/cut/label=1, node=2, draw=0, others>=1.
    need = {'merge': 2, 'split': 1, 'cut': 1, 'label': 1, 'node': 2}.get(request.action, 1)
    if request.action == 'draw':
        if chosen:
            raise ValueError('The pen tool takes no region selection.')
    elif len(chosen) < need:
        hint = ('two or more adjacent regions' if request.action == 'merge'
                else 'exactly one region' if request.action in ('split', 'cut', 'label')
                else 'exactly two regions sharing a boundary' if request.action == 'node'
                else 'at least one region')
        raise ValueError(f'Select {hint} for this action.')
    elif request.action in ('split', 'cut', 'label') and len(chosen) != 1:
        raise ValueError('Select exactly one region for this action.')
    elif request.action == 'node' and len(chosen) != 2:
        raise ValueError('Select exactly two regions sharing the boundary you want to drag.')
    valid_palette = {p['id'] for p in bundle['palette']}
    if request.action == 'draw':
        if request.palette_id is None:
            raise ValueError('Choose a number group (palette) for the drawn region.')
        pid = int(request.palette_id)
    else:
        pid = request.palette_id or chosen[0]['paletteId']
    if pid not in valid_palette: raise ValueError('Unknown palette group.')
    if request.action == 'merge':
        if len(chosen) < 2: raise ValueError('Select two or more adjacent regions.')
        union = unary_union([make_valid(region_polygon(r)) for r in chosen])
        if union.geom_type != 'Polygon' or not union.is_valid:
            raise ValueError('Merge only edge-adjacent regions; disconnected pieces cannot be one tap target.')
        used = set(request.region_ids)
        g['regions'] = [r for r in g['regions'] if r['id'] not in used]
        _prune_edges(g, used)
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
        _prune_edges(g, {target['id']})
        for idx, (part_cmds, _rings) in enumerate(parts):
            split_id = 'r-s-' + hashlib.sha256((target['id'] + version + str(idx)).encode()).hexdigest()[:12]
            g['regions'].append(pack_region(part_cmds, split_id, target['paletteId'], target['objectId'],
                                            source='split-disconnected', fit_tolerance=0.0))
    elif request.action == 'cut':
        if len(chosen) != 1: raise ValueError('Select exactly one region to cut.')
        if not request.d:
            raise ValueError('Draw a cut line first: the pen line must cross the whole region.')
        target = chosen[0]
        cut_polys = flatten_d(request.d)
        if not cut_polys:
            raise ValueError('The cut line path is empty or unparsable.')
        line = max(cut_polys, key=len)
        target_poly = region_polygon(target)
        pieces = cut_polygon(target_poly, line)
        if len(pieces) < 2 or any(p.area < min_px for p in pieces):
            smallest = min((p.area for p in pieces), default=target_poly.area)
            raise ValueError(f'The cut line must cross the whole region; pieces of {smallest:.0f} px² are too '
                             f'small to tap. Draw the line from outside one edge to outside the opposite edge.')
        # Prior artwork edges of the target keep their classification.
        prior_art_lines: List[LineString] = []
        for e in g.get('edges') or []:
            if e.get('kind') == 'artwork' and target['id'] in (e.get('leftRegion'), e.get('rightRegion')):
                prior_art_lines.extend(_ref_lines(flatten_d(e.get('d', ''))))
        g['regions'] = [r for r in g['regions'] if r['id'] != target['id']]
        if g.get('edges') is not None:
            g['edges'] = [e for e in g['edges']
                          if target['id'] not in (e.get('leftRegion'), e.get('rightRegion'))]
        new_regions = []
        for idx, piece in enumerate(pieces):
            cut_id = 'r-c-' + hashlib.sha256((target['id'] + version + str(idx)).encode()).hexdigest()[:12]
            reg = pack_region(piece, cut_id, target['paletteId'], target['objectId'],
                              source='cut-region', fit_tolerance=fit_tolerance)
            if target.get('masterShapeId'):
                reg['masterShapeId'] = target['masterShapeId']
            g['regions'].append(reg)
            new_regions.append(reg)
        g.setdefault('edges', [])
        g.setdefault('boundaryStyle', BOUNDARY_STYLE_DEFAULT)
        index = _RegionIndex(g['regions'])
        cut_ref = [LineString(line)]
        for reg, piece in zip(new_regions, pieces):
            _emit_classified_edges(g['edges'], piece, cut_ref, 'subdivision', 'artwork',
                                   index, prior_art_lines, self_id=reg['id'])
        deviation = max(region_polygon(reg).symmetric_difference(piece).area
                        for reg, piece in zip(new_regions, pieces))
        g['partitionTolerance'] = round(float(g.get('partitionTolerance', 0.0)) + deviation + 0.01, 3)
    elif request.action == 'draw':
        if not request.d:
            raise ValueError('Draw a closed shape first (the pen region path).')
        rings = flatten_d(request.d)
        if not rings or any(len(ring) < 3 for ring in rings):
            raise ValueError('Draw a closed shape with at least three points.')
        # Flatten closes rings implicitly (Polygon closes open loops: a
        # missing Z auto-closes by joining the last point back to the first).
        solids = solid_polygons(rings, 'evenodd')
        poly = _safe_union(solids) if len(solids) > 1 else (solids[0] if solids else Polygon())
        if poly.is_empty or poly.area < min_px:
            raise ValueError(f'The drawn shape is only {max(0.0, float(poly.area)):.0f} px²; '
                             f'playable regions need at least {min_px:.0f} px² to tap.')
        existing = [region_polygon(r) for r in g['regions'] + g.get('decorations', [])]
        existing = [p for p in existing if not p.is_empty and p.area > 0]
        cover = _safe_union(existing) if existing else Polygon()
        # P0.1 artwork pen ABOVE the art: the drawn shape is painted on top,
        # so its FULL geometry is the tap surface. The gameplay regions
        # underneath are carved (A := A - P) and rebuilt, which keeps the
        # studio invariant - gameplay regions never overlap - working on
        # fully covered canvases (normal finished Color Duel artwork)
        # instead of only over empty canvas. z_behind and the gameplay
        # region pen keep the visible-surface semantics (poly - coverage).
        carve = bool(request.paint) and not request.z_behind
        if carve:
            usable = [p for p in _polys(make_valid(poly)) if p.area >= min_px]
            if not usable:
                raise ValueError(f'Every piece of the drawn shape is below the {min_px:.0f} px² tap minimum.')
        else:
            try:
                visible = make_valid(poly.difference(cover))
            except Exception:
                visible = Polygon()
            parts = _polys(visible)
            usable = [p for p in parts if p.area >= min_px]
            if not usable:
                if request.paint:
                    raise ValueError('The shape would be completely hidden behind the existing artwork; '
                                     'place it above the art (artwork mode carves the regions underneath) '
                                     'or draw it on uncovered canvas.')
                raise ValueError('The drawn shape overlaps fully with existing regions; draw it on '
                                 'uncovered canvas, or use artwork mode so the regions underneath are carved.')
            if request.paint and request.z_behind and poly.area > 0:
                ratio = sum(p.area for p in usable) / float(poly.area)
                if ratio < 0.25:
                    pen_warning = (f'The pen shape is only {ratio * 100:.0f}% visible behind the existing '
                                   f'artwork (placed below the art); most of its tap surface is occluded.')
        drawn_ref = [LineString(ring) for ring in rings]
        # Artwork pen (P0 contract): the drawn shape also becomes finished
        # artwork - a paint.json path with a stable shapeId (sp-*), fill,
        # optional ink outline and z-order is emitted, and every playable
        # region references it via masterShapeId, so the normal recolor /
        # QA / revision flow works on pen-drawn art exactly like imported SVG
        # shapes. The region pen (paint=False) stays a gameplay-only white tap
        # target (the artist paints it later).
        paint = bundle['paint']
        palette_entry = next((entry for entry in bundle['palette'] if entry['id'] == pid), None)
        if request.paint and request.color:
            fill = request.color.upper()
            if not SAFE_HEX.match(fill):
                raise ValueError('Choose a #RRGGBB fill color for the artwork shape.')
            # P0.2 palette identity: a custom fill NEVER mutates the chosen
            # group's shared swatch (other regions' answer key would rot).
            # The pen regions join the palette group whose answer color IS
            # the fill - reused when it exists, created otherwise.
            if str((palette_entry or {}).get('hex', '')).upper() != fill:
                pid = int(_palette_entry_for_color(bundle, fill)['id'])
        else:
            fill = (palette_entry or {}).get('hex', '#808080')
        zs = [float(entry.get('z', 0) or 0)
              for entry in (paint.get('paths') or []) + (paint.get('inkPaths') or [])]
        if not zs:
            z_order = 1
        elif request.paint and request.z_behind:
            z_order = int(math.floor(min(zs))) - 1
        else:
            z_order = int(math.floor(max(zs))) + 1
        stroke_w = round(float(request.stroke_width or 0.0), 3)
        # P0.1 carve pass: every region/decoration the pen shape covers loses
        # that area and is rebuilt, keeping its palette group, object id and
        # master shape link. Carved pieces are emitted with EXACT polygonal
        # masters (fit=False) so they tile exactly against their neighbours'
        # stored flat rings - a curve re-fit would drift the shared boundaries
        # and break the raster partition invariant. Remainder pieces below the
        # tap minimum become DECORATIONS (the compiler's own semantics for
        # sub-minimum surfaces): non-interactive, but still part of the
        # partition, so the tiling never develops holes.
        deco_ids = {d['id'] for d in g.get('decorations', [])}
        carved: List[tuple] = []
        if carve:
            for r in g['regions'] + g.get('decorations', []):
                rp = region_polygon(r)
                if rp.is_empty or rp.area <= 0 or not rp.intersects(poly):
                    continue
                try:
                    remainder = make_valid(rp.difference(poly))
                except Exception:
                    continue
                if remainder.is_empty or remainder.area <= 1e-9:
                    carved.append((r, []))       # fully covered by the new art
                else:
                    carved.append((r, _polys(remainder)))
            if carved:
                removed = {r['id'] for r, _pieces in carved}
                g['regions'] = [r for r in g['regions'] if r['id'] not in removed]
                g['decorations'] = [d for d in g.get('decorations', []) if d['id'] not in removed]
                _prune_edges(g, removed)
        carved_regions = []
        for r, pieces in carved:
            for idx, piece in enumerate(pieces):
                carve_id = 'r-v-' + hashlib.sha256((r['id'] + version + str(idx)).encode()).hexdigest()[:12]
                reg = pack_region(piece, carve_id, r['paletteId'], r['objectId'],
                                  source='pen-carved', fit_tolerance=fit_tolerance, fit=False)
                if r.get('masterShapeId'):
                    reg['masterShapeId'] = r['masterShapeId']
                if piece.area < min_px or r['id'] in deco_ids:
                    g.setdefault('decorations', []).append(reg)
                else:
                    g['regions'].append(reg)
                    carved_regions.append((reg, piece))
        if carve and carved:
            # Document the true partition band introduced by this edit: the
            # symmetric difference between the pre-edit surface union and the
            # rebuilt one (same precedent as merge/cut refit deviations).
            try:
                after_polys = [region_polygon(x) for x in g['regions'] + g.get('decorations', [])]
                after_union = unary_union([make_valid(p) for p in after_polys if not p.is_empty])
                drift = cover.symmetric_difference(after_union).area if not cover.is_empty else 0.0
            except Exception:
                drift = 0.0
            g['partitionTolerance'] = round(float(g.get('partitionTolerance', 0.0)) + float(drift) + 0.01, 3)
        new_regions = []
        for idx, piece in enumerate(usable):
            pen_id = 'r-p-' + hashlib.sha256((request.d + version + str(idx)).encode()).hexdigest()[:12]
            reg = pack_region(piece, pen_id, int(pid), request.group,
                              source='pen-drawn', fit_tolerance=fit_tolerance)
            if request.paint:
                shape_id = 'sp-' + hashlib.sha256((pen_id + 'shape').encode()).hexdigest()[:12]
                # The paint path carries the region's master geometry so the
                # painted surface and the tap surface coincide exactly.
                entry = {'z': z_order, 'shapeId': shape_id, 'd': reg['d'],
                         'fillRule': reg['fillRule'], 'fill': fill}
                if stroke_w > 0:
                    entry['stroke'] = INK
                    entry['strokeWidth'] = stroke_w
                paint['paths'].append(entry)
                paint['sourceColorShapeCount'] = int(paint.get('sourceColorShapeCount', 0) or 0) + 1
                reg['masterShapeId'] = shape_id
                # Semantic object model: an artwork pen stroke is a first-class
                # object owning its paint shape(s); the sync before emit merges
                # all pieces of this stroke into one object record.
                reg['objectId'] = 'obj-' + hashlib.sha256((pen_id + 'object').encode()).hexdigest()[:12]
                bundle.setdefault('_pending_object_names', {})[reg['objectId']] = \
                    'Pen artwork ' + str(len(new_regions) + 1)
            g['regions'].append(reg)
            new_regions.append(reg)
        # (P0.2) No palette mutation here: a diverging custom fill already
        # reassigned the pen regions to the matching group above, and the
        # default fill IS the chosen group's swatch - the answer key stays
        # truthful either way.
        g.setdefault('edges', [])
        g.setdefault('boundaryStyle', BOUNDARY_STYLE_DEFAULT)
        index = _RegionIndex(g['regions'])
        # Carved pieces first: boundary segments hugging the pen outline are
        # artwork (the outline IS master art now); the rest stays subdivision.
        for reg, piece in carved_regions:
            _emit_classified_edges(g['edges'], piece, drawn_ref, 'artwork', 'subdivision', index,
                                   self_id=reg['id'])
        for reg, piece in zip(new_regions, usable):
            # Outline segments near the drawn path are artwork; the subtraction
            # segments bordering existing regions are subdivision.
            _emit_classified_edges(g['edges'], piece, drawn_ref, 'artwork', 'subdivision', index,
                                   self_id=reg['id'])
    elif request.action == 'node':
        # Drag boundary anchors (stage-3 contract A): the shared boundary of
        # exactly two regions is re-drawn as a new polyline; the swept lens
        # moves area from one side to the other. A true artwork boundary stays
        # artwork ("true boundary stays true"); a subdivision stays subdivision.
        if len(chosen) != 2:
            raise ValueError('Select exactly two regions sharing the boundary you want to drag.')
        a, b = chosen
        pair_ids = {a['id'], b['id']}
        E = next((e for e in (g.get('edges') or [])
                  if {e.get('leftRegion'), e.get('rightRegion')} == pair_ids), None)
        if E is None:
            raise ValueError('Select a shared boundary between exactly two regions.')
        old_subs = flatten_d(E.get('d') or '')
        new_subs = flatten_d(request.d) if request.d else []
        if not old_subs or not new_subs:
            raise ValueError('Draw the new boundary first — drag at least one anchor.')
        old_line = LineString(max(old_subs, key=len))
        new_pts = [(float(x), float(y)) for x, y in max(new_subs, key=len)]
        new_line = LineString(new_pts)
        # No-op guard: an undragged boundary (or a sub-pixel lens) changes nothing.
        if old_line.hausdorff_distance(new_line) < 0.75:
            raise ValueError('Drag at least one anchor to a new position.')
        A, B = region_polygon(a), region_polygon(b)
        # The lens is swept from the ACTUAL shared boundary of the two region
        # polygons (adjacent regions touch exactly along the refit-shared
        # line), not from the simplified edge 'd' the anchors were rendered
        # from: sweeping the rendered line leaves hairline gaps against the
        # true polygons and detaches the swept strip from its new owner.
        # Fallback when the polygons do not touch exactly: the edge line
        # itself (the sliver tolerance below then absorbs the artifacts).
        shared = A.intersection(B)
        shared_parts = shared.geoms if hasattr(shared, 'geoms') else [shared]
        shared_lines = [q for q in shared_parts if q.geom_type == 'LineString' and q.length > 0]
        sweep = max(shared_lines, key=lambda q: q.length) if shared_lines else old_line
        ring = [(float(x), float(y)) for x, y in sweep.coords] + list(reversed(new_pts))
        try:
            lens = make_valid(Polygon(ring))
        except Exception:
            lens = None
        lens_polys = [p for p in (polygon_parts(lens) if lens is not None else []) if p.area > 0]
        S = _safe_union(lens_polys) if lens_polys else Polygon()
        if S.is_empty or S.area < 1.0:
            raise ValueError('Drag at least one anchor to a new position.')
        # Spill guard: the lens must stay inside the two chosen regions.
        others = [region_polygon(r) for r in g['regions'] + g.get('decorations', [])
                  if r['id'] not in pair_ids]
        others = [p for p in others if not p.is_empty and p.area > 0]
        if others and S.intersection(_safe_union(others)).area > max(2.0, 0.02 * S.area):
            raise ValueError('The dragged boundary crosses other regions — keep it between the two selected regions.')
        Ap = make_valid(A.difference(S).union(S.intersection(B)))
        Bp = make_valid(B.difference(S).union(S.intersection(A)))
        rebuilt = []
        for src, geom in ((a, Ap), (b, Bp)):
            parts = _polys(geom)
            if not parts:
                raise ValueError(f'The drag would erase region {src["id"]} — keep the boundary between the two selected regions.')
            if len(parts) > 1:
                # Refit slivers: the lens is swept from the SIMPLIFIED edge
                # polyline while region polygons carry the refit detail, so a
                # hair-thin fragment can detach along the old line. Keep the
                # dominant piece and drop slivers under the documented
                # tolerance (they become unowned gaps); a genuine split that
                # removes real area stays an error.
                parts = sorted(parts, key=lambda p: -p.area)
                main, slivers = parts[0], parts[1:]
                lost = sum(p.area for p in slivers)
                if lost > max(8.0, 0.005 * main.area):
                    raise ValueError(f'The drag would split region {src["id"]} into disconnected pieces — keep the boundary in one piece.')
                parts = [main]
            merged = parts[0]
            if merged.geom_type != 'Polygon':
                raise ValueError(f'The drag would split region {src["id"]} into disconnected pieces — keep the boundary in one piece.')
            if merged.area < min_px:
                raise ValueError(f'Region {src["id"]} would be only {merged.area:.0f} px² after the drag — too small to tap.')
            rebuilt.append((src, merged))
        # Prior artwork edges of the pair keep their classification (collected
        # BEFORE pruning so the reclassification below can re-check against them).
        prior_art_lines: List[LineString] = []
        for e in g.get('edges') or []:
            if e.get('kind') == 'artwork' and (e.get('leftRegion') in pair_ids or e.get('rightRegion') in pair_ids):
                prior_art_lines.extend(_ref_lines(flatten_d(e.get('d', ''))))
        g['regions'] = [r for r in g['regions'] if r['id'] not in pair_ids]
        _prune_edges(g, pair_ids)
        new_regions = []
        for idx, (src, poly) in enumerate(rebuilt):
            node_id = 'r-n-' + hashlib.sha256(('|'.join(sorted(pair_ids)) + version + str(idx)).encode()).hexdigest()[:12]
            reg = pack_region(poly, node_id, src['paletteId'], src['objectId'],
                              source='node-edit', fit_tolerance=fit_tolerance)
            if src.get('masterShapeId'):
                reg['masterShapeId'] = src['masterShapeId']
            g['regions'].append(reg)
            new_regions.append(reg)
        g.setdefault('edges', [])
        g.setdefault('boundaryStyle', BOUNDARY_STYLE_DEFAULT)
        index = _RegionIndex(g['regions'])
        e_kind = E.get('kind') if E.get('kind') in EDGE_KINDS else 'subdivision'
        other_kind = 'artwork' if e_kind == 'subdivision' else 'subdivision'
        for reg, (_src, poly) in zip(new_regions, rebuilt):
            _emit_classified_edges(g['edges'], poly, [new_line], e_kind, other_kind,
                                   index, prior_art_lines, self_id=reg['id'])
        deviation = max(region_polygon(reg).symmetric_difference(poly).area
                        for reg, (_src, poly) in zip(new_regions, rebuilt))
        g['partitionTolerance'] = round(float(g.get('partitionTolerance', 0.0)) + deviation + 0.01, 3)
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
        _prune_edges(g, remove)
        g['decorations'].extend(chosen)
    groups = defaultdict(list)
    for r in g['regions']:
        if r['objectId'] != 'unassigned':
            groups[r['objectId']].append(r['id'])
    m['objectGroups'] = [{'id': key, 'title': key.replace('-', ' ').title(), 'regionIds': ids} for key, ids in sorted(groups.items())]
    # Semantic object model: regions are the membership truth; records carry
    # the authoring metadata. This keeps objects.json alive through every
    # edit (merge/split/cut/pen/node inherit objectId via pack_region).
    bundle['objects'] = _sync_objects_from_regions(bundle)
    m['version'] = version; g['artworkVersion'] = version
    m.pop('review', None)
    m['provenance']['lastEdit'] = request.action
    output.mkdir(parents=True, exist_ok=True)
    master_name = m['assets'].get('sourceMaster', 'source-master.png')
    for f in {master_name, 'build-settings.json'}:
        if (source / f).is_file(): shutil.copy2(source / f, output / f)
    qa = emit_bundle(output, bundle)
    if pen_warning:
        qa.setdefault('warnings', []).append(pen_warning)
        write_json(output / 'validation.json', qa)
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
    # Optional edges (stage-2 boundary contract): id/path/kind + region refs
    # must exist; boundaryStyle overrides must stay shapely-safe.
    edges = g.get('edges')
    if edges is not None:
        if not isinstance(edges, list):
            errors.append('Invalid edges.')
        else:
            edge_ids = set()
            for e in edges:
                if not isinstance(e, dict):
                    errors.append('Invalid edge entry.')
                    continue
                eid = str(e.get('id', ''))
                if not SAFE_ID.match(eid) or eid in edge_ids:
                    errors.append(f'Duplicate or unsafe edge ID: {eid}')
                edge_ids.add(eid)
                if not isinstance(e.get('d'), str) or not SAFE_D_OPEN.match(e['d']):
                    errors.append(f'Invalid edge path data: {eid}')
                if e.get('kind') not in EDGE_KINDS:
                    errors.append(f'Invalid edge kind: {eid}')
                for side in ('leftRegion', 'rightRegion'):
                    value = e.get(side)
                    if value is not None and value not in seen:
                        errors.append(f'Edge {eid} references an unknown region.')
            style = g.get('boundaryStyle')
            if style is not None:
                if not isinstance(style, dict):
                    errors.append('Invalid boundaryStyle.')
                else:
                    for kind in EDGE_KINDS:
                        part = style.get(kind)
                        if part is None:
                            continue
                        if (not isinstance(part, dict) or not isinstance(part.get('stroke'), str)
                                or not SAFE_HEX.match(part['stroke'])
                                or not isinstance(part.get('strokeWidth'), (int, float))
                                or not (part['strokeWidth'] > 0)):
                            errors.append(f'Invalid boundaryStyle.{kind}.')
                            continue
                        dash = part.get('dash')
                        if dash is not None and (not isinstance(dash, str) or len(dash) > 40):
                            errors.append(f'Invalid boundaryStyle.{kind}.dash.')
    return errors


def _runtime_geometry(g: dict) -> dict:
    """Lean runtime geometry: exactly what the game adapter needs."""
    keep = ('schemaVersion', 'geometrySchema', 'source', 'artworkId', 'artworkVersion',
            'viewBox', 'fillRule', 'stroke', 'strokeWidth', 'backend',
            'flattenTolerance', 'curveFitTolerance', 'partitionTolerance',
            'visibleRegionGeometry', 'detailPaths', 'edges', 'boundaryStyle')
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
        if (folder / 'objects.json').is_file():
            names.append('objects.json')
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
                                'geometry.edges (when present) carries boundary kinds: artwork = true master boundary (solid), subdivision = artificial gameplay boundary (light dashed); geometry.boundaryStyle overrides the default stroke/dash per kind; render region paths fill-only when edges exist. '
                                'Preserve viewBox, per-region fill rules (evenodd/nonzero), gradients, contentHash and version. Do not stretch a full painting into each region. Do not use numbered.svg as hit-test metadata. Validation does not establish copyright clearance.\n')
    (folder / '.runtime-regions.json').unlink(missing_ok=True)
    (folder / '.runtime-paint.json').unlink(missing_ok=True)
    return output.getvalue()
