"""OpenClaw's ACP stream with native, session-scoped Gateway model controls.

The ACP bridge remains responsible for turns, tools, reasoning and approvals.
Only model discovery/selection uses the official ``gateway call`` CLI. That
entrypoint owns configuration/auth discovery and requests the least privilege
scope for each method. In particular, a model-only ``sessions.patch`` requests
operator.write, never operator.admin: native sticky model preferences therefore
cannot write an Agent or global default as a side effect of a Kaus selection.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from drivers.acp.client import AcpAgentSpec, AcpRpcError, AcpTransportError
from drivers.acp.driver import AcpDriver, _CatalogSnapshot, _RuntimeState
from drivers.base import ModelDescriptor, ModelRejectedError, RuntimeHandle, UnsupportedCapabilityError
from runtime.capability_matrix import ModelCapabilities, declare

_GATEWAY_METHODS = frozenset({"models.list", "sessions.resolve", "sessions.describe", "sessions.patch"})
_MODEL_OPTION = "kaus_gateway_model"
_PRIVATE_SNAPSHOT = "_kausOpenClawModels"


class OpenClawGateway:
    """Restricted native CLI RPC; no configuration or credential write methods."""

    def __init__(self, spec: AcpAgentSpec, *, timeout: float = 30.0) -> None:
        self.spec, self.timeout = spec, timeout
        try:
            index = spec.command.index("acp")
        except ValueError as exc:
            raise ValueError("OpenClaw 模型控制需要原生 acp 启动命令") from exc
        self.prefix = spec.command[:index]
        if not self.prefix:
            raise ValueError("OpenClaw 启动命令不完整")
        self.options: dict[str, str] = {}
        args = spec.command[index + 1:]
        i = 0
        while i < len(args):
            option, _, inline = args[i].partition("=")
            if option in {"--url", "--token", "--token-file", "--password", "--password-file", "--session", "--session-label"}:
                if inline:
                    self.options[option] = inline
                elif i + 1 < len(args):
                    i += 1
                    self.options[option] = args[i]
                else:
                    raise ValueError("OpenClaw 启动参数缺少取值")
            i += 1

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method not in _GATEWAY_METHODS:
            raise ValueError("Unsupported Gateway operation")
        if method == "sessions.patch" and set(params) - {"key", "agentId", "expectedSessionId", "expectedLifecycleRevision", "model"}:
            raise ValueError("Only session model selection is allowed")
        env = self.spec.build_env()
        env.update({"OPENCLAW_HIDE_BANNER": "1", "OPENCLAW_SUPPRESS_NOTES": "1", "NO_COLOR": "1"})
        # CLI Gateway has no --token-file flag. Keep explicit ACP credentials out
        # of child argv and error messages, and let native configuration resolve
        # all other auth sources. These values never enter a Kaus model/catalog.
        for option, variable in (("token", "OPENCLAW_GATEWAY_TOKEN"), ("password", "OPENCLAW_GATEWAY_PASSWORD")):
            direct = self.options.get(f"--{option}")
            source = self.options.get(f"--{option}-file")
            if source:
                try:
                    path = Path(source).expanduser()
                    if not path.is_absolute() and self.spec.cwd:
                        path = Path(self.spec.cwd) / path
                    direct = path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError) as exc:
                    raise AcpTransportError("OpenClaw Gateway 凭据文件不可读") from exc
            if direct:
                env[variable] = direct
        args = [*self.prefix, "gateway", "call", method, "--json", "--params", json.dumps(dict(params), ensure_ascii=False), "--timeout", str(int(self.timeout * 1000))]
        if self.options.get("--url"):
            # The explicit environment URL path accepts environment credentials;
            # --url intentionally requires --token/--password in argv instead.
            env["OPENCLAW_GATEWAY_URL"] = self.options["--url"]
        try:
            process = await asyncio.create_subprocess_exec(*args, cwd=self.spec.cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            raise AcpTransportError("OpenClaw Gateway 调用无法启动") from exc
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout + 5)
        except BaseException as exc:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if isinstance(exc, asyncio.TimeoutError):
                raise AcpTransportError("等待 OpenClaw Gateway 超时") from exc
            raise
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AcpTransportError("OpenClaw Gateway 未返回有效结果") from exc
        if process.returncode or not isinstance(result, dict):
            # The CLI may include config paths and auth diagnostics in failures.
            # Keep those in its native terminal, never echo arbitrary output.
            raise AcpRpcError(-32000, "OpenClaw Gateway 未接受操作，请检查连接、模型及原生权限")
        return result

    async def session_key(self, session_id: str, *, require_existing: bool = False) -> str:
        if self.options.get("--session-label") and not self.options.get("--session"):
            selector = {"label": self.options["--session-label"]}
        else:
            key = self.options.get("--session") or session_id
            try:
                uuid.UUID(key)
            except ValueError:
                if not self.options.get("--session") and not key.startswith(("agent:", "acp-bridge:")):
                    raise AcpTransportError("无法确定 OpenClaw 原生会话")
            else:
                if not self.options.get("--session"):
                    # Native ACP newSession creates this exact bridge key; load
                    # recovers it from its own event ledger on another process.
                    key = f"acp-bridge:{key}"
            selector = {"key": key}
        resolved = await self.call("sessions.resolve", {**selector, "allowMissing": not require_existing})
        if resolved.get("ok") is True and isinstance(resolved.get("key"), str):
            return resolved["key"]
        if not require_existing and "key" in selector and resolved.get("ok") is False and not resolved.get("candidates"):
            return selector["key"]
        raise AcpTransportError("OpenClaw 原生会话不存在或不唯一，未修改模型")


class OpenClawAcpDriver(AcpDriver):
    """Use the shared ACP lifecycle for both conversations and Group leaders."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            self.gateway: OpenClawGateway | None = OpenClawGateway(self.agent_spec, timeout=self._call_timeout)
        except ValueError:
            # Explicit non-native wrapper commands can still speak ordinary ACP.
            self.gateway = None

    def _absorb_initialize(self, result: Any) -> None:
        super()._absorb_initialize(result)
        if self.gateway is not None:
            self.quirks = replace(self.quirks, model_switch="config_option")
            self._capabilities = self._capabilities.model_copy(update={"models": ModelCapabilities.model_validate({
                **self._capabilities.models.model_dump(),
                "providers": declare("supported", verification="live"),
                "conversation_scoped": declare("supported", verification="live", note="OpenClaw Gateway sessions.patch with operator.write"),
            })})

    def _config_stamp(self, binding: Any) -> tuple:
        env = self.agent_spec.build_env()
        home = Path(env.get("OPENCLAW_HOME") or "~").expanduser()
        root = Path(env["OPENCLAW_STATE_DIR"]).expanduser() if env.get("OPENCLAW_STATE_DIR") else home / ".openclaw"
        paths = [Path(env["OPENCLAW_CONFIG_PATH"]).expanduser() if env.get("OPENCLAW_CONFIG_PATH") else root / "openclaw.json"]
        paths.extend(root.glob("agents/*/agent/models.json"))
        stamps = []
        for path in paths:
            try:
                stat = path.stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                stamps.append((str(path), None))
        return tuple(stamps)

    def group_context_isolation(self) -> bool:
        # A user-supplied fixed key/label makes every new ACP UUID point at one
        # native conversation. It cannot represent independent Group members.
        pinned = self.gateway and (self.gateway.options.get("--session") or self.gateway.options.get("--session-label"))
        return not pinned and super().group_context_isolation()

    async def start_runtime(self, conversation: Any, surface: Any, *, session_options: Any = None) -> Any:
        binding = self._binding_snapshot(conversation.agent_binding_id)
        group_role = binding.runtime_config.get("group_execution") if binding else None
        if (group_role or conversation.created_by_collaboration_id) and not self.group_context_isolation():
            raise UnsupportedCapabilityError("此启动配置固定了原生会话，无法为协作组建立独立上下文")
        return await super().start_runtime(conversation, surface, session_options=session_options)

    async def _new_session(self, connection: Any, options: Any) -> tuple[Any, Mapping[str, Any]]:
        result, params = await super()._new_session(connection, options)
        await self._augment_models(result, result.get("sessionId"))
        return result, params

    async def _resume_session(self, connection: Any, session_id: str, cwd: str) -> Any:
        result = await super()._resume_session(connection, session_id, cwd)
        await self._augment_models(result, session_id)
        return result

    async def _augment_models(self, result: Any, session_id: str) -> None:
        if self.gateway is None or not isinstance(result, dict) or not session_id:
            return
        try:
            key = await self.gateway.session_key(session_id)
            catalog, description = await asyncio.gather(
                self.gateway.call("models.list", {"sessionKey": key, "includeDetails": True, "view": "configured"}),
                self.gateway.call("sessions.describe", {"key": key}),
            )
            raw_models = catalog.get("models")
            if not isinstance(raw_models, list):
                raise AcpTransportError("OpenClaw Gateway 未提供模型目录")
            models: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in raw_models:
                if not isinstance(row, dict) or row.get("manualSelectionAllowed") is False:
                    continue
                provider, model = row.get("provider"), row.get("id")
                if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
                    continue
                model_id = f"{provider}/{model}"
                if model_id in seen:
                    continue
                seen.add(model_id)
                models.append({"model_id": model_id, "display_name": row.get("alias") or row.get("name") or model,
                               "provider_id": provider, "provider_label": provider,
                               "reasoning_levels": tuple(level["id"] for level in row["thinkingLevels"] if isinstance(level, dict) and isinstance(level.get("id"), str)) if isinstance(row.get("thinkingLevels"), list) else None,
                               "context_window": row.get("contextWindow") if isinstance(row.get("contextWindow"), int) and row["contextWindow"] > 0 else None})
            current = _current_model(description.get("session"))
            row = description.get("session")
            result[_PRIVATE_SNAPSHOT] = {"models": models, "current": current, "key": key, "exists": isinstance(row, dict),
                                        "sessionId": row.get("sessionId") if isinstance(row, dict) else None,
                                        "lifecycleRevision": row.get("lifecycleRevision") if isinstance(row, dict) else None}
            self._install_model_option(result)
        except (AcpRpcError, AcpTransportError, asyncio.TimeoutError) as exc:
            result[_PRIVATE_SNAPSHOT] = {"error": str(exc)}

    def _install_model_option(self, result: dict[str, Any]) -> None:
        snapshot = result.get(_PRIVATE_SNAPSHOT, {})
        models = snapshot.get("models", [])
        if not models:
            return
        options = [entry for entry in result.get("configOptions", []) if isinstance(entry, dict) and entry.get("id") != _MODEL_OPTION]
        options.append({"id": _MODEL_OPTION, "name": "Model", "category": "model", "type": "select",
                        "currentValue": snapshot.get("current") or "", "options": [
                            {"value": row["model_id"], "name": row["display_name"]} for row in models]})
        result["configOptions"] = options

    def _snapshot_from_session(self, result: Any) -> _CatalogSnapshot:
        snapshot = super()._snapshot_from_session(result)
        native = result.get(_PRIVATE_SNAPSHOT) if isinstance(result, Mapping) else None
        if not isinstance(native, Mapping):
            return snapshot
        if native.get("error"):
            return replace(snapshot, models=(), degraded=True, engine_default_available=False, diagnostics=(native["error"],))
        levels = snapshot.thought_levels or snapshot.mode_ids
        models = tuple(ModelDescriptor(**{**row, "reasoning_levels": row["reasoning_levels"] if row.get("reasoning_levels") is not None else levels}, is_current_provider=bool(native.get("current", "") and native["current"].startswith(f"{row['provider_id']}/"))) for row in native.get("models", []))
        return replace(snapshot, models=models, current_model_id=native.get("current"),
                       model_option_id=_MODEL_OPTION if models else None,
                       engine_default_available=True, degraded=False, diagnostics=())

    def _accept_config_update(self, state: _RuntimeState, result: Any) -> None:
        # Native thought/mode updates omit our model extension. Preserve its
        # scope and selection while accepting native reasoning options.
        if isinstance(result, dict) and _PRIVATE_SNAPSHOT not in result and _PRIVATE_SNAPSHOT in state.session_result:
            result = {**result, _PRIVATE_SNAPSHOT: state.session_result[_PRIVATE_SNAPSHOT]}
        if isinstance(result, dict):
            self._install_model_option(result)
        super()._accept_config_update(state, result)

    async def _set_model_config_option(self, state: _RuntimeState, model_id: str) -> None:
        if self.gateway is None:
            return await super()._set_model_config_option(state, model_id)
        native = state.session_result.get(_PRIVATE_SNAPSHOT, {})
        if model_id not in {row["model_id"] for row in native.get("models", [])}:
            raise AcpRpcError(-32602, "模型不在当前 OpenClaw 会话的可选目录中")
        if not native.get("exists") and not state.handle.metadata.get("resumed"):
            # ACP newSession initially owns only an event ledger, not a Gateway
            # row. Ask that exact ACP session to retain inherited usage reporting
            # before resolving a mutable Gateway identity. Catalog
            # probes remain read-only and an absent resumed row is never rebuilt.
            preference = next((entry for entry in state.session_result.get("configOptions", []) if isinstance(entry, dict) and entry.get("id") == "response_usage"), None)
            if not preference or preference.get("currentValue") != "inherit":
                raise AcpTransportError("OpenClaw 尚未建立可修改的原生会话")
            await state.connection.call("session/set_config_option", {"sessionId": state.session_id, "configId": "response_usage", "value": "inherit"}, timeout=self._call_timeout)
        # Cache only a Gateway-confirmed identity. The atomic expected identity
        # guards every later selection against resets without three CLI launches
        # on each dropdown change. A missing/stale row is never recreated here.
        key = native.get("key")
        if not native.get("sessionId"):
            key = await self.gateway.session_key(state.session_id, require_existing=True)
            described = await self.gateway.call("sessions.describe", {"key": key})
            session = described.get("session")
            if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
                raise AcpTransportError("OpenClaw 原生会话已变化，未修改模型")
            native.update({"key": key, "sessionId": session["sessionId"], "lifecycleRevision": session.get("lifecycleRevision")})
        params = {"key": key, "expectedSessionId": native["sessionId"], "model": model_id}
        if native.get("lifecycleRevision"):
            params["expectedLifecycleRevision"] = native["lifecycleRevision"]
        # A timeout/cancellation can happen after the native mutation commits.
        # Until confirmed, do not send another prompt with an unknown route.
        native["selectionPending"] = True
        reply = await self.gateway.call("sessions.patch", params)
        actual = _current_model(reply.get("resolved")) or _current_model(reply.get("entry"))
        if actual is None:
            actual = _current_model((await self.gateway.call("sessions.describe", {"key": key})).get("session"))
        if actual != model_id:
            raise AcpRpcError(-32000, "OpenClaw 未确认所选模型，未保存选择")
        native["current"] = actual
        native["exists"] = True
        native["selectionPending"] = False
        self._install_model_option(state.session_result)
        self._accept_config_update(state, state.session_result)

    async def _send_message_locked(self, runtime: RuntimeHandle, content: Any) -> None:
        state = self._state(runtime)
        if state.session_result.get(_PRIVATE_SNAPSHOT, {}).get("selectionPending"):
            raise ModelRejectedError("模型切换尚未确认", reason="请重新选择模型或重新连接会话后继续")
        await super()._send_message_locked(runtime, content)


def _current_model(row: Any) -> str | None:
    if not isinstance(row, Mapping):
        return None
    provider = row.get("providerOverride") or row.get("modelProvider") or row.get("provider")
    model = row.get("modelOverride") or row.get("model")
    return f"{provider}/{model}" if isinstance(provider, str) and provider and isinstance(model, str) and model else None
