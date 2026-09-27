"""受管块与指令拼接的纯函数单测（批次四十四 / AD-165）。

这一份守的是本批**最不能错**的那条：块外一个字节都不许动。
"""

from __future__ import annotations

import pytest

from app.capabilities.instructions import InstructionsSpec, spec_of
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from drivers import managed_block
from drivers.instructions_projection import (
    INSTRUCTIONS_MARKER,
    compose_instructions,
    instruction_entries,
)

MARKER = INSTRUCTIONS_MARKER
PROJECT = "project:media"


def _block(body: str, project_id: str = PROJECT) -> str:
    return managed_block.render_block(MARKER, body, project_id)


# --------------------------------------------------------------------------- #
# 五种 action
# --------------------------------------------------------------------------- #


def test_create_on_a_missing_file() -> None:
    result = managed_block.upsert_block("", MARKER, "你好", PROJECT)
    assert result.action == "create"
    assert result.text == _block("你好") + "\n"
    assert managed_block.read_block(result.text, MARKER) == (PROJECT, "你好")


def test_create_on_a_whitespace_only_file() -> None:
    """只有空白的文件等于没有内容——不该在它下面追加出一堆空行。"""
    assert managed_block.upsert_block("\n\n  \n", MARKER, "x", PROJECT).action == "create"


def test_append_keeps_every_byte_of_the_user_content() -> None:
    original = "# 我自己的规矩\n\n别删我。\n"
    result = managed_block.upsert_block(original, MARKER, "项目指令", PROJECT)
    assert result.action == "append"
    assert result.text.startswith(original)
    assert "别删我。" in result.text
    assert managed_block.read_block(result.text, MARKER) == (PROJECT, "项目指令")


def test_append_when_the_file_has_no_trailing_newline() -> None:
    result = managed_block.upsert_block("末行没有换行", MARKER, "x", PROJECT)
    assert result.action == "append"
    assert result.text.startswith("末行没有换行\n")
    assert managed_block.read_block(result.text, MARKER) == (PROJECT, "x")


def test_replace_only_touches_the_inside_of_the_block() -> None:
    original = f"上面\n\n{_block('旧的')}\n\n下面\n"
    result = managed_block.upsert_block(original, MARKER, "新的", PROJECT)
    assert result.action == "replace"
    assert managed_block.read_block(result.text, MARKER) == (PROJECT, "新的")
    outside = result.text.replace(_block("新的"), "")
    assert outside == original.replace(_block("旧的"), "")


def test_unchanged_is_byte_for_byte_identical() -> None:
    original = f"头\n\n{_block('一样的')}\n"
    result = managed_block.upsert_block(original, MARKER, "一样的", PROJECT)
    assert result.action == "unchanged"
    assert result.text == original


def test_refuse_shared_when_the_block_belongs_to_another_project() -> None:
    original = _block("别人的", "project:trading") + "\n"
    result = managed_block.upsert_block(original, MARKER, "我的", PROJECT)
    assert result.action == "refuse_shared"
    assert result.text == original, "拒绝写就是一个字节都不动"


def test_a_block_head_without_project_is_still_recognised() -> None:
    """块头被人改坏时要**认得出**它——认不出就会再追加一个，文件里于是两个块。"""
    broken = "<!-- kaus:instructions:begin -->\n旧\n<!-- kaus:instructions:end -->\n"
    assert managed_block.read_block(broken, MARKER) == ("", "旧")
    assert managed_block.upsert_block(broken, MARKER, "新", PROJECT).action == "refuse_shared"


# --------------------------------------------------------------------------- #
# 边角
# --------------------------------------------------------------------------- #


def test_crlf_files_stay_crlf() -> None:
    original = "标题\r\n\r\n正文\r\n"
    result = managed_block.upsert_block(original, MARKER, "指令", PROJECT)
    assert result.action == "append"
    assert "\r\n<!-- kaus:instructions:begin" in result.text
    assert "\n<!-- kaus:instructions:begin" in result.text
    # 没有一处裸 LF 混进新写的那一段。
    tail = result.text[len(original) :]
    assert "\n" not in tail.replace("\r\n", "")
    assert managed_block.read_block(result.text, MARKER) == (PROJECT, "指令")


def test_two_blocks_take_the_first_and_warn() -> None:
    original = f"{_block('一')}\n\n{_block('二')}\n"
    result = managed_block.upsert_block(original, MARKER, "三", PROJECT)
    assert result.action == "replace"
    assert result.warnings and "2" in result.warnings[0]
    blocks = managed_block.find_blocks(result.text, MARKER)
    assert [b.body for b in blocks] == ["三", "二"], "只动第一个，第二个原样留着"


def test_read_block_on_a_file_without_one() -> None:
    assert managed_block.read_block("什么都没有\n", MARKER) is None


def test_round_trip_is_idempotent() -> None:
    text = "序\n"
    for _ in range(3):
        text = managed_block.upsert_block(text, MARKER, "正文", PROJECT).text
    assert len(managed_block.find_blocks(text, MARKER)) == 1


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


def test_body_must_not_be_blank() -> None:
    with pytest.raises(ValueError):
        InstructionsSpec(body="   ")
    with pytest.raises(ValueError):
        InstructionsSpec(body="")


def test_spec_of_accepts_both_wrappings() -> None:
    assert spec_of({"body": "x"}).order == 100
    assert spec_of({"value": {"body": "x", "order": 5}}).order == 5
    assert spec_of({"order": 5}) is None
    assert spec_of(None) is None


# --------------------------------------------------------------------------- #
# 拼接顺序
# --------------------------------------------------------------------------- #


def _entry(
    capability_id: str,
    body: str,
    *,
    order: int | None = None,
    chain: tuple[str, ...] = ("project:root",),
) -> EffectiveCapability:
    config: dict[str, object] = {"body": body}
    if order is not None:
        config["order"] = order
    return EffectiveCapability(
        capability_type="instructions",
        capability_id=capability_id,
        config=config,
        source_project_id=chain[-1],
        inherited=len(chain) > 1,
        contributing_project_ids=chain,
    )


def _effective(*entries: EffectiveCapability) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id="project:media", entries=entries)


def test_order_then_depth_then_id() -> None:
    deep = ("project:root", "project:media")
    effective = _effective(
        _entry("zzz", "Z", order=100),
        _entry("aaa", "A", order=100),
        _entry("late", "L", order=900),
        _entry("early", "E", order=10),
        _entry("child", "C", order=100, chain=deep),
    )
    assert [e.capability_id for e in instruction_entries(effective)] == [
        "early",  # order 最小
        "aaa",  # 同 order、同深度 → 按 id
        "zzz",
        "child",  # 同 order、更深 → 排在根定的后面
        "late",
    ]


def test_compose_is_deterministic_and_shaped() -> None:
    effective = _effective(_entry("house-style", "一律说人话。"), _entry("rules", "先读规矩。"))
    text = compose_instructions(effective)
    assert text == "## house-style\n\n一律说人话。\n\n## rules\n\n先读规矩。"
    assert compose_instructions(effective) == text


def test_compose_on_nothing_is_empty() -> None:
    assert compose_instructions(_effective()) == ""


def test_malformed_entries_are_dropped_not_rendered_as_empty_sections() -> None:
    effective = _effective(_entry("ok", "有内容"), _entry("blank", "   "))
    assert compose_instructions(effective) == "## ok\n\n有内容"
