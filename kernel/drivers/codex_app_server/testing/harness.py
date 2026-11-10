"""``FakeAppServerHarness``: the shared contract suite on a real fake app-server child.

Scenarios the protocol cannot express are declared unsupported here instead of
being faked: ``streaming-tool-output`` (no cumulative snapshot before
``item/completed``), ``streaming-reasoning`` (no reasoning *status* event),
``authentication`` (login lives in the CLI), ``delegated-run`` (sub-agents are
items of the parent turn), ``external-http-shaped``.
"""

from __future__ import annotations

import tempfile

from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.base import BackendDriver
from drivers.codex_app_server.driver import CodexAppServerDriver
from drivers.codex_app_server.testing.fake_app_server import fake_app_server_spec
from drivers.contract_tests.harness import ContractScenario

SCENARIO_SCRIPTS: dict[ContractScenario, str] = {
    ContractScenario.TEXT_STREAM: "text-stream",
    ContractScenario.TOOL_LIFECYCLE: "tool-lifecycle",
    ContractScenario.PERMISSION: "permission",
    ContractScenario.QUESTION: "question",
    ContractScenario.INTERRUPT: "interrupt",
    ContractScenario.FAILURE: "failure",
    ContractScenario.EXTENSION: "extension",
    ContractScenario.FULL_LIFECYCLE: "full-lifecycle",
}


class FakeAppServerHarness:
    def __init__(self, backend_key: str = "codex-app-server-test", *, workspace_root: str | None = None) -> None:
        self.backend_key = backend_key
        self.workdir = workspace_root or tempfile.gettempdir()

    def make_driver(self, scenario: str = "text-stream") -> BackendDriver:
        return CodexAppServerDriver(
            fake_app_server_spec(scenario, cwd=self.workdir),
            backend_key=self.backend_key, default_cwd=self.workdir, call_timeout=20.0,
            turn_timeout=30.0, interrupt_grace=3.0, native_history=False,
        )

    def make_project(self, slug: str = "contract") -> Project:
        return Project.create(slug=slug, display_name=slug, workspace_root=self.workdir)

    def make_binding(self, project: Project, driver: BackendDriver, *, discriminator: str | None = None) -> AgentBinding:
        binding = AgentBinding.create(project=project, backend=driver.backend_id, discriminator=discriminator,
                                     native_scope_ref=f"scope-{discriminator or 'primary'}", is_default=discriminator is None,
                                     runtime_config={"workspace_root": self.workdir})
        driver.register_binding(binding)
        return binding

    def make_conversation(self, project: Project, binding: AgentBinding, *, title: str = "contract") -> Conversation:
        return Conversation.create(project_id=project.id, agent_binding_id=binding.id, title=title)

    def make_foreign_binding(self, project: Project) -> AgentBinding | None:
        return AgentBinding.create(project=project, backend="backend:someone-else", native_scope_ref="foreign")

    def supports(self, scenario: ContractScenario) -> bool:
        return scenario in SCENARIO_SCRIPTS

    async def arrange(self, driver: BackendDriver, conversation: Conversation, scenario: ContractScenario) -> None:
        del conversation
        driver.agent_spec = fake_app_server_spec(SCENARIO_SCRIPTS[scenario], cwd=self.workdir)


__all__ = ["FakeAppServerHarness", "SCENARIO_SCRIPTS"]
