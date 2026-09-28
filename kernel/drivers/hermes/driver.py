"""``HermesDriver``：N §5.3 的 15 个方法，``driver_kind="native"``（AD-18）。

本文件只做**编排**：每一条规则的正身都在旁边的模块里（见
:mod:`drivers.hermes` 的模块地图）。这样做的直接好处是 translator / history /
oob_watcher 三个最容易出错的地方都能脱离 HTTP 单测。

三条一直要记着的边界
--------------------
1. **公共层一行都不改。** Hermes 的私有信息只有四个出口：
   ``RuntimeHandle.metadata``、``NativeHistoryEntry.metadata``、
   ``AgentBinding.runtime_config_json``、``extension.event``（namespace ``"hermes"``）。
2. **key 只经 credential_ref。** ``RuntimeHandle.metadata`` 里放的是 ``keyRef``，
   不是值（规格 §2.6 的注释与 §7.2 的表）。
3. **不伪造。** 没有来源的字段一律 ``None``、没有来源的能力一律 false
   （N §7.3 规则 6 / N §13.1）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Mapping
from urllib.parse import unquote, urlsplit

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.ids import normalize_backend_id
from app.projects.models import AgentBinding, Project
from drivers.base import (
    AuthState,
    BackendProbeResult,
    CliLaunchSpec,
    CreateSessionOptions,
    DriftReport,
    DriverError,
    EngineSettings,
    FailureHint,
    InteractionNotFoundError,
    InteractionResponse,
    MessageInput,
    ModelCatalog,
    NativeHistory,
    NativeSession,
    ProjectionResult,
    RuntimeHandle,
    RuntimeNotFoundError,
    SessionProjection,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
)
from drivers.hermes import BACKEND_KEY, DRIVER_VERSION, PINNED_BACKEND_VERSION
from drivers.hermes import capabilities as capabilities_mod
from drivers.hermes import engine_settings as engine_settings_mod
from drivers.hermes import history as history_mod
from drivers.hermes import model_catalog as catalog_mod
from drivers.hermes import projection_map
from drivers.hermes import projector as projector_mod
from drivers.hermes import reconnect
from drivers.hermes import session_mapper
from drivers.hermes import failure_hints
from drivers.hermes.credentials import (
    credential_ref_present,
    describe_credential_ref,
)
from drivers.hermes.http_client import (
    HermesHttpError,
    HermesIdempotencyConflictError,
    new_idempotency_key,
)
from drivers.hermes.oob_watcher import HermesOutOfBandWatcher
from drivers.hermes.redaction import redact
from drivers.hermes.supervisor import (
    GatewayConfig,
    HermesGatewaySupervisor,
    Spawner,
)
from drivers.hermes.translator import (
    HermesRunTranslator,
    OPTION_TO_CHOICE,
    TranslationTarget,
)
from runtime.capability_matrix import BackendCapabilities, InterruptCapability
from runtime.event_envelope import (
    AgentEventEnvelope,
    EventSource,
    ExtensionEvent,
    make_envelope,
)
from runtime.event_reducer import (
    MODEL_ADOPTED_NAME,
    MODEL_ADOPTED_NAMESPACE,
    MODEL_ADOPTED_REASON_NOT_SCOPED,
)

#: 规格 §2.7：同一 gateway 上同时在跑的 run 限在 ≤ 8，留 2 个余量给用户自己的客户端
#: （后端默认 ``max_concurrent_runs`` 是 10）。
MAX_CONCURRENT_RUNS = 8

#: AD-147：`POST /stop` 之后最多等多久一个终态（秒）。
STOP_CONFIRM_TIMEOUT = 30.0

#: 同上：轮询间隔。规格 §5 的检测器就是 1s 一跳，这里共用同一个口径。
STOP_POLL_INTERVAL = 1.0

_SENTINEL = object()


@dataclass
class _RunState:
    translator: HermesRunTranslator
    run_id: str
    idempotency_key: str
    task: "asyncio.Task[None] | None" = None
    finalized: bool = False
    #: 本 run 已投递的第一个事件的原始 ``data`` 文本（§3.6 的重放判别依据）。
    first_frame: str | None = None
    replay_behaviour: reconnect.ReplayBehaviour = reconnect.ReplayBehaviour.UNKNOWN
    #: 批次十八第 2 件：``POST /stop`` 的 200 响应体（规格 §2.0 实测是一份完整的
    #: run 对象）。收尾时优先用它，省掉一次 ``GET /v1/runs/{id}`` 往返——不管
    #: finalize 是被 interrupt 直接触发的，还是被 SSE 泵的 ``finally`` 触发的。
    stop_run_object: Mapping[str, Any] | None = None
    #: 同上：``interrupt`` 已经**认领**了这一轮的收尾。SSE 泵被取消时它的
    #: ``finally`` 不要再抢——那一次是在被取消的上下文里跑的，只会把 ``finalized``
    #: 置真而一条事件都发不出来（页面于是永远停在「正在停止」）。
    stop_owns_finalize: bool = False
    #: 批次二十第 3 件：本 run 从网关收到的**事件类型序列**（只有名字，没有一个
    #: 字的内容）。SSE 流健康自检端点就读它——真机上「回合中途还在、收尾丢了」
    #: 这类问题，第一件要确认的就是「网关到底发了哪些事件、发了几条」。
    stream_event_types: list[str] = field(default_factory=list)
    #: 批次二十一：`/stop` 之后那条「等一个终态」的后台任务（见 ``interrupt``）。
    stop_task: "asyncio.Task[None] | None" = None
    #: 批次二十一第 2 件：这一轮停止的**取证记录**（只有状态词与结果词，没有
    #: 一个字的内容）。形状见 :meth:`HermesDriver.interrupt`；取证端点原样转出。
    stop_record: dict[str, Any] | None = None


@dataclass
class _RuntimeState:
    handle: RuntimeHandle
    conversation: Conversation
    binding: AgentBinding
    supervisor: HermesGatewaySupervisor
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    run: _RunState | None = None
    closed: bool = False


LOGGER = logging.getLogger(__name__)


class HermesDriver:
    """Hermes Native Driver（HTTP + SSE）。

    参数
    ----
    hermes_root:
        ``~/.hermes`` 的位置。测试与 ``--live`` 冒烟把它换成隔离目录。
    default_gateway:
        ``probe()`` 用的 gateway 配置（probe 是 backend 级、没有 Binding）。
    static_catalog_path / canonical_archive_path:
        ``model-options.json`` 与 ``session-archive.json``；两者都**只读**，
        读不到就按「没有」处理。
    credential_store / spawner:
        凭据存储读取面（B3）与子进程拉起器（managed）。都不给就只能跑 adopted +
        B1 —— 这正是最小权限的默认。
    """

    driver_kind: Literal["native"] = "native"

    def __init__(
        self,
        *,
        backend_key: str = BACKEND_KEY,
        hermes_root: Path | str | None = None,
        default_gateway: GatewayConfig | None = None,
        static_catalog_path: Path | str | None = None,
        canonical_archive_path: Path | str | None = None,
        credential_store: Mapping[str, str] | Callable[[str], str | None] | None = None,
        spawner: Spawner | None = None,
        hermes_bin: str = "hermes",
        driver_version: str = DRIVER_VERSION,
    ) -> None:
        self.backend_id = normalize_backend_id(backend_key)
        self._backend_key = backend_key
        self.hermes_root = Path(hermes_root).expanduser() if hermes_root else Path("~/.hermes").expanduser()
        self.default_gateway = default_gateway
        self.static_catalog_path = Path(static_catalog_path) if static_catalog_path else None
        self.canonical_archive_path = (
            Path(canonical_archive_path) if canonical_archive_path else None
        )
        self.credential_store = credential_store
        self.spawner = spawner
        self.hermes_bin = hermes_bin
        self.driver_version = driver_version

        self._bindings: dict[str, AgentBinding] = {}
        self._supervisors: dict[str, HermesGatewaySupervisor] = {}
        self._watchers: dict[str, HermesOutOfBandWatcher] = {}
        self._runtimes: dict[str, _RuntimeState] = {}
        self._isolated_runtimes: dict[str, Any] = {}
        self._capabilities: BackendCapabilities | None = None
        self._report: capabilities_mod.HermesCapabilityReport | None = None
        self._backend_version: str | None = None
        self._probe_message: str | None = None
        self._runtime_counter = 0
        #: 批次十六第 2 件：真机上出现过「点了停止但回合没进终态」→ 把
        #: ``card.interrupt`` 降为 ``unverified``（verification=live）。
        #: 只降不升：一次没确认就足以推翻「immediate」这句声明。
        self._interrupt_unconfirmed = False
        #: 批次十七第 4 件：最近一次原生历史 HTTP 响应的**原始**响应体，按
        #: native session id 存一条。只在内存里，只给 `DASH_DEBUG=1` 才挂载的
        #: 取证端点用，且那条端点输出前会脱敏（键名 + 值类型 + 截断 80 字）。
        self._last_history_payload: dict[str, Any] = {}
        #: 批次二十第 3 件：每条 Conversation 最近一次 run 的 SSE **类型序列与
        #: 计数**（不含任何内容）。取证专用，见 :meth:`debug_last_stream`。
        self._last_stream: dict[str, dict[str, Any]] = {}
        #: 停止确认的提示期限与轮询间隔。到点只提示未确认，继续跟踪原 run。
        self.stop_confirm_timeout: float = STOP_CONFIRM_TIMEOUT
        self.stop_poll_interval: float = STOP_POLL_INTERVAL

    # ------------------------------------------------------------------ #
    # Binding → gateway
    # ------------------------------------------------------------------ #

    def _assert_binding(self, binding: AgentBinding) -> None:
        """N §13.2：Catalog / Session 归 Binding，不得跨 Backend 串数据。"""
        if binding.backend_id != self.backend_id:
            raise DriverError(
                f"Binding {binding.id!r} 属于 {binding.backend_id!r}，"
                f"不能交给 {self.backend_id!r} 的 Driver"
            )

    def gateway_config_for(self, binding: AgentBinding) -> GatewayConfig:
        profile = binding.native_scope_ref or session_mapper.DEFAULT_SCOPE
        home = session_mapper.resolve_hermes_home(profile, hermes_root=self.hermes_root)
        config = GatewayConfig.from_runtime_config(
            binding.runtime_config, hermes_home=home, profile=profile
        )
        if config.port == 0 and self.default_gateway is not None:
            config = GatewayConfig(
                hermes_home=config.hermes_home,
                host=self.default_gateway.host,
                port=self.default_gateway.port,
                profile=config.profile,
                key_ref=config.key_ref or self.default_gateway.key_ref,
                mode=config.mode,
            )
        return config

    def supervisor_for(self, binding: AgentBinding) -> HermesGatewaySupervisor:
        """规格 §1.1：一个 ``HERMES_HOME`` 至多一个 Supervisor（也就至多一个进程）。"""
        self._assert_binding(binding)
        config = self.gateway_config_for(binding)
        key = str(config.hermes_home)
        existing = self._supervisors.get(key)
        if existing is None or existing.config != config:
            existing = HermesGatewaySupervisor(
                config=config,
                credential_store=self.credential_store,
                spawner=self.spawner,
                hermes_bin=self.hermes_bin,
            )
            self._supervisors[key] = existing
        return existing

    def watcher_for(self, binding: AgentBinding) -> HermesOutOfBandWatcher:
        """规格 §5：每个 ``HERMES_HOME`` 一个检测器，被该 home 下所有句柄共享。"""
        home = self.gateway_config_for(binding).hermes_home
        key = str(home)
        watcher = self._watchers.get(key)
        if watcher is None:
            watcher = HermesOutOfBandWatcher(hermes_home=home)
            self._watchers[key] = watcher
        return watcher

    def canonical_map_for(self, binding: AgentBinding) -> session_mapper.CanonicalMap:
        profile = binding.native_scope_ref or session_mapper.DEFAULT_SCOPE
        if self.canonical_archive_path is None:
            return session_mapper.CanonicalMap.empty(profile)
        return session_mapper.load_canonical_map(self.canonical_archive_path, profile)

    # ------------------------------------------------------------------ #
    # Probe / 能力 / 模型
    # ------------------------------------------------------------------ #

    def _probe_supervisor(self) -> HermesGatewaySupervisor | None:
        if self.default_gateway is None:
            return None
        key = str(self.default_gateway.hermes_home)
        supervisor = self._supervisors.get(key)
        if supervisor is None or supervisor.config != self.default_gateway:
            supervisor = HermesGatewaySupervisor(
                config=self.default_gateway,
                credential_store=self.credential_store,
                spawner=self.spawner,
                hermes_bin=self.hermes_bin,
            )
            self._supervisors[key] = supervisor
        return supervisor

    async def probe(self) -> BackendProbeResult:
        installed = shutil.which(self.hermes_bin) is not None
        supervisor = self._probe_supervisor()
        if supervisor is None:
            self._capabilities = BackendCapabilities()
            return BackendProbeResult(
                backend_id=self.backend_id,
                driver_kind=self.driver_kind,
                state="unavailable",
                installed=installed,
                driver_version=self.driver_version,
                message="尚未配置 gateway 连接（缺少 api_server 配置或 credential_ref）",
                capabilities=self._capabilities,
                probed_at=_utcnow(),
            )

        status = await supervisor.ensure_ready(force=True)
        self._backend_version = status.version
        report = status.report
        if report is None:
            self._capabilities = BackendCapabilities()
            self._report = None
            # 走查 F5：探测失败的 message 要说人话并带修法（`GET /api/backends/{id}`
            # 的 `probeMessage` 读的就是它）。认不出根因时退回原始（已脱敏）错误。
            self._probe_message = status.describe()
            return BackendProbeResult(
                backend_id=self.backend_id,
                driver_kind=self.driver_kind,
                state=status.state,
                installed=installed,
                version=status.version,
                driver_version=self.driver_version,
                message=status.describe() or "gateway 未就绪",
                capabilities=self._capabilities,
                probed_at=_utcnow(),
            )

        self._report = report
        self._capabilities = report.capabilities
        message = capabilities_mod.probe_message(report, version=status.version)
        state = status.state
        # 验收点 7：版本守卫。低于基准时**必须**降级或给出明确版本提示，
        # 不得静默按 0.21.0 的事件名解析（0.18.2/0.20.0 的行为差异见规格 §2.7）。
        if status.version and _version_tuple(status.version) < _version_tuple(
            PINNED_BACKEND_VERSION
        ):
            state = "degraded"
            message = (
                f"后端版本 {status.version} 低于本 Driver 的基准 {PINNED_BACKEND_VERSION}，"
                "事件形状与会话续接行为可能不同；请先升级 Hermes。" + message
            )
        self._probe_message = message
        self._capabilities = self._apply_live_evidence(self._capabilities)
        return BackendProbeResult(
            backend_id=self.backend_id,
            driver_kind=self.driver_kind,
            state=state,
            installed=installed,
            version=status.version,
            driver_version=self.driver_version,
            message=message,
            capabilities=self._capabilities,
            probed_at=_utcnow(),
        )

    async def get_capabilities(self) -> BackendCapabilities:
        if self._capabilities is None:
            await self.probe()
        return self._apply_live_evidence(self._capabilities or BackendCapabilities())

    def conversation_controls(self, binding: AgentBinding) -> dict[str, Any]:
        return {
            "reasoning": bool(self._capabilities and self._capabilities.models.conversation_scoped.is_supported),
            "approvalModes": [],
        }

    def group_context_isolation(self) -> bool:
        from drivers.hermes.isolated_runtime import available
        return available(self.hermes_bin)

    async def set_conversation_selection(
        self, runtime: RuntimeHandle, conversation: Conversation
    ) -> None:
        if runtime.runtime_id in self._isolated_runtimes:
            raise UnsupportedCapabilityError("执行中的组长使用已保存的配置，请在下一项任务切换。")
        state = self._state(runtime)
        if state.run is not None and not state.run.finalized:
            raise TurnAlreadyRunningError("上一轮尚未结束。")
        if not (await self.get_capabilities()).models.conversation_scoped.is_supported:
            raise UnsupportedCapabilityError("当前网关不支持会话设置。")
        # /v1/runs accepts model/provider/model_options per request. Keep the
        # selection on this conversation; never mutate the shared profile.
        state.conversation = conversation

    def note_interrupt_unconfirmed(self) -> None:
        """Session Host 的回灌口（批次十六第 2 件）：这台引擎没确认过中断。

        真机证据一旦出现就**不再自动收回**——能力矩阵宁可保守：``unverified``
        比继续声明 ``immediate`` 诚实。重新探测（``probe()``）也不会把它擦掉，
        因为 ``/v1/capabilities`` 只会再说一遍「我有 run_stop」，那正是被这次
        证据推翻的那句话。
        """
        self._interrupt_unconfirmed = True
        if self._capabilities is not None:
            self._capabilities = self._apply_live_evidence(self._capabilities)

    def _apply_live_evidence(
        self, capabilities: BackendCapabilities
    ) -> BackendCapabilities:
        """把真机证据盖在协商结果上（目前只有中断这一条）。"""
        if not self._interrupt_unconfirmed:
            return capabilities
        if capabilities.card.interrupt.value == "none":
            return capabilities
        card = capabilities.card.model_copy(
            update={
                "interrupt": InterruptCapability(
                    value="unverified",
                    verification="live",
                    note="真机上发过停止请求，但这一轮没有在约定时间内进终态",
                )
            }
        )
        return capabilities.model_copy(update={"card": card})

    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog:
        """规格 §2.3（批次十三改判规则 4）：动态目录不可用时按 provider 过滤。

        每一条「为什么退化」都写进 ``diagnostics`` 并上 wire：动态目录不可用有
        四种完全不同的原因（后端没声明 ``model_options`` / 请求失败 / 非 2xx /
        响应体形状不认识），此前它们一律表现为「静态全集 + 没有任何提示」，
        真机上根本分不清。
        """
        self._assert_binding(binding)
        supervisor = self.supervisor_for(binding)
        static = (
            catalog_mod.load_static_catalog(self.static_catalog_path)
            if self.static_catalog_path
            else {}
        )
        dynamic: list[tuple[str, str | None, str | None]] = []
        notes: list[str] = []
        report = self._report
        endpoints = report.endpoints if report else capabilities_mod.HermesEndpoints.from_capabilities(None)
        if report is not None and not report.features.get("model_options"):
            notes.append(
                "后端的 /v1/capabilities.features 没有声明 model_options，"
                "本网关上没有动态模型目录端点"
            )
        else:
            try:
                response = await supervisor.client().request(
                    "GET", endpoints.path("model_options")
                )
                if response.ok:
                    parsed = catalog_mod.parse_model_options(response.json())
                    dynamic = list(parsed)
                    # 批次二十九：解析阶段就知道的原因（一个已登录的 provider 都没有）
                    # 原样带上——它与「形状认不出」是两件完全不同的事，压成同一句话
                    # 用户就不知道该去登录还是该报 bug。
                    notes.extend(parsed.diagnostics)
                    if not dynamic and not parsed.diagnostics:
                        # 认不出来就当没有动态目录，绝不猜一个出来——但要说出
                        # 「是形状认不出」这件事。（``providers[]`` 那一支已在
                        # 2026-09-05 取证定案，见 model_catalog 模块 docstring。）
                        notes.append(
                            "动态模型目录的响应体形状无法识别，已按「没有动态目录」处理"
                        )
                else:
                    notes.append(f"动态模型目录返回 HTTP {response.status}")
            except HermesHttpError as exc:
                notes.append(f"动态模型目录请求失败：{redact(str(exc))}")

        provider_ids: tuple[str, ...] = ()
        engine_model_id: str | None = None
        engine_provider_id: str | None = None
        if not dynamic:
            # 只在需要兜底时才去读引擎配置：动态目录好用的时候没有理由碰磁盘。
            # 批次十四：引擎当前配置的模型一定要在列——它就是这台引擎正在用的那个。
            engine = await self.read_engine_settings(binding)
            provider_ids = engine.provider_ids
            engine_model_id = engine.model_id
            engine_provider_id = engine.model_provider_id

        merged = catalog_mod.merge_catalog(
            binding_id=binding.id,
            dynamic=dynamic or None,
            static=static,
            default_model_id=binding.default_model_id,
            default_provider_id=binding.default_provider_id,
            provider_ids=provider_ids,
            engine_model_id=engine_model_id,
            engine_provider_id=engine_provider_id,
            extra_diagnostics=tuple(notes),
        )
        return merged.catalog

    async def read_engine_settings(self, binding: AgentBinding) -> EngineSettings:
        """规格外、批次十三第 1 件：读这个 scope 的 ``config.yaml``（只读、无凭据）。

        键名与取证见 :mod:`drivers.hermes.engine_settings` 的模块 docstring。
        ``.env`` / ``credentials/`` / ``auth*`` 一律不碰；``providers`` 只取键名。

        批次十四：按 Hermes 的继承链读——profile 缺的键回落到
        ``<hermes_root>/config.yaml``。``self.hermes_root`` 正是
        ``session_bootstrap.hermes_root_for(gateway)`` 在造 Driver 时算出来的那个
        root，所以这里直接用它，不必再倒推一次。
        """
        self._assert_binding(binding)
        return engine_settings_mod.read_engine_settings(
            binding_id=binding.id,
            hermes_home=self.gateway_config_for(binding).hermes_home,
            hermes_root=self.hermes_root,
        )

    # ------------------------------------------------------------------ #
    # 登录状态（AD-82 / AD-93，批次十九第 1 件）
    # ------------------------------------------------------------------ #

    async def read_auth_state(self, binding: AgentBinding) -> AuthState:
        """这条 Binding 现在算不算「已登录」。

        Hermes 的登录模型是 ``managed-credential``：它没有账号、没有 OAuth，
        「登录」在这台引擎上就等于**手上这把 API key 被网关认了**。因此判据是
        两条，缺一不可：

        1. ``key_ref`` **指得到东西**——``hermes-env:`` 形态只判那个 ``.env`` 里
           有没有这个**键名**，``credential-store:`` 形态只判这个名字在不在进程
           环境变量里。两条路都**不读值**（见
           :func:`~drivers.hermes.credentials.credential_ref_present`）；
        2. 网关这一侧没有把我们顶回来——``/v1/capabilities`` 回 401/403 就是
           ``signed_out``（supervisor 已经把它归成
           :data:`~drivers.hermes.failure_hints.GATEWAY_KEY_MISMATCH`）。

        网关**离线**时返回 ``unknown`` 而不是 ``signed_out``：连不上不等于没登录，
        把它报成未登录会让用户去修一个根本没坏的东西（他要修的是没在跑的网关）。
        ``account`` 恒为 ``None``——Hermes 没有账号概念，编一个出来只会让界面上
        多一行假信息。
        """
        self._assert_binding(binding)
        config = self.gateway_config_for(binding)
        checked_at = _utcnow()
        # 只判引用指不指得到，**不解析、不取值**。
        ref_present = credential_ref_present(config.key_ref)

        try:
            supervisor = self.supervisor_for(binding)
            status = await supervisor.ensure_ready()
        except DriverError as exc:
            # 连 supervisor 都装配不出来（配置本身有问题）：这不是「没登录」，
            # 是「问不出来」。消息已脱敏，但仍不放进 AuthState——那里只放修法。
            LOGGER.debug("read_auth_state 取不到 gateway 状态：%s", redact(str(exc)))
            return AuthState(
                state="unknown", model="managed-credential", checked_at=checked_at
            )

        failure: FailureHint | None = status.failure
        if status.state == "unavailable":
            return AuthState(
                state="unknown",
                model="managed-credential",
                checked_at=checked_at,
                hint=failure.hint if failure is not None else None,
            )

        key_rejected = (
            failure is not None and failure.code == failure_hints.GATEWAY_KEY_MISMATCH
        )
        if key_rejected or not ref_present:
            hint = (
                failure.hint
                if failure is not None and failure.hint
                else failure_hints.gateway_key_mismatch().hint
            )
            return AuthState(
                state="signed_out",
                model="managed-credential",
                checked_at=checked_at,
                hint=hint,
            )
        return AuthState(
            state="signed_in",
            model="managed-credential",
            checked_at=checked_at,
        )

    # ------------------------------------------------------------------ #
    # 能力投射
    # ------------------------------------------------------------------ #

    async def materialize_project_capabilities(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities,
        *,
        dry_run: bool = True,
        adopt: bool = False,
    ) -> ProjectionResult:
        """批次二十四：真写 ``<HERMES_HOME>/config.yaml``（``dry_run=False`` 时）。

        写什么、不写什么、备份与回滚全在 :mod:`drivers.hermes.projector`；
        本方法只负责把 Driver 手上的三样东西喂进去：这条 Binding 的 home、
        它的通用运行配置（审批档 / 推理强度）、以及它的默认模型。
        """
        self._assert_binding(binding)
        return projector_mod.project_capabilities(
            binding_id=binding.id,
            hermes_home=self.gateway_config_for(binding).hermes_home,
            effective=effective_capabilities,
            capabilities=await self.get_capabilities(),
            # 批次十三第 3 件：通用 `approval_mode` 有确定的引擎落点，一并列进
            # 投射报告（映射表见 drivers.hermes.approval_map）。
            runtime_config=binding.runtime_config,
            default_model_id=binding.default_model_id,
            default_provider_id=binding.default_provider_id,
            project_id=project.id,
            dry_run=dry_run,
            adopt=adopt,
        )

    def workspace_conventions(self, binding: AgentBinding | None = None) -> dict[str, Any]:
        """这台引擎在它的 profile 目录里认哪个指令文件（批次四十四）。

        与 ACP 那一路同名同形状（可选方法，接入层 ``getattr`` 探它）：「哪个引擎读
        哪个文件」是 Driver 的私有知识，公共层只收一个字符串。
        """
        del binding
        rule = projection_map.file_rule_for("instructions")
        return {"instructionsFile": rule.filename if rule is not None else None}

    async def inspect_drift(
        self,
        project: Project,
        binding: AgentBinding,
        effective_capabilities: EffectiveCapabilities | None = None,
    ) -> DriftReport:
        """``config.yaml`` 那一面的四态对账（批次二十四）。

        不给 ``effective_capabilities`` 时**没有期望侧**——那时只能报
        :func:`~drivers.hermes.projector.stale_drift`（「这次没对账」），
        不能假装 in_sync（规格 §2.4 末句）。
        """
        self._assert_binding(binding)
        del project
        if effective_capabilities is None:
            return projector_mod.stale_drift(binding.id)
        return projector_mod.inspect_config_drift(
            binding_id=binding.id,
            hermes_home=self.gateway_config_for(binding).hermes_home,
            effective=effective_capabilities,
            capabilities=await self.get_capabilities(),
            runtime_config=binding.runtime_config,
            default_model_id=binding.default_model_id,
            default_provider_id=binding.default_provider_id,
        )

    # ------------------------------------------------------------------ #
    # Native Session
    # ------------------------------------------------------------------ #

    async def list_native_sessions(
        self, binding: AgentBinding
    ) -> tuple[NativeSession, ...]:
        self._assert_binding(binding)
        supervisor = self.supervisor_for(binding)
        await supervisor.ensure_ready()
        endpoints = self._endpoints()
        canonical = self.canonical_map_for(binding)
        rows: list[Mapping[str, Any]] = []
        offset = 0
        # 规格 §2.5：单页 50，按 has_more + offset 迭代，硬上限 500 条。
        while offset < 500:
            response = await supervisor.client().request_ok(
                "GET", f"{endpoints.path('sessions')}?limit=50&offset={offset}"
            )
            payload = response.json() or {}
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, list):
                break
            rows.extend(row for row in data if isinstance(row, Mapping))
            if not payload.get("has_more"):
                break
            offset += 50
        return session_mapper.dedupe_by_head(
            session_mapper.visible_sessions(rows, binding_id=binding.id, canonical=canonical)
        )

    async def count_native_sessions(self, binding: AgentBinding) -> int | None:
        """这条 Binding 下**看得见**的原生会话数（批次十九第 2 件）。

        口径与 :meth:`list_native_sessions` 逐字一致——数的就是那份列表：
        ``hidden`` / ``archived`` 的不数、归档分段并进 canonical head 后只数一条
        （规格 §4.4 规则 3）。刻意不去读 ``has_more`` 之类的总数字段：那个数字
        算的是另一件事（后端库里有多少行），与界面上「你有几条会话」对不上。

        取不到就是 ``None``——网关没在跑、401、响应形状不认识，都属于「这次数
        不出来」。返回 ``0`` 会被界面渲染成「0 条会话」，那是一句谎话。
        """
        self._assert_binding(binding)
        try:
            return len(await self.list_native_sessions(binding))
        except Exception as exc:  # noqa: BLE001 - 数不出来 ≠ 一条都没有
            LOGGER.debug("count_native_sessions 数不出来：%s", redact(str(exc)))
            return None

    async def create_native_session(
        self, binding: AgentBinding, options: CreateSessionOptions
    ) -> NativeSession:
        self._assert_binding(binding)
        supervisor = self.supervisor_for(binding)
        await supervisor.ensure_ready()
        endpoints = self._endpoints()
        # 规格 §2.5：先 POST {}（文档说「create empty session」，请求体字段未验证），
        # 再按需 PATCH 补标题。model / reasoning 在建会话时**无字段可下发**（§8-⑦）。
        response = await supervisor.client().request_ok(
            "POST", endpoints.path("sessions"), body={}
        )
        payload = response.json() or {}
        session = payload.get("session") if isinstance(payload, Mapping) else None
        session = session if isinstance(session, Mapping) else payload
        session_id = session.get("id") if isinstance(session, Mapping) else None
        if not isinstance(session_id, str) or not session_id:
            raise DriverError(
                "创建原生会话的响应里没有会话 id（v1.0 §8.7 禁止按「最新会话」猜测）"
            )
        if options.title:
            with contextlib.suppress(HermesHttpError):
                await supervisor.client().request(
                    "PATCH",
                    endpoints.path("sessions") + f"/{session_id}",
                    body={"title": options.title},
                )
        canonical = self.canonical_map_for(binding)
        row = dict(session)
        if options.title:
            row["title"] = options.title
        return session_mapper.to_native_session(
            row, binding_id=binding.id, canonical=canonical
        )

    async def load_native_history(
        self, binding: AgentBinding, native_session_id: str
    ) -> NativeHistory:
        """规格 §4.2 的三级优先级。**HTTP 是主路径，直读只是回退。**"""
        self._assert_binding(binding)
        canonical = self.canonical_map_for(binding)
        # 规格 §4.4 规则 5：只加载 head 的 messages，归档分段默认不回放。
        head_id = canonical.head_for(native_session_id)
        supervisor = self.supervisor_for(binding)
        endpoints = self._endpoints()
        try:
            await supervisor.ensure_ready()
            response = await supervisor.client().request(
                "GET",
                endpoints.path("session_messages", session_id=head_id)
                + f"?limit={history_mod.DEFAULT_PAGE_SIZE}&order=latest",
            )
            if response.ok:
                payload = response.json()
                # 批次十七第 4 件：留一份原始响应体给取证端点（内存里，脱敏后才上 wire）。
                if isinstance(payload, Mapping):
                    self._last_history_payload[head_id] = payload
                history = history_mod.history_from_http(head_id, payload)
                if history is not None:
                    return history
        except HermesHttpError:
            pass
        # 回退：直读 state.db（只读、WAL、按 schema 分档）。
        db_path = self.gateway_config_for(binding).hermes_home / "state.db"
        if db_path.exists():
            with contextlib.suppress(Exception):
                return history_mod.read_history_from_db(db_path, head_id)
        return NativeHistory(
            native_session_id=head_id, complete=False, missing=("history",)
        )

    def debug_last_history_payload(
        self, native_session_id: str | None = None
    ) -> Mapping[str, Any] | None:
        """最近一次原生历史 HTTP 响应的**原始**响应体（批次十七第 4 件）。

        取证专用：真机上工具行补不出来时，第一件要确认的就是「这份响应到底长
        什么样、``tool_calls`` 到底在哪一层」。本方法只把内存里那份原样交出去，
        **脱敏由调用方（接入层的 debug 端点）负责**——那条端点只在
        ``DASH_DEBUG=1`` 时挂载，且只输出键名 + 值类型 + 截断 80 字。

        不给 session id 就给最近记下的那一条。没有就是 ``None``（不编）。
        """
        if native_session_id:
            head = self._last_history_payload.get(native_session_id)
            if head is not None:
                return head
            # 传进来的可能是归档分段 id；按 head 再找一次。
            for binding in self._bindings.values():
                canonical = self.canonical_map_for(binding).head_for(native_session_id)
                if canonical in self._last_history_payload:
                    return self._last_history_payload[canonical]
            return None
        if not self._last_history_payload:
            return None
        return next(reversed(list(self._last_history_payload.values())))

    def _record_stream_shape(self, state: _RuntimeState, run: _RunState) -> None:
        """收尾时把这一轮 SSE 的**形状**记下来（批次二十第 3 件）。

        只记类型名与计数：内容、入参、输出、文本一个字都不留——这份东西是给取证
        端点看的，而那条端点在真机上要经过一次 HTTP 才到人手里。
        """
        types = list(run.stream_event_types)
        counts: dict[str, int] = {}
        for name in types:
            counts[name] = counts.get(name, 0) + 1
        self._last_stream[state.conversation.id] = {
            "runId": run.run_id,
            "eventTypes": types,
            "counts": counts,
            "total": len(types),
            "sawRunCompleted": run.translator.saw_run_completed,
            "replayBehaviour": run.replay_behaviour.value
            if hasattr(run.replay_behaviour, "value")
            else str(run.replay_behaviour),
            "recordedAt": _utcnow().isoformat(),
            # 批次二十一第 2 件：这一轮有没有点过停止、`/stop` 到底回了什么。
            "stop": dict(run.stop_record) if run.stop_record is not None else None,
        }

    def debug_last_stream(self, conversation_id: str) -> Mapping[str, Any] | None:
        """这条会话最近一次 run 从网关收到的事件**类型序列与计数**。

        没有就是 ``None``（不编）。任何内容都不在里面——见 :meth:`_record_stream_shape`。
        """
        return self._last_stream.get(conversation_id)

    # ------------------------------------------------------------------ #
    # Card Runtime
    # ------------------------------------------------------------------ #

    async def session_options_for(
        self, conversation: Conversation, effective: EffectiveCapabilities
    ) -> SessionProjection | None:
        binding = self._binding_for(conversation)
        if not binding.runtime_config.get("group_execution"):
            return None
        from drivers.hermes.isolated_runtime import create_driver
        return await create_driver(self, binding).session_options_for(conversation, effective)

    async def start_runtime(
        self, conversation: Conversation, surface: Literal["card"], *,
        session_options: CreateSessionOptions | None = None,
    ) -> RuntimeHandle:
        if surface != "card":
            raise UnsupportedCapabilityError(
                "Driver 只负责 Card Surface；站外 CLI 走 build_external_cli_launch"
            )
        binding = self._binding_for(conversation)
        if binding.runtime_config.get("group_execution"):
            from drivers.hermes.isolated_runtime import create_driver, start
            driver = create_driver(self, binding)
            handle = await start(driver, binding, conversation, surface, session_options=session_options)
            # A new driver starts its counter at one; use a unique owner handle.
            native_handle = handle
            handle = handle.model_copy(update={"runtime_id": "isolated-" + uuid.uuid4().hex})
            self._isolated_runtimes[handle.runtime_id] = (driver, native_handle)
            return handle
        supervisor = self.supervisor_for(binding)
        status = await supervisor.ensure_ready()
        if status.state == "unavailable":
            # 批次十六第 1 件：把修法一路带到接入层（`runtime_start_failed` 的
            # `detail.hint`），而不是只丢一句「引擎没能起来」。
            raise DriverError(
                status.describe() or "gateway 未就绪", failure=status.failure
            )

        native_session_id = conversation.native_session_id
        if not native_session_id:
            session = await self.create_native_session(binding, CreateSessionOptions())
            native_session_id = session.native_session_id
        else:
            native_session_id = self.canonical_map_for(binding).head_for(native_session_id)

        watcher = self.watcher_for(binding)
        watcher.track(native_session_id)
        watcher.prime()

        self._runtime_counter += 1
        handle = RuntimeHandle(
            runtime_id=f"rt_{uuid.uuid4()}",
            conversation_id=conversation.id,
            binding_id=conversation.agent_binding_id,
            backend_id=self.backend_id,
            native_session_id=native_session_id,
            started_at=_utcnow(),
            # 规格 §2.6：metadata 是 Driver 私有区（N §5.2 路径 B）。
            # **只有 ref，没有值**；不得进公共事件。
            metadata={
                "baseUrl": supervisor.base_url,
                "keyRef": supervisor.config.key_ref,
                "keyRefDescription": describe_credential_ref(supervisor.config.key_ref),
                "hermesHome": str(supervisor.config.hermes_home),
                "gatewayMode": supervisor.mode,
                "activeRunId": None,
            },
        )
        self._runtimes[handle.runtime_id] = _RuntimeState(
            handle=handle,
            conversation=conversation,
            binding=binding,
            supervisor=supervisor,
        )
        return handle

    def adopt_conversation(
        self, runtime: RuntimeHandle, conversation: Conversation
    ) -> None:
        """Session Host 换了 Conversation（改了模型快照等）→ 换我们手上这份。"""
        if runtime.runtime_id in self._isolated_runtimes:
            return
        state = self._runtimes.get(runtime.runtime_id)
        if state is not None and state.conversation.id == conversation.id:
            state.conversation = conversation

    async def effective_model_id(self, binding: AgentBinding) -> str | None:
        """这条 Binding 此刻**实际**会用哪个模型（批次十七第 3 件）。

        解析链与 ``GET /api/bindings/{id}/effective-settings`` 逐字相同：
        **Binding 覆盖 → 引擎自己的配置 → 目录默认**。两处必须是同一条链——
        界面上显示的是这条链算出来的值，判「会话快照与实际不一致」时却只看
        Binding 那一层的话，就会出现「界面显示 gpt-5.6-sol、后端说你指定了
        gpt-5.6-sol 而默认是 None」这种自相矛盾（真机 500 的来路，见
        `docs/quality/verify5.md` ②）。

        每一层都**取不到就跳过**，不是失败：读不到引擎配置、网关不在、目录为空，
        都只是让链短一层（N §13.1）。
        """
        if binding.default_model_id:
            return binding.default_model_id
        try:
            engine = await self.read_engine_settings(binding)
        except Exception:  # noqa: BLE001 - 少一层来源，不是错误
            engine = None
        if engine is not None and engine.model_id:
            return engine.model_id
        try:
            catalog = await self.get_model_catalog(binding)
        except Exception:  # noqa: BLE001 - 同上
            return None
        return catalog.default_model_id or None

    async def _model_adoption(self, state: _RuntimeState) -> str | None:
        """这一句要不要**采纳**引擎当前的模型？要就返回那个模型 id（AD-155）。

        会话级模型快照这台引擎认不认（批次十六第 4 件 / 规格 §2.5）。

        优先级链是「Conversation 快照 > Binding 默认」（v1.0 §7.3）。问题是
        ``POST /v1/runs`` 的请求体里**没有** model 字段（规格 §2.5 / §8-⑦ 未验证，
        本规格纪律：不发规格里没有的字段）——也就是说这台引擎只会按它自己的当前
        配置跑。

        批次十七把比较对象从「Binding 默认」换成「有效模型」：真机上 Binding 的
        ``default_model_id`` 是空的，模型来自引擎的 ``config.yaml``，而草稿页建
        会话时按 effective-settings 写了一份同值的快照——两个值其实说的是同一个
        模型，旧比较却判成不一致，于是新会话的第一条消息直接炸掉。

        **AD-155（批次三十一）：不一致不再拦发送。** 在此之前这里抛
        :class:`UnsupportedCapabilityError`（接入层 501
        ``conversation_model_unsupported``）。真机上它的后果是：用户把项目配置
        物化到引擎、``model.default`` 从 A 变成 B 之后，**所有**快照还是 A 的旧
        会话一句话都发不出去——快照过期不是用户做错了什么，把整段历史锁死是最坏
        的一种诚实。改判：返回引擎此刻的有效模型，由 :meth:`send_message` 先发一条
        ``kaus/model.adopted`` 说明「从这里起按 B 继续」，再照常提交。

        仍然拒绝的只剩一处：显式的 ``PATCH /conversations/{id}`` 换模型——那是
        用户主动要求一件这台引擎做不到的事，静默接受才是撒谎（见
        :func:`app.api.session_router` 的 ``patch_conversation``）。

        返回 ``None`` = 不用采纳（没有快照 / 快照就是有效模型 / 引擎本来就支持
        按会话指定模型 / 根本问不出有效模型是什么）。
        """
        snapshot = state.conversation.model_id
        if not snapshot:
            return None
        if snapshot == state.binding.default_model_id:
            return None
        effective = await self.effective_model_id(state.binding)
        if not effective or snapshot == effective:
            # 问不出「实际会用哪个」时不采纳：采纳成一个我们并不知道的值，
            # 比留着一份过期快照更糟（N §13.1：没有来源就不编）。
            return None
        try:
            capabilities = await self.get_capabilities()
        except Exception:  # noqa: BLE001 - 问不到能力表就按「不支持」保守处理
            capabilities = None
        if capabilities is not None and capabilities.models.conversation_scoped.is_supported:
            # 引擎能按回合带模型：快照是有效的，没有什么可采纳。
            return None
        return effective

    def _model_adopted_envelope(
        self, state: _RuntimeState, *, from_model: str | None, to_model: str
    ) -> AgentEventEnvelope:
        """AD-155 的那条通知：``kaus/model.adopted``。

        走 :func:`make_envelope` 而不是 Translator：它**不属于任何一轮**
        （``run_id=None``），必须排在 ``run.started`` 前面——它解释的正是这一轮
        为什么用的是另一个模型。``sequence`` 由 Session Host 统一重排。
        """
        conversation = state.conversation
        return make_envelope(
            event=ExtensionEvent(
                namespace=MODEL_ADOPTED_NAMESPACE,
                name=MODEL_ADOPTED_NAME,
                data={
                    "from": from_model,
                    "to": to_model,
                    "reason": MODEL_ADOPTED_REASON_NOT_SCOPED,
                },
            ),
            project_id=conversation.project_id,
            conversation_id=conversation.id,
            agent_binding_id=conversation.agent_binding_id,
            backend_id=self.backend_id,
            native_session_id=state.handle.native_session_id,
            run_id=None,
            sequence=0,
            source=EventSource(
                driver_kind=self.driver_kind, driver_version=self.driver_version
            ),
        )

    async def send_message(self, runtime: RuntimeHandle, content: MessageInput) -> None:
        if runtime.runtime_id in self._isolated_runtimes:
            driver, handle = self._isolated_runtimes[runtime.runtime_id]
            return await driver.send_message(handle, content)
        state = self._state(runtime)
        # AD-155：先算「要不要采纳」，但**先不发事件**——这一句还可能因为「上一轮
        # 还在跑」或网关拒绝而根本没发出去，那时时间线上不该留下一条采纳通知。
        adopted_model = await self._model_adoption(state)
        if state.run is not None and not state.run.finalized:
            # 批次二十七第 2 件：这不是故障，是「等一等」。用专门的异常类型说出来，
            # 接入层才能把它翻成一句人话而不是一个异常类名。
            raise TurnAlreadyRunningError(
                "上一轮尚未结束；先 interrupt 或等待终态",
                failure=failure_hints.turn_already_running(),
            )
        if self.active_run_count() >= MAX_CONCURRENT_RUNS:
            # 规格 §2.7：把同一 gateway 同时在跑的 run 限在 ≤ 8，留余量给用户自己的客户端。
            raise DriverError(
                "同一后端同时在跑的回合过多，请稍后再试（已为你自己的客户端留出余量）"
            )
        endpoints = self._endpoints()
        session_id = runtime.native_session_id
        idempotency_key = new_idempotency_key()
        headers = {
            "Idempotency-Key": idempotency_key,
        }
        if session_id:
            header_name = (
                self._report.session_continuity_header
                if self._report
                else "X-Hermes-Session-Id"
            )
            headers[header_name] = session_id

        body: dict[str, Any] = {"input": input_with_attachments(content)}
        if session_id:
            body["session_id"] = session_id
        if self._capabilities and self._capabilities.models.conversation_scoped.is_supported:
            if state.conversation.model_id:
                body["model"] = state.conversation.model_id
                if state.conversation.provider_id:
                    body["provider"] = state.conversation.provider_id
            if state.conversation.reasoning_mode:
                effort = state.conversation.reasoning_mode
                body["model_options"] = {"reasoning": (
                    {"enabled": False} if effort == "none"
                    else {"enabled": True, "effort": effort}
                )}
        # 规格 §2.7：**不发 conversation_history**——那等于把 Dashboard 的渲染状态
        # 当账本（违反 D-16）。历史由 Hermes 自己从 SessionDB 载入。

        try:
            response = await state.supervisor.client().request_ok(
                "POST", endpoints.path("runs"), body=body, headers=headers
            )
        except HermesIdempotencyConflictError:
            raise
        except HermesHttpError as exc:
            # 批次二十七第 2 件：网关拒绝提交时，此前整串
            # 「… → HTTP 500: …」原样冒到 wire 上（真机 C3 的 `detail`）。
            # 「忙」翻成可等待的状态，其余带上网关自己那句话——但不带状态码。
            if failure_hints.looks_busy(exc.code, str(exc)):
                raise TurnAlreadyRunningError(
                    str(exc), failure=failure_hints.turn_already_running()
                ) from exc
            raise DriverError(
                str(exc), failure=failure_hints.run_submit_rejected(_gateway_words(exc))
            ) from exc
        payload = response.json() or {}
        run_id = payload.get("run_id") if isinstance(payload, Mapping) else None
        if not isinstance(run_id, str) or not run_id:
            raise DriverError("提交回合的响应里没有 run_id")
        replayed = bool(payload.get("replayed")) or (
            response.headers.get("idempotency-replayed", "").lower() == "true"
        )

        translator = HermesRunTranslator(
            target=TranslationTarget(
                project_id=state.conversation.project_id,
                conversation_id=state.conversation.id,
                agent_binding_id=state.conversation.agent_binding_id,
                backend_id=self.backend_id,
                native_session_id=session_id,
                driver_version=self.driver_version,
                backend_version=self._backend_version,
            ),
            run_id=run_id,
        )
        run_state = _RunState(
            translator=translator, run_id=run_id, idempotency_key=idempotency_key
        )
        state.run = run_state
        state.handle.metadata["activeRunId"] = run_id
        self.watcher_for(state.binding).set_run_active(True)

        if adopted_model is not None:
            # AD-155：**先**采纳通知，**后** run.started。顺序就是它在时间线上的
            # 位置——「这一轮为什么换了模型」得在这一轮开始之前说。
            await state.queue.put(
                self._model_adopted_envelope(
                    state,
                    from_model=state.conversation.model_id,
                    to_model=adopted_model,
                )
            )
        for envelope in translator.run_started(replayed=replayed):
            await state.queue.put(envelope)
        run_state.task = asyncio.create_task(
            self._pump_run(state, run_state), name=f"hermes-run:{run_id}"
        )

    async def _pump_run(self, state: _RuntimeState, run: _RunState) -> None:
        """读 SSE → 翻译 → 入队；断线按 §3.6 重连；流结束后查状态收尾（§3.4）。"""
        endpoints = self._endpoints()
        path = endpoints.path("run_events", run_id=run.run_id)
        elapsed = 0.0
        attempt = 0
        try:
            while True:
                reconnecting = attempt > 0
                first_frame: str | None = None
                stream = state.supervisor.client().stream_sse(path)
                try:
                    async for event in stream:
                        if first_frame is None:
                            first_frame = event.data
                            if reconnecting:
                                # 规格 §3.6：比对重连后的首事件与本 run 已投递的首事件，
                                # 判别服务端是「从头重放」还是「只发新事件」。
                                behaviour = reconnect.classify_replay(
                                    run.first_frame, first_frame
                                )
                                run.replay_behaviour = behaviour
                                if reconnect.needs_reconciliation(behaviour):
                                    await state.queue.put(
                                        run.translator.notice(
                                            "warn",
                                            "实时流断开后未能续上完整片段，"
                                            "本轮结束时会从原生历史补齐",
                                        )
                                    )
                            else:
                                run.first_frame = first_frame
                        run.stream_event_types.append(_event_name(event))
                        for envelope in run.translator.translate_sse(event):
                            await state.queue.put(envelope)
                        if run.translator.saw_run_completed:
                            # **规格 §3.4：「终态后 Driver 关闭该 run 的 SSE」——
                            # 关流是我们的事，不是网关的事。** 0.21 的网关在 run
                            # 跑完之后仍然把 body 挂着（§3.6 引的文档：未被消费的
                            # 事件缓冲要五分钟才过期，就是为了防「detached
                            # client」），所以在这里等它关流 = 永远等不到：收尾、
                            # 末尾正文与终态一条都发不出去，页面永远 Running，
                            # 最后只能被 Session Host 自愈成 runtime_lost。
                            # 这正是 AD-146 那条真机阻断的根因。
                            return
                    return
                except asyncio.CancelledError:
                    raise
                except HermesHttpError as exc:
                    if exc.status in (404, 410):
                        # 规格 §3.6 (c)：缓冲已过期 → 直接进 POLLING（由 finalize 收尾）。
                        run.replay_behaviour = reconnect.ReplayBehaviour.EXPIRED
                        return
                    delay = reconnect.backoff_for(attempt)
                    attempt += 1
                    elapsed += delay
                    if reconnect.exhausted(elapsed) or run.translator.saw_run_completed:
                        await state.queue.put(
                            run.translator.notice("warn", str(redact(str(exc))))
                        )
                        return
                    await asyncio.sleep(delay)
                finally:
                    # 收尾前必须先把这条流真的关掉：``async for`` 上的 ``return``
                    # 只是不再取值，异步生成器要显式 ``aclose()`` 才会走到它的
                    # ``finally`` 去关 socket。收尾还要读一次原生历史补工具账，
                    # 挂着一条半开的连接对真机没有好处。
                    with contextlib.suppress(Exception):
                        await stream.aclose()
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(asyncio.CancelledError):
                await self._finalize_run(state, run)

    async def _finalize_run(
        self,
        state: _RuntimeState,
        run: _RunState,
        *,
        run_object: Mapping[str, Any] | None = None,
        by_stop: bool = False,
    ) -> None:
        """原生终态确认后补工具账、正文和终态；读取不到状态时保持未完成。"""
        if state.closed or run.finalized or (run.stop_owns_finalize and not by_stop):
            return
        endpoints = self._endpoints()
        run_object = run_object or run.stop_run_object
        if run_object is None:
            try:
                response = await state.supervisor.client().request(
                    "GET", endpoints.path("run_status", run_id=run.run_id)
                )
                if response.ok and isinstance(response.json(), Mapping):
                    run_object = response.json()
            except HermesHttpError:
                run_object = None
        status = str((run_object or {}).get("status") or "")
        if status not in TERMINAL_STOP_STATUSES:
            if not run.translator.saw_run_completed:
                return
            # SSE 已明确给出结束信号，状态读取可能还停留在旧值。
            run_object = {**(run_object or {}), "status": "completed"}
        run.finalized = True
        # 批次十六第 3 件：**先**补工具账，**再**发终态。顺序不能反——终态之后的
        # 内容事件会被 reducer 当成迟到事件丢掉（N §7.3 规则 9）。
        #
        # AD-146：这一段是**尽力而为**的补账，绝不允许它拖死收尾。回填要读一次
        # 原生历史（HTTP + 解析别人给的 JSON），任何一处抛异常——历史端点 500、
        # 响应形状不认识、配对数量对不上时的解析路径——都会从这里逃出去，把后面
        # 的末尾正文与终态一起吞掉；而 ``_pump_run`` 里那次 finalize 跑在一个独立
        # Task 的 ``finally`` 上，异常连日志都只是「Task exception was never
        # retrieved」。于是页面永远停在 Running。**终态必须发出去。**
        try:
            backfilled = await self._tool_output_backfill(state, run)
        except Exception as exc:  # noqa: BLE001 - 收尾不可被回填拖死（AD-146）
            LOGGER.warning(
                "工具回填抛异常，已跳过补账继续收尾：%s: %s",
                type(exc).__name__,
                redact(str(exc)),
            )
            backfilled = (
                run.translator.notice(
                    "info",
                    "本回合的工具输出没能补齐（回填过程出错），回合结果不受影响",
                ),
            )
        for envelope in backfilled:
            await state.queue.put(envelope)
        closing = run.translator.finalize(run_object)
        for envelope in closing:
            await state.queue.put(envelope)
        self._record_stream_shape(state, run)
        state.handle.metadata["activeRunId"] = None
        watcher = self.watcher_for(state.binding)
        watcher.set_run_active(False)
        # 规格 §5.2 自写抑制：回合结束把水位推到当前 MAX(id)，
        # 否则我们自己刚写的消息会被当成带外写入。
        watcher.sync_watermark(state.handle.native_session_id)

    async def _tool_output_backfill(
        self, state: _RuntimeState, run: _RunState
    ) -> tuple[AgentEventEnvelope, ...]:
        """回合结束后从原生历史补齐工具输出与完整入参（规格 §3.2-D / §4.2）。

        为什么必须补：这条 SSE 的 ``tool.started`` 只给一个**截断的** ``preview``，
        ``tool.completed`` 连输出都不带（两者都是实测）。也就是说工具行在流式阶段
        永远是「跑了个 terminal，看不到跑了什么、也看不到结果」——展开工具行看不到
        输出是用户最先会抱怨的事。完整入参与结果只在 ``messages.tool_calls`` 与
        ``role="tool"`` 行里，只有回合结束后才读得到。

        配对规则（与 §3.3 的 FIFO 同一条）：本 run 合成的 callId 按出现顺序，
        与原生历史里**最后 N 条**工具调用一一对应（N = 本 run 的工具调用数）。
        原生历史里的调用数**少于** N（读不到、被压缩、分页没覆盖到）时
        **一条都不发**——宁可维持现状，也不能把 A 工具的输出贴到 B 的卡上。
        """
        pairs = run.translator.tool_calls_in_order
        if not pairs:
            return ()
        diagnostics = self._tool_backfill_diagnostics_enabled(state.binding)

        def _note(level: str, message: str) -> list[AgentEventEnvelope]:
            """日志始终写；事件只在 debug 开关打开时发（批次十七第 4 件）。"""
            LOGGER.info("工具回填：%s", message)
            return [run.translator.notice(level, message)] if diagnostics else []

        try:
            history = await self.load_native_history(
                state.binding, state.handle.native_session_id or ""
            )
        except (DriverError, OSError) as exc:
            return tuple(
                _note(
                    "info",
                    "读不到原生历史，本回合的工具输出没能补齐"
                    f"（{type(exc).__name__}: {redact(str(exc))}）",
                )
            )
        native_calls, results = history_mod.tool_calls_from_history(history.entries)
        if len(native_calls) < len(pairs):
            # 读不到就不发，不猜（N §13.1）。
            return tuple(
                _note(
                    "info",
                    f"原生历史里的工具调用 {len(native_calls)} 条 < 本回合的 "
                    f"{len(pairs)} 条，为避免张冠李戴，这一轮一条都不补",
                )
            )
        tail = native_calls[-len(pairs) :]
        envelopes: list[AgentEventEnvelope] = []
        for (call_id, tool_name), native in zip(pairs, tail):
            output = results.get(native.call_id)
            if output is None and native.arguments is None:
                # 既没有入参也没有结果 → 这条补账等于没有内容，不发。
                continue
            envelopes.append(
                run.translator.tool_backfill(
                    call_id,
                    output=output,
                    arguments=native.arguments,
                    native_call_id=native.call_id,
                    tool_name=native.name or tool_name,
                )
            )
        return tuple(
            [
                *envelopes,
                *_note(
                    "info",
                    f"已从原生历史补齐 {len(envelopes)} 条工具输出"
                    f"（本回合 {len(pairs)} 条调用，历史里 {len(native_calls)} 条）",
                ),
            ]
        )

    def _tool_backfill_diagnostics_enabled(self, binding: AgentBinding) -> bool:
        """回填诊断的开关：``runtime_config.tool_backfill_diagnostics``。

        **默认关**：这些是取证用的絮语，不该出现在正常用户的时间线上。日志无条件
        写，因此关着也不影响事后排查——开关只决定「要不要把它也发给页面」。
        """
        raw = (binding.runtime_config or {}).get("tool_backfill_diagnostics")
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)

    async def interrupt(self, runtime: RuntimeHandle) -> None:
        """规格 §2.0：``POST /v1/runs/{run_id}/stop``（实测 200 + 完整 run 对象）。

        批次十六第 2 件把这里的两处「静默成功」堵上：

        1. 后端**没有**停止接口（能力里 ``run_stop`` 为假，或该端点回 404/405/501）
           → 抛 :class:`UnsupportedCapabilityError`，而不是假装停了。接入层据此
           回 501，前端按 AD-71 不显示停止按钮。
        2. 其余 HTTP 失败（网络抖动、5xx）仍然吞掉：请求发不出去不代表回合还在跑，
           下面的收尾照做；真正「引擎没认」的判定在 Session Host 那一层（它等
           终态，等不到就发 warn）。
        """
        if runtime.runtime_id in self._isolated_runtimes:
            driver, handle = self._isolated_runtimes[runtime.runtime_id]
            return await driver.interrupt(handle)
        state = self._state(runtime)
        run = state.run
        if run is None:
            raise DriverError("当前没有正在进行的回合")
        if run.finalized or run.stop_owns_finalize:
            # 同一轮的重复点击复用现有确认任务，不能让两个收尾者相互取消。
            return
        report = self._report
        if report is not None and not report.features.get("run_stop"):
            raise UnsupportedCapabilityError(
                "该 Hermes 网关没有声明 run_stop：这一轮只能等它自己跑完，"
                "或到引擎那一侧停止"
            )
        endpoints = self._endpoints()
        # 批次十八第 2 件（AD-130 补）：从这一刻起**这条路负责收尾**。
        # 认领必须早于 POST：SSE 泵那边可能因为服务端关流而同时跑进它的
        # ``finally``，两条 finalize 抢同一条 HTTP 连接（一条被 /stop 占着）会
        # 卡住，而先到的那条已经把 `finalized` 置真——于是谁都没发出终态，
        # 页面永远停在「正在停止」。认领之后泵那边直接让路。
        run.stop_owns_finalize = True
        record: dict[str, Any] = {
            "responseStatus": None,
            "bodyStatus": None,
            "polled": [],
            "outcome": None,
        }
        run.stop_record = record
        try:
            try:
                response = await state.supervisor.client().request(
                    "POST", endpoints.path("run_stop", run_id=run.run_id)
                )
            except HermesHttpError:
                response = None
            if response is not None:
                record["responseStatus"] = response.status
                record["bodyStatus"] = _status_word(response)
            else:
                record["outcome"] = "request_failed"
            if response is not None and response.status in (404, 405, 501):
                record["outcome"] = "unsupported"
                # 404 既可能是「没有这个端点」也可能是「这个 run 不认识了」。后者在
                # 这里无害（回合已经不在了），前者必须说出来——两者都不该被当成
                # 「已中断」。
                raise UnsupportedCapabilityError(
                    f"停止接口不可用（HTTP {response.status}）：这一轮没能停下来"
                )
            # 规格 §2.0 实测 ``POST /stop`` 回 200 + **完整 run 对象**：它已经把
            # 「这一轮到底怎么结束的」写在响应体里了，那就当场收尾——不必等 SSE
            # 关流，也不必让 Session Host 空等满 5 秒去发一句「引擎没有确认中断」。
            # 真机 ② 看到的正是那句话，而事实上 run 早已终态。只在**终态**上这么
            # 做：``stopping`` 是过渡态（规格 §3.4），那时仍旧走下面的等待。
            terminal = _terminal_run_object(response)
            if terminal is not None:
                record["outcome"] = "stop_body_terminal"
                run.stop_run_object = terminal
                # 关 SSE 要排在收尾之前：收尾还要读一次原生历史补工具账，而那条
                # HTTP 连接正被这一轮的事件流占着，不先放开就会一直等下去。
                await self._close_run_task(run)
                await self._finalize_run(state, run, by_stop=True)
                return
            # 未确认时在后台持续核查，响应点击无需等待期限；真实终态照常走事件流。
            run.stop_task = asyncio.create_task(
                self._settle_stop(state, run, record), name=f"hermes-stop:{run.run_id}"
            )
        except BaseException:
            # 请求失败或调用者取消时，把收尾权还给 SSE 泵，允许后续重试停止。
            run.stop_owns_finalize = False
            raise

    async def run_is_active(self, runtime: RuntimeHandle) -> bool | None:
        """这条 handle 上的回合**现在还在跑吗**（AD-147 第 3 件的判据）。

        ``True`` 还在、``False`` 已经不在、``None`` 不知道（HTTP 读不到）。
        Session Host 只在**明确的 ``False``** 上才合成终态：不知道就不动，
        编一句「中断了」比不收敛更糟。

        这是一个**可选钩子**（`BackendDriver` 协议里没有它），Session Host 用
        ``getattr`` 取；没有这条面的 Driver 一律按「不知道」处理。
        """
        if runtime.runtime_id in self._isolated_runtimes:
            return None  # The delegated transport has no out-of-band status API.
        try:
            state = self._state(runtime)
        except DriverError:
            return False
        run = state.run
        if run is None or run.finalized:
            return False
        endpoints = self._endpoints()
        try:
            response = await state.supervisor.client().request(
                "GET", endpoints.path("run_status", run_id=run.run_id)
            )
        except HermesHttpError as exc:
            # 404 = 网关不认识这个 run 了：它肯定不在跑了。其余读不到 = 不知道。
            return False if exc.status in (404, 410) else None
        if response.status in (404, 410):
            return False
        status = _status_word(response)
        if status is None:
            return None
        return status not in TERMINAL_STOP_STATUSES

    async def _settle_stop(
        self, state: _RuntimeState, run: _RunState, record: dict[str, Any]
    ) -> None:
        """保留 SSE 并持续核查原 run；超时提示一次，真实终态到达后才收尾。"""
        warned = False
        try:
            while not state.closed and not run.finalized:
                try:
                    polled, unconfirmed = await self._await_stop_terminal(state, run, record)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 读不到不等于已结束
                    LOGGER.warning("停止状态核查失败：%s: %s", type(exc).__name__, redact(str(exc)))
                    polled, unconfirmed = None, True
                    record["outcome"] = "poll_failed"
                # 长时间等待只保留最近的诊断状态，不积累无限历史。
                record["polled"] = record["polled"][-120:]
                if polled is not None:
                    run.stop_run_object = polled
                if not unconfirmed:
                    await self._close_run_task(run)
                    await self._finalize_run(state, run, by_stop=True)
                    return
                if record["outcome"] == "run_gone":
                    await self._close_run_task(run)
                    run.finalized = True
                    for envelope in run.translator.run_unavailable():
                        await state.queue.put(envelope)
                    state.handle.metadata["activeRunId"] = None
                    self.watcher_for(state.binding).set_run_active(False)
                    self._record_stream_shape(state, run)
                    return
                self.note_interrupt_unconfirmed()
                if not warned:
                    last_status = record["polled"][-1] if record["polled"] else record["bodyStatus"]
                    for envelope in run.translator.stop_unconfirmed(None, last_status=last_status):
                        await state.queue.put(envelope)
                    warned = True
                self._record_stream_shape(state, run)
                # HTTP 失败可能立即返回，至少等一个间隔，避免重试忙循环。
                await asyncio.sleep(max(self.stop_poll_interval, 0.05))
        finally:
            run.stop_owns_finalize = False

    async def _await_stop_terminal(
        self, state: _RuntimeState, run: _RunState, record: dict[str, Any]
    ) -> tuple[Mapping[str, Any] | None, bool]:
        """`/stop` 之后按规格 §5 每秒读一次状态，直到终态或到点（AD-147）。

        返回 ``(终态 run 对象 | None, 是否未确认)``。三条出口：

        - SSE 先给了终态（``run.completed``）→ ``(None, False)``，走正常收尾；
        - ``GET /v1/runs/{id}`` 读到终态 → ``(run 对象, False)``；
        - 到点仍是 ``running`` / ``stopping`` → ``(None, True)``，提示后继续核查。

        轮询期间**不关流**：引擎可能只是慢，SSE 上先到的终态照样算数。
        """
        endpoints = self._endpoints()
        deadline = time.monotonic() + self.stop_confirm_timeout
        while True:
            if run.translator.saw_run_completed:
                record["outcome"] = "sse_terminal"
                return None, False
            try:
                response = await state.supervisor.client().request(
                    "GET", endpoints.path("run_status", run_id=run.run_id)
                )
            except HermesHttpError as exc:
                record["polled"].append(f"error:{exc.status}")
                response = None
            if response is not None and response.status in (404, 410):
                # 原生记录已不可查询，调用方会明确报告结果缺失。
                record["polled"].append("gone")
                record["outcome"] = "run_gone"
                return None, True
            if response is not None:
                record["polled"].append(_status_word(response) or "<none>")
                terminal = _terminal_run_object(response)
                if terminal is not None:
                    record["outcome"] = "polled_terminal"
                    return terminal, False
            if time.monotonic() >= deadline:
                record["outcome"] = "stop_unconfirmed"
                return None, True
            await asyncio.sleep(self.stop_poll_interval)

    @staticmethod
    async def _close_run_task(run: _RunState) -> None:
        """把这一轮的 SSE 泵收掉（它的 ``finally`` 里那次 finalize 已被跳过）。"""
        task = run.task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def resolve_interaction(
        self,
        runtime: RuntimeHandle,
        interaction_id: str,
        response: InteractionResponse,
    ) -> None:
        """规格 §2.8：``POST /v1/runs/{run_id}/approval`` 解决精确请求。"""
        if runtime.runtime_id in self._isolated_runtimes:
            driver, handle = self._isolated_runtimes[runtime.runtime_id]
            return await driver.resolve_interaction(handle, interaction_id, response)
        state = self._state(runtime)
        if response.kind != "permission":
            # question / authentication 在 API server 上没有对应端点（规格 §2.8）。
            raise UnsupportedCapabilityError(
                f"该 Backend 没有 {response.kind} 类交互的解决端点；请用 Open in CLI"
            )
        run = state.run
        if run is None:
            raise InteractionNotFoundError(f"没有待解决的交互请求：{interaction_id!r}")
        choice = "deny" if response.cancelled else OPTION_TO_CHOICE.get(
            response.option_id or "", "once"
        )
        endpoints = self._endpoints()
        http = await state.supervisor.client().request(
            "POST",
            endpoints.path("run_approval", run_id=run.run_id),
            # 0.21.0 accepts the exact upstream request id.  Supplying it keeps
            # concurrent approvals scoped correctly; older servers ignore the
            # additional field.
            body={"choice": choice, "all": False, "request_id": interaction_id},
        )
        if http.ok:
            return
        if (
            http.status == 400 and http.error_code() == "invalid_approval_choice"
        ) or (
            http.status == 409
            and http.error_code() in {"approval_not_active", "approval_not_pending"}
        ):
            # 0.21.0 reports an already-resolved request as 409; older builds
            # used 400 invalid_approval_choice.  Both converge to the same
            # public state instead of surfacing a spurious error.
            await state.queue.put(run.translator.resolved_elsewhere(interaction_id))
            return
        raise DriverError(f"解决审批失败：{http.error_message()}")

    async def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEventEnvelope]:
        if runtime.runtime_id in self._isolated_runtimes:
            driver, handle = self._isolated_runtimes[runtime.runtime_id]
            async for event in driver.events(handle):
                yield event
            return
        state = self._state(runtime)
        while True:
            item = await state.queue.get()
            if item is _SENTINEL:
                return
            yield item

    async def stop_runtime(self, runtime: RuntimeHandle) -> None:
        """规格 §2.6：关 SSE、清 activeRunId、释放句柄。**不停 gateway 进程。**"""
        isolated = self._isolated_runtimes.pop(runtime.runtime_id, None)
        if isolated is not None:
            driver, handle = isolated
            return await driver.stop_runtime(handle)
        state = self._runtimes.pop(runtime.runtime_id, None)
        if state is None:
            return
        state.closed = True
        run = state.run
        if run is not None and run.stop_task is not None and not run.stop_task.done():
            # 批次二十一：`/stop` 之后那条后台等待随句柄一起收掉。
            run.stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run.stop_task
        if run is not None and run.task is not None and not run.task.done():
            run.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run.task
        state.handle.metadata["activeRunId"] = None
        if state.handle.native_session_id and not self._home_still_in_use(state):
            self.watcher_for(state.binding).untrack(state.handle.native_session_id)
        await state.queue.put(_SENTINEL)

    def _home_still_in_use(self, closing: _RuntimeState) -> bool:
        home = str(self.gateway_config_for(closing.binding).hermes_home)
        return any(
            str(self.gateway_config_for(other.binding).hermes_home) == home
            for other in self._runtimes.values()
        )

    # ------------------------------------------------------------------ #
    # 站外 CLI
    # ------------------------------------------------------------------ #

    async def build_external_cli_launch(
        self, conversation: Conversation
    ) -> CliLaunchSpec:
        """规格 §2.9：``hermes -p <profile> chat --resume <id>``。

        ``-p`` 的存疑（§8-⑧）在 0.21.0 上依旧：flag 表里没有它，但官方文档与生产
        Dashboard 都在用。冒烟脚本的 ``--check profile-flag`` 就是为定案它准备的。
        **不加 ``--create-if-missing``**：会话不存在是个应当报错的状态，不该被掩盖。
        """
        binding = self._binding_for(conversation)
        capabilities = await self.get_capabilities()
        if not capabilities.external_cli.supported:
            raise UnsupportedCapabilityError("该 Backend 不支持站外 CLI")
        profile = binding.native_scope_ref or session_mapper.DEFAULT_SCOPE
        canonical = self.canonical_map_for(binding)
        command: list[str] = [self.hermes_bin, "-p", profile, "chat"]
        resume = False
        if conversation.native_session_id:
            # 规格 §4.4 规则 4：一律用 canonical head，不用分段 id。
            command += ["--resume", canonical.head_for(conversation.native_session_id)]
            resume = True
        return CliLaunchSpec(
            command=tuple(command),
            cwd=None,
            # AD-10：只能列变量名，不能给值。HERMES_HOME 由 ``-p`` / wrapper 决定，
            # 绝不写成 ("env", "HERMES_HOME=…", …)（规格 §2.9 的「不允许的形态」）。
            env_passthrough=("PATH", "HOME"),
            title=conversation.title,
            correlation_id=f"launch-{conversation.id}",
            resume=resume,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def active_run_count(self) -> int:
        return sum(
            1
            for state in self._runtimes.values()
            if state.run is not None
            and not state.run.finalized
        )

    def _endpoints(self) -> capabilities_mod.HermesEndpoints:
        if self._report is not None:
            return self._report.endpoints
        return capabilities_mod.HermesEndpoints.from_capabilities(None)

    def _state(self, runtime: RuntimeHandle) -> _RuntimeState:
        state = self._runtimes.get(runtime.runtime_id)
        if state is None or state.closed:
            raise RuntimeNotFoundError(f"未知或已停止的 runtime：{runtime.runtime_id!r}")
        return state

    def _binding_for(self, conversation: Conversation) -> AgentBinding:
        binding = self._bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise DriverError(
                f"Driver 不认识这条 Binding：{conversation.agent_binding_id!r}；"
                "请先用 register_binding 登记（Binding 仓库属于上层，Driver 只缓存）"
            )
        return binding

    def register_binding(self, binding: AgentBinding) -> None:
        """把 Binding 交给 Driver。

        ``start_runtime`` / ``build_external_cli_launch`` 的入参只有 Conversation，
        而 backend 与原生作用域都在 Binding 上——Session Host 那边由
        ``AgentBindingRepository`` 解析，Driver 这边只需要一个本地缓存。
        """
        self._assert_binding(binding)
        self._bindings[binding.id] = binding


#: 批次十八第 2 件：``POST /v1/runs/{id}/stop`` 响应体里算「这一轮已经结束」的
#: status 取值。``stopping`` **不在**里面——规格 §3.4 明写它是过渡态，那时回合
#: 可能还在跑，当成终态就等于替引擎宣布了一件没发生的事。``stopped`` 是文档没写
#: 但同义的写法，一并认下。
TERMINAL_STOP_STATUSES: frozenset[str] = frozenset(
    {"cancelled", "stopped", "completed", "failed"}
)


def _gateway_words(exc: HermesHttpError) -> str:
    """从 ``… → HTTP 500: 网关自己那句话`` 里只取网关自己那句话。

    :func:`~drivers.hermes.http_client.classify_error` 拼出来的串是给日志看的
    （带上下文与状态码）；给用户看的那一半是冒号之后的部分。取不出来就退回整串
    ——**不编**，但也不会因为格式变了就变成空字符串。
    """
    text = str(exc)
    marker = " → HTTP "
    if marker in text:
        tail = text.split(marker, 1)[1]
        _, _, words = tail.partition(": ")
        if words.strip():
            return words.strip()
    return text


def _terminal_run_object(response: Any) -> Mapping[str, Any] | None:
    """``POST /stop`` 的响应体里带终态就把它取出来，否则 ``None``。

    ``None`` 有三种来路，行为一致（维持原来的等待逻辑）：请求没发出去、响应不是
    200、body 里没有终态 status。**不猜**：没写状态就是没写状态。

    ``stopped`` 归一成 ``cancelled`` 再交给 translator——公共层的终态映射只认规格
    §3.4 那五个取值，多一个别名不该让它掉进「未知状态」那条降级路径。
    """
    if response is None or not getattr(response, "ok", False):
        return None
    body = response.json()
    if not isinstance(body, Mapping):
        return None
    status = body.get("status")
    if not isinstance(status, str) or status not in TERMINAL_STOP_STATUSES:
        return None
    return {**body, "status": "cancelled" if status == "stopped" else status}


def _status_word(response: Any) -> str | None:
    """响应体里的 ``status`` **状态词**，取不到就是 ``None``（批次二十一第 2 件）。

    取证端点只吐状态词：run 对象里的 ``output``（完整助手正文）、``session_id``、
    ``usage`` 一律不碰。
    """
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 取证不该因为一份坏 JSON 就报错
        return None
    if not isinstance(body, Mapping):
        return None
    status = body.get("status")
    return status if isinstance(status, str) else None


def _event_name(event: Any) -> str:
    """一条 SSE 帧的事件名，取不到就是 ``"<unnamed>"``（批次二十第 3 件）。

    只看 ``data`` 载荷里的 ``event`` 键（规格 §3.1：这条流没有 ``event:`` 行）。
    **不返回载荷里的任何其它内容**——这个函数的产物会被送上取证端点。
    """
    payload = event.payload() if hasattr(event, "payload") else None
    if isinstance(payload, Mapping):
        name = payload.get("event")
        if isinstance(name, str) and name:
            return name
    return "<unnamed>"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)



def input_with_attachments(content: MessageInput) -> str:
    """把附件并进 ``/v1/runs`` 的文本输入。

    文本附件直接附上内容；其余文件（图片、PDF、表格……）只给本机路径，让 Hermes
    用自己的文件工具读取——字节不经过这条请求。
    """
    if not content.attachments:
        return content.text
    parts = [content.text] if content.text else []
    parts.append("附件：")
    for attachment in content.attachments:
        name = attachment.name or attachment.ref
        path = _local_path(attachment.ref)
        if attachment.content_text is not None:
            parts.append(f"--- {name} ---\n{attachment.content_text}\n--- {name} 结束 ---")
        else:
            kind = attachment.mime_type or "文件"
            parts.append(f"- {name}（{kind}）：{path}")
    return "\n\n".join(parts)


def _local_path(ref: str) -> str:
    """``file://`` URI → 本机路径；别的形状原样给出。"""
    parsed = urlsplit(ref)
    return unquote(parsed.path) if parsed.scheme == "file" else ref


__all__ = [
    "MAX_CONCURRENT_RUNS",
    "STOP_CONFIRM_TIMEOUT",
    "STOP_POLL_INTERVAL",
    "TERMINAL_STOP_STATUSES",
    "HermesDriver",
]
