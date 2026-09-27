"""Group（临时协作组）接口的序列化：纯函数，不依赖任何 web 框架。

为什么单独一个模块
------------------
与 :mod:`app.api.session_views` 同一个理由：路由层只剩「取参数、调服务、返回
dict」，序列化本身可以脱离 HTTP 单测；而且本模块不 import 框架，公共层纯净性
扫描（``kernel/tests/test_public_type_purity.py`` 会 import ``app`` 下每个子模块）
不需要装 fastapi。

三个口径
--------
1. **成员列表现算，不是快照。** N §9.4 明写「UI 与 API 从第一天支持成员列表动态
   变化，不得把 Group 成员固化为创建时快照」——所以这里没有任何「成员列表」形态
   的聚合对象，:func:`member_to_wire` 每次都由调用方现取的成员行 + 现取的
   Conversation 拼出来。
2. **拿不到就是 ``null``，不编。** 成员指向的 Conversation 被删掉了（v1.0 §16.6
   允许），``conversation`` 就是 ``null`` 而不是一个空壳摘要——N §13.1。
3. **公共层词汇。** 只认识 :mod:`app` / :mod:`runtime` 的类型，不认识任何一家
   Backend 的私有概念（N §3 核心约束）。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.collaboration.models import (
    CollaborationMember,
    CollaborationMessage,
    CollaborationSession,
)
# batch52 第 4 件：Context Packet 的「最近一句」改从**组时间线**取，所以这里要
# 认识房间那张显示名表与点名解析（两个都是纯函数，本模块的纯净性不受影响）。
from app.collaboration.room import RoomMember, resolve_mentions  # batch52
from app.conversations.models import Conversation

#: 产品自己的扩展事件 namespace（AD-86 定的那一个；信封 v1.1 冻结之后，
#: Group 通知只能从这个出口走）。
GROUP_EVENT_NAMESPACE = "kaus"

#: Group 变更通知的事件名。**一个名字管全部变更**，具体变了什么在
#: ``data.change`` 里——前端拿这一个 name 就能挂上监听，不必跟着后端每加一种
#: 变更就改一次订阅表。
GROUP_EVENT_NAME = "group.changed"

#: ``data.change`` 的封闭取值。加成员/移出/暂停/恢复/关闭是任务书点名的五条，
#: 其余三条（建组、改标题或最小化、提升为普通会话）走同一条通道，因为它们对
#: 前端的后果是同一件事：手上那份 Group 视图过期了，去重取。
GROUP_CHANGES: frozenset[str] = frozenset(
    {
        "group_created",
        "group_updated",
        "materials_updated",
        "group_closed",
        "member_joined",
        "member_left",
        "member_paused",
        "member_resumed",
        "member_promoted",
        # 批次五十二第 5 件（AD-173）：这位成员在本组改了名（`roleLabel`）。单开一
        # 个取值而不是复用 `group_updated`：改名同时改了**地址**（`@名字` 解析回
        # 成员 id 的那张表），浮窗除了重取成员还要把 `@` 菜单与房间说明一并刷新。
        # batch52
        "member_renamed",
        # 批次二十六：组自己的时间线上多了一行（广播 / 定向 / 成员变更的系统行）。
        # 带 `messageId`，前端据此决定是增量追加还是重取一页。
        "message_posted",
        # 批次四十五 a（PRD §B2）：某个成员答完了一轮，时间线上多了一条
        # `member_turn`。与 `message_posted` 分开一个取值而不是复用它：浮窗要能
        # 在「我发出去的那条落了地」与「有人回话了」之间分出轻重——后者是用户
        # 正在等的东西，前者他刚刚按过发送。沿用同一个 `messageId`。
        "member_turn",
        # 批次四十五 b（PRD §B4）：房间线程的状态变了（开转 / 换人 / 停 / 收口 /
        # 轮数到顶）。与 `member_turn` 分开一个取值：前者说「时间线多了一行」，
        # 这一条说「组头那一行要改」——浮窗据此刷 `GET /groups/{id}` 拿新的
        # `thread`，而不必为了看一眼「轮到谁」把整条时间线重取一遍。
        "thread_state",
    }
)

#: 一次投递的结局（任务书第 1 件的 ``deliveries[].status``）。
#:
#: 四个取值刻意把「没发」拆成三种而不是一个 ``false``：**为什么没发**决定了界面
#: 该说什么话。``skipped_paused`` 是用户自己按下的暂停（不该报错），
#: ``skipped_left`` 是这个人已经不在组里了（视图过期），``failed`` 才是真的出了
#: 事（引擎起不来、上一轮还在跑且这台引擎不排队）。
DELIVERY_STATUSES: frozenset[str] = frozenset(
    {"sent", "skipped_paused", "skipped_left", "failed"}
)

#: 批次二十七：``status`` 说「成了没有」，``reason`` 说「为什么」。
#:
#: 拆成两个字段是因为一个 ``failed`` 里塞着两类完全不同的事：**等一等就好**
#: （``turn_already_running``——上一轮还在跑，这在广播刚发完之后是常态）与
#: **坏了**（引擎起不来、Driver 没注册）。真机上它们被压成同一句
#: ``runtime_start_failed：DriverError: …``，用户既不知道该等还是该修，前端也
#: 没有可以分支的东西。``detail`` 从此只放**纯文本人话**（:func:`plain_text`），
#: 机器可读的那一半全在 ``reason`` 上。
DELIVERY_REASONS: frozenset[str] = frozenset(
    {
        # skipped_*
        "member_left",
        "member_paused",
        "member_join_failed",
        # failed
        "conversation_missing",
        "binding_not_found",
        "driver_not_registered",
        "runtime_start_failed",
        # AD-159：引擎的**进程**没拉起来（文件不在 / 没有执行权限 / 二进制是坏的）。
        # 与 runtime_start_failed 分开，因为修法完全不同：那一条是「把网关跑起来」，
        # 这一条是「把同一条命令贴进终端跑一次，看它到底为什么起不来」。
        "agent_spawn_failed",
        "turn_already_running",
        # AD-157：引擎说「先去登录」。它与 turn_already_running 同类——不是故障，
        # 是一个用户自己能修的状态，只不过修法在站外的终端里。
        "auth_required",
        "conversation_model_unsupported",
        "message_send_failed",
        "send_failed",
    }
)

#: 成员会话摘要的字数上限（任务书第 3 件：「最后一条用户/助手消息摘要 ≤200 字」）。
SUMMARY_LIMIT: int = 200


def group_to_wire(
    group: CollaborationSession, *, member_count: int | None = None
) -> dict[str, Any]:
    """CollaborationSession → wire dict（驼峰键，与 v1.0 §4.6 一致）。

    ``memberCount`` 只在调用方数过的时候出现（列表端点会数，详情端点用
    ``members`` 的长度）。数不了就**不放这个键**，而不是给一个 0——「没数」与
    「一个成员都没有」在界面上是两句不同的话。
    """
    payload = group.model_dump(mode="json", by_alias=True)
    if member_count is not None:
        payload["memberCount"] = member_count
    return payload


def member_conversation_summary(
    conversation: Conversation,
    *,
    backend_id: str | None,
    run_state: str | None,
    surface: str,
) -> dict[str, Any]:
    """成员那条 Conversation 的摘要（Phase 7 成员能力清单第 6 条的数据面）。

    「新成员来源、角色、AgentBinding、Model、Workspace/Worktree 和状态可见」——
    角色与 Workspace 在成员行上（``roleLabel`` / ``worktreeOrRuntimeRef``），
    其余在这里。刻意是**扁平的一小把字段**而不是整个 Conversation：Group 浮窗
    要的是一行摘要，完整对象走 ``GET /api/conversations/{id}``。

    ``backendId`` 从 Binding 上取（Conversation 自己不存 backend——D-06 之下
    Binding 才是那条不变的绑定关系）；取不到就是 ``null``，不编造。
    """
    return {
        "conversationId": conversation.id,
        "title": conversation.title,
        "projectId": conversation.project_id,
        "bindingId": conversation.agent_binding_id,
        "backendId": backend_id,
        "modelId": conversation.model_id,
        "reasoningMode": conversation.reasoning_mode,
        "runState": run_state,
        "surface": surface,
        "origin": conversation.origin,
        # 关闭组时的处置全看这两个字段（N §9.5），所以它们必须在成员行上就看得见，
        # 而不是等用户点进去才知道这条会话关组时会不会被归档。
        "visibility": conversation.visibility,
        "retention": conversation.retention,
        "state": conversation.state,
    }


def member_to_wire(
    member: CollaborationMember,
    *,
    conversation: dict[str, Any] | None = None,
    display_name: str | None = None,
    is_leader: bool = False,
) -> dict[str, Any]:
    """CollaborationMember → wire dict。

    ``conversation`` 是 :func:`member_conversation_summary` 的结果；取不到就是
    ``null``（口径 2）。

    ``display_name``（批次四十六 / PRD §B10-2）是**后端那张共用表**算出来的最终显示
    名，含重名后缀 ``#2``。它是这个成员在房间说明里被告知的名字，也必须是界面上叫他
    的名字——真机截图里两个同名成员在时间线上都写 ``media``，而它们自己在房间里叫
    ``media`` / ``media#2``，于是成员说「@media#2 负责执行与质检」时用户分不出那是谁。
    算不出（调用方没传）就**不出现这个键**，前端退回本地推断（老后端兼容）。
    """
    wire: dict[str, Any] = {
        "id": member.id,
        "groupId": member.collaboration_session_id,
        "conversationId": member.conversation_id,
        "sourceConversationId": member.source_conversation_id,
        "joinMode": member.join_mode,
        "roleLabel": member.role_label,
        "participationState": member.participation_state,
        "isolationMode": member.isolation_mode,
        "worktreeOrRuntimeRef": member.worktree_or_runtime_ref,
        "joinedAt": _iso(member.joined_at),
        "leftAt": _iso(member.left_at),
        # 批次四十五 b：这个成员的房间增量看到时间线的哪一条了（PRD §B4）。
        # 还没投过就是 null——那是「从线程起点算」，不是「看过第 0 条」。
        "lastDeliveredSequence": member.last_delivered_sequence,
        # 批次四十八（PRD §B6）：这一位是不是组长。**恒在**（不像 `displayName` 那样
        # 缺席即退回推断）：前端算不出这件事——它是组上的一个字段，成员行里没有。
        "isLeader": is_leader,
        "conversation": conversation,
    }
    if display_name:
        wire["displayName"] = display_name
    return wire


def truncate_summary(text: str | None, *, limit: int = SUMMARY_LIMIT) -> str | None:
    """把一段消息正文压成一行摘要（≤ ``limit`` 字），空白全部折叠。

    ``None`` 与「折叠完只剩空白」都回 ``None``——Context Packet 里「这条会话还
    没说过话」应当表现为**没有这个键**，而不是一个空字符串（同 AD-71 的口径）。
    截断时补一个省略号，让读的人知道这里被剪过。
    """
    if text is None:
        return None
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def collaboration_message_to_wire(
    message: CollaborationMessage,
) -> dict[str, Any]:
    """Group 时间线的一行 → wire dict（任务书第 1 件的形状）。

    ``targetMemberIds`` 与 ``deliveries`` 存在领域模型的 ``metadata`` 里：它们是
    **这一次路由做了什么**的账，不是 Group 的结构。给它们各开一列意味着以后每加
    一种路由结果就要改一次表；放进 metadata 则只改这里的读法。取不到就是空列表，
    而不是 ``null``——「没有投递记录」与「投递了 0 个人」在界面上是同一句话。

    ``authorRole`` 是 ``user | system | member``。批次四十五 a 起 ``member``
    真的会出现（``kind="member_turn"``，PRD §B2）：那一行带
    ``authorMemberId`` / ``conversationId``，让「这句话是谁说的」在时间线上一眼
    答得出来——那正是 AD-153 的底线，而不是它的例外。

    ``member_turn`` 特有的四个键（``runId`` / ``toolCount`` / ``truncated`` /
    ``outcome``）从 ``metadata`` 里摊到顶层，**取不到就不出现这个键**
    （N §13.1）：前端因此不必为「没有 outcome」与「outcome 是 null」各写一支。
    ``toolCount`` 只给数，不给工具的名字与入参——那些属于成员自己那条会话的
    页面（PRD §B8 L1）。
    """
    metadata = message.metadata or {}
    targets = metadata.get("targetMemberIds")
    deliveries = metadata.get("deliveries")
    wire: dict[str, Any] = {
        "id": message.id,
        "groupId": message.collaboration_session_id,
        "sequence": message.sequence,
        "kind": message.kind,
        "authorRole": message.author_type,
        "text": message.content,
        "targetMemberIds": list(targets) if isinstance(targets, list) else [],
        "deliveries": list(deliveries) if isinstance(deliveries, list) else [],
        "createdAt": _iso(message.created_at),
    }
    if message.author_member_id is not None:
        wire["authorMemberId"] = message.author_member_id
    if message.conversation_id is not None:
        wire["conversationId"] = message.conversation_id
    # 批次四十五 b：房间循环记的那几个（`epoch` / `round` / `speakerIndex` /
    # `deliveredSince` / `deliveredTo` / `passed` / `final`）同样只在**有**的时候
    # 出现——不属于任何一轮的成员发言（用户自己在会话页里发的一句）不该被编出一个
    # 「第 0 轮」来。
    for key in (
        "coordinator",
        "eventAfter",
        "control",
        "modelId",
        "providerId",
        "runId",
        "toolCount",
        "truncated",
        "outcome",
        "roomHeaderVersion",
        "epoch",
        "round",
        "speakerIndex",
        "deliveredSince",
        "deliveredTo",
        "passed",
        "final",
        # `phase="closing"` 的那一条是触顶那次收口（它也带 `final`，界面渲染成同一
        # 张高亮卡）。`finalIgnored`（批次四十八）说这一条以「最终答复」开头、但作者
        # 不是组长所以不生效——界面上**不单独标**它，它在 wire 上是为了回头能答出
        # 「我明明写了最终答复，房间为什么没停」。
        "phase",
        "finalIgnored",
        # `mentions`（批次五十）：这一段话点到的成员 id。第五次修订之后**只有组长的
        # 点名进队列**，所以这一格正是「评审点了打杂、下一轮却没有打杂」那件事在
        # 时间线上的证据（测试单 L20）。没点到人的那几条不带这个键。
        "mentions",
        # `routed`（批次五十一）：**组长那条**真正被路由进下一轮的那几位（不含组长
        # 自己）。它与同一行上的 `mentions` 是两把尺子的两笔账——宽尺子记「他提到了
        # 谁」，窄尺子（只认 `@`）记「谁真的被点了名」。两格并排，时间线才答得出
        # 「组长提到了打杂，但打杂没进下一轮」（测试单 L21）。
        "routed",
    ):
        value = metadata.get(key)
        if value is not None:
            wire[key] = value
    # Older records may have marked a failed run as the final answer. Preserve
    # the stored history, but do not present that invalid conclusion to clients.
    if wire.get("outcome"):
        wire.pop("final", None)
        wire.pop("finalIgnored", None)
    return wire


# batch52
def packet_recent_lines(
    messages: Sequence[CollaborationMessage],
    roster: Sequence[RoomMember],
) -> dict[str, dict[str, str]]:
    """从**组时间线**里摘出每位成员的「最近一句」（批次五十二第 4 件 / 真机 UI-01）。

    为什么换来源
    ------------
    Context Packet v0（批次二十六）是从**成员自己那条会话的事件流**里摘「最后
    一条用户消息 / 助手消息」的。在房间里这两句都不是它们看上去的意思：投给成员
    的「用户消息」正是**房间增量**（里面装着别人的发言），所以「最近一句」经常
    显示成别人说的话或上一条输入——一张本该让人一眼看清「谁到哪儿了」的表，给出
    的是一个会误读的答案。

    换成时间线之后两句话各有准确的出处：

    * ``lastAssistantMessage`` = 这位成员**最近一条 ``member_turn`` 的正文**。
      标了 ``passed`` 的跳过：「（略过）」不是他说的一句话，把它当成最近发言等于
      告诉用户他最后什么都没说。
    * ``lastUserMessage`` = 最近一条**点到他**的用户消息。判据两条：
      ``metadata.targetMemberIds`` 含他（勾选发送 / 定向发送），或者这一行没写
      目标、而正文里**点了他的名**（``resolve_mentions``，宽尺子——用户那句话
      认裸名，见 :func:`resolve_mentions` 的 docstring）。目标为空且一个人都没
      点到 = 发给全体，那对每位成员都算「点到他」。

    入参 ``messages`` 按 ``sequence`` **升序**（仓储的原样），所以后来的覆盖先前
    的。返回 ``memberId → {键: 摘要}``，摘要照旧 ≤200 字
    （:func:`truncate_summary`），**没有的键不出现**（N §13.1）。
    """
    member_ids = [member.member_id for member in roster]
    found: dict[str, dict[str, str]] = {member_id: {} for member_id in member_ids}
    for message in messages:
        metadata = message.metadata or {}
        text = truncate_summary(message.content)
        if message.kind == "member_turn":
            author = message.author_member_id
            if author is None or author not in found:
                continue
            if metadata.get("passed"):
                continue  # 「（略过）」不是一句发言
            if text:
                found[author]["lastAssistantMessage"] = text
            continue
        if message.kind not in ("broadcast", "directed"):
            continue
        targets = metadata.get("targetMemberIds")
        targets = list(targets) if isinstance(targets, list) else []
        if targets:
            addressed: Sequence[str] = [mid for mid in targets if mid in found]
        else:
            mentioned = resolve_mentions(message.content or "", roster)
            # 一个人都没点到 = 这一条是发给全体的。
            addressed = mentioned or member_ids
        if not text:
            continue
        for member_id in addressed:
            if member_id in found:
                found[member_id]["lastUserMessage"] = text
    return found


def context_packet_markdown(
    *, title: str, generated_at: str | None, members: Sequence[dict[str, Any]]
) -> str:
    """Context Packet 的可复制文本（任务书第 3 件的 ``markdown``）。

    与 ``members`` 是**同一份内容的两种形状**，不是两份数据：结构化的给界面渲染，
    这一份给用户按一下「复制」贴进任何地方。AD-154：它**不会**被自动喂给任何
    会话——要注入就得用户显式点「作为消息发给某成员」，那条路是定向消息。

    刻意不用表格：一行里塞得下的字段有限，而摘要本身就有 200 字，表格会被撑成
    横向滚动条。分节的列表在终端、聊天框、Markdown 渲染器里都读得通。

    批次五十二第 4 件：小标题用的是 ``displayName``（后端那张共用表算的，含重名
    后缀 ``#2``），不再是会话标题——真机上两位成员在这张表里都叫 ``media``，而
    成员栏、时间线、房间说明里它们是 ``media`` 与 ``media#2``（真机 UI-02）。
    会话标题仍旧列在下面那几条事实里，原标题一个字都没丢。
    """
    lines = [f"# {title} · Context Packet"]
    if generated_at:
        lines.append(f"生成于 {generated_at}（只读汇编，未发给任何成员）")
    if not members:
        lines.append("")
        lines.append("这个组现在没有成员。")
        return "\n".join(lines)
    for index, member in enumerate(members, start=1):
        lines.append("")
        # batch52
        heading = (
            member.get("displayName")
            or member.get("title")
            or member.get("conversationId")
            or "（无标题）"
        )
        lines.append(f"## {index}. {heading}")
        facts = [
            ("会话标题", member.get("title")),
            ("会话", member.get("conversationId")),
            ("Binding", member.get("bindingId")),
            ("引擎", member.get("backendId")),
            ("模型", member.get("modelId")),
            ("运行态", member.get("runState")),
            ("参与状态", member.get("participationState")),
            ("最近完成", member.get("lastCompletedAt")),
        ]
        for label, value in facts:
            if value:
                lines.append(f"- {label}：{value}")
        last_user = member.get("lastUserMessage")
        last_assistant = member.get("lastAssistantMessage")
        # batch52：两句的出处都换成了**组时间线**，标签因此照着改——「最后一条
        # 用户消息」此前指的是投给它的那份房间增量（里面装着别人的话）。
        if last_user:
            lines.append(f"- 最近一条点到它的话：{last_user}")
        if last_assistant:
            lines.append(f"- 它在组里最近说的：{last_assistant}")
        if not last_user and not last_assistant:
            lines.append("- 还没有可摘要的对话内容")
    return "\n".join(lines)


def group_change_data(
    *,
    group_id: str,
    change: str,
    member_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """``kaus/group.changed`` 的 ``data``（任务书第 3 件的形状）。

    ``memberId`` / ``conversationId`` 在与成员无关的变更（建组、改标题、关闭）上
    **不出现这个键**，而不是为 null——与 AD-94 的 ``clientRef`` 同一个做法：
    让「不适用」在 wire 上是「键不存在」，前端不用区分两种空。
    """
    if change not in GROUP_CHANGES:
        raise ValueError(f"未知的 Group 变更种类：{change!r}")
    data: dict[str, Any] = {"groupId": group_id, "change": change}
    if member_id is not None:
        data["memberId"] = member_id
    if conversation_id is not None:
        data["conversationId"] = conversation_id
    if message_id is not None:
        data["messageId"] = message_id
    return data


def group_list_to_wire(
    groups: Sequence[CollaborationSession],
    *,
    member_counts: dict[str, int],
    member_conversation_ids: dict[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """``GET /api/groups`` 的响应体。

    ``memberConversationIds``（批次二十六第 4 件，批次二十三的前端提的补项）：
    列表里每行直接给出成员的会话 id，浮窗因此不必为了「这个组里有没有我正在看
    的那条会话」而对每个组再打一次 ``GET /groups/{id}``。**只有 id，不带摘要**
    ——摘要在详情端点里，列表要的是能对一下集合的最小信息。
    """
    entries = []
    for group in groups:
        payload = group_to_wire(group, member_count=member_counts.get(group.id, 0))
        payload["memberConversationIds"] = list(
            (member_conversation_ids or {}).get(group.id, ())
        )
        entries.append(payload)
    return {"groups": entries, "count": len(entries)}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "DELIVERY_REASONS",
    "DELIVERY_STATUSES",
    "GROUP_CHANGES",
    "GROUP_EVENT_NAME",
    "GROUP_EVENT_NAMESPACE",
    "SUMMARY_LIMIT",
    "collaboration_message_to_wire",
    "context_packet_markdown",
    "group_change_data",
    "group_list_to_wire",
    "group_to_wire",
    # batch52
    "packet_recent_lines",
    "member_conversation_summary",
    "member_to_wire",
    "truncate_summary",
]
