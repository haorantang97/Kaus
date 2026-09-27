"""Group HTTP API: isolated member sessions, explicit shared materials, and room routing.

A member runs in a Group-owned Conversation. Joining from an individual chat
copies its binding and session settings, not its native session or history.
The source ID is retained for provenance; one source may join multiple Groups.

Materials are selected by the user and stored per Group, with CAS revisions.
Each user-triggered discussion snapshots a single version for all recipients.
The separate Context Packet endpoint remains a read-only activity overview.

Authentication and the v1.1 event envelope are shared with the session API;
Group changes use the kaus/group.changed extension and the Group SSE stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.api_errors import ApiError, json_error_endpoint
from app.api.group_views import (
    GROUP_EVENT_NAME,
    GROUP_EVENT_NAMESPACE,
    collaboration_message_to_wire,
    context_packet_markdown,
    group_change_data,
    group_list_to_wire,
    group_to_wire,
    member_conversation_summary,
    member_to_wire,
    # batch52
    packet_recent_lines,
    truncate_summary,
)
from app.api.session_auth import (
    SessionAuthPolicy,
    auth_route_class,
    require_session_auth,
)
from app.api.session_views import (
    KEEPALIVE_FRAME,
    SSE_HEADERS,
    SSE_PADDING_FRAME,
    conversation_surface,
    conversation_to_wire,
    sse_data_line,
)
from app.api.views import normalize_project_id
from app.api.group_coordinator import GroupCoordinator
from app.collaboration.coordinator import CONFIG_KEY, DEFAULTS_KEY
from app.collaboration.member_turns import (
    RUN_TERMINAL_EVENT_TYPES,
    find_recorded_turn,
    member_turn_metadata,
    membership_covers,
    run_started_at as scan_run_started_at,
    summarize_turn,
    truncate_body,
    turn_outcome,
)
from app.collaboration.materials import (GroupMaterial, GroupMaterials, MaterialConflict, material_context)
from app.collaboration.models import (
    MAX_ROUND_CAP,
    MIN_ROUND_CAP,
    ROUND_CAP_KEY,
    CollaborationMember,
    CollaborationMessage,
    CollaborationSession,
    RoomThread,
)
from app.collaboration.room import (
    ROOM_HEADER_VERSION,
    MemberTurn,
    RoomMember,
    advance_thread,
    closing_prompt,
    is_final,
    is_pass,
    is_stop_command,
    next_round_queue,
    public_entries,
    resolve_display_names,
    resolve_everyone,
    resolve_mentions,
    room_delta,
    room_header,
    start_thread,
    stop_thread,
    strip_room_header,
    with_room_header,
)
from app.conversations.models import Conversation
from app.errors import DomainInvariantError
from app.persistence.base import RepositorySet
from app.projects.binding_snapshot import register_binding_snapshot
from app.api.event_wait import DEFAULT_RUN_ID_TIMEOUT, first_run_id as _first_run_id
from drivers.base import (
    AGENT_SPAWN_FAILED,
    AgentSpawnError,
    DriverNotRegisteredError,
    FailureHint,
    MessageInput,
    TurnAlreadyRunningError,
    UnsupportedCapabilityError,
    plain_text,
)
from runtime.event_envelope import ExtensionEvent
from runtime.session_host import SessionHost

LOGGER = logging.getLogger(__name__)

#: 全局 Group SSE 的心跳周期（秒）。与会话流同一个理由（AD-62）：中间的代理
#: 与浏览器不该把一条长时间静默的连接判死。
DEFAULT_GROUP_KEEPALIVE_INTERVAL: float = 15.0

#: 一个订阅者积压多少条变更就认为它跟不上了。变更通知是「去重取」的触发器，
#: 不是内容本身——丢几条的后果只是少刷新一次，所以宁可丢也不要把内存撑爆。
GROUP_QUEUE_MAXSIZE: int = 64

#: 非流式快照一次最多回多少帧（批次三十第 1 件）。与会话那条同一个数，理由也一样：
#: 不让一次响应无界增长，超了就把 ``truncated`` 置真让客户端接着取。
SNAPSHOT_MAX_EVENTS: int = 2000

#: 「组已关闭」的稳定 code。关组之后所有成员端点都回它（任务书第 4 件）。
GROUP_CLOSED_CODE = "group_closed"

#: 一条会话已经在**另一个**还开着的组里（detail 带那个 ``groupId``）。

#: 关组时对成员会话的处置动作（任务书第 4 件的三行）。
CLOSE_ACTIONS: frozenset[str] = frozenset({"kept", "promoted", "archived"})

#: ``POST /close`` 的 ``onClose`` 取值。
ON_CLOSE_CHOICES: frozenset[str] = frozenset({"keep", "archive"})

#: 一次「推进」最多连着走几步（PRD §B4，批次四十五 b）。
#:
#: 正常情况下推进在**第一位真的收到**那一刻就停下来等他跑完，所以这个数只在一种
#: 情形下用得上：一整轮里每个人的投递都没送出去（引擎全挂了）。那时状态机会按
#: 「全员没说话 → idle」自己停住，这个上限是它之外的第二道保险——**一条循环里
#: 每走一步都要发一次网络请求，它绝不该有可能不终止**。
MAX_PUMP_STEPS: int = 200

#: 到达安全上限时时间线上那一行系统消息（PRD §B4 终止 4）。
#:
#: 轮数只是安全阀，所以这句话说的是两件事：上限到了、已经让组长去收口。它不是
#: 一次失败（第一版那句「N 轮到了，没有最终答复」是），房间接下来就会去要一个答复。
EXHAUSTED_LINE: str = "到达安全上限（{rounds} 轮），已让 {leader} 收口"

#: 组长读完全场之后没再点任何人（PRD §B4 终止 3）：它那一段就是结论。
#:
#: 为什么要写这一句：那条 `member_turn` 会被标成最终答复卡，而**为什么它是最后
#: 一条**只有这里说得出来——用户看到的否则是「聊着聊着就不聊了」。
LEADER_CLOSED_LINE: str = "组长 {leader} 没有再点名，讨论到此为止"

#: 组长自己以「最终答复」开头收口（终止 2）。
LEADER_FINAL_LINE: str = "组长 {leader} 给出了最终答复"

#: 没有可收口的成员（终止 6）：组长暂停 / 离开且组里没有别人能接手。
LEADER_MISSING_LINE: str = "没有可收口的成员"

#: 收口那一轮没成（投递失败 / 跑失败）。**不重试**——重试等于再花一次钱去赌同一件
#: 刚失败的事，而用户此刻要的是知道「它没收成」。
CLOSING_FAILED_LINE: str = "收口没有成功：{reason}"

#: 组长换人（加入时自动指定 / PATCH 换 / 自动移交）在时间线上那一行。
LEADER_SET_LINE: str = "{leader} 是组长"

#: 组长暂停 / 离开时自动移交（PRD §B4 终止 6 后半句）。
LEADER_HANDOVER_LINE: str = "{previous} 不在了，{leader} 接任组长"

#: 打字说「停」时时间线上那一行（PRD §B10-1）。把原话引回去，用户才知道是哪一句
#: 被当成了指令——这条规则是词表匹配，说清楚它匹配到了什么是它该付的代价。
STOP_COMMAND_LINE: str = "已停止（你说了『{word}』）"

#: 改了某位成员**在本组的名字**（批次五十二第 5 件 / AD-173）。
#:
#: 两个名字都写出来：显示名同时是称呼与地址（``@名字`` 要解析回成员 id），改名
#: 之后时间线上那之前的发言仍旧署着旧名字——只说新名字的话，用户读不出这两串是
#: 同一个人。
MEMBER_RENAMED_LINE: str = "{previous} 现在叫 {name}"


class GroupApiError(ApiError):
    """带稳定 code 的 Group 接口错误。

    与 :class:`~app.api.session_router.SessionApiError` 是并列的两个名字、同一个
    基类、同一个 wire 形状——分开只是为了 ``except`` 时能说清「这是组这一层的
    错误」。
    """


def _not_found(code: str, message: str) -> GroupApiError:
    return GroupApiError(404, code, message)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# --------------------------------------------------------------------------- #
# 全局变更广播
# --------------------------------------------------------------------------- #


#: 组级流的内存环形缓冲长度（任务书第 4 件：「内存环形缓冲 200 条即可」）。
GROUP_REPLAY_BUFFER: int = 200


class GroupEventHub:
    """组级变更的**进程内**广播（``GET /api/groups/events``）。

    为什么不复用 Event Store：那是**按会话**的重放缓冲（D-16），它的坐标是
    ``(conversationId, sequence)``；而这条流回答的是「哪个组变了」，压根没有
    conversation 这根轴——建组、改标题、关组三种变更甚至一条会话都不涉及。
    硬塞进去等于给 Event Store 造第二套主键。

    **短重放（批次二十六第 4 件）。** 骨架期它一条都不重放，恢复手段是重取
    ``GET /api/groups``。现在多了一个 ``?since=<sequence>``：进程内留最近
    :data:`GROUP_REPLAY_BUFFER` 条，重连时先把漏掉的那几条补上，再接实时。
    这仍然**不是** Event Store——

    - 缓冲是进程内的、有上限的、进程一重启就没了；
    - 游标断档（``since`` 比缓冲里最早的一条还早，或者根本不认识）时不报错，
      而是回一帧 ``replayTruncated``，让前端知道「这一段补不齐，去重取列表」。

    多进程部署下每个进程只看得见自己这边的变更——那是另一条裁决，本批不做。
    """

    def __init__(self, *, buffer_size: int = GROUP_REPLAY_BUFFER) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._sequence = 0
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max(1, buffer_size))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def sequence(self) -> int:
        """最后一条广播出去的帧的 ``sequence``（还没发过就是 0）。"""
        return self._sequence

    def replay(self, since: int) -> tuple[tuple[dict[str, Any], ...], bool]:
        """``?since=`` 的重放：返回 ``(帧, 是否断档)``。

        断档为真表示「你要的那一段有一部分已经被挤出缓冲了」——那时给得出的
        帧仍然给，但前端必须补一次 ``GET /api/groups`` 才敢认为自己是全的。
        """
        if not self._buffer:
            # 缓冲空：只有「这个进程还没发过任何变更」这一种可能。
            # 那么 since 落在未来（进程重启过）才算断档。
            return (), since > self._sequence
        earliest = self._buffer[0]["sequence"]
        frames = tuple(frame for frame in self._buffer if frame["sequence"] > since)
        truncated = since + 1 < earliest or since > self._sequence
        return frames, truncated

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=GROUP_QUEUE_MAXSIZE
        )
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:  # pragma: no cover - 重复退订不该炸
            pass

    def publish(self, data: dict[str, Any]) -> dict[str, Any]:
        """广播一条变更；返回真正发出去的那一帧（含 ``sequence``）。"""
        self._sequence += 1
        frame = {
            "type": f"{GROUP_EVENT_NAMESPACE}/{GROUP_EVENT_NAME}",
            "sequence": self._sequence,
            "occurredAt": _now().isoformat().replace("+00:00", "Z"),
            "data": dict(data),
        }
        self._buffer.append(frame)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # 跟不上的订阅者丢这一条。见类 docstring：这是刷新触发器，
                # 不是内容；丢了的后果是少刷新一次，而不是少一段历史。
                pass
        return frame


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class CreateGroupBody(_Body):
    """``POST /api/groups``。

    ``home_project_id`` 可空（C-6：可空仅为表达能力预留，不构成「允许跨 Project」
    的产品裁决）；给了就必须是一个存在的 Project。
    """

    title: str = Field(min_length=1, max_length=200)
    home_project_id: str | None = None


class CoordinatorBody(_Body):
    config: dict[str, Any]
    expected_revision: int = 0


class PatchGroupBody(_Body):
    """``PATCH /api/groups/{id}``：改标题 / 最小化 ⇄ 展开 / 改兜底轮数。

    ``status`` 只收 ``minimized`` 与 ``active`` 两个值：关组是**另一个**动作
    （``POST /close``，它要对每个成员会话做处置），不能靠 PATCH 一个状态字符串
    顺手完成。

    ``round_cap``（PRD §B4）是**设置**不是动作，所以跟标题走同一条 PATCH，而不是
    新开一个 ``/thread/cap`` 端点——「不新增转发 / 讨论端点」那一条的同一个精神。

    ``leader_member_id``（PRD §B6，批次四十八）同理：换组长是改组的一个字段，不是
    一次「任命」仪式。必须是 ``active`` 成员，否则 400 ``leader_not_active``。
    """

    title: str | None = None
    status: str | None = None
    round_cap: int | None = None
    leader_member_id: str | None = None


class MaterialBody(_Body):
    expected_revision: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=12_000)
    source_kind: str = "note"
    source_id: str | None = None
    source_label: str | None = Field(default=None, max_length=200)


class AddMemberBody(_Body):
    """``POST /api/groups/{id}/members``：加入一条**已有**会话（N §9.3 路径 A）。"""

    conversation_id: str
    role: str | None = None


class PatchMemberBody(_Body):
    """``PATCH /api/groups/{id}/members/{memberId}``：改他**在这个组里叫什么**。

    批次五十二第 5 件（AD-173）。写的是 ``role_label``——它在显示名那条回退链的
    **第一位**（``roleLabel > 会话标题 > 成员 id 尾段``），所以改它正好就是「这位
    在这个房间里的名字」，而**会话自己的标题一个字不动**：同一条会话可以在一个组
    里叫「评审」、在别处仍旧叫它本来的名字。

    空字符串 = **清掉**，名字回退到会话标题。不给这个键（``None``）= 什么都没要求
    改，400 ``empty_patch``——「清掉」与「没提」必须分得开，否则一次只想改别的字段
    的 PATCH 会顺手把名字抹掉。
    """

    role_label: str | None = None


class SpawnMemberBody(_Body):
    """``POST /api/groups/{id}/spawn``：在组里启动一个新成员（N §9.3 路径 B）。

    ``binding_id`` 不给就用该 Project 的默认 Binding；两者都没有就是 400——
    公共层不替用户挑一条 Binding（D-06：绑定关系是显式的）。
    """

    project_id: str
    binding_id: str | None = None
    model_id: str | None = None
    title: str | None = None
    role: str | None = None
    initial_message: str | None = None


class BroadcastBody(_Body):
    """``POST /api/groups/{id}/broadcast``（AD-153）。

    ``target_member_ids`` 不给 = 发给组内**全部** ``active`` 成员（广播）；
    给了 = 只发给点名的这几个（定向）。两者走同一条投递路径，区别只在收件人
    集合与时间线上那一行的 ``kind``——把它们做成两个端点会让「发给三个人」
    和「发给全组」看起来像两件不同的事，而它们不是。
    """

    text: str = Field(min_length=1, max_length=100_000)
    target_member_ids: list[str] | None = None


class SendToMemberBody(_Body):
    """``POST /api/groups/{id}/members/{memberId}/send``：单目标糖衣。"""

    text: str = Field(min_length=1, max_length=100_000)


class CloseGroupBody(_Body):
    """``POST /api/groups/{id}/close``。

    ``on_close`` 只管 ``retention=decide_on_group_close`` 的那些成员会话；
    ``persistent`` 不动、``ephemeral`` 一律归档（见 :func:`_close_disposition`）。
    缺省 ``keep``。
    """

    on_close: str | None = None


# --------------------------------------------------------------------------- #
# 关组处置
# --------------------------------------------------------------------------- #


def _close_disposition(conversation: Conversation, *, on_close: str) -> str:
    """关组时这条成员会话该怎么处置（任务书第 4 件 / N §9.5）。

    ==============================  =========================================
    ``retention``                    动作
    ==============================  =========================================
    ``persistent``                   ``kept`` —— 一个字段都不动
    ``decide_on_group_close``        ``on_close="keep"`` → ``promoted``
                                     （``project_visible + persistent``）；
                                     ``on_close="archive"`` → ``archived``
    ``ephemeral``                    ``archived`` —— 不问，也不删
    ==============================  =========================================

    **归档不是删除**：只把会话置 ``state="ended"``，事件缓冲一条不清、原生
    Session 一个不动。删除是显式动作（``DELETE /api/conversations/{id}``，
    AD-105），关一个组不该顺手替用户做那件不可逆的事。
    """
    if conversation.retention == "persistent":
        return "kept"
    if conversation.retention == "ephemeral":
        return "archived"
    return "promoted" if on_close == "keep" else "archived"


def _apply_disposition(conversation: Conversation, action: str) -> Conversation:
    """把处置动作落到 Conversation 上。``kept`` 是恒等。"""
    if action == "promoted":
        return conversation.promote_to_project()
    if action == "archived":
        return conversation.evolve(state="ended", updated_at=_now())
    return conversation


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def build_group_router(
    *,
    session_host: SessionHost,
    repositories: RepositorySet,
    registry: Any,
    auth_policy: SessionAuthPolicy,
    hub: GroupEventHub | None = None,
    prefix: str = "/api",
    tag: str = "groups",
    keepalive_interval: float = DEFAULT_GROUP_KEEPALIVE_INTERVAL,
    run_id_timeout: float = DEFAULT_RUN_ID_TIMEOUT,
    coordinator_enabled: bool = True,
    coordinator_root: str | None = None,
) -> Any:
    """装配 Group 路由。返回一个框架的 ``APIRouter``，由调用方挂载。

    ``auth_policy`` 没有默认值——与会话路由同一条（D-17 / AD-66）：挂上这套路由
    与这套路由被鉴权是同一件事。``GET /groups/events`` 额外接受 ``?token=``
    （``EventSource`` 带不了头），其余端点只认 ``Authorization: Bearer``。
    """
    from fastapi import APIRouter, Body, Depends, Query
    from fastapi.responses import JSONResponse, StreamingResponse

    router = APIRouter(prefix=prefix, tags=[tag], route_class=auth_route_class())
    host = session_host
    events = hub if hub is not None else GroupEventHub()
    _auth = Depends(require_session_auth(auth_policy))
    _auth_sse = Depends(require_session_auth(auth_policy, allow_query_token=True))
    _endpoint = json_error_endpoint(JSONResponse)

    # --- 取对象 ------------------------------------------------------------ #

    async def _load_group(group_id: str) -> CollaborationSession:
        group = await repositories.collaborations.get(group_id)
        if group is None:
            raise _not_found("group_not_found", f"未知 Group：{group_id}")
        return group

    async def _load_open_group(group_id: str) -> CollaborationSession:
        """取一个**还开着**的组。已关闭 → 409（任务书第 4 件末句）。

        为什么是 409 而不是 404：那个组确实存在，用户手上那份视图只是过期了。
        404 会让前端把它从列表里抹掉，而事实是它还在，只是关了。
        """
        group = await _load_group(group_id)
        if group.status in ("closed", "archived"):
            raise GroupApiError(
                409,
                GROUP_CLOSED_CODE,
                f"Group {group.id} 已经关闭",
                detail={"groupId": group.id, "status": group.status},
            )
        return group

    async def _load_member(
        group: CollaborationSession, member_id: str
    ) -> CollaborationMember:
        member = await repositories.members.get(member_id)
        if member is None or member.collaboration_session_id != group.id:
            # 「不在这个组里」与「压根没有这个成员」对调用方是同一件事：
            # 它手上那个 id 在这条路径上没有意义。不区分，省得泄漏别的组的存在。
            raise _not_found(
                "member_not_found", f"Group {group.id} 里没有成员 {member_id}"
            )
        return member

    async def _load_conversation(conversation_id: str) -> Conversation:
        conversation = await repositories.conversations.get(conversation_id)
        if conversation is None:
            raise _not_found(
                "conversation_not_found", f"未知 Conversation：{conversation_id}"
            )
        return conversation

    # --- 成员视图（现算，口径 2） -------------------------------------------- #

    async def _conversation_summary(conversation_id: str) -> dict[str, Any] | None:
        conversation = await repositories.conversations.get(conversation_id)
        if conversation is None:
            # 会话被删了（v1.0 §16.6 允许），成员行还在。如实回 null。
            return None
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        lease = None
        describe = getattr(getattr(host, "leases", None), "describe", None)
        if describe is not None:
            try:
                lease = await describe(conversation.id)
            except Exception:  # noqa: BLE001 - 少一层来源，不是这条请求失败
                lease = None
        return member_conversation_summary(
            conversation,
            backend_id=binding.backend_id if binding is not None else None,
            run_state=await host.run_state_of(conversation),
            surface=conversation_surface(conversation, lease=lease),
        )

    async def _members_of(
        group: CollaborationSession, *, include_left: bool = False
    ) -> list[dict[str, Any]]:
        rows = await repositories.members.list_for_collaboration(
            group.id, include_left=include_left
        )
        names = await _display_names(group)
        return [
            member_to_wire(
                member,
                conversation=await _conversation_summary(member.conversation_id),
                display_name=names.get(member.id),
                is_leader=not coordinator_enabled and member.id == group.leader_member_id,
            )
            for member in rows
        ]

    async def _member_wire(
        group: CollaborationSession, member: CollaborationMember
    ) -> dict[str, Any]:
        """单个成员的 wire 行（带 ``displayName``）。

        批次四十六（PRD §B10-2）：显示名由**后端那张共用表**算（含 ``#2`` 后缀），
        所以每一处回成员行的地方都得带上它——真机截图里时间线显示两个 ``media``，
        而房间说明里它们是 ``media`` / ``media#2``：界面一个真相、引擎另一个真相，
        用户分不出成员自己说的「@media#2 负责执行与质检」指的是谁。
        """
        names = await _display_names(group)
        return member_to_wire(
            member,
            conversation=await _conversation_summary(member.conversation_id),
            display_name=names.get(member.id),
            is_leader=not coordinator_enabled and member.id == group.leader_member_id,
        )

    # --- 变更通知（口径 4） -------------------------------------------------- #

    # --- Group 自己的时间线（第 1 件） --------------------------------------- #

    #: 成员变更 → 时间线上那一行系统消息的正文模板。写成中文人话而不是事件名：
    #: 这一行是给用户读的，`member_paused` 不是一句话。
    SYSTEM_LINES = {
        "member_joined": "{who} 加入了这个组",
        "member_left": "{who} 被移出了这个组",
        "member_paused": "{who} 被暂停，暂时不接收组内消息",
        "member_resumed": "{who} 恢复参与",
        "member_promoted": "{who} 被保留为普通项目会话",
    }

    async def _append_message(
        *,
        group: CollaborationSession,
        kind: str,
        content: str,
        author_type: str = "user",
        author_member_id: str | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CollaborationMessage:
        """往 Group 自己的时间线追加一行（``collaboration_messages``）。

        AD-13：这张表是 **Group 拥有的数据**，没有 ``expires_at``，不随事件重放
        缓冲过期——所以「这个组当时对谁说了什么、送到了没有」在缓冲被清之后仍然
        查得到。``sequence`` 由仓储现算，是浮窗断线续传的游标。
        """
        sequence = await repositories.group_messages.next_sequence(group.id)
        return await repositories.group_messages.append(
            CollaborationMessage.create(
                collaboration_session_id=group.id,
                sequence=sequence,
                kind=kind,  # type: ignore[arg-type]
                author_type=author_type,  # type: ignore[arg-type]
                author_member_id=author_member_id,
                conversation_id=conversation_id,
                content=content,
                metadata=metadata,
            )
        )

    async def _append_system_line(
        *,
        group: CollaborationSession,
        change: str,
        member: CollaborationMember,
        conversation: Conversation | None,
        display_name: str | None = None,
    ) -> CollaborationMessage | None:
        """成员变更在时间线上留一行 ``kind="system"``。

        为什么成员变更也进时间线：任务书第 1 件写的是「每条广播/定向/**成员
        变更**在 Group 自己的时间线里各一行」。理由很实在——用户回头看这个组
        时，「A 是在我发那条广播之前还是之后进来的」决定了他要不要给 A 补发一次。
        只有消息没有成员变更的时间线答不了这个问题。

        批次五十二第 2 件（真机 UI-03）：``{who}`` 从**会话标题**改成
        :func:`resolve_display_names` 那张共用表里的显示名——第二条标题同为 media
        的会话加进来时，这一行此前写的是「media 加入了这个组」，而它在房间说明里
        被告知自己叫 ``media#2``，成员栏与时间线气泡（批次四十六）也已经这么叫它。
        同一个组、同一个人，三处名字得是一个。

        这张表**现算**（此时成员行已经落库，所以新来的那位算得进去）。移出那一条
        是例外：``list_for_collaboration`` 默认不带 ``left``，他一走名字就不在表里
        了，所以调用方要在**动手之前**把名字取出来交给 ``display_name``——「刚被
        移出的那位叫什么」正是这一行唯一要说的事。两条路都走不通（老数据）时退回
        原来那条链：会话标题 > roleLabel > 会话 id。
        """
        template = SYSTEM_LINES.get(change)
        if template is None:
            return None
        names = await _display_names(group)
        who = (
            display_name
            or names.get(member.id)
            or (conversation.title if conversation is not None else None)
            or member.role_label
            or member.conversation_id
        )
        return await _append_message(
            group=group,
            kind="system",
            author_type="system",
            content=template.format(who=who),
            metadata={
                "change": change,
                "memberId": member.id,
                "conversationId": member.conversation_id,
            },
        )

    # --- 组长（PRD §A6 / §B6，批次四十八） ----------------------------------- #

    async def _leader_line(
        group: CollaborationSession, template: str, **names: str
    ) -> CollaborationMessage:
        """组长变了 → 时间线上一行 system。

        **每一次换人都写一行**（自动指定 / 用户改 / 自动移交三条路都写）：组长决定
        房间什么时候停，「现在是谁」因此是用户读这条时间线时必须答得出的事——尤其是
        自动移交那一次，它发生在用户没做任何事的时候。
        """
        return await _append_message(
            group=group,
            kind="system",
            author_type="system",
            content=template.format(**names),
            metadata={"change": "leader_changed", "leaderMemberId": group.leader_member_id},
        )

    async def _set_leader(
        group: CollaborationSession,
        leader_member_id: str | None,
        *,
        template: str = LEADER_SET_LINE,
        previous_name: str | None = None,
    ) -> CollaborationSession:
        """把组长换成这一位并写一行 system。已经是他了就什么都不做。"""
        if group.leader_member_id == leader_member_id:
            return group
        updated = await repositories.collaborations.save(
            group.evolve(leader_member_id=leader_member_id, updated_at=_now())
        )
        if leader_member_id is None:
            # 一个 active 成员都没有：不写「（无）是组长」这种话（AD-71），
            # 房间下一次要收口时会由终止 6 说出「没有可收口的成员」。
            return updated
        names = await _display_names(updated)
        name = names.get(leader_member_id, leader_member_id)
        await _leader_line(
            updated,
            template,
            leader=name,
            **({"previous": previous_name} if previous_name else {}),
        )
        return updated

    async def _ensure_leader(group: CollaborationSession) -> CollaborationSession:
        """组里还没有组长、而现在有 active 成员 → 指定**最早加入的那位**。

        默认最早加入（PRD §A6）：那正是前三版里悄悄写最终答复的「队首」，这一批把它
        露出来。成员行本来就按加入时间排，所以「第一位 active」就是答案。
        """
        if coordinator_enabled or group.leader_member_id is not None:
            return group
        rows = await repositories.members.list_for_collaboration(group.id)
        first = next((m for m in rows if m.participation_state == "active"), None)
        if first is None:
            return group
        return await _set_leader(group, first.id)

    async def _leader_left(
        group: CollaborationSession,
        member: CollaborationMember,
        *,
        display_name: str | None = None,
    ) -> CollaborationSession:
        """这一位刚暂停 / 离开 → 他要是组长，自动移交（PRD §B4 终止 6）。

        移交给**最早加入的其他 active 成员**；一个都没有就置 ``None``——房间此后
        没有可收口的人，如实说，不硬找一个已经不在的人来收口。
        """
        if coordinator_enabled or group.leader_member_id != member.id:
            return group
        # 他的名字要在**他已经不在名册上**之后还说得出来（移出的成员不进那张共用
        # 表）。batch52 第 2 件：调用方在动手之前取到的那个名字（含 `#2`）优先，
        # 没有才自己退一档：roleLabel > 会话标题 > 成员 id 尾段。
        names = await _display_names(group)
        gone = await repositories.conversations.get(member.conversation_id)
        previous = (
            display_name
            or names.get(member.id)
            or member.role_label
            or (gone.title if gone is not None else None)
            or member.id
        )
        rows = await repositories.members.list_for_collaboration(group.id)
        heir = next(
            (
                m
                for m in rows
                if m.participation_state == "active" and m.id != member.id
            ),
            None,
        )
        return await _set_leader(
            group,
            heir.id if heir is not None else None,
            template=LEADER_HANDOVER_LINE,
            previous_name=previous,
        )

    async def _notify(
        *,
        group: CollaborationSession,
        change: str,
        member: CollaborationMember | None = None,
        conversation: Conversation | None = None,
        message_id: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """发一条 ``kaus/group.changed``：有会话就进那条会话的流，同时上全局流。

        两条路送同一份 ``data``（:func:`group_change_data`）：会话流是给正在看
        那条会话的人（「这条会话被拉进/移出了某个组」），全局流是给右下角浮窗
        （「有组变了，去重取一次列表」）。谁先到用谁。
        """
        if message_id is None and member is not None:
            row = await _append_system_line(
                group=group,
                change=change,
                member=member,
                conversation=conversation,
                display_name=display_name,
            )
            message_id = row.id if row is not None else None
        data = group_change_data(
            group_id=group.id,
            change=change,
            member_id=member.id if member is not None else None,
            conversation_id=(
                conversation.id
                if conversation is not None
                else (member.conversation_id if member is not None else None)
            ),
            message_id=message_id,
        )
        if conversation is not None:
            await host.emit_conversation_event(
                conversation,
                ExtensionEvent(
                    namespace=GROUP_EVENT_NAMESPACE,
                    name=GROUP_EVENT_NAME,
                    data=dict(data),
                ),
            )
        events.publish(data)
        return data

    # --- 组 ----------------------------------------------------------------- #

    @_endpoint
    async def create_group(body: CreateGroupBody = Body(...)) -> Any:
        home_project_id: str | None = None
        if body.home_project_id:
            home_project_id = normalize_project_id(body.home_project_id)
            if await repositories.projects.get(home_project_id) is None:
                raise _not_found(
                    "project_not_found", f"未知 Project：{body.home_project_id}"
                )
        group = await repositories.collaborations.save(
            CollaborationSession.create(
                title=body.title.strip(), home_project_id=home_project_id
            )
        )
        if coordinator_enabled:
            default = await coordinator.defaults()
            settings = {"coordinationVersion": 1}
            if default:
                settings[CONFIG_KEY] = {**default, "revision": 1, "sourceRevision": default["revision"]}
            group = await repositories.collaborations.save(group.evolve(settings=settings))
        await _notify(group=group, change="group_created")
        return JSONResponse(
            status_code=201, content=_group_wire(group, member_count=0)
        )

    @_endpoint
    async def list_groups(status: str = Query("active")) -> Any:
        if status not in ("active", "all"):
            raise GroupApiError(
                400,
                "invalid_status_filter",
                "status 只接受 active 或 all",
                detail={"status": status},
            )
        groups = (
            await repositories.collaborations.list_all()
            if status == "all"
            else await repositories.collaborations.list_active()
        )
        counts: dict[str, int] = {}
        conversation_ids: dict[str, list[str]] = {}
        for group in groups:
            members = await repositories.members.list_for_collaboration(group.id)
            counts[group.id] = len(members)
            # 批次二十六第 4 件：列表行直接带成员会话 id，浮窗不必为了对一下集合
            # 而对每个组再打一次详情端点。
            conversation_ids[group.id] = [m.conversation_id for m in members]
        result = group_list_to_wire(
            groups, member_counts=counts, member_conversation_ids=conversation_ids
        )
        if coordinator_enabled:
            for item in result["groups"]:
                item["coordinatorEnabled"] = True
                item["leaderMemberId"] = None
        return result

    @_endpoint
    async def get_group(group_id: str) -> Any:
        group = await _load_group(group_id)
        members = await _members_of(group)
        return {
            "group": _group_wire(group, member_count=len(members)),
            # N §9.4：每次现算，不是创建时快照。
            "members": members,
            "memberCount": len(members),
        }

    @_endpoint
    async def patch_group(group_id: str, body: PatchGroupBody = Body(...)) -> Any:
        group = await _load_open_group(group_id)
        title = (body.title or "").strip() or None
        status = (body.status or "").strip() or None
        round_cap = body.round_cap
        leader_member_id = (body.leader_member_id or "").strip() or None
        if title is None and status is None and round_cap is None and leader_member_id is None:
            raise GroupApiError(
                400,
                "empty_patch",
                "请求里没有任何要改的项（title / status / roundCap / leaderMemberId 至少给一个）",
            )
        if status is not None and status not in ("active", "minimized"):
            raise GroupApiError(
                400,
                "invalid_status",
                "status 只接受 active 或 minimized；关组请走 POST /close",
                detail={"status": status},
            )
        if round_cap is not None and not (
            MIN_ROUND_CAP <= round_cap <= MAX_ROUND_CAP
        ):
            # 这个数直接乘以队列长度就是引擎调用次数，所以上限是**产品口径**而不是
            # 防御（PRD §B4「成本」）：在这里挡住，比让用户设 500 之后收到账单好。
            raise GroupApiError(
                400,
                "invalid_round_cap",
                f"roundCap 只接受 {MIN_ROUND_CAP}–{MAX_ROUND_CAP} 之间的整数",
                detail={"roundCap": round_cap},
            )
        if leader_member_id is not None and coordinator_enabled:
            raise GroupApiError(400, "independent_coordinator", "请使用组长配置入口")
        if leader_member_id is not None:
            # 组长必须是**还活跃**的成员：一个暂停 / 已移出的组长等于一个永远收不了
            # 口的房间，而那正是终止 6 在防的事。400 而不是静默改一个别人，是因为
            # 用户点的就是这一位——替他挑另一个人，他不会知道。
            candidate = await repositories.members.get(leader_member_id)
            if (
                candidate is None
                or candidate.collaboration_session_id != group.id
                or candidate.participation_state != "active"
            ):
                raise GroupApiError(
                    400,
                    "leader_not_active",
                    "组长必须是这个组里还在参与的成员",
                    detail={"leaderMemberId": leader_member_id},
                )
        changes: dict[str, Any] = {"updated_at": _now()}
        if title is not None:
            changes["title"] = title
        if status is not None:
            changes["status"] = status
        if round_cap is not None:
            changes["settings"] = {**(group.settings or {}), ROUND_CAP_KEY: round_cap}
        updated = await repositories.collaborations.save(group.evolve(**changes))
        if leader_member_id is not None:
            updated = await _set_leader(updated, leader_member_id)
        await _notify(group=updated, change="group_updated")
        return {"group": _group_wire(updated)}

    @_endpoint
    async def close_group(
        group_id: str, body: CloseGroupBody | None = Body(None)
    ) -> Any:
        """关组：先对每个成员会话按 ``retention`` 做处置，再把组置 ``closed``。

        顺序不能换——处置要读成员行，而成员行在组关掉之后仍然留着（它是这次
        关组做了什么的账本）。成员**不**被标 ``left``：他们是关组时在场的人，
        抹掉这件事等于让「这个组当时有谁」变得查不到。
        """
        group = await _load_open_group(group_id)
        on_close = ((body.on_close if body is not None else None) or "keep").strip()
        if on_close not in ON_CLOSE_CHOICES:
            raise GroupApiError(
                400,
                "invalid_on_close",
                "onClose 只接受 keep 或 archive",
                detail={"onClose": on_close},
            )
        if coordinator_enabled:
            await coordinator.stop(group.id, reason="closed")
            group = await _load_open_group(group.id)
        dispositions: list[dict[str, Any]] = []
        for member in await repositories.members.list_for_collaboration(group.id):
            conversation = await repositories.conversations.get(member.conversation_id)
            if conversation is None:
                dispositions.append(
                    {
                        "memberId": member.id,
                        "conversationId": member.conversation_id,
                        "retention": None,
                        "action": "missing",
                    }
                )
                continue
            action = _close_disposition(conversation, on_close=on_close)
            updated = _apply_disposition(conversation, action)
            if updated is not conversation:
                saved = await repositories.conversations.save(updated)
                host.update_conversation(saved)
            dispositions.append(
                {
                    "memberId": member.id,
                    "conversationId": conversation.id,
                    "retention": conversation.retention,
                    "action": action,
                }
            )
        closed = await repositories.collaborations.save(group.close())
        await _notify(group=closed, change="group_closed")
        return {
            "group": _group_wire(closed, member_count=len(dispositions)),
            "onClose": on_close,
            "dispositions": dispositions,
        }

    # Group materials are explicit user selections, never Project capabilities.
    async def _editable_materials(group_id: str) -> CollaborationSession:
        group = await _load_open_group(group_id)
        if group.thread and group.thread.status in ("running", "closing"):
            raise GroupApiError(409, "materials_in_use", "请先停止讨论，再修改资料")
        return group

    async def _material_from_body(group: CollaborationSession, body: MaterialBody,
                                  previous: GroupMaterial | None = None) -> GroupMaterial:
        content, title = body.content.strip(), body.title.strip()
        source_label = body.source_label
        if not content or not title:
            raise GroupApiError(400, "invalid_material", "标题和内容不能为空")
        if body.source_kind == "group_message":
            message = await repositories.group_messages.get(body.source_id or "")
            if message is None or message.collaboration_session_id != group.id:
                raise _not_found("message_not_found", "本组没有这条消息")
            # The editor submits the user-reviewed selection, with a source reference.
            source_label = "组内消息"
        elif body.source_kind == "conversation":
            source = await _load_conversation(body.source_id or "")
            source_label = source.title
        elif body.source_kind not in ("note", "file"):
            raise GroupApiError(400, "invalid_material", "不支持的资料来源")
        if body.source_kind in ("note", "file") and body.source_id is not None:
            raise GroupApiError(400, "invalid_material", "资料来源不匹配")
        fields = dict(title=title, content=content, source_kind=body.source_kind,
                      source_id=body.source_id, source_label=source_label,
                      updated_at=datetime.now(timezone.utc))
        try:
            return previous.evolve(**fields) if previous else GroupMaterial(**fields)
        except ValueError as exc:
            raise GroupApiError(400, "invalid_material", "资料内容过长或格式不正确") from exc

    async def _store_materials(group: CollaborationSession, items: Sequence[GroupMaterial],
                               expected_revision: int) -> dict[str, Any]:
        try:
            saved = await repositories.group_materials.save(
                GroupMaterials(group_id=group.id, items=tuple(items)),
                expected_revision=expected_revision,
            )
        except MaterialConflict as exc:
            raise GroupApiError(409, "materials_conflict", str(exc)) from exc
        except ValueError as exc:
            raise GroupApiError(400, "materials_limit", "最多 20 条资料，标题与内容合计不超过 12,000 字符") from exc
        await _notify(group=group, change="materials_updated")
        return saved.wire()

    @_endpoint
    async def get_materials(group_id: str) -> Any:
        await _load_group(group_id)
        return (await repositories.group_materials.get(group_id)).wire()

    @_endpoint
    async def add_material(group_id: str, body: MaterialBody = Body(...)) -> Any:
        group = await _editable_materials(group_id)
        current = await repositories.group_materials.get(group.id)
        item = await _material_from_body(group, body)
        return await _store_materials(group, (*current.items, item), body.expected_revision)

    @_endpoint
    async def edit_material(group_id: str, material_id: str, body: MaterialBody = Body(...)) -> Any:
        group = await _editable_materials(group_id)
        current = await repositories.group_materials.get(group.id)
        previous = next((x for x in current.items if x.id == material_id), None)
        if previous is None:
            raise _not_found("material_not_found", "资料不存在")
        item = await _material_from_body(group, body, previous)
        return await _store_materials(group, tuple(item if x.id == material_id else x for x in current.items), body.expected_revision)

    @_endpoint
    async def delete_material(group_id: str, material_id: str, expected_revision: int = Query(..., alias="expectedRevision", ge=0)) -> Any:
        group = await _editable_materials(group_id)
        current = await repositories.group_materials.get(group.id)
        items = tuple(x for x in current.items if x.id != material_id)
        if len(items) == len(current.items):
            raise _not_found("material_not_found", "资料不存在")
        return await _store_materials(group, items, expected_revision)

    # --- 成员 --------------------------------------------------------------- #

    async def _reject_if_duplicate(
        conversation_id: str, *, group: CollaborationSession
    ) -> None:
        """同一条会话在同一个组里只能有一条活跃成员（N §9.2 的反面）。"""
        for member in await repositories.members.list_for_collaboration(group.id):
            if conversation_id in (member.source_conversation_id, member.conversation_id):
                raise GroupApiError(
                    409,
                    "member_duplicate",
                    "这条会话已经是本组成员",
                    detail={"memberId": member.id, "conversationId": conversation_id},
                )

    @_endpoint
    async def add_member(group_id: str, body: AddMemberBody = Body(...)) -> Any:
        group = await _load_open_group(group_id)
        conversation = await _load_conversation(body.conversation_id)
        await _reject_if_duplicate(conversation.id, group=group)
        source = conversation
        owns_session = (source.created_by_collaboration_id == group.id and source.visibility == "group_only")
        if not owns_session:
            conversation = await repositories.conversations.save(
                Conversation.create_group_spawned(
                    project_id=source.project_id,
                    agent_binding_id=source.agent_binding_id,
                    title=source.title,
                    collaboration_id=group.id,
                    model_id=source.model_id,
                    provider_id=source.provider_id,
                    reasoning_mode=source.reasoning_mode,
                    retention="persistent",
                ).evolve(approval_mode=source.approval_mode, execution_mode=source.execution_mode)
            )
        member = await repositories.members.save(
            CollaborationMember.create(
                collaboration_session_id=group.id,
                conversation_id=conversation.id,
                join_mode="existing",
                source_conversation_id=None if owns_session else source.id,
                role_label=(body.role or "").strip() or None,
            )
        )
        await _notify(
            group=group,
            change="member_joined",
            member=member,
            conversation=conversation,
        )
        # 组里还没有组长 → 这一位（最早加入的 active 成员）就是（PRD §B6）。
        group = await _ensure_leader(group)
        return JSONResponse(
            status_code=201,
            content={"member": await _member_wire(group, member)},
        )

    @_endpoint
    async def remove_member(group_id: str, member_id: str) -> Any:
        """移出：只标 ``left``，**不删**成员行、不删会话、不删原生 Session。

        为什么不 ``delete``：N §9.4 允许「稍后重新加入」，而重新加入的入口是
        ``/resume``——把行删了，那条路就只能靠再 POST 一次 members 重建，
        「这个人来过又走了」这件事也随之消失。
        """
        group = await _load_open_group(group_id)
        member = await _load_member(group, member_id)
        if member.participation_state == "left":
            # 已经走了。再移出一次的结果与第一次一模一样——不报错。
            return {"member": await _member_wire(group, member), "left": True}
        # batch52 第 2 件：名字要在他**还在名册上**的时候取。移出之后那张共用表
        # 里就没有他了（`list_for_collaboration` 默认不带 `left`），`#2` 也就跟着
        # 没了——而「被移出的是 media 还是 media#2」正是这一行唯一要说清的事。
        display_name = (await _display_names(group)).get(member.id)
        left = await repositories.members.save(member.leave())
        conversation = await repositories.conversations.get(member.conversation_id)
        await _notify(
            group=group,
            change="member_left",
            member=left,
            conversation=conversation,
            display_name=display_name,
        )
        # 走的是组长 → 自动移交给最早加入的其他 active 成员（PRD §B4 终止 6）。
        group = await _leader_left(group, left, display_name=display_name)
        return {"member": await _member_wire(group, left), "left": True}

    @_endpoint
    async def pause_member(group_id: str, member_id: str) -> Any:
        group = await _load_open_group(group_id)
        member = await _load_member(group, member_id)
        if member.participation_state == "left":
            raise GroupApiError(
                409,
                "member_left",
                "这个成员已经被移出，不能暂停；要让他回来请用 /resume",
                detail={"memberId": member.id},
            )
        if member.participation_state == "paused":
            return {"member": await _member_wire(group, member)}
        paused = await repositories.members.save(member.pause())
        conversation = await repositories.conversations.get(member.conversation_id)
        await _notify(
            group=group, change="member_paused", member=paused, conversation=conversation
        )
        group = await _leader_left(group, paused)
        return {"member": await _member_wire(group, paused)}

    @_endpoint
    async def resume_member(group_id: str, member_id: str) -> Any:
        """恢复。``left`` 的成员走这条路就是 N §9.4 的「重新加入」。

        重新加入要重新过唯一性：他离开之后，同一条会话可能已经被另一条成员行
        （甚至另一个组）接手了。仓储那层也拦一道（两套实现都拦），这里先给出
        一条说得清的 409，而不是让 ``DomainInvariantError`` 漏成 500。
        """
        group = await _load_open_group(group_id)
        member = await _load_member(group, member_id)
        if member.participation_state == "active":
            return {"member": await _member_wire(group, member)}
        if member.participation_state == "left":
            await _reject_if_duplicate(member.source_conversation_id or member.conversation_id, group=group)
        try:
            resumed = await repositories.members.save(member.resume())
        except DomainInvariantError as exc:
            raise GroupApiError(
                409, "member_duplicate", str(exc), detail={"memberId": member.id}
            ) from exc
        conversation = await repositories.conversations.get(member.conversation_id)
        await _notify(
            group=group,
            change="member_resumed",
            member=resumed,
            conversation=conversation,
        )
        # 组长位空着（上一位走的时候组里没有别人）→ 回来的这位接上。
        group = await _ensure_leader(group)
        return {"member": await _member_wire(group, resumed)}

    @_endpoint
    async def patch_member(
        group_id: str, member_id: str, body: PatchMemberBody = Body(...)
    ) -> Any:
        """``PATCH /api/groups/{id}/members/{memberId}``：改组内名字（AD-173）。

        批次五十二第 5 件。真机上测试员找不到「这位在这个房间里叫什么」的入口，
        而会话标题那条路改的是**会话**的名字（那条会话在别处也跟着改）。写
        ``role_label`` 正好是这件事：它在显示名那条回退链的第一位。

        改完立刻**重算**那张共用表：新名字与别人撞上时照旧加 ``#2``，所以时间线上
        那一行写的是重算之后的名字，而不是用户刚敲进去的那一串——用户看到的必须是
        引擎接下来真的会被告知的那个名字。

        时间线上一行 system「X 现在叫 Y」：显示名同时是称呼与地址，改名之前的发言
        仍旧署着旧名字，只说新名字的话用户读不出这两串是同一个人。
        """
        group = await _load_open_group(group_id)
        member = await _load_member(group, member_id)
        if body.role_label is None:
            raise GroupApiError(
                400,
                "empty_patch",
                "请求里没有任何要改的项（roleLabel 至少给一个；空字符串 = 清掉）",
                detail={"memberId": member.id},
            )
        label = body.role_label.strip() or None
        if (member.role_label or None) == label:
            # 已经叫这个名字了：什么都不做，也不往时间线上添一行没内容的话。
            return {"member": await _member_wire(group, member)}
        previous = (await _display_names(group)).get(member.id, member.id)
        saved = await repositories.members.save(member.evolve(role_label=label))
        names = await _display_names(group)
        row = await _append_message(
            group=group,
            kind="system",
            author_type="system",
            content=MEMBER_RENAMED_LINE.format(
                previous=previous, name=names.get(saved.id, saved.id)
            ),
            metadata={
                "change": "member_renamed",
                "memberId": saved.id,
                "conversationId": saved.conversation_id,
            },
        )
        await _notify(
            group=group,
            change="member_renamed",
            member=saved,
            conversation=await repositories.conversations.get(saved.conversation_id),
            message_id=row.id,
        )
        return {"member": await _member_wire(group, saved)}

    @_endpoint
    async def promote_member(group_id: str, member_id: str) -> Any:
        """把 ``group_only`` 的成员会话提升为普通项目会话（N §9.5「保留到项目」）。

        Phase 7 成员能力清单第 7 条的数据面。**组不必关**：用户在组还开着的时候
        就决定「这条会话我要留下」是完全正常的事，关组时那一条会因为
        ``retention`` 已经是 ``persistent`` 而落进 ``kept``。

        已经是 ``project_visible + persistent`` 时是一次无操作，仍回 200——
        「提升一条已经提升过的会话」的结果与「提升成功」一模一样。
        """
        group = await _load_group(group_id)
        member = await _load_member(group, member_id)
        conversation = await _load_conversation(member.conversation_id)
        already = (
            conversation.visibility == "project_visible"
            and conversation.retention == "persistent"
        )
        if already:
            return {
                "member": await _member_wire(group, member),
                "conversation": conversation_to_wire(
                    conversation, run_state=await host.run_state_of(conversation)
                ),
                "promoted": False,
            }
        saved = await repositories.conversations.save(
            conversation.promote_to_project()
        )
        host.update_conversation(saved)
        await _notify(
            group=group, change="member_promoted", member=member, conversation=saved
        )
        return {
            "member": await _member_wire(group, member),
            "conversation": conversation_to_wire(
                saved, run_state=await host.run_state_of(saved)
            ),
            "promoted": True,
        }

    # --- 在组里启动新成员（N §9.3 路径 B） ----------------------------------- #

    async def _resolve_binding(project_id: str, binding_id: str | None) -> Any:
        if binding_id:
            binding = await repositories.bindings.get(binding_id)
            if binding is None:
                raise _not_found("binding_not_found", f"未知 Binding：{binding_id}")
            if binding.project_id != project_id:
                raise GroupApiError(
                    400,
                    "binding_project_mismatch",
                    f"Binding {binding.id} 不属于 Project {project_id}（D-06：不在中途换绑）",
                )
            return binding
        binding = await repositories.bindings.get_default_for_project(project_id)
        if binding is None:
            raise GroupApiError(
                400,
                "binding_required",
                f"Project {project_id} 没有默认 Binding，请显式给 bindingId",
                detail={"projectId": project_id},
            )
        return binding

    async def _send_first_message(
        conversation: Conversation, text: str
    ) -> dict[str, Any]:
        """替用户按一次「发送」：spawn 的首句与组路由的每一次投递都走这里。

        三步与 ``POST /api/conversations/{id}/messages`` 逐字相同：
        ``register_binding``（AD-58：Driver 侧的 Binding 缓存由接入层灌，AD-175
        补上项目的工作目录）→
        ``ensure_runtime`` → ``send_message``，并且**和那条端点一样先挂订阅再发**，
        等到本轮第一个带 ``runId`` 的事件为止。等到了才叫「发出去了」——批次
        二十七之前这里只要 ``send_message`` 没抛异常就报成功，于是「引擎收下了
        但一轮都没起来」在 wire 上和真的发出去了长得一模一样。

        **失败不回滚**（AD-105）：spawn 本身已经成功了（会话与成员都在库里），
        不该因为第一句话没发出去就把它们删掉；删是显式动作。但失败必须**说得清**
        ——批次二十七之前这里把四种完全不同的失败（Driver 没注册 / 引擎起不来 /
        上一轮还在跑 / 这台引擎不接受这个模型）一律压成 ``runtime_start_failed``，
        message 是 ``f"{type(exc).__name__}: {exc}"``：一个用户看不懂、前端也没法
        分支的英文类名。现在每一步各有各的 code，人话经 :func:`plain_text` 规整，
        修法走 ``hint``。

        返回 ``{sent, runId, runIdPending, error}``；``error`` 是
        ``{code, message, hint}``（``hint`` 缺省时不放这个键，不放 null）。
        """
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            return _send_failure(
                "binding_not_found",
                message=f"这条会话指向的引擎绑定不存在：{conversation.agent_binding_id}",
            )
        try:
            driver = registry.get(binding.backend_id)
        except DriverNotRegisteredError:
            # 不用异常原文：``DriverNotRegisteredError`` 也是 ``KeyError``，
            # ``str()`` 会带上一层引号（真机上 detail 里那对多余的双引号）。
            return _send_failure(
                "driver_not_registered",
                message=f"这台机器上还没有可用的引擎「{binding.backend_id}」",
                hint="这台引擎的网关没在运行，或 dashboard-config.json 里没有它",
            )
        await register_binding_snapshot(driver, binding, repositories.projects)

        # 上一轮还在跑就**在任何投递动作之前**短路：与其发出去让引擎拒、再把
        # 网关的 500 翻回来，不如自己先答一句「等它结束」。少一次往返，也少一条
        # 落进时间线却永远等不到回复的用户消息。
        #
        # 先收敛一次再问（AD-136 的同一条路）：丢了终态的那一轮在时间线上会一直
        # 显示 running，不收敛就等于把这条会话**永久**锁成「忙」——那比原来的
        # 500 更糟。
        await host.reconcile_conversation(conversation)
        if await host.run_state_of(conversation) != "idle":
            return _send_failure(
                "turn_already_running",
                message="上一轮还在运行，这一句没有发出去",
                hint="等这一轮结束再发，或先点停止",
            )

        try:
            await host.ensure_runtime(conversation)
        except AgentSpawnError as exc:
            # AD-159：进程压根没拉起来。与「网关不可达」分开一个 reason——组投递
            # 那一行要说的是「去终端跑一次这条命令」，不是「等等再试」。
            return _send_failure(AGENT_SPAWN_FAILED, exc=exc)
        except Exception as exc:  # noqa: BLE001 - 起不来是显式状态，不是崩溃
            return _send_failure("runtime_start_failed", exc=exc)

        # 先挂订阅再发（与会话端点同一条口径）：这样「发出去到 run.started」
        # 之间没有窗口期，等 runId 也不会漏掉已经过去的那一条。
        after = await host.event_store.latest_sequence(conversation.id)
        subscription = host.subscribe(conversation.id, after_sequence=after)
        try:
            await host.send_message(conversation.id, MessageInput(text=text))
            run_id = await _first_run_id(subscription, timeout=run_id_timeout)
        except TurnAlreadyRunningError as exc:
            return _send_failure("turn_already_running", exc=exc)
        except UnsupportedCapabilityError as exc:
            # 这台引擎不接受这条会话的模型快照之类：code 由 Driver 给
            # （`conversation_model_unsupported`），不要压成一句「发送失败」。
            return _send_failure("message_send_failed", exc=exc)
        except Exception as exc:  # noqa: BLE001 - 同上：显式状态，不是崩溃
            return _send_failure("message_send_failed", exc=exc)
        finally:
            subscription.close()
        return {
            "sent": True,
            "error": None,
            "runId": run_id,
            # N §13.1：等不到就说等不到，不合成一个假的 runId。
            "runIdPending": run_id is None,
        }

    def _send_failure(
        code: str,
        exc: BaseException | None = None,
        *,
        message: str | None = None,
        hint: str | None = None,
    ) -> dict[str, Any]:
        """把一次失败整理成 ``error``：稳定 code + 纯文本人话 + 可选修法。

        Driver 认得出根因时（``DriverError.failure``）**以它为准**：那份
        :class:`FailureHint` 里的 code 比这里的分步 code 更具体
        （``turn_already_running`` / ``conversation_model_unsupported`` /
        ``gateway_unreachable``），前端按它分支才分得清「等一等」和「坏了」。
        """
        failure = getattr(exc, "failure", None) if exc is not None else None
        if isinstance(failure, FailureHint):
            code = failure.code or code
            message = failure.message
            hint = hint or failure.hint
        elif message is None:
            # 兜底：连 Driver 都说不出所以然时才用异常原文，且**先剥掉类名**
            # （`plain_text` 的第二步）——`DriverError: …` 不是给用户看的话。
            message = str(exc) if exc is not None else "这一句没有发出去"
        error: dict[str, Any] = {"code": code, "message": plain_text(message or "")}
        if hint:
            error["hint"] = plain_text(hint)
        return {"sent": False, "error": error, "runId": None, "runIdPending": False}

    @_endpoint
    async def spawn_member(group_id: str, body: SpawnMemberBody = Body(...)) -> Any:
        """建一条 ``group_spawned`` 会话并把它加成成员（v1.2 §Phase 2 第三条验收）。

        三个字段取 N §9.5 的建议默认：``origin=group_spawned`` /
        ``visibility=group_only`` / ``retention=decide_on_group_close``，
        并记下 ``created_by_collaboration_id``（N §9.7）。这些默认值住在
        :meth:`Conversation.create_group_spawned` 里，本层不再重述一遍。
        """
        group = await _load_open_group(group_id)
        project_id = normalize_project_id(body.project_id)
        if await repositories.projects.get(project_id) is None:
            raise _not_found("project_not_found", f"未知 Project：{body.project_id}")
        binding = await _resolve_binding(project_id, body.binding_id)
        conversation = await repositories.conversations.save(
            Conversation.create_group_spawned(
                project_id=project_id,
                agent_binding_id=binding.id,
                title=(body.title or "").strip() or binding.display_name,
                collaboration_id=group.id,
                model_id=(body.model_id or "").strip() or None,
            )
        )
        member = await repositories.members.save(
            CollaborationMember.create(
                collaboration_session_id=group.id,
                conversation_id=conversation.id,
                join_mode="spawned_in_group",
                role_label=(body.role or "").strip() or None,
            )
        )
        await _notify(
            group=group,
            change="member_joined",
            member=member,
            conversation=conversation,
        )
        group = await _ensure_leader(group)
        initial: dict[str, Any] | None = None
        text = (body.initial_message or "").strip()
        if text:
            room = await _room_members(group)
            header = (room_header(group_title=group.title, members=room, recipient_member_id=member.id,
                                  leader_member_id=group.leader_member_id) or "")
            header += material_context((await repositories.group_materials.get(group.id)).wire())
            initial = await _send_first_message(conversation, with_room_header(header, text))
        # 首句可能已经改了会话状态（绑上原生 Session 之类），回读一次最新的。
        latest = await repositories.conversations.get(conversation.id) or conversation
        return JSONResponse(
            status_code=201,
            content={
                "member": await _member_wire(group, member),
                "conversation": conversation_to_wire(
                    latest, run_state=await host.run_state_of(latest)
                ),
                # 没给首句时整个键是 null，而不是一个 sent=false 的假失败。
                # 给了就一定带 `sent` / `runId` / `runIdPending`，失败时另带
                # `error{code,message,hint}`（批次二十七第 1 件）。
                "initialMessage": initial,
            },
        )

    # --- 路由：广播 / 定向（第 1、2 件 / AD-153） ----------------------------- #

    # --- 房间说明（PRD §B3 / 批次四十五 a） ---------------------------------- #

    async def _engine_label(conversation: Conversation | None) -> str | None:
        """这条会话跑在哪台引擎上：``backend:<key>`` → ``<key>``。

        刻意**不用** ``binding.display_name``：它的缺省值是 ``<backend>:<slug>``
        （形如 ``xxx:media``），出现在一句给人读的话里像一段配置而不像一个引擎名。
        取不到就是 ``None``，那时名片上括号整个不出现（AD-71）。
        """
        if conversation is None:
            return None
        binding = await repositories.bindings.get(conversation.agent_binding_id)
        if binding is None:
            return None
        return binding.backend_id.split(":", 1)[-1] or None

    async def _room_members(group: CollaborationSession) -> list[RoomMember]:
        """名片用的成员表：**一次取齐**，所有收件人共用同一份。

        共用是必须的而不是省事：显示名的 ``#2`` 后缀由这张表定，每个收件人各算
        一次的话，同一轮广播里两个人看到的「组里还有谁」可能编号不同。

        ``listed`` 只给 ``active``（PRD §B3 末句：暂停 / 离开的不列入「组里还有」）；
        但暂停的人仍留在表里占着自己的名字，见 :class:`RoomMember` 的 docstring。
        """
        rows = await repositories.members.list_for_collaboration(group.id)
        entries: list[RoomMember] = []
        for member in rows:
            conversation = await repositories.conversations.get(member.conversation_id)
            name = member.role_label or (conversation.title if conversation is not None else None) or ""
            if coordinator_enabled and name.strip() == "组长":
                name = "组长（成员）"
            entries.append(
                RoomMember(
                    member_id=member.id,
                    raw_name=name,
                    engine=await _engine_label(conversation),
                    listed=member.participation_state == "active",
                )
            )
        return entries

    async def _display_names(group: CollaborationSession) -> dict[str, str]:
        """``memberId → 显示名``（含重名后缀 ``#2``），**和名片用的是同一张表**。

        批次四十六（PRD §B10-2）：这张表此前只用来拼给引擎看的名片，没上 wire，
        于是界面自己按「roleLabel > 标题 > id 尾段」再算一次——算得出名字，算不出
        后缀。两个都叫 ``media`` 的成员因此在时间线上长得一模一样，而它们在房间里
        被告知自己叫 ``media`` 与 ``media#2``。同一个组，两个真相。
        """
        return resolve_display_names(await _room_members(group))

    async def _deliver(
        member: CollaborationMember,
        text: str,
        *,
        room: Sequence[RoomMember] | None = None,
        group_title: str = "",
        header: str | None = None,
        leader_member_id: str | None = None,
    ) -> dict[str, Any]:
        """把一条用户消息投给一个成员，返回 ``deliveries[]`` 的一行。

        **不中断其它成员**：这个函数从不抛异常，它把每一种结局翻成一个状态字。
        广播的语义是「尽力送到每个人」——一个成员的引擎起不来，不该让另外三个人
        也收不到；而「谁没收到、为什么」必须留在时间线上，否则用户会以为全送到了。

        走的是 ``POST /api/conversations/{id}/messages`` 的同三步
        （``register_binding`` AD-58 → ``ensure_runtime`` → ``send_message``），
        不是另一条捷径：路由只是「替用户按了 N 次发送」，不该有第二套发送语义。
        """
        row: dict[str, Any] = {
            "memberId": member.id,
            "conversationId": member.conversation_id,
        }
        # 三种「不投」都在**任何投递动作之前**短路：暂停的成员一次网关往返都不该
        # 发生（批次二十七第 2 件——真机上定向发给一个暂停成员，摘要里出现的却是
        # 一条 `HTTP 500`）。
        if member.participation_state == "left":
            return {
                **row,
                "status": "skipped_left",
                "reason": "member_left",
                "detail": "这个成员已被移出本组",
            }
        if member.participation_state == "paused":
            return {
                **row,
                "status": "skipped_paused",
                "reason": "member_paused",
                "detail": "这个成员已暂停参与",
            }
        if member.participation_state == "failed":
            return {
                **row,
                "status": "skipped_paused",
                "reason": "member_join_failed",
                "detail": "这个成员上一次加入失败，暂不投递",
            }
        conversation = await repositories.conversations.get(member.conversation_id)
        if conversation is None:
            # 会话被删了（v1.0 §16.6 允许），成员行还在。如实说，不当成功。
            return {
                **row,
                "status": "failed",
                "reason": "conversation_missing",
                "detail": "这条会话已经不存在了",
            }
        # 房间说明只加在**投递给引擎**的这一份上（PRD §B3）：时间线上落的仍是
        # 用户原文，见 `_route` 里那一行的 `metadata.roomHeaderVersion`。
        #
        # 批次四十五 b：房间循环自己会先把**房间增量**（§B4 的扩版名片）拼好传进来，
        # 那时这里不再拼第二份——两处各拼一次就会出现「界面说轮到 B、发给 B 的那份
        # 却没写本轮发言人」这类只能靠对读两段代码才发现的偏差。
        if header is None:
            header = (
                room_header(
                    group_title=group_title,
                    members=room,
                    recipient_member_id=member.id,
                    leader_member_id=leader_member_id,
                )
                if room
                else None
            )
        outcome = await _send_first_message(
            conversation, with_room_header(header, text)
        )
        if outcome["sent"]:
            sent = {**row, "status": "sent", "runId": outcome.get("runId")}
            if header:
                # 「这个人收到的那一份带了名片」是投递这一行的事实，不是整条消息
                # 的：定向发给一个暂停成员时压根没投，那一行当然不该说带了名片。
                sent["roomHeaderVersion"] = ROOM_HEADER_VERSION
            if outcome.get("runIdPending"):
                # 引擎收下了，但这一轮还没起来。说出来——否则它和「真的开跑了」
                # 在界面上一模一样，而用户要等的正是那个区别。
                sent["runIdPending"] = True
            return sent
        error = outcome["error"] or {}
        # 批次二十七：`detail` 是**给人看的一句话**，不再是 `code：类名: 原文`。
        # 机器要分支就看 `reason`（稳定 code），两者各司其职。
        detail = error.get("message") or "这一句没有发出去"
        if error.get("hint"):
            detail = f"{detail}（{error['hint']}）"
        return {
            **row,
            "status": "failed",
            "reason": error.get("code", "send_failed"),
            "detail": plain_text(detail),
        }

    # --- 房间循环（PRD §B4 / 批次四十五 b） ----------------------------------- #

    #: ``(group_id, member_id) → (deliveredSince, deliveredTo)``：最近一次投给他的
    #: 那份增量截的是时间线的哪一段。它落在**下一条** ``member_turn`` 的 metadata
    #: 上（PRD §B4「记录」/ L8）。
    #:
    #: 只放进程内存，不进库：它唯一的用处是「这一轮的答案回来时，把那次投递的游标
    #: 抄到那一行上」，而线程本来就活不过一次重启（重启时 ``running`` 一律收成
    #: ``stopped``）。为它加一列，等于给一件只在几秒内有意义的事开一本永久的账。
    delivery_marks: dict[tuple[str, str], tuple[int | None, int | None]] = {}

    #: 还在飞的「投给下一位」后台任务。留一份强引用只为不被 GC 提前收走
    #: （``asyncio`` 不为任务持有强引用），done 之后自己退出来。
    pending_pumps: set[Any] = set()

    def _schedule_pump(group_id: str, message_id: str | None) -> None:
        """把「投给下一位」丢到一个**后台任务**里，而不是在旁观者里直接投。

        这不是为了快，是为了不死锁：旁观者是在**上一位那条会话的事件流里**被调用
        的（``SessionHost._notify_observers``），在那里 ``await`` 一次完整的投递
        （``ensure_runtime`` + ``send_message`` + 等 ``runId``，最多两秒）就等于把
        A 的事件泵按住两秒；而如果这期间有人要停掉 A（关浮窗、关组、进程退出），
        停就会等泵、泵在等 B，两边互相等着——本批第一版真的这么挂过，现象是测试
        在 ``aclose()`` 上永远不返回。

        任务里抛的异常只记一条 WARNING：推不动下一位是这条线程的事，不该让上一位
        那条会话的事件流跟着断（与 ``add_event_observer`` 的第三条同一个理由）。
        """

        async def _runner() -> None:
            try:
                group = await repositories.collaborations.get(group_id)
                if group is not None:
                    await _pump(group, message_id=message_id)
            except asyncio.CancelledError:  # pragma: no cover - 进程收尾
                raise
            except Exception:  # noqa: BLE001 - 见 docstring
                LOGGER.warning("房间循环推进失败（组 %s）", group_id, exc_info=True)

        task = asyncio.ensure_future(_runner())
        pending_pumps.add(task)
        task.add_done_callback(pending_pumps.discard)

    async def _save_thread(
        group: CollaborationSession, thread: RoomThread | None
    ) -> CollaborationSession:
        return await repositories.collaborations.save(
            group.evolve(thread=thread, updated_at=_now())
        )

    async def _append_delivery(message_id: str, row: dict[str, Any]) -> None:
        """把一次投递补记到**开这条线程的那条用户消息**的 ``deliveries[]`` 上。

        为什么是回填而不是像批次二十六那样先投完再落行：房间增量是**从时间线上读
        出来的**，所以用户那句话必须先在时间线上，第一位成员才可能被告知它。顺序
        一旦反过来，第一位收到的增量里就没有用户刚说的那句。

        回填是 upsert 同一行（``sequence`` 不动，它是 ``IMMUTABLE_FIELDS``），
        所以一条线程转完之后，那一行的 ``deliveries`` 就是这次提问最终发给了谁、
        每一次的结局——正是 AD-153 定下、AD-168 接着守的那本账。
        """
        message = await repositories.group_messages.get(message_id)
        if message is None:  # pragma: no cover - 行刚写完就不见了
            return
        metadata = dict(message.metadata or {})
        deliveries = list(metadata.get("deliveries") or [])
        deliveries.append(row)
        metadata["deliveries"] = deliveries
        if row.get("roomHeaderVersion"):
            metadata["roomHeaderVersion"] = ROOM_HEADER_VERSION
        await repositories.group_messages.append(message.evolve(metadata=metadata))

    async def _deliver_to_speaker(
        group: CollaborationSession,
        thread: RoomThread,
        member: CollaborationMember,
        room: Sequence[RoomMember],
    ) -> dict[str, Any]:
        """给队列里这一位投一次：房间增量 + 分隔行 + 他要回应的那条原文。

        增量的起点是**他自己**的 ``last_delivered_sequence``（PRD §B4）。记在成员
        上而不是线程上，正是为了旁观者：C 在 A、B 辩论的两轮里一个字都没收到，等
        用户说「C 你总结一下」时，他这一次收到的增量从他上次说话之后算起，A 与 B
        那几轮都在里面——L4 要的「旁观增量生效」由这一个游标提供，**不必**为了让
        他「跟上」而每轮给他发一次（那会让一个明说「不说话」的成员每轮都跑一次
        引擎，与 §B4 自己那句「每轮 = 队列长度次引擎调用」也对不上）。
        """
        since = member.last_delivered_sequence
        rows = await repositories.group_messages.list_for_collaboration(
            group.id, after_sequence=since
        )
        entries = public_entries(rows, since_sequence=since)
        delivered_to = rows[-1].sequence if rows else since
        # 第一次投给他时游标是空的。落在 metadata 上的 `deliveredSince` 仍要是一个
        # **数**（L8 要能答出「他当时看见了哪一段」），所以取「最早那一条的前一位」
        # ——它与「从这条之后算起」是同一句话。
        effective_since = (
            since if since is not None else (rows[0].sequence - 1 if rows else None)
        )
        if thread.phase == "closing":
            # 收口轮（PRD §B4）：正文不是「上一位说了什么」，而是一段固定指令——
            # 把讨论收成用户要的东西。所以增量里**一条都不切给正文**，全部留在
            # 「房间里发生了什么」那一段：收口人要读的正是这整段讨论。
            origin = (
                await repositories.group_messages.get(thread.started_by_message_id)
                if thread.started_by_message_id
                else None
            )
            latest = closing_prompt(
                origin.content if origin is not None else "",
                round_cap=group.round_cap,
            )
            body = entries
        elif entries:
            # 「这一轮要你回应的最新一条原文」= 增量里最后那一条；它前面的进「房间
            # 里发生了什么」。分开是 PRD 的格式：正文是要他回应的**那一句**。
            latest = entries[-1].text
            body = entries[:-1]
        else:
            # 上一位略过了、这一位的游标又已经追平——那就把开这条线程的那句话再给
            # 他一次。宁可重复用户的原话，也不要投一条空正文过去。
            origin = (
                await repositories.group_messages.get(thread.started_by_message_id)
                if thread.started_by_message_id
                else None
            )
            latest = (origin.content if origin is not None else "") or ""
            body = ()
        header = room_delta(
            group_title=group.title,
            members=room,
            recipient_member_id=member.id,
            # 「本轮发言人」就是队列本身（批次四十八：没有第二个「有效发言者」的
            # 概念了），组长在末尾自然带上。
            speaker_ids=thread.speaker_queue,
            spectator_ids=thread.spectators,
            entries=body,
            leader_member_id=group.leader_member_id,
        )
        # 游标先记后投：假引擎（和一台很快的真引擎）可以在 `send_message` 还没返回
        # 之前就把整一轮跑完，那条 `run.completed` 于是先于这里的下一行到达旁观者
        # ——投完再记的话，那一行 `member_turn` 上的 `deliveredSince` 就是空的。
        origin = (await repositories.group_messages.get(thread.started_by_message_id)
                  if thread.started_by_message_id else None)
        snapshot = dict(origin.metadata).get("materialsSnapshot") if origin else None
        header = (header or "") + material_context(snapshot)
        delivery_marks[(group.id, member.id)] = (effective_since, delivered_to)
        row = await _deliver(
            member, latest, room=room, group_title=group.title, header=header
        )
        row["materialsRevision"] = snapshot.get("revision", 0) if snapshot else 0
        if row.get("status") == "sent":
            # **投出去了才动游标**：没送到的那一份他一个字都没看到，把游标推过去
            # 等于让这一段永远不再出现在任何人的增量里。
            await repositories.members.save(
                member.evolve(last_delivered_sequence=delivered_to)
            )
        else:
            delivery_marks.pop((group.id, member.id), None)
        return row

    async def _finish_thread_side_effects(
        group: CollaborationSession, thread: RoomThread
    ) -> CollaborationSession:
        """线程停下来 / 进收口轮时在时间线上留一句人话（PRD §B4 / §A3）。

        **为什么停的**决定了这里说什么话——这正是 ``ended_reason`` 存在的理由：
        「组长没再点人」与「一轮里没人说话」在库里都只是一次安静，在界面上却是两句
        完全不同的话（前者是结论已经给了，后者是大家都没吭声）。

        三种不写（AD-71：不重复说）：``stopped`` 是用户自己刚按的（或刚打字说的，
        那一行已经写过了）；``silent`` 那一轮时间线上就是一串折叠的「略过」，它自己
        说得清楚；``closed`` 是触顶那次收口回来了，而那条发言本身就是高亮卡。
        """
        reason = thread.ended_reason
        if reason is None:
            return group
        names = await _display_names(group)
        leader = names.get(
            group.leader_member_id or "", group.leader_member_id or ""
        )
        line: str | None = None
        change = "thread_state"
        if thread.status == "closing" and reason == "round_cap":
            change = "thread_closing"
            line = EXHAUSTED_LINE.format(rounds=group.round_cap, leader=leader)
        elif reason == "leader_closed":
            change = "thread_all_done"
            line = LEADER_CLOSED_LINE.format(leader=leader)
        elif reason == "leader_final":
            change = "thread_all_done"
            line = LEADER_FINAL_LINE.format(leader=leader)
        elif reason == "leader_failed":
            line = f"组长 {leader} 未完成回复，讨论已停止"
        elif reason == "leader_missing":
            change = "thread_no_leader"
            line = LEADER_MISSING_LINE
        if line is None:
            return group
        await _append_message(
            group=group,
            kind="system",
            author_type="system",
            content=line,
            metadata={"change": change, "epoch": thread.epoch},
        )
        return group

    async def _closing_failed(
        group: CollaborationSession, thread: RoomThread, reason: str
    ) -> None:
        """收口那一轮没成：说清楚为什么，然后**不重试**（PRD §B4）。

        重试等于再花一次钱去赌同一件刚刚失败的事；而用户此刻要的不是房间再试一次，
        是知道「它没收成」——否则组头一直挂着「正在收口」，永远等不到那张卡。
        """
        await _append_message(
            group=group,
            kind="system",
            author_type="system",
            content=CLOSING_FAILED_LINE.format(reason=plain_text(reason)),
            metadata={"change": "thread_closing_failed", "epoch": thread.epoch},
        )

    async def _pump(
        group: CollaborationSession, *, message_id: str | None
    ) -> CollaborationSession:
        """把队列往前推，直到**有人真的收到了**或者线程停下来。

        「收到了」之后就地返回：接下来由旁观者（``_record_member_turn``）在那一轮
        跑完时再叫一次这里。这就是 PRD §B4 的「串行」——一轮内一个说完再投下一个，
        并行会让两个人同时回一个已经过时的房间。

        投不出去（暂停 / 已移出 / 引擎没起来 / 上一轮还在跑）的那一位**按「这一轮
        他没说话」算**，立刻推给下一位：等一个永远不会来的 ``run.completed`` 会把
        整个房间挂死，而那比少一个人发言严重得多。一整轮没有一个人说出话时，状态机
        按「全员没说话 → idle」自己停住。

        组长或收口轮投不出去时停止本次讨论并留下失败提示，不自动换人或重试。
        """
        for _ in range(MAX_PUMP_STEPS):
            thread = group.thread
            if thread is None or not thread.is_awaiting:
                return group
            member_id = thread.awaiting_member_id
            if member_id is None:  # pragma: no cover - 在等人才会走到这里
                return group
            member = await repositories.members.get(member_id)
            room = await _room_members(group)
            row: dict[str, Any]
            if member is None:
                row = {
                    "memberId": member_id,
                    "conversationId": None,
                    "status": "skipped_left",
                    "reason": "member_left",
                    "detail": "这个成员已经不在了",
                }
            else:
                row = await _deliver_to_speaker(group, thread, member, room)
            if message_id is not None:
                await _append_delivery(message_id, row)
            if row.get("status") == "sent":
                return group
            if thread.phase == "closing":
                stopped = thread.evolve(
                    status="stopped",
                    awaiting_member_id=None,
                    ended_reason="closing_failed",
                )
                group = await _save_thread(group, stopped)
                await _closing_failed(
                    group, stopped, row.get("detail") or row.get("reason") or "投不出去"
                )
                await _notify(group=group, change="thread_state")
                return group
            active_ids = _active_ids(await _members_rows(group))
            advanced = advance_thread(
                thread,
                member_id=member_id,
                passed=True,
                final=False,
                outcome=row.get("reason") or "delivery_failed",
                next_queue=await _next_queue(group, thread, room, active_ids),
                active_member_ids=active_ids,
                leader_member_id=_active_leader(group, active_ids),
                round_cap=group.round_cap,
            )
            group = await _save_thread(group, advanced)
            group = await _finish_thread_side_effects(group, advanced)
            await _notify(group=group, change="thread_state")
        return group  # pragma: no cover - MAX_PUMP_STEPS 是第二道保险

    async def _members_rows(
        group: CollaborationSession,
    ) -> Sequence[CollaborationMember]:
        return await repositories.members.list_for_collaboration(group.id)

    def _active_ids(members: Sequence[CollaborationMember]) -> tuple[str, ...]:
        """还活跃的成员 id。"""
        return tuple(m.id for m in members if m.participation_state == "active")

    def _active_leader(
        group: CollaborationSession, active_ids: Sequence[str]
    ) -> str | None:
        """组长，**只在他还活跃时**。

        暂停 / 离开的组长不该占队尾：那是一个永远投不出去的收口人，房间会卡在
        「轮到他」上。正常路径下这不会发生（暂停与离开都会自动移交），这一句守的是
        「自动移交与线程推进之间那一小段」。
        """
        leader = group.leader_member_id
        return leader if leader is not None and leader in set(active_ids) else None

    async def _round_turns(
        group: CollaborationSession, thread: RoomThread
    ) -> tuple[MemberTurn, ...]:
        """**这一轮**已经落进时间线的那几条 ``member_turn``（按 sequence 正序）。

        下一轮的队列是从它们里面读出来的（:func:`next_round_queue`）——「谁被点了
        名」的证据就是**组长说的那句话**，而那句话已经在时间线上了。刻意不在线程上
        攒一份点名清单：那等于把同一件事记两处，而时间线是那本一定对的账。

        **照旧整轮传进去，由纯函数自己筛组长**（批次五十，任务书 §2.2 的二选一里
        选了这一条）：接入层少一处「谁是组长」的判断，而那件事纯函数已经要知道了
        （它得把组长追加到队尾）。两处各判一次，早晚有一天判得不一样。

        略过的那几条**不算**：`（略过）` 里没有名字，读它只是白跑一次解析。
        """
        # Epoch numbers can repeat after legacy recovery or migration. Only turns
        # after this discussion's originating user message may schedule speakers.
        anchor = await repositories.group_messages.get(thread.started_by_message_id or "")
        if anchor is None or anchor.collaboration_session_id != group.id:
            return ()
        rows = await repositories.group_messages.list_for_collaboration(
            group.id, kinds=("member_turn",)
        )
        turns: list[MemberTurn] = []
        for row in sorted(rows, key=lambda item: item.sequence):
            if row.sequence <= anchor.sequence:
                continue
            metadata = row.metadata or {}
            if metadata.get("epoch") != thread.epoch:
                continue
            if metadata.get("round") != thread.round:
                continue
            if metadata.get("passed") or metadata.get("outcome") or not row.author_member_id:
                continue
            turns.append(MemberTurn(row.author_member_id, row.content or ""))
        return tuple(turns)

    async def _next_queue(
        group: CollaborationSession,
        thread: RoomThread,
        room: Sequence[RoomMember],
        active_ids: Sequence[str],
    ) -> tuple[str, ...]:
        """下一轮该谁说（PRD §B4「谁该说」）：**组长本轮点到的人** + 组长。

        成员那几条里的点名不进队列（第五次修订 / 裁决 B）——它们仍旧落在各自那行的
        ``metadata.mentions`` 上，所以时间线上看得出「评审点了打杂但没生效」。
        """
        return next_round_queue(
            await _round_turns(group, thread),
            room,
            _active_leader(group, active_ids),
            active_ids,
        )

    async def _advance_after_turn(
        group: CollaborationSession,
        member: CollaborationMember,
        content: str,
        *,
        room: Sequence[RoomMember],
        message: CollaborationMessage | None = None,
        outcome: str | None = None,
    ) -> None:
        """一位成员这一轮记完了 → 算下一个状态，必要时投给下一位。

        路由层只把「他说了什么」翻成事实（略过 / 组长收口 / 下一轮该谁说），
        **终止判定全在 ``advance_thread`` 里**——散在这一层的话，「为什么房间停了」
        就得靠读一遍路由才答得出，而它正是最该能单测的东西。

        ``message`` 是刚落进时间线的那一行。组长读完全场之后没再点人时（终止 3），
        **它那一段就是结论**，所以这里回头把那一行标成最终答复卡：这件事只有在
        整轮走完、算过下一轮队列之后才知道，写那一行的时候还不知道。
        """
        thread = group.thread
        if thread is None:  # pragma: no cover - 调用方已经判过
            return
        closing = thread.phase == "closing"
        active_ids = _active_ids(await _members_rows(group))
        leader_id = _active_leader(group, active_ids)
        next_queue = await _next_queue(group, thread, room, active_ids)
        advanced = advance_thread(
            thread,
            member_id=member.id,
            # 失败 / 中断的部分输出不能推进讨论或成为最终答复。
            # 时间线上那一行仍旧标 `outcome` 而**不**标 `passed`：失败要看得见，
            # 折进「本轮 N 人略过」里就看不见了（★J-2：失败是唯一染色的一档）。
            passed=is_pass(content) or bool(outcome),
            # 终止 2：**只认组长写的**「最终答复」（PRD §B4 / 审计 §7.3）。别人写了
            # 也不生效，那一行的 `finalIgnored` 在落行时已经记过了。
            final=not outcome and is_final(content) and member.id == leader_id,
            outcome=outcome,
            next_queue=next_queue,
            active_member_ids=active_ids,
            leader_member_id=leader_id,
            round_cap=group.round_cap,
        )
        group = await _save_thread(group, advanced)
        if advanced.ended_reason == "leader_closed" and message is not None:
            await _mark_final(message)
        # 批次五十一：组长那条另记一笔「真正被路由的是谁」。它与同一行上的
        # `mentions`（宽尺子：他提到了谁）一起，正好答出「提到了但没路由」——
        # 测试单 L21 验的就是这两格同时成立。
        if (
            message is not None
            and leader_id is not None
            and member.id == leader_id
            and advanced.status == "running"
            and advanced.round == thread.round + 1
        ):
            await _mark_routed(
                message, tuple(mid for mid in advanced.speaker_queue if mid != leader_id)
            )
        if closing and advanced.ended_reason == "closing_failed":
            await _closing_failed(group, advanced, outcome or "这一轮没有跑完")
        else:
            group = await _finish_thread_side_effects(group, advanced)
        await _notify(group=group, change="thread_state")
        if advanced.is_awaiting:
            _schedule_pump(group.id, advanced.started_by_message_id)

    async def _mark_final(message: CollaborationMessage) -> None:
        """把组长那一条 ``member_turn`` 标成最终答复（PRD §B4 终止 3）。

        为什么是回头改而不是落行时就标：「他没再点任何人」这件事要等**整轮走完、
        下一轮队列算出来是空的**才知道，而那时这一行已经写下去了。回填走的是与
        ``deliveries`` 同一条路（upsert 同一行，``sequence`` 不动）。
        """
        latest = await repositories.group_messages.get(message.id) or message
        metadata = dict(latest.metadata or {})
        if metadata.get("final"):
            return
        metadata["final"] = True
        metadata["phase"] = "closing"
        metadata.pop("finalIgnored", None)
        await repositories.group_messages.append(latest.evolve(metadata=metadata))

    async def _mark_routed(
        message: CollaborationMessage, routed: Sequence[str]
    ) -> None:
        """把「组长这一条真的把谁路由进了下一轮」记在它那一行上（批次五十一）。

        与 ``mentions`` 是**两把尺子的两笔账**：``mentions`` 是宽尺子（含裸名）的
        「他提到了谁」，``routed`` 是窄尺子（只认 ``@``）之后**真正进了下一轮队列**
        的那几位，按队列顺序。两格都在同一行上，时间线因此答得出「组长提到了打杂，
        但打杂没被路由」——这正是第二次外派 §8 里那件事（测试单 L21）。

        **不含组长自己**：它是无条件追加到队尾的，把它算进「被路由的人」会让每一条
        都至少有一个名字，那一格就不再说明任何事情。空的时候**不加这个键**（与
        ``mentions`` 同一条口径：没有内容的键不写）。

        回头改而不是落行时就写，理由同 :func:`_mark_final`：下一轮队列要等整轮走完
        才算得出来。
        """
        routed_ids = [member_id for member_id in routed]
        if not routed_ids:
            return
        latest = await repositories.group_messages.get(message.id) or message
        metadata = dict(latest.metadata or {})
        if metadata.get("routed") == routed_ids:
            return
        metadata["routed"] = routed_ids
        await repositories.group_messages.append(latest.evolve(metadata=metadata))

    async def _route(
        group: CollaborationSession, text: str, target_member_ids: Sequence[str] | None
    ) -> dict[str, Any]:
        """用户说一句 → 时间线上落一行 + 开一条新线程（AD-168）。

        **顺序与批次二十六相反**：那时是先投递再落时间线（那一行要带 deliveries）。
        现在必须先落行——房间增量是从时间线上读出来的，用户这句话不在时间线上，
        第一位成员就无从被告知它。``deliveries`` 改为随投递回填（:func:`_append_delivery`）。

        响应体里的 ``deliveries`` 是**这一刻**已经发生的那几行：串行之下，正常情况
        就是队首那一位。其余的随房间转下去陆续补进同一行（组级流的 ``thread_state``
        与 ``member_turn`` 会叫前端回来重取）。
        """
        if coordinator_enabled:
            message = await coordinator.route(group, text, target_member_ids)
            wire = collaboration_message_to_wire(message)
            return {"message": wire, "deliveries": wire["deliveries"]}
        members = await repositories.members.list_for_collaboration(
            group.id, include_left=True
        )
        by_id = {m.id: m for m in members}
        room = await _room_members(group)
        active = [m for m in members if m.participation_state == "active"]
        if target_member_ids is None:
            kind = "broadcast"
            # 点名解析（PRD §B4）：「A 和 B 你们辩论一下」与 `@A @B` 是同一件事。
            # 命中 ≥1 → 队列 = 命中者；否则 = 全体 active。`@所有人` 是**显式的
            # 全体**，所以它先判：一句「@所有人 谁来说说」里没有别的名字，按点名解析
            # 会落到「没命中 → 全体」同一个结果，但两者是两句不同的话，读代码的人
            # 不该靠这次巧合来相信它。
            mentioned = () if resolve_everyone(text) else resolve_mentions(text, room)
            queue_ids = [
                m.id for m in active if m.id in mentioned
            ] or [m.id for m in active]
        else:
            kind = "directed"
            # 定向发送 = 点名（PRD §B4）。暂停 / 已移出的成员照旧留在队列里：他们
            # 换来的那条 `skipped_*` 是用户点名点到他们时该看到的答复。
            queue_ids = []
            for member_id in target_member_ids:
                member = by_id.get(member_id)
                if member is None:
                    raise _not_found(
                        "member_not_found", f"Group {group.id} 里没有成员 {member_id}"
                    )
                queue_ids.append(member.id)
        spectators = [m.id for m in active if m.id not in queue_ids]
        message = await _append_message(
            group=group,
            kind=kind,
            author_type="user",
            content=text,
            metadata={
                "targetMemberIds": (
                    list(target_member_ids) if target_member_ids is not None else []
                ),
                "deliveries": [],
                "materialsSnapshot": (await repositories.group_materials.get(group.id)).wire(),
                # L8：任何一条成员发言都追得到 epoch 的起点，而起点这一行自己也
                # 说得出「我开的是第几条线程」。
                "epoch": (group.thread.epoch if group.thread is not None else 0) + 1,
            },
        )
        # 用户新消息 → 无论当前状态都开新 epoch（旧线程若还在转，`start_thread`
        # 直接换掉它——「停」的语义由 epoch 承担：旧线程的成员回来时对不上
        # `awaiting_member_id`，于是它的回复照记，但推不动新线程）。
        thread = start_thread(
            previous=group.thread,
            speaker_queue=queue_ids,
            spectators=spectators,
            started_by_message_id=message.id,
            # 组长若还活跃，**无论点没点它**都排在队尾（PRD §A3）——房间永远要有
            # 一个读完全场再说话的人。
            leader_member_id=_active_leader(group, [m.id for m in active]),
        )
        group = await _save_thread(group, thread)
        await _notify(group=group, change="message_posted", message_id=message.id)
        await _notify(group=group, change="thread_state")
        await _pump(group, message_id=message.id)
        latest = await repositories.group_messages.get(message.id) or message
        wire = collaboration_message_to_wire(latest)
        return {"message": wire, "deliveries": wire["deliveries"]}

    # --- 成员回复进时间线（PRD §B2 / 批次四十五 a） --------------------------- #

    async def _run_started_at(conversation_id: str, run_id: str) -> datetime | None:
        """这一轮是什么时候开跑的（用来判「那时他还是成员吗」）。

        从重放缓冲里找那条 ``run.started``，取的是 ``StoredEvent.created_at``
        ——**落库那一刻**，也就是我们自己的钟。刻意**不用** ``envelope.occurred_at``：
        那是引擎给的时间戳，和 ``member.joined_at`` 不是同一个钟（假引擎就把它
        钉死成一个常量），拿两个钟相减去判「他那时在不在组里」，结果取决于对面
        机器的时区与时钟漂移。

        找不到（缓冲被挤掉了、或者这台引擎压根没发 ``run.started``）就是
        ``None``——:func:`membership_covers` 对 ``None`` 判真，宁可多记一行可追溯
        的发言，也不要因为缺一个时间戳而静默丢掉成员的回答。
        """
        try:
            rows = await host.event_store.read_range(conversation_id)
        except Exception:  # noqa: BLE001 - 取不到时间戳不该让这一行记不成
            return None
        # batch53 / AD-174：扫描本身是纯的，搬进了
        # :func:`app.collaboration.member_turns.run_started_at`——**从尾往前扫**，
        # 为的是给账本里已有的撞名历史数据兜底（理由写在那个函数的 docstring 里）。
        return scan_run_started_at(rows, run_id)

    async def _record_member_turn(envelope: Any) -> None:
        """成员会话的一轮跑完了 → 每个「那时他在的」组里各记一条 ``member_turn``。

        这是 PRD §A1 那句「今天成员的回复根本不进这条时间线（那是个 bug，数据
        模型里留了位置没人写），先修」的修法。它**不是** Agent-to-Agent 传话
        （那是 §B4/§B5）：这里一个字都不会被投递给任何人，只是把「这个成员刚才
        答了什么」记进组自己的账本——AD-153 的底线「任何一句话都追得到发起人」
        由此反而更牢：作者是这个成员，``runId`` 指回他自己那条会话的那一轮。

        四条口径，每条都有一个它想防住的具体错法：

        - **只在终态记**（``run.completed`` / ``failed`` / ``interrupted``）：
          每片 delta 都记一行会把组时间线变成一条流。
        - **一条会话在多个组各记一条**（N §9.9 允许一条会话参加多个组）：
          循环跑的是这条会话的全部成员行，不是「第一个组」。
        - **幂等键 ``(group, conversation, run)``**：重放、重复投递、进程重启补记
          都会走到这里，已经有了就什么都不做。
        - **按 ``run.started`` 判成员期**：见 :func:`membership_covers`。
        """
        event_type = getattr(getattr(envelope, "event", None), "type", None)
        if event_type not in RUN_TERMINAL_EVENT_TYPES:
            return
        run_id = getattr(envelope, "run_id", None)
        conversation_id = getattr(envelope, "conversation_id", None)
        if not run_id or not conversation_id:
            # N §13.1：没有 runId 就没有幂等键。宁可不记，也不要记一行永远会被
            # 下一次重放再记一遍的账。
            return
        if coordinator_enabled and coordinator.owns(conversation_id, run_id):
            return
        members = await repositories.members.list_for_conversation(conversation_id)
        if not members:
            return
        started_at = await _run_started_at(conversation_id, run_id)
        body, tool_count = summarize_turn(host.timeline(conversation_id), run_id)
        outcome = turn_outcome(event_type)
        if outcome:
            # 失败 / 中断那一轮的正文**可以是空的**（PRD §B2）：引擎可能一个字
            # 都没说完。空正文加一个 outcome 比编一句「（失败）」诚实。
            body = body or ""
        content, truncated = truncate_body(body)
        for member in members:
            group = await repositories.collaborations.get(
                member.collaboration_session_id
            )
            if group is None or group.status in ("closed", "archived"):
                # 关掉的组是账本，只读（AD-153 末条）——不往里补写新行。
                continue
            if not membership_covers(
                joined_at=member.joined_at,
                left_at=member.left_at,
                run_started_at=started_at,
            ):
                continue
            recorded = await repositories.group_messages.list_for_collaboration(
                group.id, kinds=("member_turn",)
            )
            if (
                find_recorded_turn(
                    recorded, conversation_id=conversation_id, run_id=run_id
                )
                is not None
            ):
                # **记一行与推一格是同一件事。** 幂等键守的是「这一轮我们已经处理
                # 过了」，所以重放同一条终态事件既不多记一行，也不该让房间往前走
                # 一格——否则一次重放就能替一个人「说完」他的下一轮。
                continue
            # 批次四十五 b：这一轮是不是房间循环投出去的那一轮。判据是线程**正在
            # 等他**——不是「这个组有条线程在转」：用户自己在成员会话页里发的一句
            # 话也会走到这里，它不属于任何一轮，不该顶掉队列里那一位。
            thread = group.thread
            in_thread = (
                thread is not None
                and not thread.coordination
                and thread.is_awaiting
                and thread.awaiting_member_id == member.id
            )
            # 触顶那次收口的正文**就是**用户等的答复，所以它无条件标 `final`——
            # 它不必（也被明确要求不要）以「最终答复」开头。
            closing = bool(in_thread and thread is not None and thread.phase == "closing")
            passed = bool(in_thread and not closing and is_pass(content))
            # 终止 2：「最终答复」**只认组长写的**（PRD §B4 / 审计 §7.3 的白名单）。
            # 别人写了照常落行、房间照转，只多一笔 `finalIgnored`——不留这一笔，
            # 「我明明写了最终答复，房间为什么没停」就只能靠读代码回答。
            is_leader = group.leader_member_id == member.id
            wrote_final = bool(in_thread and not outcome and not closing and is_final(content))
            final = bool(in_thread and not outcome and (closing or (wrote_final and is_leader)))
            since, delivered_to = (
                delivery_marks.get((group.id, member.id), (None, None))
                if in_thread
                else (None, None)
            )
            # 批次五十（裁决 B）：这一段话点了谁，**不管他是不是组长**都记一笔。
            # 队列只认组长那条（`next_round_queue`），所以时间线上这一笔正是「评审
            # 点了打杂、下一轮却没有打杂」那件事的唯一证据（测试单 L20）。
            # 略过的那条不解析：`（略过）` 里没有名字。
            mentions = (
                resolve_mentions(content, await _room_members(group))
                if in_thread and not passed and not outcome
                else ()
            )
            message = await _append_message(
                group=group,
                kind="member_turn",
                author_type="member",
                author_member_id=member.id,
                conversation_id=conversation_id,
                content=content,
                metadata=member_turn_metadata(
                    run_id=run_id,
                    tool_count=tool_count,
                    truncated=truncated,
                    outcome=outcome,
                    epoch=thread.epoch if in_thread and thread else None,
                    round_number=thread.round if in_thread and thread else None,
                    speaker_index=(
                        thread.speaker_index if in_thread and thread else None
                    ),
                    delivered_since=since,
                    delivered_to=delivered_to,
                    passed=passed,
                    final=final,
                    final_ignored=wrote_final and not is_leader,
                    phase="closing" if closing else None,
                    mentions=mentions,
                ),
            )
            await _notify(
                group=group,
                change="member_turn",
                member=member,
                message_id=message.id,
            )
            if in_thread:
                await _advance_after_turn(
                    group,
                    member,
                    content,
                    room=await _room_members(group),
                    message=message,
                    outcome=outcome,
                )

    async def _stop_by_word(
        group: CollaborationSession, text: str
    ) -> dict[str, Any]:
        """用户**打字**说了「停」（PRD §B10-1）：等同按了那枚「停」。

        真机截图里这一句被当成一条普通消息投给了成员，成员老老实实回了「已停止。」
        ——而房间其实还在转。所以这条路径把四件事一次做掉：**不落** ``broadcast`` /
        ``directed`` 行、**不投递**、**不开新 epoch**，只写一条 system 说清楚发生了
        什么，并把线程收成 ``stopped``。

        返回体形状与 :func:`_route` 一样（``{message, deliveries}``），前端那条
        「把后端给的那一行接到时间线末尾」的路径因此一个字都不用改：它接到的是那条
        system 行，正好就是用户该看到的东西。
        """
        if coordinator_enabled:
            await coordinator.stop(group.id)
            group = await _load_group(group.id)
        stopped = stop_thread(group.thread)
        if stopped is not group.thread:
            group = await _save_thread(group, stopped)
        message = await _append_message(
            group=group,
            kind="system",
            author_type="system",
            content=STOP_COMMAND_LINE.format(word=text.strip()),
            metadata={
                "change": "thread_stopped_by_word",
                "stopWord": text.strip(),
                "epoch": group.thread.epoch if group.thread is not None else 0,
            },
        )
        await _notify(group=group, change="message_posted", message_id=message.id)
        await _notify(group=group, change="thread_state")
        wire = collaboration_message_to_wire(message)
        return {"message": wire, "deliveries": wire["deliveries"], "stopped": True}

    @_endpoint
    async def broadcast(group_id: str, body: BroadcastBody = Body(...)) -> Any:
        group = await _load_open_group(group_id)
        if is_stop_command(body.text):
            return await _stop_by_word(group, body.text)
        return await _route(group, body.text, body.target_member_ids)

    @_endpoint
    async def send_to_member(
        group_id: str, member_id: str, body: SendToMemberBody = Body(...)
    ) -> Any:
        """单目标糖衣：与 ``/broadcast {targetMemberIds: [memberId]}` 同一条路径。"""
        group = await _load_open_group(group_id)
        await _load_member(group, member_id)
        # 停止词在**两条路上都要挡**（PRD §B10-1）：真机那一次是定向发的，
        # 只挡广播等于只修了一半——而这一半正好不是出事的那一半。
        if is_stop_command(body.text):
            return await _stop_by_word(group, body.text)
        return await _route(group, body.text, [member_id])

    @_endpoint
    async def stop_thread_endpoint(group_id: str) -> Any:
        """``POST /api/groups/{id}/thread/stop``：唯一的那枚按钮（PRD §A2）。

        **立刻生效**：正在跑的那位这一轮的回复照记（旁观者认的是引擎的终态，不是
        线程状态），但不再投下一位。不去打断那条会话——那会丢掉一段已经付过钱的
        正文，而用户按「停」要的是「别再往下转了」，不是「把刚才那句作废」。

        没在转的时候按它是一次无操作，仍回 200：结果与「停成功了」一模一样。
        """
        group = await _load_open_group(group_id)
        if coordinator_enabled:
            await coordinator.stop(group.id)
            group = await _load_group(group.id)
        stopped = stop_thread(group.thread)
        if stopped is not group.thread:
            group = await _save_thread(group, stopped)
            await _notify(group=group, change="thread_state")
        return {
            "group": _group_wire(group),
            "thread": (
                group.thread.model_dump(mode="json", by_alias=True)
                if group.thread is not None
                else None
            ),
        }

    @_endpoint
    async def list_messages(
        group_id: str,
        limit: int = Query(50, ge=1, le=200),
        before: int | None = Query(None, ge=0),
    ) -> Any:
        """``GET /api/groups/{id}/messages?limit=50&before=``：按时间**倒序**。

        游标是 ``sequence``（组内单调递增）而不是时间戳：同一毫秒里可以有两行，
        时间戳当游标会漏或会重。``nextBefore`` 是本页最早那一行的 sequence，
        再往前翻直接把它填回 ``before``；已经到头就是 ``null``。

        批次五十二第 3 件（真机 UI-04）：**本页最早那一行的 sequence 是 0 时也是
        到头**。此前这里照样回 0，前端于是还渲染着「加载更早」，用户得再点一次、
        拿到一页空的才看见它消失——而那一行明明就是组建起来的第一行。组内 sequence
        从 0 起，所以「最早行 == 0」是**精确**判据，不是「这一页少于 limit 条」
        那种会看走眼的启发式（同时到达的两行能让最后一页正好满）。

        组关了也照读——时间线是这个组做过什么的账本，关组不该把账本锁上。
        """
        group = await _load_group(group_id)
        rows = await repositories.group_messages.list_for_collaboration(
            group.id, before_sequence=before, limit=limit
        )
        entries = [collaboration_message_to_wire(row) for row in reversed(rows)]
        # 倒序，所以「本页最早那一行」是最后一个。
        earliest = entries[-1]["sequence"] if entries else None
        return {
            "groupId": group.id,
            "messages": entries,
            "count": len(entries),
            "nextBefore": None if earliest in (None, 0) else earliest,
        }

    # --- Context Packet v0（第 3 件 / AD-154） ------------------------------- #

    async def _last_messages(conversation: Conversation) -> dict[str, Any]:
        """从事件里摘出这条会话的**最近一次完成时间**。

        AD-154：Context Packet 是**只读汇编**，所以这里只**读**——除了一处例外：
        缓冲整个是空的时候调一次 :meth:`SessionHost.restore_from_native_history`
        （AD-143 的同一条路），它自己就只在「没有任何缓冲事件 + 没有活跃 runtime
        + 绑了原生 Session」时才动手，其余情形是彻底的 no-op。读不到就**什么都
        不放**（键不出现），不编一句「（暂无内容）」——N §13.1。

        批次五十二第 4 件（真机 UI-01）：两句摘要**不再从这里取**。此前
        ``lastUserMessage`` 摘的是事件流里那条 ``kaus/user.message``——而在房间
        里，投给成员的「用户消息」正是**房间增量**，里面装着别人的发言，于是
        「最近一句」显示成了别人说的话或上一条输入。它们改由
        :func:`packet_recent_lines` 从**组时间线**取（那里「谁说的」是一等公民）。
        这里只留 ``lastCompletedAt``：一次 run 什么时候跑完，只有会话自己的事件
        流知道，组时间线上没有这个事实。
        """
        store = host.event_store
        if await store.latest_sequence(conversation.id) is None:
            try:
                await host.restore_from_native_history(conversation)
            except Exception:  # noqa: BLE001 - 汇编失败不该让整个 packet 报错
                pass
        try:
            envelopes = await store.replay(conversation.id)
        except Exception:  # noqa: BLE001 - 同上
            return {}
        last_completed: str | None = None
        for envelope in envelopes:
            if getattr(envelope.event, "type", None) == "run.completed":
                last_completed = (
                    envelope.occurred_at.isoformat().replace("+00:00", "Z")
                )
        if last_completed is None:
            return {}
        return {"lastCompletedAt": last_completed}

    @_endpoint
    async def context_packet(group_id: str) -> Any:
        """``GET /api/groups/{id}/context-packet``：只读汇编，**不发给任何人**。

        AD-154 的边界写在返回体本身上：这里没有任何「注入」的动作，也没有一个
        叫 ``inject`` 的参数。要把它送进某条会话，得由用户显式点「作为消息发给
        某成员」——那就是一条定向消息，走 ``/members/{id}/send``。之所以定死这
        一条：自动注入会让每个成员的上下文里悄悄多出一段它没同意过的内容，而
        「这句话是谁说的」在多 Agent 场景里正是最要紧的事。
        """
        group = await _load_group(group_id)
        rows = await repositories.members.list_for_collaboration(group.id)
        # batch52 第 4 件：名字与「最近一句」都改从组这边取——名字用那张共用表
        # （与成员栏 / 时间线 / 房间说明一致，含 `#2`），两句摘要用组时间线。
        roster = await _room_members(group)
        names = resolve_display_names(roster)
        recent = packet_recent_lines(
            await repositories.group_messages.list_for_collaboration(group.id),
            roster,
        )
        members: list[dict[str, Any]] = []
        for member in rows:
            conversation = await repositories.conversations.get(member.conversation_id)
            if conversation is None:
                members.append(
                    {
                        "memberId": member.id,
                        "conversationId": member.conversation_id,
                        "displayName": names.get(member.id),
                        "title": None,
                        "bindingId": None,
                        "backendId": None,
                        "modelId": None,
                        "runState": None,
                        "participationState": member.participation_state,
                        **recent.get(member.id, {}),
                    }
                )
                continue
            binding = await repositories.bindings.get(conversation.agent_binding_id)
            entry: dict[str, Any] = {
                "memberId": member.id,
                "conversationId": conversation.id,
                "displayName": names.get(member.id),
                "title": conversation.title,
                "bindingId": conversation.agent_binding_id,
                "backendId": binding.backend_id if binding is not None else None,
                "modelId": conversation.model_id,
                "runState": await host.run_state_of(conversation),
                "participationState": member.participation_state,
            }
            entry.update(recent.get(member.id, {}))
            entry.update(await _last_messages(conversation))
            members.append(entry)
        generated_at = _now().isoformat().replace("+00:00", "Z")
        return {
            "groupId": group.id,
            "generatedAt": generated_at,
            "members": members,
            "markdown": context_packet_markdown(
                title=group.title, generated_at=generated_at, members=members
            ),
        }

    # --- 组级 SSE ----------------------------------------------------------- #

    @_endpoint
    async def stream_group_events(since: int | None = Query(None, ge=0)) -> Any:
        """``GET /api/groups/events?since=<sequence>``：组级变更的全局流。

        ``since``（批次二十六第 4 件）先补最近的漏帧再接实时，缓冲上限
        :data:`GROUP_REPLAY_BUFFER` 条。补不齐时先发一帧
        ``{"type": "kaus/group.replayTruncated", "sequence": <当前}``，
        告诉前端「这一段我给不全，请重取 GET /api/groups」——静默少给几条会让
        浮窗的计数长期对不上，而它自己永远不会发现。

        它仍然是**进程内**的：多进程部署下每个进程只广播自己这边的变更，
        进程一重启缓冲就空了（那时 ``since`` 落在未来，同样报断档）。
        """
        queue = events.subscribe()
        backlog: tuple[dict[str, Any], ...] = ()
        truncated = False
        if since is not None:
            backlog, truncated = events.replay(since)

        async def frames():
            try:
                if truncated:
                    yield sse_data_line(
                        {
                            "type": f"{GROUP_EVENT_NAMESPACE}/group.replayTruncated",
                            "sequence": events.sequence,
                            "occurredAt": _now().isoformat().replace("+00:00", "Z"),
                            "data": {"since": since},
                        },
                        serializer=_dumps,
                    )
                for frame in backlog:
                    yield sse_data_line(frame, serializer=_dumps)
                # 批次二十八：补帧放完、开始等之前推一段填充注释，隧道才会把
                # 前面这一批真的转出去。
                yield SSE_PADDING_FRAME
                while True:
                    try:
                        frame = await asyncio.wait_for(
                            queue.get(), timeout=keepalive_interval
                        )
                    except asyncio.TimeoutError:
                        # AD-62 同一条：静默久了自己发注释帧，别让代理把连接判死。
                        yield KEEPALIVE_FRAME
                        continue
                    yield sse_data_line(frame, serializer=_dumps)
            finally:
                events.unsubscribe(queue)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers=dict(SSE_HEADERS),
        )

    @_endpoint
    async def group_events_snapshot(since: int | None = Query(None, ge=0)) -> Any:
        """``GET /api/groups/events/snapshot?since=<sequence>``（批次三十第 1 件）。

        组级流的非流式版本：同一份环形缓冲、同一批帧，只是用普通 ``GET`` 拿。
        隧道下 SSE 一个字节都不到时，浮窗靠它照旧知道「哪个组变了」。

        ``replayTruncated`` 与 SSE 那条同义：这一段补不齐，请重取
        ``GET /api/groups``。上限 :data:`SNAPSHOT_MAX_EVENTS`（缓冲本身只有
        :data:`GROUP_REPLAY_BUFFER` 条，这条上限实际是防御性的）。
        """
        frames: tuple[dict[str, Any], ...]
        truncated = False
        if since is None:
            # 不带游标 = 「我刚来，只要现在的位置」：不重放，避免把 200 条历史
            # 变更当成新变更再触发 200 次重取。
            frames = ()
        else:
            frames, truncated = events.replay(since)
        page = list(frames[:SNAPSHOT_MAX_EVENTS])
        return {
            "events": page,
            "lastSequence": page[-1]["sequence"] if page else events.sequence,
            "replayTruncated": truncated,
            "truncated": len(frames) > len(page),
        }

    coordinator = GroupCoordinator(host=host, repositories=repositories, registry=registry,
        append_message=_append_message, notify=_notify, room_members=_room_members,
        execution_root=coordinator_root)

    def _group_wire(group, *, member_count=None):
        result = group_to_wire(group, member_count=member_count)
        if coordinator_enabled:
            result["coordinatorEnabled"] = True
            result["leaderMemberId"] = None
        return result

    @_endpoint
    async def coordinator_options():
        from app.api.views import binding_to_wire
        rows = []
        for project in await repositories.projects.list_all():
            for binding in await repositories.bindings.list_for_project(project.id):
                if not binding.enabled or binding.runtime_config.get('group_execution'):
                    continue
                try:
                    driver = registry.get(binding.backend_id)
                    hook = getattr(driver, 'group_context_isolation', None)
                    supported = bool(hook and hook())
                except Exception:
                    supported = False
                if supported:
                    rows.append({**binding_to_wire(binding), 'projectName': project.display_name})
        return {'bindings': rows}

    @_endpoint
    async def coordinator_defaults():
        return {"config": await coordinator.defaults()}

    @_endpoint
    async def set_coordinator_defaults(body: CoordinatorBody = Body(...)):
        return {"config": await coordinator.update_defaults(body.config, body.expected_revision)}

    @_endpoint
    async def get_coordinator(group_id: str):
        group = await _load_open_group(group_id)
        candidate = None
        if not group.settings.get(CONFIG_KEY) and group.leader_member_id:
            member = await repositories.members.get(group.leader_member_id)
            conv = await repositories.conversations.get(member.conversation_id) if member else None
            if conv:
                candidate = {"bindingId": conv.agent_binding_id, "modelId": conv.model_id,
                    "providerId": conv.provider_id, "reasoningMode": conv.reasoning_mode,
                    "approvalMode": conv.approval_mode, "executionMode": conv.execution_mode}
        return {"config": group.settings.get(CONFIG_KEY), "candidate": candidate,
                "defaults": await coordinator.defaults()}

    @_endpoint
    async def set_coordinator(group_id: str, body: CoordinatorBody = Body(...)):
        await _load_open_group(group_id)
        group = await coordinator.configure(group_id, body.config, body.expected_revision)
        return {"group": _group_wire(group)}

    @_endpoint
    async def handoff_coordinator(group_id: str):
        group = await _load_open_group(group_id)
        state = group.thread.coordination if group.thread else None
        if not state:
            raise GroupApiError(409, 'task_missing', '没有可以接管的任务')
        rows = await repositories.group_messages.list_for_collaboration(group.id)
        source = next((r for r in rows if r.id == state['taskId']), None)
        if source is None:
            raise GroupApiError(409, 'task_missing', '原始任务已不存在')
        targets = tuple(state.get('addressedMembers', state['allowedMembers'])) if state['explicitScope'] else None
        row = await coordinator.route(group, '继续上一任务：\n' + source.content, targets)
        return {"message": collaboration_message_to_wire(row)}

    router.group_coordinator = coordinator

    # --- 注册 --------------------------------------------------------------- #

    for path, endpoint, methods, name, dependencies in (
        ("/group-settings/coordinator/options", coordinator_options, ["GET"], "coordinator_options", [_auth]),
        ("/group-settings/coordinator", coordinator_defaults, ["GET"], "coordinator_defaults", [_auth]),
        ("/group-settings/coordinator", set_coordinator_defaults, ["PUT"], "coordinator_defaults_set", [_auth]),
        ("/groups/{group_id}/coordinator/handoff", handoff_coordinator, ["POST"], "coordinator_handoff", [_auth]),
        ("/groups/{group_id}/coordinator", get_coordinator, ["GET"], "coordinator_get", [_auth]),
        ("/groups/{group_id}/coordinator", set_coordinator, ["PUT"], "coordinator_set", [_auth]),
        # `/groups/events` 必须排在 `/groups/{group_id}` 之前：Starlette 按注册
        # 顺序匹配，字面量段先注册先赢（同 `capabilities/_meta` 的做法）。
        # 批次三十：`/groups/events/snapshot` 更具体，排在 `/groups/events` 之前。
        (
            "/groups/events/snapshot",
            group_events_snapshot,
            ["GET"],
            "groups_events_snapshot",
            [_auth],
        ),
        ("/groups/events", stream_group_events, ["GET"], "groups_events", [_auth_sse]),
        ("/groups", create_group, ["POST"], "groups_create", [_auth]),
        ("/groups", list_groups, ["GET"], "groups_list", [_auth]),
        ("/groups/{group_id}", get_group, ["GET"], "groups_get", [_auth]),
        ("/groups/{group_id}", patch_group, ["PATCH"], "groups_patch", [_auth]),
        ("/groups/{group_id}/close", close_group, ["POST"], "groups_close", [_auth]),
        ("/groups/{group_id}/spawn", spawn_member, ["POST"], "groups_spawn", [_auth]),
        ("/groups/{group_id}/materials", get_materials, ["GET"], "groups_materials", [_auth]),
        ("/groups/{group_id}/materials", add_material, ["POST"], "groups_add_material", [_auth]),
        ("/groups/{group_id}/materials/{material_id}", edit_material, ["PATCH"], "groups_edit_material", [_auth]),
        ("/groups/{group_id}/materials/{material_id}", delete_material, ["DELETE"], "groups_delete_material", [_auth]),
        (
            "/groups/{group_id}/broadcast",
            broadcast,
            ["POST"],
            "groups_broadcast",
            [_auth],
        ),
        (
            # 批次四十五 b（PRD §B4）：唯一的新端点。「转发」「讨论」都没有——
            # 房间自己会转，用户只说话与按停。
            "/groups/{group_id}/thread/stop",
            stop_thread_endpoint,
            ["POST"],
            "groups_thread_stop",
            [_auth],
        ),
        (
            "/groups/{group_id}/messages",
            list_messages,
            ["GET"],
            "groups_messages",
            [_auth],
        ),
        (
            "/groups/{group_id}/context-packet",
            context_packet,
            ["GET"],
            "groups_context_packet",
            [_auth],
        ),
        (
            "/groups/{group_id}/members",
            add_member,
            ["POST"],
            "groups_add_member",
            [_auth],
        ),
        (
            "/groups/{group_id}/members/{member_id}",
            remove_member,
            ["DELETE"],
            "groups_remove_member",
            [_auth],
        ),
        (
            # 批次五十二第 5 件（AD-173）：改这位成员在**本组**叫什么（`roleLabel`）。
            "/groups/{group_id}/members/{member_id}",
            patch_member,
            ["PATCH"],
            "groups_patch_member",
            [_auth],
        ),
        (
            "/groups/{group_id}/members/{member_id}/pause",
            pause_member,
            ["POST"],
            "groups_pause_member",
            [_auth],
        ),
        (
            "/groups/{group_id}/members/{member_id}/resume",
            resume_member,
            ["POST"],
            "groups_resume_member",
            [_auth],
        ),
        (
            "/groups/{group_id}/members/{member_id}/promote",
            promote_member,
            ["POST"],
            "groups_promote_member",
            [_auth],
        ),
        (
            "/groups/{group_id}/members/{member_id}/send",
            send_to_member,
            ["POST"],
            "groups_send_to_member",
            [_auth],
        ),
    ):
        router.add_api_route(
            path, endpoint, methods=methods, name=name, dependencies=dependencies
        )
    # 批次四十五 a：把「成员答完一轮」这件事接进组时间线。挂在装配这一层而不是
    # Session Host 里，理由与 `capability_projector` 那条一样：Host 只负责让事实
    # 流过去，「哪些会话是谁的成员、记到哪个组的账上」是组这一层的知识。
    host.add_event_observer(_record_member_turn)
    router.group_event_hub = events  # type: ignore[attr-defined]
    router.record_member_turn = _record_member_turn  # type: ignore[attr-defined]
    return router


async def reconcile_room_threads(repositories: RepositorySet, *, coordinator_enabled: bool = True) -> tuple[str, ...]:
    """进程起来时把上一条还标着 ``running`` 的线程收成 ``stopped``（PRD §B4），
    并给还没有组长的老组补一位（PRD §B6）。

    为什么必须做：房间循环是**进程内**的——推进下一位靠的是挂在 Session Host 上的
    旁观者，进程一没了，那个「等谁说完」的承诺就没人兑现了。库里那条 ``running``
    于是变成一句假话：组头显示「第 2 轮 · 轮到 B」，而永远不会有人去投给 B。

    所以这里既不假装它还在转，也不替用户把它续上（续上等于在他不在场的时候替他
    花钱跑一轮引擎）。收成 ``stopped`` 并在时间线上留一行系统消息说清楚为什么，
    用户再说一句话就是一条新线程。

    组长那一件是**兼容**：批次四十八之前建的组库里那一格是 NULL，而房间从此靠组长
    收口——没有组长的组下一次转起来就会走终止 6（「没有可收口的成员」）。规则与加入
    时那条一样：**最早加入的 active 成员**，并在时间线上写一行说清楚是谁。

    返回被收掉的组 id，供启动日志与自检使用。**只数线程那一件**：给老组补组长不是
    「上一次没收尾」，它是一次迁移，不该混进那个数里。
    """
    touched: list[str] = []
    for group in await repositories.collaborations.list_active():
        if not coordinator_enabled and group.leader_member_id is None:
            members = await repositories.members.list_for_collaboration(group.id)
            first = next(
                (m for m in members if m.participation_state == "active"), None
            )
            if first is not None:
                roster: list[RoomMember] = []
                for row in members:
                    conversation = await repositories.conversations.get(
                        row.conversation_id
                    )
                    roster.append(
                        RoomMember(
                            member_id=row.id,
                            raw_name=(
                                row.role_label
                                or (
                                    conversation.title
                                    if conversation is not None
                                    else None
                                )
                                or ""
                            ),
                            engine=None,
                            listed=row.participation_state == "active",
                        )
                    )
                names = resolve_display_names(roster)
                sequence = await repositories.group_messages.next_sequence(group.id)
                await repositories.group_messages.append(
                    CollaborationMessage.create(
                        collaboration_session_id=group.id,
                        sequence=sequence,
                        kind="system",
                        author_type="system",
                        content=LEADER_SET_LINE.format(
                            leader=names.get(first.id, first.id)
                        ),
                        metadata={
                            "change": "leader_changed",
                            "leaderMemberId": first.id,
                        },
                    )
                )
                group = await repositories.collaborations.save(
                    group.evolve(leader_member_id=first.id, updated_at=_now())
                )
        thread = group.thread
        # 收口轮同样要收尾（批次四十六）：一条挂在 `closing` 上的线程与挂在
        # `running` 上的是同一句假话——组头说「正在收口」，而没有人在收。
        if thread is None or not thread.is_awaiting:
            continue
        sequence = await repositories.group_messages.next_sequence(group.id)
        await repositories.group_messages.append(
            CollaborationMessage.create(
                collaboration_session_id=group.id,
                sequence=sequence,
                kind="system",
                author_type="system",
                content="后端重启了，这一轮没有继续下去；再说一句就重新开始。",
                metadata={"change": "thread_stopped_on_restart", "epoch": thread.epoch},
            )
        )
        await repositories.collaborations.save(
            group.evolve(
                thread=thread.evolve(
                    status="stopped",
                    awaiting_member_id=None,
                    ended_reason="closing_failed" if thread.phase == "closing" else None,
                    coordination={**thread.coordination, "activeConversationId": None, "activeSpeaker": None} if thread.coordination else None,
                ),
                updated_at=_now(),
            )
        )
        touched.append(group.id)
    return tuple(touched)


async def group_ids_for_conversations(
    repositories: RepositorySet, conversation_ids: Sequence[str]
) -> dict[str, str]:
    """``conversationId → groupId``（任务书第 2 件：会话索引行要标组）。

    **按组查，不按会话查**：一条会话一次点查会让 50 条侧栏列表变成 50 次查库
    （与 ``backendId`` / lease 那两处同一个理由），而还开着的组本来就是个位数——
    把它们的成员行一次取完再反查，代价与组数成正比，与会话数无关。

    只认**还开着**的组里**没走**的成员：关掉的组是历史，侧栏不该再给那条会话
    挂一个组标签。
    """
    return {
        conversation_id: label["id"]
        for conversation_id, label in (
            await group_labels_for_conversations(repositories, conversation_ids)
        ).items()
    }


async def group_labels_for_conversations(
    repositories: RepositorySet, conversation_ids: Sequence[str]
) -> dict[str, dict[str, str]]:
    """``conversationId → {"id", "title"}``（批次二十六第 4 件：索引行要带组名）。

    与 :func:`group_ids_for_conversations` 是同一次查库，只是多带了标题。侧栏
    此前只拿得到 ``groupId``，于是要么显示一个 id、要么再打一次 ``GET /groups``
    才能把它翻成人话——两条都不该是侧栏该做的事。

    取数口径一字未改：**按组查，不按会话查**（一条会话一次点查会让 50 条侧栏
    列表变成 50 次查库），且只认**还开着**的组里**没走**的成员（关掉的组是历史，
    侧栏不该再给那条会话挂一个组标签）。
    """
    if not conversation_ids:
        return {}
    wanted = set(conversation_ids)
    mapping: dict[str, dict[str, str]] = {}
    for group in await repositories.collaborations.list_active():
        for member in await repositories.members.list_for_collaboration(group.id):
            if member.conversation_id in wanted:
                mapping.setdefault(
                    member.conversation_id, {"id": group.id, "title": group.title}
                )
    return mapping


def _dumps(payload: Any) -> str:
    # 组标题里有中文；SSE 是 UTF-8 传输，不必转义成 \\uXXXX（同会话流）。
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "CLOSE_ACTIONS",
    "DEFAULT_GROUP_KEEPALIVE_INTERVAL",
    "GROUP_CLOSED_CODE",
    "GROUP_REPLAY_BUFFER",
    "ON_CLOSE_CHOICES",
    "AddMemberBody",
    "BroadcastBody",
    "CloseGroupBody",
    "CreateGroupBody",
    "GroupApiError",
    "GroupEventHub",
    "PatchGroupBody",
    "SendToMemberBody",
    "SpawnMemberBody",
    "CLOSING_FAILED_LINE",
    "EXHAUSTED_LINE",
    "LEADER_CLOSED_LINE",
    "LEADER_FINAL_LINE",
    "LEADER_HANDOVER_LINE",
    "LEADER_MISSING_LINE",
    "LEADER_SET_LINE",
    "MAX_PUMP_STEPS",
    "STOP_COMMAND_LINE",
    "build_group_router",
    "group_ids_for_conversations",
    "reconcile_room_threads",
    "group_labels_for_conversations",
]
