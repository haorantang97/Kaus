"""Session Host 建会话时把项目 MCP 喂给引擎（批次四十三第 2/3 件）。

用户在验收时发现的那件事：项目上定的能力，除了走落盘投影器的那一家，谁都收不到。
管子接了（ACP 的 ``session/new`` 一直带 ``mcpServers``），水龙头没开——Session Host
建会话时只给 ``metadata={"backendId": …}``。

这份测试守住水龙头的三个状态：**不装 = 完全同以前**、**装了 = 送得到**、
**算不出来 = 会话照常起来**。加上一条：送进去的东西回得到 API 上（只有名字）。
"""

from __future__ import annotations

from typing import Any

from app.capabilities.models import (
    EffectiveCapabilities,
    EffectiveCapability,
)
from app.projects.models import AgentBinding

from runtime.tests.test_session_host import World, make_world


def _effective(project_id: str, *pairs: tuple[str, dict[str, Any]]) -> EffectiveCapabilities:
    return EffectiveCapabilities(
        project_id=project_id,
        entries=tuple(
            EffectiveCapability(
                capability_type="mcp",
                capability_id=capability_id,
                config={"value": value},
                source_project_id=project_id,
                inherited=False,
            )
            for capability_id, value in pairs
        ),
    )


def _install(world: World, projector) -> None:
    """把投影器装到已经搭好的 Host 上（构造参数与这里是同一个私有字段）。"""
    world.host._capability_projector = projector  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 三个状态
# --------------------------------------------------------------------------- #


async def test_without_a_projector_nothing_changes() -> None:
    """不装投影器 = 与本批之前一模一样：一个字都不解析、一个字都不送。"""
    world = make_world()
    driver, conversation = await world.add_backend()
    await world.host.start_runtime(conversation)
    assert driver.projected_capabilities == {}
    assert driver.received_session_metadata == {conversation.id: {}}
    assert world.host.session_projection(conversation.id) is None
    await world.host.stop_runtime(conversation.id)


async def test_the_driver_actually_receives_the_project_mcp() -> None:
    """装了就送得到：Driver 那边**记录**到了这条会话的 MCP 条目。"""
    world = make_world()
    driver, conversation = await world.add_backend()
    seen: list[AgentBinding] = []

    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        seen.append(binding)
        return _effective(
            binding.project_id,
            ("files", {"command": "node"}),
            ("git", {"command": "git-mcp"}),
        )

    _install(world, projector)
    await world.host.start_runtime(conversation)

    assert [b.id for b in seen] == [conversation.agent_binding_id]
    assert driver.projected_capabilities[conversation.id] == ("files", "git")
    # 收到不等于落盘：mock 只是记下来（与真实的落盘投影器是两条路）。
    assert conversation.id in driver.received_session_metadata
    await world.host.stop_runtime(conversation.id)


async def test_a_broken_projector_never_blocks_the_conversation() -> None:
    """能力算不出来不该让用户发不出消息——这一轮不带能力，照常开。"""
    world = make_world()
    driver, conversation = await world.add_backend()

    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        raise RuntimeError("能力表读不出来")

    _install(world, projector)
    handle = await world.host.start_runtime(conversation)
    assert handle.conversation_id == conversation.id
    assert world.host.session_projection(conversation.id) is None
    await world.host.stop_runtime(conversation.id)


async def test_an_empty_capability_set_projects_nothing() -> None:
    """一条 MCP 都没定 = 没什么可送，``projection`` 保持 null（不是空壳）。"""
    world = make_world()
    driver, conversation = await world.add_backend()

    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        return _effective(binding.project_id)

    _install(world, projector)
    await world.host.start_runtime(conversation)
    assert driver.projected_capabilities[conversation.id] == ()
    assert world.host.session_projection(conversation.id) is None
    await world.host.stop_runtime(conversation.id)


async def test_the_summary_only_carries_names() -> None:
    """回显那一份**只有名字**：命令行一个字都不许跟出来。"""
    world = make_world()
    driver, conversation = await world.add_backend()

    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        return _effective(binding.project_id, ("files", {"command": "secret-binary"}))

    _install(world, projector)
    await world.host.start_runtime(conversation)
    projection = world.host.session_projection(conversation.id)
    assert projection is not None
    assert projection.summary == {"mcpServers": ["files"]}
    assert "secret-binary" not in repr(projection.summary)
    await world.host.stop_runtime(conversation.id)


async def test_a_driver_without_the_hook_is_simply_skipped() -> None:
    """没实现 ``session_options_for`` 的 Driver = 这台引擎没有这条路，静默跳过。"""
    world = make_world()
    driver, conversation = await world.add_backend()
    called = False

    async def projector(binding: AgentBinding) -> EffectiveCapabilities:
        nonlocal called
        called = True
        return _effective(binding.project_id, ("files", {"command": "node"}))

    _install(world, projector)

    class _NoHook:
        """一台走落盘投影器的引擎：什么都代理给 mock，**只是没有这个钩子**。"""

        driver_kind = "mock"

        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.backend_id = inner.backend_id

        def __getattr__(self, name: str) -> Any:
            if name == "session_options_for":
                raise AttributeError(name)
            return getattr(self._inner, name)

    world.registry.register(_NoHook(driver), replace=True)
    await world.host.start_runtime(conversation)
    assert called is False
    assert world.host.session_projection(conversation.id) is None
    await world.host.stop_runtime(conversation.id)
