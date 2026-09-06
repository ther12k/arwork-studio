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

class PromoteRequest(StrictModel):
    rights_confirmed: bool

class EditRequest(StrictModel):
    base_revision: str
    action: Literal['merge','group','palette','label','decorate']
    region_ids: list[str] = Field(min_length=1, max_length=1600)
    group: str = Field('unassigned', pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    palette_id: int | None = None
    x: float | None = Field(None, allow_inf_nan=False)
    y: float | None = Field(None, allow_inf_nan=False)

class ReviewRequest(StrictModel):
    revision: str
    note: str = Field(min_length=10, max_length=1500)
    confirmed: bool

class ActivateRequest(StrictModel):
    revision: str
