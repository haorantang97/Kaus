"""Sending messages and conversation files.

``POST /messages`` ensures the runtime, subscribes before sending so the
first ``run.*`` frame cannot be missed, and maps every start / send failure
to a stable code. Uploads and file reads go through
:class:`~app.api.conversation_files.ConversationFiles`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app.api.api_errors import ApiError
from app.api.conversation_files import MAX_UPLOAD_JSON_BYTES
from app.api.event_wait import first_run_id as _first_run_id
from app.api.session_views import envelope_to_wire
from app.projects.retirement import require_open_project
from drivers.base import (
    AgentSpawnError,
    AuthRequiredError,
    DriverError,
    FailureHint,
    MessageInput,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
)
from runtime.session_host import RuntimeNotActiveError
from app.api.session_routes.common import (
    RouteSpec,
    SendMessageBody,
    SessionApiError,
    attach_client_ref,
    auth_required_error,
    driver_message,
    hint_detail,
    spawn_failed_error,
    start_failed_error,
    unsupported_error,
)
from app.api.session_routes.context import SessionRouteContext


def build_message_routes(ctx: SessionRouteContext) -> list[RouteSpec]:
    """Message send / receipt and attachment endpoints."""
    from fastapi import Body, Query, Request
    from fastapi.responses import JSONResponse, Response

    host = ctx.host
    repositories = ctx.repositories
    registry = ctx.registry
    files = ctx.files
    run_id_timeout = ctx.run_id_timeout

    async def upload_attachment(conversation_id, request):
        await ctx.load_conversation(conversation_id)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_UPLOAD_JSON_BYTES:
                raise SessionApiError(413, "attachment_too_large", "单个附件不能超过 10 MB。")
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise SessionApiError(400, "invalid_attachment", "附件请求需要有效的 JSON。") from None
        if not isinstance(body, dict) or set(body) - {"name", "mimeType", "data"}:
            raise SessionApiError(400, "invalid_attachment", "附件请求字段无效。")
        attachment = files.upload(conversation_id, name=body.get("name"), data=body.get("data"), mime_type=body.get("mimeType"))
        return JSONResponse(status_code=201, content=attachment.model_dump(mode="json", by_alias=True, exclude_none=True))

    upload_attachment.__annotations__ = {"conversation_id": str, "request": Request, "return": Any}
    upload_attachment = ctx.endpoint(upload_attachment)

    @ctx.endpoint
    async def get_file(conversation_id: str, path: str = Query(..., max_length=4096), download: bool = False) -> Any:
        conversation = await ctx.load_conversation(conversation_id)
        from app.conversations.continuation import owned_reference_alias
        continuation = await repositories.settings.get(f"continuation:{conversation_id}")
        owned = owned_reference_alias(continuation, path)
        file = files.read(conversation_id, None if owned else await ctx.conversation_workspace(conversation), owned or path)
        disposition = "attachment" if download or not (file.mime_type.startswith("image/") or file.mime_type in {"text/plain", "application/pdf"}) else "inline"
        return Response(
            file.content, media_type=file.mime_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file.name, safe='')}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'; form-action 'none'; frame-ancestors 'self'",
                "Cache-Control": "no-store",
            },
        )

    @ctx.endpoint
    async def send_message(
        conversation_id: str, body: SendMessageBody = Body(...)
    ) -> Any:
        conversation = await ctx.load_conversation(conversation_id)
        # Before any runtime start: a confirmed pre-dispatch rejection.
        await require_open_project(repositories, conversation.project_id)
        if conversation.archived_at:
            raise SessionApiError(409, "conversation_archived", "请先恢复此对话。")
        if not body.text.strip() and not body.attachments:
            raise SessionApiError(400, "empty_message", "请输入消息或添加附件。")
        prepared = files.prepare(conversation_id, await ctx.conversation_workspace(conversation), body.attachments)
        message = MessageInput(text=body.text, attachments=prepared, metadata=dict(body.metadata), client_ref=(body.client_ref or "").strip() or None)
        # References come only from this conversation's accepted handoff record.
        # Never interpret client metadata as local-file authorization. They are
        # supplied on the first accepted turn (or a confirmed failed retry).
        from app.conversations.continuation import readable_record
        continuation = readable_record(await repositories.settings.get(f"continuation:{conversation_id}"))
        if continuation and continuation.get("contextFilesVersion") == 1:
            prior = await host.timeline_from_store(conversation_id)
            started = any(item.kind == "message" and item.role == "user" and item.delivery_status != "failed"
                          for item in (prior.items if prior else ()))
            if not started:
                from drivers.base import AttachmentRef
                context_files = []
                for reference in continuation.get("files", ()):
                    # Only conversation-owned uploads qualify; workspace access
                    # is deliberately absent here. A removed optional reference
                    # does not prevent sending the reviewed handoff itself.
                    try:
                        document = files.read(conversation_id, None, reference["ref"])
                        context_files.append(AttachmentRef(kind="file", ref=reference["ref"], name=document.name, mime_type=document.mime_type))
                    except (ApiError, OSError, ValueError, KeyError):
                        continue
                message = message.model_copy(update={"context_files": tuple(context_files)})
        # 批次十一第 2 件：Runtime 起不来 = **一个字都没发出去**。这类失败要顺带
        # 告诉前端「这条会话至今是空的」，它才敢紧接着调 DELETE 把空壳收掉。
        # 起来之后再失败就不带这个标记了——那时用户那句话已经在时间线上。
        try:
            handle = await ctx.ensure_runtime(conversation)
        except SessionApiError as exc:
            exc.extra.update(await ctx.empty_conversation_extra(conversation))
            raise
        except AuthRequiredError as exc:
            # AD-157：引擎说「你还没登录」。这不是 500，也不是「引擎没能起来」——
            # 用户有一条明确的路可走（去终端登一次），所以 400 + 稳定 code +
            # 修法，并且**在时间线上留一条 run.failed**：真机上这一句失败得毫无
            # 痕迹，页面只写「历史没有到达」，用户根本不知道发生过什么。
            # 先算「这条会话是不是白建的」**再**合成事件：我们自己补的那条
            # run.failed 不该让「删了也不丢东西」变成假。
            extra = await ctx.empty_conversation_extra(conversation)
            await ctx.emit_send_failed(conversation, exc)
            raise auth_required_error(exc, extra) from exc
        except AgentSpawnError as exc:
            # AD-159：引擎的**进程**没能拉起来。与「网关不可达」不是同一件事，
            # 所以不共用 `runtime_start_failed` 那个码：用户对这一条有一个两秒钟
            # 的动作（把同一条命令贴进终端跑一次），前端要据 code 把那句修法显示
            # 出来。仍然是 503（这台机器上的引擎坏了），仍然带「这条会话是空的」。
            raise spawn_failed_error(exc, await ctx.empty_conversation_extra(conversation)) from exc
        except UnsupportedCapabilityError as exc:
            # A coded refusal from the session projection (for example a selected
            # skill the engine archived): keep its stable code so the client can
            # localize it, and still mark the conversation as empty.
            failure = getattr(exc, "failure", None)
            if isinstance(failure, FailureHint):
                raise SessionApiError(
                    409, failure.code, failure.message, detail=failure.to_detail(),
                    **await ctx.empty_conversation_extra(conversation),
                ) from exc
            raise start_failed_error(exc, await ctx.empty_conversation_extra(conversation)) from exc
        except Exception as exc:  # noqa: BLE001 - 网关不可达之类必须是显式状态
            # 走查 F5：这条错误此前只说「坏了」，没说「怎么修」。Driver 认得出
            # 根因时（网关没在跑 / key 不匹配）会在 `DriverError.failure` 上带一份
            # 人话 + 修法，这里原样接进 `detail`；认不出来才退回类型名 + 原文。
            raise start_failed_error(exc, await ctx.empty_conversation_extra(conversation)) from exc
        # 先挂订阅再发：这样「发出去到 run.started」之间没有窗口期。
        if prepared:
            binding = await repositories.bindings.get(conversation.agent_binding_id)
            driver = registry.get(binding.backend_id)
            capabilities = await driver.get_capabilities()
            support = capabilities.card.attachments.value
            if support not in {"files", "images"} or (support == "images" and any(not (a.mime_type or "").startswith("image/") for a in prepared)):
                raise SessionApiError(400, "attachments_unsupported", "当前引擎尚未声明支持这些附件；请更换引擎或移除附件后发送。")
        after = await host.event_store.latest_sequence(conversation_id)
        subscription = host.subscribe(conversation_id, after_sequence=after)
        try:
            try:
                await host.send_message(conversation_id, message)
                run_id = await _first_run_id(subscription, timeout=run_id_timeout)
            except RuntimeNotActiveError as exc:
                raise SessionApiError(409, "runtime_not_active", str(exc)) from exc
            except UnsupportedCapabilityError as exc:
                # 批次十七第 3 件：Driver 说得出「不支持哪一项」时按那个 code 回，
                # 而不是一律 `unsupported_capability`——前端要按 code 分支决定
                # 是「改 Binding 默认」还是「这台引擎没这个能力」。
                raise unsupported_error(exc) from exc
            except TurnAlreadyRunningError as exc:
                # 批次二十七第 2 件：上一轮还在跑不是**我们**坏了，是一个可等待的
                # 状态。此前它是一个没人接的 `DriverError`，于是从这条端点漏成 500
                # ——真机 C3 的 `HTTP 500` 就有这一份。
                raise SessionApiError(
                    409, "turn_already_running", driver_message(exc), detail=hint_detail(exc)
                ) from exc
            except DriverError as exc:
                # 引擎明确拒绝了这一句（网关回了非 2xx）。它的状态码是**它的**，不是
                # 我们的：一律 502，并把网关自己那句人话带上去，不把 `HTTP 500` 原样
                # 上 wire。
                raise SessionApiError(
                    502, "message_rejected", driver_message(exc), detail=hint_detail(exc)
                ) from exc
        except SessionApiError as exc:
            # R6（批次三十七）：这一段的失败**都发生在用户那句话已经落库之后**，
            # 所以错误体要能被对回那条消息——把 AD-94 的对账编号原样放进
            # `detail.clientRef`。前端据它把本地那条置灰，而不必去猜是哪一句
            # （同一条会话上可能连发了好几句）。没给编号就不放这个键，不放 null。
            attach_client_ref(exc, body.client_ref)
            raise
        finally:
            subscription.close()
        return JSONResponse(
            status_code=202,
            content={
                "conversationId": conversation_id,
                "runtimeId": handle.runtime_id,
                "runId": run_id,
                # N §13.1：等不到就说等不到，不合成一个假的 runId。
                "runIdPending": run_id is None,
                "acceptedAfterSequence": after,
            },
        )

    @ctx.endpoint
    async def message_receipt(conversation_id: str, clientRef: str = Query(..., min_length=1, max_length=64)) -> Any:
        await ctx.load_conversation(conversation_id)
        events=await host.event_store.message_receipt(conversation_id,clientRef)
        status="unknown" if not events else "failed" if events[-1].event.name == "user.message.failed" else "recorded"
        return {"conversationId":conversation_id,"clientRef":clientRef,"status":status,
                "events":[envelope_to_wire(event) for event in events]}

    return [
        RouteSpec('/conversations/{conversation_id}/messages', send_message, ["POST"], 'session_send_message'),
        RouteSpec('/conversations/{conversation_id}/messages/receipt', message_receipt, ["GET"], 'session_message_receipt'),
        RouteSpec('/conversations/{conversation_id}/attachments', upload_attachment, ["POST"], 'session_upload_attachment'),
        RouteSpec('/conversations/{conversation_id}/files', get_file, ["GET"], 'session_get_file'),
    ]


__all__ = ["build_message_routes"]
