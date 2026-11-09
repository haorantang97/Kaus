"""Project capabilities that do not reach a session are recorded and shown (D-03)."""

from __future__ import annotations

from typing import Any

from app.api.session_views import session_projection_to_wire
from app.capabilities.models import EffectiveCapabilities, EffectiveCapability
from app.projects.models import AgentBinding
from drivers.base import FailureHint, UnappliedCapability, UnsupportedCapabilityError, WorkspaceRequiredError
from runtime.event_envelope import ExtensionEvent
from runtime.session_host import PROJECT_UNAPPLIED_NAME

from runtime.tests.test_session_host import World, make_world


def _entry(project_id: str, capability_type: str, capability_id: str, config: dict[str, Any]) -> EffectiveCapability:
    return EffectiveCapability(capability_type=capability_type, capability_id=capability_id, config=config,
                               source_project_id=project_id, inherited=False)


def _effective(project_id: str) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id=project_id, entries=(
        _entry(project_id, "instructions", "style", {"body": "Be brief."}),
        _entry(project_id, "mcp", "files", {"value": {"command": "secret-binary"}}),
    ))


def _install(world: World) -> None:
    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        return _effective(binding.project_id)

    world.host._capability_projector = projector  # noqa: SLF001


async def _notices(world: World, conversation_id: str) -> list[dict]:
    return [
        envelope.event.data
        for envelope in await world.event_store.replay(conversation_id)
        if isinstance(envelope.event, ExtensionEvent) and envelope.event.namespace == "kaus"
        and envelope.event.name == PROJECT_UNAPPLIED_NAME
    ]


async def test_projection_failure_is_recorded_and_announced() -> None:
    world = make_world()
    driver, conversation = await world.add_backend()
    _install(world)

    async def refuse(conversation, effective):
        raise UnsupportedCapabilityError("当前引擎不支持项目 MCP")

    driver.session_options_for = refuse
    await world.host.start_runtime(conversation)
    projection = world.host.session_projection(conversation.id)
    assert projection is not None
    assert {(gap.kind, gap.names, gap.reason) for gap in projection.unapplied} == {
        ("instructions", ("style",), "projection_failed"), ("mcp", ("files",), "projection_failed")}
    assert session_projection_to_wire(projection) == {"unapplied": ["instructions", "mcp"]}
    (notice,) = await _notices(world, conversation.id)
    assert notice["projectId"] == conversation.project_id
    assert {item["kind"] for item in notice["items"]} == {"instructions", "mcp"}
    assert "secret-binary" not in repr(notice)
    await world.host.stop_runtime(conversation.id)


async def test_driver_reported_gap_is_merged_and_not_repeated_on_restart() -> None:
    world = make_world()
    driver, conversation = await world.add_backend()
    _install(world)

    async def gaps(conversation, effective, **_kwargs):
        return (UnappliedCapability(kind="instructions", names=("style",), reason="not_materialized"),)

    driver.unapplied_project_capabilities = gaps
    await world.host.start_runtime(conversation)
    projection = world.host.session_projection(conversation.id)
    assert projection.summary == {"mcpServers": ["files"]}  # delivered kinds stay listed
    assert [gap.kind for gap in projection.unapplied] == ["instructions"]
    assert session_projection_to_wire(projection) == {"mcpServers": ["files"], "unapplied": ["instructions"]}
    await world.host.stop_runtime(conversation.id)
    await world.host.start_runtime(conversation)
    notices = await _notices(world, conversation.id)
    assert len(notices) == 1
    assert notices[0]["items"] == [{"kind": "instructions", "names": ["style"], "reason": "not_materialized"}]
    await world.host.stop_runtime(conversation.id)


async def test_gap_without_session_options_is_still_recorded() -> None:
    world = make_world()
    driver, conversation = await world.add_backend()

    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        return EffectiveCapabilities(project_id=binding.project_id, entries=(
            _entry(binding.project_id, "instructions", "style", {"body": "Be brief."}),))

    world.host._capability_projector = projector  # noqa: SLF001

    async def gaps(conversation, effective, **_kwargs):
        return (UnappliedCapability(kind="instructions", names=("style",), reason="no_convention"),)

    driver.unapplied_project_capabilities = gaps
    await world.host.start_runtime(conversation)
    # The driver had nothing to send, so start_runtime got no session options.
    assert driver.received_session_metadata[conversation.id] == {}
    projection = world.host.session_projection(conversation.id)
    assert [gap.reason for gap in projection.unapplied] == ["no_convention"]
    assert len(await _notices(world, conversation.id)) == 1
    await world.host.stop_runtime(conversation.id)


async def test_failing_delivery_check_does_not_block_or_announce() -> None:
    world = make_world()
    driver, conversation = await world.add_backend()
    _install(world)

    async def broken(conversation, effective, **_kwargs):
        raise RuntimeError("check failed")

    driver.unapplied_project_capabilities = broken
    handle = await world.host.start_runtime(conversation)
    assert handle.conversation_id == conversation.id
    assert world.host.session_projection(conversation.id).unapplied == ()
    assert await _notices(world, conversation.id) == []
    await world.host.stop_runtime(conversation.id)


async def test_delivered_capabilities_emit_no_notice() -> None:
    world = make_world()
    driver, conversation = await world.add_backend()
    _install(world)
    await world.host.start_runtime(conversation)
    assert world.host.session_projection(conversation.id).unapplied == ()
    assert await _notices(world, conversation.id) == []
    await world.host.stop_runtime(conversation.id)


async def test_workspace_required_stops_the_start_with_its_code() -> None:
    """项目有要送达的指令却没有工作目录：不开会话，稳定码原样上抛（2026-11-09）。"""
    world = make_world()
    driver, conversation = await world.add_backend()
    _install(world)

    async def refuse(conversation, effective):
        raise WorkspaceRequiredError("先选文件夹", failure=FailureHint(code="workspace_required", message="没有工作目录"))

    driver.session_options_for = refuse
    import pytest
    with pytest.raises(WorkspaceRequiredError) as caught:
        await world.host.start_runtime(conversation)
    assert caught.value.failure.code == "workspace_required"
    assert conversation.id not in world.host._runtimes  # noqa: SLF001
    assert world.host.session_projection(conversation.id) is None
