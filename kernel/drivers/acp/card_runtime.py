"""Card Runtime lifecycle: one ACP process per conversation runtime.

``start_runtime`` spawns the process, creates or resumes the native session,
applies the conversation's selections before the first prompt and wires the
inbound callbacks; ``stop_runtime`` tears it down. Events reach the host
through the per-runtime queue (``events`` / ``_push``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import AsyncIterator, Literal, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from drivers.approval import is_host_approval
from drivers.error_text import safe_error_text, safe_exception_message
from drivers.acp.capabilities import (
    DEFAULT_THOUGHT_LEVEL,
    resolve_thought_level_id,
    resolve_conversation_mode_id,
)
from drivers.acp.client import AcpRpcError
from drivers.acp.translator import AcpTranslationContext, AcpTranslator
from drivers.acp.interactions import ClientInteractions
from drivers.base import (
    CreateSessionOptions,
    NativeSession,
    RuntimeHandle,
    RuntimeNotFoundError,
    UnsupportedCapabilityError,
)
from runtime.event_envelope import AgentEventEnvelope
from drivers.acp.runtime_state import DRIVER_VERSION, _RuntimeState, _SENTINEL, _now
from drivers.acp.catalog import _strip_effort_suffix, _mode_id_of
from drivers.acp.sessions import _require_session_id


class AcpCardRuntimeMixin:
    """Runtime start/stop and event delivery; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    async def start_runtime(
        self,
        conversation: Conversation,
        surface: Literal["card"],
        *,
        session_options: CreateSessionOptions | None = None,
    ) -> RuntimeHandle:
        if surface != "card":
            raise UnsupportedCapabilityError(
                "Driver 只负责 Card Surface；ACP 不提供站外 CLI 的启动信息"
            )
        from drivers.computer_use.settings import is_enabled, runtime_revision
        computer_use_enabled = is_enabled()
        computer_use_revision = runtime_revision()
        workspace_root = (session_options.workspace_root if session_options and session_options.workspace_root
                          else self._workspace_root_for(conversation.agent_binding_id))
        approval_mode = conversation.approval_mode or self._approval_mode_for(conversation.agent_binding_id)
        binding = self._binding_snapshot(conversation.agent_binding_id)
        role = binding.runtime_config.get('group_execution') if binding else None
        skill_plan = self._skill_plans.get(conversation.id)
        spec = skill_plan.spec if skill_plan and skill_plan.spec is not None else self.agent_spec
        runtime_adapter = self._runtime_adapter()
        runtime_metadata = dict(session_options.metadata if session_options else {"_meta": skill_plan.metadata} if skill_plan else {})
        restricted = runtime_metadata.get("_kausExecutionPolicy") == "context-only"
        if restricted:
            from drivers.workbench_execution import adapter_for
            adapter = adapter_for(getattr(self.preset, "restricted_execution_adapter", None))
            if adapter is None or not session_options or not session_options.workspace_root:
                raise UnsupportedCapabilityError("当前 Agent 尚未适配此模块的执行范围。")
            await self.get_capabilities()
            workspace_root = session_options.workspace_root
            spec, runtime_metadata = await adapter.prepare(self.agent_spec.model_copy(update={"cwd": workspace_root}),
                runtime_metadata, (self._backend_name, self._backend_version))
            runtime_metadata.pop("_kausComputerUse", None)
            skill_plan, runtime_adapter, role = None, None, None
        runtime_metadata["_kausWorkspaceRoot"] = self._resolve_cwd(workspace_root)
        if role:
            from drivers.group_execution import adapter_for
            adapter = adapter_for(self.preset.group_isolation_adapter if self.preset else None)
            if adapter is not None and not adapter.supported(self.agent_spec):
                raise UnsupportedCapabilityError("该引擎不支持组长上下文隔离")
            spec = adapter.prepare(spec, role) if adapter else spec
        old_computer_plan = runtime_metadata.get("_kausComputerUse", {})
        if not restricted and (not old_computer_plan or
                old_computer_plan.get("revision") != computer_use_revision):
            from drivers.computer_use.integration import server_fingerprint
            old_auto = {row["name"]: row["fingerprint"] for row in old_computer_plan.get("autoInjectedServers", [])}
            if old_auto:
                runtime_metadata["mcpServers"] = [server for server in runtime_metadata.get("mcpServers", [])
                    if old_auto.get(server.get("name")) != server_fingerprint(server)]
            from drivers.computer_use.integration import prepare_projection
            runtime_metadata = await prepare_projection(
                spec, runtime_adapter, binding, conversation,
                self._computer_use_effective.get(conversation.id) or EffectiveCapabilities(project_id=conversation.project_id),
                runtime_metadata, supports_mcp=bool(self.quirks.mcp_via_session_new or
                    (self.preset and self.preset.project_mcp_via_runtime)),
            )
        prepare = getattr(runtime_adapter, "prepare_runtime_spec", None)
        if prepare is not None:
            spec = await prepare(spec, conversation, runtime_metadata)
            if skill_plan is not None:
                skill_plan.spec = spec
        from drivers.computer_use.native_policy import prepare_spec as prepare_computer_policy
        spec = prepare_computer_policy(spec, runtime_adapter, runtime_metadata)
        if skill_plan is not None:
            skill_plan.spec = spec
        if session_options and session_options.workspace_root:
            spec = spec.model_copy(update={"cwd": self._resolve_cwd(workspace_root)})
        connection, _init = await self._connect(workspace_root=None if restricted else workspace_root, agent_spec=spec)
        try:
            if skill_plan is not None:
                self._remember_skill_projection(skill_plan)
            cwd = self._resolve_cwd(workspace_root)
            before_session = getattr(runtime_adapter, "before_runtime_session", None)
            if before_session is not None:
                runtime_metadata = await before_session(connection, spec, conversation, runtime_metadata)
            self._computer_use_plans[conversation.id] = dict(runtime_metadata.get("_kausComputerUse", {}))
            self._computer_use_contexts[conversation.id] = (spec, runtime_metadata)
            session_options = (session_options or CreateSessionOptions(title=conversation.title)).model_copy(update={"metadata": runtime_metadata})
            if conversation.native_session_id:
                result = await self._resume_session(
                    connection, conversation.native_session_id, cwd,
                    metadata=session_options.metadata if session_options else {"_meta": skill_plan.metadata} if skill_plan else None,
                )
                session_id = conversation.native_session_id
                resumed = True
            else:
                # 批次四十三：Session Host 装了能力投影器时，这里拿到的是一份
                # 已经翻好的 ``mcpServers``（见 :meth:`session_options_for`）；
                # 没装就与本批之前一模一样，只带标题。
                options = session_options or CreateSessionOptions(
                    title=conversation.title,
                    metadata={"_meta": skill_plan.metadata} if skill_plan else {},
                )
                if options.workspace_root is None and workspace_root is not None:
                    # AD-175：``_new_session`` 自己按 ``options.workspace_root``
                    # 算 cwd，而建会话这条路上谁都没往里填——于是**即使 Binding
                    # 写了工作目录**，``session/new`` 拿到的仍是 ``default_cwd``
                    # （后端进程的启动目录）。这里把上面已经解析好的那一个交出去，
                    # 让「起会话的 cwd」和「客户端 fs 的根」是同一个目录。
                    options = options.model_copy(
                        update={"workspace_root": workspace_root}
                    )
                result, _params = await self._new_session(connection, options)
                session_id = _require_session_id(result)
                resumed = False
            after_session = getattr(runtime_adapter, "after_runtime_session", None)
            if after_session is not None:
                await after_session(connection, spec, conversation, runtime_metadata, session_id)
            mode_id = None
            # Cache the engine's defaults before applying this conversation's overrides.
            self._remember_models(conversation.agent_binding_id, result, connection=connection)
            if is_host_approval(conversation.approval_mode):
                if not getattr(self.preset, "host_approval", False):
                    raise UnsupportedCapabilityError("当前引擎未提供审批委托。")
            elif conversation.approval_mode is not None:
                mode_id = resolve_conversation_mode_id(conversation.approval_mode, self._permission_modes(result), self.preset.approval_mode_ids or None if self.preset else None)
                if not self._supports_permissions(self._config_options(result)) or mode_id is None:
                    raise UnsupportedCapabilityError("引擎未提供这条会话的权限选项。")
                await self._set_permission_mode(connection, session_id, result, mode_id)
            if (conversation.reasoning_mode is not None and self.quirks.mode_semantics == "thought_level"
                    and not self._config_options(result).thought_option_id):
                mode_id = resolve_thought_level_id(conversation.reasoning_mode, self._available_modes(result))
                if not self.quirks.supports_set_mode or mode_id is None:
                    raise UnsupportedCapabilityError("引擎未提供这条会话的推理选项。")
                await connection.call("session/set_mode", {"sessionId": session_id, "modeId": mode_id}, timeout=self._call_timeout)
            elif conversation.approval_mode is None:
                mode_id = await self._apply_set_mode(
                    connection, session_id, result, approval_mode,
                    thought_level=self._binding_reasoning_effort(conversation.agent_binding_id) or DEFAULT_THOUGHT_LEVEL,
                )
            if conversation.execution_mode:
                config = self._config_options(result)
                mode_ids = tuple(_mode_id_of(mode) for mode in self._available_modes(result))
                if self.quirks.mode_semantics != "none" or not (config.mode_option_id or self.quirks.supports_set_mode) or conversation.execution_mode not in mode_ids:
                    raise UnsupportedCapabilityError("引擎未提供这个运行模式。")
                await self._set_session_mode(connection, session_id, result, conversation.execution_mode)
            if (role and conversation.model_id and self.quirks.model_switch == "none"
                    and conversation.model_id != self._snapshot_from_session(result).current_model_id):
                raise UnsupportedCapabilityError("当前引擎不支持切换到所选模型。")
        except AcpRpcError as exc:
            # AD-157：「引擎说你还没登录」是一个用户自己能修的状态，不是故障。
            # 认得出来就换成带修法的类型化异常，接入层据此回 400 `auth_required`
            # 并在时间线上留一条 run.failed；认不出来照旧原样上抛。
            await connection.aclose()
            # Keep the native cause in local diagnostics, not in the message UI.
            # Shutdown drains trailing stderr before this bounded, redacted log.
            logging.getLogger(__name__).warning(
                "ACP runtime initialization failed (%s): %s; stderr: %s",
                self.backend_id, safe_exception_message(exc, environ=spec.build_env()),
                safe_error_text(connection.stderr_text(), environ=spec.build_env(), limit=12000),
            )
            auth_error = self._auth_error(conversation.agent_binding_id, exc)
            if auth_error is not None:
                raise auth_error from exc
            raise
        except BaseException:
            await connection.aclose()
            raise

        self.session_ledger.setdefault(conversation.agent_binding_id, {}).setdefault(
            session_id,
            NativeSession(
                native_session_id=session_id,
                binding_id=conversation.agent_binding_id,
                title=conversation.title,
                head_id=session_id,
                created_at=_now(),
                updated_at=_now(),
            ),
        )

        self._runtime_counter += 1
        handle = RuntimeHandle(
            runtime_id=f"{self.backend_id}-runtime-{self._runtime_counter}",
            conversation_id=conversation.id,
            binding_id=conversation.agent_binding_id,
            backend_id=self.backend_id,
            native_session_id=session_id,
            started_at=_now(),
            # N §5.2 路径 B：Driver 私有的连接信息只能待在这里。
            metadata={
                "acpSessionId": session_id,
                "resumed": resumed,
                "workspaceRoot": cwd if workspace_root is not None else None,
                # 审批档到底切成了哪一档（没切成就是 None）——排错时第一个要看的。
                "acpModeId": mode_id,
                "clientFs": bool(
                    self.quirks.needs_client_fs and workspace_root is not None
                ),
                "computerUse": runtime_metadata.get("_kausComputerUse", {}),
            },
        )
        translator = AcpTranslator(
            AcpTranslationContext(
                project_id=conversation.project_id,
                conversation_id=conversation.id,
                agent_binding_id=conversation.agent_binding_id,
                backend_id=self.backend_id,
                session_id=session_id,
                # AD-174：run id 的跨进程唯一性在**这里**铸出来。每次接上会话
                # （新建或续接）都现铸一枚，翻译器把它拼进 run id。铸在 Driver
                # 侧是因为随机数是 IO，而翻译器不许有 IO。
                #
                # 刻意**不用** handle.runtime_id：它是进程内计数器
                # （`<backend>-runtime-<n>`），重启后一样从 1 开始，换一个同样
                # 的毛病不算修。
                run_token=uuid.uuid4().hex[:8],
                driver_version=DRIVER_VERSION,
                backend_version=self._backend_version,
            ),
            # AD-49 / AD-151：全量还是增量由怪癖表说了算，不在翻译器里写死。
            tool_output_cumulative=self.quirks.tool_update_cumulative,
        )
        state = _RuntimeState(
            handle=handle,
            conversation=conversation,
            connection=connection,
            translator=translator,
            session_id=session_id,
            workspace_root=(
                workspace_root if self.quirks.needs_client_fs else None
            ),
            approval_mode=approval_mode,
            available_modes=self._available_modes(result),
            session_result=dict(result),
            skill_plan=skill_plan,
            runtime_spec=spec,
            runtime_metadata=runtime_metadata,
            computer_use_enabled=computer_use_enabled,
            computer_use_revision=computer_use_revision,
            prompt_capabilities=dict(
                (_init.get("agentCapabilities") or {}).get("promptCapabilities") or {}
            ) if isinstance(_init, Mapping) and isinstance(_init.get("agentCapabilities"), Mapping) else {},
        )
        self._runtimes[handle.runtime_id] = state
        state.client_interactions = ClientInteractions(
            session_id=session_id, methods=getattr(self.preset, "client_methods", {}),
            forms=bool(getattr(self.preset, "elicitation_forms", False)), translator=translator,
            emit=lambda events: self._push(state, events), respond=connection.respond,
        )
        connection.on_notification = lambda method, params: self._on_notification(
            state, method, params
        )
        connection.on_request = lambda rpc_id, method, params: self._on_request(
            state, rpc_id, method, params
        )
        # Writable selections must reach the engine before its first prompt.
        # Engines without a selection RPC may still have an observed/default
        # model snapshot; that snapshot must not prevent their normal startup.
        selected_model = conversation.model_id
        if not selected_model:
            # The binding default applies to conversations without their own
            # model, mirroring the binding reasoning effort below.
            # Only when the live session exposes a model control; otherwise
            # effective-settings reports the binding default as unapplied.
            binding_model = binding.default_model_id if binding else None
            live = self._snapshot_from_session(state.session_result)
            if binding_model and binding_model != live.current_model_id and self._selection_applies(live):
                selected_model = binding_model
        if not selected_model and conversation.reasoning_mode and self.quirks.model_id_format == "effort_suffix":
            selected_model = self._catalog[conversation.agent_binding_id].current_model_id
        try:
            if selected_model and (self._config_options(state.session_result).model_option_id or self.quirks.model_switch != "none"):
                await self.set_conversation_model(handle, selected_model)
            config = self._config_options(state.session_result)
            wanted_effort = conversation.reasoning_mode or self._binding_reasoning_effort(conversation.agent_binding_id)
            if not wanted_effort and selected_model and self.quirks.model_id_format == "effort_suffix":
                base = _strip_effort_suffix(selected_model)
                if base != selected_model:
                    wanted_effort = selected_model[len(base) + 1:-1]
            if config.thought_option_id and wanted_effort:
                await self._set_reasoning_config(state, wanted_effort)
            for key, value in conversation.config_options.items():
                await self._apply_extra_config(connection, session_id, state.session_result, key, value)
        except BaseException:
            self._runtimes.pop(handle.runtime_id, None)
            state.closed = True
            await connection.aclose()
            raise
        self._push(state, translator.session_resumed() if resumed else translator.session_created())
        return handle

    def adopt_conversation(self, runtime: RuntimeHandle, conversation: Conversation) -> None:
        state = self._runtimes.get(runtime.runtime_id)
        if state is not None and state.conversation.id == conversation.id:
            state.conversation = conversation

    async def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEventEnvelope]:
        state = self._state(runtime)
        while True:
            item = await state.queue.get()
            if item is _SENTINEL:
                return
            yield item

    def runtime_reusable(self, runtime: RuntimeHandle) -> bool:
        state = self._runtimes.get(runtime.runtime_id)
        from drivers.computer_use.settings import runtime_revision
        return bool(state is not None and not state.closed and not state.transport_retired and (
            state.computer_use_revision is None or state.computer_use_revision == runtime_revision()))

    async def stop_runtime(self, runtime: RuntimeHandle) -> None:
        state = self._runtimes.pop(runtime.runtime_id, None)
        if state is None:
            return
        state.closed = True
        self._cancel_pending_interactions(state)
        task = state.prompt_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await state.connection.aclose()
        await state.queue.put(_SENTINEL)

    def _push(
        self, state: _RuntimeState, envelopes: Sequence[AgentEventEnvelope]
    ) -> None:
        for envelope in envelopes:
            state.queue.put_nowait(envelope)

    def _state(self, runtime: RuntimeHandle) -> _RuntimeState:
        state = self._runtimes.get(runtime.runtime_id)
        if state is None or state.closed:
            raise RuntimeNotFoundError(f"未知或已停止的 runtime：{runtime.runtime_id!r}")
        return state
