"""Generic ACP Driver：:class:`~drivers.base.BackendDriver` 的 ACP 实现。

``driver_kind = "acp"``（N §5.1）。这是 baseline §9.2 的**路径 A**：任何声明支持
Agent Client Protocol 的 Agent 都可以用它接入，接入方只需要给出
:class:`~drivers.acp.client.AcpAgentSpec`（怎么把这个 agent 以 ACP 模式拉起来）。

进程拓扑
--------
ACP 的 stdio 形态是「一个进程 = 一条管道 = 一个客户端」，因此：

- **每个 Card Runtime 一个子进程**：``start_runtime`` 拉起，``stop_runtime`` 收掉；
- **会话管理操作（probe / create / list）用短命连接**：起进程 → ``initialize``
  → 干一件事 → 关掉。之所以敢这么做，是因为实测确认「另起一个进程可以
  ``session/resume`` 前一个进程创建的会话」——会话不随进程消失。
- 这条协议**做不到**多客户端同时 attach 同一个会话；需要并发的上层必须自己
  排队。这一条写在这里是为了避免上层按 HTTP 那套「一个常驻服务多路复用」的
  心智模型用它。

会话身份（不猜、不靠「最新会话」）
--------------------------------
``session/list`` 只能看见**本进程**创建的会话（实测），所以它不能作为发现手段。
Driver 因此维护自己的账本 :attr:`AcpDriver.session_ledger`：
``binding_id -> {native_session_id -> NativeSession}``，由 ``create_native_session``
与 ``start_runtime`` 写入。``list_native_sessions`` 返回账本内容，并用
``session/list`` 的结果做**核对**（产出 :class:`AcpDiscoveryReport`），
不把「只在 ``session/list`` 里出现、归属未知」的会话硬塞给某条 Binding。

续接用 ``session/resume``
-------------------------
实测：``session/load`` 在真实 agent 上会被参数校验拒掉，而 ``session/resume``
带 ``{sessionId, cwd}`` 成功。因此本 Driver 的续接首选 ``session/resume``，
只有在 agent 明确不认这个方法（``-32601``）时才回落到 ``session/load``。
两者的参数形状都走 :func:`~drivers.acp.client.call_with_param_shapes` 协商，
而不是按某一家的口径写死。

历史
----
原生适配器可通过 ``load_native_history`` 提供稳定 ID 的公开历史；没有经过
验证的读取入口时显式报告不支持。缺失的工具、附件等记录由完整性字段标明。

模块分工
--------
本文件只保留 :class:`AcpDriver` 的装配：构造、Binding 快照与少量基础工具。
各项职责按模块拆开，由 mixin 组合成同一个类（公开 API 与此前一致）：

- :mod:`drivers.acp.runtime_state`：目录快照、运行时状态等共享数据结构；
- :mod:`drivers.acp.handshake`：起进程、``initialize``、probe；
- :mod:`drivers.acp.catalog`：模型目录探测与登录态缓存；
- :mod:`drivers.acp.session_config`：模型 / 推理 / 权限 / 运行模式映射；
- :mod:`drivers.acp.projection`：项目能力投影与适配器查找；
- :mod:`drivers.acp.sessions`：原生会话的创建、列出、续接；
- :mod:`drivers.acp.card_runtime`：Card Runtime 启停与事件队列；
- :mod:`drivers.acp.turns`：``session/prompt``、中断与内容块转换；
- :mod:`drivers.acp.inbound`：入站通知、权限请求与客户端 fs；
- :mod:`drivers.acp.external_cli`：站外终端续接。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from app.ids import normalize_backend_id
from app.projects.models import AgentBinding
from drivers.acp.presets import AcpPreset, AgentQuirks
from drivers.acp.client import AcpAgentSpec
from drivers.base import NativeSession
from runtime.capability_matrix import BackendCapabilities
from drivers.acp.card_runtime import AcpCardRuntimeMixin
from drivers.acp.catalog import (  # noqa: F401
    AcpCatalogMixin,
    AUTH_STATUS_WAIT_SECONDS,
    _connect_diagnostic,
    _connect_hint,
    _mode_id_of,
    _strip_effort_suffix,
)
from drivers.acp.external_cli import AcpExternalSurfaceMixin
from drivers.acp.handshake import AcpHandshakeMixin
from drivers.acp.inbound import AcpInboundMixin, _read_text, _resolve_within  # noqa: F401
from drivers.acp.projection import AcpProjectionMixin
from drivers.acp.session_config import AcpSessionConfigMixin
from drivers.acp.sessions import (  # noqa: F401
    AcpNativeSessionMixin,
    _mcp_servers,
    _require_session_id,
    _session_ids,
)
from drivers.acp.turns import AcpTurnMixin, _prompt_blocks  # noqa: F401
from drivers.acp.runtime_state import (  # noqa: F401
    AcpDiscoveryReport,
    CATALOG_TTL_SECONDS,
    DRIVER_VERSION,
    _CatalogSnapshot,
    _PendingFsWrite,
    _RuntimeState,
    _SENTINEL,
    _now,
)


class AcpDriver(
    AcpHandshakeMixin,
    AcpCatalogMixin,
    AcpSessionConfigMixin,
    AcpProjectionMixin,
    AcpNativeSessionMixin,
    AcpCardRuntimeMixin,
    AcpTurnMixin,
    AcpInboundMixin,
    AcpExternalSurfaceMixin,
):
    """通用 ACP Backend Driver。

    参数
    ----
    agent_spec:
        怎么把 agent 拉起来。
    backend_key:
        ``backend:<key>`` 的 key。同一进程可以注册多个不同 key 的 ACP Driver
        （分别指向不同的 agent）。
    default_cwd:
        ``session/new`` 的 ``cwd`` 默认值（实测：该参数必填）。缺省用
        ``agent_spec.cwd``，再缺省用当前工作目录。
    """

    driver_kind: Literal["acp"] = "acp"

    def __init__(
        self,
        agent_spec: AcpAgentSpec,
        *,
        backend_key: str = "acp",
        default_cwd: str | None = None,
        call_timeout: float = 30.0,
        prompt_timeout: float = 300.0,
        cancel_grace: float = 2.0,
        preset: AcpPreset | None = None,
    ) -> None:
        self.backend_id = normalize_backend_id(backend_key)
        self.agent_spec = agent_spec
        #: 这条 Backend 用的预设（``backends[].preset``）。``None`` = 裸 ACP 配置，
        #: 行为与本批之前逐字相同。
        self.preset = preset
        #: 本次连接实际生效的怪癖：预设声明 + ``initialize`` 的覆盖（AD-151）。
        self.quirks: AgentQuirks = preset.quirks if preset is not None else AgentQuirks()
        #: AD-58：Binding 快照由接入层灌（``register_binding``）。审批档与
        #: workspaceRoot 都在 Binding 上，而 ``start_runtime`` 只拿得到 Conversation。
        self._bindings: dict[str, AgentBinding] = {}
        #: 装配期与运行期的告警（``session/set_mode`` 发不出去之类）。只进 wire
        #: 与日志，不进事件流。
        self.warnings: list[str] = []
        self._default_cwd = default_cwd or agent_spec.cwd
        self._call_timeout = call_timeout
        self._prompt_timeout = prompt_timeout
        self._cancel_grace = cancel_grace

        self._capabilities: BackendCapabilities | None = None
        self._backend_version: str | None = None
        self._backend_name: str | None = None
        self._probe_message: str | None = None
        self._inventory_refresh_task: asyncio.Task | None = None
        self._inventory_refresh_error = False
        self._inventory_closed = False
        self._installed: bool | None = None

        #: binding_id -> {native_session_id -> NativeSession}。见模块 docstring。
        self.session_ledger: dict[str, dict[str, NativeSession]] = {}
        #: 最近一次 list_native_sessions 的核对结果，按 binding 存。
        self.discovery_reports: dict[str, AcpDiscoveryReport] = {}
        #: binding_id -> 上一次目录探测/真实会话留下的快照（TTL 见
        #: :data:`CATALOG_TTL_SECONDS`）。
        self._catalog: dict[str, _CatalogSnapshot] = {}
        self._catalog_config_stamps: dict[str, tuple] = {}
        self._selection_catalog: dict[tuple[str, str], tuple[tuple, _CatalogSnapshot]] = {}
        self._runtimes: dict[str, _RuntimeState] = {}
        self._skill_plans: dict[str, Any] = {}
        #: 每条会话最近一次的进程规格与私有元数据，终端续接沿用同一份。
        self._session_contexts: dict[str, tuple[Any, dict]] = {}
        self._binding_skill_plans: dict[str, Any] = {}
        #: conversation_id -> 这次启动的送达计划（指令通道、原生 MCP 排除；2026-11-09）。
        #: 由 ``session_options_for`` 写、``start_runtime`` 读。
        self._launch_plans: dict[str, Any] = {}
        self._skill_projection_verification = None
        self._runtime_counter = 0

    # ------------------------------------------------------------------ #
    # Binding 快照（AD-58）
    # ------------------------------------------------------------------ #

    def register_binding(self, binding: AgentBinding) -> None:
        """把 Binding 交给 Driver（接入层在开 Runtime 前灌一次）。

        Driver 从这里拿两样东西，且**只拿这两样**：审批档（决定 ``set_mode``
        与 fs 写的放行口径）与 workspaceRoot（决定客户端 fs 的边界）。凭据、
        地址一律不经过这里。
        """
        self._assert_binding(binding)
        self._bindings[binding.id] = binding
        source_id = binding.runtime_config.get('group_source_binding_id')
        if source_id and source_id in self._catalog and binding.id not in self._catalog:
            self._catalog[binding.id] = self._catalog[source_id]

    def _binding_snapshot(self, binding_id: str) -> AgentBinding | None:
        return self._bindings.get(binding_id)

    def _workspace_root_for(self, binding_id: str) -> str | None:
        """AD-152：客户端 fs 的根。取自 Binding 的 ``runtime_config.workspace_root``。

        没有登记过 Binding、或那条 Binding 没写工作目录 → ``None``：那时既不声明
        fs 能力，也就不存在越界这回事。**不**拿 Driver 的 ``default_cwd`` 顶替
        ——那是「进程从哪起」，不是「用户授权了哪个目录」。
        """
        binding = self._binding_snapshot(binding_id)
        if binding is None:
            return None
        raw = binding.runtime_config.get("workspace_root")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return os.path.realpath(os.path.abspath(os.path.expanduser(raw)))

    def _approval_mode_for(self, binding_id: str) -> str:
        """AD-118 的通用审批档。读不到就按 ``ask``——默认停下来问人，不默认放行。"""
        binding = self._binding_snapshot(binding_id)
        if binding is None:
            return "ask"
        value = binding.runtime_config.get("approval_mode")
        return value if value in ("ask", "auto", "deny") else "ask"

    def _warn(self, message: str) -> None:
        self.warnings.append(message)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _assert_binding(self, binding: AgentBinding) -> None:
        """N §13.2：Catalog / Session 归 Binding，不得跨 Backend 串数据。"""
        if binding.backend_id != self.backend_id:
            raise ValueError(
                f"Binding {binding.id!r} 属于 {binding.backend_id!r}，"
                f"不能交给 {self.backend_id!r} 的 Driver"
            )

    def _resolve_cwd(self, workspace_root: str | None) -> str:
        cwd = workspace_root or self._default_cwd or os.getcwd()
        return os.path.abspath(cwd)


__all__ = ["AcpDiscoveryReport", "AcpDriver", "DRIVER_VERSION"]
