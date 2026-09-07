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
    include_reference: bool = True
    confirm_paid: bool = False

class PromoteRequest(StrictModel):
    rights_confirmed: bool

class EditRequest(StrictModel):
    base_revision: str
    action: Literal['merge', 'group', 'palette', 'recolor', 'label', 'decorate', 'split']
    region_ids: list[str] = Field(min_length=1, max_length=1600)
    group: str = Field('unassigned', pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    palette_id: int | None = None
    x: float | None = Field(None, allow_inf_nan=False)
    y: float | None = Field(None, allow_inf_nan=False)
    # 'recolor' (visible appearance, distinct from 'palette' = number group):
    color: str | None = Field(None, pattern=r'^#[0-9A-Fa-f]{6}$')
    preserve_shading: bool = Field(False, description='Keep gradient shading (tinted toward the target color) instead of replacing the fill.')

class ReviewRequest(StrictModel):
    revision: str
    note: str = Field(min_length=10, max_length=1500)
    confirmed: bool

class ActivateRequest(StrictModel):
    revision: str
