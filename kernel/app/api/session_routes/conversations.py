"""Conversation records: list, create, read, archive, move, delete, native history.

Everything here changes or reads the Conversation row and its native
session mapping without sending a message. Settings (model / reasoning /
permission) are in :mod:`~app.api.session_routes.settings`; messaging in
:mod:`~app.api.session_routes.messages`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.api.group_router import group_labels_for_conversations
from app.api.session_views import (
    advisory_to_wire,
    conversation_index_entry_to_wire,
    conversation_list_to_wire,
    awaiting_input,
    conversation_status,
    conversation_to_wire,
    native_history_to_wire,
    runtime_view,
    session_projection_to_wire,
    timeline_summary,
)
from app.api.views import normalize_project_id
from app.conversations.models import Conversation
from app.conversations.repository import DEFAULT_RECENT_LIMIT, MAX_RECENT_LIMIT
from app.projects.retirement import is_retired, project_deleted_error, require_open_project
from drivers.base import (
    DriverNotRegisteredError,
    RuntimeNotFoundError,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
)
from app.api.session_routes.common import (
    ArchiveConversationBody,
    BookmarkBody,
    QueueBody,
    ViewedBody,
    CreateConversationBody,
    MoveConversationBody,
    RouteSpec,
    SessionApiError,
    _not_found,
    _parse_timestamp,
)
from app.api.session_routes.context import SessionRouteContext


def build_conversation_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Conversation CRUD, archive / move and native history endpoints."""
    from fastapi import Body, Query
    from fastapi.responses import JSONResponse

    host = ctx.host
    repositories = ctx.repositories
    registry = ctx.registry
    surface_coordinator = ctx.surface_coordinator
    module_conversation_ids = ctx.module_conversation_ids

    @ctx.endpoint
    async def list_conversations(
        project_id: str, include_group_only: bool = Query(False), archived: bool = Query(False)
    ) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise _not_found("project_not_found", f"未知 Project：{project_id}")
        conversations = list(
            await repositories.conversations.list_for_project(
                normalized, include_group_only=include_group_only, archived=archived
            )
        )
        if module_conversation_ids is not None:
            hidden = module_conversation_ids()
            conversations = [item for item in conversations if item.id not in hidden]
        return conversation_list_to_wire(
            conversations,
            project_id=normalized,
            surfaces=await ctx.surfaces_of(conversations),
            # AD-159：列表里的旧字段 `state` 也按 runState 收敛，免得侧栏与会话页
            # 对同一条会话说两句话。
            run_states={
                c.id: await host.run_state_of(c) for c in conversations
            },
        )

    @ctx.endpoint
    async def create_conversation(
        project_id: str, body: CreateConversationBody = Body(...)
    ) -> Any:
        normalized = normalize_project_id(project_id)
        project = await repositories.projects.get(normalized)
        if project is None:
            raise _not_found("project_not_found", f"未知 Project：{project_id}")
        if is_retired(project):
            raise project_deleted_error()
        binding = await repositories.bindings.get(body.binding_id)
        if binding is None:
            raise _not_found("binding_not_found", f"未知 Binding：{body.binding_id}")
        if binding.project_id != normalized:
            raise SessionApiError(
                400,
                "binding_project_mismatch",
                f"Binding {binding.id} 不属于 Project {normalized}（D-06：不在中途换绑）",
            )
        from app.api.backend_facets import require_new_work
        require_new_work(registry.try_get(binding.backend_id), backend_id=binding.backend_id)
        conversation = Conversation.create(
            project_id=normalized,
            agent_binding_id=binding.id,
            title=(body.title or "").strip() or binding.display_name,
            native_session_id=body.native_session_ref,
        )
        saved = await repositories.conversations.save(conversation)
        return JSONResponse(status_code=201, content=conversation_to_wire(saved))

    @ctx.endpoint
    async def list_recent_conversations(
        updated_after: str | None = Query(None, alias="updated_after"),
        limit: int = Query(DEFAULT_RECENT_LIMIT, ge=1, le=MAX_RECENT_LIMIT),
        project: str | None = Query(None),
        include_group_only: bool = Query(False),
        archived: bool = Query(False),
    ) -> Any:
        """``GET /api/conversations``：跨项目的最近会话，按 ``updatedAt`` 倒序。

        侧栏此前是 `GET /api/projects` + 每个项目一次 `/conversations`，
        项目一多就是 30s 一次的 N+1。这里一次查完。

        ``?project=`` 把同一个端点收窄成单项目视图（接受 ``project:<slug>``
        与裸 slug，与其它端点同一口径）；``?updated_after=`` 是增量游标
        （ISO-8601，严格大于）。
        """
        since = _parse_timestamp(updated_after)
        project_id: str | None = None
        if project:
            project_id = normalize_project_id(project)
            if await repositories.projects.get(project_id) is None:
                raise _not_found("project_not_found", f"未知 Project：{project}")
        hidden = module_conversation_ids() if module_conversation_ids else set()
        conversations = list(
            await repositories.conversations.list_recent(
                updated_after=since,
                project_id=project_id,
                limit=limit + len(hidden),
                include_group_only=include_group_only,
                archived=archived,
            )
        )
        conversations = [item for item in conversations if item.id not in hidden][:limit]
        # Binding 按 id 去重后再取：一条会话一次查库会让 50 条列表变成 50 次
        # 点查，而同一个项目下的会话通常共用少数几条 Binding。
        backend_by_binding: dict[str, str | None] = {}
        for binding_id in {c.agent_binding_id for c in conversations}:
            binding = await repositories.bindings.get(binding_id)
            backend_by_binding[binding_id] = binding.backend_id if binding else None

        surfaces = await ctx.surfaces_of(conversations)
        # 批次二十二第 2 件：侧栏每行要标「这条会话在哪个组里」。按组查一次，
        # 不是一条会话一次点查——理由与上面 Binding 去重同一条。
        groups = await group_labels_for_conversations(
            repositories, [c.id for c in conversations]
        )
        viewed = await repositories.conversations.viewed_sequences([c.id for c in conversations])
        entries = []
        for conversation in conversations:
            timeline = host.timeline(conversation.id)
            entry = conversation_index_entry_to_wire(
                conversation,
                backend_id=backend_by_binding.get(conversation.agent_binding_id),
                status=conversation_status(
                    conversation,
                    live_run_state=(
                        timeline.run_state if timeline is not None else None
                    ),
                ),
                last_sequence=await host.event_store.latest_sequence(
                    conversation.id
                ),
                surface=surfaces.get(conversation.id, "card"),
                group_id=(groups.get(conversation.id) or {}).get("id"),
                group_title=(groups.get(conversation.id) or {}).get("title"),
            )
            if awaiting_input(timeline):
                entry["awaitingInput"] = True
            # 侧栏「已结束未查看」：与 lastSequence 比较；从没看过时为 null。
            entry["viewedSequence"] = viewed.get(conversation.id)
            # Hook-reported state of an agent in an external terminal (advisory).
            if surface_coordinator is not None and entry.get("surface") == "external-cli":
                if (terminal := surface_coordinator.terminal_status(conversation.id)) is not None:
                    entry["terminalStatus"] = terminal
            entries.append(entry)
        return {
            "conversations": entries,
            "count": len(entries),
            # 下一次增量刷新的游标：本页最大的 updatedAt。空页时回 null，
            # 由客户端沿用它手上那个（不要退回「从头再来」）。
            "nextUpdatedAfter": entries[0]["updatedAt"] if entries else None,
        }

    @ctx.endpoint
    async def get_conversation(conversation_id: str) -> Any:
        """会话 + 时间线摘要 + runtime 状态 + ``runState``（批次十七第 1/5 件）。

        取会话是页面进来的第一件事，所以自愈也放在这里（AD-136 的第二处）：
        「页面显示运行中、后端其实早就没了」这种失配在这一刻就收敛掉，用户不必
        先点一次停止才发现对不上。
        """
        conversation = await ctx.load_conversation(conversation_id)
        await host.reconcile_conversation(conversation)
        # AD-143：缓冲空了就从原生历史重建（v1.0 §11.3）。放在自愈之后：自愈只
        # 收敛「跑没跑完」，重建管的是「内容还在不在」，两件事互不替代。
        await host.restore_from_native_history(conversation)
        conversation = await ctx.load_conversation(conversation_id)
        handle = host.handle_for(conversation_id)
        owner = await host.describe_runtime_owner(conversation_id)
        # AD-159：一份 runState，两个字段共用。旧的 `conversation.state` 从此是它的
        # 投影，不再是另一份可能过期的快照——真机上这两个字段曾经在同一个响应里
        # 各说各话（VERIFY-BATCH-31/34）。
        run_state = await host.run_state_of(conversation)
        runtime = runtime_view(active=host.is_active(conversation_id), handle=handle, owner=owner)
        runtime["workspaceRoot"] = await ctx.conversation_workspace(conversation)
        from app.conversations.continuation import readable_record
        continuation = readable_record(await repositories.settings.get(f"continuation:{conversation_id}"))
        timeline = await ctx.timeline_of(conversation_id)
        if continuation:
            origin = continuation.get("source", {})
            source_available = origin.get("kind") == "conversation" and await repositories.conversations.get(origin["id"]) is not None
            continuation = {**{key: value for key, value in continuation.items() if key != "fileAliases"}, "sourceAvailable": source_available}
        if continuation and timeline and any(item.kind == "message" and item.role == "user" and item.delivery_status != "failed" for item in timeline.items):
            continuation = {**continuation, "draft": ""}
        return {
            "conversation": conversation_to_wire(conversation, run_state=run_state),
            **({"continuation": continuation} if continuation else {}),
            "bookmarkedMessageIds": list(await repositories.conversations.bookmarks(conversation_id)),
            "historyStorage": await host.native_storage_status(conversation),
            "runtime": runtime,
            # 页头以它为准，而不是自己从时间线推断（批次十七第 5 件）。
            "runState": run_state,
            "timeline": timeline_summary(timeline),
            # R-04：软提示与「能不能发」无关，UI 照常允许发送。
            "advisory": advisory_to_wire(host.advisory_for(conversation_id)),
            # 批次四十三：这条会话建起来时**真的送进引擎**的项目能力，只有名字
            # （没有 command / url / env——那些一个字都不上 wire）。null 的含义是
            # 「这条路没走」：没装能力投影器、这台引擎没有随会话送能力这条路、
            # 或者这是一条续接上来的会话（协议不允许续接时换挂载）。
            "projection": session_projection_to_wire(
                host.session_projection(conversation_id)
            ),
        }

    @ctx.endpoint
    async def get_queue(conversation_id: str) -> Any:
        """排队消息：保存在后端，手机与电脑看到同一份；由开着的会话页在这一轮结束后发出。"""
        await ctx.load_conversation(conversation_id)
        stored = await repositories.settings.get(f"queue:{conversation_id}")
        return {"items": list(stored.get("items", [])) if isinstance(stored, dict) else []}

    @ctx.endpoint
    async def put_queue(conversation_id: str, body: QueueBody = Body(...)) -> Any:
        await ctx.load_conversation(conversation_id)
        items = [item.model_dump() for item in body.items]
        await repositories.settings.save(f"queue:{conversation_id}", {"items": items})
        return {"items": items}

    @ctx.endpoint
    async def mark_viewed(conversation_id: str, body: ViewedBody = Body(...)) -> Any:
        """会话页看到哪儿了。只前进不后退；不占运行租约、不碰引擎。"""
        await ctx.load_conversation(conversation_id)
        if body.locale is not None:
            from app.conversations.attention_notifier import LOCALE_SETTING_KEY
            await repositories.settings.save(LOCALE_SETTING_KEY, {"value": body.locale})
        return {"viewedSequence": await repositories.conversations.mark_viewed(conversation_id, body.sequence)}

    @ctx.endpoint
    async def set_bookmark(conversation_id: str, body: BookmarkBody = Body(...)) -> Any:
        await ctx.load_conversation(conversation_id)
        if body.saved:
            timeline = await ctx.timeline_of(conversation_id)
            if timeline is None or not any(
                item.item_id == body.message_id and item.kind == "message" and item.role == "user"
                for item in timeline.items
            ):
                raise SessionApiError(404, "message_not_found", "消息不存在。")
        # Personal navigation metadata never takes a runtime lease or calls the engine.
        await repositories.conversations.set_bookmark(conversation_id, body.message_id, body.saved)
        return {"messageId": body.message_id, "saved": body.saved}

    @ctx.endpoint
    async def archive_conversation(conversation_id: str, body: ArchiveConversationBody = Body(...)) -> Any:
        try:
            async with host.conversation_control(conversation_id):
                conversation = await ctx.load_conversation(conversation_id)
                if not body.archived:
                    # A deleted Project's conversations stay archived (read-only).
                    await require_open_project(repositories, conversation.project_id)
                owner = await host.describe_runtime_owner(conversation_id)
                if body.archived and owner and owner.owner_type == "external-cli":
                    raise SessionApiError(409, "surface_external_active", "请先结束终端中的对话。")
                if body.archived:
                    await host.stop_runtime(conversation_id)
                    conversation = await ctx.load_conversation(conversation_id)
                saved = await repositories.conversations.save(conversation.evolve(
                    archived_at=(conversation.archived_at or datetime.now(timezone.utc)) if body.archived else None,
                    updated_at=datetime.now(timezone.utc),
                ))
                return {"conversation": conversation_to_wire(saved)}
        except TurnAlreadyRunningError as exc:
            raise SessionApiError(409, "turn_already_running", "本轮结束后可归档。") from exc

    @ctx.endpoint
    async def move_conversation(conversation_id: str, body: MoveConversationBody = Body(...)) -> Any:
        """Move an archived conversation into an active Project and restore it.

        The engine stays the same: the target Binding (ensured by the caller through
        ``/projects/{id}/bindings/ensure``) must use the source Binding's Backend.
        Native resume uses the stored session id with the runtime's working folder and
        the Binding's native scope; if either differs the move needs explicit
        confirmation (``workspace_changed``) because the session may not resume.
        """
        from app.errors import DomainInvariantError
        try:
            async with host.conversation_control(conversation_id):
                conversation = await ctx.load_conversation(conversation_id)
                if conversation.archived_at is None:
                    raise SessionApiError(409, "conversation_not_archived", "请先归档此对话。")
                target_id = normalize_project_id(body.project_id)
                target = await repositories.projects.get(target_id)
                if target is None:
                    raise _not_found("project_not_found", f"未知 Project：{body.project_id}")
                if is_retired(target):
                    raise project_deleted_error()
                if target_id == conversation.project_id:
                    raise SessionApiError(409, "conversation_same_project", "对话已在此 Project。")
                source_binding = await repositories.bindings.get(conversation.agent_binding_id)
                binding = await repositories.bindings.get(body.binding_id)
                if (binding is None or binding.project_id != target_id or not binding.enabled
                        or binding.runtime_config.get("group_execution")):
                    raise SessionApiError(400, "binding_project_mismatch", "请选择目标 Project 的 Agent。")
                if source_binding is None or binding.backend_id != source_binding.backend_id:
                    raise SessionApiError(409, "engine_mismatch", "移动后须使用同一引擎。")
                if conversation.native_session_id is not None and not body.confirm_workspace_change:
                    source_project = await repositories.projects.get(conversation.project_id)
                    source_workspace = (
                        await ctx.conversation_workspace(conversation)
                        or source_binding.runtime_config.get("workspace_root")
                        or (source_project.workspace_root if source_project else None)
                    )
                    target_workspace = binding.runtime_config.get("workspace_root") or target.workspace_root
                    scope_changed = binding.native_scope_ref != source_binding.native_scope_ref
                    if source_workspace != target_workspace or scope_changed:
                        raise SessionApiError(
                            409, "workspace_changed", "目标 Project 的工作目录或原生配置不同，原生会话可能无法续接。",
                            sourceWorkspace=source_workspace, targetWorkspace=target_workspace,
                            nativeScopeChanged=scope_changed,
                        )
                try:
                    saved = await repositories.conversations.save(conversation.relocate_archived(
                        project_id=target_id, agent_binding_id=binding.id, at=datetime.now(timezone.utc)))
                except DomainInvariantError as exc:
                    raise SessionApiError(409, "native_session_conflict", "目标 Agent 已关联同一原生会话。") from exc
                return {"conversation": conversation_to_wire(saved)}
        except TurnAlreadyRunningError as exc:
            raise SessionApiError(409, "turn_already_running", "本轮结束后可移动。") from exc

    @ctx.endpoint
    async def delete_conversation(conversation_id: str) -> Any:
        """删一条 Conversation（v1.0 §16.6）。

        三步，顺序不能换：**先**停 Runtime（还在跑的话，停的时候还要写回会话
        状态，领域行得在），**再**删领域记录，**最后**清事件缓冲并给在线的 SSE
        订阅者播一条 ``kaus/conversation.deleted`` 后收流。

        **原生 Session 不删**：那是 Backend 的账本（D-16 / §16.6），仪表盘只删
        自己这一侧的映射与缓冲。删过之后 ``GET /api/conversations`` 与
        ``/projects/{id}/conversations`` 自然不再返回它——不需要额外的「已删除」
        标记，因为这里做的是真删除，不是软删除。
        """
        conversation = await ctx.load_conversation(conversation_id)
        await host.stop_runtime(conversation_id)
        try:
            async with host.conversation_control(conversation_id):
                conversation = await ctx.load_conversation(conversation_id)
                owner = await host.describe_runtime_owner(conversation_id)
                if owner and owner.owner_type == "external-cli":
                    # Keep the native-session identity until the existing
                    # handoff confirms local and remote tools have stopped.
                    raise SessionApiError(409, "surface_external_active", "请先结束终端中的对话。")
                await repositories.conversations.delete(conversation_id)
                await host.purge_conversation(conversation)
        except TurnAlreadyRunningError as exc:
            raise SessionApiError(409, "turn_already_running", "请结束当前回复后重试。") from exc
        return {"conversationId": conversation_id, "deleted": True}

    @ctx.endpoint
    async def get_history(conversation_id: str) -> Any:
        conversation = await ctx.load_conversation(conversation_id)
        if conversation.native_session_id is None:
            raise SessionApiError(
                409,
                "native_session_not_bound",
                "这条 Conversation 还没有绑定原生 Session（N §9 允许懒创建）",
            )
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
        try:
            load_preview = getattr(driver, "load_native_history_preview", driver.load_native_history)
            history = await load_preview(
                binding, conversation.native_session_id
            )
        except UnsupportedCapabilityError as exc:
            raise SessionApiError(501, "unsupported_capability", str(exc)) from exc
        except (RuntimeNotFoundError, KeyError) as exc:
            raise _not_found("native_session_not_found", str(exc)) from exc
        return native_history_to_wire(history)

    return [
        RouteSpec('/conversations', list_recent_conversations, ["GET"], 'session_list_recent_conversations'),
        RouteSpec('/projects/{project_id}/conversations', list_conversations, ["GET"], 'session_list_conversations'),
        RouteSpec('/projects/{project_id}/conversations', create_conversation, ["POST"], 'session_create_conversation'),
        RouteSpec('/conversations/{conversation_id}', get_conversation, ["GET"], 'session_get_conversation'),
        RouteSpec('/conversations/{conversation_id}/archive', archive_conversation, ["PATCH"], 'session_archive_conversation'),
        RouteSpec('/conversations/{conversation_id}/move', move_conversation, ["POST"], 'session_move_conversation'),
        RouteSpec('/conversations/{conversation_id}/bookmarks', set_bookmark, ["PUT"], 'session_set_bookmark'),
        RouteSpec('/conversations/{conversation_id}/viewed', mark_viewed, ["PUT"], 'session_mark_viewed'),
        RouteSpec('/conversations/{conversation_id}/queue', get_queue, ["GET"], 'session_get_queue'),
        RouteSpec('/conversations/{conversation_id}/queue', put_queue, ["PUT"], 'session_put_queue'),
        RouteSpec('/conversations/{conversation_id}', delete_conversation, ["DELETE"], 'session_delete_conversation'),
        RouteSpec('/conversations/{conversation_id}/history', get_history, ["GET"], 'session_history'),
    ]


__all__ = ["build_conversation_routes"]
