"""Sanitized SVG-master import for Color Duel Art Studio.

An imported SVG master is NEVER rasterized and never retraced: curve
commands (C/Q), holes (evenodd subpaths), supported gradients,
transforms and drawing order are preserved as the authoritative master
geometry.  Flattened rings produced here are derived approximations with
an explicit tolerance, used only for classification/coverage decisions.

Security policy (upload surface):
- DTDs / entities are rejected outright.
- <script>, <foreignObject>, <image>, <iframe>, <animate*> and unknown
  elements are dropped and REPORTED, never silently kept.
- Event handler attributes (on*), external/url hrefs are stripped.
- Only a whitelisted element/attribute subset is interpreted.
- Output is serialized with ElementTree (attribute values are XML-escaped
  by the serializer) and every emitted id is GENERATED, never copied from
  the source document — an id like ``x" onload="...`` cannot survive.

Fidelity policy (what the sanitizer must preserve):
- Per-shape fill-rule (evenodd AND nonzero), fill-opacity, opacity,
  stroke/stroke-width on FILLED shapes, gradients (percentage or unit
  coordinates, objectBoundingBox and userSpaceOnUse, gradientTransform)
  and the original drawing order of shapes vs. ink.
- Unsupported constructs are rejected with a precise error instead of
  silently changing the artwork's rendered appearance.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

from shapely.geometry import Polygon
from shapely.ops import unary_union

from .curves import (
    Command, Mat, arc_to_cubics, commands_bbox, flatten_path, evenodd_area,
    fmt_num, format_path, mat_identity, mat_mul, mat_apply, parse_path,
    parse_transform, transform_commands,
)

FLAT_TOL = 0.25  # documented tolerance for derived rings (classification only)

_NAMED_COLORS = {
    'black': '#000000', 'white': '#FFFFFF', 'red': '#FF0000', 'green': '#008000',
    'blue': '#0000FF', 'yellow': '#FFFF00', 'orange': '#FFA500', 'purple': '#800080',
    'brown': '#A52A2A', 'gray': '#808080', 'grey': '#808080', 'pink': '#FFC0CB',
    'cyan': '#00FFFF', 'magenta': '#FF00FF', 'gold': '#FFD700', 'silver': '#C0C0C0',
    'teal': '#008080', 'navy': '#000080', 'ivory': '#FFFFF0', 'beige': '#F5F5DC',
    'tan': '#D2B48C', 'crimson': '#DC143C', 'salmon': '#FA8072', 'khaki': '#F0E68C',
    'violet': '#EE82EE', 'turquoise': '#40E0D0', 'lime': '#00FF00', 'olive': '#808000',
    'maroon': '#800000', 'aqua': '#00FFFF', 'fuchsia': '#FF00FF', 'coral': '#FF7F50',
    'darkgreen': '#006400', 'lightgray': '#D3D3D3', 'lightgrey': '#D3D3D3',
    'darkgray': '#A9A9A9', 'darkgrey': '#A9A9A9', 'skyblue': '#87CEEB',
}

_STYLE_KEYS = ('fill', 'fill-opacity', 'fill-rule', 'opacity', 'stroke', 'stroke-width', 'transform')
_BASIC = ('path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line')
_DROPPED_ELEMENTS = ('script', 'foreignobject', 'image', 'iframe', 'animate', 'animatetransform',
                     'animatemotion', 'animatecolor', 'set', 'text', 'use', 'style', 'clippath',
                     'mask', 'filter', 'pattern', 'symbol', 'title', 'desc', 'metadata')
_MAX_ELEMENTS = 20000
_MAX_COMMANDS = 400000


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1].lower()


def _attrs(elem: ET.Element) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in elem.attrib.items():
        out[key.rsplit('}', 1)[-1].lower()] = value
    return out


def _num(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    m = re.match(r'^\s*([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)', str(value))
    if not m:
        return default
    v = float(m.group(1))
    if not math.isfinite(v) or abs(v) > 1e6:
        raise ValueError('SVG coordinate out of supported range.')
    return v


def _gcoord(raw: str | None, default: float, units: str, axis_len: float) -> float:
    """Parse one gradient coordinate, honouring percentage values.

    objectBoundingBox percentages are fractions of 1 ("50%" -> 0.5);
    userSpaceOnUse percentages are fractions of the viewport axis
    ("50%" of the viewBox width/height).  Unsupported syntax raises a
    precise error instead of being silently misparsed.
    """
    if raw is None:
        return default
    v = str(raw).strip()
    pct = v.endswith('%')
    if pct:
        v = v[:-1].strip()
    m = re.match(r'^([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)$', v)
    if not m:
        raise ValueError(f'Unsupported gradient coordinate {raw!r}: use numbers or percentages.')
    n = float(m.group(1))
    if not math.isfinite(n) or abs(n) > 1e6:
        raise ValueError('SVG coordinate out of supported range.')
    if pct:
        n = (n / 100.0) if units == 'objectBoundingBox' else (n / 100.0 * axis_len)
    return n


def _parse_color(value: str | None) -> Optional[str]:
    """Normalize a supported CSS/SVG color to #RRGGBB, else None."""
    if value is None:
        return None
    v = value.strip().lower()
    if v in ('none', 'transparent', ''):
        return None
    if v.startswith('#'):
        hexpart = v[1:]
        if re.fullmatch(r'[0-9a-f]{3}', hexpart):
            return '#' + ''.join(c + c for c in hexpart).upper()
        if re.fullmatch(r'[0-9a-f]{6}', hexpart):
            return '#' + hexpart.upper()
        return None
    m = re.fullmatch(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', v)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
        return f'#{r:02X}{g:02X}{b:02X}'
    m = re.fullmatch(r'rgb\(\s*([\d.]+)%\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%\s*\)', v)
    if m:
        r, g, b = (min(255, round(float(x) * 2.55)) for x in m.groups())
        return f'#{r:02X}{g:02X}{b:02X}'
    return _NAMED_COLORS.get(v)


def _style_dict(style: str | None) -> Dict[str, str]:
    if not style:
        return {}
    out: Dict[str, str] = {}
    for chunk in style.split(';'):
        if ':' not in chunk:
            continue
        key, value = chunk.split(':', 1)
        key = key.strip().lower()
        if key in _STYLE_KEYS:
            out[key] = value.strip()
    return out


class _Context:
    __slots__ = ('matrix', 'opacity', 'fill', 'fill_opacity', 'fill_rule', 'stroke', 'stroke_width',
                 'object_ref', 'object_name', 'object_parent')

    def __init__(self) -> None:
        self.matrix: Mat = mat_identity()
        self.opacity = 1.0
        self.fill: Optional[str] = '#000000'   # SVG default fill is black
        self.fill_opacity: Optional[float] = 1.0
        self.fill_rule = 'nonzero'
        self.stroke: Optional[str] = None
        self.stroke_width = 1.0
        # Semantic object context (data-cd-object / data-cd-name on any <g>):
        # inherited by descendant shapes; this is how object identity survives
        # the master -> compile boundary (the objects.json authoring layer).
        # A nested data-cd-object group records the enclosing object as parent.
        self.object_ref: Optional[str] = None
        self.object_name: Optional[str] = None
        self.object_parent: Optional[str] = None

    def child(self, attrs: Dict[str, str]) -> '_Context':
        style = _style_dict(attrs.get('style'))
        merged = dict(attrs)
        merged.update(style)
        ctx = _Context()
        ctx.matrix = self.matrix
        if 'transform' in merged:
            ctx.matrix = mat_mul(self.matrix, parse_transform(merged['transform']))
        ctx.opacity = self.opacity * _num(merged.get('opacity'), 1.0)
        ctx.fill = _parse_color(merged['fill']) if 'fill' in merged else self.fill
        if 'fill' in merged and merged.get('fill', '').strip().lower().startswith('url('):
            ctx.fill = self.fill  # url() resolved later
        fo = merged.get('fill-opacity')
        ctx.fill_opacity = self.fill_opacity if fo is None else max(0.0, min(1.0, _num(fo, 1.0)))
        ctx.fill_rule = merged.get('fill-rule', self.fill_rule).strip().lower() or 'nonzero'
        ctx.stroke = _parse_color(merged.get('stroke')) if 'stroke' in merged else self.stroke
        sw = merged.get('stroke-width')
        ctx.stroke_width = self.stroke_width if sw is None else max(0.0, _num(sw, 1.0))
        obj = (merged.get('data-cd-object') or '').strip()
        if obj:
            ctx.object_ref = obj[:64]
            nm = (merged.get('data-cd-name') or '').strip()
            ctx.object_name = nm[:80] or None
            explicit_parent = (merged.get('data-cd-parent') or '').strip()
            if explicit_parent:
                ctx.object_parent = explicit_parent[:64]
            else:
                ctx.object_parent = self.object_ref if self.object_ref and self.object_ref != obj else self.object_parent
        else:
            ctx.object_ref = self.object_ref
            ctx.object_name = self.object_name
            ctx.object_parent = self.object_parent
        return ctx

    def fill_ref(self, attrs: Dict[str, str]) -> Optional[str]:
        """Gradient reference from fill="url(#id)" (attribute or inline style)."""
        raw = (attrs.get('fill') or '').strip()
        if not raw:
            raw = (_style_dict(attrs.get('style')).get('fill') or '').strip()
        raw = raw.strip()
        if raw.lower().startswith('url('):
            m = re.search(r'url\(\s*["\']?#([^"\')\s]+)["\']?\s*\)', raw)
            if m:
                return m.group(1)
        return None


def _rect_commands(x: float, y: float, w: float, h: float, rx: float, ry: float) -> List[Command]:
    if w <= 0 or h <= 0:
        raise ValueError('Degenerate rect.')
    rx = max(0.0, min(rx if rx else ry, w / 2))
    ry = max(0.0, min(ry if ry else rx, h / 2))
    if rx <= 0 or ry <= 0:
        return [('M', x, y), ('L', x + w, y), ('L', x + w, y + h), ('L', x, y + h), ('Z')]
    # rounded rect: straight edges + cubic corners (kappa)
    k = 0.5522847498
    cmds: List[Command] = [
        ('M', x + rx, y),
        ('L', x + w - rx, y),
        ('C', x + w - rx + k * rx, y, x + w, y + ry - k * ry, x + w, y + ry),
        ('L', x + w, y + h - ry),
        ('C', x + w, y + h - ry + k * ry, x + w - rx + k * rx, y + h, x + w - rx, y + h),
        ('L', x + rx, y + h),
        ('C', x + rx - k * rx, y + h, x, y + h - ry + k * ry, x, y + h - ry),
        ('L', x, y + ry),
        ('C', x, y + ry - k * ry, x + rx - k * rx, y, x + rx, y),
        ('Z',
        )]
    return cmds


def _ellipse_commands(cx: float, cy: float, rx: float, ry: float) -> List[Command]:
    if rx <= 0 or ry <= 0:
        raise ValueError('Degenerate ellipse.')
    cmds: List[Command] = [('M', cx + rx, cy)]
    for start in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        arc = arc_to_cubics(cx + rx * math.cos(start), cy + ry * math.sin(start),
                            rx, ry, 0, 0, 1,
                            cx + rx * math.cos(start + math.pi / 2), cy + ry * math.sin(start + math.pi / 2))
        cmds.extend(arc)
    cmds.append(('Z',))
    return cmds


def _points_commands(raw: str, close: bool) -> List[Command]:
    nums = re.findall(r'[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?', raw or '')
    if len(nums) < 4 or len(nums) % 2:
        raise ValueError('Degenerate polygon/polyline points.')
    pts = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums), 2)]
    for p in pts:
        if not math.isfinite(p[0]) or abs(p[0]) > 1e6 or abs(p[1]) > 1e6:
            raise ValueError('SVG coordinate out of supported range.')
    cmds: List[Command] = [('M', pts[0][0], pts[0][1])]
    cmds.extend(('L', x, y) for x, y in pts[1:])
    if close:
        cmds.append(('Z',))
    return cmds


def _element_commands(tag: str, attrs: Dict[str, str]) -> List[Command]:
    if tag == 'path':
        return parse_path(attrs.get('d', ''))
    if tag == 'rect':
        return _rect_commands(_num(attrs.get('x')), _num(attrs.get('y')),
                              _num(attrs.get('width'), -1), _num(attrs.get('height'), -1),
                              _num(attrs.get('rx'), 0.0), _num(attrs.get('ry'), 0.0))
    if tag == 'circle':
        cx, cy, r = _num(attrs.get('cx')), _num(attrs.get('cy')), _num(attrs.get('r'), -1)
        return _ellipse_commands(cx, cy, r, r)
    if tag == 'ellipse':
        return _ellipse_commands(_num(attrs.get('cx')), _num(attrs.get('cy')),
                                 _num(attrs.get('rx'), -1), _num(attrs.get('ry'), -1))
    if tag == 'polygon':
        return _points_commands(attrs.get('points', ''), close=True)
    if tag == 'polyline':
        return _points_commands(attrs.get('points', ''), close=False)
    if tag == 'line':
        return [('M', _num(attrs.get('x1')), _num(attrs.get('y1'))),
                ('L', _num(attrs.get('x2')), _num(attrs.get('y2')))]
    raise ValueError(f'Unsupported geometry element {tag}.')


class MasterDoc:
    """Parsed, sanitized SVG master — the authoritative curve source."""

    def __init__(self) -> None:
        self.view_box: Tuple[float, float, float, float] = (0, 0, 576, 768)
        self.shapes: List[dict] = []       # fillable shapes, document order
        self.ink_shapes: List[dict] = []   # stroke-only line art, document order
        self.gradients: Dict[str, dict] = {}
        self.report: dict = {'removed': [], 'warnings': [], 'rasterized': False,
                             'notes': ['Imported SVG masters keep original curve commands; '
                                       'nothing is rasterized or retraced.']}
        self.source_text: str = ''

    def summary(self) -> dict:
        curved = sum(1 for s in self.shapes + self.ink_shapes
                     if any(c[0] in ('C', 'Q') for c in s['commands']))
        total_cmds = sum(len(s['commands']) for s in self.shapes + self.ink_shapes)
        curved_cmds = sum(1 for s in self.shapes + self.ink_shapes for c in s['commands']
                          if c[0] in ('C', 'Q'))
        return {
            'viewBox': list(self.view_box),
            'shapes': len(self.shapes),
            'inkShapes': len(self.ink_shapes),
            'gradients': len(self.gradients),
            'curvedShapes': curved,
            'commands': total_cmds,
            'curvedCommands': curved_cmds,
            'hiddenShapes': sum(1 for s in self.shapes if s.get('hidden')),
            'removed': self.report['removed'],
            'warnings': self.report['warnings'],
            'rasterized': False,
        }


def _collect_gradients(root: ET.Element, doc: MasterDoc) -> None:
    vw, vh = float(doc.view_box[2]), float(doc.view_box[3])
    diag = math.sqrt((vw * vw + vh * vh) / 2.0)
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag not in ('lineargradient', 'radialgradient'):
            continue
        attrs = _attrs(elem)
        gid = attrs.get('id')
        if not gid:
            doc.report['warnings'].append('A gradient without id was dropped.')
            continue
        units = attrs.get('gradientunits', 'objectBoundingBox').strip()
        if units not in ('objectBoundingBox', 'userSpaceOnUse'):
            raise ValueError(f'Gradient {gid}: unsupported gradientUnits {units!r}.')
        stops = []
        for child in elem:
            if _local(child.tag) != 'stop':
                continue
            sattrs = _attrs(child)
            sstyle = _style_dict(sattrs.get('style'))
            merged = {**sattrs, **sstyle}
            raw_off = merged.get('offset', '0')
            m = re.match(r'^\s*([\d.]++)\s*(%)?', str(raw_off))
            off = float(m.group(1)) / (100.0 if m.group(2) else 1.0) if m else 0.0
            color = _parse_color(merged.get('stop-color', '#000000'))
            opacity = max(0.0, min(1.0, _num(merged.get('stop-opacity'), 1.0)))
            stops.append({'offset': round(off, 4), 'color': color or '#000000', 'opacity': opacity})
        stops.sort(key=lambda s: s['offset'])
        gtransform = parse_transform(attrs.get('gradientTransform'))
        entry = {'id': gid, 'type': 'linear' if tag == 'lineargradient' else 'radial',
                 'units': units, 'transform': gtransform, 'stops': stops}
        if tag == 'lineargradient':
            entry['x1'] = _gcoord(attrs.get('x1'), 0.0, units, vw)
            entry['y1'] = _gcoord(attrs.get('y1'), 0.0, units, vh)
            entry['x2'] = _gcoord(attrs.get('x2'), 1.0, units, vw)
            entry['y2'] = _gcoord(attrs.get('y2'), 0.0, units, vh)
        else:
            entry['cx'] = _gcoord(attrs.get('cx'), 0.5, units, vw)
            entry['cy'] = _gcoord(attrs.get('cy'), 0.5, units, vh)
            entry['r'] = _gcoord(attrs.get('r'), 0.5, units, diag)
            entry['fx'] = _gcoord(attrs.get('fx'), entry['cx'], units, vw)
            entry['fy'] = _gcoord(attrs.get('fy'), entry['cy'], units, vh)
        if not stops:
            doc.report['warnings'].append(f'Gradient {gid} has no stops and was dropped.')
            continue
        doc.gradients[gid] = entry


def _resolve_gradient(entry: dict, bbox: Tuple[float, float, float, float]) -> dict:
    """Resolve objectBoundingBox units to user space for one shape bbox."""
    x0, y0, x1, y1 = bbox
    w = max(1e-6, x1 - x0)
    h = max(1e-6, y1 - y0)
    tr: Mat = entry['transform']

    def uv(px: float, py: float) -> Tuple[float, float]:
        if entry['units'] == 'userSpaceOnUse':
            return mat_apply(tr, (px, py))
        return mat_apply(tr, (x0 + px * w, y0 + py * h))

    resolved = {'type': entry['type'], 'stops': entry['stops'], 'units': 'userSpaceOnUse'}
    if entry['type'] == 'linear':
        a = uv(entry['x1'], entry['y1'])
        b = uv(entry['x2'], entry['y2'])
        resolved['x1'], resolved['y1'] = round(a[0], 3), round(a[1], 3)
        resolved['x2'], resolved['y2'] = round(b[0], 3), round(b[1], 3)
    else:
        c = uv(entry['cx'], entry['cy'])
        f = uv(entry['fx'], entry['fy'])
        # radius: scale relative bbox diagonal-ish; userSpace r is used directly
        if entry['units'] == 'userSpaceOnUse':
            r = entry['r']
        else:
            r = entry['r'] * math.hypot(w, h) / math.sqrt(2)
        resolved['cx'], resolved['cy'] = round(c[0], 3), round(c[1], 3)
        resolved['r'] = round(r, 3)
        resolved['fx'], resolved['fy'] = round(f[0], 3), round(f[1], 3)
    return resolved


def _shapely_poly(rings) -> Polygon:
    if not rings:
        return Polygon()
    prepped = [[(float(x), float(y)) for x, y in ring] for ring in rings if len(ring) >= 3]
    if not prepped:
        return Polygon()
    outer = prepped[0]
    holes = prepped[1:]
    try:
        poly = Polygon(outer, holes)
        if not poly.is_valid:
            from shapely import make_valid
            poly = make_valid(poly)
        return poly
    except Exception:
        return Polygon(outer)


def shape_solids(rings, fill_rule: str):
    """Solid polygon(s) of flattened rings under the shape's fill rule.

    Used for area, coverage and label decisions; evenodd nests by parity,
    nonzero by cumulative ring orientation (a same-winding nested subpath
    is a union, not a hole, exactly like SVG's nonzero rule).
    """
    from .curves import solid_polygons
    try:
        return solid_polygons(rings, 'nonzero' if fill_rule == 'nonzero' else 'evenodd')
    except Exception:
        return [_shapely_poly(rings)]


def _safe_gid(order: int, ref: str) -> str:
    """Generated, charset-safe internal gradient id (never source text)."""
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '-', ref or 'grad')[:48].strip('-') or 'grad'
    return f'g-{order:04d}-{cleaned}'


def _solid_union(solids) -> Polygon:
    """Union of rule-aware solid polygons, tolerant of bad inputs."""
    from shapely import make_valid
    cleaned = []
    for g in solids:
        if g is None or g.is_empty:
            continue
        if not g.is_valid:
            g = make_valid(g)
        if g.geom_type == 'Polygon':
            if g.area > 0:
                cleaned.append(g)
        elif g.geom_type in ('MultiPolygon', 'GeometryCollection'):
            for child in g.geoms:
                if child.geom_type == 'Polygon' and child.area > 0:
                    cleaned.append(child)
    if not cleaned:
        return Polygon()
    if len(cleaned) == 1:
        return cleaned[0]
    try:
        return unary_union(cleaned)
    except Exception:
        return cleaned[0]


def import_master(text: str) -> MasterDoc:
    """Parse + sanitize SVG text into a MasterDoc (never rasterizes)."""
    if not isinstance(text, str):
        raise ValueError('SVG master must be text.')
    if len(text.encode('utf-8')) > 12 * 1024 * 1024:
        raise ValueError('Use an SVG file under 12 MB.')
    lowered = text.lower()
    if '<!doctype' in lowered or '<!entity' in lowered:
        raise ValueError('DTDs and entity declarations are not accepted in SVG masters.')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f'The SVG file is not well-formed XML: {exc}') from exc
    if _local(root.tag) != 'svg':
        raise ValueError('The root element must be <svg>.')

    doc = MasterDoc()
    doc.source_text = text

    # viewBox / size
    attrs = _attrs(root)
    if 'viewbox' in attrs:
        parts = re.findall(r'[-+]?(?:\d*\.\d+|\d+\.?)', attrs['viewbox'])
        if len(parts) == 4:
            vb = [float(p) for p in parts]
            if vb[2] > 0 and vb[3] > 0:
                doc.view_box = (vb[0], vb[1], vb[2], vb[3])
            else:
                raise ValueError('viewBox must have positive width and height.')
        else:
            raise ValueError('viewBox must contain four numbers.')
    else:
        w = _num(attrs.get('width'), 0.0)
        h = _num(attrs.get('height'), 0.0)
        if w > 0 and h > 0:
            doc.view_box = (0.0, 0.0, w, h)
        else:
            raise ValueError('The SVG needs a viewBox or width/height.')

    _collect_gradients(root, doc)
    id_map: Dict[str, ET.Element] = {a['id']: elem for elem in root.iter()
                                      if (a := _attrs(elem)).get('id')}
    total_elements = sum(1 for _ in root.iter())
    if total_elements > _MAX_ELEMENTS:
        raise ValueError(f'SVG exceeds {_MAX_ELEMENTS} elements.')

    order = 0
    seen_use: set = set()
    counter = {'n': 0}

    def order_counter() -> int:
        counter['n'] += 1
        return counter['n'] - 1

    def walk(elem: ET.Element, ctx: _Context) -> None:
        for child in elem:
            tag = _local(child.tag)
            cattrs = _attrs(child)
            if tag in ('defs', 'lineargradient', 'radialgradient', 'stop'):
                continue  # definitions, not drawable
            if tag in _DROPPED_ELEMENTS and tag != 'use':
                label = tag if tag not in ('title', 'desc', 'metadata') else None
                if tag in ('script', 'foreignobject', 'image', 'iframe', 'style', 'text',
                           'clippath', 'mask', 'filter', 'pattern', 'symbol', 'animate',
                           'animatetransform', 'animatemotion', 'animatecolor', 'set'):
                    doc.report['removed'].append(f'<{tag}>')
                continue
            if tag == 'g':
                walk(child, ctx.child(cattrs))
                continue
            if tag == 'use':
                href = cattrs.get('href') or cattrs.get('xlink:href')
                if not href or not href.startswith('#'):
                    doc.report['removed'].append('<use> (external or missing reference)')
                    continue
                target = id_map.get(href[1:])
                if target is None or id(target) in seen_use:
                    doc.report['warnings'].append(f'<use {href}> could not be resolved and was skipped.')
                    continue
                seen_use.add(id(target))
                use_ctx = ctx.child(cattrs)
                ux, uy = _num(cattrs.get('x')), _num(cattrs.get('y'))
                use_ctx.matrix = mat_mul(use_ctx.matrix, (1.0, 0.0, 0.0, 1.0, ux, uy))
                ttag = _local(target.tag)
                if ttag == 'g':
                    walk(target, use_ctx)
                elif ttag in _BASIC:
                    _emit_shape(target, use_ctx, ttag, doc, order_counter())
                continue
            if tag in _BASIC:
                _emit_shape(child, ctx, tag, doc, order_counter())
                continue
            doc.report['removed'].append(f'<{tag}>')

    def _emit_shape(elem: ET.Element, ctx: _Context, tag: str, doc: MasterDoc, ordn: int) -> None:
        sattrs = _attrs(elem)
        sctx = ctx.child(sattrs)
        try:
            cmds = _element_commands(tag, sattrs)
        except ValueError as exc:
            doc.report['warnings'].append(f'A <{tag}> was skipped: {exc}')
            return
        cmds = transform_commands(cmds, sctx.matrix)
        if not cmds or sum(1 for c in cmds if c[0] != 'Z') < 1:
            return
        if sum(len(s['commands']) for s in doc.shapes) + sum(len(s['commands']) for s in doc.ink_shapes) + len(cmds) > _MAX_COMMANDS:
            raise ValueError('SVG master is too complex (command budget exceeded).')
        shape_id = f's{ordn:04d}'          # generated: never emitted from source text
        source_id = sattrs.get('id')        # kept for reports only
        role = sattrs.get('data-cd-role') or None
        if role not in (None, 'gameplay', 'shading', 'ink'):
            role = None
        fill_ref = sctx.fill_ref(sattrs) or ctx.fill_ref(sattrs)
        flat = flatten_path(cmds, FLAT_TOL)
        bbox = commands_bbox(cmds)
        has_area = len(flat) >= 1 and any(len(r) >= 3 for r in flat)
        fillable = sctx.fill is not None or fill_ref is not None
        stroke_only = (not fillable) and sctx.stroke is not None
        base = {
            'order': ordn, 'id': shape_id, 'sourceId': source_id,
            'kind': 'ink' if (stroke_only or role == 'ink') else 'shape',
            'role': role, 'element': tag,
            'objectRef': ctx.object_ref, 'objectName': ctx.object_name,
            'objectParent': ctx.object_parent,
            'commands': cmds, 'd': format_path(cmds),
            'fillRule': 'evenodd' if sctx.fill_rule == 'evenodd' else 'nonzero',
            'bbox': bbox, 'rings': flat, 'opacity': round(sctx.opacity, 4),
        }
        if stroke_only or role == 'ink':
            base.update({'stroke': sctx.stroke, 'strokeWidth': round(sctx.stroke_width, 3)})
            doc.ink_shapes.append(base)
            return
        if not has_area:
            if sctx.stroke is not None:
                base.update({'stroke': sctx.stroke, 'strokeWidth': round(sctx.stroke_width, 3)})
                doc.ink_shapes.append(base)
            else:
                doc.report['warnings'].append(f'Shape {shape_id} has no fill and no stroke; skipped.')
            return
        solids = shape_solids(flat, base['fillRule'])
        poly = _solid_union(solids)
        if poly.is_empty:
            poly = _shapely_poly(flat)
        area = float(poly.area) if not poly.is_empty else evenodd_area(flat)
        fill = sctx.fill or '#000000'
        if fill_ref:
            if fill_ref not in doc.gradients:
                doc.report['warnings'].append(
                    f'Shape {shape_id} references missing gradient #{fill_ref}; painted with flat fallback.')
            else:
                fill = None  # resolved per-shape below
        base.update({
            'fill': fill, 'gradientRef': fill_ref,
            'fillOpacity': round(sctx.fill_opacity, 4),
            'area': area, 'hidden': False,
            'stroke': sctx.stroke, 'strokeWidth': round(sctx.stroke_width, 3),
        })
        doc.shapes.append(base)

    walk(root, _Context())

    # resolve gradient references into per-shape user-space instances
    for shape in doc.shapes:
        ref = shape.get('gradientRef')
        if not ref:
            continue
        entry = doc.gradients.get(ref)
        if not entry:
            shape['fill'] = shape['fill'] or '#808080'
            continue
        resolved = _resolve_gradient(entry, shape['bbox'])
        shape['gradient'] = {
            'id': _safe_gid(shape['order'], ref),
            'ref': ref,
            **resolved,
        }
        # representative palette color: average of stops
        stops = entry['stops']
        if stops:
            mid = stops[len(stops) // 2]
            shape['fill'] = mid['color']
        else:
            shape['fill'] = '#808080'

    # hidden-shape exclusion: fully covered by opaque shapes ABOVE (z-order).
    # Solid area honours the shape's own fill rule (nonzero unions count).
    opaque = [s for s in doc.shapes
              if (s.get('fill') or s.get('gradientRef')) and s.get('fillOpacity', 1.0) * s.get('opacity', 1.0) >= 0.999]
    for idx, shape in enumerate(doc.shapes):
        if shape.get('fillOpacity', 1.0) * shape.get('opacity', 1.0) < 0.999 or not shape.get('fill') and not shape.get('gradientRef'):
            continue
        solids = shape_solids(shape['rings'], shape['fillRule'])
        own = _solid_union(solids)
        if own.is_empty or own.area <= 0:
            continue
        covers = [s for s in opaque if s['order'] > shape['order'] and s is not shape]
        if not covers:
            continue
        try:
            above = unary_union([_solid_union(shape_solids(s['rings'], s['fillRule'])) for s in covers])
            visible = own.difference(above)
            ratio = 0.0 if visible.is_empty else visible.area / max(own.area, 1e-9)
            if ratio < 0.02:
                shape['hidden'] = True
                shape['hiddenBy'] = [s['id'] for s in covers[:3]]
        except Exception:
            continue

    if not doc.shapes and not doc.ink_shapes:
        raise ValueError('The SVG master contains no supported drawable shapes.')
    return doc


def emit_master_svg(doc: MasterDoc) -> str:
    """Serialize the sanitized master as standalone SVG (preview-safe).

    Safety + fidelity contract:
    - Built as an ElementTree and serialized by ET, so every attribute
      value is XML-escaped properly (attribute-context injection such as
      an id breaking out into an onload handler is structurally impossible).
    - Every emitted id is GENERATED (s0001 / g-0001-...); source ids are
      never copied into the output.
    - Original drawing order is preserved: filled shapes and ink are
      interleaved in one ordered stream (ink is not hoisted above fills).
    - Filled shapes keep their stroke, fill-rule, fill-opacity and opacity.
    """
    x, y, w, h = doc.view_box
    root = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'viewBox': f'{fmt_num(x)} {fmt_num(y)} {fmt_num(w)} {fmt_num(h)}',
        'width': fmt_num(w), 'height': fmt_num(h),
    })
    grads = [s['gradient'] for s in doc.shapes if s.get('gradient')]
    if grads:
        defs = ET.SubElement(root, 'defs')
        for g in grads:
            if g['type'] == 'linear':
                node = ET.SubElement(defs, 'linearGradient', {
                    'id': g['id'], 'gradientUnits': 'userSpaceOnUse',
                    'x1': fmt_num(g['x1']), 'y1': fmt_num(g['y1']),
                    'x2': fmt_num(g['x2']), 'y2': fmt_num(g['y2'])})
            else:
                node = ET.SubElement(defs, 'radialGradient', {
                    'id': g['id'], 'gradientUnits': 'userSpaceOnUse',
                    'cx': fmt_num(g['cx']), 'cy': fmt_num(g['cy']), 'r': fmt_num(g['r']),
                    'fx': fmt_num(g['fx']), 'fy': fmt_num(g['fy'])})
            for s in g['stops']:
                stop = {'offset': fmt_num(s['offset'], 4), 'stop-color': s['color']}
                if s.get('opacity', 1) != 1:
                    stop['stop-opacity'] = fmt_num(s['opacity'], 3)
                ET.SubElement(node, 'stop', stop)
    # One ordered stream: shapes and ink interleaved by document order.
    # Shapes carrying the same objectRef are wrapped in <g data-cd-object>
    # runs (document order is preserved — fragments never interleave), so
    # object identity survives the sanitize round-trip and any later rebuild
    # of this master reconstructs the same objects.json.
    stream = [s for s in sorted(doc.shapes + doc.ink_shapes, key=lambda t: t['order'])
              if not s.get('hidden')]

    def shape_node(parent: ET.Element, s: dict) -> None:
        if s.get('kind') == 'ink':
            ET.SubElement(parent, 'path', {
                'id': s['id'], 'fill': 'none',
                'stroke': s.get('stroke') or '#29383E',
                'stroke-width': fmt_num(s.get('strokeWidth', 1.5)),
                'stroke-linecap': 'round', 'stroke-linejoin': 'round',
                'd': s['d']})
            return
        fill = s.get('fill') or '#000000'
        if s.get('gradient'):
            fill = f'url(#{s["gradient"]["id"]})'
        attrs = {'id': s['id'], 'fill': fill, 'fill-rule': s['fillRule'], 'd': s['d']}
        if s.get('fillOpacity', 1.0) < 0.999:
            attrs['fill-opacity'] = fmt_num(s['fillOpacity'], 3)
        if s.get('opacity', 1.0) < 0.999:
            attrs['opacity'] = fmt_num(s['opacity'], 3)
        if s.get('stroke') and s.get('strokeWidth', 0) > 0:
            attrs['stroke'] = s['stroke']
            attrs['stroke-width'] = fmt_num(s['strokeWidth'])
            attrs['stroke-linejoin'] = 'round'
        ET.SubElement(parent, 'path', attrs)

    i = 0
    while i < len(stream):
        s = stream[i]
        ref = s.get('objectRef')
        if not ref:
            shape_node(root, s)
            i += 1
            continue
        run = [s]
        j = i + 1
        while j < len(stream) and stream[j].get('objectRef') == ref:
            run.append(stream[j])
            j += 1
        gattrs = {'data-cd-object': ref}
        name = next((t.get('objectName') for t in run if t.get('objectName')), None)
        if name:
            gattrs['data-cd-name'] = name
        parent = next((t.get('objectParent') for t in run if t.get('objectParent')), None)
        if parent:
            gattrs['data-cd-parent'] = parent
        gnode = ET.SubElement(root, 'g', gattrs)
        for t in run:
            shape_node(gnode, t)
        i = j
    return ET.tostring(root, encoding='unicode')


def clean_svg(data: bytes, destination: Path) -> dict:
    """Upload entry point: sanitize an SVG master and persist it."""
    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError('Use an SVG file under 12 MB.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('The SVG file must be UTF-8 text.') from exc
    doc = import_master(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sanitized = emit_master_svg(doc)
    destination.write_text(sanitized, encoding='utf-8')
    summary = doc.summary()
    if not any(c[0] in ('C', 'Q') for s in doc.shapes for c in s['commands']):
        summary['warnings'] = summary['warnings'] + [
            'This master contains no curve commands; the curve-preserving import had nothing to preserve.']
    return {
        'width': round(doc.view_box[2], 3),
        'height': round(doc.view_box[3], 3),
        'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
        'kind': 'svg',
        'summary': summary,
    }
