"""Process spawn, ``initialize`` handshake and the Backend probe.

Short-lived management connections (probe / list) and the per-runtime
connection both start here: :meth:`AcpHandshakeMixin._connect` spawns the
agent, completes ``initialize`` and absorbs the negotiated capabilities and
quirks (AD-151). ``probe`` turns a handshake into a
:class:`~drivers.base.BackendProbeResult`, including the readable cause when
the process never answered (AD-159).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from drivers.acp.capabilities import (
    agent_name,
    agent_version,
    capabilities_from_initialize,
    effective_quirks,
    initialize_params,
    session_discovery_verdict,
)
from drivers.acp.failure_hints import spawn_failed
from drivers.acp.client import (
    AcpAgentSpec,
    AcpConnection,
    AcpRpcError,
    AcpSpawnError,
    AcpTimeoutError,
    AcpTransportError,
)
from drivers.base import BackendProbeResult, FailureHint
from runtime.capability_matrix import BackendCapabilities
from drivers.acp.runtime_state import DRIVER_VERSION, _now


class AcpHandshakeMixin:
    """Connection setup and probe; composed into :class:`~drivers.acp.driver.AcpDriver`."""

    async def _connect(
        self, *, workspace_root: str | None = None, agent_spec: AcpAgentSpec | None = None
    ) -> tuple[AcpConnection, Any]:
        """起一条连接并完成 ``initialize``。调用方负责 :meth:`AcpConnection.aclose`。

        ``workspace_root`` 只影响一件事：要不要向 agent 声明 ``fs`` 客户端能力
        （AD-152）。管理类的短命连接（probe / list）不给它，因为那些连接不跑
        回合，也就没有替 agent 读写文件这回事。
        """
        actual_spec = agent_spec or self.agent_spec
        adapter = self._runtime_adapter()
        policy_factory = getattr(adapter, "session_start_retry_policy", None)
        retry_policy = await policy_factory(actual_spec) if policy_factory is not None else None
        connection = await AcpConnection.spawn(actual_spec)
        connection.native_auth_state = None
        connection.native_auth_event = asyncio.Event()
        connection.native_auth_expected = False
        connection.on_notification = lambda method, params: self._capture_auth_status(connection, method, params)
        connection.session_start_retry_policy = retry_policy
        connection.graceful_shutdown_timeout = getattr(adapter, "GRACEFUL_SHUTDOWN_TIMEOUT", None)
        try:
            try:
                result = await connection.call(
                    "initialize",
                    initialize_params(self.quirks, workspace_root=workspace_root),
                    timeout=self._call_timeout,
                )
            except AcpTransportError as exc:
                spawn_failure = await self._handshake_failure(connection, exc)
                raise spawn_failure or exc from exc
            meta = ((result.get("agentCapabilities") or {}).get("_meta") or {}) if isinstance(result, Mapping) else {}
            connection.native_auth_expected = isinstance(meta, Mapping) and "authStatus" in meta
            self._absorb_initialize(result)
            return connection, result
        except BaseException:
            # Includes cancellation during initialize or failure diagnosis,
            # before a runtime owns the native process.
            await connection.aclose()
            raise

    async def _handshake_failure(
        self, connection: AcpConnection, exc: AcpTransportError
    ) -> AcpSpawnError | None:
        """握手期间的传输失败到底是不是「这个进程根本没起来」（AD-159）。

        三种认得出来的形状，其余一律 ``None``（照旧抛原始错误，不编根因）：

        1. **进程退了，退出码非零**——它自己拒绝启动（依赖缺失、参数不认、
           权限不对）。退出码就是用户在终端里会看到的那一个。
        2. **进程退了，退出码是 0**——更隐蔽的一种：它「成功」地什么都没做就走了
           （包装脚本转发丢了、把 ``--help`` 打完就退）。这一条没有退出码可报，
           所以必须明说「没回答 initialize 就退了」。
        3. **进程还活着，但超时了，而且 stderr 上有话**——它卡在某个提示上或者把
           stdout 当日志用了。stderr 的第一句就是线索；stderr 是空的时候我们什么
           都不知道，那就不装作知道。
        """
        exit_code = await connection.exit_status()
        stderr = connection.stderr_text()
        if exit_code is not None and exit_code != 0:
            cause = f"进程在握手期间退出（exit {exit_code}）"
        elif exit_code is not None:
            cause = "进程没有回答 initialize 就自己退出了（exit 0）"
        elif isinstance(exc, AcpTimeoutError) and stderr.strip():
            cause = f"进程起来了但没有回答 initialize（{self._call_timeout:g}s 超时）"
        else:
            return None
        failure = spawn_failed(
            self.agent_spec.command,
            cause=cause,
            stderr=stderr,
            environ=self.agent_spec.build_env(),
        )
        return AcpSpawnError(failure.message, failure=failure)

    def _absorb_initialize(self, result: Any) -> None:
        # AD-151：实测覆盖声明——先把怪癖按 initialize 收敛，再算能力。
        if self.preset is not None:
            self.quirks = effective_quirks(self.preset.quirks, result, self.preset)
        self._capabilities = capabilities_from_initialize(result, self.preset)
        if callable(getattr(self._runtime_adapter(), "load_native_history", None)):
            from runtime.capability_matrix import CapabilityState
            self._capabilities = self._capabilities.model_copy(update={
                "sessions": self._capabilities.sessions.model_copy(update={
                    "history": CapabilityState(value="supported", verification="declared",
                        note="原生公开历史；完整度由每次读取结果报告"),
                }),
            })
        self._backend_version = agent_version(result)
        self._protocol_version = result.get("protocolVersion") if isinstance(result, Mapping) else None
        self._backend_name = agent_name(result)
        self._installed = True
        if self._skill_projection_verification == self._skill_projection_key():
            self._mark_skill_capability()
        else:
            self._skill_projection_verification = None

    async def probe(self) -> BackendProbeResult:
        try:
            connection, result = await self._connect()
        except AcpTransportError as exc:
            # AD-159：认得出「进程没起来」时，``probeMessage`` 就是那句人话 + 修法
            # （引擎卡上显示的正是它）。认不出来才退回异常原文。
            failure = getattr(exc, "failure", None)
            message = (
                failure.describe()
                if isinstance(failure, FailureHint)
                else str(exc)
            )
            # A present native engine can fail its ACP handshake because a
            # companion service is stopped. Keep installation and readiness
            # separate; do not offer reinstallation for a connection failure.
            from drivers.acp.native_cli import native_path
            import os
            executable = native_path(self)
            self._installed = bool(executable and Path(executable).is_file()
                                   and os.access(executable, os.X_OK))
            self._probe_message = message
            return BackendProbeResult(
                backend_id=self.backend_id,
                driver_kind=self.driver_kind,
                state="unavailable",
                installed=self._installed,
                driver_version=DRIVER_VERSION,
                message=message,
                capabilities=self._capabilities or BackendCapabilities(),
                probed_at=_now(),
            )
        except AcpRpcError as exc:
            self._installed = True
            self._probe_message = f"initialize 被拒：{exc}"
            return BackendProbeResult(
                backend_id=self.backend_id,
                driver_kind=self.driver_kind,
                state="degraded",
                installed=True,
                driver_version=DRIVER_VERSION,
                message=self._probe_message,
                capabilities=self._capabilities or BackendCapabilities(),
                probed_at=_now(),
            )
        noise = connection.protocol_noise()
        await connection.aclose()
        message = None
        state: Literal["ready", "degraded", "unavailable"] = "ready"
        if noise:
            # stdout 被日志污染：连接还活着，但迟早会咬人，必须显式降级。
            state = "degraded"
            message = f"agent 在 stdout 上写了 {len(noise)} 条非 JSON-RPC 内容"
        elif self._backend_version is None:
            message = f"agent 未报版本（agentInfo.version 缺失）；name={self._backend_name!r}"
        inventory_refresh = getattr(self._skill_adapter(), "refresh_inventory", None)
        if callable(inventory_refresh) and not self._inventory_closed:
            if self._inventory_refresh_error:
                notice = "技能来源刷新失败，可重试"
                message = f"{message}；{notice}" if message else notice
            self.start_skill_inventory_refresh()
        self._probe_message = message
        assert self._capabilities is not None  # noqa: S101
        return BackendProbeResult(
            backend_id=self.backend_id,
            driver_kind=self.driver_kind,
            state=state,
            installed=True,
            version=self._backend_version,
            driver_version=DRIVER_VERSION,
            message=message,
            capabilities=self._capabilities,
            probed_at=_now(),
        )

    def start_skill_inventory_refresh(self) -> None:
        """Read native Skill inventory without initializing an Agent session."""
        refresh = getattr(self._skill_adapter(), "refresh_inventory", None)
        if not callable(refresh) or self._inventory_closed:
            return
        if self._inventory_refresh_task is None or self._inventory_refresh_task.done():
            # The same bounded, driver-owned job serves first connection and probe.
            self._inventory_refresh_task = asyncio.create_task(
                self._refresh_skill_inventory(refresh, self.agent_spec))

    async def _refresh_skill_inventory(self, refresh, spec):
        try:
            await asyncio.wait_for(refresh(spec), timeout=35)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - adapter code; the probe reports it as retryable
            logging.getLogger(__name__).warning(
                "Skill inventory refresh failed (%s)", self.backend_id, exc_info=True)
            self._inventory_refresh_error = True
        else:
            self._inventory_refresh_error = False

    def skill_inventory_pending(self) -> bool:
        """Host onboarding waits for any already-running inventory refresh."""
        task = self._inventory_refresh_task
        return task is not None and not task.done()

    async def aclose(self) -> None:
        """Release driver-owned discovery work; SessionHost owns live sessions."""
        self._inventory_closed = True
        task = self._inventory_refresh_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._inventory_refresh_task = None

    async def get_capabilities(self) -> BackendCapabilities:
        if self._capabilities is None:
            await self.probe()
        return self._capabilities or BackendCapabilities()

    def session_discovery_support(self):
        """``sessions.list`` 的显式判定（PARTIAL：可核对、不可发现）。

        布尔位 ``sessions.list`` 只说「方法能调」；这一条说「结果能信到什么程度」。
        """
        return session_discovery_verdict()
