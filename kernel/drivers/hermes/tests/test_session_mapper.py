"""会话身份与 canonical head（规格 §4.1 / §4.4）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivers.hermes import session_mapper

BINDING = "binding:demo:hermes"

#: run3 实测的列表项全字段（一条 ACP 会话）。
ACP_ROW = {
    "id": "47001d1c-5a8e-47cd-b05a-fd1cbee30758",
    "source": "acp",
    "user_id": None,
    "model": "deepseek-v4-flash",
    "title": "Run shell command echo HERMES-PROBE-TOOL-MARKER",
    "started_at": 1788339708.659033,
    "ended_at": None,
    "end_reason": None,
    "message_count": 4,
    "tool_call_count": 1,
    "input_tokens": 12912,
    "output_tokens": 87,
    "cache_read_tokens": 12928,
    "cache_write_tokens": 0,
    "reasoning_tokens": 17,
    "estimated_cost_usd": 0.0018682384,
    "actual_cost_usd": None,
    "api_call_count": 2,
    "parent_session_id": None,
    "last_active": 1788339741.038286,
    "preview": "Run the shell command `echo HERMES-PROBE-TOOL-MARKER` and th...",
    "pinned": False,
    "archived": False,
    "hidden": False,
    "has_system_prompt": True,
    "has_model_config": True,
}


@pytest.mark.parametrize(
    ("session_id", "shape"),
    [
        ("api_1788339747_a7afdfa5", "api"),
        ("20260902_170427_716727", "cli"),
        ("47001d1c-5a8e-47cd-b05a-fd1cbee30758", "acp"),
        ("something-else", "unknown"),
    ],
)
def test_three_id_shapes_coexist(session_id: str, shape: str) -> None:
    """规格 §4.1：三种形状落在同一张 sessions 表，Driver 原样存、不归一。"""
    assert session_mapper.session_id_shape(session_id) == shape


def test_scope_ref_resolves_to_hermes_home(tmp_path: Path) -> None:
    """规格 §1.7 规则 2：``default`` → root；其余 → ``root/profiles/<name>``。"""
    assert session_mapper.resolve_hermes_home("default", hermes_root=tmp_path) == tmp_path
    assert (
        session_mapper.resolve_hermes_home("coder", hermes_root=tmp_path)
        == tmp_path / "profiles" / "coder"
    )
    assert session_mapper.resolve_hermes_home(None, hermes_root=tmp_path) == tmp_path


def test_list_item_maps_to_the_public_dto_without_leaking_private_fields() -> None:
    session = session_mapper.to_native_session(
        ACP_ROW, binding_id=BINDING, canonical=session_mapper.CanonicalMap.empty()
    )
    assert session.native_session_id == ACP_ROW["id"]
    assert session.message_count == 4
    # run3 推翻了「无 updated_at」：列表项带 last_active。
    assert session.updated_at is not None
    assert session.updated_at.timestamp() == pytest.approx(1788339741.038286)
    # preview / token / cost 没有公共字段——不许塞进公共 DTO（N §3）。
    dumped = session.model_dump()
    for private in ("preview", "estimated_cost_usd", "tool_call_count", "input_tokens"):
        assert private not in dumped


def test_hidden_and_archived_are_filtered(tmp_path: Path) -> None:
    rows = [
        ACP_ROW,
        {**ACP_ROW, "id": "hidden-one", "hidden": True},
        {**ACP_ROW, "id": "archived-one", "archived": True},
    ]
    visible = session_mapper.visible_sessions(
        rows, binding_id=BINDING, canonical=session_mapper.CanonicalMap.empty()
    )
    assert [s.native_session_id for s in visible] == [ACP_ROW["id"]]


def test_pinned_first_then_most_recent() -> None:
    rows = [
        {**ACP_ROW, "id": "old", "last_active": 1.0},
        {**ACP_ROW, "id": "recent", "last_active": 100.0},
        {**ACP_ROW, "id": "pinned", "last_active": 2.0, "pinned": True},
    ]
    visible = session_mapper.visible_sessions(
        rows, binding_id=BINDING, canonical=session_mapper.CanonicalMap.empty()
    )
    assert [s.native_session_id for s in visible] == ["pinned", "recent", "old"]


# --------------------------------------------------------------------------- #
# canonical head（存量兼容，规格 §4.4）
# --------------------------------------------------------------------------- #


def _write_archive(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "kind": "dashboard.canonical_sessions.v1",
                "schema_version": 1,
                "policy": {
                    "archived_segments_not_replayed_by_default": True,
                    "canonical_head_is_only_visible_window": True,
                    "runtime_restore_must_bind_to_canonical_head": True,
                },
                "profiles": {
                    "default": {
                        "canonical_session_id": "20260611_131228_3ddd03",
                        "canonical_titles": {"20260611_131228_3ddd03": "主线"},
                        "chains": [
                            {
                                "head": "20260611_131228_3ddd03",
                                "archived_session_ids": [
                                    "20260610_101010_aaaaaa",
                                    "20260609_090909_bbbbbb",
                                ],
                                "title": "主线（含归档）",
                            }
                        ],
                        "hidden_session_ids": ["20260601_000000_cccccc"],
                        "standalone_archived_session_ids": ["20260531_000000_dddddd"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_canonical_head_and_segments(tmp_path: Path) -> None:
    canonical = session_mapper.load_canonical_map(_write_archive(tmp_path / "a.json"))
    assert canonical.head_for("20260610_101010_aaaaaa") == "20260611_131228_3ddd03"
    assert canonical.segments_for("20260611_131228_3ddd03") == (
        "20260610_101010_aaaaaa",
        "20260609_090909_bbbbbb",
    )
    # canonical_titles[head] 优先于 sessions.title。
    assert canonical.title_for("20260611_131228_3ddd03") == "主线"


def test_hidden_and_segments_are_excluded_from_the_list(tmp_path: Path) -> None:
    """规格 §4.4 规则 3：否则用户会看到本该被折叠的历史分段。"""
    canonical = session_mapper.load_canonical_map(_write_archive(tmp_path / "a.json"))
    rows = [
        {**ACP_ROW, "id": "20260611_131228_3ddd03"},
        {**ACP_ROW, "id": "20260610_101010_aaaaaa"},
        {**ACP_ROW, "id": "20260601_000000_cccccc"},
        {**ACP_ROW, "id": "20260531_000000_dddddd"},
    ]
    visible = session_mapper.visible_sessions(rows, binding_id=BINDING, canonical=canonical)
    assert [s.native_session_id for s in visible] == ["20260611_131228_3ddd03"]
    assert visible[0].head_id == "20260611_131228_3ddd03"
    assert len(visible[0].segments) == 2


def test_missing_or_broken_archive_degrades_to_empty(tmp_path: Path) -> None:
    """canonical 机制坏掉不该让卡片打不开。"""
    assert session_mapper.load_canonical_map(tmp_path / "nope.json").chains == ()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert session_mapper.load_canonical_map(bad).chains == ()
    wrong_kind = tmp_path / "wrong.json"
    wrong_kind.write_text(json.dumps({"kind": "something.else"}), encoding="utf-8")
    assert session_mapper.load_canonical_map(wrong_kind).chains == ()


def test_new_api_sessions_are_their_own_head(tmp_path: Path) -> None:
    """规格 §4.4 规则 6：新会话不写归档文件，自己就是 head、无 segments。"""
    canonical = session_mapper.load_canonical_map(_write_archive(tmp_path / "a.json"))
    session = session_mapper.to_native_session(
        {**ACP_ROW, "id": "api_1788339747_a7afdfa5"},
        binding_id=BINDING,
        canonical=canonical,
    )
    assert session.head_id == "api_1788339747_a7afdfa5"
    assert session.segments == ()
