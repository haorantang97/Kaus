"""起会话真的起在项目的工作目录里（批次五十四 PJ-01 / AD-175，端到端）。

这一份是真机 K5 的自动化等价物。K5 那天的链条是：用户在**项目**上设了工作目录 →
指令被写进了那个目录里的指令文件 → 新建会话 → 问引擎「你的项目指令里有什么」→
引擎答的是**另一个目录**里那份指令。原因不在文件那一头，在起会话这一头：

1. 接入层灌给 Driver 的 Binding 快照上没有 ``workspace_root``（用户设在项目上）；
2. 就算有，``start_runtime`` 也没把它交给 ``session/new``——``_new_session`` 按
   ``options.workspace_root`` 自己算 cwd，而建会话这条路上谁都没往里填。

两处都修完之后，这条用例断言的是**线上真的发出去的那个 ``cwd``**：假 agent 是真的
子进程，参数是真的 JSON-RPC 报文。不 mock 任何一层，只在连接上搭一个旁听。

隔离：目录全在 ``tmp_path``，不碰任何真实目录、不读任何凭据。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.conversations.models import Conversation
from app.projects.binding_snapshot import register_binding_snapshot
from app.projects.models import AgentBinding, Project
from drivers.acp import client as acp_client
from drivers.acp.driver import AcpDriver
from drivers.acp.testing.fake_acp_agent import fake_agent_spec

SLUG = "batch54"


class _Projects:
    """够用的 ``ProjectRepository``：接入层只问它一句 ``get``。"""

    def __init__(self, project: Project) -> None:
        self._project = project

    async def get(self, project_id: str) -> Project | None:
        return self._project if project_id == self._project.id else None


def _driver(default_cwd: str) -> AcpDriver:
    return AcpDriver(
        fake_agent_spec("text-stream", cwd=default_cwd),
        backend_key="acp",
        # 「后端进程从哪起」——真机上它就是那个让 K5 走错的目录。
        default_cwd=default_cwd,
        call_timeout=20.0,
        prompt_timeout=30.0,
        cancel_grace=1.5,
    )


def _spy(monkeypatch) -> list[tuple[str, object]]:
    """在连接上搭一个旁听，原样留下每一次 RPC 的方法名与参数。"""
    seen: list[tuple[str, object]] = []
    original = acp_client.AcpConnection.call

    async def call(self, method, params=None, *, timeout=None):  # noqa: ANN001
        seen.append((method, params))
        return await original(self, method, params, timeout=timeout)

    monkeypatch.setattr(acp_client.AcpConnection, "call", call)
    return seen


def _new_session_cwd(seen: list[tuple[str, object]]) -> str | None:
    for method, params in seen:
        if method == "session/new" and isinstance(params, dict):
            return params.get("cwd")
    return None


async def _start_and_collect(
    monkeypatch,
    *,
    default_cwd: Path,
    project_workspace: str | None,
    binding_workspace: str | None,
) -> str | None:
    driver = _driver(str(default_cwd))
    project = Project.create(
        slug=SLUG, display_name="批次五十四", workspace_root=project_workspace
    )
    runtime_config: dict[str, object] = {"approval_mode": "ask"}
    if binding_workspace is not None:
        runtime_config["workspace_root"] = binding_workspace
    binding = AgentBinding.create(
        project=project, backend="acp", runtime_config=runtime_config
    )
    # AD-58 的那一次灌入，走的正是接入层六处调用点共用的那个函数。
    await register_binding_snapshot(driver, binding, _Projects(project))
    conversation = Conversation.create(
        project_id=project.id, agent_binding_id=binding.id, title="会话"
    )
    seen = _spy(monkeypatch)
    runtime = await driver.start_runtime(conversation, "card")
    try:
        return _new_session_cwd(seen)
    finally:
        await driver.stop_runtime(runtime)


async def test_session_new_gets_the_project_workspace(tmp_path, monkeypatch) -> None:
    """项目设了工作目录、Binding 没设 → ``session/new`` 的 ``cwd`` 就是它。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    cwd = await _start_and_collect(
        monkeypatch,
        default_cwd=elsewhere,
        project_workspace=str(workspace),
        binding_workspace=None,
    )
    assert cwd == os.path.realpath(str(workspace))
    assert cwd != os.path.realpath(str(elsewhere)), "又掉回后端进程的启动目录了"


async def test_the_binding_workspace_still_wins(tmp_path, monkeypatch) -> None:
    """Binding 上那一个更具体（AD-152），项目的只是回落。"""
    project_workspace = tmp_path / "project"
    project_workspace.mkdir()
    binding_workspace = tmp_path / "binding"
    binding_workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    cwd = await _start_and_collect(
        monkeypatch,
        default_cwd=elsewhere,
        project_workspace=str(project_workspace),
        binding_workspace=str(binding_workspace),
    )
    assert cwd == os.path.realpath(str(binding_workspace))


async def test_without_any_workspace_it_falls_back_to_the_process_cwd(
    tmp_path, monkeypatch
) -> None:
    """两边都没设：仍然是启动目录。

    这不是本批要改的事——``cwd`` 是 ``session/new`` 的必填参数，总得给一个。
    留这一条是为了把「回落确实还在」钉住：上面两条断言的是**不再**走到这里。
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    cwd = await _start_and_collect(
        monkeypatch,
        default_cwd=elsewhere,
        project_workspace=None,
        binding_workspace=None,
    )
    assert cwd == os.path.abspath(str(elsewhere))
