from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["conversation", "archive"]
    id: str = Field(max_length=256)
    projectId: str = Field(max_length=256)


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Source
    bindingId: str = Field(max_length=256)


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=32)
    context: str = Field(max_length=48000)
    assetIds: list[str] = Field(default_factory=list, max_length=8)


def build_continuation_router(service, *, auth_policy):
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse
    from app.api.api_errors import json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth
    router = APIRouter(prefix="/api/continuations", route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @router.post("/preview", dependencies=[auth])
    @endpoint
    async def preview(body: Preview):
        return await service.preview(body.source.model_dump(), body.bindingId)

    @router.post("", dependencies=[auth])
    @endpoint
    async def create(body: Create):
        return await service.create(body.token, body.context, body.assetIds)

    @router.get("/{conversation_id}", dependencies=[auth])
    @endpoint
    async def detail(conversation_id: str):
        return {"continuation": await service.detail(conversation_id)}
    return router
