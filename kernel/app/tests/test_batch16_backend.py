"""批次十六（接入层 + Session Host）：错误修法 / 中断确认 / 会话级模型。

对应任务书：
- 第 1 件：``runtime_start_failed`` 的 ``detail.hint``（走查 F5）；
- 第 2 件：``interrupt`` 之后等不到确认 → ``diagnostic.notice(level=warn)``
  + 把证据回灌给 Driver（走查 F2）；
- 第 4 件：``PATCH /api/conversations/{id}`` 的合法 / 非法 / 501 三条路径，
  以及「送出去的那一轮用的是快照」。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver 或本文件里的桩，
不碰任何真实引擎、不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import DriverError, FailureHint  # noqa: E402
from drivers.mock.capabilities import DEFAULT_CAPABILITIES  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.capability_matrix import ModelCapabilities  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch16"


class Harness:
    def __init__(self, tmp_path: Path, *, driver=None) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = driver or MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
            # 用例不该为了看一条「没确认」的提示等满 5 秒。
            interrupt_confirm_timeout=0.2,
        )
        self.token = TEST_TOKEN
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: self.token),
                run_id_timeout=5.0,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        await repos.backends.save(
            Backend.create(
                key=self.driver.backend_id.split(":", 1)[1],
                display_name="Mock",
                driver_kind=self.driver.driver_kind,
            )
        )
        project = await repos.projects.save(
            Project.create(slug=SLUG, display_name="批次十六")
        )
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project,
                backend=self.driver.backend_id.split(":", 1)[1],
                display_name="绑定",
                is_default=True,
            )
        )
        self.project_id = project.id

    async def new_conversation(self, **fields) -> Conversation:
        conversation = Conversation.create(
            project_id=self.project_id,
            agent_binding_id=self.binding.id,
            title="会话",
            **fields,
        )
        return await self.repositories.conversations.save(conversation)

    def close(self) -> None:
        asyncio.run(self.host.aclose())
        self.unit_of_work.close()


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    asyncio.run(built.seed())
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def client(harness):
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {harness.token}"}
    ) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 第 1 件：runtime_start_failed 带修法
# --------------------------------------------------------------------------- #


class _BrokenGatewayDriver(MockDriver):
    """一台「网关连不上」的引擎：``start_runtime`` 抛带修法的 DriverError。"""

    async def start_runtime(self, conversation, surface):  # type: ignore[override]
        raise DriverError(
            "连不上网关",
            failure=FailureHint(
                code="gateway_unreachable",
                message="连不上引擎网关（http://127.0.0.1:8642）",
                hint="网关没在运行：在终端把它启动起来，或检查端口",
            ),
        )


def test_runtime_start_failed_carries_the_hint(tmp_path):
    """走查 F5：起不来的错误要说人话 + 给修法，而不是只报异常类型名。"""
    built = Harness(tmp_path, driver=_BrokenGatewayDriver())
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.post(
                f"/api/conversations/{conversation.id}/messages", json={"text": "hi"}
            )
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "runtime_start_failed"
        assert "连不上引擎网关" in error["message"]
        assert error["detail"]["hint"].startswith("网关没在运行")
        assert error["detail"]["cause"] == "gateway_unreachable"
        # 起不来 = 一个字都没发出去 → 前端据此敢把空壳会话收掉（批次十一第 2 件）。
        assert error["emptyConversation"] is True
    finally:
        built.close()


def test_runtime_start_failed_without_a_known_cause_keeps_the_raw_reason(tmp_path):
    """认不出根因时不编修法：退回原始异常文本，detail 里没有 hint。"""

    class _Odd(MockDriver):
        async def start_runtime(self, conversation, surface):  # type: ignore[override]
            raise RuntimeError("说不清哪坏了")

    built = Harness(tmp_path, driver=_Odd())
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.post(
                f"/api/conversations/{conversation.id}/messages", json={"text": "hi"}
            )
        error = response.json()["error"]
        assert "说不清哪坏了" in error["message"]
        assert "hint" not in error.get("detail", {})
    finally:
        built.close()


# --------------------------------------------------------------------------- #
# 第 2 件：中断没确认 → diagnostic.notice
# --------------------------------------------------------------------------- #


class _DeafDriver(MockDriver):
    """点了停止什么都不做的引擎——真机上「回合继续跑」的最小复现。"""

    interrupt_calls = 0
    unconfirmed = 0

    async def interrupt(self, runtime):  # type: ignore[override]
        type(self).interrupt_calls += 1  # 不动剧本：回合仍在跑

    def note_interrupt_unconfirmed(self) -> None:
        type(self).unconfirmed += 1


async def test_interrupt_without_confirmation_emits_a_warning_notice(tmp_path):
    """走查 F2：5 秒（用例里 0.2 秒）内没进终态 → 一条 warn，并回灌给 Driver。"""
    _DeafDriver.interrupt_calls = 0
    _DeafDriver.unconfirmed = 0
    built = Harness(tmp_path, driver=_DeafDriver())
    await built.seed()
    try:
        conversation = await built.new_conversation()
        await built.host.start_runtime(conversation)
        await built.host.send_message(conversation.id, "跑起来")
        await asyncio.sleep(0.05)
        await built.host.interrupt(conversation.id)
        notices = [
            envelope
            for envelope in await built.host.event_store.replay(conversation.id)
            if envelope.event.type == "diagnostic.notice"
        ]
        assert notices, "等不到确认必须留下一条 diagnostic.notice"
        assert notices[-1].event.level == "warn"
        assert "没有确认中断" in notices[-1].event.message
        assert _DeafDriver.interrupt_calls == 1
        assert _DeafDriver.unconfirmed == 1
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


async def test_interrupt_that_converges_says_nothing(tmp_path):
    """引擎确认了就一句话都不多说（Mock 的 interrupt 会发 run.interrupted）。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.new_conversation()
        await built.host.start_runtime(conversation)
        await built.host.send_message(conversation.id, "跑起来")
        await asyncio.sleep(0.05)
        await built.host.interrupt(conversation.id)
        notices = [
            envelope
            for envelope in await built.host.event_store.replay(conversation.id)
            if envelope.event.type == "diagnostic.notice"
            and "没有确认中断" in envelope.event.message
        ]
        assert notices == []
    finally:
        await built.host.aclose()
        built.unit_of_work.close()


# --------------------------------------------------------------------------- #
# 第 4 件：PATCH /api/conversations/{id}
# --------------------------------------------------------------------------- #


def test_patch_conversation_writes_the_snapshot(harness, client):
    """合法：模型在目录里、推理强度在该模型的档位里 → 写快照，不动 Binding。"""
    conversation = asyncio.run(harness.new_conversation())
    response = client.patch(
        f"/api/conversations/{conversation.id}",
        json={"modelId": "mock-large", "reasoningMode": "xhigh"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["conversation"]
    assert payload["modelId"] == "mock-large"
    assert payload["reasoningMode"] == "xhigh"
    assert payload["providerId"] == "mock-provider"
    # AD-12：Binding 的默认值一动不动。
    binding = asyncio.run(harness.repositories.bindings.get(harness.binding.id))
    assert binding.default_model_id is None


def test_patch_conversation_rejects_a_model_outside_the_catalog(harness, client):
    conversation = asyncio.run(harness.new_conversation())
    response = client.patch(
        f"/api/conversations/{conversation.id}", json={"modelId": "not-a-model"}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "model_not_in_catalog"
    assert "mock-large" in error["detail"]["available"]


def test_patch_conversation_rejects_an_unknown_reasoning_level(harness, client):
    """``mock-small`` 没有 xhigh 这一档 → 400，并把可选档位列出来。"""
    conversation = asyncio.run(harness.new_conversation())
    response = client.patch(
        f"/api/conversations/{conversation.id}",
        json={"modelId": "mock-small", "reasoningMode": "xhigh"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "reasoning_mode_not_supported"
    assert error["detail"]["available"] == ["low", "medium", "high"]


def test_patch_conversation_is_501_when_the_engine_cannot_honour_it(tmp_path):
    """引擎不支持会话级模型 → 501 ``conversation_model_unsupported`` + 修法。"""
    driver = MockDriver(
        capabilities=DEFAULT_CAPABILITIES.model_copy(
            update={
                "models": ModelCapabilities(
                    mode="constrained", reasoning=True, providers=True
                )
            }
        )
    )
    built = Harness(tmp_path, driver=driver)
    asyncio.run(built.seed())
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test_client:
            response = test_client.patch(
                f"/api/conversations/{conversation.id}", json={"modelId": "mock-large"}
            )
        assert response.status_code == 501
        error = response.json()["error"]
        assert error["code"] == "conversation_model_unsupported"
        # 缺省 unknown 也是「不支持」这一档（AD-71：不知道就不给控件）。
        assert error["detail"]["conversationScoped"] == "unknown"
        assert error["detail"]["hint"]
    finally:
        built.close()


def test_patch_conversation_rejects_an_empty_patch(harness, client):
    conversation = asyncio.run(harness.new_conversation())
    response = client.patch(f"/api/conversations/{conversation.id}", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_patch"


async def test_send_message_uses_the_conversation_snapshot(tmp_path):
    """v1.0 §7.3：这一轮用的是会话快照，而且 PATCH 之后**当场**生效。"""
    built = Harness(tmp_path)
    await built.seed()
    try:
        conversation = await built.new_conversation(model_id="mock-small")
        handle = await built.host.start_runtime(conversation)
        await built.host.send_message(conversation.id, "第一轮")
        assert built.driver.last_model_id(handle) == "mock-small"
        # 活跃 runtime 上改快照：下一轮必须按新值发，而不是等重开会话。
        built.host.update_conversation(conversation.snapshot_model(model_id="mock-large"))
        # Mock 不允许并发两轮：先把这一轮停掉再发下一轮。
        await built.host.interrupt(conversation.id)
        await built.host.send_message(conversation.id, "第二轮")
        assert built.driver.last_model_id(handle) == "mock-large"
    finally:
        await built.host.aclose()
        built.unit_of_work.close()
