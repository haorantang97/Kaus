"""钩子（2026-11-11）：定义校验、时点换算、各 Agent 的按次送达、插件里的钩子、Agent 自装钩子的发现。

全部用临时目录与合成数据，不启动任何 Agent。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities.effective import with_hook_definitions, without_native_hooks
from app.capabilities.hook_library import HookLibrary
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.api.api_errors import ApiError
from drivers import hooks as hook_module
from drivers.capability_binding import annotate
from drivers.hooks import HookError, discover_native_hooks, normalize, plugin_hooks, resolve
from drivers.launch_delivery import REASON_NATIVE_UNSUPPORTED, apply_launch_plan, expand_plugins, plan_launch
from drivers.skill_execution import claude, codex, cursor, gemini, qwen

PROJECT = "project:default"
TURN_END = {"name": "notify", "event": "turn_end", "command": "say done", "timeout": 5}
EDIT = {"name": "fmt", "event": "after_file_edit", "command": "make fmt", "timeout": 30}
CLAUDE_ONLY = {"name": "compact", "event": "PreCompact", "agent": "claude-code", "command": "echo c", "timeout": 10}


def _entry(hook, capability_id="hook-aaaaaaaaaaaa"):
    return EffectiveCapability(capability_type="hooks", capability_id=capability_id, config=dict(hook),
                               source_project_id=PROJECT, inherited=False)


def _plan(adapter, *hooks):
    effective = EffectiveCapabilities(project_id=PROJECT, entries=tuple(_entry(h, f"hook-{i:012d}") for i, h in enumerate(hooks)))
    return plan_launch(effective, adapter=adapter, instructions_file=None, workspace_root=None, project_id=PROJECT)


class Spec(SimpleNamespace):
    def build_env(self):
        return dict(self.env)

    def with_env(self, **values):
        return Spec(env={**self.env, **values}, root=self.root)


def test_definitions_are_validated():
    assert normalize({"event": "turn_end", "command": "say done"})["timeout"] == 60
    for bad in ({"event": "turn_end"}, {"event": "turn_end", "command": "x", "timeout": 0},
                {"event": "turn_end", "command": "x", "agent": "claude-code"},
                {"event": "PreCompact", "command": "x"}, {"event": "", "command": "x"}):
        with pytest.raises(HookError):
            normalize(bad)


def test_generic_moments_become_each_agents_event_and_others_are_native_unsupported():
    delivered, unsupported = resolve([TURN_END, EDIT, CLAUDE_ONLY], claude)
    assert [(h["event"], h["matcher"]) for h in delivered] == [
        ("Stop", None), ("PostToolUse", "Edit|Write|MultiEdit|NotebookEdit"), ("PreCompact", None)]
    assert unsupported == []
    delivered, unsupported = resolve([TURN_END, EDIT, CLAUDE_ONLY], codex)
    assert [h["event"] for h in delivered] == ["Stop"] and unsupported == ["fmt"]  # Codex: no file-edit event
    assert [h["event"] for h in resolve([TURN_END, EDIT], gemini)[0]] == ["AfterAgent", "AfterTool"]
    assert resolve([TURN_END], cursor) == ([], ["notify"])  # no per-launch channel
    assert resolve([CLAUDE_ONLY], cursor) == ([], [])       # another Agent's own event: neither sent nor reported


def test_plan_reports_native_unsupported_and_keeps_what_it_delivers():
    plan = _plan(codex, TURN_END, EDIT)
    assert [h["name"] for h in plan.hooks] == ["notify"]
    assert plan.summary["hooks"] == ["notify"] and plan.summary["hooksNative"] == ["unverified"]
    [gap] = plan.unapplied
    assert gap.kind == "hooks" and gap.names == ("fmt",) and gap.reason == REASON_NATIVE_UNSUPPORTED


def test_claude_hooks_ride_the_sdk_settings_layer_with_excluded_plugins():
    metadata = {"_meta": {"claudeCode": {"options": {"settings": {"enabledPlugins": {"p@m": False},
                                                                  "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "old"}]}]}}}}}}
    plan = _plan(claude, TURN_END, EDIT)
    _spec, out, _warnings = apply_launch_plan(plan, object(), metadata, adapter=claude, cwd=None)
    settings = out["_meta"]["claudeCode"]["options"]["settings"]
    assert settings["enabledPlugins"] == {"p@m": False}
    assert [g["hooks"][0]["command"] for g in settings["hooks"]["Stop"]] == ["old", "say done"]
    assert settings["hooks"]["PostToolUse"] == [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                                                "hooks": [{"type": "command", "command": "make fmt", "timeout": 30}]}]


def test_codex_hooks_go_into_the_session_config_layer():
    spec = Spec(env={"CODEX_CONFIG": json.dumps({"memories": {"use_memories": False}})}, root=None)
    spec, _meta, _w = apply_launch_plan(_plan(codex, TURN_END), spec, {}, adapter=codex, cwd=None)
    config = json.loads(spec.env["CODEX_CONFIG"])
    assert config["memories"] == {"use_memories": False}
    assert config["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "say done", "timeout": 5}]}]}


@pytest.mark.parametrize("adapter,variable,scale", [(gemini, "GEMINI_CLI_SYSTEM_SETTINGS_PATH", 1000),
                                                    (qwen, "QWEN_CODE_SYSTEM_SETTINGS_PATH", 1)])
def test_gemini_and_qwen_hooks_merge_onto_the_settings_overlay(tmp_path, monkeypatch, adapter, variable, scale):
    system = tmp_path / "system.json"
    system.write_text(json.dumps({"tools": {"exclude": ["save_memory"]}}))
    monkeypatch.setattr(adapter, "system_settings_path", lambda spec: Path(spec.build_env().get(variable) or system))
    from drivers.skill_execution import settings_overlay
    monkeypatch.setattr(settings_overlay, "state_root", lambda spec: tmp_path / "state")
    spec = Spec(env={}, root=tmp_path)
    spec, _meta, _w = apply_launch_plan(_plan(adapter, TURN_END), spec, {}, adapter=adapter, cwd=None)
    written = json.loads(Path(spec.env[variable]).read_text())
    assert written["tools"] == {"exclude": ["save_memory"]}  # the existing overlay content is kept
    [group] = next(iter(written["hooks"].values()))
    assert group["hooks"][0]["timeout"] == 5 * scale
    assert json.loads(system.read_text()) == {"tools": {"exclude": ["save_memory"]}}  # source untouched


def test_plugin_hooks_are_delivered_like_library_hooks_and_only_the_rest_is_partial(tmp_path):
    package = tmp_path / "pkg"
    (package / ".claude-plugin").mkdir(parents=True)
    (package / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "notifier", "version": "1.0.0"}))
    (package / "hooks").mkdir()
    (package / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/done.sh"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}))
    rows = plugin_hooks(package, "claude", "notifier")
    assert rows[0]["event"] == "turn_end" and rows[0]["command"] == f"{package}/done.sh"
    assert rows[1] == {"name": "notifier · PreToolUse", "event": "PreToolUse", "agent": "claude-code",
                       "matcher": "Bash", "command": "guard", "timeout": 60}
    effective = EffectiveCapabilities(project_id=PROJECT, entries=(EffectiveCapability(
        capability_type="plugins", capability_id="notifier", config={"name": "notifier", "sourcePath": str(package)},
        source_project_id=PROJECT, inherited=False),))
    expansion = expand_plugins(effective, adapter=gemini)
    assert expansion.unapplied == ()  # hooks no longer make the whole plugin "partly unsupported"
    plan = plan_launch(expansion.effective, adapter=gemini, instructions_file=None, workspace_root=None,
                       project_id=PROJECT, plugins=expansion)
    assert [h["event"] for h in plan.hooks] == ["AfterAgent"]  # the Claude-only matcher hook is not for Gemini


def test_native_hooks_are_discovered_read_only_and_bound(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "afplay done.aiff", "timeout": 3}]}]}}
    (claude_home / "settings.json").write_text(json.dumps(settings))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "./n.sh"}]}}))
    records = discover_native_hooks([("claude-code", "Claude Code"), ("cursor", "Cursor"), ("pi", "Pi")], PROJECT,
                                    home=tmp_path, env={})
    by_agent = {r["agentKey"]: r for r in records}
    assert set(by_agent) == {"claude-code", "cursor"}
    assert by_agent["claude-code"]["capabilityType"] == "claude-code:hooks" and by_agent["claude-code"]["timeout"] == 3
    annotated = annotate(records, supported={"claude-code": ("skill", "mcp", "plugin"), "cursor": ("skill",)})
    assert all(r["bound"] and r["boundReason"] == "not_isolable" for r in annotated)
    assert json.loads((claude_home / "settings.json").read_text()) == settings


def test_native_hook_rows_never_reach_delivery_and_library_rows_get_their_definition():
    native = EffectiveCapability(capability_type="claude-code:hooks", capability_id="native-hook-x", config={},
                                 source_project_id=PROJECT, inherited=False)
    library = EffectiveCapability(capability_type="hooks", capability_id="hook-aaaaaaaaaaaa", config={},
                                  source_project_id=PROJECT, inherited=False)
    effective = without_native_hooks(EffectiveCapabilities(project_id=PROJECT, entries=(native, library)))
    catalog = {"hook-aaaaaaaaaaaa": {"capabilityType": "hooks", "capabilityId": "hook-aaaaaaaaaaaa", **TURN_END}}
    [row] = with_hook_definitions(effective, catalog).entries
    assert row.capability_type == "hooks" and row.config["command"] == "say done" and row.config["event"] == "turn_end"


def test_hook_library_adds_lists_and_removes(tmp_path):
    library = HookLibrary(tmp_path)
    added = library.add({"name": "通知", "event": "turn_end", "command": "say done", "timeout": 5})
    assert added["id"].startswith("hook-") and library.records()[0]["command"] == "say done"
    [record] = library.discover()
    assert record["kind"] == "hook" and record["capabilityType"] == "hooks" and record["agentKey"] is None
    with pytest.raises(ApiError):
        library.add({"event": "turn_end"})
    library.remove(added["id"])
    assert library.records() == []
    with pytest.raises(ApiError):
        library.remove(added["id"])


def test_hooks_are_arranged_in_the_tree_without_switching_skill_loading(tmp_path):
    from app.capabilities.skill_tree import SkillTreeService
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.projects.models import Project
    unit = SqliteUnitOfWork(tmp_path / "db.sqlite3")
    try:
        asyncio.run(unit.repositories.projects.save(Project.create(slug="default", display_name="Root")))
        library = HookLibrary(tmp_path / "library")
        hook = library.add(TURN_END)
        tree = SkillTreeService(unit.database, discover=library.discover)
        snapshot = tree.snapshot()
        [skill] = [s for s in snapshot["skills"] if s["id"] == hook["id"]]
        assert skill["kind"] == "hook" and skill["event"] == "turn_end" and skill["command"] == "say done"
        after = tree.act({"revision": snapshot["revision"], "action": "assign", "skillId": hook["id"],
                          "targetProjectId": PROJECT})
        assert any(a["skillId"] == hook["id"] for a in after["assignments"])
        from app.capabilities.skill_tree import MANAGED_PROJECTS_KEY
        with unit.database.transaction() as conn:
            assert not tree._setting(conn, MANAGED_PROJECTS_KEY)  # a hook does not make Skills managed
    finally:
        unit.close()


def test_every_hook_adapter_declares_unverified_delivery():
    for module in (claude, codex, gemini, qwen):
        assert callable(module.deliver_hooks) and module.NATIVE_HOOKS_VERIFIED is False
        assert set(module.HOOK_EVENTS) <= set(hook_module.GENERIC_EVENTS) and module.HOOK_AGENT
    assert not hasattr(cursor, "deliver_hooks")
