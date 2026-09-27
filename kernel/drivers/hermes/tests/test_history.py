"""历史加载：HTTP 优先 + ``state.db`` 只读回退（规格 §4.2 / §4.3，验收点 3）。

fixture 的列名与行内容都来自 `docs/probes/2026-09-02-run3-live.md`：
``sessions`` 行是 ``native.history.acp`` 证据块的 55 列原文，``messages`` 列取
规格 §4.3 那两条 SQL 里列出的列（探针报告的 tables-and-columns 转储在 ``messages``
之前被截断了，所以列名的第一来源是规格 §4.3，它标的是 ``[实测]``）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from drivers.hermes import history

SESSION_ID = "api_1788339747_a7afdfa5"
CALL_ID = "call_00_smUvuG9hSJ7fY6kcQmcR1565"

#: run3 实测的一轮完整历史（4 行：user / assistant+tool_calls / tool / assistant）。
PROBE_ROWS = [
    {
        "id": 9,
        "session_id": SESSION_ID,
        "role": "user",
        "content": "Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and then reply with the output.",
        "timestamp": 1788339748.1,
        "active": "1",
    },
    {
        "id": 10,
        "session_id": SESSION_ID,
        "role": "assistant",
        "tool_calls": json.dumps(
            [
                {
                    "id": CALL_ID,
                    "call_id": CALL_ID,
                    "response_item_id": "fc_00_smUvuG9hSJ7fY6kcQmcR1565",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command": "echo HERMES-PROBE-TOOL-MARKER"}',
                    },
                }
            ]
        ),
        "finish_reason": "tool_calls",
        "reasoning": "The user wants me to run a shell command…",
        "reasoning_content": "The user wants me to run a shell command…",
        "timestamp": 1788339749.5,
        "active": "1",
    },
    {
        "id": 11,
        "session_id": SESSION_ID,
        "role": "tool",
        "content": '{"output": "HERMES-PROBE-TOOL-MARKER", "exit_code": 0, "error": null}',
        "tool_call_id": CALL_ID,
        "tool_name": "terminal",
        "timestamp": 1788339749.7,
        "active": "1",
    },
    {
        "id": 12,
        "session_id": SESSION_ID,
        "role": "assistant",
        "content": "HERMES-PROBE-TOOL-MARKER",
        "finish_reason": "stop",
        "timestamp": 1788339750.9,
        "active": "1",
    },
]


def build_state_db(path: Path, *, schema_version: int = 26, rows=PROBE_ROWS) -> Path:
    """按 schema 26 的列名造一个最小 ``state.db``。"""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE schema_version (version INTEGER)")
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (schema_version,))
    connection.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT, model_config TEXT,"
        " system_prompt TEXT, system_prompt_hash TEXT, parent_session_id TEXT,"
        " started_at REAL, ended_at REAL, end_reason TEXT, message_count INTEGER,"
        " tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
        " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " cwd TEXT, git_branch TEXT, git_repo_root TEXT, git_metadata_generation INTEGER,"
        " billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,"
        " estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,"
        " pricing_version TEXT, title TEXT, title_source TEXT, last_activity_at REAL,"
        " last_activity_description TEXT, last_activity_provenance TEXT, api_call_count INTEGER,"
        " compression_ineffective_count INTEGER, profile_name TEXT, rewind_count INTEGER,"
        " archived INTEGER, pinned INTEGER, hidden INTEGER, last_read_at REAL)"
    )
    connection.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
        " tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,"
        " display_kind TEXT, display_metadata TEXT, timestamp REAL, token_count INTEGER,"
        " finish_reason TEXT, reasoning TEXT, reasoning_content TEXT, api_content TEXT,"
        " active TEXT, observed TEXT, compacted INTEGER)"
    )
    connection.execute(
        "CREATE TABLE session_model_usage ("
        " session_id TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,"
        " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,"
        " billing_provider TEXT)"
    )
    connection.execute(
        "INSERT INTO sessions (id, source, model, started_at, message_count, tool_call_count,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
        " estimated_cost_usd, actual_cost_usd, cost_status, cost_source, billing_provider,"
        " title, title_source, last_activity_at, profile_name, archived, pinned, hidden)"
        " VALUES (?, 'api_server', 'deepseek-v4-flash', 1788339747.19, 4, 1,"
        " 14694, 87, 14720, 0, 17, 0.0018682384, NULL, 'estimated', 'official_docs_snapshot',"
        " 'deepseek', 'Run shell command', 'llm', 1788339750.9, 'default', 0, 0, 0)",
        (SESSION_ID,),
    )
    connection.execute(
        "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated_cost_usd,"
        " actual_cost_usd, cost_status) VALUES"
        " (?, 'deepseek-v4-flash', 14694, 87, 14720, 0, 17, 0.0018682384, NULL, 'estimated')",
        (SESSION_ID,),
    )
    for row in rows:
        keys = list(row)
        connection.execute(
            f"INSERT INTO messages ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            [row[k] for k in keys],
        )
    connection.commit()
    connection.close()
    return path


@pytest.fixture()
def state_db(tmp_path: Path) -> Path:
    return build_state_db(tmp_path / "state.db")


# --------------------------------------------------------------------------- #
# 只读回退
# --------------------------------------------------------------------------- #


def test_schema_26_enables_the_full_fallback(state_db: Path) -> None:
    tier = history.classify_schema(history.read_schema_version(state_db))
    assert tier.schema_version == 26
    assert tier.enabled and tier.full


def test_older_schema_is_downgraded_not_disabled(tmp_path: Path) -> None:
    """``[22, 26)``：只取公共列子集，但仍然可用。"""
    path = build_state_db(tmp_path / "old.db", schema_version=23)
    tier = history.classify_schema(history.read_schema_version(path))
    assert tier.enabled and not tier.full
    result = history.read_history_from_db(path, SESSION_ID, tier=tier)
    assert len(result.entries) == 4
    assert "schema_downgrade" in result.missing


def test_newer_schema_disables_the_fallback(tmp_path: Path) -> None:
    """``>26``：无法保证列语义未变 → 禁用直读，只用 HTTP（且不让卡片失败）。"""
    path = build_state_db(tmp_path / "new.db", schema_version=99)
    tier = history.classify_schema(history.read_schema_version(path))
    assert not tier.enabled
    assert "99" in (tier.reason or "")
    result = history.read_history_from_db(path, SESSION_ID, tier=tier)
    assert result.entries == () and result.missing == ("history",)


def test_reads_the_probe_turn_back_out_of_the_db(state_db: Path) -> None:
    result = history.read_history_from_db(state_db, SESSION_ID)
    assert result.native_session_id == SESSION_ID
    assert [e.role for e in result.entries] == ["user", "assistant", "tool", "assistant"]
    assert [e.kind for e in result.entries] == [
        "message",
        "tool_call",
        "tool_result",
        "message",
    ]
    # 完整入参与结果只能从这里取（SSE 不给，规格 §3.2-D）。
    call = result.entries[1]
    assert call.metadata["toolCalls"][0]["function"]["name"] == "terminal"
    assert result.entries[2].metadata["toolCallId"] == CALL_ID
    # reasoning_content 优先于 reasoning。
    assert call.metadata["reasoning"].startswith("The user wants me")
    # token_count 实测没有值 → 不要在 metadata 里放一个假的 0。
    assert "tokenCount" not in call.metadata


def test_ad34_tool_level_completeness_holds(state_db: Path) -> None:
    """AD-34：工具级不触发回退——原生历史里工具调用与结果都在。"""
    result = history.read_history_from_db(state_db, SESSION_ID)
    assert "tool_results" not in result.missing


def test_missing_tool_result_is_reported_not_swallowed(tmp_path: Path) -> None:
    rows = [row for row in PROBE_ROWS if row["id"] != 11]
    path = build_state_db(tmp_path / "partial.db", rows=rows)
    result = history.read_history_from_db(path, SESSION_ID)
    assert result.complete is False
    assert "tool_results" in result.missing


def test_approval_decisions_are_reported_missing_when_disposition_is_empty(
    state_db: Path,
) -> None:
    """§8-② 未定案：有工具调用而 ``effect_disposition`` 全空 → 如实报缺。"""
    result = history.read_history_from_db(state_db, SESSION_ID)
    assert "approval_decisions" in result.missing


def test_compacted_rows_are_reported(tmp_path: Path) -> None:
    rows = [*PROBE_ROWS, {**PROBE_ROWS[0], "id": 13, "compacted": 1}]
    path = build_state_db(tmp_path / "compacted.db", rows=rows)
    result = history.read_history_from_db(path, SESSION_ID)
    assert "compacted_segments" in result.missing


def test_inactive_rows_are_filtered(tmp_path: Path) -> None:
    rows = [*PROBE_ROWS, {**PROBE_ROWS[0], "id": 14, "active": "0", "content": "old"}]
    path = build_state_db(tmp_path / "inactive.db", rows=rows)
    result = history.read_history_from_db(path, SESSION_ID)
    assert all(e.text != "old" for e in result.entries)


def test_tail_window_and_upward_cursor(state_db: Path) -> None:
    """C-2：默认加载尾部窗口，向上懒加载。"""
    page = history.read_history_from_db(state_db, SESSION_ID, limit=2)
    assert [e.entry_id for e in page.entries] == ["11", "12"], "尾部两条，且是时间正序"
    assert page.earlier_cursor == "id:11"
    earlier = history.read_history_from_db(state_db, SESSION_ID, limit=2, before_id=11)
    assert [e.entry_id for e in earlier.entries] == ["9", "10"]


def test_connection_is_read_only(state_db: Path) -> None:
    """规格 §4.3：**永远只读**。写入必须被 SQLite 自己拒掉。"""
    with history.open_readonly(state_db) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO messages (session_id, role) VALUES ('x','user')")


def test_usage_fallback_reads_session_model_usage(state_db: Path) -> None:
    rows = history.read_usage_from_db(state_db, SESSION_ID)
    assert rows and rows[0]["model"] == "deepseek-v4-flash"
    # 成本口径：cost_status=estimated → UI 必须带「估算」标记，actual 为空不得当 0。
    assert rows[0]["cost_status"] == "estimated"
    assert rows[0]["actual_cost_usd"] is None


def test_session_row_exposes_the_measured_columns(state_db: Path) -> None:
    row = history.read_session_row(state_db, SESSION_ID)
    assert row is not None
    assert row["source"] == "api_server"
    assert row["title_source"] == "llm"
    assert row["last_activity_at"] == 1788339750.9


def test_watermark_query(state_db: Path) -> None:
    assert history.read_watermark(state_db, SESSION_ID) == (12, 4)


# --------------------------------------------------------------------------- #
# HTTP 路径
# --------------------------------------------------------------------------- #


def test_http_history_uses_the_measured_pagination_shape() -> None:
    payload = {
        "object": "list",
        "session_id": SESSION_ID,
        "data": list(reversed(PROBE_ROWS)),  # order=latest → 服务端给的是倒序
        "pagination": {"limit": 4, "offset": 0, "order": "latest", "returned": 4},
    }
    result = history.history_from_http(SESSION_ID, payload)
    assert result is not None
    assert [e.entry_id for e in result.entries] == ["9", "10", "11", "12"], "必须转成正序"
    assert result.earlier_cursor == "off:4"


def test_http_history_without_more_pages_has_no_cursor() -> None:
    payload = {
        "object": "list",
        "session_id": SESSION_ID,
        "data": [],
        "pagination": {"limit": 500, "offset": 0, "order": "latest", "returned": 0},
    }
    result = history.history_from_http(SESSION_ID, payload)
    assert result is not None and result.earlier_cursor is None


def test_cursor_roundtrip() -> None:
    assert history.decode_cursor(history.encode_cursor(120)) == 120
    assert history.decode_cursor(None) == 0
    assert history.decode_cursor("garbage") == 0


def test_batch16_tool_calls_from_history_pairs_calls_with_results() -> None:
    """批次十六第 3 件的数据源：真实 call_id、完整入参、按 id 索引的结果。"""
    entries = [history.to_history_entry(row) for row in PROBE_ROWS]
    calls, results = history.tool_calls_from_history(entries)
    assert [c.call_id for c in calls] == [CALL_ID]
    assert calls[0].name == "terminal"
    assert calls[0].arguments == {"command": "echo HERMES-PROBE-TOOL-MARKER"}
    # 批次十八第 3 件：取 `output` 字段本身，不是整个信封。
    assert results[CALL_ID] == "HERMES-PROBE-TOOL-MARKER"


def test_batch16_tool_calls_from_history_skips_rows_without_ids() -> None:
    """认不出来的行直接跳过，不猜一个 id 出来。"""
    entries = [
        history.to_history_entry(
            {
                "id": 1,
                "role": "assistant",
                "tool_calls": json.dumps([{"function": {"name": "terminal"}}]),
            }
        )
    ]
    calls, results = history.tool_calls_from_history(entries)
    assert calls == () and results == {}
