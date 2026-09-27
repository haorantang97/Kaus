"""原生历史：HTTP 优先，``state.db`` 只读回退（规格 §4.2 / §4.3）。

优先级（规格 §4.2）
-------------------
1. ``GET /api/sessions/{id}/messages``；分页 ``limit`` / ``offset`` /
   ``order``（默认 ``latest``，实测）。C-2 的「尾部窗口 + 向上懒加载」直接用它，
   ``earlier_cursor`` 编码成 ``off:<offset>``。
2. 直读 ``state.db``（**只读、WAL、query_only**）。
3. 两者都失败 → ``complete=False, missing=("history",)``，卡片显式提示。

schema 版本分档（AD-35 / 规格 §4.3）
------------------------------------
==================== ==========================================================
``schema_version==26`` 直读回退全功能
``[22, 26)``           只取公共列子集（0.21.0 新增列改为可选）
``>26`` 或读不到       **禁用直读回退**（只用 HTTP）+ 一条 warn
==================== ==========================================================

任何情况下都不因 schema 不认识而让卡片失败——HTTP 是主路径，直读只是回退。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from drivers.base import NativeHistory, NativeHistoryEntry

LOGGER = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
#: 实测 ``pagination.limit`` 默认 500；上限未验证（§8-⑦）→ 不主动越过它。
MAX_PAGE_SIZE = 500

CURSOR_PREFIX = "off:"

PINNED_SCHEMA_VERSION = 26
MIN_SCHEMA_VERSION = 22

#: 规格 §4.3：0.21.0 才有的列。低版本分档时它们改为可选取值。
SCHEMA_26_ONLY_MESSAGE_COLUMNS: frozenset[str] = frozenset({"compacted", "effect_disposition"})

_ROLES = {"user", "assistant", "system", "tool"}

#: 批次十七第 4 件：HTTP 响应体的字段名规格里是 ``[未验证]``（§4.2 只实测了
#: ``messages`` 表的列名）。真机上工具行读不出来最可能的原因就是它换了个位置或
#: 换了个写法，于是这里把三种写法都认下来，并**归一到列名口径**再往下走：
#: 顶层 snake_case（规格写的那种）/ 顶层 camelCase / 藏在 ``metadata`` 之下。
#: 认不出来仍然当没有——放宽的是「同一个东西的别名」，不是猜一个值出来。
_ROW_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "tool_calls": ("tool_calls", "toolCalls"),
    "tool_call_id": ("tool_call_id", "toolCallId"),
    "tool_name": ("tool_name", "toolName"),
    "effect_disposition": ("effect_disposition", "effectDisposition"),
    "finish_reason": ("finish_reason", "finishReason"),
    "reasoning_content": ("reasoning_content", "reasoningContent"),
    "token_count": ("token_count", "tokenCount"),
}


def normalize_message_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """把一行原始 message 归一到 ``messages`` 表的列名口径（批次十七第 4 件）。

    只做**搬运**：顶层已有这个列名就原样留着；没有就去 camelCase 写法、再去
    ``metadata`` 子对象里找同名/驼峰的键。找不到就没有这个键——不造值。
    """
    merged = dict(row)
    nested = row.get("metadata")
    nested = nested if isinstance(nested, Mapping) else {}
    for column, aliases in _ROW_FIELD_ALIASES.items():
        if merged.get(column) not in (None, ""):
            continue
        for source in (row, nested):
            for alias in aliases:
                value = source.get(alias)
                if value not in (None, ""):
                    merged[column] = value
                    break
            if merged.get(column) not in (None, ""):
                break
    return merged


# --------------------------------------------------------------------------- #
# 一行 → NativeHistoryEntry
# --------------------------------------------------------------------------- #


def _occurred_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except ValueError:
            return None
    return None


def _classify(row: Mapping[str, Any]) -> str:
    """规格 §4.2 的 ``kind`` 派生规则。"""
    if row.get("role") == "tool" or row.get("tool_call_id"):
        return "tool_result"
    if row.get("tool_calls"):
        return "tool_call"
    if row.get("effect_disposition"):
        return "decision"
    return "message"


def _private_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Hermes 私有字段的**唯一**合法去处之一：``NativeHistoryEntry.metadata``。

    ``reasoning_content`` 优先于 ``reasoning``（实测两者本轮内容相同，规格 §4.2）。
    ``token_count`` 实测**没有值**——所以缺失时不写 0，宁可没有这个键。
    """
    metadata: dict[str, Any] = {}
    tool_calls = row.get("tool_calls")
    if isinstance(tool_calls, str) and tool_calls:
        try:
            tool_calls = json.loads(tool_calls)
        except ValueError:
            pass
    if tool_calls:
        metadata["toolCalls"] = tool_calls
    for source, target in (
        ("tool_call_id", "toolCallId"),
        ("tool_name", "toolName"),
        ("effect_disposition", "effectDisposition"),
        ("finish_reason", "finishReason"),
    ):
        value = row.get(source)
        if value not in (None, ""):
            metadata[target] = value
    reasoning = row.get("reasoning_content") or row.get("reasoning")
    if reasoning:
        metadata["reasoning"] = reasoning
    token_count = row.get("token_count")
    if isinstance(token_count, int):
        metadata["tokenCount"] = token_count
    if row.get("compacted"):
        metadata["compacted"] = True
    raw_role = row.get("role")
    if raw_role not in _ROLES:
        metadata["rawRole"] = raw_role
    return metadata


def to_history_entry(row: Mapping[str, Any]) -> NativeHistoryEntry:
    role = row.get("role")
    return NativeHistoryEntry(
        entry_id=str(row.get("id")),
        role=role if role in _ROLES else "system",
        kind=_classify(row),  # type: ignore[arg-type]
        text=row.get("content") if isinstance(row.get("content"), str) else None,
        occurred_at=_occurred_at(row.get("timestamp")),
        metadata=_private_metadata(row),
    )


def assess_completeness(rows: Sequence[Mapping[str, Any]]) -> tuple[bool, tuple[str, ...]]:
    """规格 §4.2 的 ``complete`` / ``missing`` 判定（R-02 / AD-34 的判据）。

    - 有 ``tool_calls`` 但配不到结果行 → ``tool_results``。
      **AD-34 已裁定工具级不触发回退**——这里如实上报，但不据此建
      ``conversation_events``（那是上层的事）。
    - 有工具调用而 ``effect_disposition`` 全空 → ``approval_decisions``（§8-② 未定案）。
    - 存在 ``compacted=1`` 的行 → ``compacted_segments``。
    """
    missing: list[str] = []
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    has_disposition = False
    has_compacted = False
    for row in rows:
        raw_calls = row.get("tool_calls")
        if isinstance(raw_calls, str) and raw_calls:
            try:
                raw_calls = json.loads(raw_calls)
            except ValueError:
                raw_calls = None
        if isinstance(raw_calls, list):
            for call in raw_calls:
                if isinstance(call, Mapping):
                    identifier = call.get("id") or call.get("call_id")
                    if isinstance(identifier, str):
                        call_ids.add(identifier)
        tool_call_id = row.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            result_ids.add(tool_call_id)
        if row.get("effect_disposition"):
            has_disposition = True
        if row.get("compacted"):
            has_compacted = True
    if call_ids - result_ids:
        missing.append("tool_results")
    if call_ids and not has_disposition:
        missing.append("approval_decisions")
    if has_compacted:
        missing.append("compacted_segments")
    return (not missing), tuple(missing)


@dataclass(frozen=True)
class NativeToolCall:
    """原生历史里的一次工具调用（``messages.tool_calls`` 的一个元素）。

    ``call_id`` 是**真实**的调用 id（实测形如 ``call_00_smUvuG9hSJ7fY6kcQmcR1565``，
    与 ``role="tool"`` 行的 ``tool_call_id`` 相等）；``arguments`` 是完整入参
    （能解析成 JSON 就给结构，解析不了就原样给字符串——不猜、不丢）。
    """

    call_id: str
    name: str | None = None
    arguments: Any = None


def _parse_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


#: 批次十八第 3 件：真机取证（`docs/quality/verify-next.md` 末尾）确认工具结果行
#: 的 ``content`` 是一个 **JSON 字符串**，形如 ``{"output": "/Users/…\n…"}``。
#: 工具卡要显示的是那段输出本身，不是包着它的那层信封。
TOOL_RESULT_OUTPUT_KEY = "output"


def _parse_output(text: str | None) -> Any:
    """工具结果行的 ``content`` → 卡片上的 ``output``（规格 §4.2 / 批次十八第 3 件）。

    三档，依次退让：

    1. 是 JSON 对象且带 ``output`` 键 → 取那个值（真机就是这一档）；
    2. 是别的合法 JSON → 原样给结构（不猜里面哪个字段是输出）；
    3. 不是 JSON → 原样给字符串。

    任何一档都不会把内容丢掉：解析失败退回原串，比给一个空工具卡诚实。
    """
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, Mapping) and TOOL_RESULT_OUTPUT_KEY in parsed:
        return parsed[TOOL_RESULT_OUTPUT_KEY]
    return parsed


def tool_calls_from_history(
    entries: Sequence[Any],
) -> tuple[tuple[NativeToolCall, ...], dict[str, Any]]:
    """从 :class:`NativeHistoryEntry` 序列里取「工具调用」与「工具结果」两张表。

    批次十六第 3 件的数据来源：SSE 不带工具入参与输出（规格 §3.2-D），只有这里
    有。调用按历史顺序返回（同一行里的多个 ``tool_calls`` 保持数组顺序），结果
    按真实 ``tool_call_id`` 索引。

    读不到就少一条，不编：``metadata`` 里没有 ``toolCalls`` 的行直接跳过，
    结果行没有 ``toolCallId`` 的也跳过（无从配对）。
    """
    calls: list[NativeToolCall] = []
    results: dict[str, Any] = {}
    for entry in entries:
        metadata = getattr(entry, "metadata", None) or {}
        raw_calls = metadata.get("toolCalls")
        if isinstance(raw_calls, str):
            try:
                raw_calls = json.loads(raw_calls)
            except ValueError:
                raw_calls = None
        if isinstance(raw_calls, list):
            for item in raw_calls:
                if not isinstance(item, Mapping):
                    continue
                identifier = item.get("id") or item.get("call_id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                function = item.get("function")
                name = None
                arguments = None
                if isinstance(function, Mapping):
                    raw_name = function.get("name")
                    name = raw_name if isinstance(raw_name, str) else None
                    arguments = _parse_arguments(function.get("arguments"))
                calls.append(
                    NativeToolCall(call_id=identifier, name=name, arguments=arguments)
                )
        result_id = metadata.get("toolCallId")
        if isinstance(result_id, str) and result_id:
            results[result_id] = _parse_output(getattr(entry, "text", None))
    return tuple(calls), results


def encode_cursor(offset: int) -> str:
    return f"{CURSOR_PREFIX}{offset}"


def decode_cursor(cursor: str | None) -> int:
    if not cursor or not cursor.startswith(CURSOR_PREFIX):
        return 0
    try:
        return max(0, int(cursor[len(CURSOR_PREFIX) :]))
    except ValueError:
        return 0


def build_history(
    native_session_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    earlier_cursor: str | None = None,
    complete_override: bool | None = None,
    extra_missing: Iterable[str] = (),
) -> NativeHistory:
    complete, missing = assess_completeness(rows)
    missing = tuple(dict.fromkeys((*missing, *extra_missing)))
    if complete_override is not None:
        complete = complete_override
    return NativeHistory(
        native_session_id=native_session_id,
        entries=tuple(to_history_entry(row) for row in rows),
        complete=complete and not missing,
        missing=missing,
        earlier_cursor=earlier_cursor,
    )


# --------------------------------------------------------------------------- #
# HTTP 响应
# --------------------------------------------------------------------------- #


def history_from_http(
    native_session_id: str, payload: Mapping[str, Any] | None
) -> NativeHistory | None:
    """``GET /api/sessions/{id}/messages`` 的响应 → :class:`NativeHistory`。

    响应体的字段名 ``[未验证]``（规格 §4.2）→ 取值时与 ``messages`` 表列名取交集
    并做容错，认不出来就当没有这一条，而不是猜一个。

    批次十七第 4 件：每行先过一遍 :func:`normalize_message_row`（顶层 / 驼峰 /
    ``metadata`` 下三种写法都认），并把**实际见到的键名**写一行日志——真机上
    工具回填没生效时，第一件要问的就是「这份响应到底长什么样」。日志只打键名，
    不打值。
    """
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    raw_rows = [row for row in data if isinstance(row, Mapping)]
    rows = [normalize_message_row(row) for row in raw_rows]
    if raw_rows:
        observed: set[str] = set()
        for row in raw_rows:
            observed.update(str(key) for key in row.keys())
        LOGGER.info(
            "原生历史 HTTP 响应形状：%d 行，键名 %s，其中带 tool_calls 的 %d 行",
            len(raw_rows),
            sorted(observed),
            sum(1 for row in rows if row.get("tool_calls")),
        )
    pagination = payload.get("pagination")
    cursor = None
    if isinstance(pagination, Mapping):
        limit = pagination.get("limit")
        offset = pagination.get("offset")
        returned = pagination.get("returned")
        if (
            isinstance(limit, int)
            and isinstance(offset, int)
            and isinstance(returned, int)
            and returned >= limit > 0
        ):
            cursor = encode_cursor(offset + returned)
    # 服务端按 order=latest 给的是倒序尾部窗口；卡片要的是时间正序。
    rows.sort(key=lambda row: _sort_key(row))
    return build_history(native_session_id, rows, earlier_cursor=cursor)


def _sort_key(row: Mapping[str, Any]) -> tuple[float, float]:
    timestamp = row.get("timestamp")
    identifier = row.get("id")
    ts = float(timestamp) if isinstance(timestamp, (int, float)) else 0.0
    try:
        ident = float(identifier)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        ident = 0.0
    return (ts, ident)


# --------------------------------------------------------------------------- #
# state.db 只读回退
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SchemaTier:
    """规格 §4.3 的分档结果。"""

    schema_version: int | None
    enabled: bool
    full: bool
    reason: str | None = None


def read_schema_version(db_path: Path | str) -> int | None:
    try:
        with open_readonly(db_path) as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
    except sqlite3.Error:
        pass
    try:
        with open_readonly(db_path) as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
            if row and row[0]:
                return int(row[0])
    except sqlite3.Error:
        return None
    return None


def classify_schema(schema_version: int | None) -> SchemaTier:
    if schema_version is None:
        return SchemaTier(
            None,
            enabled=False,
            full=False,
            reason="读不到 state.db 的 schema 版本，直读回退已禁用（只用 HTTP）",
        )
    if schema_version == PINNED_SCHEMA_VERSION:
        return SchemaTier(schema_version, enabled=True, full=True)
    if MIN_SCHEMA_VERSION <= schema_version < PINNED_SCHEMA_VERSION:
        return SchemaTier(
            schema_version,
            enabled=True,
            full=False,
            reason=f"state.db schema {schema_version} 低于基准 {PINNED_SCHEMA_VERSION}，只取公共列子集",
        )
    return SchemaTier(
        schema_version,
        enabled=False,
        full=False,
        reason=(
            f"state.db schema {schema_version} 高于基准 {PINNED_SCHEMA_VERSION}，"
            "无法保证列语义未变，直读回退已禁用"
        ),
    )


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    """规格 §4.3 的连接口径。**永远只读**，绝不 checkpoint、绝不改 journal_mode。"""
    uri = f"file:{Path(db_path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = 1")
    connection.execute("PRAGMA busy_timeout = 1000")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


MESSAGE_COLUMNS: tuple[str, ...] = (
    "id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "effect_disposition",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "active",
    "compacted",
)

SESSION_COLUMNS: tuple[str, ...] = (
    "id",
    "source",
    "title",
    "title_source",
    "model",
    "started_at",
    "ended_at",
    "last_activity_at",
    "message_count",
    "tool_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "cost_source",
    "billing_provider",
    "parent_session_id",
    "cwd",
    "profile_name",
    "archived",
    "pinned",
    "hidden",
    "last_read_at",
)


def read_session_row(db_path: Path | str, session_id: str) -> dict[str, Any] | None:
    """规格 §4.3 的会话元数据查询（列按实际存在的取交集）。"""
    with open_readonly(db_path) as connection:
        available = _table_columns(connection, "sessions")
        columns = [c for c in SESSION_COLUMNS if c in available]
        if not columns:
            return None
        row = connection.execute(
            f"SELECT {', '.join(columns)} FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def read_history_from_db(
    db_path: Path | str,
    session_id: str,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
    tier: SchemaTier | None = None,
) -> NativeHistory:
    """规格 §4.3 的历史查询：尾部窗口 + 向上懒加载（C-2）。"""
    tier = tier or classify_schema(read_schema_version(db_path))
    if not tier.enabled:
        return NativeHistory(
            native_session_id=session_id,
            complete=False,
            missing=("history",),
        )
    with open_readonly(db_path) as connection:
        available = _table_columns(connection, "messages")
        columns = [c for c in MESSAGE_COLUMNS if c in available]
        if "id" not in columns or "role" not in columns:
            return NativeHistory(
                native_session_id=session_id, complete=False, missing=("history",)
            )
        where = ["session_id = ?"]
        params: list[Any] = [session_id]
        if "active" in available:
            # 规格 §4.3：active 的确切语义 [未验证]（§8-⑦），但 active=1 与
            # Omnigent 的用法一致，且 run3 的 4 行全是 1。
            where.append("active = 1")
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        params.append(max(1, min(limit, MAX_PAGE_SIZE)))
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM messages"
            f" WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    ordered = [dict(row) for row in reversed(rows)]
    cursor = None
    if ordered and len(rows) >= limit:
        cursor = f"id:{ordered[0]['id']}"
    extra: list[str] = []
    if not tier.full:
        extra.append("schema_downgrade")
    return build_history(session_id, ordered, earlier_cursor=cursor, extra_missing=extra)


def read_usage_from_db(db_path: Path | str, session_id: str) -> list[dict[str, Any]]:
    """规格 §3.7 的用量回退：``session_model_usage``（只在 HTTP 不可用时读）。"""
    try:
        with open_readonly(db_path) as connection:
            available = _table_columns(connection, "session_model_usage")
            wanted = [
                "model",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "estimated_cost_usd",
                "actual_cost_usd",
                "cost_status",
            ]
            columns = [c for c in wanted if c in available]
            if not columns:
                return []
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM session_model_usage WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def read_watermark(db_path: Path | str, session_id: str) -> tuple[int | None, int]:
    """规格 §5 第二级：``SELECT MAX(id), COUNT(*) FROM messages WHERE session_id = ?``。"""
    with open_readonly(db_path) as connection:
        row = connection.execute(
            "SELECT MAX(id), COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None:
        return None, 0
    max_id = row[0]
    return (int(max_id) if max_id is not None else None), int(row[1] or 0)


__all__ = [
    "CURSOR_PREFIX",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MESSAGE_COLUMNS",
    "MIN_SCHEMA_VERSION",
    "PINNED_SCHEMA_VERSION",
    "SESSION_COLUMNS",
    "SchemaTier",
    "assess_completeness",
    "build_history",
    "classify_schema",
    "decode_cursor",
    "encode_cursor",
    "NativeToolCall",
    "TOOL_RESULT_OUTPUT_KEY",
    "history_from_http",
    "open_readonly",
    "read_history_from_db",
    "read_schema_version",
    "read_session_row",
    "read_usage_from_db",
    "read_watermark",
    "to_history_entry",
    "normalize_message_row",
    "tool_calls_from_history",
]
