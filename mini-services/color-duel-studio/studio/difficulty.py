"""Difficulty Optimization engine.

optimize_gameplay_difficulty() moves ONLY the gameplay layer of a compiled
bundle — regions, region labels, object subdivision budgets and the derived
difficulty metrics — toward a requested tier. The artwork is frozen: every
accepted iteration must leave paint paths and objects.shapeIds byte-identical
and pass the geometry QA; any violation rolls back to the last healthy state.

Two directions, one contract:

    current > target  ->  conservative semantic MERGE
                          (same objectId, adjacent, same palette first, then
                          closest palette ΔE; never across objects)
    current < target  ->  semantic SPLIT
                          (the existing object-budget auto-subdivider:
                          area × detailWeight × complexity, clamped to
                          [minRegions, maxRegions], min playable area kept)

Bounded deterministic loop (default 3 iterations): measure -> move -> QA ->
measure again. Quality always outranks the requested label: label conflicts,
below-minimum tap targets, a tiny-region explosion or a no-move ceiling
reject a candidate, and an unreachable target reports the best safe result
with human-readable reasons instead of forcing the number. Same bundle +
same target => identical output.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from copy import deepcopy
from typing import Callable, Dict, List, Tuple

import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.validation import make_valid
from shapely.strtree import STRtree

from .models import BuildSettings
from .pipeline import (
    _RegionIndex,
    _auto_subdivide,
    _emit_edge,
    _lab_of,
    _prune_edges,
    difficulty_profile,
    label_conflict,
    make_label,
    pack_region,
    region_polygon,
    validate_bundle,
)

# Canonical tier geometry (single source; generation.py re-exports these as
# DIFFICULTY_RANGES / DIFFICULTY_INITIAL_TARGETS).
TIER_RANGES: Dict[str, Tuple[int, int]] = {
    'easy': (100, 180),
    'medium': (180, 320),
    'hard': (320, 550),
    'master': (550, 800),
}
TIER_TARGETS: Dict[str, int] = {'easy': 140, 'medium': 250, 'hard': 430, 'master': 650}

# Safety ceilings (quality outranks the requested difficulty label):
# - no region may shrink below the build's minimum playable area
# - labels must never conflict (clearance gone / font unreadable)
# - worst-case zoom above 9x makes targets effectively untappable
# - more than 18% of regions under the tiny floor (max(70px², 2×minimum))
#   is microscopic garbage, not difficulty — and would also sink the
#   Convert gameReadiness gate downstream.
MAX_REQUIRED_ZOOM = 9.0
MAX_TINY_RATIO = 0.18

# Split pieces must stay at or above the tiny floor: the optimizer splits
# with a doubled build minimum, so it stops at a safe region ceiling instead
# of shredding the board into microscopic targets when a tier is unreachable.
SPLIT_MIN_FACTOR = 2

_SETTING_KEYS = ('min_region_pixels', 'curve_tolerance', 'corner_angle_deg',
                 'min_label_radius', 'palette_colors', 'backend', 'max_edge')


def _palette_distance(hex_a: str, hex_b: str, labs: Dict[Tuple[str, str], float]) -> float:
    """Mean CIELAB ΔE between two #RRGGBB colors (memoized per optimizer run)."""
    key = (hex_a, hex_b) if hex_a <= hex_b else (hex_b, hex_a)
    if key not in labs:
        try:
            ra = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
            rb = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
            labs[key] = float(abs(_lab_of(np.array([ra], dtype=float))[0]
                                  - _lab_of(np.array([rb], dtype=float))[0]).max())
        except Exception:
            labs[key] = 442.0
    return labs[key]


def _label_conflict(label: dict) -> bool:
    return label_conflict(label)


def _conflicts(regions: list) -> int:
    """Number of playable regions carrying an unreadable label."""
    return sum(1 for r in regions if label_conflict(r.get('label') or {}))


def _merge_down(regions: list, geometry: dict, target: int,
                palette: list) -> int:
    """Conservatively merge gameplay regions DOWN toward ``target``.

    Hard rules: same objectId (never across semantic objects), edge-adjacent,
    merged geometry stays one valid polygon and the merged label stays
    readable. Preference order per round: same palette group, then the
    closest palette ΔE; ties break to the smallest combined area and region
    ids (deterministic). Conflict-free within a round (a region merges at
    most once); adjacencies created by a round are picked up by the next.
    """
    hexes = {e['id']: str(e.get('hex') or '#808080').upper() for e in palette}
    labs: Dict[Tuple[str, str], float] = {}
    merged_total = 0
    while len(regions) > target:
        polys = {r['id']: region_polygon(r) for r in regions}
        live = [r for r in regions if not polys[r['id']].is_empty]
        if len(live) < 2:
            break
        tree = STRtree([polys[r['id']] for r in live])
        candidates = []
        for i, ra in enumerate(live):
            pa = polys[ra['id']]
            for j in np.atleast_1d(tree.query(pa.buffer(0.6))):
                j = int(j)
                if j <= i:
                    continue
                rb = live[j]
                if (rb.get('objectId') or 'unassigned') != (ra.get('objectId') or 'unassigned'):
                    continue                      # never merge across objects
                pb = polys[rb['id']]
                try:
                    if not pa.touches(pb):
                        continue
                except Exception:
                    continue
                de = (0.0 if ra['paletteId'] == rb['paletteId']
                      else _palette_distance(hexes.get(ra['paletteId'], '#808080'),
                                             hexes.get(rb['paletteId'], '#808080'), labs))
                # Viability is decided at candidate time: corner-touching
                # cells (MultiPolygon unions) and unreadable merged labels
                # are dropped WITHOUT locking their regions, so the round can
                # still merge those regions with their edge neighbours.
                union = unary_union([make_valid(pa), make_valid(pb)])
                if union.geom_type != 'Polygon' or not union.is_valid or union.area <= 0:
                    continue
                # larger piece wins the palette group (deterministic tie-break)
                keep = ra if (ra['area'], ra['id']) >= (rb['area'], rb['id']) else rb
                label = make_label(union, keep['paletteId'])
                if label_conflict(label):
                    continue
                candidates.append(((de, ra['area'] + rb['area'],
                                    min(ra['id'], rb['id']), max(ra['id'], rb['id'])),
                                   ra, rb, union, keep, label))
        if not candidates:
            break
        candidates.sort(key=lambda c: c[0])
        used: set = set()
        removals: set = set()
        additions = []
        for _key, ra, rb, union, keep, label in candidates:
            if ra['id'] in used or rb['id'] in used:
                continue
            merged_id = 'r-om-' + hashlib.sha256(
                '|'.join(sorted((ra['id'], rb['id']))).encode()).hexdigest()[:12]
            # Exact pixel-edge master (fit=False): the gameplay layer is
            # invisible on top of the frozen paint, and an exact union keeps
            # the raster partition watertight for the roundtrip QA — a
            # curve refit here would shave sub-pixel gaps against the
            # pixel-exact neighbours.
            merged = pack_region(union, merged_id, keep['paletteId'],
                                 ra.get('objectId') or 'unassigned', label=label,
                                 source='difficulty-merge', fit_tolerance=0.0, fit=False)
            if ra.get('masterShapeId') and ra.get('masterShapeId') == rb.get('masterShapeId'):
                merged['masterShapeId'] = ra['masterShapeId']
            used.update((ra['id'], rb['id']))
            removals.update((ra['id'], rb['id']))
            additions.append(merged)
        if not additions:
            break
        regions[:] = [r for r in regions if r['id'] not in removals]
        regions.extend(additions)
        _prune_edges(geometry, removals)
        merged_total += len(additions)
    return merged_total


def _rebuild_gameplay_edges(geometry: dict) -> list:
    """Full deterministic rebuild of an edges overlay after gameplay moves.

    Merges/subdivisions invalidate old edge entries: after the move, the
    boundary between two regions of the SAME object is a subdivision edge;
    a boundary against a different object or the canvas is an artwork edge
    (the same semantics the compiler and the edit actions use). Consecutive
    boundary segments sharing (kind, neighbour) coalesce into one entry.
    Returns the new edges list (the geometry is not modified)."""
    regions = geometry['regions']
    index = _RegionIndex(regions)
    obj_of = {r['id']: r.get('objectId') for r in regions}
    prepared = {}
    for r in regions:
        poly = region_polygon(r)
        prepared[r['id']] = prep(poly) if not poly.is_empty else None
    edges: list = []
    for r in regions:
        poly = region_polygon(r)
        if poly.is_empty:
            continue
        mine = prepared[r['id']]
        rings = [list(poly.exterior.coords)] + [list(ring.coords) for ring in poly.interiors]
        for ring in rings:
            runs: List[Tuple[Tuple[str, str | None], List[Tuple[float, float]]]] = []
            for k in range(len(ring) - 1):
                p0 = (float(ring[k][0]), float(ring[k][1]))
                p1 = (float(ring[k + 1][0]), float(ring[k + 1][1]))
                mx, my = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
                dx, dy = p1[0] - p0[0], p1[1] - p0[1]
                length = (dx * dx + dy * dy) ** 0.5 or 1.0
                nx, ny = -dy / length, dx / length
                other = None
                for sign in (1.0, -1.0):
                    probe = Point(mx + sign * nx * 0.6, my + sign * ny * 0.6)
                    if mine is not None and mine.contains(probe):
                        continue
                    found = index.owner(probe.x, probe.y, max_dist=1.5)
                    if found and found != r['id']:
                        other = found
                        break
                same_object = bool(other) and obj_of.get(other) == r.get('objectId')
                key = ('subdivision' if same_object else 'artwork', other)
                if runs and runs[-1][0] == key:
                    runs[-1][1].append(p1)
                else:
                    runs.append((key, [p0, p1]))
            for (kind, other), pts in runs:
                if len(pts) >= 2:
                    _emit_edge(edges, pts, kind, r['id'], other, None, self_id=r['id'])
    return edges


def _split_up(regions: list, geometry: dict, settings: BuildSettings,
              objects: list | None) -> int:
    """Split gameplay regions UP toward the target via the existing semantic
    budget auto-subdivider (min playable area + readable labels enforced per
    split; exact pixel-edge masters keep the raster partition watertight).

    Bundles compiled with an edges overlay get the new subdivision edges; the
    raster Convert bundles have no edges list by contract, so the splitter
    writes to a scratch list and the geometry stays edgeless (same export
    fallback as straight out of compile_image)."""
    attach = geometry['edges'] if geometry.get('edges') is not None else []
    return _auto_subdivide(regions, settings, attach, objects, fit=False)


def _health(bundle: dict, min_px: float, tiny_floor: float, baseline: dict,
            baseline_conflicts: int) -> Tuple[dict, List[str]]:
    """Measure a candidate and list every quality violation (empty = healthy).
    Caps never police BELOW the initial state: label conflicts and ceiling
    exceedances are judged relative to the input bundle — the optimizer may
    not make it worse, but it is not asked to repair the compiler's output."""
    profile = difficulty_profile(bundle)
    areas = [float(r['area']) for r in bundle['geometry']['regions']] or [1.0]
    tiny = sum(1 for a in areas if a < tiny_floor) / len(areas)
    zoom = float(profile['metrics']['requiredZoom'])
    worst = min(areas)
    reasons: List[str] = []
    conflicts = _conflicts(bundle['geometry']['regions'])
    if conflicts > baseline_conflicts:
        reasons.append(f'label clearance would degrade: {conflicts - baseline_conflicts} '
                       'more unreadable label(s)')
    if zoom > MAX_REQUIRED_ZOOM and zoom > baseline['zoom'] + 1e-9:
        reasons.append(f'required zoom {zoom:.1f}x exceeds the {MAX_REQUIRED_ZOOM:.0f}x playability ceiling')
    if worst < min_px:
        reasons.append(f'a region would fall below the {min_px:.0f}px² tap minimum')
    if tiny > MAX_TINY_RATIO and tiny > baseline['tiny'] + 1e-9:
        reasons.append(f'{tiny:.0%} of regions would be under {tiny_floor:.0f}px² (microscopic targets)')
    return profile, reasons


def optimize_gameplay_difficulty(bundle: dict, target_tier: str, *,
                                 target_regions: int | None = None,
                                 max_iterations: int = 3,
                                 progress: Callable = lambda *_: None) -> Tuple[dict, dict]:
    """Drive a compiled bundle's gameplay layer toward ``target_tier``.

    Returns ``(bundle, report)`` — the bundle is optimized in place (regions,
    labels, objects.subdivision.preferredRegions, manifest regionCount and
    difficulty); the artwork (paint paths, palette, object shapeIds, source
    master) is verified untouched after every accepted iteration. The report
    carries per-iteration measurements, the final per-object budgets, the
    outcome and human-readable reasons when the tier could not be safely
    reached. Deterministic: same bundle + same target => identical regions.
    """
    tier = target_tier if target_tier in TIER_TARGETS else 'hard'
    target = max(30, min(1600, int(target_regions or TIER_TARGETS[tier])))
    lo, hi = TIER_RANGES[tier]
    manifest, geometry = bundle['manifest'], bundle['geometry']
    objects = bundle.get('objects') or None
    stored = (manifest.get('generation') or {}).get('settings') or {}
    base_settings = {k: stored[k] for k in _SETTING_KEYS if k in stored}
    min_px = float(base_settings.get('min_region_pixels', 35))
    tiny_floor = max(70.0, 2.0 * min_px)
    edgeful = geometry.get('edges') is not None

    frozen_paint = [(p.get('shapeId'), p.get('fill'), p.get('d'))
                    for p in (bundle['paint'].get('paths') or [])]
    frozen_shapes = {o['id']: list(o.get('shapeIds') or []) for o in (objects or [])}

    regions = geometry['regions']
    initial_profile = difficulty_profile(bundle)
    initial_count = len(regions)
    areas0 = [float(r['area']) for r in regions] or [1.0]
    baseline = {'zoom': float(initial_profile['metrics']['requiredZoom']),
                'tiny': sum(1 for a in areas0 if a < tiny_floor) / max(1, initial_count)}
    baseline_conflicts = _conflicts(regions)

    def _snapshot() -> tuple:
        return (deepcopy(regions),
                list(geometry.get('edges') or []) if edgeful else None,
                geometry.get('partitionTolerance', 0.0))

    def _restore(snap: tuple) -> None:
        regions[:] = snap[0]
        if edgeful:
            geometry['edges'] = list(snap[1] or [])
        geometry['partitionTolerance'] = snap[2]
        manifest['regionCount'] = len(regions)

    # The count range IS the tier contract; the measured score/rating is
    # reported honestly but can saturate on small canvases, so it must not
    # dominate the move decision.
    def _distance() -> int:
        return abs(len(regions) - target)

    def _tier_hit() -> bool:
        return lo <= len(regions) <= hi

    report = {
        'requestedTier': tier,
        'targetRegions': target,
        'tierRange': [lo, hi],
        'initial': {'rating': initial_profile['rating'], 'score': initial_profile['score'],
                    'regionCount': initial_count},
        'iterations': [],
        'merges': 0,
        'splits': 0,
        'outcome': 'already-at-target',
        'reasons': [],
        'budgets': {},
        'regionCountBefore': initial_count,
        'regionCountAfter': initial_count,
        'changed': False,
    }

    best_snap = _snapshot()
    best_distance = _distance()
    healthy = _snapshot()
    attempt = target

    tier_reached = _tier_hit()
    if not tier_reached:
        report['outcome'] = 'best-safe-result'
        for it in range(1, max_iterations + 1):
            entry: dict = {'iteration': it, 'fromCount': len(regions), 'attemptTarget': attempt}
            count = len(regions)
            moved = 0
            if count > attempt:
                moved = _merge_down(regions, geometry, attempt, bundle['palette'])
                entry['merges'] = moved
            elif count < attempt:
                split_settings = BuildSettings(**base_settings).model_copy(update={
                    'target_regions': attempt,
                    'auto_subdivide': True,
                    # doubled minimum: every piece stays at/above the tiny floor
                    'min_region_pixels': int(max(SPLIT_MIN_FACTOR * min_px, tiny_floor)),
                })
                moved = _split_up(regions, geometry, split_settings, objects)
                entry['splits'] = moved
            if moved == 0:
                # Split/merge availability does not depend on the attempt
                # value: zero movement means the reachable extremum is hit —
                # a genuine safety ceiling, not something backoff can pass.
                report['reasons'].append(
                    f'no further safe {"merge" if count > attempt else "split"} toward {attempt}: '
                    'the remaining boundaries separate objects/colors or sit at the tap minimum')
                entry['to'] = {'regionCount': count}
                report['iterations'].append(entry)
                report['outcome'] = 'safe-ceiling'
                break
            # Re-measure the partition band (same mechanism as the merge/cut
            # edit actions): exact-fit pieces re-expose the source flats'
            # sub-tolerance seams, and the measured deviation becomes this
            # geometry's documented partitionTolerance allowance.
            vw, vh = float(geometry['viewBox'][2]), float(geometry['viewBox'][3])
            move_polys = [q for q in (region_polygon(r) for r in regions + geometry.get('decorations', []))
                          if not q.is_empty]
            if move_polys:
                move_union = unary_union([make_valid(q) for q in move_polys])
                deviation = max(sum(q.area for q in move_polys) - move_union.area,
                                vw * vh - move_union.area, 0.0)
                if deviation > float(geometry.get('partitionTolerance', 0.0)):
                    geometry['partitionTolerance'] = round(deviation + 0.01, 3)
            if edgeful:
                geometry['edges'] = _rebuild_gameplay_edges(geometry)
            manifest['regionCount'] = len(regions)
            qa = validate_bundle(bundle)
            profile, health_reasons = _health(bundle, min_px, tiny_floor, baseline,
                                              baseline_conflicts)
            invariants_ok = (
                [(p.get('shapeId'), p.get('fill'), p.get('d'))
                 for p in (bundle['paint'].get('paths') or [])] == frozen_paint
                and {o['id']: list(o.get('shapeIds') or []) for o in (objects or [])} == frozen_shapes)
            if qa.get('errors') or health_reasons or not invariants_ok:
                _restore(healthy)
                reasons = list(health_reasons or qa.get('errors') or
                               ['an artwork invariant would have changed'])
                report['reasons'].extend(reasons)
                entry['rejected'] = reasons[:3]
                report['iterations'].append(entry)
                backoff = round((len(regions) + attempt) / 2)
                if backoff in (len(regions), attempt):
                    report['outcome'] = 'safe-ceiling'
                    break
                attempt = backoff
                continue
            healthy = _snapshot()
            report['merges'] += entry.get('merges', 0)
            report['splits'] += entry.get('splits', 0)
            entry['to'] = {'rating': profile['rating'], 'score': profile['score'],
                           'regionCount': len(regions)}
            report['iterations'].append(entry)
            if _distance() < best_distance:
                best_distance = _distance()
                best_snap = _snapshot()
            tier_reached = _tier_hit()
            if tier_reached:
                report['outcome'] = 'target-reached'
                break
            attempt = target          # re-aim at the full target next iteration

    if not tier_reached and report['outcome'] == 'best-safe-result' and not report['reasons']:
        report['reasons'].append(
            f'could not safely reach {tier} within {max_iterations} iterations')

    _restore(best_snap)
    final_profile = difficulty_profile(bundle)
    manifest['difficulty'] = final_profile
    report['achieved'] = {'rating': final_profile['rating'], 'score': final_profile['score'],
                          'regionCount': len(regions)}
    report['regionCountAfter'] = len(regions)
    report['changed'] = len(regions) != initial_count
    report['reasons'] = list(dict.fromkeys(report['reasons']))

    # Record what the final geometry actually honours per object — the same
    # engine re-run later (the Optimize button) reproduces this split.
    if objects:
        counts: Dict[str, int] = defaultdict(int)
        for r in regions:
            counts[r.get('objectId') or 'unassigned'] += 1
        report['budgets'] = {o['id']: int(counts.get(o['id'], 0)) for o in objects}
        for obj in objects:
            sub = dict(obj.get('subdivision') or {})
            sub['preferredRegions'] = int(counts.get(obj['id'], 0))
            obj['subdivision'] = sub
    progress(1, f"Gameplay difficulty: {final_profile['rating']} · {final_profile['score']}")
    return bundle, report
