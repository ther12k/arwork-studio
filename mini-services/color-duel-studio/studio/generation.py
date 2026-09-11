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
from pathlib import Path
import re
import shutil
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import BuildSettings
from .pipeline import (
    OBJECTS_SCHEMA_VERSION,
    compile_svg_master,
    load_bundle,
    normalize_objects,
    read_json,
    validate_bundle,
    write_json,
)
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

    def mutate_plan(self, session_id: str, mutations: list[dict]) -> dict:
        """Apply mutations to the draft plan of an active session."""
        session = self.get_session(session_id)
        if session['status'] not in ('draft_plan', 'failed'):
            raise ValueError(f"Cannot mutate plan while session is {session['status']}.")

        updated_plan = apply_scene_mutations(session['scenePlan'], mutations)
        session['scenePlan'] = updated_plan
        session['requestedDifficulty'] = updated_plan['requestedDifficulty']
        session['targetRegionRange'] = updated_plan['targetRegionRange']
        session['targetRegions'] = updated_plan['targetRegions']
        session['updatedAt'] = _now()
        session['status'] = 'draft_plan'
        session['error'] = None

        sdir = self.session_path(session_id)
        write_json(sdir / 'session.json', session)
        return session

    def update_status(self, session_id: str, status: str,
                      error: str | None = None, meta: dict | None = None) -> dict:
        session = self.get_session(session_id)
        session['status'] = status
        session['updatedAt'] = _now()
        if error is not None:
            session['error'] = error
        if meta:
            session.setdefault('meta', {}).update(meta)
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
            specs = [_fragment_spec(o) for o in objects]
            svg_text, stages = provider.svg_compose_from_objects(specs, session['aspect'], progress)
            master_path = sdir / 'source-master.svg'
            clean_svg(svg_text.encode('utf-8'), master_path)
            session = self.get_session(session_id)
            session.setdefault('meta', {})['generationStages'] = stages
            session['updatedAt'] = _now()
            write_json(sdir / 'session.json', session)
            usage = {'kind': 'generation-synthesis', 'calls': len(specs), 'stages': stages}
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

            # Store measured difficulty and validation in session
            measured = {
                'rating': result['manifest']['difficulty']['rating'],
                'score': result['manifest']['difficulty']['score'],
                'metrics': result['manifest']['difficulty']['metrics'],
            }
            self.update_status(session_id, 'ready_to_commit', meta={
                'qa': qa,
                'measuredDifficulty': measured,
                'regionCount': result['manifest']['regionCount'],
            })
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
