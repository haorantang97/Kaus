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
        # 「提升为普通会话」2026-11-03 删除，不再发出；保留取值以便旧事件重放。
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
    "collaboration_message_to_wire",
    "group_change_data",
    "group_list_to_wire",
    "group_to_wire",
    "member_conversation_summary",
    "member_to_wire",
]
