"""Card ⇄ External CLI 的交接编排（v1.0 §8.6 / §8.7；Phase 4 第 4、5 件）。

为什么单独一层
--------------
「回到 Card」有两个触发点：用户点一下（``POST /surface/card``）与监视器发现外部
进程退了（:class:`~runtime.external_cli.ExternalCliMonitor`）。两条路必须做**完全
一样**的事——校准历史、发同一批事件、释放同一把 lease——所以编排住在这里，
接入层与监视器都只是调用方。

状态机（v1.0 §8.6）
-------------------
::

    idle ──open external──> external_active ──external exit──> reconciling ──> idle
      ↑                            │
      └────── return to card ──────┘

三条硬口径
----------
1. **同一 Native Session 永不双写**（AD-122）。开终端前先安全释放 Card：还在跑的
   一轮不给切（409 ``card_running``），空闲才 ``stop_runtime`` → 释放 card lease。
2. **不按「最新 Session」猜绑定**（v1.0 §8.7）。开终端前若这条 Conversation 还没有
   ``native_session_id``，先经 Driver **预创建**一个再把 ``--resume`` 带进命令；
   Driver 不支持预创建就 501，明说「该引擎不支持从站内开终端续接」，
   而不是开完终端再回头猜哪条新会话是这次的。
3. **校准只折成一条扩展事件**。AgentEventEnvelope v1.1 已冻结，外部终端期间的
   原生条目**不逐条伪造**成核心事件（那会凭空造出 ``message.started`` 之类
   Backend 从没发过的事实）；它们折成一条 ``kaus/history.reconciled``，
   ``complete=False`` 时再补一条 ``diagnostic.notice``——不承诺的东西就说不承诺
   （N §13.1）。

三种扩展事件（AD-86 的同一个出口）
----------------------------------
==============================  ===================================================
``kaus/surface.changed``        ``{surface, launchId?}``：写权换手了
``kaus/history.reconciled``     ``{entries, complete, gaps, lastEntryId}``：回站校准
``kaus/lease.taken_over``       ``{previousOwner, previousOwnerLabel, previousOwnerId,
                                stale}``：强制接管
==============================  ===================================================

它们全部走 ``extension.event`` + namespace ``kaus``，与 ``user.message``（AD-86）、
``conversation.deleted``（AD-105）同一个机制——信封冻结，不新增核心事件类型。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from app.ids import terminal_launch_id

from app.conversations.models import Conversation
from app.errors import DomainError
from app.persistence.base import RepositorySet
from app.projects.binding_snapshot import register_binding_snapshot
from app.runtimes.models import TerminalLaunch
from drivers.base import (
    CreateSessionOptions,
    NativeHistory,
    UnsupportedCapabilityError,
    TurnAlreadyRunningError,
)
from runtime.event_envelope import DiagnosticNotice, ExtensionEvent
from runtime.event_reducer import USER_MESSAGE_NAMESPACE
from runtime.external_cli import ExitSignal, LaunchResult, TerminalLauncher
from runtime.lease_manager import LeaseDescription
from runtime.session_host import SessionHost

#: 三种通知与 ``user.message`` 共用产品自己的 namespace（AD-86）。
KAUS_NAMESPACE = USER_MESSAGE_NAMESPACE

SURFACE_CHANGED_NAME = "surface.changed"
HISTORY_RECONCILED_NAME = "history.reconciled"
LEASE_TAKEN_OVER_NAME = "lease.taken_over"

#: ``complete=False`` 时那句诚实话（任务书原文）。
INCOMPLETE_NOTICE = "外部终端期间的结构化轨迹无法完整恢复"

#: 一条 ``history.reconciled`` 最多折进多少条原生条目。原生历史本身已经是尾部
#: 窗口（C-2），这里再兜一次底：事件是要入库和广播的，不该被一次超长回灌撑爆。
MAX_RECONCILED_ENTRIES = 200


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


class SurfaceError(DomainError):
    """交接失败。``code`` 是稳定的机器可读串，接入层按它选状态码。"""

    def __init__(
        self, code: str, message: str, *, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


class SurfaceConflictError(SurfaceError):
    """409：另一个 Surface 正持有写权，或 Card 这一轮还在跑。"""


class SurfaceUnsupportedError(SurfaceError):
    """501：这个引擎给不了这条路（站外 CLI / 预创建 Session）。"""


# --------------------------------------------------------------------------- #
# 返回形状
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExternalHandoff:
    """``POST /surface/external`` 的结果。"""

    conversation: Conversation
    launch: TerminalLaunch
    lease: LeaseDescription | None
    command_summary: str
    launched: bool
    reason: str | None = None


@dataclass(frozen=True)
class CardHandoff:
    """回站校准的结果。``reconciled=False`` = 这次没有可校准的原生历史。"""

    conversation: Conversation
    entries: tuple[dict[str, Any], ...]
    complete: bool
    gaps: tuple[str, ...]
    reconciled: bool


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #


class SurfaceCoordinator:
    """两个 Surface 之间的交接。接入层与监视器共用同一份语义。"""

    def __init__(
        self,
        *,
        session_host: SessionHost,
        repositories: RepositorySet,
        registry: Any,
        launcher: TerminalLauncher,
        clock=_utcnow,
    ) -> None:
        self._host = session_host
        self._repositories = repositories
        self._registry = registry
        self._launcher = launcher
        self._clock = clock
        self._handoff_locks: dict[str, asyncio.Lock] = {}

    # --- 开终端 --------------------------------------------------------- #

    async def to_external(
        self, conversation: Conversation, *, force: bool = False
    ) -> ExternalHandoff:
        """站内卡片 → 外部终端。"""
        lock = self._handoff_locks.setdefault(conversation.id, asyncio.Lock())
        async with lock:
            try:
                async with self._host.conversation_control(conversation.id):
                    current = await self._repositories.conversations.get(conversation.id)
                    current = current or conversation
                    if current.archived_at is not None:
                        raise SurfaceConflictError("conversation_archived", "请先恢复这条对话")
                    return await self._to_external(current, force=force)
            except TurnAlreadyRunningError as exc:
                raise SurfaceConflictError("card_running", "请等待当前回复结束，再打开终端") from exc

    async def _to_external(self, conversation: Conversation, *, force: bool) -> ExternalHandoff:
        conversation = await self._release_card(conversation)
        binding, driver = await self._driver_for(conversation)

        # v1.0 §8.7：先有确定的 native_session_id，再去开窗。
        conversation = await self._ensure_native_session(conversation, binding, driver)

        # 冲突先看一眼再开窗：拿不到写权还把终端开出来，用户就得手动去关一个
        # 本来不该存在的窗口。真正的授予以下面的 try_acquire 为准。
        await self._precheck_external_lease(conversation, force=force)

        try:
            spec = await driver.build_external_cli_launch(conversation)
        except UnsupportedCapabilityError as exc:
            raise SurfaceUnsupportedError(
                "external_cli_unsupported",
                f"该引擎不支持从站内开终端：{exc}",
            ) from exc

        launch_uuid = uuid4().hex
        reserved_id = terminal_launch_id(launch_uuid)
        acquisition = await self._host.leases.try_acquire_external_cli(
            conversation.id, owner_id=reserved_id,
            metadata={"launchId": reserved_id, "backendId": binding.backend_id},
            at=self._clock(), force=force,
        )
        if not acquisition.granted:
            raise SurfaceConflictError("lease_held", "这条会话正在被另一个窗口使用",
                                       detail=_lease_detail(acquisition.conflict))
        try:
            result: LaunchResult = await self._launcher.launch(
                spec, conversation_id=conversation.id, launch_uuid=launch_uuid,
            )
            launch = await self._repositories.terminal_launches.save(result.launch)
        except BaseException:
            await self._host.leases.release(conversation.id, expected_owner_id=reserved_id)
            raise
        if not result.launched:
            await self._host.leases.release(conversation.id, expected_owner_id=reserved_id)
            conversation = await self._save_conversation(conversation, state="idle", preferred_surface="card")
            return ExternalHandoff(conversation=conversation, launch=launch, lease=None,
                command_summary=result.command_summary, launched=False, reason=result.reason)
        # The reservation exists before any process starts. Add the observed pid
        # only after launch and retain the same owner throughout.
        await self._host.leases.try_acquire_external_cli(
            conversation.id, owner_id=reserved_id, backend_process_id=launch.external_process_ref,
            metadata={"launchId": reserved_id, "launcher": launch.launcher, "backendId": binding.backend_id},
            at=self._clock(),
        )
        await self._announce_takeover(conversation, acquisition)

        conversation = await self._save_conversation(
            conversation, state="running-external", preferred_surface="external-cli"
        )
        await self._host.emit_conversation_event(
            conversation,
            ExtensionEvent(
                namespace=KAUS_NAMESPACE,
                name=SURFACE_CHANGED_NAME,
                data={"surface": "external-cli", "launchId": launch.id},
            ),
        )
        return ExternalHandoff(
            conversation=conversation,
            launch=launch,
            lease=await self._host.leases.describe(conversation.id),
            command_summary=result.command_summary,
            launched=result.launched,
            reason=result.reason,
        )

    async def _release_card(self, conversation: Conversation) -> Conversation:
        """安全释放 Card：还在跑就不给切（v1.0 §8.6 ``releasing_card``）。"""
        if not self._host.is_active(conversation.id):
            return conversation
        timeline = self._host.timeline(conversation.id)
        if timeline is not None and timeline.run_state == "running":
            raise SurfaceConflictError(
                "card_running",
                "站内这一轮还在跑；先停止或等它结束，再开外部终端",
                detail={"runState": timeline.run_state},
            )
        await self._host.stop_runtime(conversation.id)
        refreshed = await self._repositories.conversations.get(conversation.id)
        return refreshed or conversation

    async def _ensure_native_session(
        self, conversation: Conversation, binding: Any, driver: Any
    ) -> Conversation:
        if conversation.native_session_id:
            return conversation
        try:
            session = await driver.create_native_session(
                binding,
                CreateSessionOptions(
                    title=conversation.title,
                    model_id=conversation.model_id,
                    provider_id=conversation.provider_id,
                    reasoning_mode=conversation.reasoning_mode,
                ),
            )
        except UnsupportedCapabilityError as exc:
            raise SurfaceUnsupportedError(
                "native_session_precreate_unsupported",
                "该引擎不支持从站内开终端续接（不能预创建原生 Session，"
                f"而按「最新 Session」猜绑定是禁止的）：{exc}",
            ) from exc
        return await self._save_conversation(
            conversation, native_session_id=session.native_session_id
        )

    async def _precheck_external_lease(
        self, conversation: Conversation, *, force: bool
    ) -> None:
        if force:
            return
        description = await self._host.leases.describe(conversation.id)
        if description is None or description.is_stale:
            return
        if description.owner_type != "external-cli":
            return
        raise SurfaceConflictError(
            "lease_held",
            "这条会话的写权已被别人持有；确认要强制接管请带 ?force=1",
            detail=_lease_detail(description),
        )

    # --- 回站 ----------------------------------------------------------- #

    async def to_card(
        self, conversation: Conversation, *, force: bool = False
    ) -> CardHandoff:
        """外部终端 → 站内卡片：校准 → 释放 lease → 置回 idle/card。"""
        lock = self._handoff_locks.setdefault(conversation.id, asyncio.Lock())
        async with lock:
            return await self._to_card(conversation, force=force)

    async def _to_card(self, conversation: Conversation, *, force: bool) -> CardHandoff:
        description = await self._host.leases.describe(conversation.id)
        if (
            not force
            and description is not None
            and description.owner_type == "external-cli"
            and not description.is_stale
        ):
            raise SurfaceConflictError(
                "external_active",
                "外部终端还活着；确认要收回写权请带 ?force=1",
                detail=_lease_detail(description),
            )
        history = await self._load_history(conversation)
        entries, complete, gaps = _diff_history(
            history, seen=await self._last_reconciled_entry_id(conversation)
        )
        if history is not None:
            await self._host.emit_conversation_event(
                conversation,
                ExtensionEvent(
                    namespace=KAUS_NAMESPACE,
                    name=HISTORY_RECONCILED_NAME,
                    data={
                        "entries": list(entries),
                        "complete": complete,
                        "gaps": list(gaps),
                        "lastEntryId": entries[-1]["entryId"] if entries else None,
                        "nativeSessionId": history.native_session_id,
                    },
                ),
            )
            if not complete:
                await self._host.emit_conversation_event(
                    conversation,
                    DiagnosticNotice(level="warn", message=INCOMPLETE_NOTICE),
                )
        if description is not None:
            await self._host.leases.release(
                conversation.id, expected_owner_id=description.owner_id
            )
        conversation = await self._save_conversation(
            conversation, state="idle", preferred_surface="card"
        )
        await self._host.emit_conversation_event(
            conversation,
            ExtensionEvent(
                namespace=KAUS_NAMESPACE,
                name=SURFACE_CHANGED_NAME,
                data={"surface": "card"},
            ),
        )
        return CardHandoff(
            conversation=conversation,
            entries=entries,
            complete=complete,
            gaps=gaps,
            reconciled=history is not None,
        )

    async def on_external_exit(self, signal: ExitSignal) -> None:
        """监视器的回调：外部进程退了就自动做一次「回到 Card」（不需要用户点）。"""
        lock = self._handoff_locks.setdefault(signal.conversation_id, asyncio.Lock())
        async with lock:
            owner = await self._host.leases.describe(signal.conversation_id)
            if owner is None or owner.owner_id != signal.launch.id:
                return  # An older terminal cannot reclaim a newer writer's lease.
            conversation = await self._repositories.conversations.get(signal.conversation_id)
            if conversation is None:
                await self._host.leases.release(signal.conversation_id, expected_owner_id=signal.launch.id)
                return
            await self._to_card(conversation, force=True)

    async def _load_history(self, conversation: Conversation) -> NativeHistory | None:
        """读原生历史（尾部窗口，C-2）。读不到不是错误——校准就少这一段。"""
        if not conversation.native_session_id:
            return None
        try:
            binding, driver = await self._driver_for(conversation)
        except Exception:  # noqa: BLE001 - Driver 摘掉了不该让回站失败
            return None
        try:
            return await driver.load_native_history(
                binding, conversation.native_session_id
            )
        except Exception:  # noqa: BLE001 - 同上：校准是尽力而为
            return None

    async def _last_reconciled_entry_id(self, conversation: Conversation) -> str | None:
        """上一次校准折到哪条为止。没有就从窗口头开始。"""
        for envelope in reversed(await self._host.event_store.replay(conversation.id)):
            event = envelope.event
            if (
                getattr(event, "type", None) == "extension.event"
                and event.namespace == KAUS_NAMESPACE
                and event.name == HISTORY_RECONCILED_NAME
            ):
                data = event.data if isinstance(event.data, dict) else {}
                marker = data.get("lastEntryId")
                return str(marker) if marker else None
        return None

    # --- 只读视图 ------------------------------------------------------- #

    async def describe(self, conversation: Conversation) -> dict[str, Any]:
        """``GET /surface`` 的 wire 形状。"""
        description = await self._host.leases.describe(conversation.id)
        launch = await self.latest_launch(conversation.id)
        if (
            description is not None
            and description.owner_type == "external-cli"
            and not description.is_stale
        ):
            surface = "external-cli"
        else:
            surface = "card"
        return {
            "conversationId": conversation.id,
            "surface": surface,
            # 批次十五第 3 件：装了 Launcher 才有这条路。没装的那种装配由接入层
            # 直接回 supported:false（编排层根本不存在），两边形状逐字一致。
            "supported": True,
            "lease": _lease_wire(description),
            "launch": launch_to_wire(launch),
        }

    async def latest_launch(self, conversation_id: str) -> TerminalLaunch | None:
        launches = await self.list_launches(conversation_id)
        return launches[0] if launches else None

    async def list_launches(self, conversation_id: str) -> tuple[TerminalLaunch, ...]:
        """按 ``launched_at`` 倒序（最近一次在前）。"""
        launches = await self._repositories.terminal_launches.list_for_conversation(
            conversation_id
        )
        return tuple(sorted(launches, key=lambda item: item.launched_at, reverse=True))

    # --- 杂项 ----------------------------------------------------------- #

    async def _driver_for(self, conversation: Conversation) -> tuple[Any, Any]:
        binding = await self._repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise SurfaceError(
                "binding_not_found",
                f"Conversation 指向的 Agent Binding 不存在：{conversation.agent_binding_id}",
            )
        driver = self._registry.get(binding.backend_id)
        # AD-58 的灌入 + AD-175 的工作目录补全，与接入层那五处同一个函数。
        await register_binding_snapshot(driver, binding, self._repositories.projects)
        return binding, driver

    async def _save_conversation(
        self, conversation: Conversation, **changes: Any
    ) -> Conversation:
        current = await self._repositories.conversations.get(conversation.id)
        base = current or conversation
        pending = {
            key: value for key, value in changes.items() if getattr(base, key) != value
        }
        if not pending:
            return base
        return await self._repositories.conversations.save(
            base.evolve(**pending, updated_at=self._clock())
        )

    async def _announce_takeover(self, conversation: Conversation, acquisition) -> None:
        previous = acquisition.previous
        if previous is None:
            return
        await self._host.emit_conversation_event(
            conversation,
            ExtensionEvent(
                namespace=KAUS_NAMESPACE,
                name=LEASE_TAKEN_OVER_NAME,
                data={
                    "previousOwner": previous.owner_type,
                    # 批次十五第 5 件：`previousOwner` 是机器词（`card` /
                    # `external-cli`），横幅上要显示的是人话。翻译放在产生事件的
                    # 这一处，而不是让每个读者各译一遍——前端译一份、通知译一份、
                    # 日志再译一份，迟早对不上。机器字段原样保留。
                    "previousOwnerLabel": owner_label(previous.owner_type),
                    "previousOwnerId": previous.owner_id,
                    "stale": acquisition.stale_recovered,
                    "reason": acquisition.reason,
                },
            ),
        )
        if acquisition.stale_recovered:
            await self._host.emit_conversation_event(
                conversation,
                DiagnosticNotice(
                    level="warn",
                    message=f"回收了一条已过期的 lease：{acquisition.reason}",
                ),
            )


# --------------------------------------------------------------------------- #
# wire helpers
# --------------------------------------------------------------------------- #


#: 写权持有者的人话名（批次十五第 5 件）。取值是封闭集合，未知值原样回显
#: ——编一个「未知界面」出来只会让用户更糊涂（N §13.1）。
OWNER_LABELS: dict[str, str] = {
    "card": "站内卡片",
    "external-cli": "外部终端",
}


def owner_label(owner_type: str | None) -> str | None:
    if owner_type is None:
        return None
    return OWNER_LABELS.get(owner_type, owner_type)


def _lease_detail(description: LeaseDescription | None) -> dict[str, Any]:
    """409 的 detail：``{owner, acquiredAt, launchId}``（任务书原文）。"""
    if description is None:
        return {}
    return {
        "owner": description.owner_type,
        "ownerId": description.owner_id,
        "acquiredAt": description.acquired_at.isoformat(),
        "launchId": description.metadata.get("launchId"),
    }


def _lease_wire(description: LeaseDescription | None) -> dict[str, Any] | None:
    if description is None:
        return None
    return {
        "owner": description.owner_type,
        "ownerId": description.owner_id,
        "acquiredAt": description.acquired_at.isoformat(),
        "heartbeatAt": description.heartbeat_at.isoformat(),
        "expiresAt": (
            description.expires_at.isoformat()
            if description.expires_at is not None
            else None
        ),
        "stale": description.is_stale,
        "launchId": description.metadata.get("launchId"),
    }


def launch_to_wire(launch: TerminalLaunch | None) -> dict[str, Any] | None:
    """``TerminalLaunch`` 的 wire 形状。不含任何 Secret（§16.6）。"""
    if launch is None:
        return None
    return {
        "id": launch.id,
        "launcher": launch.launcher,
        "commandSummary": launch.command_summary,
        "correlationId": launch.correlation_id,
        "externalProcessRef": launch.external_process_ref,
        "status": launch.status,
        "exitStatus": launch.exit_code,
        "launched": "degraded" not in launch.metadata,
        "envPassthrough": list(launch.env_passthrough),
        "launchedAt": launch.launched_at.isoformat(),
        "exitedAt": launch.exited_at.isoformat() if launch.exited_at else None,
    }


def _diff_history(
    history: NativeHistory | None, *, seen: str | None
) -> tuple[tuple[dict[str, Any], ...], bool, tuple[str, ...]]:
    """把原生历史里**新增**的那一段折成可入事件的条目列表。

    ``seen`` 是上一次校准折到的最后一条；在窗口里找得到它就只取它之后的，
    找不到（窗口已经滚过去了）就整段取——宁可重复一次，也不静默丢一段。
    """
    if history is None:
        return (), True, ()
    entries: Sequence[Any] = history.entries
    if seen is not None:
        index = next(
            (
                position
                for position, entry in enumerate(entries)
                if entry.entry_id == seen
            ),
            None,
        )
        if index is not None:
            entries = entries[index + 1 :]
    trimmed = entries[-MAX_RECONCILED_ENTRIES:]
    gaps = list(history.missing)
    if len(trimmed) < len(entries):
        gaps.append(
            f"只折入了最近 {MAX_RECONCILED_ENTRIES} 条，更早的请到原生历史里看"
        )
    folded = tuple(
        {
            "entryId": entry.entry_id,
            "role": entry.role,
            "kind": entry.kind,
            "text": entry.text,
            "occurredAt": (
                entry.occurred_at.isoformat() if entry.occurred_at else None
            ),
        }
        for entry in trimmed
    )
    return folded, bool(history.complete) and not gaps, tuple(gaps)


__all__ = [
    "CardHandoff",
    "ExternalHandoff",
    "HISTORY_RECONCILED_NAME",
    "INCOMPLETE_NOTICE",
    "KAUS_NAMESPACE",
    "LEASE_TAKEN_OVER_NAME",
    "MAX_RECONCILED_ENTRIES",
    "OWNER_LABELS",
    "SURFACE_CHANGED_NAME",
    "SurfaceConflictError",
    "SurfaceCoordinator",
    "SurfaceError",
    "SurfaceUnsupportedError",
    "launch_to_wire",
    "owner_label",
]
