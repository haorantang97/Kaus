"""Authenticated catalogue and atomic Project Skill arrangement endpoints."""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.api.api_errors import json_error_endpoint
from app.capabilities.skill_tree import SkillTreeService


class SkillTreeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    action: Literal["assign", "move", "copy", "block", "restore", "remove", "reorder", "undo"]
    skillId: str | None = None
    sourceProjectId: str | None = None
    targetProjectId: str | None = None
    beforeSkillId: str | None = None
    undoToken: str | None = None


class SkillOnboardingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    outcome: Literal["skipped", "done"]


def build_skill_tree_router(service: SkillTreeService, *, auth_policy: Any):
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse
    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix="/api", tags=["skills"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @router.get("/skill-tree", dependencies=[auth])
    @endpoint
    async def read_tree():
        return await asyncio.to_thread(service.snapshot)

    @router.post("/skill-tree/actions", dependencies=[auth])
    @endpoint
    async def act_on_tree(body: SkillTreeAction):
        return await asyncio.to_thread(service.act, body.model_dump())

    @router.get("/skill-tree/onboarding", dependencies=[auth])
    @endpoint
    async def onboarding():
        return await asyncio.to_thread(service.pending_onboarding)

    @router.post("/skill-tree/onboarding", dependencies=[auth])
    @endpoint
    async def finish_onboarding(body: SkillOnboardingResult):
        return await asyncio.to_thread(service.finish_onboarding, body.id, body.outcome)

    return router
