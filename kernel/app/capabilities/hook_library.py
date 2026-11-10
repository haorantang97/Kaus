"""能力库里的钩子（2026-11-11，设计稿 ``docs/product/hooks-design-20261111.md``）。

一个文件一个钩子：``<capability-library>/hooks/<id>.json``，只存定义（名称、时点、匹配、命令、
超时）。目录类型 ``hooks``、``kind: "hook"``，和技能、MCP、插件一样在技能树上按 Project 分配；
开对话时由送达层（``drivers.launch_delivery``）按各 Agent 的格式送出。

新建不做专门表单（chat-first）：接口 ``POST /api/capability-library/hooks`` 接受定义。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from app.api.api_errors import ApiError
from drivers.hooks import HookError, normalize

HOOK_CAPABILITY_TYPE = "hooks"
HOOK_KIND = "hook"
_ID = re.compile(r"^hook-[0-9a-f]{12}$")


class HookLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.skill_tree: Any = None

    @property
    def folder(self) -> Path:
        return self.root / "hooks"

    def records(self) -> list[dict]:
        if not self.folder.is_dir():
            return []
        rows = []
        for path in sorted(self.folder.glob("hook-*.json")):
            if path.is_symlink() or not _ID.match(path.stem):
                continue
            try:
                rows.append({**normalize(json.loads(path.read_text(encoding="utf-8"))), "id": path.stem})
            except (OSError, ValueError, HookError):
                continue
        return rows

    def add(self, definition: Mapping[str, Any]) -> dict:
        try:
            hook = normalize(definition)
        except HookError as exc:
            raise ApiError(400, "hook_invalid", str(exc)) from exc
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        hook_id = "hook-" + uuid.uuid4().hex[:12]
        path = self.folder / f"{hook_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(hook, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        self._refresh()
        return {**hook, "id": hook_id}

    def remove(self, hook_id: str) -> dict:
        path = self.folder / f"{hook_id}.json"
        if not _ID.match(hook_id) or not path.is_file():
            raise ApiError(404, "hook_not_found", "钩子不存在")
        path.unlink()
        if self.skill_tree is not None:
            self.skill_tree.forget(HOOK_CAPABILITY_TYPE, hook_id)
        return {"removed": hook_id}

    def discover(self) -> list[dict]:
        """技能树目录记录（与 ``drivers.skill_inventory.discover_skills`` 同形）。"""
        return [{"id": row["id"], "kind": HOOK_KIND, "name": row["name"], "description": row.get("description") or "",
                 "agentKey": None, "agentLabel": "", "available": True,
                 "capabilityType": HOOK_CAPABILITY_TYPE, "capabilityId": row["id"],
                 **{key: row[key] for key in ("event", "agent", "matcher", "command", "timeout") if key in row}}
                for row in self.records()]

    def _refresh(self) -> None:
        if self.skill_tree is not None:
            self.skill_tree.snapshot()


def build_hook_library_router(library: HookLibrary, *, auth_policy: Any):
    import asyncio

    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse

    from app.api.api_errors import json_error_endpoint
    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix="/api", tags=["capability-library"], route_class=auth_route_class())
    auth = Depends(require_session_auth(auth_policy))
    endpoint = json_error_endpoint(JSONResponse)

    @router.get("/capability-library/hooks", dependencies=[auth])
    @endpoint
    async def list_hooks():
        return {"hooks": await asyncio.to_thread(library.records)}

    @router.post("/capability-library/hooks", dependencies=[auth])
    @endpoint
    async def add_hook(body: dict[str, Any] = Body(...)):
        return await asyncio.to_thread(library.add, body)

    @router.delete("/capability-library/hooks/{hook_id}", dependencies=[auth])
    @endpoint
    async def remove_hook(hook_id: str):
        return await asyncio.to_thread(library.remove, hook_id)

    return router


__all__ = ["HOOK_CAPABILITY_TYPE", "HOOK_KIND", "HookLibrary", "build_hook_library_router"]
