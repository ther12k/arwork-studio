from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class BuildSettings(StrictModel):
    target_regions: int = Field(650, ge=30, le=1600)
    palette_colors: int = Field(32, ge=4, le=80)
    paint_colors: int = Field(80, ge=16, le=160)
    max_edge: int = Field(1024, ge=256, le=1536)
    compactness: float = Field(10.0, ge=1, le=25)
    min_region_pixels: int = Field(35, ge=4, le=600)
    min_label_radius: float = Field(3.0, ge=1, le=12)
    ink_threshold: int = Field(40, ge=0, le=80)
    backend: Literal['spline-local', 'polygon-legacy'] = Field('spline-local',
        description='Conversion backend: curve-preserving local spline fitting (default) or the pre-upgrade pixel-edge polygon tracer (comparison).')
    curve_tolerance: float = Field(1.0, ge=0.1, le=3.0,
        description='Max px deviation of fitted curves from the pixel-exact boundary. Higher smooths more; corners are preserved separately.')
    corner_angle_deg: float = Field(60.0, ge=20.0, le=120.0,
        description='Turning angle above which a boundary vertex is a deliberate corner and stays straight.')
    auto_subdivide: bool = Field(False,
        description='Deterministically subdivide oversized regions with organic hand-cut boundaries until the target region count is reached (SVG-master builds; true-vector, no rasterization).')

class CreateProject(StrictModel):
    title: str = Field('Untitled artwork', min_length=1, max_length=100)
    brief: str = Field('', max_length=8000)

class UpdateProject(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    brief: str = Field(max_length=8000)

class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=3000)
    include_reference: bool = True
    confirm_paid: bool = False

class GenerateRequest(StrictModel):
    source: Literal['brief', 'reference', 'current'] = 'brief'
    prompt: str = Field(min_length=1, max_length=12000)
    quality: Literal['low', 'medium', 'high'] = 'medium'
    size: Literal['1024x1536', '1536x1024', '1024x1024'] = '1024x1536'
    confirm_paid: bool = False

class GenerateSvgRequest(StrictModel):
    """Separate SVG-generation route: providers that can author SVG masters."""
    prompt: str = Field(min_length=1, max_length=12000)
    aspect: Literal['1024x1536', '1536x1024', '1024x1024'] = '1024x1536'
    mode: Literal['single', 'multistage'] = Field('single',
        description='single = one master in one call; multistage = scene plan + per-object vector fragments composed in z order.')
    target_regions: int = Field(300, ge=60, le=1200,
        description='Multistage only: region count the next deterministic build should aim for via auto-subdivide.')
    include_reference: bool = True
    confirm_paid: bool = False

class PromoteRequest(StrictModel):
    rights_confirmed: bool

class EditRequest(StrictModel):
    base_revision: str
    action: Literal['merge', 'group', 'palette', 'recolor', 'label', 'decorate', 'split', 'cut', 'draw', 'node', 'shape']
    region_ids: list[str] = Field(max_length=1600,
        description='Validated per action: merge>=2, split/cut/label=1, node=2 (the two regions sharing the dragged boundary), draw=0, others>=1.')
    d: str | None = Field(None, pattern=r'^M[\s\d.,eE+\-MLQCZ]+$',
        description="SVG path data in master units: 'cut' = open line crossing the region, 'draw' = closed pen shape (Z closes it), 'node' = the dragged new shared-boundary polyline, 'shape' = the edited master path — CLOSED for filled shapes (must end Z), OPEN for ink strokes authored open (M… without Z; per-subpath positive length).")
    group: str = Field('unassigned', pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    palette_id: int | None = None
    x: float | None = Field(None, allow_inf_nan=False)
    y: float | None = Field(None, allow_inf_nan=False)
    # 'recolor' (visible appearance, distinct from 'palette' = number group):
    color: str | None = Field(None, pattern=r'^#[0-9A-Fa-f]{6}$')
    preserve_shading: bool = Field(False, description='Keep gradient shading (tinted toward the target color) instead of replacing the fill.')
    # 'draw' artwork pen (P0 contract): emit a paint.json path with a stable
    # shapeId for the drawn shape, set masterShapeId on the playable region and
    # keep the palette swatch in sync - the normal recolor / QA / revision flow
    # then works on pen-drawn art exactly like imported SVG shapes.
    paint: bool = Field(False,
        description="'draw': also emit a paint.json artwork path (stable shapeId + masterShapeId, fill, z-order). False = gameplay-only white tap target (region pen).")
    stroke_width: float | None = Field(None, ge=0, le=8,
        description="'draw' with paint=true: draw an ink outline of this width on the paint path (0 or null = no outline).")
    z_behind: bool = Field(False,
        description="'draw' with paint=true: place the new paint path BEHIND the existing art (min z - 1) instead of on top (max z + 1).")
    # Task 33 — Artwork Path node mode: edit ONE source shape's geometry by
    # its stable internal id. The full recompile re-derives paint + visible
    # gameplay surfaces; shapeId and object ownership are preserved.
    shape_id: str | None = Field(None, min_length=1, max_length=64)

class ReviewRequest(StrictModel):
    revision: str
    note: str = Field(min_length=10, max_length=1500)
    confirmed: bool

class PlaytestRecord(StrictModel):
    """One completed play-test run recorded against a revision.

    Puzzle modes (number/memory/duel) feed difficulty calibration; 'free' is
    tracked as an engagement/interaction metric only and never blended into
    the puzzle difficulty score.
    """
    seconds: float = Field(..., ge=10, le=86400)
    filled: int = Field(..., ge=1)
    total: int = Field(..., ge=1)
    mistakes: int = Field(..., ge=0)
    mode: Literal['number', 'memory', 'duel', 'free']

class ActivateRequest(StrictModel):
    revision: str

class OptimizeRequest(StrictModel):
    """Task 27 — Optimize Difficulty: reshape the GAMEPLAY layer of the
    current revision toward a tier. The artwork (paint bytes, object
    shapeIds, source master) stays byte-identical; the engine is
    studio.difficulty.optimize_gameplay_difficulty."""
    base_revision: str
    tier: Literal['easy', 'medium', 'hard', 'master']

class CreateSessionRequest(StrictModel):
    mode: Literal['ai_chat', 'image_reference', 'image_convert'] = 'ai_chat'
    requested_difficulty: Literal['easy', 'medium', 'hard', 'master'] = 'hard'
    prompt: str = Field('', max_length=12000)
    aspect: Literal['1024x1536', '1536x1024', '1024x1024'] = '1024x1536'
    fidelity: Literal['stylized', 'balanced', 'faithful'] | None = 'balanced'

class MutateScenePlanRequest(StrictModel):
    mutations: list[dict] = Field(..., max_length=50)

class CommitSessionRequest(StrictModel):
    title: str | None = Field(None, max_length=100)
    idempotency_key: str = Field('', max_length=80)

class ObjectUpdateRequest(StrictModel):
    """Task 32 — Semantic Object / Layer inspector update request: rename,
    reparent, lock, adjust subdivision detail priority, or reorder layers
    in an existing committed revision."""
    base_revision: str
    object_id: str = Field(..., min_length=1, max_length=64)
    name: str | None = Field(None, min_length=1, max_length=80)
    parent_id: str | None = Field(None, max_length=64)
    locked: bool | None = None
    detail_weight: float | None = Field(None, ge=0.01, le=20.0)
    min_regions: int | None = Field(None, ge=0, le=5000)
    preferred_regions: int | None = Field(None, ge=0, le=5000)
    max_regions: int | None = Field(None, ge=0, le=5000)
    order_action: Literal['bring_to_front', 'send_to_back', 'above', 'below'] | None = None
    target_object_id: str | None = Field(None, max_length=64)

