"""ACP reports Project capabilities a session will not receive (D-03). No subprocess."""

from __future__ import annotations

from pathlib import Path

from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.acp.driver import AcpDriver
from drivers.acp.presets import AcpPreset, AgentQuirks, WorkspaceConventions
from drivers.acp.testing.fake_acp_agent import fake_agent_spec

INSTRUCTIONS_FILE = "ENGINE-NOTES.md"


def _driver(tmp_path: Path, instructions_file: str | None = INSTRUCTIONS_FILE) -> AcpDriver:
    preset = AcpPreset(id="fake-for-tests", label="Fake", command=("true",),
                       quirks=AgentQuirks(mcp_via_session_new=True),
                       workspace=WorkspaceConventions(instructions_file=instructions_file))
    return AcpDriver(fake_agent_spec("text-stream", cwd=str(tmp_path)), backend_key="acp",
                     default_cwd=str(tmp_path), preset=preset)


def _wire(driver: AcpDriver, workspace: Path) -> tuple[Project, AgentBinding, Conversation]:
    project = Project.create(slug="media", display_name="Media", workspace_root=str(workspace))
    binding = AgentBinding.create(project=project, backend=driver.backend_id, native_scope_ref="scope-primary",
                                  is_default=True, runtime_config={"workspace_root": str(workspace)})
    driver.register_binding(binding)
    return project, binding, Conversation.create(project_id=project.id, agent_binding_id=binding.id, title="t")


def _entry(capability_type: str, capability_id: str, config: dict) -> EffectiveCapability:
    return EffectiveCapability(capability_type=capability_type, capability_id=capability_id, config=config,
                               source_project_id="project:media", inherited=False)


def _effective(*entries: EffectiveCapability) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id="project:media", entries=entries)


INSTRUCTIONS = _entry("instructions", "house-style", {"body": "Be brief."})


async def test_instructions_not_materialized_are_reported(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _, _, conversation = _wire(driver, tmp_path)
    gaps = await driver.unapplied_project_capabilities(conversation, _effective(INSTRUCTIONS))
    assert [(gap.kind, gap.names, gap.reason) for gap in gaps] == [
        ("instructions", ("house-style",), "not_materialized")]
    assert not (tmp_path / INSTRUCTIONS_FILE).exists()  # the check writes nothing


async def test_materialized_instructions_count_as_delivered(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    project, binding, conversation = _wire(driver, tmp_path)
    effective = _effective(INSTRUCTIONS)
    await driver.materialize_project_capabilities(project, binding, effective, dry_run=False)
    assert await driver.unapplied_project_capabilities(conversation, effective) == ()
    # Editing the Project text again makes the file stale.
    changed = _effective(_entry("instructions", "house-style", {"body": "Be thorough."}))
    (gap,) = await driver.unapplied_project_capabilities(conversation, changed)
    assert gap.reason == "not_materialized"


async def test_engine_without_instruction_file_reports_no_convention(tmp_path: Path) -> None:
    driver = _driver(tmp_path, instructions_file=None)
    _, _, conversation = _wire(driver, tmp_path)
    (gap,) = await driver.unapplied_project_capabilities(conversation, _effective(INSTRUCTIONS))
    assert (gap.kind, gap.reason) == ("instructions", "no_convention")


async def test_invalid_instruction_and_skipped_mcp_are_reported(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _, _, conversation = _wire(driver, tmp_path)
    effective = _effective(
        _entry("instructions", "empty", {"body": "  "}),
        _entry("mcp", "files", {"value": {"command": "node"}}),
        _entry("mcp", "broken", {"value": {"args": ["x"]}}),
    )
    gaps = {gap.kind: gap for gap in await driver.unapplied_project_capabilities(conversation, effective)}
    assert (gaps["instructions"].names, gaps["instructions"].reason) == (("empty",), "invalid_config")
    assert (gaps["mcp"].names, gaps["mcp"].reason) == (("broken",), "invalid_config")


async def test_nothing_to_report_without_capabilities(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    _, _, conversation = _wire(driver, tmp_path)
    effective = _effective(_entry("mcp", "files", {"value": {"command": "node"}}))
    assert await driver.unapplied_project_capabilities(conversation, effective) == ()
