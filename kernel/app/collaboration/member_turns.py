"""成员一轮的正文怎么摘、怎么截、怎么判重（PRD §B2，批次四十五 a）。

为什么单独一个模块
------------------
「这一轮说了什么」这件事有三条口径要定死，而它们都不需要 HTTP 也不需要数据库：

1. **正文口径**：Reducer 已经把一轮归并成一串卡片（``runtime.event_reducer``），
   所以「这一轮的正文」就是**归属这一轮的最后一条 assistant 消息**的文本——
   不是把这一轮所有消息拼起来（那会把中途的思考片段和最终结论混成一段），也不是
   「整条会话的最后一条 assistant 消息」（并发时会认错轮次）。取不到归属这一轮的，
   才退到整条时间线的最后一条 assistant 消息（PRD §B2 的括号那一句）。
2. **工具只给数**（PRD §B8 L1）。组时间线是**给人读一眼**的地方，一轮跑了十几个
   工具的入参与输出属于那条会话自己的页面；这里只留「跑了 N 个工具」。
3. **超长就截**：4000 字，且**必须标出来**（PRD §B7 L7）——一段被悄悄砍掉尾巴的
   正文，比一段明说「后面还有」的更危险。

判重（:func:`find_recorded_turn`）也放在这里：``(group_id, conversation_id,
run_id)`` 这个键是 PRD 定的，它的**实现**是在这个组已有的 ``member_turn`` 行里
按 ``metadata.runId`` 找——不给仓储加一张唯一索引，是因为这张表的键住在
``metadata`` 里（AD-153 定的：路由这一次做了什么是账，不是 Group 的结构），
加索引就得先把它抬成一列，而下一批（§B4 的 ``relay``）又会有自己的键。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

#: 正文上限（PRD §B1）。超过就截断并在 ``metadata.truncated`` 上标真。
MEMBER_TURN_MAX_CHARS: int = 4000

#: 被截断时补在末尾的一句。它是**正文的一部分**（读的人一眼看见），
#: ``metadata.truncated`` 是同一件事给机器看的那一面。
TRUNCATION_NOTICE: str = "……（这一轮的正文过长，组时间线只留前 4000 字）"

#: ``run.*`` 三个终态 → ``metadata.outcome``。``completed`` **不写这个键**：
#: 「正常结束」是默认情形，给它一个值只会让每一行都背一个恒等于同一个词的字段
#: （N §13.1：不适用就不出现这个键）。
RUN_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"run.completed", "run.failed", "run.interrupted"}
)

#: 事件类型 → outcome。
TURN_OUTCOMES: dict[str, str | None] = {
    "run.completed": None,
    "run.failed": "failed",
    "run.interrupted": "interrupted",
}


def turn_outcome(event_type: str) -> str | None:
    """终态事件类型 → ``metadata.outcome``（正常结束是 ``None``）。"""
    return TURN_OUTCOMES.get(event_type)


def _is_assistant_message(item: Any) -> bool:
    return item.kind == "message" and item.role == "assistant"


def summarize_turn(state: Any, run_id: str) -> tuple[str, int]:
    """从 Reducer 状态里摘出 ``(这一轮的正文, 这一轮跑了几个工具)``。

    ``state`` 是 :class:`runtime.event_reducer.TimelineState`（按属性取，不 import
    ——本模块因此可以拿一个最小假对象单测）。``None`` 表示 runtime 已经回收、
    渲染态没了：那时正文取不到，回 ``("", 0)``，由调用方决定要不要退到别的来源。
    """
    if state is None:
        return "", 0
    items = state.items
    of_run = [item for item in items if item.run_id == run_id]
    tool_count = sum(1 for item in of_run if item.kind == "tool")
    candidates = [item for item in of_run if _is_assistant_message(item)]
    if not candidates:
        # 这一轮一条 assistant 消息都没归上（有的引擎不给 ``runId``）→ 退到整条
        # 时间线的最后一条。退一步，但**说清楚退的是哪一步**：这是 PRD §B2
        # 「没有就取最后一条 assistant 文本」那一句。
        candidates = [item for item in items if _is_assistant_message(item)]
    if not candidates:
        return "", tool_count
    return candidates[-1].text, tool_count


def truncate_body(text: str) -> tuple[str, bool]:
    """按 :data:`MEMBER_TURN_MAX_CHARS` 截断；返回 ``(正文, 是否截过)``。"""
    if len(text) <= MEMBER_TURN_MAX_CHARS:
        return text, False
    return text[:MEMBER_TURN_MAX_CHARS] + TRUNCATION_NOTICE, True


def member_turn_metadata(
    *,
    run_id: str,
    tool_count: int,
    truncated: bool,
    outcome: str | None,
    epoch: int | None = None,
    round_number: int | None = None,
    speaker_index: int | None = None,
    delivered_since: int | None = None,
    delivered_to: int | None = None,
    passed: bool = False,
    final: bool = False,
    final_ignored: bool = False,
    phase: str | None = None,
    mentions: Sequence[str] = (),
) -> dict[str, Any]:
    """``member_turn`` 那一行的 ``metadata``（PRD §B1 / §B4 的键名）。

    ``truncated`` 为假、``outcome`` 为空时**都不出现这个键**——时间线上绝大多数
    行是「正常结束、没截断」，让它们各背两个恒假的字段只会让 wire 更难读。

    后面那七个是批次四十五 b 的房间循环记的账（PRD §B4「记录」）：``epoch`` /
    ``round`` / ``speakerIndex`` 说这一句发生在哪一次提问的第几轮第几位，
    ``deliveredSince`` / ``deliveredTo`` 说投给他的那份增量截的是时间线的哪一段
    ——**L8 要的就是这两个数**：有了它们，任何一条发言都能沿着 ``epoch`` 追回到
    起点那条用户消息，也能答出「他当时看见了什么」。

    **不属于房间循环的那一轮（用户自己在成员会话页里发的一句）一个键都不加**：
    它确实不属于任何一轮，给它编一个 ``round: 0`` 会让「第几轮」这个问题永远有
    一个假答案。

    ``mentions`` 是批次五十加的（裁决 B）：这一段话点到的成员 id，**没点到就不加
    这个键**。它是一条证据而不是一次路由——见下面那段注释。
    """
    metadata: dict[str, Any] = {"runId": run_id, "toolCount": tool_count}
    if truncated:
        metadata["truncated"] = True
    if outcome:
        metadata["outcome"] = outcome
    if epoch is not None:
        metadata["epoch"] = epoch
    if round_number is not None:
        metadata["round"] = round_number
    if speaker_index is not None:
        metadata["speakerIndex"] = speaker_index
    if delivered_since is not None:
        metadata["deliveredSince"] = delivered_since
    if delivered_to is not None:
        metadata["deliveredTo"] = delivered_to
    if passed:
        metadata["passed"] = True
    if final:
        metadata["final"] = True
    # 批次四十八（PRD §B4 终止 2）：**非组长**写了「最终答复」开头。那一行照常
    # 落、房间照转，只留一笔——不这么记的话，「我明明写了最终答复，房间为什么没停」
    # 就得靠读代码才答得出。
    if final_ignored:
        metadata["finalIgnored"] = True
    # 批次五十（裁决 B）：这一段话里点到的成员 id。**记的是「他点了谁」，不是
    # 「下一轮有谁」**——第五次修订之后成员的点名不进队列，于是时间线上必须看得出
    # 「评审点了打杂，但下一轮没有打杂」，否则「我明明 @ 了他」只能靠读代码回答。
    # 组长那条也记：它那一条的 `mentions` 正好就是下一轮队列的来源，对得上账。
    if mentions:
        metadata["mentions"] = list(mentions)
    # `phase` 只有收口那一轮有（讨论轮不背一个恒等于 "discussion" 的字段）。
    if phase:
        metadata["phase"] = phase
    return metadata


def find_recorded_turn(
    messages: Iterable[Any], *, conversation_id: str, run_id: str
) -> Any | None:
    """这个组里是不是已经记过这一轮了（幂等键 ``(group, conversation, run)``）。

    调用方只取本组 ``kind="member_turn"`` 的行喂进来，所以 ``group_id`` 那一维
    已经由取数收敛掉。重放同一条终态事件、两个订阅各触发一次、进程重启后补记——
    三种都会走到这里，返回已有那一行就是「什么都不做」。
    """
    for message in messages:
        if message.conversation_id != conversation_id:
            continue
        if message.metadata.get("runId") == run_id:
            return message
    return None


def run_started_at(rows: Sequence[Any], run_id: str) -> Any | None:
    """从重放缓冲里找这一轮的 ``run.started``，返回它的 ``created_at``（batch53）。

    ``rows`` 是按 ``sequence`` **升序**的 ``StoredEvent``；取的是落库那一刻
    （我们自己的钟），不是引擎给的 ``occurred_at`` —— 理由见
    :func:`membership_covers`：两个钟相减判不出「他那时在不在组里」。

    **从尾往前扫。** run id 现在是唯一的（AD-174：``run_token`` 由 Driver 在
    attach 时注入），所以方向本该无所谓；但账本里还躺着修复之前的**撞名历史
    数据** —— 后端一重启，续接同一条会话的第一轮又叫 ``<session>:r1``，和重启前
    那一轮同名。从头扫命中的是重启**前**那条（更旧），从尾扫命中的是**较新**
    那条，后者是两者中更接近真相的一个。

    这是对历史数据的**兜底**，不是主要防线：主要防线是 run id 本身不再撞。
    历史数据不迁移（AD-174），所以这道兜底要一直留着。

    找不到就是 ``None``——:func:`membership_covers` 对 ``None`` 判真。
    """
    for row in reversed(rows):
        envelope = row.envelope
        if envelope.event.type == "run.started" and envelope.run_id == run_id:
            return row.created_at
    return None


def membership_covers(
    *, joined_at: Any, left_at: Any, run_started_at: Any
) -> bool:
    """这一轮是不是发生在**当成员的那段时间**里（PRD §B2：``joined ≤ start < left``）。

    为什么按 ``run.started`` 而不是按终态时间判：一轮可能跑十分钟，用户在它跑的
    中途把成员移出组——那一轮是他**还在组里的时候**被要求做的事，它的答案属于这
    个组。反过来按终态判，同一轮会因为用户手快而消失。

    时间戳取不到（``run_started_at`` 为 ``None``）时**判真**：宁可多记一行可追溯
    的发言，也不要因为缺一个时间戳而静默丢掉成员的回答。
    """
    if run_started_at is None:
        return True
    if joined_at is not None and run_started_at < joined_at:
        return False
    if left_at is not None and run_started_at >= left_at:
        return False
    return True


def active_member_ids(members: Sequence[Any]) -> tuple[str, ...]:
    """仅供调用方省一行的小工具：还在参与的成员 id。"""
    return tuple(
        member.id
        for member in members
        if member.participation_state == "active"
    )


__all__ = [
    "MEMBER_TURN_MAX_CHARS",
    "RUN_TERMINAL_EVENT_TYPES",
    "TRUNCATION_NOTICE",
    "TURN_OUTCOMES",
    "active_member_ids",
    "find_recorded_turn",
    "member_turn_metadata",
    "membership_covers",
    "run_started_at",
    "summarize_turn",
    "truncate_body",
    "turn_outcome",
]
