"""Agent 自己装的插件：导入成初始配置，整理过之后启动时只关掉没分到这里的（假安装目录、无真实 Agent）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from app.capabilities.models import BlockedCapability, EffectiveCapabilities, EffectiveCapability
from drivers.launch_delivery import apply_launch_plan, plan_launch
from drivers.native_plugins import discover_native_plugins
from drivers.skill_execution import claude, codex, gemini
from drivers.tests.test_plugin_packages import claude_plugin

PROJECT = "project:p"


def _projects(tmp_path):
    from app.projects.models import Project
    root = Project.create(slug="default")
    child = Project.create(slug="child", parent_project_id=root.id, workspace_root=str(tmp_path / "workspace"))
    return root, child


def _claude_home(home: Path, workspace: Path) -> Path:
    cache = home / ".claude/plugins/cache/team-market"
    installs = {}
    for name, scope in (("review-kit", "user"), ("lint-kit", "project"), ("muted-kit", "user")):
        package = claude_plugin(cache / name / "1.0.0", name=name)
        installs[f"{name}@team-market"] = [{"scope": scope, "installPath": str(package), "projectPath": str(workspace)}]
    claude_plugin(cache / "cached-only/1.0.0", name="cached-only")  # 缓存里有、没登记安装：不算
    (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": installs}))
    (home / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {"muted-kit@team-market": False}}))
    return home


def test_claude_installs_follow_its_install_record_and_scope(tmp_path):
    root, child = _projects(tmp_path)
    records = discover_native_plugins((root, child), home=_claude_home(tmp_path, Path(child.workspace_root)), env={})
    by_ref = {row["pluginRef"]: row for row in records}
    assert set(by_ref) == {"review-kit@team-market", "lint-kit@team-market"}
    assert by_ref["review-kit@team-market"]["defaultProjectIds"] == [root.id]
    assert by_ref["lint-kit@team-market"]["defaultProjectIds"] == [child.id]  # 项目级安装归那个工作目录
    assert by_ref["review-kit@team-market"]["capabilityType"] == "claude-code:plugins"
    assert "docs" in by_ref["review-kit@team-market"]["mcpServers"]
    again = discover_native_plugins((root, child), home=tmp_path, env={})
    assert {row["id"] for row in again} == {row["id"] for row in records}  # id 稳定
    assert discover_native_plugins((root, child), home=tmp_path, env={}, agents=("gemini",)) == []


def test_gemini_extensions_and_agents_without_a_source(tmp_path):
    root, child = _projects(tmp_path)
    ext = tmp_path / ".gemini/extensions/notes"
    ext.mkdir(parents=True)
    (ext / "gemini-extension.json").write_text(json.dumps({"name": "notes", "version": "1"}))
    rows = discover_native_plugins((root, child), home=tmp_path, env={})
    assert [(r["name"], r["capabilityType"], r["defaultProjectIds"]) for r in rows] == [("notes", "gemini:plugins", [root.id])]
    assert discover_native_plugins((root, child), home=tmp_path, env={}, agents=("opencode",)) == []


def _row(record_id, name, ref, servers=None):
    return {"capabilityId": record_id, "name": name, "pluginRef": ref, "mcpServers": servers or {}}


INVENTORY = (_row("native-plugin-a", "review-kit", "review-kit@m", {"docs": {"command": "docs"}}),
             _row("native-plugin-b", "lint-kit", "lint-kit@m"),
             _row("native-plugin-c", "fresh-kit", "fresh-kit@m"))


def _assigned(record_id):
    return EffectiveCapability(capability_type="fake:plugins", capability_id=record_id, config={},
                               source_project_id=PROJECT, inherited=False)


def _effective(**fields):
    return EffectiveCapabilities(project_id=PROJECT, entries=(), **fields)


class _Spec(SimpleNamespace):
    def model_copy(self, *, update):
        return _Spec(**{**self.__dict__, **update})


def _plan(effective, adapter):
    return plan_launch(effective, adapter=adapter, instructions_file=None, workspace_root=None,
                       project_id=PROJECT, native_plugin_inventory=INVENTORY, require_workspace=False)


def test_untouched_tree_changes_nothing():
    calls = []
    adapter = SimpleNamespace(exclude_native_plugins=lambda spec, meta, **kw: calls.append(kw) or (spec, meta, ()))
    plan = _plan(_effective(), adapter)
    assert plan.native_plugins_excluded == () and len(plan.native_plugins_kept) == 3
    assert "nativePluginsExcluded" not in plan.summary


def test_arranged_tree_turns_off_known_unassigned_plugins_and_keeps_new_ones():
    seen = {}
    def hook(spec, meta, *, excluded, kept):
        seen["excluded"], seen["kept"] = [r["name"] for r in excluded], [r["name"] for r in kept]
        return spec, meta, ("note",)
    adapter = SimpleNamespace(exclude_native_plugins=hook)
    effective = _effective(native_plugins=(_assigned("native-plugin-a"),),
                           native_plugins_known=("native-plugin-a", "native-plugin-b"))
    plan = _plan(effective, adapter)
    assert plan.summary["nativePluginsExcluded"] == ["lint-kit"]
    _spec, _meta, warnings = apply_launch_plan(plan, _Spec(command=("x",)), {}, adapter=adapter, cwd=None)
    assert seen == {"excluded": ["lint-kit"], "kept": ["review-kit", "fresh-kit"]} and "note" in warnings


def test_blocked_only_counts_as_arranged_and_missing_hook_is_reported():
    effective = _effective(native_plugins_blocked=(
        BlockedCapability(capability_type="fake:plugins", capability_id="native-plugin-b", blocked_by_project_id=PROJECT),))
    plan = _plan(effective, SimpleNamespace())
    assert set(plan.summary["nativePluginsUnisolated"]) == {"review-kit", "lint-kit", "fresh-kit"}
    assert plan.native_plugins_excluded == ()  # 关不掉就照常加载，不假装隔离
    assert [(u.kind, u.reason) for u in plan.unapplied] == [("plugins", "native_not_isolated")]


def test_claude_turns_plugins_off_in_the_flag_layer_and_passes_kept_mcp_in_strict_mode():
    metadata = {"_meta": {"claudeCode": {"options": {"strictMcpConfig": True, "mcpServers": {"kaus": {"command": "k"}},
                                                     "settings": {"model": "x"}}}}}
    _spec, out, warnings = claude.exclude_native_plugins(None, metadata, excluded=[INVENTORY[1]], kept=[INVENTORY[0]])
    options = out["_meta"]["claudeCode"]["options"]
    assert options["settings"] == {"model": "x", "enabledPlugins": {"lint-kit@m": False}}
    assert options["mcpServers"] == {"kaus": {"command": "k"}, "review-kit-docs": {"command": "docs"}}
    assert warnings == ()
    _spec, same, warnings = claude.exclude_native_plugins(None, {"_meta": {"claudeCode": {"options": {"settings": "/x.json"}}}},
                                                          excluded=[INVENTORY[1]], kept=[])
    assert warnings and "enabledPlugins" not in json.dumps(same)


def test_gemini_lists_the_extensions_that_stay_on():
    spec = _Spec(command=("gemini", "--acp"))
    out, _meta, _w = gemini.exclude_native_plugins(spec, {}, excluded=[INVENTORY[1]], kept=[INVENTORY[0]])
    assert out.command == ("gemini", "--acp", "--extensions", "review-kit")
    none, _meta, _w = gemini.exclude_native_plugins(spec, {}, excluded=list(INVENTORY), kept=[])
    assert none.command[-1] == gemini.NO_EXTENSIONS
    untouched, _meta, _w = gemini.exclude_native_plugins(spec, {}, excluded=[], kept=list(INVENTORY))
    assert untouched.command == spec.command


def _codex_toml(home: Path) -> Path:
    cache = home / ".codex/plugins/cache/local-market"
    for name in ("pdf", "muted", "cached-only"):
        root = cache / name / "1.0"
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(json.dumps({"name": name, "version": "1.0"}))
    (home / ".codex/config.toml").write_text('[plugins."pdf@local-market"]\nenabled = true\n'
                                             '[plugins."muted@local-market"]\nenabled = false\n')
    return home


def test_codex_installs_are_the_ones_config_toml_enables(tmp_path):
    root, child = _projects(tmp_path)
    rows = discover_native_plugins((root, child), home=_codex_toml(tmp_path), env={})
    assert [(r["pluginRef"], r["capabilityType"]) for r in rows] == [("pdf@local-market", "codex:plugins")]


class _EnvSpec(_Spec):
    def build_env(self):
        return dict(self.env)

    def with_env(self, **values):
        return _EnvSpec(**{**self.__dict__, "env": {**self.env, **values}})


def test_codex_turns_off_each_excluded_plugins_skills_and_mcp(tmp_path):
    package = tmp_path / "cache/m/pdf/1.0"
    (package / ".codex-plugin").mkdir(parents=True)
    (package / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "pdf", "skills": "./skills"}))
    for name in ("read", "fill"):
        (package / "skills" / name).mkdir(parents=True)
        (package / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    kept_path = str((package / "skills/read/SKILL.md").resolve())
    spec = _EnvSpec(env={"CODEX_CONFIG": json.dumps({"skills": {"config": [{"path": kept_path, "enabled": True}]}})})
    row = {**_row("native-plugin-p", "pdf", "pdf@m", {"pdf-tools": {"command": "x"}}), "sourcePath": str(package)}
    out, _meta, _w = codex.exclude_native_plugins(spec, {}, excluded=[row], kept=[INVENTORY[0]])
    config = json.loads(out.env["CODEX_CONFIG"])
    assert config["mcp_servers"] == {"pdf-tools": {"enabled": False}}
    assert {(e["path"].rsplit("/", 2)[-2], e["enabled"]) for e in config["skills"]["config"]} == {("read", True), ("fill", False)}
    untouched, _meta, _w = codex.exclude_native_plugins(spec, {}, excluded=[], kept=list(INVENTORY))
    assert untouched.env == spec.env


def test_plugin_is_one_package_in_the_tree_and_its_skills_follow_it(tmp_path):
    import asyncio
    from app.capabilities.effective import resolve_effective_for_binding
    from app.capabilities.skill_tree import SkillTreeService
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.projects.models import AgentBinding, Backend
    from drivers.skill_execution.inventory import additional_roots
    from drivers.skill_inventory import discover_skills
    root, child = _projects(tmp_path)
    package = tmp_path / ".codex/plugins/cache/m/pdf/1.0"
    (package / ".codex-plugin").mkdir(parents=True)
    (package / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "pdf", "skills": "./skills"}))
    (package / "skills/read").mkdir(parents=True)
    (package / "skills/read/SKILL.md").write_text("---\nname: read\n---\n")
    (tmp_path / ".codex/config.toml").write_text('[plugins."pdf@m"]\nenabled = true\n')
    (tmp_path / ".agents/skills/notes").mkdir(parents=True)
    (tmp_path / ".agents/skills/notes/SKILL.md").write_text("---\nname: notes\n---\n")
    projects = (root, child)
    with SqliteUnitOfWork(tmp_path / "domain.db") as uow:
        for project in projects:
            asyncio.run(uow.repositories.projects.save(project))
        asyncio.run(uow.repositories.backends.save(Backend.create(key="codex", display_name="Codex", driver_kind="acp")))
        tree = SkillTreeService(uow.database, discover=lambda: [
            *discover_skills(additional_roots(projects, home=tmp_path, env={})),
            *discover_native_plugins(projects, home=tmp_path, env={})])
        view = tree.snapshot()
        assert sorted(s["name"] for s in view["skills"]) == ["notes", "pdf"]  # 插件里的 read 不单列
        plugin = next(s for s in view["skills"] if s["name"] == "pdf")
        assert plugin["kind"] == "plugin"
        tree.act({"revision": view["revision"], "action": "block", "skillId": plugin["id"], "targetProjectId": child.id})

        def selected(project):
            binding = asyncio.run(uow.repositories.bindings.save(AgentBinding.create(
                project=project, backend="codex", runtime_config={"skill_mode": "managed"})))
            effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
            return sorted(a["name"] for a in effective.get("skill-selection", "default").config["assets"])
        assert selected(root) == ["notes", "pdf:read"]
        assert selected(child) == ["notes"]  # 插件被屏蔽，它的技能也不在范围里


def test_tree_managed_agent_turns_off_plugins_not_in_the_tree():
    seen = {}
    adapter = SimpleNamespace(exclude_native_plugins=lambda spec, meta, *, excluded, kept: seen.update(
        excluded=[r["name"] for r in excluded]) or (spec, meta, ()))
    plan = _plan(_effective(tree_managed=True), adapter)
    assert {r["name"] for r in plan.native_plugins_excluded} == {"review-kit", "lint-kit", "fresh-kit"}
