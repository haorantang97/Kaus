"""能力库插件的送达：原生格式原样交出，其余拆成技能与 MCP，送不到的部分报未生效（假包、临时目录）。"""
from __future__ import annotations

from types import SimpleNamespace

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from drivers.launch_delivery import (
    PLUGIN_UNVERIFIED, REASON_PLUGIN_PARTS_UNSUPPORTED, REASON_PLUGIN_UNAVAILABLE, expand_plugins,
)
from drivers.skill_execution import claude
from drivers.tests.test_plugin_packages import claude_plugin


def _effective(*rows):
    return EffectiveCapabilities(project_id="project:p", entries=tuple(rows))


def _plugin_row(plugin_id, path, *, available=True):
    return EffectiveCapability(capability_type="plugins", capability_id=plugin_id,
                               config={"sourcePath": str(path), "available": available, "name": plugin_id},
                               source_project_id="project:p", inherited=False)


NATIVE = SimpleNamespace(NATIVE_PLUGIN_FORMATS=("claude",), deliver_plugins=lambda spec, meta, plugins: (spec, meta))
PLAIN = SimpleNamespace()


def test_native_format_is_handed_over_as_is_and_unverified_stays_in_the_summary(tmp_path):
    package = claude_plugin(tmp_path / "review-kit", hooks=True)
    result = expand_plugins(_effective(_plugin_row("review-kit", package)), adapter=NATIVE)
    assert [row["path"] for row in result.native] == [str(package)]
    assert result.summary["pluginNative"] == [PLUGIN_UNVERIFIED] and result.warnings == ()
    assert result.unapplied == ()  # 原生加载时钩子也由 Agent 自己处理
    assert not any(e.capability_type == "mcp" for e in result.effective.entries)


def test_other_agents_get_the_skills_and_mcp_and_hear_about_the_rest(tmp_path):
    package = claude_plugin(tmp_path / "review-kit", hooks=True, commands=True)
    result = expand_plugins(_effective(_plugin_row("review-kit", package)), adapter=PLAIN)
    selection = next(e for e in result.effective.entries if e.capability_type == "skill-selection")
    assert selection.config["mode"] == "managed" and all(a["pluginId"] == "review-kit" for a in selection.config["assets"])
    mcp = [e for e in result.effective.entries if e.capability_type == "mcp"]
    assert mcp and all(e.config["pluginId"] == "review-kit" for e in mcp)
    assert [(u.kind, u.names, u.reason) for u in result.unapplied] == [("plugins", ("review-kit",), REASON_PLUGIN_PARTS_UNSUPPORTED)]
    assert result.summary["pluginsExtracted"] == ["review-kit"]


def test_missing_package_is_reported_and_does_not_block(tmp_path):
    result = expand_plugins(_effective(_plugin_row("gone", tmp_path / "nowhere")), adapter=PLAIN)
    assert [(u.names, u.reason) for u in result.unapplied] == [(("gone",), REASON_PLUGIN_UNAVAILABLE)]
    unavailable = expand_plugins(_effective(_plugin_row("off", claude_plugin(tmp_path / "off"), available=False)), adapter=PLAIN)
    assert unavailable.unapplied[0].reason == REASON_PLUGIN_UNAVAILABLE


def test_agents_without_skill_projection_still_get_mcp(tmp_path):
    package = claude_plugin(tmp_path / "review-kit")
    result = expand_plugins(_effective(_plugin_row("review-kit", package)), adapter=PLAIN, can_project_skills=False)
    assert not any(e.capability_type == "skill-selection" for e in result.effective.entries)
    assert any(e.capability_type == "mcp" for e in result.effective.entries)
    assert result.unapplied[0].reason == "unsupported"


def test_claude_hook_appends_local_plugins_and_widens_the_skill_allowlist():
    spec = SimpleNamespace()
    metadata = {"_meta": {"claudeCode": {"options": {"plugins": [{"type": "local", "path": "/kaus/project"}], "skills": ["kaus-project:a"]}}}}
    plugins = [{"id": "review-kit", "name": "review-kit", "format": "claude", "path": "/lib/review-kit/pkg", "skills": ["review", "lint"]}]
    _spec, out = claude.deliver_plugins(spec, metadata, plugins)
    options = out["_meta"]["claudeCode"]["options"]
    assert options["plugins"] == [{"type": "local", "path": "/kaus/project"}, {"type": "local", "path": "/lib/review-kit/pkg"}]
    assert options["skills"] == ["kaus-project:a", "review-kit:review", "review-kit:lint"]
    _spec, again = claude.deliver_plugins(spec, out, plugins)
    assert again["_meta"]["claudeCode"]["options"]["plugins"] == options["plugins"]  # 不重复加


def _mcp_row(name, definition):
    return EffectiveCapability(capability_type="mcp", capability_id=name, config={"value": definition},
                               source_project_id="project:p", inherited=False)


def _with_mcp(root, name, servers):
    package = claude_plugin(root, name=name)
    (package / ".mcp.json").write_text(__import__("json").dumps({"mcpServers": servers}), encoding="utf-8")
    return package


def test_same_mcp_is_sent_once_and_a_different_one_keeps_both_with_a_prefix(tmp_path):
    github = {"command": "github-mcp", "args": ["--stdio"]}
    a = _with_mcp(tmp_path / "a", "alpha", {"github": github, "notes": {"command": "notes-a"}})
    b = _with_mcp(tmp_path / "b", "beta", {"github": github, "notes": {"command": "notes-b"}})
    result = expand_plugins(_effective(_mcp_row("github", github), _plugin_row("alpha", a), _plugin_row("beta", b)), adapter=PLAIN)
    names = sorted(e.capability_id for e in result.effective.entries if e.capability_type == "mcp")
    # github：项目里那条与两个插件的完全一样，只送一次；notes：两个插件各不相同，第二个加前缀。
    assert names == ["github", "notes", "beta-notes"] or names == sorted(["github", "notes", "beta-notes"])
    assert result.summary["pluginMcpRenamed"] == ["notes → beta-notes"]


def test_project_mcp_with_the_same_name_but_different_command_is_not_overwritten(tmp_path):
    a = _with_mcp(tmp_path / "a", "alpha", {"github": {"command": "other-github"}})
    result = expand_plugins(_effective(_mcp_row("github", {"command": "github-mcp"}), _plugin_row("alpha", a)), adapter=PLAIN)
    rows = {e.capability_id: e.config["value"] for e in result.effective.entries if e.capability_type == "mcp"}
    assert rows["github"] == {"command": "github-mcp"} and rows["alpha-github"]["command"] == "other-github"


def test_claude_passes_plugin_mcp_explicitly_only_in_strict_mode():
    plugins = [{"id": "kit", "name": "kit", "format": "claude", "path": "/lib/kit", "skills": [], "mcpServers": {"docs": {"command": "docs-mcp"}}}]
    relaxed = claude.deliver_plugins(SimpleNamespace(), {}, plugins)[1]["_meta"]["claudeCode"]["options"]
    assert "mcpServers" not in relaxed  # 不在严格模式：插件自己会加载
    strict = {"_meta": {"claudeCode": {"options": {"strictMcpConfig": True, "mcpServers": {"github": {"command": "g"}}}}}}
    options = claude.deliver_plugins(SimpleNamespace(), strict, plugins)[1]["_meta"]["claudeCode"]["options"]
    assert options["mcpServers"] == {"github": {"command": "g"}, "kit-docs": {"command": "docs-mcp"}}


def test_strict_mode_keeps_approved_workspace_mcp_and_names_the_rest(tmp_path):
    import json
    from drivers.tests.test_launch_delivery import _spec
    work = tmp_path / "work"
    work.mkdir()
    (work / ".mcp.json").write_text(json.dumps({"mcpServers": {"db": {"command": "db-mcp"}, "web": {"command": "web-mcp"}, "old": {"command": "x"}}}))
    config = tmp_path / "claude"
    config.mkdir()
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {}, "projects": {str(work): {
        "enabledMcpjsonServers": ["db"], "disabledMcpjsonServers": ["old"]}}}))
    _spec_out, metadata, warnings = claude.exclude_native_mcp(_spec(CLAUDE_CONFIG_DIR=str(config)), {}, excluded=(), assigned=(), cwd=str(work))
    servers = metadata["_meta"]["claudeCode"]["options"]["mcpServers"]
    assert servers == {"db": {"command": "db-mcp"}}
    assert any("web" in w and "批准" in w for w in warnings) and not any("old" in w for w in warnings)
