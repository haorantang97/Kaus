"""MockDriver 的契约测试夹具实现。

把 :class:`~drivers.contract_tests.harness.ContractScenario` 映射到
:mod:`drivers.mock.fixtures` 里的剧本，让通用契约套件可以直接跑 MockDriver。

真实 Driver 接入时照抄这个文件的形状即可（换掉 ``arrange`` 的实现），
套件本身一行都不用改——这正是 N §13「所有 Driver 至少接受相同的契约测试」
在工程上的落法。
"""

from __future__ import annotations

from typing import Callable

from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Backend, Project
from drivers.base import BackendDriver, ModelDescriptor
from drivers.contract_tests.harness import ContractScenario
from drivers.mock.driver import MockDriver
from drivers.mock.capabilities import LEAN_CAPABILITIES
from runtime.capability_matrix import BackendCapabilities
from drivers.mock.fixtures import (
    MockScript,
    authentication_roundtrip_script,
    delegated_run_script,
    extension_event_script,
    external_http_shaped_script,
    failure_script,
    full_lifecycle_script,
    hold_script,
    permission_roundtrip_script,
    question_roundtrip_script,
    streaming_reasoning_script,
    streaming_tool_output_script,
    text_stream_script,
    tool_lifecycle_script,
)

#: 场景 → 剧本工厂。
SCENARIO_SCRIPTS: dict[ContractScenario, Callable[[], MockScript]] = {
    ContractScenario.TEXT_STREAM: text_stream_script,
    ContractScenario.TOOL_LIFECYCLE: tool_lifecycle_script,
    ContractScenario.STREAMING_TOOL_OUTPUT: streaming_tool_output_script,
    ContractScenario.STREAMING_REASONING: streaming_reasoning_script,
    ContractScenario.PERMISSION: permission_roundtrip_script,
    ContractScenario.QUESTION: question_roundtrip_script,
    ContractScenario.AUTHENTICATION: authentication_roundtrip_script,
    ContractScenario.DELEGATED_RUN: delegated_run_script,
    ContractScenario.INTERRUPT: hold_script,
    ContractScenario.FAILURE: failure_script,
    ContractScenario.EXTENSION: extension_event_script,
    ContractScenario.FULL_LIFECYCLE: full_lifecycle_script,
    ContractScenario.EXTERNAL_HTTP_SHAPED: external_http_shaped_script,
}

#: batch53：这些场景的 run id **不归夹具管**。
#:
#: ``external-http-shaped`` 的 run id 来自被重放的那条外部 HTTP 报文
#: （``POST`` 的响应体），剧本只是把它原样搬过来——夹具在这里换一个 id，换掉的
#: 就是「这条外部流能无损映射」这件事本身的证据。其余场景的 run id 是夹具自己
#: 编的，所以由 :meth:`MockDriverContractHarness.arrange` 每次现编一个新的。
SCENARIOS_WITH_FOREIGN_RUN_ID: frozenset[ContractScenario] = frozenset(
    {ContractScenario.EXTERNAL_HTTP_SHAPED}
)


LEAN_MODELS = (
    ModelDescriptor(model_id="mock-only", display_name="Mock Only", context_window=8_000),
)


class MockDriverContractHarness:
    """:class:`~drivers.contract_tests.harness.DriverContractHarness` 的 Mock 实现。

    ``capabilities`` 为 ``None`` 时用 MockDriver 的默认全能力声明；传
    :data:`LEAN_CAPABILITIES` 则得到一个「能力受限」的 Backend，用来跑通
    契约套件的 skip / raise 分支。
    """

    def __init__(
        self,
        backend_key: str = "mock",
        *,
        capabilities: BackendCapabilities | None = None,
        models: tuple[ModelDescriptor, ...] | None = None,
    ) -> None:
        self.backend_key = backend_key
        self.capabilities = capabilities
        self.models = models
        #: batch53：``arrange`` 叫到第几次了——用来给每次编排现编一个新的 run id。
        self._arrangements = 0
        #: 契约套件跑完写回来的取证等级（见 :meth:`record_bench_verification`）。
        self.benched_capabilities: BackendCapabilities | None = None

    # --- 构造被测对象 -------------------------------------------------------- #

    def record_bench_verification(self, capabilities: BackendCapabilities) -> None:
        """接住契约套件的取证写回（可选钩子）。

        套件跑完会把「假引擎实测过」的项标成 ``verification="bench"``，交给夹具
        留存。Mock 把它记在这里，测试据此断言写回真的发生了；真实 Driver 的夹具
        可以把它落到 Backend 仓库上（那才是声明的长期落点）。
        """
        self.benched_capabilities = capabilities

    def make_driver(self) -> BackendDriver:
        kwargs: dict[str, object] = {"backend_key": self.backend_key}
        if self.capabilities is not None:
            kwargs["capabilities"] = self.capabilities
        if self.models is not None:
            kwargs["models"] = self.models
        return MockDriver(**kwargs)  # type: ignore[arg-type]

    def make_backend(self) -> Backend:
        return Backend.create(
            key=self.backend_key, driver_kind="mock", installed=True, version="0.0.1-mock"
        )

    def make_project(self, slug: str = "contract") -> Project:
        return Project.create(slug=slug, display_name=slug, workspace_root="/tmp/contract")

    def make_binding(
        self,
        project: Project,
        driver: BackendDriver,
        *,
        discriminator: str | None = None,
    ) -> AgentBinding:
        return AgentBinding.create(
            project=project,
            backend=driver.backend_id,
            discriminator=discriminator,
            native_scope_ref=f"scope-{discriminator or 'primary'}",
            is_default=discriminator is None,
        )

    def make_conversation(
        self, project: Project, binding: AgentBinding, *, title: str = "contract"
    ) -> Conversation:
        return Conversation.create(
            project_id=project.id,
            agent_binding_id=binding.id,
            title=title,
        )

    def make_foreign_binding(self, project: Project) -> AgentBinding:
        """另一套 Backend key 的 Binding，用于 N §13.2 的跨 Backend 隔离检查。"""
        foreign_key = f"{self.backend_key}-other"
        return AgentBinding.create(
            project=project,
            backend=foreign_key,
            native_scope_ref="scope-foreign",
        )

    # --- 场景 ---------------------------------------------------------------- #

    def supports(self, scenario: ContractScenario) -> bool:
        return scenario in SCENARIO_SCRIPTS

    async def arrange(
        self,
        driver: BackendDriver,
        conversation: Conversation,
        scenario: ContractScenario,
    ) -> None:
        """把剧本装进 Driver。

        batch53 / AD-174：**每次编排都现编一个新的 run id**。此前所有剧本都用
        写死的默认值（``run-text`` 之类），于是同一条 Conversation 编排两次会
        播出两条同名的 ``run.started``——那正是 MV-01 在真机上的形状，而一个用来
        验收「run id 不许重」的夹具自己不该这么演。run id 由后端给，夹具就是这
        里的「后端」。

        例外见 :data:`SCENARIOS_WITH_FOREIGN_RUN_ID`。
        """
        script_factory = SCENARIO_SCRIPTS[scenario]
        self._arrangements += 1
        kwargs: dict[str, str] = {}
        if scenario not in SCENARIOS_WITH_FOREIGN_RUN_ID:
            kwargs["run_id"] = f"run-{scenario.value}-{self._arrangements}"
            if scenario is ContractScenario.DELEGATED_RUN:
                kwargs["child_run_id"] = f"run-{scenario.value}-child-{self._arrangements}"
        script: MockScript = script_factory(**kwargs)  # type: ignore[arg-type]
        assert isinstance(driver, MockDriver)  # noqa: S101 - 夹具内部约束
        driver.set_script(conversation.id, script)


__all__ = [
    "LEAN_CAPABILITIES",
    "LEAN_MODELS",
    "MockDriverContractHarness",
    "SCENARIOS_WITH_FOREIGN_RUN_ID",
    "SCENARIO_SCRIPTS",
]
