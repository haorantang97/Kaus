"""批次二十六第 5 件：批次二十五留下的后端补项。

覆盖
----
1. ``GET /bindings/{id}/projection/_meta``：前端的门控信号，不再拿 drift 的成败当
   探测（含「没有投射面」与「Driver 没注册」两档）；
2. ``GET /bindings/{id}/projections``：``projection_results`` 终于有读出口；
3. ``ProjectionResult.warnings[]`` 与 ``unsupported.detail`` 上 wire 是纯文本人话；
4. ``binding_busy`` 的 ``detail`` 带 ``activeConversations``（含标题），
   原来的 ``activeConversationIds`` 一字未动；
5. ``PATCH /conversations/{id}`` 的模型下发面：被拒 → 400 ``model_rejected``；
   无活跃 Runtime → 只存快照（``appliedToRuntime=false``）。

隔离：SQLite 与 Mock Driver 的 home 都在 ``tmp_path``；不碰任何真实引擎，
不读任何凭据，不起任何真进程。
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.tests.test_batch24_projection_api import (  # noqa: E402
    TEST_TOKEN,
    Harness,
    _materialize,
    client,  # noqa: F401 - fixture
    harness,  # noqa: F401 - fixture
)
from drivers.base import (  # noqa: E402
    ModelRejectedError,
    ProjectionEntry,
    ProjectionResult,
    plain_text,
)


# --------------------------------------------------------------------------- #
# ① projection/_meta
# --------------------------------------------------------------------------- #


def test_projection_meta_reports_support_without_touching_any_file(client, harness):
    """有投射面 → ``supported: true``，且这次请求**没读没写任何配置文件**。"""
    response = client.get(f"/api/bindings/{harness.binding.id}/projection/_meta")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["bindingId"] == harness.binding.id
    assert payload["supported"] is True
    # 批次四十四：多一段工作目录信息。两个字段各自独立地可能为 null——
    # 「项目没填工作目录」与「这台引擎没有指令文件约定」是两件不同的事。
    assert payload["workspace"] == {"root": None, "instructionsFile": None}
    assert not harness.config_path().exists()


def test_projection_meta_reports_the_workspace_root_and_convention(client, harness):
    """批次四十四：界面要说「往哪个目录的哪个文件写」，两段各有各的来源。

    ``root`` 来自 Project（用户能改），``instructionsFile`` 来自 Driver（用户改
    不了）。文件名向 Driver 要而不是在接入层查目录——公共层不 import 任何一家的
    预设表（N §3）。
    """
    async def _prepare() -> None:
        project = harness.project.evolve(workspace_root="/tmp/kaus-workspace-demo")
        await harness.repositories.projects.save(project)

    asyncio.run(_prepare())
    harness.driver.workspace_conventions = lambda binding=None: {  # type: ignore[attr-defined]
        "instructionsFile": "DEMO-NOTES.md"
    }
    payload = client.get(
        f"/api/bindings/{harness.binding.id}/projection/_meta"
    ).json()
    assert payload["workspace"] == {
        "root": "/tmp/kaus-workspace-demo",
        "instructionsFile": "DEMO-NOTES.md",
    }


def test_projection_meta_names_the_missing_method(client, harness):
    """没有投射面时说出缺的是哪一个方法——而不是让前端去猜 drift 为什么 400。"""

    class _NoProjector:
        backend_id = harness.driver.backend_id
        driver_kind = "mock"

    class _Registry:
        def get(self, backend_id: str):
            return _NoProjector()

    from app.api.binding_router import build_binding_write_router
    from app.api.session_auth import SessionAuthPolicy

    app = fastapi.FastAPI()
    app.include_router(
        build_binding_write_router(
            harness.repositories,
            auth_policy=SessionAuthPolicy(lambda: TEST_TOKEN),
            session_host=harness.host,
            registry=_Registry(),
        )
    )
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as test:
        payload = test.get(
            f"/api/bindings/{harness.binding.id}/projection/_meta"
        ).json()
    assert payload["supported"] is False
    assert payload["missingMethod"] == "materialize_project_capabilities"
    assert payload["reason"] == "projection_unsupported"


def test_projection_meta_is_200_even_without_a_registry(tmp_path):
    """「这台引擎不支持」是一个正常答案，不是一次失败的请求（AD-71）。"""
    built = Harness(tmp_path, with_driver=False)
    asyncio.run(built.seed())
    try:
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        ) as test:
            response = test.get(
                f"/api/bindings/{built.binding.id}/projection/_meta"
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["supported"] is False
        assert payload["missingMethod"] is None
        assert payload["reason"] == "driver_not_registered"
    finally:
        built.close()


def test_projection_meta_404s_on_an_unknown_binding(client):
    assert client.get("/api/bindings/binding:nope/projection/_meta").status_code == 404


# --------------------------------------------------------------------------- #
# ② projections 列表
# --------------------------------------------------------------------------- #


def test_projections_list_reads_back_the_records(client, harness):
    """``confirm=1`` 各留一行，倒序读回；``limit`` 生效。"""
    empty = client.get(f"/api/bindings/{harness.binding.id}/projections").json()
    assert empty == {
        "bindingId": harness.binding.id,
        "records": [],
        "count": 0,
    }

    _materialize(client, harness, confirm="1")
    _materialize(client, harness, confirm="1")

    payload = client.get(f"/api/bindings/{harness.binding.id}/projections").json()
    assert payload["count"] == 2
    first = payload["records"][0]
    assert first["bindingId"] == harness.binding.id
    assert first["projectId"] == harness.project.id
    assert first["appliedAt"].endswith("Z")
    # 这张表只存摘要不存值（§5.4）：有键路径与计数，没有 before/after。
    assert harness.key_path() in first["summary"]["keyPaths"]
    assert "before" not in str(first["summary"])

    capped = client.get(
        f"/api/bindings/{harness.binding.id}/projections", params={"limit": 1}
    ).json()
    assert capped["count"] == 1


# --------------------------------------------------------------------------- #
# ③ 纯文本人话
# --------------------------------------------------------------------------- #


def test_projection_text_is_plain_prose_on_the_wire():
    """warnings 与 unsupported.detail 上 wire 前去掉 Markdown 与异常类名。

    这两处会直接出现在界面的一段普通文字里——不是 Markdown 渲染区。所以
    ``**一个字节都没写**`` 在用户眼里就是四个星号，``FileNotFoundError: …``
    则是一句他既看不懂也没法据此行动的话。
    """
    result = ProjectionResult(
        binding_id="binding:x",
        warnings=(
            "dry-run：算出 3 处改动，**一个字节都没写**。要真写请带 `?confirm=1`。",
            "   ",
        ),
        unsupported=(
            ProjectionEntry(
                capability_type="mcp",
                capability_id="demo",
                level="native",
                detail="OSError: 这个键当前有值，且不在 `.kaus-projected.json` 的登记里",
            ),
        ),
    )
    assert result.warnings == (
        "dry-run：算出 3 处改动，一个字节都没写。要真写请带 ?confirm=1。",
    )
    detail = result.unsupported[0].detail
    assert detail == "这个键当前有值，且不在 .kaus-projected.json 的登记里"
    assert "*" not in detail and "`" not in detail and "OSError" not in detail


def test_plain_text_only_subtracts():
    assert plain_text("**粗体** 与 `代码`") == "粗体 与 代码"
    assert plain_text("ValueError：出事了") == "出事了"
    assert plain_text("  多  个   空白 ") == "多 个 空白"
    # 名字里带 Error 但不是「类名: 消息」形状的，一个字都不该被吃掉。
    assert plain_text("Error 码是 42") == "Error 码是 42"


def test_real_projection_warnings_carry_no_markup(client, harness):
    """走一次真的 dry-run：它产生的 warning 也是纯文本。"""
    payload = _materialize(client, harness).json()
    warnings = payload["result"]["warnings"]
    assert warnings and all("**" not in w and "`" not in w for w in warnings)


# --------------------------------------------------------------------------- #
# ④ binding_busy 带标题
# --------------------------------------------------------------------------- #


def test_binding_busy_names_the_conversations(client, harness):
    """只给 id 的话，界面上是一串 ``conversation:…``，用户不知道该停哪一条。"""
    conversation = asyncio.run(
        harness.repositories.conversations.save(
            _a_conversation(harness, "正在跑的那条")
        )
    )
    harness.host.active.add(conversation.id)
    response = _materialize(client, harness, confirm="1")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "binding_busy"
    # 既有形状一字未动……
    assert error["activeConversationIds"] == [conversation.id]
    # ……新键多带一个标题。
    assert error["activeConversations"] == [
        {"conversationId": conversation.id, "title": "正在跑的那条"}
    ]
    # dry-run 仍然不受此限（批次二十四的口径没变）。
    assert _materialize(client, harness).status_code == 200


def _a_conversation(harness, title: str):
    from app.conversations.models import Conversation

    return Conversation.create(
        project_id=harness.project.id,
        agent_binding_id=harness.binding.id,
        title=title,
    )


# --------------------------------------------------------------------------- #
# ⑤ 模型下发面
# --------------------------------------------------------------------------- #


def _session_harness(tmp_path, *, on_set_model=None):
    """会话路由 + 一个带 ``set_conversation_model`` 的 MockDriver。"""
    from app.tests.test_batch16_backend import Harness as SessionHarness

    built = SessionHarness(tmp_path)
    if on_set_model is not None:
        built.driver.set_conversation_model = on_set_model  # type: ignore[attr-defined]
    asyncio.run(built.seed())
    return built


def test_patch_conversation_without_a_runtime_only_stores_the_snapshot(tmp_path):
    """没有活跃 Runtime 时不算失败：下一次 ``session/new`` 会带上它。"""
    calls: list[str] = []

    async def _never(handle, model_id):  # pragma: no cover - 断言它不会被调用
        calls.append(model_id)

    built = _session_harness(tmp_path, on_set_model=_never)
    try:
        conversation = asyncio.run(built.new_conversation())
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test:
            response = test.patch(
                f"/api/conversations/{conversation.id}", json={"modelId": "mock-large"}
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["conversation"]["modelId"] == "mock-large"
        assert payload["appliedToRuntime"] is False
        assert calls == []
    finally:
        built.close()


def test_patch_conversation_pushes_the_model_to_a_live_runtime(tmp_path):
    seen: list[tuple[str, str]] = []

    async def _accept(handle, model_id):
        seen.append((handle.conversation_id, model_id))
        return model_id

    built = _session_harness(tmp_path, on_set_model=_accept)
    try:
        conversation = asyncio.run(built.new_conversation())
        asyncio.run(built.host.ensure_runtime(conversation))
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test:
            payload = test.patch(
                f"/api/conversations/{conversation.id}", json={"modelId": "mock-large"}
            ).json()
        assert payload["appliedToRuntime"] is True
        assert seen == [(conversation.id, "mock-large")]
    finally:
        built.close()


def test_patch_conversation_reports_a_rejected_model_as_400(tmp_path):
    """被拒 → 400 ``model_rejected`` 并带上 agent 那句话；**快照不写**。"""

    async def _reject(handle, model_id):
        raise ModelRejectedError(
            f"引擎拒绝了模型 {model_id!r}", reason="unknown model: mock-large"
        )

    built = _session_harness(tmp_path, on_set_model=_reject)
    try:
        conversation = asyncio.run(built.new_conversation())
        asyncio.run(built.host.ensure_runtime(conversation))
        with TestClient(
            built.app, headers={"Authorization": f"Bearer {built.token}"}
        ) as test:
            response = test.patch(
                f"/api/conversations/{conversation.id}", json={"modelId": "mock-large"}
            )
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == "model_rejected"
        assert error["detail"]["agentReason"] == "unknown model: mock-large"
        assert error["detail"]["modelId"] == "mock-large"
        # 400 说的是「这次没改成」，所以库里那条快照必须还是原样。
        stored = asyncio.run(
            built.repositories.conversations.get(conversation.id)
        )
        assert stored.model_id != "mock-large"
    finally:
        built.close()
