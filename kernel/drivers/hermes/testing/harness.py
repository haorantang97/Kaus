"""``FakeHermesHarness``：让通用契约套件跑 :class:`~drivers.hermes.driver.HermesDriver`。

形状照抄 ``drivers/mock/harness.py``——套件本身一行都不用改，这正是 N §13
「所有 Driver 至少接受相同的契约测试」在工程上的落法。

``supports()`` 里的每一个 ``False`` 都是**规格里写明的降级**，不是偷懒
--------------------------------------------------------------------
=========================== =================================================
``TOOL_LIFECYCLE``          套件要求至少一条 ``tool.updated``；这条 SSE 只有
                            ``tool.started`` / ``tool.completed``（规格 §3.2-A）
``STREAMING_TOOL_OUTPUT``   同上，且 ``tool.completed`` 不带输出（AD-38 的场景）
``STREAMING_REASONING``     ``reasoning.available`` 的载荷语义未定案（§8-③）；
                            套件要求「≥2 片 delta + 至少一条 status 并存」，
                            后端给不出这个形状
``PERMISSION``              审批闭环未验证（§8-② / AD-34）。能力仍声明 true
                            （后端两项 feature 都是 true），但**不拿未实测的
                            载荷去跑 happy path**
``QUESTION`` /
``AUTHENTICATION``          ``card.questions`` / ``card.authentication`` = false
``DELEGATED_RUN``           ``subagent.*`` 目前走 ``extension.event``（规格 §3.5）
=========================== =================================================
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.base import BackendDriver
from drivers.contract_tests.harness import ContractScenario
from drivers.hermes.driver import HermesDriver
from drivers.hermes.supervisor import GatewayConfig
from drivers.hermes.testing.fake_api_server import (
    FakeHermesApiServer,
    RunScript,
    extension_script,
    failure_script,
    hold_script,
    run3_script,
    text_only_script,
)

SCENARIO_SCRIPTS: dict[ContractScenario, Callable[[], RunScript]] = {
    ContractScenario.TEXT_STREAM: text_only_script,
    ContractScenario.INTERRUPT: hold_script,
    ContractScenario.FAILURE: failure_script,
    ContractScenario.EXTENSION: extension_script,
    ContractScenario.FULL_LIFECYCLE: run3_script,
    ContractScenario.EXTERNAL_HTTP_SHAPED: run3_script,
}


class FakeHermesHarness:
    """契约夹具：每条 Binding 一台假 gateway（= 一个 ``HERMES_HOME``）。

    「每条 Binding 一台」不是为了省事，而是**照着真实拓扑来**：规格 §1.1 说一个
    ``HERMES_HOME`` 至多一个 gateway 进程，而不同 profile 各有自己的 home、端口与
    key。套件的「多 Binding 数据隔离」因此测的是真东西，不是内存里分个字典。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = Path(tmp_path)
        self.servers: dict[str, FakeHermesApiServer] = {}
        self._drivers: list[HermesDriver] = []
        self._primary: FakeHermesApiServer | None = None

    # --- 生命周期 ------------------------------------------------------ #

    def close(self) -> None:
        for server in self.servers.values():
            server.stop()
        self.servers.clear()

    def server_for(self, profile: str) -> FakeHermesApiServer:
        server = self.servers.get(profile)
        if server is None:
            server = FakeHermesApiServer().start()
            self.servers[profile] = server
            home = self._home_for(profile)
            home.mkdir(parents=True, exist_ok=True)
            # key 只经 credential_ref 取得（规格 §1.3 B1）：写进沙盒 .env。
            (home / ".env").write_text(
                f"API_SERVER_ENABLED=true\nAPI_SERVER_KEY={server.api_key}\n",
                encoding="utf-8",
            )
            if self._primary is None:
                self._primary = server
        return server

    def _home_for(self, profile: str) -> Path:
        root = self.tmp_path / "hermes-root"
        return root if profile == "default" else root / "profiles" / profile

    # --- 构造被测对象 --------------------------------------------------- #

    def make_driver(self) -> BackendDriver:
        primary_profile = "default"
        server = self.server_for(primary_profile)
        static_path = self.tmp_path / "model-options.json"
        if not static_path.exists():
            static_path.write_text(
                json.dumps(
                    {
                        "deepseek-v4-flash": {
                            "default": "deepseek-v4-flash",
                            "provider": "deepseek",
                            "family": "DeepSeek",
                            "tier": "V4 Flash",
                            "context_window": 128000,
                            "reasoning_levels": ["low", "medium", "high"],
                            "fast_mode": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
        driver = HermesDriver(
            hermes_root=self.tmp_path / "hermes-root",
            default_gateway=GatewayConfig(
                hermes_home=self._home_for(primary_profile),
                port=server.port,
                key_ref=f"hermes-env:{self._home_for(primary_profile)}/.env#API_SERVER_KEY",
                mode="adopted",
            ),
            static_catalog_path=static_path,
            hermes_bin="hermes",
        )
        self._drivers.append(driver)
        return driver

    def make_project(self, slug: str = "contract") -> Project:
        return Project.create(
            slug=slug, display_name=slug, workspace_root=str(self.tmp_path / "work")
        )

    def make_binding(
        self,
        project: Project,
        driver: BackendDriver,
        *,
        discriminator: str | None = None,
    ) -> AgentBinding:
        profile = discriminator or "default"
        server = self.server_for(profile)
        home = self._home_for(profile)
        binding = AgentBinding.create(
            project=project,
            backend=driver.backend_id,
            discriminator=discriminator,
            native_scope_ref=profile,
            is_default=discriminator is None,
            runtime_config={
                # 规格 §1.3 规则 1：这一段里只许出现这六个键，且**没有** key 明文。
                "api_server": {
                    "host": "127.0.0.1",
                    "port": server.port,
                    "profile": profile,
                    "hermes_home": str(home),
                    "key_ref": f"hermes-env:{home}/.env#API_SERVER_KEY",
                    "mode": "adopted",
                }
            },
        )
        if isinstance(driver, HermesDriver):
            driver.register_binding(binding)
        return binding

    def make_conversation(
        self, project: Project, binding: AgentBinding, *, title: str = "contract"
    ) -> Conversation:
        return Conversation.create(
            project_id=project.id, agent_binding_id=binding.id, title=title
        )

    def make_foreign_binding(self, project: Project) -> AgentBinding:
        return AgentBinding.create(
            project=project, backend="other-agent", native_scope_ref="scope-foreign"
        )

    # --- 场景 ---------------------------------------------------------- #

    def supports(self, scenario: ContractScenario) -> bool:
        return scenario in SCENARIO_SCRIPTS

    async def arrange(
        self,
        driver: BackendDriver,
        conversation: Conversation,
        scenario: ContractScenario,
    ) -> None:
        del conversation
        assert isinstance(driver, HermesDriver)  # noqa: S101 - 夹具内部约束
        factory = SCENARIO_SCRIPTS[scenario]
        for server in self.servers.values():
            server.set_script(factory)


__all__ = ["SCENARIO_SCRIPTS", "FakeHermesHarness"]
