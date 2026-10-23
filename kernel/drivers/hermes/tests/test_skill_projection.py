"""Temporary-directory file projection; no real Agent/model is launched."""
import asyncio
import json
from pathlib import Path

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability, ProjectCapability
from app.capabilities.effective import resolve_effective_for_binding
from app.capabilities.skill_tree import SkillTreeService
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Project
from drivers.hermes import config_yaml, project_scope, skill_projection
from drivers.hermes.driver import HermesDriver
from drivers.skill_inventory import NativeSkillRoot, discover_skills
from runtime.capability_matrix import BackendCapabilities


def package(root, name, body="Body", metadata=True):
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text((f"---\nname: {name}\n---\n" if metadata else "") + body)
    (directory / "script.py").write_text("print('synthetic')")
    return path


def selection(binding, assets):
    return EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type="skill-selection", capability_id="default", config={"assets": assets},
        source_project_id=binding.project_id, inherited=False),))


def asset(path, name=None, kind="hermes:skills"):
    return {"id": path.parent.name, "name": name or path.parent.name, "sourcePath": str(path),
            "available": True, "capabilityType": kind, "capabilityId": path.parent.name}


def setup(tmp_path):
    root = tmp_path / "native"
    root.mkdir()
    (root / "config.yaml").write_text("skills:\n  external_dirs: [/native/extra]\n  trusted_project_dirs: [/native/project]\n  disabled: [one]\n  create_dir: /native/write\n")
    first = package(root / "skills", "one")
    second = package(root / "skills", "two", metadata=False)
    binding = AgentBinding.create(project="work", backend="hermes", runtime_config={"project_execution": "isolated"})
    home = project_scope.prepare(root, root, binding)
    return root, binding, home, first, second


def test_private_copy_selection_dry_run_no_native_mutation(tmp_path):
    root, binding, home, first, second = setup(tmp_path)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".kaus-scopes" not in p.parts}
    effective = selection(binding, [asset(second)])
    planned = skill_projection.project_skills(home=home, binding=binding, effective=effective, dry_run=True)
    assert planned.dry_run and (home / "skills").is_symlink()
    result = skill_projection.project_skills(home=home, binding=binding, effective=effective, dry_run=False)
    assert not result.unsupported and not (home / "skills").is_symlink()
    files = list((home / "skills").rglob("SKILL.md"))
    assert len(files) == 1 and "name: \"two\"" in files[0].read_text()
    files[0].write_text("Private evolution")
    assert second.read_text() == "Body"
    config = config_yaml.load((home / "config.yaml").read_text())["skills"]
    assert config["external_dirs"] == [] and not config["project_discovery"]
    assert config["create_dir"] == str(home / "skills") and "disabled" not in config
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".kaus-scopes" not in p.parts}
    backup_count = len(list((home / ".kaus-skill-backups").iterdir()))
    repeated = skill_projection.project_skills(home=home, binding=binding, effective=effective, dry_run=False)
    assert all(e.action == "unchanged" for e in repeated.applied)
    assert files[0].read_text() == "Private evolution"
    assert len(list((home / ".kaus-skill-backups").iterdir())) == backup_count


def test_removed_selection_preserves_unmanaged_private_skills_and_backup(tmp_path):
    _, binding, home, first, second = setup(tmp_path)
    skill_projection.project_skills(home=home, binding=binding, effective=selection(binding, [asset(first)]), dry_run=False)
    custom = package(home / "skills", "created-locally")
    result = skill_projection.project_skills(home=home, binding=binding, effective=selection(binding, []), dry_run=False)
    assert custom.exists()
    assert len(list((home / "skills").rglob("SKILL.md"))) == 1
    assert result.backup_path and len(list(Path(result.backup_path).rglob("SKILL.md"))) == 2


def test_foreign_scope_missing_collision_and_nested_parent_are_explicit(tmp_path):
    _, binding, home, first, second = setup(tmp_path)
    parent = package(tmp_path / "packages", "bundle")
    child = package(parent.parent / "modules", "child")
    effective = selection(binding, [asset(first, kind="codex:skills"), asset(second), asset(parent),
                                    asset(child), {**asset(second), "id": "duplicate", "sourcePath": "/missing/SKILL.md"}])
    result = skill_projection.project_skills(home=home, binding=binding, effective=effective, dry_run=False)
    assert len(result.unsupported) == 3
    assert [e.after for e in result.applied if e.capability_type != "skill-selection"] == ["two", "child"]
    assert len(list((home / "skills").rglob("SKILL.md"))) == 2
    collision = selection(binding, [asset(first, name="duplicate"), asset(second, name="duplicate")])
    result = skill_projection.project_skills(home=home, binding=binding, effective=collision, dry_run=False)
    assert len(result.unsupported) == 2
    assert not list((home / "skills").rglob("SKILL.md"))


def test_manifest_failure_rolls_back_directory_config_and_manifest(tmp_path, monkeypatch):
    _, binding, home, first, second = setup(tmp_path)
    skill_projection.project_skills(home=home, binding=binding, effective=selection(binding, [asset(first)]), dry_run=False)
    before = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file() and ".kaus-skill-backups" not in p.parts}
    write = skill_projection.atomic_write_text
    failed = False
    def fail_once(path, text):
        nonlocal failed
        if Path(path).name == skill_projection.MANIFEST and not failed:
            failed = True
            raise OSError("synthetic write failure")
        return write(path, text)
    monkeypatch.setattr(skill_projection, "atomic_write_text", fail_once)
    with pytest.raises(OSError):
        skill_projection.project_skills(home=home, binding=binding, effective=selection(binding, [asset(second)]), dry_run=False)
    after = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file() and ".kaus-skill-backups" not in p.parts}
    assert before == after


def test_unowned_native_directory_rejected(tmp_path):
    root, binding, _, first, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="独立执行目录"):
        skill_projection.project_skills(home=root, binding=binding, effective=selection(binding, [asset(first)]), dry_run=False)


def test_catalogue_assignment_reaches_driver_and_generic_path(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    (native / "config.yaml").write_text("{}\n")
    one = package(native / "skills", "one")
    two = package(native / "skills", "two")
    generic = package(tmp_path / "common", "writing")
    with SqliteUnitOfWork(tmp_path / "domain.sqlite3") as uow:
        for slug, parent in (("default", None), ("work", "project:default")):
            asyncio.run(uow.repositories.projects.save(Project.create(slug=slug, parent_project_id=parent)))
        asyncio.run(uow.repositories.capabilities.save(ProjectCapability.create(project_id="project:work",
            capability_type="skills", capability_id="writing", config={"path": str(generic.parent)})))
        service = SkillTreeService(uow.database, discover=lambda: discover_skills([
            NativeSkillRoot(native / "skills", "hermes", "Hermes", "project:default")]))
        snapshot = service.snapshot()
        skill_id = next(s["id"] for s in snapshot["skills"] if s["name"] == "one")
        service.act({"revision": snapshot["revision"], "action": "block", "skillId": skill_id, "targetProjectId": "project:work"})
        binding = AgentBinding.create(project="work", backend="hermes", runtime_config={"project_execution": "isolated"})
        effective = asyncio.run(resolve_effective_for_binding(uow.repositories, binding))
        driver = HermesDriver(hermes_root=native)
        driver._capabilities = BackendCapabilities()
        result = asyncio.run(driver.materialize_project_capabilities(Project.create(slug="work"), binding, effective, dry_run=False))
        assert not result.unsupported
        home = driver.projection_scope(binding)
        contents = [p.read_text() for p in (home / "skills").rglob("SKILL.md")]
        assert len(contents) == 2 and all("name: one" not in s for s in contents)
        native_binding = binding.evolve(runtime_config={})
        result = asyncio.run(driver.materialize_project_capabilities(Project.create(slug="work"), native_binding, effective, dry_run=True))
        assert result.unsupported[-1].reason == "no_convention"
