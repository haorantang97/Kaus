"""Temporary policy and protocol fixtures; never launch an installed Agent."""
import json
import os
import sys
import types
import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

from drivers.hermes import skill_launch, skill_runtime_guard
from drivers.base import UnsupportedCapabilityError


def policy(tmp_path):
    home = tmp_path / "private"
    skills = home / "skills" / "selected"
    skills.mkdir(parents=True)
    selected = skills / "SKILL.md"
    selected.write_text("Selected copy")
    source = tmp_path / "native" / "SKILL.md"
    source.parent.mkdir()
    source.write_text("Native original")
    marker = {"bindingId": "binding:test", "projectId": "project:test", "skillPolicyRequired": True}
    (home / ".kaus-scope.json").write_text(json.dumps(marker))
    (home / ".kaus-skills.json").write_text(json.dumps({
        "runtimePolicy": {"version": 1, "bindingId": marker["bindingId"], "projectId": marker["projectId"]},
        "entries": {"one": {"directory": "selected", "sourcePath": str(source), "pluginName": "plugin:allowed"}},
    }))
    return home, source, selected


def upstream(monkeypatch):
    monkeypatch.setenv("HERMES_BUNDLED_SKILLS", "/synthetic-before")
    agent, utils = types.ModuleType("agent"), types.ModuleType("agent.skill_utils")
    utils.get_skill_create_dir = lambda: None
    utils.get_disabled_skill_names = lambda *args, **kwargs: {"selected"}
    utils.get_all_skills_dirs = lambda: [Path("/external")]
    utils.get_project_skills_dirs = lambda: [Path("/workspace")]
    utils.get_scan_ordered_skills_dirs = lambda: [Path("/workspace"), Path("/external")]
    utils.iter_skill_index_files = lambda directory, filename: Path(directory).rglob(filename)
    agent.skill_utils = utils
    prompt = types.ModuleType('agent.prompt_builder')
    def build_prompt(*, skills_dir_override=None):
        return skills_dir_override
    prompt.build_skills_system_prompt = build_prompt
    agent.prompt_builder = prompt
    monkeypatch.setattr(skill_runtime_guard, 'PROMPT_LOADER_HASH',
        hashlib.sha256(inspect.getsource(build_prompt).encode()).hexdigest())
    package, plugins = types.ModuleType("hermes_cli"), types.ModuleType("hermes_cli.plugins")

    @dataclass
    class Registration:
        kind: str
        key: str
        release: object
        active: bool = True
        def dispose(self):
            self.active = False
            self.release()

    class Manager:
        def __init__(self):
            self.entries, self.tools = {}, []
        def find_plugin_skill(self, name):
            return self.entries.get(name)
        def list_plugin_skills(self, namespace):
            return [name.split(":", 1)[1] for name in self.entries if name.startswith(namespace + ":")]
        def list_plugin_skill_metadata(self):
            return [{"name": name} for name in self.entries]

    class Context:
        def __init__(self, manager):
            self.manager = manager
            self.manifest = types.SimpleNamespace(skill_namespace="", name="plugin")
        def register_skill(self, name, path, description="", frontmatter=None):
            self.manager.entries["plugin:" + name] = path
            return Registration("skill", name, lambda: self.manager.entries.pop("plugin:" + name, None))
        def register_tool(self, name):
            self.manager.tools.append(name)

    plugins.PluginRegistration, plugins.PluginContext, plugins.PluginManager = Registration, Context, Manager
    package.plugins = plugins
    alias = types.ModuleType("tools.already_imported")
    tools = types.ModuleType("tools")
    writer = types.ModuleType("tools.skill_manager_tool")
    viewer = types.ModuleType("tools.skills_tool")
    for module in (writer, viewer):
        module._skills_dir = lambda: Path("/native/skills")
    writer._find_skill = lambda name: None
    viewer._is_skill_disabled = lambda *args, **kwargs: True
    tools.skill_manager_tool, tools.skills_tool = writer, viewer
    constants = types.ModuleType("hermes_constants")
    constants.get_bundled_skills_dir = lambda: Path(os.environ["HERMES_BUNDLED_SKILLS"])
    alias.get_all_skills_dirs = utils.get_all_skills_dirs
    for name, module in (("agent", agent), ("agent.skill_utils", utils), ('agent.prompt_builder', prompt), ("hermes_cli", package),
                         ("hermes_cli.plugins", plugins), ("tools.already_imported", alias),
                         ("hermes_constants", constants), ("tools", tools), ("tools.skill_manager_tool", writer), ("tools.skills_tool", viewer)):
        monkeypatch.setitem(sys.modules, name, module)
    return utils, plugins, alias


def test_plugin_skills_are_selected_and_redirected_without_disabling_tools(tmp_path, monkeypatch):
    home, source, selected = policy(tmp_path)
    utils, plugins, alias = upstream(monkeypatch)
    skill_runtime_guard.install(home)
    manager = plugins.PluginManager()
    context = plugins.PluginContext(manager)
    context.register_tool("plugin-tool")
    allowed = context.register_skill("allowed", source)
    denied = context.register_skill("denied", tmp_path / "not-selected.md")
    assert allowed.active and not denied.active
    assert manager.tools == ["plugin-tool"]
    assert manager.find_plugin_skill("plugin:allowed").read_text() == selected.read_text()
    assert manager.find_plugin_skill("plugin:allowed") != selected
    assert manager.find_plugin_skill("plugin:denied") is None
    # A late registry insertion cannot bypass the lookup/list boundary.
    manager.entries["plugin:late"] = source.parent / "late.md"
    assert manager.list_plugin_skills("plugin") == ["allowed"]
    assert manager.list_plugin_skill_metadata() == [{"name": "plugin:allowed"}]
    assert len(alias.get_all_skills_dirs()) == 1
    assert alias.get_all_skills_dirs()[0].is_relative_to(home / ".kaus-skill-sessions")
    assert utils.get_project_skills_dirs() == []
    assert utils.get_disabled_skill_names() == set()
    assert sys.modules["tools.skills_tool"]._is_skill_disabled("selected") is False
    assert not list(utils.iter_skill_index_files(source.parent, "SKILL.md"))
    assert list(utils.iter_skill_index_files(home / "skills", "SKILL.md")) == []  # Plugin assets remain namespaced.
    assert source.read_text() == "Native original"


def test_runtime_skips_symlink_escape_and_keeps_private_created_skills(tmp_path, monkeypatch):
    home, source, _ = policy(tmp_path)
    utils, plugins, _ = upstream(monkeypatch)
    (home / "skills" / "escape").symlink_to(source.parent, target_is_directory=True)
    created = home / "skills" / "created" / "SKILL.md"
    created.parent.mkdir()
    created.write_text("Private evolution")
    with pytest.raises(RuntimeError, match="links"):
        skill_runtime_guard.install(home)
    (home / "skills" / "escape").unlink()
    skill_runtime_guard.install(home)
    skills = utils.get_all_skills_dirs()[0]
    manager = plugins.PluginManager()
    context = plugins.PluginContext(manager)
    context.register_skill("created", created)
    assert manager.find_plugin_skill("plugin:created") is None
    assert list(utils.iter_skill_index_files(skills, "SKILL.md")) == [skills / "created/SKILL.md"]
    assert all(p.resolve().is_relative_to(skills) for p in utils.iter_skill_index_files(skills, "SKILL.md"))


def test_upstream_change_fails_before_partial_patching(tmp_path, monkeypatch):
    home, _, _ = policy(tmp_path)
    utils, plugins, _ = upstream(monkeypatch)
    original = utils.get_all_skills_dirs
    plugins.PluginManager.find_plugin_skill = None
    with pytest.raises(RuntimeError, match="does not support"):
        skill_runtime_guard.install(home)
    assert utils.get_all_skills_dirs is original


def test_missing_manifest_cannot_fall_back_to_unmanaged_launch(tmp_path, monkeypatch):
    home, _, _ = policy(tmp_path)
    monkeypatch.setattr(skill_launch, "hermes_python", lambda _: "/synthetic/python")
    (home / ".kaus-skills.json").unlink()
    command = skill_launch.command_for("hermes", home, ("acp",))
    assert command[0] == "/synthetic/python" and command[-1] == "acp"
    with pytest.raises(FileNotFoundError):
        skill_runtime_guard.install(home)
    monkeypatch.setattr(skill_launch, "hermes_python", lambda _: None)
    with pytest.raises(UnsupportedCapabilityError):
        skill_launch.command_for("hermes", home, ("acp",))


def test_policy_owner_version_and_private_targets_are_required(tmp_path):
    home, source, selected = policy(tmp_path)
    selected.unlink()
    selected.symlink_to(source)
    with pytest.raises(RuntimeError, match="links"):
        skill_runtime_guard.install(home)


def test_group_start_installs_same_policy_and_does_not_swallow_failure(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from drivers.hermes import group_launcher
    home, _, _ = policy(tmp_path)
    calls = []
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setitem(sys.modules, "skill_runtime_guard", SimpleNamespace(install=calls.append))
    group_launcher._install_skill_policy()
    assert calls == [home]
    (home / ".kaus-skills.json").unlink()
    def fail(_):
        raise RuntimeError("missing policy")
    monkeypatch.setitem(sys.modules, "skill_runtime_guard", SimpleNamespace(install=fail))
    with pytest.raises(RuntimeError, match="missing policy"):
        group_launcher._install_skill_policy()
    # Existing native homes never declared managed retain their native behavior.
    (home / ".kaus-scope.json").unlink()
    group_launcher._install_skill_policy()
