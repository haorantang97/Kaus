"""Defenses against the engine's own skill maintenance and legacy host residue.

Everything runs against temporary directories and protocol fakes; no Hermes
process, model call or real ``~/.hermes`` is involved.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.base import UnsupportedCapabilityError
from drivers.hermes import config_yaml, failure_hints, isolated_runtime, project_scope, skill_projection, skill_usage_sync
from drivers.hermes.driver import HermesDriver
from drivers.hermes.projector import project_capabilities
from drivers.workspace_projection import REASON_LINKED, materialize_block
from runtime.capability_matrix import BackendCapabilities
from runtime.event_envelope import EventSource, RunCompleted, make_envelope

OLD = "2026-01-01T00:00:00+00:00"
NEW = "2026-03-01T12:00:00+00:00"
LEGACY = (project_scope.LEGACY_CONSTITUTION_BEGIN + "\n# 旧宪法\n- 旧规则\n"
          + project_scope.LEGACY_CONSTITUTION_END + "\n\n")


def record(**overrides):
    base = {"created_by": "agent", "use_count": 2, "view_count": 1, "last_used_at": OLD, "last_viewed_at": None,
            "patch_count": 0, "state": "stale", "pinned": False, "created_at": OLD}
    return {**base, **overrides}


def scope_pair(tmp_path, observed, *, source_records=None, manifest_names=("one",)):
    source = tmp_path / "native"
    (source / "skills").mkdir(parents=True)
    if source_records is not None:
        (source / "skills" / ".usage.json").write_text(json.dumps(source_records, indent=2, sort_keys=True))
    scope = tmp_path / "scope"
    (scope / "skills").mkdir(parents=True)
    (scope / ".kaus-scope.json").write_text(json.dumps({"bindingId": "b", "projectId": "p"}))
    entries = {}
    for index, name in enumerate(manifest_names):
        directory = f"kaus-{index}"
        (scope / "skills" / directory).mkdir()
        (scope / "skills" / directory / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody")
        entries[name] = {"directory": directory, "name": name, "sourcePath": str(source / "skills" / name / "SKILL.md")}
    (scope / ".kaus-skills.json").write_text(json.dumps({"entries": entries}))
    (scope / "skills" / ".usage.json").write_text(json.dumps(observed))
    return scope, source


def usage(source):
    return json.loads((source / "skills" / ".usage.json").read_text())


def test_usage_merge_raises_existing_records_only_and_is_idempotent(tmp_path):
    scope, source = scope_pair(tmp_path, {
        "one": {"use_count": 5, "view_count": 3, "last_used_at": NEW, "last_viewed_at": NEW, "state": "active",
                "created_by": None, "pinned": True},
        "ghost": {"use_count": 9, "last_used_at": NEW},
    }, source_records={"one": record(), "other": record(use_count=7)}, manifest_names=("one", "ghost"))
    report = skill_usage_sync.merge_usage(scope, source)
    assert report.merged == ("one",) and report.written
    merged = usage(source)
    assert merged["one"]["use_count"] == 7 and merged["one"]["view_count"] == 4
    assert merged["one"]["last_used_at"] == NEW and merged["one"]["last_viewed_at"] == NEW
    # Lifecycle and provenance fields belong to the engine; the copy's values never travel.
    assert merged["one"]["state"] == "stale" and merged["one"]["created_by"] == "agent" and merged["one"]["pinned"] is False
    assert merged["other"] == record(use_count=7)
    assert "ghost" not in merged
    before = (source / "skills" / ".usage.json").read_bytes()
    assert not skill_usage_sync.merge_usage(scope, source).written
    assert (source / "skills" / ".usage.json").read_bytes() == before
    # Further use in the copy reports only the new delta.
    (scope / "skills" / ".usage.json").write_text(json.dumps({"one": {"use_count": 6, "last_used_at": OLD}}))
    skill_usage_sync.merge_usage(scope, source)
    assert usage(source)["one"]["use_count"] == 8 and usage(source)["one"]["last_used_at"] == NEW


def test_usage_merge_never_creates_the_source_file_or_touches_native_mode_homes(tmp_path):
    scope, source = scope_pair(tmp_path, {"one": {"use_count": 1, "last_used_at": NEW}})
    assert skill_usage_sync.merge_usage(scope, source) == skill_usage_sync.UsageMergeReport()
    assert not (source / "skills" / ".usage.json").exists()
    assert sorted(p.name for p in source.rglob("*") if p.name.startswith(".usage")) == []
    # Native-mode homes link the source skills; Hermes already records there.
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".kaus-scope.json").write_text("{}")
    (linked / "skills").symlink_to(source / "skills", target_is_directory=True)
    (source / "skills" / ".usage.json").write_text(json.dumps({"one": record()}))
    assert skill_usage_sync.merge_usage(linked, source) == skill_usage_sync.UsageMergeReport()
    assert usage(source)["one"] == record()


def test_usage_merge_is_serialized_across_concurrent_writers(tmp_path):
    scope, source = scope_pair(tmp_path, {"one": {"use_count": 5, "last_used_at": NEW}},
                               source_records={"one": record()})
    failures = []

    def run():
        try:
            skill_usage_sync.merge_usage(scope, source)
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            failures.append(exc)

    workers = [threading.Thread(target=run) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert not failures
    assert usage(source)["one"]["use_count"] == 7
    assert not [p for p in (source / "skills").iterdir() if p.name.startswith(".usage_")]


def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(isolated_runtime, "available", lambda _: True)
    root = tmp_path / "native"
    root.mkdir()
    (root / "config.yaml").write_text("model:\n  default: global-model\ncurator:\n  interval_hours: 48\n")
    driver = HermesDriver(hermes_root=root)
    driver._capabilities = BackendCapabilities()
    return root, driver


def test_driver_mirrors_usage_on_run_end_and_runtime_stop(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    binding = driver.prepare_project_binding(AgentBinding.create(project="one", backend="hermes", origin="domain"))
    driver.register_binding(binding)
    calls = []
    monkeypatch.setattr(skill_usage_sync, "merge_usage", lambda scope, source: calls.append((scope, source)))
    terminal = make_envelope(event=RunCompleted(run_id="r"), project_id=binding.project_id, conversation_id="c",
                             agent_binding_id=binding.id, backend_id="hermes", sequence=1, run_id="r",
                             source=EventSource(driver_kind="native", driver_version="0"))

    class Child:
        async def events(self, handle):
            yield terminal

        async def stop_runtime(self, handle):
            calls.append("stopped")

    handle = SimpleNamespace(runtime_id="isolated-x")
    driver._isolated_runtimes[handle.runtime_id] = (Child(), handle)
    driver._isolated_bindings[handle.runtime_id] = binding

    async def scenario():
        events = [event async for event in driver.events(handle)]
        assert events == [terminal]
        await driver.stop_runtime(handle)

    asyncio.run(scenario())
    expected = (project_scope.home_for(root, binding), root)
    assert calls == [expected, "stopped", expected]
    assert handle.runtime_id not in driver._isolated_bindings


def package(root, name):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody")
    return directory / "SKILL.md"


def selection(binding, assets):
    return EffectiveCapabilities(project_id=binding.project_id, entries=(EffectiveCapability(
        capability_type="skill-selection", capability_id="default", config={"assets": assets},
        source_project_id=binding.project_id, inherited=False),))


def asset(path):
    return {"id": path.parent.name, "name": path.parent.name, "sourcePath": str(path), "available": True,
            "capabilityType": "hermes:skills", "capabilityId": path.parent.name}


def test_archived_source_skill_is_a_coded_start_refusal(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    skill = package(root / "skills", "one")
    shutil.move(str(skill.parent), str(root / "skills" / ".archive" / "one"))
    project = Project.create(slug="one")
    binding = driver.prepare_project_binding(AgentBinding.create(project=project, backend="hermes", origin="domain"))
    driver.register_binding(binding)
    effective = selection(binding, [asset(skill)])
    assert skill_projection.archived_skills(effective) == ("one",)
    home = project_scope.prepare(root, root, binding)
    result = skill_projection.project_skills(home=home, binding=binding, effective=effective, dry_run=True)
    assert "归档" in result.unsupported[0].detail
    conversation = Conversation.create(title="t", project_id=project.id, agent_binding_id=binding.id)
    with pytest.raises(UnsupportedCapabilityError) as caught:
        asyncio.run(driver.session_options_for(conversation, effective))
    failure = caught.value.failure
    assert failure.code == failure_hints.SKILL_ARCHIVED_BY_ENGINE == "skill_archived_by_engine"
    assert "one" in failure.message and "hermes curator restore one" in failure.hint
    # A plainly missing source is still the generic refusal.
    shutil.rmtree(root / "skills" / ".archive")
    assert skill_projection.archived_skills(effective) == ()
    with pytest.raises(UnsupportedCapabilityError) as plain:
        asyncio.run(driver.session_options_for(conversation, effective))
    assert plain.value.failure is None


def test_archive_lookup_accepts_stamped_and_renamed_directories(tmp_path):
    skills = tmp_path / "skills"
    package(skills / "nested" / "deep", "tool")
    (skills / ".archive").mkdir()
    shutil.move(str(skills / "nested" / "deep" / "tool"), str(skills / ".archive" / "tool-20260101120000"))
    assert skill_projection.engine_archive_of(skills / "nested" / "deep" / "tool" / "SKILL.md", "tool") == (
        skills / ".archive" / "tool-20260101120000")
    assert skill_projection.engine_archive_of(skills / "nested" / "deep" / "none", "other") is None
    package(skills / "present", "live")
    assert skill_projection.engine_archive_of(skills / "present" / "live" / "SKILL.md", "live") is None


def test_scope_config_disables_the_native_curator_and_source_stays_untouched(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    source_before = (root / "config.yaml").read_bytes()
    binding = driver.prepare_project_binding(AgentBinding.create(project="one", backend="hermes", origin="domain"))
    home = project_scope.prepare(root, root, binding)
    values = config_yaml.load((home / "config.yaml").read_text())
    assert values["curator"] == {"interval_hours": 48, "enabled": False}
    assert values["model"] == {"default": "global-model"}
    assert (root / "config.yaml").read_bytes() == source_before
    # Refreshing keeps the key even when the source gains an explicit opt-in.
    (root / "config.yaml").write_text("curator:\n  enabled: true\n")
    project_scope.prepare(root, root, binding, refresh=True)
    assert config_yaml.load((home / "config.yaml").read_text())["curator"] == {"enabled": False}


def test_legacy_profile_projection_never_writes_the_curator_key(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  default: m\n")
    effective = EffectiveCapabilities(project_id="project:p", entries=(EffectiveCapability(
        capability_type="hermes:runtime-config", capability_id="toolsets", config={"value": ["web"]},
        source_project_id="project:p", inherited=False),))
    project_capabilities(binding_id="b", hermes_home=home, effective=effective, capabilities=BackendCapabilities(),
                         project_id="project:p", dry_run=False)
    assert "curator" not in config_yaml.load((home / "config.yaml").read_text())


def test_legacy_constitution_block_is_stripped_from_the_private_persona(tmp_path, monkeypatch):
    root, driver = seeded(tmp_path, monkeypatch)
    persona = "# 人格\n\n我是测试人格。\n"
    (root / "SOUL.md").write_text(LEGACY + persona + LEGACY + "尾注\n")
    binding = AgentBinding.create(project="one", backend="hermes", origin="domain",
                                  runtime_config={"project_execution": "isolated"}, native_scope_ref="persona")
    home = project_scope.prepare(root, root, binding)
    assert (home / "SOUL.md").read_text() == persona + "尾注\n"
    assert "⚖" not in (home / "SOUL.md").read_text()
    assert (root / "SOUL.md").read_text().startswith(LEGACY)
    assert project_scope.strip_legacy_constitution("no block") == "no block"
    assert project_scope.strip_legacy_constitution(project_scope.LEGACY_CONSTITUTION_BEGIN + "\nopen") == (
        project_scope.LEGACY_CONSTITUTION_BEGIN + "\nopen")


def test_linked_config_is_reported_as_a_conflict_and_left_in_place(tmp_path):
    original = tmp_path / "shared" / "config.yaml"
    original.parent.mkdir()
    original.write_text("model:\n  default: m\n")
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").symlink_to(original)
    effective = EffectiveCapabilities(project_id="project:p", entries=(EffectiveCapability(
        capability_type="hermes:runtime-config", capability_id="toolsets", config={"value": ["web"]},
        source_project_id="project:p", inherited=False),))
    for dry_run in (True, False):
        result = project_capabilities(binding_id="b", hermes_home=home, effective=effective,
                                      capabilities=BackendCapabilities(), project_id="project:p", dry_run=dry_run)
        assert [(e.capability_id, e.reason) for e in result.unsupported] == [("toolsets", "target_linked")]
        assert not result.applied and result.backup_path is None
    assert (home / "config.yaml").is_symlink()
    assert original.read_text() == "model:\n  default: m\n"
    assert not (home / ".kaus-projected.json").exists()
    assert not list(home.glob("config.yaml.kaus-backup-*"))


def test_linked_instruction_file_is_reported_as_a_conflict_and_left_in_place(tmp_path):
    original = tmp_path / "shared" / "SOUL.md"
    original.parent.mkdir()
    original.write_text("# persona\n")
    home = tmp_path / "profile"
    home.mkdir()
    (home / "SOUL.md").symlink_to(original)
    outcome = materialize_block(rows=[("instructions", "house")], body="rule", workspace_root=str(home),
                                filename="SOUL.md", marker="kaus:instructions", project_id="project:p",
                                dry_run=False, guard=False)
    assert [e.reason for e in outcome.unsupported] == [REASON_LINKED] and not outcome.applied
    assert (home / "SOUL.md").is_symlink() and original.read_text() == "# persona\n"
