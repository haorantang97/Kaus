"""AgentBinding 的写端点（批次八第 2 件）。

提供的端点（前缀 ``/api``）
---------------------------
==================================================  ==================================
``PATCH  /bindings/{binding_id}``                   改 ``defaultModelId`` / ``runtimeConfig``
``POST   /bindings/{binding_id}/make-default``      把它设成所属 Project 的默认 Binding
``POST   /projects/{project_id}/bindings``          新建一条**非默认** Binding
``DELETE /bindings/{binding_id}``                   删一条 Binding（只删领域库的行）
``PATCH  /projects/{project_id}``                   只改 ``workspaceRoot``（批次十三第 4 件）
``GET    /bindings/{binding_id}/status``            登录态 + 原生会话数 + 探测态（批次十九）
``POST   /bindings/{binding_id}/materialize``       把有效能力物化进引擎原生配置（批次二十四）
``GET    /bindings/{binding_id}/drift``             原生配置四态对账（批次二十四）
``GET    /bindings/{binding_id}/projection/_meta``  这条 Binding 有没有投射面（批次二十六）
``GET    /bindings/{binding_id}/projections``       最近几次物化的账（批次二十六）
==================================================  ==================================

为什么一个 GET 也在这份「写端点」router 里
------------------------------------------
判据从来不是 HTTP 动词，是「要不要鉴权」（见下）。``/status`` 回答的是「这台引擎
认不认你手上这把凭据」——那是一个关于本机凭据配置的答案，只该给已鉴权的调用方。
放进只读 router 就等于把它挂在 AD-72 的免鉴权口径上。

为什么「改项目工作目录」也在这份 router 里
------------------------------------------
它是写端点，而这份 router 的判据从来不是「改的是哪张表」，是「要不要鉴权」
（见下面那段）。把唯一一个 Project 写端点放进只读 router，就正好踩中那段话
警告的事：一个 router 里两种准入口径。

为什么单独一个 router，而不是塞进 `app/api/router.py`
-----------------------------------------------------
那份 router 的开篇就写着「Phase 1 没有写端点：领域库此刻只有一个写入者——
一次性迁移器」。现在多了第二个写入者，那是一次**口径变更**，不该表现为在只读
router 里悄悄多几个 POST。分成两份还有一个实际好处：只读 router 不鉴权
（AD-72：Phase 1 只读端点暂不鉴权），而写端点必须鉴权——两种准入口径混在一个
router 里，早晚会有人漏挂一条。

四条口径
--------
1. **只改 Binding 行，不投影到引擎。** ``PATCH`` 写的是领域库里的
   ``default_model_id`` / ``runtime_config``，**不**调用任何 Driver 的投射接口
   ——把领域配置物化到原生引擎是 Phase 5 的 Capability Registry 的事
   （AD-07 / R-12）。这里多做一步，就等于在 Phase 5 之前先造出第二本账。
2. **新建的 Binding 带 ``origin="domain"``。** 它在旧状态里没有对应物，
   `/api/_domain/diff` 因此把它列进 ``bindings.domainOwned`` 而不是
   ``unexpectedInDb``，导入器也只报告、不删（见
   :data:`~app.projects.models.BindingOrigin`）。
3. **默认 Binding 不可删、活跃 Runtime 挡删除（409）。** 前者是因为
   「Project 至少要有一条默认 Binding」是新建会话的前提；后者是因为删掉一条
   正在跑的 Binding 会让那条 runtime 的事件无处归属。两者都是**显式冲突**，
   不是静默忽略（N §13.1）。
4. **C-4：删的只有 Dashboard 自己的行。** 原生 Session、原生配置一个都不碰。
5. **只有 ``/materialize?confirm=1`` 会改用户机器上的文件（批次二十四 / AD-149）。**
   这条 router 里其余每一个写端点动的都是领域库。物化默认 dry-run——不传
   ``confirm=1`` 就只算变更集、一个字节都不落盘；真写前该 Binding 名下有活跃
   会话时返回 409 ``binding_busy``（引擎正在读那份配置，边跑边改是两本账）。

错误形状与会话端点一致：``{"error": {"code", "message"}}``，见
:mod:`app.api.api_errors`。

为什么 web 框架是**函数内**导入：同 :mod:`app.api.router`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.binding_status import read_binding_status
from app.api.views import (
    binding_to_wire,
    normalize_backend_key_id,
    normalize_project_id,
    project_to_wire,
)
from app.capabilities.projection_records import ProjectionRecord
from app.errors import InvalidIdentifierError
from app.ids import binding_id as make_binding_id
from app.persistence.base import RepositorySet
from app.projects.models import AgentBinding
from drivers.base import APPROVAL_MODES

#: ``runtime_config`` 里允许通过 ``PATCH`` 写的**通用**键。
#:
#: 白名单而不是黑名单：``runtime_config`` 是 backend 私有配置的存放处
#: （N §5.2 路径 B），里面可能有 ``twin_mode`` 这类由导入器写、改了就对不上账的
#: 东西。公共写端点只认这几个「哪个引擎都有」的旋钮；引擎私有的旋钮属于
#: Phase 5 的 Capability Registry，不从这里进。
GENERIC_RUNTIME_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        # 推理强度（N §10.1 的 reasoning levels）。
        "reasoning_effort",
        # 危险命令审批档（批次十三第 3 件）。取值是 Kaus 的通用词
        # `ask | auto | deny`（AD-106），各引擎自己的取值由各自的 Driver 映射——
        # 它够格当通用键，是因为三家引擎都有「要不要为危险命令停下来问人」这一档，
        # 只是各自叫法不同。
        "approval_mode",
        # 输出长度上限与采样温度：三家引擎都有的通用旋钮。
        "max_output_tokens",
        "temperature",
        # 系统提示的追加片段（不是替换——替换属于引擎私有配置）。
        "system_prompt_suffix",
    }
)


def _projection_summary(result: Any) -> dict[str, Any]:
    """``projection_results.summary``：**只有键路径与计数，没有值**。

    ``before`` / ``after`` 虽然已经过 Driver 的脱敏器，但脱敏是「尽力而为」而记账
    是「长期留存」——两者的风险不对等（§5.4）。所以这张表只回答「上次动了哪几个
    键、有几条没写成、为什么」，要看值就现读现比（``GET /drift``）。
    """
    reasons: dict[str, int] = {}
    for entry in result.unsupported:
        key = entry.reason or "unknown"
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "dryRun": bool(result.dry_run),
        "backupPath": result.backup_path,
        "appliedCount": len(result.applied),
        "changedCount": len(result.changed),
        "unsupportedCount": len(result.unsupported),
        "unsupportedReasons": reasons,
        "keyPaths": sorted(
            {e.key_path for e in result.applied if e.key_path is not None}
        ),
        "warnings": list(result.warnings),
    }


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class PatchBindingBody(_Body):
    """``PATCH /bindings/{id}``。

    两个字段都是**可选**的，且「没给」与「显式给 null」含义不同：没给 = 不动，
    显式 ``null`` = 清空。pydantic 分不开这两者，所以这里用
    :meth:`provided` 查原始输入的键名。
    """

    default_model_id: str | None = None
    runtime_config: dict[str, Any] | None = None

    def provided(self, name: str) -> bool:
        return name in self.model_fields_set


class PatchProjectBody(_Body):
    """``PATCH /projects/{id}``：**只**接受 ``workspaceRoot``（批次十三第 4 件）。

    刻意不收别的字段。Project 上其余的东西（slug / 父子关系 / 状态）各有各的
    前置条件，混进一个「改项目」的通用端点里，只会得到一个谁也说不清能改什么的
    入口。``extra="forbid"`` 让多给一个字段直接 422，而不是被静默忽略。
    """

    workspace_root: str = Field(min_length=1)


class CreateBindingBody(_Body):
    """``POST /projects/{id}/bindings``：建一条**非默认** Binding。

    刻意不收 ``isDefault``：换默认是另一件事（``/make-default``），它要在同一个
    动作里把旧默认降级，混进创建接口只会得到一个「一半成功」的中间态。
    """

    backend_id: str = Field(min_length=1)
    discriminator: str | None = None
    display_name: str | None = None


def build_binding_write_router(
    repositories: RepositorySet,
    *,
    auth_policy: Any,
    session_host: Any = None,
    registry: Any = None,
    prefix: str = "/api",
    tag: str = "bindings",
) -> Any:
    """装配 Binding 写路由。返回一个框架的 ``APIRouter``，由调用方 include。

    ``auth_policy`` 是必填的（同 session_router 的理由：挂上路由与路由被鉴权
    必须是同一件事）。``session_host`` 可选——给了才能回答「这条 Binding 现在
    有没有活跃 Runtime」；没给时删除只挡默认 Binding，并在响应里说明这一点。

    ``registry`` 也是可选的：给了才能用 Model Catalog 校验 ``reasoning_effort``
    （批次十三第 3 件：合法值 = 目录里该模型的 ``reasoning_levels``）。不给、
    或目录取不到、或目录是空的，就**不校验**——公共层不认识任何一家引擎的强度
    词表，凭空写死一份只会拦住真实存在的取值。
    """
    from fastapi import APIRouter, Body, Depends
    from fastapi.responses import JSONResponse

    from app.api.session_auth import auth_route_class, require_session_auth

    router = APIRouter(prefix=prefix, tags=[tag], route_class=auth_route_class())
    _auth = Depends(require_session_auth(auth_policy))
    _endpoint = json_error_endpoint(JSONResponse)

    def _now() -> datetime:
        return datetime.now(tz=timezone.utc)

    async def _load(binding_id: str) -> AgentBinding:
        binding = await repositories.bindings.get(binding_id)
        if binding is None:
            raise ApiError(404, "binding_not_found", f"未知 Binding：{binding_id}")
        return binding

    async def _running_conversations(binding: AgentBinding) -> list[dict[str, str]]:
        """这条 Binding 名下**这一刻真的在跑**的会话（id + 标题）。

        批次二十七热修：判据从「有没有活跃 Runtime」（``is_active``）改成「这一轮
        跑没跑完」（``run_state_of``）。两者是**两件事**——一轮结束之后 Runtime
        照旧活着（要等空闲回收才收掉），于是真机上会话页已经显示 Idle、物化却一直
        409 ``binding_busy``：「1 conversations are running」说的是一条早就跑完的
        会话。而 ``binding_busy`` 想拦的是「引擎正在读那份配置」，那件事只跟有没有
        回合在跑有关，跟有没有一条闲着的 runtime 无关。

        口径与 Group 投递的 ``turn_already_running`` 逐字相同：**先收敛再问**
        （AD-136）。不收敛的话，一条丢了终态的会话会把这条 Binding 永久锁死——
        那比原来的误报更糟。收敛只对「手上真有 runtime」的那几条做，没有 runtime
        的会话 ``run_state_of`` 本来就是 idle，不必为它多跑一遍自愈。

        带标题是批次二十六第 5 件④：``binding_busy`` 此前只给 id，界面上只能显示
        一串 ``conversation:…``，用户根本不知道该去停哪一条。取标题不多一次查库
        ——会话对象本来就已经读出来了。
        """
        if session_host is None:
            return []
        conversations = await repositories.conversations.list_for_binding(binding.id)
        running: list[dict[str, str]] = []
        for conversation in conversations:
            if not session_host.is_active(conversation.id):
                continue
            await session_host.reconcile_conversation(conversation)
            if await session_host.run_state_of(conversation) != "idle":
                running.append(
                    {"conversationId": conversation.id, "title": conversation.title}
                )
        return running

    async def _active_runtime_ids(binding: AgentBinding) -> list[str]:
        """这条 Binding 名下**有活跃 Runtime**的会话 id。

        与 :func:`_running_conversations` 是两件事，不要合并：删 Binding 拦的是
        「有 Runtime 正挂在这条绑定上」（闲着的也算——删了它那条 runtime 就没有
        绑定可查了），物化拦的是「有回合正在读那份配置」。
        """
        if session_host is None:
            return []
        conversations = await repositories.conversations.list_for_binding(binding.id)
        return [c.id for c in conversations if session_host.is_active(c.id)]

    async def _reasoning_levels(binding: AgentBinding, model_id: str | None) -> tuple[str, ...]:
        """这条 Binding 上、这个模型的合法推理强度。取不到就是空元组（= 不校验）。

        取不到的情形一个都不当错误：没给 Registry、Backend 没注册、Driver 不支持
        目录、目录是空的（批次十三第 2 件之后这是常态）——都只意味着「这次没有
        可依据的词表」。
        """
        if registry is None:
            return ()
        try:
            driver = registry.get(binding.backend_id)
            catalog = await driver.get_model_catalog(binding)
        except Exception:  # noqa: BLE001 - 校验的依据取不到 ≠ 这次写入非法
            return ()
        target = model_id or catalog.default_model_id
        for model in catalog.models:
            if model.model_id == target:
                return tuple(model.reasoning_levels)
        return ()

    async def _validate_runtime_config(
        binding: AgentBinding, merged: dict[str, Any], *, model_id: str | None
    ) -> None:
        """通用键的**取值**校验（键名校验在调用处）。

        只校验两个有封闭取值集合的键。其余通用键（``temperature`` 之类）的取值
        范围因引擎而异，公共层不替它们定。
        """
        approval = merged.get("approval_mode")
        if approval is not None and approval not in APPROVAL_MODES:
            raise ApiError(
                400,
                "invalid_approval_mode",
                f"approval_mode 只接受 {list(APPROVAL_MODES)}，拿到 {approval!r}"
                "（各引擎自己的取值由各自的 Driver 映射，这里只认通用词）",
                allowedValues=list(APPROVAL_MODES),
            )
        effort = merged.get("reasoning_effort")
        if effort is None:
            return
        levels = await _reasoning_levels(binding, model_id)
        if levels and effort not in levels:
            raise ApiError(
                400,
                "invalid_reasoning_effort",
                f"模型 {model_id or '（未指定）'} 不支持推理强度 {effort!r}，"
                f"支持：{list(levels)}",
                allowedValues=list(levels),
            )

    # --- PATCH ------------------------------------------------------------- #

    @_endpoint
    async def patch_binding(
        binding_id: str, body: PatchBindingBody = Body(...)
    ) -> Any:
        binding = await _load(binding_id)
        changes: dict[str, Any] = {}

        if body.provided("default_model_id"):
            changes["default_model_id"] = body.default_model_id

        if body.provided("runtime_config"):
            incoming = body.runtime_config or {}
            rejected = sorted(set(incoming) - GENERIC_RUNTIME_CONFIG_KEYS)
            if rejected:
                raise ApiError(
                    400,
                    "runtime_config_key_not_generic",
                    "runtime_config 只接受通用键 "
                    f"{sorted(GENERIC_RUNTIME_CONFIG_KEYS)}，"
                    f"拒绝 {rejected}（引擎私有配置属于 Phase 5 的 Capability Registry）",
                    rejectedKeys=rejected,
                )
            # 合并而不是替换：库里那份可能带着导入器写的 `twin_mode` 之类，
            # 一次 PATCH 只该动它明确提到的键。传 null 清掉某个键。
            merged = dict(binding.runtime_config)
            for key, value in incoming.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            # 批次十三第 3 件：合并之后再校验取值——校验的对象是「这次写完之后
            # 库里会是什么」，不是「这次带了什么」。
            await _validate_runtime_config(
                binding,
                merged,
                model_id=changes.get("default_model_id", binding.default_model_id),
            )
            changes["runtime_config"] = merged

        if not changes:
            raise ApiError(
                400,
                "empty_patch",
                "PATCH 至少要带一个字段（defaultModelId 或 runtimeConfig）",
            )

        updated = await repositories.bindings.save(
            binding.evolve(**changes, updated_at=_now())
        )
        payload = binding_to_wire(updated)
        # 说清楚这次写入的边界，免得调用方以为引擎那边也跟着变了。
        payload["projectedToBackend"] = False
        return payload

    # --- make-default ------------------------------------------------------ #

    @_endpoint
    async def make_default(binding_id: str) -> Any:
        binding = await _load(binding_id)
        if binding.is_default:
            return {**binding_to_wire(binding), "changed": False}
        previous = await repositories.bindings.get_default_for_project(
            binding.project_id
        )
        # 先降级旧的再升级新的：`agent_bindings` 上有「一个 Project 一个默认」的
        # 约束，反过来写必然撞闸。
        if previous is not None and previous.id != binding.id:
            await repositories.bindings.save(
                previous.evolve(is_default=False, updated_at=_now())
            )
        updated = await repositories.bindings.save(
            binding.evolve(is_default=True, updated_at=_now())
        )
        return {
            **binding_to_wire(updated),
            "changed": True,
            "previousDefaultBindingId": previous.id if previous else None,
        }

    # --- 新建 -------------------------------------------------------------- #

    @_endpoint
    async def create_binding(
        project_id: str, body: CreateBindingBody = Body(...)
    ) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        backend_id = normalize_backend_key_id(body.backend_id)
        backend = await repositories.backends.get(backend_id)
        if backend is None:
            raise ApiError(
                404, "backend_not_found", f"未知 Backend：{body.backend_id}"
            )
        try:
            new_id = make_binding_id(project.slug, backend.key, body.discriminator)
        except InvalidIdentifierError as exc:
            raise ApiError(400, "invalid_discriminator", str(exc)) from exc
        if await repositories.bindings.get(new_id) is not None:
            raise ApiError(
                409,
                "binding_already_exists",
                f"这条 Binding 已经存在：{new_id}"
                "（同一 Project 挂多条同 Backend 的 Binding 请给不同的 discriminator）",
            )
        binding = AgentBinding.create(
            project=project,
            backend=backend,
            discriminator=body.discriminator,
            display_name=(body.display_name or "").strip() or None,
            # 换默认是另一个动作，创建一律非默认（见 CreateBindingBody 的注释）。
            is_default=False,
            origin="domain",
        )
        saved = await repositories.bindings.save(binding)
        return JSONResponse(status_code=201, content=binding_to_wire(saved))

    # --- 项目工作目录 -------------------------------------------------------- #

    @_endpoint
    async def patch_project(
        project_id: str, body: PatchProjectBody = Body(...)
    ) -> Any:
        """``PATCH /projects/{id}``：只改 ``workspace_root``（批次十三第 4 件）。

        三条校验，全部是**显式冲突**而不是静默纠正（N §13.1）：绝对路径、存在、
        是目录。**不做 chdir**——本进程的工作目录是全局状态，一条 HTTP 请求改它
        会波及所有别的会话；``workspace_root`` 只是一条记在领域库里的路径，
        由 Driver 在拉起 Runtime 时各自使用。
        """
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise ApiError(404, "project_not_found", f"未知 Project：{project_id}")
        raw = body.workspace_root.strip()
        path = Path(raw)
        if not path.is_absolute():
            raise ApiError(
                400,
                "workspace_root_not_absolute",
                f"workspaceRoot 必须是绝对路径，拿到 {raw!r}"
                "（相对路径的含义取决于服务进程当时在哪，不是一个能记账的值）",
            )
        if not path.exists():
            raise ApiError(
                400, "workspace_root_not_found", f"这个目录不存在：{raw}"
            )
        if not path.is_dir():
            raise ApiError(
                400, "workspace_root_not_a_directory", f"这不是一个目录：{raw}"
            )
        updated = await repositories.projects.save(
            project.evolve(workspace_root=str(path), updated_at=_now())
        )
        return project_to_wire(updated)

    # --- 状态（登录态 / 原生会话数 / 探测态） --------------------------------- #

    @_endpoint
    async def binding_status(binding_id: str) -> Any:
        """``GET /bindings/{id}/status``（批次十九第 3 件 / AD-82 / AD-93）。

        一次请求给齐引擎卡与会话页页头都要的三件事，前端因此不必为「引擎离线」
        与「登录状态」各拉一次后端。取数在
        :func:`~app.api.binding_status.read_binding_status`，与
        ``/projects/{id}/bindings`` 每行用的是同一处。
        """
        binding = await _load(binding_id)
        backend = await repositories.backends.get(binding.backend_id)
        return await read_binding_status(
            binding, registry=registry, backend=backend
        )

    # --- 物化与对账（批次二十四 / AD-149） ------------------------------------ #

    async def _project_of(binding: AgentBinding) -> Any:
        project = await repositories.projects.get(binding.project_id)
        if project is None:
            raise ApiError(
                404,
                "project_not_found",
                f"这条 Binding 指向的 Project 不存在：{binding.project_id}",
            )
        return project

    async def _driver_of(binding: AgentBinding) -> Any:
        """这条 Binding 的 Driver。取不到就是 400——**不是** 500。

        「宿主没给 Registry」「这个 Backend 没注册」都是装配态的事实，不是服务器
        内部错误；把它们说清楚，调用方才知道该去修配置而不是重试。
        """
        if registry is None:
            raise ApiError(
                400,
                "driver_not_registered",
                "本进程没有装配 Driver Registry，物化与对账都无从谈起",
            )
        try:
            return registry.get(binding.backend_id)
        except Exception as exc:  # noqa: BLE001 - Registry 的缺失语义各实现不同
            raise ApiError(
                400,
                "driver_not_registered",
                f"未注册的 Backend：{binding.backend_id}（{exc}）",
            ) from exc

    def _require_projector(driver: Any) -> None:
        for method in ("materialize_project_capabilities", "inspect_drift"):
            if not callable(getattr(driver, method, None)):
                raise ApiError(
                    400,
                    "projection_unsupported",
                    f"这个 Driver 没有投射面（缺 {method}）——"
                    "物化是引擎相关的能力，公共层不替它编一个",
                    missingMethod=method,
                )

    async def _effective_of(binding: AgentBinding) -> Any:
        """走与读端点**同一个** Resolver（理由同 session_router 的 `_effective_entry`）。

        带 ``backend_key`` 过滤（R-01）：物化的目标是这一家引擎，别家的 scoped
        能力不该出现在它的变更集里。

        批次四十三：算法本体搬去 :mod:`app.capabilities.effective`——Session Host
        建会话时要算同一件事，三处各抄一遍迟早给出三个答案。
        """
        from app.capabilities.effective import resolve_effective_for_binding

        return await resolve_effective_for_binding(repositories, binding)

    def _flag(value: Any) -> bool:
        """``?confirm=1`` / ``?adopt=true``：只认明确的真值，其余一律 False。

        默认必须是「不写」，所以这里对拿不准的取值一律从严——把 ``?confirm=maybe``
        当成真，就是拿用户的配置文件赌一个拼写。
        """
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @_endpoint
    async def materialize_binding(
        binding_id: str, confirm: str = "0", adopt: str = "0"
    ) -> Any:
        """``POST /bindings/{id}/materialize?confirm=0|1&adopt=0|1``。

        ``confirm=0``（默认）只算变更集；``confirm=1`` 才落盘，并在
        ``projection_results`` 留一行账（**只记键路径与计数，不记值**）。
        ``adopt=1`` 允许接管「当前有值但不是 Kaus 写下的」键（AD-59）。
        """
        binding = await _load(binding_id)
        project = await _project_of(binding)
        driver = await _driver_of(binding)
        _require_projector(driver)
        write = _flag(confirm)
        if write:
            active = await _running_conversations(binding)
            if active:
                raise ApiError(
                    409,
                    "binding_busy",
                    f"这条 Binding 名下还有 {len(active)} 条会话正在跑，"
                    "引擎正在读那份配置——先停掉再物化"
                    "（只看变更集可以：不带 confirm=1 的 dry-run 不受此限）",
                    # 两个键并存：`activeConversationIds` 是既有形状（不动它），
                    # `activeConversations` 多带一个标题，界面才说得出「去停哪一条」。
                    activeConversationIds=[row["conversationId"] for row in active],
                    activeConversations=active,
                )
        effective = await _effective_of(binding)
        result = await driver.materialize_project_capabilities(
            project, binding, effective, dry_run=not write, adopt=_flag(adopt)
        )
        payload = result.model_dump(mode="json", by_alias=True)
        if write:
            await repositories.projections.save(
                ProjectionRecord.create(
                    binding_id=binding.id,
                    project_id=project.id,
                    summary=_projection_summary(result),
                )
            )
        return {
            "bindingId": binding.id,
            "dryRun": bool(result.dry_run),
            "result": payload,
            "backupPath": result.backup_path,
        }

    @_endpoint
    async def projection_meta(binding_id: str) -> Any:
        """``GET /bindings/{id}/projection/_meta``：这条 Binding 有没有投射面。

        前端此前只能**拿 drift 的成败当信号**——打一次 ``GET /drift``，回 400 就
        当作「不支持」。那是把一个探测问题伪装成一次真实调用：`/drift` 会去读
        用户机器上的配置文件，而且它失败的原因不止一种（Registry 没装、Backend
        没注册、Driver 没投射面、配置读不动），全被压成同一个「不支持」。

        这条端点只回答那一个问题，且**不碰任何文件**：``supported`` 为假时
        ``missingMethod`` 说出缺的是哪一个方法（``driver_not_registered`` 那一档
        没有具体方法可报，就是 ``null``）。它自己不报 4xx——「这台引擎不支持」是
        一个正常的答案，不是一次失败的请求（AD-71：缺能力静默不显示，前端要的是
        一个能安静分支的布尔）。
        """
        binding = await _load(binding_id)
        try:
            driver = await _driver_of(binding)
        except ApiError as exc:
            return {
                "bindingId": binding.id,
                "supported": False,
                "missingMethod": None,
                "reason": exc.code,
            }
        for method in ("materialize_project_capabilities", "inspect_drift"):
            if not callable(getattr(driver, method, None)):
                return {
                    "bindingId": binding.id,
                    "supported": False,
                    "missingMethod": method,
                    "reason": "projection_unsupported",
                }
        return {
            "bindingId": binding.id,
            "supported": True,
            "workspace": await _workspace_meta(binding, driver),
        }

    async def _workspace_meta(binding: AgentBinding, driver: Any) -> Any:
        """``projection/_meta`` 里那一小段工作目录信息（批次四十四）。

        两个字段各自独立地可能为 ``null``，因为它们说的是两件事：``root`` 是
        「这个项目有没有工作目录」（用户能去项目设置里填），``instructionsFile``
        是「这台引擎认不认某个指令文件」（用户改不了，AD-167）。压成一个布尔，
        界面就说不清该让谁去做什么。

        文件名**向 Driver 要**，不在这里查任何目录：哪个引擎读哪个文件是 Driver
        的私有知识（N §3 / 批次四十三同一条界线——公共层不 import 具体驱动）。
        没实现这个可选方法的 Driver 就是 ``null``。
        """
        project = await repositories.projects.get(binding.project_id)
        root = project.workspace_root if project else None
        if not root:
            raw = binding.runtime_config.get("workspace_root")
            root = raw if isinstance(raw, str) and raw.strip() else None
        instructions_file: str | None = None
        conventions = getattr(driver, "workspace_conventions", None)
        if callable(conventions):
            try:
                declared = conventions(binding)
            except Exception:  # noqa: BLE001 - 探测失败不该让整个 _meta 变 4xx
                declared = None
            if isinstance(declared, Mapping):
                value = declared.get("instructionsFile")
                instructions_file = value if isinstance(value, str) and value else None
        return {"root": root, "instructionsFile": instructions_file}

    @_endpoint
    async def list_projections(binding_id: str, limit: int = 20) -> Any:
        """``GET /bindings/{id}/projections?limit=20``：最近几次物化的账。

        ``projection_results`` 此前只有写入没有读出口（批次二十四的遗留）。
        这张表**只存摘要不存值**（键路径、动作计数、unsupported 的原因计数），
        所以这条端点也不可能漏出任何配置值——要看值就现读现比（``GET /drift``）。
        按时间倒序，最近的在前。
        """
        binding = await _load(binding_id)
        try:
            requested = int(limit)
        except (TypeError, ValueError):
            requested = 20
        requested = max(1, min(requested, 100))
        records = await repositories.projections.list_for_binding(
            binding.id, limit=requested
        )
        return {
            "bindingId": binding.id,
            "records": [
                {
                    "id": record.id,
                    "bindingId": record.binding_id,
                    "projectId": record.project_id,
                    "appliedAt": record.applied_at.isoformat().replace("+00:00", "Z"),
                    "summary": dict(record.summary),
                }
                for record in records
            ],
            "count": len(records),
        }

    @_endpoint
    async def binding_drift(binding_id: str) -> Any:
        """``GET /bindings/{id}/drift``：四态对账（in_sync / drifted / unmanaged / missing）。"""
        binding = await _load(binding_id)
        project = await _project_of(binding)
        driver = await _driver_of(binding)
        _require_projector(driver)
        effective = await _effective_of(binding)
        report = await driver.inspect_drift(project, binding, effective)
        return {
            "bindingId": binding.id,
            "checkedAt": (
                report.checked_at.isoformat().replace("+00:00", "Z")
                if report.checked_at is not None
                else None
            ),
            "items": [
                {
                    "capabilityType": item.capability_type,
                    "capabilityId": item.capability_id,
                    "keyPath": item.key_path,
                    "expected": item.expected,
                    "actual": item.actual,
                    "state": item.state,
                    "detail": item.detail,
                }
                for item in report.entries
            ],
            "driftedCount": report.drifted_count,
        }

    # --- 删除 -------------------------------------------------------------- #

    @_endpoint
    async def delete_binding(binding_id: str) -> Any:
        binding = await _load(binding_id)
        if binding.is_default:
            raise ApiError(
                409,
                "default_binding_not_deletable",
                "默认 Binding 不可删——先把别的 Binding 设成默认"
                "（POST /api/bindings/{id}/make-default）再删这条",
            )
        active = await _active_runtime_ids(binding)
        if active:
            raise ApiError(
                409,
                "binding_has_active_runtime",
                f"这条 Binding 名下还有 {len(active)} 条活跃 Runtime，先停掉再删",
                activeConversationIds=active,
            )
        # C-4 / v1.0 §16.6：只删 Dashboard 自己的行。
        await repositories.bindings.delete(binding.id)
        return {"bindingId": binding.id, "deleted": True}

    for path, endpoint, methods, name in (
        ("/bindings/{binding_id}", patch_binding, ["PATCH"], "binding_patch"),
        (
            "/bindings/{binding_id}/make-default",
            make_default,
            ["POST"],
            "binding_make_default",
        ),
        ("/projects/{project_id}/bindings", create_binding, ["POST"], "binding_create"),
        ("/projects/{project_id}", patch_project, ["PATCH"], "project_patch"),
        ("/bindings/{binding_id}", delete_binding, ["DELETE"], "binding_delete"),
        (
            "/bindings/{binding_id}/status",
            binding_status,
            ["GET"],
            "binding_status",
        ),
        # 字面量段先注册先赢（同 group_router 里 `/groups/events` 的做法）：
        # `projection/_meta` 必须排在任何 `/bindings/{binding_id}/…` 的通配之前。
        (
            "/bindings/{binding_id}/projection/_meta",
            projection_meta,
            ["GET"],
            "binding_projection_meta",
        ),
        (
            "/bindings/{binding_id}/projections",
            list_projections,
            ["GET"],
            "binding_projections",
        ),
        (
            "/bindings/{binding_id}/materialize",
            materialize_binding,
            ["POST"],
            "binding_materialize",
        ),
        (
            "/bindings/{binding_id}/drift",
            binding_drift,
            ["GET"],
            "binding_drift",
        ),
    ):
        router.add_api_route(
            path, endpoint, methods=methods, name=name, dependencies=[_auth]
        )
    return router


__all__ = [
    "APPROVAL_MODES",
    "GENERIC_RUNTIME_CONFIG_KEYS",
    "CreateBindingBody",
    "PatchBindingBody",
    "PatchProjectBody",
    "build_binding_write_router",
]
