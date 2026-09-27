"""契约测试的 Driver 无关夹具接口与通用工具。

为什么需要 harness
------------------
N §13 的契约要求覆盖「工具生命周期」「Permission/Question round trip」
「Cancel」「Error」等场景，但**如何让某个 Backend 进入这些场景**是各家自己的事
（Mock 靠脚本，真实 Driver 可能靠特定 prompt 或沙箱夹具）。
因此套件通过 :class:`DriverContractHarness` 向 Driver 侧要两样东西：

1. 构造被测对象（driver / project / binding / conversation）；
2. ``arrange(scenario)``：把 Driver 置于产生该场景的状态；
   ``supports(scenario)`` 返回 False 时该用例被 skip——这正是 N §13.1
   「不支持项返回明确状态，不使用空对象伪装支持」在测试侧的表达。

对应规范
--------
- N §13 Backend Contract Test 基线（13.1–13.4；13.5 属前端，不在 Python 套件内）。
- v1.0 §16.3：所有 Backend 必须通过同一套测试。
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Awaitable, Callable, Protocol, runtime_checkable

from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Project
from drivers.base import BackendDriver, RuntimeHandle
from runtime.event_envelope import TERMINAL_RUN_EVENT_TYPES, AgentEventEnvelope


class ContractScenario(str, Enum):
    """契约测试需要 Driver 能进入的运行场景（N §13.4）。"""

    TEXT_STREAM = "text-stream"
    TOOL_LIFECYCLE = "tool-lifecycle"
    #: AD-27：tool.updated 的增量/全量输出（cumulative）。
    STREAMING_TOOL_OUTPUT = "streaming-tool-output"
    #: AD-27：reasoning.delta 流式思考。
    STREAMING_REASONING = "streaming-reasoning"
    PERMISSION = "permission"
    QUESTION = "question"
    #: AD-08：认证请求-响应闭环。
    AUTHENTICATION = "authentication"
    #: AD-08：委派 / 子 run（信封头 parentRunId）。
    DELEGATED_RUN = "delegated-run"
    INTERRUPT = "interrupt"
    FAILURE = "failure"
    EXTENSION = "extension"
    FULL_LIFECYCLE = "full-lifecycle"
    #: AD-32：把 live 探针实测到的外部 HTTP/SSE 事件流翻成公共事件后重放。
    EXTERNAL_HTTP_SHAPED = "external-http-shaped"


@runtime_checkable
class DriverContractHarness(Protocol):
    """Driver 侧为契约套件提供的夹具。"""

    def make_driver(self) -> BackendDriver:
        """构造一个全新的、彼此独立的 Driver 实例。"""
        ...

    def make_project(self, slug: str = "contract") -> Project: ...

    def make_binding(
        self, project: Project, driver: BackendDriver, *, discriminator: str | None = None
    ) -> AgentBinding:
        """为该 Driver 的 Backend 造一条 Binding；``discriminator`` 支持 R-09 多 Binding。"""
        ...

    def make_conversation(
        self, project: Project, binding: AgentBinding, *, title: str = "contract"
    ) -> Conversation: ...

    def make_foreign_binding(self, project: Project) -> AgentBinding | None:
        """造一条**属于别的 Backend** 的 Binding，用来验证 N §13.2 的数据隔离。

        返回 ``None`` 表示该环境下无法构造外来 Binding，对应用例会被 skip。
        """
        ...

    def supports(self, scenario: ContractScenario) -> bool:
        """该 Driver 是否能进入这个场景。返回 False 时对应用例被 skip。"""
        ...

    async def arrange(
        self,
        driver: BackendDriver,
        conversation: Conversation,
        scenario: ContractScenario,
    ) -> None:
        """把 Driver 置于「下一次 send_message 会产生该场景」的状态。"""
        ...


InteractionHandler = Callable[[AgentEventEnvelope], Awaitable[None]]

_INTERACTION_TYPES = frozenset(
    {"permission.requested", "question.requested", "authentication.requested"}
)


async def collect_until_run_terminal(
    driver: BackendDriver,
    runtime: RuntimeHandle,
    *,
    on_interaction: InteractionHandler | None = None,
    timeout: float = 5.0,
) -> tuple[AgentEventEnvelope, ...]:
    """消费事件流直到**顶层** run 出现终态事件（``completed`` / ``interrupted`` / ``failed``）。

    ``on_interaction`` 在收到交互请求时被调用，用来完成请求-响应闭环；
    因为闭环发生在同一个事件循环里，回调里 ``await resolve_interaction`` 会让
    Driver 继续推进剧本，随后的事件仍由本循环收集。

    AD-08：带 ``parentRunId`` 的终态事件属于**子 run**，不结束本轮收集——
    否则一个委派出去的子任务先跑完，收集就会在父 run 还在跑的时候提前返回。
    """
    collected: list[AgentEventEnvelope] = []

    async def _loop() -> None:
        async for envelope in driver.events(runtime):
            collected.append(envelope)
            event_type = envelope.event.type
            if event_type in _INTERACTION_TYPES and on_interaction is not None:
                await on_interaction(envelope)
            if event_type in TERMINAL_RUN_EVENT_TYPES and not envelope.is_child_run:
                return

    await asyncio.wait_for(_loop(), timeout)
    return tuple(collected)


async def collect_in_background(
    driver: BackendDriver,
    runtime: RuntimeHandle,
    *,
    on_interaction: InteractionHandler | None = None,
    timeout: float = 5.0,
) -> "asyncio.Task[tuple[AgentEventEnvelope, ...]]":
    """在后台收集事件，供 interrupt 这类需要并发操作的场景使用。"""
    return asyncio.create_task(
        collect_until_run_terminal(
            driver, runtime, on_interaction=on_interaction, timeout=timeout
        )
    )


def events_of_type(
    envelopes: tuple[AgentEventEnvelope, ...], event_type: str
) -> tuple[AgentEventEnvelope, ...]:
    return tuple(e for e in envelopes if e.event.type == event_type)


__all__ = [
    "ContractScenario",
    "DriverContractHarness",
    "InteractionHandler",
    "collect_in_background",
    "collect_until_run_terminal",
    "events_of_type",
]
