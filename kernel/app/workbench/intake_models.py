"""Host-owned local project intake requests."""
from pydantic import Field
from app.workbench.models import WireModel


class InspectBody(WireModel):
    path: str = Field(min_length=1, max_length=4096)


class AdaptBody(WireModel):
    project_id: str = Field(min_length=1, max_length=256)
    binding_id: str = Field(min_length=1, max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    provider_id: str | None = Field(default=None, max_length=256)
    reasoning_mode: str | None = Field(default=None, max_length=256)
    instruction: str = Field(default="", max_length=6000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class InstallIntakeBody(WireModel):
    project_id: str | None = Field(default=None, max_length=256)
    binding_id: str | None = Field(default=None, max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    provider_id: str | None = Field(default=None, max_length=256)
    reasoning_mode: str | None = Field(default=None, max_length=256)
    grants: list[str] = Field(default_factory=list, max_length=8)
