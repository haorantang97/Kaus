import json
from pathlib import Path

from app.projects.models import Project
from app.projects.models import AgentBinding
from drivers.skill_execution.inventory import additional_roots, driver_roots
from drivers.skill_inventory import configured_roots, discover_skills


def skill(folder, name="review"):
    path = folder / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\nSynthetic only")
    return path


def manifest(package, agent, data):
    path = package / (".codex-plugin" if agent == "codex" else ".claude-plugin") / "plugin.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def seed(tmp_path):
    root = Project.create(slug="default")
    child = Project.create(slug="child", parent_project_id=root.id, workspace_root=str(tmp_path / "workspace"))
    return [root, child]


def test_codex_only_explicit_enabled_unique_version_and_native_user_source(tmp_path):
    projects = seed(tmp_path)
    codex = tmp_path / ".codex"
    for name in ("enabled", "disabled", "cached-only", "ambiguous"):
        package = codex / "plugins/cache/market" / name / "1"
        manifest(package, "codex", {"name": name, "skills": "./skills"})
        skill(package / "skills")
    package = codex / "plugins/cache/market/ambiguous/2"
    manifest(package, "codex", {"name": "ambiguous", "skills": "./skills"})
    skill(package / "skills")
    # A native latest alias does not manufacture a second distinct version.
    (codex / "plugins/cache/market/enabled/latest").symlink_to("1", target_is_directory=True)
    (codex / "config.toml").write_text('''[plugins."enabled@market"]
enabled=true
[plugins."disabled@market"]
enabled=false
[plugins."ambiguous@market"]
enabled=true
''')
    skill(tmp_path / ".agents/skills", "native-user")
    notes = []
    roots = additional_roots(projects, home=tmp_path, env={}, diagnostics=notes)
    rows = discover_skills(roots)
    assert {r["name"] for r in rows} == {"enabled:review", "native-user"}
    assert all(r["agentKey"] == "codex" for r in rows)
    assert {r["defaultProjectId"] for r in rows} == {projects[0].id}
    assert {n["code"] for n in notes} >= {"codex_plugin_version_ambiguous"}


def test_codex_version_path_and_manifest_paths_are_checked(tmp_path):
    projects = seed(tmp_path)
    codex = tmp_path / ".codex"
    for version in ("1", "2"):
        package = codex / "plugins/cache/market/pinned" / version
        manifest(package, "codex", {"name": "pinned", "skills": ["./skills", "../../../../outside"]})
        skill(package / "skills", "version-" + version)
    custom = tmp_path / "custom"
    manifest(custom, "codex", {"name": "custom", "skills": "./skills"})
    skill(custom / "skills")
    (custom / "__init__.py").write_text("raise RuntimeError('never import plugin')")
    outside = tmp_path / "outside"
    skill(outside)
    (custom / "skills/escape").symlink_to(outside, target_is_directory=True)
    # A declared root escaping the package is rejected, including symlinks.
    (custom / "other").symlink_to(outside, target_is_directory=True)
    manifest(custom, "codex", {"name": "custom", "skills": ["./skills/review", "./other"]})
    (codex / "config.toml").write_text(f'''[plugins."pinned@market"]
enabled=true
version="1"
[plugins."custom@local"]
enabled=true
path={json.dumps(str(custom))}
''')
    rows = discover_skills(additional_roots(projects, home=tmp_path, env={}))
    assert {r["name"] for r in rows} == {"pinned:version-1", "custom:review"}


def test_workspace_sources_stay_with_corresponding_project_and_same_names_do_not_merge(tmp_path):
    projects = seed(tmp_path)
    workspace = Path(projects[1].workspace_root)
    for folder in (tmp_path / ".agents/skills", workspace / ".agents/skills", workspace / ".claude/skills"):
        skill(folder)
    rows = discover_skills(configured_roots(None, projects, [], home=tmp_path, env={}))
    assert len(rows) == 3 and len({r["id"] for r in rows}) == 3
    assert {(r["agentKey"], r["defaultProjectId"]) for r in rows} == {
        ("codex", projects[0].id), ("codex", projects[1].id), ("claude-code", projects[1].id)}


def test_claude_installed_scopes_manifest_paths_and_disabled_state(tmp_path):
    projects = seed(tmp_path)
    claude = tmp_path / ".claude"
    records = {}
    for name, scope in (("user", "user"), ("project", "project"), ("disabled", "user"), ("external", "project")):
        package = claude / "plugins/cache/market" / name / "1"
        manifest(package, "claude-code", {"name": name, "skills": "./extra"})
        skill(package / "skills", "default")
        skill(package / "extra", "extra")
        records[name + "@market"] = [{"scope": scope, "installPath": str(package),
            "projectPath": projects[1].workspace_root if name == "project" else str(tmp_path / "unrelated") }]
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": records}))
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"disabled@market": False}}))
    marketplace = claude / "plugins/marketplaces/uninstalled"
    manifest(marketplace, "claude-code", {"name": "uninstalled"})
    skill(marketplace / "skills")
    rows = discover_skills(additional_roots(projects, home=tmp_path, env={}))
    assert {r["name"] for r in rows} == {"user:default", "user:extra", "project:default", "project:extra"}
    assert {r["defaultProjectId"] for r in rows if r["name"].startswith("project:")} == {projects[1].id}
    assert {r["defaultProjectId"] for r in rows if r["name"].startswith("user:")} == {projects[0].id}


def test_claude_project_disable_cannot_be_seeded_at_ancestor(tmp_path):
    projects = seed(tmp_path)
    claude = tmp_path / ".claude"
    package = claude / "plugins/cache/market/user/1"
    skill(package / "skills")
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "user@market": [{"scope": "user", "installPath": str(package)}]}}))
    settings = Path(projects[1].workspace_root) / ".claude/settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"enabledPlugins": {"user@market": False}}))
    notes = []
    rows = discover_skills(additional_roots(projects, home=tmp_path, env={}, diagnostics=notes))
    assert len(rows) == 1
    assert rows[0]["defaultProjectId"] == projects[0].id
    assert rows[0]["defaultBlockedProjectIds"] == [projects[1].id]
    assert notes == []


def test_read_only_inventory_missing_or_malformed_files_does_not_mutate_sources(tmp_path):
    projects = seed(tmp_path)
    codex, claude = tmp_path / "custom-codex", tmp_path / "custom-claude"
    codex.mkdir()
    claude.mkdir()
    (codex / "config.toml").write_text("not valid toml")
    (claude / "plugins").mkdir()
    (claude / "plugins/installed_plugins.json").write_text("invalid json")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not discover_skills(additional_roots(projects, home=tmp_path,
        env={"CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)}))
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_driver_custom_home_is_honored_and_other_agents_are_excluded(tmp_path):
    from types import SimpleNamespace
    projects = seed(tmp_path)
    codex, claude = tmp_path / "codex-profile", tmp_path / "claude-profile"
    skill(codex / "skills", "codex-native")
    skill(claude / "skills", "claude-native")
    package = codex / "plugins/cache/market/custom/1"
    manifest(package, "codex", {"name": "custom", "skills": "./skills"})
    skill(package / "skills")
    (codex / "config.toml").write_text('[plugins."custom@market"]\nenabled=true\n')
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)}
    spec = SimpleNamespace(build_env=lambda: env)
    rows = discover_skills(driver_roots(spec, "codex", projects))
    assert {r["name"] for r in rows} == {"codex-native", "custom:review"}
    assert all(r["agentKey"] == "codex" for r in rows)
    settings = Path(projects[1].workspace_root) / ".codex/config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('[plugins."custom@market"]\nenabled=false\n')
    assert {r["name"] for r in discover_skills(driver_roots(spec, "codex", projects))} == {"codex-native", "custom:review"}
    assert {r["name"] for r in discover_skills(driver_roots(spec, "claude-code", projects))} == {"claude-native"}
    assert driver_roots(spec, "claude", projects) == driver_roots(spec, "claude-code", projects)


def test_binding_workspace_override_is_mapped_only_to_its_project_and_agent(tmp_path):
    projects = seed(tmp_path)
    overrides = tmp_path / "conversation-location"
    skill(overrides / ".agents/skills", "selected-location")
    skill(overrides / ".claude/skills", "different-agent")
    skill(tmp_path / ".agents/skills", "global")
    bindings = [AgentBinding.create(project=projects[1], backend="codex",
        runtime_config={"workspace_root": str(overrides)}),
        AgentBinding.create(project="unregistered", backend="claude-code",
        runtime_config={"workspace_root": str(overrides)})]
    rows = discover_skills(configured_roots(None, projects, bindings, home=tmp_path, env={}))
    assert {r["name"] for r in rows} == {"selected-location", "global"}
    row = next(r for r in rows if r["name"] == "selected-location")
    assert row["defaultProjectIds"] == [projects[1].id] and row["agentKey"] == "codex"


def test_project_plugin_disable_is_a_project_block_and_restores_on_refresh(tmp_path):
    import asyncio
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.capabilities.skill_tree import SkillTreeService
    projects = seed(tmp_path)
    projects.append(Project.create(slug='sibling', parent_project_id=projects[0].id))
    projects.append(Project.create(slug='grandchild', parent_project_id=projects[1].id))
    codex = tmp_path / '.codex'
    plugin = codex / 'plugins/cache/market/shared/1'
    manifest(plugin, 'codex', {'name': 'shared', 'skills': 'skills'})
    skill(plugin / 'skills')
    (codex / 'config.toml').write_text('[plugins."shared@market"]\nenabled=true\n')
    override = Path(projects[1].workspace_root) / '.codex/config.toml'
    override.parent.mkdir(parents=True)
    override.write_text('[plugins."shared@market"]\nenabled=false\n')
    with SqliteUnitOfWork(tmp_path / 'domain.db') as uow:
        for project in projects:
            asyncio.run(uow.repositories.projects.save(project))
        service = SkillTreeService(uow.database, discover=lambda: discover_skills(additional_roots(projects, home=tmp_path, env={})))
        snapshot = service.snapshot()
        ident = next(row['id'] for row in snapshot['skills'] if row['name'] == 'shared:review')
        entries = {row['projectId']: row for row in snapshot['entries'] if row['skillId'] == ident}
        assert not entries[projects[0].id]['blocked'] and not entries[projects[2].id]['blocked']
        assert entries[projects[1].id]['blocked'] and entries[projects[3].id]['blocked']
        assert entries[projects[3].id]['sourceProjectId'] == projects[1].id
        override.write_text('[plugins."shared@market"]\nenabled=true\n')
        assert all(not row['blocked'] for row in service.snapshot()['entries'] if row['skillId'] == ident)
        # Explicit user blocking is not removed by an unrelated inventory refresh.
        snapshot = service.snapshot()
        service.act({'revision': snapshot['revision'], 'action': 'block', 'skillId': ident, 'targetProjectId': projects[2].id})
        assert next(row for row in service.snapshot()['entries'] if row['skillId'] == ident and row['projectId'] == projects[2].id)['blocked']


def test_user_copy_then_block_is_not_cleared_by_native_policy_refresh(tmp_path):
    import asyncio
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.capabilities.skill_tree import SkillTreeService
    projects = seed(tmp_path)
    codex = tmp_path / '.codex'
    plugin = codex / 'plugins/cache/market/shared/1'
    manifest(plugin, 'codex', {'name': 'shared', 'skills': 'skills'})
    skill(plugin / 'skills')
    (codex / 'config.toml').write_text('[plugins."shared@market"]\nenabled=true\n')
    override = Path(projects[1].workspace_root) / '.codex/config.toml'
    override.parent.mkdir(parents=True)
    override.write_text('[plugins."shared@market"]\nenabled=false\n')
    with SqliteUnitOfWork(tmp_path / 'domain.db') as uow:
        for project in projects:
            asyncio.run(uow.repositories.projects.save(project))
        service = SkillTreeService(uow.database, discover=lambda: discover_skills(additional_roots(projects, home=tmp_path, env={})))
        view = service.snapshot()
        ident = view['skills'][0]['id']
        view = service.act({'revision': view['revision'], 'action':'copy', 'skillId':ident,
            'sourceProjectId':projects[0].id, 'targetProjectId':projects[1].id})
        view = service.act({'revision': view['revision'], 'action':'block', 'skillId':ident,
            'targetProjectId':projects[1].id})
        override.write_text('[plugins."shared@market"]\nenabled=true\n')
        rows = service.snapshot()['entries']
        assert next(row for row in rows if row['skillId']==ident and row['projectId']==projects[1].id)['blocked']
