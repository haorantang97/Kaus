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


class CapabilityImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agentKey: str
    ids: list[str] = []


class SelfCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agentKey: str
    modelId: str | None = None


class SelfCheckReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict[str, Any]] = []
    summary: str | None = None


class CapabilitySyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class SkillOnboardingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    outcome: Literal["skipped", "done"]


def build_skill_tree_router(service: SkillTreeService, *, auth_policy: Any, imports: Any = None, self_check: Any = None):
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

    @router.get("/skill-tree/onboarding/{invitation_id}", dependencies=[auth])
    @endpoint
    async def onboarding_status(invitation_id: str):
        return await asyncio.to_thread(service.onboarding_status, invitation_id)

    @router.post("/skill-tree/onboarding", dependencies=[auth])
    @endpoint
    async def finish_onboarding(body: SkillOnboardingResult):
        return await asyncio.to_thread(service.finish_onboarding, body.id, body.outcome)

    if imports is not None:
        # Explicit import of an Agent's own capabilities and its change report
        # (docs/product/capability-import-20261110.md).
        @router.get("/skill-tree/import", dependencies=[auth])
        @endpoint
        async def import_agents():
            return await asyncio.to_thread(imports.agents)

        @router.get("/skill-tree/import/{agent_key}", dependencies=[auth])
        @endpoint
        async def import_candidates(agent_key: str):
            return await asyncio.to_thread(imports.candidates, agent_key)

        @router.post("/skill-tree/import", dependencies=[auth])
        @endpoint
        async def import_selected(body: CapabilityImportRequest):
            return await asyncio.to_thread(imports.import_selected, body.agentKey, body.ids)

        @router.post("/skill-tree/import/skip", dependencies=[auth])
        @endpoint
        async def import_skip(body: CapabilityImportRequest):
            return await asyncio.to_thread(imports.skip, body.agentKey)

        @router.post("/skill-tree/import/ignore", dependencies=[auth])
        @endpoint
        async def import_ignore(body: CapabilityImportRequest):
            return await asyncio.to_thread(imports.ignore, body.agentKey, body.ids)

        @router.post("/skill-tree/import/bind", dependencies=[auth])
        @endpoint
        async def import_bind(body: CapabilityImportRequest):
            return await asyncio.to_thread(imports.mark_bound, body.agentKey, body.ids)

        @router.post("/skill-tree/import/sync", dependencies=[auth])
        @endpoint
        async def import_sync(body: CapabilitySyncRequest):
            return await asyncio.to_thread(imports.sync, body.id)

        @router.get("/skill-tree/changes", dependencies=[auth])
        @endpoint
        async def capability_changes():
            return await asyncio.to_thread(imports.changes)

        @router.post("/skill-tree/changes/seen", dependencies=[auth])
        @endpoint
        async def capability_changes_seen():
            return await asyncio.to_thread(imports.mark_seen)

    if self_check is not None:
        # Agent 自查 (2026-11-11): one read-only conversation per run, review submitted by the built-in MCP.
        @router.get("/skill-tree/self-check/options/{agent_key}", dependencies=[auth])
        @endpoint
        async def self_check_options(agent_key: str):
            return await self_check.options(agent_key)

        @router.post("/skill-tree/self-check", dependencies=[auth])
        @endpoint
        async def self_check_start(body: SelfCheckRequest):
            return await self_check.start(body.agentKey, body.modelId)

        @router.get("/skill-tree/self-check/{conversation_id}", dependencies=[auth])
        @endpoint
        async def self_check_get(conversation_id: str):
            return await self_check.get(conversation_id)

        @router.post("/skill-tree/self-check/{conversation_id}/review", dependencies=[auth])
        @endpoint
        async def self_check_review(conversation_id: str, body: SelfCheckReview):
            return await self_check.submit(conversation_id, body.items, body.summary)

    return router
