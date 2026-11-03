"""Inbound agent traffic: notifications, client requests, permissions and fs.

Session updates are translated into public events; ``session/request_permission``
opens an approval card that ``resolve_interaction`` answers exactly once;
client ``fs/*`` requests are confined to the session workspace root and writes
go through the same approval card (AD-152). Requests arriving after a turn
stopped are rejected instead of answered.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Mapping

from drivers.approval import allow_once_option
from drivers.acp.client import AcpTransportError
from drivers.base import (
    InteractionNotFoundError,
    InteractionResponse,
    RuntimeHandle,
    UnsupportedCapabilityError,
)
from drivers.acp.runtime_state import _PendingFsWrite, _RuntimeState


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


class AcpInboundMixin:
    """Agent-to-client callbacks; composed into :class:`~drivers.acp.driver.AcpDriver`."""

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
