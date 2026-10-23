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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
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
HISTORY_CHECKPOINT_NAME = "history.checkpoint"
LEASE_TAKEN_OVER_NAME = "lease.taken_over"

#: ``complete=False`` 时那句诚实话（任务书原文）。
INCOMPLETE_NOTICE = "外部终端期间的结构化轨迹无法完整恢复"
HISTORY_RETRY_NOTICE = "终端记录暂未同步，请稍后重试"

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
    last_entry_id: str | None = None


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

    async def aclose(self) -> None:
        close = getattr(self._launcher, "aclose", None)
        if callable(close):
            await close()

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

        previous_external = await self._host.leases.describe(conversation.id) if force else None
        await self._precheck_external_lease(conversation, force=force)
        if await self._history_pending(conversation):
            recovered = await self._reconcile_history(conversation)
            if not recovered.reconciled:
                raise SurfaceConflictError("history_sync_pending", HISTORY_RETRY_NOTICE)
        await self._host.prepare_external_session(conversation, binding, driver)

        # v1.0 §8.7：先有确定的 native_session_id，再去开窗。
        conversation = await self._ensure_native_session(conversation, binding, driver)

        # 冲突先看一眼再开窗：拿不到写权还把终端开出来，用户就得手动去关一个
        # 本来不该存在的窗口。真正的授予以下面的 try_acquire 为准。
        try:
            spec = await driver.build_external_cli_launch(conversation)
        except UnsupportedCapabilityError as exc:
            raise SurfaceUnsupportedError(
                "external_cli_unsupported",
                f"该引擎不支持从站内开终端：{exc}",
            ) from exc

        launch_uuid = uuid4().hex
        reserved_id = terminal_launch_id(launch_uuid)
        computer_hook = getattr(driver, "external_computer_use", None)
        computer = computer_hook(conversation) if computer_hook is not None else {}
        resources = tuple(computer.get("resources", ()))
        if resources:
            import os
            from drivers.terminals.managed_process import process_identity
            computer = {**computer, "owner": process_identity(os.getpid())}
        computer_metadata = {"computerUse": computer} if resources else {}
        acquisition = await self._host.leases.try_acquire_external_cli(
            conversation.id, owner_id=reserved_id,
            metadata={"launchId": reserved_id, "backendId": binding.backend_id, **computer_metadata},
            at=self._clock(), force=force,
        )
        if not acquisition.granted:
            raise SurfaceConflictError("lease_held", "这条会话正在被另一个窗口使用",
                                       detail=_lease_detail(acquisition.conflict))
        pending_launch = None
        try:
            try:
                baseline = await self._load_history(conversation)
            except Exception as exc:
                raise SurfaceConflictError("history_sync_pending", HISTORY_RETRY_NOTICE) from exc
            if baseline is not None and baseline.entries:
                await self._host.emit_conversation_event(conversation, ExtensionEvent(
                    namespace=KAUS_NAMESPACE, name=HISTORY_CHECKPOINT_NAME,
                    data={"lastEntryId": baseline.entries[-1].entry_id,
                          "nativeSessionId": baseline.native_session_id},
                ))
            if resources:
                pending_launch = TerminalLaunch.create(
                    conversation_id=conversation.id, launcher=self._launcher.selected_app(),
                    command_summary="Native Agent", correlation_id=reserved_id,
                    metadata=computer_metadata, launch_uuid=launch_uuid, launched_at=self._clock(),
                )
                # Recovery can find this launch even if the backend exits while
                # the terminal is opening. Only resource IDs are persisted here.
                await self._repositories.terminal_launches.save(pending_launch)
                await self._reserve_computer_resources(pending_launch)
            result: LaunchResult = await self._launcher.launch(
                spec, conversation_id=conversation.id, launch_uuid=launch_uuid,
            )
            launch = await self._repositories.terminal_launches.save(result.launch.evolve(
                metadata={**result.launch.metadata, **computer_metadata}))
        except BaseException:
            stopped = True
            if pending_launch is not None:
                stopped = await self._stop_computer_launch(pending_launch, conversation, driver)
            if stopped:
                await self._host.leases.release(conversation.id, expected_owner_id=reserved_id)
            raise
        if not result.launched:
            if resources and not await self._stop_computer_launch(launch, conversation, driver):
                raise SurfaceConflictError("external_stop_failed", "终端任务尚未结束，请稍后重试")
            await self._host.leases.release(conversation.id, expected_owner_id=reserved_id)
            conversation = await self._save_conversation(conversation, state="idle", preferred_surface="card")
            return ExternalHandoff(conversation=conversation, launch=launch, lease=None,
                command_summary=result.command_summary, launched=False, reason=result.reason)
        # The reservation exists before any process starts. Add the observed pid
        # only after launch and retain the same owner throughout.
        await self._host.leases.try_acquire_external_cli(
            conversation.id, owner_id=reserved_id, backend_process_id=launch.external_process_ref,
            metadata={"launchId": reserved_id, "launcher": launch.launcher, "backendId": binding.backend_id,
                      **computer_metadata},
            at=self._clock(),
        )
        if previous_external is not None and previous_external.owner_type == "external-cli" and acquisition.previous is None:
            acquisition = replace(acquisition, previous=previous_external,
                                  stale_recovered=previous_external.is_stale, reason="external writer replaced")
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
        projection = self._host.session_projection(conversation.id)
        options = (projection.options if projection else CreateSessionOptions()).model_copy(
            update={
                "title": conversation.title,
                "model_id": conversation.model_id,
                "provider_id": conversation.provider_id,
                "reasoning_mode": conversation.reasoning_mode,
            }
        )
        try:
            precreate = getattr(driver, "create_external_native_session", None)
            session = (
                await precreate(conversation, options)
                if precreate is not None
                else await driver.create_native_session(binding, options)
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
        description = await self._host.leases.describe(conversation.id)
        if description is None:
            return
        if description.owner_type != "external-cli":
            return
        if force:
            # Fold the previous terminal's messages before taking a fresh
            # outbound baseline, including a forced replacement of that writer.
            await self._to_card(conversation, force=True)
            return
        raise SurfaceConflictError(
            "lease_held",
            "这条会话的写权已被别人持有；确认要强制接管请带 ?force=1",
            detail=_lease_detail(description),
        )

    async def _stop_external_writer(self, description: LeaseDescription) -> None:
        launch = await self._repositories.terminal_launches.get(description.owner_id)
        if launch is None:
            raise SurfaceConflictError("external_stop_failed", "无法确认终端进程，请先退出终端中的 Agent")
        computer = launch.metadata.get("computerUse", {})
        if computer.get("resources"):
            conversation = await self._repositories.conversations.get(description.conversation_id)
            if conversation is None:
                raise SurfaceConflictError("external_stop_failed", "终端会话尚未恢复，请先退出终端中的 Agent")
            _, driver = await self._driver_for(conversation)
            stopped = await self._stop_computer_launch(launch, conversation, driver)
            reason = "终端工具仍在运行，请先结束任务"
        else:
            stopped, reason = await self._launcher.stop(launch)
        if not stopped:
            raise SurfaceConflictError("external_stop_failed", reason or "终端尚未结束，请稍后重试")
        if launch.status not in ("exited", "failed"):
            await self._repositories.terminal_launches.save(launch.mark_exited(exit_code=-1, at=self._clock()))

    async def _reserve_computer_resources(self, launch: TerminalLaunch) -> None:
        from drivers.computer_use.lease import ResourceLease, ResourceBusyError
        from drivers.computer_use.integration import reconcile_resources
        await reconcile_resources()
        computer = launch.metadata["computerUse"]
        directory = Path(self._launcher.root) / launch.id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        held = []
        try:
            for resource in sorted(set(computer["resources"])):
                lease = ResourceLease(resource, timeout=0)
                await lease.acquire()
                held.append(lease)
            for lease in held:
                lease.reserve_external(launch.id, directory, remote=computer.get("remote") is True)
        except ResourceBusyError as exc:
            raise SurfaceConflictError("computer_in_use", "电脑工具正在使用中，请稍后打开终端") from exc
        finally:
            for lease in reversed(held):
                lease.release()

    async def _stop_computer_launch(self, launch, conversation, driver) -> bool:
        """Use the native process-group guard, then confirm any remote work."""
        from drivers.computer_use.lease import ResourceLease
        directory = Path(self._launcher.root) / launch.id
        computer = launch.metadata.get("computerUse", {})
        try:
            if not (directory / "native.json").exists():
                # This reserved launch never reached the native entrypoint.
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                (directory / "revoked").touch(mode=0o600)
                stopped = True
            else:
                stopped, _ = await self._launcher.stop(launch)
            if not stopped:
                return False
            # A revoked entrypoint that never exec'd the CLI cannot own a
            # remote operation. Failed opens must not strand a writer lease.
            if computer.get("remote") and (directory / "process.json").exists():
                confirm = getattr(driver, "stop_external_computer_use", None)
                if confirm is None or not await confirm(conversation):
                    return False
            for resource in computer.get("resources", ()):
                await ResourceLease(resource).clear_external(launch.id)
            return True
        except Exception:
            return False

    # --- 回站 ----------------------------------------------------------- #

    async def to_card(
        self, conversation: Conversation, *, force: bool = False
    ) -> CardHandoff:
        """外部终端 → 站内卡片：校准 → 释放 lease → 置回 idle/card。"""
        lock = self._handoff_locks.setdefault(conversation.id, asyncio.Lock())
        async with lock:
            try:
                async with self._host.conversation_control(conversation.id):
                    current = await self._repositories.conversations.get(conversation.id)
                    return await self._to_card(current or conversation, force=force)
            except TurnAlreadyRunningError as exc:
                owner = await self._host.leases.describe(conversation.id)
                if self._host.is_active(conversation.id) and (owner is None or owner.owner_type == "card"):
                    current = await self._repositories.conversations.get(conversation.id)
                    return await self._already_card(current or conversation)
                raise SurfaceConflictError("card_running", "请等待当前回复结束") from exc

    async def _to_card(self, conversation: Conversation, *, force: bool) -> CardHandoff:
        description = await self._host.leases.describe(conversation.id)
        if (description is not None and description.owner_type == "card") or (
            self._host.is_active(conversation.id) and description is None
        ):
            return await self._already_card(conversation)
        if (
            not force
            and description is not None
            and description.owner_type == "external-cli"
        ):
            raise SurfaceConflictError(
                "external_active",
                "外部终端还活着；确认要收回写权请带 ?force=1",
                detail=_lease_detail(description),
            )
        if description is not None and description.owner_type == "external-cli":
            await self._stop_external_writer(description)
        reconciliation = await self._reconcile_history(conversation)
        if description is not None and description.owner_type == "external-cli":
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
        return replace(reconciliation, conversation=conversation)

    async def _already_card(self, conversation: Conversation) -> CardHandoff:
        pending = await self._history_pending(conversation)
        return CardHandoff(conversation=conversation, entries=(), complete=not pending,
            gaps=(HISTORY_RETRY_NOTICE,) if pending else (), reconciled=False,
            last_entry_id=await self._last_reconciled_entry_id(conversation))

    async def _reconcile_history(self, conversation: Conversation) -> CardHandoff:
        if conversation.origin == "standard":
            # New inbound text may use native references. The watermark protects
            # every event that predates this reconciliation, including old imports.
            await self._host.event_store.native_storage.begin(conversation.id, conversation.native_session_id)
        seen = await self._last_reconciled_entry_id(conversation)
        pending = await self._history_pending(conversation)
        try:
            history = await self._load_history(conversation)
            failed = history is None and pending
        except Exception:
            history, failed = None, True
        if failed:
            # Use the durable history event: a later process must retry this
            # inbound window before writing a new outbound checkpoint.
            await self._host.emit_conversation_event(conversation, ExtensionEvent(
                namespace=KAUS_NAMESPACE, name=HISTORY_RECONCILED_NAME,
                data={"entries": [], "complete": False, "gaps": [HISTORY_RETRY_NOTICE],
                      "lastEntryId": seen, "nativeSessionId": conversation.native_session_id,
                      "pending": True},
            ))
            await self._host.emit_conversation_event(conversation,
                DiagnosticNotice(level="warn", message=HISTORY_RETRY_NOTICE))
            return CardHandoff(conversation=conversation, entries=(), complete=False,
                gaps=(HISTORY_RETRY_NOTICE,), reconciled=False, last_entry_id=seen)
        known = await self._reconciled_entry_ids(conversation)
        entries, complete, gaps = _diff_history(history, seen=seen, known=known)
        last_entry_id = history.entries[-1].entry_id if history is not None and history.entries else seen
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
                        "lastEntryId": last_entry_id,
                        "nativeSessionId": history.native_session_id,
                    },
                ),
            )
            await self._host.compact_native_text(conversation)
            if not complete:
                await self._host.emit_conversation_event(
                    conversation,
                    DiagnosticNotice(level="warn", message=INCOMPLETE_NOTICE),
                )
        return CardHandoff(
            conversation=conversation,
            entries=entries,
            complete=complete,
            gaps=gaps,
            reconciled=history is not None,
            last_entry_id=last_entry_id,
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
            try:
                async with self._host.conversation_control(signal.conversation_id):
                    await self._to_card(conversation, force=True)
            except TurnAlreadyRunningError:
                return  # A concurrent Card turn retains its own writer lease.

    async def _load_history(self, conversation: Conversation) -> NativeHistory | None:
        """Unsupported histories are optional; failed reads must remain retryable."""
        if not conversation.native_session_id:
            return None
        binding, driver = await self._driver_for(conversation)
        try:
            history = await driver.load_native_history(
                binding, conversation.native_session_id
            )
        except UnsupportedCapabilityError:
            return None
        if history.native_session_id != conversation.native_session_id:
            raise SurfaceError("native_history_scope_mismatch", "原生记录与当前会话不匹配")
        return await self._host.normalize_native_history(conversation.id, history)

    async def _history_pending(self, conversation: Conversation) -> bool:
        for envelope in reversed(await self._host.event_store.replay(conversation.id)):
            event = envelope.event
            if (event.type == "extension.event" and event.namespace == KAUS_NAMESPACE
                    and event.name == HISTORY_RECONCILED_NAME and isinstance(event.data, dict)
                    and event.data.get("nativeSessionId") in (None, conversation.native_session_id)):
                return event.data.get("pending") is True
        return False

    async def _last_reconciled_entry_id(self, conversation: Conversation) -> str | None:
        """上一次校准折到哪条为止。没有就从窗口头开始。"""
        for envelope in reversed(await self._host.event_store.replay(conversation.id)):
            event = envelope.event
            if (
                event.type == "extension.event"
                and event.namespace == KAUS_NAMESPACE
                and event.name in (HISTORY_RECONCILED_NAME, HISTORY_CHECKPOINT_NAME)
            ):
                data = event.data if isinstance(event.data, dict) else {}
                if data.get("nativeSessionId") not in (None, conversation.native_session_id):
                    continue
                marker = data.get("lastEntryId")
                if marker:
                    return str(marker)
        return None

    async def _reconciled_entry_ids(self, conversation: Conversation) -> frozenset[str]:
        known = set()
        for envelope in await self._host.event_store.replay(conversation.id):
            event = envelope.event
            if (event.type == "extension.event" and event.namespace == KAUS_NAMESPACE
                    and event.name == HISTORY_RECONCILED_NAME and isinstance(event.data, dict)
                    and event.data.get("nativeSessionId") in (None, conversation.native_session_id)):
                for entry in event.data.get("entries", []):
                    if isinstance(entry, dict) and isinstance(entry.get("entryId"), str):
                        known.add(entry["entryId"])
        return frozenset(known)

    # --- 只读视图 ------------------------------------------------------- #

    async def describe(self, conversation: Conversation) -> dict[str, Any]:
        """``GET /surface`` 的 wire 形状。"""
        description = await self._host.leases.describe(conversation.id)
        launch = await self.latest_launch(conversation.id)
        if (
            description is not None
            and description.owner_type == "external-cli"
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
    history: NativeHistory | None, *, seen: str | None, known: frozenset[str] = frozenset()
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
    entries = [entry for entry in entries
               if entry.entry_id not in known and entry.role in ("user", "assistant", "tool")]
    gaps = list(history.missing)
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
        for entry in entries
    )
    return folded, bool(history.complete) and not gaps, tuple(gaps)


__all__ = [
    "CardHandoff",
    "ExternalHandoff",
    "HISTORY_RECONCILED_NAME",
    "INCOMPLETE_NOTICE",
    "KAUS_NAMESPACE",
    "LEASE_TAKEN_OVER_NAME",
    "OWNER_LABELS",
    "SURFACE_CHANGED_NAME",
    "SurfaceConflictError",
    "SurfaceCoordinator",
    "SurfaceError",
    "SurfaceUnsupportedError",
    "launch_to_wire",
    "owner_label",
]
