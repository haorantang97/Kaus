"""批次三十七 · R2：同一会话并发启动只许产生一条 runtime。

外部评审的复现（`docs/quality/external-review-2026-09-07.md` R2）：
「是否已有运行实例」的判断发生在多次 ``await`` 之前，两个并发 ``ensure_runtime``
都能通过检查；同一个 Host owner 的 lease 允许续期，所以它也拦不住第二次启动。
结果是 Driver 手上两条实例、Host 只记一条，ACP 场景下落库的原生会话 id 还可能
指向那条 Host 不认识的实例。

本文件三条用例逐条对应裁决里的验收：

1. 延迟 Mock 的两个并发 ``ensure_runtime`` → 一条 runtime，Driver 也只有一条；
2. 假 ACP 同样并发 → 库里的原生会话 id 与 Host 手上那条一致；
3. 启动进行到一半被取消 → Host 与 Driver 两边都不留半成品。

隔离：只用 MockDriver 与仓库自带的假 ACP 子进程，不碰任何真实引擎与凭据。
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from app.conversations.models import Conversation
from app.conversations.repository import InMemoryConversationRepository
from app.events.repository import InMemoryEventStoreRepository
from app.projects.models import AgentBinding, Project
from app.projects.repository import InMemoryAgentBindingRepository
from app.runtimes.repository import InMemoryRuntimeLeaseRepository
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.mock.driver import MockDriver
from drivers.registry import BackendDriverRegistry
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseManager
from runtime.session_host import SessionHost

TIMEOUT = 20.0


class SlowMockDriver(MockDriver):
    """``start_runtime`` 里插一个真实的 ``await``。

    R2 的根因就是「检查」与「登记」之间有别的挂起点；不把那个挂起点造出来，
    两个协程会被事件循环顺序跑完，用例就永远是绿的——那正是这个缺陷此前没被
    任何测试抓到的原因。
    """

    def __init__(self, *, delay: float = 0.05, **kwargs) -> None:
        super().__init__(**kwargs)
        self.delay = delay
        self.start_calls = 0
        self.started = asyncio.Event()

    async def start_runtime(self, conversation, surface="card"):  # noqa: ANN001
        self.start_calls += 1
        self.started.set()
        await asyncio.sleep(self.delay)
        return await super().start_runtime(conversation, surface)


def _make_host(
    *, driver, conversations, bindings
) -> SessionHost:
    registry = BackendDriverRegistry([driver])
    return SessionHost(
        registry=registry,
        bindings=bindings,
        event_store=EventStore(InMemoryEventStoreRepository()),
        lease_manager=LeaseManager(InMemoryRuntimeLeaseRepository()),
        conversations=conversations,
    )


async def _seed(driver, *, slug: str) -> tuple[SessionHost, Conversation, object]:
    conversations = InMemoryConversationRepository()
    bindings = InMemoryAgentBindingRepository()
    project = Project.create(
        slug=slug, display_name=slug, workspace_root=tempfile.gettempdir()
    )
    binding = AgentBinding.create(
        project=project,
        backend=driver.backend_id,
        native_scope_ref=f"scope-{slug}",
        is_default=True,
    )
    await bindings.save(binding)
    register = getattr(driver, "register_binding", None)
    if register is not None:
        register(binding)
    conversation = await conversations.save(
        Conversation.create(
            project_id=project.id, agent_binding_id=binding.id, title=slug
        )
    )
    host = _make_host(driver=driver, conversations=conversations, bindings=bindings)
    return host, conversation, conversations


# --------------------------------------------------------------------------- #
# 1 · 延迟 Mock
# --------------------------------------------------------------------------- #


async def test_concurrent_ensure_runtime_starts_exactly_one_runtime() -> None:
    driver = SlowMockDriver(backend_key="slow")
    host, conversation, _ = await _seed(driver, slug="slow")
    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                host.ensure_runtime(conversation), host.ensure_runtime(conversation)
            ),
            TIMEOUT,
        )
        # 两个调用方拿到的是**同一条** runtime，不是两条各自为政的。
        assert first.runtime_id == second.runtime_id
        assert host.active_conversation_ids() == (conversation.id,)
        assert host.handle_for(conversation.id).runtime_id == first.runtime_id
        # Driver 侧只被拉起一次——这一条才是「不会多出一个引擎进程」的证据。
        assert driver.start_calls == 1
        assert driver.runtime_ids() == (first.runtime_id,)
    finally:
        await host.aclose()


async def test_concurrent_start_runtime_still_rejects_the_second_caller() -> None:
    """``start_runtime`` 的语义没被锁改掉：显式启动第二次仍然是错误。

    锁只保证两个调用不重叠，不保证第二个也算成功——那是 ``ensure_runtime``
    的语义（模块文档口径 1）。
    """
    driver = SlowMockDriver(backend_key="slow2")
    host, conversation, _ = await _seed(driver, slug="slow2")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                host.start_runtime(conversation),
                host.start_runtime(conversation),
                return_exceptions=True,
            ),
            TIMEOUT,
        )
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(failures) == 1
        assert driver.start_calls == 1
        assert len(driver.runtime_ids()) == 1
    finally:
        await host.aclose()


# --------------------------------------------------------------------------- #
# 2 · 假 ACP：落库的原生会话 id 必须是 Host 手上那条
# --------------------------------------------------------------------------- #


async def test_concurrent_start_with_fake_acp_persists_one_native_session_id() -> None:
    workdir = tempfile.gettempdir()
    driver = AcpDriver(
        fake_agent_spec("text-stream", cwd=workdir),
        backend_key="acp-concurrent",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        cancel_grace=1.5,
    )
    host, conversation, conversations = await _seed(driver, slug="acpconc")
    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                host.ensure_runtime(conversation), host.ensure_runtime(conversation)
            ),
            TIMEOUT,
        )
        assert first.runtime_id == second.runtime_id
        assert host.active_conversation_ids() == (conversation.id,)
        live = host.handle_for(conversation.id)
        assert live.native_session_id
        stored = await conversations.get(conversation.id)
        # 评审里最难看的那一格：库里存着 A、Host 手上跑着 B。
        assert stored.native_session_id == live.native_session_id
    finally:
        await host.aclose()


# --------------------------------------------------------------------------- #
# 3 · 中途取消：两边都不留半成品
# --------------------------------------------------------------------------- #


class SlowSaveConversationRepository(InMemoryConversationRepository):
    """落库那一步可以被卡住的会话库。

    要测的是**拿到 handle 之后**才失败的那一段：取消如果发生在 Driver 还没返回
    handle 的时候，旧代码本来就会把 lease 还回去；真正没人管的是「Driver 已经
    开出实例、Host 还没登记完」这个窗口。
    """

    def __init__(self) -> None:
        super().__init__()
        self.slow = False
        self.entered = asyncio.Event()

    async def save(self, conversation):  # noqa: ANN001
        if self.slow:
            self.entered.set()
            await asyncio.sleep(10.0)
        return await super().save(conversation)


async def test_cancelled_start_leaves_nothing_behind() -> None:
    driver = MockDriver(backend_key="cancelled")
    conversations = SlowSaveConversationRepository()
    bindings = InMemoryAgentBindingRepository()
    project = Project.create(
        slug="cancelled", display_name="cancelled", workspace_root=tempfile.gettempdir()
    )
    binding = AgentBinding.create(
        project=project,
        backend=driver.backend_id,
        native_scope_ref="scope-cancelled",
        is_default=True,
    )
    await bindings.save(binding)
    conversation = await conversations.save(
        Conversation.create(
            project_id=project.id, agent_binding_id=binding.id, title="cancelled"
        )
    )
    host = _make_host(driver=driver, conversations=conversations, bindings=bindings)
    conversations.slow = True
    try:
        task = asyncio.create_task(host.ensure_runtime(conversation))
        await asyncio.wait_for(conversations.entered.wait(), TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.active_conversation_ids() == ()
        # Driver 侧那条也被拆掉了：留着它，下一次启动就是第二个引擎进程。
        assert driver.runtime_ids() == ()
        # 写权还回去了，否则这条会话要挂到 lease ttl 过期才能再开。
        owner = await host.describe_runtime_owner(conversation.id)
        assert owner is None or owner.owner_id != host.owner_id
        conversations.slow = False
        stored = await conversations.get(conversation.id)
        assert stored.state != "running-card"
    finally:
        conversations.slow = False
        await host.aclose()
