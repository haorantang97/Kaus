"""Project 记忆的接口（设计稿第 5 节）。

项目页「记忆」卡片与记忆 MCP 共用这一组端点。写入经 ``actor`` 记录来源（Agent 或
``user``）；上级 Project 的条目在下级只读（``memory_inherited``）。

``POST /projects/{id}/memories/write`` 是给 Agent 的一步式写入：``action=add`` 时先查相似
条目，有相似且没带 ``confirm`` 就只返回它们，由 Agent 再选新增 / 补充 / 使失效 / 不变；
每次都返回索引长度与上限，超出时要求顺带合并或作废旧条目。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.api.api_errors import ApiError, json_error_endpoint
from app.projects.memory import (
    INDEX_CHAR_LIMIT, INDEX_LINE_LIMIT, MemoryError_, OVERVIEW, ProjectMemory, over_limit, rank,
)

_STATUS = {"project_not_found": 404, "memory_not_found": 404, "history_not_found": 404,
           "memory_inherited": 403, "invalid_memory": 400}


def _text(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) else None


def build_project_memory_router(repositories: Any, *, auth_policy: Any, prefix: str = "/api") -> Any:
    from fastapi import APIRouter, Body, Depends, Query
    from fastapi.responses import JSONResponse

    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix=prefix, tags=["projects"], route_class=auth_route_class())
    _auth = Depends(require_session_auth(auth_policy))
    _endpoint = json_error_endpoint(JSONResponse)
    memory = ProjectMemory(repositories)

    async def _guard(call):
        try:
            return await call
        except MemoryError_ as exc:
            raise ApiError(_STATUS.get(exc.code, 400), exc.code, str(exc)) from exc

    async def _index_state(project_id: str) -> dict:
        lines, _overview = await memory.index(project_id)
        return {"indexLines": len(lines), "indexChars": sum(len(line) + 1 for line in lines),
                "lineLimit": INDEX_LINE_LIMIT, "charLimit": INDEX_CHAR_LIMIT, "overLimit": over_limit(lines)}

    @_endpoint
    async def list_memories(project_id: str, include_inactive: bool = Query(False, alias="includeInactive")) -> Any:
        entries = await _guard(memory.visible(project_id, include_inactive=include_inactive))
        return {"projectId": project_id, "memories": entries, **await _index_state(project_id)}

    @_endpoint
    async def search(project_id: str, q: str = Query(..., min_length=1, max_length=500),
                     limit: int = Query(8, ge=1, le=30)) -> Any:
        return {"memories": await _guard(memory.search(project_id, q, limit))}

    @_endpoint
    async def history(project_id: str, limit: int = Query(50, ge=1, le=200)) -> Any:
        return {"history": await _guard(memory.history(project_id, limit))}

    @_endpoint
    async def create(project_id: str, body: dict[str, Any] = Body(...)) -> Any:
        kind = OVERVIEW if body.get("kind") == OVERVIEW else "note"
        return await _guard(memory.add(project_id, _text(body, "topic") or "", _text(body, "body") or "",
                                       kind=kind, actor=_text(body, "actor") or "user"))

    @_endpoint
    async def patch(project_id: str, memory_id: str, body: dict[str, Any] = Body(...)) -> Any:
        return await _guard(memory.update(project_id, memory_id, topic=_text(body, "topic"), body=_text(body, "body"),
                                          actor=_text(body, "actor") or "user"))

    @_endpoint
    async def remove(project_id: str, memory_id: str) -> Any:
        return await _guard(memory.delete(project_id, memory_id, actor="user"))

    @_endpoint
    async def undo(project_id: str, history_id: str) -> Any:
        return await _guard(memory.undo(project_id, history_id, actor="user"))

    @_endpoint
    async def write(project_id: str, body: dict[str, Any] = Body(...)) -> Any:
        """Agent 的写入：add / update / invalidate / keep。"""
        action = body.get("action")
        actor = _text(body, "actor")
        if action == "keep":
            return {"action": "keep", **await _index_state(project_id)}
        if action == "add":
            topic, text = _text(body, "topic") or "", _text(body, "body") or ""
            kind = OVERVIEW if body.get("kind") == OVERVIEW else "note"
            if kind != OVERVIEW and not body.get("confirm"):
                similar = rank(await _guard(memory.visible(project_id)), f"{topic} {text}", 5)
                if similar:
                    return {"action": "choose", "similar": similar, **await _index_state(project_id),
                            "next": "有相似条目：补充已有条目用 action=update（带 memory_id），旧内容过时用 action=invalidate，"
                                    "确实是新内容就带 confirm=true 再写一次，没有新东西用 action=keep。"}
            saved = await _guard(memory.add(project_id, topic, text, kind=kind, actor=actor))
        elif action == "update":
            saved = await _guard(memory.update(project_id, _text(body, "memory_id") or "", topic=_text(body, "topic"),
                                               body=_text(body, "body"), actor=actor))
        elif action == "invalidate":
            saved = await _guard(memory.invalidate(project_id, _text(body, "memory_id") or "",
                                                   replaced_by=_text(body, "replaced_by"), actor=actor))
        else:
            raise ApiError(400, "invalid_memory", "action 只能是 add / update / invalidate / keep")
        state = await _index_state(project_id)
        result = {"action": action, "memory": saved, **state}
        if state["overLimit"]:
            result["next"] = "索引已超出上限：请在这次写入里顺带合并相近条目或作废过时条目，把索引压回上限内。"
        return result

    for path, endpoint, methods, name in (
        ("/projects/{project_id}/memories", list_memories, ["GET"], "memory_list"),
        ("/projects/{project_id}/memories", create, ["POST"], "memory_create"),
        ("/projects/{project_id}/memories/search", search, ["GET"], "memory_search"),
        ("/projects/{project_id}/memories/history", history, ["GET"], "memory_history"),
        ("/projects/{project_id}/memories/write", write, ["POST"], "memory_write"),
        ("/projects/{project_id}/memories/history/{history_id}/undo", undo, ["POST"], "memory_undo"),
        ("/projects/{project_id}/memories/{memory_id}", patch, ["PATCH"], "memory_patch"),
        ("/projects/{project_id}/memories/{memory_id}", remove, ["DELETE"], "memory_delete"),
    ):
        router.add_api_route(path, endpoint, methods=methods, name=name, dependencies=[_auth])
    return router


__all__ = ["build_project_memory_router"]
