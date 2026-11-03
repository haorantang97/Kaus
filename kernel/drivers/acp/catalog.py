"""Model catalog discovery and login-state reporting (AD-157 / AD-159).

One disposable ``session/new`` answers three questions at once: which models
the engine offers, which one it is using, and whether it lets us open a
session at all. The snapshot is cached per Binding for
:data:`~drivers.acp.runtime_state.CATALOG_TTL_SECONDS` and invalidated by
external configuration changes (``_config_stamp``); a real runtime session
overwrites it with fresher data (``_remember_models``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app.projects.models import AgentBinding
from drivers.acp.capabilities import current_model_from_session_result, models_from_session_result
from drivers.acp.failure_hints import (
    auth_methods_from_error,
    auth_required,
    login_diagnostic,
    looks_like_auth_error,
    looks_like_missing_credentials,
)
from drivers.acp.client import AcpConnection, AcpRpcError, AcpTransportError
from drivers.base import (
    AuthRequiredError,
    AuthState,
    CreateSessionOptions,
    EngineSettings,
    ModelCatalog,
    ModelDescriptor,
    FailureHint,
    RuntimeHandle,
    unknown_auth_state,
)
from drivers.discovery_scope import (
    DiscoveryCredentialsUnavailable,
    configuration_paths,
    isolated_agent_spec,
    prepare_discovery_credentials,
)
from drivers.model_discovery_access import connection_configured, offered_models
from drivers.acp.runtime_state import _CatalogSnapshot, _now


AUTH_STATUS_WAIT_SECONDS = 6.0


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


def _mode_id_of(mode: Any) -> str | None:
    """``availableModes`` 的一项 → modeId（认不出来就 ``None``）。"""
    if isinstance(mode, Mapping):
        mode_id = mode.get("id") or mode.get("modeId")
    else:
        mode_id = mode
    return mode_id if isinstance(mode_id, str) and mode_id else None


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


class AcpCatalogMixin:
    """Catalog probing and auth-state cache; composed into :class:`~drivers.acp.driver.AcpDriver`."""

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

    def _selection_applies(self, snapshot) -> bool | None:
        """Whether a Kaus model selection reaches this engine at session start.

        Decided by what the live session exposes, not by the preset alone: a
        model config option, or a ``session/set_model`` transport with reported
        models. ``None`` when there is no session evidence (no snapshot, or a
        degraded probe).
        """
        if snapshot is None:
            return None
        if snapshot.model_option_id:
            return True
        if snapshot.degraded:
            return None
        return self.quirks.model_switch == "set_model" and bool(snapshot.models)

    def _model_catalog_from_snapshot(self, binding, snapshot, capabilities) -> ModelCatalog:
        applies = self._selection_applies(snapshot)
        return ModelCatalog(
            binding_id=binding.id,
            mode=capabilities.models.mode,
            models=snapshot.models,
            # Binding 自己写了默认模型就以它为准；没写才用引擎此刻在用的那个。
            # 引擎选不了模型时，Binding 默认到不了引擎，不能当成目录默认。
            default_model_id=(binding.default_model_id if applies is not False else None)
            or snapshot.current_model_id,
            selection_applies=applies,
            default_provider_id=binding.default_provider_id,
            supports_reasoning=bool(capabilities.models.reasoning) or bool(snapshot.thought_option_id and snapshot.thought_levels),
            degraded=snapshot.degraded,
            engine_default_available=snapshot.engine_default_available,
            diagnostics=snapshot.diagnostics,
        )

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
                logging.getLogger(__name__).debug(
                    "Catalog probe failed while reading auth state (%s)", self.backend_id, exc_info=True)
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
