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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.conversations.configuration import validate_config_values
from drivers.approval import host_approval_options, is_host_approval, allow_once_option
from drivers.error_text import safe_error_text, safe_exception_message
from app.ids import normalize_backend_id
from app.projects.models import AgentBinding, Project
from drivers.acp.capabilities import (
    CONFIG_CATEGORY_MODEL,
    DEFAULT_THOUGHT_LEVEL,
    agent_name,
    config_options_from_session_result,
    conversation_permission_options,
    current_model_from_session_result,
    agent_version,
    available_modes,
    capabilities_from_initialize,
    effective_quirks,
    initialize_params,
    models_from_session_result,
    resolve_mode_id,
    resolve_thought_level_id,
    resolve_conversation_mode_id,
    session_discovery_verdict,
)
from drivers.acp.failure_hints import (
    auth_methods_from_error,
    auth_required,
    login_diagnostic,
    looks_like_auth_error,
    looks_like_missing_credentials,
    spawn_failed,
)
from drivers.acp.presets import AcpPreset, AgentQuirks
from drivers.acp.client import (
    AcpAgentSpec,
    AcpConnection,
    AcpRpcError,
    AcpSpawnError,
    AcpTimeoutError,
    AcpTransportError,
    call_with_param_shapes,
)
from drivers.acp.translator import AcpTranslationContext, AcpTranslator
from drivers.acp.interactions import ClientInteractions
from drivers.base import (
    AuthRequiredError,
    AuthState,
    BackendProbeResult,
    CliLaunchSpec,
    CreateSessionOptions,
    DriftReport,
    EngineSettings,
    InteractionNotFoundError,
    InteractionResponse,
    MessageInput,
    ModelCatalog,
    ModelDescriptor,
    ModelRejectedError,
    NativeHistory,
    NativeSession,
    FailureHint,
    ProjectionEntry,
    ProjectionResult,
    RuntimeHandle,
    RuntimeNotFoundError,
    SessionProjection,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
    turn_already_running_hint,
    unknown_auth_state,
)
from drivers.acp.mcp_projection import project_mcp_servers
from drivers.discovery_scope import (
    DiscoveryCredentialsUnavailable, configuration_paths, isolated_agent_spec,
    prepare_discovery_credentials,
)
from drivers.model_discovery_access import connection_configured, offered_models
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    INVALID_DETAIL,
    compose_instructions,
    instruction_entries,
    invalid_instruction_entries,
)
from drivers.workspace_projection import (
    inspect_block_drift,
    materialize_block,
)
from runtime.capability_matrix import (
    BackendCapabilities,
    SupportLevel,
    evaluate_support,
)
from runtime.event_envelope import AgentEventEnvelope

DRIVER_VERSION: str = "0.1.0"
AUTH_STATUS_WAIT_SECONDS = 6.0

_SENTINEL = object()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _connect_diagnostic(exc: BaseException) -> str:
    """目录探测连不上引擎时挂在空目录上的那一句（``ModelCatalog.diagnostics``）。

    AD-159：进程没起来是这里最常见的一种，而它有修法——把那份人话 + 修法原样
    挂出来，用户在模型下拉旁边就能看见「为什么这里是空的、该怎么办」，不必先去
    翻引擎卡。认不出根因时才退回异常原文。
    """
    failure = getattr(exc, "failure", None)
    if isinstance(failure, FailureHint):
        return failure.describe()
    return f"这次没能连上引擎：{exc}"


def _connect_hint(exc: BaseException) -> str | None:
    """同一次失败挂到 ``AuthState.hint`` 上的那一句。认不出根因就 ``None``。"""
    failure = getattr(exc, "failure", None)
    return failure.describe() if isinstance(failure, FailureHint) else None


@dataclass(frozen=True)
class AcpDiscoveryReport:
    """一次 ``list_native_sessions`` 的核对结果。

    ``ledger_only``
        Driver 账本里有、``session/list`` 看不见的会话。**这是常态**（实测：
        新进程看不见别的进程建的会话），不是错误。
    ``agent_only``
        ``session/list`` 报了、账本里没有的 id。归属未知，因此**不**返回给上层
        （把它挂到某条 Binding 上就是在猜）。
    ``method_error``
        ``session/list`` 本身失败时的错误文本。
    """

    binding_id: str
    checked_at: datetime
    ledger_only: tuple[str, ...] = ()
    agent_only: tuple[str, ...] = ()
    confirmed: tuple[str, ...] = ()
    method_error: str | None = None


#: 目录探测的缓存寿命（秒）。十分钟是一个折中：登录这件事的变化频率以「用户去
#: 终端跑一次命令」计，而每次失效都要多起一个子进程。
CATALOG_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class _CatalogSnapshot:
    """一条 Binding 的「上一次问出来的样子」（AD-157）。

    它同时是三个问题的答案，因为这三个答案来自**同一次** ``session/new``：
    这台引擎有哪些模型、它此刻用哪个、以及**它到底让不让我们开会话**（登录态）。
    分三次去问就会出现「目录说没登录、登录行说不知道」这种自相矛盾的界面——
    与 :mod:`app.api.binding_status` 那条「取数只有一处」是同一条纪律。
    """

    fetched_at: datetime
    models: tuple[ModelDescriptor, ...] = ()
    current_model_id: str | None = None
    #: ``configOptions`` 里那一项模型选项自己的 ``id``（AD-158）。走
    #: ``session/set_config_option`` 换模型时要发它；``None`` = 这次会话结果里
    #: 没有这一项（那时按 :data:`CONFIG_CATEGORY_MODEL` 兜底）。
    model_option_id: str | None = None
    mode_ids: tuple[str, ...] = ()
    mode_definitions: tuple[Any, ...] = ()
    current_mode_id: str | None = None
    thought_levels: tuple[str, ...] = ()
    thought_option_id: str | None = None
    current_thought_level: str | None = None
    mode_option_id: str | None = None
    approval_option_id: str | None = None
    approval_ids: tuple[str, ...] = ()
    approval_definitions: tuple[Any, ...] = ()
    current_approval_id: str | None = None
    config_options: tuple[dict[str, Any], ...] = ()
    auth_state: Literal["signed_in", "signed_out", "unknown"] = "unknown"
    engine_default_available: bool = False
    degraded: bool = False
    diagnostics: tuple[str, ...] = ()
    #: 登录态是 ``signed_out`` 时的下一步（引擎卡登录行读它）。
    auth_hint: str | None = None
    #: 这次连都没连上时的那句人话 + 修法（AD-159）。登录态因此仍是 ``unknown``
    #: ——进程没起来时我们**不知道**用户登没登录，编一个 ``signed_out`` 会把他
    #: 支去做一件毫无用处的登录；但「为什么问不出来」是知道的，就挂在这里，
    #: ``read_auth_state`` 把它当 ``hint`` 送上 ``/bindings/{id}/status``。
    connect_hint: str | None = None

    def is_fresh(self, now: datetime, ttl: float = CATALOG_TTL_SECONDS) -> bool:
        return (now - self.fetched_at).total_seconds() < (min(ttl, 5.0) if self.degraded else ttl)


@dataclass
class _PendingFsWrite:
    """一次等审批的 ``fs/write_text_file``（AD-152）。

    与普通权限请求的区别只在**回执形状**：权限请求回
    ``{"outcome": {...}}``，这条要回一个真正的写结果（或一个错误）。因此它必须
    被单独记一笔，否则 ``resolve_interaction`` 会拿权限的形状去回 fs 的请求。
    """

    path: str
    content: str


@dataclass
class _RuntimeState:
    handle: RuntimeHandle
    conversation: Conversation
    connection: AcpConnection
    translator: AcpTranslator
    session_id: str
    #: AD-152：客户端 fs 的边界。``None`` = 本次连接根本没声明 fs 能力。
    workspace_root: str | None = None
    prompt_capabilities: dict[str, Any] = field(default_factory=dict)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: AD-118：这条 Binding 的审批档（``ask`` / ``auto`` / ``deny``）。
    approval_mode: str = "ask"
    permission_pending: bool = False
    config_pending: set[str] = field(default_factory=set)
    available_modes: tuple[Any, ...] = ()
    session_result: dict[str, Any] = field(default_factory=dict)
    config_revision: int = 0
    skill_plan: Any = None
    runtime_spec: AcpAgentSpec | None = None
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    #: request_id -> 待审批的写请求。
    fs_writes: dict[str, _PendingFsWrite] = field(default_factory=dict)
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    prompt_task: "asyncio.Task[None] | None" = None
    computer_waiting: bool = False
    computer_use_enabled: bool | None = None
    computer_use_revision: str | None = None
    interrupt_requested: bool = False
    client_interactions: Any = None
    transport_retired: bool = False
    closed: bool = False


class AcpDriver:
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
        self._computer_use_plans: dict[str, dict] = {}
        self._computer_use_contexts: dict[str, tuple[Any, dict]] = {}
        self._computer_use_effective: dict[str, Any] = {}
        self._binding_skill_plans: dict[str, Any] = {}
        self._skill_projection_verification = None
        self._runtime_counter = 0

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #

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

    def _warn(self, message: str) -> None:
        self.warnings.append(message)

    # ------------------------------------------------------------------ #
    # Probe / 能力 / 模型
    # ------------------------------------------------------------------ #

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
        except Exception:
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

    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog:
        """这条 Binding 能选哪些模型。**没有缓存就现探一次**（AD-157）。

        真机现象（批次三十三）：引擎卡写 ``Model: NOT SET``、``?binding=`` 的目录
        端点回 ``models: []``、输入区连模型选择器都没有——因为此前这份目录**只在
        一次成功的 session/new 之后**才有内容，而用户还没发过第一句话。也就是说
        「还没聊过」被渲染成了「这台引擎没有模型」，两件完全不同的事。

        所以这里改成：缓存里没有（或过期了）就起一条短命连接、开一个会话、把它
        报出来的模型抄下来、**随即把那个会话收掉**。探测一次的代价是一个子进程，
        缓存 :data:`CATALOG_TTL_SECONDS` 秒；真正的会话（``start_runtime``）跑起来
        时会用它自己那次 ``session/new`` 的结果覆盖这份快照——那份更新。

        探不出来不是错误：撞上「需要先登录」时返回**空目录 + degraded + 一条说得
        出修法的 diagnostics**，而不是抛异常（N §13.1：空目录加一句人话是合法且
        有意义的返回，一个 500 不是）。
        """
        self._assert_binding(binding)
        capabilities = await self.get_capabilities()
        snapshot = await self._catalog_snapshot(binding)
        return self._model_catalog_from_snapshot(binding, snapshot, capabilities)

    async def runtime_model_catalog(self, runtime: RuntimeHandle, binding: AgentBinding) -> ModelCatalog:
        state = self._state(runtime)
        snapshot = self._with_connection_auth(self._snapshot_from_session(state.session_result), state.connection)
        return self._model_catalog_from_snapshot(binding, snapshot, await self.get_capabilities())

    @staticmethod
    def _model_catalog_from_snapshot(binding, snapshot, capabilities) -> ModelCatalog:
        return ModelCatalog(
            binding_id=binding.id,
            mode=capabilities.models.mode,
            models=snapshot.models,
            # Binding 自己写了默认模型就以它为准；没写才用引擎此刻在用的那个。
            default_model_id=binding.default_model_id or snapshot.current_model_id,
            default_provider_id=binding.default_provider_id,
            supports_reasoning=bool(capabilities.models.reasoning) or bool(snapshot.thought_option_id and snapshot.thought_levels),
            degraded=snapshot.degraded,
            engine_default_available=snapshot.engine_default_available,
            diagnostics=snapshot.diagnostics,
        )

    # ------------------------------------------------------------------ #
    # 目录探测（AD-157）
    # ------------------------------------------------------------------ #

    async def _catalog_snapshot(self, binding: AgentBinding) -> _CatalogSnapshot:
        """缓存里那份还新鲜就用它，否则探一次。"""
        cached = self._catalog.get(binding.id)
        stamp = self._config_stamp(binding)
        if (cached is not None and cached.is_fresh(_now())
                and self._catalog_config_stamps.get(binding.id, ()) == stamp):
            return cached
        snapshot = await self._probe_catalog(binding)
        snapshot = self._retain_auth_state(binding.id, snapshot)
        self._catalog[binding.id] = snapshot
        self._catalog_config_stamps[binding.id] = stamp
        return snapshot

    async def preview_model_settings(self, binding: AgentBinding, model_id: str) -> tuple[ModelCatalog, dict]:
        """Discover model-dependent controls without sending a prompt or changing defaults."""
        snapshot = await self._catalog_snapshot(binding)
        if snapshot.model_option_id and self.quirks.model_id_format == "effort_suffix":
            base = _strip_effort_suffix(model_id)
            ids = {item.model_id for item in snapshot.models}
            if model_id not in ids and base in ids:
                model_id = base
        if snapshot.current_model_id != model_id and not snapshot.degraded:
            key = (binding.id, model_id)
            stamp = self._config_stamp(binding)
            cached = self._selection_catalog.get(key)
            if cached and cached[0] == stamp and cached[1].is_fresh(_now()):
                snapshot = cached[1]
            else:
                snapshot = await self._probe_catalog(binding, model_id=model_id)
                if len(self._selection_catalog) >= 64:
                    self._selection_catalog.pop(next(iter(self._selection_catalog)))
                self._selection_catalog[key] = (stamp, snapshot)
        catalog = self._model_catalog_from_snapshot(binding, snapshot, await self.get_capabilities())
        return catalog.model_copy(update={"default_model_id": snapshot.current_model_id}), self._controls_from_snapshot(binding, snapshot)

    def _config_stamp(self, binding: AgentBinding) -> tuple:
        """Invalidate model discovery after external configuration changes."""
        import shutil
        environment = self.agent_spec.build_env()
        stamps = []
        for path in configuration_paths(environment, self.preset.id if self.preset else None):
            try:
                stat = path.stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                stamps.append((str(path), None))
        # An installed engine/adapter may change without changing its config.
        # This also works for custom connections with no named preset.
        command = self.agent_spec.command
        executable = shutil.which(command[0], path=environment.get("PATH")) if command else None
        for raw in ([executable] if executable else []) + [arg for arg in command[1:] if Path(arg).is_absolute()]:
            try:
                path = Path(raw)
                stat = path.stat()
                stamps.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                stamps.append((raw, None))
        if self.preset is None or not self.preset.config_root_default:
            return tuple(stamps)
        explicit_root = environment.get(self.preset.config_root_env or "")
        root = Path(explicit_root or self.preset.config_root_default).expanduser()
        if not explicit_root and self.preset.config_xdg_dir and environment.get("XDG_CONFIG_HOME"):
            root = Path(environment["XDG_CONFIG_HOME"]).expanduser() / self.preset.config_xdg_dir
        roots = [root]
        workspace = self._workspace_root_for(binding.id)
        if workspace:
            roots.append(Path(workspace) / (self.preset.workspace_config_dir if self.preset.workspace_config_dir is not None else Path(self.preset.config_root_default).name))
        for folder in roots:
            for name in self.preset.config_watch_files:
                paths = sorted(folder.glob(name)) if "*" in name else [folder / name]
                if not paths:
                    stamps.append((str(folder / name), None))
                for path in paths:
                    try:
                        stat = path.stat()
                        stamps.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
                    except OSError:
                        stamps.append((str(path), None))
        return tuple(stamps)

    async def _probe_catalog(self, binding: AgentBinding, *, model_id: str | None = None) -> _CatalogSnapshot:
        """Read native selectors in disposable storage, without a model prompt."""
        family = self.preset.id if self.preset else None
        if connection_configured(self.agent_spec, family) is False:
            hint = login_diagnostic(self._login_command())
            return _CatalogSnapshot(fetched_at=_now(), auth_state="signed_out",
                degraded=True, diagnostics=(hint,), auth_hint=hint)
        with isolated_agent_spec(self.agent_spec,
                preset_id=self.preset.id if self.preset else None,
                workspace=self._workspace_root_for(binding.id)) as spec:
            try:
                await prepare_discovery_credentials(spec, self.agent_spec.build_env(),
                    preset_id=self.preset.id if self.preset else None)
            except DiscoveryCredentialsUnavailable as exc:
                return _CatalogSnapshot(fetched_at=_now(), degraded=True,
                    auth_state="signed_out" if exc.signed_out else "unknown",
                    diagnostics=(str(exc),), auth_hint=str(exc))
            return await self._probe_catalog_in_scope(binding, spec, model_id=model_id)

    async def _probe_catalog_in_scope(self, binding, spec, *, model_id=None):
        try:
            connection, _init = await self._connect(agent_spec=spec)
        except (AcpTransportError, AcpRpcError) as exc:
            return _CatalogSnapshot(
                fetched_at=_now(),
                degraded=True,
                diagnostics=(_connect_diagnostic(exc),),
                connect_hint=_connect_hint(exc),
            )
        result: Any = None
        try:
            result, _params = await self._new_session(
                connection,
                CreateSessionOptions(
                    workspace_root=spec.cwd
                ),
            )
            if model_id:
                result = await self._select_probe_model(connection, result, model_id)
        except AcpRpcError as exc:
            failure = self._auth_failure(exc)
            if failure is not None:
                snapshot = _CatalogSnapshot(
                    fetched_at=_now(),
                    auth_state="signed_out",
                    degraded=True,
                    diagnostics=(login_diagnostic(self._login_command()),),
                    auth_hint=failure.hint,
                )
            else:
                snapshot = _CatalogSnapshot(
                    fetched_at=_now(),
                    degraded=True,
                    diagnostics=(f"引擎没有开出会话：{exc.message}",),
                )
        except AcpTransportError as exc:
            snapshot = _CatalogSnapshot(
                fetched_at=_now(),
                degraded=True,
                diagnostics=(_connect_diagnostic(exc),),
                connect_hint=_connect_hint(exc),
            )
        else:
            await self._await_connection_auth(connection)
            snapshot = self._with_connection_auth(self._snapshot_from_session(result), connection)
        finally:
            await self._close_probe_session(connection, result)
        return snapshot

    @staticmethod
    def _capture_auth_status(connection, method, params):
        # ACP extension advertised by native adapters. No brand-name branching
        # and no interpreting silence as a logged-out account.
        if method != "_auth/status_update" or not isinstance(params, Mapping):
            return
        status = params.get("authStatus")
        kind = status.get("kind") if isinstance(status, Mapping) else None
        if kind == "none":
            connection.native_auth_state = "signed_out"
        elif kind in {"account", "api_key", "gateway", "external"}:
            connection.native_auth_state = "signed_in"
        if kind in {"none", "account", "api_key", "gateway", "external"}:
            event = getattr(connection, "native_auth_event", None)
            if event is not None:
                event.set()

    @staticmethod
    async def _await_connection_auth(connection):
        # Advertised auth is a separate asynchronous push. session/new can
        # finish first; closing immediately would discard the login result.
        if (getattr(connection, "native_auth_expected", False)
                and getattr(connection, "native_auth_state", None) is None):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(connection.native_auth_event.wait(), AUTH_STATUS_WAIT_SECONDS)

    def _with_connection_auth(self, snapshot, connection):
        auth = getattr(connection, "native_auth_state", None)
        if auth == "signed_out":
            hint = login_diagnostic(self._login_command())
            return replace(snapshot, models=(), model_option_id=None,
                engine_default_available=False, auth_state=auth, degraded=True,
                auth_hint=hint, diagnostics=(hint,))
        if auth is None and getattr(connection, "native_auth_expected", False):
            return replace(snapshot, models=(), model_option_id=None,
                engine_default_available=False, auth_state="unknown", degraded=True,
                diagnostics=("暂时无法读取引擎登录状态",))
        return replace(snapshot, auth_state=auth) if auth else snapshot

    async def _select_probe_model(self, connection: AcpConnection, result: dict, model_id: str) -> dict:
        """Use the transport's session-local selector for a disposable preview."""
        config = self._config_options(result)
        if config.model_option_id:
            reply = await self._call_config_option(connection, result["sessionId"], config.model_option_id, model_id)
            if not isinstance(reply, Mapping) or not isinstance(reply.get("configOptions"), list):
                raise AcpTransportError("引擎未返回模型配置")
            result = {**result, **reply}
            if self._config_options(result).current_model_id != model_id:
                raise AcpTransportError("引擎未确认所选模型")
        elif self.quirks.model_switch == "set_model":
            reply = await connection.call("session/set_model", {"sessionId": result["sessionId"], "modelId": model_id}, timeout=self._call_timeout)
            result = {**result, **(reply if isinstance(reply, Mapping) else {})}
            result["models"] = {**result.get("models", {}), "currentModelId": model_id}
        return result

    async def _close_probe_session(self, connection: AcpConnection, result: Any) -> None:
        """收掉探测会话：能取消就取消，然后一律关连接。

        协议里没有「关闭会话」这个方法（只有 ``session/cancel`` 这个**通知**），
        所以这里不去猜一个 ``session/close`` 出来调——发一个引擎不认识的方法换来
        的只是一条 -32601 加一行噪音日志。
        """
        session_id = None
        if isinstance(result, Mapping):
            raw = result.get("sessionId")
            session_id = raw if isinstance(raw, str) and raw else None
        if session_id is not None:
            with contextlib.suppress(AcpTransportError):
                connection.notify("session/cancel", {"sessionId": session_id})
        await connection.aclose()

    def _snapshot_from_session(self, result: Any) -> _CatalogSnapshot:
        """一次成功的 ``session/new`` / ``session/resume`` 结果 → 快照。

        标准 ``configOptions`` 的模型项优先；旧版 ``models`` 用作兼容来源。
        """
        config = self._config_options(result)
        # The current session's standard selectors outrank legacy preset data.
        models = config.models if config.model_option_id else models_from_session_result(result)
        models = offered_models(tuple(self._label_model(model) for model in models),
            self.agent_spec, self.preset.id if self.preset else "", self._default_cwd)
        if self.quirks.model_id_format == "effort_suffix" and not config.model_option_id:
            levels: dict[str, list[str]] = {}
            for model in models:
                base = _strip_effort_suffix(model.model_id)
                if base != model.model_id:
                    effort = model.model_id[len(base) + 1:-1]
                    if effort not in levels.setdefault(base, []):
                        levels[base].append(effort)
            models = tuple(model.model_copy(update={"reasoning_levels": tuple(levels.get(_strip_effort_suffix(model.model_id), model.reasoning_levels))}) for model in models)
        current = config.current_model_id if config.model_option_id else current_model_from_session_result(result)
        # A custom route may report only its active model. Keep that observed
        # selection visible without inventing a provider-wide model catalog.
        if current and not any(model.model_id == current for model in models):
            models = (*models, *offered_models((self._label_model(ModelDescriptor(model_id=current)),),
                self.agent_spec, self.preset.id if self.preset else "", self._default_cwd))
        modes = tuple(
            mode_id
            for mode_id in (
                _mode_id_of(mode) for mode in self._available_modes(result)
            )
            if mode_id
        )
        if self.quirks.mode_semantics == "thought_level":
            models = tuple(model.model_copy(update={"reasoning_levels": config.thought_levels or modes}) for model in models)
        elif config.thought_option_id:
            models = tuple(model.model_copy(update={"reasoning_levels": config.thought_levels}) for model in models)
        # A successfully opened session without a model selector can use its
        # native default. Stale preset claims must not invent a missing picker
        # or turn this valid state into a discovery failure.
        engine_default = bool(isinstance(result, Mapping) and result.get("sessionId") and not models and not config.model_option_id)
        return _CatalogSnapshot(
            fetched_at=_now(),
            models=models,
            current_model_id=current,
            model_option_id=config.model_option_id,
            mode_ids=modes,
            mode_definitions=self._available_modes(result),
            mode_option_id=config.mode_option_id,
            approval_option_id=config.approval_option_id,
            approval_ids=config.approval_ids,
            approval_definitions=config.approval_definitions,
            current_approval_id=config.current_approval_id,
            config_options=config.config_options,
            current_mode_id=(config.current_mode_id if config.mode_option_id else
                             (result.get("modes", {}).get("currentModeId") if isinstance(result, Mapping) and isinstance(result.get("modes"), Mapping) else None)),
            thought_levels=config.thought_levels,
            thought_option_id=config.thought_option_id,
            current_thought_level=config.current_thought_level,
            auth_state="signed_in" if getattr(self.preset, "session_creation_proves_auth", True) else "unknown",
            engine_default_available=engine_default,
            degraded=not models and not engine_default,
            diagnostics=() if models or engine_default else ("引擎开出了会话，但没有报告可选模型",),
        )

    def _label_model(self, model: ModelDescriptor) -> ModelDescriptor:
        """带 ``[effort]`` 后缀的目录 id：**显示**去掉后缀，**id 保留原文**（AD-158）。

        两件事必须分开：后缀是这台引擎发 RPC 时要求的形状（裸 id 回 -32603），
        而列表里给用户看的应该是模型本身的名字。把后缀塞进显示名，用户会以为
        「同一个模型有六个」；把后缀从 id 里剥掉，下一次 ``session/set_model``
        就会被拒。
        """
        if self.quirks.model_id_format != "effort_suffix":
            return model
        base = _strip_effort_suffix(model.model_id)
        if base == model.model_id or model.display_name:
            return model
        return model.model_copy(update={"display_name": base})

    def _login_command(self) -> str | None:
        return self.preset.login_command if self.preset is not None else None

    def _engine_label(self) -> str:
        """错误文案里那个「谁」。引擎自报的名字优先，其次预设的标签。"""
        if self._backend_name:
            return self._backend_name
        if self.preset is not None:
            return self.preset.label
        return self.backend_id

    def _auth_failure(self, exc: AcpRpcError) -> FailureHint | None:
        """这次拒绝是不是「还没登录」。不是就 ``None``（不编修法）。"""
        if not looks_like_auth_error(exc.code, exc.message, exc.data):
            return None
        return auth_required(
            self._engine_label(),
            login_command=self._login_command(),
            auth_methods=auth_methods_from_error(exc.data),
        )

    def _auth_error(self, binding_id: str, exc: AcpRpcError) -> AuthRequiredError | None:
        """认得出「要先登录」就造一个带修法的异常，顺手把登录态记进缓存。"""
        failure = self._auth_failure(exc)
        if failure is None and looks_like_missing_credentials(exc.code, exc.message):
            failure = FailureHint(code="auth_required", message="引擎缺少可用的模型凭据", hint="在引擎中配置对应模型服务的 API Key，然后重试。")
        if failure is None:
            return None
        self._catalog[binding_id] = _CatalogSnapshot(
            fetched_at=_now(),
            auth_state="signed_out",
            degraded=True,
            diagnostics=(login_diagnostic(self._login_command()),),
            auth_hint=failure.hint,
        )
        return AuthRequiredError(
            failure.message,
            failure=failure,
            auth_methods=auth_methods_from_error(exc.data),
        )

    async def read_engine_settings(self, binding: AgentBinding) -> EngineSettings:
        """ACP 协议没有「读引擎配置」这一面 → 空设置（批次十三第 1 件）。

        ACP 只暴露 ``session/new`` 的协商结果，没有任何读取代理端持久配置的方法；
        猜一个配置文件路径出来读，等于替某个具体引擎做假设（N §5.4）。
        """
        self._assert_binding(binding)
        return EngineSettings(
            binding_id=binding.id,
            diagnostics=("ACP 协议没有读取代理端配置的方法，本层无来源",),
        )

    async def read_auth_state(self, binding: AgentBinding) -> AuthState:
        """ACP 上的登录态是 ``unknown``（批次十九第 1 件 / AD-93）。

        协议里确实有 ``authenticate`` 这一面，但那是「需要时把用户领去登录」，
        **没有**一个「你现在登着吗」的查询方法：agent 的登录归它自己的产品管
        （AD-82：登录态不跨引擎共享）。因此这里如实报未知，能力表上
        ``auth.state_reporting`` 也停在 ``unknown``——两处必须一致。

        登录模型报 ``own-auth``：这一条不是猜的，是 ACP 的形状本身决定的
        （凭据不经 Kaus 托管）。状态未知与模型已知并不矛盾。
        """
        self._assert_binding(binding)
        snapshot = self._catalog.get(binding.id)
        if snapshot is None or not snapshot.is_fresh(_now()):
            try:
                snapshot = await self._catalog_snapshot(binding)
            except Exception:  # noqa: BLE001 - 问不出来仍然是一个可渲染的状态
                snapshot = None
        if snapshot is None or snapshot.auth_state == "unknown":
            # AD-159：状态仍是 ``unknown``（进程都没起来，登没登录我们不知道），
            # 但「为什么问不出来 + 怎么修」是知道的——把它当 hint 带上，
            # ``/bindings/{id}/status`` 的 ``auth.hint`` 于是说得出人话。
            return unknown_auth_state(
                "own-auth",
                hint=snapshot.connect_hint if snapshot is not None else None,
                checked_at=snapshot.fetched_at if snapshot is not None else None,
            )
        return AuthState(
            state=snapshot.auth_state,
            model="own-auth",
            checked_at=snapshot.fetched_at,
            hint=snapshot.auth_hint if snapshot.auth_state == "signed_out" else None,
        )

    # ------------------------------------------------------------------ #
    # 能力投射
    # ------------------------------------------------------------------ #

    # --- 工作目录文件投影（批次四十四 / AD-166） ------------------------------ #

    @property
    def _instructions_file(self) -> str | None:
        """项目指令落到工作目录里的哪个文件。目录没登记就是 ``None``（不猜）。"""
        if self.preset is None:
            return None
        return self.preset.workspace.instructions_file

    def workspace_conventions(self, binding: AgentBinding | None = None) -> dict[str, Any]:
        """这台引擎在工作目录里认哪些文件（``projection/_meta`` 用，批次四十四）。

        **可选**方法：接入层用 ``getattr`` 探它，没有就当 ``null``。它存在的理由是
        公共层不该 import 任何一家的预设目录——「哪个引擎读哪个文件」是 Driver 的
        私有知识（N §3，与批次四十三把 MCP 翻译放在 Driver 侧是同一条界线）。
        """
        del binding  # 约定来自预设，与具体哪条 Binding 无关
        return {"instructionsFile": self._instructions_file}

    def _workspace_of(self, project: Project, binding: AgentBinding) -> str | None:
        """文件投影的目标目录 = 项目的工作目录。

        以 ``Project.workspace_root`` 为准（AD-166：工作目录就是项目的边界），
        它没填时才回落到 Binding 上那一个——后者本来是给客户端 fs 划边界用的
        （AD-152），但两处指的是同一个地方，有一处填了就不该说「无处可写」。
        """
        if project.workspace_root and project.workspace_root.strip():
            return project.workspace_root
        return self._workspace_root_for(binding.id)

    def _instruction_rows(
        self, effective_capabilities: EffectiveCapabilities
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            (entry.capability_type, entry.capability_id)
            for entry in instruction_entries(effective_capabilities)
        )

    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
        *,
        dry_run: bool = True,
        adopt: bool = False,
    ) -> ProjectionResult:
        """Project MCP uses ACP metadata or a verified native runtime adapter.

        Native adapters own their private settings and connection receipts.
        Workspace instructions use a managed file block; adopt never authorizes
        replacing content outside that block.
        """
        self._assert_binding(binding)
        capabilities = await self.get_capabilities()
        applied: list[ProjectionEntry] = []
        unsupported: list[ProjectionEntry] = []
        warnings: list[str] = list(effective_capabilities.warnings)
        from drivers.skill_execution.common import selection_of, is_skill_entry
        skill_adapter = self._skill_adapter()
        skill_plan = None
        if selection_of(effective_capabilities) is None and not dry_run:
            self._binding_skill_plans.pop(binding.id, None)
        if skill_adapter is None and selection_of(effective_capabilities) is not None and not dry_run:
            raise UnsupportedCapabilityError("当前引擎尚未提供项目技能范围控制；可使用原生技能模式")
        if skill_adapter is not None and selection_of(effective_capabilities) is not None:
            skill_plan = await skill_adapter.project(self.agent_spec, binding, effective_capabilities,
                self._workspace_of(project, binding), dry_run=dry_run,
                runtime_info=(self._backend_name, self._backend_version))
            applied.extend(skill_plan.report.applied)
            unsupported.extend(skill_plan.report.unsupported)
            warnings.extend(skill_plan.report.warnings)
            if not dry_run:
                self._binding_skill_plans[binding.id] = skill_plan
                self._remember_skill_projection(skill_plan)

        instructions = materialize_block(
            rows=self._instruction_rows(effective_capabilities),
            body=compose_instructions(effective_capabilities),
            workspace_root=self._workspace_of(project, binding),
            filename=self._instructions_file,
            marker=INSTRUCTIONS_MARKER,
            project_id=project.id,
            dry_run=dry_run,
        )
        applied.extend(instructions.applied)
        unsupported.extend(instructions.unsupported)
        warnings.extend(instructions.warnings)
        # 形状不对的指令条目进不了拼接，但**必须**有去处（D-03）。
        for broken in invalid_instruction_entries(effective_capabilities):
            unsupported.append(
                ProjectionEntry(
                    capability_type=broken.capability_type,
                    capability_id=broken.capability_id,
                    level=SupportLevel.UNSUPPORTED,
                    reason="invalid_config",
                    detail=INVALID_DETAIL,
                )
            )
        instruction_coords = {
            (row.capability_type, row.capability_id)
            for row in (*applied, *unsupported)
        }

        for entry in effective_capabilities.entries:
            if skill_plan is not None and is_skill_entry(entry):
                continue
            if (entry.capability_type, entry.capability_id) in instruction_coords:
                continue  # 文件投影那一面已经给它出过一行了
            (verdict,) = evaluate_support([entry.capability_type], capabilities)
            projection = ProjectionEntry(
                capability_type=entry.capability_type,
                capability_id=entry.capability_id,
                level=verdict.level,
                target_ref=(
                    f"acp://{binding.id}/{'runtime' if self.preset and self.preset.project_mcp_via_runtime else 'session-new'}/mcpServers/{entry.capability_id}"
                    if verdict.projectable
                    else None
                ),
                detail=verdict.reason,
                reason=None if verdict.projectable else "not_mapped",
            )
            (applied if verdict.projectable else unsupported).append(projection)
        if any(entry.capability_type == "mcp" for entry in effective_capabilities.entries):
            warnings.append("项目 MCP 在会话启动或恢复时应用")
        return ProjectionResult(
            binding_id=binding.id,
            applied=tuple(applied),
            unsupported=tuple(unsupported),
            warnings=tuple(warnings),
            dry_run=dry_run,
            backup_path=instructions.backup_path,
        )

    async def inspect_drift(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities | None = None,
    ) -> DriftReport:
        """Inspect managed workspace instructions.

        MCP configuration is verified by each runtime adapter. It does not
        participate in this shared file-drift report.
        """
        self._assert_binding(binding)
        if effective_capabilities is None:
            # 没有期望侧就没有对账。这里仍回 in_sync=True 是因为本 Driver 在没有
            # 文件投影目标时确实没有任何持久状态——但 entries 为空，界面不会把它
            # 显示成「对过账、一切正常」。
            return DriftReport(binding_id=binding.id, checked_at=_now(), in_sync=True)
        entries = inspect_block_drift(
            rows=self._instruction_rows(effective_capabilities),
            body=compose_instructions(effective_capabilities),
            workspace_root=self._workspace_of(project, binding),
            filename=self._instructions_file,
            marker=INSTRUCTIONS_MARKER,
            project_id=project.id,
        )
        report = DriftReport(
            binding_id=binding.id, checked_at=_now(), in_sync=True, entries=entries
        )
        return report.model_copy(update={"in_sync": report.drifted_count == 0})

    # ------------------------------------------------------------------ #
    # Native Session
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Card Runtime
    # ------------------------------------------------------------------ #

    async def session_options_for(
        self, conversation: Conversation, effective: EffectiveCapabilities, *, workspace_root: str | None = None
    ) -> SessionProjection | None:
        """Build configuration for the next runtime without changing active sessions.

        MCP uses session/new or the preset's native runtime projection. Skill adapters carry
        their verified selection through new and resume metadata, with a separate
        process spec for each conversation. A required projection failure aborts
        startup rather than silently falling back to ambient native Skills.
        """
        from drivers.skill_execution.common import selection_of
        metadata: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        warnings: list[str] = []
        mcp_projection = project_mcp_servers(effective)
        if mcp_projection.servers and not (self.quirks.mcp_via_session_new or
                (self.preset and self.preset.project_mcp_via_runtime)):
            raise UnsupportedCapabilityError("当前引擎不支持项目 MCP；请在项目中停用对应服务后重试")
        warnings.extend(mcp_projection.warnings)
        binding = self._binding_snapshot(conversation.agent_binding_id)
        adapter = self._skill_adapter()
        if selection_of(effective) is None:
            self._skill_plans.pop(conversation.id, None)
        if selection_of(effective) is not None and (adapter is None or binding is None):
            raise UnsupportedCapabilityError("当前引擎尚未提供项目技能范围控制；可使用原生技能模式")
        if adapter is not None and binding is not None and selection_of(effective) is not None:
            await self.get_capabilities()
            project = getattr(adapter, "project_session", adapter.project)
            session_args = {"conversation": conversation} if hasattr(adapter, "project_session") else {}
            plan = await project(self.agent_spec, binding, effective,
                workspace_root or self._workspace_root_for(binding.id), dry_run=False,
                runtime_info=(self._backend_name, self._backend_version), **session_args)
            self._skill_plans[conversation.id] = plan
            self._remember_skill_projection(plan)
            if plan.metadata:
                metadata["_meta"] = plan.metadata
                summary["skills"] = list(plan.names)
            warnings.extend(plan.report.warnings)
            warnings.extend(item.detail or "项目技能未应用" for item in plan.report.unsupported)
        if mcp_projection.servers:
            metadata["mcpServers"] = [dict(s) for s in mcp_projection.servers]
            summary["mcpServers"] = list(mcp_projection.names)
        from drivers.computer_use.integration import prepare_projection
        prepared = self._skill_plans.get(conversation.id)
        metadata = await prepare_projection(
            prepared.spec if prepared is not None and prepared.spec is not None else self.agent_spec,
            self._runtime_adapter(), binding, conversation, effective, metadata,
            supports_mcp=bool(self.quirks.mcp_via_session_new or
                              (self.preset and self.preset.project_mcp_via_runtime)),
        )
        self._computer_use_effective[conversation.id] = effective
        self._computer_use_plans[conversation.id] = dict(metadata.get("_kausComputerUse", {}))
        self._computer_use_contexts[conversation.id] = (
            prepared.spec if prepared is not None and prepared.spec is not None else self.agent_spec,
            metadata,
        )
        if "mcpServers" in metadata:
            summary["mcpServers"] = [server["name"] for server in metadata["mcpServers"]]
        remember_options = getattr(self._runtime_adapter(), "remember_session_options", None)
        if remember_options is not None:
            # Cold terminal launches can bypass runtime preparation. Retain the
            # latest projection, including an explicitly empty MCP set.
            prepared = self._skill_plans.get(conversation.id)
            remember_options(prepared.spec if prepared is not None and prepared.spec is not None
                             else self.agent_spec, conversation, metadata)
        for warning in warnings:
            self._warn(warning)
        if not metadata:
            return None
        return SessionProjection(
            options=CreateSessionOptions(
                title=conversation.title,
                metadata=metadata,
            ),
            summary=summary,
            warnings=tuple(warnings),
        )

    def supports_workspace_override(self) -> bool:
        return True

    async def workspace_session_projection(self, conversation, effective, *, workspace_root):
        projection = await self.session_options_for(conversation, effective, workspace_root=workspace_root)
        projection = projection or SessionProjection(options=CreateSessionOptions(title=conversation.title))
        return projection.model_copy(update={"options": projection.options.model_copy(
            update={"workspace_root": workspace_root})})

    def _skill_adapter(self):
        from drivers.skill_execution import adapter_for
        return adapter_for(self.preset.skill_projection_adapter if self.preset else None)

    def _runtime_adapter(self):
        from importlib import import_module
        module = self.preset.runtime_adapter if self.preset else None
        return import_module(module) if module else self._skill_adapter()

    async def computer_use_status(self, binding=None):
        from drivers.computer_use.integration import ambient_mcp, native_status
        adapter = self._runtime_adapter()
        result = dict(await native_status(adapter, self.agent_spec, binding, force=True))
        for candidate in ambient_mcp(self.agent_spec, adapter):
            if result.get(candidate.capability, {}).get("status") != "ready":
                result[candidate.capability] = candidate.to_dict()
        return result

    async def recover_computer_use(self, reference):
        recover = getattr(self._runtime_adapter(), "recover_computer_use", None)
        return await recover(self.agent_spec, reference) is True if recover is not None else False

    def skill_inventory_roots(self, projects, bindings):
        adapter = self._skill_adapter()
        if adapter is None:
            return ()
        provider = getattr(adapter, "inventory_roots", None)
        if callable(provider):
            return provider(self.agent_spec, projects, bindings)
        from drivers.skill_execution.inventory import driver_roots
        return driver_roots(self.agent_spec, adapter.__name__.rsplit(".", 1)[-1], projects, bindings)

    def _remember_skill_projection(self, plan):
        if plan.root is not None and self._capabilities is not None:
            self._skill_projection_verification = self._skill_projection_key()
            self._mark_skill_capability()

    def _skill_projection_key(self):
        import hashlib
        return (self._backend_name, self._backend_version,
                hashlib.sha256(self.agent_spec.model_dump_json().encode()).hexdigest())

    def _mark_skill_capability(self):
        if self._capabilities is not None:
            levels = dict(self._capabilities.capability_projection)
            levels.update(skills=SupportLevel.ADAPTED)
            self._capabilities = self._capabilities.model_copy(update={"capability_projection": levels})

    def requires_project_projection(self, binding: AgentBinding) -> bool:
        return self._skill_adapter() is not None

    def coordinator_selection(self, model_id: str, reasoning_mode: str | None) -> str:
        # The coordinator shares the normal session path. Transport-specific
        # effort encoding is applied only after the session protocol is known.
        return model_id

    def group_context_isolation(self) -> bool:
        from drivers.group_execution import adapter_for
        adapter = adapter_for(self.preset.group_isolation_adapter if self.preset else None)
        # Every runtime already owns a process and an explicitly created session.
        # Optional engine hooks suppress ambient memory; they are not a second
        # implementation of the engine or a whitelist of coordinator products.
        return adapter is None or adapter.supported(self.agent_spec)

    def supports_execution_policy(self, policy: str) -> bool:
        return policy == "project" or (policy == "context-only" and bool(
            getattr(self.preset, "restricted_execution_adapter", None)))

    async def restricted_session_projection(self, conversation, *, policy, workspace_root):
        if policy != "context-only" or not self.supports_execution_policy(policy):
            raise UnsupportedCapabilityError("当前 Agent 尚未适配此模块的执行范围。")
        return SessionProjection(options=CreateSessionOptions(workspace_root=workspace_root,
            metadata={"_kausExecutionPolicy": policy}), summary={"executionPolicy": [policy]})

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
                except Exception:
                    # aclose marks its connection closed before terminating
                    # children. A failed close is not proof of termination.
                    pass
                stopped = True
                if confirm_native is not None:
                    try:
                        stopped = await asyncio.wait_for(confirm_native(
                            state.runtime_spec, state.session_id, state.runtime_metadata), timeout=10)
                    except Exception:
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
                except Exception:
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
                    except Exception:
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
                    except Exception:
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
        except Exception:
            # Adapter or native response errors must finish the turn too. Raw
            # diagnostics may contain local paths, headers or credentials.
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

    def _cancel_pending_interactions(self, state: _RuntimeState) -> None:
        """Revoke every outstanding reply before a stopped turn can write."""
        if state.client_interactions:
            for request_id in tuple(state.client_interactions.pending):
                with contextlib.suppress(AcpTransportError):
                    state.client_interactions.resolve(request_id, InteractionResponse(kind="question", cancelled=True))
        for pending in state.translator.pending_permissions():
            state.translator.take_permission(pending.request_id)
            write = state.fs_writes.pop(pending.request_id, None)
            with contextlib.suppress(AcpTransportError):
                if write is not None:
                    state.connection.respond(pending.rpc_id, error={"code": -32001, "message": "write cancelled"})
                else:
                    state.connection.respond(pending.rpc_id, {"outcome": {"outcome": "cancelled"}})
            self._push(state, state.translator.on_permission_resolved(pending.request_id, "cancelled"))
        state.fs_writes.clear()

    @staticmethod
    def _interactions_stopped(state: _RuntimeState) -> bool:
        return state.closed or state.interrupt_requested or bool(state.prompt_task and state.prompt_task.done())

    def _reject_stopped_request(self, state: _RuntimeState, rpc_id: Any, method: str) -> bool:
        if not self._interactions_stopped(state):
            return False
        with contextlib.suppress(AcpTransportError):
            if method == "session/request_permission" or method in (
                getattr(self.preset, "client_methods", {}).get("questions"),
                getattr(self.preset, "client_methods", {}).get("plan"),
            ):
                state.connection.respond(rpc_id, {"outcome": {"outcome": "cancelled"}})
            elif method == "elicitation/create":
                state.connection.respond(rpc_id, {"action": "cancel"})
            else:
                state.connection.respond(rpc_id, error={"code": -32001, "message": "session is not accepting tool requests"})
        return True

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

    async def resolve_interaction(
        self,
        runtime: RuntimeHandle,
        interaction_id: str,
        response: InteractionResponse,
    ) -> None:
        state = self._state(runtime)
        if self._interactions_stopped(state):
            self._cancel_pending_interactions(state)
            raise InteractionNotFoundError("这轮对话已结束，交互请求已取消")
        if state.client_interactions and state.client_interactions.resolve(interaction_id, response):
            return
        if response.kind != "permission":
            raise UnsupportedCapabilityError(
                "ACP 只有 session/request_permission 一种会话内交互请求；"
                f"不支持 {response.kind!r}"
            )
        pending = state.translator.pending_permission(interaction_id)
        if pending is None:
            raise InteractionNotFoundError(f"没有待解决的权限请求：{interaction_id!r}")
        if not response.cancelled and response.option_id not in pending.option_ids:
            raise ValueError("请选择引擎提供的权限选项")
        state.translator.take_permission(interaction_id)
        fs_write = state.fs_writes.pop(interaction_id, None)
        if fs_write is not None:
            # AD-152：fs 写的回执形状是 fs 的，不是权限的——同一张审批卡，
            # 两种回执，所以这里必须分叉。
            approved = (
                not response.cancelled
                and bool(response.option_id)
                and not str(response.option_id).startswith("deny")
            )
            decision = "cancelled" if response.cancelled else (response.option_id or "")
            if approved:
                self._commit_write(state, pending.rpc_id, fs_write.path, fs_write.content)
            else:
                state.connection.respond(
                    pending.rpc_id,
                    error={"code": -32001, "message": "write rejected by user"},
                )
            self._push(
                state, state.translator.on_permission_resolved(interaction_id, decision)
            )
            return
        if response.cancelled:
            outcome: dict[str, Any] = {"outcome": "cancelled"}
            decision = "cancelled"
        else:
            decision = response.option_id or ""
            if not decision:
                raise ValueError("权限响应必须带 option_id（ACP 要求回传 optionId）")
            outcome = {"outcome": "selected", "optionId": decision}
        state.connection.respond(pending.rpc_id, {"outcome": outcome})
        self._push(state, state.translator.on_permission_resolved(interaction_id, decision))

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

    # ------------------------------------------------------------------ #
    # 站外 CLI
    # ------------------------------------------------------------------ #

    def validate_external_session(self, conversation: Conversation) -> None:
        for state in self._runtimes.values():
            if (state.handle.conversation_id == conversation.id and
                    state.prompt_task is not None and not state.prompt_task.done()):
                raise TurnAlreadyRunningError("请等待当前回复结束，再打开终端",
                                              failure=turn_already_running_hint())
        plan = self._skill_plans.get(conversation.id)
        adapter = self._skill_adapter()
        if adapter is not None and plan is not None:
            adapter.cli_command(plan, ())

    def external_computer_use(self, conversation: Conversation) -> dict:
        """Pass resource ownership to the existing terminal handoff lifecycle."""
        plan = self._computer_use_plans.get(conversation.id, {})
        resources = set(plan.get("resources", ()))
        adapter = self._runtime_adapter()
        # These native entrypoints do not forward ACP session/new MCP options.
        # A server merely installed/injected for the card is not a TUI tool.
        native_only = getattr(adapter, "COMPUTER_USE_TERMINAL_MCP", True) is False
        known, carried = set(), set()
        for capability in ("browser", "desktop"):
            candidate = plan.get(capability, {})
            resource = candidate.get("resource")
            if not resource:
                continue
            known.add(resource)
            card_only = candidate.get("source") in {"project", "fallback"} and native_only
            card_only = card_only or (adapter is None and candidate.get("source") == "fallback")
            if not card_only:
                carried.add(resource)
        if "browser:shared" in known:
            known.add("desktop:local")
        if "browser:shared" in carried:
            carried.add("desktop:local")
        # Mixed-purpose or unknown adapters retain their existing conservative
        # ownership; this filtering only removes confirmed card-only providers.
        resources = tuple(sorted((resources - known) | (resources & carried)))
        return {"resources": resources, "remote": bool(resources and
            getattr(adapter, "stop_computer_use", None) is not None)}

    async def stop_external_computer_use(self, conversation: Conversation) -> bool:
        stop = getattr(self._runtime_adapter(), "stop_computer_use", None)
        if stop is None:
            return True
        spec, metadata = self._computer_use_contexts.get(conversation.id, (self.agent_spec, {}))
        try:
            return await asyncio.wait_for(stop(spec, conversation.native_session_id, metadata), timeout=10) is True
        except Exception:
            return False

    async def build_external_cli_launch(
        self, conversation: Conversation
    ) -> CliLaunchSpec:
        """「在 CLI 里打开」。**只有预设给了续接模板时才成立。**

        ACP 本身不规定任何命令行（它只规定 stdio 上的 JSON-RPC），所以这条命令
        不是从协议推出来的，而是预设目录里那一列显式写下的事实。没有模板 =
        我们不知道这个 CLI 怎么续接 → 显式不支持，界面上直接没有这个入口
        （AD-71），而不是给一条猜出来的命令让用户在终端里吃报错。
        """
        self.validate_external_session(conversation)
        preset = self.preset
        if preset is None or preset.resume_argv_template is None:
            raise UnsupportedCapabilityError(
                "此引擎暂不支持终端续接"
            )
        session_id = conversation.native_session_id
        if not session_id:
            raise UnsupportedCapabilityError(
                "会话尚未就绪，请先在 Kaus 中开始对话"
            )
        command = preset.resume_argv(session_id)
        assert command is not None  # noqa: S101 - 上面已判 template 非 None
        import os
        from drivers.acp.native_cli import native_path
        search_path = self.agent_spec.build_env().get("PATH", os.defpath)
        executable = native_path(self)
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise UnsupportedCapabilityError("原生终端命令尚未安装，当前会话可继续在界面中使用")
        native_command = (executable, *command[1:])
        plan = self._skill_plans.get(conversation.id)
        adapter = self._runtime_adapter()
        from drivers.computer_use.native_policy import prepare_spec as prepare_computer_policy
        metadata = self._computer_use_contexts.get(conversation.id, (None, {}))[1]
        if plan is not None and getattr(plan, "spec", None) is not None:
            plan.spec = prepare_computer_policy(plan.spec, adapter, metadata)
        else:
            prepare_computer_policy(self.agent_spec, adapter, metadata)
        external_command = getattr(adapter, "external_cli_command", None)
        if external_command is not None:
            native_command = external_command(plan, native_command, conversation=conversation,
                binding=self._bindings.get(conversation.agent_binding_id), spec=self.agent_spec)
        elif plan is not None and adapter is not None:
            native_command = adapter.cli_command(plan, native_command)
        from drivers.computer_use.native_policy import external_command as computer_external_command
        metadata = self._computer_use_contexts.get(conversation.id, (None, {}))[1]
        native_command = computer_external_command(native_command, adapter, metadata, scoped=plan is not None)
        from drivers.terminals.environment import selected_environment
        # The card may have a per-conversation loader/configuration overlay.
        # The terminal must receive that same environment, not the unprepared
        # connection defaults. Native history roots stay wherever the driver
        # selected them; only probes/tests choose a temporary home.
        terminal_spec = getattr(plan, "spec", None) or self.agent_spec
        terminal_context = getattr(adapter, "external_cli_context", None)
        if terminal_context is not None:
            native_command, terminal_spec = terminal_context(native_command, terminal_spec, metadata)
        prepare_terminal = getattr(adapter, "prepare_terminal_launch", None)
        if prepare_terminal is not None:
            native_command, terminal_spec = await prepare_terminal(native_command, terminal_spec, metadata,
                conversation=conversation, binding=self._bindings.get(conversation.agent_binding_id))
        terminal_env = terminal_spec.build_env()
        return CliLaunchSpec(
            command=native_command,
            private_env=selected_environment(terminal_spec, preset.env_keys_hint),
            terminal_integration=preset.id,
            config_directory=(
                (preset.config_root_env, str(Path(terminal_env.get(
                    preset.config_root_env) or preset.config_root_default).expanduser().resolve()))
                if preset.config_root_env and preset.config_root_default else None
            ),
            path_prefix=tuple(part for part in terminal_env.get("PATH", search_path).split(os.pathsep)
                              if part and os.path.isabs(part)),
            cwd=self._workspace_root_for(conversation.agent_binding_id)
            or self._default_cwd,
            # v1.0 §16.6：只带变量**名**，一个值都不带。
            env_passthrough=preset.env_keys_hint,
            title=conversation.title,
            resume=True,
        )

    # ------------------------------------------------------------------ #
    # 入站回调
    # ------------------------------------------------------------------ #

    def _on_notification(self, state: _RuntimeState, method: str, params: dict) -> None:
        if method == "_auth/status_update":
            self._capture_auth_status(state.connection, method, params)
            self._catalog[state.conversation.agent_binding_id] = self._with_connection_auth(
                self._snapshot_from_session(state.session_result), state.connection)
            return
        if state.closed:
            return
        if state.client_interactions:
            artifact_uri = artifact_mime = None
            if method == getattr(self.preset, "client_methods", {}).get("image"):
                raw_path = params.get("filePath")
                root = getattr(state.handle, "metadata", {}).get("workspaceRoot")
                if isinstance(raw_path, str) and root:
                    import mimetypes
                    from pathlib import Path
                    try:
                        target = (Path(root) / raw_path).resolve()
                        target.relative_to(Path(root).resolve())
                        if target.is_file():
                            artifact_uri = target.as_uri()
                            artifact_mime = mimetypes.guess_type(str(target))[0]
                    except (ValueError, OSError):
                        pass
            if state.client_interactions.notification(method, params, artifact_uri=artifact_uri, artifact_mime=artifact_mime):
                return
        if method == "session/update":
            update = params.get("update")
            display_title = getattr(self._runtime_adapter(), 'filter_tool_title', None)
            if (display_title is not None and params.get('sessionId') == state.session_id
                    and isinstance(update, Mapping) and update.get('sessionUpdate') in ('tool_call', 'tool_call_update')
                    and isinstance(update.get('title'), str)):
                update = {**update, 'title': display_title(state.runtime_spec, update['title'])}
                params = {**params, 'update': update}
            filter_update = getattr(self._skill_adapter(), "filter_session_update", None)
            if (filter_update is not None and state.skill_plan is not None
                    and params.get("sessionId") == state.session_id and isinstance(update, Mapping)
                    and update.get("sessionUpdate") == "available_commands_update"):
                update = filter_update(state.skill_plan, update)
                params = {**params, "update": update}
            if (params.get("sessionId") == state.session_id and isinstance(update, Mapping)
                    and update.get("sessionUpdate") == "config_option_update"
                    and isinstance(update.get("configOptions"), list)):
                self._accept_config_update(state, {"configOptions": update["configOptions"]})
            self._push(state, state.translator.on_session_update(params))
            return
        # 未知通知不丢：走扩展通道让 Generic Event Card 显示（N §8.2）。
        self._push(state, state.translator.on_session_update({"update": {"sessionUpdate": method}}))

    async def _on_request(
        self, state: _RuntimeState, rpc_id: Any, method: str, params: dict
    ) -> None:
        if self._reject_stopped_request(state, rpc_id, method):
            return
        if getattr(state, "runtime_metadata", {}).get("_kausExecutionPolicy") == "context-only":
            if method == "session/request_permission":
                state.connection.respond(rpc_id, result={"outcome": {"outcome": "cancelled"}})
            else:
                state.connection.respond(rpc_id, error={"code": -32601, "message": "Tool access is disabled"})
            return
        if state.client_interactions and state.client_interactions.request(rpc_id, method, params):
            return
        if method == "session/request_permission":
            display_title = getattr(self._runtime_adapter(), 'filter_tool_title', None)
            tool_call = params.get('toolCall')
            if (display_title is not None and params.get('sessionId') == state.session_id
                    and isinstance(tool_call, Mapping) and isinstance(tool_call.get('title'), str)):
                params = {**params, 'toolCall': {**tool_call,
                    'title': display_title(state.runtime_spec, tool_call['title'])}}
            envelopes = state.translator.on_permission_request(rpc_id, params)
            if not envelopes:
                # 会话不匹配：不能替别的会话做决定，回一个明确错误。
                state.connection.respond(
                    rpc_id, error={"code": -32602, "message": "unknown sessionId"}
                )
                return
            self._push(state, envelopes)
            if state.approval_mode == "host:auto":
                selected = allow_once_option(params.get("options", []))
                if selected:
                    pending = next((p for p in state.translator.pending_permissions() if p.rpc_id == rpc_id), None)
                    if pending:
                        await self.resolve_interaction(state.handle, pending.request_id, InteractionResponse(kind="permission", option_id=selected))
            return
        if method in ("fs/read_text_file", "fs/write_text_file"):
            await self._on_fs_request(state, rpc_id, method, params)
            return
        # 没声明过的客户端能力（``terminal/*`` 本批一律不声明）出现了就如实回
        # -32601，而不是假装做了。
        state.connection.respond(
            rpc_id,
            error={"code": -32601, "message": f"client does not implement {method}"},
        )

    # ------------------------------------------------------------------ #
    # 客户端 fs（AD-152）
    # ------------------------------------------------------------------ #

    async def _on_fs_request(
        self, state: _RuntimeState, rpc_id: Any, method: str, params: Mapping[str, Any]
    ) -> None:
        """``fs/read_text_file`` / ``fs/write_text_file``。

        边界只有一条，且**先于**审批检查：解析后的真实路径必须落在
        ``workspaceRoot`` 子树内。先审批后校验会让「同意一次」变成一把能写任何
        地方的钥匙——用户点的那个「允许」是对着一个文件名点的，不是对着整块磁盘。

        读不走审批（读的是用户自己授权的目录），写一律走审批：写是不可逆的。
        """
        if self._reject_stopped_request(state, rpc_id, method):
            return
        if state.workspace_root is None:
            state.connection.respond(
                rpc_id,
                error={
                    "code": -32601,
                    "message": f"client does not implement {method}",
                },
            )
            return
        raw_path = params.get("path")
        resolved = _resolve_within(state.workspace_root, raw_path)
        if resolved is None:
            # 越界：如实拒绝，并且**不回显**解析后的绝对路径（那是在替 agent
            # 确认「你猜的这个路径长什么样」）。
            state.connection.respond(
                rpc_id,
                error={
                    "code": -32602,
                    "message": "path is outside the session workspace root",
                },
            )
            return
        if method == "fs/read_text_file":
            try:
                text = _read_text(resolved, params)
            except OSError as exc:
                state.connection.respond(
                    rpc_id, error={"code": -32603, "message": f"read failed: {exc.strerror}"}
                )
                return
            state.connection.respond(rpc_id, {"content": text})
            return

        content = params.get("content")
        if not isinstance(content, str):
            state.connection.respond(
                rpc_id, error={"code": -32602, "message": "content must be a string"}
            )
            return
        if state.approval_mode in ("auto", "bypass", "host:auto"):
            self._commit_write(state, rpc_id, resolved, content)
            return
        if state.approval_mode in ("deny", "read_only", "plan"):
            state.connection.respond(
                rpc_id,
                error={"code": -32001, "message": "write denied by approval policy"},
            )
            return
        # ask：发一张审批卡，等 resolve_interaction。**这条 RPC 不回执**——
        # agent 就该在这里等着，那正是「停下来问人」的意思。
        envelopes = state.translator.on_permission_request(
            rpc_id,
            {
                "sessionId": state.session_id,
                "toolCall": {
                    "toolCallId": f"fs-write:{os.path.basename(resolved)}",
                    "title": f"写入文件 {os.path.relpath(resolved, state.workspace_root)}",
                },
                "options": [
                    {"optionId": "allow", "name": "允许写入", "kind": "allow_once"},
                    {"optionId": "deny", "name": "拒绝", "kind": "reject_once"},
                ],
                # 前端按这一位把 fs 写审批与普通工具审批区分开（形状仍是同一种）。
                "_meta": {"kaus": {"kind": "fs_write", "bytes": len(content)}},
            },
        )
        pending = next(
            (p for p in state.translator.pending_permissions() if p.rpc_id is rpc_id),
            None,
        )
        if pending is None:  # pragma: no cover - 上一行刚登记，取不到只能算内部错
            state.connection.respond(
                rpc_id, error={"code": -32603, "message": "failed to open approval"}
            )
            return
        state.fs_writes[pending.request_id] = _PendingFsWrite(
            path=resolved, content=content
        )
        self._push(state, envelopes)

    def _commit_write(
        self, state: _RuntimeState, rpc_id: Any, path: str, content: str
    ) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            state.connection.respond(
                rpc_id, error={"code": -32603, "message": f"write failed: {exc.strerror}"}
            )
            return
        state.connection.respond(rpc_id, {})

    def _push(
        self, state: _RuntimeState, envelopes: Sequence[AgentEventEnvelope]
    ) -> None:
        for envelope in envelopes:
            state.queue.put_nowait(envelope)

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

    def _state(self, runtime: RuntimeHandle) -> _RuntimeState:
        state = self._runtimes.get(runtime.runtime_id)
        if state is None or state.closed:
            raise RuntimeNotFoundError(f"未知或已停止的 runtime：{runtime.runtime_id!r}")
        return state

    def _resolve_cwd(self, workspace_root: str | None) -> str:
        cwd = workspace_root or self._default_cwd or os.getcwd()
        return os.path.abspath(cwd)

    def _remember_models(self, binding_id: str, result: Any, *, connection=None) -> None:
        """真实会话的 ``session/new`` 结果覆盖目录快照（AD-157 的「运行时失效」）。

        探测出来的那份是「十分钟前问的」，这一份是「此刻这条会话真的拿到的」——
        后者更新目录。仅创建本地记录的桥保留既有认证证据，直到原生提示成功或
        明确返回认证错误；静态模型目录不会被当作已登录。
        """
        snapshot = self._snapshot_from_session(result)
        if connection is not None:
            snapshot = self._with_connection_auth(snapshot, connection)
        self._catalog[binding_id] = self._retain_auth_state(binding_id, snapshot)
        binding = self._bindings.get(binding_id)
        if binding is not None:
            self._catalog_config_stamps[binding_id] = self._config_stamp(binding)

    def _retain_auth_state(self, binding_id: str, snapshot: _CatalogSnapshot) -> _CatalogSnapshot:
        previous = self._catalog.get(binding_id)
        if snapshot.auth_state == "unknown" and previous is not None and previous.auth_state != "unknown":
            if previous.auth_state == "signed_out":
                return replace(snapshot, auth_state="signed_out", auth_hint=previous.auth_hint,
                    models=(), model_option_id=None, engine_default_available=False,
                    degraded=True, diagnostics=previous.diagnostics)
            return replace(snapshot, auth_state=previous.auth_state, auth_hint=previous.auth_hint)
        return snapshot


# --------------------------------------------------------------------------- #
# 报文取值
# --------------------------------------------------------------------------- #


def _strip_effort_suffix(model_id: str) -> str:
    """``"gpt-x[low]"`` → ``"gpt-x"``。没有后缀就原样返回（AD-158）。

    只认**结尾**那一对方括号，且里面不能再有方括号——模型 id 里出现方括号本身
    就少见，宁可少剥一个也不要把一个正常 id 剁短。
    """
    if not model_id.endswith("]"):
        return model_id
    head, _, suffix = model_id[:-1].rpartition("[")
    if not head or "[" in suffix or "]" in suffix:
        return model_id
    return head


def _mode_id_of(mode: Any) -> str | None:
    """``availableModes`` 的一项 → modeId（认不出来就 ``None``）。"""
    if isinstance(mode, Mapping):
        mode_id = mode.get("id") or mode.get("modeId")
    else:
        mode_id = mode
    return mode_id if isinstance(mode_id, str) and mode_id else None


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


def _resolve_within(root: str, raw_path: Any) -> str | None:
    """把 agent 给的路径解析到 ``root`` 子树内的真实路径；越界返回 ``None``。

    用 ``realpath`` 而不是字符串前缀比较：``root/../etc/passwd`` 与一条指向
    ``/etc`` 的软链都会被前缀比较放过去。解析的是**父目录**——写一个还不存在的
    文件时 ``realpath`` 对它自己没有意义，但它的父目录必须已经在圈里。
    """
    if not isinstance(raw_path, str) or not raw_path:
        return None
    root_real = os.path.realpath(root)
    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(root_real, raw_path)
    resolved = os.path.realpath(candidate)
    anchor = resolved if os.path.exists(resolved) else os.path.realpath(
        os.path.dirname(candidate)
    )
    if anchor != root_real and not anchor.startswith(root_real + os.sep):
        return None
    return resolved


def _read_text(path: str, params: Mapping[str, Any]) -> str:
    """按 ACP 的 ``line`` / ``limit`` 语义读一段文本（两者都缺就是整份）。"""
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    start = params.get("line")
    if isinstance(start, int) and start > 0:
        lines = lines[start - 1 :]
    limit = params.get("limit")
    if isinstance(limit, int) and limit >= 0:
        lines = lines[:limit]
    return "".join(lines)


def _mcp_servers(metadata: Mapping[str, Any]) -> list[Any]:
    servers = metadata.get("mcpServers")
    if isinstance(servers, Sequence) and not isinstance(servers, (str, bytes)):
        return list(servers)
    return []


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


__all__ = ["AcpDiscoveryReport", "AcpDriver", "DRIVER_VERSION"]
