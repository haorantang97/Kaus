"""Conversation settings: model, reasoning, permission, execution mode and catalog.

``PATCH /conversations/{id}`` validates a snapshot against what the Driver
exposes for this Binding (catalog, conversation controls, model-dependent
preview) and pushes it to the live runtime when one exists; the catalog and
effective-settings reads use the same Driver surface.
"""

from __future__ import annotations

import logging
from typing import Any

from app.api import proxy_actions
from app.api.session_views import (
    conversation_to_wire,
    effective_settings_to_wire,
    model_catalog_to_wire,
)
from app.api.conversation_settings import conversation_controls_of, scoped_model_catalog
from app.api.views import backend_to_wire, normalize_backend_key_id
from app.conversations.configuration import validate_config_values
from app.conversations.models import Conversation
from app.projects.binding_snapshot import register_binding_snapshot
from drivers.base import (
    DriverNotRegisteredError,
    ModelRejectedError,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
)
from drivers.model_selection import select_model
from app.api.session_routes.common import (
    PatchConversationBody,
    RouteSpec,
    SessionApiError,
    _not_found,
)
from app.api.session_routes.context import SessionRouteContext


def build_settings_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Settings patch, model catalog and effective-settings endpoints."""
    from fastapi import Body, Query

    host = ctx.host
    repositories = ctx.repositories
    registry = ctx.registry

    @ctx.endpoint
    async def patch_conversation(
        conversation_id: str, body: PatchConversationBody = Body(...)
    ) -> Any:
        if body.proxy is not None and (body.title is not None or body.execution_mode or body.config_options):
            # Proxied changes are limited to what is recorded in the timeline.
            raise SessionApiError(400, "proxy_setting_unsupported", "主 Agent 只能修改模型、推理强度和权限。")
        if body.model_id or body.reasoning_mode or body.approval_mode or body.execution_mode or body.config_options:
            try:
                async with host.conversation_control(conversation_id):
                    return await _patch_conversation_locked(conversation_id, body)
            except TurnAlreadyRunningError as exc:
                raise SessionApiError(409, "turn_already_running", "本轮结束后可修改设置。") from exc
        return await _patch_conversation_locked(conversation_id, body)

    async def _patch_conversation_locked(conversation_id: str, body: PatchConversationBody) -> Any:
        """会话级改模型 / 推理强度（批次十六第 4 件，落实 AD-114 的后半句）。

        在此之前，界面上的模型下拉改的是 **Binding 的默认模型**——也就是「改一条
        会话的模型」会顺带改掉这个项目下所有新会话的默认值，所以前端得先弹一次
        确认。这个端点给出会话级的那条路。

        三条口径：

        1. **快照即快照（AD-12）**：写的是这条 Conversation 从此刻起用什么，
           不回溯已经发生的回合（那由原生历史回答，D-16），也不动 Binding。
        2. **引擎认不认要先问**：``models.conversation_scoped`` 不是「支持」的
           引擎回 501 ``conversation_model_unsupported``——静默接受一个引擎不会
           照做的快照，等于在界面上撒谎——真机上就有引擎是这一档：它发起回合的
           请求里根本没有模型字段。
        3. **校验按目录来，目录为空就不校验**：模型必须在该 Binding 的 Catalog 里，
           推理强度必须在该模型的 ``reasoning_levels`` 里。目录是空的（引擎没报
           可用模型，批次十三改判规则 4b）时不拦——那时我们并不知道什么是合法的，
           拦下去只会把用户堵死。
        """
        conversation = await ctx.load_conversation(conversation_id)
        model_id = (body.model_id or "").strip() or None
        reasoning_mode = (body.reasoning_mode or "").strip() or None
        config_options = body.config_options or {}
        if config_options and (body.model_id or body.reasoning_mode or body.approval_mode or body.execution_mode):
            raise SessionApiError(400, "settings_separate", "请分别修改扩展设置和其他设置。")
        if len(config_options) > 1:
            raise SessionApiError(400, "settings_separate", "请逐项修改扩展设置。")
        approval_mode = (body.approval_mode or "").strip() or None
        execution_mode = (body.execution_mode or "").strip() or None
        if execution_mode and (model_id or reasoning_mode or approval_mode):
            raise SessionApiError(400, "mixed_settings_patch", "运行模式与其他设置请分别修改。")
        if approval_mode and (model_id or reasoning_mode):
            raise SessionApiError(400, "mixed_settings_patch", "权限与模型设置请分别修改。")
        if model_id is None and reasoning_mode is None and approval_mode is None and execution_mode is None and not config_options and body.title is None:
            raise SessionApiError(
                400,
                "empty_patch",
                "请求里没有任何要改的项（title / modelId / reasoningMode 至少给一个）",
            )

        # 批次五十二第 5 件：改标题**先落，且不过引擎那一关**。
        #
        # 下面那几步（取 Binding → 取 Driver → 问能力表）是给「会话级模型」准备的，
        # 而给一条会话改个名字与引擎毫无关系——把它塞在那后面，会让一台不支持会话级
        # 模型的引擎上「改名」吃一个 501 `conversation_model_unsupported`。
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise SessionApiError(
                    400,
                    "empty_title",
                    "会话标题不能为空（清空标题没有意义：列表里那一行总要有字）",
                    detail={"conversationId": conversation.id},
                )
            conversation = await repositories.conversations.save(
                conversation.evolve(title=title)
            )
            # 活跃 runtime 手上那份也换一下（同下面模型快照那一步的理由）。
            host.update_conversation(conversation)
            if model_id is None and reasoning_mode is None and approval_mode is None and execution_mode is None and not config_options:
                return {
                    "conversation": conversation_to_wire(
                        conversation, run_state=await host.run_state_of(conversation)
                    ),
                    # 标题不下发给引擎，所以这一格恒为 false（它说的是模型那件事）。
                    "appliedToRuntime": False,
                }
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            raise _not_found(
                "binding_not_found",
                f"Conversation 指向的 Agent Binding 不存在：{conversation.agent_binding_id}",
            )
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError as exc:
            raise ctx.driver_not_registered(binding.backend_id) from exc
        await register_binding_snapshot(driver, binding, repositories.projects)

        def _unsupported_model_switch(scoped: str | None = None) -> SessionApiError:
            return SessionApiError(
                501,
                "conversation_model_unsupported",
                "这台引擎不支持按会话指定模型或推理强度：它只按 Binding 的默认值跑。",
                detail={
                    "backendId": binding.backend_id,
                    "bindingId": binding.id,
                    "conversationScoped": scoped,
                    "hint": "改这条 Binding 的默认模型，或新建一条会话",
                },
            )

        capabilities = await driver.get_capabilities()
        provider_id: str | None = None
        live_handle = host.handle_for(conversation.id)
        try:
            catalog = await scoped_model_catalog(driver, binding, live_handle)
        except UnsupportedCapabilityError:
            catalog = None
        live_handle = host.handle_for(conversation.id)
        controls = conversation_controls_of(driver, binding, live_handle, default={})
        preview = getattr(driver, "preview_model_settings", None)
        preview_valid = not model_id or not catalog or not catalog.models or any(item.model_id == model_id for item in catalog.models)
        if live_handle is None and callable(preview) and (model_id or conversation.model_id) and preview_valid:
            catalog, controls = await preview(binding, model_id or conversation.model_id)
        if model_id and not controls.get("model", capabilities.models.conversation_scoped.is_supported):
            raise _unsupported_model_switch(capabilities.models.conversation_scoped.value)
        try:
            validate_config_values(config_options, controls.get("configOptions", []))
        except ValueError as exc:
            raise SessionApiError(501, "conversation_config_unsupported", str(exc)) from exc
        if reasoning_mode and not controls.get("reasoning", False):
            raise SessionApiError(501, "conversation_reasoning_unsupported", "当前引擎不支持会话推理设置。")
        if approval_mode and approval_mode not in controls.get("approvalModes", []):
            raise SessionApiError(501, "conversation_approval_unsupported", "当前引擎未提供这个会话权限选项。")
        if execution_mode and execution_mode not in [option['id'] for option in controls.get('executionModes', [])]:
            raise SessionApiError(501, "conversation_execution_unsupported", "当前引擎未提供这个运行模式。")
        if catalog is not None and catalog.models:
            target_model = model_id or conversation.model_id or catalog.default_model_id
            try:
                descriptor = select_model(catalog, target_model, body.provider_id if model_id else conversation.provider_id) if target_model else None
            except ValueError as exc:
                if model_id is not None:
                    raise SessionApiError(400, "model_not_in_catalog", str(exc), detail={
                        "modelId": model_id,
                        "available": [model.model_id for model in catalog.models if model.availability != "unavailable"],
                    }) from exc
                descriptor = None
            if descriptor is not None:
                provider_id = descriptor.provider_id
                if (
                    (reasoning_mode or (conversation.reasoning_mode if model_id else None)) is not None
                    and descriptor.reasoning_levels
                    and (reasoning_mode or conversation.reasoning_mode) not in descriptor.reasoning_levels
                ):
                    raise SessionApiError(
                        400,
                        "reasoning_mode_not_supported",
                        f"模型 {descriptor.model_id!r} 不支持当前推理强度，请先选择兼容的档位。",
                        detail={
                            "modelId": descriptor.model_id,
                            "available": list(descriptor.reasoning_levels),
                        },
                    )

        # 批次二十六第 5 件⑤：快照之外，还得**真的告诉引擎**。
        #
        # 此前这条端点只写 Kaus 自己的快照：能力表说这台引擎支持会话级模型，
        # 界面于是给了下拉，可 agent 那边一无所知，下一轮仍按它自己的默认模型跑
        # ——「改了没生效」正是这么来的。有下发面（``set_conversation_model``）
        # 且此刻有活跃 Runtime 时就发一次；发不出去就**不写快照**，让 400 与事实
        # 一致：这次没改成。
        #
        # 没有活跃 Runtime 时只存快照，不算失败——下一次 ``session/new`` 会带上
        # 它。这不是偷懒：一条没起来的会话本来就没有「当前模型」可改。
        updated = conversation.snapshot_model(
            model_id=model_id,
            provider_id=provider_id if model_id is not None else None,
            reasoning_mode=reasoning_mode,
        )
        if model_id is not None:
            updated = updated.evolve(provider_id=provider_id)
        if config_options:
            updated = updated.evolve(config_options={**conversation.config_options, **config_options})
        if approval_mode:
            updated = updated.evolve(approval_mode=approval_mode)
        if execution_mode:
            updated = updated.evolve(execution_mode=execution_mode)
        if conversation.preferred_surface != "card":
            raise SessionApiError(409, "external_settings", "请先返回站内对话再修改设置。")
        applied_to_runtime = False
        apply_model = getattr(driver, "set_conversation_model", None)
        apply_selection = getattr(driver, "set_conversation_selection", None)
        handle = host.handle_for(conversation.id)
        if handle is not None and (callable(apply_selection) or (model_id is not None and callable(apply_model))):
            try:
                if callable(apply_selection):
                    applied_selection = await apply_selection(handle, updated)
                    if isinstance(applied_selection, Conversation):
                        updated = updated.evolve(reasoning_mode=applied_selection.reasoning_mode, config_options=applied_selection.config_options)
                else:
                    await apply_model(handle, model_id)
            except TurnAlreadyRunningError as exc:
                raise SessionApiError(409, "turn_already_running", "上一轮尚未结束，请结束后再切换模型。") from exc
            except UnsupportedCapabilityError as exc:
                # 能力表说支持、下发面说不支持：以实测为准，但这不是 500。
                if approval_mode or reasoning_mode or execution_mode or config_options:
                    raise SessionApiError(501, "conversation_setting_unsupported", str(exc)) from None
                raise _unsupported_model_switch() from None
            except ModelRejectedError as exc:
                raise SessionApiError(
                    400,
                    "model_rejected",
                    f"设置未生效：{exc.reason or exc}",
                    detail={
                        "modelId": model_id,
                        "bindingId": binding.id,
                        "backendId": binding.backend_id,
                        # agent 自己那句话，原样给——用户据它换一个模型。
                        "agentReason": exc.reason,
                    },
                ) from exc
            applied_to_runtime = True

        saved = await repositories.conversations.save(updated)
        # 活跃 runtime 手上那份也要换，否则这一轮还按旧快照发（AD-12 的反面）。
        host.update_conversation(saved)
        payload: dict[str, Any] = {
            "conversation": conversation_to_wire(
                saved, run_state=await host.run_state_of(saved)
            ),
            # 这次改动到底有没有下发给正在跑的那条 Runtime。false = 只存了快照，
            # 下一次 `session/new` 之后才生效——界面据此决定要不要说一句。
            "appliedToRuntime": applied_to_runtime,
        }
        if body.proxy is not None:
            changes: list[tuple[Any, str, str | None]] = []
            if model_id is not None:
                model_label = next((m.display_name for m in (catalog.models if catalog else ())
                                    if m.model_id == saved.model_id and m.display_name), None)
                changes.append(("model", saved.model_id or model_id, model_label))
            if reasoning_mode is not None:
                changes.append(("reasoning", saved.reasoning_mode or reasoning_mode, None))
            if approval_mode is not None:
                approval_label = next((str(o.get("name")) for o in controls.get("approvalOptions") or ()
                                       if isinstance(o, dict) and o.get("id") == approval_mode and o.get("name")), None)
                changes.append(("approval", approval_mode, approval_label))
            recorded = [await ctx.record_proxy(saved, proxy_actions.setting_event(
                setting, value, label, actor=body.proxy)) for setting, value, label in changes]
            payload["recorded"] = all(recorded)
        return payload

    @ctx.endpoint
    async def get_models(backend_id: str, binding: str | None = Query(None),
                         conversation_id: str | None = Query(None, alias="conversationId")) -> Any:
        normalized = normalize_backend_key_id(backend_id)
        if not binding:
            raise SessionApiError(
                400,
                "binding_required",
                "Model Catalog 按 Binding 取（N §10.1：每个 Binding 一份，互不污染）——"
                "请带上 ?binding=<binding_id>",
            )
        record = await repositories.bindings.get(binding)
        if record is None:
            raise _not_found("binding_not_found", f"未知 Binding：{binding}")
        conversation = await ctx.load_conversation(conversation_id) if conversation_id else None
        if conversation is not None and conversation.agent_binding_id != record.id:
            raise SessionApiError(400, "binding_mismatch", "对话不属于这个引擎配置。")
        if record.backend_id != normalized:
            raise SessionApiError(
                400,
                "binding_backend_mismatch",
                f"Binding {record.id} 属于 {record.backend_id}，不是 {normalized}",
            )
        try:
            driver = registry.get(normalized)
        except DriverNotRegisteredError as exc:
            raise ctx.driver_not_registered(normalized) from exc
        await register_binding_snapshot(driver, record, repositories.projects)
        try:
            handle = host.handle_for(conversation_id) if conversation_id else None
            catalog = await scoped_model_catalog(driver, record, handle)
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        return model_catalog_to_wire(catalog)

    @ctx.endpoint
    async def get_effective_settings(
        binding_id: str, include_catalog: bool = Query(False, alias="includeCatalog"),
        conversation_id: str | None = Query(None, alias="conversationId"),
        model_id: str | None = Query(None, alias="modelId"),
    ) -> Any:
        """``GET /bindings/{id}/effective-settings``：工具栏那四栏显示什么、谁定的。

        为什么这条 GET 住在会话 router 而不是只读领域 router：它要问 Driver
        （引擎自己的配置 + Model Catalog），而 Driver Registry 只在这一层有。
        另一半理由与写端点相同——它要鉴权，只读领域 router 按 AD-72 不鉴权。

        **取不到就当没有。** Driver 报错、Backend 没注册、引擎没配置文件，都只是
        让解析链少一层来源，不是这条请求失败：工具栏总得渲染出来。
        """
        binding = await repositories.bindings.get(binding_id)
        if binding is None:
            raise _not_found("binding_not_found", f"未知 Binding：{binding_id}")
        conversation = await ctx.load_conversation(conversation_id) if conversation_id else None
        if conversation is not None and conversation.agent_binding_id != binding_id:
            raise SessionApiError(400, "binding_mismatch", "对话不属于这个引擎配置。")
        project = await repositories.projects.get(binding.project_id)

        engine = None
        catalog = None
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError:
            driver = None
        if driver is not None:
            await register_binding_snapshot(driver, binding, repositories.projects)
            reader = getattr(driver, "read_engine_settings", None)
            if reader is not None:
                try:
                    engine = await reader(binding)
                except Exception:  # noqa: BLE001 - 少一层来源，不是这条请求失败
                    logging.getLogger(__name__).debug(
                        "Engine settings unavailable for binding %s; rendering without them", binding.id, exc_info=True)
                    engine = None
            try:
                handle = host.handle_for(conversation_id) if conversation_id else None
                catalog = await scoped_model_catalog(driver, binding, handle)
            except Exception:  # noqa: BLE001 - 同上
                logging.getLogger(__name__).debug(
                    "Model catalog unavailable for binding %s; rendering without it", binding.id, exc_info=True)
                catalog = None
        payload = effective_settings_to_wire(
            binding=binding, project=project, engine=engine, catalog=catalog
        )
        handle = host.handle_for(conversation_id) if conversation_id else None
        payload["conversationControls"] = conversation_controls_of(
            driver, binding, handle, default={"reasoning": False, "approvalModes": []})
        preview = getattr(driver, "preview_model_settings", None)
        requested_model = model_id or (conversation.model_id if conversation else None)
        if handle is None and callable(preview) and requested_model:
            catalog, controls = await preview(binding, requested_model)
            payload = {**payload, **effective_settings_to_wire(binding=binding, project=project, engine=engine, catalog=catalog),
                       "conversationControls": controls}
        if include_catalog:
            # The toolbar uses the same catalog already read to resolve its defaults.
            # Session discovery is fresher than the persisted installation probe.
            backend = await repositories.backends.get(binding.backend_id)
            payload["modelCatalog"] = model_catalog_to_wire(catalog) if catalog is not None else None
            payload["uiCapabilities"] = backend_to_wire(backend)["capabilities"]["ui"] if backend is not None else None
            if driver is not None:
                from runtime.capability_matrix import capabilities_to_wire
                try:
                    payload["uiCapabilities"] = capabilities_to_wire(await driver.get_capabilities())["ui"]
                except Exception:  # noqa: BLE001 - Keep the installation snapshot during a reconnect.
                    logging.getLogger(__name__).debug(
                        "Live capabilities unavailable for backend %s; keeping the stored snapshot",
                        binding.backend_id, exc_info=True)
            if payload["uiCapabilities"] is not None:
                model_caps = payload["uiCapabilities"]["models"]
                controls = payload["conversationControls"]
                if "model" in controls:
                    model_caps["conversationScoped"] = "supported" if controls["model"] else "unsupported"
                if "reasoning" in controls:
                    model_caps["reasoning"] = "supported" if controls["reasoning"] else "unsupported"
        return payload

    return [
        RouteSpec('/conversations/{conversation_id}', patch_conversation, ["PATCH"], 'session_patch_conversation'),
        RouteSpec('/backends/{backend_id}/models', get_models, ["GET"], 'session_model_catalog'),
        RouteSpec('/bindings/{binding_id}/effective-settings', get_effective_settings, ["GET"], 'session_effective_settings'),
    ]


__all__ = ["build_settings_routes"]
