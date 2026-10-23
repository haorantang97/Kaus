"""OpenClaw's ACP stream with native, session-scoped Gateway controls.

The ACP bridge remains responsible for turns, tools, reasoning and approvals.
Model and permission selection use the official CLI RPC module through a warm
sidecar when compatible, with the native CLI as fallback. Native code owns configuration/auth discovery and requests the least privilege
scope for each method. In particular, a model-only ``sessions.patch`` requests
operator.write, never operator.admin: native sticky model preferences therefore
cannot write an Agent or global default as a side effect of a Kaus selection.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from drivers.acp.client import AcpAgentSpec, AcpRpcError, AcpTransportError
from drivers.acp.driver import AcpDriver, _CatalogSnapshot, _RuntimeState
from drivers.base import ModelDescriptor, ModelRejectedError, RuntimeHandle, UnsupportedCapabilityError
from runtime.capability_matrix import ModelCapabilities, declare
from .gateway_transport import NativeGatewayTransport, sidecar_spec

_GATEWAY_METHODS = frozenset({"models.list", "sessions.resolve", "sessions.describe", "sessions.patch", "chat.history"})
_MODEL_OPTION = "kaus_gateway_model"
_PERMISSION_OPTION = "kaus_gateway_permission"
_PERMISSION_OPTIONS = (
    ("inherit", "Engine default"), ("read-only", "Read only"),
    ("guarded", "Guarded"), ("workspace", "Workspace"), ("full", "Full access"),
)
_PRIVATE_SNAPSHOT = "_kausOpenClawModels"


class OpenClawGateway:
    """Restricted native CLI RPC; no configuration or credential write methods."""

    allowed_methods = _GATEWAY_METHODS

    def __init__(self, spec: AcpAgentSpec, *, timeout: float = 30.0) -> None:
        self.spec, self.timeout = spec, timeout
        self.transport = NativeGatewayTransport()
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
        if method not in self.allowed_methods:
            raise ValueError("Unsupported Gateway operation")
        if method == "sessions.patch":
            self.validate_session_patch(params)
        env = self.connection_environment()
        env.update({"OPENCLAW_HIDE_BANNER": "1", "OPENCLAW_SUPPRESS_NOTES": "1", "NO_COLOR": "1"})
        args = [*self.prefix, "gateway", "call", method, "--json", "--params", json.dumps(dict(params), ensure_ascii=False), "--timeout", str(int(self.timeout * 1000))]
        warmed = sidecar_spec(self.spec, self.prefix, env)
        if warmed is not None:
            try:
                return await self.transport.call(warmed, method, params, self.timeout)
            except asyncio.CancelledError:
                raise
            except (AcpRpcError, AcpTransportError, OSError, asyncio.TimeoutError) as exc:
                raise AcpRpcError(-32000, "OpenClaw Gateway 未接受操作，请检查连接、模型及原生权限") from exc
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

    def validate_session_patch(self, params):
        if set(params) - {"key", "agentId", "expectedSessionId", "expectedLifecycleRevision", "model", "permissionMode", "expectedPermissionMode"}:
            raise ValueError("Only session model or permission selection is allowed")
        if "model" in params and "permissionMode" in params:
            raise ValueError("Model and permission changes must be separate")
        if "permissionMode" in params and params["permissionMode"] not in {None, "read-only", "guarded", "workspace", "full"}:
            raise ValueError("Unknown session permission mode")

    def connection_environment(self) -> dict[str, str]:
        """Resolve the ACP target once for RPC and private terminal handoff."""
        env = self.spec.build_env()
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
        if self.options.get("--url"):
            # The explicit environment URL path accepts environment credentials;
            # --url intentionally requires --token/--password in argv instead.
            env["OPENCLAW_GATEWAY_URL"] = self.options["--url"]
        return env

    async def close(self):
        await self.transport.close()

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

    # Connection-level declaration: source sessions retain native tool policy.
    # This is metadata only; displaying settings does not query a Gateway.
    native_computer_use_permission_control = "agent"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_gateways: dict[str, OpenClawGateway] = {}
        try:
            self.gateway: OpenClawGateway | None = OpenClawGateway(self.agent_spec, timeout=self._call_timeout)
        except ValueError:
            # Explicit non-native wrapper commands can still speak ordinary ACP.
            self.gateway = None
        if self.preset is not None:
            self.preset = replace(self.preset, approval_option_id=_PERMISSION_OPTION,
                                  approval_mode_ids={"ask": "guarded", "auto": "workspace", "bypass": "full", "read_only": "read-only"})

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

    @staticmethod
    def _display_tool_call(state, call):
        """Translate the ACP display title without touching tool invocation data."""
        plan = state.skill_plan
        title = call.get('title')
        if plan is None or not isinstance(title, str):
            return call
        native, separator, remainder = title.partition('__')
        if not separator:
            return call
        names = getattr(plan, 'mcp_display_names', {})
        label = names.get(native)
        agent_id = getattr(plan, 'mcp_namespace', getattr(plan, 'agent_id', ''))
        if label is None and agent_id and (native == agent_id or native.startswith(agent_id + '-')):
            # OpenClaw may truncate the registry component before appending the
            # tool name. A truncation shared by several services is ambiguous.
            matches = {value for name, value in names.items() if name.startswith(native)}
            label = next(iter(matches)) if len(matches) == 1 else 'MCP'
        if label is None:
            return call
        tool = re.match(r'[\w.-]+', remainder)
        return {**call, 'title': f'{label} · {tool.group()}' if tool else label}

    def _on_notification(self, state, method, params):
        update = params.get('update')
        if (method == 'session/update' and params.get('sessionId') == state.session_id
                and isinstance(update, Mapping) and update.get('sessionUpdate') in {'tool_call', 'tool_call_update'}):
            params = {**params, 'update': self._display_tool_call(state, update)}
        return super()._on_notification(state, method, params)

    async def _on_request(self, state, rpc_id, method, params):
        call = params.get('toolCall')
        if (method == 'session/request_permission' and params.get('sessionId') == state.session_id
                and isinstance(call, Mapping)):
            params = {**params, 'toolCall': self._display_tool_call(state, call)}
        return await super()._on_request(state, rpc_id, method, params)

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
        try:
            if binding is not None and (session_options is None or
                    session_options.metadata.get('_kausExecutionPolicy') != 'context-only'):
                from app.capabilities.models import EffectiveCapabilities
                from drivers.base import CreateSessionOptions
                from drivers.computer_use.integration import prepare_projection
                from .skill_adapter import remember_runtime_context, prepare_runtime_spec
                effective = getattr(self, '_openclaw_effective', {}).get(conversation.id)
                effective = effective or EffectiveCapabilities(project_id=conversation.project_id)
                session_options = session_options or CreateSessionOptions(title=conversation.title)
                remember_runtime_context(conversation, binding,
                    effective,
                    self._skill_plans, session_options.workspace_root or self._workspace_root_for(binding.id),
                    workspace_scoped=bool(session_options.workspace_root))
                metadata = dict(session_options.metadata)
                from drivers.computer_use.settings import is_enabled, runtime_revision
                if ('_kausComputerUse' not in metadata or
                        metadata['_kausComputerUse'].get('revision') != runtime_revision()):
                    metadata = await prepare_projection(self.agent_spec, self._runtime_adapter(),
                        binding, conversation, effective, metadata, supports_mcp=True)
                await prepare_runtime_spec(self.agent_spec, conversation, metadata)
                session_options = session_options.model_copy(update={'metadata': metadata})
            return await super().start_runtime(conversation, surface, session_options=session_options)
        except BaseException:
            from .skill_adapter import forget_runtime_context
            forget_runtime_context(conversation.id)
            plan = self._skill_plans.get(conversation.id)
            if plan is not None and getattr(plan, 'gateway', None):
                await plan.gateway.close()
            raise

    async def stop_runtime(self, runtime):
        state = self._runtimes.get(runtime.runtime_id)
        try:
            await super().stop_runtime(runtime)
        finally:
            if state is not None:
                from .skill_adapter import forget_runtime_context
                forget_runtime_context(state.conversation.id)
                gateway = self._session_gateways.pop(state.session_id, None)
                if gateway is not None:
                    await gateway.close()
                if state.skill_plan is not None and getattr(state.skill_plan, 'gateway', None):
                    await state.skill_plan.gateway.close()
            if not self._runtimes and self.gateway is not None:
                close = getattr(self.gateway, 'close', None)
                if close: await close()

    async def _new_session(self, connection: Any, options: Any) -> tuple[Any, Mapping[str, Any]]:
        self._require_no_session_mcp(options.metadata)
        result, params = await super()._new_session(connection, options)
        self._bind_session_route(result.get("sessionId"), params.get("_meta"))
        await self._augment_models(result, result.get("sessionId"))
        await self._apply_project_mcp(connection, result.get("sessionId"), result, params.get("_meta"))
        return result, params

    async def _resume_session(
        self, connection: Any, session_id: str, cwd: str, *,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        self._require_no_session_mcp(metadata or {})
        result = await super()._resume_session(connection, session_id, cwd, metadata=metadata)
        self._bind_session_route(session_id, (metadata or {}).get("_meta"))
        await self._augment_models(result, session_id)
        await self._apply_project_mcp(connection, session_id, result, (metadata or {}).get("_meta"), resumed=True)
        if isinstance(result, dict) and _PRIVATE_SNAPSHOT in result:
            result[_PRIVATE_SNAPSHOT]["resumed"] = True
        return result

    async def _apply_project_mcp(self, connection, session_id, result, metadata, *, resumed=False):
        key = (metadata or {}).get('sessionKey')
        if not key:
            return
        from .skill_adapter import apply_session_mcp
        for plan in self._skill_plans.values():
            if getattr(plan, 'session_key', None) == key:
                await self._ensure_gateway_identity(connection, session_id, result, resumed=resumed)
                await apply_session_mcp(plan)
                return

    @staticmethod
    def _require_no_session_mcp(metadata: Mapping[str, Any]) -> None:
        if metadata.get("mcpServers"):
            raise UnsupportedCapabilityError("当前 Gateway 不支持会话级 MCP；请在 Gateway 配置中管理")

    async def session_options_for(self, conversation: Any, effective: Any, *, workspace_root: str | None = None) -> Any:
        from drivers.base import CreateSessionOptions, SessionProjection
        from .skill_adapter import (project_session, remember_runtime_context,
                                    prepare_runtime_spec, project_scope_selected)
        binding = self._binding_snapshot(conversation.agent_binding_id)
        if binding is not None:
            remember_runtime_context(conversation, binding, effective, self._skill_plans, workspace_root)
        projection = await super().session_options_for(conversation, effective, workspace_root=workspace_root)
        if binding is not None:
            self._openclaw_effective = getattr(self, '_openclaw_effective', {})
            self._openclaw_effective[conversation.id] = effective
            metadata = dict(projection.options.metadata) if projection else {}
            previous = self._skill_plans.get(conversation.id)
            private_scope = (project_scope_selected(effective) or bool(workspace_root)
                or (previous is not None and not getattr(previous, 'source_session', False)))
            if private_scope:
                plan = await project_session(self.agent_spec, binding, effective,
                    workspace_root or self._workspace_root_for(binding.id), conversation=conversation,
                    force_workspace=bool(workspace_root), runtime_mcp=metadata.get('mcpServers', []),
                    computer_controls=metadata.get('_kausComputerUse', {}).get('controls', {}))
                if getattr(plan, 'agent_id', None):
                    self._skill_plans[conversation.id] = plan
                    if previous is not None and previous is not plan and getattr(previous, 'gateway', None):
                        await previous.gateway.close()
            else:
                await prepare_runtime_spec(self.agent_spec, conversation, metadata)
                if projection:
                    projection = SessionProjection(
                        options=projection.options.model_copy(update={'metadata': metadata}),
                        summary=projection.summary, warnings=projection.warnings)
        plan = self._skill_plans.get(conversation.id)
        if plan is not None and getattr(plan, 'agent_id', None):
            metadata = dict(projection.options.metadata) if projection else {}
            # The Gateway registry has already been scoped and validated. ACP
            # cannot carry these services itself; retain the owned route only.
            metadata.pop('mcpServers', None)
            metadata['_meta'] = {**metadata.get('_meta', {}), **plan.metadata}
            if getattr(plan, 'source_session', False):
                metadata['_kausOpenClawOwnership'] = plan.ownership
            return SessionProjection(options=CreateSessionOptions(title=conversation.title, metadata=metadata),
                summary=projection.summary if projection else {}, warnings=projection.warnings if projection else ())
        return projection

    async def load_native_history(self, binding, native_session_id):
        from .history import load_history
        self._assert_binding(binding)
        if self.gateway is None:
            raise UnsupportedCapabilityError("当前连接没有原生历史读取入口")
        return await load_history(self.agent_spec, binding, native_session_id)

    async def create_external_native_session(self, conversation, options):
        from datetime import datetime, timezone
        from drivers.base import NativeSession
        handle = await self.start_runtime(conversation, "card", session_options=options)
        try:
            state = self._state(handle)
            await self._ensure_gateway_identity(state.connection, state.session_id, state.session_result,
                                                resumed=bool(conversation.native_session_id))
            now = datetime.now(timezone.utc)
            return NativeSession(native_session_id=state.session_id, binding_id=conversation.agent_binding_id,
                title=options.title, head_id=state.session_id, created_at=now, updated_at=now)
        finally:
            await self.stop_runtime(handle)

    async def build_external_cli_launch(self, conversation):
        """Resume the Gateway session after applying current conversation choices."""
        import os
        from drivers.acp.native_cli import native_path
        from drivers.base import CliLaunchSpec
        from drivers.terminals.environment import selected_environment
        if not conversation.native_session_id or self.gateway is None:
            raise UnsupportedCapabilityError("此连接没有可续接的原生会话")
        executable = native_path(self)
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise UnsupportedCapabilityError("原生终端命令尚未安装")
        if len(self.gateway.prefix) != 1:
            raise UnsupportedCapabilityError("此自定义启动命令尚未提供终端接续")
        # Loading through the ordinary runtime applies cold model, reasoning and
        # permission choices and checks the owned Agent scope, without inference.
        handle = await self.start_runtime(conversation, "card")
        try:
            state = self._state(handle)
            gateway = self._gateway_for(state.session_id)
            await self._ensure_gateway_identity(state.connection, state.session_id, state.session_result, resumed=True)
            native = state.session_result.get(_PRIVATE_SNAPSHOT, {})
            key = native.get("key")
            if not key or native.get("error") or native.get("selectionPending"):
                raise AcpTransportError("无法确认原生终端会话")
            if state.skill_plan is not None:
                from .skill_adapter import validate_turn
                await validate_turn(state.skill_plan, self._workspace_root_for(conversation.agent_binding_id))
                if key != state.skill_plan.session_key:
                    raise AcpTransportError("原生终端会话与项目技能范围不一致")
            env = gateway.connection_environment()
            private = selected_environment(self.agent_spec, self.preset.env_keys_hint)
            for name in ("OPENCLAW_GATEWAY_URL", "OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_GATEWAY_PASSWORD"):
                if name in env:
                    private[name] = env[name]
            command = (executable, "tui", "--session", key)
            if conversation.reasoning_mode:
                command += ("--thinking", conversation.reasoning_mode)
            search_path = env.get("PATH", os.defpath)
            return CliLaunchSpec(command=command, private_env=private,
                config_directory=("OPENCLAW_STATE_DIR", str(Path(env.get("OPENCLAW_STATE_DIR") or Path(env.get("OPENCLAW_HOME") or env.get("HOME") or "~").expanduser() / ".openclaw").resolve())),
                path_prefix=tuple(p for p in search_path.split(os.pathsep) if os.path.isabs(p)),
                env_passthrough=self.preset.env_keys_hint, terminal_integration="openclaw",
                cwd=self._workspace_root_for(conversation.agent_binding_id) or self._default_cwd,
                title=conversation.title, resume=True)
        finally:
            await self.stop_runtime(handle)

    def _bind_session_route(self, session_id: str, metadata: Any) -> None:
        key = metadata.get('sessionKey') if isinstance(metadata, Mapping) else None
        owned = any(getattr(plan, 'session_key', None) == key for plan in self._skill_plans.values())
        if not session_id or not isinstance(key, str) or not (owned or key.startswith('agent:kaus-')):
            return
        gateway = OpenClawGateway(self.agent_spec, timeout=self._call_timeout)
        gateway.options.pop('--session-label', None)
        gateway.options['--session'] = key
        self._session_gateways[session_id] = gateway

    def _gateway_for(self, session_id: str):
        return self._session_gateways.get(session_id, self.gateway)

    async def _augment_models(self, result: Any, session_id: str) -> None:
        gateway = self._gateway_for(session_id)
        if gateway is None or not isinstance(result, dict) or not session_id:
            return
        try:
            key = await gateway.session_key(session_id)
            catalog, description = await asyncio.gather(
                gateway.call("models.list", {"sessionKey": key, "includeDetails": True, "view": "configured"}),
                gateway.call("sessions.describe", {"key": key}),
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
                               "availability": "unavailable" if row.get("available") is False else "available" if row.get("available") is True else "configured",
                               "provider_id": provider, "provider_label": provider,
                               "reasoning_levels": tuple(level["id"] for level in row["thinkingLevels"] if isinstance(level, dict) and isinstance(level.get("id"), str)) if isinstance(row.get("thinkingLevels"), list) else None,
                               "context_window": row.get("contextWindow") if isinstance(row.get("contextWindow"), int) and row["contextWindow"] > 0 else None})
            current = _current_model(description.get("session"))
            row = description.get("session")
            result[_PRIVATE_SNAPSHOT] = {"models": models, "current": current, "key": key, "exists": isinstance(row, dict),
                                        "permissionMode": row.get("permissionMode") if isinstance(row, dict) else None,
                                        "sessionId": row.get("sessionId") if isinstance(row, dict) else None,
                                        "lifecycleRevision": row.get("lifecycleRevision") if isinstance(row, dict) else None}
            self._install_model_option(result)
        except (AcpRpcError, AcpTransportError, asyncio.TimeoutError) as exc:
            result[_PRIVATE_SNAPSHOT] = {"error": str(exc)}

    def _install_model_option(self, result: dict[str, Any]) -> None:
        snapshot = result.get(_PRIVATE_SNAPSHOT, {})
        if not snapshot or snapshot.get("error"):
            return
        models = snapshot.get("models", [])
        options = [entry for entry in result.get("configOptions", []) if isinstance(entry, dict) and entry.get("id") not in {_MODEL_OPTION, _PERMISSION_OPTION}]
        if models:
            options.append({"id": _MODEL_OPTION, "name": "Model", "category": "model", "type": "select",
                            "currentValue": snapshot.get("current") or "", "options": [
                                {"value": row["model_id"], "name": row["display_name"]} for row in models]})
        options.append({"id": _PERMISSION_OPTION, "name": "Permissions", "type": "select",
                        "currentValue": snapshot.get("permissionMode") or "inherit", "options": [
                            {"value": value, "name": name} for value, name in _PERMISSION_OPTIONS]})
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
        gateway = self._gateway_for(state.session_id)
        if gateway is None:
            return await super()._set_model_config_option(state, model_id)
        native = state.session_result.get(_PRIVATE_SNAPSHOT, {})
        if model_id not in {row["model_id"] for row in native.get("models", [])}:
            raise AcpRpcError(-32602, "模型不在当前 OpenClaw 会话的可选目录中")
        await self._ensure_gateway_identity(state.connection, state.session_id, state.session_result,
                                            resumed=bool(state.handle.metadata.get("resumed")))
        key = native["key"]
        params = {"key": key, "expectedSessionId": native["sessionId"], "model": model_id}
        if native.get("lifecycleRevision"):
            params["expectedLifecycleRevision"] = native["lifecycleRevision"]
        # A timeout/cancellation can happen after the native mutation commits.
        # Until confirmed, do not send another prompt with an unknown route.
        native["selectionPending"] = True
        reply = await gateway.call("sessions.patch", params)
        actual = _current_model(reply.get("resolved")) or _current_model(reply.get("entry"))
        if actual is None:
            actual = _current_model((await gateway.call("sessions.describe", {"key": key})).get("session"))
        if actual != model_id:
            raise AcpRpcError(-32000, "OpenClaw 未确认所选模型，未保存选择")
        native["current"] = actual
        native["exists"] = True
        native["selectionPending"] = False
        self._install_model_option(state.session_result)
        self._accept_config_update(state, state.session_result)

    async def _ensure_gateway_identity(self, connection: Any, session_id: str, result: dict, *, resumed: bool = False) -> None:
        gateway = self._gateway_for(session_id)
        native = result.get(_PRIVATE_SNAPSHOT, {})
        if not native.get("exists") and not resumed:
            # ACP newSession initially owns only an event ledger, not a Gateway
            # row. Ask that exact ACP session to retain inherited usage reporting
            # before resolving a mutable Gateway identity. Catalog
            # probes remain read-only and an absent resumed row is never rebuilt.
            preference = next((entry for entry in result.get("configOptions", []) if isinstance(entry, dict) and entry.get("id") == "response_usage"), None)
            if not preference or preference.get("currentValue") != "inherit":
                raise AcpTransportError("OpenClaw 尚未建立可修改的原生会话")
            await connection.call("session/set_config_option", {"sessionId": session_id, "configId": "response_usage", "value": "inherit"}, timeout=self._call_timeout)
        # Cache only a Gateway-confirmed identity. The atomic expected identity
        # guards every later selection against resets without three CLI launches
        # on each dropdown change. A missing/stale row is never recreated here.
        key = native.get("key")
        if not native.get("sessionId"):
            key = await gateway.session_key(session_id, require_existing=True)
            described = await gateway.call("sessions.describe", {"key": key})
            session = described.get("session")
            if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
                raise AcpTransportError("OpenClaw 原生会话已变化，未修改设置")
            native.update({"key": key, "sessionId": session["sessionId"], "lifecycleRevision": session.get("lifecycleRevision"),
                           "permissionMode": session.get("permissionMode")})

    async def _set_permission_mode(self, connection: Any, session_id: str, result: dict, mode_id: str) -> None:
        gateway = self._gateway_for(session_id)
        if gateway is None or self._config_options(result).approval_option_id != _PERMISSION_OPTION:
            return await super()._set_permission_mode(connection, session_id, result, mode_id)
        if mode_id not in {value for value, _ in _PERMISSION_OPTIONS}:
            raise AcpRpcError(-32602, "OpenClaw 未提供这个权限选项")
        native = result[_PRIVATE_SNAPSHOT]
        await self._ensure_gateway_identity(connection, session_id, result, resumed=native.get("resumed", False))
        observed = (await gateway.call("sessions.describe", {"key": native["key"]})).get("session")
        if not isinstance(observed, dict) or observed.get("sessionId") != native["sessionId"]:
            raise AcpTransportError("OpenClaw 原生会话已变化，未修改权限")
        value = None if mode_id == "inherit" else mode_id
        params = {"key": native["key"], "expectedSessionId": native["sessionId"],
                  "expectedPermissionMode": observed.get("permissionMode"), "permissionMode": value}
        if native.get("lifecycleRevision") is not None:
            params["expectedLifecycleRevision"] = native["lifecycleRevision"]
        reply = await gateway.call("sessions.patch", params)
        row = reply.get("entry")
        if not isinstance(row, dict):
            row = (await gateway.call("sessions.describe", {"key": native["key"]})).get("session")
        if (reply.get("ok") is False or not isinstance(row, dict) or row.get("sessionId") != native["sessionId"]
                or row.get("permissionMode") != value or row.get("permissionModePending")):
            raise AcpTransportError("OpenClaw 未确认所选权限，未保存选择")
        native["permissionMode"] = value
        native["exists"] = True
        self._install_model_option(result)

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
