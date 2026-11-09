"""开对话前的自动送达（设计稿第 3 / 3a 节）：计划怎么算、各适配器钩子把什么落到进程上。

不起任何子进程、不碰任何真实家目录；文件只写进 ``tmp_path``。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities.models import BlockedCapability, EffectiveCapabilities, EffectiveCapability
from drivers import managed_block
from drivers.acp.client import AcpAgentSpec
from drivers.base import WorkspaceRequiredError
from drivers.instructions_projection import INSTRUCTIONS_MARKER
from drivers.launch_delivery import (
    CHANNEL_LAUNCH,
    CHANNEL_WORKSPACE_FILE,
    MCP_CLEAN,
    MCP_NONE,
    MCP_UNISOLATED,
    MCP_UNMANAGED,
    MCP_UNVERIFIED,
    REASON_NATIVE_NOT_ISOLATED,
    REASON_WORKSPACE_IN_USE,
    apply_launch_plan,
    plan_launch,
)
from drivers.skill_execution import claude, codex, copilot, gemini, opencode, qwen

PROJECT = "project:media"


def _instruction(capability_id: str, body: str, **extra) -> EffectiveCapability:
    return EffectiveCapability(capability_type="instructions", capability_id=capability_id,
                               config={"body": body, **extra}, source_project_id=PROJECT, inherited=False)


def _native(capability_id: str, agent: str = "fake") -> EffectiveCapability:
    return EffectiveCapability(capability_type=f"{agent}:mcp", capability_id=capability_id, config={},
                               source_project_id=PROJECT, inherited=False)


def _effective(*entries, **fields) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id=PROJECT, entries=entries, **fields)


def _record(name: str, capability_id: str) -> dict:
    return {"name": name, "capabilityId": capability_id, "sourcePath": "/synthetic/config",
            "server": {"transport": "stdio", "command": name}}


LAUNCH_ADAPTER = SimpleNamespace(deliver_instructions=lambda spec, metadata, body: (spec, {**metadata, "body": body}))
FILE_ADAPTER = SimpleNamespace()


def _spec(**env) -> AcpAgentSpec:
    return AcpAgentSpec(command=("agent", "--acp"), env=env, inherit_env=False)


# --------------------------------------------------------------------------- #
# 指令通道
# --------------------------------------------------------------------------- #


def test_launch_channel_wins_and_writes_no_file(tmp_path: Path) -> None:
    plan = plan_launch(_effective(_instruction("style", "简短。")), adapter=LAUNCH_ADAPTER,
                       instructions_file="NOTES.md", workspace_root=str(tmp_path), project_id=PROJECT)
    assert plan.instructions_channel == CHANNEL_LAUNCH
    assert plan.summary == {"instructions": ["style"]}
    assert plan.unapplied == ()
    assert not (tmp_path / "NOTES.md").exists()
    spec, metadata, warnings = apply_launch_plan(plan, _spec(), {}, adapter=LAUNCH_ADAPTER, cwd=str(tmp_path))
    assert metadata["body"] == "## style\n\n简短。" and warnings == ()


def test_file_channel_writes_the_managed_block_now(tmp_path: Path) -> None:
    (tmp_path / "NOTES.md").write_text("# 我的\n", encoding="utf-8")
    plan = plan_launch(_effective(_instruction("style", "简短。")), adapter=FILE_ADAPTER,
                       instructions_file="NOTES.md", workspace_root=str(tmp_path), project_id=PROJECT)
    assert plan.instructions_channel == CHANNEL_WORKSPACE_FILE
    text = (tmp_path / "NOTES.md").read_text(encoding="utf-8")
    assert text.startswith("# 我的\n")
    assert managed_block.read_block(text, INSTRUCTIONS_MARKER) == (PROJECT, "## style\n\n简短。")
    assert plan.unapplied == ()


def test_file_channel_still_writes_when_the_folder_is_in_use_and_says_so(tmp_path: Path) -> None:
    plan = plan_launch(_effective(_instruction("style", "简短。")), adapter=FILE_ADAPTER,
                       instructions_file="NOTES.md", workspace_root=str(tmp_path), project_id=PROJECT,
                       workspace_in_use=True)
    assert plan.instructions_channel == CHANNEL_WORKSPACE_FILE
    assert (tmp_path / "NOTES.md").exists()
    assert [(gap.kind, gap.names, gap.reason) for gap in plan.unapplied] == [
        ("instructions", ("style",), REASON_WORKSPACE_IN_USE)]


def test_file_channel_without_a_workspace_requires_one(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceRequiredError) as caught:
        plan_launch(_effective(_instruction("style", "简短。")), adapter=FILE_ADAPTER,
                    instructions_file="NOTES.md", workspace_root=None, project_id=PROJECT)
    assert caught.value.failure is not None and caught.value.failure.code == "workspace_required"
    # 不该拦住启动的场合只报进 unapplied。
    plan = plan_launch(_effective(_instruction("style", "简短。")), adapter=FILE_ADAPTER,
                       instructions_file="NOTES.md", workspace_root=None, project_id=PROJECT,
                       require_workspace=False)
    assert [gap.reason for gap in plan.unapplied] == ["no_workspace_root"]


def test_launch_channel_needs_no_workspace(tmp_path: Path) -> None:
    plan = plan_launch(_effective(_instruction("style", "简短。")), adapter=LAUNCH_ADAPTER,
                       instructions_file="NOTES.md", workspace_root=None, project_id=PROJECT)
    assert plan.instructions_channel == CHANNEL_LAUNCH


def test_neither_channel_reports_no_convention_and_invalid_rows_are_reported(tmp_path: Path) -> None:
    plan = plan_launch(_effective(_instruction("style", "简短。"), _instruction("blank", "  ")),
                       adapter=FILE_ADAPTER, instructions_file=None, workspace_root=str(tmp_path), project_id=PROJECT)
    assert plan.instructions_channel is None
    assert {(gap.names, gap.reason) for gap in plan.unapplied} == {
        (("blank",), "invalid_config"), (("style",), "no_convention")}


def test_shared_workspace_block_is_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "NOTES.md").write_text(
        managed_block.render_block(INSTRUCTIONS_MARKER, "别人的", "project:other") + "\n", encoding="utf-8")
    before = (tmp_path / "NOTES.md").read_bytes()
    plan = plan_launch(_effective(_instruction("style", "简短。")), adapter=FILE_ADAPTER,
                       instructions_file="NOTES.md", workspace_root=str(tmp_path), project_id=PROJECT)
    assert plan.instructions_channel is None
    assert [gap.reason for gap in plan.unapplied] == ["workspace_shared"]
    assert (tmp_path / "NOTES.md").read_bytes() == before


# --------------------------------------------------------------------------- #
# 原生 MCP 排除
# --------------------------------------------------------------------------- #

INVENTORY = (_record("github", "native-mcp-aaa"), _record("docs", "native-mcp-bbb"))
EXCLUDING_ADAPTER = SimpleNamespace(
    exclude_native_mcp=lambda spec, metadata, *, excluded, assigned, cwd: (
        spec.with_env(EXCLUDED=",".join(sorted(r["name"] for r in excluded))), metadata, ("note",)),
)


def test_no_native_inventory_means_nothing_to_isolate(tmp_path: Path) -> None:
    plan = plan_launch(_effective(), adapter=EXCLUDING_ADAPTER, instructions_file=None,
                       workspace_root=str(tmp_path), project_id=PROJECT, native_inventory=())
    assert plan.native_mcp_mode == MCP_NONE and "nativeMcp" not in plan.summary


def test_unmanaged_native_mcp_is_left_alone(tmp_path: Path) -> None:
    plan = plan_launch(_effective(), adapter=EXCLUDING_ADAPTER, instructions_file=None,
                       workspace_root=str(tmp_path), project_id=PROJECT, native_inventory=INVENTORY)
    assert plan.native_mcp_mode == MCP_UNMANAGED and plan.native_excluded == ()
    spec, _metadata, _w = apply_launch_plan(plan, _spec(), {}, adapter=EXCLUDING_ADAPTER, cwd=None)
    assert "EXCLUDED" not in spec.env


def test_unassigned_native_mcp_is_excluded_and_marked_unverified(tmp_path: Path) -> None:
    effective = _effective(native_mcp=(_native("native-mcp-aaa"),))
    plan = plan_launch(effective, adapter=EXCLUDING_ADAPTER, instructions_file=None,
                       workspace_root=str(tmp_path), project_id=PROJECT, native_inventory=INVENTORY)
    assert plan.native_mcp_mode == MCP_UNVERIFIED
    assert plan.excluded_names == ("docs",)
    assert plan.summary == {"nativeMcpExcluded": ["docs"], "nativeMcp": [MCP_UNVERIFIED]}
    assert any("未" in warning and "验证" in warning for warning in plan.warnings)
    spec, _metadata, warnings = apply_launch_plan(plan, _spec(), {}, adapter=EXCLUDING_ADAPTER, cwd=None)
    assert spec.env["EXCLUDED"] == "docs" and warnings == ("note",)


def test_blocked_only_counts_as_managed(tmp_path: Path) -> None:
    effective = _effective(native_mcp_blocked=(
        BlockedCapability(capability_type="fake:mcp", capability_id="native-mcp-aaa", blocked_by_project_id=PROJECT),))
    plan = plan_launch(effective, adapter=EXCLUDING_ADAPTER, instructions_file=None,
                       workspace_root=str(tmp_path), project_id=PROJECT, native_inventory=INVENTORY)
    assert set(plan.excluded_names) == {"github", "docs"}


def test_all_assigned_is_clean(tmp_path: Path) -> None:
    effective = _effective(native_mcp=(_native("native-mcp-aaa"), _native("native-mcp-bbb")))
    plan = plan_launch(effective, adapter=EXCLUDING_ADAPTER, instructions_file=None,
                       workspace_root=str(tmp_path), project_id=PROJECT, native_inventory=INVENTORY)
    assert plan.native_mcp_mode == MCP_CLEAN and plan.summary == {"nativeMcp": [MCP_CLEAN]}


def test_adapter_without_a_mechanism_reports_unisolated(tmp_path: Path) -> None:
    effective = _effective(native_mcp=(_native("native-mcp-aaa"),))
    plan = plan_launch(effective, adapter=FILE_ADAPTER, instructions_file=None,
                       workspace_root=str(tmp_path), project_id=PROJECT, native_inventory=INVENTORY)
    assert plan.native_mcp_mode == MCP_UNISOLATED
    assert [(gap.kind, gap.names, gap.reason) for gap in plan.unapplied] == [
        ("mcp", ("docs",), REASON_NATIVE_NOT_ISOLATED)]


# --------------------------------------------------------------------------- #
# 各适配器的钩子（只验形状，不起进程）
# --------------------------------------------------------------------------- #


def test_claude_appends_the_system_prompt_through_session_meta() -> None:
    spec, metadata = claude.deliver_instructions(
        _spec(), {"_meta": {"claudeCode": {"options": {"plugins": ["x"]}}}}, "## style\n\n简短。")
    options = metadata["_meta"]["claudeCode"]["options"]
    assert options["plugins"] == ["x"]  # 技能投射的键不动
    assert options["systemPrompt"] == {"type": "preset", "preset": "claude_code", "append": "## style\n\n简短。"}
    assert spec.env == {}


def test_claude_strict_mcp_passes_assigned_definitions_and_respects_disabled(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / ".mcp.json").write_text("{}", encoding="utf-8")
    (config_dir / ".claude.json").write_text(json.dumps({
        "mcpServers": {"github": {"type": "stdio", "command": "gh-mcp", "env": {"TOKEN": "synthetic"}},
                       "docs": {"type": "http", "url": "https://docs.example.test"},
                       "off": {"type": "stdio", "command": "off-mcp"}},
        "projects": {str(cwd): {"mcpServers": {"local": {"command": "local-mcp"}},
                                "disabledMcpServers": ["off"]}},
    }), encoding="utf-8")
    spec = _spec(CLAUDE_CONFIG_DIR=str(config_dir))
    assigned = (_record("github", "a"), _record("off", "c"), _record("gone", "d"))
    excluded = (_record("docs", "b"),)
    spec, metadata, warnings = claude.exclude_native_mcp(spec, {}, excluded=excluded, assigned=assigned, cwd=str(cwd))
    options = metadata["_meta"]["claudeCode"]["options"]
    assert options["strictMcpConfig"] is True
    assert set(options["mcpServers"]) == {"github", "local"}  # docs 被排除、off 被用户关掉、gone 已不存在
    assert options["mcpServers"]["github"]["env"] == {"TOKEN": "synthetic"}  # 定义只在内存里传给子进程
    assert any("gone" in w for w in warnings) and any(".mcp.json" in w for w in warnings)
    assert json.loads((config_dir / ".claude.json").read_text())["mcpServers"]["docs"]  # 用户文件没动
    assert spec.env == {"CLAUDE_CONFIG_DIR": str(config_dir)}


def test_codex_developer_instructions_and_enabled_false_keep_other_overrides() -> None:
    spec = _spec(CODEX_CONFIG=json.dumps({"model": "m", "mcp_servers": {"docs": {"startup_timeout_sec": 5}}}))
    spec, _metadata = codex.deliver_instructions(spec, {}, "简短。")
    spec, _metadata, warnings = codex.exclude_native_mcp(
        spec, {}, excluded=(_record("docs", "b"), _record("github", "a")), assigned=(), cwd=None)
    config = json.loads(spec.env["CODEX_CONFIG"])
    assert config["model"] == "m" and config["developer_instructions"] == "简短。"
    assert config["mcp_servers"] == {"docs": {"startup_timeout_sec": 5, "enabled": False},
                                     "github": {"enabled": False}}
    assert warnings == ()


def test_copilot_and_qwen_repeat_the_disable_flag() -> None:
    excluded = (_record("docs", "b"), _record("github", "a"))
    spec, _m, _w = copilot.exclude_native_mcp(_spec(), {}, excluded=excluded, assigned=(), cwd=None)
    assert spec.command == ("agent", "--acp", "--disable-mcp-server=docs", "--disable-mcp-server=github")
    spec, _m, _w = qwen.exclude_native_mcp(_spec(), {}, excluded=excluded, assigned=(), cwd=None)
    assert spec.command == ("agent", "--acp", "--disable-mcp-server=docs", "--disable-mcp-server=github")
    spec, _m, warnings = qwen.exclude_native_mcp(_spec(), {}, excluded=(_record("a,b", "x"),), assigned=(), cwd=None)
    assert spec.command == ("agent", "--acp") and warnings


def test_gemini_allow_list_includes_project_mcp_and_uses_a_sentinel_when_empty() -> None:
    metadata = {"mcpServers": [{"name": "kaus-probe", "command": "x"}]}
    spec, _m, _w = gemini.exclude_native_mcp(_spec(), metadata, excluded=(_record("docs", "b"),),
                                             assigned=(_record("github", "a"),), cwd=None)
    assert spec.command == ("agent", "--acp", "--allowed-mcp-server-names", "github,kaus-probe")
    spec, _m, _w = gemini.exclude_native_mcp(_spec(), {}, excluded=(_record("docs", "b"),), assigned=(), cwd=None)
    assert spec.command[-1] == gemini.ALLOW_NOTHING_SENTINEL


def test_opencode_disables_through_inline_config_content() -> None:
    spec = _spec(OPENCODE_CONFIG_CONTENT=json.dumps({"plugin": ["p"], "mcp": {"docs": {"type": "local", "command": ["d"]}}}))
    spec, _m, _w = opencode.exclude_native_mcp(spec, {}, excluded=(_record("docs", "b"), _record("github", "a")),
                                               assigned=(), cwd=None)
    config = json.loads(spec.env["OPENCODE_CONFIG_CONTENT"])
    assert config["plugin"] == ["p"]
    assert config["mcp"] == {"docs": {"type": "local", "command": ["d"], "enabled": False},
                             "github": {"enabled": False}}


def test_every_exclusion_hook_is_marked_unverified() -> None:
    for module in (claude, codex, copilot, gemini, qwen, opencode):
        assert module.NATIVE_MCP_EXCLUSION_VERIFIED is False
