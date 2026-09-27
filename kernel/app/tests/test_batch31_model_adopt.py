"""批次三十一（AD-155）：模型快照过期不拦发送，改为**采纳**。

这一份守 Session Host 与保留期这两侧：Driver 发一条 ``kaus/model.adopted``，
Host 就把 Conversation 的模型快照写成事实（并把新的那份塞回 Driver 手里）。
Hermes 侧的形状（事件顺序、什么时候不采纳）在
``drivers/hermes/tests/test_batch31_model_adopt.py``。

隔离：SQLite 建在 ``tmp_path``，Driver 是 MockDriver，不起任何真引擎、
不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from app.conversations.models import Conversation
from app.events.models import PRODUCT_CONTENT_EVENT_NAMES, retention_for
from app.persistence.sqlite import SqliteUnitOfWork
from app.projects.models import AgentBinding, Backend, Project
from drivers.mock.driver import MockDriver
from drivers.registry import BackendDriverRegistry
from runtime.event_envelope import ExtensionEvent
from runtime.event_reducer import (
    MODEL_ADOPTED_NAME,
    MODEL_ADOPTED_NAMESPACE,
    MODEL_ADOPTED_REASON_NOT_SCOPED,
)
from runtime.event_store import EventStore
from runtime.lease_manager import LeaseManager
from runtime.session_host import SessionHost

SLUG = "batch31"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = MockDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
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
            Project.create(slug=SLUG, display_name="批次三十一")
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
        return await self.repositories.conversations.save(
            Conversation.create(
                project_id=self.project_id,
                agent_binding_id=self.binding.id,
                title="会话",
                **fields,
            )
        )

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


def _adopted(to_model: str | None, *, from_model: str | None = "gpt-5.6-sol"):
    return ExtensionEvent(
        namespace=MODEL_ADOPTED_NAMESPACE,
        name=MODEL_ADOPTED_NAME,
        data={
            "from": from_model,
            "to": to_model,
            "reason": MODEL_ADOPTED_REASON_NOT_SCOPED,
        },
    )


async def _adopt(harness: Harness, conversation: Conversation, event) -> Conversation:
    await harness.host.start_runtime(conversation)
    await harness.host.emit_conversation_event(conversation, event)
    stored = await harness.repositories.conversations.get(conversation.id)
    assert stored is not None
    return stored


def test_host_writes_the_adopted_model_into_the_snapshot(harness) -> None:
    """AD-155 的落库那一半：采纳事件到达 → 快照变成引擎实际会用的那个。

    真机形态（``docs/quality/verify-batch30-retest.md`` ①）：物化把
    ``model.default`` 从 ``gpt-5.6-sol`` 改成了 ``gpt-5.6-terra``，旧会话的快照
    还停在前者。采纳之后快照即事实，界面上那句不匹配告警不再出现。
    """
    conversation = asyncio.run(harness.new_conversation(model_id="gpt-5.6-sol"))
    stored = asyncio.run(_adopt(harness, conversation, _adopted("gpt-5.6-terra")))
    assert stored.model_id == "gpt-5.6-terra"


def test_host_ignores_an_adoption_without_a_target(harness) -> None:
    """``to`` 缺席 / 不是字符串：什么都不做，绝不把快照清成 None。

    「不知道要换成什么」和「换成空」是两件事（N §13.1：没有来源就不编）。
    """
    conversation = asyncio.run(harness.new_conversation(model_id="gpt-5.6-sol"))
    stored = asyncio.run(_adopt(harness, conversation, _adopted(None)))
    assert stored.model_id == "gpt-5.6-sol"


def test_adoption_is_idempotent(harness) -> None:
    """同一条采纳重放两次不该产生第二次写（快照已经是这个值）。"""
    conversation = asyncio.run(harness.new_conversation(model_id="gpt-5.6-sol"))
    first = asyncio.run(_adopt(harness, conversation, _adopted("gpt-5.6-terra")))

    async def again() -> Conversation:
        await harness.host.emit_conversation_event(first, _adopted("gpt-5.6-terra"))
        stored = await harness.repositories.conversations.get(first.id)
        assert stored is not None
        return stored

    second = asyncio.run(again())
    assert second.model_id == "gpt-5.6-terra"
    assert second.updated_at == first.updated_at


def test_model_adopted_keeps_the_normal_retention() -> None:
    """保留期与其它 ``kaus/*`` 正文事件同一档（不是 24 小时诊断期）。

    它解释的是「这条会话为什么从某一刻起换了模型」——一天以后重开还得看得见，
    否则时间线上只剩一个无从解释的变化。
    """
    assert MODEL_ADOPTED_NAME in PRODUCT_CONTENT_EVENT_NAMES
    assert retention_for(
        "extension.event",
        namespace=MODEL_ADOPTED_NAMESPACE,
        name=MODEL_ADOPTED_NAME,
    ) == timedelta(days=7)
    # 别家 Backend 的私有扩展事件仍旧走诊断期。
    assert retention_for(
        "extension.event", namespace="hermes", name="whatever"
    ) == timedelta(hours=24)
