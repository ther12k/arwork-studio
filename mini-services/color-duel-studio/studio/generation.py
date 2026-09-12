"""Color Duel Art Studio — Generation Orchestrator (Phase 2A).

Provides isolated, transactional generation sessions:
- ScenePlan draft stage with strict schema and structured mutations.
- Multi-step state machine: draft_plan -> generating -> compiling -> ready_to_commit -> committed.
- Isolation: generation runs in temporary workspace (sessions/{session_id}),
  never writing to healthy revisions until QA validation passes and commit is called.
- Rollback: errors or cancellations discard/fail the session cleanly without
  corrupting existing revisions.
- Provenance: revision manifest records generation metadata (mode, requested vs
  measured difficulty, fidelity, scenePlan).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
import re
import shutil
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .models import BuildSettings
from .pipeline import (
    OBJECTS_SCHEMA_VERSION,
    compile_image,
    compile_svg_master,
    emit_bundle,
    load_bundle,
    normalize_objects,
    read_json,
    validate_bundle,
    write_json,
)
from .difficulty import optimize_gameplay_difficulty
from .svg_master import clean_svg

# ---------------------------------------------------------------------------
# Difficulty Presets and Target Ranges
# ---------------------------------------------------------------------------

DIFFICULTY_TIERS = ('easy', 'medium', 'hard', 'master')

DIFFICULTY_RANGES: Dict[str, Tuple[int, int]] = {
    'easy': (100, 180),
    'medium': (180, 320),
    'hard': (320, 550),
    'master': (550, 800),
}

DIFFICULTY_INITIAL_TARGETS: Dict[str, int] = {
    'easy': 140,
    'medium': 250,
    'hard': 430,
    'master': 650,
}

# ---------------------------------------------------------------------------
# Fidelity Policies (for Image Convert / Reference)
# ---------------------------------------------------------------------------

FIDELITY_POLICIES: Dict[str, Dict[str, Any]] = {
    'stylized': {
        'semantic_similarity': 'high',
        'layout_preservation': 'medium',
        'contour_simplification': 2.5,
        'palette_colors': 20,
        'curve_tolerance': 2.0,
        'discard_micro_details': True,
    },
    'balanced': {
        'semantic_similarity': 'high',
        'layout_preservation': 'high',
        'contour_simplification': 1.2,
        'palette_colors': 32,
        'curve_tolerance': 1.0,
        'discard_micro_details': False,
    },
    'faithful': {
        'semantic_similarity': 'very_high',
        'layout_preservation': 'very_high',
        'contour_simplification': 0.6,
        'palette_colors': 48,
        'curve_tolerance': 0.5,
        'discard_micro_details': False,
    },
}

ASPECT_VIEWBOX: Dict[str, str] = {
    '1024x1536': '0 0 576 768',
    '1536x1024': '0 0 768 576',
    '1024x1024': '0 0 640 640',
}

_SLUG_CLEAN = re.compile(r'[^a-z0-9]+')
_OBJECT_ID_CLEAN = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _viewbox_dimensions(aspect: str) -> Tuple[float, float]:
    vb = ASPECT_VIEWBOX.get(aspect, '0 0 576 768')
    parts = [float(p) for p in vb.split()]
    return parts[2], parts[3]


def slugify(text: str, max_len: int = 32) -> str:
    cleaned = _SLUG_CLEAN.sub('-', text.lower()).strip('-')
    return cleaned[:max_len] or 'object'


# ---------------------------------------------------------------------------
# ScenePlan Schema & Normalization
# ---------------------------------------------------------------------------

def normalize_plan_objects(objects: list, vw: float, vh: float) -> List[dict]:
    """Validate, sanitize and sort planned objects for a ScenePlan."""
    clean = []
    seen_ids = set()
    for i, obj in enumerate(objects):
        if not isinstance(obj, dict):
            continue
        name = str(obj.get('name') or f'Object {i + 1}').strip()[:80]
        raw_id = str(obj.get('id') or '').strip()
        if not raw_id or not _OBJECT_ID_CLEAN.match(raw_id) or raw_id in seen_ids:
            slug = slugify(name)
            raw_id = f'obj-{slug}' if f'obj-{slug}' not in seen_ids else f'obj-{slug}-{i + 1}'
        seen_ids.add(raw_id)

        # Bounding box clamp to viewBox
        bbox = obj.get('bbox')
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
            bx, by, bw, bh = [float(v) for v in bbox]
            bx = max(0.0, min(bx, vw - 8.0))
            by = max(0.0, min(by, vh - 8.0))
            bw = max(8.0, min(bw, vw - bx))
            bh = max(8.0, min(bh, vh - by))
            clean_bbox = [round(bx, 1), round(by, 1), round(bw, 1), round(bh, 1)]
        else:
            clean_bbox = [0.0, 0.0, vw, vh]

        role = str(obj.get('role') or 'midground').strip().lower()
        if role not in ('background', 'midground', 'foreground', 'subject', 'accent'):
            role = 'midground'

        fills = [f.upper() for f in (obj.get('fills') or [])
                 if isinstance(f, str) and re.fullmatch(r'#[0-9A-Fa-f]{6}', f)]
        if not fills:
            fills = ['#4A6B82']

        try:
            dw = float(obj.get('detailWeight', 1.0))
            dw = max(0.05, min(dw, 10.0))
        except (TypeError, ValueError):
            dw = 1.0

        rec = {
            'id': raw_id,
            'name': name,
            'description': str(obj.get('description') or '').strip()[:300],
            'role': role,
            'z': int(obj.get('z') if obj.get('z') is not None else i),
            'bbox': clean_bbox,
            'fills': fills[:8],
            'detailWeight': round(dw, 3),
        }
        parent = str(obj.get('parentId') or '').strip()
        if parent and _OBJECT_ID_CLEAN.match(parent) and parent != raw_id:
            rec['parentId'] = parent
        sub = obj.get('subdivision')
        if isinstance(sub, dict):
            rec['subdivision'] = {
                k: sub[k] for k in ('minRegions', 'preferredRegions', 'maxRegions', 'preserveSilhouette')
                if k in sub
            }
        gen = obj.get('generation')
        if isinstance(gen, dict):
            rec['generation'] = {
                k: gen[k] for k in ('prompt', 'provider', 'locked')
                if k in gen
            }
        clean.append(rec)

    # Sort objects primarily by z-index (background to foreground)
    clean.sort(key=lambda o: (o['z'], o['bbox'][1], o['bbox'][0]))
    return clean


def create_scene_plan(title: str, description: str, aspect: str = '1024x1536',
                      requested_difficulty: str = 'hard',
                      objects: list[dict] | None = None) -> dict:
    """Create a structured ScenePlan draft."""
    vw, vh = _viewbox_dimensions(aspect)
    diff = requested_difficulty if requested_difficulty in DIFFICULTY_RANGES else 'hard'
    r_min, r_max = DIFFICULTY_RANGES[diff]
    target = DIFFICULTY_INITIAL_TARGETS[diff]

    plan = {
        'schemaVersion': 1,
        'title': str(title or 'Untitled Scene')[:100],
        'description': str(description or '')[:1000],
        'aspect': aspect,
        'viewBox': [0.0, 0.0, vw, vh],
        'requestedDifficulty': diff,
        'targetRegionRange': [r_min, r_max],
        'targetRegions': target,
        'objects': normalize_plan_objects(objects or [], vw, vh),
    }
    return plan


# ---------------------------------------------------------------------------
# Structured ScenePlan Mutations
# ---------------------------------------------------------------------------

def apply_scene_mutations(plan: dict, mutations: list[dict]) -> dict:
    """Apply a list of structured operations to a ScenePlan deterministically.

    Supported operations:
    - update_object: {op: 'update_object', objectId: '...', changes: {...}}
    - add_object:    {op: 'add_object', object: {...}}
    - remove_object: {op: 'remove_object', objectId: '...'}
    - reorder_objects: {op: 'reorder_objects', order: ['obj-1', 'obj-2', ...]}
    - set_difficulty: {op: 'set_difficulty', difficulty: 'hard'}
    - update_plan:   {op: 'update_plan', changes: {...}}
    """
    updated = copy.deepcopy(plan)
    vw, vh = updated['viewBox'][2], updated['viewBox'][3]

    for mut in mutations:
        if not isinstance(mut, dict):
            continue
        op = mut.get('op') or mut.get('operation')

        if op == 'update_object':
            oid = mut.get('objectId')
            changes = mut.get('changes') or {}
            target = next((o for o in updated['objects'] if o['id'] == oid), None)
            if target and isinstance(changes, dict):
                for k, v in changes.items():
                    if k in ('name', 'description', 'role', 'parentId'):
                        target[k] = v
                    elif k == 'z':
                        target['z'] = int(v)
                    elif k == 'detailWeight':
                        target['detailWeight'] = round(max(0.05, min(float(v), 10.0)), 3)
                    elif k == 'bbox' and isinstance(v, (list, tuple)) and len(v) == 4:
                        target['bbox'] = [float(x) for x in v]
                    elif k == 'fills' and isinstance(v, list):
                        target['fills'] = [f.upper() for f in v if isinstance(f, str) and re.fullmatch(r'#[0-9A-Fa-f]{6}', f)]
                    elif k in ('subdivision', 'generation') and isinstance(v, dict):
                        target[k] = target.get(k, {})
                        target[k].update(v)

        elif op == 'add_object':
            new_obj = mut.get('object')
            if isinstance(new_obj, dict):
                updated['objects'].append(new_obj)
                updated['objects'] = normalize_plan_objects(updated['objects'], vw, vh)

        elif op == 'remove_object':
            oid = mut.get('objectId')
            updated['objects'] = [o for o in updated['objects'] if o['id'] != oid]
            # Clear parentId on children that referenced the removed object
            for o in updated['objects']:
                if o.get('parentId') == oid:
                    o.pop('parentId', None)

        elif op == 'reorder_objects':
            order = mut.get('order') or mut.get('objectIds') or []
            if isinstance(order, list):
                order_map = {oid: idx for idx, oid in enumerate(order)}
                for o in updated['objects']:
                    if o['id'] in order_map:
                        o['z'] = order_map[o['id']]
                updated['objects'].sort(key=lambda o: (o['z'], o['bbox'][1], o['bbox'][0]))

        elif op == 'set_difficulty':
            diff = str(mut.get('difficulty') or '').lower()
            if diff in DIFFICULTY_RANGES:
                updated['requestedDifficulty'] = diff
                updated['targetRegionRange'] = list(DIFFICULTY_RANGES[diff])
                updated['targetRegions'] = DIFFICULTY_INITIAL_TARGETS[diff]

        elif op == 'update_plan':
            changes = mut.get('changes') or {}
            if isinstance(changes, dict):
                if 'title' in changes:
                    updated['title'] = str(changes['title'])[:100]
                if 'description' in changes:
                    updated['description'] = str(changes['description'])[:1000]

    # Re-normalize objects after all mutations
    updated['objects'] = normalize_plan_objects(updated['objects'], vw, vh)
    return updated


# ---------------------------------------------------------------------------
# Plan-change classification (Task 29 review patch)
# ---------------------------------------------------------------------------
# Visual plan fields can only reach the artwork through generation (the
# compiler reads shape/color from the master SVG; plan records merge metadata
# only). Metadata fields (name, lock) and gameplay fields (difficulty,
# subdivision budgets) need no AI work. Every visual field that differs from
# the artwork-synced plan stays pending until THAT object is regenerated —
# one successful regenerate never clears another object's pending change.
VISUAL_PLAN_FIELDS = ('description', 'fills', 'bbox', 'z')


def _pending_artwork_changes(old_objects: list, new_objects: list) -> dict:
    """Diff two plan object lists into {objectId: [changed visual fields]}.

    Added objects pend as ['added'] (no artwork exists yet); removed ones as
    ['removed'] (the master still carries their shapes until a bulk
    regeneration composes from the trimmed plan)."""
    old_by_id = {o['id']: o for o in old_objects}
    new_by_id = {o['id']: o for o in new_objects}
    pending: dict = {}
    for oid, old in old_by_id.items():
        new = new_by_id.get(oid)
        if new is None:
            pending[oid] = ['removed']
            continue
        changed = [f for f in VISUAL_PLAN_FIELDS if old.get(f) != new.get(f)]
        if changed:
            pending[oid] = changed
    for oid in new_by_id:
        if oid not in old_by_id:
            pending[oid] = ['added']
    return pending


# ---------------------------------------------------------------------------
# Generation Session State Machine
# ---------------------------------------------------------------------------

def _fragment_spec(plan_obj: dict) -> dict:
    """Translate a ScenePlan object into the provider fragment spec
    (ai.Provider.svg_object): shapes count derives from detailWeight when the
    plan does not carry an explicit count."""
    shapes = plan_obj.get('shapes')
    if not shapes:
        try:
            shapes = max(6, min(30, round(float(plan_obj.get('detailWeight', 1.0)) * 12)))
        except (TypeError, ValueError):
            shapes = 12
    return {'id': plan_obj.get('id'), 'name': plan_obj['name'],
            'description': plan_obj.get('description', ''),
            'bbox': plan_obj['bbox'], 'fills': plan_obj.get('fills') or ['#8899AA'],
            'shapes': int(shapes), 'z': plan_obj.get('z', 0)}


def _plan_objects_from_provider(raw: list, vw: float, vh: float) -> List[dict]:
    """Map raw provider scene-plan objects (name/description/z/bbox/shapes/fills)
    into ScenePlan records: deterministic obj-* ids, detailWeight derived from
    the planned shape count (semantic complexity signal)."""
    mapped = []
    for i, o in enumerate(raw or []):
        if not isinstance(o, dict):
            continue
        try:
            shapes = float(o.get('shapes') or 12)
        except (TypeError, ValueError):
            shapes = 12.0
        mapped.append({
            'name': str(o.get('name') or f'Object {i + 1}'),
            'description': str(o.get('description') or ''),
            'z': o.get('z'),
            'bbox': o.get('bbox'),
            'fills': o.get('fills') or [],
            'detailWeight': round(max(0.2, min(shapes / 12.0, 4.0)), 3),
        })
    return normalize_plan_objects(mapped, vw, vh)


def _reinject_locked_objects(master_path: Path, old_master_text: str,
                             locked_ids: list, objects: list) -> None:
    """Carry locked objects' shapes from the previous session master into a
    freshly composed one (bulk regeneration must not redraw locked artwork).

    Doc-level merge, NOT shape replacement: the new master was composed
    WITHOUT the locked objects (their fragments are never generated), so
    there is nothing to replace. Each locked object's sanitized items are
    inserted at their plan z-order position in the composed stream and the
    whole stream is renumbered; gradients ride along."""
    from .svg_master import emit_master_svg, import_master
    new_doc = import_master(master_path.read_text(encoding='utf-8'))
    old_doc = import_master(old_master_text)
    z_of = {o['id']: o.get('z', 0) for o in objects}
    for oid in locked_ids:
        items = [dict(sh) for sh in old_doc.shapes + old_doc.ink_shapes
                 if sh.get('objectRef') == oid]
        if not items:
            continue                    # nothing generated for it yet
        z0 = z_of.get(oid, 0)
        stream = sorted(new_doc.shapes + new_doc.ink_shapes, key=lambda t: t['order'])
        idx = len(stream)
        for i, sh in enumerate(stream):
            owner = sh.get('objectRef')
            if owner is not None and owner != oid and z_of.get(owner, 0) > z0:
                idx = i
                break
        stream[idx:idx] = items
        for k, sh in enumerate(stream):
            sh['order'] = k
        new_doc.shapes = [sh for sh in stream if sh.get('kind') != 'ink']
        new_doc.ink_shapes = [sh for sh in stream if sh.get('kind') == 'ink']
        for gid, grad in old_doc.gradients.items():
            new_doc.gradients.setdefault(gid, grad)
    clean_svg(emit_master_svg(new_doc).encode('utf-8'), master_path)


def replace_object_shapes(master_text: str, object_id: str, fragment_text: str,
                          object_name: str | None = None) -> str:
    """Targeted object regeneration (Phase 2B): replace every shape owned by
    object_id in a sanitized master with the shapes of a fresh fragment.

    The objectId is PRESERVED even though all its internal shapeIds change —
    objects.json ownership (region objectId + record id) stays stable. New
    shapes take over the old shapes' z slots one-for-one (extra new shapes
    are appended above the last slot), so untouched objects keep their exact
    layer position. Neighbouring objects are NOT touched here; their visible
    surfaces are re-derived by the normal recompile that follows."""
    from .svg_master import import_master, emit_master_svg
    doc = import_master(master_text)
    frag = import_master(fragment_text)
    if not frag.shapes and not frag.ink_shapes:
        raise ValueError('The regenerated fragment contains no drawable shapes.')
    stream = sorted(doc.shapes + doc.ink_shapes, key=lambda s: s['order'])
    old = [s for s in stream if s.get('objectRef') == object_id]
    if not old:
        raise ValueError(f'Object {object_id} has no shapes in the current master.')
    # Deterministic per-(object, fragment) id prefix: keeps new shape/gradient
    # ids collision-free against the rest of the master AND across repeated
    # regenerations of different objects (old shapes are removed in the same
    # operation, so re-using the prefix for the same object+fragment is safe).
    prefix = (object_id + '-' +
              hashlib.sha1((object_id + '\x00' + fragment_text).encode()).hexdigest()[:8] + '-')
    new_shapes = []
    for s in frag.shapes + frag.ink_shapes:
        e = dict(s)
        e['id'] = prefix + s['id']
        grad = e.get('gradient')
        if grad and grad.get('id', '').startswith('g-'):
            grad = dict(grad)
            grad['id'] = 'g-' + prefix + grad['id'][2:]
            e['gradient'] = grad
        e['objectRef'] = object_id
        e['objectName'] = object_name or s.get('objectName') or object_id.replace('-', ' ').title()
        new_shapes.append(e)
    max_order = max((s['order'] for s in stream), default=0)
    result = []
    ni = 0
    for s in stream:
        if s.get('objectRef') == object_id:
            if ni < len(new_shapes):
                e = new_shapes[ni]
                e['order'] = s['order']
                result.append(e)
                ni += 1
            # more old slots than new shapes: the surplus slot is dropped
        else:
            result.append(s)
    if ni < len(new_shapes):
        # fragment grew: append the surplus above everything (documented behavior)
        for e in new_shapes[ni:]:
            max_order += 1
            e['order'] = max_order
            result.append(e)
    combined = type(doc)()
    combined.view_box = doc.view_box
    for s in sorted(result, key=lambda t: t['order']):
        if s.get('kind') == 'ink':
            combined.ink_shapes.append(s)
        else:
            combined.shapes.append(s)
    return emit_master_svg(combined)


# ---------------------------------------------------------------------------
# Phase 2D — Convert Artwork: fidelity policies as real algorithm parameters
# ---------------------------------------------------------------------------
# Principle: AI decides WHAT the objects are; deterministic CV decides WHERE
# the pixel boundaries are. Vision never traces pixels; SLIC never decides
# semantics — association joins the two.

CONVERT_POLICIES: Dict[str, Dict[str, Any]] = {
    'stylized': {
        'segmentDensity': 0.6,        # SLIC candidate density multiplier
        'colorMergeDeltaE': 14.0,     # high color merge tolerance
        'curveTolerance': 2.0,        # heavy Bézier simplification
        'minComponentArea': 120,      # aggressive tiny-component removal
        'paletteTarget': 16,          # aggressive palette quantization
        'assocConfidence': 0.30,      # below: segment stays unassigned
        'visualGate': 55,
        'gameReadinessGate': 80,
    },
    'balanced': {
        'segmentDensity': 1.0,
        'colorMergeDeltaE': 9.0,
        'curveTolerance': 1.1,
        'minComponentArea': 42,
        'paletteTarget': 24,
        'assocConfidence': 0.35,
        'visualGate': 70,
        'gameReadinessGate': 80,
    },
    'faithful': {
        'segmentDensity': 1.8,
        'colorMergeDeltaE': 5.0,      # low merge tolerance
        'curveTolerance': 0.6,        # low simplification
        'minComponentArea': 18,       # conservative tiny removal
        'paletteTarget': 40,
        'assocConfidence': 0.40,
        'visualGate': 82,
        'gameReadinessGate': 80,
    },
}


CONVERT_CANDIDATE_BASE = 220      # fidelity-only candidate density base; NEVER
                                  # scaled by the difficulty target (2D.1: the
                                  # reconstruction is difficulty-invariant)


def resolve_convert_policy(fidelity: str, overrides: dict | None = None) -> dict:
    """Resolved, reproducible convert policy stored in the session."""
    base = dict(CONVERT_POLICIES.get(fidelity, CONVERT_POLICIES['balanced']))
    for key, val in (overrides or {}).items():
        if key in base and val is not None:
            base[key] = val
    base['fidelity'] = fidelity if fidelity in CONVERT_POLICIES else 'balanced'
    return base


def _rgb_hex(c) -> str:
    return '#' + ''.join(f'{int(round(v)):02X}' for v in np.clip(c, 0, 255))


def _hex_rgb(hx: str) -> np.ndarray:
    hx = (hx or '#808080').lstrip('#')
    return np.array([int(hx[i:i + 2], 16) for i in (0, 2, 4)], dtype=float)


def associate_segments_to_objects(rgb: np.ndarray, labels: np.ndarray, objects: list,
                                  policy: dict,
                                  plan_space: Tuple[float, float] | None = None
                                  ) -> Tuple[Dict[int, str], List[dict], List[dict]]:
    """Score-based assignment of deterministic CV segments to AI-decided
    semantic objects (Phase 2D association layer).

    segment → object score = bbox-overlap prior + semantic color
    compatibility + neighborhood consistency (one smoothing pass). Low-
    confidence segments stay 'unassigned' — bad guesses are never forced.
    Returns (label→objectId map, per-segment records for decomposition.json,
    per-object region stats)."""
    h, w = labels.shape
    values = [int(v) for v in np.unique(labels) if int(v) != 0]
    if not objects:
        return {}, [], []
    # ---- per-label stats ----
    stats: Dict[int, dict] = {}
    flat = labels.ravel()
    for v in values:
        mask = flat == v
        area = int(mask.sum())
        ys, xs = np.divmod(np.nonzero(mask)[0], w)
        stats[v] = {'bbox': (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())),
                    'centroid': (float(xs.mean()), float(ys.mean())),
                    'area': area, 'neighbors': set()}
        stats[v]['color'] = rgb[labels == v].mean(0)
    # ---- adjacency (4-neighborhood) ----
    for a, b in ((labels[:, :-1], labels[:, 1:]), (labels[:-1, :], labels[1:, :])):
        edge = (a != b)
        for la, lb in zip(a[edge], b[edge]):
            la, lb = int(la), int(lb)
            if la and lb and la in stats and lb in stats:
                stats[la]['neighbors'].add(lb)
                stats[lb]['neighbors'].add(la)
    # ---- independent scoring pass ----
    # Plan bboxes live in the ScenePlan viewBox space (e.g. 576x768); segment
    # bboxes live in image pixel space. Scale the plan bboxes into image space
    # before comparing, or the overlap prior is meaningless (2D.1 fix).
    plan_vw, plan_vh = plan_space or (float(w), float(h))
    sx, sy = (float(w) / max(1e-6, plan_vw), float(h) / max(1e-6, plan_vh))
    obj_bboxes = []
    for o in objects:
        bx, by, bw, bh = o['bbox']
        obj_bboxes.append((bx * sx, by * sy, (bx + bw) * sx, (by + bh) * sy))
    obj_rgbs = [np.mean([_hex_rgb(f) for f in (o.get('fills') or ['#808080'])], axis=0) for o in objects]

    def bbox_score(bb, ob):
        ix = max(0.0, min(bb[2], ob[2]) - max(bb[0], ob[0]))
        iy = max(0.0, min(bb[3], ob[3]) - max(bb[1], ob[1]))
        inter = ix * iy
        union = (bb[2] - bb[0]) * (bb[3] - bb[1]) + (ob[2] - ob[0]) * (ob[3] - ob[1]) - inter
        return inter / union if union > 0 else 0.0

    scores: Dict[int, Dict[str, float]] = {}
    for v in values:
        st = stats[v]
        row = {}
        for o, ob, orgb in zip(objects, obj_bboxes, obj_rgbs):
            overlap = bbox_score(st['bbox'], ob)
            dist = float(np.abs(st['color'] - orgb).mean())
            color = max(0.0, 1.0 - dist / 128.0)
            row[o['id']] = 0.6 * overlap + 0.4 * color
        scores[v] = row
    # ---- one neighbor-consistency smoothing pass ----
    smoothed: Dict[int, str] = {}
    for v in values:
        row = scores[v]
        best_oid = max(row, key=lambda k: row[k])
        best = row[best_oid]
        if st_neighbors := stats[v]['neighbors']:
            votes: Dict[str, float] = defaultdict(float)
            for n in st_neighbors:
                if n in smoothed:
                    votes[smoothed[n]] += 1.0
                else:
                    nrow = scores[n]
                    votes[max(nrow, key=lambda k: nrow[k])] += 0.5
            if votes:
                nb_oid, nb_votes = max(votes.items(), key=lambda kv: kv[1])
                share = nb_votes / len(st_neighbors)
                # neighbor consensus can rescue a close call, never override a
                # strong one; 'unassigned' neighbours carry no object vote.
                if (nb_oid != 'unassigned' and best < 0.45 and share >= 0.5
                        and row.get(nb_oid, 0.0) >= 0.6 * best):
                    best_oid = nb_oid
                    best = max(best, 0.45)
        smoothed[v] = best_oid if best >= float(policy.get('assocConfidence', 0.35)) else 'unassigned'
    # ---- records for decomposition.json ----
    by_oid: Dict[str, dict] = defaultdict(lambda: {'area': 0, 'segments': 0})
    segments = []
    for v in values:
        st = stats[v]
        oid = smoothed[v]
        row = scores[v]
        segments.append({'id': f'seg-{v:04d}', 'objectId': oid,
                         'confidence': round(min(1.0, row.get(oid, 0.0)), 3),
                         'meanColor': _rgb_hex(st['color']), 'area': st['area'],
                         'bbox': [round(c, 1) for c in st['bbox']]})
        rec = by_oid[oid]
        rec['area'] += st['area']
        rec['segments'] += 1
    object_stats = [{'objectId': oid, 'segmentCount': rec['segments'], 'area': rec['area'],
                     'areaPercent': round(100 * rec['area'] / float(h * w), 2)}
                    for oid, rec in sorted(by_oid.items())]
    label_map = {v: oid for v, oid in smoothed.items() if oid != 'unassigned'}
    return label_map, segments, object_stats


def _path_command_count(d: str) -> int:
    return sum(1 for ch in d if ch.upper() in 'MLCQZ')


def conversion_quality_score(bundle: dict, source_rgb: np.ndarray, policy: dict) -> dict:
    """Two INDEPENDENT scores for Convert (never blindly averaged):
    visualFidelity — how close the vector reconstruction looks to the source;
    gameReadiness — playability/structure health incl. over-vectorization.
    Gate: fidelity >= preset minimum AND readiness >= 80 AND geometry QA passed."""
    g = bundle['geometry']
    paint = bundle['paint']
    h, w = source_rgb.shape[:2]
    # ---- visual fidelity: render the reconstruction, compare to source ----
    fidelity = 0.0
    try:
        import cairosvg
        from io import BytesIO
        from PIL import Image as _PILImage
        from .pipeline import svg_open, svg_paint
        final = svg_open(w, h) + svg_paint(paint) + '</svg>'
        raw = cairosvg.svg2png(bytestring=final.encode(), output_width=w, output_height=h)
        render = np.asarray(_PILImage.open(BytesIO(raw)).convert('RGB'), dtype=float)
        src = np.asarray(_PILImage.fromarray(source_rgb).resize((w, h)), dtype=float)
        diff = float(np.abs(render - src).mean())
        fidelity = max(0.0, min(100.0, 100.0 * (1.0 - diff / 96.0)))
    except Exception:
        fidelity = 0.0
    # ---- game readiness + over-vectorization metrics ----
    paths = paint.get('paths') or []
    command_counts = [_path_command_count(p.get('d', '')) for p in paths]
    total_commands = sum(command_counts)
    nodes_per_shape = round(total_commands / len(paths), 2) if paths else 0.0
    areas = [r['area'] for r in g['regions']]
    min_area = 35.0
    tiny_regions = sum(1 for a in areas if a < 2 * min_area)
    tiny_ratio = round(tiny_regions / len(areas), 4) if areas else 1.0
    megapixel = max(1e-6, (w * h) / 1_000_000)
    shapes_per_megapixel = round(len(paths) / megapixel, 1)
    short_edges = sum(1 for c in command_counts if c > 400)
    readiness = 100.0
    readiness -= min(30.0, nodes_per_shape * 0.08)          # anchor bloat
    readiness -= min(25.0, tiny_ratio * 100 * 0.9)          # microscopic fragments
    readiness -= min(20.0, max(0.0, shapes_per_megapixel - 120) * 0.15)  # fragment storm
    readiness -= min(15.0, short_edges * 1.5)               # traced-bitmap signatures
    # NOTE: geometry QA is not re-checked here — compile_image → emit_bundle
    # already RAISES on validate_bundle failures, so invalid geometry can
    # never reach this scorer.
    readiness = max(0.0, min(100.0, readiness))
    unassigned = sum(1 for r in g['regions'] if (r.get('objectId') or 'unassigned') == 'unassigned')
    unassigned_pct = round(100 * unassigned / len(g['regions']), 1) if g['regions'] else 100.0
    notes = []
    if readiness < float(policy.get('gameReadinessGate', 80)) and (nodes_per_shape > 220 or shapes_per_megapixel > 260):
        notes.append('Artwork is over-segmented: too many vector fragments/anchors for the source. '
                     'Try Balanced or Stylized fidelity.')
    if unassigned_pct > 40:
        notes.append(f'{unassigned_pct}% of regions have no semantic object '
                     '(the scene plan did not cover the image well).')
    return {
        'visualFidelity': round(fidelity, 1),
        'gameReadiness': round(readiness, 1),
        'gates': {'visualGate': policy['visualGate'],
                  'gameReadinessGate': policy['gameReadinessGate']},
        'passed': bool(fidelity >= float(policy['visualGate'])
                       and readiness >= float(policy['gameReadinessGate'])),
        'metrics': {'nodesPerVisualShape': nodes_per_shape,
                    'tinyRegionRatio': tiny_ratio,
                    'visualShapes': len(paths),
                    'visualShapesPerMegapixel': shapes_per_megapixel,
                    'unassignedRegionPercent': unassigned_pct},
        'notes': notes,
    }


class GenerationSessionManager:
    """Manages transactional generation sessions for a project."""

    def __init__(self, project_dir: Path, pid: str):
        self.project_dir = project_dir
        self.pid = pid
        self.sessions_dir = project_dir / 'sessions'
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def session_path(self, session_id: str) -> Path:
        if not re.fullmatch(r'sess-[a-f0-9]{8,16}', session_id):
            raise ValueError('Invalid session ID format.')
        return self.sessions_dir / session_id

    def create_session(self, mode: str, requested_difficulty: str = 'hard',
                       prompt: str = '', aspect: str = '1024x1536',
                       fidelity: str = 'balanced',
                       scene_plan: dict | None = None) -> dict:
        """Create a new generation session in draft_plan status."""
        sid = f'sess-{uuid.uuid4().hex[:12]}'
        sdir = self.session_path(sid)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / 'fragments').mkdir(parents=True, exist_ok=True)

        if scene_plan:
            plan = copy.deepcopy(scene_plan)
        else:
            plan = create_scene_plan(
                title=prompt[:40] if prompt else 'New Artwork Plan',
                description=prompt,
                aspect=aspect,
                requested_difficulty=requested_difficulty,
            )

        now_str = _now()
        session_data = {
            'id': sid,
            'projectId': self.pid,
            'mode': mode,
            'status': 'draft_plan',
            'requestedDifficulty': plan['requestedDifficulty'],
            'targetRegionRange': plan['targetRegionRange'],
            'targetRegions': plan['targetRegions'],
            'fidelity': fidelity if mode in ('image_reference', 'image_convert') else None,
            'aspect': aspect,
            'prompt': prompt,
            'scenePlan': plan,
            'meta': {},
            'error': None,
            'createdAt': now_str,
            'updatedAt': now_str,
        }
        write_json(sdir / 'session.json', session_data)
        return session_data

    def get_session(self, session_id: str) -> dict:
        sdir = self.session_path(session_id)
        sfile = sdir / 'session.json'
        if not sfile.is_file():
            raise FileNotFoundError(f'Session {session_id} not found.')
        return read_json(sfile)

    def list_sessions(self) -> List[dict]:
        out = []
        for sdir in sorted(self.sessions_dir.glob('sess-*')):
            sfile = sdir / 'session.json'
            if sfile.is_file():
                try:
                    out.append(read_json(sfile))
                except Exception:
                    pass
        return sorted(out, key=lambda s: s.get('createdAt', ''), reverse=True)

# ---------------------------------------------------------------------------
    def mutate_plan(self, session_id: str, mutations: list[dict]) -> dict:
        """Apply mutations to the draft plan of an active session.

        Mutating a ready_to_commit session is allowed (Task 29: 'continue
        editing the plan'): the session returns to draft_plan. VISUAL changes
        (description/fills/bbox/z, added or removed objects) are recorded in
        meta.pendingArtworkChanges per object — the compiled artwork is not
        considered in sync (and cannot reach ready_to_commit / commit) until
        each pending object is regenerated (or a bulk regeneration re-composes
        from the new plan). Metadata/gameplay-only edits never pend."""
        session = self.get_session(session_id)
        if session['status'] not in ('draft_plan', 'failed', 'ready_to_commit'):
            raise ValueError(f"Cannot mutate plan while session is {session['status']}.")
        master_exists = (self.session_path(session_id) / 'source-master.svg').is_file()

        old_objects = session['scenePlan'].get('objects') or []
        updated_plan = apply_scene_mutations(session['scenePlan'], mutations)
        new_objects = updated_plan.get('objects') or []
        pending = dict(session.get('meta', {}).get('pendingArtworkChanges') or {})
        for oid, fields in _pending_artwork_changes(old_objects, new_objects).items():
            merged = sorted(set(pending.get(oid, [])) | set(fields))
            pending[oid] = merged
        session['scenePlan'] = updated_plan
        session['requestedDifficulty'] = updated_plan['requestedDifficulty']
        session['targetRegionRange'] = updated_plan['targetRegionRange']
        session['targetRegions'] = updated_plan['targetRegions']
        session['updatedAt'] = _now()
        session['status'] = 'draft_plan'
        session['error'] = None
        meta = session.setdefault('meta', {})
        if pending and master_exists:
            meta['pendingArtworkChanges'] = pending
            meta['artworkStale'] = True
        else:
            # nothing visual is outstanding against the existing artwork
            meta.pop('pendingArtworkChanges', None)
            meta.pop('artworkStale', None)

        sdir = self.session_path(session_id)
        write_json(sdir / 'session.json', session)
        return session

    def mutate_plan_with_ai(self, session_id: str, provider, instruction: str,
                            progress: Callable = lambda *_: None):
        """Task 29 — artist chat → structured plan mutations.

        ONE paid strict-JSON call translates the instruction into mutation
        ops against the CURRENT plan (ids, bboxes, roles); the deterministic
        apply_scene_mutations engine stays the only plan writer. Returns
        (session, usage, summary, applied_mutations)."""
        session = self.get_session(session_id)
        if session['status'] not in ('draft_plan', 'failed', 'ready_to_commit'):
            raise ValueError(f"Cannot revise the plan while session is {session['status']}.")
        if not (instruction or '').strip():
            raise ValueError('Write what should change about the scene first.')
        progress(.2, 'Translating your instruction into plan changes')
        mutations, summary, usage = provider.plan_mutations(session['scenePlan'], instruction.strip())
        if not mutations:
            session = self.get_session(session_id)
            session.setdefault('meta', {})['lastPlanChat'] = {
                'instruction': instruction.strip(), 'summary': summary,
                'applied': 0, 'at': _now()}
            session['updatedAt'] = _now()
            write_json(self.session_path(session_id) / 'session.json', session)
            return session, usage, (summary or 'Nothing needed to change.'), []
        session = self.mutate_plan(session_id, mutations)
        session = self.get_session(session_id)
        session.setdefault('meta', {})['lastPlanChat'] = {
            'instruction': instruction.strip(), 'summary': summary,
            'applied': len(mutations), 'mutations': mutations, 'at': _now()}
        session['updatedAt'] = _now()
        write_json(self.session_path(session_id) / 'session.json', session)
        return session, usage, (summary or f'{len(mutations)} change(s) applied.'), mutations

    def update_status(self, session_id: str, status: str,
                      error: str | None = None, meta: dict | None = None,
                      clear_meta: list | None = None) -> dict:
        session = self.get_session(session_id)
        session['status'] = status
        session['updatedAt'] = _now()
        if error is not None:
            session['error'] = error
        if meta:
            session.setdefault('meta', {}).update(meta)
        for key in clear_meta or []:
            session.get('meta', {}).pop(key, None)
        sdir = self.session_path(session_id)
        write_json(sdir / 'session.json', session)
        return session

    # -----------------------------------------------------------------------
    # AI synthesis steps (Phase 2B — Create with AI). Every step is a separate
    # paid call: plan first (cheap JSON), then per-object fragments (expensive)
    # only after the user is satisfied with the draft plan.
    # -----------------------------------------------------------------------

    def plan_session_with_ai(self, session_id: str, provider,
                             instructions: str = '',
                             progress: Callable = lambda *_: None) -> Tuple[dict, dict]:
        """Synthesize ScenePlan objects from the session prompt via the AI
        provider (strict-JSON scene planning). Replaces the draft's object
        list; title/description/difficulty set by earlier mutations survive."""
        session = self.get_session(session_id)
        if session['status'] not in ('draft_plan', 'failed'):
            raise ValueError(f"Cannot plan while session is '{session['status']}'.")
        plan = session['scenePlan']
        prompt = session.get('prompt', '')
        if plan.get('description'):
            prompt += '\nScene brief so far: ' + plan['description']
        if instructions:
            prompt += '\nPlanning instructions: ' + instructions
        prompt += f"\nRequested difficulty: {session['requestedDifficulty']} (target ≈{plan['targetRegions']} gameplay regions)."
        progress(.15, 'Planning the scene with AI (objects, composition, z order)')
        raw, usage = provider.scene_plan(prompt, plan['aspect'], None, plan['targetRegions'])
        vw, vh = plan['viewBox'][2], plan['viewBox'][3]
        plan['objects'] = _plan_objects_from_provider(raw, vw, vh)
        if not plan['objects']:
            raise ValueError('The AI scene plan contained no usable objects. '
                             'The draft plan is unchanged; retry explicitly to spend again.')
        session['scenePlan'] = plan
        session['updatedAt'] = _now()
        session.setdefault('meta', {})['planUsage'] = usage
        sdir = self.session_path(session_id)
        write_json(sdir / 'session.json', session)
        return session, usage

    def generate_session_master(self, session_id: str, provider,
                                progress: Callable = lambda *_: None) -> Tuple[dict, dict]:
        """Synthesize one vector fragment per planned object, compose the
        master SVG into the session workspace, and compile + QA it — all in
        isolation. Healthy revisions are untouched until commit."""
        session = self.get_session(session_id)
        if session['status'] == 'committed':
            raise ValueError('This session is already committed.')
        objects = session['scenePlan'].get('objects') or []
        if not objects:
            raise ValueError('The ScenePlan has no objects yet. Run the AI planning step (or add objects) first.')
        self.update_status(session_id, 'generating')
        sdir = self.session_path(session_id)
        try:
            master_path = sdir / 'source-master.svg'
            old_master_text = master_path.read_text(encoding='utf-8') if master_path.is_file() else None
            # Locked objects (generation.locked) that ALREADY have artwork are
            # skipped entirely — no provider call is spent on them; their
            # exact previous shapes are re-injected after the fresh compose,
            # so unlocking is the only way their artwork changes. Locked
            # objects without artwork yet still generate (initial generation).
            from .svg_master import import_master
            old_doc = import_master(old_master_text) if old_master_text else None
            def _has_artwork(oid: str) -> bool:
                return bool(old_doc) and any(
                    sh.get('objectRef') == oid for sh in old_doc.shapes + old_doc.ink_shapes)
            locked_keep = [o['id'] for o in objects
                           if (o.get('generation') or {}).get('locked') and _has_artwork(o['id'])]
            spec_objects = [o for o in objects if o['id'] not in locked_keep]
            stages = {}
            if spec_objects:
                specs = [_fragment_spec(o) for o in spec_objects]
                svg_text, stages = provider.svg_compose_from_objects(specs, session['aspect'], progress)
                clean_svg(svg_text.encode('utf-8'), master_path)
                if locked_keep:
                    _reinject_locked_objects(master_path, old_master_text, locked_keep, objects)
                calls = len(specs)
            else:
                # everything is locked with existing artwork: the master IS
                # the artwork to keep; zero provider calls this round.
                calls = 0
                stages = {'keptLocked': {'objects': locked_keep, 'usage': {}}}
            session = self.get_session(session_id)
            session.setdefault('meta', {})['generationStages'] = stages
            if locked_keep:
                session['meta']['keptLockedObjects'] = locked_keep
            # The composed master now reflects the CURRENT plan in full —
            # every visual pending change is resolved by this bulk pass.
            session['meta'].pop('pendingArtworkChanges', None)
            session['meta'].pop('artworkStale', None)
            session['updatedAt'] = _now()
            write_json(sdir / 'session.json', session)
            usage = {'kind': 'generation-synthesis', 'calls': calls, 'stages': stages}
        except Exception as exc:
            self.update_status(session_id, 'failed', error=str(exc))
            raise
        result = self.compile_session(session_id, master_path,
                                      BuildSettings(target_regions=session['targetRegions'],
                                                    auto_subdivide=True),
                                      progress=progress)
        return self.get_session(session_id), usage

    def regenerate_session_object(self, session_id: str, provider, object_id: str,
                                  instructions: str = '',
                                  progress: Callable = lambda *_: None) -> Tuple[dict, dict]:
        """Targeted object regeneration: replace ONLY this object's shapes in
        the session master with a freshly generated fragment, then recompile.

        The objectId is preserved (internal shapeIds change); untouched
        objects keep their shapes verbatim, and neighbours' visible surfaces
        are re-derived by the recompile — local regeneration without blind
        path patching."""
        session = self.get_session(session_id)
        if session['status'] == 'committed':
            raise ValueError('This session is already committed.')
        master_path = self.session_path(session_id) / 'source-master.svg'
        if not master_path.is_file():
            raise ValueError('This session has no master yet. Run the generate step first.')
        plan_obj = next((o for o in session['scenePlan'].get('objects') or []
                         if o['id'] == object_id), None)
        if not plan_obj:
            raise ValueError(f'Object {object_id} is not part of this session plan. '
                             'Add it to the plan (and regenerate the master) first.')
        if (plan_obj.get('generation') or {}).get('locked'):
            raise ValueError(f'"{plan_obj["name"]}" is locked. Unlock it in the scene plan '
                             'before regenerating it.')
        self.update_status(session_id, 'generating')
        sdir = self.session_path(session_id)
        try:
            spec = _fragment_spec(plan_obj)
            if instructions:
                spec['description'] = (spec['description'] + '\n' if spec['description'] else '') + instructions
            view_box = '0 0 ' + ' '.join(str(int(v)) for v in session['scenePlan']['viewBox'][2:])
            fragment, usage = provider.svg_object(spec, view_box)
            progress(.7, f'Replacing object {object_id}')
            master_text = master_path.read_text(encoding='utf-8')
            new_master = replace_object_shapes(master_text, object_id, fragment,
                                               object_name=plan_obj['name'])
            # The replaced master is already sanitized output of emit_master_svg;
            # persist it verbatim and recompile the bundle from it.
            master_path.write_text(new_master, encoding='utf-8')
            session = self.get_session(session_id)
            session.setdefault('meta', {})[f'regenUsage:{object_id}'] = usage
            # This object's artwork now matches the plan again — resolve ONLY
            # its pending entry (other objects stay pending until their own
            # regeneration; Task 29 review patch).
            pending = session.get('meta', {}).get('pendingArtworkChanges') or {}
            pending.pop(object_id, None)
            if pending:
                session['meta']['pendingArtworkChanges'] = pending
            else:
                session['meta'].pop('pendingArtworkChanges', None)
                session['meta'].pop('artworkStale', None)
            session['updatedAt'] = _now()
            write_json(sdir / 'session.json', session)
            usage_out = {'kind': 'object-regeneration', 'objectId': object_id, **usage}
        except Exception as exc:
            self.update_status(session_id, 'failed', error=str(exc))
            raise
        result = self.compile_session(session_id, master_path,
                                      BuildSettings(target_regions=session['targetRegions'],
                                                    auto_subdivide=True),
                                      progress=progress)
        return self.get_session(session_id), usage_out

    def plan_session_from_image(self, session_id: str, provider, reference: Path,
                                instructions: str = '',
                                progress: Callable = lambda *_: None) -> Tuple[dict, dict]:
        """Phase 2C — Use as Reference: vision understanding of an uploaded
        image becomes a NEW semantic ScenePlan, and the artwork itself is
        generated as native vectors afterwards. Per the provider contract the
        plan reuses only the image's broad mood, palette and subject
        categories — never its composition: this answers 'what is in the
        image and what makes the composition recognizable', not 'where are
        the pixel color boundaries'. Image-reference sessions REUSE the
        Create-with-AI machinery — only the initial plan input differs."""
        session = self.get_session(session_id)
        if session['status'] not in ('draft_plan', 'failed'):
            raise ValueError(f"Cannot plan while session is '{session['status']}'.")
        if session['mode'] not in ('image_reference', 'ai_chat'):
            raise ValueError('This session mode does not support reference planning.')
        if not reference.is_file():
            raise ValueError('Upload the reference image first.')
        plan = session['scenePlan']
        prompt = session.get('prompt', '') or 'Interpret this reference image as an original artwork.'
        if plan.get('description'):
            prompt += '\nScene brief so far: ' + plan['description']
        if instructions:
            prompt += '\nReference interpretation instructions: ' + instructions
        prompt += f"\nRequested difficulty: {session['requestedDifficulty']} (target ≈{plan['targetRegions']} gameplay regions)."
        progress(.15, 'Understanding the reference (vision scene analysis)')
        raw, usage = provider.scene_plan(prompt, plan['aspect'], reference, plan['targetRegions'])
        vw, vh = plan['viewBox'][2], plan['viewBox'][3]
        plan['objects'] = _plan_objects_from_provider(raw, vw, vh)
        if not plan['objects']:
            raise ValueError('The vision scene plan contained no usable objects. '
                             'The draft plan is unchanged; retry explicitly to spend again.')
        session['scenePlan'] = plan
        session['updatedAt'] = _now()
        session.setdefault('meta', {})['planUsage'] = usage
        session['meta']['referenceFile'] = reference.name
        sdir = self.session_path(session_id)
        write_json(sdir / 'session.json', session)
        return session, usage

    def convert_session_image(self, session_id: str, provider, source_png: Path,
                              policy_overrides: dict | None = None,
                              instructions: str = '',
                              progress: Callable = lambda *_: None) -> dict:
        """Phase 2D — Convert Artwork, end to end inside the session sandbox.

        understand (vision semantic decomposition, composition=True)
          → candidate segmentation (SLIC via the compiler's own label step)
          → semantic association (score-based segment→objectId)
          → vector reconstruction (the existing raster compiler with the
            segment map — shared boundary fitting stays watertight)
          → quality scoring (visualFidelity / gameReadiness, gated)
          → gameplay difficulty optimization (regions/labels/budgets only —
            the reconstruction stays byte-identical across tiers)
          → ready_to_commit or a failed, revisable session.
        Healthy revisions are never touched."""
        session = self.get_session(session_id)
        if session['status'] == 'committed':
            raise ValueError('This session is already committed.')
        if session['mode'] != 'image_convert':
            raise ValueError('Conversion requires a session with mode image_convert.')
        if not source_png.is_file():
            raise ValueError('Upload the source image first.')
        policy = resolve_convert_policy(session.get('fidelity') or 'balanced', policy_overrides)
        plan = session['scenePlan']

        # 1. semantic decomposition: AI decides WHAT the objects are
        prompt = session.get('prompt', '') or 'Convert this image into structured Color Duel artwork.'
        if instructions:
            prompt += '\nConversion instructions: ' + instructions
        progress(.08, 'Understanding the image (semantic decomposition)')
        raw, usage = provider.scene_plan(prompt, plan['aspect'], source_png,
                                         plan['targetRegions'], composition=True)
        vw, vh = plan['viewBox'][2], plan['viewBox'][3]
        plan['objects'] = _plan_objects_from_provider(raw, vw, vh)
        if not plan['objects']:
            raise ValueError('The vision decomposition found no usable objects. '
                             'The session is unchanged; retry explicitly to spend again.')
        session['scenePlan'] = plan
        session.setdefault('meta', {})['planUsage'] = usage
        session['meta']['convertPolicy'] = {k: v for k, v in policy.items()}
        sdir = self.session_path(session_id)
        write_json(sdir / 'session.json', session)

        # 2+3. candidate segmentation + semantic association (deterministic CV)
        # Candidate count is FIDELITY-ONLY (Phase 2D.1): a fixed base density
        # scaled by segmentDensity, never by the difficulty target — the
        # reconstruction must be byte-identical across Easy..Master; gameplay
        # density comes from per-object subdivision budgets, not from here.
        self.update_status(session_id, 'compiling')
        # Candidate segmentation is FIDELITY-ONLY (Phase 2D.1): a fixed base
        # density scaled by segmentDensity, never by the difficulty target —
        # the reconstruction must be byte-identical across Easy..Master. The
        # requested difficulty is applied AFTERWARD by the gameplay-only
        # difficulty optimizer (Task 26), which re-subdivides/merges regions
        # toward the tier without touching the reconstruction.
        convert_settings = BuildSettings(
            target_regions=CONVERT_CANDIDATE_BASE,    # fidelity-only; see above
            auto_subdivide=False,
            curve_tolerance=float(policy['curveTolerance']),
            min_region_pixels=max(4, int(policy['minComponentArea'])),
            palette_colors=max(4, min(80, int(policy['paletteTarget']))),
        )
        progress(.30, 'Segmenting the image (candidate regions)')
        from .pipeline import _image_labels
        rgb, labels, original, (w, h) = _image_labels(source_png, convert_settings,
                                                      segment_density=float(policy['segmentDensity']),
                                                      color_merge_delta_e=float(policy['colorMergeDeltaE']))
        progress(.42, 'Associating segments with semantic objects')
        plan_vw, plan_vh = plan['viewBox'][2], plan['viewBox'][3]
        label_map, segments, object_stats = associate_segments_to_objects(
            rgb, labels, plan['objects'], policy, plan_space=(plan_vw, plan_vh))
        decomposition = {'schemaVersion': 1, 'policy': {k: v for k, v in policy.items()},
                         'objects': plan['objects'], 'objectStats': object_stats,
                         'segments': segments,
                         'unassignedSegments': sum(1 for s in segments if s['objectId'] == 'unassigned')}
        write_json(sdir / 'decomposition.json', decomposition)

        # 4. vector reconstruction + gameplay compilation via the EXISTING
        #    raster compiler (shared-boundary fitting stays watertight)
        bundle_dir = sdir / 'bundle'
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir, ignore_errors=True)
        try:
            result = compile_image(source_png, bundle_dir, artwork_id=self.pid,
                                   version='0.0.0-draft', title=plan['title'],
                                   settings=convert_settings, segment_object_map=label_map,
                                   objects=plan['objects'],
                                   segment_density=float(policy['segmentDensity']),
                                   color_merge_delta_e=float(policy['colorMergeDeltaE']),
                                   provenance={'source': 'User-supplied image (Convert pipeline; semantic decomposition + deterministic segmentation)',
                                               'rightsConfirmedByUser': True,
                                               'legalClearanceVerified': False},
                                   progress=progress)
        except Exception as exc:
            self.update_status(session_id, 'failed', error=str(exc))
            raise

        # 5. quality scoring gate (visualFidelity + gameReadiness, never averaged)
        scores = conversion_quality_score(
            {'geometry': result.get('geometry') or load_bundle(bundle_dir)['geometry'],
             'paint': read_json(bundle_dir / 'paint.json'),
             'manifest': read_json(bundle_dir / 'artwork.json')},
            rgb, policy)
        session = self.get_session(session_id)
        session['meta']['qa'] = result.get('validation') or read_json(bundle_dir / 'validation.json')
        session['meta']['conversionScores'] = scores
        session['meta']['decompositionSummary'] = {
            'objects': len(plan['objects']), 'segments': len(segments),
            'unassignedSegments': decomposition['unassignedSegments'],
            'objectStats': object_stats}
        session.setdefault('meta', {})['generationStages'] = {'convert': {'calls': 1, 'segments': len(segments)}}
        if not scores['passed']:
            reasons = '; '.join(scores['notes'] or [
                f"visualFidelity {scores['visualFidelity']} < gate {scores['gates']['visualGate']}"
                if scores['visualFidelity'] < scores['gates']['visualGate']
                else f"gameReadiness {scores['gameReadiness']} < gate {scores['gates']['gameReadinessGate']}"])
            self.update_status(session_id, 'failed',
                               error=f'Conversion quality gate rejected the result: {reasons}')
            raise ValueError(f'Conversion quality gate rejected the result: {reasons}')

        # 6. Difficulty Optimization (Task 26): the artwork is FROZEN from
        #    here on — the requested tier reshapes ONLY the gameplay layer
        #    (regions, labels, object subdivision budgets). The optimizer
        #    verifies the paint/objects.shapeIds invariants itself; if the
        #    moved geometry were to break the readiness gate anyway, the
        #    pre-optimization gameplay is restored and shipped instead.
        tier = session.get('requestedDifficulty')
        if tier not in DIFFICULTY_TIERS:
            tier = 'hard'
        progress(.84, f'Optimizing gameplay geometry toward {tier}')
        bundle = load_bundle(bundle_dir)
        pre = {
            'regions': copy.deepcopy(bundle['geometry']['regions']),
            'partitionTolerance': bundle['geometry'].get('partitionTolerance', 0.0),
            'objects': copy.deepcopy(bundle.get('objects') or []),
            'difficulty': copy.deepcopy(bundle['manifest'].get('difficulty')),
            'regionCount': bundle['manifest'].get('regionCount'),
        }
        bundle, diff_report = optimize_gameplay_difficulty(
            bundle, tier, target_regions=int(session['targetRegions']), progress=progress)
        emit_bundle(bundle_dir, bundle)
        post_scores = conversion_quality_score(
            {'geometry': bundle['geometry'], 'paint': read_json(bundle_dir / 'paint.json'),
             'manifest': read_json(bundle_dir / 'artwork.json')}, rgb, policy)
        if not post_scores['passed']:
            geometry = bundle['geometry']
            geometry['regions'] = pre['regions']
            geometry['partitionTolerance'] = pre['partitionTolerance']
            bundle['objects'] = pre['objects'] or None
            bundle['manifest']['difficulty'] = pre['difficulty']
            bundle['manifest']['regionCount'] = pre['regionCount']
            diff_report['reverted'] = ('post-optimization quality gate failed; '
                                       'kept the initial gameplay geometry')
            diff_report['regionCountAfter'] = len(pre['regions'])
            diff_report['changed'] = False
            emit_bundle(bundle_dir, bundle)
            post_scores = scores
        session['meta']['qa'] = read_json(bundle_dir / 'validation.json')
        session['meta']['conversionScores'] = post_scores
        session['meta']['difficultyOptimization'] = diff_report
        session['status'] = 'ready_to_commit'
        session['updatedAt'] = _now()
        write_json(sdir / 'session.json', session)
        # reconstructed-master.svg audit artifact (paint layer of the bundle)
        try:
            from .pipeline import svg_open, svg_paint as _sp
            (sdir / 'reconstructed-master.svg').write_text(
                svg_open(w, h) + _sp(read_json(bundle_dir / 'paint.json')) + '</svg>', encoding='utf-8')
        except Exception:
            pass
        return session

    def cancel_session(self, session_id: str) -> dict:
        """Cancel an in-progress or draft session and clean up temp assets."""
        session = self.get_session(session_id)
        if session['status'] == 'committed':
            raise ValueError('Cannot cancel an already committed session.')
        session['status'] = 'canceled'
        session['updatedAt'] = _now()
        sdir = self.session_path(session_id)
        # Clean up temporary build files while keeping session.json for audit
        bundle_dir = sdir / 'bundle'
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir, ignore_errors=True)
        write_json(sdir / 'session.json', session)
        return session

    def discard_session(self, session_id: str) -> None:
        """Permanently delete a session folder."""
        sdir = self.session_path(session_id)
        if sdir.exists():
            shutil.rmtree(sdir, ignore_errors=True)

    def compile_session(self, session_id: str, master_svg_path: Path,
                        settings: BuildSettings | None = None,
                        progress: Callable = lambda *_: None) -> dict:
        """Compile master SVG into session's isolated temp bundle and validate.

        Never touches healthy project revisions.
        """
        session = self.get_session(session_id)
        sdir = self.session_path(session_id)
        bundle_dir = sdir / 'bundle'
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir, ignore_errors=True)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        plan = session['scenePlan']
        effective_settings = settings or BuildSettings(
            target_regions=plan['targetRegions'],
            auto_subdivide=True,
        )

        self.update_status(session_id, 'compiling')
        try:
            result = compile_svg_master(
                source=master_svg_path,
                output=bundle_dir,
                artwork_id=self.pid,
                version='0.0.0-draft',
                title=plan['title'],
                settings=effective_settings,
                objects=plan['objects'],
                progress=progress,
            )
            qa = result['validation']
            if not qa.get('passed', False):
                err_msg = 'Geometry QA checks failed: ' + '; '.join(qa.get('errors', ['Unknown error'])[:3])
                self.update_status(session_id, 'failed', error=err_msg)
                raise ValueError(err_msg)

            # Store measured difficulty and validation in session. A compile
            # only marks the artwork READY when no visual plan change is
            # pending: geometry QA passing proves the OLD master is valid, not
            # that it matches the newest plan (Task 29 review patch).
            measured = {
                'rating': result['manifest']['difficulty']['rating'],
                'score': result['manifest']['difficulty']['score'],
                'metrics': result['manifest']['difficulty']['metrics'],
            }
            pending = self.get_session(session_id).get('meta', {}).get('pendingArtworkChanges') or {}
            if pending:
                # The compiled bundle is valid but visually outdated — keep it
                # for preview, stay in draft_plan with the pending list intact.
                self.update_status(session_id, 'draft_plan', meta={
                    'qa': qa,
                    'measuredDifficulty': measured,
                    'regionCount': result['manifest']['regionCount'],
                })
            else:
                self.update_status(session_id, 'ready_to_commit', meta={
                    'qa': qa,
                    'measuredDifficulty': measured,
                    'regionCount': result['manifest']['regionCount'],
                }, clear_meta=['artworkStale'])
            return result
        except Exception as exc:
            self.update_status(session_id, 'failed', error=str(exc))
            raise

    def commit_session(self, session_id: str, version: str,
                       title: str | None = None) -> dict:
        """Atomically promote a verified session bundle into an immutable project revision.

        Guarantees:
        - Session must have a passed bundle in ready_to_commit status.
        - The new revision directory is created and populated.
        - Manifest is enriched with the generation block.
        - Session is marked 'committed'.
        - Current healthy revision was untouched during generation.
        """
        session = self.get_session(session_id)
        if session['status'] != 'ready_to_commit':
            raise ValueError(f"Session cannot be committed in status '{session['status']}'. It must be 'ready_to_commit'.")
        pending = session.get('meta', {}).get('pendingArtworkChanges') or {}
        if pending:
            pending_list = ', '.join(f'{oid} ({"/".join(fields)})' for oid, fields in sorted(pending.items()))
            raise ValueError('The plan has visual changes the artwork does not reflect yet: '
                             f'{pending_list}. Regenerate those objects (or bulk regenerate) before committing.')

        sdir = self.session_path(session_id)
        bundle_dir = sdir / 'bundle'
        if not (bundle_dir / 'artwork.json').is_file():
            raise FileNotFoundError('Session bundle is missing or incomplete.')

        rev_id = f'rev-{uuid.uuid4().hex[:16]}'
        rev_dir = self.project_dir / 'revisions' / rev_id
        if rev_dir.exists():
            raise FileExistsError(f'Revision directory {rev_id} already exists.')

        # Copy bundle atomically to revision folder
        shutil.copytree(bundle_dir, rev_dir)

        # Update manifest in destination revision
        mfile = rev_dir / 'artwork.json'
        manifest = read_json(mfile)
        manifest['version'] = version
        if title:
            manifest['title'] = title

        # Stamp the rich generation contract
        plan = session['scenePlan']
        measured = session.get('meta', {}).get('measuredDifficulty') or {
            'rating': manifest.get('difficulty', {}).get('rating', 'unrated'),
            'score': manifest.get('difficulty', {}).get('score', 0),
        }
        manifest['generation'] = {
            'mode': session['mode'],
            'requestedDifficulty': session['requestedDifficulty'],
            'targetRegionRange': session['targetRegionRange'],
            'measuredDifficulty': measured,
            'fidelity': session.get('fidelity'),
            'sessionId': session_id,
            'scenePrompt': session.get('prompt', ''),
            'scenePlan': plan,
            'committedAt': _now(),
        }
        write_json(mfile, manifest)

        # Also update regions.json version
        rfile = rev_dir / 'regions.json'
        if rfile.is_file():
            geom = read_json(rfile)
            geom['artworkVersion'] = version
            write_json(rfile, geom)

        # Mark session committed
        self.update_status(session_id, 'committed', meta={'committedRevision': rev_id})

        qa = read_json(rev_dir / 'validation.json')
        return {
            'revision': {
                'id': rev_id,
                'version': version,
                'createdAt': _now(),
                'kind': 'generation',
                'generationMode': session['mode'],
                'regionCount': manifest.get('regionCount', 0),
                'qa': qa,
                'manifestUrl': f'/api/projects/{self.pid}/revisions/{rev_id}/files/artwork.json',
            },
            'manifest': manifest,
            'validation': qa,
        }
