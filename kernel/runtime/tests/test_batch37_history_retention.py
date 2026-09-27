"""批次三十七 · R4（AD-162）：没有原生历史可回放的引擎，站内正文不再按 TTL 清掉。

外部评审的复现（`docs/quality/external-review-2026-09-07.md` R4）：用假 ACP 跑完
一轮、关掉运行实例、把测试时钟推进八天，原有 9 条事件全被清理，恢复 0 条——
Conversation 与原生会话关联都还在，用户找得到这条会话，却看不见任何内容。
根因是两条规则叠在一起：ACP Driver 明确拒绝 ``load_native_history``（协议上没有
这个方法），而站内事件按「可重建的缓存」七天过期——于是这类引擎的**唯一**历史
来源被当成缓存删掉了。

隔离：只用仓库自带的假 ACP 子进程与 MockDriver，时钟是可推进的假时钟。
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.conversations.models import Conversation
from app.conversations.repository import InMemoryConversationRepository
from app.events.models import RETAIN_UNTIL_CONVERSATION_GONE, is_content_event
from app.events.repository import InMemoryEventStoreRepository
from app.projects.models import AgentBinding, Project
from app.projects.repository import InMemoryAgentBindingRepository
from app.runtimes.repository import InMemoryRuntimeLeaseRepository
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.base import MessageInput
from drivers.mock.driver import MockDriver
from drivers.mock.fixtures import text_stream_script
from drivers.registry import BackendDriverRegistry
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseManager
from runtime.session_host import SessionHost

TIMEOUT = 30.0
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
EIGHT_DAYS = timedelta(days=8)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _World:
    def __init__(self, driver, *, slug: str) -> None:
        self.clock = _Clock()
        self.driver = driver
        self.conversations = InMemoryConversationRepository()
        self.bindings = InMemoryAgentBindingRepository()
        self.event_store = EventStore(
            InMemoryEventStoreRepository(), clock=self.clock
        )
        self.host = SessionHost(
            registry=BackendDriverRegistry([driver]),
            bindings=self.bindings,
            event_store=self.event_store,
            lease_manager=LeaseManager(
                InMemoryRuntimeLeaseRepository(), clock=self.clock
            ),
            conversations=self.conversations,
            clock=self.clock,
        )
        self.slug = slug

    async def seed(self) -> Conversation:
        project = Project.create(
            slug=self.slug,
            display_name=self.slug,
            workspace_root=tempfile.gettempdir(),
        )
        binding = AgentBinding.create(
            project=project,
            backend=self.driver.backend_id,
            native_scope_ref=f"scope-{self.slug}",
            is_default=True,
        )
        await self.bindings.save(binding)
        register = getattr(self.driver, "register_binding", None)
        if register is not None:
            register(binding)
        return await self.conversations.save(
            Conversation.create(
                project_id=project.id,
                agent_binding_id=binding.id,
                title=self.slug,
            )
        )


async def _run_one_turn(world: _World, conversation: Conversation) -> None:
    """跑完一轮，等到本轮进终态为止。"""
    await world.host.ensure_runtime(conversation)
    async with world.host.subscribe(conversation.id) as subscription:
        await world.host.send_message(
            conversation.id, MessageInput(text="你好，这句话一周后还要看得见")
        )
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        async for envelope in subscription:
            if envelope.event.type in (
                "run.completed",
                "run.failed",
                "run.interrupted",
            ):
                break
            if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
                pytest.fail("这一轮没有在超时内收敛")
    await world.host.stop_runtime(conversation.id)


def _acp_driver() -> AcpDriver:
    workdir = tempfile.gettempdir()
    return AcpDriver(
        fake_agent_spec("text-stream", cwd=workdir),
        backend_key="acp-retention",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        cancel_grace=1.5,
    )


# --------------------------------------------------------------------------- #
# 评审的原始复现：八天之后重放仍然是完整的
# --------------------------------------------------------------------------- #


async def test_acp_content_survives_eight_days() -> None:
    world = _World(_acp_driver(), slug="acpret")
    conversation = await world.seed()
    try:
        await _run_one_turn(world, conversation)
        before = await world.event_store.replay(conversation.id)
        assert before, "这一轮本来就该有事件"

        world.clock.advance(EIGHT_DAYS)
        await world.host.purge_expired_events()

        after = await world.event_store.replay(conversation.id)
        content_before = [
            e
            for e in before
            if is_content_event(
                e.event.type,
                namespace=getattr(e.event, "namespace", None),
                name=getattr(e.event, "name", None),
            )
        ]
        content_after = [
            e
            for e in after
            if is_content_event(
                e.event.type,
                namespace=getattr(e.event, "namespace", None),
                name=getattr(e.event, "name", None),
            )
        ]
        assert content_before
        # 评审那一轮这里是 0；正文一条都不能少。
        assert [e.event_id for e in content_after] == [
            e.event_id for e in content_before
        ]
        # 用户说的那句话是重灾区（它借道 extension.event，AD-86）。
        assert any(
            e.event.type == "extension.event" and e.event.name == "user.message"
            for e in content_after
        )
    finally:
        await world.host.aclose()


async def test_acp_diagnostics_still_expire() -> None:
    """这条规则不是「什么都不删了」：诊断照旧按诊断期走。"""
    world = _World(_acp_driver(), slug="acpdiag")
    conversation = await world.seed()
    try:
        await world.host.ensure_runtime(conversation)
        from runtime.event_envelope import DiagnosticNotice

        runtime = world.host._runtimes[conversation.id]  # noqa: SLF001 - 白盒夹具
        await world.host._emit_host_event(  # noqa: SLF001
            runtime,
            DiagnosticNotice(level="warning", message="只是一条诊断"),
            run_id=None,
        )
        await world.host.stop_runtime(conversation.id)

        world.clock.advance(EIGHT_DAYS)
        await world.host.purge_expired_events()
        after = await world.event_store.replay(conversation.id)
        assert not any(e.event.type == "diagnostic.notice" for e in after)
    finally:
        await world.host.aclose()


# --------------------------------------------------------------------------- #
# 能回放原生历史的引擎不受影响
# --------------------------------------------------------------------------- #


async def test_a_backend_with_native_history_keeps_the_old_ttl() -> None:
    """Mock（``sessions.history = supported``，Hermes 同）照旧七天清掉——
    它有第二份账本，站内这份就是缓存。"""
    driver = MockDriver(backend_key="withhistory")
    world = _World(driver, slug="withhistory")
    conversation = await world.seed()
    driver.set_script(conversation.id, text_stream_script())
    try:
        await _run_one_turn(world, conversation)
        assert await world.event_store.replay(conversation.id)

        world.clock.advance(EIGHT_DAYS)
        purged = await world.host.purge_expired_events()
        assert purged > 0
        assert await world.event_store.replay(conversation.id) == ()
    finally:
        await world.host.aclose()


# --------------------------------------------------------------------------- #
# 规则本身
# --------------------------------------------------------------------------- #


def test_retention_is_a_function_of_namespace_and_capability() -> None:
    """正文类的判据只有事件类别，不含任何引擎名字。"""
    assert is_content_event("message.delta")
    assert is_content_event("tool.started")
    assert is_content_event("run.completed")
    assert is_content_event(
        "extension.event", namespace="kaus", name="user.message"
    )
    assert is_content_event(
        "extension.event", namespace="kaus", name="model.adopted"
    )
    # Backend 私有的未知扩展帧不是正文——它正是 v1.0 §8.5 说的短期诊断数据。
    assert not is_content_event(
        "extension.event", namespace="somebackend", name="whatever"
    )
    assert not is_content_event("diagnostic.notice")
    assert not is_content_event("usage.updated")


def test_the_sentinel_is_far_enough_away_to_mean_until_deleted() -> None:
    assert RETAIN_UNTIL_CONVERSATION_GONE.year == 9999
