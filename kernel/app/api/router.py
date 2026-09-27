"""只读领域端点的路由装配（v1.0 §12.1 的子集）。

提供的端点
----------
====================================  =========================================
``GET /api/projects``                 全部 Project（按 slug 排序）+ 根列表
``GET /api/projects/{project_id}``    单个 Project（可传 ``project:<slug>`` 或裸 slug）
``GET /api/projects/{id}/bindings``   该 Project 的全部 AgentBinding（R-09：可能多条）
                                      + 每行的 ``auth`` / ``nativeSessionCount``（批次十九）
``GET /api/projects/{id}/capabilities``            该 Project 的**本地**能力赋值行
``GET /api/projects/{id}/effective-capabilities``  Resolver 算出的**有效**能力
``GET /api/backends``                 Backend Registry 现状
``GET /api/backends/{backend_id}``    单个 Backend 的能力声明（ui/detail 两层 + unknownCount）
``GET /api/bindings/{binding_id}``    单条 AgentBinding
``GET /api/_domain/diff``             Phase 1 对账：旧 JSON 状态 ↔ 领域库
====================================  =========================================

错误形状
--------
和会话端点**逐字一致**：``{"error": {"code", "message"}}``，code 是稳定的机器
可读串（``project_not_found`` / ``backend_not_found`` / ``binding_not_found`` /
``reconciliation_unavailable``），message 给人看。批次八第 5 件之前这里回的是
web 框架默认的 ``{"detail": "..."}``，同一个宿主上因此有两种错误体，前端不得不
写两条解析分支。定义在 :mod:`app.api.api_errors`，与 session_router 共用一份。

两个能力端点的分工（R-01 / AD-42）
----------------------------------
- `/capabilities` 是**这一个节点上写了什么**（local / block 行，v1.0 §11.1 的原始行）；
- `/effective-capabilities` 是**这个节点最终得到什么**：把「根 → … → 自己」整条
  祖先链喂给 Capability Resolver，输出每条能力的来源 Project、是否继承而来、
  是否被链上更近的节点覆盖过，以及被阻断的条目和阻断者。
  `?backend=<key>` 只做过滤视图（通用能力 + 该 backend 的 scoped 能力），
  计算规则完全相同——这就是 R-01「一套继承引擎」的直接体现。

为什么没有写端点
----------------
Phase 1 的目标是「换地基，界面不动」：领域库此刻只有一个写入者——一次性迁移器。
写端点属于 Phase 2（UI 切到新 API）与 Phase 5（Capability Registry 成为规范源），
在那之前多开一个写入口只会制造第二本账。

为什么 web 框架是**函数内**导入
--------------------------------
``app/`` 是公共领域层，它的每个子模块都会被公共层纯净性扫描 import；领域层的
依赖只有 pydantic。把框架的 import 放进 :func:`build_domain_router`，模块本身
就仍然只依赖标准库与 pydantic，装不装框架都能被扫描。
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.backend_facets import backend_facets
from app.api.binding_status import status_provider
from app.api.views import (
    backend_to_wire,
    binding_to_wire,
    build_domain_diff,
    capability_to_wire,
    capability_value_digest,
    effective_capabilities_to_wire,
    normalize_backend_key_id,
    normalize_project_id,
    project_to_wire,
)
from app.capabilities.resolver import CapabilityResolutionRequest, TreeCapabilityResolver
from app.persistence.base import RepositorySet

#: 对账端点的期望侧数据源：返回 ``{"projects": [...], "bindings": [...]}``。
#: 可以是同步或异步可调用对象；返回 ``None`` 表示「本次运行没有可用的期望侧」，
#: 端点会以 503 明确报告，而不是假装 in-sync。
ExpectedSnapshotProvider = Callable[
    [], "Mapping[str, Sequence[Mapping[str, Any]]] | None | Awaitable[Mapping[str, Sequence[Mapping[str, Any]]] | None]"
]


class _RouterLike(Protocol):  # pragma: no cover - 仅为类型可读性
    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None: ...


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def build_domain_router(
    repositories: RepositorySet,
    *,
    expected_snapshot: ExpectedSnapshotProvider | None = None,
    prefix: str = "/api",
    tag: str = "domain",
) -> Any:
    """装配只读路由。返回一个框架的 ``APIRouter``，由调用方 include。"""
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix=prefix, tags=[tag])
    resolver = TreeCapabilityResolver()
    # 每个处理器就地包一层，把 ApiError 变成 {"error": {...}}——不要求宿主装
    # app 级 exception handler（`server.py` 只允许多一个 attach 调用）。
    _endpoint = json_error_endpoint(JSONResponse)

    @_endpoint
    async def list_projects() -> dict[str, Any]:
        projects = list(await repositories.projects.list_all())
        return {
            "projects": [project_to_wire(p) for p in projects],
            "roots": [p.id for p in projects if p.is_root],
            "count": len(projects),
        }

    @_endpoint
    async def get_project(project_id: str) -> dict[str, Any]:
        project = await repositories.projects.get(normalize_project_id(project_id))
        if project is None:
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        children = await repositories.projects.list_children(project.id)
        payload = project_to_wire(project)
        payload["childProjectIds"] = [c.id for c in children]
        return payload

    @_endpoint
    async def list_project_bindings(project_id: str) -> dict[str, Any]:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        bindings = [b for b in await repositories.bindings.list_for_project(normalized) if not b.runtime_config.get("group_execution")]
        rows = [binding_to_wire(b) for b in bindings]
        # 批次十九第 3 件：每行并上 `auth` 与 `nativeSessionCount`（AD-82 / AD-93）。
        # 取数面由会话运行时在装配时登记（它才有 Driver Registry）；没登记时这两个
        # 键**整个不出现**，前端按缺字段静默不渲染那两行（AD-71），而不是收到 null
        # 再去猜是「没有」还是「没读到」。
        provider = status_provider()
        if provider is not None:
            for binding, row in zip(bindings, rows):
                try:
                    extra = await provider(binding)
                except Exception:  # noqa: BLE001 - 一条读不出来不该拖垮整页
                    extra = None
                if extra:
                    row.update(
                        {k: v for k, v in extra.items() if k != "bindingId"}
                    )
        return {
            "projectId": normalized,
            # R-09：同一 Project 可挂多条同 Backend 的 Binding，因此这里永远是列表。
            "bindings": rows,
            "count": len(bindings),
        }

    @_endpoint
    async def list_project_capabilities(project_id: str) -> dict[str, Any]:
        """这个 Project **自己**写了哪些能力赋值（local / block），不含继承。"""
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        assignments = list(await repositories.capabilities.list_for_project(normalized))
        return {
            "projectId": normalized,
            "assignments": [capability_to_wire(a) for a in assignments],
            "counts": {
                "total": len(assignments),
                "local": sum(1 for a in assignments if a.assignment_mode != "block"),
                "block": sum(1 for a in assignments if a.assignment_mode == "block"),
            },
        }

    @_endpoint
    async def get_effective_capabilities(
        project_id: str, backend: str | None = None
    ) -> dict[str, Any]:
        """R-01 树计算层：把整条祖先链喂给 Resolver，返回有效能力 + 来源。"""
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        ancestry = [p.id for p in await repositories.projects.ancestry(normalized)]
        assignments = await repositories.capabilities.list_for_projects(ancestry)
        effective = resolver.resolve(
            CapabilityResolutionRequest(
                ancestry=tuple(ancestry), backend_key=backend or None
            ),
            assignments,
        )
        return effective_capabilities_to_wire(effective, ancestry=ancestry)

    def _backend_row(backend: Any) -> dict[str, Any]:
        """Backend → wire，外加装配态的只读补充（批次二十五第 5 件）。

        ``preset`` / ``quirks`` 来自 Driver Registry，不来自领域库——理由见
        :mod:`app.api.backend_facets`。取数面没登记时这两个键整个不出现
        （AD-71 缺字段静默不渲染），不是 null。
        """
        payload = backend_to_wire(backend)
        payload.update(backend_facets(backend.id))
        return payload

    @_endpoint
    async def list_backends() -> dict[str, Any]:
        backends = list(await repositories.backends.list_all())
        return {
            "backends": [_backend_row(b) for b in backends],
            "count": len(backends),
        }

    @_endpoint
    async def get_backend(backend_id: str) -> dict[str, Any]:
        """v1.0 §12.3 能力协商：单个 Backend 的能力声明（AD-71 两层 + unknownCount）。

        路径参数同时接受 ``backend:<key>`` 与裸 key，和 Project 端点的口径一致。
        """
        normalized = normalize_backend_key_id(backend_id)
        backend = await repositories.backends.get(normalized)
        if backend is None:
            raise ApiError(404, "backend_not_found", f"未知 Backend：{backend_id}")
        return _backend_row(backend)

    @_endpoint
    async def get_binding(binding_id: str) -> dict[str, Any]:
        binding = await repositories.bindings.get(binding_id)
        if binding is None:
            raise ApiError(404, "binding_not_found", f"未知 Binding：{binding_id}")
        return binding_to_wire(binding)

    @_endpoint
    async def domain_diff() -> dict[str, Any]:
        if expected_snapshot is None:
            raise ApiError(
                503,
                "reconciliation_unavailable",
                "本次运行没有配置对账数据源，无法给出差异（拒绝以空差异冒充一致）。",
            )
        snapshot = await _maybe_await(expected_snapshot())
        if snapshot is None:
            raise ApiError(
                503,
                "reconciliation_unavailable",
                "对账数据源本次未能给出期望快照（拒绝以空差异冒充一致）。",
            )
        # 能力维度是可选的：期望侧给了 `capabilities` 才对账（AD-42）。
        # 给不了时**不**假装 in-sync——`capabilities` 整段不出现，读者一眼看得出没对过。
        expected_capabilities = snapshot.get("capabilities")
        actual_capabilities = None
        if expected_capabilities is not None:
            actual_capabilities = await effective_capability_rows(repositories)

        diff = build_domain_diff(
            expected_projects=list(snapshot.get("projects", ())),
            expected_bindings=list(snapshot.get("bindings", ())),
            actual_projects=list(await repositories.projects.list_all()),
            actual_bindings=list(await _all_bindings(repositories)),
            expected_capabilities=(
                list(expected_capabilities) if expected_capabilities is not None else None
            ),
            actual_capabilities=actual_capabilities,
            capability_notes=snapshot.get("capabilityNotes"),
        )
        payload = diff.to_wire()
        warnings = snapshot.get("warnings")
        if warnings is not None:
            payload["sourceWarnings"] = list(warnings)
        return payload

    for path, endpoint, name in (
        ("/projects", list_projects, "domain_list_projects"),
        ("/projects/{project_id}", get_project, "domain_get_project"),
        ("/projects/{project_id}/bindings", list_project_bindings, "domain_list_project_bindings"),
        (
            "/projects/{project_id}/capabilities",
            list_project_capabilities,
            "domain_list_project_capabilities",
        ),
        (
            "/projects/{project_id}/effective-capabilities",
            get_effective_capabilities,
            "domain_get_effective_capabilities",
        ),
        ("/backends", list_backends, "domain_list_backends"),
        ("/backends/{backend_id}", get_backend, "domain_get_backend"),
        ("/bindings/{binding_id}", get_binding, "domain_get_binding"),
        ("/_domain/diff", domain_diff, "domain_diff"),
    ):
        router.add_api_route(path, endpoint, methods=["GET"], name=name)
    return router


def capability_row_id(project_id: str, capability_type: str, capability_id: str) -> str:
    """对账行的自然键。两侧（接入层的期望侧、这里的实际侧）共用这一个构造函数。"""
    return f"{project_id}|{capability_type}|{capability_id}"


async def effective_capability_rows(
    repositories: RepositorySet, *, backend_key: str | None = None
) -> list[dict[str, Any]]:
    """全库每个 Project 的**有效**能力，摊平成对账行（带值摘要，不带值）。

    这是对账的「实际侧」：走的是和 `/effective-capabilities` 端点**同一个** Resolver，
    所以对账验证的正是端点会返回的东西，而不是另一条平行算法。
    """
    resolver = TreeCapabilityResolver()
    rows: list[dict[str, Any]] = []
    for project in await repositories.projects.list_all():
        ancestry = [p.id for p in await repositories.projects.ancestry(project.id)]
        assignments = await repositories.capabilities.list_for_projects(ancestry)
        effective = resolver.resolve(
            CapabilityResolutionRequest(ancestry=tuple(ancestry), backend_key=backend_key),
            assignments,
        )
        for entry in effective.entries:
            rows.append(
                {
                    "id": capability_row_id(
                        project.id, entry.capability_type, entry.capability_id
                    ),
                    "project_id": project.id,
                    "capability_type": entry.capability_type,
                    "capability_id": entry.capability_id,
                    # 摘要取整份 config：公共层不认识 config 里的键名约定
                    # （那是接入层的事），只负责「一样/不一样」。
                    "digest": capability_value_digest(entry.config),
                    "source_project_id": entry.source_project_id,
                }
            )
    return rows


async def _all_bindings(repositories: RepositorySet) -> list[Any]:
    """遍历全部 Project 收集 Binding。

    Repository 契约刻意没有 ``list_all_bindings``（R-09 下「全库 Binding」不是
    任何领域用例的输入），对账是唯一需要全集的地方，因此在这里按 Project 汇总，
    而不是给 Repository 加一个只有对账用得上的方法。
    """
    collected: list[Any] = []
    for project in await repositories.projects.list_all():
        collected.extend(await repositories.bindings.list_for_project(project.id))
    return collected


__all__ = [
    "ExpectedSnapshotProvider",
    "build_domain_router",
    "capability_row_id",
    "effective_capability_rows",
]
