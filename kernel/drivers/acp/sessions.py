"""Native session lifecycle: create, list, resume and history access.

The driver keeps its own ledger (``session_ledger``) because ``session/list``
only sees sessions created by the same process; the list result is used for
reconciliation (:class:`~drivers.acp.runtime_state.AcpDiscoveryReport`), not
for discovery. Resume prefers ``session/resume`` and falls back to
``session/load`` only when the agent does not know the method.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from app.conversations.models import Conversation
from app.projects.models import AgentBinding
from drivers.acp.client import AcpConnection, AcpRpcError, AcpTransportError, call_with_param_shapes
from drivers.base import (
    CreateSessionOptions,
    NativeHistory,
    NativeSession,
    UnsupportedCapabilityError,
)
from drivers.acp.runtime_state import AcpDiscoveryReport, _now


def _require_session_id(result: Any) -> str:
    if isinstance(result, Mapping):
        session_id = result.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id
    raise AcpTransportError(f"session/new 的结果里没有 sessionId：{result!r}")


def _session_ids(result: Any) -> set[str]:
    if not isinstance(result, Mapping):
        return set()
    sessions = result.get("sessions")
    if not isinstance(sessions, Sequence) or isinstance(sessions, (str, bytes)):
        return set()
    found: set[str] = set()
    for item in sessions:
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, Mapping):
            session_id = item.get("sessionId") or item.get("id")
            if isinstance(session_id, str) and session_id:
                found.add(session_id)
    return found


def _mcp_servers(metadata: Mapping[str, Any]) -> list[Any]:
    servers = metadata.get("mcpServers")
    if isinstance(servers, Sequence) and not isinstance(servers, (str, bytes)):
        return list(servers)
    return []


class AcpNativeSessionMixin:
    """Native session management; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    async def create_external_native_session(
        self, conversation: Conversation, options: CreateSessionOptions
    ) -> NativeSession:
        from drivers.external_session import precreate_external_session
        options = options.model_copy(update={"metadata": {**options.metadata, "_kausExternalPrecreate": True}})
        return await precreate_external_session(self, conversation, options)

    async def create_native_session(
        self, binding: AgentBinding, options: CreateSessionOptions
    ) -> NativeSession:
        self._assert_binding(binding)
        plan = self._binding_skill_plans.get(binding.id)
        connection, _init = await self._connect(agent_spec=plan.spec if plan else None)
        if plan and plan.metadata:
            options = options.model_copy(update={"metadata": {**options.metadata, "_meta": plan.metadata}})
        try:
            result, _params = await self._new_session(connection, options)
        except AcpRpcError as exc:
            auth_error = self._auth_error(binding.id, exc)
            if auth_error is not None:
                raise auth_error from exc
            raise
        finally:
            await connection.aclose()
        session_id = _require_session_id(result)
        self._remember_models(binding.id, result, connection=connection)
        session = NativeSession(
            native_session_id=session_id,
            binding_id=binding.id,
            title=options.title,
            head_id=session_id,
            created_at=_now(),
            updated_at=_now(),
        )
        self.session_ledger.setdefault(binding.id, {})[session_id] = session
        return session

    async def list_native_sessions(
        self, binding: AgentBinding
    ) -> tuple[NativeSession, ...]:
        """账本为准，``session/list`` 只用于核对（见模块 docstring 与
        :meth:`session_discovery_support`）。"""
        self._assert_binding(binding)
        capabilities = await self.get_capabilities()
        if not capabilities.sessions.list:
            raise UnsupportedCapabilityError(
                "该 agent 的 initialize 没有声明 sessionCapabilities.list"
            )
        ledger = self.session_ledger.get(binding.id, {})
        agent_ids: set[str] = set()
        method_error: str | None = None
        try:
            connection, _init = await self._connect()
        except (AcpTransportError, AcpRpcError) as exc:
            method_error = str(exc)
        else:
            try:
                result = await connection.call("session/list", {}, timeout=self._call_timeout)
                agent_ids = _session_ids(result)
            except (AcpTransportError, AcpRpcError) as exc:
                method_error = str(exc)
            finally:
                await connection.aclose()
        self.discovery_reports[binding.id] = AcpDiscoveryReport(
            binding_id=binding.id,
            checked_at=_now(),
            confirmed=tuple(sorted(agent_ids & set(ledger))),
            ledger_only=tuple(sorted(set(ledger) - agent_ids)),
            agent_only=tuple(sorted(agent_ids - set(ledger))),
            method_error=method_error,
        )
        return tuple(ledger.values())

    async def count_native_sessions(self, binding: AgentBinding) -> int | None:
        """账本里有几条（批次十九第 2 件）。列不出来就是 ``None``。

        ``session/list`` 只是核对面，数字仍以本地账本为准——与
        :meth:`list_native_sessions` 同一个口径，不另起一套算法。
        """
        self._assert_binding(binding)
        try:
            return len(await self.list_native_sessions(binding))
        except (UnsupportedCapabilityError, AcpTransportError, AcpRpcError):
            return None

    async def load_local_native_history(
        self, binding: AgentBinding, native_session_id: str
    ) -> NativeHistory:
        """Explicit filesystem-only hook; a general history API is not proof."""
        self._assert_binding(binding)
        loader = getattr(self._runtime_adapter(), "load_local_native_history", None)
        if not callable(loader):
            raise UnsupportedCapabilityError("local_native_history_unsupported")
        return await loader(self.agent_spec, binding, native_session_id)

    async def load_native_history(
        self, binding: AgentBinding, native_session_id: str
    ) -> NativeHistory:
        """Use a verified native reader when the adapter provides one."""
        self._assert_binding(binding)
        loader = getattr(self._runtime_adapter(), "load_native_history", None)
        if loader is not None:
            return await loader(self.agent_spec, binding, native_session_id)
        raise UnsupportedCapabilityError(
            "ACP 没有『读取会话历史』的方法：session/load 的语义是把会话装回来并"
            "重放 session/update，不是返回历史条目，且实测存在 agent 拒绝该方法的"
            f"情况。会话 {native_session_id!r} 的历史必须由上层的 Event Store 或"
            "该 agent 的专用 Driver 提供（R-02）。"
        )

    async def _new_session(
        self, connection: AcpConnection, options: CreateSessionOptions
    ) -> tuple[Any, Mapping[str, Any]]:
        cwd = self._resolve_cwd(options.workspace_root)
        mcp_servers = _mcp_servers(options.metadata)
        # 形状协商：cwd + mcpServers 是实测必填组合；两者都省的形状留作兜底，
        # 以防未来的 agent 把它们改成可选。
        shapes: list[dict[str, Any]] = [
            {"cwd": cwd, "mcpServers": mcp_servers},
            {"cwd": cwd},
        ]
        if mcp_servers:
            shapes = shapes[:1]
        if options.metadata.get("_meta"):
            for shape in shapes:
                shape["_meta"] = options.metadata["_meta"]
        retry_policy = getattr(connection, "session_start_retry_policy", None)
        attempt = 0
        while True:
            checkpoint = connection.stderr_checkpoint() if retry_policy else None
            try:
                return await call_with_param_shapes(
                    connection, "session/new", shapes, timeout=self._call_timeout
                )
            except AcpRpcError as exc:
                if retry_policy is None:
                    raise
                # stdout and stderr are independent pipes. Allow the error's
                # trailing diagnostic to arrive, never reuse a previous call's.
                await asyncio.sleep(0.05)
                delay = retry_policy(exc, connection.stderr_since(checkpoint), attempt)
                if delay is None:
                    raise
                attempt += 1
                logging.getLogger(__name__).info(
                    "Retrying pre-session transport failure (%s), attempt %s",
                    self.backend_id, attempt,
                )
                await asyncio.sleep(delay)

    async def _resume_session(
        self, connection: AcpConnection, session_id: str, cwd: str, *, metadata: Mapping[str, Any] | None = None
    ) -> Any:
        """续接。首选 ``session/resume``，只有它不存在时才试 ``session/load``。"""
        shapes: list[dict[str, Any]] = [
            {"sessionId": session_id, "cwd": cwd, "mcpServers": _mcp_servers(metadata or {})},
            {"sessionId": session_id, "cwd": cwd},
        ]
        # A nonempty requested server set must not disappear during shape negotiation.
        if _mcp_servers(metadata or {}):
            shapes = shapes[:1]
        if metadata and metadata.get("_meta"):
            for shape in shapes:
                shape["_meta"] = metadata["_meta"]
        if getattr(self.quirks, "prefer_session_load", False) and self.quirks.supports_session_load:
            result, _params = await call_with_param_shapes(
                connection, "session/load", shapes, timeout=self._call_timeout
            )
            return result
        if self.quirks.supports_session_resume:
            await self._close_before_resume(connection, session_id)
            try:
                result, _params = await call_with_param_shapes(
                    connection, "session/resume", shapes, timeout=self._call_timeout
                )
                return result
            except AcpRpcError as exc:
                if exc.code != -32601:
                    raise
        elif not self.quirks.supports_session_load:
            # 两条路都不通：这不是「试一下看看」的场合，直接说清楚。
            raise UnsupportedCapabilityError(
                "该 agent 既不认 session/resume 也不认 session/load，"
                "本条会话续接不了（见预设的怪癖表）"
            )
        result, _params = await call_with_param_shapes(
            connection, "session/load", shapes, timeout=self._call_timeout
        )
        return result

    async def _close_before_resume(
        self, connection: AcpConnection, session_id: str
    ) -> None:
        """``resume_requires_close`` 的那一步（AD-158）。

        真机现象：对**还活着**的会话直接 ``session/resume`` 回
        ``-32602 already active``，先 ``session/close`` 一次就通（换一个新进程也
        通，但那是上层的事）。协议没规定这个先后关系，所以它是目录里的一位，
        不是所有引擎都走的一步。

        ``-32601`` 静默放过——那说明这家压根没有 ``session/close``，也就不需要
        这一步。别的失败记一条 warning 之后**照样往下走**：关不掉不等于续不上，
        为一个礼貌性的收尾把整条续接判死是本末倒置。
        """
        if not self.quirks.resume_requires_close:
            return
        try:
            await connection.call(
                "session/close",
                {"sessionId": session_id},
                timeout=self._call_timeout,
            )
        except AcpRpcError as exc:
            if exc.code != -32601:
                self._warn(f"resume_close_failed：session/close 被拒（{exc}）")
        except AcpTransportError as exc:
            self._warn(f"resume_close_failed：session/close 发不出去（{exc}）")
