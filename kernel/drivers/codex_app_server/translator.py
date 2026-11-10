"""app-server notifications and server requests → :class:`AgentEventEnvelope` stream.

Pure like the ACP translator (no IO, no clock of its own): the driver injects the
``run_token``; the same notification sequence always yields the same envelopes.

ID rules (same family as ``drivers/acp/translator.py``, AD-174)
----------------------------------------------------------------
```text
runId     = "<threadId>:<runToken>:r<N>"   N = N-th turn/start on this attach
messageId = app-server item id              (agentMessage / plan item; a reasoning
                                             item opens the segment that the next
                                             agentMessage joins)
callId    = app-server item id              (commandExecution / fileChange / mcpToolCall …)
requestId = "<runId>:p<M>"                   approval requests
            "<runId>:q<M>"                   user-input questions
eventId   = uuid5(uuid5(NAMESPACE, bindingId), f"{threadId}:{runToken}:{sequence}")
```

What is deliberately *not* translated
-------------------------------------
- ``reasoning.status``: the protocol has no reasoning status/summary event, only
  text deltas (``item/reasoning/*TextDelta``) → ``reasoning.delta`` only.
- ``userMessage`` items are the echo of our own input → ``extension.event``.
- Child/sub-agent items (``subAgentActivity`` / ``collabAgentToolCall``) are
  rendered as tool cards of the parent turn; ``parentRunId`` stays empty because
  the protocol does not run them as separate turns on this thread.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from drivers.codex_app_server import protocol as P
from runtime.event_envelope import (
    AgentError,
    AgentEvent,
    AgentEventEnvelope,
    DiagnosticNotice,
    EventSource,
    ExtensionEvent,
    FileChanged,
    InteractionOption,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    PlanEntry,
    PlanUpdated,
    QuestionRequest,
    QuestionRequested,
    QuestionResolved,
    ReasoningDelta,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    SessionCreated,
    SessionResumed,
    ToolCompleted,
    ToolStarted,
    ToolUpdated,
    UsageSnapshot,
    UsageUpdated,
    make_envelope,
)

EXTENSION_NAMESPACE: str = "codex-app-server"

#: Frozen root namespace for eventId derivation (changing it renames history).
EVENT_ID_NAMESPACE: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/openai/codex#app-server-driver-event-id/v1"
)

_PLAN_STATUS: Mapping[str, str] = {"pending": "pending", "inProgress": "in_progress", "completed": "completed"}


def make_event_id(binding_id: str, thread_id: str, sequence: int, *, run_token: str) -> str:
    return str(uuid.uuid5(uuid.uuid5(EVENT_ID_NAMESPACE, binding_id), f"{thread_id}:{run_token}:{sequence}"))


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class TranslationContext:
    project_id: str
    conversation_id: str
    agent_binding_id: str
    backend_id: str
    thread_id: str
    run_token: str
    driver_version: str | None = None
    backend_version: str | None = None


@dataclass
class PendingApproval:
    """A server request waiting for ``resolve_interaction``; ``rpc_id`` is its JSON-RPC id."""

    request_id: str
    rpc_id: Any
    method: str
    option_ids: tuple[str, ...]
    item_id: str | None = None
    #: ``item/permissions/requestApproval`` echoes this profile back when granted.
    permissions: Any = None


@dataclass
class PendingUserInput:
    """One ``item/tool/requestUserInput`` = several questions answered one by one."""

    rpc_id: Any
    #: question request_id → app-server question id
    questions: dict[str, str]
    option_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    answers: dict[str, list[str]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return set(self.answers) == set(self.questions.values())

    def response(self) -> dict[str, Any]:
        return {"answers": {qid: {"answers": list(values)} for qid, values in self.answers.items()}}


@dataclass
class _Item:
    item_id: str
    kind: str
    message_id: str | None = None
    terminal: bool = False
    changes_emitted: bool = False


class AppServerTranslator:
    def __init__(self, context: TranslationContext, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self.context = context
        self._clock = clock
        self._sequence = 0
        self._run_index = 0
        self._run_id: str | None = None
        self._turn_id: str | None = None
        self._run_terminal = True
        self._items: dict[str, _Item] = {}
        self._open_message_id: str | None = None
        self._open_message_has_text = False
        self._approval_index = 0
        self._question_index = 0
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_inputs: dict[str, PendingUserInput] = {}
        self.ignored_foreign = 0
        self.ignored_after_terminal = 0
        self.unknown_notifications: list[str] = []

    # ------------------------------------------------------------------ #
    # read-only views
    # ------------------------------------------------------------------ #

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def run_active(self) -> bool:
        return not self._run_terminal

    @property
    def sequence(self) -> int:
        return self._sequence

    def pending_approval(self, request_id: str) -> PendingApproval | None:
        return self._pending_approvals.get(request_id)

    def pending_approvals(self) -> tuple[PendingApproval, ...]:
        return tuple(self._pending_approvals.values())

    def take_approval(self, request_id: str) -> PendingApproval | None:
        return self._pending_approvals.pop(request_id, None)

    def pending_input(self, request_id: str) -> PendingUserInput | None:
        return self._pending_inputs.get(request_id)

    def pending_inputs(self) -> tuple[PendingUserInput, ...]:
        return tuple(dict.fromkeys(self._pending_inputs.values()))

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def session_created(self) -> tuple[AgentEventEnvelope, ...]:
        return (self._emit(SessionCreated(session_id=self.context.thread_id)),)

    def session_resumed(self) -> tuple[AgentEventEnvelope, ...]:
        return (self._emit(SessionResumed(session_id=self.context.thread_id)),)

    def begin_run(self) -> tuple[AgentEventEnvelope, ...]:
        self._run_index += 1
        self._run_id = f"{self.context.thread_id}:{self.context.run_token}:r{self._run_index}"
        self._turn_id = None
        self._run_terminal = False
        self._items.clear()
        self._open_message_id = None
        self._open_message_has_text = False
        self._approval_index = 0
        self._question_index = 0
        self._pending_approvals.clear()
        self._pending_inputs.clear()
        return (self._emit(RunStarted(run_id=self._run_id)),)

    def bind_turn(self, turn_id: str | None) -> None:
        """``turn/start`` answered: notifications of this turn belong to the open run."""
        if isinstance(turn_id, str) and turn_id and self._turn_id is None:
            self._turn_id = turn_id

    def on_turn_completed(self, turn: Mapping[str, Any], *, interrupted: bool = False) -> tuple[AgentEventEnvelope, ...]:
        if self._run_terminal:
            self.ignored_after_terminal += 1
            return ()
        events = list(self._close_message())
        run_id = self._run_id or f"{self.context.thread_id}:{self.context.run_token}:r0"
        status = turn.get("status")
        self._run_terminal = True
        if interrupted or status == P.TURN_INTERRUPTED:
            events.append(self._emit(RunInterrupted(run_id=run_id, reason=str(status or "interrupted"))))
        elif status == P.TURN_FAILED:
            error = turn.get("error") if isinstance(turn.get("error"), Mapping) else {}
            events.append(self._emit(RunFailed(run_id=run_id, error=AgentError(
                code=str(error.get("codexErrorInfo") or "turn_failed"),
                message=str(error.get("message") or "Codex 回合失败"),
                detail={"additionalDetails": error["additionalDetails"]} if error.get("additionalDetails") else None,
            ))))
        else:
            events.append(self._emit(RunCompleted(run_id=run_id)))
        return tuple(events)

    def on_error(self, code: Any, message: str, data: Any = None) -> tuple[AgentEventEnvelope, ...]:
        """``turn/start`` rejected (or transport died) → ``run.failed``."""
        if self._run_terminal:
            self.ignored_after_terminal += 1
            return ()
        events = list(self._close_message())
        self._run_terminal = True
        events.append(self._emit(RunFailed(run_id=self._run_id, error=AgentError(
            code=str(code), message=message, detail={"data": data} if data is not None else None))))
        return tuple(events)

    def local_interrupt(self) -> tuple[AgentEventEnvelope, ...]:
        """Interrupt requested before the turn reached the engine."""
        return self.on_turn_completed({"status": P.TURN_INTERRUPTED}, interrupted=True)

    # ------------------------------------------------------------------ #
    # notifications
    # ------------------------------------------------------------------ #

    def on_notification(self, method: str, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        if not self._owns(params):
            return ()
        handler = {
            P.NOTE_TURN_STARTED: self._on_turn_started,
            P.NOTE_TURN_COMPLETED: self._on_turn_completed_note,
            P.NOTE_TURN_PLAN: self._on_plan,
            P.NOTE_ITEM_STARTED: self._on_item_started,
            P.NOTE_ITEM_COMPLETED: self._on_item_completed,
            P.NOTE_AGENT_MESSAGE_DELTA: self._on_message_delta,
            P.NOTE_PLAN_DELTA: self._on_message_delta,
            P.NOTE_REASONING_SUMMARY_DELTA: self._on_reasoning_delta,
            P.NOTE_REASONING_TEXT_DELTA: self._on_reasoning_delta,
            P.NOTE_REASONING_SUMMARY_PART: lambda _p: (),
            P.NOTE_COMMAND_OUTPUT_DELTA: self._on_tool_output_delta,
            P.NOTE_FILE_CHANGE_OUTPUT_DELTA: self._on_tool_output_delta,
            P.NOTE_MCP_PROGRESS: self._on_mcp_progress,
            P.NOTE_TOKEN_USAGE: self._on_token_usage,
            P.NOTE_SERVER_REQUEST_RESOLVED: self._on_server_request_resolved,
            P.NOTE_ERROR: self._on_error_note,
            P.NOTE_WARNING: self._on_warning,
            P.NOTE_DEPRECATION: self._on_warning,
            P.NOTE_CONFIG_WARNING: self._on_warning,
        }.get(method)
        if handler is None:
            self.unknown_notifications.append(method)
            return (self._extension(method, params),)
        return handler(params)

    def _owns(self, params: Mapping[str, Any]) -> bool:
        thread_id = params.get("threadId")
        if thread_id is None:
            thread = params.get("thread")
            thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if thread_id is not None and thread_id != self.context.thread_id:
            self.ignored_foreign += 1
            return False
        turn_id = params.get("turnId")
        if turn_id is None and isinstance(params.get("turn"), Mapping):
            turn_id = params["turn"].get("id")
        if isinstance(turn_id, str) and turn_id:
            if self._run_terminal:
                # Items of a turn that is not ours (replay, another client).
                self.ignored_after_terminal += 1
                return False
            if self._turn_id is None:
                self._turn_id = turn_id
            elif turn_id != self._turn_id:
                self.ignored_foreign += 1
                return False
        return True

    def _on_turn_started(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        return ()

    def _on_turn_completed_note(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        turn = params.get("turn")
        return self.on_turn_completed(turn if isinstance(turn, Mapping) else {})

    # --- items -------------------------------------------------------- #

    def _on_item_started(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return (self._extension(P.NOTE_ITEM_STARTED, params),)
        kind = str(item.get("type") or "")
        item_id = _str(item.get("id"))
        if item_id is None:
            return (self._extension(P.NOTE_ITEM_STARTED, params),)
        if kind == P.ITEM_USER_MESSAGE:
            return (self._extension("user_message", item),)
        if kind in (P.ITEM_AGENT_MESSAGE, P.ITEM_PLAN):
            return self._start_message(item_id, kind, _str(item.get("text")))
        if kind == P.ITEM_REASONING:
            events = list(self._open_message_for(item_id, P.ITEM_REASONING))
            for text in (*item.get("summary", ()), *item.get("content", ())):
                if isinstance(text, str) and text:
                    events.append(self._emit(ReasoningDelta(message_id=self._items[item_id].message_id or item_id, text=text)))
            return tuple(events)
        if kind == P.ITEM_CONTEXT_COMPACTION:
            return (self._extension("context_compaction", item),)
        return self._start_tool(item_id, kind, item)

    def _on_item_completed(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return (self._extension(P.NOTE_ITEM_COMPLETED, params),)
        kind = str(item.get("type") or "")
        item_id = _str(item.get("id"))
        if item_id is None:
            return (self._extension(P.NOTE_ITEM_COMPLETED, params),)
        if kind == P.ITEM_USER_MESSAGE:
            return ()
        if kind in (P.ITEM_AGENT_MESSAGE, P.ITEM_PLAN):
            events: list[AgentEventEnvelope] = []
            state = self._items.get(item_id)
            if state is None or state.message_id is None:
                events.extend(self._start_message(item_id, kind, None))
                state = self._items[item_id]
            message_id = state.message_id or item_id
            text = _str(item.get("text"))
            if self._open_message_id == message_id:
                self._open_message_id = None
                self._open_message_has_text = False
            state.terminal = True
            events.append(self._emit(MessageCompleted(message_id=message_id, text=text)))
            return tuple(events)
        if kind == P.ITEM_REASONING:
            # The reasoning segment stays open: the agentMessage that follows joins it.
            return ()
        if kind == P.ITEM_CONTEXT_COMPACTION:
            return ()
        return self._complete_tool(item_id, kind, item)

    def _start_message(self, item_id: str, kind: str, text: str | None) -> tuple[AgentEventEnvelope, ...]:
        events = list(self._open_message_for(item_id, kind))
        if text:
            self._open_message_has_text = True
            events.append(self._emit(MessageDelta(message_id=self._items[item_id].message_id or item_id, text=text)))
        return tuple(events)

    def _open_message_for(self, item_id: str, kind: str) -> tuple[AgentEventEnvelope, ...]:
        """Bind ``item_id`` to a message segment, opening one when none can be joined."""
        existing = self._items.get(item_id)
        if existing is not None and existing.message_id is not None and not existing.terminal:
            return ()
        if self._open_message_id is not None and not self._open_message_has_text and kind != P.ITEM_REASONING:
            # A reasoning item opened this segment; the assistant text joins it (same card).
            self._items[item_id] = _Item(item_id=item_id, kind=kind, message_id=self._open_message_id)
            return ()
        if self._open_message_id is not None and kind == P.ITEM_REASONING:
            self._items[item_id] = _Item(item_id=item_id, kind=kind, message_id=self._open_message_id)
            return ()
        events = list(self._close_message())
        self._open_message_id = item_id
        self._open_message_has_text = False
        self._items[item_id] = _Item(item_id=item_id, kind=kind, message_id=item_id)
        events.append(self._emit(MessageStarted(message_id=item_id)))
        return tuple(events)

    def _on_message_delta(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        item_id = _str(params.get("itemId"))
        delta = params.get("delta")
        if item_id is None or not isinstance(delta, str) or not delta:
            return ()
        events = list(self._open_message_for(item_id, P.ITEM_AGENT_MESSAGE))
        self._open_message_has_text = True
        events.append(self._emit(MessageDelta(message_id=self._items[item_id].message_id or item_id, text=delta)))
        return tuple(events)

    def _on_reasoning_delta(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        item_id = _str(params.get("itemId"))
        delta = params.get("delta")
        if item_id is None or not isinstance(delta, str) or not delta:
            return ()
        events = list(self._open_message_for(item_id, P.ITEM_REASONING))
        events.append(self._emit(ReasoningDelta(message_id=self._items[item_id].message_id or item_id, text=delta)))
        return tuple(events)

    # --- tools -------------------------------------------------------- #

    def _start_tool(self, item_id: str, kind: str, item: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        if item_id in self._items and self._items[item_id].kind == kind:
            return ()
        events = list(self._close_message())
        self._items[item_id] = _Item(item_id=item_id, kind=kind)
        name, payload = _tool_name_and_input(kind, item)
        events.append(self._emit(ToolStarted(call_id=item_id, name=name, input=payload)))
        status = _str(item.get("status"))
        if status in P.TOOL_TERMINAL_STATUSES:
            events.extend(self._complete_tool(item_id, kind, item))
        return tuple(events)

    def _complete_tool(self, item_id: str, kind: str, item: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        state = self._items.get(item_id)
        events: list[AgentEventEnvelope] = []
        if state is None:
            # Completed without a start (one-shot item): build the card first.
            events.extend(self._close_message())
            self._items[item_id] = state = _Item(item_id=item_id, kind=kind)
            name, payload = _tool_name_and_input(kind, item)
            events.append(self._emit(ToolStarted(call_id=item_id, name=name, input=payload)))
        if state.terminal:
            self.ignored_after_terminal += 1
            return tuple(events)
        state.terminal = True
        status = _str(item.get("status")) or P.TURN_COMPLETED
        if kind == P.ITEM_FILE_CHANGE and status == "completed" and not state.changes_emitted:
            state.changes_emitted = True
            for change in item.get("changes", ()):
                if isinstance(change, Mapping) and isinstance(change.get("path"), str):
                    change_kind = change.get("kind")
                    operation = change_kind.get("type") if isinstance(change_kind, Mapping) else _str(change_kind)
                    events.append(self._emit(FileChanged(path=change["path"], diff=_str(change.get("diff")),
                                                         operation=operation)))
        output: Any
        is_error = status in P.TOOL_ERROR_STATUSES
        if kind == P.ITEM_COMMAND:
            output = item.get("aggregatedOutput")
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                is_error = True
        elif kind == P.ITEM_MCP_TOOL_CALL:
            output = item.get("result") if item.get("error") is None else item.get("error")
            is_error = is_error or item.get("error") is not None
        elif kind == P.ITEM_DYNAMIC_TOOL_CALL:
            output = item.get("contentItems")
            is_error = is_error or item.get("success") is False
        elif kind == P.ITEM_FILE_CHANGE:
            output = status
        else:
            output = {k: v for k, v in item.items() if k not in ("id", "type")} or None
        events.append(self._emit(ToolCompleted(call_id=item_id, output=output, is_error=is_error)))
        return tuple(events)

    def _on_tool_output_delta(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        item_id = _str(params.get("itemId"))
        delta = params.get("delta")
        if item_id is None or not isinstance(delta, str):
            return ()
        state = self._items.get(item_id)
        if state is not None and state.terminal:
            self.ignored_after_terminal += 1
            return ()
        events: list[AgentEventEnvelope] = []
        if state is None:
            events.extend(self._close_message())
            self._items[item_id] = _Item(item_id=item_id, kind=P.ITEM_COMMAND)
            events.append(self._emit(ToolStarted(call_id=item_id, name=item_id, input=None)))
        if delta:
            events.append(self._emit(ToolUpdated(call_id=item_id, output=delta, cumulative=False)))
        return tuple(events)

    def _on_mcp_progress(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        item_id = _str(params.get("itemId"))
        if item_id is None:
            return ()
        state = self._items.get(item_id)
        if state is None or state.terminal:
            return (self._extension(P.NOTE_MCP_PROGRESS, params),)
        return (self._emit(ToolUpdated(call_id=item_id, output=None, progress=params.get("message"))),)

    # --- plan / usage / diagnostics ----------------------------------- #

    def _on_plan(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        steps = params.get("plan")
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            return (self._extension(P.NOTE_TURN_PLAN, params),)
        entries = []
        for index, raw in enumerate(steps):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("step"), str) or not raw["step"]:
                continue
            entries.append(PlanEntry(entry_id=f"e{index + 1}", content=raw["step"],
                                     status=_PLAN_STATUS.get(str(raw.get("status")))))  # type: ignore[arg-type]
        events = list(self._close_message())
        events.append(self._emit(PlanUpdated(plan_id=self._run_id, entries=tuple(entries))))
        return tuple(events)

    def _on_token_usage(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        usage = params.get("tokenUsage")
        if not isinstance(usage, Mapping):
            return (self._extension(P.NOTE_TOKEN_USAGE, params),)
        total = usage.get("total") if isinstance(usage.get("total"), Mapping) else {}
        return (self._emit(UsageUpdated(usage=UsageSnapshot(
            input_tokens=_int(total.get("inputTokens")),
            output_tokens=_int(total.get("outputTokens")),
            cached_input_tokens=_int(total.get("cachedInputTokens")),
            reasoning_tokens=_int(total.get("reasoningOutputTokens")),
            total_tokens=_int(total.get("totalTokens")),
            context_window=_int(usage.get("modelContextWindow")),
        ))),)

    def _on_error_note(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        error = params.get("error") if isinstance(params.get("error"), Mapping) else params
        message = _str(error.get("message")) or "Codex 报告了一个错误"
        level = "warning" if params.get("willRetry") else "error"
        return (self._emit(DiagnosticNotice(level=level, message=message)),)

    def _on_warning(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        message = _str(params.get("message")) or _str(params.get("summary"))
        if message is None:
            return (self._extension(P.NOTE_WARNING, params),)
        return (self._emit(DiagnosticNotice(level="warning", message=message)),)

    def _on_server_request_resolved(self, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        """Another client answered the approval: close our card without replying."""
        rpc_id = params.get("requestId")
        events: list[AgentEventEnvelope] = []
        for pending in list(self._pending_approvals.values()):
            if pending.rpc_id == rpc_id:
                self._pending_approvals.pop(pending.request_id, None)
                events.append(self._emit(PermissionResolved(request_id=pending.request_id, decision="resolved_elsewhere")))
        for request_id, batch in list(self._pending_inputs.items()):
            if batch.rpc_id == rpc_id:
                self._pending_inputs.pop(request_id, None)
                events.append(self._emit(QuestionResolved(request_id=request_id)))
        return tuple(events)

    # ------------------------------------------------------------------ #
    # server requests (the app-server blocks until we reply)
    # ------------------------------------------------------------------ #

    def on_approval_request(self, rpc_id: Any, method: str, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        if not self._owns(params):
            return ()
        item_id = _str(params.get("itemId"))
        self._approval_index += 1
        run_id = self._run_id or f"{self.context.thread_id}:{self.context.run_token}:r0"
        request_id = f"{run_id}:p{self._approval_index}"
        if method == P.REQ_PERMISSIONS_APPROVAL:
            rows = P.PERMISSION_PROFILE_OPTIONS
            title = "Codex 请求额外权限"
            detail = _str(params.get("reason"))
        else:
            rows = P.APPROVAL_OPTIONS
            available = params.get("availableDecisions")
            if isinstance(available, Sequence) and not isinstance(available, (str, bytes)):
                allowed = {_decision_tag(d) for d in available}
                rows = tuple(row for row in rows if row[0] in allowed) or rows
            if method == P.REQ_COMMAND_APPROVAL:
                title = _str(params.get("command")) or "运行命令"
                parts = [p for p in (_str(params.get("reason")), _str(params.get("cwd"))) if p]
                detail = "\n".join(parts) or None
            else:
                title = "修改文件"
                parts = [p for p in (_str(params.get("reason")), _str(params.get("grantRoot"))) if p]
                detail = "\n".join(parts) or None
        options = tuple(InteractionOption(option_id=oid, label=label, kind=kind) for oid, label, kind in rows)
        self._pending_approvals[request_id] = PendingApproval(
            request_id=request_id, rpc_id=rpc_id, method=method,
            option_ids=tuple(o.option_id for o in options), item_id=item_id,
            permissions=params.get("permissions"),
        )
        events = list(self._close_message())
        events.append(self._emit(PermissionRequested(request=PermissionRequest(
            request_id=request_id, title=title, detail=detail, tool_call_id=item_id, options=options))))
        return tuple(events)

    def on_permission_resolved(self, request_id: str, decision: str) -> tuple[AgentEventEnvelope, ...]:
        return (self._emit(PermissionResolved(request_id=request_id, decision=decision)),)

    def on_user_input_request(self, rpc_id: Any, params: Mapping[str, Any]) -> tuple[AgentEventEnvelope, ...]:
        if not self._owns(params):
            return ()
        questions = params.get("questions")
        if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)) or not questions:
            return ()
        batch = PendingUserInput(rpc_id=rpc_id, questions={})
        events = list(self._close_message())
        run_id = self._run_id or f"{self.context.thread_id}:{self.context.run_token}:r0"
        for raw in questions:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
                continue
            self._question_index += 1
            request_id = f"{run_id}:q{self._question_index}"
            options: list[InteractionOption] = []
            labels: dict[str, str] = {}
            for index, option in enumerate(raw.get("options") or ()):
                if not isinstance(option, Mapping) or not isinstance(option.get("label"), str):
                    continue
                option_id = f"o{index + 1}"
                labels[option_id] = option["label"]
                options.append(InteractionOption(option_id=option_id, label=option["label"], kind=_str(option.get("description"))))
            batch.questions[request_id] = raw["id"]
            batch.option_labels[request_id] = labels
            self._pending_inputs[request_id] = batch
            header = _str(raw.get("header"))
            prompt = _str(raw.get("question")) or header or raw["id"]
            events.append(self._emit(QuestionRequested(request=QuestionRequest(
                request_id=request_id, prompt=f"{header}\n\n{prompt}" if header and header != prompt else prompt,
                options=tuple(options), allow_free_text=bool(raw.get("isOther")) or not options))))
        if not batch.questions:
            return ()
        return tuple(events)

    def answer_question(self, request_id: str, *, option_id: str | None, text: str | None,
                        cancelled: bool) -> tuple[tuple[AgentEventEnvelope, ...], PendingUserInput | None, bool]:
        """Record one answer. Returns ``(events, batch when every question is answered, cancelled)``."""
        batch = self._pending_inputs.pop(request_id, None)
        if batch is None:
            raise KeyError(request_id)
        question_id = batch.questions[request_id]
        if cancelled:
            for other in list(batch.questions):
                self._pending_inputs.pop(other, None)
            events = tuple(self._emit(QuestionResolved(request_id=rid)) for rid in batch.questions)
            return events, batch, True
        labels = batch.option_labels.get(request_id, {})
        if option_id is not None and option_id in labels:
            answer = labels[option_id]
        elif text is not None:
            answer = text
        elif option_id is not None:
            answer = option_id
        else:
            raise ValueError("请选择一个选项或输入回答")
        batch.answers[question_id] = [answer]
        events = (self._emit(QuestionResolved(request_id=request_id)),)
        return events, (batch if batch.complete else None), False

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _close_message(self) -> tuple[AgentEventEnvelope, ...]:
        message_id = self._open_message_id
        if message_id is None:
            return ()
        self._open_message_id = None
        self._open_message_has_text = False
        for state in self._items.values():
            if state.message_id == message_id:
                state.terminal = True
        return (self._emit(MessageCompleted(message_id=message_id)),)

    def _extension(self, name: str, data: Any) -> AgentEventEnvelope:
        return self._emit(ExtensionEvent(namespace=EXTENSION_NAMESPACE, name=name,
                                         data=dict(data) if isinstance(data, Mapping) else data))

    def _emit(self, event: AgentEvent) -> AgentEventEnvelope:
        sequence = self._sequence
        self._sequence += 1
        context = self.context
        return make_envelope(
            event=event,
            project_id=context.project_id,
            conversation_id=context.conversation_id,
            agent_binding_id=context.agent_binding_id,
            backend_id=context.backend_id,
            native_session_id=context.thread_id,
            run_id=self._run_id,
            sequence=sequence,
            occurred_at=self._clock(),
            event_id=make_event_id(context.agent_binding_id, context.thread_id, sequence, run_token=context.run_token),
            source=EventSource(driver_kind="native", driver_version=context.driver_version,
                               backend_version=context.backend_version),
        )


def _tool_name_and_input(kind: str, item: Mapping[str, Any]) -> tuple[str, Any]:
    if kind == P.ITEM_COMMAND:
        command = _str(item.get("command")) or "shell"
        return command, {"command": command, "cwd": item.get("cwd")}
    if kind == P.ITEM_FILE_CHANGE:
        changes = [
            {"path": c.get("path"), "kind": (c.get("kind") or {}).get("type") if isinstance(c.get("kind"), Mapping) else c.get("kind")}
            for c in item.get("changes", ()) if isinstance(c, Mapping)
        ]
        return "apply_patch", {"changes": changes}
    if kind == P.ITEM_MCP_TOOL_CALL:
        return f"{item.get('server', 'mcp')}/{item.get('tool', 'tool')}", item.get("arguments")
    if kind == P.ITEM_DYNAMIC_TOOL_CALL:
        return str(item.get("tool") or "tool"), item.get("arguments")
    if kind == P.ITEM_WEB_SEARCH:
        return "web_search", {"query": item.get("query")}
    if kind == P.ITEM_IMAGE_VIEW:
        return "view_image", {"path": item.get("path")}
    return kind, {k: v for k, v in item.items() if k not in ("id", "type")} or None


def _decision_tag(decision: Any) -> str | None:
    if isinstance(decision, str):
        return decision
    if isinstance(decision, Mapping) and len(decision) == 1:
        return next(iter(decision))
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "EVENT_ID_NAMESPACE",
    "EXTENSION_NAMESPACE",
    "AppServerTranslator",
    "PendingApproval",
    "PendingUserInput",
    "TranslationContext",
    "make_event_id",
]
