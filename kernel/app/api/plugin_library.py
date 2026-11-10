"""能力库插件接口（2026-11-09，设计稿第 6 / 7 节）。鉴权与其他会话接口相同。

``GET    /api/library/plugins``          已安装插件 ``{plugins: [...]}``
``POST   /api/library/plugins/inspect``  ``{source}`` → ``{kind: "plugin", plugin}`` 或 ``{kind: "marketplace", name, plugins}``
``POST   /api/library/plugins``          ``{source, plugin?}`` → 201 安装记录
``DELETE /api/library/plugins/{id}``     卸载：目录项、能力行、文件一起删

错误码（``{"error": {"code", "message"}}``）：``source_required`` / ``source_unsupported`` /
``source_not_found`` / ``not_a_plugin`` / ``invalid_manifest`` / ``clone_failed`` / ``too_large`` /
``symlink_not_allowed`` / ``plugin_required`` / ``plugin_not_in_marketplace`` /
``plugin_already_installed`` / ``plugin_not_found`` / ``install_failed``。
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.api_errors import json_error_endpoint
from app.capabilities.plugin_library import PluginLibrary


class InspectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=2000)


class InstallBody(InspectBody):
    plugin: str | None = Field(default=None, max_length=200)


def build_plugin_library_router(library: PluginLibrary, *, auth_policy: Any, prefix: str = "/api") -> Any:
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse
    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix=prefix, tags=["library"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @router.get("/library/plugins", dependencies=[auth])
    @endpoint
    async def list_plugins():
        return await asyncio.to_thread(library.list)

    @router.post("/library/plugins/inspect", dependencies=[auth])
    @endpoint
    async def inspect_source(body: InspectBody):
        return await asyncio.to_thread(library.inspect, body.source)

    @router.post("/library/plugins", dependencies=[auth], status_code=201)
    @endpoint
    async def install_plugin(body: InstallBody):
        record = await asyncio.to_thread(library.install, body.source, body.plugin)
        return JSONResponse(record, status_code=201)

    @router.delete("/library/plugins/{plugin_id}", dependencies=[auth])
    @endpoint
    async def uninstall_plugin(plugin_id: str):
        return await asyncio.to_thread(library.uninstall, plugin_id)

    return router


__all__ = ["build_plugin_library_router"]
