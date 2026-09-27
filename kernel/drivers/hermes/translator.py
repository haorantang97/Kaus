"""SSE 载荷 → :class:`~runtime.event_envelope.AgentEventEnvelope`（规格 §3）。

纯函数式的有状态翻译器：喂载荷，出信封。不做 IO、不认识 HTTP，因此可以直接拿
run3 的原始 ``data:`` 行当 fixture 逐事件断言（验收点 2）。

ID 合成（AD-37，**规则一经发布不得更改**）
------------------------------------------
SSE 里只有 ``run_id`` 一个稳定 ID，其余全部合成：

=============== ==================================================================
``messageId``   ``<runId>:m<序号>``，助手消息段序号从 1 起
``callId``      ``<runId>:t<序号>``，按 ``tool.started`` 出现顺序，从 1 起
``eventId``     ``uuid5(binding 命名空间, "<runId>:<本地序号>")``
``nativeEventId`` ``<runId>#<本地序号，6 位补零>``（规格 §3.3 的重连去重依据）
=============== ==================================================================

确定性是硬要求：重连重放时同一条原生事件必须产出同一个 ``eventId``，
Session Host 的「已见过就整条丢弃」才生效（N §7.3 规则 9）。因此本类的所有
计数器都只随**收到的事件**推进，不掺任何时钟或随机数。

三条降级
--------
1. ``tool.completed`` 的载荷**不带工具结果** → ``output=None``。AD-38：reducer
   见到 ``output=None`` 会保留已累积的内容，不会把卡片清空。工具的真实输出只能在
   回合结束后从 ``messages`` 表取（规格 §3.2-D）。
2. ``reasoning.available`` 的语义未定（§8-③）→ 按规格 §3.2 的三分支降级。
3. 未列出的事件名 → ``extension.event{namespace:"hermes"}``；``data`` 不是 JSON
   或没有 ``event`` 键 → 丢弃 + **每 run 至多一条** ``diagnostic.notice``。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from drivers.hermes.http_client import SseEvent
from runtime.event_envelope import (
    AgentError,
    AgentEvent,
    AgentEventEnvelope,
    DiagnosticNotice,
    EventSource,
    ExtensionEvent,
    InteractionOption,
    MessageCompleted,
    MessageDelta,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    ReasoningDelta,
    ReasoningStatus,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    ToolCompleted,
    ToolStarted,
    ToolUpdated,
    UsageSnapshot,
    UsageUpdated,
    make_envelope,
)

EXTENSION_NAMESPACE = "hermes"

#: run3 实测的事件名全集（规格 §3.2-A）。名字之外的一律走 extension.event。
MEASURED_EVENT_NAMES: tuple[str, ...] = (
    "tool.started",
    "tool.completed",
    "message.delta",
    "reasoning.available",
    "run.completed",
)

#: 规格 §3.4：``GET /v1/runs/{id}`` 的 status 取值集合（``stopping`` 是过渡态）。
RUN_STATUS_VALUES: frozenset[str] = frozenset(
    {"started", "completed", "failed", "cancelled", "stopping"}
)

#: AD-147：停止请求发出去了、引擎始终不给终态时，那条合成终态的 ``reason``。
#: 前端按它把「这一轮以未确认的中断收场」与正常的用户中断（``"stopped"``）分开。
STOP_UNCONFIRMED_REASON = "stop_unconfirmed"

#: 规格 §2.8：审批端点的 400 响应实测暴露的取值集合。
APPROVAL_CHOICES: tuple[str, ...] = ("always", "deny", "once", "session")

#: Hermes 的 choice ↔ 公共层 option_id（规格 §2.8 的映射表，双向都用它）。
CHOICE_TO_OPTION: Mapping[str, str] = {
    "once": "allow_once",
    "session": "allow_session",
    "always": "allow_always",
    "deny": "deny",
}
OPTION_TO_CHOICE: Mapping[str, str] = {v: k for k, v in CHOICE_TO_OPTION.items()}
CHOICE_LABELS: Mapping[str, str] = {
    "once": "仅这一次",
    "session": "本次会话内允许",
    "always": "始终允许",
    "deny": "拒绝",
}


def binding_namespace(binding_id: str) -> uuid.UUID:
    """把 Binding id 变成一个稳定的 uuid5 命名空间（AD-37 的 ``NAMESPACE=binding_id``）。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"urn:dashboard:binding:{binding_id}")


@dataclass(frozen=True)
class TranslationTarget:
    """一条 Conversation 的信封头常量。"""

    project_id: str
    conversation_id: str
    agent_binding_id: str
    backend_id: str
    native_session_id: str | None = None
    driver_version: str | None = None
    backend_version: str | None = None


@dataclass
class HermesRunTranslator:
    """一条 run 的 SSE 流 → 公共事件流。

    每条 run 一个实例。``send_message`` 拿到 202 之后立刻用 ``run_id`` 造它，
    ``run.completed`` / 终态由 :meth:`finalize` 收尾。
    """

    target: TranslationTarget
    run_id: str

    # --- 合成状态（全部只随事件推进，保证可重放） --- #
    _sequence: int = 0
    _message_ordinal: int = 0
    _tool_ordinal: int = 0
    _open_calls: list[str] = field(default_factory=list)
    #: 本 run 内按出现顺序合成的 (callId, 工具名)。回合结束的工具输出回填按它
    #: 与 ``messages.tool_calls`` 做 FIFO 配对（规格 §3.3 的同一条配对规则）。
    _call_sequence: list[tuple[str, str]] = field(default_factory=list)
    _delta_chunks: list[str] = field(default_factory=list)
    _reasoning_texts: list[str] = field(default_factory=list)
    _pending_approvals: list[str] = field(default_factory=list)
    _malformed_reported: bool = False
    _foreign_run_reported: bool = False
    _saw_run_completed: bool = False
    _finalized: bool = False
    #: 收尾的正文与 usage 已经发过，重复状态读取不重复发布。
    _prelude_emitted: bool = False

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def message_id(self) -> str:
        """当前助手消息段的 id；还没有 delta 时按第 1 段给（AD-37）。"""
        return f"{self.run_id}:m{max(self._message_ordinal, 1)}"

    @property
    def tool_calls_in_order(self) -> tuple[tuple[str, str], ...]:
        """本 run 合成过的 (callId, 工具名)，按出现顺序。"""
        return tuple(self._call_sequence)

    @property
    def accumulated_text(self) -> str:
        return "".join(self._delta_chunks)

    @property
    def saw_run_completed(self) -> bool:
        """收到过 ``run.completed``。规格 §3.4：它只是「该去查一次状态」的信号。"""
        return self._saw_run_completed

    def native_event_id(self, sequence: int) -> str:
        return f"{self.run_id}#{sequence:06d}"

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    def translate_sse(self, event: SseEvent) -> tuple[AgentEventEnvelope, ...]:
        """翻译一条 SSE 帧。载荷不合规时按 §3.2 的最后两行降级。"""
        payload = event.payload()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("event"), str):
            return self._malformed(event)
        return self.translate_payload(payload)

    def translate_payload(
        self, payload: Mapping[str, Any]
    ) -> tuple[AgentEventEnvelope, ...]:
        name = payload.get("event")
        if not isinstance(name, str):
            return self._malformed(None)

        payload_run_id = payload.get("run_id")
        if isinstance(payload_run_id, str) and payload_run_id != self.run_id:
            # 规格 §3.3：载荷 run_id 与当前 handle 的 activeRunId 不符 → 丢弃 + 提示。
            if self._foreign_run_reported:
                return ()
            self._foreign_run_reported = True
            return (
                self._envelope(
                    DiagnosticNotice(
                        level="warn",
                        message="收到不属于当前回合的后端事件，已忽略",
                    ),
                    occurred_at=_occurred_at(payload),
                ),
            )

        occurred_at = _occurred_at(payload)

        if name == "message.delta":
            return self._message_delta(payload, occurred_at)
        if name == "tool.started":
            return self._tool_started(payload, occurred_at)
        if name == "tool.completed":
            return self._tool_completed(payload, occurred_at)
        if name == "reasoning.available":
            return self._reasoning_available(payload, occurred_at)
        if name == "run.completed":
            # 规格 §3.4：终态以 GET /v1/runs/{id}.status 为准，这里只记信号。
            self._saw_run_completed = True
            return ()
        if name == "approval.request":
            return self._approval_request(payload, occurred_at)
        if name == "approval.responded":
            return self._approval_responded(payload, occurred_at)
        return (
            self._envelope(
                ExtensionEvent(
                    namespace=EXTENSION_NAMESPACE, name=name, data=dict(payload)
                ),
                occurred_at=occurred_at,
            ),
        )

    def translate_stream(
        self, events: Sequence[SseEvent | Mapping[str, Any]]
    ) -> tuple[AgentEventEnvelope, ...]:
        out: list[AgentEventEnvelope] = []
        for event in events:
            if isinstance(event, SseEvent):
                out.extend(self.translate_sse(event))
            else:
                out.extend(self.translate_payload(event))
        return tuple(out)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def run_started(self, *, replayed: bool = False) -> tuple[AgentEventEnvelope, ...]:
        """``POST /v1/runs`` 的 202 响应 → ``run.started``。

        规格 §2.7：``replayed=true``（或响应头 ``Idempotency-Replayed``）表示这是
        幂等回放，不重复建卡——所以这时一条事件都不发。
        """
        if replayed:
            return ()
        return (self._envelope(RunStarted(run_id=self.run_id)),)

    def finalize(
        self, run_object: Mapping[str, Any] | None, *, fallback_status: str = "completed"
    ) -> tuple[AgentEventEnvelope, ...]:
        """回合收尾：``GET /v1/runs/{id}`` 的结果 → usage + 正文兜底 + 终态。

        产出顺序固定：``usage.updated`` → 可选的 ``diagnostic.notice`` →
        ``message.completed`` → 终态 ``run.*``。终态必须最后发，否则 reducer 会在
        run 已收敛之后再收到内容事件（N §7.3 规则 9 会把它们丢掉）。

        ``message.completed`` 的取值以后端的 ``output`` 为准（规格 §3.4：它比我们
        本地拼接的 delta 可靠）。**只要这一轮出现过助手正文就一定发**——没有它
        消息卡永远不进终态，卡片会一直停在「正在输出」。
        """
        if self._finalized:
            return ()
        run_object = run_object or {}
        status = str(run_object.get("status") or fallback_status)
        if status in {"queued", "started", "running", "stopping"}:
            return ()
        if status not in {"completed", "failed", "cancelled", "stopped"}:
            return (self.notice("warn", f"后端报告了未知的回合状态：{status}"),)
        self._finalized = True
        envelopes = self._prelude(run_object)
        envelopes.extend(self._terminal(status, run_object))
        return tuple(envelopes)

    def stop_unconfirmed(
        self,
        run_object: Mapping[str, Any] | None,
        *,
        last_status: str | None,
    ) -> tuple[AgentEventEnvelope, ...]:
        """报告停止未确认，保持正文与运行状态开放，继续接收真实结果。"""
        del run_object
        if self._finalized:
            return ()
        status_word = last_status or "unknown"
        return (
            self._envelope(
                DiagnosticNotice(
                    level="warn",
                    message=(
                        "引擎没有确认中断：停止请求已发出，但这一轮始终没有进入终态"
                        f"（最后读到的状态：{status_word}）。仍在等待引擎确认；"
                        "如需立即停止，请到引擎那一侧处理"
                    ),
                )
            ),
            self._envelope(
                ExtensionEvent(
                    namespace=EXTENSION_NAMESPACE,
                    name="run.stop_unconfirmed",
                    data={"stopStatus": status_word, "reason": STOP_UNCONFIRMED_REASON},
                )
            ),
        )

    def run_unavailable(self) -> tuple[AgentEventEnvelope, ...]:
        """原生 run 已不再可查询；报告结果缺失，不伪称取消或成功。"""
        if self._finalized:
            return ()
        self._finalized = True
        return (
            self._envelope(
                RunFailed(
                    run_id=self.run_id,
                    error=AgentError(
                        code="hermes.run_unavailable",
                        message="引擎已无法查询这一轮，运行结果未能确认；请检查原生会话。",
                        retriable=False,
                    ),
                )
            ),
        )

    def _prelude(self, run_object: Mapping[str, Any]) -> list[AgentEventEnvelope]:
        """收尾的**非终态**那一半：usage + 正文兜底（终态由调用方追加）。

        每条 run 只发一次——见 ``_prelude_emitted``。
        """
        if self._prelude_emitted:
            return []
        self._prelude_emitted = True
        envelopes: list[AgentEventEnvelope] = []

        usage = _usage_from_run(run_object.get("usage"))
        if usage is not None:
            envelopes.append(self._envelope(UsageUpdated(usage=usage)))

        output = run_object.get("output")
        text = output if isinstance(output, str) else None
        # 只在**实质**不一致时提示：run3 实测里后端的 output 已经把首尾空白去掉了
        # （delta 首片是 "\n\nHER"），为这点差异弹一条 warn 只会淹没真正的丢包。
        if (
            text is not None
            and self._delta_chunks
            and text.strip() != self.accumulated_text.strip()
        ):
            envelopes.append(
                self._envelope(
                    DiagnosticNotice(
                        level="warn",
                        message="流式片段与后端最终文本不一致，已改用后端文本",
                    )
                )
            )
        if self._message_ordinal or text:
            envelopes.append(
                self._envelope(
                    MessageCompleted(
                        message_id=self.message_id,
                        text=text if text is not None else self.accumulated_text,
                    )
                )
            )

        return envelopes

    def _terminal(
        self, status: str, run_object: Mapping[str, Any]
    ) -> list[AgentEventEnvelope]:
        if status == "failed":
            message = run_object.get("error") or run_object.get("last_event") or "run failed"
            return [
                self._envelope(
                    RunFailed(
                        run_id=self.run_id,
                        error=AgentError(
                            code="hermes.run_failed",
                            message=str(message),
                            retriable=False,
                        ),
                    )
                )
            ]
        if status in {"cancelled", "stopped"}:
            return [
                self._envelope(RunInterrupted(run_id=self.run_id, reason="stopped"))
            ]
        return [self._envelope(RunCompleted(run_id=self.run_id))]

    def backend_restarted(self) -> tuple[AgentEventEnvelope, ...]:
        """规格 §1.5：gateway 重启 → 在途 run 不可恢复，发可重试的失败。"""
        if self._finalized:
            return ()
        self._finalized = True
        return (
            self._envelope(
                RunFailed(
                    run_id=self.run_id,
                    error=AgentError(
                        code="backend.restarted",
                        message="后端进程已重启，本轮实时流不可恢复；历史将从原生存储对账",
                        retriable=True,
                    ),
                )
            ),
        )

    def notice(self, level: str, message: str) -> AgentEventEnvelope:
        return self._envelope(DiagnosticNotice(level=level, message=message))

    def tool_backfill(
        self,
        call_id: str,
        *,
        output: Any = None,
        arguments: Any = None,
        native_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> AgentEventEnvelope:
        """回合结束后的工具补账（批次十六第 3 件 / 规格 §3.2-D）。

        SSE 的 ``tool.started`` 只有截断的 ``preview``，``tool.completed`` 干脆
        不带输出——完整入参与结果只在原生历史的 ``messages.tool_calls`` /
        ``role="tool"`` 行里。这条事件把它们补回同一张卡：

        - ``output`` = 工具结果（全量，因此 ``cumulative=True``）；
        - ``progress`` = 完整入参与真实调用 id。**不新增信封字段**（v1.1 冻结）：
          ``ToolUpdated.progress`` 本来就是 ``Any``，这里放一个带
          ``source="native_history"`` 标记的结构，UI 一眼看得出这是事后补的，
          而不是流式来的。
        """
        progress: dict[str, Any] = {"source": "native_history"}
        if arguments is not None:
            progress["input"] = arguments
        if native_call_id:
            progress["nativeCallId"] = native_call_id
        if tool_name:
            progress["name"] = tool_name
        return self._envelope(
            ToolUpdated(
                call_id=call_id,
                output=output,
                progress=progress if len(progress) > 1 else None,
                cumulative=True,
            )
        )

    # ------------------------------------------------------------------ #
    # 各事件
    # ------------------------------------------------------------------ #

    def _message_delta(
        self, payload: Mapping[str, Any], occurred_at: datetime | None
    ) -> tuple[AgentEventEnvelope, ...]:
        delta = payload.get("delta")
        if not isinstance(delta, str) or delta == "":
            return ()
        if self._message_ordinal == 0:
            self._message_ordinal = 1
        self._delta_chunks.append(delta)
        return (
            self._envelope(
                MessageDelta(message_id=self.message_id, text=delta),
                occurred_at=occurred_at,
            ),
        )

    def _tool_started(
        self, payload: Mapping[str, Any], occurred_at: datetime | None
    ) -> tuple[AgentEventEnvelope, ...]:
        tool = str(payload.get("tool") or "tool")
        self._tool_ordinal += 1
        call_id = f"{self.run_id}:t{self._tool_ordinal}"
        self._open_calls.append(call_id)
        self._call_sequence.append((call_id, tool))
        preview = payload.get("preview")
        return (
            self._envelope(
                ToolStarted(
                    call_id=call_id,
                    name=tool,
                    # 规格 §3.2：preview 是**截断的展示串**，不是完整入参。
                    # 完整入参只能在回合结束后从 messages.tool_calls 取。
                    input={"preview": preview} if preview is not None else None,
                ),
                occurred_at=occurred_at,
            ),
        )

    def _tool_completed(
        self, payload: Mapping[str, Any], occurred_at: datetime | None
    ) -> tuple[AgentEventEnvelope, ...]:
        # 规格 §3.3：FIFO 配对——匹配最早一个未完成的调用。载荷没有 callId，
        # 也没有工具名之外的线索，所以同名并发调用会配错（已知局限，见下）。
        envelopes: list[AgentEventEnvelope] = []
        if len(self._open_calls) > 1:
            envelopes.append(
                self._envelope(
                    DiagnosticNotice(
                        level="warn",
                        message="同一回合有多个未完成的工具调用，配对可能不准确",
                    ),
                    occurred_at=occurred_at,
                )
            )
        if self._open_calls:
            call_id = self._open_calls.pop(0)
        else:
            # 中途接流：没见过 started 也要给出确定的 callId，不能崩。
            self._tool_ordinal += 1
            call_id = f"{self.run_id}:t{self._tool_ordinal}"
            self._call_sequence.append((call_id, str(payload.get("tool") or "tool")))
        envelopes.append(
            self._envelope(
                ToolCompleted(
                    call_id=call_id,
                    # 规格 §3.2-A：载荷不带工具结果 → 恒 None。
                    # AD-38：reducer 见到 None 会保留已累积内容，不清空卡片。
                    output=None,
                    is_error=bool(payload.get("error")),
                ),
                occurred_at=occurred_at,
            )
        )
        return tuple(envelopes)

    def _reasoning_available(
        self, payload: Mapping[str, Any], occurred_at: datetime | None
    ) -> tuple[AgentEventEnvelope, ...]:
        """规格 §3.2 的三分支降级（§8-③ 定案前的口径）。"""
        text = payload.get("text")
        if not isinstance(text, str) or text == "":
            return (
                self._envelope(
                    ReasoningStatus(status="available"), occurred_at=occurred_at
                ),
            )

        # 分支 1：与本 run 已收到的 delta 拼接结果全等或互为前缀 → 判为「答案重复」，
        # 丢弃，避免思考区和答案区显示同一段话。
        # 比较前先 strip：run3 实测的 delta 首片是 "\n\nHER"，而 reasoning 文本没有
        # 那两个前导换行——不 strip 的话这条实测样本就会漏过判据，把答案原样塞进思考区。
        accumulated = self.accumulated_text.strip()
        probe = text.strip()
        if accumulated and probe and (accumulated.startswith(probe) or probe.startswith(accumulated)):
            return ()

        # 分支 2：同一 run 内后一条以前一条为前缀 → 判为增量流 → reasoning.delta。
        if self._reasoning_texts:
            previous = self._reasoning_texts[-1]
            if text.startswith(previous) and text != previous:
                self._reasoning_texts.append(text)
                return (
                    self._envelope(
                        ReasoningDelta(
                            message_id=self.message_id, text=text[len(previous) :]
                        ),
                        occurred_at=occurred_at,
                    ),
                )

        # 分支 3：其余情况按后端「一次性给出的摘要」处理。
        self._reasoning_texts.append(text)
        return (
            self._envelope(
                ReasoningStatus(status="available", summary=text),
                occurred_at=occurred_at,
            ),
        )

    # --- 审批（规格 §3.2-B，本轮未实测，§8-② 定案前是兼容分支） --- #

    def synth_request_id(self, payload: Mapping[str, Any]) -> str:
        """规格 §3.3：``appr_<run_id>_<pattern_key 或 sha1(command)[:12]>_<ms>``。

        载荷自带 request/approval id 时**直接用它**——§8-② 一旦拿到真实载荷，
        这条合成规则就该退居回退位（规格 §8-② 的第二个分支）。
        """
        for key in ("request_id", "requestId", "approval_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        discriminator = payload.get("pattern_key")
        if not isinstance(discriminator, str) or not discriminator:
            command = payload.get("command")
            digest = hashlib.sha1(  # noqa: S324 - 只用来做稳定短标识，不作安全用途
                str(command or "").encode("utf-8")
            ).hexdigest()
            discriminator = digest[:12]
        timestamp = payload.get("timestamp")
        millis = int(float(timestamp) * 1000) if isinstance(timestamp, (int, float)) else 0
        return f"appr_{self.run_id}_{discriminator}_{millis}"

    def _approval_request(
        self, payload: Mapping[str, Any], occurred_at: datetime | None
    ) -> tuple[AgentEventEnvelope, ...]:
        request_id = self.synth_request_id(payload)
        command = payload.get("command")
        description = payload.get("description")
        title = description if isinstance(description, str) and description else None
        if title is None:
            first_line = str(command or "").splitlines()[:1]
            title = f"需要批准：{first_line[0]}" if first_line else "需要批准"
        raw_choices = payload.get("choices")
        choices = (
            [c for c in raw_choices if isinstance(c, str)]
            if isinstance(raw_choices, list) and raw_choices
            else list(APPROVAL_CHOICES)
        )
        options = tuple(
            InteractionOption(option_id=CHOICE_TO_OPTION[c], label=CHOICE_LABELS[c])
            for c in choices
            if c in CHOICE_TO_OPTION
        )
        self._pending_approvals.append(request_id)
        envelopes: list[AgentEventEnvelope] = []
        if len(self._pending_approvals) > 1:
            # 规格 §2.8：端点是 run 级、不带 request id → 维持「每 run 至多一个
            # pending」的假设；出现第二个时如实提示，只允许操作最新一张卡。
            envelopes.append(
                self._envelope(
                    DiagnosticNotice(
                        level="warn",
                        message="同一回合出现了多个待决审批，只有最新一条可操作",
                    ),
                    occurred_at=occurred_at,
                )
            )
        envelopes.append(
            self._envelope(
                PermissionRequested(
                    request=PermissionRequest(
                        request_id=request_id,
                        title=title,
                        detail=str(command) if command is not None else None,
                        tool_call_id=self._open_calls[0] if self._open_calls else None,
                        options=options,
                    )
                ),
                occurred_at=occurred_at,
            )
        )
        return tuple(envelopes)

    def _approval_responded(
        self, payload: Mapping[str, Any], occurred_at: datetime | None
    ) -> tuple[AgentEventEnvelope, ...]:
        request_id = self.synth_request_id(payload)
        if request_id in self._pending_approvals:
            self._pending_approvals.remove(request_id)
        elif self._pending_approvals:
            request_id = self._pending_approvals.pop()
        decision = payload.get("choice")
        return (
            self._envelope(
                PermissionResolved(
                    request_id=request_id,
                    decision=str(decision) if decision is not None else "resolved",
                ),
                occurred_at=occurred_at,
            ),
        )

    def resolved_elsewhere(self, request_id: str) -> AgentEventEnvelope:
        """规格 §2.8：无待决时的 400 不是错误——审批已被别处解决。"""
        if request_id in self._pending_approvals:
            self._pending_approvals.remove(request_id)
        return self._envelope(
            PermissionResolved(request_id=request_id, decision="resolved_elsewhere")
        )

    def synthesized_resolution(self, request_id: str, decision: str) -> AgentEventEnvelope:
        """规格 §2.8：2s 内没收到 ``approval.responded`` 时自行合成闭环。"""
        if request_id in self._pending_approvals:
            self._pending_approvals.remove(request_id)
        return self._envelope(
            PermissionResolved(request_id=request_id, decision=decision)
        )

    def has_pending_approval(self, request_id: str) -> bool:
        return request_id in self._pending_approvals

    def _malformed(self, event: SseEvent | None) -> tuple[AgentEventEnvelope, ...]:
        """``data`` 不是 JSON、或没有 ``event`` 键（OpenAI 兼容分支混入）。"""
        del event
        if self._malformed_reported:
            return ()
        self._malformed_reported = True
        return (
            self._envelope(
                DiagnosticNotice(
                    level="warn",
                    message="后端事件流里出现了无法识别的帧，已忽略（每回合只提示一次）",
                )
            ),
        )

    # ------------------------------------------------------------------ #
    # 信封
    # ------------------------------------------------------------------ #

    def _envelope(
        self, event: AgentEvent, *, occurred_at: datetime | None = None
    ) -> AgentEventEnvelope:
        sequence = self._sequence
        self._sequence += 1
        native_event_id = self.native_event_id(sequence)
        return make_envelope(
            event=event,
            project_id=self.target.project_id,
            conversation_id=self.target.conversation_id,
            agent_binding_id=self.target.agent_binding_id,
            backend_id=self.target.backend_id,
            native_session_id=self.target.native_session_id,
            run_id=self.run_id,
            native_event_id=native_event_id,
            # AD-37：eventId = uuid5(binding 命名空间, "<runId>:<本地序号>")。
            # 确定性是重连去重的前提，不得改成 uuid4。
            event_id=str(
                uuid.uuid5(
                    binding_namespace(self.target.agent_binding_id),
                    f"{self.run_id}:{sequence}",
                )
            ),
            # Session Host 会统一重排 sequence（它是 Conversation 级游标）；
            # 这里给的是 Driver 本地序号，只保证同一流内单调。
            sequence=sequence,
            occurred_at=occurred_at,
            source=EventSource(
                driver_kind="native",
                driver_version=self.target.driver_version,
                backend_version=self.target.backend_version,
            ),
        )


def _occurred_at(payload: Mapping[str, Any]) -> datetime | None:
    """载荷的 ``timestamp``（float 秒）→ UTC datetime。

    规格 §3.3：**不要用本地接收时间**——实测同一段 delta 的间隔小到 0.2ms，
    本地时间会把顺序打乱。
    """
    raw = payload.get("timestamp")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return None


def _usage_from_run(raw: Any) -> UsageSnapshot | None:
    """``GET /v1/runs/{id}.usage`` → :class:`UsageSnapshot`。

    N §7.3 规则 6：缺的字段一律 ``None``，不伪造。实测字段名是 ``output_tokens``
    而不是文档写的 ``completion_tokens``——以实测为准，同时容忍文档名。
    """
    if not isinstance(raw, Mapping):
        return None
    output = raw.get("output_tokens")
    if output is None:
        output = raw.get("completion_tokens")
    snapshot = UsageSnapshot(
        input_tokens=_as_int(raw.get("input_tokens")),
        output_tokens=_as_int(output),
        total_tokens=_as_int(raw.get("total_tokens")),
        # HTTP 路径无 context_used 来源（规格 §3.7）→ 保持 None，不伪造。
    )
    if (
        snapshot.input_tokens is None
        and snapshot.output_tokens is None
        and snapshot.total_tokens is None
    ):
        return None
    return snapshot


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


__all__ = [
    "APPROVAL_CHOICES",
    "CHOICE_LABELS",
    "CHOICE_TO_OPTION",
    "OPTION_TO_CHOICE",
    "EXTENSION_NAMESPACE",
    "MEASURED_EVENT_NAMES",
    "RUN_STATUS_VALUES",
    "STOP_UNCONFIRMED_REASON",
    "HermesRunTranslator",
    "TranslationTarget",
    "binding_namespace",
]
