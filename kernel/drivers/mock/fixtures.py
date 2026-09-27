"""可编程事件脚本（Mock Driver 的 fixture 语言）。

一个 :class:`MockScript` 是一串有序动作：

- :class:`EmitStep`：发一条公共事件（Envelope 的 eventId / sequence / occurredAt
  由 Driver 填，脚本只关心事件本体）；
- :class:`AwaitInteractionStep`：停在这里，直到调用方对指定 ``request_id``
  调用 ``resolve_interaction``——这就是 Permission / Question 的**请求-响应闭环**；
- :class:`HoldStep`：无限期挂起，直到 ``interrupt`` / ``stop_runtime``——
  用来构造「运行中被打断」的场景。

对应规范
--------
- N Phase 3A 验收：Mock Driver 与可控事件 fixture；Tool update 更新同一卡片；
  Permission/Question 有请求与响应闭环；断线重连可重放。
- N §13.4 Event 契约：Text delta 合并、Tool lifecycle、Permission/Question round
  trip、Cancel 与 terminal state、Error 收敛、Unknown native event 进 Generic Extension。
- N §7.2/§7.3：脚本只能产出公共事件；原生/实验信息走 ``extension.event``。
"""

from __future__ import annotations

from typing import Annotated, Literal, Mapping, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field

from runtime.event_envelope import (
    AgentError,
    AgentEvent,
    ArtifactCreated,
    ArtifactRef,
    AuthenticationRequest,
    AuthenticationRequested,
    DiagnosticNotice,
    ExtensionEvent,
    FileChanged,
    InteractionOption,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    PermissionRequest,
    PermissionRequested,
    PlanEntry,
    PlanUpdated,
    QuestionRequest,
    QuestionRequested,
    ReasoningDelta,
    ReasoningStatus,
    RunCompleted,
    RunFailed,
    RunStarted,
    SessionCreated,
    TerminalCompleted,
    TerminalStarted,
    TerminalUpdated,
    ToolCompleted,
    ToolStarted,
    ToolUpdated,
    UsageSnapshot,
    UsageUpdated,
)


class _StepModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class EmitStep(_StepModel):
    """发一条公共事件。

    ``run_id`` / ``parent_run_id``（AD-08）用来构造**子 run**：两者同时给出时，
    这条事件的信封头写成「我是 ``run_id`` 这个子 run，我由 ``parent_run_id``
    派生」。不给就沿用剧本当前的顶层 run。
    """

    kind: Literal["emit"] = "emit"
    event: AgentEvent
    run_id: str | None = None
    parent_run_id: str | None = None


class AwaitInteractionStep(_StepModel):
    """挂起，直到该 ``request_id`` 被 ``resolve_interaction`` 解决。"""

    kind: Literal["await_interaction"] = "await_interaction"
    request_id: str


class HoldStep(_StepModel):
    """无限期挂起，直到 ``interrupt`` / ``stop_runtime``。"""

    kind: Literal["hold"] = "hold"
    reason: str = "waiting for interrupt"


ScriptStep = Annotated[
    Union[EmitStep, AwaitInteractionStep, HoldStep], Field(discriminator="kind")
]


class MockScript(_StepModel):
    """一段可复用的事件剧本。"""

    name: str
    steps: tuple[ScriptStep, ...] = ()

    def then(self, *steps: ScriptStep) -> MockScript:
        return MockScript(name=self.name, steps=(*self.steps, *steps))


def _emit(*events: AgentEvent) -> tuple[EmitStep, ...]:
    return tuple(EmitStep(event=event) for event in events)


def _emit_child(
    *events: AgentEvent, run_id: str, parent_run_id: str
) -> tuple[EmitStep, ...]:
    """AD-08：把一串事件标成某个子 run 的事件。"""
    return tuple(
        EmitStep(event=event, run_id=run_id, parent_run_id=parent_run_id)
        for event in events
    )


# --------------------------------------------------------------------------- #
# 预置剧本
# --------------------------------------------------------------------------- #


def text_stream_script(
    *,
    run_id: str = "run-text",
    message_id: str = "msg-text",
    chunks: Sequence[str] = ("Hello", ", ", "world", "!"),
) -> MockScript:
    """N §13.4：Text delta 合并。"""
    return MockScript(
        name="text-stream",
        steps=(
            *_emit(RunStarted(run_id=run_id), MessageStarted(message_id=message_id)),
            *_emit(*(MessageDelta(message_id=message_id, text=c) for c in chunks)),
            *_emit(
                MessageCompleted(message_id=message_id, text="".join(chunks)),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def tool_lifecycle_script(
    *,
    run_id: str = "run-tool",
    call_id: str = "call-1",
    tool_name: str = "read_file",
    update_count: int = 3,
) -> MockScript:
    """N §7.3 规则 2 / N §13.4：tool start → 多次 update → complete，始终同一张卡。"""
    updates = tuple(
        ToolUpdated(call_id=call_id, progress={"step": index + 1, "of": update_count})
        for index in range(update_count)
    )
    return MockScript(
        name="tool-lifecycle",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                ToolStarted(call_id=call_id, name=tool_name, input={"path": "README.md"}),
            ),
            *_emit(*updates),
            *_emit(
                ToolCompleted(call_id=call_id, output={"bytes": 42}, is_error=False),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def streaming_tool_output_script(
    *,
    run_id: str = "run-tool-stream",
    call_id: str = "call-stream",
    tool_name: str = "run_shell",
    chunks: Sequence[str] = ("line-1\n", "line-2\n", "line-3\n"),
) -> MockScript:
    """AD-27：``tool.updated`` 的**增量**语义（分片 stdout）。

    这正是 AD-27 要解决的场景：一个长跑工具分片吐输出。在 ``cumulative``
    之前，每片都会整体替换上一片，卡片只剩最后一行。剧本最后再发一条
    ``cumulative=True`` 的全量快照，让契约测试能同时验证两种语义。
    """
    joined = "".join(chunks)
    return MockScript(
        name="streaming-tool-output",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                ToolStarted(call_id=call_id, name=tool_name, input={"cmd": "build"}),
            ),
            # 增量：默认 cumulative=False。
            *_emit(*(ToolUpdated(call_id=call_id, output=c) for c in chunks)),
            # 全量：Backend 重发「到目前为止的所有输出」，必须整体替换而不是再追加。
            *_emit(ToolUpdated(call_id=call_id, output=joined, cumulative=True)),
            *_emit(
                ToolCompleted(call_id=call_id, is_error=False),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def streaming_reasoning_script(
    *,
    run_id: str = "run-reasoning",
    message_id: str = "msg-reasoning",
    reasoning_chunks: Sequence[str] = ("先看输入，", "再想一步，", "得出结论。"),
    answer: str = "结论：可以。",
) -> MockScript:
    """AD-27：``reasoning.delta`` 的流式思考。

    ``reasoning.status`` 照发（语义不变，仍表示 Backend 明确给出的状态），
    思考原文走 ``reasoning.delta`` 累积到 ``messageId`` 对应的消息上——
    两条通道并存且互不覆盖，正是 AD-27 的分工。
    """
    return MockScript(
        name="streaming-reasoning",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                ReasoningStatus(status="thinking"),
                MessageStarted(message_id=message_id),
            ),
            *_emit(
                *(
                    ReasoningDelta(message_id=message_id, text=c)
                    for c in reasoning_chunks
                )
            ),
            *_emit(
                ReasoningStatus(status="done", summary="想好了"),
                MessageDelta(message_id=message_id, text=answer),
                MessageCompleted(message_id=message_id, text=answer),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def permission_roundtrip_script(
    *,
    run_id: str = "run-permission",
    call_id: str = "call-perm",
    request_id: str = "req-permission",
) -> MockScript:
    """N §13.4：Permission round trip。

    脚本在 :class:`AwaitInteractionStep` 停住；``resolve_interaction`` 会由
    Driver 发出 ``permission.resolved`` 并唤醒脚本，工具随后完成。
    """
    return MockScript(
        name="permission-roundtrip",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                ToolStarted(call_id=call_id, name="write_file", input={"path": "a.txt"}),
                PermissionRequested(
                    request=PermissionRequest(
                        request_id=request_id,
                        title="允许写入 a.txt？",
                        detail="Mock Driver 请求写入权限",
                        tool_call_id=call_id,
                        options=(
                            InteractionOption(option_id="allow", label="允许", kind="accept"),
                            InteractionOption(option_id="deny", label="拒绝", kind="reject"),
                        ),
                    )
                ),
            ),
            AwaitInteractionStep(request_id=request_id),
            *_emit(
                ToolCompleted(call_id=call_id, output={"written": True}, is_error=False),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def question_roundtrip_script(
    *,
    run_id: str = "run-question",
    request_id: str = "req-question",
    message_id: str = "msg-question",
) -> MockScript:
    """N §13.4：Question round trip。"""
    return MockScript(
        name="question-roundtrip",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                QuestionRequested(
                    request=QuestionRequest(
                        request_id=request_id,
                        prompt="要用哪个分支？",
                        options=(
                            InteractionOption(option_id="main", label="main"),
                            InteractionOption(option_id="dev", label="dev"),
                        ),
                        allow_free_text=True,
                    )
                ),
            ),
            AwaitInteractionStep(request_id=request_id),
            *_emit(
                MessageStarted(message_id=message_id),
                MessageDelta(message_id=message_id, text="收到，使用所选分支。"),
                MessageCompleted(message_id=message_id, text="收到，使用所选分支。"),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def authentication_roundtrip_script(
    *,
    run_id: str = "run-authentication",
    request_id: str = "req-authentication",
    message_id: str = "msg-authentication",
) -> MockScript:
    """AD-08：Authentication round trip。

    与 Permission / Question 完全同形：请求 → 挂起 → ``resolve_interaction``
    发出 ``authentication.resolved`` 唤醒剧本 → 继续。这正是 AD-08 之前缺的
    那一半（认证卡以前只能靠 ``extension.event`` 收尾）。
    """
    return MockScript(
        name="authentication-roundtrip",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                AuthenticationRequested(
                    request=AuthenticationRequest(
                        request_id=request_id,
                        message="需要先完成认证才能继续",
                        methods=("device-code", "api-key"),
                    )
                ),
            ),
            AwaitInteractionStep(request_id=request_id),
            *_emit(
                MessageStarted(message_id=message_id),
                MessageDelta(message_id=message_id, text="认证完成，继续执行。"),
                MessageCompleted(message_id=message_id, text="认证完成，继续执行。"),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def delegated_run_script(
    *,
    run_id: str = "run-parent",
    child_run_id: str = "run-child",
    message_id: str = "msg-parent",
    child_message_id: str = "msg-child",
) -> MockScript:
    """AD-08：委派 / 子 run。

    父 run 派生一个子 run，子 run 的每条事件都带 ``parentRunId``。关键断言点：
    子 run 的 ``run.completed`` **不得**把父 run 判成已完成——父 run 自己发完
    ``run.completed`` 这一轮才算结束。
    """
    return MockScript(
        name="delegated-run",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                MessageStarted(message_id=message_id),
                MessageDelta(message_id=message_id, text="这一步交给子任务。"),
            ),
            *_emit_child(
                RunStarted(run_id=child_run_id),
                MessageStarted(message_id=child_message_id),
                MessageDelta(message_id=child_message_id, text="子任务已处理。"),
                MessageCompleted(message_id=child_message_id, text="子任务已处理。"),
                RunCompleted(run_id=child_run_id),
                run_id=child_run_id,
                parent_run_id=run_id,
            ),
            *_emit(
                MessageCompleted(message_id=message_id, text="这一步交给子任务。已回收结果。"),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def hold_script(
    *, run_id: str = "run-hold", message_id: str = "msg-hold"
) -> MockScript:
    """N §13.4：Cancel 与 terminal state —— 运行到一半挂起，等待 interrupt。"""
    return MockScript(
        name="hold",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                MessageStarted(message_id=message_id),
                MessageDelta(message_id=message_id, text="正在长时间工作"),
            ),
            HoldStep(),
        ),
    )


def failure_script(
    *,
    run_id: str = "run-failure",
    code: str = "mock.boom",
    message: str = "Mock Driver 故意失败",
) -> MockScript:
    """N §13.4：Error 收敛。"""
    return MockScript(
        name="failure",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                DiagnosticNotice(level="warning", message="即将失败"),
                RunFailed(
                    run_id=run_id,
                    error=AgentError(code=code, message=message, retriable=False),
                ),
            ),
        ),
    )


def extension_event_script(
    *,
    run_id: str = "run-extension",
    namespace: str = "mock",
    name: str = "native.unknown",
) -> MockScript:
    """N §7.3 规则 7 / N §13.4：未知原生事件必须能进 Generic Extension，不污染公共 union。"""
    return MockScript(
        name="extension-event",
        steps=(
            *_emit(
                RunStarted(run_id=run_id),
                ExtensionEvent(
                    namespace=namespace, name=name, data={"rawShape": "driver-private"}
                ),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


def external_http_shaped_script(
    *, run_id: str = "run_external_1", stream: Sequence[Mapping[str, object]] | None = None
) -> MockScript:
    """AD-32：把一条**外部 HTTP/SSE 事件流**原样翻成公共事件后播放。

    剧本本身没有手写任何事件——它由
    :class:`~drivers.mock.external_shapes.ExternalHttpStreamTranslator` 从
    :data:`~drivers.mock.external_shapes.EXTERNAL_HTTP_SAMPLE_STREAM` 生成，
    因此「MockDriver 能播出来」本身就是「这条外部流能无损映射进 v1.1」的证据。

    唯一由 Driver **合成**的事件是开头的 ``run.started``：外部流没有开始事件
    （run id 来自 ``POST`` 的响应体，不在 SSE 里），Driver 拿到 run id 就该开卡。
    合成一条开始事件不损失任何信息，也不需要公共层新增字段。
    """
    from drivers.mock.external_shapes import (
        EXTERNAL_HTTP_SAMPLE_STREAM,
        ExternalHttpStreamTranslator,
    )

    payloads = EXTERNAL_HTTP_SAMPLE_STREAM if stream is None else tuple(stream)
    translated = ExternalHttpStreamTranslator().translate_stream(payloads)  # type: ignore[arg-type]
    return MockScript(
        name="external-http-shaped",
        steps=(*_emit(RunStarted(run_id=run_id)), *_emit(*translated)),
    )


def full_lifecycle_script(
    *,
    run_id: str = "run-full",
    session_id: str = "native-session-full",
    message_id: str = "msg-full",
    call_id: str = "call-full",
    terminal_id: str = "term-full",
    permission_request_id: str = "req-full-permission",
    question_request_id: str = "req-full-question",
    authentication_request_id: str = "req-full-authentication",
) -> MockScript:
    """一条覆盖 N §8.1 全部卡片种类的完整生命周期剧本。

    AD-08 起也包含认证请求-响应闭环（认证卡此前没有 resolved 事件可发）。
    """
    return MockScript(
        name="full-lifecycle",
        steps=(
            *_emit(
                SessionCreated(session_id=session_id),
                RunStarted(run_id=run_id),
                ReasoningStatus(status="thinking", summary="正在规划"),
                PlanUpdated(
                    plan_id="plan-full",
                    entries=(
                        PlanEntry(entry_id="p1", content="读取文件", status="in_progress"),
                        PlanEntry(entry_id="p2", content="写回结果", status="pending"),
                    ),
                ),
                MessageStarted(message_id=message_id),
                MessageDelta(message_id=message_id, text="先看一下文件"),
                ToolStarted(call_id=call_id, name="read_file", input={"path": "a.txt"}),
                ToolUpdated(call_id=call_id, progress={"pct": 50}),
                ToolCompleted(call_id=call_id, output={"lines": 3}, is_error=False),
                TerminalStarted(terminal_id=terminal_id, command="echo hi"),
                TerminalUpdated(terminal_id=terminal_id, output="hi\n", cumulative=False),
                TerminalCompleted(terminal_id=terminal_id, exit_code=0),
                PermissionRequested(
                    request=PermissionRequest(
                        request_id=permission_request_id,
                        title="允许写入 a.txt？",
                        options=(
                            InteractionOption(option_id="allow", label="允许"),
                            InteractionOption(option_id="deny", label="拒绝"),
                        ),
                    )
                ),
            ),
            AwaitInteractionStep(request_id=permission_request_id),
            *_emit(
                FileChanged(path="a.txt", diff="@@ -1 +1 @@", operation="modified"),
                ArtifactCreated(
                    artifact=ArtifactRef(
                        artifact_id="artifact-full", kind="file", title="a.txt"
                    )
                ),
                QuestionRequested(
                    request=QuestionRequest(
                        request_id=question_request_id,
                        prompt="还需要我做别的吗？",
                        allow_free_text=True,
                    )
                ),
            ),
            AwaitInteractionStep(request_id=question_request_id),
            *_emit(
                AuthenticationRequested(
                    request=AuthenticationRequest(
                        request_id=authentication_request_id,
                        message="需要认证后才能提交结果",
                        methods=("device-code",),
                    )
                ),
            ),
            AwaitInteractionStep(request_id=authentication_request_id),
            *_emit(
                MessageDelta(message_id=message_id, text="，已完成。"),
                MessageCompleted(message_id=message_id, text="先看一下文件，已完成。"),
                UsageUpdated(
                    usage=UsageSnapshot(input_tokens=120, output_tokens=40, total_tokens=160)
                ),
                RunCompleted(run_id=run_id),
            ),
        ),
    )


DEFAULT_SCRIPT_FACTORY = full_lifecycle_script

__all__ = [
    "AwaitInteractionStep",
    "DEFAULT_SCRIPT_FACTORY",
    "EmitStep",
    "HoldStep",
    "MockScript",
    "ScriptStep",
    "authentication_roundtrip_script",
    "delegated_run_script",
    "extension_event_script",
    "external_http_shaped_script",
    "failure_script",
    "full_lifecycle_script",
    "hold_script",
    "permission_roundtrip_script",
    "question_roundtrip_script",
    "streaming_reasoning_script",
    "streaming_tool_output_script",
    "text_stream_script",
    "tool_lifecycle_script",
]
