"""Validated module manifests and host-owned execution settings."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

PERMISSIONS = frozenset({"agent:run", "data:read", "data:write", "backend:run", "editor:chatcut"})
MODULE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


def validate_relative_entry(value: str, suffixes: tuple[str, ...]) -> str:
    if value.startswith(("/", "\\")) or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")) or not value.endswith(suffixes):
        raise ValueError("Entry must be a relative package file")
    return value


class ModuleBackend(WireModel):
    runtime: Literal["node"] = "node"
    entry: str = Field(min_length=1, max_length=200)

    @field_validator("entry")
    @classmethod
    def valid_entry(cls, value: str) -> str:
        return validate_relative_entry(value, (".mjs", ".js"))


class Manifest(WireModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=300)
    icon: str = Field(default="box", max_length=32)
    entry: str = Field(default="index.html", min_length=1, max_length=200)
    permissions: list[str] = Field(default_factory=list, max_length=8)
    capabilities: list[Literal["workflow"]] = Field(default_factory=list, max_length=1)
    execution_policy: Literal["project", "context-only"] = "project"
    backend: ModuleBackend | None = None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not MODULE_ID.fullmatch(value):
            raise ValueError("Invalid module id")
        return value

    @field_validator("entry")
    @classmethod
    def valid_entry(cls, value: str) -> str:
        return validate_relative_entry(value, (".html",))

    @model_validator(mode="after")
    def backend_permission(self):
        if self.backend is not None and "backend:run" not in self.permissions:
            raise ValueError("A backend requires backend:run permission")
        return self

    @field_validator("permissions")
    @classmethod
    def valid_permissions(cls, value: list[str]) -> list[str]:
        if set(value) - PERMISSIONS or len(set(value)) != len(value):
            raise ValueError("Unknown or repeated permission")
        return value


class ModulePatch(WireModel):
    enabled: bool | None = None
    pinned: bool | None = None
    project_id: str | None = Field(default=None, max_length=256)
    binding_id: str | None = Field(default=None, max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    provider_id: str | None = Field(default=None, max_length=256)
    reasoning_mode: str | None = Field(default=None, max_length=256)
    grants: list[str] | None = Field(default=None, max_length=8)


class StepDefinition(WireModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=50000)


class Workflow(WireModel):
    name: str = Field(min_length=1, max_length=100)
    steps: list[StepDefinition] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_steps(self):
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("Step ids must be unique")
        return self


class PromptBody(WireModel):
    prompt: str = Field(min_length=1, max_length=100000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class WorkflowRunBody(WireModel):
    input: str = Field(default="", max_length=100000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class DataBody(WireModel):
    value: Any


class InstallBody(WireModel):
    path: str = Field(min_length=1, max_length=4096)


class PinsBody(WireModel):
    module_ids: list[str] = Field(max_length=100)
