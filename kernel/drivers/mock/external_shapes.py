"""外部事件形状 → 公共事件的映射示例（AD-32 / AD-33 的实测形状）。

为什么在 ``drivers/mock/`` 里
----------------------------
这是**夹具**，不是公共层的一部分：它演示「一个真实 Backend 的原生 SSE 事件流
可以无损翻译成 N §7.2 的公共 union」，用来在 3B 真正接 Driver 之前，先把
Envelope v1.1 的表达力钉死。公共层（``app/`` / ``runtime/`` / ``drivers/base.py``）
里不会、也不得出现任何一家的事件名——那正是 N §3 的核心约束。

形状来源
--------
- **事件名是实测的**：``docs/probes/2026-09-02-run3-live.md`` 的 live 探针记录了
  某个外部 HTTP + SSE 后端的五个事件名（见 :data:`EXTERNAL_EVENT_NAMES`），
  AD-32 据此定下了 Phase 3B 的映射方向。
- **字段名取自同一份探针里抓到的 SSE 报文**（``run_id`` / ``delta`` / ``tool`` /
  ``preview`` / ``duration`` / ``error`` / ``text`` / ``timestamp``）。
- **未验证**：探针抓到的样本流被截断在最后一条 ``reasoning.available``，
  ``run.completed`` 的**报文字段**没有实测样本；这里按同族事件的形状取
  ``{event, run_id, timestamp}``，仅作为夹具，不作为事实。

映射里最值得记的三件事
----------------------
1. **外部流没有消息 ID**：``message.delta`` 只带 ``run_id`` 和 ``delta``。
   N §7.3 规则 1 要求可增量更新的对象有稳定 ID，所以 Driver 必须**合成**一个
   （这里是 ``<runId>:message``）。合成规则必须确定、可重复，否则重连后 delta
   会落到新卡上。
2. **外部流没有工具调用 ID**：``tool.started`` / ``tool.completed`` 只带工具名。
   Driver 按「同名工具的未闭合调用」配对并合成 ``callId``，否则同一轮里两次
   调用同一个工具就会挤成一张卡。
3. **``reasoning.available`` 带全文**：AD-32 说「若含增量文本则 ``reasoning.delta``」。
   带 ``text`` 就翻成 :class:`~runtime.event_envelope.ReasoningDelta`（AD-27 新增，
   累积到对应消息的思考区）；不带就退回 :class:`~runtime.event_envelope.ReasoningStatus`。
   没有 AD-27 的这一条，思考原文只能塞进 ``summary`` 被反复覆盖。

没有公共归宿的原生字段（:data:`UNMAPPED_EXTERNAL_FIELDS`）走信封头或
``extension.event``，绝不新增公共字段——N §7.3 规则 7。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from runtime.event_envelope import (
    AgentEvent,
    MessageDelta,
    ReasoningDelta,
    ReasoningStatus,
    RunCompleted,
    ToolCompleted,
    ToolStarted,
)

#: live 探针实测到的外部 SSE 事件名（AD-32）。
EXTERNAL_EVENT_NAMES: tuple[str, ...] = (
    "tool.started",
    "tool.completed",
    "message.delta",
    "reasoning.available",
    "run.completed",
)

#: 外部事件名 → 它可能翻成的公共事件类型（AD-32 的映射方向）。
#: ``reasoning.available`` 是唯一一条二选一：带增量文本走 ``reasoning.delta``，
#: 否则退回 ``reasoning.status``。其余都是一对一。
EXTERNAL_EVENT_MAP: Mapping[str, tuple[str, ...]] = {
    "tool.started": ("tool.started",),
    "tool.completed": ("tool.completed",),
    "message.delta": ("message.delta",),
    "reasoning.available": ("reasoning.delta", "reasoning.status"),
    "run.completed": ("run.completed",),
}

#: 原生报文里没有公共归宿的字段。它们不是「丢了」：``timestamp`` 进信封头的
#: ``occurredAt``，其余需要时走 ``extension.event``（N §7.3 规则 7）。
UNMAPPED_EXTERNAL_FIELDS: frozenset[str] = frozenset({"timestamp", "duration"})


def synthetic_message_id(run_id: str) -> str:
    """外部流不带消息 ID → 按 run 合成一个确定的 ID（N §7.3 规则 1）。"""
    return f"{run_id}:message"


def synthetic_call_id(run_id: str, tool: str, ordinal: int) -> str:
    """外部流不带工具调用 ID → 按「run + 工具名 + 第几次」合成。"""
    return f"{run_id}:tool:{tool}:{ordinal}"


class ExternalHttpStreamTranslator:
    """把一条外部 HTTP/SSE 事件流翻成公共事件。

    有状态，因为合成 ID 需要记住「这一轮里某个工具已经开过几次、哪次还没闭合」。
    每条外部事件产出**恰好一条**公共事件——这就是「无损」在事件粒度上的含义：
    不合并、不丢弃、不额外发明。
    """

    def __init__(self) -> None:
        #: 工具名 → 已开始的次数（用于合成 ordinal）。
        self._tool_counts: dict[str, int] = {}
        #: 工具名 → 尚未闭合的 callId 栈。
        self._open_calls: dict[str, list[str]] = {}

    def translate(self, payload: Mapping[str, Any]) -> AgentEvent:
        name = payload["event"]
        run_id = payload["run_id"]
        if name == "message.delta":
            return MessageDelta(
                message_id=synthetic_message_id(run_id), text=payload["delta"]
            )
        if name == "reasoning.available":
            text = payload.get("text")
            if text:
                # AD-32 + AD-27：带增量文本 → 思考区，不再挤进 summary。
                return ReasoningDelta(
                    message_id=synthetic_message_id(run_id), text=text
                )
            return ReasoningStatus(status="available")
        if name == "tool.started":
            tool = payload["tool"]
            ordinal = self._tool_counts.get(tool, 0)
            self._tool_counts[tool] = ordinal + 1
            call_id = synthetic_call_id(run_id, tool, ordinal)
            self._open_calls.setdefault(tool, []).append(call_id)
            preview = payload.get("preview")
            return ToolStarted(
                call_id=call_id,
                name=tool,
                input={"preview": preview} if preview is not None else None,
            )
        if name == "tool.completed":
            tool = payload["tool"]
            open_calls = self._open_calls.get(tool) or []
            # 没见过 started（中途接流）时也要给出确定的 callId，不能崩。
            call_id = (
                open_calls.pop(0)
                if open_calls
                else synthetic_call_id(run_id, tool, self._tool_counts.get(tool, 0))
            )
            return ToolCompleted(call_id=call_id, is_error=bool(payload.get("error")))
        if name == "run.completed":
            return RunCompleted(run_id=run_id)
        raise KeyError(f"未知的外部事件名：{name!r}（应走 extension.event）")

    def translate_stream(
        self, payloads: Sequence[Mapping[str, Any]]
    ) -> tuple[AgentEvent, ...]:
        return tuple(self.translate(payload) for payload in payloads)


#: 一条外部流样本：形状照抄探针报文，内容换成中性内容（探针里的标记串不搬进代码）。
#: 顺序也照抄实测：工具先跑完，正文流式吐出，最后才来思考文本。
EXTERNAL_HTTP_SAMPLE_STREAM: tuple[Mapping[str, Any], ...] = (
    {
        "event": "tool.started",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_749.557,
        "tool": "terminal",
        "preview": "make build",
    },
    {
        "event": "tool.completed",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_749.728,
        "tool": "terminal",
        "duration": 0.17,
        "error": False,
    },
    {
        "event": "message.delta",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_750.877,
        "delta": "BUILD",
    },
    {
        "event": "message.delta",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_750.878,
        "delta": "-",
    },
    {
        "event": "message.delta",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_750.879,
        "delta": "OK",
    },
    {
        "event": "reasoning.available",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_750.883,
        "text": "先跑构建，再看退出码。",
    },
    {
        "event": "run.completed",
        "run_id": "run_external_1",
        "timestamp": 1_788_339_750.889,
    },
)


__all__ = [
    "EXTERNAL_EVENT_MAP",
    "EXTERNAL_EVENT_NAMES",
    "EXTERNAL_HTTP_SAMPLE_STREAM",
    "ExternalHttpStreamTranslator",
    "UNMAPPED_EXTERNAL_FIELDS",
    "synthetic_call_id",
    "synthetic_message_id",
]
