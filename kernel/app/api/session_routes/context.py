"""Shared dependencies and helpers for the session route modules.

:class:`SessionRouteContext` carries what every endpoint group of
:func:`~app.api.session_router.build_session_router` needs: the Session Host,
the repositories, the Driver registry, the attachment store, the auth
dependencies and the ``ApiError`` → JSON wrapper. The helper methods are the
conversions that more than one endpoint group performs (load a conversation,
hand the Driver its Binding snapshot, ensure a runtime, read the workspace of
a conversation, record a proxied action). Endpoint-group specific helpers stay
in their own module.

Pure translations from Driver exceptions to :class:`SessionApiError` live in
:mod:`app.api.session_routes.common`; this module holds only what needs the
host or the repositories.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from app.api.conversation_files import ConversationFiles
from app.api.session_routes.common import SessionApiError, _not_found
from app.api.session_views import conversation_surface
from app.api.views import normalize_backend_key_id
from app.conversations.models import Conversation
from app.persistence.base import RepositorySet
from app.projects.binding_snapshot import register_binding_snapshot
from drivers.base import (
    AuthRequiredError,
    DriverNotRegisteredError,
    FailureHint,
    UnsupportedCapabilityError,
    plain_text,
)
from runtime.event_envelope import AgentError, RunFailed
from runtime.event_reducer import TimelineState
from runtime.session_host import (
    BindingNotFoundError,
    ExternalSurfaceActiveError,
    SessionHost,
)
from runtime.surface_handoff import SurfaceCoordinator


class SessionRouteContext:
    """Dependencies shared by the session endpoint groups.

    ``endpoint`` is the ``ApiError`` → JSON wrapper built once per router;
    ``auth`` / ``auth_sse`` are the framework dependencies that enforce
    :class:`~app.api.session_auth.SessionAuthPolicy` (the SSE one also accepts
    ``?token=``).
    """

    def __init__(
        self,
        *,
        session_host: SessionHost,
        repositories: RepositorySet,
        registry: Any,
        files: ConversationFiles,
        endpoint: Callable[..., Any],
        auth: Any,
        auth_sse: Any,
        configured_backend_ids: Sequence[str] = (),
        surface_coordinator: SurfaceCoordinator | None = None,
        keepalive_interval: float,
        run_id_timeout: float,
        module_conversation_ids: Callable[[], set[str]] | None = None,
        attachment_root: str | Path | None = None,
    ) -> None:
        self.host = session_host
        self.repositories = repositories
        self.registry = registry
        self.files = files
        self.endpoint = endpoint
        self.auth = auth
        self.auth_sse = auth_sse
        self.surface_coordinator = surface_coordinator
        self.keepalive_interval = keepalive_interval
        self.run_id_timeout = run_id_timeout
        self.module_conversation_ids = module_conversation_ids
        self.attachment_root = attachment_root
        self.configured = frozenset(
            normalize_backend_key_id(value) for value in configured_backend_ids
        )

    # --- Driver / Binding -------------------------------------------------- #

    def driver_not_registered(self, backend_id: str) -> SessionApiError:
        """「这个 Backend 没有 Driver」→ 一句人话 + 怎么修（批次十一第 3 件）。

        原来这里回的是 ``str(exc)``，也就是一句 ``未注册的 backend：'backend:xxx'``
        —— 这句话对的，但它只说了「坏了」，没说「怎么修」，用户在界面上看到的
        就是一条内部报错。现在 ``message`` 说人话，``detail`` 里放机器要的三样：
        ``backendId`` / 当前**已注册**的 id 列表 / 一句 ``hint``。

        两种 hint 对应两种根因，靠「配置里有没有这条 id」区分：配置里压根没有，
        那是少写了一段配置；配置里有却没注册上，那是这条配置本身没生效
        （``base_url`` 没填、网关没起）。detail 里只有 id，不含任何配置内容。
        """
        normalized = normalize_backend_key_id(backend_id)
        if normalized in self.configured:
            failure = FailureHint(
                code="backend_configured_but_not_registered",
                message=f"这台机器上还没有可用的「{normalized}」引擎，所以这条会话发不出去。",
                hint="该引擎的网关没在运行或 base_url 未配置",
            )
        else:
            failure = FailureHint(
                code="backend_not_configured",
                message=f"这台机器上还没有可用的「{normalized}」引擎，所以这条会话发不出去。",
                hint=(
                    f"在 dashboard-config.json 的 backends 里加一条 id 为 {normalized}、"
                    "driver 为 native-http 的配置（见 docs/ops/backends.md）"
                ),
            )
        return SessionApiError(
            503,
            "driver_not_registered",
            failure.message,
            detail={
                "backendId": normalized,
                "registered": list(self.registry.backend_ids()),
                # 批次十六第 1 件：`hint` 的形状与 `runtime_start_failed` 统一，
                # 都来自 :class:`drivers.base.FailureHint`（AD-108 的口径推广）。
                **failure.to_detail(),
            },
        )

    async def load_conversation(self, conversation_id: str) -> Conversation:
        conversation = await self.repositories.conversations.get(conversation_id)
        if conversation is None:
            raise _not_found(
                "conversation_not_found", f"未知 Conversation：{conversation_id}"
            )
        return conversation

    async def prepare_driver(self, conversation: Conversation) -> Any:
        """AD-58：Driver 侧的 Binding 缓存由接入层在开 Runtime 前灌一次。

        Binding 仓库属上层，Driver 不主动查库；有 ``register_binding`` 的 Driver
        在这里拿到快照，没有的（Mock）什么都不做。

        AD-175：灌进去之前先用**项目的**工作目录把 Binding 上空着的那一格补上
        （:func:`~app.projects.binding_snapshot.register_binding_snapshot`），
        这样「起会话的 cwd」和「投影写文件的目标」用的是同一把尺子。
        """
        binding = await self.repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise _not_found(
                "binding_not_found",
                f"Conversation 指向的 Agent Binding 不存在：{conversation.agent_binding_id}",
            )
        try:
            driver = self.registry.get(binding.backend_id)
        except DriverNotRegisteredError as exc:
            raise self.driver_not_registered(binding.backend_id) from exc
        await register_binding_snapshot(driver, binding, self.repositories.projects)
        return binding

    async def ensure_runtime(self, conversation: Conversation) -> Any:
        binding = await self.prepare_driver(conversation)
        try:
            return await self.host.ensure_runtime(conversation)
        except BindingNotFoundError as exc:
            raise _not_found("binding_not_found", str(exc)) from exc
        except DriverNotRegisteredError as exc:
            # 上面 `prepare_driver` 已经挡过一次，这里是 Session Host 自己再查
            # 一遍注册表的兜底路径（比如两次查询之间 Driver 被摘掉了）。
            raise self.driver_not_registered(binding.backend_id) from exc
        except ExternalSurfaceActiveError as exc:
            # AD-122：外部终端持有写权时站内**不写**。前端按这个 code 显示
            # 「正在外部终端运行」，并给一条「收回站内」（`/surface/card`）的路。
            raise SessionApiError(
                409,
                "surface_external_active",
                str(exc),
                detail={
                    "owner": exc.lease.owner_type,
                    "ownerId": exc.lease.owner_id,
                    "acquiredAt": exc.lease.acquired_at.isoformat(),
                    "launchId": exc.lease.metadata.get("launchId"),
                },
            ) from exc
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc

    # --- Conversation state ------------------------------------------------ #

    async def emit_send_failed(
        self, conversation: Conversation, exc: AuthRequiredError
    ) -> None:
        """把「这一句没发出去」写进会话流，让时间线**看得见**这次失败。

        用已有的 ``run.failed`` 信封（v1.1 冻结期内不新造事件类型），``run_id``
        留空——这一轮压根没开始。合成失败不能反过来把这条请求变成 500，所以整个
        包在 suppress 里：用户要看到的是那句 400 的错误，不是我们记事件时摔的跤。
        """
        failure = exc.failure
        message = (
            failure.describe()
            if isinstance(failure, FailureHint)
            else plain_text(str(exc)) or "这台引擎还没有登录"
        )
        with contextlib.suppress(Exception):
            await self.host.emit_conversation_event(
                conversation,
                RunFailed(
                    run_id=None,
                    error=AgentError(
                        code="auth_required", message=message, retriable=True
                    ),
                ),
            )

    async def empty_conversation_extra(self, conversation: Conversation) -> dict[str, Any]:
        """这条会话到现在为止是不是「白建的」（批次十一第 2 件）。

        判据两条，都要满足：``EventStore`` 里一条事件都没有，且还没有绑上原生
        Session。满足就说明它从建出来到现在什么都没发生过——首条消息又发不出去，
        留着就是一条永远空着的会话。

        服务端**不自动删**：删除是显式动作（有单独的 ``DELETE`` 端点），这里只
        给前端一个「删了也不会丢东西」的凭据，删不删由用户那一侧决定。
        """
        if conversation.native_session_id is not None:
            return {}
        if await self.host.event_store.latest_sequence(conversation.id) is not None:
            return {}
        return {"emptyConversation": True}

    async def surfaces_of(
        self,
        conversations: Sequence[Conversation],
    ) -> dict[str, str]:
        """批次十五第 2 件：一批会话各自在哪个 Surface 上写。

        lease 一次性全取（``describe_all``）：会话索引是侧栏每 30s 拉一次的东西，
        一条会话一次点查会让 50 条列表变成 50 次查库——与 backendId 那里同一个
        理由。取不到 lease 表就退回每条会话自己的 ``preferred_surface``：
        没有写权记录时，那正是「上次在哪个界面」的正确答案。
        """
        leases = {
            description.conversation_id: description
            for description in await self.host.leases.describe_all()
        }
        return {
            conversation.id: conversation_surface(
                conversation, lease=leases.get(conversation.id)
            )
            for conversation in conversations
        }

    async def timeline_of(self, conversation_id: str) -> TimelineState | None:
        """活跃 runtime 直接取 Reducer 的状态；否则从缓冲重算一次。

        重算而不是「返回 null」：停掉 runtime 之后卡片仍要能显示上一轮的样子，
        而 Event Store 里的事件本来就是为重放存在的（D-16）。缓冲已过期清空时
        才真的是 ``None``。

        批次十七：重算这件事下沉到 Session Host（自愈要用同一份判断，两处各写
        一遍迟早会算出两个答案）。这里只留一个名字。
        """
        return await self.host.timeline_from_store(conversation_id)

    async def conversation_workspace(self, conversation: Conversation) -> str | None:
        handle = self.host.handle_for(conversation.id)
        if handle is not None:
            value = handle.metadata.get("workspaceRoot")
            return value if isinstance(value, str) and value else None
        # A project may have changed directories since this conversation ran.
        # Only a runtime-observed snapshot can authorize historical file access.
        events = await self.host.event_store.replay(conversation.id)
        for envelope in reversed(events):
            event = envelope.event
            if event.type == "extension.event" and event.namespace == "kaus" and event.name == "runtime.workspace":
                value = event.data.get("workspaceRoot") if isinstance(event.data, dict) else None
                return value if isinstance(value, str) and value else None
        return None

    async def record_proxy(self, conversation: Conversation, event: Any, *, run_id: str | None = None) -> bool:
        """Timeline row for a proxied action. The action already happened, so a
        failed write is reported as ``recorded: false`` rather than as a failure."""
        try:
            await self.host.emit_conversation_event(conversation, event, run_id=run_id)
        except Exception:  # noqa: BLE001 - see docstring
            logging.getLogger(__name__).warning(
                "Proxied action on %s happened but could not be recorded", conversation.id, exc_info=True)
            return False
        return True


__all__ = ["SessionRouteContext"]
