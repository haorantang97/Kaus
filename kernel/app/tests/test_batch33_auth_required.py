"""批次三十三（AD-157）：接入层把「引擎说你还没登录」说成一句人话。

真机现象：ACP 引擎没登录时，第一句话**失败得毫无痕迹**——页面写「历史没有到达」，
侧栏 ``Failed``，一个字的原因都没有。根因是 Driver 抛出去的那个异常在这一层被
压成了 503 ``runtime_start_failed``（一句「引擎没能起来」），而且这条会话的事件流
里一条终态都没有，时间线因此永远是空的。

本文件守三件事：

1. ``POST /conversations/{id}/messages`` 回 **400 ``auth_required``**，``detail``
   里带 ``hint``（那条终端命令）与 ``authMethods``（引擎自报的登录方式 id）；
2. 同一次失败在会话流里留下一条 ``run.failed{error.code="auth_required"}``——
   时间线要看得见这次失败，而不是一片空白；
3. 组投递把它当成一个**可修的状态**：``reason == "auth_required"``，不是
   ``runtime_start_failed``。

隔离：SQLite 建在 ``tmp_path``，Driver 是本文件里的桩（只把 ``start_runtime``
换成抛异常），不起任何真引擎、不读任何凭据。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.group_views import DELIVERY_REASONS  # noqa: E402
from app.api.session_auth import SessionAuthPolicy  # noqa: E402
from app.api.session_router import build_session_router  # noqa: E402
from app.conversations.models import Conversation  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import AgentBinding, Backend, Project  # noqa: E402
from drivers.base import AuthRequiredError, FailureHint  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.lease_manager import LeaseManager  # noqa: E402
from runtime.session_host import SessionHost  # noqa: E402

TEST_TOKEN = "test-token-0123456789"
SLUG = "batch33"

LOGIN_HINT = "在终端运行 `fake-cli login`，然后重试"
AUTH_METHODS = ("terminal-login", "api-key")


class SignedOutDriver(MockDriver):
    """一台「还没登录」的引擎：``start_runtime`` 一律抛类型化的登录异常。

    只覆盖这一个方法——其余全走 MockDriver，因为本文件测的是**接入层怎么翻译
    这个异常**，不是某个 Driver 怎么认出它（那一半在
    ``drivers/acp/tests/test_batch33_auth_catalog.py``）。
    """

    async def start_runtime(self, conversation, surface="card"):  # noqa: ANN001
        raise AuthRequiredError(
            "Fake Engine 还没有登录",
            failure=FailureHint(
                code="auth_required",
                message="Fake Engine 还没有登录",
                hint=LOGIN_HINT,
            ),
            auth_methods=AUTH_METHODS,
        )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.unit_of_work = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
        self.repositories = self.unit_of_work.repositories
        self.driver = SignedOutDriver()
        self.registry = BackendDriverRegistry([self.driver])
        self.host = SessionHost(
            registry=self.registry,
            bindings=self.repositories.bindings,
            event_store=EventStore(self.repositories.events),
            lease_manager=LeaseManager(self.repositories.leases),
            conversations=self.repositories.conversations,
            idle_timeout=timedelta(minutes=15),
        )
        self.token = TEST_TOKEN
        self.app = fastapi.FastAPI()
        self.app.include_router(
            build_session_router(
                session_host=self.host,
                repositories=self.repositories,
                registry=self.registry,
                auth_policy=SessionAuthPolicy(lambda: self.token),
                run_id_timeout=2.0,
            )
        )

    async def seed(self) -> None:
        repos = self.repositories
        key = self.driver.backend_id.split(":", 1)[1]
        await repos.backends.save(
            Backend.create(key=key, display_name="Fake", driver_kind=self.driver.driver_kind)
        )
        project = await repos.projects.save(
            Project.create(slug=SLUG, display_name="批次三十三")
        )
        self.project_id = project.id
        self.binding = await repos.bindings.save(
            AgentBinding.create(
                project=project, backend=key, display_name="绑定", is_default=True
            )
        )

    async def new_conversation(self) -> Conversation:
        return await self.repositories.conversations.save(
            Conversation.create(
                project_id=self.project_id,
                agent_binding_id=self.binding.id,
                title="会话",
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


@pytest.fixture()
def client(harness):
    with TestClient(
        harness.app, headers={"Authorization": f"Bearer {harness.token}"}
    ) as test_client:
        yield test_client


def _send(client, conversation) -> object:
    return client.post(
        f"/api/conversations/{conversation.id}/messages", json={"text": "你好"}
    )


def test_send_message_returns_400_auth_required_with_the_login_command(
    harness, client
) -> None:
    """不是 503「引擎没能起来」，而是 400「你还没登录，去终端跑这一句」。"""
    conversation = asyncio.run(harness.new_conversation())
    response = _send(client, conversation)
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "auth_required"
    assert body["message"] == "Fake Engine 还没有登录"
    assert body["detail"]["hint"] == LOGIN_HINT
    assert body["detail"]["cause"] == "auth_required"


def test_the_error_carries_the_engine_reported_auth_methods(harness, client) -> None:
    """``authMethods`` 只有 id，用来说清「去登哪一种」；一个值都不带。"""
    conversation = asyncio.run(harness.new_conversation())
    detail = _send(client, conversation).json()["error"]["detail"]
    assert detail["authMethods"] == list(AUTH_METHODS)


def test_the_failed_turn_leaves_a_run_failed_in_the_timeline(harness, client) -> None:
    """时间线要**看得见**这次失败——真机上这里此前一条事件都没有。"""
    conversation = asyncio.run(harness.new_conversation())
    assert _send(client, conversation).status_code == 400
    envelopes = asyncio.run(harness.host.event_store.replay(conversation.id))
    failures = [e for e in envelopes if e.event.type == "run.failed"]
    assert len(failures) == 1
    error = failures[0].event.error
    assert error.code == "auth_required"
    assert LOGIN_HINT in error.message
    # 这一轮压根没开始，所以不编一个 runId 出来。
    assert failures[0].event.run_id is None


def test_the_conversation_is_reported_as_still_empty(harness, client) -> None:
    """一个字都没发出去 → 前端据此敢把这条空会话收掉（批次十一第 2 件的口径）。"""
    conversation = asyncio.run(harness.new_conversation())
    body = _send(client, conversation).json()["error"]
    # 我们自己补的那条 run.failed 不该把这个凭据变成假（顺序：先判空、后合成）。
    assert body["emptyConversation"] is True


def test_auth_required_is_a_known_group_delivery_reason() -> None:
    """组投递的 reason 集合认得它——不认得就会被前端渲染成一句原始错误文本。"""
    assert "auth_required" in DELIVERY_REASONS


def test_binding_status_carries_signed_out_and_the_login_hint(harness) -> None:
    """引擎卡的登录行：``auth.state = signed_out`` + 那条终端命令（AD-82 / AD-157）。

    读的是 :func:`app.api.binding_status.read_binding_status` ——``GET
    /api/bindings/{id}/status`` 与 bindings 列表共用的**那一处**取数，所以卡片与
    页头不可能各说各话。
    """
    from datetime import datetime, timezone

    from app.api.binding_status import read_binding_status
    from drivers.base import AuthState

    async def read_auth_state(binding):  # noqa: ANN001
        return AuthState(
            state="signed_out",
            model="own-auth",
            checked_at=datetime.now(tz=timezone.utc),
            hint=LOGIN_HINT,
        )

    harness.driver.read_auth_state = read_auth_state
    payload = asyncio.run(
        read_binding_status(harness.binding, registry=harness.registry)
    )
    assert payload["auth"]["state"] == "signed_out"
    assert payload["auth"]["model"] == "own-auth"
    assert payload["auth"]["hint"] == LOGIN_HINT
