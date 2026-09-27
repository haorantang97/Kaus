"""Session API 的序列化：纯函数，不依赖任何 web 框架。

为什么单独一个模块
------------------
与 :mod:`app.api.views` 同样的理由：路由层只剩「取参数、调服务、返回 dict」，
序列化本身可以脱离 HTTP 单测；而且本模块不 import 框架，公共层纯净性扫描
（``kernel/tests/test_public_type_purity.py`` 会 import ``app`` 下每个子模块）
不需要装 fastapi。

三个口径
--------
1. **Timeline 摘要不是 Timeline 本身。** ``GET /api/conversations/{id}`` 返回的是
   「这条会话现在长什么样」的**概要**（run 状态、各类卡片计数、最后一个
   sequence、usage、error），**不是**完整卡片内容。完整内容走事件流
   （``/events`` 的 ``?after=`` 重放），这正是 D-16「Event Store 是短期重放缓冲」
   的接口体现——不在 REST 上再造一份对话账本。
2. **不支持 ≠ 空值。** 拿不到的东西一律显式为 ``null`` 并配一个说明字段
   （N §13.1），不用空对象伪装成「有但是空的」。
3. **公共层词汇。** 本模块只认识 :mod:`app` / :mod:`runtime` 的类型，不认识任何
   一家 Backend 的私有概念（N §3 核心约束）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.api.api_errors import error_body
from app.conversations.models import Conversation
from app.runtimes.models import ConcurrencyAdvisory
from drivers.base import APPROVAL_MODES
from runtime.event_envelope import AgentEventEnvelope
from runtime.event_reducer import TimelineState

#: 错误响应的固定形状 ``{"error": {"code", "message"}}`` 定义在
#: :mod:`app.api.api_errors`：批次八第 5 件之后，只读领域端点也用同一个形状，
#: 定义只能有一份。这里保留同名再导出，老的 import 路径不必跟着改。


#: 旧字段 ``conversation.state`` 里**与 Card runtime 有关**的那两个取值。
#: ``running-external`` / ``paused`` / ``ended`` / ``error`` 说的是别的事
#: （终端持有写权、人为暂停、会话结束、上一轮崩了），``runState`` 管不着它们，
#: 也就不该去改它们。
_CARD_STATES: frozenset[str] = frozenset({"idle", "running-card"})


def derive_conversation_state(state: str, run_state: str | None) -> str:
    """让旧字段 ``state`` 与 ``runState`` 在**同一个响应里**不可能互相矛盾（AD-159）。

    真机现象（VERIFY-BATCH-31 / VERIFY-BATCH-34）：``GET /api/conversations/{id}``
    同时给出 ``conversation.state = "running-card"`` 与 ``runState = "idle"``。
    两者都不是错的——``state`` 是落盘快照（``start_runtime`` 时写的），``runState``
    是此刻算出来的；但一个响应里两个字段各说各话，读它的人只能靠猜，而真机上
    确实有人先信了那个旧字段。

    单一真源的做法：``runState`` 是唯一权威，``state`` 在**读的那一刻**按它导出。
    字段本身保留（老客户端还在读），语义收敛为「``runState`` 的粗粒度投影」。

    ``run_state is None``（问不出来）时原样返回：不知道就不改，N §13.1。
    """
    if run_state is None or state not in _CARD_STATES:
        return state
    return "running-card" if run_state in ("running", "stopping-unconfirmed") else "idle"


def conversation_to_wire(
    conversation: Conversation, *, run_state: str | None = None
) -> dict[str, Any]:
    """Conversation → wire dict（驼峰键，与 v1.0 §4.4 一致）。

    给了 ``run_state`` 就把旧字段 ``state`` 按它收敛（见
    :func:`derive_conversation_state`）——同一个响应里这两个字段不允许打架。
    """
    payload = conversation.model_dump(mode="json", by_alias=True)
    payload["state"] = derive_conversation_state(conversation.state, run_state)
    return payload


def envelope_to_wire(envelope: AgentEventEnvelope) -> dict[str, Any]:
    """AgentEventEnvelope → wire dict。SSE 的一行 ``data:`` 就是它的 JSON。"""
    return envelope.model_dump(mode="json", by_alias=True)


def advisory_to_wire(advisory: ConcurrencyAdvisory | None) -> dict[str, Any] | None:
    """带外写入软提示（R-04）。没有提示时是 ``null``，不是空对象。"""
    if advisory is None:
        return None
    return advisory.model_dump(mode="json", by_alias=True)


def session_projection_to_wire(projection: Any) -> dict[str, list[str]] | None:
    """建会话时随协议参数送进引擎的项目能力（批次四十三）。

    **只有名字**。``summary`` 那一份由 Driver 自己填（只有它认识自家协议的参数
    名），公共层不解释它的键，只保证一件事：值永远是一串字符串。命令行、URL、
    环境变量的值一个字都不会到这里来——Driver 那边已经把它们挡在
    ``options.metadata`` 里了，而 ``metadata`` 不上 wire。

    ``None`` 的含义是「这条路没走」，与「走了但一条都没送」（``{}``）不是一回事。
    """
    if projection is None:
        return None
    summary = getattr(projection, "summary", None) or {}
    return {str(key): [str(name) for name in names] for key, names in summary.items()}


def timeline_summary(state: TimelineState | None) -> dict[str, Any] | None:
    """Timeline 摘要（见模块口径 1）。

    ``state is None`` 表示这条会话在本进程里既没有活跃 runtime、缓冲里也没有
    任何事件——返回 ``null`` 而不是「计数全 0 的摘要」，因为两者含义不同：
    前者是「不知道」，后者是「知道，且确实是空的」。
    """
    if state is None:
        return None
    counts: dict[str, int] = {}
    pending_interactions: list[dict[str, Any]] = []
    for item in state.items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        if item.kind == "interaction" and getattr(item, "status", None) == "pending":
            pending_interactions.append(
                {
                    "interactionId": item.item_id.split(":", 1)[-1],
                    "interactionKind": getattr(item, "interaction_kind", None),
                    "runId": item.run_id,
                }
            )
    return {
        "conversationId": state.conversation_id,
        "runState": state.run_state,
        "activeRunId": state.active_run_id,
        "lastSequence": state.last_sequence,
        "itemCount": len(state.items),
        "itemCounts": dict(sorted(counts.items())),
        # 待答的审批/提问：卡片要据此把 Composer 换成「答一下」的形态。
        "pendingInteractions": pending_interactions,
        "childRunIds": [child.run_id for child in state.child_runs],
        "usage": state.usage.model_dump(mode="json", by_alias=True)
        if state.usage is not None
        else None,
        "error": state.error.model_dump(mode="json", by_alias=True)
        if state.error is not None
        else None,
        "droppedDuplicates": state.dropped_duplicates,
        "droppedStale": state.dropped_stale,
    }


def runtime_view(
    *,
    active: bool,
    handle: Any | None,
    owner: Any | None,
) -> dict[str, Any]:
    """Runtime 现状 + Runtime Owner（v1.0 §8.8.4：UI 必须始终显示 owner）。

    ``owner is None`` 是**合法状态**（AD-11：「无人持有」不是异常），因此这里
    照实返回 ``null``，由 UI 显示「无人持有」。
    """
    return {
        "active": active,
        "runtimeId": getattr(handle, "runtime_id", None),
        "backendId": getattr(handle, "backend_id", None),
        "nativeSessionId": getattr(handle, "native_session_id", None),
        "owner": owner.model_dump(mode="json", by_alias=True)
        if owner is not None
        else None,
    }


def native_history_to_wire(history: Any) -> dict[str, Any]:
    """原生历史（R-02：原生 session 存储才是权威账本）。"""
    return history.model_dump(mode="json", by_alias=True)


def model_catalog_to_wire(catalog: Any) -> dict[str, Any]:
    """Model Catalog（N §10.1：每个 Binding 一份，互不污染）。"""
    return catalog.model_dump(mode="json", by_alias=True)


#: ``/effective-settings`` 里每一项的 ``source`` 取值（封闭集合，前端按它分支）。
#:
#: ============  ==========================================================
#: ``binding``   这条 Binding 自己写了（Kaus 的账，用户在本界面上改过）
#: ``engine``    引擎自己的配置里有（Kaus 没写，值来自引擎那本账）
#: ``catalog``   来自 Model Catalog 的默认值
#: ``project``   来自 Project（目前只有 ``workspaceRoot``）
#: ``none``      **哪儿都没有**——``value`` 必为 ``null``，前端按 AD-71 不渲染
#: ============  ==========================================================
SETTING_SOURCES: tuple[str, ...] = ("binding", "engine", "catalog", "project", "none")


def _setting(value: Any, source: str, **extra: Any) -> dict[str, Any]:
    """一项有效设置。``value is None`` 与 ``source == "none"`` 必须同时成立。"""
    resolved = source if value is not None else "none"
    return {"value": value if resolved != "none" else None, "source": resolved, **extra}


def effective_settings_to_wire(
    *,
    binding: Any,
    project: Any = None,
    engine: Any = None,
    catalog: Any = None,
) -> dict[str, Any]:
    """把四个来源解析成「界面工具栏要显示什么、这个值是谁定的」（批次十三第 1 件）。

    解析顺序（前面的赢）：**Binding 行 → 引擎自己的配置 → 目录默认**。
    ``workspaceRoot`` 只有 Project 一个来源。

    为什么要有 ``source``：真机上 Media 项目的 Binding 行是空的，而引擎那边
    model / 推理强度 / 审批档全都有值，界面却显示「未设置」。光给 ``value``
    修不好这件事——用户还要知道「这是我设的，还是引擎那边本来就有的」，
    改一个「来自引擎」的值意味着从此由 Kaus 接管它。

    缺项返回 ``{"value": null, "source": "none"}``，**不编造默认值**
    （N §13.1）；前端按 AD-71 不渲染这一栏。
    """
    runtime_config = dict(getattr(binding, "runtime_config", None) or {})
    engine_model = getattr(engine, "model_id", None)
    engine_effort = getattr(engine, "reasoning_effort", None)
    engine_approval = getattr(engine, "approval_mode", None)
    catalog_default = getattr(catalog, "default_model_id", None)

    model_value = getattr(binding, "default_model_id", None)
    model_source = "binding"
    if not model_value and engine_model:
        model_value, model_source = engine_model, "engine"
    if not model_value and catalog_default:
        model_value, model_source = catalog_default, "catalog"

    effort_value = runtime_config.get("reasoning_effort")
    effort_source = "binding"
    if effort_value is None and engine_effort:
        effort_value, effort_source = engine_effort, "engine"

    approval_value = runtime_config.get("approval_mode")
    approval_source = "binding"
    if approval_value is None and engine_approval:
        approval_value, approval_source = engine_approval, "engine"

    return {
        "bindingId": getattr(binding, "id", None),
        "model": _setting(model_value or None, model_source),
        "reasoningEffort": _setting(
            effort_value,
            effort_source,
            # 这个模型支持哪些强度。空列表 = 目录说不出（AD-71：前端不渲染下拉）。
            levels=list(reasoning_levels_for(catalog, model_value)),
        ),
        "approvalMode": _setting(
            approval_value, approval_source, options=list(APPROVAL_MODES)
        ),
        "workspaceRoot": _setting(getattr(project, "workspace_root", None), "project"),
    }


def reasoning_levels_for(catalog: Any, model_id: str | None) -> tuple[str, ...]:
    """目录里这个模型的 ``reasoning_levels``；找不到就是空元组（不猜一份词表）。"""
    if catalog is None or not model_id:
        return ()
    for model in getattr(catalog, "models", ()) or ():
        if getattr(model, "model_id", None) == model_id:
            return tuple(getattr(model, "reasoning_levels", ()) or ())
    return ()


#: 取证输出里值一律截断到这个长度（批次十七第 4 件）。
DEBUG_PREVIEW_CHARS = 80

#: 键名里出现这些片段就连截断值都不给——取证要的是**形状**，不是内容，
#: 而这类键正是最不该被顺手复制出来的东西（AD-48：凭据不进响应/日志）。
_SECRET_KEY_HINTS: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "cookie",
)


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def debug_shape(value: Any, *, depth: int = 0) -> Any:
    """把任意 JSON 值变成**脱敏的形状描述**（键名 + 值类型 + 截断 80 字）。

    这是 ``/debug/last-history`` 唯一的输出通道（批次十七第 4 件）。为什么不能
    直接把原始响应体回给用户去看：那是一整段会话正文，而我们要回答的问题只有
    一个——「``tool_calls`` 到底在哪一层、叫什么名字」。形状足够回答它，正文不
    必上 wire。

    三条口径：
    1. 值一律截到 :data:`DEBUG_PREVIEW_CHARS` 字符，超了标 ``truncated``；
    2. 键名像凭据的（key/token/secret/…）连截断值都不给；
    3. 嵌套最多 4 层、数组最多描述前 3 个元素——再深就不是形状而是转储了。
    """
    if depth >= 4:
        return {"type": type(value).__name__, "note": "嵌套过深，已停止展开"}
    if isinstance(value, Mapping):
        return {
            "type": "object",
            "keys": {
                str(key): (
                    {"type": "redacted"}
                    if _looks_secret(str(key))
                    else debug_shape(item, depth=depth + 1)
                )
                for key, item in value.items()
            },
        }
    if isinstance(value, (list, tuple)):
        return {
            "type": "array",
            "length": len(value),
            "items": [debug_shape(item, depth=depth + 1) for item in list(value)[:3]],
        }
    if value is None or isinstance(value, bool):
        return {"type": "null" if value is None else "boolean", "value": value}
    if isinstance(value, (int, float)):
        return {"type": "number", "value": value}
    text = str(value)
    shape: dict[str, Any] = {
        "type": "string",
        "length": len(text),
        "preview": text[:DEBUG_PREVIEW_CHARS],
    }
    if len(text) > DEBUG_PREVIEW_CHARS:
        shape["truncated"] = True
    return shape


def conversation_surface(conversation: Conversation, *, lease: Any = None) -> str:
    """这条会话此刻在**哪个界面**上写：``card | external-cli``（批次十五第 2 件）。

    两个来源，前者赢：
    ``lease``                    还没过期的写权归谁，谁就是当前 Surface。这是
                                 事实（AD-122：lease 是单写者）。
    ``preferred_surface``        库里落盘的偏好。没有 lease（谁都没在写）时用它
                                 ——「上次在终端，现在没人写」仍然应该显示成
                                 外部终端，否则用户会以为交接已经回来了。

    过期的 lease 不算数：它的持有者早就不在了，再拿它当事实就是说谎。
    """
    if lease is not None and not getattr(lease, "is_stale", False):
        owner = getattr(lease, "owner_type", None)
        if owner in ("card", "external-cli"):
            return owner
    preferred = getattr(conversation, "preferred_surface", "card")
    return preferred if preferred in ("card", "external-cli") else "card"


def conversation_list_to_wire(
    conversations: Sequence[Conversation],
    *,
    project_id: str,
    surfaces: Mapping[str, str] | None = None,
    run_states: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """某个 Project 的会话列表。

    ``surfaces`` 是 ``conversation_id -> surface``（批次十五第 2 件）。不给就按
    每条会话自己的 ``preferred_surface`` 算——那是「没人在写」时的正确答案。

    ``run_states`` 同形（AD-159）：给了哪一条，那一条的旧字段 ``state`` 就按它
    收敛；没给的原样返回落盘值。
    """
    resolved = dict(surfaces or {})
    states = dict(run_states or {})
    rows = []
    for conversation in conversations:
        row = conversation_to_wire(
            conversation, run_state=states.get(conversation.id)
        )
        row["surface"] = resolved.get(
            conversation.id, conversation_surface(conversation)
        )
        rows.append(row)
    return {
        "projectId": project_id,
        "conversations": rows,
        "count": len(rows),
    }


#: :data:`~app.conversations.models.ConversationState` → 侧栏用的粗粒度状态。
#: 侧栏只需要回答「这条在跑吗」，把 card / external 两种在跑合并成一个
#: ``running``；``paused`` / ``ended`` / ``error`` 原样透出，因为它们要显示成
#: 不同的样子。取值是**封闭集合**，前端按它分支。
_STATUS_BY_STATE: dict[str, str] = {
    "idle": "idle",
    "running-card": "running",
    "running-external": "running",
    "paused": "paused",
    "ended": "ended",
    "error": "error",
}


def conversation_status(
    conversation: Conversation, *, live_run_state: str | None = None
) -> str:
    """这条会话此刻的状态：``idle | running | paused | ended | error``。

    ``live_run_state`` 是 Session Host 里那条 runtime 的 Reducer 状态（本进程
    知道的**实时**情况）；有它就以它为准，因为库里的 ``state`` 是落盘的快照，
    进程崩过一次之后可能还停在 ``running-card`` 上。拿不到就退回库里的值——
    N §13.1：不知道就说不知道，这里的「不知道」表现为「用落盘的那份」，
    而不是编一个 idle 出来。
    """
    if live_run_state == "running":
        return "running"
    if live_run_state in ("completed", "interrupted"):
        return "idle"
    if live_run_state == "failed":
        return "error"
    return _STATUS_BY_STATE.get(conversation.state, conversation.state)


def conversation_index_entry_to_wire(
    conversation: Conversation,
    *,
    backend_id: str | None,
    status: str,
    last_sequence: int | None,
    surface: str = "card",
    group_id: str | None = None,
    group_title: str | None = None,
) -> dict[str, Any]:
    """跨项目会话列表的一行（批次八第 1 件）。

    刻意是**扁平的一小把字段**，不是整个 Conversation：这个端点服务的是侧栏，
    每 30s 全量拉一次，字段越少越好。要完整对象请走
    ``GET /api/conversations/{id}``。

    ``backendId`` 从 Binding 上取（Conversation 自己不存 backend——D-06 之下
    Binding 才是那条不变的绑定关系）；取不到就是 ``null``，不编造。

    ``surface``（批次十五第 2 件）与 ``status`` 是两根**互不替代**的轴：status
    说「在不在跑」，surface 说「在哪个界面上写」。侧栏要同时显示这两件事——
    一条在外部终端里跑着的会话，点进去看到的是只读态，列表上就该先说清楚。
    见 :func:`conversation_surface`。

    ``groupId``（批次二十二第 2 件）是「这条会话现在在哪个**还开着的** Group 里」
    ——侧栏要给它标一个组标记。不在任何组里就是 ``null``；关掉的组不算数
    （那是历史，不该在侧栏上继续挂着）。取数在
    :func:`~app.api.group_router.group_labels_for_conversations`。

    ``groupTitle``（批次二十六第 4 件）与它同来同去：只有 id 的话侧栏要么显示一串
    ``collaboration:…``，要么为了把它翻成人话再打一次 ``GET /groups``——两条都不
    该是侧栏该做的事。同一次查库本来就读到了标题，顺手带上。
    """
    return {
        "id": conversation.id,
        "groupId": group_id,
        "groupTitle": group_title,
        "projectId": conversation.project_id,
        "bindingId": conversation.agent_binding_id,
        "backendId": backend_id,
        "title": conversation.title,
        "status": status,
        "archivedAt": conversation.archived_at.isoformat() if conversation.archived_at else None,
        "surface": surface,
        "updatedAt": conversation.updated_at.isoformat().replace("+00:00", "Z"),
        "lastSequence": last_sequence,
    }


def sse_data_line(payload: Mapping[str, Any], *, serializer) -> str:
    """一条 SSE 数据帧：``data: <json>`` + 空行。

    刻意只发 ``data:``，不发 ``event:``：事件种类已经在 envelope 的
    ``event.type`` 里，再在 SSE 层复述一遍就等于把公共事件的分类规则复制到
    传输层，将来加事件要改两处。
    """
    return f"data: {serializer(payload)}\n\n"


#: 服务端心跳（AD-62：Backend 侧没有心跳，我们自己发；注释行不进 EventSource
#: 的 message 回调，因此不会被前端当成事件）。
KEEPALIVE_FRAME = ": keepalive\n\n"

#: 开流填充（批次二十八）。真机现象：测试员用 Cloudflare quick tunnel 看页面，
#: 后端明明重放了 50+ 条事件，页面却显示「历史没有随重放到达」——隧道/代理在
#: 转发前会先攒够一小段字节，重放批不够大就一直卡在缓冲里。所以重放之后、开始
#: 等待之前先推 2KB 注释：注释行不会进 ``EventSource`` 的 message 回调（对前端
#: 完全透明），但足以把代理的缓冲顶出去。
SSE_PADDING_BYTES = 2048
SSE_PADDING_FRAME = ": " + " " * SSE_PADDING_BYTES + "\n\n"

#: 每条 SSE 端点都该带的响应头（批次二十八）。``no-transform`` 是关键的一条：
#: 光有 ``no-cache`` 挡不住中间代理为了省流量做压缩/重新分块，而那正是 SSE 被
#: 缓冲住的另一种死法。``X-Accel-Buffering: no`` 对 nginx 一类的代理生效。
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


__all__ = [
    "DEBUG_PREVIEW_CHARS",
    "KEEPALIVE_FRAME",
    "SSE_HEADERS",
    "SSE_PADDING_BYTES",
    "SSE_PADDING_FRAME",
    "SETTING_SOURCES",
    "advisory_to_wire",
    "conversation_index_entry_to_wire",
    "conversation_list_to_wire",
    "conversation_status",
    "conversation_surface",
    "conversation_to_wire",
    "derive_conversation_state",
    "debug_shape",
    "effective_settings_to_wire",
    "envelope_to_wire",
    "error_body",
    "model_catalog_to_wire",
    "native_history_to_wire",
    "reasoning_levels_for",
    "session_projection_to_wire",
    "runtime_view",
    "sse_data_line",
    "timeline_summary",
]
