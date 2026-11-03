"""Project capability write endpoints (Phase 5 first step / AD-144).

Assign, block or release one capability row of a Project and answer with
the effective entry computed by the same Resolver the read endpoints use.
"""

from __future__ import annotations

from typing import Any

from app.api.views import (
    effective_capability_entry_to_wire,
    normalize_backend_key_id,
    normalize_project_id,
)
from app.capabilities.models import (
    GENERIC_CAPABILITY_TYPES,
    ProjectCapability,
    parse_capability_type,
)
from app.capabilities.effective import resolve_effective_for_project
from app.errors import CapabilityTypeError
from app.api.session_routes.common import PutCapabilityBody, RouteSpec, SessionApiError, _not_found
from app.api.session_routes.context import SessionRouteContext


def build_capability_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Capability assignment endpoints."""
    from fastapi import Body

    repositories = ctx.repositories

    async def _known_capability_type(capability_type: str) -> str:
        """校验 ``type`` 落在**已知**类型集合内，并回归一化后的写法。

        两档，对应 R-01 的两种形态：

        - 通用能力：必须在 :data:`GENERIC_CAPABILITY_TYPES` 里。它不是「随便什么
          标识符都行」的开放集合——写端点是**产生**赋值行的地方，把打错的
          ``skils`` 收下来，只会在 Resolver 输出里长出一条谁也认不出的能力；
        - backend-scoped（``<backend-key>:<name>``）：backend 必须是这台机器上
          **有记录**的那些。公共层不硬编码任何 backend 名字（N §3），所以「已知」
          的定义就是领域库里的 ``backends`` 行。
        """
        try:
            ref = parse_capability_type(capability_type)
        except CapabilityTypeError as exc:
            raise SessionApiError(
                400, "unknown_capability_type", str(exc)
            ) from exc
        if ref.backend_key is None:
            if ref.name not in GENERIC_CAPABILITY_TYPES:
                raise SessionApiError(
                    400,
                    "unknown_capability_type",
                    f"未知的通用能力类型：{ref.name!r}",
                    detail={"known": sorted(GENERIC_CAPABILITY_TYPES)},
                )
            return ref.name
        backend_id = normalize_backend_key_id(ref.backend_key)
        if await repositories.backends.get(backend_id) is None:
            raise SessionApiError(
                400,
                "unknown_capability_type",
                f"能力类型里的 backend 不存在：{ref.backend_key!r}",
                detail={"backendId": backend_id},
            )
        return str(ref)

    async def _effective_entry(
        project_id: str, capability_type: str, capability_id: str
    ) -> Any:
        """写完之后这条能力**最终**是什么——走与读端点同一个 Resolver。

        写端点回「写进去的那一行」是不够的：AD-45 的禁止对子树生效、祖先的赋值
        是否被这次覆盖、这次删除之后回到哪一层继承——这些只有把整条祖先链算一遍
        才知道。算两遍不同的账迟早会给出两个答案，所以这里**不另写**一套。
        """
        ancestry = [p.id for p in await repositories.projects.ancestry(project_id)]
        effective = await resolve_effective_for_project(repositories, project_id)
        return {
            "projectId": project_id,
            "capabilityType": capability_type,
            "capabilityId": capability_id,
            "ancestry": ancestry,
            # 没有这一条时是 null：既没赋值也没继承来，是一个如实的「没有」。
            "entry": effective_capability_entry_to_wire(
                effective,
                capability_type=capability_type,
                capability_id=capability_id,
            ),
        }

    async def _load_project(project_id: str) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise _not_found("project_not_found", f"未知 Project：{project_id}")
        return project

    async def _existing_assignment(
        project_id: str, capability_type: str, capability_id: str
    ) -> Any:
        for assignment in await repositories.capabilities.list_for_project(project_id):
            if (
                assignment.capability_type == capability_type
                and assignment.capability_id == capability_id
            ):
                return assignment
        return None

    @ctx.endpoint
    async def put_capability(
        project_id: str,
        capability_type: str,
        capability_id: str,
        body: PutCapabilityBody = Body(...),
    ) -> Any:
        """``PUT /projects/{id}/capabilities/{type}/{capId}``：赋值 / 禁止 / 解除。

        一个端点三件事，因为它们写的是**同一行**（v1.0 §5.2：同一
        ``(project, type, capabilityId)`` 只应有一条赋值）：

        - ``blocked=true`` → 写一条 ``assignment_mode="block"`` 的行。AD-45：
          禁止对**子树**生效，所以它不是「本节点不显示」而是「从这里往下都没有」；
          block 行不带 config（领域模型直接拒绝），因此同时给 ``value`` 是矛盾的
          请求，回 400 而不是悄悄丢掉其中一半；
        - ``blocked=false`` → 解除禁止。这里只删**本项目层**那条 block 行；解除
          之后这条能力回到继承（可能仍被更上面的祖先挡着——那是另一个节点的事，
          不该由这个端点越过它去改）；
        - 给了 ``value`` → 写本项目层的赋值（``assignment_mode="local"``）。

        Projector 不物化（AD-07）：这里只改领域库那一行，不去动任何 Backend 的
        原生配置——落盘是 Phase 5 后续的事。
        """
        project = await _load_project(project_id)
        normalized_type = await _known_capability_type(capability_type)
        if not capability_id.strip():
            raise SessionApiError(
                400, "invalid_capability_id", "capabilityId 不能为空"
            )
        if body.blocked is None and body.value is None:
            raise SessionApiError(
                400,
                "empty_capability_patch",
                "请求里没有任何要写的项（value / blocked 至少给一个）",
            )
        if body.blocked and body.value is not None:
            raise SessionApiError(
                400,
                "blocked_with_value",
                "禁止（blocked=true）与赋值（value）是互斥的：block 行不携带配置"
                "（v1.0 §5.2）。要改值就先解除禁止。",
            )
        existing = await _existing_assignment(
            project.id, normalized_type, capability_id
        )
        if body.blocked is False:
            # 解除：本项目层有 block 行就删掉它；本来就没有就是无操作，
            # 不报错——「解除一个没有的禁止」的结果与「解除成功」一模一样。
            if existing is not None and existing.assignment_mode == "block":
                await repositories.capabilities.delete(existing.id)
            return await _effective_entry(project.id, normalized_type, capability_id)

        mode = "block" if body.blocked else "local"
        config = dict(body.value or {}) if mode == "local" else {}
        if existing is not None:
            await repositories.capabilities.delete(existing.id)
        await repositories.capabilities.save(
            ProjectCapability.create(
                project_id=project.id,
                capability_type=normalized_type,
                capability_id=capability_id,
                assignment_mode=mode,  # type: ignore[arg-type]
                config=config,
            )
        )
        return await _effective_entry(project.id, normalized_type, capability_id)

    @ctx.endpoint
    async def delete_capability(
        project_id: str, capability_type: str, capability_id: str
    ) -> Any:
        """删掉**本项目层**的那条赋值 → 这条能力回到继承。

        与 ``blocked=false`` 的区别只在删的是哪一种行：这里删的是任意一条本地行
        （local 或 block），语义是「这个节点不再对它有意见」。祖先写的东西一行
        不动——那是别人的节点。
        """
        project = await _load_project(project_id)
        normalized_type = await _known_capability_type(capability_type)
        existing = await _existing_assignment(
            project.id, normalized_type, capability_id
        )
        if existing is not None:
            await repositories.capabilities.delete(existing.id)
        payload = await _effective_entry(project.id, normalized_type, capability_id)
        payload["deleted"] = existing is not None
        return payload

    @ctx.endpoint
    async def capabilities_meta(project_id: str) -> Any:
        """``GET /projects/{id}/capabilities/_meta``（批次二十第 2 件）。

        前端要回答的问题只有一个：**这个后端认不认能力写端点**（AD-144 之前的版本
        不认）。此前它只能拿 ``PUT`` 一条不存在的能力去探，而 405 与 404 在真机上
        既可能是「没这条路由」，也可能是 CORS 中间件先答了——探不准，还留下一次
        写请求。这里给一条只读的自述：路由挂上了，``writable`` 就是 true。

        ``_meta`` 与 ``{capability_type}`` 走的是同一层路径，因此这条路由**必须**
        排在参数化路由之前注册（下面的注册顺序已经保证）。能力类型不允许以下划线
        开头，不会撞名。
        """
        project = await _load_project(project_id)
        return {
            "projectId": project.id,
            "writable": True,
            "methods": ["PUT", "DELETE"],
            "path": "/api/projects/{project_id}/capabilities/{capability_type}/{capability_id}",
        }

    return [
        RouteSpec('/projects/{project_id}/capabilities/_meta', capabilities_meta, ["GET"], 'session_capabilities_meta'),
        RouteSpec('/projects/{project_id}/capabilities/{capability_type}/{capability_id}', put_capability, ["PUT"], 'session_put_capability'),
        RouteSpec('/projects/{project_id}/capabilities/{capability_type}/{capability_id}', delete_capability, ["DELETE"], 'session_delete_capability'),
    ]


__all__ = ["build_capability_routes"]
