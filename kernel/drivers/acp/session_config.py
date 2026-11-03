"""Session-level configuration: model, reasoning, permission and execution mode.

Maps Kaus conversation settings onto what the live session exposes
(``configOptions``, ``session/set_model``, ``session/set_mode``) and keeps the
runtime's ``session_result`` in step with every confirmed change (AD-158).
Branches follow the quirk table (:class:`~drivers.acp.presets.AgentQuirks`),
never an agent's name.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from app.conversations.models import Conversation
from app.conversations.configuration import validate_config_values
from drivers.approval import host_approval_options, is_host_approval
from app.projects.models import AgentBinding
from drivers.acp.capabilities import (
    CONFIG_CATEGORY_MODEL,
    DEFAULT_THOUGHT_LEVEL,
    config_options_from_session_result,
    conversation_permission_options,
    available_modes,
    resolve_mode_id,
    resolve_thought_level_id,
    resolve_conversation_mode_id,
)
from drivers.acp.client import AcpConnection, AcpRpcError, AcpTransportError
from drivers.base import (
    ModelRejectedError,
    RuntimeHandle,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
    turn_already_running_hint,
)
from drivers.acp.runtime_state import _CatalogSnapshot, _RuntimeState
from drivers.acp.catalog import _mode_id_of, _strip_effort_suffix


class AcpSessionConfigMixin:
    """Session option mapping; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    def _option_ids(self) -> dict[str, str | None]:
        """预设登记的「审批档 / 思考档」对应的 configOption id；没有预设就都没有。"""
        if self.preset is None:
            return {"approval_option_id": None, "thought_option_id": None}
        return {
            "approval_option_id": self.preset.approval_option_id,
            "thought_option_id": self.preset.thought_option_id,
        }

    def _config_options(self, result: Any):
        return config_options_from_session_result(result, **self._option_ids())

    def _permission_modes(self, result: Any) -> tuple[Any, ...]:
        config = self._config_options(result)
        return (config.approval_definitions or config.approval_ids) if config.approval_option_id else self._available_modes(result)

    def _available_modes(self, result: Any) -> tuple[Any, ...]:
        return available_modes(result, **self._option_ids())

    def _supports_permissions(self, config) -> bool:
        return bool(config.approval_option_id or (self.quirks.mode_semantics == "approval" and (self.quirks.supports_set_mode or config.mode_option_id)))

    def conversation_controls(self, binding: AgentBinding) -> dict[str, Any]:
        return self._controls_from_snapshot(binding, self._catalog.get(binding.id))

    def runtime_conversation_controls(self, runtime: RuntimeHandle, binding: AgentBinding) -> dict[str, Any]:
        state = self._state(runtime)
        return self._controls_from_snapshot(binding, self._snapshot_from_session(state.session_result))

    def _controls_from_snapshot(self, binding: AgentBinding, snapshot: _CatalogSnapshot | None) -> dict[str, Any]:
        modes = (snapshot.mode_definitions or snapshot.mode_ids) if snapshot is not None else ()
        permission_options = (snapshot.approval_definitions or snapshot.approval_ids) if snapshot and snapshot.approval_option_id else modes
        choices = []
        if (snapshot and snapshot.approval_option_id) or ((self.quirks.supports_set_mode or (snapshot and snapshot.mode_option_id)) and self.quirks.mode_semantics == "approval"):
            choices = conversation_permission_options(permission_options, self.preset.approval_mode_ids or None if self.preset else None)
        delegated = not choices and bool(getattr(self.preset, "host_approval", False))
        if delegated:
            choices = host_approval_options()
        native_default = (snapshot.current_approval_id if snapshot.approval_option_id else snapshot.current_mode_id) if snapshot else None
        if binding.runtime_config.get("approval_mode") in ("ask", "auto", "deny"):
            native_default = resolve_mode_id(self._approval_mode_for(binding.id), permission_options, self.preset.approval_mode_ids or None if self.preset else None) or native_default
        approval_default = next((option["id"] for option in choices if option.get("nativeId") == native_default), None)
        return {
            "model": bool(snapshot and snapshot.model_option_id) or (
                self.quirks.model_switch != "none" and (snapshot is None or bool(snapshot.models))),
            "reasoning": (self.quirks.model_id_format == "effort_suffix" and not (snapshot and snapshot.model_option_id))
            or (self.quirks.supports_set_mode and self.quirks.mode_semantics == "thought_level")
            or bool(snapshot and snapshot.thought_option_id and snapshot.thought_levels),
            "reasoningDefault": snapshot.current_thought_level if snapshot else None,
            "reasoningLevels": list((snapshot.thought_levels or (snapshot.mode_ids if self.quirks.mode_semantics == "thought_level" else ())) if snapshot else ()),
            "approvalModes": [option["id"] for option in choices],
            "approvalOptions": choices,
            "approvalDefault": "host:ask" if delegated else approval_default,
            "configOptions": list(snapshot.config_options) if snapshot else [],
            "executionModes": list(modes) if snapshot and (snapshot.mode_option_id or self.quirks.supports_set_mode) and self.quirks.mode_semantics == "none" else [],
            "executionDefault": snapshot.current_mode_id if snapshot and (snapshot.mode_option_id or self.quirks.supports_set_mode) and self.quirks.mode_semantics == "none" else None,
        }

    async def _apply_set_mode(
        self,
        connection: AcpConnection,
        session_id: str,
        session_result: Any,
        mode: str,
        *,
        thought_level: str,
    ) -> str | None:
        """``session/new`` 之后发一次 ``session/set_mode``。

        四条纪律（第 3 件 ① / AD-151 / AD-158）：

        1. **按怪癖表分支，不按 agent 名字。** 预设说不支持就一次也不发。
        2. **切的是哪一种档由 ``mode_semantics`` 说了算。** 真机上有一类实现把这
           个方法当**思考强度**的开关用（``off/minimal/low/medium/high/xhigh``），
           那时映射的是 Binding 的推理强度，**一个字都不碰审批**——把「自动批准」
           翻译成「多想一会儿」是一次静默的语义错位，界面上完全看不出来。
        3. **只发 agent 自己列过的 modeId。** ``modes.availableModes`` 里没有对应
           项就不发并记一条 warning——发一个它不认识的 id，换来的是一次 -32602
           加一个谁也不知道现在处于哪一档的会话。
        4. **失败不致命。** 档没切成是「这条会话仍在 agent 的默认档」，不是
           「这条会话开不起来」；记 warning，让界面能如实说明。
        """
        semantics = self.quirks.mode_semantics
        config = self._config_options(session_result)
        if not config.approval_option_id and (semantics == "none" or (not self.quirks.supports_set_mode and not config.mode_option_id)):
            return None
        if semantics == "thought_level" and config.thought_option_id:
            return None  # Applied after model selection using its own config id.
        wanted = thought_level if semantics == "thought_level" else mode
        available = self._available_modes(session_result) if semantics == "thought_level" else self._permission_modes(session_result)
        if not available:
            self._warn(
                f"set_mode_unavailable：agent 没有报 modes.availableModes（{wanted}）"
            )
            return None
        mode_id = (
            resolve_thought_level_id(wanted, available)
            if semantics == "thought_level"
            else resolve_mode_id(wanted, available, self.preset.approval_mode_ids or None if self.preset else None)
        )
        if mode_id is None:
            self._warn(
                f"set_mode_no_match：{semantics} 档 {wanted} 在 agent 的 "
                "availableModes 里没有对应项"
            )
            return None
        try:
            if semantics == "thought_level":
                await self._set_session_mode(connection, session_id, session_result, mode_id)
            else:
                await self._set_permission_mode(connection, session_id, session_result, mode_id)
        except (AcpRpcError, AcpTransportError) as exc:
            self._warn(f"set_mode_failed：{wanted} → {mode_id} 被拒（{exc}）")
            return None
        return mode_id

    async def set_conversation_model(
        self, runtime: RuntimeHandle, model_id: str
    ) -> str:
        state = self._state(runtime)
        async with state.control_lock:
            if state.prompt_task is not None and not state.prompt_task.done():
                raise TurnAlreadyRunningError("上一轮尚未结束，请结束后再切换模型。", failure=turn_already_running_hint())
            return await self._set_conversation_model_locked(runtime, model_id)

    async def set_conversation_selection(
        self, runtime: RuntimeHandle, conversation: Conversation
    ) -> Conversation:
        state = self._state(runtime)
        async with state.control_lock:
            if state.prompt_task is not None and not state.prompt_task.done():
                raise TurnAlreadyRunningError("上一轮尚未结束。", failure=turn_already_running_hint())
            previous = state.conversation
            config_revision = state.config_revision
            permission_changed = conversation.approval_mode != previous.approval_mode or state.permission_pending
            effort_changed = conversation.reasoning_mode != previous.reasoning_mode
            model_changed = conversation.model_id != previous.model_id
            execution_changed = conversation.execution_mode != previous.execution_mode
            extra_changed = conversation.config_options != previous.config_options or bool(state.config_pending)
            if extra_changed:
                if permission_changed or effort_changed or model_changed or execution_changed:
                    raise UnsupportedCapabilityError("请分别修改扩展设置和其他设置。")
                changed = {key: value for key, value in conversation.config_options.items() if key not in previous.config_options or previous.config_options[key] != value or key in state.config_pending}
                if len(changed) != 1:
                    raise UnsupportedCapabilityError("请逐项修改扩展设置。")
                key, value = next(iter(changed.items()))
                try:
                    await self._apply_extra_config(state.connection, state.session_id, state.session_result, key, value)
                except AcpRpcError as exc:
                    raise ModelRejectedError("设置未生效", reason=str(exc)) from exc
                except (AcpTransportError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    state.config_pending.add(key)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ModelRejectedError("设置尚未确认", reason="请重新选择该设置后继续。") from exc
                self._accept_config_update(state, state.session_result)
                state.config_pending.discard(key)
                conversation = self._confirmed_config_values(conversation, state.session_result)
                state.conversation = conversation
                return conversation
            config = self._config_options(state.session_result)
            if execution_changed:
                if permission_changed or effort_changed or model_changed:
                    raise UnsupportedCapabilityError("请分别修改运行模式和其他设置。")
                mode_ids = tuple(_mode_id_of(mode) for mode in self._available_modes(state.session_result))
                if self.quirks.mode_semantics != "none" or not (config.mode_option_id or self.quirks.supports_set_mode) or conversation.execution_mode not in mode_ids:
                    raise UnsupportedCapabilityError("引擎未提供这个运行模式。")
                try:
                    await self._set_session_mode(state.connection, state.session_id, state.session_result, conversation.execution_mode)
                    self._accept_config_update(state, state.session_result)
                except (AcpRpcError, AcpTransportError) as exc:
                    raise ModelRejectedError("运行模式未生效", reason=str(exc)) from exc
                state.conversation = conversation
                return conversation
            if model_changed and effort_changed and config.thought_option_id:
                raise UnsupportedCapabilityError("请分别修改模型和推理设置。")
            if model_changed and effort_changed and self.quirks.mode_semantics == "thought_level":
                raise UnsupportedCapabilityError("请分别修改模型和推理设置。")
            if permission_changed and is_host_approval(conversation.approval_mode):
                if not getattr(self.preset, "host_approval", False):
                    raise UnsupportedCapabilityError("当前引擎未提供审批委托。")
                state.approval_mode = conversation.approval_mode
                state.permission_pending = False
            elif permission_changed:
                if not self._supports_permissions(config):
                    raise UnsupportedCapabilityError("当前引擎不支持会话权限切换。")
                mode_id = resolve_conversation_mode_id(conversation.approval_mode, self._permission_modes(state.session_result), self.preset.approval_mode_ids or None if self.preset else None)
                if mode_id is None:
                    raise UnsupportedCapabilityError("引擎未提供这个权限选项。")
                try:
                    await self._set_permission_mode(state.connection, state.session_id, state.session_result, mode_id)
                    self._accept_config_update(state, state.session_result)
                except AcpRpcError as exc:
                    raise ModelRejectedError("权限设置未生效", reason=str(exc)) from exc
                except (AcpTransportError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    state.permission_pending = True
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ModelRejectedError("权限切换尚未确认", reason="请重新选择权限后继续。") from exc
                state.approval_mode = conversation.approval_mode
                state.permission_pending = False
            elif effort_changed and config.thought_option_id:
                await self._set_reasoning_config(state, conversation.reasoning_mode)
            elif effort_changed and self.quirks.mode_semantics == "thought_level":
                mode_id = resolve_thought_level_id(conversation.reasoning_mode, state.available_modes)
                if mode_id is None:
                    raise UnsupportedCapabilityError("引擎未提供这个推理档位。")
                try:
                    await state.connection.call("session/set_mode", {"sessionId": state.session_id, "modeId": mode_id}, timeout=self._call_timeout)
                except (AcpRpcError, AcpTransportError) as exc:
                    raise ModelRejectedError("推理设置未生效", reason=str(exc)) from exc
            elif model_changed or effort_changed:
                if effort_changed and self.quirks.model_id_format != "effort_suffix":
                    raise UnsupportedCapabilityError("当前引擎不支持会话推理切换。")
                model_id = conversation.model_id
                if not model_id:
                    snapshot = self._catalog.get(state.handle.binding_id)
                    model_id = snapshot.current_model_id if snapshot is not None else None
                if not model_id:
                    raise UnsupportedCapabilityError("请先选择模型。")
                # Native config values are opaque. Legacy model/effort encoding
                # belongs only to the session/set_model transport below.
                await self._set_conversation_model_locked(runtime, model_id, reasoning_mode=conversation.reasoning_mode)
            if model_changed and state.config_revision != config_revision:
                applied = self._config_options(state.session_result)
                if applied.thought_option_id and applied.current_thought_level:
                    conversation = conversation.evolve(reasoning_mode=applied.current_thought_level)
                elif config.thought_option_id and not applied.thought_option_id:
                    conversation = conversation.evolve(reasoning_mode=None)
            conversation = self._confirmed_config_values(conversation, state.session_result)
            state.conversation = conversation
            return conversation

    async def _set_conversation_model_locked(
        self, runtime: RuntimeHandle, model_id: str, *, reasoning_mode: str | None = None
    ) -> str:
        """Apply a native session selector and preserve rejection diagnostics.

        Standard configOptions take precedence over legacy transport quirks;
        only verified legacy transports use session/set_model or mode IDs.
        """
        state = self._state(runtime)
        switch = "config_option" if self._config_options(state.session_result).model_option_id else self.quirks.model_switch
        if switch == "none":
            raise UnsupportedCapabilityError(
                "这台引擎没有会话级换模型的入口（session/set_model 与 "
                "session/set_config_option 两条路都不通）"
            )
        try:
            if switch == "config_option":
                await self._set_model_config_option(state, model_id)
            else:
                result = await state.connection.call(
                    "session/set_model",
                    {
                        "sessionId": state.session_id,
                        "modelId": self._wire_model_id(state, model_id, reasoning_mode=reasoning_mode),
                    },
                    timeout=self._call_timeout,
                )
                if isinstance(result, Mapping) and isinstance(result.get("configOptions"), list):
                    self._accept_config_update(state, result)
        except AcpRpcError as exc:
            raise ModelRejectedError(
                f"引擎拒绝了模型 {model_id!r}", reason=exc.message
            ) from exc
        except AcpTransportError as exc:
            raise ModelRejectedError(
                f"没能把模型 {model_id!r} 发给引擎", reason=str(exc)
            ) from exc
        return model_id

    async def _set_model_config_option(
        self, state: _RuntimeState, model_id: str
    ) -> None:
        """``session/set_config_option`` 那条路（AD-158）。

        两条纪律：

        1. **值原样送回去。** 真机上这一项的取值有普通 id，也有
           ``["deepseek-official","deepseek-v4-flash"]`` 这种 JSON 元组字符串。
           我们不解析、不重排、不拼装——目录里那个 ``value`` 是引擎自己给的，
           改动它的任何一个字符都只会换来一次拒绝。
        2. **optionId 按 ``category=model`` 那一项的 ``id`` 发**，问不出来才落
           ``"model"``（取证到的四家都叫这个名字，但那是巧合不是协议）。
        """
        snapshot = self._snapshot_from_session(state.session_result) if getattr(state, "session_result", None) else self._catalog.get(state.handle.binding_id)
        # Existing conversations may store a legacy combined selection. Only
        # decode it when the new catalog confirms the base id and effort.
        if snapshot and self.quirks.model_id_format == "effort_suffix" and snapshot.thought_option_id:
            ids = {model.model_id for model in snapshot.models}
            base = _strip_effort_suffix(model_id)
            if model_id not in ids and base in ids and model_id[len(base) + 1:-1] in snapshot.thought_levels:
                model_id = base
        option_id = (
            snapshot.model_option_id
            if snapshot is not None and snapshot.model_option_id
            else CONFIG_CATEGORY_MODEL
        )
        await self._set_runtime_config(state, option_id, model_id)

    async def _set_runtime_config(self, state: _RuntimeState, option_id: str, value: str) -> None:
        result = await self._call_config_option(state.connection, state.session_id, option_id, value)
        if isinstance(result, Mapping) and isinstance(result.get("configOptions"), list):
            self._accept_config_update(state, result)
            actual = next((entry.get("currentValue") for entry in result["configOptions"]
                           if isinstance(entry, Mapping) and entry.get("id") == option_id), None)
            if actual != value:
                raise AcpTransportError("引擎未确认所选配置")
        elif getattr(state, "session_result", None):
            # Older engines acknowledge without returning the updated catalog.
            for entry in state.session_result.get("configOptions", []):
                if isinstance(entry, dict) and entry.get("id") == option_id:
                    entry["currentValue"] = value
            self._accept_config_update(state, state.session_result)

    async def _set_reasoning_config(self, state: _RuntimeState, value: str | None) -> None:
        config = self._config_options(state.session_result)
        if not config.thought_option_id or value not in config.thought_levels:
            raise UnsupportedCapabilityError("引擎未提供这个推理档位。")
        try:
            await self._set_runtime_config(state, config.thought_option_id, value)
        except (AcpRpcError, AcpTransportError) as exc:
            raise ModelRejectedError("推理设置未生效", reason=str(exc)) from exc

    async def _apply_extra_config(self, connection, session_id: str, result: dict, key: str, value: str | bool) -> None:
        options = list(self._config_options(result).config_options)
        try:
            validate_config_values({key: value}, options)
        except ValueError as exc:
            raise UnsupportedCapabilityError(str(exc)) from exc
        reply = await self._call_config_option(connection, session_id, key, value)
        entries = reply.get("configOptions") if isinstance(reply, Mapping) else None
        if isinstance(entries, list):
            actual = next((item.get("currentValue") for item in entries if isinstance(item, Mapping) and item.get("id") == key), None)
            if type(actual) is not type(value) or actual != value:
                raise AcpTransportError("引擎未确认所选配置")
            result["configOptions"] = entries
        else:
            raise AcpTransportError("引擎未返回配置状态")

    async def _set_permission_mode(self, connection: AcpConnection, session_id: str, result: dict, mode_id: str) -> None:
        config = self._config_options(result)
        if not config.approval_option_id:
            await self._set_session_mode(connection, session_id, result, mode_id)
            return
        reply = await self._call_config_option(connection, session_id, config.approval_option_id, mode_id)
        self._accept_mode_reply(result, reply, mode_id, config.approval_option_id)

    @staticmethod
    def _accept_mode_reply(result: dict, reply: Any, mode_id: str, option_id: str | None = None) -> None:
        # An authoritative catalog must confirm the requested value. Never
        # overwrite a refusal/stale reply with our desired selection.
        if option_id and isinstance(reply, Mapping) and isinstance(reply.get("configOptions"), list):
            actual = next((entry.get("currentValue") for entry in reply["configOptions"]
                           if isinstance(entry, Mapping) and entry.get("id") == option_id), None)
            if actual != mode_id:
                raise AcpTransportError("引擎未确认所选权限或模式")
            result["configOptions"] = reply["configOptions"]
        else:
            modes = reply.get("modes") if isinstance(reply, Mapping) else None
            if isinstance(modes, Mapping) and modes.get("currentModeId") != mode_id:
                raise AcpTransportError("引擎未确认所选权限或模式")
            for entry in result.get("configOptions", []):
                if option_id and isinstance(entry, dict) and entry.get("id") == option_id:
                    entry["currentValue"] = mode_id

    async def _set_session_mode(self, connection: AcpConnection, session_id: str, result: dict, mode_id: str) -> None:
        config = self._config_options(result)
        if config.mode_option_id and self.quirks.mode_semantics != "thought_level":
            try:
                reply = await self._call_config_option(connection, session_id, config.mode_option_id, mode_id)
            except AcpRpcError as exc:
                if exc.code != -32601 or not self.quirks.supports_set_mode:
                    raise
                reply = await connection.call("session/set_mode", {"sessionId": session_id, "modeId": mode_id}, timeout=self._call_timeout)
            self._accept_mode_reply(result, reply, mode_id, config.mode_option_id)
        else:
            reply = await connection.call("session/set_mode", {"sessionId": session_id, "modeId": mode_id}, timeout=self._call_timeout)
            self._accept_mode_reply(result, reply, mode_id)
        if isinstance(result.get("modes"), dict):
            result["modes"]["currentModeId"] = mode_id

    def _accept_config_update(self, state: _RuntimeState, result: Mapping) -> None:
        state.session_result.update(result)
        state.config_revision += 1
        state.available_modes = self._available_modes(state.session_result)

    def _confirmed_config_values(self, conversation: Conversation, result: Mapping) -> Conversation:
        current = {item["id"]: item["currentValue"] for item in self._config_options(result).config_options}
        return conversation.evolve(config_options={key: current[key] for key in conversation.config_options if key in current})

    async def _call_config_option(self, connection: AcpConnection, session_id: str, option_id: str, value: str | bool) -> Any:
        # ACP 正式字段 configId 优先；仅 -32602 协商旧字段，
        # 其他错误原样上抛，避免重复执行可能已经生效的变更。
        shapes: list[dict[str, Any]] = [
            {"sessionId": session_id, "configId": option_id, "value": value},
            {"sessionId": session_id, "optionId": option_id, "value": value},
            {
                "sessionId": session_id,
                "configOptionId": option_id,
                "value": value,
            },
            {"sessionId": session_id, "id": option_id, "value": value},
        ]
        if isinstance(value, bool):
            for params in shapes:
                params["type"] = "boolean"
        first_error: AcpRpcError | None = None
        for params in shapes:
            try:
                return await connection.call(
                    "session/set_config_option", params, timeout=self._call_timeout
                )
            except AcpRpcError as exc:
                if exc.code != -32602:
                    raise
                if first_error is None:
                    first_error = exc
        assert first_error is not None  # noqa: S101 - shapes 非空
        # 与 :func:`~drivers.acp.client.call_with_param_shapes` 的**唯一**区别：
        # 那边抛最后一个错，这边抛第一个。理由是这条路上 -32602 有两种来源——
        # 「字段名不对」与「这个值不在 options[] 里」——而第一种形状是我们相信
        # 对的那一种，它那句话才是用户能看懂的原因（«unknown value: …»）。抛最后
        # 一个错，用户看到的会是「缺字段 optionId」这种与他无关的话。
        raise first_error

    def _wire_model_id(self, state: _RuntimeState, model_id: str, *, reasoning_mode: str | None = None) -> str:
        """要发给 ``session/set_model`` 的那个 id（AD-158）。

        ``model_id_format == "effort_suffix"`` 的引擎只认 ``modelId[effort]``：
        目录里已经带后缀的原样发，没带的按这条会话的推理档补一个（会话没写就用
        Binding 的，再没有就 :data:`DEFAULT_THOUGHT_LEVEL`）。补出来的那个档是
        **这条会话此刻的档**，不是一个写死的常量——否则用户把推理调到 high、
        换个模型又被悄悄打回 medium。
        """
        if self.quirks.model_id_format != "effort_suffix":
            return model_id
        effort = reasoning_mode or state.conversation.reasoning_mode
        if effort:
            return f"{_strip_effort_suffix(model_id)}[{effort}]"
        if model_id.endswith("]") and "[" in model_id:
            return model_id
        return f"{model_id}[{self._reasoning_effort_for(state)}]"

    def _reasoning_effort_for(self, state: _RuntimeState) -> str:
        conversation_effort = state.conversation.reasoning_mode
        if isinstance(conversation_effort, str) and conversation_effort:
            return conversation_effort
        binding_effort = self._binding_reasoning_effort(state.handle.binding_id)
        return binding_effort or DEFAULT_THOUGHT_LEVEL

    def _binding_reasoning_effort(self, binding_id: str) -> str | None:
        """Binding 的通用推理强度。读不到就是 ``None``（调用方决定缺省）。"""
        binding = self._binding_snapshot(binding_id)
        if binding is None:
            return None
        value = binding.runtime_config.get("reasoning_effort")
        return value if isinstance(value, str) and value else None
