"""Hermes reports Project capabilities a session will not receive (D-03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from drivers.hermes.testing.harness import FakeHermesHarness


@pytest.fixture()
def rig(tmp_path: Path):
    harness = FakeHermesHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _entry(project_id: str, capability_type: str, capability_id: str, config: dict) -> EffectiveCapability:
    return EffectiveCapability(capability_type=capability_type, capability_id=capability_id, config=config,
                               source_project_id=project_id, inherited=False)


async def test_profile_instructions_and_native_skill_scope(rig) -> None:
    driver = rig.make_driver()
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    conversation = Conversation.create(project_id=project.id, agent_binding_id=binding.id, title="t")
    effective = EffectiveCapabilities(project_id=project.id, entries=(
        _entry(project.id, "instructions", "house-style", {"body": "Be brief."}),))

    (gap,) = await driver.unapplied_project_capabilities(conversation, effective)
    assert (gap.kind, gap.names, gap.reason) == ("instructions", ("house-style",), "not_materialized")

    await driver.materialize_project_capabilities(project, binding, effective, dry_run=False)
    assert await driver.unapplied_project_capabilities(conversation, effective) == ()

    managed = EffectiveCapabilities(project_id=project.id, entries=(
        *effective.entries, _entry(project.id, "skill-selection", "default", {"mode": "project"})))
    (skills,) = await driver.unapplied_project_capabilities(conversation, managed)
    assert (skills.kind, skills.reason) == ("skills", "unsupported")
