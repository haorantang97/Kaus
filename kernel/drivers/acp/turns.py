"""Turn execution: ``session/prompt``, interruption and prompt content blocks.

A turn runs as a driver-owned task; skill adapters may validate or watch it,
and shared desktop resources are leased for the duration of the native tool
loop (``_computer_prompt``). ``_prompt_blocks`` converts a public
:class:`~drivers.base.MessageInput` into ACP content blocks according to the
agent's declared prompt capabilities.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import replace
from typing import Any, Mapping

from drivers.error_text import safe_exception_message
from drivers.acp.client import AcpRpcError, AcpTransportError
from drivers.base import (
    MessageInput,
    ModelRejectedError,
    RuntimeHandle,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
    turn_already_running_hint,
)
from drivers.acp.runtime_state import _RuntimeState


def _prompt_blocks(
    content: MessageInput, prompt_capabilities: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Send prepared content without opening arbitrary paths inside the driver.

    ACP 规定每个 agent 都必须收 ``text`` 与 ``resource_link``，所以附件总能送到：
    agent 声明了 ``image`` / ``embeddedContext`` 就内嵌内容，否则给本机文件链接。
    已校验的文本同时用基线 text 块传递：上传目录可能不在 agent 的工作区内。
    二进制链接仍需 agent 自己读取。``prompt_capabilities`` 为 ``None`` 时按全都支持处理。
    """
    can_image = prompt_capabilities is None or prompt_capabilities.get("image") is True
    can_embed = prompt_capabilities is None or prompt_capabilities.get("embeddedContext") is True
    blocks: list[dict[str, Any]] = ([{"type": "text", "text": content.text}] if content.text else [])
    for attachment in content.attachments:
        is_image = (attachment.mime_type or "").startswith("image/")
        if is_image and can_image and attachment.content_base64 is not None:
            blocks.append({"type": "image", "data": attachment.content_base64, "mimeType": attachment.mime_type})
        elif can_embed and (attachment.content_text is not None or attachment.content_base64 is not None):
            resource: dict[str, Any] = {"uri": attachment.ref, "mimeType": attachment.mime_type or "application/octet-stream"}
            if attachment.content_text is not None:
                resource["text"] = attachment.content_text
            else:
                resource["blob"] = attachment.content_base64
            blocks.append({"type": "resource", "resource": resource})
        elif attachment.kind == "file":
            blocks.append(
                {
                    "type": "resource_link",
                    "uri": attachment.ref,
                    "name": attachment.name or attachment.ref,
                    **(
                        {"mimeType": attachment.mime_type}
                        if attachment.mime_type
                        else {}
                    ),
                }
            )
            if attachment.content_text is not None:
                name = attachment.name or attachment.ref
                blocks.append({
                    "type": "text",
                    "text": f"--- {name} ---\n{attachment.content_text}\n--- {name} 结束 ---",
                })
        else:
            blocks.append(
                {"type": "text", "text": f"[{attachment.kind}] {attachment.ref}"}
            )
    return blocks


_LOG = logging.getLogger(__name__)


def _runtime_label(state: Any) -> str | None:
    """Runtime id for log context; tolerant of partially built state."""
    return getattr(getattr(state, "handle", None), "runtime_id", None)


class AcpTurnMixin:
    """Prompt / interrupt handling; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    async def send_message(self, runtime: RuntimeHandle, content: MessageInput) -> None:
        state = self._state(runtime)
        async with state.control_lock:
            await self._send_message_locked(runtime, content)

    async def _send_message_locked(self, runtime: RuntimeHandle, content: MessageInput) -> None:
        state = self._state(runtime)
        if state.permission_pending or state.config_pending:
            raise ModelRejectedError("设置切换尚未确认", reason="请重新选择尚未确认的设置后继续。")
        if state.prompt_task is not None and not state.prompt_task.done():
            # R7（批次三十七）：这不是故障，是「等一等」。此前抛的是一个裸
            # ``RuntimeError``，从会话端点漏进统一 500 兜底——真机上用户看到的是
            # 「服务端出错：RuntimeError」，而别家 Driver 走同一条路早就回 409 加一句
            # 人话。忙这件事是公共层的词汇，两家共用同一个异常与同一份提示。
            raise TurnAlreadyRunningError(
                "上一轮尚未结束；先 interrupt 或等待终态",
                failure=turn_already_running_hint(),
            )
        adapter = self._skill_adapter()
        validate = getattr(adapter, "validate_turn", None)
        if validate is not None and state.skill_plan is not None:
            await validate(state.skill_plan, self._resolve_cwd(getattr(state.handle, "metadata", {}).get("workspaceRoot") or self._workspace_root_for(runtime.binding_id)))
        state.interrupt_requested = False
        self._push(state, state.translator.begin_run())
        state.prompt_task = asyncio.create_task(self._prompt(state, content))

    async def _guarded_prompt(self, state: _RuntimeState, params):
        adapter = self._skill_adapter()
        validate_prompt = getattr(adapter, "validate_prompt", None)
        if validate_prompt is not None and state.skill_plan is not None:
            await validate_prompt(state.skill_plan, params.get("prompt", []),
                self._resolve_cwd(getattr(state.handle, "metadata", {}).get("workspaceRoot") or self._workspace_root_for(state.handle.binding_id)))
        watch = getattr(adapter, "watch", None)
        if watch is None or state.skill_plan is None:
            return await state.connection.call("session/prompt", params, timeout=self._prompt_timeout)
        request = asyncio.create_task(state.connection.call("session/prompt", params, timeout=self._prompt_timeout))
        guard = asyncio.create_task(watch(state.skill_plan,
            self._resolve_cwd(getattr(state.handle, "metadata", {}).get("workspaceRoot") or self._workspace_root_for(state.handle.binding_id))))
        try:
            done, _ = await asyncio.wait((request, guard), return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                try:
                    state.connection.notify("session/cancel", {"sessionId": state.session_id})
                    await asyncio.wait_for(asyncio.shield(request), 2)
                except (AcpRpcError, AcpTransportError, asyncio.TimeoutError):
                    await state.connection.aclose()
                guard.result()
            return await request
        finally:
            for task in (request, guard):
                if not task.done():
                    task.cancel()
            await asyncio.gather(request, guard, return_exceptions=True)

    async def _computer_prompt(self, state: _RuntimeState, invoke):
        """Serialize shared targets before entering the native tool loop."""
        from drivers.computer_use.lease import ResourceLease
        from drivers.computer_use.integration import reconcile_resources
        plan = state.runtime_metadata.get("_kausComputerUse", {})
        resources = plan.get("resources", ()) if isinstance(plan, Mapping) else ()
        leases = []
        started = False
        async def retire_and_confirm():
            nonlocal leases
            state.transport_retired = True
            # Transfer ownership before awaiting shutdown: a second cancel
            # must not hand the desktop to another still-running turn.
            from drivers.computer_use.integration import quarantine, release_quarantine
            held = tuple(leases)
            confirm_native = getattr(self._runtime_adapter(), "stop_computer_use", None)
            transport_closed = False

            async def stop_owned_runtime():
                nonlocal transport_closed
                try:
                    await state.connection.aclose()
                    transport_closed = getattr(state.connection, "close_confirmed", True) is True
                except Exception:  # noqa: BLE001 - shutdown must finish; the lease stays quarantined
                    # aclose marks its connection closed before terminating
                    # children. A failed close is not proof of termination.
                    _LOG.warning("Transport close failed during computer-use retirement (%s)",
                                 _runtime_label(state), exc_info=True)
                stopped = True
                if confirm_native is not None:
                    try:
                        stopped = await asyncio.wait_for(confirm_native(
                            state.runtime_spec, state.session_id, state.runtime_metadata), timeout=10)
                    except Exception:  # noqa: BLE001 - adapter code; unconfirmed keeps the quarantine
                        _LOG.warning("Native stop confirmation failed (%s)", _runtime_label(state), exc_info=True)
                        stopped = False
                return transport_closed and stopped

            cleanup = asyncio.create_task(stop_owned_runtime())

            async def confirm_stopped(*_):
                if not cleanup.done():
                    return await asyncio.shield(cleanup)
                if cleanup.cancelled():
                    return False
                try:
                    stopped = cleanup.result()
                except Exception:  # noqa: BLE001 - already logged where it failed
                    stopped = False
                if not transport_closed:
                    # AcpConnection shares and retries its owned shutdown
                    # task; a transient close failure must not wedge forever.
                    return await stop_owned_runtime()
                if stopped or confirm_native is None:
                    return stopped
                # A remote service can acknowledge its earlier cancellation
                # later; retry its check without trusting a closed transport.
                return await confirm_native(
                    state.runtime_spec, state.session_id, state.runtime_metadata)

            def cleanup_finished(task):
                if not task.cancelled():
                    try:
                        stopped = task.result()
                    except Exception:  # noqa: BLE001 - already logged where it failed
                        stopped = False
                    if stopped:
                        release_quarantine(held)

            quarantine(held, confirm_stopped, state.runtime_spec, state.session_id, state.runtime_metadata,
                       monitor=True)
            leases = []
            cleanup.add_done_callback(cleanup_finished)
            await asyncio.shield(cleanup)

        try:
            if resources:
                state.computer_waiting = True
                await reconcile_resources()
            for resource in sorted(set(resources)):
                lease = ResourceLease(resource)
                state.computer_waiting = True
                await lease.acquire()
                leases.append(lease)
            state.computer_waiting = False
            if state.interrupt_requested:
                return {"stopReason": "cancelled"}, None
            reference_hook = getattr(self._runtime_adapter(), "computer_use_reference", None)
            if leases and reference_hook is not None:
                from drivers.terminals.managed_process import process_identity
                reference = await reference_hook(state.runtime_spec, state.session_id, state.runtime_metadata)
                if state.interrupt_requested:
                    return {"stopReason": "cancelled"}, None
                reference = {**reference, "backendId": state.handle.backend_id,
                             "bindingId": state.handle.binding_id,
                             "conversationId": state.handle.conversation_id}
                pid = getattr(state.connection, "owned_process_id", None)
                transport = process_identity(pid) if pid is not None else None
                if pid is not None and transport is None:
                    raise RuntimeError("Cannot establish remote transport ownership")
                owner_id = "card:" + uuid.uuid4().hex
                for lease in leases:
                    lease.reserve_card(owner_id, reference, transport=transport)
                for lease in leases:
                    lease.mark_card_dispatched()
            started = True
            outcome = await invoke()
            result = outcome[0] if isinstance(outcome, tuple) and outcome else outcome
            cancelled = isinstance(result, Mapping) and result.get("stopReason") == "cancelled"
            if leases and (state.interrupt_requested or cancelled):
                # Some bridges resolve cancelled before their remote abort has
                # completed. The prompt receipt alone cannot release control.
                await retire_and_confirm()
            elif leases:
                # Remote engines may finish the parent reply while descendants
                # continue. Observe that execution tree without cancelling it.
                confirm = getattr(self._runtime_adapter(), "confirm_computer_use_complete", None)
                if confirm is not None:
                    try:
                        completed = await asyncio.wait_for(confirm(
                            state.runtime_spec, state.session_id, state.runtime_metadata), timeout=8)
                    except Exception:  # noqa: BLE001 - adapter code; unconfirmed goes to quarantine
                        _LOG.warning("Computer-use completion check failed (%s)", _runtime_label(state), exc_info=True)
                        completed = False
                    if state.interrupt_requested:
                        await retire_and_confirm()
                    elif completed is not True:
                        from drivers.computer_use.integration import quarantine
                        quarantine(leases, confirm, state.runtime_spec, state.session_id,
                                   state.runtime_metadata, monitor=True)
                        leases = []
            return outcome
        except BaseException:
            if started and leases:
                await retire_and_confirm()
            raise
        finally:
            state.computer_waiting = False
            for lease in reversed(leases):
                lease.release()

    async def _prompt(self, state: _RuntimeState, content: MessageInput) -> None:
        if state.interrupt_requested:
            # Cancel may arrive before this scheduled task starts. No prompt has
            # reached the engine yet, so cancel locally instead of starting work
            # after the engine already received session/cancel.
            self._push(state, state.translator.on_stop_reason("cancelled", interrupted=True))
            return
        params = {
            "sessionId": state.session_id,
            "prompt": _prompt_blocks(content, state.prompt_capabilities),
        }
        try:
            adapter = self._runtime_adapter()
            before = getattr(adapter, "before_runtime_prompt", None)
            after = getattr(adapter, "after_runtime_prompt", None)
            hook_args = (
                state.connection, state.runtime_spec, state.conversation,
                state.runtime_metadata, state.session_id, params,
            ) if before is not None or after is not None else ()
            async def invoke():
                token = await before(*hook_args) if before is not None else None
                if state.interrupt_requested:
                    return {"stopReason": "cancelled"}, None
                result = await self._guarded_prompt(state, params)
                completion = await after(*hook_args, result, token) if after is not None else None
                return result, completion
            result, completion_policy = await self._computer_prompt(state, invoke)
        except UnsupportedCapabilityError as exc:
            self._push(state, state.translator.on_error("skills.scope_changed", str(exc)))
            return
        except asyncio.CancelledError:  # pragma: no cover - stop_runtime 路径
            if state.interrupt_requested and not state.closed:
                self._push(state, state.translator.on_stop_reason("cancelled", interrupted=True))
            raise
        except AcpRpcError as exc:
            self._cancel_pending_interactions(state)
            self._auth_error(state.handle.binding_id, exc)
            self._push(state, state.translator.on_error(exc.code, exc.message, exc.data))
            return
        except AcpTransportError as exc:
            self._cancel_pending_interactions(state)
            self._push(state, state.translator.on_error("acp.transport", str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - the turn must reach a terminal event
            # Adapter or native response errors must finish the turn too. Raw
            # diagnostics may contain local paths, headers or credentials, so
            # the local log gets the redacted message and the card a fixed one.
            _LOG.warning("ACP turn failed (%s, %s): %s", self.backend_id, _runtime_label(state),
                         safe_exception_message(exc, environ=state.runtime_spec.build_env() if state.runtime_spec else None))
            self._cancel_pending_interactions(state)
            self._push(state, state.translator.on_error("agent.runtime_failed", "回复中断，请重试"))
            return
        finally:
            self._cancel_pending_interactions(state)
        stop_reason = result.get("stopReason") if isinstance(result, Mapping) else None
        if stop_reason == "end_turn" and not state.interrupt_requested:
            auth_state = completion_policy.get("auth_state") if isinstance(completion_policy, Mapping) else None
            if auth_state not in ("signed_in", "signed_out", "unknown"):
                auth_state = "signed_in"
            self._catalog[state.handle.binding_id] = replace(
                self._snapshot_from_session(state.session_result), auth_state=auth_state)
        self._push(
            state,
            state.translator.on_stop_reason(
                stop_reason, interrupted=state.interrupt_requested
            ),
        )

    async def interrupt(self, runtime: RuntimeHandle) -> None:
        state = self._state(runtime)
        capabilities = await self.get_capabilities()
        if not capabilities.card.interrupt:
            raise UnsupportedCapabilityError("该 agent 未声明中断能力")
        state.interrupt_requested = True
        self._cancel_pending_interactions(state)
        if state.computer_waiting and state.prompt_task is not None:
            state.prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.prompt_task
            return
        # ACP 的 cancel 是**通知**，没有回执：终态只会以 session/prompt 的
        # stopReason 形式回来（实测）。
        with contextlib.suppress(AcpTransportError):
            state.connection.notify("session/cancel", {"sessionId": state.session_id})
        task = state.prompt_task
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), self._cancel_grace)
        # A timeout is not evidence that the engine stopped. The host keeps its
        # stopping-unconfirmed state until the prompt result/transport says so.
