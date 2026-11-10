"""``CodexAppServerDriver``: Kaus Card Runtime on the official ``codex app-server``.

Protocol source: ``openai/codex`` ``main`` @ ``8703d32b`` (2026-11-09),
``codex-rs/app-server-protocol`` (see :mod:`drivers.codex_app_server.protocol`).

Process topology
----------------
Same as the ACP driver: one ``codex app-server`` child per Card Runtime (stdio is a
single-client pipe), short-lived processes for probes. ``codex app-server`` listens
on ``stdio://`` by default (``codex-rs/app-server-transport/src/transport/mod.rs``
``DEFAULT_LISTEN_URL``), so the command is just ``<codex> app-server``.

What the process receives (and from where)
------------------------------------------
- **Project instructions** → ``thread/start.developerInstructions`` /
  ``thread/resume.developerInstructions`` (``v2/thread.rs``). The launch-delivery
  adapter (``drivers.skill_execution.codex.deliver_instructions``) writes them into
  the ``CODEX_CONFIG`` JSON of the process spec exactly as for ``codex-acp``; this
  driver lifts ``developer_instructions`` out of that JSON into the typed parameter
  and sends the rest as ``config``.
- **Native MCP exclusion / Project MCP / memories off** → ``thread/start.config``
  (``Option<HashMap<String, JsonValue>>``), the same per-thread override layer
  ``codex-acp`` v2.6.0 fills from ``CODEX_CONFIG`` (``src/index.ts``,
  ``CodexAcpClient.ts`` ``threadStart({config, …})``). Keys:
  ``mcp_servers.<name>.enabled=false`` (``codex-rs/config/src/mcp_types.rs``),
  ``mcp_servers.<name>={command,args,env}`` for Kaus project servers including the
  Project-memory MCP, ``memories.generate_memories/use_memories=false`` and
  ``features.memories=false`` (``codex-rs/config/src/config_toml.rs``).
- **API key** → ``OPENAI_API_KEY`` in the child environment only, from the host's
  encrypted store (``drivers.credentials.api_key_for``); Kaus never reads
  ``~/.codex/auth.json``.
- **Model / effort / approval** → ``thread/start`` (``model``, ``approvalPolicy``,
  ``approvalsReviewer``, ``sandbox``) and every ``turn/start`` (``model``,
  ``effort``; policy fields when they changed mid-conversation).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping, Sequence

from app.capabilities.models import EffectiveCapabilities
from app.conversations.models import Conversation
from app.ids import normalize_backend_id
from app.projects.models import AgentBinding, Project
from drivers.acp.client import (
    AcpAgentSpec,
    AcpConnection,
    AcpRpcError,
    AcpSpawnError,
    AcpTransportError,
)
from drivers.acp.failure_hints import auth_required, login_diagnostic, looks_like_auth_error, spawn_failed
from drivers.acp.mcp_projection import project_mcp_servers
from drivers.base import (
    AuthRequiredError,
    AuthState,
    BackendProbeResult,
    CliLaunchSpec,
    CreateSessionOptions,
    DriftReport,
    EngineSettings,
    FailureHint,
    InteractionNotFoundError,
    InteractionResponse,
    MessageInput,
    ModelCatalog,
    ModelDescriptor,
    ModelRejectedError,
    NativeHistory,
    NativeSession,
    ProjectionEntry,
    ProjectionResult,
    RuntimeHandle,
    RuntimeNotFoundError,
    SessionProjection,
    TurnAlreadyRunningError,
    UnappliedCapability,
    UnsupportedCapabilityError,
    turn_already_running_hint,
)
from drivers.codex_app_server import protocol as P
from drivers.codex_app_server.capabilities import declared_capabilities
from drivers.codex_app_server.translator import AppServerTranslator, PendingApproval, TranslationContext
from drivers.error_text import safe_error_text, safe_exception_message
from drivers.instructions_projection import INVALID_DETAIL, invalid_instruction_entries
from drivers.launch_delivery import apply_launch_plan, expand_plugins, home_of, plan_launch, same_directory
from drivers.projection_gaps import SKILLS, UNSUPPORTED
from runtime.capability_matrix import BackendCapabilities, SupportLevel
from runtime.event_envelope import AgentEventEnvelope

DRIVER_VERSION: str = "0.1.0"
CLIENT_NAME: str = "kaus"
#: The adapter module whose launch-delivery hooks (``deliver_instructions`` …) this
#: driver shares with the ACP path. It writes ``CODEX_CONFIG``; nothing else of it is used.
LAUNCH_ADAPTER: str = "drivers.skill_execution.codex"
CATALOG_TTL_SECONDS: float = 600.0
_SENTINEL = object()
_LOG = logging.getLogger(__name__)
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class _Snapshot:
    """What one probe (or one live thread) told us about this connection."""

    fetched_at: datetime
    models: tuple[ModelDescriptor, ...] = ()
    current_model_id: str | None = None
    current_effort: str | None = None
    current_policy: str | None = None
    auth_state: Literal["signed_in", "signed_out", "unknown"] = "unknown"
    degraded: bool = False
    diagnostics: tuple[str, ...] = ()
    auth_hint: str | None = None
    connect_hint: str | None = None

    def is_fresh(self, now: datetime) -> bool:
        ttl = 5.0 if self.degraded else CATALOG_TTL_SECONDS
        return (now - self.fetched_at).total_seconds() < ttl


@dataclass
class _RuntimeState:
    handle: RuntimeHandle
    conversation: Conversation
    connection: AcpConnection
    translator: AppServerTranslator
    thread_id: str
    spec: AcpAgentSpec
    metadata: dict[str, Any] = field(default_factory=dict)
    snapshot: _Snapshot | None = None
    model_id: str | None = None
    effort: str | None = None
    policy: str | None = None
    #: Policy fields are resent on the next ``turn/start`` after a mid-conversation change.
    policy_dirty: bool = False
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prompt_task: "asyncio.Task[None] | None" = None
    turn_done: asyncio.Event = field(default_factory=asyncio.Event)
    turn_id: str | None = None
    interrupt_requested: bool = False
    closed: bool = False


class CodexAppServerDriver:
    """N §5.3 contract on the app-server protocol; ``driver_kind = "native"``."""

    driver_kind: Literal["native"] = "native"

    def __init__(
        self,
        process_spec: AcpAgentSpec,
        *,
        backend_key: str = "codex",
        preset: Any = None,
        default_cwd: str | None = None,
        call_timeout: float = 30.0,
        turn_timeout: float = 3600.0,
        interrupt_grace: float = 5.0,
        native_history: bool = True,
    ) -> None:
        self.backend_id = normalize_backend_id(backend_key)
        #: Process spec (``AcpAgentSpec`` is just "argv + cwd + env"; nothing ACP about it here).
        self.agent_spec = process_spec
        self.preset = preset
        self.quirks = preset.quirks if preset is not None else None
        self._default_cwd = default_cwd or process_spec.cwd
        self._call_timeout = call_timeout
        self._turn_timeout = turn_timeout
        self._interrupt_grace = interrupt_grace
        self._native_history = native_history
        self._capabilities = declared_capabilities(
            native_history=native_history,
            external_cli=getattr(preset, "resume_argv_template", None) is not None)
        self._backend_version: str | None = None
        self._installed: bool | None = None
        self.warnings: list[str] = []
        self._bindings: dict[str, AgentBinding] = {}
        self.session_ledger: dict[str, dict[str, NativeSession]] = {}
        self._catalog: dict[str, _Snapshot] = {}
        self._runtimes: dict[str, _RuntimeState] = {}
        self._launch_plans: dict[str, Any] = {}
        self._runtime_counter = 0

    # ------------------------------------------------------------------ #
    # bindings
    # ------------------------------------------------------------------ #

    def register_binding(self, binding: AgentBinding) -> None:
        self._assert_binding(binding)
        self._bindings[binding.id] = binding

    def _assert_binding(self, binding: AgentBinding) -> None:
        if binding.backend_id != self.backend_id:
            raise ValueError(f"Binding {binding.id!r} 属于 {binding.backend_id!r}，不能交给 {self.backend_id!r} 的 Driver")

    def _workspace_root_for(self, binding_id: str) -> str | None:
        binding = self._bindings.get(binding_id)
        raw = binding.runtime_config.get("workspace_root") if binding else None
        if not isinstance(raw, str) or not raw.strip():
            return None
        return os.path.realpath(os.path.abspath(os.path.expanduser(raw)))

    def _resolve_cwd(self, workspace_root: str | None) -> str:
        return os.path.abspath(workspace_root or self._default_cwd or os.getcwd())

    def _warn(self, message: str) -> None:
        self.warnings.append(message)

    def _adapter(self):
        from importlib import import_module
        return import_module(LAUNCH_ADAPTER)

    def _engine_label(self) -> str:
        return getattr(self.preset, "label", None) or "Codex"

    def _login_command(self) -> str | None:
        return getattr(self.preset, "login_command", None)

    # ------------------------------------------------------------------ #
    # process + handshake
    # ------------------------------------------------------------------ #

    async def _connect(self, spec: AcpAgentSpec | None = None) -> tuple[AcpConnection, Mapping[str, Any]]:
        actual = spec or self.agent_spec
        env_name = getattr(self.preset, "api_key_env", None)
        if env_name:
            from drivers.credentials import api_key_for
            key = api_key_for(self.backend_id)
            if key:
                actual = actual.with_env(**{env_name: key})
        connection = await AcpConnection.spawn(actual)
        try:
            try:
                result = await connection.call(P.INITIALIZE, {
                    "clientInfo": {"name": CLIENT_NAME, "title": "Kaus", "version": DRIVER_VERSION},
                    # Only the stable API: experimental fields are gated server-side by this flag.
                    "capabilities": {"experimentalApi": False},
                }, timeout=self._call_timeout)
            except AcpTransportError as exc:
                failure = await self._handshake_failure(connection, exc)
                raise failure or exc from exc
            connection.notify(P.INITIALIZED, {})
            result = result if isinstance(result, Mapping) else {}
            agent = result.get("userAgent")
            match = _VERSION_RE.search(agent) if isinstance(agent, str) else None
            self._backend_version = match.group(1) if match else self._backend_version
            self._installed = True
            return connection, result
        except BaseException:
            await connection.aclose()
            raise

    async def _handshake_failure(self, connection: AcpConnection, exc: AcpTransportError) -> AcpSpawnError | None:
        exit_code = await connection.exit_status()
        stderr = connection.stderr_text()
        if exit_code is not None and exit_code != 0:
            cause = f"进程在握手期间退出（exit {exit_code}）"
        elif exit_code is not None:
            cause = "进程没有回答 initialize 就自己退出了（exit 0）"
        elif stderr.strip():
            cause = f"进程起来了但没有回答 initialize（{self._call_timeout:g}s 超时）"
        else:
            return None
        failure = spawn_failed(self.agent_spec.command, cause=cause, stderr=stderr, environ=self.agent_spec.build_env())
        return AcpSpawnError(failure.message, failure=failure)

    async def _account(self, connection: AcpConnection) -> Literal["signed_in", "signed_out", "unknown"]:
        """``account/read`` → three-state login. Older servers without the method → unknown."""
        try:
            result = await connection.call(P.ACCOUNT_READ, {"refreshToken": False}, timeout=self._call_timeout)
        except AcpRpcError:
            return "unknown"
        except AcpTransportError:
            return "unknown"
        if not isinstance(result, Mapping):
            return "unknown"
        if result.get("account"):
            return "signed_in"
        return "signed_out" if result.get("requiresOpenaiAuth") else "unknown"

    def _auth_error(self, binding_id: str) -> AuthRequiredError:
        failure = auth_required(self._engine_label(), login_command=self._login_command())
        self._catalog[binding_id] = _Snapshot(fetched_at=_now(), auth_state="signed_out", degraded=True,
                                              diagnostics=(login_diagnostic(self._login_command()),),
                                              auth_hint=failure.hint)
        return AuthRequiredError(failure.message, failure=failure)

    def _auth_error_from_rpc(self, binding_id: str, exc: AcpRpcError) -> AuthRequiredError | None:
        if looks_like_auth_error(exc.code, exc.message, exc.data) or re.search(
                r"not (?:logged in|authenticated)|login required|unauthori[sz]ed|401", exc.message or "", re.I):
            return self._auth_error(binding_id)
        return None

    # ------------------------------------------------------------------ #
    # probe / capabilities
    # ------------------------------------------------------------------ #

    async def probe(self) -> BackendProbeResult:
        try:
            connection, _result = await self._connect()
        except AcpTransportError as exc:
            failure = getattr(exc, "failure", None)
            executable = self.agent_spec.command[0] if self.agent_spec.command else None
            resolved = shutil.which(executable, path=self.agent_spec.build_env().get("PATH")) if executable else None
            self._installed = bool(resolved and os.access(resolved, os.X_OK))
            return BackendProbeResult(
                backend_id=self.backend_id, driver_kind=self.driver_kind, state="unavailable",
                installed=self._installed, driver_version=DRIVER_VERSION,
                message=failure.describe() if isinstance(failure, FailureHint) else str(exc),
                capabilities=self._capabilities, probed_at=_now())
        except AcpRpcError as exc:
            self._installed = True
            return BackendProbeResult(
                backend_id=self.backend_id, driver_kind=self.driver_kind, state="degraded", installed=True,
                driver_version=DRIVER_VERSION, message=f"initialize 被拒：{exc}",
                capabilities=self._capabilities, probed_at=_now())
        noise = connection.protocol_noise()
        await connection.aclose()
        message = None
        state: Literal["ready", "degraded"] = "ready"
        if noise:
            state, message = "degraded", f"app-server 在 stdout 上写了 {len(noise)} 条非 JSON-RPC 内容"
        elif self._backend_version is None:
            message = "app-server 未在 initialize.userAgent 里报版本"
        return BackendProbeResult(
            backend_id=self.backend_id, driver_kind=self.driver_kind, state=state, installed=True,
            version=self._backend_version, driver_version=DRIVER_VERSION, message=message,
            capabilities=self._capabilities, probed_at=_now())

    async def get_capabilities(self) -> BackendCapabilities:
        return self._capabilities

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    # model catalog / auth / settings
    # ------------------------------------------------------------------ #

    async def get_model_catalog(self, binding: AgentBinding) -> ModelCatalog:
        self._assert_binding(binding)
        return self._catalog_from(binding, await self._snapshot(binding))

    async def runtime_model_catalog(self, runtime: RuntimeHandle, binding: AgentBinding) -> ModelCatalog:
        state = self._state(runtime)
        return self._catalog_from(binding, state.snapshot or await self._snapshot(binding))

    def _catalog_from(self, binding: AgentBinding, snapshot: _Snapshot) -> ModelCatalog:
        return ModelCatalog(
            binding_id=binding.id, mode="constrained", models=snapshot.models,
            default_model_id=binding.default_model_id or snapshot.current_model_id,
            default_provider_id=binding.default_provider_id, supports_reasoning=True,
            selection_applies=True if snapshot.models else None,
            degraded=snapshot.degraded, engine_default_available=bool(snapshot.current_model_id),
            diagnostics=snapshot.diagnostics,
        )

    async def _snapshot(self, binding: AgentBinding) -> _Snapshot:
        cached = self._catalog.get(binding.id)
        if cached is not None and cached.is_fresh(_now()):
            return cached
        snapshot = await self._probe_catalog(binding)
        self._catalog[binding.id] = snapshot
        return snapshot

    async def _probe_catalog(self, binding: AgentBinding) -> _Snapshot:
        """``model/list`` + an ephemeral ``thread/start`` in disposable native storage.

        ``thread/start.ephemeral=true`` threads are not persisted (README «Ephemeral
        threads cannot save it»); the process still writes logs and its state DB, so
        the probe runs inside :func:`drivers.discovery_scope.isolated_agent_spec`
        with the login files copied in, like the ACP catalog probe.
        """
        from drivers.discovery_scope import (
            DiscoveryCredentialsUnavailable,
            isolated_agent_spec,
            prepare_discovery_credentials,
        )
        preset_id = getattr(self.preset, "id", None)
        with isolated_agent_spec(self.agent_spec, preset_id=preset_id, workspace=self._workspace_root_for(binding.id)) as spec:
            try:
                await prepare_discovery_credentials(spec, self.agent_spec.build_env(), preset_id=preset_id)
            except DiscoveryCredentialsUnavailable as exc:
                return _Snapshot(fetched_at=_now(), degraded=True, auth_state="signed_out" if exc.signed_out else "unknown",
                                 diagnostics=(str(exc),), auth_hint=str(exc))
            return await self._probe_in_scope(spec)

    async def _probe_in_scope(self, spec: AcpAgentSpec) -> _Snapshot:
        try:
            connection, _init = await self._connect(spec)
        except (AcpTransportError, AcpRpcError) as exc:
            failure = getattr(exc, "failure", None)
            text = failure.describe() if isinstance(failure, FailureHint) else str(exc)
            return _Snapshot(fetched_at=_now(), degraded=True, diagnostics=(text,),
                             connect_hint=failure.hint if isinstance(failure, FailureHint) else None)
        try:
            auth = await self._account(connection)
            if auth == "signed_out":
                hint = login_diagnostic(self._login_command())
                return _Snapshot(fetched_at=_now(), auth_state="signed_out", degraded=True,
                                 diagnostics=(hint,), auth_hint=hint)
            models = await self._model_list(connection)
            current_model = current_effort = current_policy = None
            try:
                started = await connection.call(P.THREAD_START, {"cwd": spec.cwd, "ephemeral": True},
                                                timeout=self._call_timeout)
            except AcpRpcError as exc:
                if self._auth_error_from_rpc("probe", exc) is not None:
                    self._catalog.pop("probe", None)
                    hint = login_diagnostic(self._login_command())
                    return _Snapshot(fetched_at=_now(), auth_state="signed_out", degraded=True,
                                     diagnostics=(hint,), auth_hint=hint)
                self._warn(f"probe thread/start rejected: {exc.message}")
            else:
                if isinstance(started, Mapping):
                    current_model = started.get("model") if isinstance(started.get("model"), str) else None
                    current_effort = started.get("reasoningEffort") if isinstance(started.get("reasoningEffort"), str) else None
                    current_policy = P.policy_from_settings(started.get("approvalPolicy"), started.get("sandbox"),
                                                            started.get("approvalsReviewer"))
            if current_model and not any(m.model_id == current_model for m in models):
                models = (*models, ModelDescriptor(model_id=current_model))
            return _Snapshot(fetched_at=_now(), models=models, current_model_id=current_model,
                             current_effort=current_effort, current_policy=current_policy,
                             auth_state="signed_in" if auth == "signed_in" else "unknown",
                             degraded=not models, diagnostics=() if models else ("引擎没有报告可选模型",))
        except (AcpTransportError, AcpRpcError) as exc:
            return _Snapshot(fetched_at=_now(), degraded=True, diagnostics=(f"模型目录读取失败：{safe_exception_message(exc, environ=spec.build_env())}",))
        finally:
            await connection.aclose()

    async def _model_list(self, connection: AcpConnection) -> tuple[ModelDescriptor, ...]:
        models: list[ModelDescriptor] = []
        cursor = None
        for _page in range(10):
            params: dict[str, Any] = {"includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = await connection.call(P.MODEL_LIST, params, timeout=self._call_timeout)
            if not isinstance(result, Mapping):
                break
            for raw in result.get("data") or ():
                if not isinstance(raw, Mapping) or raw.get("hidden") or not isinstance(raw.get("id"), str):
                    continue
                efforts = tuple(
                    e["reasoningEffort"] for e in raw.get("supportedReasoningEfforts") or ()
                    if isinstance(e, Mapping) and isinstance(e.get("reasoningEffort"), str)
                )
                models.append(ModelDescriptor(model_id=raw["id"], display_name=raw.get("displayName") or None,
                                              reasoning_levels=efforts))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tuple(models)

    async def preview_model_settings(self, binding: AgentBinding, model_id: str) -> tuple[ModelCatalog, dict]:
        snapshot = await self._snapshot(binding)
        controls = self._controls(binding, snapshot, model_id=model_id)
        return self._catalog_from(binding, snapshot), controls

    async def read_engine_settings(self, binding: AgentBinding) -> EngineSettings:
        self._assert_binding(binding)
        snapshot = self._catalog.get(binding.id)
        if snapshot is None or snapshot.degraded:
            return EngineSettings(binding_id=binding.id, diagnostics=("尚未从 app-server 读到有效设置",))
        return EngineSettings(binding_id=binding.id, model_id=snapshot.current_model_id,
                              reasoning_effort=snapshot.current_effort,
                              approval_mode={"ask": "ask", "auto": "auto", "bypass": "deny"}.get(snapshot.current_policy or ""),
                              source_ref="codex app-server thread/start (ephemeral probe)")

    async def read_auth_state(self, binding: AgentBinding) -> AuthState:
        self._assert_binding(binding)
        snapshot = self._catalog.get(binding.id)
        if snapshot is None or not snapshot.is_fresh(_now()):
            try:
                snapshot = await self._snapshot(binding)
            except Exception:  # noqa: BLE001 - unknown is a renderable state
                _LOG.debug("auth probe failed (%s)", self.backend_id, exc_info=True)
                snapshot = None
        if snapshot is None or snapshot.auth_state == "unknown":
            return AuthState(state="unknown", model="own-auth", hint=snapshot.connect_hint if snapshot else None,
                             checked_at=snapshot.fetched_at if snapshot else None)
        return AuthState(state=snapshot.auth_state, model="own-auth", checked_at=snapshot.fetched_at,
                         hint=snapshot.auth_hint if snapshot.auth_state == "signed_out" else None)

    def conversation_controls(self, binding: AgentBinding) -> dict[str, Any]:
        return self._controls(binding, self._catalog.get(binding.id))

    def runtime_conversation_controls(self, runtime: RuntimeHandle, binding: AgentBinding) -> dict[str, Any]:
        state = self._state(runtime)
        return self._controls(binding, state.snapshot, model_id=state.model_id, effort=state.effort, policy=state.policy)

    def _controls(self, binding: AgentBinding, snapshot: _Snapshot | None, *, model_id: str | None = None,
                  effort: str | None = None, policy: str | None = None) -> dict[str, Any]:
        model = model_id or binding.default_model_id or (snapshot.current_model_id if snapshot else None)
        levels: tuple[str, ...] = ()
        if snapshot is not None:
            descriptor = next((m for m in snapshot.models if m.model_id == model), None)
            levels = descriptor.reasoning_levels if descriptor else ()
        binding_mode = binding.runtime_config.get("approval_mode")
        default_policy = policy or (binding_mode if binding_mode in P.APPROVAL_POLICIES else None) or (
            snapshot.current_policy if snapshot else None)
        options = P.approval_options()
        return {
            "model": True,
            "reasoning": True,
            "reasoningDefault": effort or (snapshot.current_effort if snapshot else None),
            "reasoningLevels": list(levels),
            "approvalModes": [row["id"] for row in options],
            "approvalOptions": options,
            "approvalDefault": default_policy if default_policy in P.APPROVAL_POLICIES else None,
            "configOptions": [],
            "executionModes": [],
            "executionDefault": None,
        }

    # ------------------------------------------------------------------ #
    # projection (session-delivered)
    # ------------------------------------------------------------------ #

    @property
    def _instructions_file(self) -> str | None:
        workspace = getattr(self.preset, "workspace", None)
        return getattr(workspace, "instructions_file", None)

    def workspace_conventions(self, binding: AgentBinding | None = None) -> dict[str, Any]:
        del binding
        return {"instructionsFile": self._instructions_file, "deliveryChannel": "launch"}

    def instructions_delivery_channel(self) -> str | None:
        return "launch"

    async def materialize_project_capabilities(
        self, project: Project, binding: AgentBinding, effective_capabilities: EffectiveCapabilities,
        *, dry_run: bool = True, adopt: bool = False,
    ) -> ProjectionResult:
        """Nothing is written to disk: every supported kind travels with ``thread/start``."""
        self._assert_binding(binding)
        del project, adopt
        applied: list[ProjectionEntry] = []
        unsupported: list[ProjectionEntry] = []
        broken = {(e.capability_type, e.capability_id) for e in invalid_instruction_entries(effective_capabilities)}
        for entry in effective_capabilities.entries:
            level = self._capabilities.support_for(entry.capability_type)
            if (entry.capability_type, entry.capability_id) in broken:
                unsupported.append(ProjectionEntry(capability_type=entry.capability_type, capability_id=entry.capability_id,
                                                   level=SupportLevel.UNSUPPORTED, reason="invalid_config", detail=INVALID_DETAIL))
            elif level.is_usable:
                key = "developerInstructions" if entry.capability_type == "instructions" else f"config/mcp_servers/{entry.capability_id}"
                applied.append(ProjectionEntry(capability_type=entry.capability_type, capability_id=entry.capability_id,
                                               level=level, target_ref=f"codex-app-server://{binding.id}/thread-start/{key}",
                                               action="unchanged", detail="随会话启动送达，不落盘"))
            else:
                unsupported.append(ProjectionEntry(capability_type=entry.capability_type, capability_id=entry.capability_id,
                                                   level=SupportLevel.UNSUPPORTED, reason="not_mapped"))
        warnings = tuple(effective_capabilities.warnings) + (("项目配置在会话启动或恢复时送达",) if applied else ())
        return ProjectionResult(binding_id=binding.id, applied=tuple(applied), unsupported=tuple(unsupported),
                                warnings=warnings, dry_run=dry_run)

    async def inspect_drift(self, project: Project, binding: AgentBinding,
                            effective_capabilities: EffectiveCapabilities | None = None) -> DriftReport:
        self._assert_binding(binding)
        del project, effective_capabilities
        return DriftReport(binding_id=binding.id, checked_at=_now(), in_sync=True)

    def _native_mcp_inventory(self, effective: EffectiveCapabilities) -> tuple[dict[str, Any], ...]:
        if self.preset is None:
            return ()
        from drivers.mcp_inventory import discover_mcp, preset_sources
        env = self.agent_spec.build_env()
        sources = preset_sources(self.preset.id, agent_key=self.preset.skill_scope_key or self.preset.id,
                                 agent_label=self.preset.label, project_id=effective.project_id,
                                 home=home_of(env), env=env)
        return tuple(discover_mcp(sources))

    def _workspace_in_use(self, conversation: Conversation, workspace_root: str | None) -> bool:
        for state in self._runtimes.values():
            if state.closed or state.handle.conversation_id == conversation.id:
                continue
            if same_directory(state.metadata.get("_kausWorkspaceRoot"), workspace_root):
                return True
        return False

    async def session_options_for(
        self, conversation: Conversation, effective: EffectiveCapabilities, *, workspace_root: str | None = None,
    ) -> SessionProjection | None:
        from drivers.skill_execution.common import selection_of
        root = workspace_root or self._workspace_root_for(conversation.agent_binding_id)
        metadata: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        warnings: list[str] = []
        unapplied: list[UnappliedCapability] = []
        # 2026-11-09：能力库插件先按适配器能力拆开（原生格式留给启动钩子；其余 MCP 并入 Project MCP）。
        plugin_expansion = expand_plugins(effective, adapter=self._adapter(), can_project_skills=False)
        effective = plugin_expansion.effective
        mcp = project_mcp_servers(effective)
        warnings.extend(mcp.warnings)
        if mcp.servers:
            metadata["mcpServers"] = [dict(s) for s in mcp.servers]
            summary["mcpServers"] = list(mcp.names)
        selection = selection_of(effective)
        if selection is not None:
            names = tuple(dict.fromkeys(e.capability_id for e in effective.entries
                                        if e.capability_type.startswith("skill") or e.capability_type.endswith(":skill")))
            unapplied.append(UnappliedCapability(kind=SKILLS, names=names, reason=UNSUPPORTED))
            warnings.append("app-server 传输暂不支持项目技能范围；本次按原生技能运行")
        plan = plan_launch(effective, adapter=self._adapter(), instructions_file=self._instructions_file,
                           workspace_root=root, project_id=effective.project_id,
                           native_inventory=self._native_mcp_inventory(effective),
                           workspace_in_use=self._workspace_in_use(conversation, root), plugins=plugin_expansion)
        self._launch_plans[conversation.id] = plan
        summary.update(plan.summary)
        warnings.extend(plan.warnings)
        for warning in warnings:
            self._warn(warning)
        if not metadata and not plan.summary and not unapplied:
            return None
        return SessionProjection(options=CreateSessionOptions(title=conversation.title, metadata=metadata),
                                 summary=summary, warnings=tuple(warnings), unapplied=tuple(unapplied))

    async def unapplied_project_capabilities(
        self, conversation: Conversation, effective: EffectiveCapabilities, *, workspace_root: str | None = None,
    ) -> tuple[UnappliedCapability, ...]:
        del effective, workspace_root
        plan = self._launch_plans.get(conversation.id)
        return tuple(plan.unapplied) if plan is not None else ()

    def supports_workspace_override(self) -> bool:
        return True

    async def workspace_session_projection(self, conversation, effective, *, workspace_root):
        projection = await self.session_options_for(conversation, effective, workspace_root=workspace_root)
        projection = projection or SessionProjection(options=CreateSessionOptions(title=conversation.title))
        return projection.model_copy(update={"options": projection.options.model_copy(update={"workspace_root": workspace_root})})

    # ------------------------------------------------------------------ #
    # native sessions
    # ------------------------------------------------------------------ #

    def _thread_params(self, spec: AcpAgentSpec, metadata: Mapping[str, Any], *, cwd: str,
                       model_id: str | None, effort: str | None, policy: str | None) -> dict[str, Any]:
        """``thread/start`` / ``thread/resume`` body (minus ``threadId``)."""
        raw = spec.build_env().get("CODEX_CONFIG")
        config = json.loads(raw) if raw else {}
        if not isinstance(config, dict):
            raise UnsupportedCapabilityError("原生配置覆盖必须为对象")
        params: dict[str, Any] = {"cwd": cwd}
        instructions = config.pop("developer_instructions", None)
        if isinstance(instructions, str) and instructions:
            params["developerInstructions"] = instructions
        servers = metadata.get("mcpServers")
        if isinstance(servers, Sequence) and not isinstance(servers, (str, bytes)) and servers:
            tables = dict(config.get("mcp_servers") or {})
            for name, table in P.mcp_servers_config(servers).items():
                current = tables.get(name)
                tables[name] = {**(current if isinstance(current, dict) else {}), **table, "enabled": True}
            config["mcp_servers"] = tables
        if effort:
            config["model_reasoning_effort"] = effort
        if config:
            params["config"] = config
        if model_id:
            params["model"] = model_id
        settings = P.approval_settings(policy)
        if settings is not None:
            params["approvalPolicy"], params["approvalsReviewer"], params["sandbox"] = settings
        return params

    def _selection_for(self, conversation: Conversation, binding: AgentBinding | None) -> tuple[str | None, str | None, str | None]:
        model = conversation.model_id or (binding.default_model_id if binding else None)
        effort = conversation.reasoning_mode or (binding.runtime_config.get("reasoning_effort") if binding else None)
        effort = effort if isinstance(effort, str) and effort else None
        policy = conversation.approval_mode or (binding.runtime_config.get("approval_mode") if binding else None)
        if policy is not None and P.approval_settings(policy) is None:
            raise UnsupportedCapabilityError("引擎未提供这条会话的权限选项。")
        return model, effort, policy

    async def create_native_session(self, binding: AgentBinding, options: CreateSessionOptions) -> NativeSession:
        self._assert_binding(binding)
        cwd = self._resolve_cwd(options.workspace_root or self._workspace_root_for(binding.id))
        connection, _init = await self._connect(self.agent_spec.model_copy(update={"cwd": cwd}))
        try:
            params = self._thread_params(self.agent_spec, options.metadata, cwd=cwd, model_id=options.model_id,
                                         effort=options.reasoning_mode,
                                         policy=binding.runtime_config.get("approval_mode"))
            try:
                result = await connection.call(P.THREAD_START, params, timeout=self._call_timeout)
            except AcpRpcError as exc:
                auth = self._auth_error_from_rpc(binding.id, exc)
                if auth is not None:
                    raise auth from exc
                raise
        finally:
            await connection.aclose()
        thread_id = _thread_id(result)
        session = NativeSession(native_session_id=thread_id, binding_id=binding.id, title=options.title,
                                head_id=thread_id, created_at=_now(), updated_at=_now())
        self.session_ledger.setdefault(binding.id, {})[thread_id] = session
        return session

    async def list_native_sessions(self, binding: AgentBinding) -> tuple[NativeSession, ...]:
        self._assert_binding(binding)
        return tuple(self.session_ledger.get(binding.id, {}).values())

    async def count_native_sessions(self, binding: AgentBinding) -> int | None:
        self._assert_binding(binding)
        return len(self.session_ledger.get(binding.id, {}))

    async def load_native_history(self, binding: AgentBinding, native_session_id: str) -> NativeHistory:
        self._assert_binding(binding)
        if not self._native_history:
            raise UnsupportedCapabilityError("本连接未启用原生历史读取")
        from drivers.codex.native_history import load_native_history
        return await load_native_history(self.agent_spec, binding, native_session_id)

    async def load_local_native_history(self, binding: AgentBinding, native_session_id: str) -> NativeHistory:
        return await self.load_native_history(binding, native_session_id)

    # ------------------------------------------------------------------ #
    # card runtime
    # ------------------------------------------------------------------ #

    async def start_runtime(
        self, conversation: Conversation, surface: Literal["card"], *, session_options: CreateSessionOptions | None = None,
    ) -> RuntimeHandle:
        if surface != "card":
            raise UnsupportedCapabilityError("Driver 只负责 Card Surface")
        binding = self._bindings.get(conversation.agent_binding_id)
        workspace_root = (session_options.workspace_root if session_options and session_options.workspace_root
                          else self._workspace_root_for(conversation.agent_binding_id))
        cwd = self._resolve_cwd(workspace_root)
        metadata = dict(session_options.metadata) if session_options else {}
        metadata["_kausWorkspaceRoot"] = cwd
        spec = self.agent_spec
        spec, metadata, delivery_warnings = apply_launch_plan(
            self._launch_plans.get(conversation.id), spec, metadata, adapter=self._adapter(), cwd=cwd)
        for warning in delivery_warnings:
            self._warn(warning)
        role = binding.runtime_config.get("group_execution") if binding else None
        if role:
            from drivers.group_execution import adapter_for
            group = adapter_for(getattr(self.preset, "group_isolation_adapter", None))
            spec = group.prepare(spec, role) if group is not None else spec
        spec = spec.model_copy(update={"cwd": cwd})
        model_id, effort, policy = self._selection_for(conversation, binding)
        connection, _init = await self._connect(spec)
        try:
            if await self._account(connection) == "signed_out":
                raise self._auth_error(conversation.agent_binding_id)
            params = self._thread_params(spec, metadata, cwd=cwd, model_id=model_id, effort=effort, policy=policy)
            resumed = bool(conversation.native_session_id)
            try:
                if resumed:
                    result = await connection.call(P.THREAD_RESUME, {"threadId": conversation.native_session_id, **params},
                                                   timeout=self._call_timeout)
                else:
                    result = await connection.call(P.THREAD_START, params, timeout=self._call_timeout)
            except AcpRpcError as exc:
                _LOG.warning("app-server thread start failed (%s): %s; stderr: %s", self.backend_id,
                             safe_exception_message(exc, environ=spec.build_env()),
                             safe_error_text(connection.stderr_text(), environ=spec.build_env(), limit=12000))
                auth = self._auth_error_from_rpc(conversation.agent_binding_id, exc)
                if auth is not None:
                    raise auth from exc
                raise
            thread_id = _thread_id(result) if not resumed else (conversation.native_session_id or _thread_id(result))
        except BaseException:
            await connection.aclose()
            raise
        snapshot = self._snapshot_from_thread(result, model_id)
        self._catalog[conversation.agent_binding_id] = snapshot
        self.session_ledger.setdefault(conversation.agent_binding_id, {}).setdefault(thread_id, NativeSession(
            native_session_id=thread_id, binding_id=conversation.agent_binding_id, title=conversation.title,
            head_id=thread_id, created_at=_now(), updated_at=_now()))
        self._runtime_counter += 1
        handle = RuntimeHandle(
            runtime_id=f"{self.backend_id}-runtime-{self._runtime_counter}", conversation_id=conversation.id,
            binding_id=conversation.agent_binding_id, backend_id=self.backend_id, native_session_id=thread_id,
            started_at=_now(),
            metadata={"threadId": thread_id, "resumed": resumed, "workspaceRoot": cwd if workspace_root else None,
                      "transport": "app-server", "model": snapshot.current_model_id, "effort": snapshot.current_effort,
                      "policy": snapshot.current_policy},
        )
        translator = AppServerTranslator(TranslationContext(
            project_id=conversation.project_id, conversation_id=conversation.id,
            agent_binding_id=conversation.agent_binding_id, backend_id=self.backend_id, thread_id=thread_id,
            run_token=uuid.uuid4().hex[:8], driver_version=DRIVER_VERSION, backend_version=self._backend_version))
        state = _RuntimeState(handle=handle, conversation=conversation, connection=connection, translator=translator,
                              thread_id=thread_id, spec=spec, metadata=metadata, snapshot=snapshot,
                              model_id=model_id or snapshot.current_model_id, effort=effort or snapshot.current_effort,
                              policy=policy or snapshot.current_policy)
        self._runtimes[handle.runtime_id] = state
        connection.on_notification = lambda method, params: self._on_notification(state, method, params)
        connection.on_request = lambda rpc_id, method, params: self._on_request(state, rpc_id, method, params)
        self._push(state, translator.session_resumed() if resumed else translator.session_created())
        return handle

    def _snapshot_from_thread(self, result: Any, requested_model: str | None) -> _Snapshot:
        result = result if isinstance(result, Mapping) else {}
        model = result.get("model") if isinstance(result.get("model"), str) else requested_model
        effort = result.get("reasoningEffort") if isinstance(result.get("reasoningEffort"), str) else None
        previous = None
        for cached in self._catalog.values():
            if cached.models:
                previous = cached
                break
        models = previous.models if previous else ()
        if model and not any(m.model_id == model for m in models):
            models = (*models, ModelDescriptor(model_id=model))
        return _Snapshot(fetched_at=_now(), models=models, current_model_id=model, current_effort=effort,
                         current_policy=P.policy_from_settings(result.get("approvalPolicy"), result.get("sandbox"),
                                                               result.get("approvalsReviewer")),
                         auth_state="signed_in")

    def adopt_conversation(self, runtime: RuntimeHandle, conversation: Conversation) -> None:
        state = self._runtimes.get(runtime.runtime_id)
        if state is not None and state.conversation.id == conversation.id:
            state.conversation = conversation

    def runtime_reusable(self, runtime: RuntimeHandle) -> bool:
        state = self._runtimes.get(runtime.runtime_id)
        return bool(state is not None and not state.closed and state.connection.alive)

    async def events(self, runtime: RuntimeHandle) -> AsyncIterator[AgentEventEnvelope]:
        state = self._state(runtime)
        while True:
            item = await state.queue.get()
            if item is _SENTINEL:
                return
            yield item

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

    def _push(self, state: _RuntimeState, envelopes: Sequence[AgentEventEnvelope]) -> None:
        for envelope in envelopes:
            state.queue.put_nowait(envelope)

    def _state(self, runtime: RuntimeHandle) -> _RuntimeState:
        state = self._runtimes.get(runtime.runtime_id)
        if state is None or state.closed:
            raise RuntimeNotFoundError(f"未知或已停止的 runtime：{runtime.runtime_id!r}")
        return state

    # --- turns -------------------------------------------------------- #

    @staticmethod
    def _turn_running(state: _RuntimeState) -> bool:
        """A turn is live until its terminal event; the task may still be cleaning up after that."""
        task = state.prompt_task
        return task is not None and not task.done() and state.translator.run_active

    async def send_message(self, runtime: RuntimeHandle, content: MessageInput) -> None:
        state = self._state(runtime)
        async with state.control_lock:
            if self._turn_running(state):
                raise TurnAlreadyRunningError("上一轮尚未结束；先 interrupt 或等待终态", failure=turn_already_running_hint())
            state.interrupt_requested = False
            state.turn_id = None
            state.turn_done = asyncio.Event()
            self._push(state, state.translator.begin_run())
            state.prompt_task = asyncio.create_task(self._turn(state, content))

    def _turn_params(self, state: _RuntimeState, content: MessageInput) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": state.thread_id, "input": P.user_input(content)}
        if state.model_id:
            params["model"] = state.model_id
        if state.effort:
            params["effort"] = state.effort
        if state.policy_dirty:
            settings = P.approval_settings(state.policy)
            if settings is not None:
                approval_policy, reviewer, sandbox = settings
                params["approvalPolicy"] = approval_policy
                params["approvalsReviewer"] = reviewer
                # ``TurnStartParams.sandbox_policy`` is the tagged ``SandboxPolicy`` enum
                # (``v2/permissions.rs``), not the ``SandboxMode`` string of thread/start.
                params["sandboxPolicy"] = {"read-only": {"type": "readOnly"},
                                           "workspace-write": {"type": "workspaceWrite"},
                                           "danger-full-access": {"type": "dangerFullAccess"}}[sandbox]
        return params

    async def _turn(self, state: _RuntimeState, content: MessageInput) -> None:
        if state.interrupt_requested:
            self._push(state, state.translator.local_interrupt())
            return
        try:
            result = await state.connection.call(P.TURN_START, self._turn_params(state, content), timeout=self._call_timeout)
        except AcpRpcError as exc:
            self._cancel_pending_interactions(state)
            auth = self._auth_error_from_rpc(state.handle.binding_id, exc)
            code = auth.failure.code if auth is not None and auth.failure else exc.code
            message = auth.failure.message if auth is not None and auth.failure else exc.message
            self._push(state, state.translator.on_error(code, message, exc.data))
            return
        except AcpTransportError as exc:
            self._cancel_pending_interactions(state)
            self._push(state, state.translator.on_error("app_server.transport", str(exc)))
            return
        except asyncio.CancelledError:
            if state.interrupt_requested and not state.closed:
                self._push(state, state.translator.local_interrupt())
            raise
        state.policy_dirty = False
        turn = result.get("turn") if isinstance(result, Mapping) else None
        turn_id = turn.get("id") if isinstance(turn, Mapping) else None
        if isinstance(turn_id, str) and turn_id:
            state.turn_id = turn_id
            state.translator.bind_turn(turn_id)
        if state.interrupt_requested:
            await self._send_interrupt(state)
        try:
            await asyncio.wait_for(state.turn_done.wait(), self._turn_timeout)
        except asyncio.TimeoutError:
            self._cancel_pending_interactions(state)
            self._push(state, state.translator.on_error("app_server.turn_timeout", "回合在约定时间内没有结束"))
        except asyncio.CancelledError:
            if state.interrupt_requested and not state.closed:
                self._push(state, state.translator.on_turn_completed({"status": P.TURN_INTERRUPTED}, interrupted=True))
            raise
        finally:
            self._cancel_pending_interactions(state)

    async def interrupt(self, runtime: RuntimeHandle) -> None:
        state = self._state(runtime)
        state.interrupt_requested = True
        self._cancel_pending_interactions(state)
        task = state.prompt_task
        if task is None or task.done():
            return
        if state.turn_id is not None:
            await self._send_interrupt(state)
        # Otherwise ``_turn`` sends the interrupt as soon as ``turn/start`` answers.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), self._interrupt_grace)

    async def _send_interrupt(self, state: _RuntimeState) -> None:
        if state.turn_id is None or state.closed:
            return
        try:
            await state.connection.call(P.TURN_INTERRUPT, {"threadId": state.thread_id, "turnId": state.turn_id},
                                        timeout=self._call_timeout)
        except (AcpRpcError, AcpTransportError) as exc:
            self._warn(f"turn/interrupt failed: {safe_exception_message(exc, environ=state.spec.build_env())}")

    # --- settings on a live runtime ----------------------------------- #

    async def set_conversation_model(self, runtime: RuntimeHandle, model_id: str) -> None:
        state = self._state(runtime)
        await self.set_conversation_selection(runtime, state.conversation.evolve(model_id=model_id))

    async def set_conversation_selection(self, runtime: RuntimeHandle, conversation: Conversation) -> Conversation:
        state = self._state(runtime)
        if self._turn_running(state):
            raise TurnAlreadyRunningError("上一轮尚未结束，请结束后再切换设置", failure=turn_already_running_hint())
        if conversation.model_id and state.snapshot and state.snapshot.models and not any(
                m.model_id == conversation.model_id for m in state.snapshot.models):
            raise ModelRejectedError("引擎不提供这个模型", reason=f"{conversation.model_id} 不在 model/list 里")
        if conversation.execution_mode:
            raise UnsupportedCapabilityError("引擎未提供这个运行模式。")
        if conversation.approval_mode is not None and P.approval_settings(conversation.approval_mode) is None:
            raise UnsupportedCapabilityError("引擎未提供这条会话的权限选项。")
        if conversation.model_id:
            state.model_id = conversation.model_id
        if conversation.reasoning_mode:
            state.effort = conversation.reasoning_mode
        if conversation.approval_mode is not None and conversation.approval_mode != state.policy:
            state.policy = conversation.approval_mode
            state.policy_dirty = True
        state.conversation = conversation
        return conversation

    # --- inbound ------------------------------------------------------ #

    def _on_notification(self, state: _RuntimeState, method: str, params: dict) -> None:
        if state.closed:
            return
        if method == P.NOTE_ACCOUNT_UPDATED:
            return
        turn_before = state.translator.run_active
        self._push(state, state.translator.on_notification(method, params or {}))
        if turn_before and not state.translator.run_active:
            state.turn_done.set()

    async def _on_request(self, state: _RuntimeState, rpc_id: Any, method: str, params: dict) -> None:
        params = params or {}
        if self._interactions_stopped(state):
            self._reject_request(state, rpc_id, method)
            return
        if method in (P.REQ_COMMAND_APPROVAL, P.REQ_FILE_CHANGE_APPROVAL, P.REQ_PERMISSIONS_APPROVAL):
            events = state.translator.on_approval_request(rpc_id, method, params)
            if not events:
                state.connection.respond(rpc_id, error={"code": -32602, "message": "unknown threadId"})
                return
            self._push(state, events)
            return
        if method == P.REQ_USER_INPUT:
            events = state.translator.on_user_input_request(rpc_id, params)
            if not events:
                state.connection.respond(rpc_id, {"answers": {}})
                return
            self._push(state, events)
            return
        if method == P.REQ_MCP_ELICITATION:
            # No form UI on this transport yet: decline so the MCP call fails closed.
            state.connection.respond(rpc_id, {"action": "decline", "content": None})
            return
        state.connection.respond(rpc_id, error={"code": -32601, "message": f"client does not implement {method}"})

    @staticmethod
    def _interactions_stopped(state: _RuntimeState) -> bool:
        return state.closed or state.interrupt_requested or bool(state.prompt_task and state.prompt_task.done())

    def _reject_request(self, state: _RuntimeState, rpc_id: Any, method: str) -> None:
        with contextlib.suppress(AcpTransportError):
            if method in (P.REQ_COMMAND_APPROVAL, P.REQ_FILE_CHANGE_APPROVAL):
                state.connection.respond(rpc_id, {"decision": P.DECISION_CANCEL})
            elif method == P.REQ_PERMISSIONS_APPROVAL:
                state.connection.respond(rpc_id, {"permissions": {}, "scope": "turn"})
            elif method == P.REQ_USER_INPUT:
                state.connection.respond(rpc_id, {"answers": {}})
            elif method == P.REQ_MCP_ELICITATION:
                state.connection.respond(rpc_id, {"action": "cancel", "content": None})
            else:
                state.connection.respond(rpc_id, error={"code": -32001, "message": "turn is not accepting requests"})

    def _cancel_pending_interactions(self, state: _RuntimeState) -> None:
        for pending in state.translator.pending_approvals():
            state.translator.take_approval(pending.request_id)
            with contextlib.suppress(AcpTransportError):
                self._reject_request(state, pending.rpc_id, pending.method)
            self._push(state, state.translator.on_permission_resolved(pending.request_id, "cancelled"))
        for batch in state.translator.pending_inputs():
            first = next(iter(batch.questions))
            events, _batch, _cancelled = state.translator.answer_question(first, option_id=None, text=None, cancelled=True)
            with contextlib.suppress(AcpTransportError):
                state.connection.respond(batch.rpc_id, {"answers": {}})
            self._push(state, events)

    async def resolve_interaction(self, runtime: RuntimeHandle, interaction_id: str, response: InteractionResponse) -> None:
        state = self._state(runtime)
        if self._interactions_stopped(state):
            self._cancel_pending_interactions(state)
            raise InteractionNotFoundError("这轮对话已结束，交互请求已取消")
        if response.kind == "permission":
            pending = state.translator.pending_approval(interaction_id)
            if pending is None:
                raise InteractionNotFoundError(f"没有待解决的权限请求：{interaction_id!r}")
            if not response.cancelled:
                chosen = _resolve_option(pending, response.option_id)
                if chosen is None:
                    raise ValueError("请选择引擎提供的权限选项")
                response = response.model_copy(update={"option_id": chosen})
            state.translator.take_approval(interaction_id)
            decision = "cancelled" if response.cancelled else str(response.option_id)
            state.connection.respond(pending.rpc_id, self._approval_reply(pending, response))
            self._push(state, state.translator.on_permission_resolved(interaction_id, decision))
            return
        if response.kind == "question":
            if state.translator.pending_input(interaction_id) is None:
                raise InteractionNotFoundError(f"没有待解决的提问：{interaction_id!r}")
            events, batch, cancelled = state.translator.answer_question(
                interaction_id, option_id=response.option_id, text=response.text, cancelled=response.cancelled)
            if cancelled and batch is not None:
                state.connection.respond(batch.rpc_id, {"answers": {}})
            elif batch is not None:
                state.connection.respond(batch.rpc_id, batch.response())
            self._push(state, events)
            return
        raise UnsupportedCapabilityError(f"app-server 没有 {response.kind!r} 这种会话内交互")

    @staticmethod
    def _approval_reply(pending: PendingApproval, response: InteractionResponse) -> dict[str, Any]:
        if pending.method == P.REQ_PERMISSIONS_APPROVAL:
            if response.cancelled or response.option_id == P.DECISION_DECLINE:
                return {"permissions": {}, "scope": "turn"}
            granted = pending.permissions if isinstance(pending.permissions, Mapping) else {}
            return {"permissions": dict(granted), "scope": "session" if response.option_id == "grantForSession" else "turn"}
        return {"decision": P.DECISION_CANCEL if response.cancelled else response.option_id}

    # ------------------------------------------------------------------ #
    # external terminal
    # ------------------------------------------------------------------ #

    def validate_external_session(self, conversation: Conversation) -> None:
        for state in self._runtimes.values():
            if state.handle.conversation_id == conversation.id and self._turn_running(state):
                raise TurnAlreadyRunningError("请等待当前回复结束，再打开终端", failure=turn_already_running_hint())

    async def build_external_cli_launch(self, conversation: Conversation) -> CliLaunchSpec:
        self.validate_external_session(conversation)
        preset = self.preset
        if preset is None or preset.resume_argv_template is None:
            raise UnsupportedCapabilityError("此引擎暂不支持终端续接")
        if not conversation.native_session_id:
            raise UnsupportedCapabilityError("会话尚未就绪，请先在 Kaus 中开始对话")
        env = self.agent_spec.build_env()
        executable = self.agent_spec.command[0] if self.agent_spec.command else None
        resolved = shutil.which(executable, path=env.get("PATH")) if executable and not os.path.isabs(executable) else executable
        if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
            raise UnsupportedCapabilityError("原生终端命令尚未安装，当前会话可继续在界面中使用")
        template = preset.resume_argv(conversation.native_session_id) or ()
        command: tuple[str, ...] = (resolved, *preset.terminal_args, *template[1:])
        external = getattr(self._adapter(), "external_cli_command", None)
        if external is not None:
            command = tuple(external(None, command, conversation=conversation,
                                     binding=self._bindings.get(conversation.agent_binding_id), spec=self.agent_spec))
        from drivers.terminals.environment import selected_environment
        config_root = env.get(preset.config_root_env) if preset.config_root_env else None
        return CliLaunchSpec(
            command=command,
            private_env=selected_environment(self.agent_spec, preset.env_keys_hint),
            terminal_integration=preset.id,
            config_directory=((preset.config_root_env, str(Path(config_root or preset.config_root_default).expanduser().resolve()))
                              if preset.config_root_env and preset.config_root_default else None),
            path_prefix=tuple(part for part in env.get("PATH", os.defpath).split(os.pathsep) if part and os.path.isabs(part)),
            cwd=self._workspace_root_for(conversation.agent_binding_id) or self._default_cwd,
            env_passthrough=preset.env_keys_hint,
            title=conversation.title,
            resume=True,
            workspace_trust=(preset.workspace_trust, config_root) if preset.workspace_trust else None,
            draft_flag=preset.draft_prompt_flag,
            status_hooks=preset.status_hooks,
        )


def _resolve_option(pending: PendingApproval, option_id: str | None) -> str | None:
    """Exact option id, or the ACP-vocabulary alias a card may send (``allow`` / ``reject``)."""
    if option_id in pending.option_ids:
        return option_id
    if not isinstance(option_id, str):
        return None
    alias = {"allow": P.DECISION_ACCEPT, "allow_once": P.DECISION_ACCEPT, "allow_always": P.DECISION_ACCEPT_FOR_SESSION,
             "reject": P.DECISION_DECLINE, "deny": P.DECISION_DECLINE, "reject_once": P.DECISION_DECLINE,
             "reject_always": P.DECISION_CANCEL}.get(option_id)
    if alias in pending.option_ids:
        return alias
    if pending.method == P.REQ_PERMISSIONS_APPROVAL and option_id.startswith("allow") and "grant" in pending.option_ids:
        return "grant"
    return None


def _thread_id(result: Any) -> str:
    thread = result.get("thread") if isinstance(result, Mapping) else None
    thread_id = thread.get("id") if isinstance(thread, Mapping) else None
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    raise AcpTransportError(f"thread/start 的结果里没有 thread.id：{result!r}")


# --------------------------------------------------------------------------- #
# assembly (called by ``session_bootstrap.build_driver`` through ``AcpPreset.transports``)
# --------------------------------------------------------------------------- #


def build_driver(spec: Any, *, preset: Any, process_env: Mapping[str, str], native_cli_path: str | None = None,
                 managed_root: Any = None) -> CodexAppServerDriver:
    """One ``<codex> app-server`` process spec from the connection's assembly spec.

    The native executable comes, in order, from the connection's selected CLI path,
    ``CODEX_PATH`` in the process environment, or ``codex`` on ``PATH``. A missing
    binary is not an assembly error: ``probe`` reports it as ``unavailable`` with
    the spawn failure, the same way the ACP path does.
    """
    del managed_root
    env = dict(process_env)
    chosen = native_cli_path or env.get("CODEX_PATH") or os.environ.get("CODEX_PATH")
    if chosen:
        from drivers.acp.native_cli import validated_cli_path
        chosen = validated_cli_path(chosen)
        env["PATH"] = str(Path(chosen).parent) + os.pathsep + env.get("PATH", os.environ.get("PATH", ""))
    executable = chosen or getattr(preset, "native_executable", None) or getattr(preset, "executable", None) or "codex"
    env["CODEX_PATH"] = chosen or (shutil.which(executable, path=env.get("PATH", os.environ.get("PATH"))) or executable)
    key = spec.backend_id.split(":", 1)[1]
    return CodexAppServerDriver(
        AcpAgentSpec(command=(executable, "app-server"), cwd=spec.cwd, env=env),
        backend_key=key, preset=preset,
    )


__all__ = ["CodexAppServerDriver", "DRIVER_VERSION", "build_driver"]
