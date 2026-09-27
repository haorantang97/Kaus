"""CollaborationSession / CollaborationMember 领域模型。

职责
----
表达临时协作 Group：一个容器 + 一组成员，成员的唯一抽象是 ``conversation_id``。

对应规范
--------
- N §9.1：Group 内部最终只保存 ``conversation_id``；不保存游离的「Agent 进程」，
  也不建立第二套 Group 专属 Agent 模型。
- N §9.7 推荐数据模型：

      CollaborationSession: id / title / home_project_id / status / created_at /
                            closed_at / context_policy
      CollaborationMember:  id / collaboration_session_id / conversation_id /
                            join_mode / role_label / joined_at / left_at /
                            participation_state / isolation_mode /
                            worktree_or_runtime_ref

- N §9.7：``CollaborationMember.conversation_id`` 在成员加入完成后必须非空
  （即使 Native Session 懒创建，Conversation 也必须先成为规范对象）→ 类型层为必填。
- N §9.4：成员可动态加入 / 暂停 / 移除 / 重新加入；移除后原 Conversation 与
  Native Session 不被删除。
- N §9.6：``isolation_mode`` ∈ shared_read_only | shared_workspace | git_worktree |
  backend_managed；默认不应让多个写入型成员无提示地同时写同一目录。
- v1.0 §4.6 / C-6：``home_project_id`` 可空仅为表达能力预留，不构成「允许跨 Project」
  的产品裁决。
- N §9.2：同一个 AgentBinding 可以在一个 Group 中同时启动多个 Conversation，
  因此成员集合的唯一性只按 ``conversation_id`` 判定，不按 binding 判定。

未由规范枚举、在此补齐的取值见收尾报告未决问题（``participation_state``）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal, Self, Sequence

from pydantic import Field, model_validator

from app.base import DomainModel
from app.errors import DomainInvariantError
from app.ids import (
    CollaborationId,
    CollaborationMessageId,
    ConversationId,
    MemberId,
    ProjectId,
    collaboration_id,
    collaboration_member_id,
    collaboration_message_id,
)

CollaborationStatus = Literal["active", "minimized", "archived", "closed"]
"""v1.0 §4.6 的 status 取值 + N §9.7 的 closed_at 生命周期。"""

JoinMode = Literal["existing", "spawned_in_group"]
"""N §9.7：两条加入路径最终都落到同一个 CollaborationMember（N §9.3）。"""

ParticipationState = Literal["active", "paused", "left", "failed"]
"""N §9.4 要求「暂停 / 移除 / 重新加入 / 失败」四种成员状态可表达；
N 未给出字段枚举，此处按 §9.4 的动词补齐（见收尾报告未决问题）。"""

IsolationMode = Literal[
    "shared_read_only", "shared_workspace", "git_worktree", "backend_managed"
]
"""N §9.6。"""

ThreadStatus = Literal["idle", "running", "closing", "stopped", "final", "exhausted"]
"""房间循环里一条线程的状态（PRD §B4，批次四十五 b；``closing`` 是四十六加的）。

- ``idle``：没在转（也包括「一轮内全员略过」与「没有可收口的成员」之后的安静）；
- ``running``：正在按队列一个一个投；
- ``closing``：**收口轮**——到达安全上限了，正在等**组长**把讨论收成一句答复；
- ``stopped``：用户按了「停」（或打字说了「停」）、收口没成功、后端重启收尾；
- ``final``：组长收了口（以「最终答复」开头，或读完全场之后没再点任何人）；
- ``exhausted``：批次四十五 b 的「轮数到顶」终态。**四十六起不再写入**：轮数到顶
  改成强制收口（`closing` → `final`），这个取值只为读得懂旧数据而留着。

``running`` 与 ``closing`` 都还在等人，其余是终态。分这么多取值是因为**为什么停的**
决定了界面该说什么话：``final`` 要高亮一张卡，``closing`` 要在组头说「正在收口」，
``stopped`` 是用户自己按的（什么都不必说），``idle`` 是大家都说完了或都没话说。
"""

#: 线程的两个阶段（PRD §B4「收口」）。``closing`` 那一轮**不计入 ``round``**——
#: 它不是讨论的一轮，是把讨论收成答案的一次动作。
ThreadPhase = Literal["discussion", "closing"]

#: 线程**为什么走到现在这个状态**（PRD §B4 的终止梯子，2026-09-18 第四次修订）。
#: 落在 ``thread.endedReason`` 上，接入层据它决定时间线上写哪一句人话——没有它的话，
#: 「组长没再点人」与「一轮里没人说话」在库里长得一模一样，而它们在界面上是两句
#: 不同的话。
ThreadEndedReason = Literal[
    "task_completed", "waiting_user", "call_cap", "time_limit", "protocol_error",
    "member_failed", "configuration_changed",
    "leader_closed",  # 一轮结束，没有任何人被点名（终止 3）：组长那一段就是结论
    "leader_final",  # 组长以「最终答复」开头（终止 2）
    "leader_failed",  # 组长未完成执行，停止讨论，失败不能当成结论
    "round_cap",  # 到达安全上限（终止 4）：单独投一条让组长收口
    "silent",  # 一轮内没有一个人说出话（终止 5）
    "leader_missing",  # 没有可收口的成员（终止 6）
    "closed",  # 触顶那一次收口回来了
    "closing_failed",  # 收口那一轮投递失败 / 跑失败，不重试
]

#: 兜底轮数的默认值与上下限（PRD §A3 / §B4，2026-09-14 第二次修订）。
#:
#: **6 → 12 是这一批改的**：产品方真机一试就打穿了第一版——「让它们互相介绍一下
#: 自己」聊满了 6 轮。他的判断是对的：6 是一个拍脑袋的数，简单问题嫌多、复杂问题
#: 不见得该停。所以轮数在这一批**降格成安全阀**（只防成本失控，不是「预期停止
#: 点」）。既然它不再是停止点，默认值就该调到「正常讨论碰不到」的高度，上限也随之
#: 放宽到 50。批次四十八起预期的停止点是**组长读完全场之后不再点人**（终止 3）。
DEFAULT_ROUND_CAP: int = 12
MIN_ROUND_CAP: int = 1
MAX_ROUND_CAP: int = 50

#: ``CollaborationSession.settings`` 里存兜底轮数的键（wire 上就叫这个名字）。
ROUND_CAP_KEY: str = "roundCap"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class RoomThread(DomainModel):
    """一条**房间线程**的运行态（PRD §B4）。

    一条线程 = 用户说了一句话之后房间自己转的那一段。用户每发一条就 ``epoch += 1``
    开一条新线程（旧的若还在转，先收成 ``stopped``）——所以「这句成员发言属于哪一次
    提问」永远答得出来，那正是 AD-168 接过 AD-153 的那条底线。

    为什么把它做成一个**值对象**而不是几个散落在 ``settings`` 里的键：这些字段必须
    **一起**变（推进一位 = ``speaker_index`` 加一 + ``awaiting_member_id`` 换人 +
    计数器动），分开存就会出现「下标已经前进、等的人还是上一个」这种半截状态，而它
    在一条串行队列上正好意味着房间卡死。

    ``speaker_index`` 与 ``awaiting_member_id`` 看起来重复，其实各答一个问题：前者是
    「队列走到哪了」（推进用），后者是「现在等谁」（旁观者收到一条 ``run.completed``
    时用来判「这是不是我在等的那个人」）。只留下标的话，一个**不属于本线程**的成员
    跑完一轮也会把队列推一格。
    """

    epoch: int = Field(default=0, ge=0)
    #: 第几轮（从 1 起；``idle`` 时是 0）。
    round: int = Field(default=0, ge=0)
    status: ThreadStatus = "idle"
    #: 这一轮按顺序该说话的成员（按加入先后）。
    speaker_queue: tuple[str, ...] = ()
    #: 不排队但每轮收增量的成员（PRD §B4：否则「A、B 辩论完 C 来总结」C 什么都不知道）。
    spectators: tuple[str, ...] = ()
    #: 队列里下一个该说话的人的下标。
    speaker_index: int = Field(default=0, ge=0)
    #: 现在在等谁说完；``None`` = 没在等任何人。
    awaiting_member_id: str | None = None
    #: 开这条线程的那条用户消息（L8：任何一条发言都追得到 ``epoch`` 的起点）。
    started_by_message_id: str | None = None
    #: 本轮已经开口的人数，以及其中回「（略过）」的人数（全员略过 → 停）。
    spoke_in_round: int = Field(default=0, ge=0)
    passed_in_round: int = Field(default=0, ge=0)
    #: 讨论阶段还是收口阶段。收口那一轮**不计入 ``round``**，而且只有一条路走得到
    #: 它：到达安全上限（PRD §B4 终止 4）。
    phase: ThreadPhase = "discussion"
    #: 走到现在这个状态的原因（见 :data:`ThreadEndedReason`）。还在转时是 ``None``。
    ended_reason: ThreadEndedReason | None = None
    coordination: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def is_awaiting(self) -> bool:
        """还在等人说话吗（讨论中或收口中）。

        ``closing`` 与 ``running`` 对「要不要继续投」是同一个答案，对「界面上说什么
        话」是两个答案——所以状态分开两个取值，判断收在这一个属性里，免得接入层每处
        都写 ``status in ("running", "closing")`` 而漏掉其中一处。
        """
        return self.status in ("running", "closing")


class CollaborationSession(DomainModel):
    """临时协作 Group 容器（N §9.7）。"""

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"id"})

    id: CollaborationId
    title: str = Field(min_length=1)
    #: C-6：可空仅为表达能力预留；同项目 Group 优先（N §9.9）。
    home_project_id: ProjectId | None = None
    status: CollaborationStatus = "active"
    #: N §9.4：新成员只获得按 Context Policy 生成的 Context Packet，
    #: 不自动获得所有成员的完整私有历史。具体策略在 Phase 7 定义，这里保持 opaque。
    context_policy: dict[str, Any] = Field(default_factory=dict)
    #: Group 级设置（PRD §B1 / §B4）。眼下只有一个键 ``roundCap``——开成一个开放的
    #: JSON 而不是一列 ``round_cap``，是因为 §B1 还列着 ``coordinatorMemberId`` 之类
    #: 尚未启用的设置，而它们每加一个就改一次表并不划算。**只放设置，不放运行态**：
    #: 后者住在 :attr:`thread` 上（它有自己的不变量，见 :class:`RoomThread`）。
    settings: dict[str, Any] = Field(default_factory=dict)
    #: 当前那条房间线程（PRD §B4）。从没转过就是 ``None``——与「转过、现在停了」
    #: 是两句话（N §13.1）。
    thread: RoomThread | None = None
    #: **组长**（PRD §A6 / §B6，批次四十八）：这个组里那位「读完全场再说话」的成员。
    #:
    #: 它不是新的一种 agent，是成员上的一个标记——所以住在组上而不是成员行上：
    #: 「谁是组长」是**组**的一个事实（有且只有一位），记在成员行上就得靠「只有一行
    #: 的 is_leader 为真」这条没人守得住的不变量。
    #:
    #: 默认是**最早加入的那位** active 成员（加入时自动设，见接入层）；组长暂停 /
    #: 离开时自动移交给下一位最早加入的 active 成员；一个 active 成员都没有时是
    #: ``None``——那时房间没有可收口的人（终止 6），如实说，不硬找一个。
    leader_member_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    closed_at: datetime | None = None

    @property
    def round_cap(self) -> int:
        """安全阀轮数：读 ``settings.roundCap``，读不到 / 不合法就是默认 12。

        钳在 :data:`MIN_ROUND_CAP`..:data:`MAX_ROUND_CAP` 之间而不是信库里那个数：
        这个值直接乘以队列长度就是引擎调用次数，一条脏数据在这里的后果是账单。
        """
        raw = (self.settings or {}).get(ROUND_CAP_KEY)
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_ROUND_CAP
        return max(MIN_ROUND_CAP, min(MAX_ROUND_CAP, value))

    @model_validator(mode="after")
    def _check_closed(self) -> Self:
        if self.status == "closed" and self.closed_at is None:
            raise ValueError("status=closed 必须带 closed_at（N §9.7）")
        if self.status != "closed" and self.closed_at is not None:
            raise ValueError("只有 status=closed 才能带 closed_at（N §9.7）")
        return self

    @classmethod
    def create(
        cls,
        *,
        title: str,
        home_project_id: str | None = None,
        context_policy: dict[str, Any] | None = None,
        collaboration_uuid: str | None = None,
        created_at: datetime | None = None,
    ) -> CollaborationSession:
        timestamp = created_at or _now()
        return cls(
            id=collaboration_id(collaboration_uuid),
            title=title,
            home_project_id=home_project_id,
            context_policy=dict(context_policy or {}),
            created_at=timestamp,
            updated_at=timestamp,
        )

    def close(self, *, at: datetime | None = None) -> CollaborationSession:
        timestamp = at or _now()
        return self.evolve(status="closed", closed_at=timestamp, updated_at=timestamp)


class CollaborationMember(DomainModel):
    """Group 成员。唯一成员抽象是 ``conversation_id``（N §9.1）。"""

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"id", "collaboration_session_id", "conversation_id", "source_conversation_id", "join_mode"}
    )

    id: MemberId
    collaboration_session_id: CollaborationId
    #: N §9.7：加入完成后必须非空 —— 类型层直接设为必填。
    conversation_id: ConversationId
    source_conversation_id: ConversationId | None = None
    join_mode: JoinMode
    role_label: str | None = None
    participation_state: ParticipationState = "active"
    isolation_mode: IsolationMode = "shared_read_only"
    #: N §9.6：Group UI 必须显示每个成员当前 Workspace/Worktree。具体取值由
    #: 上层（worktree 路径 / Backend sandbox 句柄）填写，公共层不解释。
    worktree_or_runtime_ref: str | None = None
    joined_at: datetime = Field(default_factory=_now)
    left_at: datetime | None = None
    #: 这个成员最近一次收到的房间增量截到组时间线的哪一条（PRD §B4）。
    #: ``None`` = 还没给他投过任何东西，那时增量从线程起点算。
    #:
    #: 记在**成员**上而不是线程上，是因为旁观者与发言人的进度天然不同步：C 旁观了
    #: 两轮之后被点名，他要补的是「上次投给我之后」的全部，不是「这一轮的」。
    last_delivered_sequence: int | None = None

    @model_validator(mode="after")
    def _check_left(self) -> Self:
        if self.participation_state == "left" and self.left_at is None:
            raise ValueError("participation_state=left 必须带 left_at（N §9.4）")
        if self.participation_state != "left" and self.left_at is not None:
            raise ValueError("只有 left 状态才能带 left_at（N §9.4）")
        return self

    @classmethod
    def create(
        cls,
        *,
        collaboration_session_id: str,
        conversation_id: str,
        join_mode: JoinMode,
        source_conversation_id: str | None = None,
        role_label: str | None = None,
        isolation_mode: IsolationMode = "shared_read_only",
        worktree_or_runtime_ref: str | None = None,
        member_uuid: str | None = None,
        joined_at: datetime | None = None,
    ) -> CollaborationMember:
        return cls(
            id=collaboration_member_id(member_uuid),
            collaboration_session_id=collaboration_session_id,
            conversation_id=conversation_id,
            join_mode=join_mode,
            source_conversation_id=source_conversation_id,
            role_label=role_label,
            isolation_mode=isolation_mode,
            worktree_or_runtime_ref=worktree_or_runtime_ref,
            joined_at=joined_at or _now(),
        )

    def pause(self) -> CollaborationMember:
        return self.evolve(participation_state="paused")

    def resume(self) -> CollaborationMember:
        """N §9.4：成员可以稍后重新加入。"""
        return self.evolve(participation_state="active", left_at=None)

    def leave(self, *, at: datetime | None = None) -> CollaborationMember:
        """N §9.4：成员移除后原 Conversation 与 Native Session 不被删除。"""
        return self.evolve(participation_state="left", left_at=at or _now())

    @property
    def is_writer_candidate(self) -> bool:
        """N §9.6：只读成员可共享目录；写入型成员需要隔离。"""
        return self.isolation_mode != "shared_read_only"


CollaborationMessageKind = Literal[
    "message",
    "broadcast",
    "directed",
    "member_turn",
    "context_packet",
    "writeback",
    "system",
]
"""AD-13 / N §9.8：Group 自己的时间线上出现的条目种类。

- ``message``：泛指的一条组内消息（骨架期的取值，仍然合法）；
- ``broadcast``：用户发给**组内全体**活跃成员的一条（AD-153）；
- ``directed``：用户发给**点名的**若干成员的一条（AD-153）；
- ``member_turn``：某个成员**一轮的最终正文**（PRD §B2，批次四十五 a）；
- ``context_packet``：按 Context Policy 生成的上下文包（N §9.4 / AD-154）；
- ``writeback``：显式写回项目的结论（N §9.8 ``/writebacks``）；
- ``system``：成员加入/离开等 Group 自身的事件。

``broadcast`` / ``directed`` 是批次二十六加的：AD-153 定下「用户就是协调者」，
路由**只有这两种**，且都由用户发起。它们不是 ``message`` 的两个子标签而是两个
独立取值，因为「这条发给了谁」是时间线上第一眼要看的事——塞进 ``metadata``
就得先解析元数据才知道这一行是什么。

``member_turn`` 是批次四十五 a 加的（PRD §B2）：此前成员的回复**根本不进这条
时间线**——数据模型里 ``author_type="member"`` 的位置留着，一个字都没人写，于是
一个组的时间线只记得住用户说过什么，记不住组里的人答了什么。同一个理由让它
是独立取值而不是 ``metadata`` 的一个标签：「这一行是人说的还是成员答的」是
时间线上第一眼要看的事。

PRD §B1 还列了 ``relay`` 与 ``discussion`` 两个取值，它们属于第一稿的 ①②③ 三档
按钮。2026-09-14 的修订把那三档合并成「房间自己会转」（§B4，批次四十五 b），
``relay`` / ``discussion`` 随之作废：房间循环投出去的每一条都是**同一条用户消息
派生出来的一轮**，时间线上落的仍是 ``member_turn``，轮次与投递起点记在它的
``metadata`` 里。**两个取值因此一个都没加**——没有写入方的枚举值只会让「这个
取值会不会出现」变成读代码才答得出的问题。
"""

CollaborationAuthorType = Literal["user", "member", "coordinator", "system"]
"""谁发的。``member`` 时 ``author_member_id`` 必须非空。"""


class CollaborationMessage(DomainModel):
    """Group 自己的一条时间线条目（v1.0 §11.1 ``collaboration_messages``）。

    AD-13：Group 消息与 Context Packet 是 **Group 拥有的数据**，与成员各自的
    Conversation 分离持久化，**不依赖事件重放缓冲的保留期**——所以这张表没有
    ``expires_at``（对比 :class:`~app.events.models.StoredEvent`）。
    成员会话的内容仍按需从原生历史取，这里只存 Group 自己产生的东西。

    ``sequence`` 在一个 Group 内单调递增，是浮窗断线续传的游标。
    """

    IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"id", "collaboration_session_id", "sequence"}
    )

    id: CollaborationMessageId
    collaboration_session_id: CollaborationId
    sequence: int = Field(ge=0)
    kind: CollaborationMessageKind = "message"
    author_type: CollaborationAuthorType
    #: ``author_type="member"`` 时指向 :class:`CollaborationMember`。
    author_member_id: MemberId | None = None
    #: 发言成员对应的 Conversation（N §9.1：成员的唯一抽象就是 Conversation）。
    conversation_id: ConversationId | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_author(self) -> Self:
        if self.author_type == "member" and self.author_member_id is None:
            raise ValueError("author_type=member 必须带 author_member_id（N §9.1）")
        if self.author_type != "member" and self.author_member_id is not None:
            raise ValueError("只有 member 发言才能带 author_member_id")
        return self

    @classmethod
    def create(
        cls,
        *,
        collaboration_session_id: str,
        sequence: int,
        content: str,
        kind: CollaborationMessageKind = "message",
        author_type: CollaborationAuthorType = "user",
        author_member_id: str | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        message_uuid: str | None = None,
        created_at: datetime | None = None,
    ) -> CollaborationMessage:
        return cls(
            id=collaboration_message_id(message_uuid),
            collaboration_session_id=collaboration_session_id,
            sequence=sequence,
            kind=kind,
            author_type=author_type,
            author_member_id=author_member_id,
            conversation_id=conversation_id,
            content=content,
            metadata=dict(metadata or {}),
            created_at=created_at or _now(),
        )


def assert_member_set_valid(members: Sequence[CollaborationMember]) -> None:
    """集合级不变量：同一 Group 内 ``conversation_id`` 不重复。

    刻意**不**限制同一 AgentBinding 只能有一个成员——N §9.2 明确允许同一 Binding
    在一个 Group 中同时启动多条 Conversation。
    """
    seen_ids: set[str] = set()
    seen_conversations: set[tuple[str, str]] = set()
    for member in members:
        if member.id in seen_ids:
            raise DomainInvariantError(f"CollaborationMember.id 重复：{member.id!r}")
        seen_ids.add(member.id)
        key = (member.collaboration_session_id, member.source_conversation_id or member.conversation_id)
        if key in seen_conversations:
            raise DomainInvariantError(
                f"同一 Group 内 conversation 重复加入：{member.conversation_id!r}"
            )
        seen_conversations.add(key)


__all__ = [
    "DEFAULT_ROUND_CAP",
    "MAX_ROUND_CAP",
    "MIN_ROUND_CAP",
    "ROUND_CAP_KEY",
    "RoomThread",
    "ThreadEndedReason",
    "ThreadPhase",
    "ThreadStatus",
    "CollaborationAuthorType",
    "CollaborationMember",
    "CollaborationMessage",
    "CollaborationMessageKind",
    "CollaborationSession",
    "CollaborationStatus",
    "IsolationMode",
    "JoinMode",
    "ParticipationState",
    "assert_member_set_valid",
]
